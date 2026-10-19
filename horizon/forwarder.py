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
import hashlib
import ipaddress
import json
import logging
import os
import re
import threading
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from fastapi import BackgroundTasks, FastAPI, Request, WebSocket
from fastapi.responses import JSONResponse, Response, StreamingResponse

from horizon.forwarder_stats import (
    HEARTBEAT_SECONDS,
    ClaudeCodeStats,
    RequestRecorder,
    claude_code_dir,
    default_state_dir,
    heartbeat,
)
from horizon.verified_savings import FORWARDER_CLAUDE_CODE, FORWARDER_HEADER

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

#: Answered by the relay itself, never forwarded: a liveness and identity probe.
HEALTH_PATH = "/__cs/health"

#: Request header the proxy reads to pass a request through untouched (paused).
BYPASS_HEADER = "x-horizon-bypass"

#: Relayed Anthropic messages calls: the path suffix that gets the Claude Code tag.
_MESSAGES_SUFFIX = "/v1/messages"
_KEEPALIVE_PATH = re.compile(r"^/k/([^/]+)/")
_KEEPALIVE_SEGMENT = re.compile(r"^/k/([A-Za-z0-9_-]{1,64})(?=/|$)")
_SESSION_HEADER = "x-claude-code-session-id"
#: The proxy's keep-alive id charset and length (horizon.proxy.project_policy).
_KEEPALIVE_ID_MAX = 64
#: Hex digits of sha256(session id) appended to the machine id: one keep-alive group per session.
_SESSION_DIGEST_HEX = 12
#: Session-end route: the plugin's session.end hook posts here (loopback only).
SESSION_END_PATH = "/__cs/session-end"
_SESSION_END_BODY_MAX = 4096
_SESSION_LIVENESS_MAX = 512

#: Set by transport plugins: the real destination's origin, and the original
#: path when the plugin normalized it for the proxy.
BASE_URL_HEADER = "x-horizon-base-url"
ORIGINAL_PATH_HEADER = "x-horizon-original-path"

#: The routing tags a transport plugin sends to name the real destination. They
#: are the one client x-horizon-* pair the relay passes on to the proxy.
_ROUTING_TAGS = (BASE_URL_HEADER, ORIGINAL_PATH_HEADER)

_DEFAULT_TIMEOUT = httpx.Timeout(300.0, connect=15.0)


def _is_loopback(host: str) -> bool:
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return host in ("localhost", "localhost.localdomain")
    return addr.is_loopback


def session_keepalive_id(machine_id: str, session_id: str) -> str | None:
    """The keep-alive id one Claude Code session gets under a machine's ``/k/`` route.

    Connect gives every session the same per-machine id; this derives
    ``<machine id>-<12 hex of sha256(session id)>`` so each session is its own
    keep-alive group and can be ended alone. ``None`` when the result would not
    fit the proxy's id rules, in which case the machine id stands.
    """
    if not session_id:
        return None
    digest = hashlib.sha256(session_id.encode("utf-8", "ignore")).hexdigest()
    derived = f"{machine_id}-{digest[:_SESSION_DIGEST_HEX]}"
    if len(derived) > _KEEPALIVE_ID_MAX or not re.fullmatch(r"[A-Za-z0-9_-]+", derived):
        return None
    return derived


def per_session_path(url: str, session_id: str) -> tuple[str, str | None]:
    """``/k/<machine>/rest`` with the machine id made per-session; returns (url, derived id).

    A path without a ``/k/`` id, or a request without a session header, is
    returned unchanged with ``None``.
    """
    match = _KEEPALIVE_SEGMENT.match(url)
    if match is None or not session_id:
        return url, None
    derived = session_keepalive_id(match.group(1), session_id)
    if derived is None:
        return url, None
    return f"/k/{derived}{url[match.end() :]}", derived


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


