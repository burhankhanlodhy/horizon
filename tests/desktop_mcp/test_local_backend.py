from __future__ import annotations

from dataclasses import dataclass

import pytest

from horizon.desktop_mcp.local_backend import LocalBackend, LocalBackendError
from horizon.desktop_mcp.measurement import measure_payload
from horizon.desktop_mcp.source_access import OpenedSource, SourceAccessError


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


def _backend(*records: _Record) -> tuple[LocalBackend, _Reader]:
    reader = _Reader(list(records))
    return LocalBackend(reader), reader


def test_path_open_creates_opaque_snapshot_and_handle_reads_recheck_access() -> None:
    source = "C:/approved/notes.txt"
    backend, reader = _backend(_Record(source, "first\nsecond\n"))
    first = backend.open(source)
    assert first is not None
    assert first["source_id"].startswith("cs1_")
    assert first["version"] == "sha256:digest-v1:text-v1"
    assert first["text"] == "first\nsecond\n"

    # A later disk version does not replace the bytes pinned by the old handle.
    reader.records[source] = _Record(source, "changed on disk\n", digest="digest-v2")
    pinned = backend.open(first["source_id"])
    assert pinned is not None
    assert pinned["text"] == "first\nsecond\n"

    reader.denied.add(source)
    with pytest.raises(LocalBackendError) as caught:
        backend.open(first["source_id"])
    assert caught.value.code == "source_unavailable"


def test_open_preserves_unicode_separators_without_counting_them_as_physical_lines() -> None:
    source = "C:/approved/unicode.txt"
    text = "first\u0085still-first\nsecond\u2028still-second\rthird\r\n"
    backend, _reader = _backend(_Record(source, text))
    result = backend.open(source)
    assert result is not None
    assert result["text"] == text
    assert result["line_count"] == 3
    assert result["selected_lines"] == {"start": 1, "end": 3}


def test_search_is_bounded_filename_discovery_and_does_not_claim_content_search() -> None:
    backend, _reader = _backend(
        _Record("C:/approved/needle-a.txt", "Alpha\nneedle here\n"),
        _Record("C:/approved/needle-b.txt", "Needle elsewhere\n"),
    )
    result = backend.search("needle", limit=1)
    assert result["hit_count"] == 1
    assert result["returned_hit_count"] == 1
    assert result["hits"][0]["name"] == "needle-a.txt"
    assert result["hits"][0]["source_id"].startswith("cs1_")
    assert result["coverage_scope"].endswith("content not searched")
    assert result["coverage"].startswith("partial")
    assert result["compression_ratio"] is None
    assert result["token_count"] is None


def test_retrieve_uses_handle_and_exact_line_bounds() -> None:
    backend, _reader = _backend(_Record("C:/approved/a.txt", "one\ntwo\nthree\n"))
    opened = backend.open("C:/approved/a.txt")
    assert opened is not None

    selected = backend.retrieve(opened["source_id"], {"type": "lines", "start": 2, "end": 2})
    assert selected["text"] == "two\n"
    assert selected["selected_lines"] == {"start": 2, "end": 2}
    assert selected["returned_text_is_raw"] is True

    with pytest.raises(LocalBackendError) as caught:
        backend.retrieve(opened["source_id"], {"type": "lines", "start": 2, "end": 9})
    assert caught.value.code == "selector_out_of_range"


def test_open_exact_paginates_only_at_line_boundaries_and_cursor_is_opaque() -> None:
    source = "C:/approved/large.txt"
    text = "".join(f"row {index:04d}: " + ("x" * 70) + "\n" for index in range(120))
    reader = _Reader([_Record(source, text)])
    backend = LocalBackend(reader, result_token_budget=512, max_result_bytes=4096)

    first = backend.open(source)
    assert first is not None
    assert first["mode"] == "exact"
    assert first["more_available"] is True
    assert isinstance(first["next_cursor"], str)
    assert len(first["next_cursor"]) <= 256
    assert first["text"].endswith("\n")
    assert first["selected_lines"]["start"] == 1

    second = backend.open(first["source_id"], mode="exact", cursor=first["next_cursor"])
    assert second is not None
    assert second["selected_lines"]["start"] == first["selected_lines"]["end"] + 1
    assert second["text"] == "".join(
        f"row {index:04d}: " + ("x" * 70) + "\n"
        for index in range(second["selected_lines"]["start"] - 1, second["selected_lines"]["end"])
    )


