from __future__ import annotations

import json

import pytest

from horizon.desktop_mcp.batching import BatchError, fuse_reads
from horizon.desktop_mcp.snapshots import SourceSnapshot

BIG = 10_000_000


def _snap(
    source_id: str,
    text: str,
    *,
    kind: str = "txt",
    version: str | None = None,
) -> SourceSnapshot:
    return SourceSnapshot(
        source_id=source_id,
        version=version or f"sha256:{source_id}:text-v1",
        root_key="root",
        relative_path=f"{source_id}.{kind}",
        source=f"C:/approved/{source_id}.{kind}",
        kind=kind,
        text=text,
        digest=source_id,
        byte_count=len(text.encode("utf-8")),
        identity="volume:file",
        extractor_version="text-v1",
        created_at=0.0,
        expires_at=1.0,
    )


def _lines(start: int, end: int) -> dict:
    return {"type": "lines", "start": start, "end": end}


def _fuse(snapshots, requests, *, max_result_bytes=BIG, token_budget=BIG, estimator=None):
    return fuse_reads(
        snapshots,
        requests,
        max_result_bytes=max_result_bytes,
        token_budget=token_budget,
        estimator=estimator,
    )


class _CountStub:
    """Estimator stub that counts characters divided by four."""

    def __init__(self) -> None:
        self.calls = 0

    def count_text(self, text: str) -> int:
        self.calls += 1
        return len(text) // 4


NUMBERED = "".join(f"line {n}\n" for n in range(1, 11))


def test_overlapping_line_requests_merge_into_one_block() -> None:
    snaps = {"s1": _snap("s1", NUMBERED)}
    result = _fuse(
        snaps,
        [
            {"source_id": "s1", "selector": _lines(1, 3)},
            {"source_id": "s1", "selector": _lines(2, 5)},
        ],
    )
    assert len(result["blocks"]) == 1
    block = result["blocks"][0]
    assert block["line_ranges"] == [{"start_line": 1, "end_line": 5}]
    assert block["requests"] == [0, 1]
    assert block["text"] == "".join(f"line {n}\n" for n in range(1, 6))
    assert result["deferred"] == [] and result["errors"] == []


def test_adjacent_line_requests_merge_but_gapped_requests_stay_separate() -> None:
    snaps = {"s1": _snap("s1", NUMBERED)}
    adjacent = _fuse(
        snaps,
        [
            {"source_id": "s1", "selector": _lines(1, 3)},
            {"source_id": "s1", "selector": _lines(4, 6)},
        ],
    )
    assert len(adjacent["blocks"]) == 1
    assert adjacent["blocks"][0]["line_ranges"] == [{"start_line": 1, "end_line": 6}]

    gapped = _fuse(
        snaps,
        [
            {"source_id": "s1", "selector": _lines(1, 2)},
            {"source_id": "s1", "selector": _lines(4, 5)},
        ],
    )
    assert [b["line_ranges"][0] for b in gapped["blocks"]] == [
        {"start_line": 1, "end_line": 2},
        {"start_line": 4, "end_line": 5},
    ]
    assert [b["requests"] for b in gapped["blocks"]] == [[0], [1]]


def test_different_sources_with_identical_spans_never_merge() -> None:
    snaps = {"s1": _snap("s1", NUMBERED), "s2": _snap("s2", NUMBERED)}
    result = _fuse(
        snaps,
        [
            {"source_id": "s1", "selector": _lines(1, 2)},
            {"source_id": "s2", "selector": _lines(1, 2)},
        ],
    )
    assert [b["source_id"] for b in result["blocks"]] == ["s1", "s2"]
    assert all(b["requests"] == [i] for i, b in enumerate(result["blocks"]))


def test_json_pointer_and_lines_on_same_lines_merge() -> None:
    text = '{\n"a": {\n"b": 1\n},\n"c": 2\n}\n'
    snaps = {"j": _snap("j", text, kind="json")}
    result = _fuse(
        snaps,
        [
            {"source_id": "j", "selector": {"type": "json_pointer", "pointer": "/a/b"}},
            {"source_id": "j", "selector": _lines(2, 3)},
        ],
    )
    assert len(result["blocks"]) == 1
    block = result["blocks"][0]
    assert block["line_ranges"] == [{"start_line": 2, "end_line": 3}]
    assert block["text"] == '"a": {\n"b": 1\n'
    scopes = {s["index"]: s for s in block["scopes"]}
    assert scopes[0]["start_line"] == 3 and scopes[0]["end_line"] == 3
    assert scopes[0]["selector"] == {"type": "json_pointer", "pointer": "/a/b"}
    assert scopes[1]["start_line"] == 2 and scopes[1]["end_line"] == 3


def test_markdown_section_scope_is_contained_and_reported() -> None:
    text = "# A\nx\n## B\ny\n# C\nz\n"
    snaps = {"md": _snap("md", text, kind="md")}
    selector = {"type": "markdown_section", "heading": "A"}
    result = _fuse(snaps, [{"source_id": "md", "selector": selector}])
    block = result["blocks"][0]
    assert block["line_ranges"] == [{"start_line": 1, "end_line": 4}]
    assert block["text"] == "# A\nx\n## B\ny\n"
    scope = block["scopes"][0]
    assert (scope["start_line"], scope["end_line"]) == (1, 4)
    assert scope["selector"] == selector


