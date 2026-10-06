"""Tests for horizon.forwarder (loopback relay to a remote Horizon proxy).

All HTTP traffic goes through ``httpx.MockTransport`` - no network.
"""

from __future__ import annotations

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from horizon.forwarder import _is_loopback, build_app

REMOTE = "https://remote.example.test"


def _make_client(handler, credential: str = "hz_feedface") -> TestClient:
    app = build_app(REMOTE, lambda: credential, transport=httpx.MockTransport(handler))
    return TestClient(app)


def test_relay_forwards_method_path_and_injects_credential() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["url"] = str(request.url)
        seen["credential"] = request.headers.get("x-horizon-proxy-token")
        seen["body"] = request.read()
        seen["custom"] = request.headers.get("x-custom")
        return httpx.Response(200, json={"ok": True})

    client = _make_client(handler)
    resp = client.post(
        "/v1/messages",
        json={"model": "claude", "max_tokens": 1},
        headers={"x-custom": "keep-me", "x-horizon-proxy-token": "spoofed"},
    )

    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    assert seen["method"] == "POST"
    assert seen["url"].startswith(REMOTE)
    assert "/v1/messages" in seen["url"]
    assert seen["credential"] == "hz_feedface"
    assert json.loads(seen["body"]) == {"model": "claude", "max_tokens": 1}
    assert seen["custom"] == "keep-me"


def test_client_cannot_spoof_credential_header() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["credential"] = request.headers.get("x-horizon-proxy-token")
        return httpx.Response(200)

    client = _make_client(handler)
    client.post("/v1/messages", headers={"x-horizon-proxy-token": "attacker-key"})
    assert seen["credential"] == "hz_feedface"


def test_credential_provider_is_read_per_request() -> None:
    seen: list[str] = []
    creds = iter(["hz_first", "hz_second"])

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("x-horizon-proxy-token", ""))
        return httpx.Response(200)

    app = build_app(REMOTE, lambda: next(creds), transport=httpx.MockTransport(handler))
    with TestClient(app) as client:
        client.get("/a")
        client.get("/b")
    assert seen == ["hz_first", "hz_second"]


def test_hop_by_hop_headers_stripped() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["connection"] = request.headers.get("connection")
        seen["transfer_encoding"] = request.headers.get("transfer-encoding")
        seen["te"] = request.headers.get("te")
        return httpx.Response(
            200,
            content=b"ok",
            headers={"connection": "close", "transfer-encoding": "chunked", "x-real": "1"},
        )

    client = _make_client(handler)
    resp = client.get("/x", headers={"connection": "keep-alive", "te": "trailers"})

    # httpx manages its own upstream Connection header, so only assert that
    # the client's hop-by-hop *values* were not blindly forwarded.
    assert seen["te"] is None
    assert seen["transfer_encoding"] is None
    assert resp.headers.get("x-real") == "1"
    assert "connection" not in resp.headers
    assert "transfer-encoding" not in resp.headers


def test_query_params_relayed() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["params"] = dict(request.url.params)
        return httpx.Response(200)

    client = _make_client(handler)
    client.get("/v1/models?beta=true&limit=5")
    assert seen["params"] == {"beta": "true", "limit": "5"}


def test_upstream_error_becomes_502() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    client = _make_client(handler)
    resp = client.get("/v1/messages")
    assert resp.status_code == 502
    assert "upstream error" in resp.text


def test_upstream_status_passthrough() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": "rate_limited"})

    client = _make_client(handler)
    resp = client.post("/v1/messages")
    assert resp.status_code == 429
    assert resp.json() == {"error": "rate_limited"}


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1"])
def test_loopback_hosts_allowed(host: str) -> None:
    assert _is_loopback(host) is True


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.5", "example.com"])
def test_non_loopback_hosts_rejected(host: str) -> None:
    assert _is_loopback(host) is False


def test_run_forwarder_refuses_non_loopback_bind() -> None:
    from horizon.forwarder import run_forwarder

    with pytest.raises(ValueError, match="non-loopback"):
        run_forwarder("https://remote.example.test", port=18788, host="0.0.0.0")


