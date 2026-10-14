"""Read-only approved-local-source backend backed by bounded RAM snapshots."""

from __future__ import annotations

import base64
import json
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any

from .artifacts import ArtifactError, ArtifactStore, Dependency
from .batching import BatchError, fuse_reads
from .broker_transport import ARTIFACT_CHUNK_BYTES
from .measurement import (
    CumulativeUsageStats,
    LocalUsageLedger,
    TokenEstimator,
    finalize_accounting,
)
from .operations import OPERATIONS_VERSION, OperationError, run_operation
from .policies import (
    POLICY_VERSION,
    CursorSigner,
    PolicyError,
    present_selected_scope,
    present_snapshot,
)
from .selectors import SelectorError, select_exact
from .snapshots import SnapshotError, SnapshotStore, SourceSnapshot
from .source_access import OpenedSource, SourceAccessError, SourceReader
from .tables import ResultTable, Table, TableError, parse_table
from .text_lines import split_physical_lines

_RENDER_FORMATS = frozenset({"csv", "json", "markdown", "text"})
_RENDER_POLICIES = frozenset({"escape", "reject"})
_TABLE_CACHE_MAX = 4
_ARTIFACT_TTL_CAP_SECONDS = 3600


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
        artifact_root: Path | None = None,
        usage_stats_path: Path | None = None,
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
        # Optional on-disk totals that survive restarts; None keeps usage in memory.
        self.cumulative_usage: CumulativeUsageStats | None = (
            CumulativeUsageStats(usage_stats_path) if usage_stats_path is not None else None
        )
        self._cursor_signer = CursorSigner()
        self._lock = threading.RLock()
        self._catalog_count = 0
        self._catalog_truncated = False
        self._catalog_skipped = 0
        self._ttl_seconds = ttl_seconds
        self._artifact_root = Path(artifact_root) if artifact_root is not None else None
        self.artifact_store: ArtifactStore | None = None
        self._artifact_closed = False
        self._table_cache: OrderedDict[tuple[str, str], Table] = OrderedDict()

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
                selected = select_exact(
                    snapshot.text,
                    values,
                    kind=snapshot.kind,
                    blocks=snapshot.blocks,
                )
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

    def analyze(
        self,
        source_id: str,
        operation: dict[str, Any],
        *,
        result_token_budget: int | None = None,
    ) -> dict[str, Any]:
        """Run one validated typed operation over every parsed row of a snapshot."""
        if not isinstance(source_id, str) or not source_id.startswith("cs1_"):
            raise LocalBackendError("snapshot_unknown")
        if not isinstance(operation, dict):
            raise LocalBackendError("invalid_arguments")
        budget = self._effective_budget(result_token_budget)
        started = time.perf_counter()
        try:
            with self._lock:
                snapshot = self._get_authorized_snapshot(source_id)
                outcome = run_operation(self._parsed_table(snapshot), operation)
                payload = self._analysis_payload(snapshot, outcome, budget)
                self._observe(
                    "contextshrink_analyze",
                    payload,
                    mode="computed",
                    status="success",
                    latency_ms=(time.perf_counter() - started) * 1000,
                )
                return payload
        except (SourceAccessError, SnapshotError, TableError, OperationError) as exc:
            self._record_error("contextshrink_analyze", "computed", started, exc.code)
            raise LocalBackendError(exc.code) from None
        except LocalBackendError as exc:
            self._record_error("contextshrink_analyze", "computed", started, exc.code)
            raise

    def render(
        self,
        source_id: str,
        *,
        fmt: str,
        title: str,
        operation: dict[str, Any] | None = None,
        formula_policy: str = "escape",
    ) -> dict[str, Any]:
        """Store a rendered table under the artifact root and return its manifest only."""
        if not isinstance(source_id, str) or not source_id.startswith("cs1_"):
            raise LocalBackendError("snapshot_unknown")
        if not isinstance(fmt, str) or fmt not in _RENDER_FORMATS:
            raise LocalBackendError("invalid_arguments")
        # "raw" is never model-facing; only the companion may request it, and it
        # does not go through this method.
        if not isinstance(formula_policy, str) or formula_policy not in _RENDER_POLICIES:
            raise LocalBackendError("invalid_arguments")
        if operation is not None and not isinstance(operation, dict):
            raise LocalBackendError("invalid_arguments")
        started = time.perf_counter()
        try:
            with self._lock:
                store = self._artifact_store_locked()
                snapshot = self._get_authorized_snapshot(source_id)
                table = self._parsed_table(snapshot)
                if operation is None:
                    result = ResultTable(
                        tuple(table.columns),
                        tuple(tuple(row.values) for row in table.rows),
                        tuple(dict(row.ref) for row in table.rows),
                    )
                    lineage: dict[str, Any] = {"source_rows": len(table.rows)}
                else:
                    outcome = run_operation(table, operation)
                    result = outcome.table
                    lineage = outcome.lineage
                dependency = Dependency(
                    source_id=snapshot.source_id,
                    version=snapshot.version,
                    root_key=snapshot.root_key,
                )
                manifest = store.create(
                    fmt,
                    result,
                    title=title,
                    dependencies=(dependency,),
                    formula_policy=formula_policy,
                )
                self._observe(
                    "contextshrink_render",
                    None,
                    mode="artifact",
                    status="success",
                    latency_ms=(time.perf_counter() - started) * 1000,
                )
                return {
                    "tool": "render",
                    "artifact": manifest,
                    "lineage": lineage,
                    "delivery": "stored_locally_not_in_result",
                }
        except (
            SourceAccessError,
            SnapshotError,
            TableError,
            OperationError,
            ArtifactError,
        ) as exc:
            self._record_error("contextshrink_render", "artifact", started, exc.code)
            raise LocalBackendError(exc.code) from None
        except LocalBackendError as exc:
            self._record_error("contextshrink_render", "artifact", started, exc.code)
            raise

    def retrieve_batch(
        self,
        requests: list[dict[str, Any]],
        *,
        result_token_budget: int | None = None,
    ) -> dict[str, Any]:
        """Fuse 1-16 exact selector reads into merged blocks under one budget."""
        budget = self._effective_budget(result_token_budget)
        started = time.perf_counter()
        try:
            with self._lock:
                # Sources that fail authorization are simply absent, so fuse_reads
                # reports them per item as source_unknown.
                snapshots: dict[str, SourceSnapshot] = {}
                if isinstance(requests, list):
                    for item in requests:
                        source_id = item.get("source_id") if isinstance(item, dict) else None
                        if not isinstance(source_id, str) or source_id in snapshots:
                            continue
                        try:
                            snapshots[source_id] = self._get_authorized_snapshot(source_id)
                        except LocalBackendError:
                            continue
                payload = fuse_reads(
                    snapshots,
                    requests,
                    max_result_bytes=self.max_result_bytes,
                    token_budget=budget,
                    estimator=self.estimator,
                )
                self._observe(
                    "contextshrink_retrieve_batch",
                    None,
                    mode="batch",
                    status="fallback" if payload.get("deferred") else "success",
                    latency_ms=(time.perf_counter() - started) * 1000,
                )
                return payload
        except BatchError as exc:
            self._record_error("contextshrink_retrieve_batch", "batch", started, exc.code)
            raise LocalBackendError(exc.code) from None
        except LocalBackendError as exc:
            self._record_error("contextshrink_retrieve_batch", "batch", started, exc.code)
            raise

    # Companion-only artifact operations. They are not recorded in the usage
    # ledger, which covers model-facing calls only.

    def artifact_list(self) -> dict[str, Any]:
        """Manifests of live artifacts; empty before any render created a store."""
        with self._lock:
            store = self._existing_artifact_store_locked()
            if store is None:
                return {"artifacts": []}
            try:
                return {"artifacts": store.list()}
            except ArtifactError as exc:
                raise LocalBackendError(exc.code) from None

    def artifact_read(self, artifact_id: str, offset: int, length: int) -> dict[str, Any]:
        """Return one base64 slice of an artifact, verified by the store on read."""
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise LocalBackendError("invalid_arguments")
        if (
            isinstance(length, bool)
            or not isinstance(length, int)
            or not 1 <= length <= ARTIFACT_CHUNK_BYTES
        ):
            raise LocalBackendError("invalid_arguments")
        with self._lock:
            store = self._existing_artifact_store_locked()
            if store is None:
                raise LocalBackendError("artifact_unknown")
            try:
                manifest = store.manifest(artifact_id)
                data = store.read(artifact_id)
            except ArtifactError as exc:
                raise LocalBackendError(exc.code) from None
        total = len(data)
        if offset > total:
            raise LocalBackendError("invalid_arguments")
        chunk = data[offset : offset + length]
        return {
            "artifact_id": artifact_id,
            "offset": offset,
            "data_base64": base64.b64encode(chunk).decode("ascii"),
            "total_bytes": total,
            "sha256": manifest["sha256"],
            "eof": offset + len(chunk) >= total,
        }

    def artifact_delete(self, artifact_id: str) -> dict[str, Any]:
        with self._lock:
            store = self._existing_artifact_store_locked()
            if store is None:
                raise LocalBackendError("artifact_unknown")
            try:
                deleted = store.delete(artifact_id)
            except ArtifactError as exc:
                raise LocalBackendError(exc.code) from None
        return {"deleted": deleted}

    def _existing_artifact_store_locked(self) -> ArtifactStore | None:
        """The current store without creating one; caller holds the lock."""
        if self._artifact_closed:
            return None
        return self.artifact_store

    def close(self) -> None:
        """Close the artifact store and drop cached tables; safe to call repeatedly."""
        with self._lock:
            self._artifact_closed = True
            self._table_cache.clear()
            store, self.artifact_store = self.artifact_store, None
            if store is not None:
                store.close()

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
                "cumulative": (
                    self.cumulative_usage.snapshot()
                    if self.cumulative_usage is not None
                    else {"persistence": "off"}
                ),
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
        event = self.usage_ledger.record(
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
        if self.cumulative_usage is not None:
            self.cumulative_usage.add(event)

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
                extractor_version=opened.extractor_version,
                blocks=opened.blocks,
                warnings=opened.warnings,
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

    def _effective_budget(self, requested: int | None) -> int:
        if requested is None:
            return self.result_token_budget
        if isinstance(requested, bool) or not isinstance(requested, int) or requested < 1:
            raise LocalBackendError("invalid_arguments")
        return min(requested, self.result_token_budget)

    def _record_error(self, tool: str, mode: str, started: float, code: str) -> None:
        self._observe(
            tool,
            None,
            mode=mode,
            status="error",
            latency_ms=(time.perf_counter() - started) * 1000,
            error_code=code,
        )

    def _parsed_table(self, snapshot: SourceSnapshot) -> Table:
        """Parse an authorized snapshot once per (source_id, version); caller holds the lock."""
        key = (snapshot.source_id, snapshot.version)
        cached = self._table_cache.get(key)
        if cached is not None:
            self._table_cache.move_to_end(key)
            return cached
        table = parse_table(snapshot.text, snapshot.kind)
        self._table_cache[key] = table
        while len(self._table_cache) > _TABLE_CACHE_MAX:
            self._table_cache.popitem(last=False)
        return table

    @staticmethod
    def _canonical_json(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)

    def _analysis_payload(
        self, snapshot: SourceSnapshot, outcome: Any, budget: int
    ) -> dict[str, Any]:
        result = outcome.table
        payload: dict[str, Any] = {
            "tool": "analyze",
            "source_id": snapshot.source_id,
            "version": snapshot.version,
            "kind": snapshot.kind,
            "operations_version": OPERATIONS_VERSION,
            "columns": list(result.columns),
            "rows": [list(row) for row in result.rows],
            "row_refs": list(result.row_refs),
            "values": list(outcome.values),
            "lineage": outcome.lineage,
            "coverage": "computed over every parsed row of this source version",
        }
        metadata = {
            "source_id": snapshot.source_id,
            "version": snapshot.version,
            "output_rows": len(result.rows),
        }
        encoded = self._canonical_json(payload)
        if (
            len(encoded.encode("utf-8")) > self.max_result_bytes
            or self.estimator.count_text(encoded) > budget
        ):
            raise LocalBackendError("analysis_result_too_large", metadata)
        # Raw baseline: the same whole-source payload shape policies.py uses for
        # exact reads, built here so no private policy helper is imported.
        line_count = max(1, len(split_physical_lines(snapshot.text)))
        raw_payload = {
            "source_id": snapshot.source_id,
            "version": snapshot.version,
            "kind": snapshot.kind,
            "mode": "exact",
            "coverage": "complete source snapshot",
            "selected_lines": {"start": 1, "end": line_count},
            "line_count": line_count,
            "text": snapshot.text,
            "approved_local_files": True,
            "digest": snapshot.digest,
            "byte_count": snapshot.byte_count,
            "extractor_version": snapshot.extractor_version,
            "policy_version": POLICY_VERSION,
            "fidelity": "byte_exact",
            "more_available": False,
            "next_cursor": None,
            "result_token_budget_applied": False,
        }
        accounted = finalize_accounting(
            payload,
            raw_payload,
            snapshot.text,
            encoded,
            estimator=self.estimator,
        )
        if len(self._canonical_json(accounted).encode("utf-8")) > self.max_result_bytes:
            raise LocalBackendError("analysis_result_too_large", metadata)
        return accounted

    def _artifact_store_locked(self) -> ArtifactStore:
        """Create the artifact store on first render; caller holds the lock."""
        if self._artifact_closed or self._artifact_root is None:
            raise LocalBackendError("render_unavailable")
        if self.artifact_store is None:
            try:
                self.artifact_store = ArtifactStore(
                    self._artifact_root,
                    dependency_check=self._dependencies_current,
                    ttl_seconds=min(self._ttl_seconds, _ARTIFACT_TTL_CAP_SECONDS),
                )
            except ArtifactError as exc:
                raise LocalBackendError(exc.code) from None
        return self.artifact_store

    def _dependencies_current(self, dependencies: tuple[Dependency, ...]) -> bool:
        """Re-authorize every dependency; any failure or drift revokes the artifact."""
        with self._lock:
            try:
                for dependency in dependencies:
                    snapshot = self._get_authorized_snapshot(dependency.source_id)
                    if (
                        snapshot.version != dependency.version
                        or snapshot.root_key != dependency.root_key
                    ):
                        return False
            except Exception:
                return False
            return True
