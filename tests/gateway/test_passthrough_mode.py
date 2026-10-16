"""Pass-through mode (``ProxyConfig.passthrough`` / ``horizon proxy --passthrough``).

The Claude Desktop 3P gateway runs the proxy as a byte-faithful relay. These
tests pin the W1 contract:

* a Desktop-like ``/v1/messages`` request (streaming and not) reaches the
  upstream byte-identical, with its headers unchanged;
* the upstream's ``usage`` reaches the client unchanged, even when the context
  guard would otherwise rewrite it;
* ``/v1/models`` and ``count_tokens`` are forwarded;
* a body a transform still mutates is replaced by the client's original bytes
  (the violation safety net), and the one allowed sanitize is logged;
* every transform in the spec is gated: the response cache, rate limiter,
  hooks, keep-alive, TTL guard, sticky beta, tool deferral, CCR, memory and the
  rest are off;
* without ``passthrough`` the same requests are changed, so the byte-identity
  assertions are not vacuous (the ``test_control_*`` cases).

The upstream is an ``httpx.AsyncBaseTransport`` that records the exact bytes it
receives, the same way ``tests/test_proxy_byte_faithful_forwarding.py`` does.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable, Iterator
from typing import Any

import httpx
import pytest

pytest.importorskip("fastapi")

from click.testing import CliRunner  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from horizon.hooks import CompressionHooks  # noqa: E402
from horizon.proxy.body_forwarding import (  # noqa: E402
    select_outbound_body,
    serialize_body_canonical,
)
from horizon.proxy.compression_decision import CompressionDecision  # noqa: E402
from horizon.proxy.image_compression_decision import ImageCompressionDecision  # noqa: E402
from horizon.proxy.memory_decision import MemoryDecision  # noqa: E402
from horizon.proxy.passthrough_mode import (  # noqa: E402
    PASSTHROUGH_STARTUP_LINE,
    is_passthrough,
    passthrough_env_enabled,
    passthrough_violations,
)
from horizon.proxy.server import ProxyConfig, create_app  # noqa: E402

CLAUDE = "claude-sonnet-4-5"
UPSTREAM_URL = "https://api.anthropic.com/v1/messages"
CLIENT_HEADERS = {
    "x-api-key": "sk-ant-test-desktop-key",
    "anthropic-version": "2023-06-01",
    "anthropic-beta": "prompt-caching-2024-07-31,extended-cache-ttl-2025-04-11",
    "user-agent": "claude-desktop/0.5.3 (external, sdk-cli)",
    "x-claude-code-session-id": "11111111-2222-3333-4444-555555555555",
    "content-type": "application/json",
}


@pytest.fixture
def horizon_logs() -> Iterator[list[logging.LogRecord]]:
    """Every record the ``horizon`` loggers emit during the test.

    ``horizon`` is configured with ``propagate=False``, so pytest's ``caplog``
    on the root logger sees nothing. Attach a capture handler to each logger.
    """
    records: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            # A child logger's record also reaches its parents' handlers.
            if all(seen is not record for seen in records):
                records.append(record)

    handler = _Capture(level=logging.DEBUG)
    names = [n for n in list(logging.Logger.manager.loggerDict) if n.startswith("horizon")]
    loggers = [logging.getLogger(n) for n in ["horizon", *names]]
    saved = [(lg, lg.level) for lg in loggers]
    for lg in loggers:
        lg.addHandler(handler)
        lg.setLevel(logging.INFO)
    try:
        yield records
    finally:
        for lg, level in saved:
            lg.removeHandler(handler)
            lg.setLevel(level)


# --------------------------------------------------------------------------- #
# Upstream double                                                              #
# --------------------------------------------------------------------------- #


class _Upstream(httpx.AsyncBaseTransport):
    """Records every request's exact bytes and answers with a scripted reply."""

    def __init__(self, reply: Callable[[dict[str, Any]], httpx.Response] | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._reply = reply

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        raw = b""
        async for chunk in request.stream:
            raw += chunk
        call = {
            "method": request.method,
            "url": str(request.url),
            "path": request.url.path,
            "headers": {k.lower(): v for k, v in request.headers.items()},
            "body": raw,
        }
        self.calls.append(call)
        if self._reply is not None:
            return self._reply(call)
        return _default_reply(call)

    @property
    def messages_calls(self) -> list[dict[str, Any]]:
        return [c for c in self.calls if c["path"] == "/v1/messages"]


def _json_reply(payload: dict[str, Any], status: int = 200) -> httpx.Response:
    return httpx.Response(
        status,
        headers={"content-type": "application/json"},
        content=json.dumps(payload, separators=(", ", ": ")).encode(),
    )


def _sse_reply(events: list[tuple[str, dict[str, Any]]]) -> httpx.Response:
    data = b"".join(
        f"event: {name}\ndata: {json.dumps(obj, separators=(', ', ': '))}\n\n".encode()
        for name, obj in events
    )
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=data)


