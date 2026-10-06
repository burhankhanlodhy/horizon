"""Loopback relay to a remote Horizon proxy.

``horizon forward start`` runs this app on the local machine: it listens on
loopback only, attaches the caller's per-user ``hz_...`` key from the OS
credential store (:mod:`horizon.vault`), and streams everything to the
remote Horizon proxy. The key therefore never appears in a plaintext config
file, and the wrapped client (Claude Code, Codex, ...) simply points at
``http://127.0.0.1:<port>``.

Streaming matters: LLM clients consume SSE responses incrementally, so the
relay pipes the upstream body through instead of buffering it.

Transport plugins (e.g. the OpenCode plugin) reroute *every* HTTP call through
the relay, tagging the real destination with ``x-horizon-base-url``. Only model
traffic belongs on the proxy, so a tagged request whose path the proxy does not
serve (sign-in, model catalogues, ...) goes straight to its real destination
instead - exactly as it would without Horizon, and without the key.

WebSocket connections (e.g. Codex's Responses transport) are relayed the same
way: the key is added to the upstream handshake and frames are piped both ways.

A relay started with a fixed ``upstream`` (``horizon forward start --upstream``)
serves a tool that talks to one OpenAI-compatible provider the proxy is not
configured for (Kimi, Mistral, xAI). It tags that tool's requests exactly as a
transport plugin would, so model calls reach that provider through the proxy
and everything else goes straight to the provider.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlparse

import httpx
from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import Response, StreamingResponse

logger = logging.getLogger(__name__)

#: Hop-by-hop headers (RFC 7230 section 6.1) - never relayed in either direction.
_HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}

#: The proxy security gate reads the credential from this header.
CREDENTIAL_HEADER = "x-horizon-proxy-token"

#: Set by transport plugins: the real destination's origin, and the original
#: path when the plugin normalized it for the proxy.
BASE_URL_HEADER = "x-horizon-base-url"
ORIGINAL_PATH_HEADER = "x-horizon-original-path"

_DEFAULT_TIMEOUT = httpx.Timeout(300.0, connect=15.0)


def _is_loopback(host: str) -> bool:
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return host in ("localhost", "localhost.localdomain")
    return addr.is_loopback


def _proxy_serves(path: str) -> bool:
    """True for the model operations an account-mode proxy accepts."""
    from horizon.proxy.account_analytics import AUXILIARY, INFERENCE

    return bool(INFERENCE.fullmatch(path) or AUXILIARY.fullmatch(path))


def _direct_target(request: Request, url: str) -> str | None:
    """The real URL of a plugin-tagged request the proxy would not serve."""
    origin = request.headers.get(BASE_URL_HEADER)
    if not origin or _proxy_serves(url):
        return None
    parsed = urlparse(origin)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return None
    path = request.headers.get(ORIGINAL_PATH_HEADER) or url
    return f"{parsed.scheme}://{parsed.netloc}{path}"


_PROJECT_PREFIX = re.compile(r"^/p/[^/]+(?=/)")


def parse_upstream(upstream: str) -> tuple[str, str]:
    """Split a provider base URL into (origin, path), e.g. ``https://api.kimi.com/coding/v1``."""
    parsed = urlparse(upstream.strip())
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError(f"upstream must be an http(s) URL, got {upstream!r}")
    if parsed.query or parsed.fragment or parsed.username or parsed.password:
        raise ValueError(f"upstream must be a plain base URL, got {upstream!r}")
    return f"{parsed.scheme}://{parsed.netloc}", parsed.path.rstrip("/")


def _upstream_path(base_path: str, url: str) -> str:
    """The provider path for relay path ``url``, given the provider's base path.

    Tools point at ``<relay>[/p/<project>]/v1``; the provider's own base may
    already end in ``/v1`` (``https://api.kimi.com/coding/v1``), so the client's
    ``/v1`` maps onto it rather than doubling up.
    """
    path = _PROJECT_PREFIX.sub("", url)
    if base_path.endswith("/v1") and (path == "/v1" or path.startswith("/v1/")):
        path = path[len("/v1") :]
    return f"{base_path}{path}"


#: Opens the upstream WebSocket: (url, headers, subprotocols) -> connection.
WsConnect = Callable[[str, dict[str, str], list[str]], Awaitable[Any]]


