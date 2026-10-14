from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path

import pytest

from horizon.desktop_mcp.source_access import SourceAccessError, SourceReader


def test_read_preserves_exact_utf8_newlines_and_returns_version_metadata(tmp_path: Path) -> None:
    root = tmp_path / "approved"
    root.mkdir()
    source = root / "note.txt"
    raw = b"first line\r\nsecond line\nlast line"
    source.write_bytes(raw)
    reader = SourceReader((root,))

    opened = reader.read(str(source))

    assert opened.text == raw.decode("utf-8")
    assert opened.byte_count == len(raw)
    assert opened.digest == hashlib.sha256(raw).hexdigest()
    assert opened.root_key
    assert opened.relative_path == "note.txt"
    assert opened.identity
    assert reader.check_access(str(source)) == opened.root_key


def test_relative_source_requires_one_root_and_catalog_omits_contents(tmp_path: Path) -> None:
    root = tmp_path / "approved"
    root.mkdir()
    (root / "docs").mkdir()
    (root / "docs" / "memo.md").write_text("secret body", encoding="utf-8")
    reader = SourceReader((root,))

    assert reader.read("docs/memo.md").text == "secret body"
    entries = reader.catalog("memo", limit=5)
    assert len(entries) == 1
    assert entries[0]["source"] == str(root / "docs" / "memo.md")
    assert entries[0]["relative_path"] == "docs/memo.md"
    assert entries[0]["kind"] == "md"
    assert "text" not in entries[0]

    other = tmp_path / "other"
    other.mkdir()
    multiple_roots = SourceReader((root, other))
    with pytest.raises(SourceAccessError) as error:
        multiple_roots.read("docs/memo.md")
    assert error.value.code == "source_unapproved"


def test_paths_outside_grants_and_traversal_are_denied(tmp_path: Path) -> None:
    root = tmp_path / "approved"
    outside = tmp_path / "outside.txt"
    root.mkdir()
    outside.write_text("private", encoding="utf-8")
    reader = SourceReader((root,))

    for candidate in (str(outside), "../outside.txt", str(root / ".." / "outside.txt")):
        with pytest.raises(SourceAccessError) as error:
            reader.read(candidate)
        assert error.value.code == "source_unapproved"


def test_replacing_the_approved_root_at_the_same_path_breaks_the_anchor(tmp_path: Path) -> None:
    root = tmp_path / "approved"
    root.mkdir()
    source = root / "note.txt"
    source.write_text("first", encoding="utf-8")
    reader = SourceReader((root,))
    root.rename(tmp_path / "old-approved")
    root.mkdir()
    (root / "note.txt").write_text("replacement", encoding="utf-8")

    with pytest.raises(SourceAccessError) as error:
        reader.check_access(str(root / "note.txt"))
    assert error.value.code == "source_unapproved"


@pytest.mark.skipif(os.name == "nt", reason="POSIX test injects an in-place mutation")
def test_mutation_during_read_returns_a_sanitized_changed_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "approved"
    root.mkdir()
    source = root / "note.txt"
    source.write_bytes(b"before")
    reader = SourceReader((root,))
    original_read = os.read
    changed = False

    def read_then_mutate(descriptor: int, count: int) -> bytes:
        nonlocal changed
        result = original_read(descriptor, count)
        if result and not changed:
            changed = True
            source.write_bytes(b"changed while reading")
        return result

    monkeypatch.setattr(os, "read", read_then_mutate)
    with pytest.raises(SourceAccessError) as error:
        reader.read(str(source))
    assert error.value.code == "source_changed"


@pytest.mark.skipif(os.name != "nt", reason="Windows sharing semantics are Windows-specific")
def test_windows_read_handle_denies_concurrent_write_and_delete(tmp_path: Path) -> None:
    root = tmp_path / "approved"
    root.mkdir()
    source = root / "note.txt"
    original = b"captured source remains unchanged\r\n"
    source.write_bytes(original)
    reader = SourceReader((root,))

    # Hold the same validated handle used by SourceReader.read. CreateFileW
    # requests FILE_SHARE_READ only, so other handles cannot mutate or remove
    # the source during capture.
    descriptor = reader._open_validated(reader._roots[0], source)
    try:
        with pytest.raises(OSError):
            os.open(source, os.O_WRONLY | os.O_TRUNC)
        with pytest.raises(OSError):
            source.unlink()
        assert os.read(descriptor, len(original)) == original
    finally:
        os.close(descriptor)

    assert reader.read(str(source)).text == original.decode("utf-8")


def test_injected_handle_identity_change_returns_changed_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "approved"
    root.mkdir()
    source = root / "note.txt"
    source.write_text("stable bytes", encoding="utf-8")
    reader = SourceReader((root,))
    import horizon.desktop_mcp.source_access as source_access

    original_identity = source_access._identity_for_fd
    regular_file_calls = 0

    def changing_identity(descriptor: int, info: os.stat_result) -> str:
        nonlocal regular_file_calls
        identity = original_identity(descriptor, info)
        if stat.S_ISREG(info.st_mode):
            regular_file_calls += 1
            return f"{identity}-{regular_file_calls}"
        return identity

    import stat

    monkeypatch.setattr(source_access, "_identity_for_fd", changing_identity)
    with pytest.raises(SourceAccessError) as error:
        reader.read(str(source))
    assert error.value.code == "source_changed"


