"""Tests for the Code-tab patcher (horizon.desktop_codetab).

Every test uses fake roots, payloads and a stand-in launcher under tmp_path.
Environment is passed explicitly so the real Claude Desktop folders, the real
%LOCALAPPDATA%\\ContextShrink data folder and ~/.claude are never read or written.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest

from horizon import desktop_codetab as dc

REAL_BYTES = b"REAL-CLAUDE-CODE-BINARY-v1" * 64
NEW_REAL_BYTES = b"REAL-CLAUDE-CODE-BINARY-v2" * 64
LAUNCHER_BYTES = b"LAUNCHER-STANDIN-v1" * 32
LAUNCHER_V2_BYTES = b"LAUNCHER-STANDIN-v2" * 32
FOREIGN_BYTES = b"VACHI-STYLE-TRAMPOLINE" * 16


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class Layout:
    """A fake Claude Desktop root with one or more version/hash folders."""

    def __init__(self, base: Path) -> None:
        self.base = base
        self.root = base / "roots" / "claude-code"
        self.root.mkdir(parents=True)
        self.data = base / "data" / "claude-code"
        self.data.parent.mkdir(parents=True, exist_ok=True)
        self.launcher = base / "src" / "claude-code-launcher.exe"
        self.launcher.parent.mkdir(parents=True, exist_ok=True)
        self.launcher.write_bytes(LAUNCHER_BYTES)
        self.environ = {"CLAUDE_CONFIG_DIR": str(base / "no-claude-settings")}

    def folder(
        self,
        version: str = "1.2.3",
        digest: str = "abc123",
        *,
        claude: bytes | None = REAL_BYTES,
        payload: dict | None | str = "auto",
    ) -> Path:
        folder = self.root / version / digest
        folder.mkdir(parents=True, exist_ok=True)
        if claude is not None:
            (folder / "claude.exe").write_bytes(claude)
        if payload == "auto":
            payload = {"sha256": _sha(REAL_BYTES), "size": len(REAL_BYTES)}
        if payload is not None:
            (folder / ".payload").write_text(json.dumps(payload), encoding="utf-8")
        return folder

    def install(self, **kwargs) -> dc.InstallResult:
        kwargs.setdefault("roots", [self.root])
        kwargs.setdefault("data", self.data)
        kwargs.setdefault("launcher", self.launcher)
        kwargs.setdefault("environ", self.environ)
        return dc.install(**kwargs)

    def remove(self, **kwargs) -> dc.RemoveResult:
        kwargs.setdefault("roots", [self.root])
        kwargs.setdefault("data", self.data)
        kwargs.setdefault("environ", self.environ)
        return dc.remove(**kwargs)

    def status(self) -> dc.StatusResult:
        return dc.status(
            roots=[self.root], data=self.data, launcher=self.launcher, environ=self.environ
        )


@pytest.fixture
def layout(tmp_path: Path) -> Layout:
    return Layout(tmp_path)


def _states(result) -> list[tuple[str, str]]:
    return [(report.action, report.status) for report in result.folders]


# ---------------------------------------------------------------------------
# install / remove basics
# ---------------------------------------------------------------------------


def test_install_patches_real_folder_and_keeps_real_binary(layout: Layout) -> None:
    folder = layout.folder()
    result = layout.install()

    assert _states(result) == [("patched", "real")]
    assert (folder / "claude.exe").read_bytes() == LAUNCHER_BYTES
    assert (folder / "claude.cs-real.exe").read_bytes() == REAL_BYTES
    assert not (folder / "claude.exe.cs-tmp").exists()

    config = json.loads(layout.data.joinpath("codetab.json").read_text(encoding="utf-8"))
    assert config == {
        "v": 1,
        "enabled": True,
        "base_url": "http://127.0.0.1:18788",
        "env": {},
        "health_port": 18788,
    }
    state = json.loads(layout.data.joinpath("codetab-state.json").read_text(encoding="utf-8"))
    assert state["launcher_sha256"] == [_sha(LAUNCHER_BYTES)]
    assert str(folder) in state["folders"]


def test_install_is_idempotent(layout: Layout) -> None:
    layout.folder()
    layout.install()
    again = layout.install()
    assert _states(again) == [("up-to-date", "patched")]


def test_install_updates_older_launcher_copy(layout: Layout) -> None:
    folder = layout.folder()
    layout.install()
    layout.launcher.write_bytes(LAUNCHER_V2_BYTES)
    result = layout.install()
    assert _states(result) == [("updated", "patched")]
    assert (folder / "claude.exe").read_bytes() == LAUNCHER_V2_BYTES
    assert (folder / "claude.cs-real.exe").read_bytes() == REAL_BYTES


def test_remove_restores_real_binary_and_deletes_only_placed_files(layout: Layout) -> None:
    folder = layout.folder()
    layout.install()
    result = layout.remove()

    assert _states(result) == [("restored", "real")]
    assert (folder / "claude.exe").read_bytes() == REAL_BYTES
    assert not (folder / "claude.cs-real.exe").exists()
    assert not layout.data.joinpath("codetab.json").exists()
    assert not layout.data.joinpath("codetab-state.json").exists()
    assert result.config_removed is True


def test_remove_refuses_when_stash_does_not_match_payload(layout: Layout) -> None:
    folder = layout.folder()
    layout.install()
    (folder / "claude.cs-real.exe").write_bytes(b"tampered")
    result = layout.remove()
    assert _states(result)[0][0] == "skipped"
    assert (folder / "claude.cs-real.exe").read_bytes() == b"tampered"
    assert (folder / "claude.exe").read_bytes() == LAUNCHER_BYTES


def test_remove_never_deletes_a_real_binary_when_both_copies_are_real(layout: Layout) -> None:
    folder = layout.folder()
    layout.install()
    # Simulate an interrupted removal: claude.exe is real again, stash remains.
    (folder / "claude.exe").write_bytes(REAL_BYTES)
    result = layout.remove()
    assert _states(result)[0] == ("skipped", "conflict")
    assert (folder / "claude.exe").read_bytes() == REAL_BYTES
    assert (folder / "claude.cs-real.exe").read_bytes() == REAL_BYTES


# ---------------------------------------------------------------------------
# Refusals and foreign files
# ---------------------------------------------------------------------------


def test_foreign_trampoline_refused_without_verified_stash(layout: Layout) -> None:
    folder = layout.folder(claude=FOREIGN_BYTES)
    result = layout.install()
    assert _states(result) == [("skipped", "foreign")]
    assert (folder / "claude.exe").read_bytes() == FOREIGN_BYTES
    assert not (folder / "claude.cs-real.exe").exists()


def test_foreign_trampoline_refused_when_stash_mismatches(layout: Layout) -> None:
    folder = layout.folder(claude=FOREIGN_BYTES)
    (folder / "claude.cs-real.exe").write_bytes(b"not the payload")
    result = layout.install()
    assert _states(result) == [("skipped", "foreign")]
    assert (folder / "claude.exe").read_bytes() == FOREIGN_BYTES


def test_foreign_trampoline_is_renamed_aside_not_deleted(layout: Layout) -> None:
    folder = layout.folder(claude=FOREIGN_BYTES)
    (folder / "claude.cs-real.exe").write_bytes(REAL_BYTES)
    result = layout.install()
    assert _states(result) == [("patched", "patched")]
    assert (folder / "claude.exe").read_bytes() == LAUNCHER_BYTES
    assert (folder / "claude.exe.cs-displaced-1").read_bytes() == FOREIGN_BYTES
    assert (folder / "claude.cs-real.exe").read_bytes() == REAL_BYTES


def test_folder_without_payload_is_skipped_untouched(layout: Layout) -> None:
    folder = layout.folder(payload=None)
    result = layout.install()
    assert _states(result) == [("skipped", "no-payload")]
    assert (folder / "claude.exe").read_bytes() == REAL_BYTES
    assert not (folder / "claude.cs-real.exe").exists()


def test_malformed_payload_is_treated_as_missing(layout: Layout) -> None:
    folder = layout.folder(payload={"sha256": "not-hex", "size": 1})
    result = layout.install()
    assert _states(result) == [("skipped", "no-payload")]
    assert (folder / "claude.exe").read_bytes() == REAL_BYTES


def test_second_real_copy_is_a_conflict_and_both_files_stay(layout: Layout) -> None:
    folder = layout.folder()
    (folder / "claude.cs-real.exe").write_bytes(REAL_BYTES)
    result = layout.install()
    assert _states(result) == [("skipped", "conflict")]
    assert (folder / "claude.exe").read_bytes() == REAL_BYTES
    assert (folder / "claude.cs-real.exe").read_bytes() == REAL_BYTES


def test_install_refuses_when_launcher_missing_and_changes_nothing(layout: Layout) -> None:
    folder = layout.folder()
    layout.launcher.unlink()
    with pytest.raises(dc.CodetabError, match="launcher not found"):
        layout.install()
    assert (folder / "claude.exe").read_bytes() == REAL_BYTES
    assert not layout.data.joinpath("codetab.json").exists()


def test_malformed_state_stops_the_run_without_overwriting(layout: Layout) -> None:
    folder = layout.folder()
    layout.data.mkdir(parents=True)
    layout.data.joinpath("codetab-state.json").write_text("{broken", encoding="utf-8")
    with pytest.raises(dc.CodetabError, match="unreadable"):
        layout.install()
    assert (folder / "claude.exe").read_bytes() == REAL_BYTES
    assert layout.data.joinpath("codetab-state.json").read_text(encoding="utf-8") == "{broken"


# ---------------------------------------------------------------------------
# Locks, rollback, dry run, watch
# ---------------------------------------------------------------------------


def test_locked_claude_exe_is_skipped_and_retried(layout: Layout, monkeypatch) -> None:
    folder = layout.folder()
    real_replace = os.replace
    calls = {"n": 0}

    def flaky_replace(src, dst):
        if Path(src) == folder / "claude.exe" and calls["n"] == 0:
            calls["n"] += 1
            raise PermissionError("in use")
        return real_replace(src, dst)

    monkeypatch.setattr(dc.os, "replace", flaky_replace)
    first = layout.install()
    assert _states(first) == [("skipped", "locked")]
    assert (folder / "claude.exe").read_bytes() == REAL_BYTES
    assert not (folder / "claude.cs-real.exe").exists()

    second = layout.install()
    assert _states(second) == [("patched", "real")]
    assert (folder / "claude.exe").read_bytes() == LAUNCHER_BYTES


def test_failed_placement_rolls_back_to_real_binary(layout: Layout, monkeypatch) -> None:
    folder = layout.folder()

    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(dc, "_place_launcher", boom)
    result = layout.install()
    # The failure is reported for this folder, and the real binary runs again at once.
    assert _states(result) == [("skipped", "error")]
    assert "disk full" in result.folders[0].detail
    assert (folder / "claude.exe").read_bytes() == REAL_BYTES
    assert not (folder / "claude.cs-real.exe").exists()


def test_dry_run_changes_nothing(layout: Layout) -> None:
    folder = layout.folder()
    result = layout.install(dry_run=True)
    assert _states(result) == [("would-patch", "real")]
    assert (folder / "claude.exe").read_bytes() == REAL_BYTES
    assert not layout.data.exists() or not any(layout.data.iterdir())


def test_watch_patches_folders_that_appear_later(layout: Layout) -> None:
    layout.folder("1.0.0", "aaa")
    seen: list[list[tuple[str, str]]] = []
    sleeps: list[float] = []

    def run():
        return layout.install()

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        layout.folder("1.1.0", "bbb")  # Desktop downloads a new version

    dc.watch(
        run, interval=10, iterations=2, sleep=sleep, on_result=lambda r: seen.append(_states(r))
    )
    assert sleeps == [10]
    assert seen[0] == [("patched", "real")]
    assert seen[1] == [("up-to-date", "patched"), ("patched", "real")]
    assert (layout.root / "1.1.0" / "bbb" / "claude.exe").read_bytes() == LAUNCHER_BYTES


def _quiet(layout: Layout, clock=None, **kwargs) -> dc.QuietInstaller:
    kwargs.setdefault("full_pass_seconds", 3600.0)
    if clock is not None:
        kwargs["clock"] = clock
    return dc.QuietInstaller(
        roots=[layout.root],
        data=layout.data,
        launcher=layout.launcher,
        environ=layout.environ,
        **kwargs,
    )


def test_quiet_watcher_skips_idle_passes_without_hashing_or_writing(
    layout: Layout, monkeypatch
) -> None:
    """An idle watch pass reads no file contents and writes nothing (security tools
    flagged the old watcher, which hashed claude.exe every 10 seconds)."""
    layout.folder()
    quiet = _quiet(layout)
    assert _states(quiet()) == [("patched", "real")]

    hashed: list[Path] = []
    real_sha256_file = dc.sha256_file
    monkeypatch.setattr(dc, "sha256_file", lambda p: hashed.append(Path(p)) or real_sha256_file(p))
    written: list[Path] = []
    real_write = dc._write_text_atomic
    monkeypatch.setattr(
        dc, "_write_text_atomic", lambda p, t: written.append(p) or real_write(p, t)
    )
    for _ in range(3):
        assert quiet() is None
    assert hashed == [] and written == []
    assert not (layout.data / dc.LOCK_NAME).exists()


def test_quiet_watcher_runs_a_pass_when_desktop_adds_a_version(layout: Layout) -> None:
    layout.folder("1.0.0", "aaa")
    quiet = _quiet(layout)
    quiet()
    assert quiet() is None
    layout.folder("1.1.0", "bbb")  # Desktop downloads a new version
    assert _states(quiet()) == [("up-to-date", "patched"), ("patched", "real")]
    assert quiet() is None


def test_quiet_watcher_retries_after_a_locked_folder(layout: Layout, monkeypatch) -> None:
    folder = layout.folder()
    real_replace = os.replace
    calls = {"n": 0}

    def flaky_replace(src, dst):
        if Path(src) == folder / "claude.exe" and calls["n"] == 0:
            calls["n"] += 1
            raise PermissionError("in use")
        return real_replace(src, dst)

    monkeypatch.setattr(dc.os, "replace", flaky_replace)
    quiet = _quiet(layout)
    assert _states(quiet()) == [("skipped", "locked")]
    # Nothing on disk changed, but the locked pass is retried rather than skipped.
    assert _states(quiet()) == [("patched", "real")]
    assert quiet() is None


def test_quiet_watcher_reverifies_after_the_full_pass_interval(layout: Layout) -> None:
    layout.folder()
    now = {"t": 0.0}
    quiet = _quiet(layout, clock=lambda: now["t"], full_pass_seconds=100.0)
    quiet()
    now["t"] = 99.0
    assert quiet() is None
    now["t"] = 100.0
    assert _states(quiet()) == [("up-to-date", "patched")]


def test_unchanged_config_and_state_are_not_rewritten(layout: Layout) -> None:
    layout.folder()
    layout.install()
    config = layout.data / dc.CONFIG_NAME
    state = layout.data / dc.STATE_NAME
    before = (config.stat().st_mtime_ns, state.stat().st_mtime_ns)
    old = 1_000_000_000_000_000_000  # 2001-09-09, a whole second on every filesystem
    os.utime(config, ns=(old, old))
    os.utime(state, ns=(old, old))
    layout.install()
    assert (config.stat().st_mtime_ns, state.stat().st_mtime_ns) == (old, old)
    assert before != (old, old)


# ---------------------------------------------------------------------------
# Status, config, roots
# ---------------------------------------------------------------------------


def test_status_reports_each_state(layout: Layout) -> None:
    layout.folder("1.0.0", "real")
    layout.folder("1.0.1", "foreign", claude=FOREIGN_BYTES)
    layout.folder("1.0.2", "nopayload", payload=None)
    layout.folder("1.0.3", "gone", claude=None)
    # Before install: the real binary is unpatched and the foreign one is foreign.
    before = {report.folder.name: report.status for report in layout.status().folders}
    assert before["real"] == "real"
    assert before["foreign"] == "foreign"
    assert before["nopayload"] == "no-payload"
    assert before["gone"] == "missing"

    layout.install()
    after = {report.folder.name: report.status for report in layout.status().folders}
    assert after["real"] == "patched"
    assert after["foreign"] == "foreign"
    assert after["nopayload"] == "no-payload"
    result = layout.status()
    assert result.launcher_present is True
    assert result.config is None or result.config["enabled"] is True


def test_status_before_install_reports_real(layout: Layout) -> None:
    layout.folder()
    result = layout.status()
    assert [report.status for report in result.folders] == ["real"]
    assert result.config is None


def test_tool_search_flag_is_written_to_config(layout: Layout) -> None:
    layout.folder()
    layout.install(tool_search=True)
    config = json.loads(layout.data.joinpath("codetab.json").read_text(encoding="utf-8"))
    assert config["env"] == {"ENABLE_TOOL_SEARCH": "true"}


def test_base_url_must_be_loopback(layout: Layout) -> None:
    layout.folder()
    with pytest.raises(dc.CodetabError, match="loopback|127.0.0.1"):
        layout.install(base_url="http://example.com:18788")


def test_base_url_taken_from_settings_when_port_matches(layout: Layout) -> None:
    layout.folder()
    config_dir = layout.base / "claude-config"
    config_dir.mkdir()
    (config_dir / "settings.json").write_text(
        json.dumps({"env": {"ANTHROPIC_BASE_URL": "http://127.0.0.1:18788/k/machine42"}}),
        encoding="utf-8",
    )
    layout.install(environ={"CLAUDE_CONFIG_DIR": str(config_dir)})
    config = json.loads(layout.data.joinpath("codetab.json").read_text(encoding="utf-8"))
    assert config["base_url"] == "http://127.0.0.1:18788/k/machine42"


def test_settings_base_url_on_other_port_is_ignored(layout: Layout) -> None:
    layout.folder()
    config_dir = layout.base / "claude-config"
    config_dir.mkdir()
    (config_dir / "settings.json").write_text(
        json.dumps({"env": {"ANTHROPIC_BASE_URL": "http://127.0.0.1:9999/k/x"}}),
        encoding="utf-8",
    )
    layout.install(environ={"CLAUDE_CONFIG_DIR": str(config_dir)})
    config = json.loads(layout.data.joinpath("codetab.json").read_text(encoding="utf-8"))
    assert config["base_url"] == "http://127.0.0.1:18788"


def test_discover_roots_honours_override_and_drops_missing(tmp_path: Path) -> None:
    present = tmp_path / "present"
    present.mkdir()
    env = {dc.ENV_ROOTS: os.pathsep.join([str(present), str(tmp_path / "absent")])}
    assert dc.discover_roots(env) == [present]
    assert dc.discover_roots({dc.ENV_ROOTS: ""}) == []


def test_data_dir_override(tmp_path: Path) -> None:
    assert dc.data_dir({dc.ENV_DATA_DIR: str(tmp_path)}) == tmp_path


def test_config_keys_match_what_the_launcher_reads() -> None:
    config = dc.build_config(
        base_url="http://127.0.0.1:18788/k/x", health_port=18788, tool_search=False
    )
    assert set(config) == {"v", "enabled", "base_url", "env", "health_port"}


# ---------------------------------------------------------------------------
# CLI wiring
# ---------------------------------------------------------------------------


def test_cli_install_status_remove_round_trip(layout: Layout) -> None:
    from click.testing import CliRunner

    from horizon.cli.main import main

    folder = layout.folder()
    runner = CliRunner()
    common = [
        "--root",
        str(layout.root),
        "--data-dir",
        str(layout.data),
        "--launcher",
        str(layout.launcher),
    ]
    env = {"CLAUDE_CONFIG_DIR": layout.environ["CLAUDE_CONFIG_DIR"]}

    result = runner.invoke(main, ["codetab", "install", *common], env=env)
    assert result.exit_code == 0, result.output
    assert "patched" in result.output
    assert (folder / "claude.exe").read_bytes() == LAUNCHER_BYTES

    result = runner.invoke(main, ["codetab", "status", *common], env=env)
    assert result.exit_code == 0, result.output
    assert "patched" in result.output

    result = runner.invoke(
        main,
        ["codetab", "remove", "--root", str(layout.root), "--data-dir", str(layout.data)],
        env=env,
    )
    assert result.exit_code == 0, result.output
    assert (folder / "claude.exe").read_bytes() == REAL_BYTES


def test_cli_help_lists_codetab() -> None:
    from click.testing import CliRunner

    from horizon.cli.main import main

    result = CliRunner().invoke(main, ["--help"])
    assert "codetab" in result.output


# ---------------------------------------------------------------------------
# End-to-end with the built Rust binaries (skipped when they are not built)
# ---------------------------------------------------------------------------

_RUST_DIR = (
    Path(__file__).resolve().parents[1] / "desktop" / "claude-code-tools" / "target" / "release"
)


@pytest.mark.skipif(
    not (_RUST_DIR / "claude-code-launcher.exe").exists()
    or not (_RUST_DIR / "cs-test-child.exe").exists(),
    reason="release launcher not built (cargo build --release in desktop/claude-code-tools)",
)
def test_built_launcher_routes_a_patched_folder(layout: Layout) -> None:
    import shutil

    child_bytes = (_RUST_DIR / "cs-test-child.exe").read_bytes()
    folder = layout.root / "2.0.0" / "e2e"
    folder.mkdir(parents=True)
    (folder / "claude.exe").write_bytes(child_bytes)
    (folder / ".payload").write_text(
        json.dumps({"sha256": _sha(child_bytes), "size": len(child_bytes)}), encoding="utf-8"
    )
    shutil.copyfile(_RUST_DIR / "claude-code-launcher.exe", layout.launcher)

    layout.install(launcher=layout.launcher)
    assert (folder / "claude.exe").read_bytes() != child_bytes

    # The health route answers 200 with {"ok": true}, so the launcher routes the child.
    import socket
    import threading

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]

    def serve_health() -> None:
        try:
            conn, _ = listener.accept()
        except OSError:
            return
        with conn:
            conn.recv(4096)
            body = b'{"ok":true}'
            head = (
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                b"Content-Length: " + str(len(body)).encode() + b"\r\nConnection: close\r\n\r\n"
            )
            conn.sendall(head + body)

    threading.Thread(target=serve_health, daemon=True).start()
    try:
        config_path = layout.data / "codetab.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        config["health_port"] = port
        config["base_url"] = f"http://127.0.0.1:{port}/k/e2e"
        config_path.write_text(json.dumps(config), encoding="utf-8")

        env = dict(os.environ)
        env["CONTEXTSHRINK_DATA_DIR"] = str(layout.data.parent)
        env.pop("ANTHROPIC_BASE_URL", None)
        completed = subprocess.run(
            [str(folder / "claude.exe"), "--print-me", "x y"],
            env=env,
            capture_output=True,
            timeout=30,
        )
    finally:
        listener.close()
    assert completed.returncode == 0
    out = completed.stdout.decode("utf-8", errors="replace")
    assert "ENV ANTHROPIC_BASE_URL=" in out
    assert f"http://127.0.0.1:{port}/k/e2e".encode().hex() in out
