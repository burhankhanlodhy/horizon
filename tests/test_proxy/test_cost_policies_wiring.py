"""End-to-end wiring for model modernization and the fast-mode governor.

Each test sends a real ``/v1/messages`` request through the app and inspects
the body forwarded upstream.
"""

from __future__ import annotations

import pytest
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


# -- Flash Observations ---------------------------------------------------------

_LOG = "\n".join(f"tests/test_io.py::test_{i} PASSED" for i in range(800))


def _flash_turns(n: int, *, thinking: bool = False) -> list[dict]:
    messages: list[dict] = [{"role": "user", "content": "run the test suite and fix failures"}]
    for t in range(n):
        content: list[dict] = [{"type": "tool_use", "id": f"call{t}", "name": "Bash", "input": {}}]
        if thinking:  # as Opus 5.5 returns them, echoed back by the client
            content.insert(0, {"type": "thinking", "thinking": "", "signature": f"sig{t}"})
        messages += [
            {"role": "assistant", "content": content},
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": f"call{t}", "content": _LOG + f"\n#{t}"}
                ],
            },
        ]
    return messages


def _flash_app(monkeypatch, tmp_path, mode: str):
    monkeypatch.setenv("HORIZON_FLASH_OBSERVATIONS", "1")
    monkeypatch.setenv("HORIZON_WORKSPACE_DIR", str(tmp_path))
    config = ProxyConfig(
        optimize=True,
        cache_enabled=False,
        rate_limit_enabled=False,
        cost_tracking_enabled=False,
        ccr_inject_tool=False,
        ccr_handle_responses=False,
        ccr_context_tracking=False,
        mode=mode,
    )
    return create_app(config)


@pytest.mark.parametrize("thinking", [False, True])
@pytest.mark.parametrize("mode", ["cache", "token"])
def test_flash_observations_are_append_only_through_the_handler(
    monkeypatch, tmp_path, mode, thinking
) -> None:
    app = _flash_app(monkeypatch, tmp_path, mode)
    forwarded: list[dict] = []
    headers: list[dict] = []
    with TestClient(app) as client:
        http = _install_fake_client(client.app.state.proxy)
        for turns in (1, 2, 3):
            body = {
                "model": "claude-opus-5-5",
                "max_tokens": 16,
                "messages": _flash_turns(turns, thinking=thinking),
            }
            assert client.post(MESSAGES, json=body).status_code == 200
            forwarded.append(_forwarded_body(http)["messages"])
            headers.append(http.post.call_args.kwargs["headers"])
    for msgs in forwarded:
        last = msgs[-1]
        assert last["role"] == "system" and last["clear_at"] == "next_user_message"
        assert _LOG in last["content"][0]["text"]
    # Cache markers move to the newest message every turn (as in Claude Code);
    # they are not part of the cached content, so compare without them.
    from horizon.proxy.cache_miss_watch import _strip_cache_control

    stripped = [_strip_cache_control(m) for m in forwarded]
    for earlier, later in zip(stripped, stripped[1:], strict=False):
        assert later[: len(earlier)] == earlier, "an earlier turn changed on a later request"
    for h in headers:
        beta = next(v for k, v in h.items() if k.lower() == "anthropic-beta")
        assert "mid-conversation-system-clear-at-2026-08-21" in beta


def test_flash_observations_off_by_default(monkeypatch) -> None:
    monkeypatch.delenv("HORIZON_FLASH_OBSERVATIONS", raising=False)
    forwarded = _send(model="claude-opus-5-5", messages=_flash_turns(1))
    assert not any(m.get("role") == "system" for m in forwarded["messages"])


def test_flash_observations_skip_unsupported_models(monkeypatch, tmp_path) -> None:
    app = _flash_app(monkeypatch, tmp_path, "cache")
    with TestClient(app) as client:
        http = _install_fake_client(client.app.state.proxy)
        body = {"model": "claude-sonnet-5", "max_tokens": 16, "messages": _flash_turns(1)}
        assert client.post(MESSAGES, json=body).status_code == 200
        forwarded = _forwarded_body(http)["messages"]
    assert not any(m.get("role") == "system" for m in forwarded)


def test_flash_observations_skip_third_party_upstreams(monkeypatch, tmp_path) -> None:
    app = _flash_app(monkeypatch, tmp_path, "cache")
    with TestClient(app) as client:
        proxy = client.app.state.proxy
        proxy.ANTHROPIC_API_URL = "https://gateway.example"
        http = _install_fake_client(proxy)
        body = {"model": "claude-opus-5-5", "max_tokens": 16, "messages": _flash_turns(1)}
        assert client.post(MESSAGES, json=body).status_code == 200
        forwarded = _forwarded_body(http)["messages"]
    assert not any(m.get("role") == "system" for m in forwarded)