def request_allowed(headers: Any, port: int) -> bool:
    """Whether a request may reach the relay, which holds the user's credential.

    A web page can make a cross-origin request to this loopback port, and a
    DNS-rebinding page can make a name it controls resolve to 127.0.0.1. Both
    would spend the user's key, so:

    * any request carrying an ``Origin`` header is refused. Native CLIs (Claude
      Code, Codex, the VS Code extension host) do not send one; browsers always
      do on cross-origin requests;
    * when ``port`` is known, the ``Host`` must be ``127.0.0.1:<port>`` or
      ``localhost:<port>``, so a rebinding name is refused.

    ``port=0`` (programmatic use, tests without a bound port) skips only the
    Host check; the Origin refusal always applies.
    """
    if headers.get("origin") is not None:
        return False
    if not port:
        return True
    host = str(headers.get("host") or "").strip().lower()
    return host in {f"127.0.0.1:{port}", f"localhost:{port}"}


def _forwarder_version() -> str:
    from horizon import __version__

    return str(__version__)


def build_app(
    remote_url: str,
    credential_provider: Callable[[], str],
    transport: httpx.AsyncBaseTransport | None = None,
    ws_connect: WsConnect | None = None,
    upstream: str | None = None,
    *,
    port: int = 0,
    stats: ClaudeCodeStats | None = None,
    pause_file: Path | None = None,
    started_at: str | None = None,
) -> FastAPI:
    """Build the relay app.

    ``upstream`` fixes the provider for untagged requests (see module doc):
    inference calls go to the proxy tagged for it, anything else (model lists,
    usage, sign-in) goes straight to it without the key.

    ``credential_provider`` is called once per request (not at startup) so a
    rotated key stored mid-session is picked up without a restart. It should
    return the raw ``hz_...`` key and may raise :class:`horizon.vault.VaultError`.
    ``transport`` and ``ws_connect`` exist for tests (inject fakes).

    ``stats`` turns on the Claude Code path: ``/v1/messages`` calls are tagged
    ``x-horizon-forwarder: claude-code``, their verified-savings comment is
    stripped, and the counters are written. ``pause_file`` makes model calls
    bypass Horizon while the file exists.
    """

    base = remote_url.rstrip("/")
    fixed = parse_upstream(upstream) if upstream else None
    started = started_at or datetime.now(timezone.utc).isoformat(timespec="seconds")
    forwarder_info = {"pid": os.getpid(), "version": _forwarder_version(), "port": port}
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

    # Claude Code session id -> the per-session keep-alive ids its messages were
    # relayed under; the session-end route ends those on the proxy.
    session_liveness: OrderedDict[str, list[str]] = OrderedDict()

    def remember_liveness(session: str, derived: str) -> None:
        ids = session_liveness.setdefault(session, [])
        if derived not in ids:
            ids.append(derived)
        session_liveness.move_to_end(session)
        while len(session_liveness) > _SESSION_LIVENESS_MAX:
            session_liveness.popitem(last=False)

    async def end_remote_keepalives(ids: list[str]) -> None:
        """Tell the proxy each keep-alive id is over (same call as ``wrap`` makes)."""
        for keepalive in ids:
            try:
                credential = credential_provider()
                resp = await client.post(
                    "/v1/horizon/keepalive/end",
                    json={"id": keepalive},
                    headers={CREDENTIAL_HEADER: credential},
                    timeout=10.0,
                )
                if resp.status_code >= 400:
                    logger.warning("forwarder keep-alive end refused: status=%s", resp.status_code)
            except Exception as exc:  # noqa: BLE001 - a missed end falls back to the idle cap
                logger.warning("forwarder keep-alive end failed: %s", type(exc).__name__)

    async def send(
        http: httpx.AsyncClient,
        request: Request,
        url: str,
        headers: dict,
        path: str,
        recorder: RequestRecorder | None = None,
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

        if recorder is not None:
            recorder.start(upstream_resp.headers.get("content-type", ""))

        async def stream_iter():
            try:
                if upstream_resp.is_stream_consumed:
                    # Fully-buffered upstream (test transports, some gateways
                    # hand back a materialized response): relay the buffer.
                    source = upstream_resp.content
                    relayed = recorder.feed(source) if recorder is not None else source
                    if relayed:
                        yield relayed
                else:
                    async for chunk in upstream_resp.aiter_raw():
                        relayed = recorder.feed(chunk) if recorder is not None else chunk
                        if relayed:
                            yield relayed
                if recorder is not None:
                    tail = recorder.finish_bytes()
                    if tail:
                        yield tail
            finally:
                await upstream_resp.aclose()
                if recorder is not None:
                    # Counted and written on the writer thread after the body
                    # ends: nothing here waits on pricing or disk.
                    recorder.schedule_complete(
                        {k.lower(): v for k, v in upstream_resp.headers.items()},
                        upstream_resp.status_code,
                    )

        return StreamingResponse(
            stream_iter(),
            status_code=upstream_resp.status_code,
            headers=resp_headers,
        )

    async def send_filtered(request: Request, url: str, headers: dict, path: str) -> Response:
        """Direct call whose JSON reply must be edited before the tool sees it.

        IBM Bob 2.0.1-2.0.5 moves its gateway to ``api.<region>:<relay port>``
        when its profile carries ``region_domain``, so the key is removed (the
        hosted proxy does the same for local wraps; see horizon.providers.bob).
        Small, buffered replies only.
        """
        from horizon.providers.bob import strip_origin_passthrough_response_keys

        try:
            upstream_resp = await direct.request(
                request.method,
                url,
                headers=headers,
                content=await request.body() if request.method not in ("GET", "HEAD") else None,
                params=request.query_params,
            )
        except httpx.HTTPError as exc:
            logger.warning("forwarder upstream error path=%s: %s", path, exc)
            return Response(
                content=f"upstream error: {exc}\n", status_code=502, media_type="text/plain"
            )
        content = upstream_resp.content
        drop = set(_HOP_BY_HOP) | {"content-encoding", "content-length"}
        filtered = None
        if upstream_resp.status_code == 200:
            filtered = strip_origin_passthrough_response_keys(url, urlparse(url).path, content)
        if filtered is not None:
            content = filtered
            # They describe the unfiltered bytes.
            drop |= {"etag", "last-modified", "cache-control", "content-digest", "digest"}
        resp_headers = {
            name: value for name, value in upstream_resp.headers.items() if name.lower() not in drop
        }
        return Response(
            content=content, status_code=upstream_resp.status_code, headers=resp_headers
        )

    @app.websocket("/{path:path}")
    async def relay_ws(websocket: WebSocket, path: str) -> None:
        from websockets.exceptions import ConnectionClosed

        from horizon.proxy.ws_headers import WS_HOP_BY_HOP_HEADERS

        if not request_allowed(websocket.headers, port):
            # Refused before accept, so no credential is attached and no relay opens.
            await websocket.close(code=1008, reason="origin or host not allowed")
            return
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
            logger.warning(
                "forwarder websocket upstream refused path=%s status=%s: %s", path, status, exc
            )
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

    @app.get(HEALTH_PATH)
    async def health(request: Request) -> Response:
        # Registered before the catch-all relay, and never relayed. It holds no
        # secret, but a rebinding page must not be able to probe it either.
        if not request_allowed(request.headers, port):
            return Response(content="forbidden\n", status_code=403, media_type="text/plain")
        return JSONResponse(
            {
                "ok": True,
                "version": forwarder_info["version"],
                "pid": forwarder_info["pid"],
                "port": port,
                "remote": base,
                "started_at": started,
            }
        )

    @app.post(SESSION_END_PATH)
    async def session_end(request: Request, background: BackgroundTasks) -> Response:
        """The plugin's session.end hook: this Claude Code session is over.

        Answered at once; the proxy call runs after the reply, so the hook never
        waits on the network. Loopback and Origin/Host rules as for the relay.
        """
        if not request_allowed(request.headers, port):
            return Response(content="forbidden\n", status_code=403, media_type="text/plain")
        raw = await request.body()
        if len(raw) > _SESSION_END_BODY_MAX:
            return Response(content="too large\n", status_code=413, media_type="text/plain")
        try:
            payload = json.loads(raw or b"{}")
        except ValueError:
            payload = None
        session = payload.get("session_id") if isinstance(payload, dict) else None
        if not isinstance(session, str) or not session.strip() or len(session) > 256:
            return Response(content="bad session id\n", status_code=400, media_type="text/plain")
        ids = session_liveness.pop(session.strip(), [])
        if ids:
            background.add_task(end_remote_keepalives, ids)
        return JSONResponse({"accepted": len(ids)}, status_code=202)

    @app.api_route(
        "/{path:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
    )
    async def relay(path: str, request: Request) -> Response:
        if not request_allowed(request.headers, port):
            # Refused before the body is read, and before any credential is used.
            return Response(content="forbidden\n", status_code=403, media_type="text/plain")
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
                from horizon.providers.bob import filters_response

                if filters_response(origin, upstream_path):
                    return await send_filtered(request, f"{origin}{upstream_path}", headers, path)
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

        # Client-sent x-horizon-* headers are internal control flags, so they stop
        # here (as the direct paths above already do). Only the forwarder's own
        # headers are added after this filter.
        headers = {
            name: value
            for name, value in request.headers.items()
            if name.lower() not in _HOP_BY_HOP
            and name.lower() != "host"
            and name.lower() != CREDENTIAL_HEADER
            and not name.lower().startswith("x-horizon-")
        }
        # Transport plugins name the real destination with these two routing tags,
        # and the proxy reads them. They are the only client x-horizon-* headers that
        # pass; every other one was dropped above.
        headers.update(
            {name: request.headers[name] for name in _ROUTING_TAGS if name in request.headers}
        )
        headers.update(tags)
        headers[CREDENTIAL_HEADER] = credential_provider()
        if pause_file is not None and pause_file.exists():
            headers[BYPASS_HEADER] = "true"
        recorder = None
        if url.endswith(_MESSAGES_SUFFIX):
            # One keep-alive group per Claude Code session, not one per machine.
            session = request.headers.get(_SESSION_HEADER, "").strip()
            url, derived = per_session_path(url, session)
            if derived is not None:
                remember_liveness(session, derived)
            headers[FORWARDER_HEADER] = FORWARDER_CLAUDE_CODE
            if stats is not None:
                recorder = await _messages_recorder(stats, request, url, forwarder_info)
        return await send(client, request, url, headers, path, recorder=recorder)

    return app


async def _messages_recorder(
    stats: ClaudeCodeStats,
    request: Request,
    url: str,
    forwarder_info: dict[str, Any],
) -> RequestRecorder:
    """Describe one ``/v1/messages`` call for the stats: session, model, keep-alive.

    Only the ``model`` field is read from the body. The body is not kept.
    """
    model = ""
    try:
        payload = json.loads(await request.body())  # Starlette caches it for send()
        if isinstance(payload, dict):
            model = str(payload.get("model") or "")
    except (ValueError, TypeError):
        model = ""
    keepalive_match = _KEEPALIVE_PATH.match(url)
    keepalive_id = keepalive_match.group(1) if keepalive_match else ""
    session_id = request.headers.get(_SESSION_HEADER, "").strip() or keepalive_id or "unknown"
    return RequestRecorder(
        stats,
        session_id=session_id[:128],
        model=model[:128],
        keepalive=bool(keepalive_id),
        forwarder_info=forwarder_info,
    )


def run_forwarder(
    remote_url: str,
    port: int,
    host: str = "127.0.0.1",
    credential_provider: Callable[[], str] | None = None,
    upstream: str | None = None,
    state_dir: Path | None = None,
    pause_file: Path | None = None,
) -> None:
    """Run the relay (blocking). Binds loopback only.

    ``credential_provider`` defaults to reading the per-user key from the OS
    credential store. Startup fails fast when no credential is stored.

    The Claude Code stats (``status.json``, ``status.txt``) are written under
    ``state_dir`` (default ``%LOCALAPPDATA%\\ContextShrink``) with a heartbeat
    every ``HEARTBEAT_SECONDS``.
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

    root = Path(state_dir) if state_dir else default_state_dir()
    stats = ClaudeCodeStats(claude_code_dir(root), pause_file=pause_file)
    info = {"pid": os.getpid(), "version": _forwarder_version(), "port": port}
    stats.flush(forwarder=info)
    stop = threading.Event()
    threading.Thread(
        target=heartbeat,
        args=(stop, lambda: stats.flush(forwarder=info), HEARTBEAT_SECONDS),
        name="cs-heartbeat",
        daemon=True,
    ).start()

    app = build_app(
        remote_url=remote_url,
        credential_provider=credential_provider,
        upstream=upstream,
        port=port,
        stats=stats,
        pause_file=stats.pause_file,
    )
    try:
        uvicorn.run(app, host=host, port=port, log_level="info")
    finally:
        stop.set()
        stats.close()
