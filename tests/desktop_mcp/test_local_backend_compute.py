from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from horizon.desktop_mcp import local_backend as local_backend_module
from horizon.desktop_mcp.artifacts import ArtifactError
from horizon.desktop_mcp.local_backend import LocalBackend, LocalBackendError
from horizon.desktop_mcp.source_access import OpenedSource, SourceAccessError

CSV_TEXT = (
    "region,amount,date\nnorth,1.10,2026-01-02\nsouth,2.20,2026-02-03\nnorth,2.20,2026-03-04\n"
)
GROUP_SPEC: dict[str, Any] = {
    "columns": {"amount": "decimal"},
    "group_by": ["region"],
    "aggregates": [
        {"op": "count", "as": "rows"},
        {"op": "sum", "column": "amount", "as": "total"},
    ],
    "order_by": [{"column": "total", "direction": "desc"}],
}
PROJECT_SPEC: dict[str, Any] = {
    "columns": {"amount": "decimal"},
    "select": ["region", "amount"],
}
UNKNOWN_CS1 = "cs1_" + "0" * 32


@dataclass
class _Record:
    source: str
    text: str
    root_key: str = "approved-root"
    identity: str = "volume:file-id"
    digest: str = "digest-v1"

    def opened(self) -> OpenedSource:
        return OpenedSource(
            root_key=self.root_key,
            relative_path="notes.txt",
            text=self.text,
            digest=self.digest,
            byte_count=len(self.text.encode("utf-8")),
            identity=self.identity,
        )


class _Reader:
    def __init__(self, records: list[_Record]) -> None:
        self.records = {record.source: record for record in records}
        self.catalog_truncated = False
        self.denied: set[str] = set()

    def catalog(self, query: str = "", limit: int = 5) -> list[dict[str, str]]:
        values = list(self.records.values())
        if query:
            values = [item for item in values if query.casefold() in item.source.casefold()]
        self.catalog_truncated = len(values) > limit
        return [
            {"source": item.source, "name": item.source.rsplit("/", 1)[-1], "kind": "txt"}
            for item in values[:limit]
        ]

    def read(self, source: str) -> OpenedSource:
        if source in self.denied:
            raise SourceAccessError("source_unavailable")
        try:
            return self.records[source].opened()
        except KeyError:
            raise SourceAccessError("source_unapproved") from None

    def check_access(self, source: str) -> str:
        if source in self.denied or source not in self.records:
            raise SourceAccessError("source_unavailable")
        return self.records[source].root_key


def _backend(*records: _Record, **kwargs: Any) -> tuple[LocalBackend, _Reader]:
    reader = _Reader(list(records))
    return LocalBackend(reader, **kwargs), reader


def _open_id(backend: LocalBackend, source: str) -> str:
    opened = backend.open(source)
    assert opened is not None
    return str(opened["source_id"])


def test_analyze_aggregate_and_projection_with_lineage_and_accounting() -> None:
    source = "C:/approved/sales.csv"
    backend, _reader = _backend(_Record(source, CSV_TEXT))
    source_id = _open_id(backend, source)

    grouped = backend.analyze(source_id, GROUP_SPEC)
    assert grouped["tool"] == "analyze"
    assert grouped["source_id"] == source_id
    assert grouped["kind"] == "csv"
    assert grouped["columns"] == ["region", "rows", "total"]
    assert [row[0] for row in grouped["rows"]] == ["north", "south"]
    assert grouped["rows"][0] == ["north", "2", "3.30"]
    assert grouped["lineage"]["source_rows"] == 3
    assert grouped["lineage"]["scanned"] == 3
    assert grouped["coverage"] == "computed over every parsed row of this source version"
    assert isinstance(grouped["operations_version"], str)
    accounting = grouped["accounting"]
    assert accounting["source"]["raw_utf8_bytes"] == len(CSV_TEXT.encode("utf-8"))
    assert isinstance(accounting["result"]["raw_equivalent_utf8_bytes"], int)
    assert isinstance(accounting["result"]["delivered_utf8_bytes"], int)

    projected = backend.analyze(source_id, PROJECT_SPEC)
    assert projected["columns"] == ["region", "amount"]
    assert projected["rows"] == [["north", "1.10"], ["south", "2.20"], ["north", "2.20"]]
    assert projected["row_refs"][0] == {"type": "rows", "start": 2, "end": 2}
    assert projected["row_refs"][2] == {"type": "rows", "start": 4, "end": 4}
    assert projected["lineage"]["source_rows"] == 3


