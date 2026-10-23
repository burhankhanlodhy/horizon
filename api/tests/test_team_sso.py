"""Security and replay tests for Team OIDC; provider and DB boundaries are mocked."""
import asyncio
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import team_sso  # noqa: E402

ISSUER = "https://login.example.test"
CLIENT_ID = "team-app"
TEAM_ID = UUID("22222222-2222-4222-8222-222222222222")


@pytest.fixture
def signing_key():
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private.public_key()))
    public_jwk.update({"kid": "known-key", "use": "sig", "alg": "RS256", "key_ops": ["verify"]})
    return private, public_jwk


def _id_token(private, **overrides):
    now = int(time.time())
    claims = {
        "iss": ISSUER,
        "sub": "idp-subject-1",
        "aud": CLIENT_ID,
        "exp": now + 300,
        "iat": now,
        "nonce": "expected-nonce",
        "email": "member@example.test",
        "email_verified": True,
    }
    claims.update(overrides)
    return jwt.encode(claims, private, algorithm="RS256", headers={"kid": "known-key"})


def _claims_config():
    return {"jwks_uri": ISSUER + "/keys", "client_id": CLIENT_ID, "issuer": ISSUER}


def test_claims_accepts_signature_and_bound_oidc_claims(monkeypatch, signing_key):
    private, jwk = signing_key

    async def request_json(_url, **_kwargs):
        return {"keys": [jwk]}

    monkeypatch.setattr(team_sso, "_request_json", request_json)
    claims = asyncio.run(team_sso._claims(_id_token(private), _claims_config(), "expected-nonce"))
    assert claims["sub"] == "idp-subject-1"
    assert claims["email"] == "member@example.test"


@pytest.mark.parametrize(
    "overrides,nonce",
    [
        ({"iss": "https://attacker.example"}, "expected-nonce"),
        ({"aud": "different-client"}, "expected-nonce"),
        ({}, "different-nonce"),
        ({"exp": int(time.time()) - 3600}, "expected-nonce"),
        ({"email_verified": False}, "expected-nonce"),
    ],
    ids=["issuer", "audience", "nonce", "expired", "unverified-email"],
)
def test_claims_reject_invalid_issuer_audience_nonce_expiry_and_email(
    monkeypatch, signing_key, overrides, nonce
):
    private, jwk = signing_key

    async def request_json(_url, **_kwargs):
        return {"keys": [jwk]}

    monkeypatch.setattr(team_sso, "_request_json", request_json)
    with pytest.raises((ValueError, jwt.PyJWTError)):
        asyncio.run(team_sso._claims(_id_token(private, **overrides), _claims_config(), nonce))


def test_claims_reject_signature_tampering_unknown_kid_and_unapproved_algorithm(monkeypatch, signing_key):
    private, jwk = signing_key

    async def request_json(_url, **_kwargs):
        return {"keys": [jwk]}

    monkeypatch.setattr(team_sso, "_request_json", request_json)
    token = _id_token(private)
    header, payload, signature = token.split(".")
    replacement = "A" if signature[-1] != "A" else "B"
    tampered = ".".join((header, payload, signature[:-1] + replacement))
    with pytest.raises(jwt.PyJWTError):
        asyncio.run(team_sso._claims(tampered, _claims_config(), "expected-nonce"))

    unknown_kid = jwt.encode(
        {"iss": ISSUER, "sub": "s", "aud": CLIENT_ID, "exp": int(time.time()) + 300,
         "iat": int(time.time()), "nonce": "expected-nonce", "email": "m@example.test",
         "email_verified": True},
        private, algorithm="RS256", headers={"kid": "unknown"},
    )
    with pytest.raises(ValueError, match="Unknown signing key"):
        asyncio.run(team_sso._claims(unknown_kid, _claims_config(), "expected-nonce"))

    hs_token = jwt.encode({"sub": "x"}, "local-test-secret-that-is-long-enough-for-hs256", algorithm="HS256", headers={"kid": "known-key"})
    with pytest.raises(ValueError, match="Unsupported signing key"):
        asyncio.run(team_sso._claims(hs_token, _claims_config(), "expected-nonce"))


