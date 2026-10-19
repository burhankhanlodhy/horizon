"""Pricing in the frozen desktop client, which cannot import litellm.

The frozen client ships the price rows the dev venv prices with (build_pricing.py
writes them to horizon/pricing/_model_prices.json). These tests block the litellm
import and read a small fixture in its place; the live module is left alone.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

import horizon.pricing.counterfactual as counterfactual
import horizon.pricing.litellm_pricing as live

FIXTURE = {
    "claude-opus-5-5": {
        "input_cost_per_token": 4e-06,
        "output_cost_per_token": 2e-05,
        "cache_read_input_token_cost": 2e-07,
        "cache_creation_input_token_cost": 5e-06,
        "cache_creation_input_token_cost_above_1hr": 8e-06,
        "litellm_provider": "anthropic",
        "mode": "chat",
    },
    "claude-haiku-5-5": {"input_cost_per_token": 1e-07, "output_cost_per_token": 5e-07},
}


@pytest.fixture
def frozen(monkeypatch):
    """litellm_pricing as a frozen client loads it: no litellm, the bundled fixture."""
    monkeypatch.setitem(sys.modules, "litellm", None)  # `import litellm` now raises ImportError
    name = "horizon.pricing._frozen_view"
    spec = importlib.util.spec_from_file_location(name, Path(live.__file__))
    module = importlib.util.module_from_spec(spec)
    # Registered before execution: dataclasses look the module up by name.
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "_read_bundled_text", lambda: json.dumps(FIXTURE))
    monkeypatch.setattr(module, "_bundled_cache", None)
    return module


def test_the_frozen_view_has_no_litellm_and_reads_the_bundled_rows(frozen) -> None:
    assert frozen.LITELLM_AVAILABLE is False
    assert frozen.model_cost_table() == FIXTURE


def test_an_opus_session_is_priced_from_the_bundled_rows(frozen) -> None:
    assert frozen.pricing_per_1m("claude-opus-5-5") == pytest.approx((4.0, 20.0))
    pricing = frozen.get_model_pricing("claude-opus-5-5")
    assert pricing is not None
    assert pricing.cache_read_per_1m == pytest.approx(0.2)
    assert pricing.cache_write_1h_per_1m == pytest.approx(8.0)


def test_an_unknown_model_stays_unpriced(frozen) -> None:
    assert frozen.pricing_per_1m("no-such-model-xyz") is None
    assert frozen.get_model_pricing("no-such-model-xyz") is None


def test_a_missing_bundled_file_means_no_prices_rather_than_an_error(frozen, monkeypatch) -> None:
    def missing() -> str:
        raise FileNotFoundError("_model_prices.json")

    monkeypatch.setattr(frozen, "_read_bundled_text", missing)
    monkeypatch.setattr(frozen, "_bundled_cache", None)
    assert frozen.model_cost_table() == {}
    assert frozen.pricing_per_1m("claude-opus-5-5") is None


def test_a_corrupt_bundled_file_means_no_prices(frozen, monkeypatch) -> None:
    monkeypatch.setattr(frozen, "_read_bundled_text", lambda: "{not json")
    monkeypatch.setattr(frozen, "_bundled_cache", None)
    assert frozen.model_cost_table() == {}


def test_with_litellm_installed_the_live_table_is_used_unchanged() -> None:
    """The fallback is never consulted when litellm imports: behaviour is as before."""
    pytest.importorskip("litellm")
    if not live.LITELLM_AVAILABLE:
        pytest.skip("litellm not importable in this environment")
    assert live.model_cost_table() is live.litellm.model_cost


def test_counterfactual_rates_resolve_from_the_bundled_rows(monkeypatch) -> None:
    """The rate resolver the savings path uses works in the frozen client too."""
    monkeypatch.setattr(live, "LITELLM_AVAILABLE", False)
    monkeypatch.setattr(live, "litellm", None)
    monkeypatch.setattr(live, "_bundled_cache", FIXTURE)
    counterfactual.resolve_rates.cache_clear()
    try:
        rates = counterfactual.resolve_rates("claude-opus-5-5")
        assert rates is not None
        assert rates.uncached == pytest.approx(4e-06)
        assert rates.read == pytest.approx(2e-07)
        assert rates.write_5m == pytest.approx(5e-06)
        assert rates.write_1h == pytest.approx(8e-06)
        assert counterfactual.resolve_rates("no-such-model-xyz") is None
    finally:
        counterfactual.resolve_rates.cache_clear()
