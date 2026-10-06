"""npm's extensionless sh shims are not runnable on Windows (WinError 193)."""

from __future__ import annotations

import pytest

from horizon.cli import wrap


@pytest.mark.parametrize("tool", ["codex", "opencode"])
def test_windows_prefers_cmd_over_bare_sh_shim(monkeypatch, tmp_path, tool) -> None:
    shim = tmp_path / tool  # POSIX sh shim: WinError 193 if launched
    shim.write_text("#!/bin/sh\n")
    found = {tool: str(shim), f"{tool}.cmd": str(tmp_path / f"{tool}.cmd")}
    monkeypatch.setattr(wrap.os, "name", "nt")
    monkeypatch.setattr(wrap.shutil, "which", lambda name: found.get(name))
    assert wrap._resolve_windows_launcher(tool) == str(tmp_path / f"{tool}.cmd")


def test_windows_prefers_exe_over_shim_and_keeps_shim_as_last_resort(monkeypatch, tmp_path) -> None:
    shim = tmp_path / "codex"
    shim.write_text("#!/bin/sh\n")
    monkeypatch.setattr(wrap.os, "name", "nt")
    found = {"codex": str(shim), "codex.exe": "C:/bin/codex.exe", "codex.cmd": "C:/npm/codex.cmd"}
    monkeypatch.setattr(wrap.shutil, "which", lambda name: found.get(name))
    assert wrap._resolve_windows_launcher("codex") == "C:/bin/codex.exe"
    monkeypatch.setattr(wrap.shutil, "which", lambda name: str(shim) if name == "codex" else None)
    assert wrap._resolve_windows_launcher("codex") == str(shim)


def test_windows_keeps_runnable_hits(monkeypatch) -> None:
    monkeypatch.setattr(wrap.os, "name", "nt")
    monkeypatch.setattr(wrap.shutil, "which", lambda name: f"C:/bin/{name}.EXE" if name == "codex" else None)
    assert wrap._resolve_windows_launcher("codex") == "C:/bin/codex.EXE"
    monkeypatch.setattr(wrap.shutil, "which", lambda name: None)
    assert wrap._resolve_windows_launcher("codex") is None


def test_non_windows_uses_plain_lookup(monkeypatch) -> None:
    monkeypatch.setattr(wrap.os, "name", "posix")
    monkeypatch.setattr(wrap.shutil, "which", lambda name: f"/usr/local/bin/{name}")
    assert wrap._resolve_windows_launcher("codex") == "/usr/local/bin/codex"
