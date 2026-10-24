"""Process-wide hooks for the local ContextShrink Code engine.

The engine (``horizon code-engine run``) is the ordinary local proxy with two
extra switches: a license gate that decides whether compression runs, and a
usage sink that counts what compression did. Nothing is installed anywhere
else, so every hook below is a no-op and the proxy behaves as before.

The gate reuses the account pass-through path. While it is closed,
``horizon.proxy.account_analytics.compression_paused()`` is true, and every
compression path serves the request unchanged through
``horizon.proxy.helpers._horizon_bypass_enabled``.

This module imports nothing heavy, so the request path can depend on it.
"""

from __future__ import annotations

from collections.abc import Callable
from contextvars import ContextVar

_license_allows: Callable[[], bool] | None = None
_usage_sink: Callable[[str, int, int], None] | None = None
_health: Callable[[], dict] | None = None

# The canonical path of the request being served (after the /p and /k prefixes are
# stripped). Bound by the ingress middlewares in horizon.proxy.server, so the usage
# meter can tell a real generation from an auxiliary route at the outcome funnel.
_request_path: ContextVar[str | None] = ContextVar("horizon_engine_request_path", default=None)

# Routes whose served requests the engine meters. Only real generation counts:
# token counting, batches, model listing, compaction and every other auxiliary route
# are deliberately absent. The Responses roots and WebSocket paths mirror
# horizon.providers.openai_responses (a test keeps the two in step).
_CLAUDE_METERED_PATHS = frozenset({"/v1/messages"})
_CODEX_METERED_PATHS = frozenset(
    {
        "/v1/responses",
        "/v1/codex/responses",
        "/backend-api/responses",
        "/backend-api/codex/responses",
        "/responses",
    }
)


def install(
    *,
    license_allows: Callable[[], bool],
    usage_sink: Callable[[str, int, int], None],
    health: Callable[[], dict],
) -> None:
    """Attach the engine. Called once, before the proxy app is created."""
    global _license_allows, _usage_sink, _health
    _license_allows = license_allows
    _usage_sink = usage_sink
    _health = health


def reset() -> None:
    """Detach the engine (tests)."""
    global _license_allows, _usage_sink, _health
    _license_allows = None
    _usage_sink = None
    _health = None


def license_denied() -> bool:
    """True when an installed license gate is closed. False when none is installed."""
    check = _license_allows
    return check is not None and not check()


def bind_request_path(path: str | None) -> None:
    """Record the canonical path of the request now being served."""
    _request_path.set(path)


def request_path() -> str | None:
    """The bound request path, or ``None`` outside a request."""
    return _request_path.get()


def metered_tool(path: str | None, *, provider: str, client: str | None) -> str | None:
    """The usage tool for a served request on ``path``, or ``None`` when it is not metered.

    ``claude`` is Anthropic Messages (``POST /v1/messages``). ``codex`` is a Responses
    turn from the Codex client on one of the Responses root paths, HTTP or WebSocket.
    """
    if path in _CLAUDE_METERED_PATHS and provider == "anthropic":
        return "claude"
    if path in _CODEX_METERED_PATHS and provider == "openai" and client == "codex":
        return "codex"
    return None


def record_usage(tool: str, tokens_before: int, tokens_after: int) -> None:
    """Count one served request for the engine's usage report, if one is installed."""
    sink = _usage_sink
    if sink is not None:
        sink(tool, tokens_before, tokens_after)


def health_payload() -> dict | None:
    """The engine's ``/__cs/health`` body, or ``None`` when no engine is installed."""
    probe = _health
    return probe() if probe is not None else None
