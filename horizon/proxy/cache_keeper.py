"""Keep a coding session's prompt cache warm through idle gaps (Anthropic).

An agent session re-reads its whole context on every request, at the cache-read
rate (0.1x input) while the provider's cache entry lives. When the user pauses
longer than the entry's lifetime (5 minutes, or 1 hour on the extended lane),
the next request writes the whole context again at 1.25x or 2x input. On a real
1,332-request Opus session those rewrites were 23% of the bill; a 500k-token
context costs about $4 to rewrite and $0.10 to re-read.

Reading a cached prefix refreshes its lifetime at no extra cost, and a
``max_tokens: 0`` request bills only that read (Anthropic, "Pre-warming the
cache"). So while a session is idle this re-sends its last forwarded request as
a pre-warm shortly before the entry would expire. It never changes what the
user's own requests contain.

Which request: per group (the liveness id ``wrap`` attaches, else a hash of the
credential), the most recent request that carried tools and a context of at
least ``min_context`` tokens. At idle that is the agent's main conversation, not
a finished subagent or a title/side call. How long: while the expected saving
stays positive. A rewrite costs ``write - read`` per token and each ping
``read``, so with a ``return_odds`` chance the user resumes, pinging pays for
``return_odds * (write - read) / read`` pings: about 9 on the 1-hour lane
(~8 hours) and 5 on the 5-minute lane (~20 minutes). A ping that misses (it had
to write the prefix) stops the group: the cache was already gone.

Measurement: a request that resumes a group after a gap longer than the entry's
lifetime, and still reads the cache, records the rewrite it avoided net of the
pings spent. Credentials stay in memory only and are dropped with the group.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import logging
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

TTL_5M, TTL_1H = 300, 3600
# Ping this long before the entry would expire: early enough to absorb a slow
# ping, late enough not to spend pings an active session makes unnecessary.
MARGINS = {TTL_5M: 45, TTL_1H: 300}
WRITE_MULTIPLIER = {TTL_5M: 1.25, TTL_1H: 2.0}
READ_MULTIPLIER = 0.1
LIVENESS_HEADER = "x-horizon-keepalive-id"

# (status, usage) for one pre-warm request; usage is the response's usage block.
Sender = Callable[[str, dict[str, str], dict[str, Any]], Awaitable[tuple[int, dict[str, Any]]]]


def ttl_of(body: dict[str, Any]) -> int:
    """Lifetime of the entries this body writes: 1 hour if any marker asks for it."""
    found = False

    def visit(node: Any) -> None:
        nonlocal found
        if isinstance(node, dict):
            cc = node.get("cache_control")
            if isinstance(cc, dict) and cc.get("ttl") == "1h":
                found = True
            for value in node.values():
                if isinstance(value, (dict, list)):
                    visit(value)
        elif isinstance(node, list):
            for value in node:
                visit(value)

    visit([body.get("tools"), body.get("system"), body.get("messages")])
    return TTL_1H if found else TTL_5M


def prewarm_body(body: dict[str, Any]) -> dict[str, Any] | None:
    """The pre-warm form of ``body``, or ``None`` when the API would reject it.

    ``max_tokens: 0`` is refused with streaming, manual extended thinking,
    structured outputs, or a forced tool choice. Streaming is switched off here
    (it is not part of the cached prompt); the others change the prompt, so the
    request is skipped rather than altered into a cache miss.
    """
    thinking = body.get("thinking")
    if isinstance(thinking, dict) and thinking.get("type") == "enabled":
        return None
    output_config = body.get("output_config")
    if isinstance(output_config, dict) and output_config.get("format"):
        return None
    tool_choice = body.get("tool_choice")
    if isinstance(tool_choice, dict) and tool_choice.get("type") in ("any", "tool"):
        return None
    warm = copy.deepcopy(body)
    warm["max_tokens"] = 0
    warm["stream"] = False
    return warm


def group_of(headers: dict[str, str], liveness_id: str | None = None) -> str:
    """Liveness id when ``wrap`` sent one, else a one-way hash of the credential."""
    if liveness_id:
        return f"live:{liveness_id[:100]}"
    lowered = {k.lower(): v for k, v in headers.items()}
    credential = lowered.get("x-api-key") or lowered.get("authorization") or ""
    return "cred:" + hashlib.sha256(credential.encode("utf-8", "ignore")).hexdigest()[:24]


@dataclass
class _Group:
    key: str
    url: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    body: dict[str, Any] = field(default_factory=dict)
    model: str = ""
    ttl: int = TTL_5M
    context_tokens: int = 0
    last_request_at: float = 0.0  # start of the last real request (monotonic)
    last_touch_at: float = 0.0  # start of the last request or ping that read the cache
    pings: int = 0
    ping_read_tokens: int = 0
    stopped: bool = False
    # Not credited on resume: what the group's first request already found warm,
    # or at least the tool definitions, which other sessions on the account
    # (same tools, any project) commonly keep warm. A resume would read those anyway.
    baseline_read: int = 0
    tools_tokens: int = 0


class CacheKeeper:
    """Tracks idle sessions and pre-warms their cache before it expires."""

    def __init__(
        self,
        sender: Sender,
        *,
        clock: Callable[[], float] = time.monotonic,
        min_context: int = 20_000,
        return_odds: float = 0.5,
        max_idle_seconds: float = 8 * 3600,
        max_groups: int = 256,
        ledger_path: Path | None = None,
    ) -> None:
        self._send = sender
        self._clock = clock
        self.min_context = min_context
        self.return_odds = return_odds
        self.max_idle_seconds = max_idle_seconds
        self._max_groups = max_groups
        self._groups: OrderedDict[str, _Group] = OrderedDict()
        self._pending: OrderedDict[str, tuple[str, str, dict[str, str], dict[str, Any], float]] = (
            OrderedDict()
        )
        self._ledger_path = ledger_path
        self._ended: set[str] = set()

    # -- real traffic -------------------------------------------------------

    def record_request(
        self,
        request_id: str,
        *,
        url: str,
        headers: dict[str, str],
        body: dict[str, Any],
        liveness_id: str | None = None,
    ) -> None:
        """Remember a forwarded request until its usage says whether it is worth keeping.

        ``headers`` are the ones sent upstream (the proxy strips its own
        ``x-horizon-*`` headers first), so the caller passes the liveness id
        from the client's request separately.
        """
        if not body.get("tools") or body.get("max_tokens") == 0:
            return  # side calls (titles, quick questions) and our own pings
        group = group_of(headers, liveness_id)
        if group in self._ended:
            return
        self._pending[request_id] = (group, url, dict(headers), body, self._clock())
        while len(self._pending) > 64:
            self._pending.popitem(last=False)

    def record_usage(
        self, request_id: str, *, model: str, cache_read: int, cache_write: int, uncached: int
    ) -> dict[str, Any] | None:
        """Adopt a completed request as its group's warm target; report a kept-warm resume."""
        pending = self._pending.pop(request_id, None)
        if pending is None:
            return None
        group_key, url, headers, body, started = pending
        context = cache_read + cache_write + uncached
        if context < self.min_context:
            return None
        g = self._groups.get(group_key)
        event = None
        if g is not None and g.pings and not g.stopped:
            idle = started - g.last_request_at
            if idle > g.ttl and cache_read >= 0.5 * context:
                event = self._resume_event(g, idle, cache_read)
        if g is None:
            g = _Group(key=group_key, baseline_read=cache_read)
            self._groups[group_key] = g
        g.url, g.headers, g.body, g.model = url, headers, copy.deepcopy(body), model
        g.ttl = ttl_of(body)
        g.tools_tokens = len(json.dumps(body.get("tools") or [])) // 4
        g.context_tokens = context
        g.last_request_at = g.last_touch_at = started
        g.pings = g.ping_read_tokens = 0
        g.stopped = False
        self._groups.move_to_end(group_key)
        while len(self._groups) > self._max_groups:
            self._groups.popitem(last=False)
        return event

    def end(self, liveness_id: str) -> bool:
        """The wrapped tool exited: stop keeping its session warm."""
        key = f"live:{liveness_id[:100]}"
        self._ended.add(key)
        return self._groups.pop(key, None) is not None

    # -- pinging --------------------------------------------------------------

    def max_pings(self, ttl: int) -> int:
        return int(self.return_odds * (WRITE_MULTIPLIER[ttl] - READ_MULTIPLIER) / READ_MULTIPLIER)

    def due(self) -> list[_Group]:
        now = self._clock()
        out = []
        for g in self._groups.values():
            if g.stopped or not g.body:
                continue
            if now - g.last_request_at > self.max_idle_seconds or g.pings >= self.max_pings(g.ttl):
                continue
            if now >= g.last_touch_at + g.ttl - MARGINS[g.ttl]:
                out.append(g)
        return out

    async def tick(self) -> int:
        """Send every due pre-warm; returns how many were sent."""
        sent = 0
        for g in self.due():
            body = prewarm_body(g.body)
            if body is None:
                g.stopped = True
                continue
            started = self._clock()
            try:
                status, usage = await self._send(g.url, g.headers, body)
            except Exception as exc:  # network errors: try again next tick, never raise
                logger.warning(
                    "event=cache_keeper_ping_error group=%s error=%s",
                    g.key[:16],
                    type(exc).__name__,
                )
                continue
            sent += 1
            read = int(usage.get("cache_read_input_tokens") or 0)
            write = int(usage.get("cache_creation_input_tokens") or 0)
            if status != 200 or write > max(read, 1):
                # Rejected, or the cache was already gone and this ping rebuilt it.
                g.stopped = True
                self._log(
                    {
                        "event": "cache_keeper_stop",
                        "group": g.key[:16],
                        "status": status,
                        "read": read,
                        "write": write,
                        "pings": g.pings,
                    }
                )
                continue
            g.pings += 1
            g.ping_read_tokens += read
            g.last_touch_at = started
            self._log(
                {
                    "event": "cache_keeper_ping",
                    "group": g.key[:16],
                    "model": g.model,
                    "ttl": g.ttl,
                    "read": read,
                    "ping": g.pings,
                    "idle_s": round(started - g.last_request_at),
                }
            )
        return sent

    async def run(self, interval: float = 15.0) -> None:
        while True:
            try:
                await self.tick()
            except Exception:  # pragma: no cover - the loop must survive anything
                logger.exception("event=cache_keeper_tick_failed")
            await asyncio.sleep(interval)

    # -- accounting -----------------------------------------------------------

    def _resume_event(self, g: _Group, idle: float, cache_read: int) -> dict[str, Any]:
        rates = _rates(g.model)
        write_rate = rates["w1h"] if g.ttl == TTL_1H else rates["w5m"]
        kept = max(
            0, cache_read - max(g.baseline_read, g.tools_tokens)
        )  # only what would have expired
        avoided = kept * (write_rate - rates["read"])
        spent = g.ping_read_tokens * rates["read"]
        event = {
            "event": "cache_keeper_resume",
            "group": g.key[:16],
            "model": g.model,
            "ttl": g.ttl,
            "idle_s": round(idle),
            "pings": g.pings,
            "cache_read": cache_read,
            "kept_tokens": kept,
            "avoided_usd": round(avoided, 6),
            "ping_cost_usd": round(spent, 6),
            "net_usd": round(avoided - spent, 6),
        }
        self._log(event)
        return event

    def _log(self, event: dict[str, Any]) -> None:
        event = {"ts": time.time(), **event}
        logger.info("event=%s %s", event["event"], json.dumps(event, sort_keys=True))
        if self._ledger_path is not None:
            try:
                with self._ledger_path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(event) + "\n")
            except OSError:
                pass


