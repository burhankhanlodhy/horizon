"""Model modernization: superseded ids served on cheaper successors, never mid-session."""

from __future__ import annotations

import json

import pytest

from horizon.proxy.model_modernize import (
    DEFAULT_MAP,
    ModelModernizer,
    ModernizeConfig,
    _matches,
    supports_fast_mode,
)


def _modernizer(monkeypatch, **env: str) -> ModelModernizer:
    monkeypatch.setenv("HORIZON_MODEL_MODERNIZE", "1")
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return ModelModernizer(ModernizeConfig.from_env())


def test_disabled_by_default(monkeypatch) -> None:
    monkeypatch.delenv("HORIZON_MODEL_MODERNIZE", raising=False)
    modernizer = ModelModernizer(ModernizeConfig.from_env())
    body = {"model": "claude-haiku-4-5-20251001"}
    assert not modernizer.apply(body).changed
    assert body["model"] == "claude-haiku-4-5-20251001"


@pytest.mark.parametrize(
    ("requested", "served"),
    [
        ("claude-haiku-4-5", "claude-haiku-5-5"),
        ("claude-haiku-4-5-20251001", "claude-haiku-5-5"),
        ("claude-sonnet-4-6", "claude-sonnet-5-5"),
        ("claude-sonnet-4-5-20250929", "claude-sonnet-5-5"),
        ("claude-opus-5", "claude-opus-5-5"),
        ("claude-opus-4-8", "claude-opus-5-5"),
    ],
)
def test_superseded_models_move_to_their_successor(monkeypatch, requested, served) -> None:
    body = {"model": requested, "thinking": {"type": "adaptive"}}
    decision = _modernizer(monkeypatch).apply(body)
    assert decision.changed
    assert body["model"] == served


@pytest.mark.parametrize(
    "model",
    [
        "claude-opus-5-5",  # already current: "claude-opus-5" must not match it
        "claude-sonnet-5-5",
        "claude-opus-4-6",  # not a clear price cut once the tokenizer is counted
        "claude-fable-5-1",
        "gpt-6.1-sol",
    ],
)
def test_current_or_unclear_models_are_left_alone(monkeypatch, model) -> None:
    body = {"model": model}
    assert not _modernizer(monkeypatch).apply(body).changed
    assert body["model"] == model


def test_version_suffix_matching() -> None:
    assert _matches("claude-opus-5-20260301", "claude-opus-5")
    assert _matches("claude-haiku-4-5-latest", "claude-haiku-4-5")
    assert not _matches("claude-opus-5-5", "claude-opus-5")
    assert not _matches("claude-opus-50", "claude-opus-5")


def test_every_turn_of_a_conversation_gets_the_same_model(monkeypatch) -> None:
    """The decision depends only on the requested id, so a growing session never switches."""
    modernizer = _modernizer(monkeypatch)
    served = set()
    messages: list[dict] = []
    for turn in range(20):
        messages.append({"role": "user", "content": "x" * (turn * 10_000)})
        body = {"model": "claude-sonnet-4-6", "messages": list(messages)}
        modernizer.apply(body, "conv-1")
        served.add(body["model"])
    assert served == {"claude-sonnet-5-5"}


def test_fast_mode_is_dropped_when_the_old_model_ignored_it(monkeypatch) -> None:
    mapping = json.dumps({"claude-opus-4-6": "claude-opus-5-5"})
    body = {"model": "claude-opus-4-6", "speed": "fast", "thinking": {"type": "adaptive"}}
    _modernizer(monkeypatch, HORIZON_MODEL_MODERNIZE_MAP=mapping).apply(body)
    assert body["model"] == "claude-opus-5-5"
    assert "speed" not in body


def test_fast_mode_is_kept_when_the_old_model_honoured_it(monkeypatch) -> None:
    body = {
        "model": "claude-opus-5",
        "speed": "fast",
        "thinking": {"type": "adaptive"},
        "output_config": {"effort": "xhigh"},
    }
    _modernizer(monkeypatch).apply(body)
    assert body["model"] == "claude-opus-5-5"
    assert body["speed"] == "fast"


def test_fast_mode_support() -> None:
    assert supports_fast_mode("claude-opus-5-5")
    assert supports_fast_mode("claude-opus-4-8")
    assert not supports_fast_mode("claude-opus-4-6")
    assert not supports_fast_mode("claude-sonnet-5-5")