def test_csv_rows_merge_when_adjacent() -> None:
    text = "h1,h2\n1,a\n2,b\n3,c\n"
    snaps = {"csv": _snap("csv", text, kind="csv")}
    result = _fuse(
        snaps,
        [
            {"source_id": "csv", "selector": {"type": "rows", "start": 2, "end": 2}},
            {"source_id": "csv", "selector": {"type": "rows", "start": 3, "end": 3}},
        ],
    )
    assert len(result["blocks"]) == 1
    assert result["blocks"][0]["text"] == "1,a\n2,b\n"
    assert result["blocks"][0]["line_ranges"] == [{"start_line": 2, "end_line": 3}]


def test_unknown_source_is_per_item_and_other_items_still_succeed() -> None:
    snaps = {"s1": _snap("s1", NUMBERED)}
    result = _fuse(
        snaps,
        [
            {"source_id": "s1", "selector": _lines(1, 1)},
            {"source_id": "missing", "selector": _lines(1, 1)},
            {"source_id": "s1", "selector": _lines(5, 5)},
        ],
    )
    assert result["errors"] == [{"index": 1, "code": "source_unknown"}]
    assert [b["requests"] for b in result["blocks"]] == [[0], [2]]
    assert result["request_count"] == 3


def test_selector_error_is_reported_with_its_code() -> None:
    snaps = {"s1": _snap("s1", NUMBERED)}
    result = _fuse(
        snaps,
        [
            {"source_id": "s1", "selector": _lines(1, 1)},
            {"source_id": "s1", "selector": _lines(1, 99)},
            {"source_id": "s1", "selector": {"type": "bogus"}},
        ],
    )
    assert result["errors"] == [
        {"index": 1, "code": "selector_out_of_range"},
        {"index": 2, "code": "selector_invalid"},
    ]
    assert [b["requests"] for b in result["blocks"]] == [[0]]


@pytest.mark.parametrize("requests", [[], [{"source_id": "s1", "selector": _lines(1, 1)}] * 17])
def test_zero_or_too_many_requests_is_whole_call_error(requests: list) -> None:
    with pytest.raises(BatchError) as info:
        _fuse({"s1": _snap("s1", NUMBERED)}, requests)
    assert info.value.code == "batch_invalid"


@pytest.mark.parametrize(
    "requests",
    [
        "not a list",
        [{"source_id": "s1"}],
        [{"source_id": "s1", "selector": _lines(1, 1), "extra": 1}],
        [{"source_id": 7, "selector": _lines(1, 1)}],
        ["s1"],
    ],
)
def test_malformed_items_are_whole_call_errors(requests) -> None:
    with pytest.raises(BatchError) as info:
        _fuse({"s1": _snap("s1", NUMBERED)}, requests)
    assert info.value.code == "batch_invalid"


def test_budget_defers_in_request_order_and_admits_later_smaller_blocks() -> None:
    snaps = {
        "a": _snap("a", "a" * 1200),
        "b": _snap("b", "b" * 1200),
        "c": _snap("c", "c" * 10),
    }
    requests = [
        {"source_id": "a", "selector": _lines(1, 1)},
        {"source_id": "b", "selector": _lines(1, 1)},
        {"source_id": "c", "selector": _lines(1, 1)},
    ]
    result = _fuse(snaps, requests, max_result_bytes=2400)
    assert [b["source_id"] for b in result["blocks"]] == ["a", "c"]
    assert len(result["deferred"]) == 1
    deferred = result["deferred"][0]
    assert deferred["source_id"] == "b"
    assert deferred["reason"] == "budget"
    assert deferred["requests"] == [1]
    assert deferred["selectors"] == [{"index": 1, "selector": _lines(1, 1)}]
    assert len(json.dumps(result, ensure_ascii=False).encode("utf-8")) <= 2400


def test_block_that_cannot_fit_alone_is_too_large_and_never_clipped() -> None:
    snaps = {"a": _snap("a", "a" * 1200), "c": _snap("c", "c" * 10)}
    requests = [
        {"source_id": "a", "selector": _lines(1, 1)},
        {"source_id": "c", "selector": _lines(1, 1)},
    ]
    result = _fuse(snaps, requests, max_result_bytes=1000)
    assert [b["source_id"] for b in result["blocks"]] == ["c"]
    assert result["deferred"][0]["reason"] == "too_large"
    assert result["deferred"][0]["source_id"] == "a"
    assert "a" * 1200 not in json.dumps(result)


