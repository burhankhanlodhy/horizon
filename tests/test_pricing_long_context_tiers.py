"""Per-model long-context price tiers (Haiku 5.5 at 100k, GPT-6.1 Sol at 272k, Opus 5.5 flat)."""

from __future__ import annotations

import pytest

pytest.importorskip("litellm")

from horizon.pricing.counterfactual import (  # noqa: E402
    CacheMix,
    is_long_context_for,
    long_context_suffix,
    long_context_threshold,
    resolve_rates,
)


@pytest.mark.parametrize(
    ("model", "threshold"),
    [
        ("claude-haiku-5-5", 100_000),
        ("gpt-6.1-sol", 272_000),
        ("claude-sonnet-4-5-20250929", 200_000),
        ("claude-opus-5-5", None),
        ("claude-sonnet-5-5", None),
    ],
)
def test_threshold_comes_from_the_models_own_row(model, threshold) -> None:
    assert long_context_threshold(model) == threshold


def test_haiku_5_5_bills_its_high_tier_above_100k() -> None:
    assert not is_long_context_for("claude-haiku-5-5", 99_000)
    assert is_long_context_for("claude-haiku-5-5", 101_000)
    low = resolve_rates("claude-haiku-5-5")
    high = resolve_rates("claude-haiku-5-5", long_context=True)
    assert high.uncached == pytest.approx(5 * low.uncached)
    assert high.read == pytest.approx(5 * low.read)
    # The combined 1h + above-100k rate is published, so it is read, not derived.
    assert high.write_1h == pytest.approx(1e-06)
    assert high.basis == "catalog"


def test_flat_rated_models_never_switch_tier() -> None:
    assert not is_long_context_for("claude-opus-5-5", 900_000)
    mix = CacheMix.from_usage(cache_read_tokens=900_000)
    assert not mix.is_long_context(model="claude-opus-5-5")


def test_gpt_tier_starts_at_272k_not_200k() -> None:
    assert not is_long_context_for("gpt-6.1-sol", 250_000)
    assert is_long_context_for("gpt-6.1-sol", 280_000)
    assert long_context_suffix("gpt-6.1-sol") == "_above_272k_tokens"
    high = resolve_rates("gpt-6.1-sol", long_context=True)
    assert high.uncached == pytest.approx(4e-06)
    assert high.read == pytest.approx(2e-07)


def test_without_a_model_the_legacy_threshold_applies() -> None:
    assert not CacheMix.from_usage(cache_read_tokens=199_000).is_long_context()
    assert CacheMix.from_usage(cache_read_tokens=201_000).is_long_context()
