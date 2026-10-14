"""Read-only approved-local-source backend backed by bounded RAM snapshots."""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

from .measurement import LocalUsageLedger, TokenEstimator
from .policies import CursorSigner, PolicyError, present_selected_scope, present_snapshot
from .selectors import SelectorError, select_exact
from .snapshots import SnapshotError, SnapshotStore, SourceSnapshot
from .source_access import OpenedSource, SourceAccessError, SourceReader


class LocalBackendError(ValueError):
    """Sanitized local-source failure with a stable machine-readable code."""

    def __init__(self, code: str, metadata: dict[str, Any] | None = None) -> None:
        self.code = code
        self.metadata = metadata or {}
        super().__init__(code)


class LocalBackend:
    """Search and read exact, immutable versions from approved local roots."""

    def __init__(
        self,
        reader: SourceReader,
        *,
        ttl_seconds: int = 3600,
        max_snapshot_bytes: int = 8 * 1024 * 1024,
        max_store_bytes: int = 32 * 1024 * 1024,
        max_snapshots: int = 128,
        result_token_budget: int = 2000,
        max_result_bytes: int = 65536,
        counting_model: str | None = None,
        eval_arm: str = "optimized",
        min_compaction_gain_tokens: int = 64,
        min_compaction_gain_percent: int = 5,
    ) -> None:
        if eval_arm not in {"optimized", "raw"}:
            raise ValueError("eval_arm must be optimized or raw")
        self.reader = reader
        self.store = SnapshotStore(
            ttl_seconds=ttl_seconds,
            max_snapshot_bytes=max_snapshot_bytes,
            max_store_bytes=max_store_bytes,
            max_snapshots=max_snapshots,
        )
        self._max_snapshot_bytes = max_snapshot_bytes
        self.result_token_budget = result_token_budget
        self.max_result_bytes = max_result_bytes
        self.counting_model = counting_model
        self.eval_arm = eval_arm
        self.min_compaction_gain_tokens = min_compaction_gain_tokens
        self.min_compaction_gain_percent = min_compaction_gain_percent
        self.estimator = TokenEstimator(counting_model)
        self.usage_ledger = LocalUsageLedger()
        self._cursor_signer = CursorSigner()
        self._lock = threading.RLock()
        self._catalog_count = 0
        self._catalog_truncated = False
        self._catalog_skipped = 0

    def update_roots(self, roots: tuple[Path, ...] | list[Path]) -> None:
        """Replace local grants and clear all snapshots; intended for local UI only."""
        approved_roots = tuple(Path(root) for root in roots)
        with self._lock:
            self.reader = SourceReader(
                approved_roots=approved_roots,
                max_source_bytes=self._max_snapshot_bytes,
            )
            self.store.clear()
            self._catalog_count = 0
            self._catalog_truncated = False
            self._catalog_skipped = 0

    def search(self, query: str, limit: int = 5) -> dict[str, Any]:
        """Discover approved files by catalog metadata and issue read handles.

        Search is filename/catalog discovery in this phase. It does not claim to
        search document contents; exact content retrieval is a separate call.
        """
        if not isinstance(query, str) or len(query) > 4096 or not isinstance(limit, int):
            raise LocalBackendError("invalid_arguments")
        if isinstance(limit, bool) or not 1 <= limit <= 20:
            raise LocalBackendError("invalid_arguments")
        started = time.perf_counter()
        with self._lock:
            try:
                candidates = self.reader.catalog(query=query, limit=limit)
                truncated = bool(self.reader.catalog_truncated)
                unreadable = int(getattr(self.reader, "catalog_unreadable_entries", 0))
            except SourceAccessError as exc:
                self._observe(
                    "contextshrink_search",
                    None,
                    mode="exact",
                    status="error",
                    latency_ms=(time.perf_counter() - started) * 1000,
                    error_code=exc.code,
                )
                raise LocalBackendError(exc.code) from None
            self._catalog_count = len(candidates)
            self._catalog_truncated = truncated
            hits: list[dict[str, Any]] = []
            skipped = max(0, unreadable)
            for item in candidates[:limit]:
                if not isinstance(item, dict):
                    skipped += 1
                    continue
                source = item.get("source")
                if not isinstance(source, str) or not source:
                    skipped += 1
                    continue
                try:
                    snapshot = self._import_source(source, item)
                except LocalBackendError:
                    # Catalog discovery is best effort. One inaccessible or
                    # unsupported file must not hide the other approved files.
                    skipped += 1
                    continue
                hits.append(
                    {
                        "source_id": snapshot.source_id,
                        "version": snapshot.version,
                        "kind": snapshot.kind,
                        "name": item.get("name") or Path(source).name,
                    }
                )
            # A later large snapshot can evict an earlier one within the same
            # catalog call. Never publish a handle that is already unavailable.
            retained_hits: list[dict[str, Any]] = []
            for hit in hits:
                try:
                    self.store.get(hit["source_id"])
                except SnapshotError:
                    skipped += 1
                else:
                    retained_hits.append(hit)
            hits = retained_hits
            is_truncated = truncated or skipped > 0
            self._catalog_skipped = skipped
            payload = {
                "mode": "exact",
                "approved_local_files": True,
                "hits": hits,
                "hit_count": len(candidates),
                "returned_hit_count": len(hits),
                "coverage": "partial (catalog limit or skipped files)"
                if is_truncated
                else "complete",
                "coverage_scope": "approved local filename catalog; content not searched",
                "catalog_truncated": truncated,
                "skipped_count": skipped,
                "token_count": None,
                "token_count_status": "unknown; no tokenizer was used",
                "compression_ratio": None,
                "baseline_tokens": None,
            }
            self._observe(
                "contextshrink_search",
                payload,
                status="fallback" if is_truncated else "success",
                latency_ms=(time.perf_counter() - started) * 1000,
            )
            return payload

    def open(
        self,
        source: str,
        *,
        start_line: int | None = None,
        end_line: int | None = None,
        mode: str = "exact",
        query: str | None = None,
        result_token_budget: int | None = None,
        cursor: str | None = None,
    ) -> dict[str, Any] | None:
        """Open a granted path into a snapshot or read an existing opaque handle."""
        if not isinstance(source, str) or not source or len(source) > 4096:
            raise LocalBackendError("invalid_arguments")
        if not isinstance(mode, str):
            raise LocalBackendError("invalid_arguments")
        if query is not None and (not isinstance(query, str) or len(query) > 4096):
            raise LocalBackendError("invalid_arguments")
        if result_token_budget is not None and (
            isinstance(result_token_budget, bool)
            or not isinstance(result_token_budget, int)
            or not 128 <= result_token_budget <= 8000
        ):
            raise LocalBackendError("invalid_arguments")
        if (start_line is None) != (end_line is None):
            raise LocalBackendError("selector_out_of_range")
        if mode not in {"exact", "compact", "focused", "overview"}:
            raise LocalBackendError("unsupported_mode")
        requested_mode = mode
        active_mode = "exact" if self.eval_arm == "raw" else mode
        effective_budget = (
            self.result_token_budget
            if result_token_budget is None
            else min(result_token_budget, self.result_token_budget)
        )
        started = time.perf_counter()
        with self._lock:
            try:
                if cursor is not None:
                    cursor_source = self._cursor_signer.decode(cursor).get("source_id")
                    if source != cursor_source or not isinstance(cursor_source, str):
                        raise LocalBackendError("cursor_mismatch")
                    snapshot = self._get_authorized_snapshot(cursor_source)
                else:
                    snapshot = (
                        self._get_authorized_snapshot(source)
                        if source.startswith("cs1_")
                        else self._import_source(source)
                    )
                payload = self._present(
                    snapshot,
                    start_line=start_line,
                    end_line=end_line,
                    mode=active_mode,
                    query=query,
                    result_token_budget=effective_budget,
                    cursor=cursor,
                    requested_mode=requested_mode,
                    eval_arm=self.eval_arm,
                    min_compaction_gain_tokens=self.min_compaction_gain_tokens,
                    min_compaction_gain_percent=self.min_compaction_gain_percent,
                )
                self._observe(
                    "contextshrink_open",
                    payload,
                    status=(
                        "fallback"
                        if self.eval_arm == "raw"
                        or (requested_mode == "compact" and not payload.get("compression_applied"))
                        else "success"
                    ),
                    latency_ms=(time.perf_counter() - started) * 1000,
                )
                return payload
            except (SourceAccessError, SnapshotError) as exc:
                self._observe(
                    "contextshrink_open",
                    None,
                    mode=requested_mode,
                    status="error",
                    latency_ms=(time.perf_counter() - started) * 1000,
                    error_code=exc.code,
                )
                raise LocalBackendError(exc.code) from None
            except PolicyError as exc:
                self._observe(
                    "contextshrink_open",
                    None,
                    mode=requested_mode,
                    status="error",
                    latency_ms=(time.perf_counter() - started) * 1000,
                    error_code=exc.code,
                )
                raise LocalBackendError(exc.code, exc.metadata) from None
            except LocalBackendError as exc:
                self._observe(
                    "contextshrink_open",
                    None,
                    mode=requested_mode,
                    status="error",
                    latency_ms=(time.perf_counter() - started) * 1000,
                    error_code=exc.code,
                )
                raise

    def retrieve(
        self,
        source_id: str,
        selector: Any,
        *,
        result_token_budget: int | None = None,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """Retrieve an exact bounded selector from an authorized snapshot."""
        if not isinstance(source_id, str) or not source_id.startswith("cs1_"):
            raise LocalBackendError("snapshot_unknown")
        values = (
            selector
            if isinstance(selector, dict)
            else getattr(selector, "model_dump", lambda: None)()
        )
        if not isinstance(values, dict):
            raise LocalBackendError("selector_invalid")
        started = time.perf_counter()
        try:
            with self._lock:
                snapshot = self._get_authorized_snapshot(source_id)
                selected = select_exact(snapshot.text, values, kind=snapshot.kind)
                result = present_selected_scope(
                    snapshot,
                    selected,
                    result_token_budget=(
                        self.result_token_budget
                        if result_token_budget is None
                        else min(result_token_budget, self.result_token_budget)
                    ),
                    max_result_bytes=self.max_result_bytes,
                    estimator=self.estimator,
                    cursor=cursor,
                    cursor_signer=self._cursor_signer,
                )
                self._observe(
                    "contextshrink_retrieve",
                    result,
                    status="success",
                    latency_ms=(time.perf_counter() - started) * 1000,
                )
                return result
        except (SourceAccessError, SnapshotError) as exc:
            self._observe(
                "contextshrink_retrieve",
                None,
                mode="exact",
                status="error",
                latency_ms=(time.perf_counter() - started) * 1000,
                error_code=exc.code,
            )
            raise LocalBackendError(exc.code) from None
        except SelectorError as exc:
            self._observe(
                "contextshrink_retrieve",
                None,
                mode="exact",
                status="error",
                latency_ms=(time.perf_counter() - started) * 1000,
                error_code=exc.code,
            )
            raise LocalBackendError(exc.code) from None
        except PolicyError as exc:
            self._observe(
                "contextshrink_retrieve",
                None,
                mode="exact",
                status="error",
                latency_ms=(time.perf_counter() - started) * 1000,
                error_code=exc.code,
            )
            raise LocalBackendError(exc.code, exc.metadata) from None
        except LocalBackendError as exc:
            self._observe(
                "contextshrink_retrieve",
                None,
                mode="exact",
                status="error",
                latency_ms=(time.perf_counter() - started) * 1000,
                error_code=exc.code,
            )
            raise

    def stats(self) -> dict[str, Any]:
        with self._lock:
            usage = self.store.stats()
            usage_ledger = self.usage_ledger.stats()
            provenance = self.estimator.provenance()
            compact_count = usage_ledger.get("retained_successful_compact_result_count", 0)
            if isinstance(compact_count, bool) or not isinstance(compact_count, int):
                compact_count = 0
            return {
                "approved_local_files": True,
                "mode": "presentation",
                "backend": "approved-local-files",
                "catalog_count": self._catalog_count,
                "catalog_truncated": self._catalog_truncated,
                "catalog_skipped": self._catalog_skipped,
                "compression_enabled": True,
                "supported_modes": ["exact", "compact", "focused", "overview"],
                "compression_applied": compact_count > 0,
                "compression_applied_scope": usage_ledger.get(
                    "retained_successful_compact_result_scope",
                    "retained successful compact responses in bounded worker ledger",
                ),
                "retained_successful_compact_result_count": compact_count,
                "compression_ratio": None,
                "baseline_tokens": None,
                "token_count_status": provenance["count_status"],
                "tokenizer_name": provenance["tokenizer_name"],
                "counter_version": provenance["counter_version"],
                "snapshot_count": usage["snapshot_count"],
                "snapshot_source_bytes": usage["snapshot_source_bytes"],
                "max_snapshot_source_bytes": usage["max_snapshot_source_bytes"],
                "max_store_source_bytes": usage["max_store_source_bytes"],
                "max_snapshots": usage["max_snapshots"],
                "snapshot_ttl_seconds": self.store.ttl_seconds,
                "evaluation_arm": self.eval_arm,
                "usage": usage_ledger,
            }

    def _observe(
        self,
        tool: str,
        payload: dict[str, Any] | None,
        *,
        mode: str | None = None,
        status: str = "success",
        latency_ms: float | None = None,
        error_code: str | None = None,
    ) -> None:
        """Record only allowlisted local metadata; never accept source text or args."""
        result = payload or {}
        self.usage_ledger.record(
            tool=tool,
            mode=mode or result.get("requested_mode") or result.get("mode") or "unknown",
            source_version=result.get("version"),
            policy_version=result.get("policy_version", "desktop-presentation-v1"),
            status=status,
            accounting=result.get("accounting"),
            compression_applied=result.get("compression_applied") is True,
            latency_ms=latency_ms,
            error_code=error_code,
        )

    def _import_source(self, source: str, metadata: dict[str, str] | None = None) -> SourceSnapshot:
        try:
            opened = self.reader.read(source)
        except SourceAccessError as exc:
            raise LocalBackendError(exc.code) from None
        if not isinstance(opened, OpenedSource):
            raise LocalBackendError("source_unavailable")
        try:
            return self.store.put(
                root_key=opened.root_key,
                relative_path=opened.relative_path,
                source=source,
                kind=(metadata or {}).get("kind") or self._kind_from_source(source),
                text=opened.text,
                digest=opened.digest,
                byte_count=opened.byte_count,
                identity=opened.identity,
            )
        except SnapshotError as exc:
            raise LocalBackendError(exc.code) from None

    def _get_authorized_snapshot(self, source_id: str) -> SourceSnapshot:
        try:
            snapshot = self.store.get(source_id)
            current_root_key = self.reader.check_access(snapshot.source)
        except SourceAccessError as exc:
            raise LocalBackendError(exc.code) from None
        except SnapshotError as exc:
            raise LocalBackendError(exc.code) from None
        if current_root_key != snapshot.root_key:
            raise LocalBackendError("source_unapproved")
        return snapshot

    def _present(
        self,
        snapshot: SourceSnapshot,
        *,
        start_line: int | None,
        end_line: int | None,
        mode: str = "exact",
        query: str | None = None,
        result_token_budget: int | None = None,
        cursor: str | None = None,
        requested_mode: str | None = None,
        eval_arm: str = "optimized",
        min_compaction_gain_tokens: int = 64,
        min_compaction_gain_percent: int = 5,
    ) -> dict[str, Any]:
        return present_snapshot(
            snapshot,
            mode=mode,
            query=query,
            result_token_budget=(
                self.result_token_budget
                if result_token_budget is None
                else min(result_token_budget, self.result_token_budget)
            ),
            max_result_bytes=self.max_result_bytes,
            estimator=self.estimator,
            cursor=cursor,
            start_line=start_line,
            end_line=end_line,
            cursor_signer=self._cursor_signer,
            requested_mode=requested_mode or mode,
            eval_arm=eval_arm,
            min_compaction_gain_tokens=min_compaction_gain_tokens,
            min_compaction_gain_percent=min_compaction_gain_percent,
        )

    @staticmethod
    def _kind_from_source(source: str) -> str:
        suffix = Path(source).suffix.lower().lstrip(".")
        return suffix if suffix else "text"
