"""Verified account context and a durable proxy-to-control-plane outbox.

Enabled only when CONTEXTSHRINK_API_URL and its service credential are set.
Raw prompts, responses, provider credentials and arbitrary tags never enter
this ledger. ContextVars follow streaming and WebSocket tasks automatically.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import sqlite3
import time
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4, uuid5

import httpx

logger = logging.getLogger("horizon.proxy.accounts")


@dataclass
class AccountContext:
    user_id: str
    key_id: str
    service: AccountAnalytics
    # False once the account's plan cap is reached (Free: $20 saved per UTC
    # month). The control plane decides; the proxy only honours it.
    compression_allowed: bool = True
    emitted: set[str] = field(default_factory=set)


_account: ContextVar[AccountContext | None] = ContextVar("verified_proxy_account", default=None)


def account_id() -> str | None:
    context = _account.get()
    return context.user_id if context else None


def compression_paused() -> bool:
    """True when the verified account must be served as plain passthrough."""
    context = _account.get()
    return context is not None and not context.compression_allowed


COMPRESSION_STATUS_HEADER = (b"x-contextshrink-compression", b"paused")


def tenant_key(key: str | None) -> str | None:
    """Namespace client-controlled session/cache keys by verified account UUID."""
    uid = account_id()
    return hashlib.sha256((uid + "\0" + key).encode()).hexdigest() if uid and key else key


# Deliberately exclude admin, telemetry, global stats and response lookup URLs.
# /inference/v1/chat/completions is IBM Bob's chat path (horizon.providers.bob).
# Provider response IDs are credentials owned by the upstream account, so shared
# proxy users must not enumerate/retrieve responses through passthrough routes.
INFERENCE = re.compile(
    r"^(?:/p/[^/]+)?(?:/k/[A-Za-z0-9_-]{1,64})?(?:/(?:anthropic/)?v1/messages|/(?:v1/)?chat/completions|/(?:v1/(?:codex/)?|backend-api/(?:codex/)?)?responses|/v1(?:beta)?/models/[^/]+:(?:generateContent|streamGenerateContent)|/inference/v1/chat/completions)/?$"
)
AUXILIARY = re.compile(
    r"^(?:/p/[^/]+)?(?:/k/[A-Za-z0-9_-]{1,64})?(?:/v1/models(?:/[^/]+)?|/v1/messages/count_tokens|/v1(?:beta)?/models/[^/]+:countTokens)/?$"
)
# Session lifecycle: ``wrap`` reports its tool exited (cache keep-alive). The
# route ends only sessions owned by the verified account.
LIFECYCLE = re.compile(r"^/v1/horizon/keepalive/end/?$")


class AccountAnalytics:
    def __init__(self):
        self.url = os.environ.get("CONTEXTSHRINK_API_URL", "").rstrip("/")
        self.token = os.environ.get("CONTEXTSHRINK_SERVICE_TOKEN", "")
        if bool(self.url) != bool(self.token):
            raise RuntimeError("Account analytics requires both the API URL and service token")
        self.enabled = bool(self.url)
        self.runtime_id = str(uuid4())
        self.started_at = datetime.now(timezone.utc).isoformat()
        self.client = None
        self.worker = None
        self.registered = False
        self.path = (
            Path(os.environ.get("HORIZON_WORKSPACE_DIR", str(Path.home() / ".horizon")))
            / "account_analytics_outbox.sqlite3"
        )

    def _db(self):
        conn = sqlite3.connect(self.path, timeout=15)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    async def start(self):
        if not self.enabled:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._db() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS outbox(event_id TEXT PRIMARY KEY,payload TEXT NOT NULL)"
            )
        if os.name != "nt":
            self.path.chmod(0o600)
        self.client = httpx.AsyncClient(
            timeout=5, headers={"X-ContextShrink-Service-Token": self.token}, trust_env=False
        )
        self.worker = asyncio.create_task(self._run(), name="account-analytics-outbox")

    async def stop(self):
        if self.worker:
            self.worker.cancel()
            await asyncio.gather(self.worker, return_exceptions=True)
        if self.client:
            await self.client.aclose()

    def _enqueue(self, event):
        with self._db() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO outbox VALUES (?,?)",
                (event["event_id"], json.dumps(event, allow_nan=False)),
            )

    def _batch(self):
        with self._db() as conn:
            return conn.execute(
                "SELECT event_id,payload FROM outbox ORDER BY rowid LIMIT 100"
            ).fetchall()

    def _ack(self, batch):
        with self._db() as conn:
            conn.executemany("DELETE FROM outbox WHERE event_id=?", [(row[0],) for row in batch])

    async def _run(self):
        retry = 1
        while True:
            try:
                if not self.registered:
                    result = await self.client.post(
                        self.url + "/internal/analytics/run",
                        json={"runtime_id": self.runtime_id, "started_at": self.started_at},
                    )
                    result.raise_for_status()
                    self.registered = True
                batch = await asyncio.to_thread(self._batch)
                if batch:
                    result = await self.client.post(
                        self.url + "/internal/analytics/events",
                        json={"events": [json.loads(r[1]) for r in batch]},
                    )
                    result.raise_for_status()
                    await asyncio.to_thread(self._ack, batch)
                retry = 1
                await asyncio.sleep(1 if not batch else 0.1)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Do not log requests/payloads/keys. Retain every unacknowledged row.
                logger.warning(
                    "Account analytics delivery unavailable (%s); retrying; events retained",
                    type(exc).__name__,
                )
                await asyncio.sleep(retry)
                retry = min(retry * 2, 30)

    async def authorize(self, key, required_scope):
        if not self.client:
            return 503, None
        try:
            result = await self.client.post(
                self.url + "/internal/proxy/authorize", json={"key": key, "scope": required_scope}
            )
            if result.status_code in (401, 403):
                return result.status_code, None
            result.raise_for_status()
            data = result.json()
            UUID(data["user_id"])
            UUID(data["key_id"])
            return 200, data
        except Exception:
            return 503, None


async def record_account_outcome(
    outcome, *, saved=0, tool_saved=0, retained=0, keepalive_usd=0.0, project=None
):
    context = _account.get()
    if context is None or outcome.request_id in context.emitted:
        return
    # Reserve before yielding; streaming finalizers are shielded by the proxy.
    context.emitted.add(outcome.request_id)
    success = outcome.status_code < 400
    prices = {}
    cost = 0.0
    policy = {}
    if success:
        try:
            from horizon.proxy.savings_tracker import (
                _estimate_input_cost_usd,
                _estimate_output_cost_usd,
                estimate_request_savings_usd,
            )

            prices = estimate_request_savings_usd(
                outcome.model,
                compression_tokens_saved=saved,
                tool_schema_tokens_saved=tool_saved,
                retained_tokens_saved=retained,
                cache_read_tokens=outcome.cache_read_tokens,
                cache_write_tokens=outcome.cache_write_tokens,
                cache_write_5m_tokens=outcome.cache_write_5m_tokens,
                cache_write_1h_tokens=outcome.cache_write_1h_tokens,
                uncached_input_tokens=outcome.uncached_input_tokens,
                cache_inferred=outcome.cache_inferred,
                local_input_tokens=outcome.optimized_tokens,
                provider=outcome.provider,
            )
            if not outcome.from_response_cache:
                cost = _estimate_input_cost_usd(
                    outcome.model,
                    outcome.provider_input_tokens or outcome.optimized_tokens,
                    cache_read_tokens=outcome.cache_read_tokens,
                    # An inferred write (OpenAI) is the uncached tokens again,
                    # with no write premium: pricing both billed them twice.
                    cache_write_tokens=0 if outcome.cache_inferred else outcome.cache_write_tokens,
                    uncached_input_tokens=outcome.uncached_input_tokens,
                ) + _estimate_output_cost_usd(outcome.model, outcome.output_tokens)
            # Flash Observations and the price policies (fast mode, Flex,
            # model modernization) lower the bill without removing tokens
            # before the request is counted; priced from this request's usage.
            from horizon.proxy import forwarded_size, policy_savings
            from horizon.proxy.flex_policy import served_flex

            priced = policy_savings.price(
                outcome, flex_served=served_flex(), forwarded_bytes=forwarded_size.get()
            )
            policy = {k: round(v, 8) for k, v in priced.usd.items()}
            cost = max(0.0, cost + priced.cost_delta)
        except Exception as exc:
            # A pricing-catalog failure must not discard measured token usage.
            logger.warning(
                "Account pricing unavailable (%s); retaining token measurements", type(exc).__name__
            )
            prices = {}
            cost = 0.0
            policy = {}
    event = {
        "event_id": str(
            uuid5(UUID(context.service.runtime_id), context.key_id + ":" + outcome.request_id)
        ),
        "user_id": context.user_id,
        "key_id": context.key_id,
        "runtime_id": context.service.runtime_id,
        "occurred_at": datetime.now(timezone.utc).isoformat(),
        "request_id": outcome.request_id,
        "provider": str(outcome.provider)[:100],
        "model": str(outcome.model)[:200],
        "project": str(project)[:100] if project else None,
        "agent": str(outcome.client)[:100] if outcome.client else None,
        "status": outcome.status_code,
        "tokens_in": max(0, outcome.provider_input_tokens or outcome.optimized_tokens)
        if success
        else 0,
        "tokens_out": max(0, outcome.output_tokens) if success else 0,
        "tokens_before": max(0, outcome.original_tokens),
        "tokens_after": max(0, outcome.optimized_tokens),
        "tokens_saved": max(0, saved + tool_saved) if success else 0,
        "cache_read": max(0, outcome.cache_read_tokens) if success else 0,
        "cache_write": max(0, outcome.cache_write_tokens) if success else 0,
        "response_cached": outcome.from_response_cache,
        "latency_ms": max(0, outcome.total_latency_ms),
        "overhead_ms": max(0, outcome.overhead_ms),
        "ttfb_ms": max(0, outcome.ttfb_ms),
        # ``keepalive_usd``: the cache rewrite this request avoided because the
        # keep-alive held its session warm. The pings that paid for it are
        # their own rows (``record_keepalive_ping``), so this is the gross figure.
        "savings_usd": prices.get("compression", 0)
        + prices.get("tool_schema", 0)
        + prices.get("retained", 0)
        + (keepalive_usd if success else 0)
        + sum(policy.values()),
        "cost_usd": cost,
        "pricing_basis": prices.get("basis", "unavailable"),
        "transforms": [str(t)[:150] for t in outcome.transforms_applied][:100],
    }
    if success and keepalive_usd > 0:
        # Only when present: an ordinary row keeps the shape older APIs accept.
        event["keepalive_usd"] = keepalive_usd
    if policy:
        # Per feature, already inside savings_usd. Same rule as keepalive_usd.
        event["policy_usd"] = policy
    await asyncio.to_thread(context.service._enqueue, event)


async def record_keepalive_ping(context: AccountContext, record: dict) -> None:
    """Bill one cache keep-alive ping to the account that owns the session.

    A ping is real spend whether or not the user comes back, so it lands as its
    own ledger row with negative savings. The rewrite a resumed session avoids
    is credited on that request (``keepalive_usd``); summed, the account sees
    the feature's net effect, abandoned sessions included.
    """
    meta = record.get("meta") or {}
    request_id = "keepalive_" + uuid4().hex
    cost = max(0.0, float(record.get("cost_usd") or 0))
    read, write, uncached = (int(record.get(k) or 0) for k in ("read", "write", "uncached"))
    status = int(record.get("status") or 0)
    event = {
        "event_id": str(uuid5(UUID(context.service.runtime_id), context.key_id + ":" + request_id)),
        "user_id": context.user_id,
        "key_id": context.key_id,
        "runtime_id": context.service.runtime_id,
        "occurred_at": datetime.now(timezone.utc).isoformat(),
        "request_id": request_id,
        "provider": str(record.get("provider") or "anthropic")[:100],
        "model": str(record.get("model") or "unknown")[:200],
        "project": str(meta["project"])[:100] if meta.get("project") else None,
        "agent": str(meta["agent"])[:100] if meta.get("agent") else None,
        # A ping whose outcome never arrived (timeout) is booked as a full read.
        "status": status if 100 <= status <= 599 else 504,
        "tokens_in": read + write + uncached,
        "cache_read": read,
        "cache_write": write,
        "latency_ms": max(0.0, float(record.get("latency_ms") or 0)),
        "savings_usd": -cost,
        "cost_usd": cost,
        "pricing_basis": str(record.get("pricing_basis") or "unavailable")[:100]
        + (":estimated" if record.get("estimated") else ""),
        "transforms": ["cache_keepalive:ping"],
        "kind": "keepalive",
    }
    await asyncio.to_thread(context.service._enqueue, event)


class AccountMiddleware:
    def __init__(self, app, service):
        self.app = app
        self.service = service

    async def __call__(self, scope, receive, send):
        if not self.service.enabled or scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        path = scope["path"]
        is_inference = bool(INFERENCE.fullmatch(path))
        is_auxiliary = bool(AUXILIARY.fullmatch(path))
        is_lifecycle = bool(LIFECYCLE.fullmatch(path))
        if not is_inference and not is_auxiliary and not is_lifecycle:
            # Only operator/UI paths remain accessible to the inner loopback guards.
            # Deny any unmatched provider/catch-all request in account mode.
            if (
                path == "/"
                or path
                in (
                    "/livez",
                    "/readyz",
                    "/health",
                    "/stats",
                    "/stats-lifetime",
                    "/stats-history",
                    "/metrics",
                    "/quota",
                    "/subscription-window",
                    "/favicon.ico",
                )
                or path.startswith(
                    ("/dashboard", "/settings", "/admin/", "/debug/", "/transformations/")
                )
            ):
                return await self.app(scope, receive, send)
            return await self._deny(scope, send, 404, "Unsupported account proxy endpoint")
        if (scope["type"] == "websocket" and not path.rstrip("/").endswith("responses")) or (
            scope["type"] == "http" and (is_inference or is_lifecycle) and scope["method"] != "POST"
        ):
            return await self._deny(scope, send, 405, "Method not allowed")
        headers = {
            k.decode("latin1").lower(): v.decode("latin1") for k, v in scope.get("headers", [])
        }
        key = headers.get("x-horizon-proxy-token", "")
        # Also accept an account key where clients permit only their usual key field.
        credential_headers = set()
        for name in ("authorization", "x-api-key"):
            value = headers.get(name, "").removeprefix("Bearer ").strip()
            if value.startswith("cs_live_"):
                credential_headers.add(name)
                if not key:
                    key = value
        if not key:
            return await self._deny(
                scope, send, 401, "Account proxy key required (X-Horizon-Proxy-Token)"
            )
        required = "proxy:responses" if path.rstrip("/").endswith("responses") else "proxy:messages"
        status, data = await self.service.authorize(key, required)
        if data is None:
            return await self._deny(
                scope,
                send,
                status,
                "Invalid/revoked proxy key or unavailable authentication service",
            )
        context = AccountContext(
            data["user_id"],
            data["key_id"],
            self.service,
            compression_allowed=data.get("compression_allowed", True) is not False,
        )
        scope.setdefault("state", {})["account_user_id"] = context.user_id
        # Never send account or service credentials upstream, or trust identity hints.
        excluded = {
            "x-horizon-user-id",
            "x-horizon-proxy-token",
            "x-contextshrink-service-token",
        } | credential_headers
        scope["headers"] = [
            (k, v)
            for k, v in scope.get("headers", [])
            if k.decode("latin1").lower() not in excluded
        ]
        token = _account.set(context)
        started = time.monotonic()
        response_status = 499

        async def wrapped_send(message):
            nonlocal response_status
            if message["type"] == "http.response.start":
                response_status = message["status"]
                if not context.compression_allowed:
                    message = {
                        **message,
                        "headers": [*message.get("headers", []), COMPRESSION_STATUS_HEADER],
                    }
            await send(message)

        async def wrapped_receive():
            message = await receive()
            if message["type"] == "websocket.receive":
                # Revalidate each incoming turn; revoked keys cannot keep using
                # a long-lived socket. A turn already sent upstream may finish.
                status, owner = await self.service.authorize(key, required)
                if (
                    owner is None
                    or owner["user_id"] != context.user_id
                    or owner["key_id"] != context.key_id
                ):
                    await send({"type": "websocket.close", "code": 1008})
                    return {"type": "websocket.disconnect", "code": 1008}
                # Picks up a cap reached mid-socket; per-connection policy
                # computed at accept time applies again on reconnect.
                context.compression_allowed = owner.get("compression_allowed", True) is not False
            return message

        try:
            await self.app(scope, wrapped_receive, wrapped_send)
        finally:
            try:
                if is_inference and not context.emitted and scope["type"] == "http":
                    from horizon.proxy.outcome import RequestOutcome
                    from horizon.proxy.project_context import get_current_project

                    fallback = RequestOutcome(
                        request_id="unmeasured_" + str(uuid4()),
                        provider="unknown",
                        model="unknown",
                        original_tokens=0,
                        optimized_tokens=0,
                        output_tokens=0,
                        tokens_saved=0,
                        attempted_input_tokens=0,
                        status_code=response_status,
                        total_latency_ms=(time.monotonic() - started) * 1000,
                    )
                    await asyncio.shield(
                        record_account_outcome(fallback, project=get_current_project())
                    )
            finally:
                _account.reset(token)

    @staticmethod
    async def _deny(scope, send, status, message):
        body = json.dumps({"error": {"type": "proxy_auth_error", "message": message}}).encode()
        if scope["type"] == "websocket":
            if "websocket.http.response" in scope.get("extensions", {}):
                await send(
                    {
                        "type": "websocket.http.response.start",
                        "status": status,
                        "headers": [(b"content-type", b"application/json")],
                    }
                )
                await send({"type": "websocket.http.response.body", "body": body})
            else:
                await send({"type": "websocket.close", "code": 1008})
        else:
            await send(
                {
                    "type": "http.response.start",
                    "status": status,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"cache-control", b"no-store"),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": body})
