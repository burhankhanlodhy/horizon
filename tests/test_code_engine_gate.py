"""The license gate on the engine's proxy: compression, pass-through, health and privacy."""

from __future__ import annotations

import base64
import json
import time

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from horizon.code_engine import license as lic
from horizon.code_engine.device import MACHINE_ID_ENV, device_hash
from horizon.code_engine.engine import Engine
from horizon.code_engine.license import write_license
from horizon.proxy import engine_hooks
from horizon.proxy.server import ProxyConfig, create_app

MACHINE = "gate-test-machine"
DH = device_hash(MACHINE)
API_HOST = "api.test"
PROMPT_MARKER = "CONFIDENTIAL-PROMPT-MARKER-7731"
MESSAGES_HEADERS = {
    "x-api-key": "test-key",
    "anthropic-version": "2023-06-01",
    "content-type": "application/json",
}


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


@pytest.fixture
def signer(monkeypatch) -> Ed25519PrivateKey:
    key = Ed25519PrivateKey.generate()
    raw = key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
    )
    monkeypatch.setattr(lic, "CODE_ENTITLEMENT_PUBLIC_KEY_B64", base64.b64encode(raw).decode())
    return key


def _note(key: Ed25519PrivateKey, *, plan: str = "trial") -> str:
    now = int(time.time())
    payload = {
        "v": 1,
        "iss": "contextshrink-code",
        "sub": "user-1",
        "dev": "dev-1",
        "dh": DH,
        "plan": plan,
        "iat": now,
        "exp": now + 72 * 3600,
        "until": now + 7 * 86400,
    }
    raw = json.dumps(payload, separators=(",", ":")).encode()
    return f"{_b64url(raw)}.{_b64url(key.sign(raw))}"


@pytest.fixture
def home(tmp_path, monkeypatch):
    path = tmp_path / "home"
    path.mkdir()
    monkeypatch.setenv("CONTEXTSHRINK_HOME", str(path))
    monkeypatch.setenv(MACHINE_ID_ENV, MACHINE)
    monkeypatch.setenv("HORIZON_SAVINGS", "max")
    yield path
    engine_hooks.reset()


def _write_device(home) -> None:
    data = {
        "version": 1,
        "api_url": f"https://{API_HOST}",
        "device_id": "dev-1",
        "device_key": "cs_dev_gate_test_key",
        "device_hash": DH,
        "tools": ["claude"],
        "created_at": "2026-10-22T00:00:00+00:00",
    }
    (home / "device.json").write_text(json.dumps(data), encoding="utf-8")


class _ApiServer:
    """A fake ContextShrink API. Records every request so the privacy test can read them."""

    def __init__(self, signer: Ed25519PrivateKey | None) -> None:
        self.signer = signer
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path == "/code/entitlement" and self.signer is not None:
            return httpx.Response(200, json={"token": _note(self.signer), "plan": "trial"})
        if request.url.path == "/code/entitlement":
            return httpx.Response(402, json={"detail": {"code": "plan_required"}})
        if request.url.path == "/code/usage":
            return httpx.Response(200, json={"accepted": 1})
        return httpx.Response(404)


class _Stream(httpx.AsyncByteStream):
    def __init__(self, data: bytes) -> None:
        self._data = data

    async def __aiter__(self):
        yield self._data


class _Upstream:
    """A fake Anthropic: records the bytes it receives and answers with a fixed message."""

    def __init__(self) -> None:
        self.body: bytes | None = None
        self.hosts: list[str] = []

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.hosts.append(request.url.host)
        self.body = await request.aread()
        payload = json.dumps(
            {
                "id": "msg_1",
                "type": "message",
                "role": "assistant",
                "model": "claude-sonnet-4-6",
                "content": [{"type": "text", "text": "ok"}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 10, "output_tokens": 2},
            }
        ).encode()
        return httpx.Response(
            200, headers={"content-type": "application/json"}, stream=_Stream(payload)
        )


def _body(model: str = "claude-sonnet-4-6") -> bytes:
    rows = [
        {"id": i, "name": f"item-{i}", "status": "ok", "path": f"/src/mod_{i % 7}.py"}
        for i in range(400)
    ]
    body = {
        "model": model,
        "max_tokens": 64,
        "messages": [
            {"role": "user", "content": f"list the items, {PROMPT_MARKER}"},
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_1",
                        "content": json.dumps(rows),
                    }
                ],
            },
        ],
    }
    return json.dumps(body).encode()


def _engine(home, api: _ApiServer) -> Engine:
    return Engine(home, client=httpx.Client(transport=httpx.MockTransport(api)))


def _app(engine: Engine | None, upstream: _Upstream):
    if engine is not None:
        engine_hooks.install(
            license_allows=engine.allows_compression,
            usage_sink=engine.record_usage,
            health=engine.health,
        )
    app = create_app(
        ProxyConfig(
            cache_enabled=False,
            rate_limit_enabled=False,
            log_requests=False,
            ccr_inject_tool=False,
            ccr_handle_responses=False,
            image_optimize=False,
        )
    )
    app.state.proxy.http_client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
    return app


