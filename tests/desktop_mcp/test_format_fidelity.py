"""Fidelity tests for the Claude Desktop MCP source-presentation layer.

These tests pin the exactness contract of exact selectors, compact JSON, the
desktop fold transforms, the compaction gate, and source bounds.  Helper
classes are local copies so this module stays independent of other test files.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from horizon.desktop_mcp.compression import compact_json, compact_text
from horizon.desktop_mcp.local_backend import LocalBackend, LocalBackendError
from horizon.desktop_mcp.policies import PolicyError, present_snapshot
from horizon.desktop_mcp.selectors import SelectorError, select_exact
from horizon.desktop_mcp.settings import DesktopSettings
from horizon.desktop_mcp.snapshots import SnapshotStore
from horizon.desktop_mcp.source_access import OpenedSource, SourceReader
from horizon.transforms.lossless_compaction import (
    expand_runs,
    path_unheading,
    search_dir_unheading,
    search_unheading,
    strip_ansi,
    unfold_repeated_blocks,
)

ROOT = "approved-root"


# ---------------------------------------------------------------------------
# Shared fakes
# ---------------------------------------------------------------------------


class _FakeEstimator:
    """Deterministic counter: one token per four characters of input text.

    ``finalize_accounting`` reads more provenance keys than the minimal
    contract, so all of them are supplied here with fixed values.
    """

    def count_text(self, text: str) -> int:
        return len(text) // 4

    def provenance(self) -> dict[str, Any]:
        return {
            "fallback": False,
            "counter_version": "fake",
            "count_status": "fake_len_div_4",
            "tokenizer_name": "fake_len_div_4",
            "counting_model_assumption": None,
        }


@dataclass
class _Record:
    source: str
    text: str
    root_key: str = ROOT
    identity: str = "volume:file-id"
    digest: str = "digest-v1"

    def opened(self) -> OpenedSource:
        return OpenedSource(
            root_key=self.root_key,
            relative_path=self.source.rsplit("/", 1)[-1],
            text=self.text,
            digest=self.digest,
            byte_count=len(self.text.encode("utf-8")),
            identity=self.identity,
        )


class _Reader:
    def __init__(self, records: list[_Record]) -> None:
        self.records = {record.source: record for record in records}
        self.catalog_truncated = False

    def read(self, source: str) -> OpenedSource:
        return self.records[source].opened()

    def check_access(self, source: str) -> str:
        return self.records[source].root_key


def _backend(*records: _Record, **kwargs: Any) -> tuple[LocalBackend, _Reader]:
    reader = _Reader(list(records))
    return LocalBackend(reader, **kwargs), reader


def _stage(text: str, kind: str, source: str, **kwargs: Any) -> tuple[LocalBackend, str]:
    """Register a source and store a snapshot of a chosen kind; return its id."""
    backend, _reader = _backend(_Record(source, text), **kwargs)
    raw = text.encode("utf-8")
    snapshot = backend.store.put(
        root_key=ROOT,
        relative_path=source.rsplit("/", 1)[-1],
        source=source,
        kind=kind,
        text=text,
        digest=hashlib.sha256(raw).hexdigest(),
        byte_count=len(raw),
        identity="volume:file-id",
    )
    return backend, snapshot.source_id


def _snapshot(text: str, kind: str):
    raw = text.encode("utf-8")
    return SnapshotStore().put(
        root_key=ROOT,
        relative_path=f"fixture.{kind}",
        source=f"C:/approved/fixture.{kind}",
        kind=kind,
        text=text,
        digest=hashlib.sha256(raw).hexdigest(),
        byte_count=len(raw),
        identity="volume:file-id",
    )


def _outcome(call):
    try:
        return call()
    except (LocalBackendError, PolicyError) as exc:
        return exc


def _assert_no_silent_truncation(full: str, outcome: Any) -> None:
    """Accept only exact complete text, explicit partial text, or result_too_large."""
    if isinstance(outcome, (LocalBackendError, PolicyError)):
        assert outcome.code == "result_too_large"
        return
    text = outcome["text"]
    if text == full:
        assert outcome["more_available"] is False
        return
    assert outcome["more_available"] is True, "partial text must be marked more_available"
    assert outcome["next_cursor"], "partial text must carry a continuation"
    assert full.startswith(text), "partial text must be an exact prefix"


# ---------------------------------------------------------------------------
# 1. Exact mode with one oversized unit
# ---------------------------------------------------------------------------

_OVERSIZED_LINE = "A" * (200 * 1024)
_TIGHT_BUDGET = {"result_token_budget": 512, "max_result_bytes": 4096}


def test_exact_open_of_oversized_single_line_never_truncates() -> None:
    source = "C:/approved/huge.txt"
    full = _OVERSIZED_LINE + "\n"
    backend, _reader = _backend(_Record(source, full), **_TIGHT_BUDGET)
    outcome = _outcome(lambda: backend.open(source))
    _assert_no_silent_truncation(full, outcome)


def test_exact_present_of_oversized_single_line_with_fake_estimator() -> None:
    full = _OVERSIZED_LINE + "\n"
    snapshot = _snapshot(full, "txt")
    outcome = _outcome(
        lambda: present_snapshot(
            snapshot,
            mode="exact",
            estimator=_FakeEstimator(),
            result_token_budget=512,
            max_result_bytes=4096,
        )
    )
    _assert_no_silent_truncation(full, outcome)


def test_json_huge_string_by_pointer_never_truncates() -> None:
    source = "C:/approved/blob.json"
    text = json.dumps({"blob": "B" * 200_000, "n": 1})
    backend, sid = _stage(text, "json", source, **_TIGHT_BUDGET)
    full = json.dumps("B" * 200_000)
    outcome = _outcome(lambda: backend.retrieve(sid, {"type": "json_pointer", "pointer": "/blob"}))
    _assert_no_silent_truncation(full, outcome)


def test_csv_huge_cell_by_rows_selector_never_truncates() -> None:
    source = "C:/approved/cells.csv"
    row = '1,"' + "C" * 200_000 + '"\n'
    text = "id,blob\n" + row
    backend, sid = _stage(text, "csv", source, **_TIGHT_BUDGET)
    outcome = _outcome(lambda: backend.retrieve(sid, {"type": "rows", "start": 2, "end": 2}))
    _assert_no_silent_truncation(row, outcome)


# ---------------------------------------------------------------------------
# 2. CSV rows are byte-identical and logically numbered
# ---------------------------------------------------------------------------

_CSV_ROWS = [
    "id,name,note\n",
    '00123,"Ada, L.","first line\nsecond line"\n',
    "\n",
    "007,Bob,plain\n",
    "\n",
    '0,"",""\n',
    '42,"say ""hi""",end\n',
]


