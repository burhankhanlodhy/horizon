"""Usage counting for served requests on the licensed engine, streamed and not.

Claude Code sends POST /v1/messages with ``stream: true`` and ``?beta=true``; Codex
sends POST /v1/responses with ``stream: true``. Each served request must add exactly
one request to the hourly bucket, and the bucket must reach the API on flush.
"""

from __future__ import annotations

import json
import logging
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from horizon.code_engine.license import write_license
from tests import test_code_engine_gate as gate
from tests.test_code_engine_gate import (
    PROMPT_MARKER,
    _ApiServer,
    _app,
    _body,
    _engine,
    _note,
    _Stream,
    _write_device,
)

# The gate tests' fixtures, shared here by assignment (same fixture objects).
home = gate.home
signer = gate.signer

CLAUDE_CODE_HEADERS = {
    "x-api-key": "test-key",
    "anthropic-version": "2023-06-01",
    "anthropic-beta": "claude-code-20250219,interleaved-thinking-2025-05-14",
    "content-type": "application/json",
    "user-agent": "claude-cli/2.0.60 (external, cli)",
    "x-app": "cli",
}
CODEX_HEADERS = {
    "authorization": "Bearer test-token",
    "content-type": "application/json",
    "user-agent": "codex-cli/0.50.0 (Windows 10.0.26100; x86_64)",
}


def _sse(events: list[tuple[str, dict]]) -> bytes:
    return "".join(f"event: {name}\ndata: {json.dumps(data)}\n\n" for name, data in events).encode()


def _anthropic_sse(input_tokens: int, output_tokens: int) -> bytes:
    return _sse(
        [
            (
                "message_start",
                {
                    "type": "message_start",
                    "message": {
                        "id": "msg_1",
                        "type": "message",
                        "role": "assistant",
                        "model": "claude-sonnet-4-6",
                        "content": [],
                        "stop_reason": None,
                        "stop_sequence": None,
                        "usage": {
                            "input_tokens": input_tokens,
                            "cache_creation_input_tokens": 0,
                            "cache_read_input_tokens": 0,
                            "output_tokens": 1,
                        },
                    },
                },
            ),
            (
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {"type": "text", "text": ""},
                },
            ),
            (
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": "ok"},
                },
            ),
            ("content_block_stop", {"type": "content_block_stop", "index": 0}),
            (
                "message_delta",
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                    "usage": {"output_tokens": output_tokens},
                },
            ),
            ("message_stop", {"type": "message_stop"}),
        ]
    )


def _anthropic_rich_sse(input_tokens: int, output_tokens: int) -> bytes:
    """A turn as Claude Code gets it: a thinking block, then a tool_use block."""
    return _sse(
        [
            (
                "message_start",
                {
                    "type": "message_start",
                    "message": {
                        "id": "msg_2",
                        "type": "message",
                        "role": "assistant",
                        "model": "claude-sonnet-4-6",
                        "content": [],
                        "stop_reason": None,
                        "stop_sequence": None,
                        "usage": {
                            "input_tokens": input_tokens,
                            "cache_creation_input_tokens": 0,
                            "cache_read_input_tokens": 600,
                            "output_tokens": 1,
                        },
                    },
                },
            ),
            (
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {"type": "thinking", "thinking": "", "signature": ""},
                },
            ),
            (
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "thinking_delta", "thinking": "list the dir"},
                },
            ),
            (
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "signature_delta", "signature": "sig-abc"},
                },
            ),
            ("content_block_stop", {"type": "content_block_stop", "index": 0}),
            (
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": 1,
                    "content_block": {
                        "type": "tool_use",
                        "id": "toolu_2",
                        "name": "Bash",
                        "input": {},
                    },
                },
            ),
            (
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": 1,
                    "delta": {"type": "input_json_delta", "partial_json": '{"command": "ls"}'},
                },
            ),
            ("content_block_stop", {"type": "content_block_stop", "index": 1}),
            (
                "message_delta",
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": "tool_use", "stop_sequence": None},
                    "usage": {"output_tokens": output_tokens},
                },
            ),
            ("message_stop", {"type": "message_stop"}),
        ]
    )


def _responses_sse(input_tokens: int, output_tokens: int) -> bytes:
    completed = {
        "id": "resp_1",
        "object": "response",
        "model": "gpt-5.5",
        "status": "completed",
        "output": [
            {
                "type": "message",
                "id": "msg_1",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "ok"}],
            }
        ],
        "usage": {
            "input_tokens": input_tokens,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens": output_tokens,
            "output_tokens_details": {"reasoning_tokens": 0},
            "total_tokens": input_tokens + output_tokens,
        },
    }
    return _sse(
        [
            ("response.created", {"type": "response.created", "response": {"id": "resp_1"}}),
            (
                "response.output_text.delta",
                {"type": "response.output_text.delta", "delta": "ok"},
            ),
            ("response.completed", {"type": "response.completed", "response": completed}),
        ]
    )


