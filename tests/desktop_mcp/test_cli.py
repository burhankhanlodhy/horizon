"""The diagnostic CLI must preserve useful failures without leaking inputs."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from horizon.desktop_mcp import capabilities
from horizon.desktop_mcp.__main__ import main


def test_failed_probe_exits_nonzero(monkeypatch, capsys) -> None:
    async def failed_probe(output=None) -> dict:
        return {"check": "stdio_probe", "status": "failed", "error": "probe_timeout"}

    monkeypatch.setattr(capabilities, "run_probe", failed_probe)
    assert main(["probe"]) == 1
    streams = capsys.readouterr()
    assert json.loads(streams.out)["status"] == "failed"
    assert streams.err == ""


def test_doctor_write_failure_exits_nonzero_without_echoing_path(tmp_path, capsys) -> None:
    assert main(["doctor", "--output", str(tmp_path)]) == 2
    streams = capsys.readouterr()
    report = json.loads(streams.out)
    assert report["output_error"].startswith("report_write_failed:")
    assert str(tmp_path) not in streams.out + streams.err


def test_missing_config_reports_only_sanitized_stderr(tmp_path, capsys) -> None:
    path = tmp_path / "sensitive-config-name.json"
    assert main(["serve", "--config", str(path)]) == 2
    streams = capsys.readouterr()
    assert streams.out == ""
    assert json.loads(streams.err)["error"] == "config_unreadable"
    assert str(path) not in streams.err


def test_absolute_stdio_launcher_exits_cleanly_when_stdin_closes(tmp_path) -> None:
    root = Path(__file__).resolve().parents[2]
    completed = subprocess.run(
        [sys.executable, str(root / "desktop" / "mcp" / "launch_stdio.py")],
        input="",
        capture_output=True,
        text=True,
        cwd=tmp_path,
        timeout=10,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == ""
    assert completed.stderr == ""
