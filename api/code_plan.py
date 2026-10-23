"""ContextShrink Code: the flat-fee plan for people who use Claude Code or Codex.

Accounts are the usual core.users. Each computer connects once through the npx
CLI (a device-code login), then asks for a signed plan note that the local
engine verifies offline. Only hashes of device keys, CLI codes and poll secrets
are stored. Stripe subscriptions for this product are routed here by
handle_stripe_event and never reach the savings-fee code in billing.py.
"""

import base64
import hmac
import json
import logging
import os
import secrets
import time
from datetime import datetime, timedelta, timezone
from typing import Literal
from uuid import UUID, uuid4

import asyncpg
import billing
import db
import security
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import Depends, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger("contextshrink.code")

SITE_URL = os.environ.get("CODE_SITE_URL", "https://code.contextshrink.com").rstrip("/")
API_URL = os.environ.get("CODE_API_URL", "https://api.contextshrink.com").rstrip("/")
# Base64 of a 32-byte raw Ed25519 seed. Unset: /code/entitlement returns 503.
ENTITLEMENT_PRIVATE_KEY_B64 = os.environ.get("CODE_ENTITLEMENT_PRIVATE_KEY", "")
CODE_PRICE_LOOKUP_KEY = os.environ.get("STRIPE_CODE_PRICE_LOOKUP_KEY", "contextshrink_code_monthly")