def test_compact_falls_back_to_exact_when_gain_gate_not_met() -> None:
    source = "C:/approved/a.txt"
    backend, _reader = _backend(_Record(source, "one\ntwo\nthree\n"))
    result = backend.open(source, mode="compact")
    assert result is not None
    assert result["requested_mode"] == "compact"
    assert result["mode"] == "exact"
    assert result["compression_applied"] is False
    assert result["text"] == "one\ntwo\nthree\n"


def test_compact_fallback_cursor_walks_every_exact_page() -> None:
    source = "C:/approved/outline.md"
    # Distinct rows defeat every fold, and headings would make an overview
    # misroute look plausible rather than raise.
    rows = ["# Title\n"] + [f"# Row {index:04d}: " + ("y" * 60) + "\n" for index in range(120)]
    reader = _Reader([_Record(source, "".join(rows))])
    backend = LocalBackend(reader, result_token_budget=512, max_result_bytes=4096)

    page = backend.open(source, mode="compact")
    assert page is not None
    collected = page["text"]
    pages = 1
    while page["more_available"]:
        previous_end = page["selected_lines"]["end"]
        page = backend.open(page["source_id"], mode="compact", cursor=page["next_cursor"])
        assert page is not None
        assert page["mode"] == "exact"
        assert page["requested_mode"] == "compact"
        assert page["compression_applied"] is False
        assert "headings" not in page
        assert page["selected_lines"]["start"] == previous_end + 1
        collected += page["text"]
        pages += 1
    assert pages > 1
    assert collected == "".join(rows)


def test_compact_cursor_is_rejected_by_other_modes() -> None:
    source = "C:/approved/large.txt"
    text = "".join(f"row {index:04d}: " + ("z" * 70) + "\n" for index in range(120))
    reader = _Reader([_Record(source, text)])
    backend = LocalBackend(reader, result_token_budget=512, max_result_bytes=4096)

    first = backend.open(source, mode="compact")
    assert first is not None and first["more_available"] is True
    for mode in ("exact", "overview"):
        with pytest.raises(LocalBackendError):
            backend.open(first["source_id"], mode=mode, cursor=first["next_cursor"])


def test_focused_result_labels_partial_scope_and_keeps_exact_recovery_refs() -> None:
    source = "C:/approved/a.txt"
    text = "intro line\n\nSQLite stores local snapshots.\n\nunrelated details\n"
    backend, _reader = _backend(_Record(source, text))
    result = backend.open(source, mode="focused", query="SQLite snapshots")
    assert result is not None
    assert result["coverage"].startswith("partial")
    assert result["fidelity"] == "partial_exact_excerpt"
    assert result["line_ranges"] == [{"start_line": 3, "end_line": 3}]
    assert "SQLite stores local snapshots.\n" in result["text"]
    assert "unrelated details" not in result["text"]


def test_overview_cursor_walks_heading_inventory_without_code_fence_headings() -> None:
    source = "C:/approved/outline.md"
    content = "```md\n# Code heading\n```\n" + "".join(
        f"# Section {index:02d}\n\n" for index in range(250)
    )
    backend = LocalBackend(
        _Reader([_Record(source, content)]),
        result_token_budget=2000,
        max_result_bytes=4096,
    )
    page = backend.open(source, mode="overview")
    assert page is not None
    collected: list[str] = []
    for _ in range(20):
        collected.extend(item["heading"] for item in page["headings"])
        cursor = page.get("next_cursor")
        if not cursor:
            break
        page = backend.open(page["source_id"], mode="overview", cursor=cursor)
        assert page is not None
    assert collected == [f"Section {index:02d}" for index in range(250)]


