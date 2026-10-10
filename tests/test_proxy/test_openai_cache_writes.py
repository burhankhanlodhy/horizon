"""OpenAI cache writes: reported and billed on GPT-5.6+, inferred before it."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi.testclient import TestClient

from horizon.proxy import openai_cache_usage
from horizon.proxy.handlers.openai import _extract_responses_usage
from horizon.proxy.server import ProxyConfig, create_app


def test_reported_writes_come_from_either_details_block() -> None:
    assert (
        openai_cache_usage.reported_writes({"input_tokens_details": {"cache_write_tokens": 900}})
        == 900
    )
    assert (
        openai_cache_usage.reported_writes({"prompt_tokens_details": {"cache_write_tokens": 0}})
        == 0
    )
    assert (
        openai_cache_usage.reported_writes({"prompt_tokens_details": {"cached_tokens": 5}}) is None
    )
    assert openai_cache_usage.reported_writes(None) is None


def test_split_keeps_reported_writes_out_of_the_uncached_tokens() -> None:
    assert openai_cache_usage.split(10_000, 6_000, 3_000) == (3_000, 1_000, False)
    assert openai_cache_usage.split(10_000, 6_000, None) == (4_000, 4_000, True)


def test_websocket_usage_reads_reported_writes() -> None:
    event = {
        "type": "response.completed",
        "response": {
            "usage": {
                "input_tokens": 10_000,
                "output_tokens": 50,
                "input_tokens_details": {"cached_tokens": 6_000, "cache_write_tokens": 3_000},
            }
        },
    }
    assert _extract_responses_usage(event) == (10_000, 50, 6_000, 3_000, 1_000)


def _app(monkeypatch, tmp_path):
    monkeypatch.setenv("HORIZON_WORKSPACE_DIR", str(tmp_path))
    return create_app(
        ProxyConfig(
            optimize=False,
            cache_enabled=False,
            rate_limit_enabled=False,
            cost_tracking_enabled=False,
            ccr_inject_tool=False,
            ccr_handle_responses=False,
            ccr_context_tracking=False,
        )
    )


def _client(proxy, payload: dict) -> None:
    def respond(*_a, **_kw):
        return httpx.Response(
            200, json=payload, request=httpx.Request("POST", "https://api.openai.com/v1/x")
        )

    http = MagicMock()
    http.post = AsyncMock(side_effect=respond)
    http.request = AsyncMock(side_effect=respond)
    http.send = AsyncMock(side_effect=respond)
    http.aclose = AsyncMock()
    proxy.http_client = http


@pytest.mark.parametrize("reported", [True, False])
def test_chat_completions_book_reported_writes(monkeypatch, tmp_path, reported) -> None:
    seen: list[Any] = []

    async def capture(outcome, **_kw):
        seen.append(outcome)

    monkeypatch.setattr("horizon.proxy.account_analytics.record_account_outcome", capture)
    details: dict[str, int] = {"cached_tokens": 6_000}
    if reported:
        details["cache_write_tokens"] = 3_000
    payload = {
        "id": "c",
        "object": "chat.completion",
        "model": "gpt-6.1-sol",
        "choices": [
            {"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}
        ],
        "usage": {
            "prompt_tokens": 10_000,
            "completion_tokens": 5,
            "total_tokens": 10_005,
            "prompt_tokens_details": details,
        },
    }
    with TestClient(_app(monkeypatch, tmp_path)) as client:
        _client(client.app.state.proxy, payload)
        body = {"model": "gpt-6.1-sol", "messages": [{"role": "user", "content": "hi"}]}
        response = client.post(
            "/v1/chat/completions", json=body, headers={"authorization": "Bearer sk-t"}
        )
        assert response.status_code == 200
    (outcome,) = seen
    if reported:
        assert (outcome.cache_write_tokens, outcome.uncached_input_tokens) == (3_000, 1_000)
        assert outcome.cache_inferred is False
    else:
        assert (outcome.cache_write_tokens, outcome.uncached_input_tokens) == (4_000, 4_000)
        assert outcome.cache_inferred is True


def test_responses_book_reported_writes(monkeypatch, tmp_path) -> None:
    seen: list[Any] = []

    async def capture(outcome, **_kw):
        seen.append(outcome)

    monkeypatch.setattr("horizon.proxy.account_analytics.record_account_outcome", capture)
    payload = {
        "id": "resp_1",
        "object": "response",
        "model": "gpt-6.1-sol",
        "status": "completed",
        "output": [
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "ok"}],
            }
        ],
        "usage": {
            "input_tokens": 10_000,
            "output_tokens": 5,
            "total_tokens": 10_005,
            "input_tokens_details": {"cached_tokens": 6_000, "cache_write_tokens": 3_000},
        },
    }
    with TestClient(_app(monkeypatch, tmp_path)) as client:
        _client(client.app.state.proxy, payload)
        body = {"model": "gpt-6.1-sol", "input": "hi"}
        response = client.post("/v1/responses", json=body, headers={"authorization": "Bearer sk-t"})
        assert response.status_code == 200
    outcome = seen[-1]
    assert (outcome.cache_write_tokens, outcome.uncached_input_tokens) == (3_000, 1_000)
    assert outcome.cache_inferred is False


def test_reported_writes_are_billed_at_the_write_rate() -> None:
    """The ledger prices a real GPT-5.6+ write at 1.25x input, not as plain input."""
    from horizon.proxy.savings_tracker import _estimate_input_cost_usd

    cost = _estimate_input_cost_usd(
        "gpt-6.1-sol",
        10_000,
        cache_read_tokens=6_000,
        cache_write_tokens=3_000,
        uncached_input_tokens=1_000,
    )
    assert cost == pytest.approx(6_000 * 1e-7 + 3_000 * 2.5e-6 + 1_000 * 2e-6)
