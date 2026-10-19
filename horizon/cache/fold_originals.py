"""Originals of folded tool results: their own bounded store, apart from the CCR store.

The fold (horizon.proxy.fold_at_rewrite) replaces an old tool result with a stub
that carries a retrieval key. The original must stay redeemable for as long as the
stub is in a conversation, so it cannot share the global 1000-entry CCR store,
where unrelated entries evict it and leave a dangling stub.

Eviction rules:

* Size-capped by bytes (``HORIZON_FOLD_STORE_MAX_MB``).
* Only sessions that are ENDED or IDLE are evictable: a session with no request or
  keep-alive ping for ``HORIZON_FOLD_IDLE_SECONDS`` (default 30 minutes). Entries of
  an active session are never evicted. If the cap is full and nothing is
  evictable, a new original is refused (the fold skips that result), so no stub
  is ever written whose original may be dropped while its session is live.
* 24-hour TTL (``HORIZON_FOLD_TTL_SECONDS``) regardless of activity.

Evicted and expired keys leave a tombstone (bounded), so a retrieval of a folded
key whose original is gone gets an explicit "original no longer available" answer
rather than a generic miss.

:func:`ccr_lookup_store` is the store every CCR retrieval path should resolve
through: it is the CCR store with fold originals behind it, with the same
``retrieve`` / ``exists`` / ``get_entry_status`` surface.
"""

from __future__ import annotations

import os
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

TTL_ENV = "HORIZON_FOLD_TTL_SECONDS"
MAX_MB_ENV = "HORIZON_FOLD_STORE_MAX_MB"
IDLE_ENV = "HORIZON_FOLD_IDLE_SECONDS"
DEFAULT_TTL_SECONDS = 24 * 3600
DEFAULT_MAX_MB = 64  # the hosted proxy shares a Raspberry Pi 5 with the API and database
DEFAULT_IDLE_SECONDS = 1800
TOMBSTONE_LIMIT = 200_000

STATUS_AVAILABLE = "available"
STATUS_EVICTED = "evicted"
STATUS_EXPIRED = "expired"
STATUS_MISSING = "missing"

_DETAIL = {
    STATUS_EVICTED: (
        "Original no longer available: this folded tool output was evicted from "
        "Horizon's fold store (its session went idle and the store reached its size "
        "cap). Re-run the command if the full output is needed."
    ),
    STATUS_EXPIRED: (
        "Original no longer available: the fold retention window (24 hours) has "
        "passed. Re-run the command if the full output is needed."
    ),
}