def test_run_forwarder_fails_fast_without_credential(monkeypatch: pytest.MonkeyPatch) -> None:
    from horizon import vault
    from horizon.forwarder import run_forwarder

    def _raise():
        raise vault.VaultError("no Horizon credential stored - set one with: horizon vault set")

    monkeypatch.setattr(vault, "get_credential", _raise)

    with pytest.raises(ValueError, match="horizon vault set"):
        run_forwarder("https://remote.example.test", port=18788)


# ── Plugin-tagged traffic: model calls to the proxy, everything else direct ──


def _capture(seen: list):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"ok": True})

    return handler


def test_tagged_non_model_call_goes_direct_without_credential() -> None:
    """Sign-in (e.g. ChatGPT device auth) must reach its real host, keyless."""
    seen: list = []
    client = _make_client(_capture(seen))
    resp = client.post(
        "/api/accounts/deviceauth/usercode",
        params={"x": "1"},
        json={"client_id": "abc"},
        headers={
            "x-horizon-base-url": "https://auth.openai.com",
            "x-horizon-project": "demo",
            "authorization": "Bearer provider-token",
        },
    )
    assert resp.status_code == 200
    (req,) = seen
    assert str(req.url) == "https://auth.openai.com/api/accounts/deviceauth/usercode?x=1"
    assert "x-horizon-proxy-token" not in req.headers
    assert not any(name.startswith("x-horizon-") for name in req.headers)
    assert req.headers["authorization"] == "Bearer provider-token"


def test_tagged_model_call_still_goes_to_the_proxy_with_credential() -> None:
    seen: list = []
    client = _make_client(_capture(seen))
    client.post(
        "/v1/chat/completions",
        json={"model": "glm"},
        headers={"x-horizon-base-url": "https://api.oneprovider.dev"},
    )
    (req,) = seen
    assert str(req.url).startswith(REMOTE + "/v1/chat/completions")
    assert req.headers["x-horizon-proxy-token"] == "hz_feedface"
    assert req.headers["x-horizon-base-url"] == "https://api.oneprovider.dev"


def test_untagged_non_model_call_still_goes_to_the_proxy() -> None:
    """Without a plugin tag there is no real destination to go to."""
    seen: list = []
    client = _make_client(_capture(seen))
    client.get("/v1/models")
    client.get("/something/else")
    assert all(str(r.url).startswith(REMOTE) for r in seen)


def test_tag_with_unsafe_scheme_is_not_followed() -> None:
    seen: list = []
    client = _make_client(_capture(seen))
    client.get("/catalog", headers={"x-horizon-base-url": "file:///etc"})
    (req,) = seen
    assert str(req.url).startswith(REMOTE)


# ── Fixed upstream (tools for providers the proxy is not configured for) ──


def _upstream_client(seen: list, upstream: str) -> TestClient:
    app = build_app(
        REMOTE, lambda: "hz_feedface", transport=httpx.MockTransport(_capture(seen)), upstream=upstream
    )
    return TestClient(app)


@pytest.mark.parametrize(
    ("upstream", "path", "origin", "original"),
    [
        # Kimi's base already ends in /v1: the client's /v1 maps onto it.
        ("https://api.kimi.com/coding/v1", "/p/demo/v1/chat/completions",
         "https://api.kimi.com", "/coding/v1/chat/completions"),
        ("https://api.mistral.ai", "/p/demo/v1/chat/completions",
         "https://api.mistral.ai", "/v1/chat/completions"),
        ("https://api.x.ai/", "/v1/responses", "https://api.x.ai", "/v1/responses"),
    ],
)
def test_fixed_upstream_tags_model_calls_for_the_proxy(upstream, path, origin, original) -> None:
    seen: list = []
    _upstream_client(seen, upstream).post(path, json={"model": "m"})
    (req,) = seen
    assert str(req.url) == REMOTE + path
    assert req.headers["x-horizon-proxy-token"] == "hz_feedface"
    assert req.headers["x-horizon-base-url"] == origin
    assert req.headers["x-horizon-original-path"] == original


