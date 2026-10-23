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

_license_allows: Callable[[], bool] | None = None
_usage_sink: Callable[[str, int, int], None] | None = None
_health: Callable[[], dict] | None = None


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


def record_usage(tool: str, tokens_before: int, tokens_after: int) -> None:
    """Count one served request for the engine's usage report, if one is installed."""
    sink = _usage_sink
    if sink is not None:
        sink(tool, tokens_before, tokens_after)


def health_payload() -> dict | None:
    """The engine's ``/__cs/health`` body, or ``None`` when no engine is installed."""
    probe = _health
    return probe() if probe is not None else None
