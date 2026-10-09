"""Flash Observations on the native Gemini API (next-turn stubbing)."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from horizon.transforms import flash_gemini
from horizon.transforms.flash_observations import FlashPolicy
from horizon.transforms.flash_openai import NOTICE

LOG = "\n".join(f"tests/test_io.py::test_{i} PASSED" for i in range(800))
POLICY = FlashPolicy(tools=frozenset(flash_gemini.DEFAULT_GEMINI_TOOLS), min_chars=8_000)


def _turns(n: int, *, tool: str = "run_shell_command", response: Any = None) -> list[dict]:
    contents: list[dict] = [{"role": "user", "parts": [{"text": "run the tests"}]}]
    for t in range(n):
        contents += [
            {
                "role": "model",
                "parts": [
                    {
                        "functionCall": {"name": tool, "args": {"command": f"pytest -k s{t}"}},
                        "thoughtSignature": f"sig{t}",
                    }
                ],
            },
            {
                "role": "user",
                "parts": [
                    {
                        "functionResponse": {
                            "name": tool,
                            "response": response
                            if response is not None
                            else {"output": LOG + f"\n#{t}"},
                        }
                    }
                ],
            },
        ]
    return contents


def _outputs(contents: list[dict]) -> list[str]:
    return [
        next(iter(part["functionResponse"]["response"].values()))
        for c in contents
        for part in c.get("parts", [])
        if "functionResponse" in part
    ]


def test_newest_output_is_shown_once_and_earlier_ones_are_stubbed() -> None:
    first = flash_gemini.apply_gemini(_turns(1), horizon=0, policy=POLICY)
    assert first.flashed == 1 and first.stubbed == 0
    assert _outputs(first.messages)[0].endswith(NOTICE)

    third = flash_gemini.apply_gemini(_turns(3), horizon=0, policy=POLICY)
    outputs = _outputs(third.messages)
    assert third.stubbed == 2 and third.flashed == 1
    assert all(o.startswith("[Horizon flash") for o in outputs[:2])
    assert outputs[2].endswith(NOTICE)
    # The field name and the model's parts (thought signatures) are untouched.
    resp = third.messages[2]["parts"][0]["functionResponse"]
    assert set(resp["response"]) == {"output"} and resp["name"] == "run_shell_command"
    assert third.messages[1] == _turns(3)[1]
    assert len(third.cleared) == 2


def test_stubs_are_stable_across_turns() -> None:
    two = flash_gemini.apply_gemini(_turns(2), horizon=0, policy=POLICY).messages
    three = flash_gemini.apply_gemini(_turns(3), horizon=0, policy=POLICY).messages
    assert three[: len(two) - 1] == two[:-1]


@pytest.mark.parametrize(
    ("kwargs", "why"),
    [
        ({"tool": "read_file"}, "not allow-listed"),
        ({"tool": "google_web_search"}, "web tools never"),
        ({"response": {"output": "short"}}, "below the size floor"),
        ({"response": {"stdout": LOG, "exit_code": 0}}, "not a single string field"),
    ],
)
def test_left_alone(kwargs, why) -> None:
    contents = _turns(2, **kwargs)
    result = flash_gemini.apply_gemini(contents, horizon=0, policy=POLICY)
    assert result.messages == contents and not result.stubbed and not result.flashed, why


def test_nothing_before_the_horizon_is_touched() -> None:
    contents = _turns(3)
    result = flash_gemini.apply_gemini(contents, horizon=5, policy=POLICY)
    assert result.messages[:5] == contents[:5]


def test_a_rerun_that_returns_the_same_output_pauses_flash() -> None:
    contents = _turns(2)
    # The model re-runs the first command and gets the same log back.
    contents += [
        {"role": "model", "parts": [{"functionCall": contents[1]["parts"][0]["functionCall"]}]},
        {"role": "user", "parts": [dict(contents[2]["parts"][0])]},
    ]
    result = flash_gemini.apply_gemini(contents, horizon=0, policy=POLICY)
    assert result.paused


# -- through the handler --------------------------------------------------------------


class _Stream:
    def __init__(self) -> None:
        self.status_code = 200
        self.headers = httpx.Headers({"content-type": "text/event-stream"})

    async def aiter_bytes(self):
        payload = {
            "candidates": [{"content": {"role": "model", "parts": [{"text": "ok"}]}}],
            "usageMetadata": {"promptTokenCount": 4000, "candidatesTokenCount": 2},
        }
        yield f"data: {json.dumps(payload)}\n\n".encode()

    async def aiter_raw(self):
        async for chunk in self.aiter_bytes():
            yield chunk

    async def aclose(self):
        return None


def _app(monkeypatch, tmp_path):
    from horizon.proxy import flash_guard
    from horizon.proxy.server import ProxyConfig, create_app

    flash_guard.reset_for_tests()
    monkeypatch.setenv("HORIZON_FLASH_GEMINI", "1")
    monkeypatch.setenv("HORIZON_WORKSPACE_DIR", str(tmp_path))
    return create_app(
        ProxyConfig(
            optimize=True,
            cache_enabled=False,
            rate_limit_enabled=False,
            cost_tracking_enabled=False,
            ccr_inject_tool=False,
            ccr_handle_responses=False,
            ccr_context_tracking=False,
            mode="cache",
        )
    )


def _client(proxy, *, status: int = 200) -> MagicMock:
    def respond(*_a, **_kw):
        payload = {
            "candidates": [{"content": {"role": "model", "parts": [{"text": "ok"}]}}],
            "usageMetadata": {"promptTokenCount": 4000, "candidatesTokenCount": 2},
        }
        return httpx.Response(
            status, json=payload, request=httpx.Request("POST", "https://g.example/x")
        )

    http = MagicMock()
    http.post = AsyncMock(side_effect=respond)
    http.request = AsyncMock(side_effect=respond)
    http.build_request = MagicMock(return_value=MagicMock(name="upstream_request"))
    http.send = AsyncMock(return_value=_Stream())
    http.aclose = AsyncMock()
    proxy.http_client = http
    return http


def _sent_contents(http: MagicMock) -> list[dict]:
    if http.build_request.call_args is not None:
        return json.loads(http.build_request.call_args.kwargs["content"])["contents"]
    return json.loads(http.post.call_args.kwargs["content"])["contents"]


@pytest.mark.parametrize(
    "path",
    [
        "/v1beta/models/gemini-3.1-pro:streamGenerateContent?alt=sse",
        "/v1beta/models/gemini-3.1-pro:generateContent",
    ],
)
def test_gemini_routes_flash_and_report_to_the_ledger(monkeypatch, tmp_path, path) -> None:
    from fastapi.testclient import TestClient

    seen: list[Any] = []

    async def capture(outcome, **_kw):
        seen.append(outcome)

    monkeypatch.setattr("horizon.proxy.account_analytics.record_account_outcome", capture)
    app = _app(monkeypatch, tmp_path)
    sent: list[list[dict]] = []
    with TestClient(app) as client:
        http = _client(client.app.state.proxy)
        for turns in (1, 2, 3):
            response = client.post(
                path, json={"contents": _turns(turns)}, headers={"x-goog-api-key": "test"}
            )
            assert response.status_code == 200
            sent.append(_sent_contents(http))
    assert _outputs(sent[0])[0].endswith(NOTICE)
    assert _outputs(sent[2])[0].startswith("[Horizon flash")
    assert any(t.startswith("flash_saved:") for t in seen[-1].transforms_applied)


def test_a_rejected_flash_is_retried_unflashed_and_switched_off(monkeypatch, tmp_path) -> None:
    from fastapi.testclient import TestClient

    from horizon.proxy import flash_guard

    app = _app(monkeypatch, tmp_path)
    with TestClient(app) as client:
        proxy = client.app.state.proxy
        bodies: list[dict] = []

        def respond(*_a, **kw):
            body = json.loads(kw["content"])
            bodies.append(body)
            flashed = "[Horizon flash" in json.dumps(body)
            payload = {"candidates": [{"content": {"role": "model", "parts": [{"text": "ok"}]}}]}
            return httpx.Response(
                400 if flashed else 200,
                json=payload if not flashed else {"error": {"message": "bad"}},
                request=httpx.Request("POST", "https://generativelanguage.googleapis.com/x"),
            )

        http = _client(proxy)
        http.post = AsyncMock(side_effect=respond)
        path = "/v1beta/models/gemini-3.1-pro:generateContent"
        client.post(path, json={"contents": _turns(1)}, headers={"x-goog-api-key": "t"})
        response = client.post(path, json={"contents": _turns(3)}, headers={"x-goog-api-key": "t"})
        assert response.status_code == 200
    assert "[Horizon flash" not in json.dumps(bodies[-1])
    assert flash_guard.is_disabled("generativelanguage.googleapis.com", "gemini-3.1-pro")
    flash_guard.reset_for_tests()


def test_off_unless_enabled(monkeypatch, tmp_path) -> None:
    from fastapi.testclient import TestClient

    app = _app(monkeypatch, tmp_path)
    monkeypatch.delenv("HORIZON_FLASH_GEMINI")
    with TestClient(app) as client:
        http = _client(client.app.state.proxy)
        client.post(
            "/v1beta/models/gemini-3.1-pro:streamGenerateContent?alt=sse",
            json={"contents": _turns(3)},
            headers={"x-goog-api-key": "test"},
        )
        assert _sent_contents(http) == _turns(3)
