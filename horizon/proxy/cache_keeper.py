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

Which request: per group, the most recent completed request that carried tools,
cache markers and a context of at least ``min_context`` tokens. At idle that is
the agent's main conversation, not a title or side call. A group is the
liveness id ``wrap`` attaches (scoped to the verified account on a hosted
proxy, where a request without one is never kept warm), else, on a local proxy,
a hash of the provider credential. How long: while the expected saving stays
positive. A rewrite costs ``write - read`` per token and each ping ``read``, so
with a ``return_odds`` chance the user resumes, pinging pays for
``return_odds * (write - read) / read`` pings: about 9 on the 1-hour lane
(~8 hours) and 5 on the 5-minute lane (~20 minutes). A ping that misses (had to
write, or read nothing) stops the group: the cache was already gone. So does a
window the scheduler missed: a late ping would only pay to rebuild it.

Measurement: every ping is billed to its owner as it happens (``reporter``),
whether or not the user ever returns, so abandoned sessions count against the
feature. A request that resumes a group after a gap longer than the entry's
lifetime, and still reads the cache, records the rewrite it avoided. Credentials
stay in memory only and are dropped when a group stops or ends.
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
# Too close to expiry to be sure the ping lands first: treat the window as missed.
LATE = 5
WRITE_MULTIPLIER = {TTL_5M: 1.25, TTL_1H: 2.0}
READ_MULTIPLIER = 0.1
LIVENESS_HEADER = "x-horizon-keepalive-id"

# (status, usage) for one pre-warm request; usage is the response's usage block.
Sender = Callable[[str, dict[str, str], dict[str, Any]], Awaitable[tuple[int, dict[str, Any]]]]
# (owner, ping record): bills one ping to the account that owns the session.
Reporter = Callable[[Any, dict[str, Any]], Awaitable[None]]


def ttl_of(body: dict[str, Any]) -> int | None:
    """Lifetime of the shortest cache entry this body writes; ``None`` if it writes none.

    The shortest entry is the one that expires first: with 1-hour tools and
    5-minute messages, the conversation itself lives five minutes.
    """
    ttls: list[int] = []

    def visit(node: Any) -> None:
        if isinstance(node, dict):
            cc = node.get("cache_control")
            if isinstance(cc, dict) and cc.get("type", "ephemeral") == "ephemeral":
                ttls.append(TTL_1H if cc.get("ttl") == "1h" else TTL_5M)
            for value in node.values():
                if isinstance(value, (dict, list)):
                    visit(value)
        elif isinstance(node, list):
            for value in node:
                visit(value)

    # Top-level ``cache_control`` is automatic caching: it marks the last block.
    visit(
        [
            {"cache_control": body.get("cache_control")},
            body.get("tools"),
            body.get("system"),
            body.get("messages"),
        ]
    )
    return min(ttls) if ttls else None


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


def group_of(
    headers: dict[str, str], liveness_id: str | None = None, owner_id: str | None = None
) -> str:
    """Owner-scoped liveness id when ``wrap`` sent one, else a one-way hash of the credential.

    Hashing the owner in means two accounts that send the same id never share a
    group, and an end request can only reach its own account's sessions.
    """
    if liveness_id:
        scoped = f"{owner_id or ''}\0{liveness_id[:200]}"
        return "live:" + hashlib.sha256(scoped.encode("utf-8", "ignore")).hexdigest()[:24]
    lowered = {k.lower(): v for k, v in headers.items()}
    credential = lowered.get("x-api-key") or lowered.get("authorization") or ""
    return "cred:" + hashlib.sha256(credential.encode("utf-8", "ignore")).hexdigest()[:24]


@dataclass
class _Pending:
    group: str
    url: str
    headers: dict[str, str]
    body: dict[str, Any]
    started: float
    owner: Any
    meta: dict[str, Any]


