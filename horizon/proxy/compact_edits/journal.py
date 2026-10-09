"""Explicit, bounded SQLite journal for provider/client call correspondence.

Construct only inside a trusted, stateful integration. Entries contain source
text/native arguments: the operator must secure the directory and backups.
There is no default path and no automatic eviction of live conversations.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from pathlib import Path
from typing import Any

from .compiler import CompactEditError
from .wire import canonical


class ReplayJournal:
    def __init__(self, path: Path, *, max_entries: int = 2000, max_bytes: int = 32_000_000):
        if not isinstance(path, Path) or not path.is_absolute() or not path.parent.is_dir():
            raise CompactEditError(
                "journal requires an absolute path in an existing secured directory"
            )
        if path.is_symlink() or max_entries <= 0 or max_bytes <= 0:
            raise CompactEditError("invalid journal configuration")
        self._lock = threading.RLock()
        self._max_entries = max_entries
        self._max_bytes = max_bytes
        self._db = sqlite3.connect(path, timeout=5, check_same_thread=False, isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        self._db.execute("""CREATE TABLE IF NOT EXISTS compact_edit_journal (
            scope TEXT NOT NULL, key TEXT NOT NULL, payload TEXT NOT NULL,
            PRIMARY KEY (scope, key))""")

    def get(self, scope: str, key: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute(
                "SELECT payload FROM compact_edit_journal WHERE scope=? AND key=?", (scope, key)
            ).fetchone()
        if row is None:
            return None
        try:
            value = json.loads(row[0])
        except ValueError as exc:
            raise CompactEditError("corrupt replay journal") from exc
        if not isinstance(value, dict):
            raise CompactEditError("corrupt replay journal")
        return value

    def put(self, scope: str, key: str, value: dict[str, Any]) -> None:
        payload = canonical(value)
        if len(payload.encode("utf-8")) > 2_100_000:
            raise CompactEditError("journal entry exceeds limit")
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                old = self._db.execute(
                    "SELECT payload FROM compact_edit_journal WHERE scope=? AND key=?", (scope, key)
                ).fetchone()
                if old is not None:
                    raise CompactEditError(
                        "call already journaled; reconcile delivery before retrying"
                    )
                count, size = self._db.execute(
                    "SELECT COUNT(*), COALESCE(SUM(LENGTH(CAST(payload AS BLOB))),0) FROM compact_edit_journal"
                ).fetchone()
                old_bytes = len(old[0].encode("utf-8")) if old is not None else 0
                if (
                    count + (old is None) > self._max_entries
                    or size - old_bytes + len(payload.encode("utf-8")) > self._max_bytes
                ):
                    raise CompactEditError("journal capacity reached; native admission required")
                self._db.execute(
                    "INSERT INTO compact_edit_journal (scope,key,payload) VALUES (?,?,?)",
                    (scope, key, payload),
                )
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise

    def record_call(self, scope: str, call: str, value: dict[str, Any]) -> None:
        """Atomically reserve the single operation and journal it before delivery."""
        payload = canonical(value)
        if len(payload.encode("utf-8")) > 2_100_000:
            raise CompactEditError("journal entry exceeds limit")
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                admission = self._db.execute(
                    "SELECT payload FROM compact_edit_journal WHERE scope=? AND key='admission'",
                    (scope,),
                ).fetchone()
                if admission is None:
                    raise CompactEditError("missing durable admission")
                state = json.loads(admission[0])
                if state.get("reserved_call"):
                    raise CompactEditError("operation already reserved; do not execute twice")
                state["reserved_call"] = call
                state_payload = canonical(state)
                count, size = self._db.execute(
                    "SELECT COUNT(*), COALESCE(SUM(LENGTH(CAST(payload AS BLOB))),0) FROM compact_edit_journal"
                ).fetchone()
                added = (
                    len(payload.encode("utf-8"))
                    + len(state_payload.encode("utf-8"))
                    - len(admission[0].encode("utf-8"))
                )
                if count + 1 > self._max_entries or size + added > self._max_bytes:
                    raise CompactEditError("journal capacity reached")
                self._db.execute(
                    "INSERT INTO compact_edit_journal (scope,key,payload) VALUES (?,?,?)",
                    (scope, "call:" + call, payload),
                )
                self._db.execute(
                    "UPDATE compact_edit_journal SET payload=? WHERE scope=? AND key='admission'",
                    (state_payload, scope),
                )
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def stop_admissions(self, reason: str) -> None:
        """Durable operator drain latch; existing replay stays available.

        There is intentionally no live resume/delete method. A new rollout must
        restore old sessions and separately qualify a new journal generation.
        """
        if not isinstance(reason, str) or not reason or len(reason) > 512:
            raise CompactEditError("bounded operator drain reason required")
        with self._lock:
            if self.get("@operator", "admission_stop") is None:
                self.put("@operator", "admission_stop", {"reason": reason})

    def admissions_stopped(self) -> bool:
        return self.get("@operator", "admission_stop") is not None

    def retirement(self, scope: str, reason: str) -> None:
        """Keep the original catalog/replay but prohibit another compact edit."""
        if not reason or len(reason) > 512:
            raise CompactEditError("bounded retirement reason required")
        with self._lock:
            if self.get(scope, "candidate_retired") is None:
                self.put(scope, "candidate_retired", {"reason": reason})

    def drain_status(self) -> list[dict[str, Any]]:
        """Non-secret operator inventory; never infer execution from delivery.

        Acknowledged is a received client result, including an error, not proof
        that no more native turns will arrive. Catalogs must remain restorable
        until the managed client certifies that the conversation has ended.
        """
        with self._lock:
            rows = self._db.execute(
                "SELECT scope,payload FROM compact_edit_journal WHERE key='admission'"
            ).fetchall()
            status = []
            for scope, payload in rows:
                saved = json.loads(payload)
                call = saved.get("reserved_call")
                record = self.get(scope, "call:" + call) if call else None
                status.append(
                    {
                        "scope": scope,
                        "state": "unpublished"
                        if not call
                        else (
                            "acknowledged"
                            if record and record.get("native_result")
                            else "pending_client_result"
                        ),
                        "candidate_retired": self.get(scope, "candidate_retired") is not None,
                    }
                )
            return status

    def backup(self, destination: Path) -> None:
        """Consistent SQLite backup, including WAL, in a secured operator directory.

        Refuse overwrite. Keep journal and backups private: they contain source
        and native arguments. This does not authorize rollback of active clients.
        """
        if not destination.is_absolute() or not destination.parent.is_dir():
            raise CompactEditError("absolute backup path in secured directory required")
        # O_EXCL also rejects a pre-existing symlink. Caller owns the directory.
        descriptor = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(descriptor)
        with self._lock, sqlite3.connect(destination) as target:
            self._db.backup(target)
            if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise CompactEditError("journal backup integrity check failed")

    def acknowledge(self, scope: str, call: str, result: dict[str, Any]) -> None:
        """Persist actual client-result receipt, never infer execution from delivery."""
        from .wire import fingerprint

        receipt = {"sha256": fingerprint(result), "is_error": result.get("is_error")}
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                row = self._db.execute(
                    "SELECT payload FROM compact_edit_journal WHERE scope=? AND key=?",
                    (scope, "call:" + call),
                ).fetchone()
                if row is None:
                    raise CompactEditError("acknowledgement has no published call")
                record = json.loads(row[0])
                previous = record.get("native_result")
                if previous is not None and previous != receipt:
                    raise CompactEditError("client result changed on replay")
                if previous is None:
                    record["native_result"] = receipt
                    payload = canonical(record)
                    size = self._db.execute(
                        "SELECT COALESCE(SUM(LENGTH(CAST(payload AS BLOB))),0) FROM compact_edit_journal"
                    ).fetchone()[0]
                    if (
                        len(payload.encode()) > 2_100_000
                        or size - len(row[0].encode()) + len(payload.encode()) > self._max_bytes
                    ):
                        raise CompactEditError("journal cannot retain client acknowledgement")
                    self._db.execute(
                        "UPDATE compact_edit_journal SET payload=? WHERE scope=? AND key=?",
                        (payload, scope, "call:" + call),
                    )
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise

    def __enter__(self) -> ReplayJournal:
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()
