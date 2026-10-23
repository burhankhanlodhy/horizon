"""Bundle detection from the embedded build's files, not from the launcher's environment.

A background start runs the embedded build's pythonw.exe directly, so HORIZON_CLIENT_EXE and
HORIZON_LAUNCHER_PID are absent. Detection must still see the bundle, and the device-id test
override must stay ignored there. Environment variables never switch the bundle off.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from horizon import bundled_client
from horizon.bundled_client import CLIENT_EXE_ENV, LAUNCHER_PID_ENV
from horizon.code_engine import device

OVERRIDE = "attacker-chosen-machine-id"


@pytest.fixture
def clean(monkeypatch):
    monkeypatch.delenv(CLIENT_EXE_ENV, raising=False)
    monkeypatch.delenv(LAUNCHER_PID_ENV, raising=False)
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    return monkeypatch


def _embedded_build(tmp_path: Path, monkeypatch) -> tuple[Path, Path]:
    """A folder laid out like the embedded build: python\\ with python312._pth, app\\horizon_main.py."""
    engine = tmp_path / "engine"
    folder = engine / "python"
    folder.mkdir(parents=True)
    (engine / "app").mkdir()
    (folder / "python312._pth").write_text("", encoding="utf-8")
    (folder / "python.exe").write_bytes(b"python")
    (folder / "pythonw.exe").write_bytes(b"pythonw")
    script = engine / "app" / "horizon_main.py"
    script.write_text("", encoding="utf-8")
    monkeypatch.setattr(sys, "executable", str(folder / "pythonw.exe"))
    return folder.resolve(), script.resolve()


def _development_interpreter(tmp_path: Path, monkeypatch) -> Path:
    scripts = tmp_path / "venv" / "Scripts"
    scripts.mkdir(parents=True)
    (scripts / "python.exe").write_bytes(b"python")
    (scripts / "pythonw.exe").write_bytes(b"pythonw")
    monkeypatch.setattr(sys, "executable", str(scripts / "python.exe"))
    return scripts


def test_the_embedded_layout_is_recognised_without_any_environment_variable(
    clean, tmp_path
) -> None:
    folder, script = _embedded_build(tmp_path, clean)

    assert bundled_client.embedded_layout() == (folder, script)
    assert bundled_client.is_bundled_client() is True


def test_the_embedded_cli_runs_pythonw_without_the_launcher(clean, tmp_path) -> None:
    folder, script = _embedded_build(tmp_path, clean)

    expected = [str(folder / "pythonw.exe"), "-I", "-s", "-X", "utf8", str(script)]
    assert bundled_client.bundled_client_command() == expected
    assert bundled_client.cli_command() == expected


def test_environment_variables_cannot_switch_an_embedded_build_off(clean, tmp_path) -> None:
    _embedded_build(tmp_path, clean)
    clean.setenv(CLIENT_EXE_ENV, str(tmp_path / "missing.exe"))
    clean.setenv(LAUNCHER_PID_ENV, "1")
    assert bundled_client.is_bundled_client() is True

    clean.delenv(CLIENT_EXE_ENV)
    clean.setenv(LAUNCHER_PID_ENV, "0")
    assert bundled_client.is_bundled_client() is True


def test_the_machine_id_override_is_ignored_under_pythonw(clean, tmp_path) -> None:
    _embedded_build(tmp_path, clean)
    clean.setenv(device.MACHINE_ID_ENV, OVERRIDE)

    assert device.machine_id() != OVERRIDE


def test_a_development_interpreter_is_not_bundled(clean, tmp_path) -> None:
    _development_interpreter(tmp_path, clean)

    assert bundled_client.embedded_layout() is None
    assert bundled_client.is_bundled_client() is False
    assert bundled_client.cli_command() == [sys.executable, "-m", "horizon.cli"]


def test_the_machine_id_override_still_works_for_a_development_interpreter(clean, tmp_path) -> None:
    _development_interpreter(tmp_path, clean)
    clean.setenv(device.MACHINE_ID_ENV, OVERRIDE)

    assert device.machine_id() == OVERRIDE


def test_a_python_folder_without_the_app_script_is_not_embedded(clean, tmp_path) -> None:
    folder, script = _embedded_build(tmp_path, clean)
    script.unlink()

    assert bundled_client.embedded_layout() is None
    assert bundled_client.is_bundled_client() is False


def test_an_app_folder_without_the_pth_marker_is_not_embedded(clean, tmp_path) -> None:
    folder, _script = _embedded_build(tmp_path, clean)
    (folder / "python312._pth").unlink()

    assert bundled_client.embedded_layout() is None
    assert bundled_client.is_bundled_client() is False


def test_the_launcher_still_wins_when_the_stub_started_the_process(clean, tmp_path) -> None:
    _embedded_build(tmp_path, clean)
    launcher = tmp_path / "horizon.exe"
    launcher.write_bytes(b"stub")
    clean.setenv(CLIENT_EXE_ENV, str(launcher))

    assert bundled_client.bundled_client_command() == [str(launcher)]