@dataclass
class _Group:
    key: str
    owner: Any = None  # who pays for the pings (the verified account), if anyone
    meta: dict[str, Any] = field(default_factory=dict)  # project / agent for attribution
    url: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    body: dict[str, Any] = field(default_factory=dict)  # emptied once the group stops pinging
    model: str = ""
    ttl: int = TTL_5M
    context_tokens: int = 0
    last_request_at: float = 0.0  # start of the last real request (monotonic)
    last_touch_at: float = 0.0  # start of the last request or ping that read the cache
    generation: int = 0  # bumped whenever a newer request becomes the target
    pings: int = 0
    ping_cost_usd: float = 0.0
    inflight: bool = False
    stopped: bool = False  # the cache is gone (or unknown): never credit a resume
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
        reporter: Reporter | None = None,
        min_context: int = 20_000,
        return_odds: float = 0.5,
        max_idle_seconds: float = 8 * 3600,
        max_groups: int = 256,
        max_context_tokens: int = 25_000_000,
        concurrency: int = 8,
        ping_timeout: float = 60.0,
        ledger_path: Path | None = None,
    ) -> None:
        self._send = sender
        self._clock = clock
        self._reporter = reporter
        self.min_context = min_context
        self.return_odds = return_odds
        self.max_idle_seconds = max_idle_seconds
        self._max_groups = max_groups
        # Bounds the request bodies held for pinging (~4 bytes a token).
        self._max_context_tokens = max_context_tokens
        self._ping_timeout = ping_timeout
        self._concurrency = concurrency
        self._groups: OrderedDict[str, _Group] = OrderedDict()
        self._pending: OrderedDict[str, _Pending] = OrderedDict()
        self._ended: OrderedDict[str, None] = OrderedDict()
        self._ledger_path = ledger_path
        self._tasks: set[asyncio.Task] = set()

    # -- real traffic -------------------------------------------------------

    def record_request(
        self,
        request_id: str,
        *,
        url: str,
        headers: dict[str, str],
        body: dict[str, Any],
        liveness_id: str | None = None,
        owner: Any = None,
        meta: dict[str, Any] | None = None,
    ) -> None:
        """Remember a forwarded request until its usage says whether it is worth keeping.

        ``headers`` are the ones sent upstream (the proxy strips its own
        ``x-horizon-*`` headers first), so the caller passes the liveness id
        from the client's request separately. ``owner`` is the verified account
        on a hosted proxy (anything with ``user_id``): its sessions are kept
        warm only when the client sent a liveness id, which is also what lets
        the client end them, and never once its plan has paused savings.
        """
        if not body.get("tools") or body.get("max_tokens") == 0:
            return  # side calls (titles, quick questions) and our own pings
        if ttl_of(body) is None:
            return  # nothing cached, nothing to keep warm
        if owner is not None and (
            not liveness_id or getattr(owner, "compression_allowed", True) is False
        ):
            return
        group = group_of(headers, liveness_id, getattr(owner, "user_id", None))
        if group in self._ended:
            return
        self._pending[request_id] = _Pending(
            group, url, dict(headers), body, self._clock(), owner, dict(meta or {})
        )
        while len(self._pending) > 64:
            self._pending.popitem(last=False)

    def record_usage(
        self, request_id: str, *, model: str, cache_read: int, cache_write: int, uncached: int
    ) -> dict[str, Any] | None:
        """Adopt a completed request as its group's warm target; report a kept-warm resume."""
        p = self._pending.pop(request_id, None)
        if p is None or p.group in self._ended:
            return None
        context = cache_read + cache_write + uncached
        if context < self.min_context:
            return None
        g = self._groups.get(p.group)
        if g is not None and p.started < g.last_request_at:
            return None  # finished after a newer request: that one is the target
        event = None
        if g is not None and g.pings and not g.stopped:
            idle = p.started - g.last_request_at
            if idle > g.ttl and cache_read >= 0.5 * context:
                event = self._resume_event(g, idle, cache_read)
        if g is None:
            g = _Group(key=p.group, baseline_read=cache_read)
            self._groups[p.group] = g
        g.owner, g.meta = p.owner, p.meta
        g.url, g.headers, g.body, g.model = p.url, p.headers, copy.deepcopy(p.body), model
        g.ttl = ttl_of(p.body) or TTL_5M
        g.tools_tokens = len(json.dumps(p.body.get("tools") or [])) // 4
        g.context_tokens = context
        g.last_request_at = g.last_touch_at = p.started
        g.generation += 1
        g.pings, g.ping_cost_usd = 0, 0.0
        g.stopped = False
        self._groups.move_to_end(p.group)
        self._enforce_limits()
        return event

    def end(self, liveness_id: str, owner_id: str | None = None) -> bool:
        """The wrapped tool exited: stop keeping its session warm, for good."""
        key = group_of({}, liveness_id, owner_id)
        self._ended[key] = None
        while len(self._ended) > 4096:
            self._ended.popitem(last=False)
        for request_id in [r for r, p in self._pending.items() if p.group == key]:
            del self._pending[request_id]
        g = self._groups.pop(key, None)
        if g is not None:
            self._log({"event": "cache_keeper_end", "group": key[:16], "pings": g.pings})
        return g is not None

    def _enforce_limits(self) -> None:
        while len(self._groups) > self._max_groups:
            self._groups.popitem(last=False)
        held = sum(g.context_tokens for g in self._groups.values() if g.body)
        for g in list(self._groups.values()):  # oldest first
            if held <= self._max_context_tokens:
                break
            if g.body:
                held -= g.context_tokens
                self._release(g, "memory")

    def _release(self, g: _Group, reason: str) -> None:
        """Stop pinging ``g`` and drop its request and credentials; keep its accounting."""
        if not g.body:
            return
        g.body, g.headers = {}, {}
        self._log(
            {
                "event": "cache_keeper_release",
                "group": g.key[:16],
                "reason": reason,
                "pings": g.pings,
            }
        )

    # -- pinging --------------------------------------------------------------

    def max_pings(self, ttl: int) -> int:
        return int(self.return_odds * (WRITE_MULTIPLIER[ttl] - READ_MULTIPLIER) / READ_MULTIPLIER)

    def due(self) -> list[_Group]:
        now = self._clock()
        out = []
        for g in list(self._groups.values()):
            if g.stopped or not g.body or g.inflight:
                continue
            if now - g.last_request_at > self.max_idle_seconds:
                self._release(g, "idle")
            elif g.pings >= self.max_pings(g.ttl):
                self._release(g, "budget")
            elif now >= g.last_touch_at + g.ttl - LATE:
                g.stopped = True  # the scheduler fell behind; a ping now would rebuild
                self._release(g, "missed")
            elif now >= g.last_touch_at + g.ttl - MARGINS[g.ttl]:
                out.append(g)
        return out

    async def tick(self) -> int:
        """Send every due pre-warm, a few at a time; returns how many were sent."""
        due = self.due()
        for g in due:
            g.inflight = True
        slots = asyncio.Semaphore(self._concurrency)
        results = await asyncio.gather(*(self._ping(g, slots) for g in due))
        return sum(results)

    async def run(self, interval: float = 15.0) -> None:
        """Tick forever. Each tick runs as its own task, so one slow ping never
        holds up another session's deadline (in-flight groups are skipped)."""
        try:
            while True:
                task = asyncio.create_task(self._safe_tick())
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)
                await asyncio.sleep(interval)
        finally:
            for task in list(self._tasks):
                task.cancel()

    async def _safe_tick(self) -> None:
        try:
            await self.tick()
        except Exception:  # pragma: no cover - the loop must survive anything
            logger.exception("event=cache_keeper_tick_failed")

    async def _ping(self, g: _Group, slots: asyncio.Semaphore) -> int:
        """One pre-warm for ``g``; bills it whatever happens and stops the group on a miss."""
        try:
            body = prewarm_body(g.body)
            if body is None:
                g.stopped = True
                self._release(g, "unsupported")
                return 0
            generation, owner, meta, model, ttl = g.generation, g.owner, g.meta, g.model, g.ttl
            rates = _rates(model)
            started = self._clock()
            status, usage, estimated = 0, {}, False
            try:
                async with slots:
                    status, usage = await asyncio.wait_for(
                        self._send(g.url, g.headers, body), self._ping_timeout
                    )
            except Exception as exc:  # timeout or network error: the outcome is unknown
                logger.warning(
                    "event=cache_keeper_ping_error group=%s error=%s",
                    g.key[:16],
                    type(exc).__name__,
                )
                # The provider may have billed it: book a full read of the context.
                usage = {"cache_read_input_tokens": g.context_tokens}
                estimated = True
            read = int(usage.get("cache_read_input_tokens") or 0)
            write = int(usage.get("cache_creation_input_tokens") or 0)
            uncached = int(usage.get("input_tokens") or 0)
            write_rate = rates["w1h"] if ttl == TTL_1H else rates["w5m"]
            cost = read * rates["read"] + write * write_rate + uncached * rates["input"]
            if status and status != 200:
                cost = 0.0  # rejected requests are not billed
            current = self._groups.get(g.key) is g and g.generation == generation
            missed = estimated or status != 200 or write > 0 or read == 0
            record = {
                "event": "cache_keeper_stop" if missed else "cache_keeper_ping",
                "group": g.key[:16],
                "model": model,
                "ttl": ttl,
                "status": status,
                "read": read,
                "write": write,
                "uncached": uncached,
                "cost_usd": round(cost, 6),
                "estimated": estimated,
                "pricing_basis": rates["basis"],
                "latency_ms": round((self._clock() - started) * 1000, 1),
                "idle_s": round(started - g.last_request_at),
                "superseded": not current,
                "meta": meta,
            }
            if current:
                if missed:
                    g.stopped = True
                    self._release(g, "miss")
                else:
                    g.pings += 1
                    g.ping_cost_usd += cost
                    g.last_touch_at = started
                record["ping"] = g.pings
            self._log({k: v for k, v in record.items() if k != "meta"})
            await self._report(owner, record)
            return 1
        finally:
            g.inflight = False

    async def _report(self, owner: Any, record: dict[str, Any]) -> None:
        if self._reporter is None or owner is None:
            return
        try:
            await self._reporter(owner, record)
        except Exception as exc:  # billing must never break the keeper
            logger.warning("event=cache_keeper_report_failed error=%s", type(exc).__name__)

    # -- accounting -----------------------------------------------------------

    def _resume_event(self, g: _Group, idle: float, cache_read: int) -> dict[str, Any]:
        rates = _rates(g.model)
        write_rate = rates["w1h"] if g.ttl == TTL_1H else rates["w5m"]
        kept = max(
            0, cache_read - max(g.baseline_read, g.tools_tokens)
        )  # only what would have expired
        avoided = kept * (write_rate - rates["read"])
        spent = g.ping_cost_usd
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
            "pricing_basis": rates["basis"],
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


def _rates(model: str) -> dict[str, Any]:
    """Per-token read / 5-minute write / 1-hour write / uncached rates for ``model``."""
    try:
        from horizon.pricing.counterfactual import resolve_rates

        r = resolve_rates(model, long_context=False, provider="anthropic")
        if r is not None:
            return {
                "read": r.read,
                "w5m": r.write_5m,
                "w1h": r.write_1h,
                "input": r.uncached,
                "basis": r.basis,
            }
    except Exception:  # pragma: no cover - pricing must never break the keeper
        pass
    base = 3e-6  # unknown model: a mid-range list price, labelled as such
    return {
        "read": base * READ_MULTIPLIER,
        "w5m": base * 1.25,
        "w1h": base * 2,
        "input": base,
        "basis": "fallback",
    }


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
