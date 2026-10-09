"""Predicted prompt-cache misses caused by the client, priced per cause.

At 2026 prices a cache miss is the most expensive event in an agent session:
Opus / Sonnet 5.5 read the cache at 0.05x input and write it at 1.25x (5-minute
lane) or 2x (1-hour lane), so re-writing a context costs 25-40x reading it. A
300k-token Opus 5.5 miss is $1.50; reading the same context is $0.06.

Anthropic documents what invalidates which part of the cache (prompt caching,
"What invalidates the cache"):

========================  ===============================================
Change                    Invalidated
========================  ===============================================
model                     everything (a new model has no cache entry)
tool definitions          everything (tools render first)
``speed`` (fast mode)     system prompt and messages
system prompt             system prompt and messages
``output_config.effort``  messages (top-level value only; a per-message
                          effort change is an appended system message and
                          keeps the cache on models that support it)
thinking settings         messages
``tool_choice``           messages
an earlier message        that message and everything after it
========================  ===============================================

Clients change these mid-session (``/effort``, ``/fast``, MCP servers
connecting late, ``git status`` in the system prompt, ``--resume`` re-rendering
history). This module compares each forwarded request with the previous one in
the same session and reports a predicted miss with its causes and its price,
``tokens x (write - read)``. It is telemetry only: it never changes a request.
The output is how the dashboard can show which client setting costs money, and
which requests are already going to miss, which is when deferred lossy rewrites
are free (``wiki/plans/2026-10-08-cost-savings-research.md``, options 1 and 6).

THE PREDICTION IS AN ESTIMATE
    Token counts are a 4-characters-per-token estimate, and the price assumes
    the cache was warm (an idle gap longer than the TTL loses the cache anyway;
    that case is the keep-alive's to report, not this module's). Message
    comparison ignores ``cache_control`` markers, which clients move every turn
    without changing the cached content.

Per-session state is a bounded LRU of fingerprints, never request content.
Never raises.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

SCOPE_ALL = "all"
SCOPE_SYSTEM = "system+messages"
SCOPE_MESSAGES = "messages"
_SCOPE_RANK = {SCOPE_MESSAGES: 0, SCOPE_SYSTEM: 1, SCOPE_ALL: 2}

#: (cause, scope it invalidates, how to read it from the body)
_FIELDS: tuple[tuple[str, str, str], ...] = (
    ("model", SCOPE_ALL, "model"),
    ("tools", SCOPE_ALL, "tools"),
    ("speed", SCOPE_SYSTEM, "speed"),
    ("system", SCOPE_SYSTEM, "system"),
    ("effort", SCOPE_MESSAGES, "output_config.effort"),
    ("thinking", SCOPE_MESSAGES, "thinking"),
    ("tool_choice", SCOPE_MESSAGES, "tool_choice"),
)


def _strip_cache_control(node: Any) -> Any:
    if isinstance(node, dict):
        return {k: _strip_cache_control(v) for k, v in node.items() if k != "cache_control"}
    if isinstance(node, list):
        return [_strip_cache_control(v) for v in node]
    return node


def _digest(value: Any) -> str:
    # Key order is the wire order, which a client keeps stable; no sort needed.
    raw = json.dumps(_strip_cache_control(value), ensure_ascii=False, default=str)
    return hashlib.blake2b(raw.encode("utf-8", "ignore"), digest_size=12).hexdigest()


def _get(body: dict[str, Any], path: str) -> Any:
    node: Any = body
    for part in path.split("."):
        if not isinstance(node, dict):
            return None
        node = node.get(part)
    return node


def _tokens(value: Any) -> int:
    if value is None:
        return 0
    try:
        return len(json.dumps(value, ensure_ascii=False, default=str)) // 4
    except (TypeError, ValueError):
        return 0


def session_key(body: dict[str, Any], headers: Any = None) -> str:
    """A session identity that survives model and setting changes.

    Prefers the per-launch id ``wrap`` attaches, then an explicit session
    header, then the first user message (stable for the life of a
    conversation, and different for side calls and subagents).
    """
    parts: list[str] = []
    if headers is not None:
        for name in ("x-horizon-keepalive-id", "x-horizon-session-id"):
            try:
                value = headers.get(name)
            except Exception:
                value = None
            if value:
                parts.append(f"{name}={value}")
                break
    for msg in body.get("messages") or []:
        if isinstance(msg, dict) and msg.get("role") == "user":
            parts.append(_digest(msg.get("content")))
            break
    return _digest(parts) if parts else ""


@dataclass
class PredictedMiss:
    causes: list[str]
    scope: str
    tokens: int
    usd: float
    model: str
    changed_from_message: int | None = None

    def label(self) -> str:
        return "cache_miss:" + "+".join(self.causes)


@dataclass
class _Session:
    fields: dict[str, str] = field(default_factory=dict)
    message_digests: list[str] = field(default_factory=list)


def _write_minus_read(model: str) -> float:
    """Per-token cost of re-writing instead of reading (5-minute lane)."""
    try:
        from horizon.pricing.counterfactual import resolve_rates

        rates = resolve_rates(model, long_context=False, provider="anthropic")
        if rates is not None and rates.write_5m > rates.read:
            return rates.write_5m - rates.read
    except Exception:
        pass
    return 3e-6 * (1.25 - 0.1)  # unpriced model: mid-range list price


class CacheMissWatch:
    """Bounded per-session record of cache-relevant request fingerprints."""

    def __init__(self, max_sessions: int = 2048) -> None:
        self._max_sessions = max_sessions
        self._sessions: OrderedDict[str, _Session] = OrderedDict()
        self._lock = threading.Lock()
        self._by_cause: dict[str, dict[str, float]] = {}
        self._requests = 0
        self._misses = 0
        self._miss_usd = 0.0

    def observe(self, key: str, body: dict[str, Any]) -> PredictedMiss | None:
        """Record ``body`` (as forwarded) and return the miss it predicts, if any."""
        if not key or not isinstance(body, dict):
            return None
        try:
            return self._observe(key, body)
        except Exception:  # pragma: no cover - telemetry must never fail a request
            logger.debug("cache miss watch failed", exc_info=True)
            return None

    def _observe(self, key: str, body: dict[str, Any]) -> PredictedMiss | None:
        fields = {cause: _digest(_get(body, path)) for cause, _scope, path in _FIELDS}
        messages = body.get("messages") or []
        digests = [_digest(m) for m in messages]

        with self._lock:
            self._requests += 1
            prev = self._sessions.pop(key, None)
            self._sessions[key] = _Session(fields=fields, message_digests=digests)
            while len(self._sessions) > self._max_sessions:
                self._sessions.popitem(last=False)
        if prev is None:
            return None

        causes: list[str] = []
        scope = ""
        for cause, cause_scope, _path in _FIELDS:
            if prev.fields.get(cause) != fields[cause]:
                causes.append(cause)
                if not scope or _SCOPE_RANK[cause_scope] > _SCOPE_RANK[scope]:
                    scope = cause_scope

        # History: the first earlier message that no longer matches what was
        # forwarded before. Appending turns is the normal case and not a miss.
        diverged: int | None = None
        for i, old in enumerate(prev.message_digests):
            if i >= len(digests) or digests[i] != old:
                diverged = i
                break
        if diverged is not None and diverged < len(prev.message_digests) - 1:
            # The last earlier message may legitimately be extended in place
            # by some clients; only a change before it is a history rewrite.
            causes.append("history")
            if not scope:
                scope = SCOPE_MESSAGES
        else:
            diverged = None

        if not causes:
            return None

        if scope == SCOPE_ALL:
            tokens = _tokens(body.get("tools")) + _tokens(body.get("system")) + _tokens(messages)
        elif scope == SCOPE_SYSTEM:
            tokens = _tokens(body.get("system")) + _tokens(messages)
        else:
            start = diverged if (causes == ["history"] and diverged is not None) else 0
            tokens = _tokens(messages[start:])
        model = str(body.get("model") or "")
        usd = tokens * _write_minus_read(model)
        miss = PredictedMiss(
            causes=causes,
            scope=scope,
            tokens=tokens,
            usd=usd,
            model=model,
            changed_from_message=diverged,
        )
        with self._lock:
            self._misses += 1
            self._miss_usd += usd
            for cause in causes:
                row = self._by_cause.setdefault(cause, {"count": 0, "tokens": 0, "usd": 0.0})
                row["count"] += 1
                # A miss with several causes is attributed to each, once.
                row["tokens"] += tokens / len(causes)
                row["usd"] += usd / len(causes)
        return miss

    def stats(self) -> dict[str, Any]:
        with self._lock:
            by_cause = {
                cause: {
                    "count": int(row["count"]),
                    "tokens": int(row["tokens"]),
                    "usd": round(row["usd"], 4),
                }
                for cause, row in sorted(self._by_cause.items())
            }
            requests, misses, miss_usd = self._requests, self._misses, self._miss_usd
        return {
            "requests_observed": requests,
            "predicted_misses": misses,
            "predicted_miss_usd": round(miss_usd, 4),
            "by_cause": by_cause,
            "estimated": True,
        }
