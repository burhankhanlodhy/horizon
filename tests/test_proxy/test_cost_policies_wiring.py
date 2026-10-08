"""End-to-end wiring for model modernization and the fast-mode governor.

Each test sends a real ``/v1/messages`` request through the app and inspects
the body forwarded upstream.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from horizon.proxy.routing_stats import clear_routing_stats_provider, get_routing_stats
from horizon.proxy.server import ProxyConfig, create_app
from tests.test_proxy.test_model_router_wiring import _forwarded_body, _install_fake_client

MESSAGES = "/v1/messages"


def _config() -> ProxyConfig:
    return ProxyConfig(
        optimize=False,
        cache_enabled=False,
        rate_limit_enabled=False,
        cost_tracking_enabled=False,
        ccr_inject_tool=False,
        ccr_handle_responses=False,
        ccr_context_tracking=False,
        mode="token",
    )


def _send(headers: dict[str, str] | None = None, **body_extra) -> dict:
    body = {
        "model": "claude-sonnet-4-6",
        "max_tokens": 16,
        "messages": [{"role": "user", "content": "hi"}],
        **body_extra,
    }
    app = create_app(_config())
    with TestClient(app) as client:
        http = _install_fake_client(client.app.state.proxy)
        resp = client.post(MESSAGES, json=body, headers=headers or {})
        assert resp.status_code == 200
        return _forwarded_body(http)


def test_modernize_off_by_default(monkeypatch) -> None:
    monkeypatch.delenv("HORIZON_MODEL_MODERNIZE", raising=False)
    assert _send()["model"] == "claude-sonnet-4-6"


def test_modernize_rewrites_the_forwarded_model(monkeypatch) -> None:
    clear_routing_stats_provider()
    monkeypatch.setenv("HORIZON_MODEL_MODERNIZE", "1")
    try:
        assert _send()["model"] == "claude-sonnet-5-5"
        stats = get_routing_stats()
        assert stats is not None
        assert stats["pairs"][0]["served"] == "claude-sonnet-5-5"
    finally:
        clear_routing_stats_provider()


def test_modernize_respects_bypass(monkeypatch) -> None:
    monkeypatch.setenv("HORIZON_MODEL_MODERNIZE", "1")
    forwarded = _send(headers={"x-horizon-bypass": "true"})
    assert forwarded["model"] == "claude-sonnet-4-6"


def test_fast_mode_dropped_for_headless_launch(monkeypatch) -> None:
    monkeypatch.setenv("HORIZON_FAST_MODE_POLICY", "headless")
    forwarded = _send(headers={"x-horizon-interactive": "0"}, model="claude-opus-5-5", speed="fast")
    assert "speed" not in forwarded


def test_fast_mode_kept_for_interactive_launch(monkeypatch) -> None:
    monkeypatch.setenv("HORIZON_FAST_MODE_POLICY", "headless")
    forwarded = _send(headers={"x-horizon-interactive": "1"}, model="claude-opus-5-5", speed="fast")
    assert forwarded["speed"] == "fast"


def test_cache_miss_watch_reports_an_effort_change_in_stats(monkeypatch) -> None:
    monkeypatch.setenv("HORIZON_CACHE_MISS_WATCH", "1")
    first = {
        "model": "claude-opus-5-5",
        "max_tokens": 16,
        "output_config": {"effort": "xhigh"},
        "messages": [{"role": "user", "content": "fix the bug " * 50}],
    }
    second = {
        **first,
        "output_config": {"effort": "low"},
        "messages": first["messages"]
        + [
            {"role": "assistant", "content": "ok"},
            {"role": "user", "content": "go on"},
        ],
    }
    app = create_app(_config())
    with TestClient(app) as client:
        _install_fake_client(client.app.state.proxy)
        assert client.post(MESSAGES, json=first).status_code == 200
        assert client.post(MESSAGES, json=second).status_code == 200
        stats = client.get("/stats").json()
    misses = stats["cache_misses"]
    assert misses["predicted_misses"] == 1
    assert set(misses["by_cause"]) == {"effort"}


def test_cache_miss_watch_off_by_default(monkeypatch) -> None:
    monkeypatch.delenv("HORIZON_CACHE_MISS_WATCH", raising=False)
    app = create_app(_config())
    with TestClient(app) as client:
        assert client.app.state.proxy.cache_miss_watch is None