def _message(usage: dict[str, Any], text: str = "ok") -> dict[str, Any]:
    return {
        "id": "msg_desktop_1",
        "type": "message",
        "role": "assistant",
        "model": CLAUDE,
        "content": [{"type": "text", "text": text}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": usage,
    }


def _usage(input_tokens: int = 1200, output_tokens: int = 40) -> dict[str, Any]:
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_input_tokens": 900,
        "cache_creation_input_tokens": 100,
        "cache_creation": {"ephemeral_5m_input_tokens": 50, "ephemeral_1h_input_tokens": 50},
    }


def _default_reply(call: dict[str, Any]) -> httpx.Response:
    if call["path"] == "/v1/models":
        return _json_reply({"data": [{"id": CLAUDE, "type": "model"}], "has_more": False})
    if call["path"] == "/v1/messages/count_tokens":
        return _json_reply({"input_tokens": 321})
    body = json.loads(call["body"] or b"{}")
    if body.get("stream"):
        return _sse_reply(
            [
                (
                    "message_start",
                    {"type": "message_start", "message": _message(_usage(), text="")},
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
                        "usage": {"output_tokens": 40},
                    },
                ),
                ("message_stop", {"type": "message_stop"}),
            ]
        )
    return _json_reply(_message(_usage()))


def _app(upstream: _Upstream, **overrides: Any) -> TestClient:
    """A proxy app whose upstream is ``upstream``, with the cache, limiter and
    cost tracking off unless a test turns one back on."""
    kwargs: dict[str, Any] = {
        "passthrough": True,
        "cache_enabled": False,
        "rate_limit_enabled": False,
        "cost_tracking_enabled": False,
        "log_requests": False,
    }
    kwargs.update(overrides)
    app = create_app(ProxyConfig(**kwargs))
    app.state.proxy.http_client = httpx.AsyncClient(transport=upstream)
    # No lifespan: the injected client is the one the handlers use.
    return TestClient(app)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# --------------------------------------------------------------------------- #
# Desktop-like request                                                         #
# --------------------------------------------------------------------------- #


def _desktop_request_bytes(*, stream: bool, model: str = CLAUDE) -> bytes:
    """A Claude Desktop-shaped body, serialized with spaces after separators.

    The spacing matters: a canonical re-serialization (compact separators) would
    produce different bytes, so byte identity here proves nothing was rewritten.
    """
    # Unsorted tool names, schema annotations, and 15 tools: enough to reach the
    # tool sort, schema compaction and the first-party tool-search deferral.
    order = (5, 3, 9, 1, 7, 2, 8, 4, 6, 0, 13, 11, 12, 10, 14)
    tools = [
        {
            "name": f"desktop_tool_{i:02d}",
            "description": f"Desktop tool number {i}. Performs one narrow task.",
            "input_schema": {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "title": f"Args{i}",
                "properties": {"query": {"type": "string", "examples": ["x"]}},
                "required": ["query"],
            },
        }
        for i in order
    ]
    tool_output = "\n".join(
        f"src/module_{n:03d}.py:{n}: def handler_{n}(): pass" for n in range(400)
    )
    body: dict[str, Any] = {
        "model": model,
        "max_tokens": 2048,
        "stream": stream,
        "system": [
            {
                "type": "text",
                "text": "You are Claude Code. Be concise. Café ünïcode 🔥.",
                "cache_control": {"type": "ephemeral", "ttl": "1h"},
            }
        ],
        "tools": tools,
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": "Hi, list the modules 🔥"}]},
            {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "Plan the listing.", "signature": "sig-abc=="},
                    {"type": "text", "text": "Listing them now."},
                    {
                        "type": "tool_use",
                        "id": "toolu_desktop_1",
                        "name": "desktop_tool_00",
                        "input": {"query": "modules"},
                    },
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_desktop_1",
                        "content": tool_output,
                    },
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==",
                        },
                    },
                    {
                        "type": "text",
                        "text": "And the tests too.",
                        "cache_control": {"type": "ephemeral"},
                    },
                ],
            },
        ],
    }
    return json.dumps(body, separators=(", ", ": "), ensure_ascii=False).encode("utf-8")


