"""Codex removals keep saving on every later turn, and that saving is priced.

A token removed from a Codex request stays out of every later request in the
conversation: over WebSocket the provider rebuilds earlier context from what
Horizon forwarded, and a keyed full-transcript request re-sends the compressed
transcript. Those repeats were never counted. They are priced like the cached
prefix they sit in -- a cache read when the request had one.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from horizon.proxy import account_analytics
from horizon.proxy.account_analytics import AccountContext, _account, record_account_outcome
from horizon.proxy.conversation_savings import (
    ConversationSavings,
    ResponseChainSavings,
    reset_conversation_savings,
)
from horizon.proxy.outcome import RequestOutcome, emit_request_outcome
from horizon.proxy.savings_tracker import estimate_request_savings_usd


@pytest.fixture(autouse=True)
def _clean_ledgers() -> Any:
    reset_conversation_savings()
    yield
    reset_conversation_savings()


# ── The ledgers ────────────────────────────────────────────────────────


def test_a_running_total_splits_into_new_and_retained() -> None:
    ledger = ConversationSavings()
    assert ledger.split("conv-a", 62_806) == (62_806, 0)
    assert ledger.split("conv-a", 76_020) == (13_214, 62_806)


def test_a_compaction_retains_only_what_is_still_removed() -> None:
    ledger = ConversationSavings()
    ledger.split("conv-a", 50_000)
    assert ledger.split("conv-a", 8_000) == (0, 8_000)


def test_novel_still_reports_only_the_new_part() -> None:
    ledger = ConversationSavings()
    ledger.novel("conv-a", 62_806)
    assert ledger.novel("conv-a", 76_020) == 13_214


def test_a_chain_carries_what_its_responses_kept_out() -> None:
    chain = ResponseChainSavings()
    chain.record("resp-1", 464)
    assert chain.carried("resp-1") == 464
    chain.record("resp-2", chain.carried("resp-1") + 2_225)
    assert chain.carried("resp-2") == 2_689


def test_an_unknown_or_missing_response_carries_nothing() -> None:
    chain = ResponseChainSavings()
    assert chain.carried("never-seen") == 0
    assert chain.carried(None) == 0
    chain.record(None, 500)
    assert chain.carried(None) == 0


def test_the_oldest_response_is_forgotten_first() -> None:
    chain = ResponseChainSavings(max_responses=2)
    chain.record("resp-1", 1)
    chain.record("resp-2", 2)
    chain.record("resp-3", 3)
    assert (chain.carried("resp-1"), chain.carried("resp-3")) == (0, 3)


# ── Pricing ────────────────────────────────────────────────────────────


def test_retained_tokens_price_as_cache_reads_on_a_warm_turn() -> None:
    prices = estimate_request_savings_usd(
        "gpt-6.1-sol",
        retained_tokens_saved=10_000,
        cache_read_tokens=80_000,
        uncached_input_tokens=2_000,
        provider="openai",
    )
    cached_only = estimate_request_savings_usd(
        "gpt-6.1-sol",
        tool_schema_tokens_saved=10_000,
        cache_read_tokens=80_000,
        uncached_input_tokens=2_000,
        provider="openai",
    )
    assert prices["retained"] > 0
    assert prices["retained"] == pytest.approx(cached_only["tool_schema"])
    assert prices["retained"] < prices["retained_list"]
    assert prices["compression"] == 0


def test_retained_tokens_past_the_cache_reads_price_as_fresh_input() -> None:
    cold = estimate_request_savings_usd(
        "gpt-6.1-sol", retained_tokens_saved=10_000, uncached_input_tokens=30_000, provider="openai"
    )
    assert cold["retained"] == pytest.approx(cold["retained_list"])


@pytest.mark.asyncio
async def test_the_account_event_adds_retained_dollars_to_savings() -> None:
    events: list[dict] = []
    service = SimpleNamespace(runtime_id=str(uuid4()), _enqueue=events.append)
    token = _account.set(AccountContext(str(uuid4()), str(uuid4()), service))
    try:
        outcome = RequestOutcome(
            request_id="req-1",
            provider="openai",
            model="gpt-6.1-sol",
            original_tokens=31_000,
            optimized_tokens=30_000,
            output_tokens=100,
            tokens_saved=1_000,
            attempted_input_tokens=31_000,
            cache_read_tokens=28_000,
            uncached_input_tokens=2_000,
            client="codex",
        )
        await record_account_outcome(outcome, saved=1_000, retained=20_000)
    finally:
        _account.reset(token)
    prices = estimate_request_savings_usd(
        "gpt-6.1-sol",
        compression_tokens_saved=1_000,
        retained_tokens_saved=20_000,
        cache_read_tokens=28_000,
        uncached_input_tokens=2_000,
        provider="openai",
    )
    assert events[0]["savings_usd"] == pytest.approx(prices["compression"] + prices["retained"])
    assert events[0]["tokens_saved"] == 1_000


# ── The funnel ─────────────────────────────────────────────────────────


class _Handler:
    def __init__(self) -> None:
        self.metrics = MagicMock()
        self.metrics.record_request = AsyncMock()
        self.cost_tracker = MagicMock()
        self.logger = None


def _outcome(**overrides: Any) -> RequestOutcome:
    defaults: dict[str, Any] = {
        "request_id": "req-1",
        "provider": "openai",
        "model": "gpt-6.1-sol",
        "original_tokens": 156_260,
        "optimized_tokens": 80_240,
        "output_tokens": 177,
        "tokens_saved": 76_020,
        "attempted_input_tokens": 90_000,
        "cache_read_tokens": 66_304,
        "conversation_key": "conv-a",
        "conversation_tokens_saved": 76_020,
        "client": "codex",
    }
    defaults.update(overrides)
    return RequestOutcome(**defaults)


@pytest.fixture
def booked(monkeypatch) -> list[dict]:
    calls: list[dict] = []

    async def _record(outcome, **kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(account_analytics, "record_account_outcome", _record)
    return calls


@pytest.mark.asyncio
async def test_a_keyed_codex_turn_books_what_earlier_turns_removed(booked) -> None:
    handler = _Handler()
    await emit_request_outcome(
        handler, _outcome(tokens_saved=62_806, conversation_tokens_saved=62_806)
    )
    await emit_request_outcome(handler, _outcome())
    assert [(c["saved"], c["retained"]) for c in booked] == [(62_806, 0), (13_214, 62_806)]


@pytest.mark.asyncio
async def test_the_websocket_count_wins_over_a_stale_key(booked) -> None:
    """The WS handler tracks the chain itself; its session-end residual repeats
    the last frame's key and total, which must not read as retained."""
    handler = _Handler()
    await emit_request_outcome(handler, _outcome(retained_tokens_saved=5_000))
    await emit_request_outcome(handler, _outcome(tokens_saved=0, retained_tokens_saved=0))
    assert [c["retained"] for c in booked] == [5_000, 0]


@pytest.mark.asyncio
async def test_incremental_codex_turns_use_the_handler_count(booked) -> None:
    handler = _Handler()
    await emit_request_outcome(
        handler,
        _outcome(
            tokens_saved=4_611,
            conversation_key=None,
            conversation_tokens_saved=None,
            retained_tokens_saved=2_808,
        ),
    )
    assert (booked[0]["saved"], booked[0]["retained"]) == (4_611, 2_808)


@pytest.mark.asyncio
@pytest.mark.parametrize("client", ["opencode", "claude-code", None])
async def test_other_clients_book_no_retained_savings(booked, client) -> None:
    handler = _Handler()
    await emit_request_outcome(
        handler, _outcome(tokens_saved=62_806, conversation_tokens_saved=62_806, client=client)
    )
    await emit_request_outcome(handler, _outcome(client=client, retained_tokens_saved=9_999))
    assert [(c["saved"], c["retained"]) for c in booked] == [(62_806, 0), (13_214, 0)]
