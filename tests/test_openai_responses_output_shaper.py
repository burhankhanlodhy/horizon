from __future__ import annotations

import copy
import json
import logging
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from horizon.proxy import runtime_env  # noqa: E402
from horizon.proxy.loopback_guard import require_loopback  # noqa: E402
from horizon.proxy.server import ProxyConfig, create_app  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate_runtime_env_overrides():
    """Keep loopback hot-reload state from leaking into later test modules."""
    runtime_env.clear_overrides()
    yield
    runtime_env.clear_overrides()


def _make_client() -> TestClient:
    app = create_app(
        ProxyConfig(
            optimize=False,
            cache_enabled=False,
            rate_limit_enabled=False,
            cost_tracking_enabled=False,
            subscription_tracking_enabled=False,
            log_requests=False,
            http2=False,
        )
    )
    app.dependency_overrides[require_loopback] = lambda: None

    # Exercise real routing/rewrites/admin settings with a fake upstream. The
    # production lifespan starts unrelated ML, quota and account workers that
    # persist user state; those services belong in separate lifecycle tests.
    @asynccontextmanager
    async def route_test_lifespan(app):
        yield

    app.router.lifespan_context = route_test_lifespan
    return TestClient(app)


async def _ok_response(
    method: str,
    url: str,
    headers: dict[str, str],
    body: dict[str, Any],
    stream: bool = False,
    **kwargs: Any,
) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": "resp_1",
            "output": [],
            "usage": {"input_tokens": 10, "output_tokens": 1},
        },
    )


def test_http_responses_output_shaper_rewrites_and_labels(monkeypatch):
    monkeypatch.setenv("HORIZON_OUTPUT_SHAPER", "1")
    monkeypatch.setenv("HORIZON_ROLLOUT_CHANNEL", "beta")
    monkeypatch.setenv("HORIZON_VERBOSITY_LEVEL", "2")
    monkeypatch.delenv("HORIZON_OUTPUT_HOLDOUT", raising=False)
    captured: dict[str, Any] = {}
    outcomes: list[Any] = []

    payload = {
        "model": "gpt-5",
        "input": [
            {
                "type": "function_call_output",
                "call_id": "call_1",
                "output": "ok",
            }
        ],
        "reasoning": {"effort": "xhigh"},
        "text": {"verbosity": "medium"},
    }

    with _make_client() as client:
        proxy = client.app.state.proxy

        async def _fake_retry(*args: Any, **kwargs: Any) -> httpx.Response:
            body = args[3]
            captured["body"] = copy.deepcopy(body)
            captured["retry_kwargs"] = dict(kwargs)
            return await _ok_response(*args, **kwargs)

        async def _record_request_outcome(outcome: Any) -> None:
            outcomes.append(outcome)

        proxy._retry_request = _fake_retry
        proxy._record_request_outcome = _record_request_outcome

        response = client.post(
            "/v1/responses",
            headers={"authorization": "Bearer test-key"},
            json=payload,
        )

    assert response.status_code == 200
    sent = captured["body"]
    assert "instructions" not in sent
    # Explicit client verbosity and reasoning remain authoritative.
    assert sent["reasoning"]["effort"] == "xhigh"
    assert sent["text"]["verbosity"] == "medium", "client value passes through"
    assert captured["retry_kwargs"]["body_mutated"] is False
    assert captured["retry_kwargs"]["original_body_bytes"] is not None
    transforms = outcomes[-1].transforms_applied
    assert any(t.startswith("output_shaper:stratum:") for t in transforms)
    assert "output_shaper:verbosity:L2" not in transforms


def test_http_responses_output_shaper_respects_bypass(monkeypatch):
    monkeypatch.setenv("HORIZON_OUTPUT_SHAPER", "1")
    monkeypatch.setenv("HORIZON_ROLLOUT_CHANNEL", "beta")
    captured: dict[str, Any] = {}
    payload = {"model": "gpt-5", "input": "hi"}

    with _make_client() as client:
        proxy = client.app.state.proxy

        async def _fake_retry(*args: Any, **kwargs: Any) -> httpx.Response:
            captured["body"] = copy.deepcopy(args[3])
            return await _ok_response(*args, **kwargs)

        proxy._retry_request = _fake_retry

        response = client.post(
            "/v1/responses",
            headers={
                "authorization": "Bearer test-key",
                "x-horizon-bypass": "true",
            },
            json=payload,
        )

    assert response.status_code == 200
    assert captured["body"] == payload