def test_custom_map_replaces_the_default(monkeypatch) -> None:
    mapping = json.dumps({"claude-opus-4-6": "claude-opus-5-5"})
    modernizer = _modernizer(monkeypatch, HORIZON_MODEL_MODERNIZE_MAP=mapping)
    assert modernizer.successor("claude-opus-4-6") == "claude-opus-5-5"
    assert modernizer.successor("claude-haiku-4-5") is None


def test_invalid_map_falls_back_to_the_default(monkeypatch) -> None:
    modernizer = _modernizer(monkeypatch, HORIZON_MODEL_MODERNIZE_MAP="[not json")
    assert dict(modernizer.config.mapping) == DEFAULT_MAP


def test_holdout_is_stable_per_conversation(monkeypatch) -> None:
    modernizer = _modernizer(monkeypatch, HORIZON_MODEL_MODERNIZE_HOLDOUT="0.5")
    outcomes = {}
    for conv in (f"conv-{i}" for i in range(200)):
        first = modernizer.decide("claude-haiku-4-5", conv).changed
        assert all(modernizer.decide("claude-haiku-4-5", conv).changed == first for _ in range(3))
        outcomes[conv] = first
    treated = sum(outcomes.values())
    assert 60 < treated < 140  # roughly half, deterministic per conversation


def test_stats_report_requested_and_served_pairs(monkeypatch) -> None:
    modernizer = _modernizer(monkeypatch)
    assert modernizer.stats() is None
    for _ in range(3):
        modernizer.apply({"model": "claude-haiku-4-5"})
    stats = modernizer.stats()
    assert stats is not None
    assert stats["downgrades"] == 3
    assert stats["pairs"] == [
        {
            "requested": "claude-haiku-4-5",
            "served": "claude-haiku-5-5",
            "direction": "downgrade",
            "count": 3,
            "enforced": 3,
            "holdout": 0,
        }
    ]


# -- thinking and effort carried over -----------------------------------------


def test_haiku_side_call_without_thinking_stays_without_thinking(monkeypatch) -> None:
    """Haiku 5.5 thinks by default; a Haiku 4.5 side call did not."""
    body = {"model": "claude-haiku-4-5", "max_tokens": 512}
    _modernizer(monkeypatch).apply(body)
    assert body["model"] == "claude-haiku-5-5"
    assert body["thinking"] == {"type": "disabled"}


def test_sonnet_without_thinking_gets_sonnet_5_5_lowest_setting(monkeypatch) -> None:
    body = {"model": "claude-sonnet-4-6", "thinking": {"type": "disabled"}}
    _modernizer(monkeypatch).apply(body)
    assert body["model"] == "claude-sonnet-5-5"
    assert body["thinking"] == {"type": "between_tools"}


def test_sonnet_without_thinking_at_xhigh_keeps_its_model(monkeypatch) -> None:
    body = {"model": "claude-sonnet-4-6", "output_config": {"effort": "max"}}
    decision = _modernizer(monkeypatch).apply(body)
    assert not decision.changed
    assert body == {"model": "claude-sonnet-4-6", "output_config": {"effort": "max"}}


@pytest.mark.parametrize(
    "thinking",
    [None, {"type": "disabled"}, {"type": "enabled", "budget_tokens": 4096}],
)
def test_requests_opus_5_5_cannot_express_keep_their_model(monkeypatch, thinking) -> None:
    body = {"model": "claude-opus-5"}
    if thinking is not None:
        body["thinking"] = thinking
    before = dict(body)
    decision = _modernizer(monkeypatch).apply(body)
    assert not decision.changed
    assert body == before


def test_opus_effort_default_is_carried_over(monkeypatch) -> None:
    """Opus 5 defaulted to high; Opus 5.5 defaults to medium."""
    body = {"model": "claude-opus-5", "thinking": {"type": "adaptive"}}
    _modernizer(monkeypatch).apply(body)
    assert body["model"] == "claude-opus-5-5"
    assert body["output_config"] == {"effort": "high"}


def test_an_explicit_effort_is_left_alone(monkeypatch) -> None:
    body = {
        "model": "claude-opus-4-8",
        "thinking": {"type": "adaptive"},
        "output_config": {"effort": "low"},
    }
    _modernizer(monkeypatch).apply(body)
    assert body["output_config"] == {"effort": "low"}