def _post(client: TestClient, raw: bytes, headers: dict[str, str] | None = None) -> Any:
    return client.post("/v1/messages", headers={**CLIENT_HEADERS, **(headers or {})}, content=raw)


# --------------------------------------------------------------------------- #
# Byte identity and headers                                                    #
# --------------------------------------------------------------------------- #


def test_streaming_desktop_request_reaches_upstream_byte_identical() -> None:
    upstream = _Upstream()
    raw = _desktop_request_bytes(stream=True)
    client = _app(upstream)

    with client.stream("POST", "/v1/messages", headers=CLIENT_HEADERS, content=raw) as resp:
        assert resp.status_code == 200
        for _ in resp.iter_bytes():
            pass

    (call,) = upstream.messages_calls
    assert _sha(call["body"]) == _sha(raw)
    assert call["body"] == raw


def test_non_streaming_desktop_request_reaches_upstream_byte_identical() -> None:
    upstream = _Upstream()
    raw = _desktop_request_bytes(stream=False)
    resp = _post(_app(upstream), raw)

    assert resp.status_code == 200
    (call,) = upstream.messages_calls
    assert call["body"] == raw


def test_client_headers_reach_upstream_unchanged() -> None:
    upstream = _Upstream()
    raw = _desktop_request_bytes(stream=False)
    _post(_app(upstream), raw)

    (call,) = upstream.messages_calls
    for name in (
        "x-api-key",
        "anthropic-version",
        "anthropic-beta",
        "user-agent",
        "x-claude-code-session-id",
    ):
        assert call["headers"][name] == CLIENT_HEADERS[name], name
    assert not [k for k in call["headers"] if k.startswith("x-horizon-")]


def test_client_accept_header_is_not_rewritten_for_non_streaming() -> None:
    """Default mode rewrites Accept to application/json on a buffered turn;
    pass-through keeps the client's header."""
    upstream = _Upstream()
    raw = _desktop_request_bytes(stream=False)
    _post(_app(upstream), raw, headers={"accept": "text/event-stream"})

    (call,) = upstream.messages_calls
    assert call["headers"]["accept"] == "text/event-stream"


def test_anthropic_beta_is_not_merged_across_turns() -> None:
    """Default mode merges a session's beta tokens into later turns (sticky).
    Pass-through sends exactly what this turn's client sent."""
    upstream = _Upstream()
    client = _app(upstream)
    first = _desktop_request_bytes(stream=False)
    _post(client, first, headers={"anthropic-beta": "alpha-beta-2025-01-01"})
    second = _desktop_request_bytes(stream=False).replace(
        b"Hi, list the modules", b"Second turn, list them again"
    )
    _post(client, second, headers={"anthropic-beta": "bravo-beta-2025-02-02"})

    turn_one, turn_two = upstream.messages_calls
    assert turn_one["headers"]["anthropic-beta"] == "alpha-beta-2025-01-01"
    assert turn_two["headers"]["anthropic-beta"] == "bravo-beta-2025-02-02"


# --------------------------------------------------------------------------- #
# Usage                                                                        #
# --------------------------------------------------------------------------- #


def _near_limit_stream_reply(call: dict[str, Any]) -> httpx.Response:
    """A streamed reply whose message_start usage is near the context window."""
    return _sse_reply(
        [
            (
                "message_start",
                {
                    "type": "message_start",
                    "message": _message(_usage(input_tokens=185_000), text=""),
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
                    "usage": {"output_tokens": 40},
                },
            ),
            ("message_stop", {"type": "message_stop"}),
        ]
    )


def _stream_bytes(client: TestClient, raw: bytes) -> bytes:
    with client.stream("POST", "/v1/messages", headers=CLIENT_HEADERS, content=raw) as resp:
        return b"".join(resp.iter_bytes())


def test_streamed_usage_reaches_client_unchanged_with_context_guard_triggered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HORIZON_CONTEXT_GUARD", "1")
    upstream = _Upstream(reply=_near_limit_stream_reply)
    raw = _desktop_request_bytes(stream=True)

    client_bytes = _stream_bytes(_app(upstream), raw)

    assert b'"input_tokens":185000' in client_bytes.replace(b": ", b":")
    assert client_bytes == _near_limit_stream_reply(upstream.calls[0]).content


