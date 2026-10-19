"""Regression tests for the independent review of the Code-tab patcher.

Each test names the review item it covers (H = high, M = medium, L = low).
Fake roots and stand-in bytes only; environment is always passed explicitly.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from horizon import desktop_codetab as dc
from tests.test_desktop_codetab import (
    FOREIGN_BYTES,
    LAUNCHER_BYTES,
    LAUNCHER_V2_BYTES,
    REAL_BYTES,
    Layout,
    _sha,
)


@pytest.fixture
def layout(tmp_path: Path) -> Layout:
    return Layout(tmp_path)


def _states(result) -> list[tuple[str, str]]:
    return [(report.action, report.status) for report in result.folders]


def _write_folder(
    root: Path, version: str, digest: str, claude: bytes | None, payload: bytes = REAL_BYTES
) -> Path:
    folder = root / version / digest
    folder.mkdir(parents=True, exist_ok=True)
    if claude is not None:
        (folder / "claude.exe").write_bytes(claude)
    (folder / ".payload").write_text(
        json.dumps({"sha256": _sha(payload), "size": len(payload)}), encoding="utf-8"
    )
    return folder


# ---------------------------------------------------------------------------
# H1: a locked launcher during an update must not abort the run
# ---------------------------------------------------------------------------


def test_h1_locked_launcher_update_finishes_and_other_roots_update(layout: Layout) -> None:
    root2 = layout.base / "roots2" / "claude-code"
    root2.mkdir(parents=True)
    folder1 = layout.folder("1.0.0", "aaa")
    folder2 = _write_folder(root2, "1.0.0", "bbb", REAL_BYTES)
    roots = [layout.root, root2]

    # Install v1 into both roots.
    first = layout.install(roots=roots)
    assert [r.status for r in first.folders] == ["real", "real"]

    # Hold root2's launcher open, as a running Code session does, then install v2.
    layout.launcher.write_bytes(LAUNCHER_V2_BYTES)
    with open(folder2 / "claude.exe", "rb"):
        second = layout.install(roots=roots)

    # The run finished: root1 is on v2, and root2 is reported as locked.
    by_folder = {r.folder: r for r in second.folders}
    assert by_folder[folder1].status == "patched"
    assert (folder1 / "claude.exe").read_bytes() == LAUNCHER_V2_BYTES
    assert by_folder[folder2].status == "locked"
    assert (folder2 / "claude.exe").read_bytes() == LAUNCHER_BYTES  # still runnable v1

    # Once released, the next pass (what --watch does) finishes root2.
    third = layout.install(roots=roots)
    assert {r.folder: r.action for r in third.folders}[folder2] == "updated"
    assert (folder2 / "claude.exe").read_bytes() == LAUNCHER_V2_BYTES


def test_h1_watch_survives_a_pass_that_raises(layout: Layout) -> None:
    layout.folder("1.0.0", "aaa")
    calls: list[str] = []
    errors: list[str] = []

    def run():
        calls.append("run")
        if len(calls) == 1:
            raise dc.CodetabError("another codetab operation holds the lock")
        return layout.install()

    dc.watch(
        run,
        interval=0,
        iterations=2,
        sleep=lambda _: None,
        on_error=lambda e: errors.append(str(e)),
    )
    assert len(calls) == 2
    assert errors == ["another codetab operation holds the lock"]
    assert (layout.root / "1.0.0" / "aaa" / "claude.exe").read_bytes() == LAUNCHER_BYTES


# ---------------------------------------------------------------------------
# H1 Case 4 and L4: foreign file handling and rollback
# ---------------------------------------------------------------------------


def test_h1_case4_locked_foreign_file_is_reported_not_raised(layout: Layout) -> None:
    folder = _write_folder(layout.root, "1.0.0", "foreign", FOREIGN_BYTES)
    (folder / "claude.cs-real.exe").write_bytes(REAL_BYTES)
    with open(folder / "claude.exe", "rb"):
        result = layout.install()
    assert _states(result) == [("skipped", "locked")]
    assert (folder / "claude.exe").read_bytes() == FOREIGN_BYTES
    assert (folder / "claude.cs-real.exe").read_bytes() == REAL_BYTES


def test_l4_failed_placement_after_displacing_restores_runnable_real_binary(
    layout: Layout, monkeypatch
) -> None:
    folder = _write_folder(layout.root, "1.0.0", "foreign", FOREIGN_BYTES)
    (folder / "claude.cs-real.exe").write_bytes(REAL_BYTES)

    def fail(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(dc, "_place_launcher", fail)
    result = layout.install()

    assert _states(result) == [("skipped", "error")]
    # The verified real binary is back at claude.exe, so the folder runs again.
    assert (folder / "claude.exe").read_bytes() == REAL_BYTES
    assert not (folder / "claude.cs-real.exe").exists()
    # The foreign file was renamed aside, not deleted.
    assert (folder / "claude.exe.cs-displaced-1").read_bytes() == FOREIGN_BYTES


# ---------------------------------------------------------------------------
# L2: remove() trusts only what it can verify
# ---------------------------------------------------------------------------


def test_l2_state_entry_outside_roots_is_never_touched(layout: Layout) -> None:
    outside = layout.base / "elsewhere" / "1.0.0" / "evil"
    outside.mkdir(parents=True)
    (outside / "claude.exe").write_bytes(LAUNCHER_BYTES)
    (outside / "claude.cs-real.exe").write_bytes(REAL_BYTES)
    (outside / ".payload").write_text(
        json.dumps({"sha256": _sha(REAL_BYTES), "size": len(REAL_BYTES)}), encoding="utf-8"
    )
    # A tampered state file claims this folder and this launcher hash.
    layout.data.mkdir(parents=True)
    (layout.data / "codetab-state.json").write_text(
        json.dumps(
            {
                "v": 1,
                "launcher_sha256": [_sha(LAUNCHER_BYTES)],
                "folders": {
                    str(outside): {
                        "launcher_sha256": _sha(LAUNCHER_BYTES),
                        "real_sha256": _sha(REAL_BYTES),
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    result = layout.remove()
    assert any(r.status == "outside-roots" for r in result.folders)
    assert (outside / "claude.exe").read_bytes() == LAUNCHER_BYTES
    assert (outside / "claude.cs-real.exe").read_bytes() == REAL_BYTES


def test_l2_unrecorded_claude_exe_is_never_overwritten(layout: Layout) -> None:
    # Looks like a launcher, but nothing recorded it. Remove must not replace it.
    folder = _write_folder(layout.root, "1.0.0", "unknown", LAUNCHER_BYTES)
    (folder / "claude.cs-real.exe").write_bytes(REAL_BYTES)
    result = layout.remove()
    assert _states(result) == [("skipped", "foreign")]
    assert (folder / "claude.exe").read_bytes() == LAUNCHER_BYTES
    assert (folder / "claude.cs-real.exe").read_bytes() == REAL_BYTES


def test_l2_install_records_the_launcher_it_placed(layout: Layout) -> None:
    folder = layout.folder()
    layout.install()
    state = json.loads((layout.data / "codetab-state.json").read_text(encoding="utf-8"))
    record = state["folders"][str(folder)]
    assert record["launcher_sha256"] == _sha(LAUNCHER_BYTES)
    assert _sha(LAUNCHER_BYTES) in state["launcher_sha256"]


# ---------------------------------------------------------------------------
# L3: exclusive lock and unique temp names
# ---------------------------------------------------------------------------


def test_l3_held_lock_makes_a_manual_install_wait_then_fail_without_changes(layout: Layout) -> None:
    folder = layout.folder()
    layout.data.mkdir(parents=True)
    (layout.data / "codetab.lock").write_text("held", encoding="utf-8")
    with pytest.raises(dc.CodetabError, match="another codetab operation holds"):
        layout.install(lock_timeout=0.3)
    assert (folder / "claude.exe").read_bytes() == REAL_BYTES
    assert (layout.data / "codetab.lock").exists()  # not ours, so not removed


def test_l3_stale_lock_is_taken_over_and_released(layout: Layout) -> None:
    layout.folder()
    layout.data.mkdir(parents=True)
    lock = layout.data / "codetab.lock"
    lock.write_text("dead", encoding="utf-8")
    old = time.time() - dc.STALE_LOCK_SECONDS - 60
    os.utime(lock, (old, old))
    result = layout.install(lock_timeout=0.3)
    assert [r.status for r in result.folders] == ["real"]
    assert not lock.exists()


def test_l3_lock_is_released_after_install(layout: Layout) -> None:
    layout.folder()
    layout.install()
    assert not (layout.data / "codetab.lock").exists()


def test_l3_user_files_named_like_temp_files_survive(layout: Layout) -> None:
    folder = layout.folder()
    (folder / "claude.exe.cs-tmp").write_bytes(b"user file")
    layout.data.mkdir(parents=True, exist_ok=True)
    (layout.data / "codetab.json.tmp").write_bytes(b"user file")
    layout.install()
    assert (folder / "claude.exe.cs-tmp").read_bytes() == b"user file"
    assert (layout.data / "codetab.json.tmp").read_bytes() == b"user file"
    assert (folder / "claude.exe").read_bytes() == LAUNCHER_BYTES


# ---------------------------------------------------------------------------
# L11: codetab.json is kept (disabled) while a folder is still patched or locked
# ---------------------------------------------------------------------------


def test_l11_remove_keeps_config_disabled_while_a_folder_is_locked(layout: Layout) -> None:
    folder = layout.folder()
    layout.install()
    with open(folder / "claude.exe", "rb"):
        result = layout.remove()
    assert _states(result) == [("skipped", "locked")]
    config = json.loads((layout.data / "codetab.json").read_text(encoding="utf-8"))
    assert config["enabled"] is False
    assert result.config_disabled is True and result.config_removed is False
    # Still recorded, so a later remove can finish the job.
    assert (layout.data / "codetab-state.json").exists()


def test_l11_remove_deletes_config_once_everything_is_restored(layout: Layout) -> None:
    layout.folder()
    layout.install()
    result = layout.remove()
    assert result.config_removed is True
    assert not (layout.data / "codetab.json").exists()


# ---------------------------------------------------------------------------
# Junctions and symlinks are never followed
# ---------------------------------------------------------------------------


def _make_junction(link: Path, target: Path) -> bool:
    if os.name != "nt":
        return False
    completed = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True,
        text=True,
    )
    return completed.returncode == 0


def test_junction_to_a_real_folder_is_not_followed(layout: Layout, tmp_path: Path) -> None:
    target_root = tmp_path / "elsewhere" / "1.0.0"
    real_folder = _write_folder(tmp_path / "elsewhere", "1.0.0", "realhash", REAL_BYTES)
    link = layout.root / "2.0.0"
    if not _make_junction(link, target_root):
        pytest.skip("cannot create a junction on this machine")

    result = layout.install()
    assert any(r.status == "reparse" and r.folder == link for r in result.folders)
    # The folder behind the junction was not patched.
    assert (real_folder / "claude.exe").read_bytes() == REAL_BYTES
    assert not (real_folder / "claude.cs-real.exe").exists()


def test_junction_skipped_by_status_and_remove_too(layout: Layout, tmp_path: Path) -> None:
    target_root = tmp_path / "elsewhere" / "1.0.0"
    real_folder = _write_folder(tmp_path / "elsewhere", "1.0.0", "realhash", REAL_BYTES)
    link = layout.root / "2.0.0"
    if not _make_junction(link, target_root):
        pytest.skip("cannot create a junction on this machine")
    status = layout.status()
    assert any(r.status == "reparse" for r in status.folders)
    result = layout.remove()
    assert any(r.status == "reparse" for r in result.folders)
    assert (real_folder / "claude.exe").read_bytes() == REAL_BYTES


def test_file_symlink_for_claude_exe_is_not_followed(layout: Layout, tmp_path: Path) -> None:
    folder = layout.folder(claude=None)
    victim = tmp_path / "victim.exe"
    victim.write_bytes(REAL_BYTES)
    try:
        (folder / "claude.exe").symlink_to(victim)
    except (OSError, NotImplementedError):
        pytest.skip("cannot create symlinks on this machine")
    result = layout.install()
    assert _states(result) == [("skipped", "reparse")]
    assert victim.read_bytes() == REAL_BYTES