def test_symlink_escape_is_rejected_after_opened_handle_check(tmp_path: Path) -> None:
    root = tmp_path / "approved"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("private", encoding="utf-8")
    link = root / "escape.txt"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation is unavailable")
    reader = SourceReader((root,))

    with pytest.raises(SourceAccessError) as error:
        reader.read(str(link))
    assert error.value.code == "source_unapproved"


def test_oversized_binary_and_invalid_utf8_sources_fail_closed(tmp_path: Path) -> None:
    root = tmp_path / "approved"
    root.mkdir()
    (root / "large.txt").write_bytes(b"x" * 9)
    (root / "binary.txt").write_bytes(b"a\x00b")
    (root / "invalid.txt").write_bytes(b"\xff\xfe")
    reader = SourceReader((root,), max_source_bytes=8)

    cases = {
        "large.txt": "source_too_large",
        "binary.txt": "source_invalid_text",
        "invalid.txt": "source_invalid_text",
    }
    for name, expected_code in cases.items():
        with pytest.raises(SourceAccessError) as error:
            reader.read(str(root / name))
        assert error.value.code == expected_code


def test_unsupported_file_types_are_not_exposed_even_when_they_are_text(tmp_path: Path) -> None:
    root = tmp_path / "approved"
    root.mkdir()
    source = root / "script.py"
    source.write_text("print('not a source type')", encoding="utf-8")
    reader = SourceReader((root,))

    with pytest.raises(SourceAccessError) as error:
        reader.read(str(source))
    assert error.value.code == "source_unsupported"


@pytest.mark.parametrize(
    "suffix",
    [
        ".yaml",
        ".yml",
        ".ini",
        ".toml",
        ".conf",
        ".config",
        ".jsonl",
        ".ndjson",
        ".diff",
        ".patch",
        ".paths",
        ".search",
    ],
)
def test_phase_c_text_source_formats_are_strictly_readable(tmp_path: Path, suffix: str) -> None:
    root = tmp_path / "approved"
    root.mkdir()
    source = root / f"fixture{suffix}"
    source.write_bytes(b"synthetic: true\n")
    opened = SourceReader((root,)).read(str(source))
    assert opened.text.replace("\r\n", "\n") == "synthetic: true\n"
    assert opened.byte_count == len(opened.text.encode("utf-8"))


def test_catalog_skips_links_and_reports_traversal_truncation(tmp_path: Path) -> None:
    root = tmp_path / "approved"
    root.mkdir()
    (root / "target.txt").write_text("content", encoding="utf-8")
    try:
        (root / "linked.txt").symlink_to(root / "target.txt")
    except (OSError, NotImplementedError):
        pass
    for index in range(1001):
        (root / f"item-{index:04}.txt").touch()

    reader = SourceReader((root,))
    results = reader.catalog(limit=3)

    assert len(results) == 3
    assert reader.catalog_truncated is True
    assert all("text" not in item for item in results)
    if os.name != "nt" or (root / "linked.txt").exists():
        assert all(not item["source"].endswith("linked.txt") for item in results)


def test_catalog_marks_result_limit_as_partial(tmp_path: Path) -> None:
    root = tmp_path / "approved"
    root.mkdir()
    for name in ("a.txt", "b.txt"):
        (root / name).write_text(name, encoding="utf-8")
    reader = SourceReader((root,))

    results = reader.catalog(limit=1)

    assert len(results) == 1
    assert reader.catalog_truncated is True


@pytest.mark.skipif(os.name != "nt", reason="Windows junctions are Windows-specific")
def test_windows_junction_escape_is_not_cataloged_or_read(tmp_path: Path) -> None:
    root = tmp_path / "approved"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    secret = outside / "secret.txt"
    secret.write_text("private", encoding="utf-8")
    junction = root / "junction"
    created = subprocess.run(
        ["cmd.exe", "/c", "mklink", "/J", str(junction), str(outside)],
        capture_output=True,
        text=True,
        check=False,
    )
    if created.returncode != 0:
        pytest.skip("Windows junction creation is unavailable")
    reader = SourceReader((root,))

    assert all(item["name"] != "secret.txt" for item in reader.catalog())
    with pytest.raises(SourceAccessError) as error:
        reader.read(str(junction / "secret.txt"))
    assert error.value.code == "source_unapproved"


@pytest.mark.skipif(os.name != "nt", reason="alternate data streams are Windows-specific")
def test_windows_alternate_data_stream_path_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "approved"
    root.mkdir()
    source = root / "note.txt"
    source.write_text("ok", encoding="utf-8")
    reader = SourceReader((root,))

    with pytest.raises(SourceAccessError) as error:
        reader.read(f"{source}:secret")
    assert error.value.code == "source_unapproved"
