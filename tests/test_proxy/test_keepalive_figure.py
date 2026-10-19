"""The verified figure with its keep-alive settled in, against what the ledger books.

For a forwarder request, the figure the client is shown is its own saving, plus
the keep-alive credit booked on it, minus the ping rows booked for the gap
before it. These tests tie that figure to the account ledger rows themselves.
"""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import uuid4

import pytest

from horizon.proxy.account_analytics import (
    AccountContext,
    _account,
    record_account_outcome,
    record_keepalive_ping,
)
from horizon.proxy.cache_keeper import CacheKeeper
from horizon.proxy.outcome import RequestOutcome, keepalive_adjusted_saving

URL = "https://api.anthropic.com/v1/messages?beta=true"
MODEL = "claude-opus-5-5"


class _Service:
    enabled = True

    def __init__(self) -> None:
        self.runtime_id = str(uuid4())
        self.events: list[dict[str, Any]] = []

    def _enqueue(self, event: dict[str, Any]) -> None:
        self.events.append(event)


def _body() -> dict[str, Any]:
    marker = {"type": "ephemeral", "ttl": "1h"}
    return {
        "model": MODEL,
        "max_tokens": 32000,
        "tools": [{"name": "Read", "input_schema": {"type": "object"}, "cache_control": marker}],
        "messages": [{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
    }


def _outcome(request_id: str, **kw: Any) -> RequestOutcome:
    return RequestOutcome(
        request_id=request_id,
        provider="anthropic",
        model=MODEL,
        original_tokens=400_000,
        optimized_tokens=400_000,
        output_tokens=50,
        tokens_saved=0,
        attempted_input_tokens=400_000,
        cache_read_tokens=392_000,
        cache_write_tokens=8_000,
        status_code=200,
        **kw,
    )


class _Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def _warm_upstream():
    async def send(url, headers, body):
        return 200, {"cache_read_input_tokens": 400_000, "cache_creation_input_tokens": 0}

    return send


def test_the_figure_is_the_saving_plus_the_booked_credit_minus_the_booked_pings() -> None:
    """Emitted == ledger: the resumed row's credit and the gap's ping rows, as booked."""
    service = _Service()
    context = AccountContext(str(uuid4()), str(uuid4()), service)

    async def reporter(owner, record):
        await record_keepalive_ping(owner, record)

    clock = _Clock()
    keeper = CacheKeeper(_warm_upstream(), clock=clock, reporter=reporter)
    headers = {"x-api-key": "cs_live_x", "anthropic-version": "2023-06-01"}

    keeper.record_request(
        "r1", url=URL, headers=headers, body=_body(), liveness_id="L", owner=context
    )
    keeper.record_usage("r1", model=MODEL, cache_read=8_000, cache_write=392_000, uncached=10)
    for at in (4300, 7600):  # two pings in the gap
        clock.t = at
        asyncio.run(keeper.tick())
    clock.t = 8000  # the user returns; the kept cache is read
    keeper.record_request(
        "r2", url=URL, headers=headers, body=_body(), liveness_id="L", owner=context
    )
    event = keeper.record_usage(
        "r2", model=MODEL, cache_read=400_000, cache_write=3_000, uncached=10
    )
    settled = keeper.last_settlement
    assert event is not None and settled.avoided_usd > 0 and settled.ping_usd > 0

    compression_usd = 0.37  # the request's own cache-aware saving (the ledger's compression)
    emitted = keepalive_adjusted_saving(
        compression_usd, keepalive_usd=settled.avoided_usd, ping_usd=settled.ping_usd
    )

    token = _account.set(context)
    try:
        asyncio.run(
            record_account_outcome(_outcome("r2"), keepalive_usd=settled.avoided_usd, saved=0)
        )
    finally:
        _account.reset(token)

    resumed = [e for e in service.events if e["request_id"] == "r2"]
    pings = [e for e in service.events if e.get("kind") == "keepalive"]
    assert len(resumed) == 1 and len(pings) == 2
    booked_credit = resumed[0].get("keepalive_usd", 0.0)
    booked_pings = sum(e["savings_usd"] for e in pings)  # ping rows are negative
    assert booked_pings == pytest.approx(-settled.ping_usd, abs=1e-6)
    assert booked_credit == pytest.approx(settled.avoided_usd)
    # The figure: this request's own saving, its booked credit, and the gap's booked pings.
    assert emitted == pytest.approx(compression_usd + booked_credit + booked_pings, abs=1e-6)


def test_a_gap_whose_pings_outweigh_the_saving_is_a_cost_and_is_never_clamped_past_it() -> None:
    assert keepalive_adjusted_saving(0.0, keepalive_usd=0.0, ping_usd=0.08) == pytest.approx(-0.08)
    # A request's own saving is never negative, so the cost is the floor.
    assert keepalive_adjusted_saving(-0.5, keepalive_usd=0.0, ping_usd=0.08) == pytest.approx(-0.08)
    assert keepalive_adjusted_saving(0.1, keepalive_usd=0.0, ping_usd=0.08) == pytest.approx(0.02)


def test_a_credit_without_pings_or_a_saving_without_either_is_unchanged() -> None:
    assert keepalive_adjusted_saving(0.25, keepalive_usd=0.0, ping_usd=0.0) == pytest.approx(0.25)
    assert keepalive_adjusted_saving(0.0, keepalive_usd=1.5, ping_usd=0.0) == pytest.approx(1.5)
    assert keepalive_adjusted_saving(0.0, keepalive_usd=0.0, ping_usd=0.0) == 0.0
