"""Every uploaded proxy event carries ``auth_mode`` (spec section 4: savings-fee fairness)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from horizon.proxy.account_analytics import (
    AccountContext,
    AccountMiddleware,
    record_account_outcome,
    record_keepalive_ping,
)
from horizon.proxy.outcome import RequestOutcome


class _Service:
    enabled = True

    def __init__(self) -> None:
        self.runtime_id = str(uuid4())
        self.events: list[dict[str, Any]] = []
        self._data = {"user_id": str(uuid4()), "key_id": str(uuid4()), "compression_allowed": True}

    async def authorize(self, key: str, scope: str):
        return 200, self._data

    def _enqueue(self, event: dict[str, Any]) -> None:
        self.events.append(event)


def _outcome() -> RequestOutcome:
    return RequestOutcome(
        request_id="req-1",
        provider="anthropic",
        model="claude-sonnet-4-6",
        original_tokens=1000,
        optimized_tokens=600,
        output_tokens=20,
        tokens_saved=400,
        attempted_input_tokens=1000,
        status_code=200,
    )


def _event_for(headers: list[tuple[bytes, bytes]]) -> dict[str, Any]:
    service = _Service()

    async def app(scope, receive, send):
        await record_account_outcome(_outcome())
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"{}"})

    async def receive():
        return {"type": "http.request", "body": b"{}", "more_body": False}

    async def send(message):
        return None

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/v1/messages",
        "headers": [(b"x-horizon-proxy-token", b"cs_live_test"), *headers],
    }
    asyncio.run(AccountMiddleware(app, service)(scope, receive, send))
    assert len(service.events) == 1
    return service.events[0]


@pytest.mark.parametrize(
    "headers,expected",
    [
        # A Claude Code session carries an OAuth token but is a subscription client.
        (
            [(b"user-agent", b"claude-cli/2.1.0"), (b"authorization", b"Bearer sk-ant-oat01-abc")],
            "subscription",
        ),
        ([(b"authorization", b"Bearer sk-ant-oat01-abc")], "oauth"),
        ([(b"authorization", b"Bearer sk-ant-api03-abc")], "payg"),
        ([(b"authorization", b"Bearer aaa.bbb.ccc")], "oauth"),
        ([(b"x-api-key", b"sk-ant-api03-abc")], "payg"),
    ],
)
def test_request_event_carries_the_inbound_auth_mode(headers, expected) -> None:
    event = _event_for(headers)
    assert event["auth_mode"] == expected


def test_event_keeps_its_existing_fields() -> None:
    event = _event_for([(b"x-api-key", b"sk-ant-api03-abc")])
    assert event["tokens_before"] == 1000
    assert event["tokens_after"] == 600
    assert event["status"] == 200


def test_keepalive_ping_bills_under_the_owning_session_mode() -> None:
    service = _Service()
    context = AccountContext(
        str(uuid4()),
        str(uuid4()),
        service,
        auth_mode="subscription",
    )
    record = {
        "meta": {},
        "cost_usd": 0.02,
        "read": 1000,
        "write": 0,
        "uncached": 5,
        "status": 200,
        "provider": "anthropic",
        "model": "claude-sonnet-4-6",
    }
    asyncio.run(record_keepalive_ping(context, record))
    assert service.events[0]["kind"] == "keepalive"
    assert service.events[0]["auth_mode"] == "subscription"


def test_context_defaults_to_payg() -> None:
    context = AccountContext(str(uuid4()), str(uuid4()), SimpleNamespace(runtime_id="r"))
    assert context.auth_mode == "payg"