def test_non_streamed_usage_reaches_client_unchanged_with_context_guard_triggered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HORIZON_CONTEXT_GUARD", "1")
    reply_bytes = json.dumps(
        _message(_usage(input_tokens=185_000)), separators=(", ", ": ")
    ).encode()

    def reply(_call: dict[str, Any]) -> httpx.Response:
        return httpx.Response(
            200, headers={"content-type": "application/json"}, content=reply_bytes
        )

    upstream = _Upstream(reply=reply)
    resp = _post(_app(upstream), _desktop_request_bytes(stream=False))

    assert resp.status_code == 200
    assert resp.content == reply_bytes
    assert resp.json()["usage"]["input_tokens"] == 185_000


def test_control_context_guard_rewrites_usage_without_passthrough(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Proves the usage test above is meaningful: with pass-through off the
    guard does rewrite the near-limit usage the client sees."""
    monkeypatch.setenv("HORIZON_CONTEXT_GUARD", "1")
    upstream = _Upstream(reply=_near_limit_stream_reply)
    raw = _desktop_request_bytes(stream=True)

    client_bytes = _stream_bytes(_app(upstream, passthrough=False, optimize=True), raw)

    assert client_bytes != _near_limit_stream_reply(upstream.calls[0]).content


# --------------------------------------------------------------------------- #
# Other endpoints                                                              #
# --------------------------------------------------------------------------- #


def test_models_and_count_tokens_are_forwarded() -> None:
    upstream = _Upstream()
    client = _app(upstream)

    models = client.get("/v1/models", headers=CLIENT_HEADERS)
    count_raw = json.dumps(
        {"model": CLAUDE, "messages": [{"role": "user", "content": "hi 🔥"}]},
        separators=(", ", ": "),
        ensure_ascii=False,
    ).encode()
    counted = client.post(
        "/v1/messages/count_tokens",
        headers=CLIENT_HEADERS,
        content=count_raw,
    )

    assert models.status_code == 200
    assert models.json()["data"][0]["id"] == CLAUDE
    assert counted.status_code == 200
    assert counted.json() == {"input_tokens": 321}

    by_path = {c["path"]: c for c in upstream.calls}
    assert by_path["/v1/models"]["method"] == "GET"
    assert by_path["/v1/messages/count_tokens"]["body"] == count_raw
    assert (
        by_path["/v1/messages/count_tokens"]["headers"]["x-api-key"] == CLIENT_HEADERS["x-api-key"]
    )


# --------------------------------------------------------------------------- #
# Violation safety net and the sanitize exception                              #
# --------------------------------------------------------------------------- #


def test_forced_mutation_forwards_the_clients_original_bytes_and_logs_violation(
    horizon_logs: list[logging.LogRecord],
) -> None:
    """A role=system message makes the wire-contract guard rewrite the body.
    Pass-through must not forward that rewrite: the client's bytes go out and
    the violation is logged once."""
    upstream = _Upstream()
    raw = json.dumps(
        {
            "model": CLAUDE,
            "max_tokens": 64,
            "messages": [
                {"role": "user", "content": "hello 🔥"},
                {"role": "system", "content": "relocate me"},
                {"role": "user", "content": "again"},
            ],
        },
        separators=(", ", ": "),
        ensure_ascii=False,
    ).encode()

    resp = _post(_app(upstream), raw)

    assert resp.status_code == 200
    (call,) = upstream.messages_calls
    assert call["body"] == raw
    violations = [
        r.getMessage() for r in horizon_logs if "passthrough_violation=" in r.getMessage()
    ]
    assert len(violations) == 1, violations
    assert "system_role_relocated" in violations[0]


def test_select_outbound_body_violation_sends_original_and_reports_reasons() -> None:
    original = b'{"model": "claude-sonnet-4-5", "messages": []}'
    body = {"model": "claude-sonnet-4-5", "messages": [{"role": "user", "content": "x"}]}

    out = select_outbound_body(
        body=body,
        original_body_bytes=original,
        body_mutated=True,
        mutation_reasons=["cache_ttl_upgrade", "model_id_sanitized"],
        passthrough=True,
    )

    assert out.content == original
    assert out.source == "passthrough"
    assert out.dropped_mutations is True
    assert out.dropped_mutation_reasons == ("cache_ttl_upgrade", "model_id_sanitized")


def test_select_outbound_body_sanitize_only_is_forwarded_canonically() -> None:
    original = b'{"model": "\x1b[1mclaude-sonnet-4-5\x1b[22m", "messages": []}'
    body = {"model": "claude-sonnet-4-5", "messages": []}

    out = select_outbound_body(
        body=body,
        original_body_bytes=original,
        body_mutated=True,
        mutation_reasons=["model_id_sanitized"],
        passthrough=True,
    )

    assert out.content == serialize_body_canonical(body)
    assert out.source == "canonical"
    assert out.dropped_mutations is False


def test_select_outbound_body_unmutated_is_original_bytes() -> None:
    original = b'{"a": 1, "b": "\xf0\x9f\x94\xa5"}'
    out = select_outbound_body(
        body={"a": 1, "b": "🔥"},
        original_body_bytes=original,
        body_mutated=False,
        passthrough=True,
    )
    assert out.content == original
    assert out.source == "passthrough"


def test_select_outbound_body_passthrough_ignores_legacy_forwarder_mode() -> None:
    original = b'{"a": 1}'
    out = select_outbound_body(
        body={"a": 1},
        original_body_bytes=original,
        body_mutated=False,
        forwarder_mode="legacy_json_kwarg",
        passthrough=True,
    )
    assert out.content == original


def test_select_outbound_body_passthrough_skips_signed_thinking_relaxation() -> None:
    """Outside pass-through, a mutation that leaves thinking blocks intact is
    forwarded. In pass-through it is not."""
    thinking = {"type": "thinking", "thinking": "t", "signature": "s=="}
    original_body = {
        "model": CLAUDE,
        "messages": [{"role": "assistant", "content": [thinking, {"type": "text", "text": "a"}]}],
    }
    original = json.dumps(original_body, separators=(", ", ": ")).encode()
    mutated = json.loads(original)
    mutated["messages"][0]["content"][1]["text"] = "rewritten"

    default = select_outbound_body(
        body=mutated, original_body_bytes=original, body_mutated=True, mutation_reasons=["x"]
    )
    passed = select_outbound_body(
        body=mutated,
        original_body_bytes=original,
        body_mutated=True,
        mutation_reasons=["x"],
        passthrough=True,
    )

    assert default.source == "canonical"
    assert passed.content == original


def test_select_outbound_body_without_original_bytes_is_canonical() -> None:
    body = {"a": 1}
    out = select_outbound_body(
        body=body, original_body_bytes=None, body_mutated=True, passthrough=True
    )
    assert out.content == serialize_body_canonical(body)


def test_select_outbound_body_default_mode_unchanged() -> None:
    original = b'{"a": 1}'
    out = select_outbound_body(
        body={"a": 2}, original_body_bytes=original, body_mutated=True, mutation_reasons=["x"]
    )
    assert out.content == serialize_body_canonical({"a": 2})
    assert out.dropped_mutations is False


def test_passthrough_violations_allows_only_the_sanitize() -> None:
    assert passthrough_violations(["model_id_sanitized"]) == []
    assert passthrough_violations(
        ["model_id_sanitized", "flash_observations", "flash_observations"]
    ) == ["flash_observations"]


def test_model_id_is_sanitized_and_the_reason_is_logged(
    horizon_logs: list[logging.LogRecord],
) -> None:
    upstream = _Upstream()
    raw = json.dumps(
        {
            "model": "\x1b[1mclaude-sonnet-4-5\x1b[22m",
            "max_tokens": 64,
            "messages": [{"role": "user", "content": "hi 🔥"}],
        },
        separators=(", ", ": "),
        ensure_ascii=False,
    ).encode()

    resp = _post(_app(upstream), raw)

    assert resp.status_code == 200
    (call,) = upstream.messages_calls
    sent = json.loads(call["body"])
    assert sent["model"] == CLAUDE
    assert sent["messages"] == [{"role": "user", "content": "hi 🔥"}]
    assert any("model_id_sanitized" in r.getMessage() for r in horizon_logs)


# --------------------------------------------------------------------------- #
# Transforms gated off                                                         #
# --------------------------------------------------------------------------- #


def test_violating_ttl_order_is_not_rewritten() -> None:
    """A 5m marker before a 1h marker: the TTL guard would move it in default
    mode. Pass-through forwards the client's order."""
    upstream = _Upstream()
    body = {
        "model": CLAUDE,
        "max_tokens": 64,
        "system": [
            {"type": "text", "text": "sys", "cache_control": {"type": "ephemeral"}},
        ],
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": "hi",
                        "cache_control": {"type": "ephemeral", "ttl": "1h"},
                    }
                ],
            }
        ],
    }
    raw = json.dumps(body, separators=(", ", ": "), ensure_ascii=False).encode()
    _post(_app(upstream), raw)

    (call,) = upstream.messages_calls
    assert call["body"] == raw