def _ws_url(base: str, path: str, query: str) -> str:
    """ws(s):// form of the remote proxy URL for ``path``."""
    parsed = urlparse(base)
    scheme = {"https": "wss", "http": "ws"}.get(parsed.scheme, parsed.scheme)
    url = f"{scheme}://{parsed.netloc}{parsed.path.rstrip('/')}/{path}"
    return f"{url}?{query}" if query else url


async def _connect_upstream_ws(url: str, headers: dict[str, str], subprotocols: list[str]) -> Any:
    import websockets

    return await websockets.connect(
        url,
        additional_headers=headers,
        subprotocols=[websockets.Subprotocol(p) for p in subprotocols] or None,
        open_timeout=30,
        close_timeout=10,
        # Keep NAT/Cloudflare paths alive, but never drop a healthy session
        # that goes quiet for a long turn (same policy as the proxy's relay).
        ping_interval=20,
        ping_timeout=None,
        # Large single frames (e.g. inline images) must pass through.
        max_size=None,
    )


def build_app(
    remote_url: str,
    credential_provider: Callable[[], str],
    transport: httpx.AsyncBaseTransport | None = None,
    ws_connect: WsConnect | None = None,
    upstream: str | None = None,
) -> FastAPI:
    """Build the relay app.

    ``upstream`` fixes the provider for untagged requests (see module doc):
    inference calls go to the proxy tagged for it, anything else (model lists,
    usage, sign-in) goes straight to it without the key.

    ``credential_provider`` is called once per request (not at startup) so a
    rotated key stored mid-session is picked up without a restart. It should
    return the raw ``hz_...`` key and may raise :class:`horizon.vault.VaultError`.
    ``transport`` and ``ws_connect`` exist for tests (inject fakes).
    """

    base = remote_url.rstrip("/")
    fixed = parse_upstream(upstream) if upstream else None
    app = FastAPI(title="Horizon Forwarder", docs_url=None, redoc_url=None, openapi_url=None)
    client = httpx.AsyncClient(
        base_url=base,
        timeout=_DEFAULT_TIMEOUT,
        follow_redirects=False,
        transport=transport,
    )

    # Direct passthrough for non-model traffic: no base URL, no credential.
    direct = httpx.AsyncClient(
        timeout=_DEFAULT_TIMEOUT, follow_redirects=False, transport=transport
    )

    async def send(
        http: httpx.AsyncClient, request: Request, url: str, headers: dict, path: str
    ) -> Response:
        body = await request.body()
        try:
            upstream = http.build_request(
                request.method,
                url,
                headers=headers,
                content=body if request.method not in ("GET", "HEAD") else None,
                params=request.query_params,
            )
            upstream_resp = await http.send(upstream, stream=True)
        except httpx.HTTPError as exc:
            logger.warning("forwarder upstream error path=%s: %s", path, exc)
            return Response(
                content=f"upstream error: {exc}\n",
                status_code=502,
                media_type="text/plain",
            )

        resp_headers = {
            name: value
            for name, value in upstream_resp.headers.items()
            if name.lower() not in _HOP_BY_HOP
        }

        async def stream_iter():
            try:
                if upstream_resp.is_stream_consumed:
                    # Fully-buffered upstream (test transports, some gateways
                    # hand back a materialized response): relay the buffer.
                    yield upstream_resp.content
                else:
                    async for chunk in upstream_resp.aiter_raw():
                        yield chunk
            finally:
                await upstream_resp.aclose()

        return StreamingResponse(
            stream_iter(),
            status_code=upstream_resp.status_code,
            headers=resp_headers,
        )

    @app.websocket("/{path:path}")
    async def relay_ws(websocket: WebSocket, path: str) -> None:
        from websockets.exceptions import ConnectionClosed

        from horizon.proxy.ws_headers import WS_HOP_BY_HOP_HEADERS

        headers = {
            name: value
            for name, value in websocket.headers.items()
            if name.lower() not in WS_HOP_BY_HOP_HEADERS and name.lower() != CREDENTIAL_HEADER
        }
        headers[CREDENTIAL_HEADER] = credential_provider()
        subprotocols = list(websocket.scope.get("subprotocols") or [])
        url = _ws_url(base, path, websocket.url.query)
        try:
            upstream = await (ws_connect or _connect_upstream_ws)(url, headers, subprotocols)
        except Exception as exc:  # noqa: BLE001 - refuse the handshake, log why
            status = getattr(getattr(exc, "response", None), "status_code", None)
            logger.warning("forwarder websocket upstream refused path=%s status=%s: %s", path, status, exc)
            # Closing before accept answers the client's handshake with 403.
            await websocket.close(code=1008 if status in (401, 403) else 1011)
            return

        await websocket.accept(subprotocol=getattr(upstream, "subprotocol", None))

        async def client_to_upstream() -> None:
            while True:
                message = await websocket.receive()
                if message["type"] == "websocket.disconnect":
                    return
                if message.get("text") is not None:
                    await upstream.send(message["text"])
                elif message.get("bytes") is not None:
                    await upstream.send(message["bytes"])

        async def upstream_to_client() -> None:
            try:
                async for frame in upstream:
                    if isinstance(frame, str):
                        await websocket.send_text(frame)
                    else:
                        await websocket.send_bytes(frame)
            except ConnectionClosed:
                pass

        tasks = [
            asyncio.ensure_future(client_to_upstream()),
            asyncio.ensure_future(upstream_to_client()),
        ]
        try:
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            for task in done:
                if task.exception() is not None:
                    logger.warning("forwarder websocket relay ended: %s", task.exception())
        finally:
            await upstream.close()
            code = getattr(upstream, "close_code", None) or 1000
            # 1005/1006/1015 describe a missing or broken close and may not be
            # sent in a close frame; report them as normal / internal error.
            code = {1005: 1000, 1006: 1011, 1015: 1011}.get(code, code)
            reason = getattr(upstream, "close_reason", None) or ""
            try:
                await websocket.close(code=code, reason=reason)
            except RuntimeError:
                pass  # the client already closed

    @app.api_route(
        "/{path:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
    )
    async def relay(path: str, request: Request) -> Response:
        url = f"/{path}" if path else "/"
        tags: dict[str, str] = {}
        if fixed is not None and BASE_URL_HEADER not in request.headers:
            origin, base_path = fixed
            upstream_path = _upstream_path(base_path, url)
            from horizon.proxy.account_analytics import INFERENCE

            if not INFERENCE.fullmatch(url):
                headers = {
                    name: value
                    for name, value in request.headers.items()
                    if name.lower() not in _HOP_BY_HOP
                    and name.lower() != "host"
                    and not name.lower().startswith("x-horizon-")
                }
                return await send(direct, request, f"{origin}{upstream_path}", headers, path)
            tags = {BASE_URL_HEADER: origin, ORIGINAL_PATH_HEADER: upstream_path}

        target = _direct_target(request, url)
        if target is not None:
            headers = {
                name: value
                for name, value in request.headers.items()
                if name.lower() not in _HOP_BY_HOP
                and name.lower() != "host"
                and not name.lower().startswith("x-horizon-")
            }
            return await send(direct, request, target, headers, path)

        headers = {
            name: value
            for name, value in request.headers.items()
            if name.lower() not in _HOP_BY_HOP
            and name.lower() != "host"
            and name.lower() != CREDENTIAL_HEADER
        }
        headers.update(tags)
        headers[CREDENTIAL_HEADER] = credential_provider()
        return await send(client, request, url, headers, path)

    return app


def run_forwarder(
    remote_url: str,
    port: int,
    host: str = "127.0.0.1",
    credential_provider: Callable[[], str] | None = None,
    upstream: str | None = None,
) -> None:
    """Run the relay (blocking). Binds loopback only.

    ``credential_provider`` defaults to reading the per-user key from the OS
    credential store. Startup fails fast when no credential is stored.
    """

    if upstream:
        parse_upstream(upstream)  # reject a bad URL before touching the vault

    if not _is_loopback(host):
        raise ValueError(
            f"refusing to bind non-loopback address {host!r} - the relay "
            "exists to keep the key local"
        )

    if credential_provider is None:
        from horizon.vault import VaultError, get_credential

        try:
            get_credential()  # fail fast with the actionable vault message
        except VaultError as exc:
            raise ValueError(str(exc)) from exc

        credential_provider = get_credential

    import uvicorn

    app = build_app(
        remote_url=remote_url, credential_provider=credential_provider, upstream=upstream
    )
    uvicorn.run(app, host=host, port=port, log_level="info")