def test_analyze_too_large_raises_metadata_and_never_truncates() -> None:
    source = "C:/approved/big.csv"
    backend, _reader = _backend(_Record(source, CSV_TEXT))
    source_id = _open_id(backend, source)
    backend.max_result_bytes = 64
    with pytest.raises(LocalBackendError) as caught:
        backend.analyze(source_id, PROJECT_SPEC)
    assert caught.value.code == "analysis_result_too_large"
    assert caught.value.metadata == {
        "source_id": source_id,
        "version": "sha256:digest-v1:text-v1",
        "output_rows": 3,
    }


def test_analyze_table_and_operation_errors_propagate_codes() -> None:
    duplicate = "C:/approved/dup.csv"
    unsupported = "C:/approved/book.xlsx"
    ok = "C:/approved/ok.csv"
    backend, _reader = _backend(
        _Record(duplicate, "a,a\n1,2\n"),
        _Record(unsupported, "x"),
        _Record(ok, CSV_TEXT),
    )
    with pytest.raises(LocalBackendError) as header:
        backend.analyze(_open_id(backend, duplicate), PROJECT_SPEC)
    assert header.value.code == "table_header_invalid"
    with pytest.raises(LocalBackendError) as kind:
        backend.analyze(_open_id(backend, unsupported), PROJECT_SPEC)
    assert kind.value.code == "table_kind_unsupported"
    with pytest.raises(LocalBackendError) as spec:
        backend.analyze(_open_id(backend, ok), {"select": ["missing_column"]})
    assert spec.value.code.startswith("operation_")


def test_analyze_rejects_unknown_revoked_and_malformed_ids() -> None:
    source = "C:/approved/sales.csv"
    backend, reader = _backend(_Record(source, CSV_TEXT))
    source_id = _open_id(backend, source)

    with pytest.raises(LocalBackendError) as unknown:
        backend.analyze(UNKNOWN_CS1, PROJECT_SPEC)
    assert unknown.value.code == "snapshot_unknown"
    with pytest.raises(LocalBackendError) as malformed:
        backend.analyze("not-a-handle", PROJECT_SPEC)
    assert malformed.value.code == "snapshot_unknown"
    with pytest.raises(LocalBackendError) as not_object:
        backend.analyze(source_id, ["select"])  # type: ignore[arg-type]
    assert not_object.value.code == "invalid_arguments"

    reader.denied.add(source)
    with pytest.raises(LocalBackendError) as revoked:
        backend.analyze(source_id, PROJECT_SPEC)
    assert revoked.value.code == "source_unavailable"


