"""Byte-faithful passthrough for Anthropic Messages while the engine's license is closed.

Compression is off then, by the same pass-through path account mode uses
(``compression_paused``). That is not enough on its own: other stages still
rewrite the body (prompt-cache breakpoints, model modernization, next-turn
flash stubs). An unlicensed request must reach the provider exactly as the
client sent it, so this middleware relays ``POST /v1/messages`` before any of
those stages run. Credentials are the client's own and are forwarded as sent.

Scope: Anthropic Messages over HTTP. Responses (Codex) and WebSocket traffic
still go through the normal handlers with compression paused.
"""

from __future__ import annotations

import json
from typing import Any

from horizon.proxy.anthropic_wire import build_anthropic_upstream_url
from horizon.proxy.engine_hooks import license_denied

MESSAGES_PATH = "/v1/messages"

# Hop-by-hop headers (RFC 9110 section 7.6.1) and the framing ones the server recomputes.
_HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)
_REQUEST_DROP = _HOP_BY_HOP | {"host", "content-length"}
_RESPONSE_DROP = _HOP_BY_HOP | {"content-length"}


class LicensePassthrough:
    """ASGI middleware: relay Messages calls verbatim while the engine's license is closed."""

    def __init__(self, app: Any, *, proxy: Any) -> None:
        self.app = app
        self.proxy = proxy

    async def __call__(self, scope, receive, send):  # noqa: ANN001 - ASGI signature
        if (
            scope["type"] != "http"
            or scope["method"] != "POST"
            or scope["path"] != MESSAGES_PATH
            or not license_denied()
        ):
            return await self.app(scope, receive, send)
        body = await _read_body(receive)
        upstream_url = build_anthropic_upstream_url(
            self.proxy.ANTHROPIC_API_URL,
            MESSAGES_PATH,
            scope.get("query_string", b"").decode("latin1"),
        )
        headers = [
            (name, value)
            for name, value in scope.get("headers", [])
            if _keep_request_header(name.decode("latin1").lower())
        ]
        client = self.proxy.http_client
        try:
            request = client.build_request("POST", upstream_url, headers=headers, content=body)
            upstream = await client.send(request, stream=True)
        except Exception:  # noqa: BLE001 - reported to the client as a gateway error
            return await _gateway_error(send)
        try:
            await send(
                {
                    "type": "http.response.start",
                    "status": upstream.status_code,
                    "headers": [
                        (name.encode("latin1"), value.encode("latin1"))
                        for name, value in upstream.headers.multi_items()
                        if name.lower() not in _RESPONSE_DROP
                    ],
                }
            )
            async for chunk in upstream.aiter_raw():
                await send({"type": "http.response.body", "body": chunk, "more_body": True})
            await send({"type": "http.response.body", "body": b"", "more_body": False})
        finally:
            await upstream.aclose()


def _keep_request_header(name: str) -> bool:
    # Internal x-horizon-* control headers never leave the engine.
    return name not in _REQUEST_DROP and not name.startswith("x-horizon-")


async def _read_body(receive) -> bytes:  # noqa: ANN001
    chunks: list[bytes] = []
    while True:
        message = await receive()
        if message["type"] != "http.request":
            break
        chunks.append(message.get("body", b""))
        if not message.get("more_body", False):
            break
    return b"".join(chunks)


async def _gateway_error(send) -> None:  # noqa: ANN001
    body = json.dumps(
        {"type": "error", "error": {"type": "api_error", "message": "upstream unreachable"}}
    ).encode()
    await send(
        {
            "type": "http.response.start",
            "status": 502,
            "headers": [
                (b"content-type", b"application/json"),
                (b"cache-control", b"no-store"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body, "more_body": False})