def test_fixed_upstream_sends_other_calls_direct_without_credential() -> None:
    seen: list = []
    client = _upstream_client(seen, "https://api.kimi.com/coding/v1")
    client.get("/p/demo/v1/models", headers={"authorization": "Bearer kimi"})
    client.get("/v1/usages", params={"a": "1"})
    assert [str(r.url) for r in seen] == [
        "https://api.kimi.com/coding/v1/models",
        "https://api.kimi.com/coding/v1/usages?a=1",
    ]
    assert all("x-horizon-proxy-token" not in r.headers for r in seen)
    assert seen[0].headers["authorization"] == "Bearer kimi"


def test_plugin_tag_wins_over_fixed_upstream() -> None:
    seen: list = []
    _upstream_client(seen, "https://api.kimi.com/coding/v1").post(
        "/v1/chat/completions", headers={"x-horizon-base-url": "https://api.other.dev"}
    )
    (req,) = seen
    assert req.headers["x-horizon-base-url"] == "https://api.other.dev"
    assert "x-horizon-original-path" not in req.headers


@pytest.mark.parametrize("bad", ["ftp://x.example", "api.kimi.com", "https://u:p@x.example/v1", "https://x.example/v1?a=1"])
def test_fixed_upstream_rejects_bad_urls(bad: str) -> None:
    with pytest.raises(ValueError):
        build_app(REMOTE, lambda: "k", upstream=bad)


# ── WebSocket relay (Codex Responses transport) ──────────────────────────────


class FakeUpstreamWS:
    """Stands in for a websockets client connection to the remote proxy."""

    def __init__(self, frames: list, subprotocol: str | None = None) -> None:
        self.frames = list(frames)
        self.sent: list = []
        self.subprotocol = subprotocol
        self.closed = False
        self.close_code = 1000
        self.close_reason = ""

    async def send(self, message) -> None:
        self.sent.append(message)

    def __aiter__(self):
        return self

    async def __anext__(self):
        import asyncio

        # Answer only after the client spoke, like a real Responses turn.
        while not self.sent and not self.closed:
            await asyncio.sleep(0.01)
        if self.closed or not self.frames:
            raise StopAsyncIteration
        return self.frames.pop(0)

    async def close(self) -> None:
        self.closed = True


def _ws_client(upstream: FakeUpstreamWS, calls: list, credential: str = "hz_feedface"):
    async def connect(url, headers, subprotocols):
        calls.append((url, headers, subprotocols))
        return upstream

    return TestClient(build_app(REMOTE, lambda: credential, ws_connect=connect))


def test_websocket_relays_frames_and_injects_credential() -> None:
    calls: list = []
    upstream = FakeUpstreamWS(["event-1", b"\x00binary"], subprotocol="chat")
    client = _ws_client(upstream, calls)
    with client.websocket_connect(
        "/v1/responses?x=1",
        subprotocols=["chat"],
        headers={"authorization": "Bearer provider-token", "x-horizon-proxy-token": "spoofed"},
    ) as ws:
        assert ws.accepted_subprotocol == "chat"
        ws.send_text('{"type":"response.create"}')
        assert ws.receive_text() == "event-1"
        assert ws.receive_bytes() == b"\x00binary"
    (url, headers, subprotocols) = calls[0]
    assert url == "wss://remote.example.test/v1/responses?x=1"
    assert headers["x-horizon-proxy-token"] == "hz_feedface"  # not the spoofed value
    assert headers["authorization"] == "Bearer provider-token"
    assert not {"sec-websocket-key", "upgrade", "connection", "host"} & set(headers)
    assert subprotocols == ["chat"]
    assert upstream.sent == ['{"type":"response.create"}']
    assert upstream.closed


def test_websocket_refused_upstream_rejects_the_handshake() -> None:
    from starlette.websockets import WebSocketDisconnect

    class Refused(Exception):
        response = type("R", (), {"status_code": 401})()

    async def connect(url, headers, subprotocols):
        raise Refused("HTTP 401")

    client = TestClient(build_app(REMOTE, lambda: "hz_x", ws_connect=connect))
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect("/v1/responses"):
            pass
    assert exc.value.code == 1008