class _FakeProvider:
    """Answers Anthropic Messages and OpenAI Responses like the real APIs, streamed or not."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, bool]] = []
        self.body: bytes | None = None

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        raw = await request.aread()
        self.body = raw
        body = json.loads(raw)
        streamed = bool(body.get("stream"))
        self.calls.append((request.url.path, streamed))
        if request.url.path == "/v1/messages":
            if streamed:
                sse = (
                    _anthropic_rich_sse(input_tokens=900, output_tokens=40)
                    if "tools" in body
                    else _anthropic_sse(input_tokens=900, output_tokens=2)
                )
                return httpx.Response(
                    200,
                    headers={"content-type": "text/event-stream", "request-id": "req_1"},
                    stream=_Stream(sse),
                )
            payload = {
                "id": "msg_1",
                "type": "message",
                "role": "assistant",
                "model": "claude-sonnet-4-6",
                "content": [{"type": "text", "text": "ok"}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 900, "output_tokens": 2},
            }
            return httpx.Response(
                200,
                headers={"content-type": "application/json"},
                stream=_Stream(json.dumps(payload).encode()),
            )
        if request.url.path == "/v1/responses":
            if streamed:
                return httpx.Response(
                    200,
                    headers={"content-type": "text/event-stream"},
                    stream=_Stream(_responses_sse(input_tokens=900, output_tokens=2)),
                )
            payload = {
                "id": "resp_1",
                "object": "response",
                "status": "completed",
                "model": "gpt-5.5",
                "output": [],
                "usage": {
                    "input_tokens": 900,
                    "output_tokens": 2,
                    "total_tokens": 902,
                },
            }
            return httpx.Response(
                200,
                headers={"content-type": "application/json"},
                stream=_Stream(json.dumps(payload).encode()),
            )
        return httpx.Response(404)


def _production_app(engine, provider):
    """The engine's app as ``horizon code-engine run`` builds it: CCR, cache and limits on."""
    from horizon.proxy import engine_hooks
    from horizon.proxy.server import ProxyConfig, create_app

    engine_hooks.install(
        license_allows=engine.allows_compression,
        usage_sink=engine.record_usage,
        health=engine.health,
    )
    app = create_app(ProxyConfig(log_requests=False, subscription_tracking_enabled=False))
    app.state.proxy.http_client = httpx.AsyncClient(transport=httpx.MockTransport(provider))
    return app


def _licensed_engine(home, signer):
    _write_device(home)
    write_license(home, _note(signer), fetched_at=time.time())
    api = _ApiServer(signer)
    engine = _engine(home, api)
    engine.load_cached_note()
    return engine, api


def _upload_engine(home, handler):
    """An engine with one recorded request, whose /code/usage posts go to ``handler``."""
    _write_device(home)
    engine = _engine(home, handler)
    engine.usage.record("claude", 100, 60)
    return engine


def test_successful_upload_is_logged_with_counts_only(home, caplog) -> None:
    engine = _upload_engine(home, _ApiServer(None))

    with caplog.at_level(logging.INFO, logger="horizon.code_engine"):
        assert engine.flush_usage()

    assert "usage upload: 1 buckets -> 200" in caplog.messages
    assert engine.usage.pending_count() == 0


def test_refused_upload_logs_a_warning_with_the_status_only(home, caplog) -> None:
    engine = _upload_engine(home, lambda request: httpx.Response(503))

    with caplog.at_level(logging.INFO, logger="horizon.code_engine"):
        assert not engine.flush_usage()

    assert "usage upload: 1 buckets -> 503" in caplog.messages
    assert "usage upload refused (status 503); 1 buckets stay pending" in caplog.messages
    assert engine.usage.pending_count() == 1


def test_unreachable_upload_logs_the_exception_type_only(home, caplog) -> None:
    def unreachable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    engine = _upload_engine(home, unreachable)

    with caplog.at_level(logging.WARNING, logger="horizon.code_engine"):
        assert not engine.flush_usage()

    assert "usage upload failed (ConnectError); 1 buckets stay pending" in caplog.messages
    assert engine.usage.pending_count() == 1


def _with_stream(raw: bytes, stream: bool) -> bytes:
    body = json.loads(raw)
    body["stream"] = stream
    return json.dumps(body).encode()