def test_response_cache_is_off_in_passthrough() -> None:
    upstream = _Upstream()
    client = _app(upstream, cache_enabled=True)
    raw = _desktop_request_bytes(stream=False)
    _post(client, raw)
    _post(client, raw)
    assert len(upstream.messages_calls) == 2


def test_control_response_cache_serves_second_identical_turn() -> None:
    upstream = _Upstream()
    client = _app(upstream, passthrough=False, cache_enabled=True, optimize=True)
    raw = _desktop_request_bytes(stream=False)
    _post(client, raw)
    _post(client, raw)
    assert len(upstream.messages_calls) == 1


def test_rate_limiter_is_off_in_passthrough() -> None:
    upstream = _Upstream()
    client = _app(upstream, rate_limit_enabled=True, rate_limit_requests_per_minute=1)
    raw = _desktop_request_bytes(stream=False)
    statuses = [_post(client, raw).status_code for _ in range(3)]
    assert statuses == [200, 200, 200]


def test_control_rate_limiter_rejects_second_turn_without_passthrough() -> None:
    upstream = _Upstream()
    client = _app(
        upstream,
        passthrough=False,
        rate_limit_enabled=True,
        rate_limit_requests_per_minute=1,
    )
    raw = _desktop_request_bytes(stream=False)
    statuses = [_post(client, raw).status_code for _ in range(2)]
    assert statuses == [200, 429]