def test_feature_flag_defaults_off(monkeypatch):
    monkeypatch.delenv("CONTEXTSHRINK_TEAM_SSO_ENABLED", raising=False)
    monkeypatch.setattr(team_sso.teams, "_guard_enabled", lambda: None)
    with pytest.raises(HTTPException) as exc:
        team_sso._guard()
    assert exc.value.status_code == 404


@pytest.mark.parametrize(
    "url",
    [
        "http://login.example.test", "https://untrusted.example.test", "https://login.example.test:444",
        "https://user:pass@login.example.test", "https://login.example.test/path#fragment",
    ],
)
def test_trusted_url_rejects_untrusted_or_unsafe_provider_urls(monkeypatch, url):
    monkeypatch.setenv("SSO_ALLOWED_HOSTS", "login.example.test")
    with pytest.raises(HTTPException) as exc:
        team_sso._trusted_url(url)
    assert exc.value.status_code == 422


def test_public_url_rejects_private_dns_results_and_allows_public_addresses(monkeypatch):
    monkeypatch.setenv("SSO_ALLOWED_HOSTS", "login.example.test")

    class Loop:
        def __init__(self, ip):
            self.ip = ip

        async def getaddrinfo(self, *_args, **_kwargs):
            return [(None, None, None, None, (self.ip, 443))]

    monkeypatch.setattr(team_sso.asyncio, "get_running_loop", lambda: Loop("127.0.0.1"))
    with pytest.raises(HTTPException, match="public addresses"):
        asyncio.run(team_sso._public_url("https://login.example.test/authorize"))

    monkeypatch.setattr(team_sso.asyncio, "get_running_loop", lambda: Loop("8.8.8.8"))
    assert asyncio.run(team_sso._public_url("https://login.example.test/authorize")) == "https://login.example.test/authorize"
    assert asyncio.run(team_sso._public_addresses("https://login.example.test/authorize")) == ["8.8.8.8"]


def test_request_json_pins_validated_ip_and_preserves_host_and_tls_name(monkeypatch):
    monkeypatch.setattr(team_sso, "_public_addresses", lambda _url: asyncio.sleep(0, result=["8.8.8.8"]))
    observed = {}

    class Response:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        def raise_for_status(self):
            return None

        async def aiter_bytes(self):
            yield b'{"discovery":true}'

    class Client:
        def __init__(self, **kwargs):
            observed["client_options"] = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        def stream(self, method, url, **kwargs):
            observed.update(method=method, url=str(url), request=kwargs)
            return Response()

    monkeypatch.setattr(team_sso.httpx, "AsyncClient", Client)
    result = asyncio.run(team_sso._request_json("https://login.example.test/token", data={"code": "abc"}))
    assert result == {"discovery": True}
    assert observed["method"] == "POST"
    assert observed["url"] == "https://8.8.8.8/token"
    assert observed["request"]["headers"] == {"Host": "login.example.test"}
    assert observed["request"]["extensions"] == {"sni_hostname": "login.example.test"}
    assert observed["client_options"]["follow_redirects"] is False
    assert observed["client_options"]["trust_env"] is False

class _Tx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


class _Acquire:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *_args):
        return False


