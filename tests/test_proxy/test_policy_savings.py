"""Flash Observations and the price policies in the account ledger.

These features lower a request's price without removing tokens before the
request is counted, so compression's figure never saw them. The ledger prices
each from the request's own usage, adds it to ``savings_usd`` (what Est.
savings, the Pro fee and the Free cap read) and breaks it out in ``policy_usd``.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi.testclient import TestClient

from horizon.proxy import policy_savings
from horizon.proxy.account_analytics import _account, record_account_outcome
from horizon.proxy.model_modernize import NEW_TOKENIZER_RATIO
from horizon.proxy.outcome import RequestOutcome
from horizon.transforms import flash_observations, flash_openai
from tests.test_keepalive_hosted import _context, _outcome, _Service

OPUS_READ, OPUS_IN, OPUS_OUT = 2e-7, 4e-6, 2e-5  # claude-opus-5-5, from the catalog


def _o(**kw: Any) -> SimpleNamespace:
    base = {
        "provider": "anthropic",
        "model": "claude-opus-5-5",
        "cache_read_tokens": 100_000,
        "cache_write_tokens": 0,
        "cache_write_5m_tokens": 0,
        "cache_write_1h_tokens": 0,
        "uncached_input_tokens": 1_000,
        "cache_inferred": False,
        "provider_input_tokens": 0,
        "optimized_tokens": 101_000,
        "output_tokens": 500,
        "transforms_applied": (),
    }
    base.update(kw)
    return SimpleNamespace(**base)


# -- markers -------------------------------------------------------------------------


def test_flash_markers_sum_and_bad_ones_are_ignored() -> None:
    tags = ["flash:1/3", "flash_saved:1200", "flash_saved:x", "flash_saved:300"]
    assert policy_savings.flash_tokens(tags) == 1500


def test_cleared_tokens_use_the_tokenizer_and_fall_back_low() -> None:
    pairs = [("a" * 8000, "s" * 400)]
    assert policy_savings.cleared_tokens(pairs, lambda s: len(s) // 3) == 2533
    assert policy_savings.cleared_tokens(pairs) == 1900  # 4 characters a token

    def broken(_s: str) -> int:
        raise RuntimeError

    assert policy_savings.cleared_tokens(pairs, broken) == 1900


# -- pricing ---------------------------------------------------------------------------


def test_flash_prices_kept_out_tokens_as_the_cache_reads_they_replace() -> None:
    priced = policy_savings.price(_o(transforms_applied=("flash_saved:10000",)))
    assert priced.usd["flash"] == pytest.approx(10_000 * OPUS_READ)


def test_flash_without_a_cache_breakdown_is_not_credited() -> None:
    outcome = _o(
        cache_read_tokens=0, uncached_input_tokens=0, transforms_applied=("flash_saved:10000",)
    )
    assert policy_savings.price(outcome).usd == {}


def test_fast_mode_dropped_saves_the_premium_on_models_that_bill_it() -> None:
    outcome = _o(transforms_applied=("fast_mode:dropped:headless",))
    standard = 100_000 * OPUS_READ + 1_000 * OPUS_IN + 500 * OPUS_OUT
    assert policy_savings.price(outcome).usd["fast_mode"] == pytest.approx(standard)
    # Sonnet bills no fast premium: nothing was saved by dropping it.
    sonnet = _o(model="claude-sonnet-5-5", transforms_applied=("fast_mode:dropped:headless",))
    assert policy_savings.price(sonnet).usd == {}


def test_flex_is_credited_only_when_the_upstream_served_it() -> None:
    outcome = _o(
        provider="openai",
        model="gpt-5.4",
        cache_read_tokens=10_000,
        uncached_input_tokens=2_000,
        transforms_applied=("service_tier:flex",),
    )
    standard = 10_000 * 2.5e-7 + 2_000 * 2.5e-6 + 500 * 1.5e-5
    flex = 10_000 * 1.3e-7 + 2_000 * 1.25e-6 + 500 * 7.5e-6
    priced = policy_savings.price(outcome)
    assert priced.usd["flex"] == pytest.approx(standard - flex)
    assert priced.cost_delta == pytest.approx(flex - standard)
    assert policy_savings.price(outcome, flex_served=False).usd == {}


def test_modernize_compares_with_the_requested_model_on_its_own_tokenizer() -> None:
    outcome = _o(
        model="claude-haiku-5-5",
        cache_read_tokens=13_000,
        uncached_input_tokens=1_300,
        output_tokens=1_300,
        transforms_applied=("modernize:claude-haiku-4-5>claude-haiku-5-5",),
    )
    served = 13_000 * 1e-8 + 1_300 * 1e-7 + 1_300 * 5e-7
    k = 1 / NEW_TOKENIZER_RATIO
    requested = (13_000 * 1e-7 + 1_300 * 1e-6 + 1_300 * 5e-6) * k
    assert policy_savings.price(outcome).usd["modernize"] == pytest.approx(requested - served)


def test_unpriced_models_are_never_credited_from_a_fallback_rate() -> None:
    outcome = _o(
        model="no-such-model-x",
        transforms_applied=("flash_saved:5000", "fast_mode:dropped:never", "service_tier:flex"),
    )
    assert policy_savings.price(outcome).usd == {}


# -- the ledger row ------------------------------------------------------------------


def _row(outcome, *, flex_served: bool = False) -> dict:
    from horizon.proxy import flex_policy

    service = _Service()
    token = _account.set(_context(service))
    flex_token = flex_policy._flex_added.set(flex_served)
    try:
        asyncio.run(record_account_outcome(outcome))
    finally:
        flex_policy._flex_added.reset(flex_token)
        _account.reset(token)
    (row,) = service.events
    return row


def test_policy_savings_are_inside_savings_usd_and_broken_out() -> None:
    plain = _row(_outcome())
    flashed = _row(_outcome(transforms_applied=("flash:0/2", "flash_saved:20000")))
    assert "policy_usd" not in plain  # ordinary rows keep the shape older APIs accept
    assert flashed["policy_usd"]["flash"] == pytest.approx(20_000 * OPUS_READ)
    assert flashed["savings_usd"] == pytest.approx(
        plain["savings_usd"] + flashed["policy_usd"]["flash"]
    )


def test_a_flex_request_costs_the_flex_price() -> None:
    def outcome():
        return RequestOutcome(
            request_id="r1",
            provider="openai",
            model="gpt-5.4",
            original_tokens=20_000,
            optimized_tokens=20_000,
            output_tokens=400,
            tokens_saved=0,
            attempted_input_tokens=20_000,
            transforms_applied=("service_tier:flex",),
        )

    standard = _row(outcome(), flex_served=False)
    flex = _row(outcome(), flex_served=True)
    assert "policy_usd" not in standard
    assert flex["cost_usd"] == pytest.approx(standard["cost_usd"] - flex["policy_usd"]["flex"])


def test_failed_requests_claim_no_policy_savings() -> None:
    row = _row(_outcome(status_code=500, transforms_applied=("flash_saved:20000",)))
    assert "policy_usd" not in row and row["savings_usd"] == 0


# -- what the transforms report --------------------------------------------------------

LOG = "\n".join(f"tests/test_io.py::test_{i} PASSED" for i in range(800))


def _claude_turns(n: int) -> list[dict]:
    messages: list[dict] = [{"role": "user", "content": "run the tests"}]
    for t in range(n):
        messages += [
            {
                "role": "assistant",
                "content": [{"type": "tool_use", "id": f"c{t}", "name": "Bash", "input": {}}],
            },
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": f"c{t}", "content": LOG + f"#{t}"}
                ],
            },
        ]
    return messages


def test_claude_flash_credits_only_outputs_no_longer_rendered() -> None:
    policy = flash_observations.flash_policy()
    one = flash_observations.apply_flash(_claude_turns(1), horizon=0, policy=policy)
    three = flash_observations.apply_flash(_claude_turns(3), horizon=0, policy=policy)
    assert one.flashed == 1 and one.cleared == []  # shown in full on this request
    assert three.stubbed == 3 and len(three.cleared) == 2
    for text, stub in three.cleared:
        assert text.startswith(LOG) and len(stub) < len(text)


def test_openai_flash_measures_what_would_have_been_forwarded() -> None:
    items = [{"type": "message", "role": "user", "content": "run the tests"}]
    for t in range(2):
        items += [
            {"type": "function_call", "call_id": f"c{t}", "name": "shell", "arguments": "{}"},
            {"type": "function_call_output", "call_id": f"c{t}", "output": LOG + f"#{t}"},
        ]
    # Horizon already compressed the forwarded copy of the first output.
    forwarded = [dict(i) for i in items]
    forwarded[2]["output"] = LOG[:9000]
    policy = flash_observations.flash_policy(flash_openai.DEFAULT_OPENAI_TOOLS)
    result = flash_openai.apply_responses(
        forwarded, horizon=0, policy=policy, originals=flash_openai.responses_originals(items)
    )
    assert result.stubbed == 1
    ((text, stub),) = result.cleared
    assert text == LOG[:9000] and len(stub) < len(text)


# -- through the Claude handler ------------------------------------------------------


def _fake_upstream(proxy) -> None:
    payload = {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": "claude-opus-5-5",
        "content": [{"type": "text", "text": "ok"}],
        "stop_reason": "end_turn",
        "usage": {
            "input_tokens": 40,
            "cache_read_input_tokens": 30_000,
            "cache_creation_input_tokens": 600,
            "output_tokens": 5,
        },
    }
    response = httpx.Response(
        200, json=payload, request=httpx.Request("POST", "http://upstream/v1/messages")
    )
    client = MagicMock()
    client.post = AsyncMock(return_value=response)
    client.request = AsyncMock(return_value=response)
    client.send = AsyncMock(return_value=response)
    client.build_request = MagicMock(
        return_value=httpx.Request("POST", "http://upstream/v1/messages", content=b"{}")
    )
    client.aclose = AsyncMock()
    proxy.http_client = client


def test_claude_flash_reaches_the_ledger_from_the_second_turn(monkeypatch, tmp_path) -> None:
    from tests.test_proxy.test_cost_policies_wiring import MESSAGES, _flash_app

    seen: list[Any] = []

    async def capture(outcome, **_kw):
        seen.append(outcome)

    monkeypatch.setattr("horizon.proxy.account_analytics.record_account_outcome", capture)
    app = _flash_app(monkeypatch, tmp_path, "cache")
    with TestClient(app) as client:
        _fake_upstream(client.app.state.proxy)
        for n in (1, 2, 3):
            body = {"model": "claude-opus-5-5", "max_tokens": 16, "messages": _claude_turns(n)}
            assert client.post(MESSAGES, json=body).status_code == 200
    tokens = [policy_savings.flash_tokens(o.transforms_applied) for o in seen]
    assert tokens[0] == 0 and 0 < tokens[1] < tokens[2]
    priced = policy_savings.price(seen[2]).usd["flash"]
    assert priced == pytest.approx(tokens[2] * OPUS_READ)
    # Compression's own figure is untouched by the flash.
    assert all(o.tokens_saved == 0 for o in seen)


def test_openai_chat_flash_is_not_also_counted_as_compression(monkeypatch, tmp_path) -> None:
    from tests.test_proxy.test_flash_openai_wiring import AUTH, _app, _chat_messages

    seen: list[Any] = []

    async def capture(outcome, **_kw):
        seen.append(outcome)

    monkeypatch.setattr("horizon.proxy.account_analytics.record_account_outcome", capture)
    app = _app(monkeypatch, tmp_path, mode="cache")
    with TestClient(app) as client:
        from tests.test_proxy.test_model_router_wiring import _install_fake_client

        _install_fake_client(client.app.state.proxy)
        # Turn 3 replays turn 2's forwarded prefix, stub included.
        for turns in (1, 2, 3):
            body = {"model": "gpt-6.1-sol", "messages": _chat_messages(turns)}
            assert client.post("/v1/chat/completions", json=body, headers=AUTH).status_code == 200
    outcome = seen[-1]
    flash = policy_savings.flash_tokens(outcome.transforms_applied)
    assert flash > 0
    assert outcome.tokens_saved == max(
        0, outcome.original_tokens - outcome.optimized_tokens - flash
    )
    json.dumps(outcome.transforms_applied)  # plain strings