class _RecordingHooks(CompressionHooks):
    def __init__(self) -> None:
        self.pre_calls = 0

    def pre_compress(self, messages, ctx):  # type: ignore[override]
        self.pre_calls += 1
        return [*messages, {"role": "user", "content": "injected by hook"}]


def test_compression_hooks_do_not_run_in_passthrough() -> None:
    upstream = _Upstream()
    hooks = _RecordingHooks()
    raw = _desktop_request_bytes(stream=False)
    _post(_app(upstream, hooks=hooks), raw)

    assert hooks.pre_calls == 0
    (call,) = upstream.messages_calls
    assert call["body"] == raw


def test_control_compression_hooks_run_without_passthrough() -> None:
    upstream = _Upstream()
    hooks = _RecordingHooks()
    raw = _desktop_request_bytes(stream=False)
    _post(_app(upstream, passthrough=False, optimize=True, hooks=hooks), raw)
    assert hooks.pre_calls >= 1


def test_control_default_mode_rewrites_the_desktop_body() -> None:
    """Without pass-through, the tool sort and schema compaction change the body,
    so the byte-identity tests above are not vacuous."""
    upstream = _Upstream()
    raw = _desktop_request_bytes(stream=False)
    _post(_app(upstream, passthrough=False, optimize=True), raw)

    (call,) = upstream.messages_calls
    assert call["body"] != raw


def test_keep_alive_is_not_built_in_passthrough(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HORIZON_CACHE_KEEPALIVE", "1")
    app = create_app(ProxyConfig(passthrough=True, cache_enabled=False, rate_limit_enabled=False))
    assert app.state.proxy.cache_keeper is None


def test_passthrough_normalizes_the_config_table() -> None:
    config = ProxyConfig(passthrough=True)
    assert config.optimize is False
    assert config.image_optimize is False
    assert config.cache_enabled is False
    assert config.rate_limit_enabled is False
    assert config.ccr_inject_tool is False
    assert config.ccr_inject_system_instructions is False
    assert config.ccr_handle_responses is False
    assert config.ccr_resolve_markers_inline is False
    assert config.ccr_proactive_expansion is False
    assert config.read_lifecycle is False
    assert config.read_maturation is False
    assert config.code_aware_enabled is False
    assert config.compress_passthrough is False
    assert config.memory_enabled is False
    assert config.traffic_learning_enabled is False
    assert config.subscription_tracking_enabled is False


# --------------------------------------------------------------------------- #
# Decisions and defaults                                                       #
# --------------------------------------------------------------------------- #


def test_defaults_are_unchanged_without_passthrough() -> None:
    config = ProxyConfig()
    assert config.passthrough is False
    assert is_passthrough(config) is False
    assert config.optimize is True
    assert config.image_optimize is True
    assert config.cache_enabled is True
    assert config.rate_limit_enabled is True


def test_is_passthrough_tolerates_objects_without_the_field() -> None:
    assert is_passthrough(None) is False
    assert is_passthrough(object()) is False


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("1", True), ("true", True), ("ON", True), ("0", False), ("", False), ("no", False)],
)
def test_env_switch_parses_truthy_values(raw: str, expected: bool) -> None:
    assert passthrough_env_enabled({"HORIZON_PASSTHROUGH": raw}) is expected


