"""The proxy's verified-savings figure: its wire form, its gate, and its ledger tie-in.

The figure is the cache-aware saving the savings ledger records. It leaves the
proxy only for requests that carry ``x-horizon-forwarder: claude-code``: as one
SSE comment before a streamed close, or as a header on a non-streamed reply.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from horizon import savings_ledger
from horizon.proxy.server import HorizonProxy
from horizon.verified_savings import (
    FORWARDER_HEADER,
    VERIFIED_SAVINGS_HEADER,
    is_claude_code_forwarder,
    parse_sse_comment_value,
    sse_comment_line,
)

UPSTREAM = (
    b'event: message_start\ndata: {"type":"message_start","message":{"id":"m1"}}\n\n'
    b'event: message_stop\ndata: {"type":"message_stop"}\n\n'
)


# ----------------------------------------------------------------- wire form


def test_comment_line_is_one_spec_comment_then_a_blank_line() -> None:
    line = sse_comment_line(0.4321)
    assert line == b": x-horizon-verified-savings-usd=0.432100\n\n"
    assert line.count(b"\n\n") == 1
    assert parse_sse_comment_value(line.split(b"\n\n")[0]) == pytest.approx(0.4321)


def test_gate_is_only_the_claude_code_forwarder_tag() -> None:
    assert is_claude_code_forwarder({FORWARDER_HEADER: "claude-code"})
    assert is_claude_code_forwarder({FORWARDER_HEADER: " Claude-Code "})
    assert not is_claude_code_forwarder({FORWARDER_HEADER: "other"})
    assert not is_claude_code_forwarder({})
    assert not is_claude_code_forwarder(None)


# ----------------------------------------------------------------- ledger tie-in


def _ledger_cost(tmp_path, **kwargs) -> float:
    path = tmp_path / "ledger.jsonl"
    assert savings_ledger.record_savings_event(path=path, **kwargs)
    event = json.loads(path.read_text(encoding="utf-8").splitlines()[-1])
    return float(event.get("cost_effective_usd", event["cost_usd"]))


@pytest.mark.parametrize(
    "cache",
    [
        {"cache_read_tokens": 5000, "uncached_input_tokens": 200, "provider": "anthropic"},
        {},  # no cache mix reported: the ledger stores only cost_usd
    ],
    ids=["cache-mix", "no-mix"],
)
def test_verified_figure_is_exactly_the_ledger_saving(tmp_path, cache: dict) -> None:
    common = {
        "tokens_before": 1000,
        "tokens_after": 600,
        "model": "claude-sonnet-4-5",
        "saved_compression": 300,
        "saved_tool_schema": 100,
    }
    verified = savings_ledger.verified_saving_usd(
        **common,
        **{k: v for k, v in cache.items() if k != "provider"},
        provider=cache.get("provider"),
    )
    recorded = _ledger_cost(tmp_path, client="claude-code", source="proxy", **common, **cache)
    assert verified > 0
    assert verified == pytest.approx(recorded, abs=1e-6)


def test_nothing_saved_means_no_verified_figure() -> None:
    assert (
        savings_ledger.verified_saving_usd(
            tokens_before=100,
            tokens_after=100,
            model="claude-sonnet-4-5",
            saved_compression=0,
            saved_tool_schema=0,
        )
        == 0.0
    )


# ----------------------------------------------------------------- streaming


def _proxy() -> HorizonProxy:
    """A HorizonProxy with only what the streaming finalizer touches, as the stream tests do."""
    proxy = object.__new__(HorizonProxy)
    proxy.http_client = MagicMock(spec=httpx.AsyncClient)
    proxy.metrics = MagicMock()
    proxy.metrics.record_request = AsyncMock(return_value=None)
    proxy.metrics.record_failed = AsyncMock(return_value=None)
    proxy.metrics.record_rate_limited = AsyncMock(return_value=None)
    proxy.cost_tracker = MagicMock()
    proxy.stats = {
        "requests_total": 0,
        "requests_optimized": 0,
        "tokens": {"original": 0, "optimized": 0, "saved": 0},
        "cost": {"total_usd": 0, "savings_usd": 0},
        "errors": 0,
        "active_requests": 0,
        "requests_per_model": {},
    }
    proxy.memory_manager = None
    proxy._config = MagicMock()
    proxy._config.memory_enabled = False
    proxy._config.ccr_inject_tool = False
    proxy._config.retry_max_attempts = 3
    proxy._config.retry_base_delay_ms = 0
    proxy._config.retry_max_delay_ms = 0
    proxy.config = proxy._config
    proxy.memory_handler = None
    # The funnel's figure, as emit_request_outcome returns it.
    proxy._record_request_outcome = AsyncMock(return_value=0.4321)
    return proxy


def _upstream_response(payload: bytes = UPSTREAM) -> AsyncMock:
    response = AsyncMock()
    response.headers = httpx.Headers({"content-type": "text/event-stream"})
    response.status_code = 200

    async def aiter_bytes():
        yield payload

    response.aiter_bytes = aiter_bytes
    response.aclose = AsyncMock()
    return response


async def _stream(tagged: bool, payload: bytes = UPSTREAM) -> bytes:
    proxy = _proxy()
    response = _upstream_response(payload)
    proxy.http_client.build_request = MagicMock(return_value=MagicMock())
    proxy.http_client.send = AsyncMock(return_value=response)
    result = await proxy._stream_response(
        url="https://api.anthropic.com/v1/messages",
        headers={"x-api-key": "sk-test", "anthropic-version": "2023-06-01"},
        body={
            "model": "claude-sonnet-4-5",
            "max_tokens": 1,
            "stream": True,
            "messages": [{"role": "user", "content": "hi"}],
        },
        provider="anthropic",
        model="claude-sonnet-4-5",
        request_id="req-1",
        original_tokens=10,
        optimized_tokens=10,
        tokens_saved=0,
        transforms_applied=[],
        tags={},
        optimization_latency=0.0,
        forwarder_tagged=tagged,
    )
    return b"".join([chunk async for chunk in result.body_iterator])


def _spec_events(stream: bytes) -> list[tuple[str, str]]:
    """A minimal SSE parser per the spec: comments are ignored."""
    events = []
    for block in stream.split(b"\n\n"):
        name, data = "message", []
        for raw in block.split(b"\n"):
            if not raw or raw.startswith(b":"):
                continue
            field, _, value = raw.partition(b":")
            value = value[1:] if value.startswith(b" ") else value
            if field == b"event":
                name = value.decode()
            elif field == b"data":
                data.append(value.decode())
        if data:
            events.append((name, "\n".join(data)))
    return events


@pytest.mark.asyncio
async def test_tagged_stream_closes_with_one_comment_before_the_end() -> None:
    body = await _stream(tagged=True)

    comment = b": x-horizon-verified-savings-usd=0.432100\n\n"
    assert body.count(b"x-horizon-verified-savings-usd") == 1
    assert body == UPSTREAM + comment
    assert body.endswith(comment)


@pytest.mark.asyncio
async def test_a_client_that_ignores_comments_parses_the_same_events() -> None:
    body = await _stream(tagged=True)
    assert _spec_events(body) == _spec_events(UPSTREAM)


@pytest.mark.asyncio
async def test_untagged_stream_has_no_comment_at_all() -> None:
    body = await _stream(tagged=False)
    assert body == UPSTREAM
    assert VERIFIED_SAVINGS_HEADER.encode() not in body


TRUNCATED = b'event: message_start\ndata: {"type":"message_start","message":{"id":"m1"}}\n\n'


@pytest.mark.asyncio
async def test_failed_stream_closes_without_the_verified_comment() -> None:
    """A stream that never reaches message_stop claims no saving to the client."""
    body = await _stream(tagged=True, payload=TRUNCATED)
    assert body == TRUNCATED
    assert b"x-horizon-verified-savings-usd" not in body