@pytest.mark.parametrize("newline", ["\n", "\r\n"], ids=["lf", "crlf"])
def test_csv_rows_return_source_bytes_without_coercion(newline: str) -> None:
    rows = [row.replace("\n", newline) for row in _CSV_ROWS]
    text = "".join(rows)
    backend, sid = _stage(text, "csv", "C:/approved/people.csv")

    for index, expected in enumerate(rows, 1):
        selected = backend.retrieve(sid, {"type": "rows", "start": index, "end": index})
        assert selected["text"] == expected, f"row {index}"

    whole = backend.retrieve(sid, {"type": "rows", "start": 1, "end": len(rows)})
    assert whole["text"] == text

    middle = backend.retrieve(sid, {"type": "rows", "start": 2, "end": 6})
    assert middle["text"] == "".join(rows[1:6])


@pytest.mark.parametrize("newline", ["\n", "\r\n"], ids=["lf", "crlf"])
def test_csv_blank_rows_count_as_logical_records_with_physical_line_refs(newline: str) -> None:
    rows = [row.replace("\n", newline) for row in _CSV_ROWS]
    text = "".join(rows)
    backend, sid = _stage(text, "csv", "C:/approved/people.csv")

    multi_line = backend.retrieve(sid, {"type": "rows", "start": 2, "end": 2})
    assert multi_line["selected_lines"] == {"start": 2, "end": 3}

    blank = backend.retrieve(sid, {"type": "rows", "start": 3, "end": 3})
    assert blank["text"] == newline
    assert blank["selected_lines"] == {"start": 4, "end": 4}

    # Row 7 is the seventh logical record even though physical lines are 8.
    last = backend.retrieve(sid, {"type": "rows", "start": 7, "end": 7})
    assert last["selected_lines"] == {"start": 8, "end": 8}