PRODUCT = "code"
NOTE_ISSUER = "contextshrink-code"
DEVICE_LIMIT = 2
TRIAL_DAYS = 7
# Days after a paid period ends (or a failed renewal) that the plan stays on.
GRACE = timedelta(days=3)
NOTE_MAX_AGE_S = 72 * 3600
CLI_REQUEST_TTL = timedelta(minutes=10)
CLI_POLL_INTERVAL_S = 3
CLI_START_LIMIT = 10
CLI_START_WINDOW_S = 60
USAGE_MAX_BUCKETS = 500
USAGE_MAX_AGE = timedelta(days=35)
# 8 characters with no 0/O/1/I, so codes read aloud or typed by hand are unambiguous.
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
SUBSCRIPTION_EVENTS = {
    "customer.subscription.created",
    "customer.subscription.updated",
    "customer.subscription.deleted",
    "customer.subscription.paused",
    "customer.subscription.resumed",
}
INVOICE_EVENTS = {"invoice.created", "invoice.paid", "invoice.payment_failed", "invoice.voided"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------- models


class CliStartIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tool: Literal["claude", "codex"]
    device_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    device_name: str = Field(min_length=1, max_length=60)
    platform: Literal["windows", "macos", "linux"]


class CliCodeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user_code: str = Field(min_length=1, max_length=20)


class CliPollIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID
    poll_secret: str = Field(min_length=20, max_length=200)


class EntitlementIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    device_hash: str = Field(pattern=r"^[a-f0-9]{64}$")


class UsageBucketIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    hour: str = Field(max_length=40)
    tool: Literal["claude", "codex"]
    requests: int = Field(ge=0, le=10**12)
    tokens_before: int = Field(ge=0, le=10**15)
    tokens_after: int = Field(ge=0, le=10**15)


class UsageIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    buckets: list[UsageBucketIn] = Field(max_length=USAGE_MAX_BUCKETS)


# --------------------------------------------------------------- helpers


def _hash(value: str) -> str:
    return security.hash_token(value)


def _iso(value) -> str | None:
    return value.isoformat() if value else None


def _bearer(authorization: str | None) -> str:
    return (authorization or "").removeprefix("Bearer ").strip()


def _normalize_code(raw: str) -> str:
    code = raw.strip().upper().replace("-", "").replace(" ", "")
    if len(code) != 8 or any(c not in CODE_ALPHABET for c in code):
        raise HTTPException(404, "Code not found or expired")
    return code


def _code_hash(raw: str) -> str:
    return _hash(_normalize_code(raw))


def _new_user_code() -> str:
    raw = "".join(secrets.choice(CODE_ALPHABET) for _ in range(8))
    return f"{raw[:4]}-{raw[4:]}"


def _device_out(device) -> dict:
    return {
        "id": str(device["id"]),
        "name": device["name"],
        "platform": device["platform"],
        "last_seen_at": _iso(device["last_seen_at"]),
    }


def _client_address(request: Request) -> str:
    # Behind the Pi 5 tunnel the socket peer is loopback; Cloudflare sets
    # CF-Connecting-IP on every request it forwards.
    return request.headers.get("cf-connecting-ip") or (
        request.client.host if request.client else "unknown"
    )


# In memory only, like the contact form's limiter: nothing about callers is stored.
_start_hits: dict[str, list[float]] = {}


def _start_allowed(client: str) -> bool:
    now = time.monotonic()
    hits = [t for t in _start_hits.get(client, []) if now - t < CLI_START_WINDOW_S]
    allowed = len(hits) < CLI_START_LIMIT
    if allowed:
        hits.append(now)
    _start_hits[client] = hits
    if len(_start_hits) > 10_000:
        for key in [
            k for k, v in _start_hits.items() if not v or now - v[-1] >= CLI_START_WINDOW_S
        ]:
            del _start_hits[key]
    return allowed


def _signer() -> Ed25519PrivateKey:
    if not ENTITLEMENT_PRIVATE_KEY_B64:
        raise HTTPException(503, "ContextShrink Code is not configured")
    try:
        return Ed25519PrivateKey.from_private_bytes(
            base64.b64decode(ENTITLEMENT_PRIVATE_KEY_B64, validate=True)
        )
    except ValueError:
        # Log the problem, never the key.
        logger.error("CODE_ENTITLEMENT_PRIVATE_KEY is not a base64 32-byte Ed25519 seed")
        raise HTTPException(503, "ContextShrink Code is not configured") from None


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _sign_note(payload: dict, signer: Ed25519PrivateKey) -> str:
    """Plan note: base64url(payload) + "." + base64url(signature over the payload bytes)."""
    body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return f"{_b64url(body)}.{_b64url(signer.sign(body))}"


# ----------------------------------------------------------- entitlement


def _trial_until(plan: dict, now: datetime) -> datetime | None:
    end = plan.get("trial_ends_at")
    return end if end is not None and now < end else None


def _paid_until(plan: dict, now: datetime) -> datetime | None:
    status = plan.get("status")
    end = plan.get("current_period_end")
    if status in billing.ENTITLED_STATUSES:
        return (max(end, now) if end else now) + GRACE
    if status == "past_due" and end is not None and now < end + GRACE:
        return end + GRACE
    return None


def _entitlement(plan: dict, now: datetime) -> tuple[str, datetime] | None:
    """(plan name, entitled until) while the account is entitled, else None."""
    paid = _paid_until(plan, now)
    trial = _trial_until(plan, now)
    if paid is None and trial is None:
        return None
    if paid is not None:
        return "active", max(filter(None, (paid, trial)))
    return "trial", trial


def _plan_state(plan: dict, now: datetime) -> str:
    status = plan.get("status")
    if status in billing.ENTITLED_STATUSES:
        return "active"
    if _trial_until(plan, now) is not None:
        return "trial"
    if status == "past_due":
        return "past_due"
    if status in billing.ENDED_STATUSES:
        return "canceled"
    return "none"


async def _plan(conn, user_id) -> dict:
    row = await conn.fetchrow(
        "SELECT trial_started_at, trial_ends_at, stripe_subscription_id, status, "
        "current_period_end, cancel_at_period_end FROM core.code_plans WHERE user_id=$1",
        user_id,
    )
    return dict(row) if row else {}


async def _active_devices(conn, user_id) -> list[dict]:
    rows = await conn.fetch(
        "SELECT id, name, platform, tools, device_hash, created_at, last_seen_at "
        "FROM core.code_devices WHERE user_id=$1 AND revoked_at IS NULL ORDER BY created_at",
        user_id,
    )
    return [dict(r) for r in rows]


async def _device_from_header(authorization: str | None, *, allow_revoked: bool = False):
    token = _bearer(authorization)
    if (
        not authorization
        or not authorization.startswith("Bearer ")
        or not token.startswith("cs_dev_")
        or len(token) > 100
    ):
        raise HTTPException(401, "Invalid device key")
    device = await db._conn().fetchrow(
        "SELECT id, user_id, device_hash, revoked_at FROM core.code_devices WHERE key_hash=$1",
        _hash(token),
    )
    if device is None or (device["revoked_at"] is not None and not allow_revoked):
        raise HTTPException(401, "Invalid device key")
    return device


async def device_auth(authorization: str | None = Header(default=None)):
    return await _device_from_header(authorization)


async def device_auth_allow_revoked(authorization: str | None = Header(default=None)):
    # Uninstall is idempotent, so a repeated call from an already-revoked device succeeds.
    return await _device_from_header(authorization, allow_revoked=True)


async def _pending_request(conn, code_hash: str):
    req = await conn.fetchrow(
        "SELECT id, tool, device_hash, device_name, platform, expires_at, approved_user_id "
        "FROM core.code_cli_requests WHERE user_code_hash=$1",
        code_hash,
    )
    if req is None or req["approved_user_id"] is not None or req["expires_at"] <= _now():
        raise HTTPException(404, "Code not found or expired")
    return req


# ---------------------------------------------------------------- trial


async def _start_trial(conn, user_id, device_hash: str, now: datetime) -> None:
    # One trial per device_hash across all accounts: the insert is the claim.
    claimed = await conn.fetchval(
        "INSERT INTO core.code_trial_devices (device_hash, user_id, started_at) VALUES ($1,$2,$3) "
        "ON CONFLICT (device_hash) DO NOTHING RETURNING device_hash",
        device_hash,
        user_id,
        now,
    )
    if claimed is None:
        return  # This computer already started a trial on another account.
    await conn.execute(
        "INSERT INTO core.code_plans (user_id, trial_started_at, trial_ends_at, updated_at) "
        "VALUES ($1,$2,$3,$2) ON CONFLICT (user_id) DO UPDATE SET "
        "trial_started_at=EXCLUDED.trial_started_at, trial_ends_at=EXCLUDED.trial_ends_at, "
        "updated_at=EXCLUDED.updated_at",
        user_id,
        now,
        now + timedelta(days=TRIAL_DAYS),
    )


# -------------------------------------------------------------- Stripe


def _metadata(obj) -> dict:
    meta = getattr(obj, "metadata", None)
    return meta.to_dict() if meta else {}


def _is_code_subscription(sub) -> bool:
    if _metadata(sub).get("product") == PRODUCT:
        return True
    return any(
        getattr(item.price, "lookup_key", None) == CODE_PRICE_LOOKUP_KEY
        for item in sub["items"].data
    )


def invoice_is_code(invoice) -> bool:
    """True when the invoice belongs to a ContextShrink Code subscription."""
    details = getattr(getattr(invoice, "parent", None), "subscription_details", None)
    return details is not None and _metadata(details).get("product") == PRODUCT


async def _is_code_subscription_id(subscription_id: str) -> bool:
    return bool(
        await db._conn().fetchval(
            "SELECT 1 FROM core.code_plans WHERE stripe_subscription_id=$1", subscription_id
        )
    )


async def sync_code_subscription(subscription_id: str) -> None:
    """Mirror a code subscription's current Stripe state onto the account."""
    sub = await billing._call(billing._stripe().v1.subscriptions.retrieve, subscription_id)
    customer_id = billing._expandable_id(sub.customer)
    user_id = await billing._user_for_customer(customer_id, _metadata(sub).get("user_id"))
    if user_id is None:
        logger.warning("Code subscription %s has no matching account", subscription_id)
        return
    period_end = billing._ts(sub["items"].data[0].current_period_end)
    cancel_at = billing._ts(sub.cancel_at) or (period_end if sub.cancel_at_period_end else None)
    # A late event for an older subscription must not overwrite the current one.
    await db._conn().execute(
        "INSERT INTO core.code_plans (user_id, stripe_subscription_id, status, current_period_end, "
        "cancel_at_period_end, updated_at) VALUES ($1,$2,$3,$4,$5,now()) "
        "ON CONFLICT (user_id) DO UPDATE SET stripe_subscription_id=EXCLUDED.stripe_subscription_id, "
        "status=EXCLUDED.status, current_period_end=EXCLUDED.current_period_end, "
        "cancel_at_period_end=EXCLUDED.cancel_at_period_end, updated_at=now() "
        "WHERE core.code_plans.stripe_subscription_id IS NULL "
        "OR core.code_plans.stripe_subscription_id=EXCLUDED.stripe_subscription_id "
        "OR core.code_plans.status IN ('canceled','incomplete_expired')",
        user_id,
        subscription_id,
        sub.status,
        period_end,
        cancel_at is not None,
    )


async def handle_stripe_event(event) -> bool:
    """Apply a ContextShrink Code event. False for every other event (billing handles it)."""
    obj = event.data.object
    kind = event.type
    if kind == "checkout.session.completed":
        if (
            _metadata(obj).get("product") != PRODUCT
            or obj.mode != "subscription"
            or not obj.subscription
        ):
            return False
        await sync_code_subscription(billing._expandable_id(obj.subscription))
        return True
    if kind in SUBSCRIPTION_EVENTS:
        if not _is_code_subscription(obj):
            return False
        await sync_code_subscription(obj.id)
        return True
    if kind in INVOICE_EVENTS:
        sub_id = billing._invoice_subscription_id(obj)
        if sub_id is None:
            return False
        if not (invoice_is_code(obj) or await _is_code_subscription_id(sub_id)):
            return False
        # Code invoices are never savings-fee invoices: only the plan state follows them.
        if kind != "invoice.created":
            await sync_code_subscription(sub_id)
        return True
    return False


# --------------------------------------------------------------- usage


def _usage_hour(value: str, now: datetime) -> datetime:
    try:
        hour = datetime.fromisoformat(value)
    except ValueError:
        raise HTTPException(422, "hour must be an ISO 8601 UTC hour") from None
    if hour.tzinfo is None:
        raise HTTPException(422, "hour must include a UTC offset")
    hour = hour.astimezone(timezone.utc)
    if hour.minute or hour.second or hour.microsecond:
        raise HTTPException(422, "hour must be a whole UTC hour")
    if hour > now or hour < now - USAGE_MAX_AGE:
        raise HTTPException(422, "hour must be within the last 35 days and not in the future")
    return hour


async def _usage_window(user_id, start: datetime) -> dict:
    row = await db._conn().fetchrow(
        "SELECT COALESCE(sum(requests),0)::bigint AS requests, "
        "COALESCE(sum(tokens_before),0)::bigint AS tokens_before, "
        "COALESCE(sum(tokens_after),0)::bigint AS tokens_after "
        "FROM metrics.code_usage_hourly WHERE user_id=$1 AND hour >= $2",
        user_id,
        start,
    )
    before, after = int(row["tokens_before"]), int(row["tokens_after"])
    saved = max(before - after, 0)
    return {
        "requests": int(row["requests"]),
        "tokens_before": before,
        "tokens_after": after,
        "tokens_saved": saved,
        "pct": round(saved / before * 100, 1) if before else 0.0,
    }


# -------------------------------------------------------------- routes


def install(app, current_user):
    async def account_user_id(authorization: str | None = Header(default=None)):
        # Usage totals are per account: a device key or a dashboard session both work.
        if _bearer(authorization).startswith("cs_dev_"):
            return (await _device_from_header(authorization))["user_id"]
        return (await current_user(authorization=authorization))["user_id"]

    @app.post("/code/cli/start")
    async def cli_start(body: CliStartIn, request: Request):
        if not _start_allowed(_client_address(request)):
            raise HTTPException(429, "Too many connect attempts. Wait a minute and try again.")
        name = body.device_name.strip()
        if not name:
            raise HTTPException(422, "Device name cannot be blank")
        conn = db._conn()
        now = _now()
        await conn.execute(
            "DELETE FROM core.code_cli_requests WHERE expires_at < $1", now - timedelta(days=1)
        )
        poll_secret = secrets.token_urlsafe(32)
        for _ in range(5):
            user_code = _new_user_code()
            request_id = uuid4()
            try:
                await conn.execute(
                    "INSERT INTO core.code_cli_requests (id, user_code_hash, poll_secret_hash, tool, "
                    "device_hash, device_name, platform, created_at, expires_at) "
                    "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)",
                    request_id,
                    _code_hash(user_code),
                    _hash(poll_secret),
                    body.tool,
                    body.device_hash,
                    name,
                    body.platform,
                    now,
                    now + CLI_REQUEST_TTL,
                )
                break
            except asyncpg.UniqueViolationError:
                continue  # Extremely rare user_code collision: draw another.
        else:
            raise HTTPException(503, "Could not start a connect request. Try again.")
        return {
            "request_id": str(request_id),
            "poll_secret": poll_secret,
            "user_code": user_code,
            "verify_url": f"{SITE_URL}/connect?code={user_code}",
            "interval": CLI_POLL_INTERVAL_S,
            "expires_in": int(CLI_REQUEST_TTL.total_seconds()),
        }

    @app.get("/code/cli/request")
    async def cli_request(code: str = Query(max_length=20), user=Depends(current_user)):
        conn = db._conn()
        req = await _pending_request(conn, _code_hash(code))
        devices = await _active_devices(conn, user["user_id"])
        matches_device = any(d["device_hash"] == req["device_hash"] for d in devices)
        return {
            "device_name": req["device_name"],
            "platform": req["platform"],
            "tool": req["tool"],
            "device_limit": {
                "reached": not matches_device and len(devices) >= DEVICE_LIMIT,
                "devices": [_device_out(d) for d in devices],
            },
        }

    @app.post("/code/cli/approve")
    async def cli_approve(body: CliCodeIn, user=Depends(current_user)):
        code_hash = _code_hash(body.user_code)
        now = _now()
        async with db._conn().acquire() as conn, conn.transaction():
            req = await conn.fetchrow(
                "SELECT id, tool, device_hash, device_name, platform, expires_at, approved_user_id "
                "FROM core.code_cli_requests WHERE user_code_hash=$1 FOR UPDATE",
                code_hash,
            )
            if req is None:
                raise HTTPException(404, "Code not found or expired")
            if req["approved_user_id"] is not None:
                # Approving twice (a double click) is harmless for the same account.
                if req["approved_user_id"] == user["user_id"]:
                    return {"status": "approved"}
                raise HTTPException(404, "Code not found or expired")
            if req["expires_at"] <= now:
                raise HTTPException(404, "Code not found or expired")
            # Serialise approvals for this account so the device limit holds.
            await conn.fetchval("SELECT id FROM core.users WHERE id=$1 FOR UPDATE", user["user_id"])
            devices = await _active_devices(conn, user["user_id"])
            same = next((d for d in devices if d["device_hash"] == req["device_hash"]), None)
            if same is not None:
                # Reusing this computer: its old key stops working; poll mints a new one.
                device_id = same["id"]
                await conn.execute(
                    "UPDATE core.code_devices SET name=$2, platform=$3, key_hash=NULL, "
                    "last_seen_at=now(), tools=ARRAY(SELECT DISTINCT t FROM "
                    "unnest(array_append(tools, $4::text)) AS t) WHERE id=$1",
                    device_id,
                    req["device_name"],
                    req["platform"],
                    req["tool"],
                )
            else:
                if len(devices) >= DEVICE_LIMIT:
                    # Returned, not raised: the marker must commit, so the terminal's
                    # poll can show the same devices while this request stays open.
                    await conn.execute(
                        "UPDATE core.code_cli_requests SET limit_user_id=$2 WHERE id=$1",
                        req["id"],
                        user["user_id"],
                    )
                    return JSONResponse(
                        status_code=409,
                        content={
                            "detail": {
                                "code": "device_limit",
                                "devices": [_device_out(d) for d in devices],
                            }
                        },
                    )
                device_id = await conn.fetchval(
                    "INSERT INTO core.code_devices (user_id, name, platform, device_hash, tools, "
                    "last_seen_at) VALUES ($1,$2,$3,$4,ARRAY[$5::text],now()) RETURNING id",
                    user["user_id"],
                    req["device_name"],
                    req["platform"],
                    req["device_hash"],
                    req["tool"],
                )
            plan = await _plan(conn, user["user_id"])
            if plan.get("trial_started_at") is None and plan.get("stripe_subscription_id") is None:
                await _start_trial(conn, user["user_id"], req["device_hash"], now)
            await conn.execute(
                "UPDATE core.code_cli_requests SET approved_user_id=$2, device_id=$3 WHERE id=$1",
                req["id"],
                user["user_id"],
                device_id,
            )
        return {"status": "approved"}

    @app.post("/code/cli/poll")
    async def cli_poll(body: CliPollIn):
        now = _now()
        async with db._conn().acquire() as conn, conn.transaction():
            req = await conn.fetchrow(
                "SELECT id, poll_secret_hash, approved_user_id, device_id, expires_at, consumed_at, "
                "limit_user_id FROM core.code_cli_requests WHERE id=$1 FOR UPDATE",
                body.request_id,
            )
            if req is None or not hmac.compare_digest(
                req["poll_secret_hash"], _hash(body.poll_secret)
            ):
                raise HTTPException(404, "Unknown connect request")
            if req["consumed_at"] is not None:
                return {"status": "expired"}  # The key was already delivered once.
            if req["approved_user_id"] is None:
                if req["expires_at"] <= now:
                    return {"status": "expired"}
                if req["limit_user_id"] is not None:
                    # The browser was refused by the device limit: show the terminal the
                    # account's devices. A later successful approval turns this to "approved".
                    devices = await _active_devices(conn, req["limit_user_id"])
                    return {
                        "status": "device_limit",
                        "devices": [_device_out(d) for d in devices],
                    }
                return {"status": "pending"}
            # The key exists only in this response: only its hash is stored.
            key = "cs_dev_" + secrets.token_urlsafe(32)
            device_id = await conn.fetchval(
                "UPDATE core.code_devices SET key_hash=$2 WHERE id=$1 AND revoked_at IS NULL "
                "RETURNING id",
                req["device_id"],
                _hash(key),
            )
            await conn.execute(
                "UPDATE core.code_cli_requests SET consumed_at=now() WHERE id=$1", req["id"]
            )
            if device_id is None:
                return {"status": "expired"}  # Revoked before the poll collected its key.
        return {
            "status": "approved",
            "device_id": str(device_id),
            "device_key": key,
            "api_url": API_URL,
        }

    @app.get("/code/account")
    async def account(user=Depends(current_user)):
        now = _now()
        conn = db._conn()
        plan = await _plan(conn, user["user_id"])
        devices = await _active_devices(conn, user["user_id"])
        return {
            "plan": {
                "state": _plan_state(plan, now),
                "trial_ends_at": _iso(plan.get("trial_ends_at")),
                "current_period_end": _iso(plan.get("current_period_end")),
                "cancel_at_period_end": bool(plan.get("cancel_at_period_end")),
                "entitled": _entitlement(plan, now) is not None,
            },
            "devices": [
                {
                    **_device_out(d),
                    "tools": list(d["tools"]),
                    "created_at": _iso(d["created_at"]),
                }
                for d in devices
            ],
            "device_limit": DEVICE_LIMIT,
        }

    @app.delete("/code/devices/{device_id}", status_code=204)
    async def revoke_device(device_id: UUID, user=Depends(current_user)):
        row = await db._conn().fetchrow(
            "UPDATE core.code_devices SET revoked_at=COALESCE(revoked_at, now()) "
            "WHERE id=$1 AND user_id=$2 RETURNING id",
            device_id,
            user["user_id"],
        )
        if row is None:
            raise HTTPException(404, "Device not found")
        return Response(status_code=204)

    @app.delete("/code/device", status_code=204)
    async def uninstall(device=Depends(device_auth_allow_revoked)):
        await db._conn().execute(
            "UPDATE core.code_devices SET revoked_at=COALESCE(revoked_at, now()) WHERE id=$1",
            device["id"],
        )
        return Response(status_code=204)

    @app.post("/code/entitlement")
    async def entitlement(body: EntitlementIn, device=Depends(device_auth)):
        if not hmac.compare_digest(body.device_hash, device["device_hash"]):
            raise HTTPException(403, "Device hash does not match this device key")
        signer = _signer()
        now = _now()
        conn = db._conn()
        await conn.execute(
            "UPDATE core.code_devices SET last_seen_at=now() WHERE id=$1", device["id"]
        )
        plan = await _plan(conn, device["user_id"])
        granted = _entitlement(plan, now)
        if granted is None:
            raise HTTPException(
                402,
                {"code": "plan_required", "trial_ended_at": _iso(plan.get("trial_ends_at"))},
            )
        name, until = granted
        iat = int(now.timestamp())
        until_s = int(until.timestamp())
        payload = {
            "v": 1,
            "iss": NOTE_ISSUER,
            "sub": str(device["user_id"]),
            "dev": str(device["id"]),
            "dh": device["device_hash"],
            "plan": name,
            "iat": iat,
            "exp": min(iat + NOTE_MAX_AGE_S, until_s),
            "until": until_s,
        }
        return {"token": _sign_note(payload, signer), "plan": name, "until": until_s}

    @app.post("/code/usage")
    async def usage(body: UsageIn, device=Depends(device_auth)):
        now = _now()
        # Validate every bucket before writing any, so a bad one rejects the batch.
        rows = [
            (
                device["user_id"],
                device["id"],
                _usage_hour(bucket.hour, now),
                bucket.tool,
                bucket.requests,
                bucket.tokens_before,
                bucket.tokens_after,
            )
            for bucket in body.buckets
        ]
        if rows:
            async with db._conn().acquire() as conn, conn.transaction():
                await conn.executemany(
                    "INSERT INTO metrics.code_usage_hourly (user_id, device_id, hour, tool, requests, "
                    "tokens_before, tokens_after, updated_at) VALUES ($1,$2,$3,$4,$5,$6,$7,now()) "
                    "ON CONFLICT (device_id, hour, tool) DO UPDATE SET requests=EXCLUDED.requests, "
                    "tokens_before=EXCLUDED.tokens_before, tokens_after=EXCLUDED.tokens_after, "
                    "updated_at=now()",
                    rows,
                )
        return {"accepted": len(rows)}

    @app.get("/code/usage/summary")
    async def usage_summary(user_id=Depends(account_user_id)):
        # UTC calendar days, the same clock the npx status line uses for "today".
        today = _now().replace(hour=0, minute=0, second=0, microsecond=0)
        return {
            "today": await _usage_window(user_id, today),
            "last_7d": await _usage_window(user_id, today - timedelta(days=6)),
            "last_30d": await _usage_window(user_id, today - timedelta(days=29)),
        }

    @app.post("/code/checkout")
    async def checkout(user=Depends(current_user)):
        plan = await _plan(db._conn(), user["user_id"])
        if plan.get("stripe_subscription_id") and plan.get("status") not in billing.ENDED_STATUSES:
            raise HTTPException(
                409,
                "You already have a ContextShrink Code subscription. Use Manage billing to change it.",
            )
        customer_id = await billing._ensure_customer(user)
        metadata = {"product": PRODUCT, "user_id": str(user["user_id"])}
        session = await billing._call(
            billing._stripe().v1.checkout.sessions.create,
            {
                "mode": "subscription",
                "customer": customer_id,
                "client_reference_id": str(user["user_id"]),
                "line_items": [
                    {"price": await billing._price_id(CODE_PRICE_LOOKUP_KEY), "quantity": 1}
                ],
                "metadata": metadata,
                "subscription_data": {"metadata": metadata},
                "success_url": f"{SITE_URL}/account?checkout=success",
                "cancel_url": f"{SITE_URL}/account?checkout=cancelled",
            },
        )
        return {"url": session.url}

    @app.post("/code/portal")
    async def portal(user=Depends(current_user)):
        row = await billing._billing_row(user["user_id"])
        if not row or not row["stripe_customer_id"]:
            raise HTTPException(404, "No billing account yet")
        session = await billing._call(
            billing._stripe().v1.billing_portal.sessions.create,
            {"customer": row["stripe_customer_id"], "return_url": f"{SITE_URL}/account"},
        )
        return {"url": session.url}