def _claude_code_body(stream: bool) -> bytes:
    """A Claude Code turn: system with cache_control, tools, thinking, a prior tool round."""
    rows = [
        {"id": i, "name": f"item-{i}", "status": "ok", "path": f"/src/mod_{i % 7}.py"}
        for i in range(400)
    ]
    body = {
        "model": "claude-sonnet-4-6",
        "max_tokens": 32000,
        "stream": stream,
        "thinking": {"type": "enabled", "budget_tokens": 16000},
        "metadata": {"user_id": "user_session_123"},
        "system": [
            {
                "type": "text",
                "text": "You are Claude Code, an agent for software engineering. " * 40,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        "tools": [
            {
                "name": "Bash",
                "description": "Run a shell command",
                "input_schema": {
                    "type": "object",
                    "properties": {"command": {"type": "string"}},
                    "required": ["command"],
                },
            }
        ],
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": f"list the items, {PROMPT_MARKER}",
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
            },
            {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "run ls", "signature": "sig-prev"},
                    {
                        "type": "tool_use",
                        "id": "toolu_1",
                        "name": "Bash",
                        "input": {"command": "ls"},
                    },
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_1",
                        "content": json.dumps(rows),
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
            },
        ],
    }
    return json.dumps(body).encode()


CLAUDE_CODE_OAUTH_HEADERS = {
    "authorization": "Bearer sk-ant-oat01-test-token",
    "anthropic-version": "2023-06-01",
    "anthropic-beta": "claude-code-20250219,oauth-2025-04-20,interleaved-thinking-2025-05-14",
    "content-type": "application/json",
    "user-agent": "claude-cli/2.0.60 (external, cli)",
    "x-app": "cli",
}


@pytest.mark.parametrize(
    "headers", [CLAUDE_CODE_HEADERS, CLAUDE_CODE_OAUTH_HEADERS], ids=["api-key", "oauth"]
)
def test_claude_code_streamed_turn_with_thinking_and_tools_is_counted(
    home, signer, headers
) -> None:
    engine, api = _licensed_engine(home, signer)
    provider = _FakeProvider()

    response = TestClient(_production_app(engine, provider)).post(
        "/v1/messages?beta=true", content=_claude_code_body(stream=True), headers=headers
    )

    assert response.status_code == 200
    assert b"tool_use" in response.content
    assert engine.usage.pending_count() == 1
    assert engine.flush_usage()
    buckets = _uploaded_buckets(api)
    assert len(buckets) == 1
    bucket = buckets[0]
    assert bucket["tool"] == "claude"
    assert bucket["requests"] == 1
    assert bucket["tokens_before"] >= bucket["tokens_after"] > 0


def _uploaded_buckets(api: _ApiServer) -> list[dict]:
    uploads = [r for r in api.requests if r.url.path == "/code/usage"]
    assert uploads, "no usage upload reached the API"
    return json.loads(uploads[-1].content)["buckets"]


@pytest.mark.parametrize("stream", [True, False])
def test_claude_code_messages_request_is_counted_once(home, signer, stream) -> None:
    engine, api = _licensed_engine(home, signer)
    provider = _FakeProvider()
    raw = _with_stream(_body(), stream)

    response = TestClient(_app(engine, provider)).post(
        "/v1/messages?beta=true", content=raw, headers=CLAUDE_CODE_HEADERS
    )

    assert response.status_code == 200
    assert provider.calls == [("/v1/messages", stream)]
    assert engine.usage.pending_count() == 1
    assert engine.flush_usage()
    buckets = _uploaded_buckets(api)
    assert len(buckets) == 1
    bucket = buckets[0]
    assert bucket["tool"] == "claude"
    assert bucket["requests"] == 1
    assert bucket["tokens_before"] >= bucket["tokens_after"] > 0


@pytest.mark.parametrize(
    ("path", "user_agent"),
    [
        ("/v1/responses", "codex-cli/0.50.0"),
        ("/v1/responses", "codex_cli_rs/0.50.0 (Windows 10.0.26100)"),
        # ChatGPT sign-in: the Codex CLI's own Rust user agent on the backend Codex route.
        ("/backend-api/codex/responses", "codex_cli_rs/0.50.0 (Windows 10.0.26100)"),
    ],
)
def test_codex_responses_stream_is_counted_once(home, signer, path, user_agent) -> None:
    engine, api = _licensed_engine(home, signer)
    provider = _FakeProvider()
    raw = _with_stream(_body("gpt-5.5"), True)

    response = TestClient(_production_app(engine, provider)).post(
        path, content=raw, headers=dict(CODEX_HEADERS, **{"user-agent": user_agent})
    )

    assert response.status_code == 200
    assert provider.calls == [("/v1/responses", True)]
    assert engine.flush_usage()
    buckets = _uploaded_buckets(api)
    assert len(buckets) == 1
    bucket = buckets[0]
    assert bucket["tool"] == "codex"
    assert bucket["requests"] == 1
    assert bucket["tokens_before"] >= bucket["tokens_after"] > 0
