"""Opt-in Team OIDC with explicit identity linking and one-time browser handoff.

Only active, already-invited members can link identities. Email addresses never
select a local account. Provider hosts are explicitly trusted by the operator.
"""

import asyncio
import base64
import hashlib
import ipaddress
import json
import os
import secrets
import socket
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode, urlsplit
from uuid import UUID, uuid4

import account_email
import db
import httpx
import jwt
import security
import teams
from cryptography.fernet import Fernet, InvalidToken
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, ConfigDict, Field

TTL = timedelta(minutes=10)
ALGORITHMS = ("RS256", "ES256")
start_by_client = account_email._Window(20, 10 * 60)
exchange_by_client = account_email._Window(60, 10 * 60)


def _fernet() -> Fernet:
    try:
        return Fernet(os.environ["SSO_ENCRYPTION_KEY"].encode())
    except (KeyError, ValueError) as exc:
        raise HTTPException(503, "Team SSO encryption is not configured") from exc


def _guard() -> None:
    teams._guard_enabled()
    if os.environ.get("CONTEXTSHRINK_TEAM_SSO_ENABLED", "false").lower() not in {
        "1", "true", "yes", "on"
    }:
        raise HTTPException(404, "Team SSO is not available")
    _fernet()


def _encrypt(value: str) -> str:
    return _fernet().encrypt(value.encode()).decode()


def _decrypt(value: str) -> str:
    try:
        return _fernet().decrypt(value.encode()).decode()
    except InvalidToken as exc:
        raise HTTPException(503, "Team SSO encryption key does not match") from exc


def _digest(value: str) -> str:
    return security.hash_token(value)


def _challenge(value: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(value.encode()).digest()).rstrip(b"=").decode()


def _trusted_url(value: str) -> str:
    """No user-provided hosts: an operator must explicitly trust each IdP host."""
    url = urlsplit(value)
    allowed = {h.strip().lower() for h in os.environ.get("SSO_ALLOWED_HOSTS", "").split(",") if h.strip()}
    try:
        valid_port = url.port in (None, 443)
    except ValueError:
        valid_port = False
    if (url.scheme != "https" or not url.hostname or url.hostname.lower() not in allowed
            or not valid_port or url.username or url.password or url.fragment):
        raise HTTPException(422, "SSO endpoint must use HTTPS on an operator-approved provider host")
    return value


async def _public_addresses(value: str) -> list[str]:
    _trusted_url(value)
    host = urlsplit(value).hostname
    try:
        addresses = await asyncio.get_running_loop().getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
            raise ValueError("private address")
    except (OSError, ValueError) as exc:
        raise HTTPException(422, "SSO provider must resolve to public addresses") from exc
    return sorted({item[4][0] for item in addresses}, key=lambda address: ":" in address)


async def _public_url(value: str) -> str:
    await _public_addresses(value)
    return value


async def _request_json(url: str, *, data: dict | None = None) -> dict:
    addresses = await _public_addresses(url)
    endpoint = httpx.URL(url)
    hostname = endpoint.raw_host.decode("ascii")
    # Redirects and ambient proxy credentials are deliberately excluded.
    # Connect to a validated IP literal; retain the original Host and TLS SNI.
    # Validating DNS and then allowing the transport to resolve again is unsafe.
    async with httpx.AsyncClient(timeout=15, follow_redirects=False, trust_env=False) as client:
        for index, address in enumerate(addresses[:4]):
            try:
                async with client.stream(
                    "POST" if data is not None else "GET", endpoint.copy_with(host=address), data=data,
                    headers={"Host": hostname}, extensions={"sni_hostname": hostname},
                ) as response:
                    response.raise_for_status()
                    raw = bytearray()
                    async for chunk in response.aiter_bytes():
                        raw.extend(chunk)
                        if len(raw) > 256_000:
                            raise ValueError("Oversized provider response")
                break
            except (httpx.ConnectError, httpx.ConnectTimeout):
                if index == len(addresses[:4]) - 1:
                    raise
    result = json.loads(raw)
    if not isinstance(result, dict):
        raise ValueError("Invalid provider response")
    return result