def test_csv_exact_open_returns_full_text_byte_identical() -> None:
    text = "".join(_CSV_ROWS)
    backend, _reader = _backend(_Record("C:/approved/people.csv", text))
    opened = backend.open("C:/approved/people.csv")
    assert opened is not None
    assert opened["text"] == text


# ---------------------------------------------------------------------------
# 3. compact_json round trips
# ---------------------------------------------------------------------------


class _Lex(str):
    """A JSON number lexeme; a str subclass keeps numbers distinct from strings."""


def _tagged(text: str) -> Any:
    return json.loads(
        text,
        object_pairs_hook=lambda pairs: ("object", list(pairs)),
        parse_float=_Lex,
        parse_int=_Lex,
    )


def _typed(node: Any) -> Any:
    if node is None:
        return ("null",)
    if isinstance(node, bool):
        return ("bool", node)
    if isinstance(node, _Lex):
        return ("number", str(node))
    if isinstance(node, str):
        return ("string", node)
    if isinstance(node, tuple):
        return ("object", [(key, _typed(value)) for key, value in node[1]])
    return ("array", [_typed(item) for item in node])


def _number_lexemes(node: Any) -> list[str]:
    kind = node[0]
    if kind == "number":
        return [node[1]]
    if kind == "array":
        return [lex for item in node[1] for lex in _number_lexemes(item)]
    if kind == "object":
        return [lex for _key, value in node[1] for lex in _number_lexemes(value)]
    return []


def _spec_tokens(text: str) -> Any:
    return json.loads(
        text,
        object_pairs_hook=list,
        parse_float=str,
        parse_int=str,
    )


_JSON_CASES = {
    "literals": '{ "a" : null , "b" : true , "c" : false }\n',
    "nested_empties": '{ "obj" : { } , "arr" : [ ] , "deep" : [ { } , [ ] , { "x" : [ ] } ] }',
    "unicode_literal": (
        '{ "emoji" : "\U0001f600\U0001f1f5\U0001f1f0" , '
        '"cjk" : "\u6f22\u5b57 \u30c6\u30ad\u30b9\u30c8" , '
        '"combining" : "e\u0301 a\u0308" }'
    ),
    "escapes": (
        r'{ "esc" : "line\nbreak \"quoted\" back\\slash \u00e9 \ud83d\ude00 tab\t" , '
        r'"keep" : "  spaces  stay  " }'
    ),
    "key_order": '{ "z" : 1 , "a" : 2 , "m" : { "y" : 0 , "b" : 1 } }',
    "array_order": "[ 3 , 1 , 2 , [ 9 , 8 ] ]",
    "big_ints": (
        '{ "big" : 123456789012345678901234567890 , '
        '"neg" : -9007199254740993 , "edge" : 9007199254740993 }'
    ),
    "precise_decimals": '{ "d" : 0.1000000000000000055511151231257827 , "x" : 2.2250738585072011e-308 }',
    "exponents": "[ 1e5 , 1E+5 , 2.5e-3 , 0e0 , -0.0 ]",
}


@pytest.mark.parametrize("name", sorted(_JSON_CASES))
def test_compact_json_preserves_tokens_order_and_number_lexemes(name: str) -> None:
    source = _JSON_CASES[name]
    outcome = compact_json(source)
    out = outcome.text

    assert outcome.applied, "every case has insignificant whitespace"
    assert outcome.fidelity == "normalized_json_whitespace"
    assert _spec_tokens(out) == _spec_tokens(source)
    assert _typed(_tagged(out)) == _typed(_tagged(source))

    original_numbers = _number_lexemes(_typed(_tagged(source)))
    assert _number_lexemes(_typed(_tagged(out))) == original_numbers
    for lexeme in original_numbers:
        assert lexeme in out, f"number lexeme {lexeme} was not kept verbatim"