def test_http_responses_output_shaper_holdout_labels_without_rewrite(monkeypatch):
    monkeypatch.setenv("HORIZON_OUTPUT_SHAPER", "1")
    monkeypatch.setenv("HORIZON_ROLLOUT_CHANNEL", "beta")
    monkeypatch.setenv("HORIZON_OUTPUT_HOLDOUT", "1")
    captured: dict[str, Any] = {}
    outcomes: list[Any] = []
    payload = {"model": "gpt-5", "input": "hi"}

    with _make_client() as client:
        proxy = client.app.state.proxy

        async def _fake_retry(*args: Any, **kwargs: Any) -> httpx.Response:
            captured["body"] = copy.deepcopy(args[3])
            return await _ok_response(*args, **kwargs)

        async def _record_request_outcome(outcome: Any) -> None:
            outcomes.append(outcome)

        proxy._retry_request = _fake_retry
        proxy._record_request_outcome = _record_request_outcome

        response = client.post(
            "/v1/responses",
            headers={"authorization": "Bearer test-key"},
            json=payload,
        )

    assert response.status_code == 200
    assert captured["body"] == payload
    transforms = outcomes[-1].transforms_applied
    assert any(t.startswith("output_shaper:control:") for t in transforms)
    assert "output_shaper:verbosity:L2" not in transforms


def test_http_output_shaper_hot_reload_changes_the_running_request_path(monkeypatch):
    """The admin endpoint must not report success while traffic stays unchanged."""
    monkeypatch.setenv("HORIZON_ROLLOUT_CHANNEL", "beta")
    monkeypatch.delenv("HORIZON_OUTPUT_SHAPER", raising=False)
    payload = {
        "model": "gpt-5",
        "input": [{"type": "function_call_output", "call_id": "call_1", "output": "ok"}],
        "reasoning": {"effort": "high"},
    }
    sent: list[dict[str, Any]] = []

    with _make_client() as client:
        proxy = client.app.state.proxy

        async def _fake_retry(*args: Any, **kwargs: Any) -> httpx.Response:
            sent.append(copy.deepcopy(args[3]))
            return await _ok_response(*args, **kwargs)

        proxy._retry_request = _fake_retry
        first = client.post(
            "/v1/responses", headers={"authorization": "Bearer test-key"}, json=payload
        )
        update = client.post("/admin/runtime-env", json={"HORIZON_OUTPUT_SHAPER": "1"})
        second = client.post(
            "/v1/responses", headers={"authorization": "Bearer test-key"}, json=payload
        )

    assert first.status_code == second.status_code == update.status_code == 200
    assert sent[0] == payload
    assert sent[1]["text"]["verbosity"] == "low"
    decision = next(
        item
        for item in update.json()["rollout"]["features"]
        if item["name"] == "proxy_output_shaper"
    )
    assert decision["enabled"] is True
    assert decision["decision"] == "legacy_alias"


def _validation_payload(dialect, output):
    if dialect == "responses":
        return "/v1/responses", {
            "model": "gpt-5",
            "input": [
                {
                    "type": "function_call",
                    "call_id": "x",
                    "name": "shell",
                    "arguments": json.dumps({"command": "python -m unittest discover"}),
                },
                {"type": "function_call_output", "call_id": "x", "output": output},
            ],
        }
    return "/v1/messages", {
        "model": "claude-sonnet-4-5",
        "system": "Original system",
        "max_tokens": 100,
        "messages": [
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "x",
                        "name": "Bash",
                        "input": {"command": "python -m unittest discover"},
                    }
                ],
            },
            # Pre-place the native cache marker so rollback assertions isolate
            # shaping from the handler's independent cache normalization.
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "x",
                        "content": output,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
            },
        ],
    }


def _capture_upstream(proxy, captured):
    async def retry(*args, **kwargs):
        captured["body"] = copy.deepcopy(args[3])
        captured["kwargs"] = dict(kwargs)
        if "messages" in args[1]:
            return httpx.Response(
                200,
                json={
                    "id": "msg_1",
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "text", "text": "ok"}],
                    "usage": {"input_tokens": 10, "output_tokens": 1},
                },
            )
        return await _ok_response(*args, **kwargs)

    proxy._retry_request = retry


