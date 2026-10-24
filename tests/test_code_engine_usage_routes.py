"""The engine's usage meter counts real model generations only.

Claude Code calls POST /v1/messages/count_tokens before real turns and Codex calls
auxiliary Responses routes; none of those generate output, so none may add to the
request count. Each real generation adds exactly one request, and no request is
recorded with zero tokens.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
from fastapi.testclient import TestClient

from horizon.providers.openai_responses import (
    OPENAI_RESPONSES_ROOT_PATHS,
    OPENAI_RESPONSES_WEBSOCKET_PATHS,
)
from horizon.proxy import engine_hooks
from horizon.proxy.outcome import RequestOutcome, _record_engine_usage
from tests import test_code_engine_gate as gate
from tests.test_code_engine_gate import _body
from tests.test_code_engine_streaming_usage import (
    CLAUDE_CODE_HEADERS,
    CODEX_HEADERS,
    _claude_code_body,
    _FakeProvider,
    _licensed_engine,
    _production_app,
    _uploaded_buckets,
    _with_stream,
)

# The gate tests' fixtures, shared by assignment (same fixture objects).
home = gate.home
signer = gate.signer


class _AuxProvider:
    """Answers the auxiliary routes the way the real APIs do, with 200 JSON.

    Generation routes (Messages, Responses) go to the streaming tests' fake provider.
    """

    def __init__(self) -> None:
        self.calls: list[str] = []
        self._generation = _FakeProvider()

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request.url.path)
        if request.url.path in ("/v1/messages", "/v1/responses"):
            return await self._generation(request)
        if request.url.path == "/v1/messages/count_tokens":
            # The real count_tokens body: no ``usage`` key, only ``input_tokens``.
            return httpx.Response(200, json={"input_tokens": 900})
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [], "has_more": False})
        if request.url.path == "/v1/responses/compact":
            return httpx.Response(
                200,
                json={
                    "id": "resp_c",
                    "object": "response.compaction",
                    "output": [],
                    "usage": {"input_tokens": 900, "output_tokens": 2, "total_tokens": 902},
                },
            )
        if request.url.path == "/v1/messages/batches/msgbatch_1":
            return httpx.Response(200, json={"id": "msgbatch_1", "type": "message_batch"})
        return httpx.Response(404)


@pytest.fixture
def recorded(monkeypatch):
    """Route the engine sink into a list, with the license gate open."""
    calls: list[tuple[str, int, int]] = []
    engine_hooks.install(
        license_allows=lambda: True,
        usage_sink=lambda tool, before, after: calls.append((tool, before, after)),
        health=lambda: {},
    )
    yield calls
    engine_hooks.reset()


def _outcome(**overrides) -> RequestOutcome:
    fields = {
        "request_id": "r1",
        "provider": "anthropic",
        "model": "claude-sonnet-4-6",
        "original_tokens": 900,
        "optimized_tokens": 600,
        "output_tokens": 2,
        "tokens_saved": 300,
        "attempted_input_tokens": 600,
        **overrides,
    }
    return RequestOutcome(**fields)


# ── Through the licensed engine ────────────────────────────────────────────


@pytest.mark.parametrize(
    ("method", "path", "headers", "body", "upstream"),
    [
        pytest.param(
            "POST",
            "/v1/messages/count_tokens?beta=true",
            CLAUDE_CODE_HEADERS,
            _claude_code_body(stream=False),
            "/v1/messages/count_tokens",
            id="claude-count-tokens",
        ),
        pytest.param(
            "GET",
            "/v1/models",
            CLAUDE_CODE_HEADERS,
            None,
            "/v1/models",
            id="model-list",
        ),
        pytest.param(
            "POST",
            "/v1/responses/compact",
            CODEX_HEADERS,
            _with_stream(_body("gpt-5.5"), False),
            "/v1/responses/compact",
            id="codex-compact",
        ),
        pytest.param(
            "GET",
            "/v1/messages/batches/msgbatch_1",
            CLAUDE_CODE_HEADERS,
            None,
            "/v1/messages/batches/msgbatch_1",
            id="batch-status",
        ),
    ],
)
def test_auxiliary_route_records_nothing(home, signer, method, path, headers, body, upstream):
    engine, _api = _licensed_engine(home, signer)
    provider = _AuxProvider()

    response = TestClient(_production_app(engine, provider)).request(
        method, path, content=body, headers=headers
    )

    assert response.status_code == 200
    assert provider.calls == [upstream]
    assert engine.usage.pending_count() == 0


def test_count_tokens_before_a_real_turn_adds_one_request(home, signer):
    engine, api = _licensed_engine(home, signer)
    provider = _AuxProvider()
    client = TestClient(_production_app(engine, provider))

    # Claude Code counts tokens for each turn, and the real turn follows the counts.
    for _ in range(5):
        assert (
            client.post(
                "/v1/messages/count_tokens?beta=true",
                content=_claude_code_body(stream=False),
                headers=CLAUDE_CODE_HEADERS,
            ).status_code
            == 200
        )
    assert engine.usage.pending_count() == 0

    real = client.post(
        "/v1/messages?beta=true",
        content=_claude_code_body(stream=False),
        headers=CLAUDE_CODE_HEADERS,
    )
    assert real.status_code == 200
    assert engine.flush_usage()
    buckets = _uploaded_buckets(api)
    assert len(buckets) == 1
    bucket = buckets[0]
    assert bucket["tool"] == "claude"
    assert bucket["requests"] == 1
    assert bucket["tokens_before"] >= bucket["tokens_after"] > 0


# ── The metering rule itself ───────────────────────────────────────────────


def test_real_messages_generation_is_recorded_once(recorded):
    engine_hooks.bind_request_path("/v1/messages")

    _record_engine_usage(_outcome())

    assert recorded == [("claude", 900, 600)]


@pytest.mark.parametrize(
    "path",
    [
        "/v1/messages/count_tokens",
        "/v1/messages/batches",
        "/v1/messages/batches/msgbatch_1",
        "/v1/models",
        "/v1/models/claude-sonnet-4-6",
        "/v1/compress",
        "/v1/usage",
        None,
    ],
)
def test_anthropic_auxiliary_paths_are_not_metered(recorded, path):
    engine_hooks.bind_request_path(path)

    _record_engine_usage(_outcome())

    assert recorded == []


@pytest.mark.parametrize("path", list(OPENAI_RESPONSES_ROOT_PATHS))
def test_codex_responses_generation_is_recorded_once(recorded, path):
    engine_hooks.bind_request_path(path)

    _record_engine_usage(
        _outcome(provider="openai", client="codex", model="gpt-5.5", original_tokens=900)
    )

    assert recorded == [("codex", 900, 600)]


@pytest.mark.parametrize(
    "path",
    [
        "/v1/responses/compact",
        "/v1/responses/input_tokens",
        "/v1/responses/resp_1",
        "/v1/embeddings",
    ],
)
def test_codex_auxiliary_paths_are_not_metered(recorded, path):
    engine_hooks.bind_request_path(path)

    _record_engine_usage(_outcome(provider="openai", client="codex"))

    assert recorded == []


def test_responses_from_a_non_codex_client_is_not_metered(recorded):
    engine_hooks.bind_request_path("/v1/responses")

    _record_engine_usage(_outcome(provider="openai", client=None))

    assert recorded == []


@pytest.mark.parametrize(
    ("overrides", "why"),
    [
        ({"from_response_cache": True, "original_tokens": 0, "optimized_tokens": 0}, "cache hit"),
        ({"original_tokens": 0, "optimized_tokens": 0}, "unknown counts"),
    ],
)
def test_served_request_without_token_counts_is_not_recorded_as_zero(recorded, overrides, why):
    engine_hooks.bind_request_path("/v1/messages")

    _record_engine_usage(_outcome(**overrides))

    assert recorded == [], why


def test_metered_routes_cover_every_responses_root_and_websocket_path():
    # The table in engine_hooks is hand-kept; these lists are the source of truth.
    for path in OPENAI_RESPONSES_ROOT_PATHS + OPENAI_RESPONSES_WEBSOCKET_PATHS:
        assert engine_hooks.metered_tool(path, provider="openai", client="codex") == "codex", path


def test_websocket_middleware_binds_the_canonical_path():
    from horizon.proxy.server import WebSocketProjectPrefixMiddleware

    seen: list[str | None] = []

    async def app(scope, receive, send):
        seen.append(engine_hooks.request_path())

    scope = {
        "type": "websocket",
        "path": "/p/acme/v1/responses",
        "headers": [],
        "query_string": b"",
    }
    asyncio.run(WebSocketProjectPrefixMiddleware(app)(scope, None, None))

    assert seen == ["/v1/responses"]