def test_compression_decision_refuses_in_passthrough() -> None:
    decision = CompressionDecision.decide(
        headers={},
        config=ProxyConfig(passthrough=True),
        usage_reporter=None,
        messages=[{"role": "user", "content": "x"}],
    )
    assert decision.should_compress is False
    assert decision.passthrough_reason == "passthrough_mode"


def test_image_decision_refuses_in_passthrough() -> None:
    decision = ImageCompressionDecision.decide(
        headers={},
        config=ProxyConfig(passthrough=True, image_optimize=True),
        messages=[{"role": "user", "content": "x"}],
    )
    assert decision.should_compress is False
    assert decision.passthrough_reason == "passthrough_mode"


def test_memory_decision_refuses_in_passthrough() -> None:
    decision = MemoryDecision.decide(
        headers={},
        memory_handler=object(),
        memory_user_id="u",
        mode_name="auto_tail",
        passthrough=True,
    )
    assert decision.inject is False
    assert decision.skip_reason == "passthrough_mode"


# --------------------------------------------------------------------------- #
# CLI                                                                          #
# --------------------------------------------------------------------------- #


@pytest.fixture
def cli_runner() -> CliRunner:
    return CliRunner()


def _invoke_proxy(cli_runner: CliRunner, args: list[str], env: dict[str, str] | None = None):
    from unittest.mock import patch

    from horizon.cli.main import main

    captured: dict[str, Any] = {}

    def fake_run_server(config, **kwargs):  # noqa: ANN001, ANN003
        captured["config"] = config

    with patch("horizon.proxy.server.run_server", fake_run_server):
        result = cli_runner.invoke(main, ["proxy", *args], env=env or {}, catch_exceptions=False)
    return result, captured.get("config")


def test_cli_passthrough_flag_sets_config_and_prints_startup_line(cli_runner: CliRunner) -> None:
    result, config = _invoke_proxy(cli_runner, ["--passthrough"])
    assert result.exit_code == 0, result.output
    assert config is not None and config.passthrough is True
    assert config.optimize is False
    assert PASSTHROUGH_STARTUP_LINE in result.output


def test_cli_passthrough_env_var_sets_config(cli_runner: CliRunner) -> None:
    result, config = _invoke_proxy(cli_runner, [], env={"HORIZON_PASSTHROUGH": "1"})
    assert result.exit_code == 0, result.output
    assert config is not None and config.passthrough is True
    assert PASSTHROUGH_STARTUP_LINE in result.output


def test_cli_default_is_not_passthrough(cli_runner: CliRunner) -> None:
    result, config = _invoke_proxy(cli_runner, [])
    assert result.exit_code == 0, result.output
    assert config is not None and config.passthrough is False
    assert PASSTHROUGH_STARTUP_LINE not in result.output


# --------------------------------------------------------------------------- #
# Claude Code / Desktop OAuth (Bearer) requests                                #
# --------------------------------------------------------------------------- #
#
# Enterprise seats send `Authorization: Bearer <claude.ai OAuth token>` instead
# of x-api-key, with an anthropic-beta that carries the OAuth token. The token
# below is an obvious fake. Pass-through must forward the header unchanged and
# must not run the OAuth-specific accounting or the 1h TTL upgrade.

OAUTH_TOKEN = "sk-ant-oat01-FAKE"
OAUTH_BETA = (
    "claude-code-20250219,oauth-2025-04-20,prompt-caching-2024-07-31,extended-cache-ttl-2025-04-11"
)
OAUTH_HEADERS = {
    "authorization": f"Bearer {OAUTH_TOKEN}",
    "anthropic-version": "2023-06-01",
    "anthropic-beta": OAUTH_BETA,
    "user-agent": "claude-cli/2.0.0 (external, cli)",
    "x-claude-code-session-id": "66666666-7777-8888-9999-000000000000",
    "content-type": "application/json",
}


class _FakeSubscriptionTracker:
    def __init__(self) -> None:
        self.notified: list[str] = []
        self.contributions: list[dict[str, Any]] = []

    def notify_active(self, token: str) -> None:
        self.notified.append(token)

    def update_contribution(self, **kwargs: Any) -> None:
        self.contributions.append(kwargs)


@pytest.fixture
def subscription_tracker(monkeypatch: pytest.MonkeyPatch) -> _FakeSubscriptionTracker:
    tracker = _FakeSubscriptionTracker()
    import horizon.subscription.tracker as tracker_mod

    monkeypatch.setattr(tracker_mod, "get_subscription_tracker", lambda: tracker)
    return tracker


