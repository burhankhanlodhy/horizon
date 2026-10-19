"""Wire contract for the "Saved $ (verified)" figure the Claude Code forwarder reads.

The proxy stamps each Anthropic ``/v1/messages`` response with the request's
cache-aware saving (the figure the savings ledger records), settled with its
cache keep-alive: plus the rewrite a resumed session avoided, minus the pings
its gap cost. That figure is signed, because pings that did not pay off are a
real cost; the value is written with its sign. It does so only when
the request carries ``x-horizon-forwarder: claude-code``, which the local
forwarder adds. A streamed response has its headers sent before the saving is
known, so the proxy appends one SSE comment just before the stream closes; a
non-streamed response carries the same value as a response header.

The forwarder strips the comment before the client sees it, so Claude Code's
SSE parser never observes it. This module is the one place that spells the
names, so the proxy and the forwarder cannot drift apart. It imports nothing
heavy: the forwarder must stay a light process.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

#: Request header the forwarder adds to every relayed ``/v1/messages`` call.
FORWARDER_HEADER = "x-horizon-forwarder"
#: The only value that enables the verified figure.
FORWARDER_CLAUDE_CODE = "claude-code"

#: Response header (non-streaming) and SSE comment name (streaming).
VERIFIED_SAVINGS_HEADER = "x-horizon-verified-savings-usd"

#: The SSE comment line prefix. A comment line per the SSE spec starts with a colon.
SSE_COMMENT_PREFIX = f": {VERIFIED_SAVINGS_HEADER}="


def is_claude_code_forwarder(headers: Mapping[str, Any] | None) -> bool:
    """True when the request came through the Claude Code forwarder."""
    if not headers:
        return False
    value = headers.get(FORWARDER_HEADER)
    if value is None:
        return False
    return str(value).strip().lower() == FORWARDER_CLAUDE_CODE


def format_verified_usd(usd: float) -> str:
    """The figure as the header/comment carries it: a plain signed decimal, 6 places."""
    value = round(float(usd), 6)
    if value == 0.0:
        value = 0.0  # no "-0.000000" for a zero that came out negative
    return f"{value:.6f}"


def sse_comment_line(usd: float) -> bytes:
    """The one SSE comment line (with its blank-line terminator) the proxy appends."""
    return f"{SSE_COMMENT_PREFIX}{format_verified_usd(usd)}\n\n".encode("ascii")


def parse_sse_comment_value(line: bytes) -> float | None:
    """The signed value of a verified-savings comment line, or ``None`` if it is not one.

    ``line`` is one complete line without its terminator (``\\r\\n`` or ``\\n``
    already removed or still present; both are accepted).
    """
    text = line.rstrip(b"\r\n")
    if not text.startswith(SSE_COMMENT_PREFIX.encode("ascii")):
        return None
    raw = text[len(SSE_COMMENT_PREFIX) :].decode("ascii", errors="replace").strip()
    try:
        value = float(raw)
    except ValueError:
        return None
    if value != value or value in (float("inf"), float("-inf")):  # NaN or infinite
        return None
    return value