def _env_number(name: str, default: float, minimum: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value >= minimum else default


@dataclass
class _Entry:
    session_id: str
    text: str
    size: int
    created: float
    last_access: float


@dataclass(frozen=True)
class FoldEntry:
    """Duck-types ``CompressionEntry`` for the fields CCR retrieval reads."""

    original_content: str
    original_item_count: int = 0
    compressed_item_count: int = 0
    retrieval_count: int = 0
    hash_key: str = ""


class FoldOriginalStore:
    """Bounded, thread-safe store of folded originals keyed by SHA-256[:24]."""

    def __init__(
        self,
        *,
        max_bytes: int,
        ttl_seconds: float,
        idle_seconds: float,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._max = int(max_bytes)
        self._ttl = float(ttl_seconds)
        self._idle = float(idle_seconds)
        self._clock = clock
        self._lock = threading.Lock()
        self._entries: OrderedDict[str, _Entry] = OrderedDict()  # LRU: oldest first
        self._total = 0
        self._active: dict[str, float] = {}  # session id -> last activity
        self._tombstones: OrderedDict[str, str] = OrderedDict()

    # -- activity ---------------------------------------------------------

    def mark_active(self, session_id: str, now: float | None = None) -> None:
        """A request or keep-alive ping touched this session: its entries are live."""
        if not session_id:
            return
        with self._lock:
            self._active[session_id] = self._now(now)

    def _is_idle(self, session_id: str, now: float) -> bool:
        last = self._active.get(session_id)
        return last is None or (now - last) > self._idle

    # -- writes -----------------------------------------------------------

    def put(self, session_id: str, key: str, text: str, now: float | None = None) -> bool:
        """File ``text`` under ``key``. False when it cannot be kept without evicting a live session."""
        with self._lock:
            current = self._now(now)
            if key in self._entries:
                entry = self._entries[key]
                entry.last_access = current
                self._entries.move_to_end(key)
                return True
            size = len(text.encode("utf-8", "surrogatepass"))
            if size > self._max:
                return False
            if self._total + size > self._max:
                self._sweep_expired(current)
            while self._total + size > self._max:
                victim = self._idle_victim(current)
                if victim is None:
                    return False
                self._drop(victim, STATUS_EVICTED)
            self._entries[key] = _Entry(session_id, text, size, current, current)
            self._total += size
            self._tombstones.pop(key, None)
            return True

    def _idle_victim(self, now: float) -> str | None:
        for key, entry in self._entries.items():  # oldest access first
            if self._is_idle(entry.session_id, now):
                return key
        return None

    def _sweep_expired(self, now: float) -> None:
        for key in [k for k, e in self._entries.items() if now - e.created > self._ttl]:
            self._drop(key, STATUS_EXPIRED)

    def _drop(self, key: str, reason: str) -> None:
        entry = self._entries.pop(key)
        self._total -= entry.size
        self._tombstones[key] = reason
        self._tombstones.move_to_end(key)
        while len(self._tombstones) > TOMBSTONE_LIMIT:
            self._tombstones.popitem(last=False)

    # -- reads ------------------------------------------------------------

    def get(self, key: str, now: float | None = None) -> str | None:
        with self._lock:
            current = self._now(now)
            entry = self._entries.get(key)
            if entry is None:
                return None
            if current - entry.created > self._ttl:
                self._drop(key, STATUS_EXPIRED)
                return None
            entry.last_access = current
            self._entries.move_to_end(key)
            return entry.text

    def status(self, key: str, now: float | None = None) -> str:
        """``available``, ``evicted``, ``expired`` or ``missing`` (never seen by this store)."""
        with self._lock:
            current = self._now(now)
            entry = self._entries.get(key)
            if entry is not None:
                if current - entry.created > self._ttl:
                    self._drop(key, STATUS_EXPIRED)
                    return STATUS_EXPIRED
                return STATUS_AVAILABLE
            return self._tombstones.get(key, STATUS_MISSING)

    def evict_all(self) -> None:
        """Drop every entry as an eviction (tests, operator reset)."""
        with self._lock:
            for key in list(self._entries):
                self._drop(key, STATUS_EVICTED)

    def stats(self) -> dict[str, Any]:
        with self._lock:
            return {
                "entries": len(self._entries),
                "bytes": self._total,
                "max_bytes": self._max,
                "sessions": len({e.session_id for e in self._entries.values()}),
                "tombstones": len(self._tombstones),
            }

    def _now(self, now: float | None) -> float:
        return self._clock() if now is None else float(now)


def detail_for(status: str) -> str:
    return _DETAIL.get(status, "Original not found in Horizon's fold store.")


_lock = threading.Lock()
_store: FoldOriginalStore | None = None


def get_fold_originals() -> FoldOriginalStore:
    global _store
    with _lock:
        if _store is None:
            _store = FoldOriginalStore(
                max_bytes=int(_env_number(MAX_MB_ENV, DEFAULT_MAX_MB, 1) * 1024 * 1024),
                ttl_seconds=_env_number(TTL_ENV, DEFAULT_TTL_SECONDS, 60),
                idle_seconds=_env_number(IDLE_ENV, DEFAULT_IDLE_SECONDS, 1),
            )
        return _store


def reset_fold_originals() -> None:
    global _store
    with _lock:
        _store = None


class CcrLookupStore:
    """The CCR store with fold originals behind it. Same surface as ``CompressionStore``."""

    def __init__(self, primary: Any, fold: FoldOriginalStore) -> None:
        self._primary = primary
        self._fold = fold

    def retrieve(self, hash_key: str, query: str | None = None) -> Any:
        entry = self._primary.retrieve(hash_key)
        if entry is not None:
            return entry
        text = self._fold.get(hash_key)
        if text is None:
            return None
        return FoldEntry(original_content=text, hash_key=hash_key)

    def exists(self, hash_key: str, clean_expired: bool = False) -> bool:
        if self._primary.exists(hash_key, clean_expired=clean_expired):
            return True
        # A folded key stays "owned" even once its original is gone, so the stub's
        # marker keeps the retrieve tool advertised and the model gets the clear answer.
        return self._fold.status(hash_key) != STATUS_MISSING

    def get_entry_status(self, hash_key: str, clean_expired: bool = False) -> dict[str, Any] | None:
        getter = getattr(self._primary, "get_entry_status", None)
        primary = getter(hash_key, clean_expired=clean_expired) if callable(getter) else None
        if primary is not None and primary.get("status") == STATUS_AVAILABLE:
            return primary
        status = self._fold.status(hash_key)
        default_ttl = int(self._fold._ttl)
        if status == STATUS_AVAILABLE:
            return {
                "hash": hash_key,
                "status": STATUS_AVAILABLE,
                "default_ttl_seconds": default_ttl,
                "ttl_seconds": default_ttl,
            }
        if status != STATUS_MISSING:
            return {
                "hash": hash_key,
                "status": status,
                "detail": detail_for(status),
                "default_ttl_seconds": default_ttl,
                "ttl_seconds": default_ttl,
            }
        return primary

    def __getattr__(self, name: str) -> Any:
        return getattr(self._primary, name)


def ccr_lookup_store() -> CcrLookupStore:
    """The store every CCR retrieval path resolves through (request-scoped CCR store if set)."""
    from horizon.cache.compression_store import get_compression_store

    return CcrLookupStore(get_compression_store(), get_fold_originals())