def test_compact_json_keeps_string_escape_spellings_verbatim() -> None:
    source = _JSON_CASES["escapes"]
    out = compact_json(source).text
    assert r"\u00e9" in out
    assert r"\ud83d\ude00" in out
    assert r"\n" in out
    assert r"\"quoted\"" in out
    assert r"\\slash" in out


def test_compact_json_preserves_key_order_as_written() -> None:
    out = compact_json(_JSON_CASES["key_order"]).text
    assert out.index('"z"') < out.index('"a"') < out.index('"m"')
    assert out.index('"y"') < out.index('"b"')


@pytest.mark.parametrize(
    "source",
    [
        '{"a":1,"a":2}',
        '{ "a" : 1 , "a" : 1 }',
        r'{"\u0061":1,"a":2}',
        "[NaN]",
        '{"x": Infinity}',
        '{"x": -Infinity}',
    ],
    ids=["dup", "dup_same_value", "dup_via_escape", "nan", "infinity", "neg_infinity"],
)
def test_compact_json_rejects_duplicates_and_nonstandard_constants(source: str) -> None:
    outcome = compact_json(source)
    assert outcome.applied is False
    assert outcome.text == source
    assert outcome.fidelity == "byte_exact"


# ---------------------------------------------------------------------------
# 4. Desktop fold round trips through compact_text
# ---------------------------------------------------------------------------

_LOG_ANSI = (
    "".join("\x1b[31mERROR\x1b[0m retrying upstream\n" for _ in range(40))
    + "\x1b[32mrecovered\x1b[0m\n"
)
_LOG_PLAIN = "heartbeat ok\n" * 25 + "shutdown\n"
_TEXT_RUNS = "alpha beta gamma\n" * 30 + "end\n"


def test_log_fold_with_ansi_reconstructs_ansi_normalized_source() -> None:
    outcome = compact_text(_LOG_ANSI, "log")
    assert outcome.applied
    assert outcome.transform == "log_runs"
    assert outcome.fidelity == "normalized_log_ansi"
    assert expand_runs(outcome.text) == strip_ansi(_LOG_ANSI)


def test_log_fold_without_ansi_is_reversible_byte_for_byte() -> None:
    outcome = compact_text(_LOG_PLAIN, "log")
    assert outcome.applied
    assert outcome.fidelity == "reversible_fold"
    assert expand_runs(outcome.text) == _LOG_PLAIN


def test_text_fold_reconstructs_source_exactly() -> None:
    outcome = compact_text(_TEXT_RUNS, "text")
    assert outcome.applied
    assert outcome.transform == "repeated_lines"
    assert expand_runs(outcome.text) == _TEXT_RUNS


def test_search_fold_many_rows_per_file_reconstructs_exactly() -> None:
    source = "".join(
        f"src/app/service.py:{number}:    value = compute({number})\n" for number in range(1, 40)
    ) + "".join(f"src/app/util.py:{number}:helper()\n" for number in range(1, 5))
    outcome = compact_text(source, "search")
    assert outcome.applied
    assert outcome.transform == "search_paths"
    assert source in (search_unheading(outcome.text), search_dir_unheading(outcome.text))


def test_search_fold_many_files_per_directory_reconstructs_exactly() -> None:
    source = "".join(f"pkg/handlers/module_{number:03d}.py:1:import os\n" for number in range(60))
    outcome = compact_text(source, "search")
    assert outcome.applied
    assert outcome.transform == "search_paths"
    assert search_dir_unheading(outcome.text) == source