def test_focused_cursor_advances_past_each_exact_passage_without_repeating() -> None:
    source = "C:/approved/focused.md"
    content = "".join(
        f"# Section {index:02d}\nneedle evidence " + (f"detail-{index:02d} " * 8) + "\n\n"
        for index in range(60)
    )
    backend = LocalBackend(
        _Reader([_Record(source, content)]),
        result_token_budget=2000,
        max_result_bytes=4096,
    )
    page = backend.open(source, mode="focused", query="needle evidence")
    assert page is not None
    ranges: list[tuple[int, int]] = []
    for _ in range(20):
        ranges.extend(
            (item["start_line"], item["end_line"]) for item in page.get("line_ranges", [])
        )
        cursor = page.get("next_cursor")
        if not cursor:
            break
        page = backend.open(
            page["source_id"], mode="focused", query="needle evidence", cursor=cursor
        )
        assert page is not None
    assert len(ranges) == 60
    assert len(set(ranges)) == 60


def test_focused_oversized_passage_is_reported_then_later_exact_passage_is_returned() -> None:
    source = "C:/approved/focused.txt"
    content = "# Large\n" + ("needle x " * 1500) + "\n\n# Small\nneedle exact evidence\n"
    backend = LocalBackend(
        _Reader([_Record(source, content)]),
        result_token_budget=2000,
        max_result_bytes=4096,
    )
    result = backend.open(source, mode="focused", query="needle")
    assert result is not None
    assert result["text"].endswith("needle exact evidence\n")
    assert result["omitted_ranges"] == [
        {"start_line": 1, "end_line": 2, "reason": "passage_exceeds_result_budget"}
    ]


def test_root_key_change_blocks_cached_handle_and_stats_make_no_savings_claim() -> None:
    source = "C:/approved/a.txt"
    backend, reader = _backend(_Record(source, "content\n"))
    opened = backend.open(source)
    assert opened is not None
    reader.records[source].root_key = "different-root"

    with pytest.raises(LocalBackendError) as caught:
        backend.open(opened["source_id"])
    assert caught.value.code == "source_unapproved"

    stats = backend.stats()
    assert stats["backend"] == "approved-local-files"
    assert stats["compression_applied"] is False
    assert stats["compression_ratio"] is None
    assert stats["snapshot_count"] == 1


def test_stats_report_counter_provenance_and_only_observed_compact_responses() -> None:
    source = "C:/approved/repeated.json"
    text = (
        "[\n"
        + ",\n".join('  { "name": "a repeated synthetic value", "value": 123 }' for _ in range(120))
        + "\n]\n"
    )
    backend, _reader = _backend(_Record(source, text))

    before = backend.stats()
    provenance = backend.estimator.provenance()
    assert before["compression_enabled"] is True
    assert before["compression_applied"] is False
    assert before["supported_modes"] == ["exact", "compact", "focused", "overview"]
    assert before["token_count_status"] == provenance["count_status"]
    assert before["tokenizer_name"] == provenance["tokenizer_name"]
    assert before["counter_version"] == provenance["counter_version"]

    compacted = backend.open(source, mode="compact")
    assert compacted is not None and compacted["compression_applied"] is True
    assert compacted["compression_encoding_version"] == "contextshrink-compact-v1"
    assert "original snapshot" in compacted["compression_decoding_hint"]
    assert (
        compacted["accounting"]["result"]["delivered_token_estimate"]
        == measure_payload(compacted, backend.estimator)["token_estimate"]
    )
    after = backend.stats()
    assert after["compression_applied"] is True
    assert after["retained_successful_compact_result_count"] == 1
    assert "retained successful compact responses" in after["compression_applied_scope"]
    assert text not in repr(after)


def test_policy_errors_are_counted_without_recording_query_or_path() -> None:
    backend, _reader = _backend(_Record("C:/approved/a.txt", "content\n"))
    with pytest.raises(LocalBackendError) as caught:
        backend.open("C:/approved/a.txt", mode="focused")
    assert caught.value.code == "query_required"
    usage = backend.stats()["usage"]
    assert usage["error_count"] == 1
    assert usage["by_mode"] == {"focused": 1}
