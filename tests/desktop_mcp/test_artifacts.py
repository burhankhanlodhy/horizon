from __future__ import annotations

import hashlib
import json
import re
import threading
from pathlib import Path

import pytest

from horizon.desktop_mcp.artifacts import (
    ArtifactError,
    ArtifactStore,
    Dependency,
    render_csv,
    render_json,
    render_markdown,
    render_text,
)
from horizon.desktop_mcp.tables import ResultTable

DEP = Dependency(source_id="cs1_src", version="sha256:abc:1", root_key="root-1")
TABLE = ResultTable(
    columns=("name", "amount"),
    rows=(("alice", "1"), ("bob", None)),
)


def _code(exc_info: pytest.ExceptionInfo[ArtifactError]) -> str:
    return exc_info.value.code


def _session_dir(base: Path) -> Path:
    matches = list(base.glob(".contextshrink-artifacts-*"))
    assert len(matches) == 1
    return matches[0]


def _always_ok(_deps: tuple[Dependency, ...]) -> bool:
    return True


def _store(base: Path, **kwargs: object) -> ArtifactStore:
    options: dict[str, object] = {"dependency_check": _always_ok}
    options.update(kwargs)
    return ArtifactStore(base, **options)  # type: ignore[arg-type]


def _table(*rows: tuple[str | None, ...], columns: tuple[str, ...] = ("a",)) -> ResultTable:
    return ResultTable(columns=columns, rows=tuple(rows))


# ---------------------------------------------------------------------------
# Renderers
# ---------------------------------------------------------------------------


def test_csv_uses_crlf_and_rfc4180_quoting() -> None:
    table = _table(
        ('say "hi"',),
        ("a,b",),
        ("line1\nline2",),
        ("cr\rhere",),
        columns=("text",),
    )
    text = render_csv(table)
    assert text == ('text\r\n"say ""hi"""\r\n"a,b"\r\n"line1\nline2"\r\n"cr\rhere"\r\n')


def test_csv_none_is_empty_and_lone_empty_field_is_quoted() -> None:
    assert render_csv(_table(("x", None), columns=("p", "q"))) == "p,q\r\nx,\r\n"
    assert render_csv(_table(("",), ("y",), columns=("h",))) == 'h\r\n""\r\ny\r\n'


def test_csv_escape_policy_prefixes_formula_cells() -> None:
    assert render_csv(_table(("=1+1",))) == "a\r\n'=1+1\r\n"
    assert render_csv(_table(("@SUM(A1)",))) == "a\r\n'@SUM(A1)\r\n"
    assert render_csv(_table(("-5.2+A1",))) == "a\r\n'-5.2+A1\r\n"
    assert render_csv(_table(("+A1",))) == "a\r\n'+A1\r\n"
    assert render_csv(_table(("-",))) == "a\r\n'-\r\n"
    assert render_csv(_table(("\t=x",))) == "a\r\n'\t=x\r\n"


@pytest.mark.parametrize("value", ["-5.2", "+3", "-0", "+.5", "-12."])
def test_csv_signed_decimal_numbers_are_not_escaped(value: str) -> None:
    assert render_csv(_table((value,))) == f"a\r\n{value}\r\n"


def test_csv_reject_policy_raises_on_formula_cell() -> None:
    with pytest.raises(ArtifactError) as caught:
        render_csv(_table(("ok",), ("=1",)), formula_policy="reject")
    assert _code(caught) == "artifact_formula_cell"
    assert render_csv(_table(("-5.2",)), formula_policy="reject") == "a\r\n-5.2\r\n"


def test_csv_raw_policy_leaves_formula_cells_unchanged() -> None:
    assert render_csv(_table(("=1+1",)), formula_policy="raw") == "a\r\n=1+1\r\n"


def test_csv_unknown_policy_is_rejected() -> None:
    with pytest.raises(ArtifactError) as caught:
        render_csv(_table(("x",)), formula_policy="bogus")
    assert _code(caught) == "artifact_formula_policy_invalid"


