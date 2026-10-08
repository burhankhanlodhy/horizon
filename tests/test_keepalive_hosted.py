"""Cache keep-alive on the hosted proxy: billing, the session end route, its auth.

Pings are real spend for the account that owns the session, so each lands in
the account ledger as its own row with negative savings, and a request that
resumes a kept-warm session carries the rewrite it avoided. Ending a session
goes through the gateway with the account key, and reaches only that
account's sessions.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from horizon.proxy.account_analytics import (
    AccountContext,
    AccountMiddleware,
    _account,
    record_account_outcome,
    record_keepalive_ping,
)
from horizon.proxy.outcome import RequestOutcome


class _Service:
    enabled = True

    def __init__(self, owners: dict[str, dict[str, Any]] | None = None) -> None:
        self.runtime_id = str(uuid4())
        self.events: list[dict[str, Any]] = []
        self.owners = owners or {}

    async def authorize(self, key: str, scope: str):
        data = self.owners.get(key)
        return (200, data) if data else (401, None)

    def _enqueue(self, event: dict[str, Any]) -> None:
        self.events.append(event)


def _context(service: _Service) -> AccountContext:
    return AccountContext(str(uuid4()), str(uuid4()), service)


# -- ledger rows ------------------------------------------------------------------


def test_a_ping_is_its_own_row_with_negative_savings() -> None:
    service = _Service()
    context = _context(service)
    record = {
        "model": "claude-opus-5-5",
        "status": 200,
        "read": 400_000,
        "write": 0,
        "uncached": 12,
        "cost_usd": 0.08,
        "pricing_basis": "catalog",
        "latency_ms": 900.0,
        "meta": {"project": "demo", "agent": "claude"},
    }
    asyncio.run(record_keepalive_ping(context, record))
    (row,) = service.events
    assert row["kind"] == "keepalive" and row["transforms"] == ["cache_keepalive:ping"]
    assert row["savings_usd"] == -0.08 and row["cost_usd"] == 0.08
    assert row["tokens_in"] == 400_012 and row["cache_read"] == 400_000
    assert (row["user_id"], row["key_id"]) == (context.user_id, context.key_id)
    assert (row["project"], row["agent"]) == ("demo", "claude")


def test_a_ping_with_no_answer_is_labelled_estimated() -> None:
    service = _Service()
    asyncio.run(
        record_keepalive_ping(
            _context(service),
            {"model": "m", "status": 0, "read": 1000, "cost_usd": 0.01, "estimated": True},
        )
    )
    (row,) = service.events
    assert row["status"] == 504 and row["pricing_basis"].endswith(":estimated")


def _outcome(**kw: Any) -> RequestOutcome:
    return RequestOutcome(
        request_id=kw.pop("request_id", "r1"),
        provider="anthropic",
        model="claude-opus-5-5",
        original_tokens=400_000,
        optimized_tokens=400_000,
        output_tokens=50,
        tokens_saved=0,
        attempted_input_tokens=400_000,
        cache_read_tokens=392_000,
        cache_write_tokens=8_000,
        status_code=kw.pop("status_code", 200),
        **kw,
    )


@pytest.mark.parametrize(("status", "credited"), [(200, 1.5), (500, 0)])
def test_a_resumed_request_carries_the_rewrite_it_avoided(status, credited) -> None:
    service = _Service()
    token = _account.set(_context(service))
    try:
        asyncio.run(record_account_outcome(_outcome(status_code=status), keepalive_usd=1.5))
    finally:
        _account.reset(token)
    (row,) = service.events
    assert row.get("keepalive_usd", 0) == credited and "kind" not in row
    assert row["savings_usd"] >= credited


def test_an_ordinary_request_row_keeps_its_old_shape() -> None:
    """Rows without keep-alive savings stay valid for an API not yet upgraded."""
    service = _Service()
    token = _account.set(_context(service))
    try:
        asyncio.run(record_account_outcome(_outcome()))
    finally:
        _account.reset(token)
    (row,) = service.events
    assert "keepalive_usd" not in row and "kind" not in row


# -- middleware ---------------------------------------------------------------------


def _call(service: _Service, method: str, path: str, key: str | None) -> tuple[int, Any]:
    seen: dict[str, Any] = {}

    async def app(scope, receive, send):
        seen["account"] = _account.get()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"{}"})

    async def receive():
        return {"type": "http.request", "body": b"{}", "more_body": False}

    sent: list[dict[str, Any]] = []

    async def send(message):
        sent.append(message)

    headers = [(b"x-horizon-proxy-token", key.encode())] if key else []
    scope = {"type": "http", "method": method, "path": path, "headers": headers}
    asyncio.run(AccountMiddleware(app, service)(scope, receive, send))
    status = next(m for m in sent if m["type"] == "http.response.start")["status"]
    return status, seen.get("account")


def test_the_end_route_needs_an_account_key() -> None:
    owner = {"user_id": str(uuid4()), "key_id": str(uuid4())}
    service = _Service({"cs_live_a": owner})
    status, account = _call(service, "POST", "/v1/horizon/keepalive/end", "cs_live_a")
    assert status == 200 and account.user_id == owner["user_id"]
    assert _call(service, "POST", "/v1/horizon/keepalive/end", None)[0] == 401
    assert _call(service, "POST", "/v1/horizon/keepalive/end", "cs_live_bad")[0] == 401
    assert _call(service, "GET", "/v1/horizon/keepalive/end", "cs_live_a")[0] == 405
    assert _call(service, "POST", "/p/x/v1/horizon/keepalive/end", "cs_live_a")[0] == 404
    assert service.events == []  # lifecycle calls are not inference: no ledger row


# -- the route on a hosted proxy ------------------------------------------------------


def test_an_account_ends_only_its_own_session(monkeypatch, tmp_path) -> None:
    from horizon.proxy.account_analytics import AccountAnalytics
    from horizon.proxy.cache_keeper import CacheKeeper
    from horizon.proxy.server import ProxyConfig, create_app

    u1, u2 = str(uuid4()), str(uuid4())
    owners = {
        "cs_live_one": {"user_id": u1, "key_id": str(uuid4())},
        "cs_live_two": {"user_id": u2, "key_id": str(uuid4())},
    }

    async def authorize(self, key, scope):
        data = owners.get(key)
        return (200, data) if data else (401, None)

    monkeypatch.setenv("CONTEXTSHRINK_API_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("CONTEXTSHRINK_SERVICE_TOKEN", "test-only")
    monkeypatch.setenv("HORIZON_WORKSPACE_DIR", str(tmp_path))
    monkeypatch.setattr(AccountAnalytics, "authorize", authorize)
    app = create_app(
        ProxyConfig(
            optimize=False,
            cache_enabled=False,
            rate_limit_enabled=False,
            cost_tracking_enabled=False,
        )
    )

    async def never(url, headers, body):  # pragma: no cover - nothing is due
        raise AssertionError("no ping expected")

    keeper = app.state.proxy.cache_keeper = CacheKeeper(never)
    body = {
        "tools": [{"name": "Read"}],
        "messages": [
            {
                "role": "user",
                "content": [{"type": "text", "text": "hi", "cache_control": {"type": "ephemeral"}}],
            }
        ],
    }
    keeper.record_request(
        "r1", url="u", headers={}, body=body, liveness_id="L", owner=SimpleNamespace(user_id=u1)
    )
    keeper.record_usage("r1", model="m", cache_read=40_000, cache_write=0, uncached=0)

    client = TestClient(app)

    def end(key: str | None) -> Any:
        headers = {"x-horizon-proxy-token": key} if key else {}
        return client.post("/v1/horizon/keepalive/end", json={"id": "L"}, headers=headers)

    assert end(None).status_code == 401
    assert end("cs_live_two").json() == {"ended": False}  # same id, other account
    assert len(keeper._groups) == 1
    assert end("cs_live_one").json() == {"ended": True}
    assert not keeper._groups
