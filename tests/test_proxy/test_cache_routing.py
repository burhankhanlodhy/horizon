"""Grok cache routing: a stable conversation id when the client sends none."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi.testclient import TestClient

from horizon.proxy import cache_routing


@pytest.fixture(autouse=True)
def _on(monkeypatch):
    monkeypatch.setenv("HORIZON_CACHE_ROUTING", "1")


def _turns(n: int) -> list[dict]:
    messages = [
        {"role": "system", "content": "be brief"},
        {"role": "user", "content": "fix the bug"},
    ]
    for t in range(n - 1):
        messages += [
            {"role": "assistant", "content": f"step {t}"},
            {"role": "user", "content": "go on"},
        ]
    return messages


def test_grok_is_recognised_by_name_or_host() -> None:
    assert cache_routing.is_grok("grok-4.6")
    assert cache_routing.is_grok("x-ai/grok-4.7")
    assert cache_routing.is_grok("some-model", "https://api.x.ai/v1")
    assert not cache_routing.is_grok("gpt-6.1-sol", "https://api.openai.com/v1")


def test_one_id_per_conversation_across_turns() -> None:
    first, later = {}, {}
    cache_routing.apply_chat(first, model="grok-4.6", url="", messages=_turns(1))
    cache_routing.apply_chat(later, model="grok-4.6", url="", messages=_turns(4))
    assert first["x-grok-conv-id"] == later["x-grok-conv-id"]
    other = {}
    cache_routing.apply_chat(
        other, model="grok-4.6", url="", messages=[{"role": "user", "content": "another task"}]
    )
    assert other["x-grok-conv-id"] != first["x-grok-conv-id"]


def test_accounts_never_share_an_id(monkeypatch) -> None:
    ids = []
    for account in ("acct-a", "acct-b"):
        monkeypatch.setattr("horizon.proxy.account_analytics.account_id", lambda a=account: a)
        headers: dict[str, str] = {}
        cache_routing.apply_chat(headers, model="grok-4.6", url="", messages=_turns(1))
        ids.append(headers["x-grok-conv-id"])
    assert ids[0] != ids[1]


def test_the_clients_own_id_wins_and_other_models_are_untouched() -> None:
    headers = {"X-Grok-Conv-Id": "client-session"}
    assert cache_routing.apply_chat(headers, model="grok-4.6", url="", messages=_turns(1)) is None
    assert headers == {"X-Grok-Conv-Id": "client-session"}
    headers = {}
    assert (
        cache_routing.apply_chat(headers, model="gpt-6.1-sol", url="", messages=_turns(1)) is None
    )
    assert headers == {}
    body = {"model": "grok-4.6", "input": "hi", "prompt_cache_key": "mine"}
    assert cache_routing.apply_responses(body, model="grok-4.6", url="") is None
    assert body["prompt_cache_key"] == "mine"


def test_off_unless_enabled(monkeypatch) -> None:
    monkeypatch.delenv("HORIZON_CACHE_ROUTING")
    headers: dict[str, str] = {}
    assert cache_routing.apply_chat(headers, model="grok-4.6", url="", messages=_turns(1)) is None
    assert headers == {}


def test_responses_chained_on_the_server_are_left_alone() -> None:
    body = {"model": "grok-4.6", "input": "go on", "previous_response_id": "resp_1"}
    assert cache_routing.apply_responses(body, model="grok-4.6", url="") is None
    assert "prompt_cache_key" not in body


# -- through the handler -----------------------------------------------------------


def _app(monkeypatch, tmp_path):
    from horizon.proxy.server import ProxyConfig, create_app

    monkeypatch.setenv("HORIZON_WORKSPACE_DIR", str(tmp_path))
    return create_app(
        ProxyConfig(
            optimize=False,
            cache_enabled=False,
            rate_limit_enabled=False,
            cost_tracking_enabled=False,
            ccr_inject_tool=False,
            ccr_handle_responses=False,
            ccr_context_tracking=False,
        )
    )


def _client(proxy) -> MagicMock:
    def respond(*_a, **_kw):
        payload = {
            "id": "x",
            "object": "chat.completion",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "ok"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 1, "total_tokens": 11},
            "output": [],
        }
        return httpx.Response(
            200, json=payload, request=httpx.Request("POST", "https://api.x.ai/v1/x")
        )

    http = MagicMock()
    http.post = AsyncMock(side_effect=respond)
    http.request = AsyncMock(side_effect=respond)
    http.send = AsyncMock(side_effect=respond)
    http.aclose = AsyncMock()
    proxy.http_client = http
    return http


def test_chat_completions_to_grok_carry_a_stable_conv_id(monkeypatch, tmp_path) -> None:
    sent: list[str | None] = []
    with TestClient(_app(monkeypatch, tmp_path)) as client:
        http = _client(client.app.state.proxy)
        for n in (1, 3):
            body = {"model": "grok-4.6", "messages": _turns(n)}
            response = client.post(
                "/v1/chat/completions", json=body, headers={"authorization": "Bearer xai-t"}
            )
            assert response.status_code == 200
            headers = {k.lower(): v for k, v in http.post.call_args.kwargs["headers"].items()}
            sent.append(headers.get("x-grok-conv-id"))
    assert sent[0] and sent[0] == sent[1]


def test_responses_to_grok_carry_a_prompt_cache_key(monkeypatch, tmp_path) -> None:
    with TestClient(_app(monkeypatch, tmp_path)) as client:
        http = _client(client.app.state.proxy)
        body = {"model": "grok-4.6", "input": [{"role": "user", "content": "fix the bug"}]}
        response = client.post(
            "/v1/responses", json=body, headers={"authorization": "Bearer xai-t"}
        )
        assert response.status_code == 200
        forwarded = json.loads(http.post.call_args.kwargs["content"])
    assert forwarded.get("prompt_cache_key")