def test_json_is_array_of_objects_in_column_order_with_null_and_unicode() -> None:
    text = render_json(
        ResultTable(columns=("name", "city"), rows=(("José", None), ("Zoë", "東京")))
    )
    assert text.endswith("]\n")
    assert '"José"' in text and "東京" in text
    assert "\\u" not in text
    assert json.loads(text) == [
        {"name": "José", "city": None},
        {"name": "Zoë", "city": "東京"},
    ]
    assert list(json.loads(text)[0]) == ["name", "city"]


def test_json_empty_table_is_empty_array() -> None:
    assert render_json(ResultTable(columns=("a",), rows=())) == "[]\n"


def test_markdown_header_separator_rows_and_none_cells() -> None:
    text = render_markdown(TABLE)
    assert text == "| name | amount |\n| --- | --- |\n| alice | 1 |\n| bob |  |\n"


def test_markdown_escapes_pipes_backslashes_and_newlines() -> None:
    table = _table(("a|b\\c",), ("line1\nline2",), columns=("x",))
    text = render_markdown(table)
    assert "| a\\|b\\\\c |" in text
    assert "| line1<br>line2 |" in text


@pytest.mark.parametrize(
    ("raw", "escaped"),
    [
        ("# head", "\\# head"),
        ("*em*", "\\*em*"),
        ("- item", "\\- item"),
        ("<b>x", "\\<b>x"),
        ("1. step", "1\\. step"),
        ("2) step", "2\\) step"),
        ("plain text", "plain text"),
    ],
)
def test_markdown_escapes_leading_markup_only(raw: str, escaped: str) -> None:
    assert render_markdown(_table((raw,), columns=("x",))) == (f"| x |\n| --- |\n| {escaped} |\n")


def test_markdown_title_is_rendered_as_heading() -> None:
    text = render_markdown(TABLE, title="Report")
    assert text.startswith("# Report\n\n| name | amount |")


def test_text_is_tab_separated_and_flattens_control_whitespace() -> None:
    table = _table(("a\tb", "x\ny"), ("z", None), columns=("c1", "c2"))
    assert render_text(table) == "c1\tc2\na b\tx y\nz\t\n"


# ---------------------------------------------------------------------------
# Store: lifecycle
# ---------------------------------------------------------------------------


def test_create_read_manifest_and_list(tmp_path: Path) -> None:
    store = _store(tmp_path)
    try:
        manifest = store.create("csv", TABLE, title="Totals", dependencies=[DEP])

        assert set(manifest) == {
            "artifact_id",
            "format",
            "title",
            "utf8_bytes",
            "sha256",
            "row_count",
            "columns",
            "preview_rows",
            "dependencies",
            "created_at",
            "expires_at",
        }
        assert re.fullmatch(r"[0-9a-f]{32}", manifest["artifact_id"])
        assert manifest["format"] == "csv"
        assert manifest["title"] == "Totals"
        assert manifest["row_count"] == 2
        assert manifest["columns"] == ["name", "amount"]
        assert manifest["preview_rows"] == [["alice", "1"], ["bob", None]]
        assert manifest["dependencies"] == [{"source_id": "cs1_src", "version": "sha256:abc:1"}]
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", manifest["created_at"])
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", manifest["expires_at"])

        data = store.read(manifest["artifact_id"])
        assert data == render_csv(TABLE).encode("utf-8")
        assert manifest["utf8_bytes"] == len(data)
        assert manifest["sha256"] == hashlib.sha256(data).hexdigest()

        assert store.manifest(manifest["artifact_id"]) == manifest
        listed = store.list()
        assert [item["artifact_id"] for item in listed] == [manifest["artifact_id"]]
    finally:
        assert store.close() is True


def test_preview_rows_are_capped_at_five(tmp_path: Path) -> None:
    rows = tuple((str(index),) for index in range(8))
    store = _store(tmp_path)
    try:
        manifest = store.create("json", _table(*rows), title="Rows", dependencies=[])
        assert manifest["row_count"] == 8
        assert manifest["preview_rows"] == [[str(i)] for i in range(5)]
    finally:
        store.close()


