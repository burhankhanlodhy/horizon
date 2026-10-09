"""HORIZON_SAVINGS: one switch that fills in the cost features' defaults."""

from __future__ import annotations

from horizon.proxy.savings_profile import PROFILES, apply_savings_profile


def test_off_by_default_sets_nothing() -> None:
    env: dict[str, str] = {}
    assert apply_savings_profile(env) == {}
    assert env == {}


def test_auto_fills_unset_and_empty_but_never_overrides() -> None:
    env = {
        "HORIZON_SAVINGS": "auto",
        "HORIZON_FLASH_OBSERVATIONS": "",  # compose passes an empty default
        "HORIZON_PRICE_CLIFF_GUARD": "0",  # an explicit "off" wins
    }
    applied = apply_savings_profile(env)
    assert env["HORIZON_FLASH_OBSERVATIONS"] == "1"
    assert env["HORIZON_PRICE_CLIFF_GUARD"] == "0"
    assert env["HORIZON_FLASH_OPENAI_UPSTREAMS"] == "*"
    assert env["HORIZON_OPENAI_FLEX_POLICY"] == "headless"
    assert "HORIZON_MODEL_MODERNIZE" not in env  # auto never swaps the model
    assert "HORIZON_PRICE_CLIFF_GUARD" not in applied
    assert apply_savings_profile(env) == {}  # idempotent


def test_max_adds_model_modernization() -> None:
    env = {"HORIZON_SAVINGS": "MAX"}
    apply_savings_profile(env)
    assert env["HORIZON_MODEL_MODERNIZE"] == "1"
    assert set(PROFILES["auto"]) < set(PROFILES["max"])


def test_unknown_profile_is_off() -> None:
    env = {"HORIZON_SAVINGS": "turbo"}
    assert apply_savings_profile(env) == {}


def test_auto_through_the_app_shows_in_stats(monkeypatch, tmp_path) -> None:
    from fastapi.testclient import TestClient

    from horizon.proxy import savings_profile
    from horizon.proxy.server import ProxyConfig, create_app

    for key in PROFILES["max"]:
        monkeypatch.setenv(key, "")  # registered, so restored after the test
    monkeypatch.setenv("HORIZON_SAVINGS", "auto")
    monkeypatch.setenv("HORIZON_WORKSPACE_DIR", str(tmp_path))
    savings_profile.reset_for_tests()
    try:
        app = create_app(
            ProxyConfig(cache_enabled=False, rate_limit_enabled=False, cost_tracking_enabled=False)
        )
        with TestClient(app) as client:
            stats = client.get("/stats").json()
        assert stats["savings_profile"]["profile"] == "auto"
        assert stats["savings_profile"]["applied"]["HORIZON_FLASH_OBSERVATIONS"] == "1"
        assert {"outputs_stubbed", "switched_off", "price_skips"} <= set(stats["flash"])
    finally:
        savings_profile.reset_for_tests()