def _post(app, raw: bytes):
    return TestClient(app).post("/v1/messages", content=raw, headers=MESSAGES_HEADERS)


def test_licensed_engine_compresses_before_forwarding(home, signer) -> None:
    _write_device(home)
    write_license(home, _note(signer), fetched_at=time.time())
    api = _ApiServer(signer)
    engine = _engine(home, api)
    engine.load_cached_note()
    upstream = _Upstream()
    raw = _body()

    response = _post(_app(engine, upstream), raw)

    assert response.status_code == 200
    assert upstream.body is not None and len(upstream.body) < len(raw)
    assert upstream.hosts == ["api.anthropic.com"]
    assert engine.usage.pending_count() == 1


@pytest.mark.parametrize("model", ["claude-sonnet-4-6", "claude-opus-4-1"])
def test_closed_gate_forwards_the_request_byte_for_byte(home, signer, model) -> None:
    _write_device(home)
    api = _ApiServer(None)
    engine = _engine(home, api)
    engine.load_cached_note()
    upstream = _Upstream()
    raw = _body(model)

    response = _post(_app(engine, upstream), raw)

    assert response.status_code == 200
    assert upstream.body == raw
    assert engine.usage.pending_count() == 0


def test_engine_without_device_json_is_pass_through_only(home, signer) -> None:
    api = _ApiServer(signer)
    engine = _engine(home, api)
    engine.load_cached_note()
    upstream = _Upstream()
    raw = _body()

    _post(_app(engine, upstream), raw)

    assert upstream.body == raw
    assert api.requests == []


def test_health_reports_the_license_state(home, signer) -> None:
    _write_device(home)
    write_license(home, _note(signer), fetched_at=time.time())
    engine = _engine(home, _ApiServer(signer))
    engine.load_cached_note()
    health = TestClient(_app(engine, _Upstream())).get("/__cs/health").json()
    assert health["ok"] is True
    assert health["plan"] == "trial"
    assert isinstance(health["engine"], str) and health["engine"]


def test_health_reports_not_ok_when_unlicensed(home, signer) -> None:
    _write_device(home)
    engine = _engine(home, _ApiServer(None))
    engine.load_cached_note()
    response = TestClient(_app(engine, _Upstream())).get("/__cs/health")
    assert response.status_code == 200
    assert response.json()["ok"] is False
    assert response.json()["plan"] is None


def test_health_route_exists_only_for_the_engine(home) -> None:
    # Without an engine the path is not the engine's health route; it falls through to
    # the proxy's relay, so the fake upstream answers and no "engine" key is present.
    app = _app(None, _Upstream())
    assert "engine" not in TestClient(app).get("/__cs/health").json()


def test_privacy_only_counts_and_entitlement_reach_the_api(home, signer) -> None:
    """Spec section 5: the API host sees /code/entitlement and /code/usage, never prompt text."""
    _write_device(home)
    write_license(home, _note(signer), fetched_at=time.time())
    api = _ApiServer(signer)
    engine = _engine(home, api)
    engine.load_cached_note()
    upstream = _Upstream()

    response = _post(_app(engine, upstream), _body())
    assert response.status_code == 200
    engine.refresh()
    assert engine.flush_usage()

    api_requests = [r for r in api.requests if r.url.host == API_HOST]
    assert {r.url.path for r in api_requests} == {"/code/entitlement", "/code/usage"}
    assert {r.url.host for r in api.requests} == {API_HOST}
    for request in api_requests:
        assert PROMPT_MARKER.encode() not in request.content
        assert b"tool_result" not in request.content
        assert b"claude-sonnet" not in request.content
    # The prompt went to the provider, and nowhere else.
    assert upstream.hosts == ["api.anthropic.com"]
    assert PROMPT_MARKER.encode() in (upstream.body or b"")
    usage = json.loads(next(r for r in api_requests if r.url.path == "/code/usage").content)
    assert set(usage) == {"buckets"}
    assert set(usage["buckets"][0]) == {"hour", "tool", "requests", "tokens_before", "tokens_after"}


def test_health_is_one_plain_http_response_with_a_content_length(home, signer) -> None:
    """The Code-tab launcher reads one HTTP/1.1 response: no chunked body, a length it can trust."""
    import socket
    import threading

    import uvicorn

    _write_device(home)
    write_license(home, _note(signer), fetched_at=time.time())
    engine = _engine(home, _ApiServer(signer))
    engine.load_cached_note()
    app = _app(engine, _Upstream())

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, lifespan="off", log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.time() + 10
        while not server.started and time.time() < deadline:
            time.sleep(0.02)
        assert server.started

        with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
            sock.sendall(
                b"GET /__cs/health HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n"
            )
            raw = b""
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                raw += chunk
    finally:
        server.should_exit = True
        thread.join(timeout=10)

    head, _, body = raw.partition(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    assert lines[0] == "HTTP/1.1 200 OK"
    headers = {
        k.strip().lower(): v.strip() for k, _, v in (line.partition(":") for line in lines[1:])
    }
    assert "transfer-encoding" not in headers
    assert headers["content-length"] == str(len(body))
    assert headers["content-type"].startswith("application/json")
    assert json.loads(body)["ok"] is True