class _ExchangeConn:
    def __init__(self, flow, member=None, *, linked_member=None, valid_session=True):
        self.flow = flow
        self.member = member
        self.linked_member = linked_member
        self.valid_session = valid_session
        self.consumed = False
        self.executed = []

    def acquire(self):
        return _Acquire(self)

    def transaction(self):
        return _Tx()

    async def fetchrow(self, sql, *args):
        if "FROM core.team_sso_flows" in sql:
            if self.consumed or args[0] != team_sso._digest(self.flow["exchange_code"]) or args[1] != team_sso._digest(self.flow["verifier"]):
                return None
            if sql.startswith("DELETE"):
                self.consumed = True
            return self.flow
        if "SELECT * FROM core.team_sso_configs" in sql:
            return {"team_id": TEAM_ID, "revision": "revision-1", "enabled": True}
        if "FROM core.team_memberships m" in sql:
            return self.member
        if "FROM core.team_sso_identities i" in sql:
            return self.linked_member
        if "SELECT user_id FROM core.team_sso_identities" in sql:
            return None
        raise AssertionError(f"Unexpected fetchrow SQL: {sql}")

    async def fetchval(self, sql, *_args):
        assert "FROM core.sessions" in sql
        return self.valid_session

    async def execute(self, sql, *args):
        self.executed.append((sql, args))
        return "INSERT 0 1"


def _flow(*, link=False, email="member@example.test"):
    return {
        "team_id": TEAM_ID,
        "revision": "revision-1",
        "claims": {"sub": "idp-subject-1", "email": email},
        "link_user_id": UUID("33333333-3333-4333-8333-333333333333") if link else None,
        "membership_id": UUID("44444444-4444-4444-8444-444444444444") if link else None,
        "session_id": UUID("55555555-5555-4555-8555-555555555555") if link else None,
        "exchange_code": "x" * 43,
        "verifier": "v" * 43,
    }


def _member(email="member@example.test"):
    return {
        "id": UUID("44444444-4444-4444-8444-444444444444"),
        "user_id": UUID("33333333-3333-4333-8333-333333333333"),
        "email": email,
        "name": "A Member",
        "email_verified_at": object(),
    }


def _patch_exchange(monkeypatch, conn):
    monkeypatch.setattr(team_sso, "db", SimpleNamespace(_conn=lambda: conn, SESSION_TTL_DAYS=30))
    monkeypatch.setattr(team_sso.security, "new_session_token", lambda: "new-session-token")


def test_exchange_binds_browser_verifier_consumes_code_once_and_requires_current_membership(monkeypatch):
    flow = _flow()
    conn = _ExchangeConn(flow, linked_member=_member())
    _patch_exchange(monkeypatch, conn)
    body = team_sso.ExchangeIn(code=flow["exchange_code"], verifier=flow["verifier"])

    result = asyncio.run(team_sso._exchange(body))
    assert result["linked"] is False
    assert result["user"]["email"] == "member@example.test"
    assert result["token"] == "new-session-token"
    assert any("INSERT INTO core.sessions" in sql for sql, _args in conn.executed)

    with pytest.raises(HTTPException) as replay:
        asyncio.run(team_sso._exchange(body))
    assert replay.value.status_code == 401

    fresh = _flow()
    conn = _ExchangeConn(fresh, linked_member=_member(), valid_session=True)
    _patch_exchange(monkeypatch, conn)
    wrong_verifier = team_sso.ExchangeIn(code=fresh["exchange_code"], verifier="w" * 43)
    with pytest.raises(HTTPException) as mismatch:
        asyncio.run(team_sso._exchange(wrong_verifier))
    assert mismatch.value.status_code == 401


def test_login_requires_current_active_membership_and_verified_matching_email(monkeypatch):
    flow = _flow()
    conn = _ExchangeConn(flow, linked_member=None)
    _patch_exchange(monkeypatch, conn)
    with pytest.raises(HTTPException) as inactive:
        asyncio.run(team_sso._exchange(team_sso.ExchangeIn(code=flow["exchange_code"], verifier=flow["verifier"])))
    assert inactive.value.status_code == 403

    flow = _flow(email="other@example.test")
    conn = _ExchangeConn(flow, linked_member=_member())
    _patch_exchange(monkeypatch, conn)
    with pytest.raises(HTTPException) as mismatch:
        asyncio.run(team_sso._exchange(team_sso.ExchangeIn(code=flow["exchange_code"], verifier=flow["verifier"])))
    assert mismatch.value.status_code == 403