@pytest.mark.parametrize(
    ("fmt", "extension", "expected_text"),
    [
        ("csv", "csv", render_csv(TABLE)),
        ("json", "json", render_json(TABLE)),
        ("markdown", "md", render_markdown(TABLE, title="T")),
        ("text", "txt", render_text(TABLE)),
    ],
)
def test_each_format_writes_generated_file_with_rendered_bytes(
    tmp_path: Path, fmt: str, extension: str, expected_text: str
) -> None:
    store = _store(tmp_path)
    try:
        manifest = store.create(fmt, TABLE, title="T", dependencies=[])
        files = list(_session_dir(tmp_path).iterdir())
        assert [path.name for path in files] == [f"artifact-{manifest['artifact_id']}.{extension}"]
        assert store.read(manifest["artifact_id"]) == expected_text.encode("utf-8")
    finally:
        store.close()


def test_create_rejects_unsupported_format_and_invalid_titles(tmp_path: Path) -> None:
    store = _store(tmp_path)
    try:
        with pytest.raises(ArtifactError) as caught:
            store.create("xlsx", TABLE, title="ok", dependencies=[])
        assert _code(caught) == "artifact_format_unsupported"

        for title in ("", "x" * 121, "line\nbreak", "tab\there", 42):
            with pytest.raises(ArtifactError) as caught:
                store.create("csv", TABLE, title=title, dependencies=[])  # type: ignore[arg-type]
            assert _code(caught) == "artifact_title_invalid"

        manifest = store.create("csv", TABLE, title="x" * 120, dependencies=[])
        assert manifest["title"] == "x" * 120
    finally:
        store.close()


def test_create_cannot_be_given_a_name_or_path(tmp_path: Path) -> None:
    store = _store(tmp_path)
    try:
        with pytest.raises(TypeError):
            store.create("csv", TABLE, title="t", dependencies=[], filename="../escape.csv")  # type: ignore[call-arg]
        with pytest.raises(TypeError):
            store.create("csv", TABLE, title="t", dependencies=[], path=str(tmp_path))  # type: ignore[call-arg]
        assert store.list() == []
        assert list(_session_dir(tmp_path).iterdir()) == []
    finally:
        store.close()


def test_manifest_contains_no_filesystem_path(tmp_path: Path) -> None:
    store = _store(tmp_path)
    try:
        manifest = store.create("csv", TABLE, title="t", dependencies=[DEP])
        rendered = json.dumps(manifest)
        assert str(tmp_path) not in rendered
        assert _session_dir(tmp_path).name not in rendered
        assert str(tmp_path).replace("\\", "/") not in rendered
    finally:
        store.close()


def test_unknown_and_malformed_ids_raise_artifact_unknown(tmp_path: Path) -> None:
    store = _store(tmp_path)
    try:
        for bad in ("0" * 32, "../../etc/passwd", "", "ABCDEF" * 5 + "AB"):
            with pytest.raises(ArtifactError) as caught:
                store.read(bad)
            assert _code(caught) == "artifact_unknown"
            with pytest.raises(ArtifactError) as caught:
                store.manifest(bad)
            assert _code(caught) == "artifact_unknown"
    finally:
        store.close()


def test_delete_removes_file_and_later_reads_are_unknown(tmp_path: Path) -> None:
    store = _store(tmp_path)
    try:
        manifest = store.create("csv", TABLE, title="t", dependencies=[])
        artifact_id = manifest["artifact_id"]
        assert store.delete(artifact_id) is True
        assert list(_session_dir(tmp_path).iterdir()) == []
        assert store.list() == []
        with pytest.raises(ArtifactError) as caught:
            store.read(artifact_id)
        assert _code(caught) == "artifact_unknown"
        with pytest.raises(ArtifactError) as caught:
            store.delete(artifact_id)
        assert _code(caught) == "artifact_unknown"
    finally:
        store.close()


def test_invalidate_where_removes_matching_manifests(tmp_path: Path) -> None:
    store = _store(tmp_path)
    try:
        keep = store.create("csv", TABLE, title="keep", dependencies=[])
        drop = store.create("json", TABLE, title="drop", dependencies=[])
        removed = store.invalidate_where(lambda item: item["title"] == "drop")
        assert removed == 1
        assert [item["artifact_id"] for item in store.list()] == [keep["artifact_id"]]
        with pytest.raises(ArtifactError) as caught:
            store.manifest(drop["artifact_id"])
        assert _code(caught) == "artifact_unknown"
    finally:
        store.close()


