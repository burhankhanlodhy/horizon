"""OpenAI Flex tier: Batch-rate pricing for headless API-key traffic, with a 429 fallback."""

from __future__ import annotations

import contextvars
import json
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

pytest.importorskip("litellm")

from horizon.proxy.flex_policy import apply_flex, fallback_body, is_headless  # noqa: E402

API = "https://api.openai.com/v1/responses"
CHATGPT = "https://chatgpt.com/backend-api/codex/responses"
EXEC = {"originator": "codex_exec"}
TUI = {"originator": "codex_cli_rs"}


def _body(**extra):
    return {"model": "gpt-6.1-sol", "input": "hi", **extra}


def _run(fn, *args, **kwargs):
    """Run in a fresh context, as each request does."""
    return contextvars.Context().run(fn, *args, **kwargs)


def test_off_by_default(monkeypatch) -> None:
    monkeypatch.delenv("HORIZON_OPENAI_FLEX_POLICY", raising=False)
    body = _body()
    assert not apply_flex(body, url=API, headers=EXEC)
    assert "service_tier" not in body


def test_headless_api_key_traffic_gets_flex(monkeypatch) -> None:
    monkeypatch.setenv("HORIZON_OPENAI_FLEX_POLICY", "headless")
    body = _body()
    assert apply_flex(body, url=API, headers=EXEC)
    assert body["service_tier"] == "flex"


def test_interactive_traffic_is_left_alone(monkeypatch) -> None:
    monkeypatch.setenv("HORIZON_OPENAI_FLEX_POLICY", "headless")
    body = _body()
    assert not apply_flex(body, url=API, headers=TUI)
    assert "service_tier" not in body


@pytest.mark.parametrize(
    ("url", "chatgpt_auth", "body_extra", "model"),
    [
        (CHATGPT, True, {}, "gpt-6.1-sol"),  # subscription traffic: tiers do not apply
        ("https://my-gateway.example/v1/responses", False, {}, "gpt-6.1-sol"),
        (API, False, {"service_tier": "priority"}, "gpt-6.1-sol"),  # client's choice wins
        (API, False, {}, "gpt-4o"),  # no Flex rate in the catalog
    ],
)
def test_ineligible_requests_are_never_changed(
    monkeypatch, url, chatgpt_auth, body_extra, model
) -> None:
    monkeypatch.setenv("HORIZON_OPENAI_FLEX_POLICY", "always")
    body = {"model": model, "input": "hi", **body_extra}
    before = dict(body)
    assert not apply_flex(body, url=url, headers=EXEC, chatgpt_auth=chatgpt_auth)
    assert body == before


def test_explicit_interactive_header_wins(monkeypatch) -> None:
    assert is_headless({"x-horizon-interactive": "0", **TUI})
    assert not is_headless({"x-horizon-interactive": "1", **EXEC})


def test_fallback_only_for_a_tier_horizon_added(monkeypatch) -> None:
    monkeypatch.setenv("HORIZON_OPENAI_FLEX_POLICY", "always")

    def added_then_429():
        body = _body()
        apply_flex(body, url=API, headers=TUI)
        first = fallback_body(body, 429)
        second = fallback_body(body, 429)
        return body, first, second

    body, first, second = _run(added_then_429)
    assert first == {"model": "gpt-6.1-sol", "input": "hi"}
    assert second is None  # one fallback per request

    def client_chose_flex():
        body = _body(service_tier="flex")
        apply_flex(body, url=API, headers=TUI)
        return fallback_body(body, 429)

    assert _run(client_chose_flex) is None

    def added_then_200():
        body = _body()
        apply_flex(body, url=API, headers=TUI)
        return fallback_body(body, 200)

    assert _run(added_then_200) is None


def test_retry_request_falls_back_to_standard_on_flex_429(monkeypatch) -> None:
    """End to end through the buffered forwarder: 429 at Flex, then 200 at standard."""
    import asyncio

    from horizon.proxy.server import HorizonProxy, ProxyConfig

    monkeypatch.setenv("HORIZON_OPENAI_FLEX_POLICY", "always")
    proxy = HorizonProxy.__new__(HorizonProxy)
    proxy.config = ProxyConfig(retry_max_attempts=1)
    sent: list[dict] = []
    replies = [httpx.Response(429, json={"error": "flex capacity"}), httpx.Response(200, json={})]

    async def post(url, **kwargs):
        sent.append(json.loads(kwargs["content"]))
        return replies.pop(0)

    proxy.http_client = MagicMock()
    proxy.http_client.post = AsyncMock(side_effect=post)

    async def scenario():
        body = _body()
        apply_flex(body, url=API, headers=EXEC)
        return await proxy._retry_request("POST", API, {}, body)

    response = contextvars.Context().run(asyncio.run, scenario())
    assert response.status_code == 200
    assert sent[0]["service_tier"] == "flex"
    assert "service_tier" not in sent[1]
