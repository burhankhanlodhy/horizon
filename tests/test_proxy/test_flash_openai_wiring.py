"""Flash Observations (next-turn form) through the OpenAI handlers."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from horizon.proxy.server import ProxyConfig, create_app
from horizon.transforms.flash_openai import NOTICE
from tests.test_proxy.test_model_router_wiring import _install_fake_client

LOG = "\n".join(f"tests/test_io.py::test_{i} PASSED" for i in range(800))
AUTH = {"authorization": "Bearer sk-test-key"}


def _app(monkeypatch, tmp_path, mode: str = "cache", enabled: bool = True):
    if enabled:
        monkeypatch.setenv("HORIZON_FLASH_OPENAI", "1")
    else:
        monkeypatch.delenv("HORIZON_FLASH_OPENAI", raising=False)
    monkeypatch.delenv("HORIZON_FLASH_ANY_UPSTREAM", raising=False)
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


def _sent(http) -> dict:
    """The JSON body of the last upstream call, whichever forward shape was used."""
    for method in (http.post, http.request, http.send, http.build_request):
        if method.call_args is None:
            continue
        kwargs = method.call_args.kwargs
        if kwargs.get("json") is not None:
            return kwargs["json"]
        if kwargs.get("content") is not None:
            return json.loads(kwargs["content"])
    raise AssertionError("nothing was forwarded")


def _responses_input(turns: int) -> list[dict]:
    items: list[dict] = [{"type": "message", "role": "user", "content": "run the tests"}]
    for t in range(turns):
        items += [
            {"type": "reasoning", "id": f"rs{t}", "summary": [], "encrypted_content": f"e{t}"},
            {"type": "function_call", "call_id": f"c{t}", "name": "shell", "arguments": "{}"},
            {"type": "function_call_output", "call_id": f"c{t}", "output": LOG + f"\n#{t}"},
        ]
    return items


def _chat_messages(turns: int) -> list[dict]:
    messages: list[dict] = [{"role": "user", "content": "run the tests"}]
    for t in range(turns):
        messages += [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": f"c{t}",
                        "type": "function",
                        "function": {"name": "bash", "arguments": "{}"},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": f"c{t}", "content": LOG + f"\n#{t}"},
        ]
    return messages


def _outputs(items: list[dict]) -> list[str]:
    return [i["output"] for i in items if i.get("type") == "function_call_output"]


@pytest.mark.parametrize("mode", ["cache", "token"])
def test_responses_newest_output_shown_once_then_stubbed(monkeypatch, tmp_path, mode) -> None:
    app = _app(monkeypatch, tmp_path, mode)
    forwarded: list[list[dict]] = []
    with TestClient(app) as client:
        http = _install_fake_client(client.app.state.proxy)
        for turns in (1, 2, 3):
            body = {"model": "gpt-6.1-sol", "input": _responses_input(turns), "stream": False}
            assert client.post("/v1/responses", json=body, headers=AUTH).status_code == 200
            forwarded.append(_sent(http)["input"])
    for turn, items in enumerate(forwarded, start=1):
        outputs = _outputs(items)
        assert outputs[-1].endswith(NOTICE)
        for stub in outputs[:-1]:
            assert stub.startswith("[Horizon flash") and LOG not in stub
        assert len(outputs) == turn
    # Append-only: a later request repeats every earlier item but the newest
    # output, which is where the stub replaces the shown-once text.
    for earlier, later in zip(forwarded, forwarded[1:], strict=False):
        assert later[: len(earlier) - 1] == earlier[:-1]
    # Encrypted reasoning items are forwarded untouched.
    assert [i for i in forwarded[-1] if i.get("type") == "reasoning"] == [
        i for i in _responses_input(3) if i.get("type") == "reasoning"
    ]


def test_responses_with_server_side_history_is_left_alone(monkeypatch, tmp_path) -> None:
    app = _app(monkeypatch, tmp_path)
    with TestClient(app) as client:
        http = _install_fake_client(client.app.state.proxy)
        body = {
            "model": "gpt-6.1-sol",
            "input": _responses_input(2),
            "previous_response_id": "resp_1",
            "stream": False,
        }
        assert client.post("/v1/responses", json=body, headers=AUTH).status_code == 200
        assert not any(
            NOTICE in o or o.startswith("[Horizon flash") for o in _outputs(_sent(http)["input"])
        )


def test_off_by_default(monkeypatch, tmp_path) -> None:
    app = _app(monkeypatch, tmp_path, enabled=False)
    with TestClient(app) as client:
        http = _install_fake_client(client.app.state.proxy)
        body = {"model": "gpt-6.1-sol", "input": _responses_input(2), "stream": False}
        assert client.post("/v1/responses", json=body, headers=AUTH).status_code == 200
        assert not any(
            NOTICE in o or o.startswith("[Horizon flash") for o in _outputs(_sent(http)["input"])
        )


def test_third_party_upstream_is_left_alone(monkeypatch, tmp_path) -> None:
    app = _app(monkeypatch, tmp_path)
    with TestClient(app) as client:
        proxy = client.app.state.proxy
        http = _install_fake_client(proxy)
        monkeypatch.setattr(proxy, "OPENAI_API_URL", "https://gateway.example")
        body = {"model": "gpt-6.1-sol", "input": _responses_input(2), "stream": False}
        assert client.post("/v1/responses", json=body, headers=AUTH).status_code == 200
        assert http.post.call_args.args[0].startswith("https://gateway.example")
        assert not any(
            NOTICE in o or o.startswith("[Horizon flash") for o in _outputs(_sent(http)["input"])
        )


@pytest.mark.parametrize("mode", ["cache", "token"])
def test_chat_completions_newest_output_shown_once_then_stubbed(
    monkeypatch, tmp_path, mode
) -> None:
    app = _app(monkeypatch, tmp_path, mode)
    forwarded: list[list[dict]] = []
    with TestClient(app) as client:
        http = _install_fake_client(client.app.state.proxy)
        for turns in (1, 2, 3):
            body = {"model": "gpt-6.1-sol", "messages": _chat_messages(turns)}
            assert client.post("/v1/chat/completions", json=body, headers=AUTH).status_code == 200
            forwarded.append(_sent(http)["messages"])
    for turn, messages in enumerate(forwarded, start=1):
        tool = [m["content"] for m in messages if m.get("role") == "tool"]
        assert len(tool) == turn
        assert tool[-1].endswith(NOTICE)
        assert all(t.startswith("[Horizon flash") for t in tool[:-1])
    for earlier, later in zip(forwarded, forwarded[1:], strict=False):
        assert later[: len(earlier) - 1] == earlier[:-1]


def test_model_where_cache_reads_are_nearly_free_is_left_alone(monkeypatch, tmp_path) -> None:
    from horizon.transforms import flash_openai

    flash_openai._DECISIONS.clear()
    monkeypatch.delenv("HORIZON_FLASH_MIN_READ_RATIO", raising=False)
    app = _app(monkeypatch, tmp_path)
    with TestClient(app) as client:
        http = _install_fake_client(client.app.state.proxy)
        body = {"model": "deepseek-v4-pro", "messages": _chat_messages(2)}
        assert client.post("/v1/chat/completions", json=body, headers=AUTH).status_code == 200
        tool = [m["content"] for m in _sent(http)["messages"] if m.get("role") == "tool"]
    assert not any(NOTICE in t or t.startswith("[Horizon flash") for t in tool)
