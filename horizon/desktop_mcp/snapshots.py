"""Bounded in-memory immutable source snapshots for the desktop MCP pilot."""

from __future__ import annotations

import threading
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass

from .extractors import DOCUMENT_KINDS, DocumentBlock


class SnapshotError(ValueError):
    """Sanitized snapshot-store failure identified by a stable code."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class SourceSnapshot:
    """One immutable text version and its source/grant provenance.

    For document kinds, ``text`` is the extracted text, ``digest`` and
    ``byte_count`` describe the original file, and ``blocks``/``warnings`` come
    from the extractor. Store quotas always count the UTF-8 bytes of ``text``.
    """

    source_id: str
    version: str
    root_key: str
    relative_path: str
    source: str
    kind: str
    text: str
    digest: str
    byte_count: int
    identity: str
    extractor_version: str
    created_at: float
    expires_at: float
    blocks: tuple[DocumentBlock, ...] = ()
    warnings: tuple[str, ...] = ()


class SnapshotStore:
    """TTL- and source-byte-quota-bounded RAM store; never persists contents.

    Quotas count original UTF-8 source bytes, not Python object/RSS overhead.
    """

    def __init__(
        self,
        *,
        ttl_seconds: int = 3600,
        max_snapshot_bytes: int = 8 * 1024 * 1024,
        max_store_bytes: int = 32 * 1024 * 1024,
        max_snapshots: int = 128,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if (
            ttl_seconds <= 0
            or max_snapshot_bytes <= 0
            or max_store_bytes <= 0
            or max_snapshots <= 0
        ):
            raise ValueError("snapshot limits must be positive")
        if max_snapshot_bytes > max_store_bytes:
            raise ValueError("max_snapshot_bytes cannot exceed max_store_bytes")
        self.ttl_seconds = ttl_seconds
        self.max_snapshot_bytes = max_snapshot_bytes
        self.max_store_bytes = max_store_bytes
        self.max_snapshots = max_snapshots
        self._clock = clock
        self._items: OrderedDict[str, SourceSnapshot] = OrderedDict()
        # Held (text) bytes per retained snapshot; quota accounting uses these.
        self._held: dict[str, int] = {}
        self._bytes = 0
        self._lock = threading.RLock()

    @property
    def used_bytes(self) -> int:
        with self._lock:
            self._expire_locked(self._clock())
            return self._bytes

    @property
    def snapshot_count(self) -> int:
        with self._lock:
            self._expire_locked(self._clock())
            return len(self._items)

    def put(
        self,
        *,
        root_key: str,
        relative_path: str,
        source: str,
        kind: str,
        text: str,
        digest: str,
        byte_count: int,
        identity: str,
        extractor_version: str = "text-v1",
        blocks: tuple[DocumentBlock, ...] = (),
        warnings: tuple[str, ...] = (),
    ) -> SourceSnapshot:
        """Store a new, independently addressable immutable version."""
        if not isinstance(text, str) or not isinstance(byte_count, int) or byte_count < 0:
            raise SnapshotError("snapshot_invalid")
        if byte_count > self.max_snapshot_bytes:
            raise SnapshotError("snapshot_too_large")
        # A decoded string may use fewer UTF-8 bytes only if metadata is wrong.
        try:
            held = len(text.encode("utf-8"))
        except UnicodeEncodeError:
            raise SnapshotError("snapshot_invalid") from None
        # Document byte_count is the original file size, not the extracted text.
        is_document = isinstance(kind, str) and kind.lower().lstrip(".") in DOCUMENT_KINDS
        if not is_document and held != byte_count:
            raise SnapshotError("snapshot_invalid")
        if held > self.max_snapshot_bytes:
            raise SnapshotError("snapshot_too_large")
        if not digest or not root_key or not relative_path or not identity:
            raise SnapshotError("snapshot_invalid")
        if not isinstance(extractor_version, str) or not extractor_version:
            raise SnapshotError("snapshot_invalid")

        now = self._clock()
        snapshot = SourceSnapshot(
            source_id=f"cs1_{uuid.uuid4().hex}",
            version=f"sha256:{digest}:{extractor_version}",
            root_key=root_key,
            relative_path=relative_path,
            source=source,
            kind=kind,
            text=text,
            digest=digest,
            byte_count=byte_count,
            identity=identity,
            extractor_version=extractor_version,
            created_at=now,
            expires_at=now + self.ttl_seconds,
            blocks=tuple(blocks),
            warnings=tuple(warnings),
        )
        with self._lock:
            self._expire_locked(now)
            while self._items and (
                self._bytes + held > self.max_store_bytes or len(self._items) >= self.max_snapshots
            ):
                self._evict_oldest_locked()
            if held > self.max_store_bytes:
                raise SnapshotError("snapshot_too_large")
            self._items[snapshot.source_id] = snapshot
            self._held[snapshot.source_id] = held
            self._bytes += held
        return snapshot

    def get(self, source_id: str) -> SourceSnapshot:
        """Return a retained version or a safe unknown/expired code."""
        if not isinstance(source_id, str) or not source_id.startswith("cs1_"):
            raise SnapshotError("snapshot_unknown")
        with self._lock:
            self._expire_locked(self._clock())
            snapshot = self._items.get(source_id)
            if snapshot is None:
                raise SnapshotError("snapshot_unknown")
            self._items.move_to_end(source_id)
            return snapshot

    def clear(self) -> None:
        """Drop all snapshots, for example when approved roots are reloaded."""
        with self._lock:
            self._items.clear()
            self._held.clear()
            self._bytes = 0

    def stats(self) -> dict[str, int]:
        with self._lock:
            self._expire_locked(self._clock())
            return {
                "snapshot_count": len(self._items),
                "snapshot_source_bytes": self._bytes,
                "max_snapshot_source_bytes": self.max_snapshot_bytes,
                "max_store_source_bytes": self.max_store_bytes,
                "max_snapshots": self.max_snapshots,
            }

    def _expire_locked(self, now: float) -> None:
        for source_id, snapshot in tuple(self._items.items()):
            if snapshot.expires_at <= now:
                self._bytes -= self._held.pop(source_id)
                del self._items[source_id]

    def _evict_oldest_locked(self) -> None:
        source_id, _snapshot = self._items.popitem(last=False)
        self._bytes -= self._held.pop(source_id)