def test_paths_fold_shared_directory_reconstructs_exactly() -> None:
    source = "".join(f"src/components/buttons/item{number}.tsx\n" for number in range(40))
    outcome = compact_text(source, "paths")
    assert outcome.applied
    assert outcome.transform == "path_prefixes"
    assert path_unheading(outcome.text) == source


def test_paths_mixed_listing_is_never_corrupted() -> None:
    source = "".join(f"src/components/buttons/item{n}.tsx\n" for n in range(40)) + "README.md\n"
    outcome = compact_text(source, "paths")
    if outcome.applied:
        assert path_unheading(outcome.text) == source
    else:
        assert outcome.text == source
        assert outcome.fidelity == "byte_exact"


_CONFIG_BLOCK = "- name: app\n  image: app:1\n  port: 8080\n  env: prod\n"


def test_config_fold_repeated_blocks_reconstructs_exactly() -> None:
    source = _CONFIG_BLOCK * 6 + "x: 1\n"
    outcome = compact_text(source, "config")
    assert outcome.applied
    assert outcome.transform == "config_blocks"
    assert expand_runs(unfold_repeated_blocks(outcome.text)) == source


def test_config_fold_repeated_single_lines_reconstructs_exactly() -> None:
    source = "replicas: 2\n" * 20 + "name: x\n"
    outcome = compact_text(source, "config")
    assert outcome.applied
    assert expand_runs(unfold_repeated_blocks(outcome.text)) == source


@pytest.mark.parametrize("kind", ["text", "log", "config"])
def test_expansion_safety_for_highly_repetitive_input(kind: str) -> None:
    source = "x = 1\n" * 10_000
    outcome = compact_text(source, kind)
    assert outcome.applied
    assert len(outcome.text) < 200
    if kind == "config":
        inverse = expand_runs(unfold_repeated_blocks(outcome.text))
    else:
        inverse = expand_runs(outcome.text)
    assert len(inverse) == len(source)
    assert inverse == source


# ---------------------------------------------------------------------------
# 5. Compaction gate boundary via present_snapshot(mode="compact")
# ---------------------------------------------------------------------------


def _boundary_source() -> str:
    """Unique prefix plus a 60-line run; measured gain is 83 tokens, 5% of raw is 99."""
    unique = "".join(
        f"row {number:04d} value {(number * 37) % 1000:03d}\n" for number in range(300)
    )
    return unique + "status ok\n" * 60


def _present_compact(text: str, *, min_tokens: int, min_percent: int) -> dict[str, Any]:
    return present_snapshot(
        _snapshot(text, "log"),
        mode="compact",
        estimator=_FakeEstimator(),
        result_token_budget=8000,
        max_result_bytes=262_144,
        min_compaction_gain_tokens=min_tokens,
        min_compaction_gain_percent=min_percent,
    )


def _measured_gain(text: str) -> tuple[int, int]:
    """Return (raw_tokens, gain) from a probe that accepts any positive gain."""
    probe = _present_compact(text, min_tokens=-(10**9), min_percent=0)
    assert probe["compression_applied"] is True
    accounting = probe["accounting"]["result"]
    raw = accounting["raw_equivalent_token_estimate"]
    gain = raw - accounting["delivered_token_estimate"]
    assert gain > 0
    return raw, gain


def test_gate_applies_exactly_at_token_threshold_and_falls_back_one_below() -> None:
    source = _boundary_source()
    _raw, gain = _measured_gain(source)

    at_threshold = _present_compact(source, min_tokens=gain, min_percent=0)
    assert at_threshold["mode"] == "compact"
    assert at_threshold["compression_applied"] is True

    above_threshold = _present_compact(source, min_tokens=gain + 1, min_percent=0)
    assert above_threshold["mode"] == "exact"
    assert above_threshold["compression_applied"] is False
    assert above_threshold["text"] == source


def test_percent_gate_wins_when_five_percent_exceeds_token_minimum() -> None:
    source = _boundary_source()
    raw, gain = _measured_gain(source)
    five_percent = int(raw * 5 / 100)
    # Preconditions: the token minimum (64) passes, but 5% of raw is larger.
    assert 64 <= gain < five_percent

    assert _present_compact(source, min_tokens=64, min_percent=0)["compression_applied"] is True
    assert _present_compact(source, min_tokens=64, min_percent=5)["compression_applied"] is False