def test_explicit_link_requires_same_live_password_session_and_verified_team_membership(monkeypatch):
    flow = _flow(link=True)
    conn = _ExchangeConn(flow, member=_member(), valid_session=False)
    _patch_exchange(monkeypatch, conn)
    body = team_sso.ExchangeIn(code=flow["exchange_code"], verifier=flow["verifier"])
    with pytest.raises(HTTPException) as no_session:
        asyncio.run(team_sso._exchange(body, "Bearer current-session-token"))
    assert no_session.value.status_code == 401

    flow = _flow(link=True)
    conn = _ExchangeConn(flow, member=None, valid_session=True)
    _patch_exchange(monkeypatch, conn)
    with pytest.raises(HTTPException) as no_member:
        asyncio.run(team_sso._exchange(body, "Bearer current-session-token"))
    assert no_member.value.status_code == 403

    flow = _flow(link=True, email="different@example.test")
    conn = _ExchangeConn(flow, member=_member(), valid_session=True)
    _patch_exchange(monkeypatch, conn)
    with pytest.raises(HTTPException) as email_mismatch:
        asyncio.run(team_sso._exchange(team_sso.ExchangeIn(code=flow["exchange_code"], verifier=flow["verifier"]), "Bearer current-session-token"))
    assert email_mismatch.value.status_code == 403

    flow = _flow(link=True)
    conn = _ExchangeConn(flow, member=_member(), valid_session=True)
    _patch_exchange(monkeypatch, conn)
    result = asyncio.run(team_sso._exchange(body, "Bearer current-session-token"))
    assert result == {"linked": True, "team_id": str(TEAM_ID)}
    assert any("INSERT INTO core.team_sso_identities" in sql for sql, _args in conn.executed)

class _ConfigConn:
    def __init__(self):
        self.executed = []

    def acquire(self):
        return _Acquire(self)

    def transaction(self):
        return _Tx()

    async def fetchrow(self, sql, *args):
        assert "core.team_sso_configs" in sql
        return None

    async def execute(self, sql, *args):
        self.executed.append((sql, args))
        return "INSERT 0 1"


