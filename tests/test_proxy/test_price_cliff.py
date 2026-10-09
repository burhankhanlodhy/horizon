"""Price-cliff guard: tighter compression near a whole-request price tier."""

from __future__ import annotations

import pytest

pytest.importorskip("litellm")

from horizon.proxy import price_cliff  # noqa: E402

KW = {"target_ratio": None, "max_items_after_crush": 15, "min_tokens_to_compress": 25}


@pytest.fixture(autouse=True)
def _enabled(monkeypatch):
    monkeypatch.setenv("HORIZON_PRICE_CLIFF_GUARD", "1")
    monkeypatch.delenv("HORIZON_PRICE_CLIFF_MARGIN", raising=False)
    monkeypatch.delenv("HORIZON_PRICE_CLIFF_TARGET_RATIO", raising=False)


def test_off_by_default(monkeypatch) -> None:
    monkeypatch.delenv("HORIZON_PRICE_CLIFF_GUARD", raising=False)
    assert price_cliff.guard("claude-haiku-5-5", 97_000, KW) is None


@pytest.mark.parametrize("tokens", [92_000, 99_000, 104_000, 110_000])
def test_haiku_5_5_engages_inside_the_band(tokens) -> None:
    decision = price_cliff.guard("claude-haiku-5-5", tokens, KW)
    assert decision is not None
    assert decision.threshold == 100_000
    assert decision.kwargs["target_ratio"] == pytest.approx(0.30)
    assert decision.kwargs["max_items_after_crush"] == 8
    assert decision.kwargs["min_tokens_to_compress"] == 10


@pytest.mark.parametrize("tokens", [50_000, 89_000, 111_000, 400_000])
def test_haiku_5_5_ignores_requests_outside_the_band(tokens) -> None:
    assert price_cliff.guard("claude-haiku-5-5", tokens, KW) is None


def test_flat_rated_models_never_engage() -> None:
    assert price_cliff.guard("claude-opus-5-5", 200_000, KW) is None
    assert price_cliff.guard("claude-sonnet-5-5", 272_000, KW) is None


def test_gpt_band_sits_at_272k() -> None:
    assert price_cliff.guard("gpt-6.1-sol", 200_000, KW) is None
    assert price_cliff.guard("gpt-6.1-sol", 265_000, KW).threshold == 272_000


def test_a_stricter_existing_ratio_is_kept() -> None:
    decision = price_cliff.guard("claude-haiku-5-5", 99_000, {**KW, "target_ratio": 0.10})
    assert decision.kwargs["target_ratio"] == pytest.approx(0.10)


def test_margin_and_ratio_are_configurable(monkeypatch) -> None:
    monkeypatch.setenv("HORIZON_PRICE_CLIFF_MARGIN", "0.2")
    monkeypatch.setenv("HORIZON_PRICE_CLIFF_TARGET_RATIO", "0.5")
    decision = price_cliff.guard("claude-haiku-5-5", 85_000, KW)
    assert decision is not None and decision.kwargs["target_ratio"] == pytest.approx(0.5)


def test_original_kwargs_are_not_mutated() -> None:
    kwargs = dict(KW)
    price_cliff.guard("claude-haiku-5-5", 99_000, kwargs)
    assert kwargs == KW


@pytest.mark.parametrize(
    ("projected", "after", "expected"),
    [
        (98_000, 90_000, "kept_below"),
        (98_000, 101_000, "crossed"),
        (104_000, 95_000, "avoided"),
        (104_000, 103_000, "already_over"),
    ],
)
def test_outcome_labels(projected, after, expected) -> None:
    decision = price_cliff.guard("claude-haiku-5-5", projected, KW)
    assert price_cliff.outcome(decision, after - 5_000, 5_000) == expected


def test_handler_runs_compression_with_the_tightened_knobs(monkeypatch) -> None:
    """End to end: a Haiku 5.5 request near 100k reaches the pipeline with guard kwargs."""
    from fastapi.testclient import TestClient

    from horizon.proxy.server import ProxyConfig, create_app
    from horizon.transforms.base import TransformResult
    from tests.test_proxy.test_model_router_wiring import _install_fake_client

    # Token counts differ by tokenizer availability; a wide band keeps the
    # request inside it whichever counter this environment uses.
    monkeypatch.setenv("HORIZON_PRICE_CLIFF_MARGIN", "0.5")
    seen: list[dict] = []

    def fake_apply(messages, **kwargs):
        seen.append(kwargs)
        return TransformResult(
            messages=messages, tokens_before=0, tokens_after=0, transforms_applied=[]
        )

    config = ProxyConfig(
        optimize=True,
        cache_enabled=False,
        rate_limit_enabled=False,
        cost_tracking_enabled=False,
        ccr_inject_tool=False,
        ccr_handle_responses=False,
        ccr_context_tracking=False,
        mode="token",
    )
    app = create_app(config)
    big = "log line with some detail\n" * 13_100  # ~97k tokens by the proxy's count
    body = {
        "model": "claude-haiku-5-5",
        "max_tokens": 16,
        "messages": [
            {"role": "user", "content": "check the logs"},
            {
                "role": "assistant",
                "content": [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {}}],
            },
            {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "t1", "content": big}],
            },
        ],
    }
    with TestClient(app) as client:
        proxy = client.app.state.proxy
        _install_fake_client(proxy)
        monkeypatch.setattr(proxy.anthropic_pipeline, "apply", fake_apply)
        assert client.post("/v1/messages", json=body).status_code == 200
    assert seen, "compression pipeline was not called"
    assert seen[0]["max_items_after_crush"] == 8
    assert seen[0]["target_ratio"] == pytest.approx(0.30)