async def _discover(issuer: str) -> dict:
    _trusted_url(issuer)
    if urlsplit(issuer).query:
        raise HTTPException(422, "Enter the exact issuer without a query")
    document = await _request_json(issuer.rstrip("/") + "/.well-known/openid-configuration")
    if document.get("issuer") != issuer:
        raise HTTPException(422, "Provider discovery issuer does not match")
    for key in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
        await _public_url(document[key])
    if "code" not in document.get("response_types_supported", []):
        raise HTTPException(422, "Provider must support authorization code flow")
    if "S256" not in document.get("code_challenge_methods_supported", []):
        raise HTTPException(422, "Provider must support PKCE S256")
    if "client_secret_post" not in document.get("token_endpoint_auth_methods_supported", ["client_secret_basic"]):
        raise HTTPException(422, "Enable client_secret_post authentication on the provider")
    return {key: document[key] for key in ("authorization_endpoint", "token_endpoint", "jwks_uri")}


def _origins() -> tuple[str, str]:
    api = os.environ.get("SSO_API_URL", "https://api.contextshrink.com").rstrip("/")
    app = os.environ.get("APP_URL", "https://app.contextshrink.com").rstrip("/")
    for url in (api, app):
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or not parsed.netloc or parsed.path
                or parsed.query or parsed.fragment or parsed.username or parsed.password):
            raise HTTPException(503, "SSO public URLs must be HTTPS origins")
    return api + "/auth/sso/callback", app + "/sso/callback"


async def _claims(token: str, config: dict, nonce: str) -> dict:
    if len(token) > 64_000:
        raise ValueError("Oversized identity token")
    header = jwt.get_unverified_header(token)
    if header.get("alg") not in ALGORITHMS or not header.get("kid"):
        raise ValueError("Unsupported signing key")
    jwks = await _request_json(config["jwks_uri"])
    candidates = [key for key in jwks.get("keys", [])
                  if key.get("kid") == header["kid"] and key.get("use", "sig") == "sig"
                  and key.get("alg", header["alg"]) == header["alg"]
                  and "verify" in key.get("key_ops", ["verify"])]
    if len(candidates) != 1:
        raise ValueError("Unknown signing key")
    key = jwt.PyJWK.from_dict(candidates[0], algorithm=header["alg"])
    claims = jwt.decode(token, key.key, algorithms=[header["alg"]],
                        audience=config["client_id"], issuer=config["issuer"], leeway=30,
                        options={"require": ["iss", "sub", "aud", "exp", "iat", "nonce"]})
    if (not isinstance(claims["sub"], str) or not 1 <= len(claims["sub"]) <= 255
            or not secrets.compare_digest(str(claims["nonce"]), nonce)
            or claims.get("email_verified") is not True
            or not isinstance(claims.get("email"), str)
            or not 1 <= len(claims["email"]) <= 320):
        raise ValueError("Unverified identity")
    if claims.get("azp", config["client_id"]) != config["client_id"]:
        raise ValueError("Wrong authorized party")
    if isinstance(claims["aud"], list) and len(claims["aud"]) > 1 and claims.get("azp") != config["client_id"]:
        raise ValueError("Missing authorized party")
    return {"sub": claims["sub"], "email": claims["email"].strip().lower()}


class ProviderIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    issuer: str = Field(min_length=8, max_length=2048)
    client_id: str = Field(min_length=1, max_length=512)
    client_secret: str = Field(min_length=1, max_length=4096)
    enabled: bool = True


class StartIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    team_id: UUID
    verifier: str = Field(min_length=43, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    link: bool = False


class ExchangeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str = Field(min_length=32, max_length=128)
    verifier: str = Field(min_length=43, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")


async def _config(team_id: UUID, conn=None, *, lock: bool = False):
    return await (conn or db._conn()).fetchrow(
        "SELECT * FROM core.team_sso_configs WHERE team_id=$1" + (" FOR UPDATE" if lock else ""), team_id)


async def _exchange(body: ExchangeIn, authorization: str | None = None) -> dict:
    async with db._conn().acquire() as conn:
        async with conn.transaction():
            flow = await conn.fetchrow(
                "SELECT * FROM core.team_sso_flows WHERE exchange_hash=$1 AND verifier_hash=$2 "
                "AND expires_at>now()", _digest(body.code), _digest(body.verifier))
            if not flow:
                raise HTTPException(401, "SSO sign-in expired or was already used")
            config = await _config(flow["team_id"], conn, lock=True)
            if not config or not config["enabled"] or config["revision"] != flow["revision"]:
                raise HTTPException(401, "Team SSO configuration changed; sign in again")
            # All operations lock config before flows/membership, including removal.
            # The second check consumes only one exchange even after a lock wait.
            flow = await conn.fetchrow(
                "DELETE FROM core.team_sso_flows WHERE exchange_hash=$1 AND verifier_hash=$2 "
                "AND expires_at>now() RETURNING *", _digest(body.code), _digest(body.verifier))
            if not flow:
                raise HTTPException(401, "SSO sign-in expired or was already used")
            claims = json.loads(flow["claims"]) if isinstance(flow["claims"], str) else flow["claims"]
            if flow["link_user_id"]:
                # Lock membership through commit so removal and linking cannot race.
                member = await conn.fetchrow(
                    "SELECT m.id,m.user_id,u.email,u.name,u.email_verified_at FROM core.team_memberships m "
                    "JOIN core.users u ON u.id=m.user_id WHERE m.id=$1 AND m.team_id=$2 "
                    "AND m.user_id=$3 AND m.left_at IS NULL FOR UPDATE OF m",
                    flow["membership_id"], flow["team_id"], flow["link_user_id"])
                if not member or not member["email_verified_at"] or member["email"].lower() != claims["email"]:
                    raise HTTPException(403, "Company identity must match your verified Team account")
                # An existing session must still be valid when the user finishes linking.
                valid = await conn.fetchval(
                    "SELECT EXISTS(SELECT 1 FROM core.sessions WHERE id=$1 AND user_id=$2 "
                    "AND token_hash=$3 AND sso_team_id IS NULL AND revoked_at IS NULL AND expires_at>now())",
                    flow["session_id"], member["user_id"],
                    _digest(authorization[7:]) if authorization and authorization.startswith("Bearer ") else "")
                if not valid:
                    raise HTTPException(401, "Sign in again before connecting company identity")
                collision = await conn.fetchrow(
                    "SELECT user_id FROM core.team_sso_identities WHERE team_id=$1 AND revision=$2 "
                    "AND subject=$3", flow["team_id"], flow["revision"], claims["sub"])
                if collision and collision["user_id"] != member["user_id"]:
                    raise HTTPException(409, "This company identity is already connected")
                await conn.execute(
                    "INSERT INTO core.team_sso_identities(team_id,revision,subject,user_id,membership_id) "
                    "VALUES($1,$2,$3,$4,$5) ON CONFLICT(team_id,user_id) DO UPDATE SET "
                    "revision=EXCLUDED.revision,subject=EXCLUDED.subject,membership_id=EXCLUDED.membership_id",
                    flow["team_id"], flow["revision"], claims["sub"], member["user_id"], member["id"])
                return {"linked": True, "team_id": str(flow["team_id"])}
            member = await conn.fetchrow(
                "SELECT m.id,m.user_id,u.email,u.name,u.email_verified_at FROM core.team_sso_identities i "
                "JOIN core.team_memberships m ON m.id=i.membership_id AND m.user_id=i.user_id AND m.team_id=i.team_id "
                "JOIN core.users u ON u.id=i.user_id WHERE i.team_id=$1 AND i.revision=$2 "
                "AND i.subject=$3 AND m.left_at IS NULL FOR UPDATE OF m",
                flow["team_id"], flow["revision"], claims["sub"])
            if not member or not member["email_verified_at"] or member["email"].lower() != claims["email"]:
                raise HTTPException(403, "Sign in with your password and connect company identity from your Team page first")
            token = security.new_session_token()
            await conn.execute(
                "INSERT INTO core.sessions(user_id,token_hash,expires_at,sso_team_id,sso_revision,sso_membership_id) "
                "VALUES($1,$2,$3,$4,$5,$6)", member["user_id"], _digest(token),
                datetime.now(timezone.utc) + timedelta(hours=8),
                flow["team_id"], flow["revision"], member["id"])
            return {"linked": False, "team_id": str(flow["team_id"]), "token": token,
                    "user": {"id": str(member["user_id"]), "email": member["email"], "name": member["name"]}}


def install(app: FastAPI, current_user) -> None:
    @app.get("/teams/{team_id}/sso")
    async def settings(team_id: UUID, user=Depends(current_user)):
        await teams.context(user, team_id)
        available = teams.enabled() and os.environ.get("CONTEXTSHRINK_TEAM_SSO_ENABLED", "false").lower() in {"1", "true", "yes", "on"}
        if not available:
            return {"configured": False, "enabled": False, "linked": False, "available": False}
        _guard()
        callback_uri, _ = _origins()
        row = await _config(team_id)
        linked = await db._conn().fetchval(
            "SELECT EXISTS(SELECT 1 FROM core.team_sso_identities i JOIN core.team_sso_configs c "
            "ON c.team_id=i.team_id AND c.revision=i.revision JOIN core.team_memberships m "
            "ON m.id=i.membership_id WHERE i.team_id=$1 AND i.user_id=$2 AND m.left_at IS NULL)",
            team_id, teams._uid(user))
        return {"configured": bool(row), "available": True, "enabled": bool(row and row["enabled"]),
                "linked": linked, "issuer": row["issuer"] if row else "", "client_id": row["client_id"] if row else "",
                "redirect_uri": callback_uri}

    @app.put("/teams/{team_id}/sso")
    async def configure(team_id: UUID, body: ProviderIn, user=Depends(current_user)):
        _guard()
        actor = await teams.context(user, team_id, "owner")
        if user.get("sso_team_id"):
            raise HTTPException(403, "Use password sign-in to change SSO settings")
        if not user.get("email_verified_at"):
            raise HTTPException(403, "Verify your email first")
        try:
            document = await _discover(body.issuer)
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            raise HTTPException(422, "Could not validate OIDC provider discovery") from exc
        async with db._conn().acquire() as conn:
            async with conn.transaction():
                await _config(team_id, conn, lock=True)
                await teams._lock_actor(conn, team_id, actor["membership_id"], "owner")
                await conn.execute(
                    "INSERT INTO core.team_sso_configs(team_id,revision,issuer,client_id,secret_ciphertext,"
                    "authorization_endpoint,token_endpoint,jwks_uri,enabled) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9) "
                    "ON CONFLICT(team_id) DO UPDATE SET revision=EXCLUDED.revision,issuer=EXCLUDED.issuer,"
                    "client_id=EXCLUDED.client_id,secret_ciphertext=EXCLUDED.secret_ciphertext,"
                    "authorization_endpoint=EXCLUDED.authorization_endpoint,token_endpoint=EXCLUDED.token_endpoint,"
                    "jwks_uri=EXCLUDED.jwks_uri,enabled=EXCLUDED.enabled,updated_at=now()",
                    team_id, uuid4(), body.issuer, body.client_id, _encrypt(body.client_secret),
                    document["authorization_endpoint"], document["token_endpoint"], document["jwks_uri"], body.enabled)
        return {"configured": True, "enabled": body.enabled}

    @app.delete("/teams/{team_id}/sso")
    async def remove_config(team_id: UUID, user=Depends(current_user)):
        _guard()
        actor = await teams.context(user, team_id, "owner")
        if user.get("sso_team_id"):
            raise HTTPException(403, "Use password sign-in to change SSO settings")
        async with db._conn().acquire() as conn:
            async with conn.transaction():
                await _config(team_id, conn, lock=True)
                await teams._lock_actor(conn, team_id, actor["membership_id"], "owner")
                await conn.execute("DELETE FROM core.team_sso_configs WHERE team_id=$1", team_id)
        return {"configured": False}

    @app.delete("/teams/{team_id}/sso/identity")
    async def unlink(team_id: UUID, user=Depends(current_user)):
        _guard()
        if user.get("sso_team_id"):
            raise HTTPException(403, "Use password sign-in to manage company identity")
        await teams.context(user, team_id)
        async with db._conn().acquire() as conn:
            async with conn.transaction():
                # Same config lock as exchange serializes concurrent unlink/login.
                await _config(team_id, conn, lock=True)
                await conn.execute("DELETE FROM core.team_sso_identities WHERE team_id=$1 AND user_id=$2",
                                   team_id, teams._uid(user))
                await conn.execute("UPDATE core.sessions SET revoked_at=now() WHERE sso_team_id=$1 AND user_id=$2 "
                                   "AND revoked_at IS NULL", team_id, teams._uid(user))
        return {"linked": False}

    @app.post("/auth/sso/start")
    async def start(body: StartIn, request: Request, authorization: str | None = Header(default=None)):
        _guard()
        if not start_by_client.allow(account_email._client(request)):
            raise HTTPException(429, "Too many SSO attempts; try again shortly")
        callback, _ = _origins()
        member = user = None
        if body.link:
            user = await current_user(authorization=authorization, x_contextshrink_team=None)
            if user.get("sso_team_id"):
                raise HTTPException(403, "Use password sign-in before connecting company identity")
            member = await teams.context(user, body.team_id)
            if not user.get("email_verified_at"):
                raise HTTPException(403, "Verify your email before connecting company identity")
        config = await _config(body.team_id)
        if not config or not config["enabled"]:
            raise HTTPException(404, "Team SSO is not configured")
        await _public_url(config["authorization_endpoint"])
        state, nonce, pkce = (secrets.token_urlsafe(32) for _ in range(3))
        # Bound storage per Team; expired handoffs contain no reusable tokens.
        async with db._conn().acquire() as conn:
            async with conn.transaction():
                locked = await _config(body.team_id, conn, lock=True)
                if not locked or locked["revision"] != config["revision"] or not locked["enabled"]:
                    raise HTTPException(409, "Team SSO settings changed; try again")
                await conn.execute("DELETE FROM core.team_sso_flows WHERE expires_at<=now()")
                count = await conn.fetchval("SELECT count(*) FROM core.team_sso_flows WHERE team_id=$1", body.team_id)
                if count >= 100:
                    raise HTTPException(429, "Too many pending SSO sign-ins; try again shortly")
                await conn.execute(
                    "INSERT INTO core.team_sso_flows(state_hash,team_id,revision,verifier_hash,nonce,"
                    "pkce_ciphertext,link_user_id,membership_id,session_id,expires_at) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)",
                    _digest(state), body.team_id, config["revision"], _digest(body.verifier), nonce, _encrypt(pkce),
                    teams._uid(user) if user else None, member["membership_id"] if member else None,
                    user["session_id"] if user else None, datetime.now(timezone.utc) + TTL)
        query = urlencode({"response_type": "code", "client_id": config["client_id"], "redirect_uri": callback,
                           "scope": "openid email", "state": state, "nonce": nonce,
                           "code_challenge": _challenge(pkce), "code_challenge_method": "S256",
                           "prompt": "select_account"})
        separator = "&" if urlsplit(config["authorization_endpoint"]).query else "?"
        return {"authorization_url": config["authorization_endpoint"] + separator + query}

    @app.get("/auth/sso/callback")
    async def callback(request: Request):
        _guard()
        callback_url, frontend = _origins()
        fragment = "error=sso_failed"
        try:
            state = request.query_params.get("state", "")
            code = request.query_params.get("code", "")
            if not 32 <= len(state) <= 128 or not 1 <= len(code) <= 8192 or request.query_params.get("error"):
                raise ValueError("Invalid callback")
            flow = await db._conn().fetchrow(
                "UPDATE core.team_sso_flows SET callback_used=true WHERE state_hash=$1 "
                "AND callback_used=false AND expires_at>now() RETURNING *", _digest(state))
            if not flow:
                raise ValueError("Expired callback")
            config = await _config(flow["team_id"])
            if not config or not config["enabled"] or config["revision"] != flow["revision"]:
                raise ValueError("Changed config")
            result = await _request_json(config["token_endpoint"], data={
                "grant_type": "authorization_code", "code": code, "redirect_uri": callback_url,
                "client_id": config["client_id"], "client_secret": _decrypt(config["secret_ciphertext"]),
                "code_verifier": _decrypt(flow["pkce_ciphertext"])})
            claims = await _claims(result["id_token"], config, flow["nonce"])
            exchange = secrets.token_urlsafe(32)
            await db._conn().execute(
                "UPDATE core.team_sso_flows SET exchange_hash=$2,claims=$3,expires_at=now()+interval '60 seconds',"
                "pkce_ciphertext='' WHERE state_hash=$1", _digest(state), _digest(exchange), json.dumps(claims))
            fragment = "code=" + exchange
        except (HTTPException, httpx.HTTPError, jwt.PyJWTError, ValueError, KeyError, TypeError):
            # Never reflect IdP errors, codes or tokens to the browser.
            pass
        return RedirectResponse(frontend + "#" + fragment, status_code=303,
                                headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})

    @app.post("/auth/sso/exchange")
    async def exchange(body: ExchangeIn, request: Request, authorization: str | None = Header(default=None)):
        _guard()
        if not exchange_by_client.allow(account_email._client(request)):
            raise HTTPException(429, "Too many SSO attempts; try again shortly")
        return await _exchange(body, authorization)