def _five_minute_tools_body(*, stream: bool) -> bytes:
    """A tool-carrying turn with only 5-minute markers: the TTL upgrade's target."""
    body = {
        "model": CLAUDE,
        "max_tokens": 256,
        "stream": stream,
        "system": [
            {"type": "text", "text": "Desktop system. 🔥", "cache_control": {"type": "ephemeral"}}
        ],
        "tools": [
            {
                "name": f"oauth_tool_{i}",
                "description": f"Tool {i}",
                "input_schema": {"type": "object", "properties": {}},
            }
            for i in (2, 0, 1)
        ],
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": "hello from OAuth",
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
            }
        ],
    }
    return json.dumps(body, separators=(", ", ": "), ensure_ascii=False).encode("utf-8")


def _oauth_post(client: TestClient, raw: bytes) -> Any:
    return client.post("/v1/messages", headers=OAUTH_HEADERS, content=raw)


def test_oauth_bearer_streaming_request_is_byte_identical_with_headers_unchanged(
    subscription_tracker: _FakeSubscriptionTracker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HORIZON_CACHE_TTL_UPGRADE", "1h")
    upstream = _Upstream()
    raw = _five_minute_tools_body(stream=True)
    client = _app(upstream)

    with client.stream("POST", "/v1/messages", headers=OAUTH_HEADERS, content=raw) as resp:
        assert resp.status_code == 200
        for _ in resp.iter_bytes():
            pass

    (call,) = upstream.messages_calls
    assert call["body"] == raw
    assert call["headers"]["authorization"] == f"Bearer {OAUTH_TOKEN}"
    assert call["headers"]["anthropic-beta"] == OAUTH_BETA
    assert call["headers"]["anthropic-version"] == "2023-06-01"
    assert call["headers"]["user-agent"] == OAUTH_HEADERS["user-agent"]
    assert "x-api-key" not in call["headers"]
    assert subscription_tracker.notified == []
    assert subscription_tracker.contributions == []


def test_oauth_bearer_non_streaming_request_is_byte_identical_with_headers_unchanged(
    subscription_tracker: _FakeSubscriptionTracker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HORIZON_CACHE_TTL_UPGRADE", "1h")
    upstream = _Upstream()
    raw = _five_minute_tools_body(stream=False)
    resp = _oauth_post(_app(upstream), raw)

    assert resp.status_code == 200
    (call,) = upstream.messages_calls
    assert call["body"] == raw
    assert call["headers"]["authorization"] == f"Bearer {OAUTH_TOKEN}"
    assert call["headers"]["anthropic-beta"] == OAUTH_BETA
    assert "x-api-key" not in call["headers"]
    assert subscription_tracker.notified == []
    assert subscription_tracker.contributions == []


def test_oauth_bearer_usage_reaches_client_unchanged(
    subscription_tracker: _FakeSubscriptionTracker,
) -> None:
    upstream = _Upstream()
    raw = _five_minute_tools_body(stream=False)
    resp = _oauth_post(_app(upstream), raw)

    expected = _default_reply(upstream.calls[0]).content
    assert resp.content == expected
    assert resp.json()["usage"]["cache_read_input_tokens"] == 900


def test_oauth_bearer_violation_still_sends_the_clients_bytes(
    subscription_tracker: _FakeSubscriptionTracker,
    horizon_logs: list[logging.LogRecord],
) -> None:
    upstream = _Upstream()
    raw = json.dumps(
        {
            "model": CLAUDE,
            "max_tokens": 64,
            "messages": [
                {"role": "user", "content": "hi"},
                {"role": "system", "content": "relocate me"},
                {"role": "user", "content": "again"},
            ],
        },
        separators=(", ", ": "),
        ensure_ascii=False,
    ).encode()

    _oauth_post(_app(upstream), raw)

    (call,) = upstream.messages_calls
    assert call["body"] == raw
    assert call["headers"]["authorization"] == f"Bearer {OAUTH_TOKEN}"
    assert any("passthrough_violation=" in r.getMessage() for r in horizon_logs)


def test_control_oauth_bearer_is_tracked_and_ttl_upgraded_without_passthrough(
    subscription_tracker: _FakeSubscriptionTracker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without pass-through the OAuth accounting runs and the 1h upgrade rewrites
    the body, so the identity assertions above are meaningful."""
    monkeypatch.setenv("HORIZON_CACHE_TTL_UPGRADE", "1h")
    upstream = _Upstream()
    raw = _five_minute_tools_body(stream=False)
    _oauth_post(_app(upstream, passthrough=False, optimize=True), raw)

    (call,) = upstream.messages_calls
    assert call["body"] != raw
    assert subscription_tracker.notified == [f"Bearer {OAUTH_TOKEN}"]
