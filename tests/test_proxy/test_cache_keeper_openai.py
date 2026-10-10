"""Cache keep-alive for OpenAI GPT-5.6+ (30-minute cache, billed writes).

Behaviour measured on gpt-6.1-sol, 2026-10-10: an exact copy of the request
with ``prompt_cache_options.prewarm`` bills only reads; Chat Completions
rejects ``prewarm`` but a 16-token copy still reads the whole prefix; an
unstored ``previous_response_id`` chain cannot be warmed over HTTP.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from horizon.proxy.cache_keeper import (
    OPENAI_CHAT,
    OPENAI_RESPONSES,
    TTL_30M,
    CacheKeeper,
    openai_prewarm_body,
    openai_ttl,
)

URL = "https://api.openai.com/v1/responses"
HEADERS = {"authorization": "Bearer sk-secret", "content-length": "99"}
TOOLS = [{"type": "function", "name": "shell", "parameters": {"type": "object"}}]


def _responses(**extra: Any) -> dict[str, Any]:
    return {
        "model": "gpt-6.1-sol",
        "store": False,
        "stream": True,
        "reasoning": {"effort": "medium"},
        "tools": TOOLS,
        "input": [{"role": "user", "content": "fix the bug"}],
        **extra,
    }


def _chat(**extra: Any) -> dict[str, Any]:
    return {
        "model": "gpt-6.1-sol",
        "stream": True,
        "stream_options": {"include_usage": True},
        "max_tokens": 8000,
        "reasoning_effort": "medium",
        "tools": TOOLS,
        "messages": [{"role": "user", "content": "fix the bug"}],
        **extra,
    }


class _Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


class _Upstream:
    """Answers with an OpenAI usage block."""

    def __init__(self, read: int = 200_000, write: int = 0, uncached: int = 12, output: int = 0):
        self.sent: list[dict[str, Any]] = []
        self.read, self.write, self.uncached, self.output = read, write, uncached, output

    async def __call__(self, url: str, headers: dict[str, str], body: dict[str, Any]):
        self.sent.append(body)
        return 200, {
            "input_tokens": self.read + self.write + self.uncached,
            "input_tokens_details": {"cached_tokens": self.read, "cache_write_tokens": self.write},
            "output_tokens": self.output,
        }


class _Billing:
    def __init__(self) -> None:
        self.rows: list[tuple[Any, dict[str, Any]]] = []

    async def __call__(self, owner: Any, record: dict[str, Any]) -> None:
        self.rows.append((owner, record))


def _seed(keeper: CacheKeeper, body: dict[str, Any], flavor: str, clock: _Clock, **kw: Any) -> None:
    keeper.record_request("r1", url=URL, headers=HEADERS, body=body, flavor=flavor, **kw)
    keeper.record_usage("r1", model="gpt-6.1-sol", cache_read=0, cache_write=200_000, uncached=12)


def test_gpt_5_6_and_later_keep_30_minutes_earlier_models_none() -> None:
    assert openai_ttl("gpt-6.1-sol") == TTL_30M
    assert openai_ttl("gpt-5.6-terra") == TTL_30M
    assert openai_ttl("openai/gpt-6") == TTL_30M
    assert openai_ttl("gpt-5.5") is None
    assert openai_ttl("gpt-4.1") is None
    assert openai_ttl("claude-opus-5-5") is None


def test_responses_ping_is_the_exact_request_marked_prewarm() -> None:
    body = _responses(prompt_cache_options={"ttl": "30m"})
    warm = openai_prewarm_body(body, OPENAI_RESPONSES)
    assert warm["prompt_cache_options"] == {"ttl": "30m", "prewarm": True}
    assert warm["stream"] is False
    # Everything the cache is keyed by stays as it was.
    for key in ("model", "reasoning", "tools", "input", "store"):
        assert warm[key] == body[key]
    assert body["stream"] is True  # the stored request is not touched


def test_chat_ping_is_the_exact_request_with_a_small_output_limit() -> None:
    warm = openai_prewarm_body(_chat(), OPENAI_CHAT)
    assert warm["max_completion_tokens"] == 16 and "max_tokens" not in warm
    assert warm["stream"] is False and "stream_options" not in warm
    assert warm["messages"] == _chat()["messages"] and warm["reasoning_effort"] == "medium"
    assert "prompt_cache_options" not in warm  # Chat rejects prewarm


def test_unstored_chains_are_not_kept_warm() -> None:
    body = _responses(previous_response_id="resp_1", store=False)
    assert openai_prewarm_body(body, OPENAI_RESPONSES) is None
    stored = _responses(previous_response_id="resp_1", store=True)
    assert openai_prewarm_body(stored, OPENAI_RESPONSES) is not None


@pytest.mark.parametrize("flavor,body", [(OPENAI_RESPONSES, _responses()), (OPENAI_CHAT, _chat())])
def test_an_idle_gpt_session_is_pinged_before_30_minutes(flavor, body) -> None:
    clock, upstream = _Clock(), _Upstream()
    keeper = CacheKeeper(upstream, clock=clock)
    _seed(keeper, body, flavor, clock)
    clock.t += TTL_30M - 200
    assert asyncio.run(keeper.tick()) == 0  # not yet
    clock.t += 100  # 2 minutes before expiry
    assert asyncio.run(keeper.tick()) == 1
    assert len(upstream.sent) == 1


def test_earlier_models_and_requests_without_tools_are_not_kept() -> None:
    clock, upstream = _Clock(), _Upstream()
    keeper = CacheKeeper(upstream, clock=clock)
    keeper.record_request(
        "a", url=URL, headers=HEADERS, body=_responses(model="gpt-5.4"), flavor=OPENAI_RESPONSES
    )
    keeper.record_usage("a", model="gpt-5.4", cache_read=0, cache_write=0, uncached=200_000)
    keeper.record_request(
        "b", url=URL, headers=HEADERS, body=_responses(tools=[]), flavor=OPENAI_RESPONSES
    )
    keeper.record_usage("b", model="gpt-6.1-sol", cache_read=0, cache_write=200_000, uncached=0)
    clock.t += TTL_30M - 60
    assert asyncio.run(keeper.tick()) == 0


def test_a_ping_is_billed_with_its_reads_and_output_and_tail_writes_are_not_a_miss() -> None:
    clock, billing = _Clock(), _Billing()
    upstream = _Upstream(read=200_000, write=6, uncached=3, output=4)
    keeper = CacheKeeper(upstream, clock=clock, reporter=billing)
    owner = SimpleNamespace(user_id="u1", compression_allowed=True)
    _seed(keeper, _chat(), OPENAI_CHAT, clock, liveness_id="live-1", owner=owner)
    clock.t += TTL_30M - 100
    asyncio.run(keeper.tick())
    ((who, record),) = billing.rows
    assert (
        who is owner and record["provider"] == "openai" and record["event"] == "cache_keeper_ping"
    )
    # gpt-6.1-sol: reads 1e-7, writes 2.5e-6, input 2e-6, output 1e-5 per token.
    assert record["cost_usd"] == pytest.approx(
        200_000 * 1e-7 + 6 * 2.5e-6 + 3 * 2e-6 + 4 * 1e-5, rel=1e-3
    )
    # It kept going: a second ping lands before the next 30-minute expiry.
    clock.t += TTL_30M - 100
    assert asyncio.run(keeper.tick()) == 1


def test_a_ping_that_rewrites_the_context_stops_the_session() -> None:
    clock = _Clock()
    upstream = _Upstream(read=0, write=200_000)
    keeper = CacheKeeper(upstream, clock=clock)
    _seed(keeper, _responses(), OPENAI_RESPONSES, clock)
    clock.t += TTL_30M - 100
    asyncio.run(keeper.tick())
    clock.t += TTL_30M - 100
    assert asyncio.run(keeper.tick()) == 0


def test_a_resume_after_more_than_30_minutes_credits_the_avoided_rewrite() -> None:
    clock = _Clock()
    keeper = CacheKeeper(_Upstream(), clock=clock)
    _seed(keeper, _responses(), OPENAI_RESPONSES, clock)
    for _ in range(2):
        clock.t += TTL_30M - 100
        asyncio.run(keeper.tick())
    keeper.record_request(
        "r2", url=URL, headers=HEADERS, body=_responses(), flavor=OPENAI_RESPONSES
    )
    event = keeper.record_usage(
        "r2", model="gpt-6.1-sol", cache_read=200_000, cache_write=50, uncached=10
    )
    assert event is not None and event["avoided_usd"] > 0 and event["pings"] == 2