def test_export_path_points_at_owned_file_and_is_not_in_manifest(tmp_path: Path) -> None:
    store = _store(tmp_path)
    try:
        manifest = store.create("csv", TABLE, title="t", dependencies=[])
        path = store.export_path(manifest["artifact_id"])
        assert path.read_bytes() == render_csv(TABLE).encode("utf-8")
        assert path.parent == _session_dir(tmp_path)
    finally:
        store.close()


# ---------------------------------------------------------------------------
# Limits
# ---------------------------------------------------------------------------


def test_size_cap_rejects_oversized_artifact_without_writing(tmp_path: Path) -> None:
    store = _store(tmp_path, max_artifact_bytes=10)
    try:
        with pytest.raises(ArtifactError) as caught:
            store.create("csv", TABLE, title="t", dependencies=[])
        assert _code(caught) == "artifact_too_large"
        assert list(_session_dir(tmp_path).iterdir()) == []
    finally:
        store.close()


def test_size_cap_is_bounded_by_staging_file_limit(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        _store(tmp_path, max_artifact_bytes=8 * 1024 * 1024 + 1)


def test_total_byte_cap_rejects_when_store_is_full(tmp_path: Path) -> None:
    first_size = len(render_csv(TABLE).encode("utf-8"))
    store = _store(tmp_path, max_total_bytes=first_size + 1)
    try:
        store.create("csv", TABLE, title="one", dependencies=[])
        with pytest.raises(ArtifactError) as caught:
            store.create("csv", TABLE, title="two", dependencies=[])
        assert _code(caught) == "artifact_store_full"
        assert len(store.list()) == 1
    finally:
        store.close()


def test_item_cap_rejects_when_store_is_full(tmp_path: Path) -> None:
    store = _store(tmp_path, max_items=2)
    try:
        store.create("csv", TABLE, title="one", dependencies=[])
        store.create("csv", TABLE, title="two", dependencies=[])
        with pytest.raises(ArtifactError) as caught:
            store.create("csv", TABLE, title="three", dependencies=[])
        assert _code(caught) == "artifact_store_full"
        store.delete(store.list()[0]["artifact_id"])
        store.create("csv", TABLE, title="three", dependencies=[])
        assert len(store.list()) == 2
    finally:
        store.close()


def test_dependencies_must_be_dependency_records(tmp_path: Path) -> None:
    store = _store(tmp_path)
    try:
        with pytest.raises(ArtifactError) as caught:
            store.create("csv", TABLE, title="t", dependencies=["cs1_src"])  # type: ignore[list-item]
        assert _code(caught) == "artifact_dependency_invalid"
    finally:
        store.close()


# ---------------------------------------------------------------------------
# Expiry and revocation
# ---------------------------------------------------------------------------


def test_ttl_expiry_with_injected_clock_removes_file(tmp_path: Path) -> None:
    now = [1000.0]
    store = _store(tmp_path, ttl_seconds=10, clock=lambda: now[0])
    try:
        manifest = store.create("csv", TABLE, title="t", dependencies=[])
        artifact_id = manifest["artifact_id"]
        now[0] = 1009.9
        assert store.read(artifact_id) == render_csv(TABLE).encode("utf-8")

        now[0] = 1010.0
        with pytest.raises(ArtifactError) as caught:
            store.read(artifact_id)
        assert _code(caught) == "artifact_unknown"
        assert list(_session_dir(tmp_path).iterdir()) == []
        assert store.list() == []
    finally:
        store.close()


def test_expire_reports_removed_count(tmp_path: Path) -> None:
    now = [0.0]
    store = _store(tmp_path, ttl_seconds=5, clock=lambda: now[0])
    try:
        store.create("csv", TABLE, title="a", dependencies=[])
        now[0] = 2.0
        store.create("csv", TABLE, title="b", dependencies=[])
        now[0] = 5.5
        assert store.expire() == 1
        assert [item["title"] for item in store.list()] == ["b"]
    finally:
        store.close()


def test_dependency_revocation_raises_revoked_and_removes_artifact(tmp_path: Path) -> None:
    seen: list[tuple[Dependency, ...]] = []
    revoked = [False]

    def check(deps: tuple[Dependency, ...]) -> bool:
        seen.append(deps)
        return not revoked[0]

    store = _store(tmp_path, dependency_check=check)
    try:
        manifest = store.create("csv", TABLE, title="t", dependencies=[DEP])
        artifact_id = manifest["artifact_id"]
        assert store.read(artifact_id)
        assert seen[-1] == (DEP,)

        revoked[0] = True
        with pytest.raises(ArtifactError) as caught:
            store.read(artifact_id)
        assert _code(caught) == "artifact_revoked"
        assert list(_session_dir(tmp_path).iterdir()) == []

        with pytest.raises(ArtifactError) as caught:
            store.read(artifact_id)
        assert _code(caught) == "artifact_unknown"
        with pytest.raises(ArtifactError) as caught:
            store.manifest(artifact_id)
        assert _code(caught) == "artifact_unknown"
    finally:
        store.close()


def test_manifest_call_also_checks_dependencies(tmp_path: Path) -> None:
    revoked = [False]
    store = _store(tmp_path, dependency_check=lambda _deps: not revoked[0])
    try:
        manifest = store.create("csv", TABLE, title="t", dependencies=[DEP])
        revoked[0] = True
        with pytest.raises(ArtifactError) as caught:
            store.manifest(manifest["artifact_id"])
        assert _code(caught) == "artifact_revoked"
    finally:
        store.close()


def test_raising_dependency_check_counts_as_revoked(tmp_path: Path) -> None:
    def broken(_deps: tuple[Dependency, ...]) -> bool:
        raise RuntimeError("source registry offline")

    store = _store(tmp_path, dependency_check=broken)
    try:
        manifest = store.create("csv", TABLE, title="t", dependencies=[DEP])
        with pytest.raises(ArtifactError) as caught:
            store.read(manifest["artifact_id"])
        assert _code(caught) == "artifact_revoked"
        assert list(_session_dir(tmp_path).iterdir()) == []
    finally:
        store.close()


def test_non_true_dependency_result_counts_as_revoked(tmp_path: Path) -> None:
    store = _store(tmp_path, dependency_check=lambda _deps: None)  # type: ignore[arg-type,return-value]
    try:
        manifest = store.create("csv", TABLE, title="t", dependencies=[DEP])
        with pytest.raises(ArtifactError) as caught:
            store.manifest(manifest["artifact_id"])
        assert _code(caught) == "artifact_revoked"
    finally:
        store.close()


def test_list_drops_revoked_artifacts(tmp_path: Path) -> None:
    revoked_ids: set[str] = set()

    def check(deps: tuple[Dependency, ...]) -> bool:
        return not revoked_ids.intersection(dep.source_id for dep in deps)

    store = _store(tmp_path, dependency_check=check)
    try:
        other = Dependency(source_id="cs1_other", version="v", root_key="r")
        store.create("csv", TABLE, title="a", dependencies=[DEP])
        keep = store.create("csv", TABLE, title="b", dependencies=[other])
        revoked_ids.add(DEP.source_id)
        assert [item["artifact_id"] for item in store.list()] == [keep["artifact_id"]]
        assert len(list(_session_dir(tmp_path).iterdir())) == 1
    finally:
        store.close()


def test_tampered_file_fails_closed_as_unavailable(tmp_path: Path) -> None:
    store = _store(tmp_path)
    try:
        manifest = store.create("csv", TABLE, title="t", dependencies=[])
        (path,) = list(_session_dir(tmp_path).iterdir())
        path.write_bytes(b"tampered")
        with pytest.raises(ArtifactError) as caught:
            store.read(manifest["artifact_id"])
        assert _code(caught) == "artifact_unavailable"
        assert list(_session_dir(tmp_path).iterdir()) == []
    finally:
        store.close()


# ---------------------------------------------------------------------------
# Close and concurrency
# ---------------------------------------------------------------------------


def test_close_removes_files_and_session_directory(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.create("csv", TABLE, title="a", dependencies=[])
    store.create("json", TABLE, title="b", dependencies=[])
    assert len(list(_session_dir(tmp_path).iterdir())) == 2

    assert store.close() is True
    assert list(tmp_path.glob(".contextshrink-artifacts-*")) == []
    with pytest.raises(ArtifactError):
        store.create("csv", TABLE, title="c", dependencies=[])
    assert store.close() is True


def test_close_never_touches_unrelated_files(tmp_path: Path) -> None:
    bystander = tmp_path / "keep-me.txt"
    bystander.write_text("unrelated", encoding="utf-8")
    store = _store(tmp_path)
    store.create("csv", TABLE, title="a", dependencies=[])
    assert store.close() is True
    assert bystander.read_text(encoding="utf-8") == "unrelated"


def test_session_directories_are_unique_per_store(tmp_path: Path) -> None:
    first = _store(tmp_path)
    second = _store(tmp_path)
    try:
        assert len(list(tmp_path.glob(".contextshrink-artifacts-*"))) == 2
    finally:
        first.close()
        second.close()
    assert list(tmp_path.glob(".contextshrink-artifacts-*")) == []


def test_concurrent_creates_keep_accounting_consistent(tmp_path: Path) -> None:
    per_artifact = len(render_csv(TABLE).encode("utf-8"))
    allowed = 20
    store = _store(tmp_path, max_items=1000, max_total_bytes=per_artifact * allowed)
    results: list[str] = []
    results_lock = threading.Lock()
    errors: list[BaseException] = []

    def worker() -> None:
        for _ in range(10):
            try:
                store.create("csv", TABLE, title="t", dependencies=[])
                outcome = "ok"
            except ArtifactError as error:
                outcome = error.code
            except BaseException as error:  # pragma: no cover - reported below
                errors.append(error)
                return
            with results_lock:
                results.append(outcome)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    try:
        assert errors == []
        assert len(results) == 80
        assert results.count("ok") == allowed
        assert set(results) - {"ok"} == {"artifact_store_full"}
        listed = store.list()
        assert len(listed) == allowed
        assert len(list(_session_dir(tmp_path).iterdir())) == allowed
        assert sum(item["utf8_bytes"] for item in listed) == per_artifact * allowed
    finally:
        assert store.close() is True
    assert list(tmp_path.glob(".contextshrink-artifacts-*")) == []


def test_render_json_rejects_duplicate_columns_instead_of_dropping_values() -> None:
    table = ResultTable(columns=("a", "a"), rows=(("1", "2"),))
    with pytest.raises(ArtifactError) as caught:
        render_json(table)
    assert caught.value.code == "artifact_columns_invalid"


def test_new_store_sweeps_only_expired_generated_files_from_old_sessions(tmp_path: Path) -> None:
    import os

    from horizon.desktop_mcp.artifacts import STALE_ARTIFACT_SECONDS, sweep_stale_sessions

    old_session = tmp_path / (".contextshrink-artifacts-" + "a" * 32)
    old_session.mkdir()
    stale = old_session / ("artifact-" + "b" * 32 + ".csv")
    fresh = old_session / ("artifact-" + "c" * 32 + ".csv")
    foreign = old_session / "notes.csv"
    for path in (stale, fresh, foreign):
        path.write_text("x", encoding="utf-8")
    bystander = tmp_path / ("artifact-" + "d" * 32 + ".csv")
    bystander.write_text("x", encoding="utf-8")
    now = 10_000_000.0
    old = now - STALE_ARTIFACT_SECONDS - 1
    for path in (stale, foreign, bystander):
        os.utime(path, (old, old))
    os.utime(fresh, (now, now))

    assert sweep_stale_sessions(tmp_path, now=now) == 1
    assert not stale.exists()
    assert fresh.exists() and foreign.exists() and bystander.exists()
    assert old_session.exists()  # not empty, so never removed

    fresh.unlink()
    foreign.unlink()
    sweep_stale_sessions(tmp_path, now=now)
    assert not old_session.exists()
