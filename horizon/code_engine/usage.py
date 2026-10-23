"""Hourly usage counts for the engine. Counts only: no models, paths or content.

Each bucket holds the running totals for one (UTC hour, tool). The API upsert
replaces a bucket's values, so the meter always sends the full cumulative
total. Re-sending a bucket is therefore harmless, which makes the upload
idempotent and lets a crash mid-upload retry safely.

Totals for hours that are not yet acknowledged, and for the current hour, are
kept in ``usage-pending.json`` so a restart continues them instead of
resetting them.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from horizon.code_engine.store import read_json, write_private_json

logger = logging.getLogger("horizon.code_engine.usage")

USAGE_FILE = "usage-pending.json"
TOOLS = ("claude", "codex")
MAX_BUCKETS_PER_UPLOAD = 500
# The API accepts hours from the last 35 days only; older buckets are dropped.
RETENTION = timedelta(days=34)

Sender = Callable[[list[dict[str, Any]]], bool]


def hour_of(timestamp: float) -> str:
    """The UTC hour containing ``timestamp``, in the API's ISO form."""
    moment = datetime.fromtimestamp(int(timestamp), tz=timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:00:00Z")


@dataclass
class _Totals:
    requests: int = 0
    tokens_before: int = 0
    tokens_after: int = 0

    def values(self) -> tuple[int, int, int]:
        return (self.requests, self.tokens_before, self.tokens_after)


class UsageMeter:
    """Accumulates hourly totals and uploads the ones the server has not confirmed."""

    def __init__(self, home: Path, *, clock: Callable[[], float] = time.time) -> None:
        self._path = home / USAGE_FILE
        self._clock = clock
        self._lock = threading.Lock()
        self._totals: dict[tuple[str, str], _Totals] = {}
        self._unacked: set[tuple[str, str]] = set()
        # When the oldest unconfirmed change happened; None when nothing is unconfirmed.
        self._dirty_since: float | None = None
        # Starts at boot, so a restored pending bucket waits for the safety interval too.
        self._last_flush_at: float = clock()
        self._load()

    def record(self, tool: str, tokens_before: int, tokens_after: int) -> None:
        """Count one served request.

        The count is written to ``usage-pending.json`` before this returns, not
        only at the next flush. A flush runs every 15 minutes and at a clean
        stop, so an engine that is killed or restarted in between would otherwise
        lose every request it served since its last upload.
        """
        now = self._clock()
        key = (hour_of(now), tool)
        with self._lock:
            if not self._unacked:
                self._dirty_since = now
            totals = self._totals.setdefault(key, _Totals())
            totals.requests += 1
            totals.tokens_before += max(0, int(tokens_before))
            totals.tokens_after += max(0, int(tokens_after))
            self._unacked.add(key)
            # Under the lock, so a slower writer cannot leave an older snapshot on disk.
            self._persist(self._snapshot_locked())

    def pending_count(self) -> int:
        """Buckets not yet confirmed by the server."""
        with self._lock:
            return len(self._unacked)

    def flush_due(self, now: float, *, debounce: float, safety: float) -> bool:
        """Whether an upload should run now.

        Nothing unconfirmed means nothing to send, so an idle engine never uploads.
        Otherwise an upload is due ``debounce`` seconds after the first unconfirmed
        change (and never sooner than ``debounce`` after the previous attempt, since a
        flush resets the change time). ``safety`` is the longest a pending bucket may
        wait since the last attempt, whatever else happened.
        """
        with self._lock:
            if not self._unacked:
                return False
            if self._dirty_since is not None and now - self._dirty_since >= debounce:
                return True
            return now - self._last_flush_at >= safety

    def flush(self, send: Sender) -> bool:
        """Upload unconfirmed buckets and persist the rest. Returns True when all were accepted.

        ``send`` posts one batch and returns True when the server accepted it.
        A bucket is confirmed only if it did not change while its batch was in flight.
        """
        with self._lock:
            self._last_flush_at = self._clock()
            self._prune_locked()
            keys = sorted(self._unacked)
            sent_values = {key: self._totals[key].values() for key in keys}
        accepted = True
        for start in range(0, len(keys), MAX_BUCKETS_PER_UPLOAD):
            chunk = keys[start : start + MAX_BUCKETS_PER_UPLOAD]
            body = [self._body(key, sent_values[key]) for key in chunk]
            if not send(body):
                accepted = False
                break
            with self._lock:
                for key in chunk:
                    current = self._totals.get(key)
                    if current is not None and current.values() == sent_values[key]:
                        self._unacked.discard(key)
        with self._lock:
            self._prune_locked()
            # Anything still unconfirmed (a refused batch, or a change made in flight) is
            # due again after one debounce interval, measured from this attempt.
            self._dirty_since = self._clock() if self._unacked else None
            self._persist(self._snapshot_locked())
        return accepted

    def _snapshot_locked(self) -> list[dict[str, Any]]:
        return [self._body(key, totals.values()) for key, totals in sorted(self._totals.items())]

    @staticmethod
    def _body(key: tuple[str, str], values: tuple[int, int, int]) -> dict[str, Any]:
        hour, tool = key
        requests, before, after = values
        return {
            "hour": hour,
            "tool": tool,
            "requests": requests,
            "tokens_before": before,
            "tokens_after": after,
        }

    def _prune_locked(self) -> None:
        """Drop finished hours that were confirmed, and hours the API would refuse."""
        now = self._clock()
        current = hour_of(now)
        oldest = hour_of((datetime.fromtimestamp(now, tz=timezone.utc) - RETENTION).timestamp())
        for key in list(self._totals):
            hour = key[0]
            if hour < oldest:
                self._totals.pop(key, None)
                self._unacked.discard(key)
            elif hour < current and key not in self._unacked:
                self._totals.pop(key, None)

    def _persist(self, buckets: list[dict[str, Any]]) -> None:
        try:
            write_private_json(self._path, {"version": 1, "buckets": buckets})
        except OSError:
            # The in-memory totals still go up on the next flush; a lost file only
            # costs what was counted since the last successful write.
            logger.warning("usage state could not be saved; counts stay in memory")

    def _load(self) -> None:
        data = read_json(self._path)
        if not data or data.get("version") != 1 or not isinstance(data.get("buckets"), list):
            return
        for row in data["buckets"]:
            key, totals = self._parse_row(row)
            if key is None or totals is None:
                continue
            self._totals[key] = totals
            # Whether the server saw it is unknown after a restart; re-sending is safe.
            self._unacked.add(key)
        self._prune_locked()
        if self._unacked:
            self._dirty_since = self._clock()

    @staticmethod
    def _parse_row(row: Any) -> tuple[tuple[str, str] | None, _Totals | None]:
        if not isinstance(row, dict) or row.get("tool") not in TOOLS:
            return None, None
        hour = row.get("hour")
        counts = [row.get(name) for name in ("requests", "tokens_before", "tokens_after")]
        if not isinstance(hour, str) or not all(
            isinstance(v, int) and not isinstance(v, bool) and v >= 0 for v in counts
        ):
            return None, None
        return (hour, row["tool"]), _Totals(*counts)