def test_http_responses_forwards_schema_valid_validation_blocks(monkeypatch):
    from horizon.proxy.validation_feedback import MARKER

    monkeypatch.setenv("HORIZON_OUTPUT_SHAPER", "1")
    monkeypatch.setenv("HORIZON_ROLLOUT_CHANNEL", "beta")
    monkeypatch.setenv("HORIZON_OUTPUT_HOLDOUT", "0")
    monkeypatch.setenv("HORIZON_VALIDATION_FEEDBACK", "1")
    blocks = [{"type": "input_text", "text": "Ran 0 tests in 0.00s\nOK"}]
    path, payload = _validation_payload("responses", blocks)
    captured = {}
    with _make_client() as client:
        _capture_upstream(client.app.state.proxy, captured)
        response = client.post(path, headers={"authorization": "Bearer test-key"}, json=payload)
    assert response.status_code == 200
    output = captured["body"]["input"][-1]["output"]
    assert output[:-1] == blocks
    assert output[-1]["type"] == "input_text"
    assert MARKER in output[-1]["text"]
    assert captured["kwargs"]["body_mutated"] is True


@pytest.mark.parametrize("dialect", ["responses", "anthropic"])
def test_http_transform_failure_forwards_original_request(monkeypatch, dialect):
    from horizon.proxy import output_shaper

    monkeypatch.setenv("HORIZON_OUTPUT_SHAPER", "1")
    monkeypatch.setenv("HORIZON_ROLLOUT_CHANNEL", "beta")
    monkeypatch.setenv("HORIZON_OUTPUT_HOLDOUT", "0")
    path, payload = _validation_payload(dialect, "Ran 0 tests in 0.00s\nOK")
    captured = {}

    def fail(body, result):
        sequence = body.get("input", body.get("messages"))
        sequence[-1]["corrupted"] = True
        raise RuntimeError("injected failure after mutation")

    monkeypatch.setattr(output_shaper, "_validation_feedback", fail)
    raw = json.dumps(payload, indent=2).encode()
    with _make_client() as client:
        _capture_upstream(client.app.state.proxy, captured)
        response = client.post(
            path,
            headers={"authorization": "Bearer test-key", "content-type": "application/json"},
            content=raw,
        )
    assert response.status_code == 200, response.text
    assert captured["body"] == payload
    assert captured["kwargs"]["body_mutated"] is False
    assert captured["kwargs"]["original_body_bytes"] == raw


@pytest.mark.parametrize("dialect", ["responses", "anthropic"])
def test_http_deep_tool_result_does_not_return_proxy_error(monkeypatch, dialect):
    monkeypatch.setenv("HORIZON_OUTPUT_SHAPER", "1")
    monkeypatch.setenv("HORIZON_ROLLOUT_CHANNEL", "beta")
    monkeypatch.setenv("HORIZON_OUTPUT_HOLDOUT", "0")
    deep = "[" * 1100 + "0" + "]" * 1100
    path, payload = _validation_payload(dialect, deep)
    captured = {}
    with _make_client() as client:
        _capture_upstream(client.app.state.proxy, captured)
        response = client.post(path, headers={"authorization": "Bearer test-key"}, json=payload)
    assert response.status_code == 200, response.text
    assert deep in json.dumps(captured["body"])


def test_ingress_logger_redacts_proxy_credentials(monkeypatch, caplog):
    token = "private-proxy-token-regression"
    monkeypatch.setenv("HORIZON_PROXY_TOKEN", token)
    with _make_client() as client, caplog.at_level(logging.INFO, logger="horizon.proxy"):
        # Proxy construction installs file logging and disables propagation.
        monkeypatch.setattr(logging.getLogger("horizon"), "propagate", True)
        response = client.get(
            "/health",
            headers={
                "X-Horizon-Proxy-Token": token,
                "X-Horizon-License_Token": "private-license-token-regression",
                "X-Horizon-Tool-Search-Deferred-Tokens": "42",
            },
        )
    assert response.status_code == 200
    logs = "\n".join(
        record.getMessage()
        for record in caplog.records
        if "event=proxy_inbound_request" in record.getMessage()
    )
    assert logs
    assert token not in logs and "private-license-token-regression" not in logs
    assert '"x-horizon-proxy-token": "[REDACTED]"' in logs
    assert '"x-horizon-tool-search-deferred-tokens": "42"' in logs