def test_provider_configuration_route_requires_owner_and_stores_encrypted_secret(monkeypatch):
    from cryptography.fernet import Fernet

    monkeypatch.setenv("CONTEXTSHRINK_TEAM_SSO_ENABLED", "true")
    monkeypatch.setenv("SSO_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setattr(team_sso.teams, "_guard_enabled", lambda: None)
    conn = _ConfigConn()
    monkeypatch.setattr(team_sso, "db", SimpleNamespace(_conn=lambda: conn))
    requested_roles = []

    async def context(_user, _team_id, minimum_role="member"):
        requested_roles.append(minimum_role)
        if minimum_role != "owner":
            raise HTTPException(403, "Owner role required")
        return {"membership_id": uuid4()}

    async def lock_actor(_conn, _team_id, _membership_id, minimum_role):
        requested_roles.append(minimum_role)

    async def discover(_issuer):
        return {
            "authorization_endpoint": "https://login.example.test/auth",
            "token_endpoint": "https://login.example.test/token",
            "jwks_uri": "https://login.example.test/keys",
        }

    monkeypatch.setattr(team_sso.teams, "context", context)
    monkeypatch.setattr(team_sso.teams, "_lock_actor", lock_actor)
    monkeypatch.setattr(team_sso, "_discover", discover)

    app = FastAPI()
    team_sso.install(app, lambda: None)
    endpoint = next(route.endpoint for route in app.routes if getattr(route, "path", None) == "/teams/{team_id}/sso" and "PUT" in route.methods)
    user = {"user_id": uuid4(), "email_verified_at": object(), "sso_team_id": None}
    body = team_sso.ProviderIn(
        issuer=ISSUER, client_id="team-client", client_secret="secret-value", enabled=True,
    )
    result = asyncio.run(endpoint(TEAM_ID, body, user=user))

    assert result == {"configured": True, "enabled": True}
    assert requested_roles == ["owner", "owner"]
    sql, args = conn.executed[0]
    assert "INSERT INTO core.team_sso_configs" in sql
    assert args[4] != "secret-value"
    assert team_sso._decrypt(args[4]) == "secret-value"

class _BrowserConn:
    def __init__(self, config):
        self.config = config
        self.flows = {}
        self.token_requests = []
        self.executed = []

    def acquire(self):
        return _Acquire(self)

    def transaction(self):
        return _Tx()

    async def fetchrow(self, sql, *args):
        if "SELECT * FROM core.team_sso_configs" in sql:
            return self.config
        if "UPDATE core.team_sso_flows SET callback_used=true" in sql:
            flow = self.flows.get(args[0])
            if not flow or flow["callback_used"] or flow["expires_at"] <= team_sso.datetime.now(team_sso.timezone.utc):
                return None
            flow["callback_used"] = True
            return flow
        if "FROM core.team_sso_flows" in sql and "exchange_hash=$1" in sql:
            for flow in self.flows.values():
                if (flow.get("exchange_hash") == args[0] and flow["verifier_hash"] == args[1]
                        and flow["expires_at"] > team_sso.datetime.now(team_sso.timezone.utc) and not flow.get("consumed")):
                    if sql.startswith("DELETE"):
                        flow["consumed"] = True
                    return flow
            return None
        if "FROM core.team_sso_identities i" in sql:
            return _member()
        raise AssertionError(f"Unexpected browser-flow fetchrow SQL: {sql}")

    async def fetchval(self, sql, *_args):
        if "count(*) FROM core.team_sso_flows" in sql:
            return 0
        raise AssertionError(f"Unexpected browser-flow fetchval SQL: {sql}")

    async def execute(self, sql, *args):
        self.executed.append((sql, args))
        if "INSERT INTO core.team_sso_flows" in sql:
            state_hash, team_id, revision, verifier_hash, nonce, encrypted_pkce, link_user_id, membership_id, session_id, expires_at = args
            self.flows[state_hash] = {
                "state_hash": state_hash, "team_id": team_id, "revision": revision,
                "verifier_hash": verifier_hash, "nonce": nonce, "pkce_ciphertext": encrypted_pkce,
                "link_user_id": link_user_id, "membership_id": membership_id, "session_id": session_id,
                "expires_at": expires_at, "callback_used": False,
            }
        elif "SET exchange_hash=$2,claims=$3" in sql:
            flow = self.flows[args[0]]
            flow.update(exchange_hash=args[1], claims=args[2], expires_at=team_sso.datetime.now(team_sso.timezone.utc) + team_sso.timedelta(seconds=60), pkce_ciphertext="")
        return "INSERT 0 1"


def _browser_setup(monkeypatch):
    from cryptography.fernet import Fernet

    monkeypatch.setenv("CONTEXTSHRINK_TEAM_SSO_ENABLED", "true")
    monkeypatch.setenv("SSO_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("SSO_API_URL", "https://api.example.test")
    monkeypatch.setenv("APP_URL", "https://app.example.test")
    monkeypatch.setattr(team_sso.teams, "_guard_enabled", lambda: None)
    config = {
        "team_id": TEAM_ID, "revision": "browser-revision-1", "enabled": True,
        "issuer": ISSUER, "client_id": CLIENT_ID, "secret_ciphertext": team_sso._encrypt("client-secret"),
        "authorization_endpoint": ISSUER + "/authorize", "token_endpoint": ISSUER + "/token",
        "jwks_uri": ISSUER + "/keys",
    }
    conn = _BrowserConn(config)
    monkeypatch.setattr(team_sso, "db", SimpleNamespace(_conn=lambda: conn, SESSION_TTL_DAYS=30))

    async def public_url(url):
        return url

    async def request_json(_url, *, data=None):
        conn.token_requests.append(data)
        return {"id_token": "opaque-signed-token"}

    async def claims(_token, _config, nonce):
        assert nonce in {flow["nonce"] for flow in conn.flows.values()}
        return {"sub": "idp-subject-1", "email": "member@example.test"}

    monkeypatch.setattr(team_sso, "_public_url", public_url)
    monkeypatch.setattr(team_sso, "_request_json", request_json)
    monkeypatch.setattr(team_sso, "_claims", claims)
    monkeypatch.setattr(team_sso.security, "new_session_token", lambda: "server-session-secret")
    app = FastAPI()
    team_sso.install(app, lambda: None)
    return TestClient(app, follow_redirects=False), conn


def _start_browser_flow(client, verifier="v" * 50):
    response = client.post("/auth/sso/start", json={"team_id": str(TEAM_ID), "verifier": verifier, "link": False})
    assert response.status_code == 200
    from urllib.parse import parse_qs, urlsplit
    query = parse_qs(urlsplit(response.json()["authorization_url"]).query)
    return verifier, query


def test_browser_start_callback_and_exchange_is_one_time_and_fragment_only(monkeypatch):
    client, conn = _browser_setup(monkeypatch)
    verifier, query = _start_browser_flow(client)
    assert query["state"]
    assert query["nonce"]
    assert query["code_challenge_method"] == ["S256"]
    state_hash = team_sso._digest(query["state"][0])
    pkce = team_sso._decrypt(conn.flows[state_hash]["pkce_ciphertext"])

    callback = client.get("/auth/sso/callback", params={"state": query["state"][0], "code": "idp-authorization-code"})
    assert callback.status_code == 303
    location = callback.headers["location"]
    fragment = location.split("#", 1)[1]
    assert fragment.startswith("code=")
    assert "token" not in fragment.lower()
    assert callback.headers["cache-control"] == "no-store"
    assert callback.headers["referrer-policy"] == "no-referrer"
    exchange_code = fragment.removeprefix("code=")
    assert len(conn.token_requests) == 1
    assert conn.token_requests[0]["code_verifier"] == pkce

    replay = client.get("/auth/sso/callback", params={"state": query["state"][0], "code": "idp-another-code"})
    assert replay.status_code == 303
    assert replay.headers["location"].endswith("#error=sso_failed")
    assert len(conn.token_requests) == 1

    wrong_verifier = client.post("/auth/sso/exchange", json={"code": exchange_code, "verifier": "w" * 50})
    assert wrong_verifier.status_code == 401
    exchange = client.post("/auth/sso/exchange", json={"code": exchange_code, "verifier": verifier})
    assert exchange.status_code == 200
    assert exchange.json()["user"]["email"] == "member@example.test"
    assert exchange.json()["token"] == "server-session-secret"
    replay_exchange = client.post("/auth/sso/exchange", json={"code": exchange_code, "verifier": verifier})
    assert replay_exchange.status_code == 401


def test_browser_callback_expired_state_is_generic_and_revision_change_invalidates_exchange(monkeypatch):
    client, conn = _browser_setup(monkeypatch)
    verifier, expired_query = _start_browser_flow(client)
    expired = conn.flows[team_sso._digest(expired_query["state"][0])]
    expired["expires_at"] = team_sso.datetime.now(team_sso.timezone.utc) - team_sso.timedelta(seconds=1)
    response = client.get("/auth/sso/callback", params={"state": expired_query["state"][0], "code": "idp-code"})
    assert response.status_code == 303
    assert response.headers["location"].endswith("#error=sso_failed")
    assert conn.token_requests == []

    verifier, query = _start_browser_flow(client)
    callback = client.get("/auth/sso/callback", params={"state": query["state"][0], "code": "idp-code"})
    exchange_code = callback.headers["location"].split("#code=", 1)[1]
    conn.config["revision"] = "browser-revision-2"
    rejected = client.post("/auth/sso/exchange", json={"code": exchange_code, "verifier": verifier})
    assert rejected.status_code == 401
    assert "configuration changed" in rejected.json()["detail"].lower()