def _rates(model: str) -> dict[str, float]:
    """Per-token read / 5-minute write / 1-hour write rates for ``model``."""
    try:
        from horizon.pricing.counterfactual import resolve_rates

        r = resolve_rates(model, long_context=False, provider="anthropic")
        if r is not None:
            return {"read": r.read, "w5m": r.write_5m, "w1h": r.write_1h}
    except Exception:  # pragma: no cover - pricing must never break the keeper
        pass
    base = 3e-6
    return {"read": base * READ_MULTIPLIER, "w5m": base * 1.25, "w1h": base * 2}


# -- 1-hour lane ---------------------------------------------------------------

TTL_UPGRADE_ENV = "HORIZON_CACHE_TTL_UPGRADE"
EXTENDED_TTL_BETA = "extended-cache-ttl-2025-04-11"


def ttl_upgrade_enabled() -> bool:
    import os

    return os.environ.get(TTL_UPGRADE_ENV, "").strip().lower() == "1h"


def upgrade_cache_ttl_to_1h(body: dict[str, Any]) -> int:
    """Put every 5-minute cache_control marker on the 1-hour lane; returns how many changed.

    Claude Code writes 5-minute entries for API-key users. Any pause past five
    minutes then rewrites the whole context (1.25x input) instead of reading it
    (0.1x); a 1-hour entry costs 2x once and survives ordinary pauses. Every
    marker moves together, which keeps Anthropic's rule that longer lifetimes
    precede shorter ones.
    """
    changed = 0

    def visit(node: Any) -> None:
        nonlocal changed
        if isinstance(node, dict):
            cc = node.get("cache_control")
            if (
                isinstance(cc, dict)
                and cc.get("type") == "ephemeral"
                and cc.get("ttl") in (None, "5m")
            ):
                node["cache_control"] = {**cc, "ttl": "1h"}
                changed += 1
            for value in node.values():
                if isinstance(value, (dict, list)):
                    visit(value)
        elif isinstance(node, list):
            for value in node:
                visit(value)

    for section in ("tools", "system", "messages"):
        visit(body.get(section))
    return changed


def add_beta(headers: dict[str, str], beta: str) -> None:
    """Append ``beta`` to the anthropic-beta header (any casing), once."""
    key = next((k for k in headers if k.lower() == "anthropic-beta"), None)
    if key is None:
        headers["anthropic-beta"] = beta
        return
    values = [v.strip() for v in headers[key].split(",") if v.strip()]
    if beta not in values:
        headers[key] = ",".join([*values, beta])