@pytest.mark.parametrize("min_tokens", [0, 64, 200])
def test_gate_threshold_is_max_of_token_minimum_and_percent_share(min_tokens: int) -> None:
    source = _boundary_source()
    raw, gain = _measured_gain(source)
    for percent in range(0, 11):
        threshold = max(min_tokens, int(raw * percent / 100))
        result = _present_compact(source, min_tokens=min_tokens, min_percent=percent)
        assert result["compression_applied"] is (gain >= threshold), (
            f"min={min_tokens} percent={percent} threshold={threshold} gain={gain}"
        )


# ---------------------------------------------------------------------------
# 6. Source and structure bounds that exist in the code
# ---------------------------------------------------------------------------


def test_oversized_source_is_rejected_with_sanitized_code(tmp_path: Path) -> None:
    target = tmp_path / "too_big_name.txt"
    target.write_bytes(b"x" * 4096)
    reader = SourceReader((tmp_path,), max_source_bytes=1024)
    backend = LocalBackend(reader)
    with pytest.raises(LocalBackendError) as caught:
        backend.open(str(target))
    assert caught.value.code == "source_too_large"
    assert str(caught.value) == "source_too_large"
    assert str(tmp_path) not in str(caught.value)
    assert "too_big_name" not in str(caught.value)


def test_source_exactly_at_limit_is_accepted(tmp_path: Path) -> None:
    target = tmp_path / "limit.txt"
    payload = b"y" * 1024
    target.write_bytes(payload)
    backend = LocalBackend(SourceReader((tmp_path,), max_source_bytes=1024))
    opened = backend.open(str(target))
    assert opened is not None
    assert opened["text"] == payload.decode("ascii")


def test_source_reader_refuses_limits_above_the_hard_ceiling(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        SourceReader((tmp_path,), max_source_bytes=25 * 1024 * 1024 + 1)


def test_settings_reject_source_limit_above_ceiling() -> None:
    with pytest.raises(ValidationError):
        DesktopSettings(max_source_bytes=26 * 1024 * 1024, max_snapshot_bytes=32 * 1024 * 1024)


def test_settings_reject_snapshot_limit_below_source_limit() -> None:
    with pytest.raises(ValidationError):
        DesktopSettings(max_source_bytes=4096, max_snapshot_bytes=2048)


def test_snapshot_quota_rejects_oversized_version_with_sanitized_code() -> None:
    text = "z" * 2000
    backend, _reader = _backend(
        _Record("C:/approved/quota.txt", text),
        max_snapshot_bytes=1024,
        max_store_bytes=4096,
    )
    with pytest.raises(LocalBackendError) as caught:
        backend.open("C:/approved/quota.txt")
    assert caught.value.code == "snapshot_too_large"


def test_selector_row_span_is_capped_at_2000_records() -> None:
    text = "a\n" * 2002
    selected = select_exact(text, {"type": "rows", "start": 1, "end": 2001}, kind="csv")
    assert len(selected.units) == 2001
    with pytest.raises(SelectorError, match="selector_out_of_range"):
        select_exact(text, {"type": "rows", "start": 1, "end": 2002}, kind="csv")


def test_json_nesting_depth_is_capped() -> None:
    deep_ok = "[" * 60 + "]" * 60
    select_exact(deep_ok, {"type": "json_pointer", "pointer": ""}, kind="json")
    too_deep = "[" * 70 + "]" * 70
    with pytest.raises(SelectorError, match="json_too_complex"):
        select_exact(too_deep, {"type": "json_pointer", "pointer": ""}, kind="json")


def test_json_node_count_is_capped() -> None:
    wide = "[" + ",".join("0" for _ in range(100_001)) + "]"
    with pytest.raises(SelectorError, match="json_too_complex"):
        select_exact(wide, {"type": "json_pointer", "pointer": ""}, kind="json")
