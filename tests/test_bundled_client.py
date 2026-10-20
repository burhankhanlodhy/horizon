"""How the bundled desktop client's own executable is found and reported."""

import sys
from pathlib import Path

import pytest

from horizon.bundled_client import (
    CLIENT_EXE_ENV,
    LAUNCHER_PID_ENV,
    bundled_client_command,
    cli_command,
    serving_pid,
)


@pytest.fixture
def clean_env(monkeypatch):
    monkeypatch.delenv(CLIENT_EXE_ENV, raising=False)
    monkeypatch.delenv(LAUNCHER_PID_ENV, raising=False)
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    return monkeypatch


def test_outside_a_bundle_the_cli_is_the_module(clean_env) -> None:
    assert bundled_client_command() is None
    assert cli_command() == [sys.executable, "-m", "horizon.cli"]


def test_frozen_build_is_its_own_cli(clean_env) -> None:
    clean_env.setattr(sys, "frozen", True, raising=False)
    assert bundled_client_command() == [sys.executable]
    assert cli_command() == [sys.executable]


def test_launcher_path_wins_when_it_exists(clean_env, tmp_path: Path) -> None:
    launcher = tmp_path / "horizon.exe"
    launcher.write_bytes(b"MZ")
    clean_env.setenv(CLIENT_EXE_ENV, str(launcher))
    assert bundled_client_command() == [str(launcher)]
    assert cli_command() == [str(launcher)]


def test_a_launcher_path_that_is_gone_is_ignored(clean_env, tmp_path: Path) -> None:
    clean_env.setenv(CLIENT_EXE_ENV, str(tmp_path / "missing.exe"))
    assert bundled_client_command() is None


def test_serving_pid_is_the_launcher_only_when_it_is_the_parent(clean_env) -> None:
    import os

    clean_env.setattr(os, "getppid", lambda: 4242)
    clean_env.setenv(LAUNCHER_PID_ENV, "4242")
    assert serving_pid() == 4242

    clean_env.setenv(LAUNCHER_PID_ENV, "9999")  # started by some other process
    assert serving_pid() == os.getpid()

    clean_env.delenv(LAUNCHER_PID_ENV)
    assert serving_pid() == os.getpid()


def test_serving_pid_ignores_a_malformed_launcher_pid(clean_env) -> None:
    import os

    clean_env.setenv(LAUNCHER_PID_ENV, "not-a-pid")
    assert serving_pid() == os.getpid()


def test_resolve_horizon_command_uses_the_bundled_launcher(clean_env, tmp_path: Path) -> None:
    from horizon.install.runtime import resolve_horizon_command

    launcher = tmp_path / "horizon.exe"
    launcher.write_bytes(b"MZ")
    clean_env.setenv(CLIENT_EXE_ENV, str(launcher))
    clean_env.setattr("horizon.install.runtime.shutil.which", lambda name: r"C:\other\horizon.exe")
    assert resolve_horizon_command() == [str(launcher)]