# ── OpenAI-format paths (Chat Completions, Responses) ──────────────────


def test_gemini_3_1_pro_band_sits_at_200k_under_its_gateway_name() -> None:
    """Gateways send gemini-3.1-pro; the catalog prices it as -preview, tier 200k."""
    assert price_cliff.guard("gemini-3.1-pro", 195_000, KW) is not None
    assert price_cliff.guard("gemini-3.1-pro", 150_000, KW) is None


def _openai_app(monkeypatch, tmp_path):
    from horizon.proxy.server import ProxyConfig, create_app

    monkeypatch.setenv("HORIZON_WORKSPACE_DIR", str(tmp_path))
    # A small tier keeps the request small: the guard reads it per model.
    monkeypatch.setattr(
        "horizon.pricing.counterfactual.long_context_threshold",
        lambda model: 10_000 if model == "gpt-5.4" else None,
    )
    monkeypatch.setenv("HORIZON_PRICE_CLIFF_MARGIN", "0.5")
    config = ProxyConfig(
        optimize=True,
        cache_enabled=False,
        rate_limit_enabled=False,
        cost_tracking_enabled=False,
        ccr_inject_tool=False,
        ccr_handle_responses=False,
        ccr_context_tracking=False,
        mode="token",
    )
    return create_app(config)


def _capture_outcomes(monkeypatch) -> list:
    seen: list = []

    async def capture(outcome, **_kw):
        seen.append(outcome)

    monkeypatch.setattr("horizon.proxy.account_analytics.record_account_outcome", capture)
    return seen


BIG = "log line with some detail\n" * 1_400  # ~10k tokens by the proxy's count


def test_chat_completions_run_compression_with_the_tightened_knobs(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    from horizon.transforms.base import TransformResult
    from tests.test_proxy.test_model_router_wiring import _install_fake_client

    outcomes = _capture_outcomes(monkeypatch)
    seen: list[dict] = []

    def fake_apply(messages, **kwargs):
        seen.append(kwargs)
        return TransformResult(
            messages=messages, tokens_before=0, tokens_after=0, transforms_applied=[]
        )

    app = _openai_app(monkeypatch, tmp_path)
    body = {
        "model": "gpt-5.4",
        "messages": [
            {"role": "user", "content": "check the logs"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {"id": "c1", "type": "function", "function": {"name": "sh", "arguments": "{}"}}
                ],
            },
            {"role": "tool", "tool_call_id": "c1", "content": BIG},
        ],
    }
    with TestClient(app) as client:
        proxy = client.app.state.proxy
        _install_fake_client(proxy)
        monkeypatch.setattr(proxy.openai_pipeline, "apply", fake_apply)
        response = client.post(
            "/v1/chat/completions", json=body, headers={"authorization": "Bearer sk-test"}
        )
        assert response.status_code == 200
    assert seen and seen[0]["max_items_after_crush"] == 8
    assert seen[0]["target_ratio"] == pytest.approx(0.30)
    assert any(t.startswith("price_cliff:10k:") for t in outcomes[-1].transforms_applied)


def test_responses_compress_tool_outputs_at_the_guard_ratio(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    from tests.test_proxy.test_model_router_wiring import _install_fake_client

    outcomes = _capture_outcomes(monkeypatch)
    app = _openai_app(monkeypatch, tmp_path)
    body = {
        "model": "gpt-5.4",
        "input": [
            {"type": "message", "role": "user", "content": "check the logs"},
            {"type": "function_call", "call_id": "c1", "name": "sh", "arguments": "{}"},
            {"type": "function_call_output", "call_id": "c1", "output": BIG},
        ],
    }
    with TestClient(app) as client:
        proxy = client.app.state.proxy
        _install_fake_client(proxy)
        ratios: list = []
        original = proxy._compress_openai_responses_live_text_units_with_router

        def spy(payload, **kwargs):
            ratios.append(kwargs.get("cliff_target_ratio"))
            return original(payload, **kwargs)

        monkeypatch.setattr(proxy, "_compress_openai_responses_live_text_units_with_router", spy)
        response = client.post(
            "/v1/responses", json=body, headers={"authorization": "Bearer sk-test"}
        )
        assert response.status_code == 200
    assert ratios and ratios[0] == pytest.approx(0.30)
    assert any(t.startswith("price_cliff:10k:") for t in outcomes[-1].transforms_applied)


def test_requests_far_from_the_tier_are_untouched(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    from tests.test_proxy.test_model_router_wiring import _install_fake_client

    outcomes = _capture_outcomes(monkeypatch)
    app = _openai_app(monkeypatch, tmp_path)
    body = {"model": "gpt-5.4", "input": [{"type": "message", "role": "user", "content": "hi"}]}
    with TestClient(app) as client:
        _install_fake_client(client.app.state.proxy)
        client.post("/v1/responses", json=body, headers={"authorization": "Bearer sk-test"})
    assert not any("price_cliff" in t for t in outcomes[-1].transforms_applied)