def test_analyze_parses_each_source_version_once_through_lru(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = "C:/approved/sales.csv"
    backend, _reader = _backend(_Record(source, CSV_TEXT))
    source_id = _open_id(backend, source)
    calls: list[str] = []
    original = local_backend_module.parse_table

    def counting(text: str, kind: str, **kwargs: Any) -> Any:
        calls.append(kind)
        return original(text, kind, **kwargs)

    monkeypatch.setattr(local_backend_module, "parse_table", counting)
    backend.analyze(source_id, PROJECT_SPEC)
    backend.analyze(source_id, GROUP_SPEC)
    assert calls == ["csv"]


def test_render_writes_each_format_under_artifact_root_with_safe_manifest(tmp_path: Path) -> None:
    source = "C:/approved/sales.csv"
    backend, _reader = _backend(_Record(source, CSV_TEXT), artifact_root=tmp_path)
    source_id = _open_id(backend, source)
    suffixes = {"csv": ".csv", "json": ".json", "markdown": ".md", "text": ".txt"}
    for fmt, suffix in suffixes.items():
        result = backend.render(source_id, fmt=fmt, title="Sales table")
        assert result["tool"] == "render"
        assert result["delivery"] == "stored_locally_not_in_result"
        manifest = result["artifact"]
        assert manifest["format"] == fmt
        assert manifest["row_count"] == 3
        assert manifest["dependencies"] == [
            {"source_id": source_id, "version": "sha256:digest-v1:text-v1"}
        ]
        assert str(tmp_path) not in json.dumps(result)
        assert "path" not in manifest
        files = list(tmp_path.rglob(f"artifact-{manifest['artifact_id']}{suffix}"))
        assert len(files) == 1
        assert backend.artifact_store is not None
        assert backend.artifact_store.read(manifest["artifact_id"]) == files[0].read_bytes()


def test_render_with_operation_uses_computed_lineage(tmp_path: Path) -> None:
    source = "C:/approved/sales.csv"
    backend, _reader = _backend(_Record(source, CSV_TEXT), artifact_root=tmp_path)
    source_id = _open_id(backend, source)
    result = backend.render(source_id, fmt="csv", title="Totals", operation=GROUP_SPEC)
    assert result["artifact"]["row_count"] == 2
    assert result["artifact"]["columns"] == ["region", "rows", "total"]
    assert result["lineage"]["source_rows"] == 3


def test_render_rejects_raw_policy_and_bad_format_without_writing(tmp_path: Path) -> None:
    source = "C:/approved/sales.csv"
    backend, _reader = _backend(_Record(source, CSV_TEXT), artifact_root=tmp_path)
    source_id = _open_id(backend, source)
    with pytest.raises(LocalBackendError) as raw:
        backend.render(source_id, fmt="csv", title="t", formula_policy="raw")
    assert raw.value.code == "invalid_arguments"
    with pytest.raises(LocalBackendError) as fmt:
        backend.render(source_id, fmt="xlsx", title="t")
    assert fmt.value.code == "invalid_arguments"
    assert list(tmp_path.rglob("artifact-*")) == []


def test_render_unavailable_without_artifact_root() -> None:
    source = "C:/approved/sales.csv"
    backend, _reader = _backend(_Record(source, CSV_TEXT))
    source_id = _open_id(backend, source)
    with pytest.raises(LocalBackendError) as caught:
        backend.render(source_id, fmt="csv", title="t")
    assert caught.value.code == "render_unavailable"
    assert backend.artifact_store is None


def test_render_bad_title_maps_artifact_code(tmp_path: Path) -> None:
    source = "C:/approved/sales.csv"
    backend, _reader = _backend(_Record(source, CSV_TEXT), artifact_root=tmp_path)
    source_id = _open_id(backend, source)
    with pytest.raises(LocalBackendError) as caught:
        backend.render(source_id, fmt="csv", title="x" * 500)
    assert caught.value.code.startswith("artifact_")


def test_revocation_on_root_change_or_removal_blocks_stored_artifact(tmp_path: Path) -> None:
    changed = "C:/approved/changed.csv"
    removed = "C:/approved/removed.csv"
    backend, reader = _backend(
        _Record(changed, CSV_TEXT),
        _Record(removed, CSV_TEXT),
        artifact_root=tmp_path,
    )
    changed_id = _open_id(backend, changed)
    removed_id = _open_id(backend, removed)
    first = backend.render(changed_id, fmt="json", title="a")["artifact"]["artifact_id"]
    second = backend.render(removed_id, fmt="json", title="b")["artifact"]["artifact_id"]
    assert backend.artifact_store is not None
    assert backend.artifact_store.read(first)

    reader.records[changed].root_key = "other-root"
    with pytest.raises(ArtifactError) as moved:
        backend.artifact_store.read(first)
    assert moved.value.code == "artifact_revoked"

    del reader.records[removed]
    with pytest.raises(ArtifactError) as gone:
        backend.artifact_store.read(second)
    assert gone.value.code == "artifact_revoked"


def test_retrieve_batch_merges_overlapping_blocks_and_flags_unknown_source() -> None:
    source = "C:/approved/notes.txt"
    text = "".join(f"line {index:03d}\n" for index in range(1, 21))
    backend, _reader = _backend(_Record(source, text))
    source_id = _open_id(backend, source)
    requests = [
        {"source_id": source_id, "selector": {"type": "lines", "start": 1, "end": 2}},
        {"source_id": source_id, "selector": {"type": "lines", "start": 2, "end": 3}},
        {"source_id": UNKNOWN_CS1, "selector": {"type": "lines", "start": 1, "end": 1}},
    ]
    payload = backend.retrieve_batch(requests)
    assert payload["mode"] == "batch"
    assert payload["request_count"] == 3
    assert len(payload["blocks"]) == 1
    assert payload["blocks"][0]["text"] == "line 001\nline 002\nline 003\n"
    assert payload["deferred"] == []
    assert payload["errors"] == [{"index": 2, "code": "source_unknown"}]


def test_retrieve_batch_defers_blocks_over_budget_and_keeps_text_exact() -> None:
    source = "C:/approved/notes.txt"
    lines = [f"line {index:03d} " + ("x" * 200) + "\n" for index in range(1, 41)]
    backend, _reader = _backend(_Record(source, "".join(lines)))
    source_id = _open_id(backend, source)
    backend.max_result_bytes = 2000
    requests = [
        {"source_id": source_id, "selector": {"type": "lines", "start": 1, "end": 1}},
        {"source_id": source_id, "selector": {"type": "lines", "start": 30, "end": 30}},
    ]
    payload = backend.retrieve_batch(requests, result_token_budget=250)
    assert [entry["reason"] for entry in payload["deferred"]] == ["budget"]
    assert len(payload["blocks"]) == 1
    assert payload["blocks"][0]["text"] == lines[0]


def test_retrieve_batch_rejects_invalid_whole_call_shapes() -> None:
    source = "C:/approved/notes.txt"
    backend, _reader = _backend(_Record(source, "one\n"))
    with pytest.raises(LocalBackendError) as empty:
        backend.retrieve_batch([])
    assert empty.value.code == "batch_invalid"
    with pytest.raises(LocalBackendError) as budget:
        backend.retrieve_batch(
            [{"source_id": UNKNOWN_CS1, "selector": {"type": "lines", "start": 1, "end": 1}}],
            result_token_budget=0,
        )
    assert budget.value.code == "invalid_arguments"


def test_ledger_events_never_contain_source_text_operation_literal_or_title(
    tmp_path: Path,
) -> None:
    marker_source = "MARKERSOURCETEXT"
    marker_literal = "MARKEROPERATIONLITERAL"
    marker_title = "MARKERTITLEVALUE"
    source = "C:/approved/sales.csv"
    text = f"region,amount\n{marker_source},1.10\n"
    backend, _reader = _backend(_Record(source, text), artifact_root=tmp_path)
    source_id = _open_id(backend, source)
    where = {"op": "eq", "column": "region", "value": marker_literal}
    spec = {"columns": {"amount": "decimal"}, "where": where, "select": ["region", "amount"]}
    backend.analyze(source_id, spec)
    with pytest.raises(LocalBackendError):
        backend.analyze(source_id, {"select": [marker_literal]})
    backend.render(source_id, fmt="text", title=marker_title, operation=spec)
    backend.retrieve_batch(
        [{"source_id": source_id, "selector": {"type": "lines", "start": 2, "end": 2}}]
    )
    events = backend.usage_ledger._events
    tools = {event["tool"] for event in events}
    assert {
        "contextshrink_analyze",
        "contextshrink_render",
        "contextshrink_retrieve_batch",
    } <= tools
    modes = {event["mode"] for event in events}
    assert {"computed", "artifact", "batch"} <= modes
    serialized = json.dumps(events, sort_keys=True) + json.dumps(backend.usage_ledger.stats())
    for marker in (marker_source, marker_literal, marker_title):
        assert marker not in serialized


def test_close_removes_artifact_session_and_is_idempotent(tmp_path: Path) -> None:
    source = "C:/approved/sales.csv"
    backend, _reader = _backend(_Record(source, CSV_TEXT), artifact_root=tmp_path)
    source_id = _open_id(backend, source)
    backend.render(source_id, fmt="csv", title="t")
    sessions = list(tmp_path.glob(".contextshrink-artifacts-*"))
    assert len(sessions) == 1
    backend.close()
    assert not sessions[0].exists()
    backend.close()
    with pytest.raises(LocalBackendError) as caught:
        backend.render(source_id, fmt="csv", title="t")
    assert caught.value.code == "render_unavailable"
    assert list(tmp_path.glob(".contextshrink-artifacts-*")) == []
