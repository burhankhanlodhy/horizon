"""Fast-mode governor: keep the 2x fast-mode premium for people who are waiting.

Opus fast mode (``speed: "fast"``) bills $8 / $40 per MTok on Opus 5.5 against
$4 / $20 at standard speed. The speed buys lower latency, which is worth paying
for only when a person is watching the reply stream. A headless run (``claude
-p``, the Agent SDK, CI, a GitHub Action) has nobody waiting, so its fast-mode
premium is pure cost (``wiki/plans/2026-10-08-cost-savings-research.md``,
option 4).

Policies (``HORIZON_FAST_MODE_POLICY``):

``allow`` (default)
    Forward ``speed`` untouched.
``headless``
    Drop ``speed: "fast"`` from requests made by a headless client.
``never``
    Drop ``speed: "fast"`` from every request.

CACHE SAFETY
    Switching between fast and standard speed invalidates the cached system
    prompt and messages, so the governor must decide the same way for every
    request of a session. Both signals it reads are fixed for the life of a
    client process: the ``x-horizon-interactive`` header (``wrap`` sets it per
    launch) and the client's User-Agent. A session is therefore either always
    governed or never governed, and the governor itself never causes a miss.

HEADLESS DETECTION
    1. ``x-horizon-interactive: 0`` (or ``false`` / ``no``) marks a request as
       headless; ``1`` marks it interactive and wins over everything else.
    2. Otherwise, Claude Code's User-Agent names its entrypoint in parentheses,
       e.g. ``claude-cli/2.1.291 (external, sdk-cli)``. Entrypoints listed in
       ``HORIZON_FAST_MODE_HEADLESS_ENTRYPOINTS`` (comma-separated; default
       ``sdk-cli,sdk-ts,sdk-py,github-action``) count as headless. The default
       list follows Claude Code's ``CLAUDE_CODE_ENTRYPOINT`` values; confirm it
       against a wire capture of your client version before relying on it.

Never raises; an unparseable request is left alone.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from typing import Any

from horizon.proxy import runtime_env

logger = logging.getLogger(__name__)

POLICY_ENV = "HORIZON_FAST_MODE_POLICY"
#: Fast-mode price over standard, every token class (Opus 5.5: $8 / $40 per
#: MTok against $4 / $20). Prices the premium a dropped ``speed`` saved.
PRICE_MULTIPLIER = 2.0
ENTRYPOINTS_ENV = "HORIZON_FAST_MODE_HEADLESS_ENTRYPOINTS"
INTERACTIVE_HEADER = "x-horizon-interactive"

POLICIES = ("allow", "headless", "never")
DEFAULT_HEADLESS_ENTRYPOINTS = ("sdk-cli", "sdk-ts", "sdk-py", "github-action")

_UA_DETAIL = re.compile(r"\(([^)]*)\)")


def policy() -> str:
    value = (runtime_env.getenv(POLICY_ENV, "allow") or "allow").strip().lower()
    if value not in POLICIES:
        logger.warning("unknown %s=%r; fast mode left untouched", POLICY_ENV, value)
        return "allow"
    return value


def headless_entrypoints() -> frozenset[str]:
    raw = runtime_env.getenv(ENTRYPOINTS_ENV)
    if raw is None:
        return frozenset(DEFAULT_HEADLESS_ENTRYPOINTS)
    return frozenset(p.strip().lower() for p in raw.split(",") if p.strip())


def _header(headers: Mapping[str, str], name: str) -> str:
    value = headers.get(name)
    if value is None:
        lowered = {k.lower(): v for k, v in headers.items()}
        value = lowered.get(name, "")
    return str(value or "").strip().lower()


def entrypoint_of(user_agent: str) -> str:
    """The entrypoint a Claude Code User-Agent names, or ``""``.

    ``claude-cli/2.1.291 (external, sdk-cli)`` -> ``sdk-cli``. The last
    comma-separated field in the first parenthesised group is the entrypoint.
    """
    match = _UA_DETAIL.search(user_agent or "")
    if not match:
        return ""
    fields = [f.strip().lower() for f in match.group(1).split(",") if f.strip()]
    return fields[-1] if fields else ""


def is_headless(headers: Mapping[str, str]) -> bool:
    flag = _header(headers, INTERACTIVE_HEADER)
    if flag in ("0", "false", "no"):
        return True
    if flag in ("1", "true", "yes"):
        return False
    return entrypoint_of(_header(headers, "user-agent")) in headless_entrypoints()


def govern(body: dict[str, Any], headers: Mapping[str, str]) -> str | None:
    """Drop ``speed: "fast"`` when the policy says so; return the reason, else ``None``."""
    try:
        if body.get("speed") != "fast":
            return None
        mode = policy()
        if mode == "allow":
            return None
        if mode == "headless" and not is_headless(headers):
            return None
        body.pop("speed", None)
        return f"fast_mode:dropped:{mode}"
    except Exception:  # pragma: no cover - never fail a request over a price policy
        logger.debug("fast-mode governor failed", exc_info=True)
        return None