def test_token_estimator_drives_budget_and_too_large() -> None:
    snaps = {
        "a": _snap("a", "a" * 400),
        "b": _snap("b", "b" * 400),
        "c": _snap("c", "c" * 4),
    }
    requests = [
        {"source_id": "a", "selector": _lines(1, 1)},
        {"source_id": "b", "selector": _lines(1, 1)},
        {"source_id": "c", "selector": _lines(1, 1)},
    ]
    stub = _CountStub()
    result = _fuse(snaps, requests, token_budget=300, estimator=stub)
    assert stub.calls > 0
    assert [b["source_id"] for b in result["blocks"]] == ["a", "c"]
    assert [(d["source_id"], d["reason"]) for d in result["deferred"]] == [("b", "budget")]

    # Blocks that fail alone are deferred, but the deferral list itself must
    # fit; when even that cannot fit, the whole call fails instead of clipping.
    with pytest.raises(BatchError) as info:
        _fuse(snaps, requests, token_budget=50, estimator=_CountStub())
    assert info.value.code == "batch_result_too_small"

    huge = {**snaps, "a": _snap("a", "a" * 1600)}
    mixed = _fuse(huge, requests, token_budget=300, estimator=_CountStub())
    assert [b["source_id"] for b in mixed["blocks"]] == ["b", "c"]
    assert [(d["source_id"], d["reason"]) for d in mixed["deferred"]] == [("a", "too_large")]


def test_estimator_without_usable_count_leaves_only_byte_limit() -> None:
    class _Broken:
        def count_text(self, text: str):
            raise RuntimeError("offline")

    result = _fuse(
        {"s1": _snap("s1", NUMBERED)},
        [{"source_id": "s1", "selector": _lines(1, 2)}],
        token_budget=1,
        estimator=_Broken(),
    )
    assert [b["requests"] for b in result["blocks"]] == [[0]]


def test_text_is_exact_for_crlf_and_missing_trailing_newline() -> None:
    crlf = "one\r\ntwo\r\nthree"
    snaps = {"crlf": _snap("crlf", crlf)}
    result = _fuse(snaps, [{"source_id": "crlf", "selector": _lines(2, 3)}])
    block = result["blocks"][0]
    assert block["text"] == "two\r\nthree"
    assert crlf.count(block["text"]) == 1
    scope = block["scopes"][0]
    assert scope["selector"] == _lines(2, 3)


def test_each_scope_text_is_contained_in_its_block() -> None:
    text = '{\n"a": {\n"b": 1\n},\n"c": 2\n}\n'
    snaps = {"j": _snap("j", text, kind="json")}
    result = _fuse(
        snaps,
        [
            {"source_id": "j", "selector": {"type": "json_pointer", "pointer": "/a/b"}},
            {"source_id": "j", "selector": _lines(4, 5)},
        ],
    )
    assert len(result["blocks"]) == 1
    block = result["blocks"][0]
    assert '"b": 1' in block["text"]
    assert '"c": 2\n' in block["text"]


def test_output_is_deterministic_and_shape_is_stable() -> None:
    snaps = {"a": _snap("a", NUMBERED), "b": _snap("b", NUMBERED)}
    requests = [
        {"source_id": "b", "selector": _lines(3, 4)},
        {"source_id": "a", "selector": _lines(1, 2)},
        {"source_id": "missing", "selector": _lines(1, 1)},
        {"source_id": "a", "selector": _lines(3, 3)},
    ]
    first = _fuse(snaps, requests)
    second = _fuse(snaps, requests)
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)
    assert set(first) == {"mode", "blocks", "deferred", "errors", "coverage", "request_count"}
    assert first["mode"] == "batch"
    assert first["coverage"] == "exact selected scopes"
    assert [b["requests"][0] for b in first["blocks"]] == [0, 1]


def test_late_deferrals_give_back_admitted_blocks_instead_of_failing() -> None:
    # One small block is admitted first; fifteen oversized blocks are then
    # deferred, and their entries alone push the admitted envelope over budget.
    snapshots = {"s00": _snap("s00", "small\n")}
    requests = [{"source_id": "s00", "selector": _lines(1, 1)}]
    for index in range(1, 16):
        source_id = f"s{index:02d}"
        snapshots[source_id] = _snap(source_id, "x" * 5000 + "\n")
        requests.append({"source_id": source_id, "selector": _lines(1, 1)})
    unconstrained = _fuse(snapshots, requests)
    assert len(unconstrained["blocks"]) == 16

    def succeeds(limit: int) -> bool:
        try:
            _fuse(snapshots, requests, max_result_bytes=limit)
        except BatchError:
            return False
        return True

    # Smallest limit that succeeds: the small block fits when first admitted,
    # but not alongside all fifteen later deferral entries.
    low, high = 1, 1_000_000
    while low < high:
        middle = (low + high) // 2
        if succeeds(middle):
            high = middle
        else:
            low = middle + 1
    limit = low

    result = _fuse(snapshots, requests, max_result_bytes=limit)
    serialized = json.dumps(result, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    assert len(serialized.encode("utf-8")) <= limit
    reasons = {entry["requests"][0]: entry["reason"] for entry in result["deferred"]}
    assert all(reasons[index] == "too_large" for index in range(1, 16))
    assert reasons[0] == "budget"
    assert result["blocks"] == []
