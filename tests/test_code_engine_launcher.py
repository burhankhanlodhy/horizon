"""The engine launcher: WMI first on Windows, the detached start as the fallback, and a stop that waits for the process to end."""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from horizon import bundled_client
from horizon.code_engine import runtime
from horizon.code_engine.store import HOME_ENV


@pytest.fixture
def home(tmp_path, monkeypatch):
    path = tmp_path / "home"
    path.mkdir()
    monkeypatch.setenv(HOME_ENV, str(path))
    return path


PYTHONW_COMMAND = [
    "C:/ContextShrink/python/pythonw.exe",
    "-I",
    "-s",
    "-X",
    "utf8",
    "C:/ContextShrink/app/horizon_main.py",
]


@pytest.fixture
def as_windows(monkeypatch):
    """Make the runtime take its Windows branches on any host."""
    monkeypatch.setattr(
        runtime, "sys", SimpleNamespace(platform="win32", executable=sys.executable)
    )
    monkeypatch.setattr(bundled_client, "cli_command", lambda: ["C:/ContextShrink/horizon.exe"])
    monkeypatch.setattr(runtime, "background_command", lambda: list(PYTHONW_COMMAND))


def _no_popen(*args, **kwargs):
    raise AssertionError("the detached start must not run when WMI works")


def _log_first_line(home) -> str:
    return (home / runtime.LOG_FILE).read_text(encoding="utf-8").splitlines()[0]


# ── Launcher choice ───────────────────────────────────────────────────


def test_windows_starts_the_engine_through_wmi_and_not_popen(home, as_windows, monkeypatch) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.test:8080")
    created = []

    def wmi(command, cwd):
        created.append((command, cwd))

    result = runtime.spawn_detached(home, 18793, wmi_start=wmi, popen=_no_popen)

    assert result == "wmi"
    command, cwd = created[0]
    assert cwd == str(home)
    # The command is the windowless interpreter itself: no cmd.exe wrapper.
    assert command.startswith("C:/ContextShrink/python/pythonw.exe -I -s -X utf8 ")
    assert "cmd" not in command.lower()
    assert f"code-engine run --port 18793 --home {home}" in command
    assert _log_first_line(home) == "launcher: WMI Win32_Process.Create"
    saved = json.loads((home / runtime.LAUNCH_ENV_FILE).read_text(encoding="utf-8"))
    assert saved["env"]["HTTPS_PROXY"] == "http://proxy.test:8080"


def test_a_wmi_failure_falls_back_to_the_detached_start_and_says_so(
    home, as_windows, monkeypatch
) -> None:
    started = []

    class _Popen:
        def __init__(self, argv, **kwargs):
            started.append((argv, kwargs))

    def refused(command, cwd):
        raise OSError("access is denied")

    monkeypatch.setattr(runtime.subprocess, "Popen", _Popen)

    assert runtime.spawn_detached(home, 18793, wmi_start=refused) == "detached"

    argv, kwargs = started[0]
    assert argv[-6:] == ["code-engine", "run", "--port", "18793", "--home", str(home)]
    assert argv[: len(PYTHONW_COMMAND)] == PYTHONW_COMMAND
    assert kwargs["creationflags"] & runtime._DETACHED_PROCESS
    assert kwargs["creationflags"] & runtime._CREATE_NEW_PROCESS_GROUP
    assert kwargs["creationflags"] & runtime._CREATE_BREAKAWAY_FROM_JOB
    assert (
        _log_first_line(home) == "launcher: detached (WMI start failed: OSError: access is denied)"
    )
    assert not (home / runtime.LAUNCH_ENV_FILE).exists(), "the saved environment was left behind"


def test_the_detached_start_retries_without_breakaway_when_the_job_refuses_it(
    home, as_windows, monkeypatch
) -> None:
    flags_seen = []

    class _Popen:
        def __init__(self, argv, **kwargs):
            flags_seen.append(kwargs["creationflags"])
            if kwargs["creationflags"] & runtime._CREATE_BREAKAWAY_FROM_JOB:
                raise PermissionError("breakaway is not allowed")

    def refused(command, cwd):
        raise OSError("no WMI here")

    monkeypatch.setattr(runtime.subprocess, "Popen", _Popen)
    runtime.spawn_detached(home, 18793, wmi_start=refused)
    assert len(flags_seen) == 2
    assert not flags_seen[1] & runtime._CREATE_BREAKAWAY_FROM_JOB


def test_the_embedded_build_runs_its_own_pythonw(tmp_path, monkeypatch) -> None:
    engine = tmp_path / "engine"
    folder = engine / "python"
    folder.mkdir(parents=True)
    (engine / "app").mkdir()
    (folder / "python312._pth").write_text("", encoding="utf-8")
    (folder / "python.exe").write_bytes(b"python")
    (folder / "pythonw.exe").write_bytes(b"pythonw")
    script = engine / "app" / "horizon_main.py"
    script.write_text("", encoding="utf-8")
    monkeypatch.delenv(bundled_client.CLIENT_EXE_ENV, raising=False)
    monkeypatch.setattr(sys, "executable", str(folder / "python.exe"))
    monkeypatch.setattr(
        runtime, "sys", SimpleNamespace(platform="win32", executable=str(folder / "python.exe"))
    )

    assert runtime.background_command() == [
        str(folder.resolve() / "pythonw.exe"),
        "-I",
        "-s",
        "-X",
        "utf8",
        str(script.resolve()),
    ]


def test_the_venv_uses_the_pythonw_beside_its_interpreter(tmp_path, monkeypatch) -> None:
    scripts = tmp_path / "Scripts"
    scripts.mkdir()
    (scripts / "python.exe").write_bytes(b"python")
    (scripts / "pythonw.exe").write_bytes(b"pythonw")
    monkeypatch.delenv(bundled_client.CLIENT_EXE_ENV, raising=False)
    monkeypatch.setattr(
        runtime, "sys", SimpleNamespace(platform="win32", executable=str(scripts / "python.exe"))
    )

    assert runtime.background_command() == [str(scripts / "pythonw.exe"), "-m", "horizon.cli"]


def test_without_a_pythonw_the_cli_command_is_used(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv(bundled_client.CLIENT_EXE_ENV, raising=False)
    monkeypatch.setattr(
        runtime, "sys", SimpleNamespace(platform="win32", executable=str(tmp_path / "python.exe"))
    )
    monkeypatch.setattr(bundled_client, "cli_command", lambda: ["the-cli"])
    assert runtime.background_command() == ["the-cli"]


def test_the_background_output_goes_to_the_log_when_there_is_no_console(home, monkeypatch) -> None:
    dup_calls = []
    monkeypatch.setattr(runtime.os, "dup2", lambda src, dst: dup_calls.append(dst))
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)

    runtime.attach_background_output(home)
    print("engine output line", file=sys.stdout)
    sys.stdout.flush()

    assert dup_calls == [1, 2]
    assert "engine output line" in (home / runtime.LOG_FILE).read_text(encoding="utf-8")
    sys.stdout.close()


def test_a_terminal_keeps_its_own_output(home, monkeypatch) -> None:
    dup_calls = []
    monkeypatch.setattr(runtime.os, "dup2", lambda src, dst: dup_calls.append(dst))
    runtime.attach_background_output(home)
    assert dup_calls == []
    assert not (home / runtime.LOG_FILE).exists()


@pytest.mark.skipif(sys.platform == "win32", reason="the POSIX start; Windows starts through WMI")
def test_non_windows_starts_in_a_new_session(home, monkeypatch) -> None:
    started = []

    class _Popen:
        def __init__(self, argv, **kwargs):
            started.append(kwargs)

    monkeypatch.setattr(runtime.subprocess, "Popen", _Popen)
    assert runtime.spawn_detached(home, 18793) == "detached"
    assert started[0]["start_new_session"] is True


# ── The WMI call ──────────────────────────────────────────────────────


def test_the_wmi_call_passes_the_command_by_environment_only(monkeypatch) -> None:
    seen = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        seen["env"] = kwargs["env"]
        return subprocess.CompletedProcess(argv, 0, stdout="4321\n", stderr="")

    monkeypatch.setattr(runtime.subprocess, "run", fake_run)
    runtime._wmi_create('"C:\\cmd.exe" /d /s /c "echo hi"', "C:\\home")

    assert seen["env"]["CONTEXTSHRINK_ENGINE_COMMAND"] == '"C:\\cmd.exe" /d /s /c "echo hi"'
    assert seen["env"]["CONTEXTSHRINK_ENGINE_DIR"] == "C:\\home"
    script = base64.b64decode(seen["argv"][-1]).decode("utf-16-le")
    assert "Win32_Process" in script and "-MethodName Create" in script
    assert "echo hi" not in script, "the command must not be written into the script"
    assert "ShowWindow = [uint16]0" in script


@pytest.mark.parametrize(
    "returncode, stdout",
    [(1, "4321"), (0, ""), (0, "not a pid")],
)
def test_the_wmi_call_fails_when_powershell_fails(monkeypatch, returncode, stdout) -> None:
    monkeypatch.setattr(
        runtime.subprocess,
        "run",
        lambda argv, **kwargs: subprocess.CompletedProcess(
            argv, returncode, stdout=stdout, stderr=""
        ),
    )
    with pytest.raises(OSError):
        runtime._wmi_create("cmd", "dir")


def test_the_wmi_call_times_out_as_a_failure(monkeypatch) -> None:
    def slow(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, 60)

    monkeypatch.setattr(runtime.subprocess, "run", slow)
    with pytest.raises(OSError, match="did not answer"):
        runtime._wmi_create("cmd", "dir")


# ── The starter's environment ─────────────────────────────────────────


def test_the_engine_applies_the_starters_environment_once(home, monkeypatch) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.test:8080")
    runtime._write_launch_env(home)
    target: dict[str, str] = {"KEEP": "yes"}

    assert runtime.take_launch_environment(home, target) > 0
    assert target["HTTPS_PROXY"] == "http://proxy.test:8080"
    assert target["KEEP"] == "yes"
    assert not (home / runtime.LAUNCH_ENV_FILE).exists()
    assert runtime.take_launch_environment(home, {}) == 0


@pytest.mark.parametrize(
    "content",
    ["not json", '{"env": ["a"]}', '{"env": {"=C:": "x", "GOOD": 3}}', "[]"],
)
def test_a_malformed_saved_environment_applies_nothing(home, content) -> None:
    (home / runtime.LAUNCH_ENV_FILE).write_text(content, encoding="utf-8")
    target: dict[str, str] = {}
    assert runtime.take_launch_environment(home, target) == 0
    assert target == {}
    assert not (home / runtime.LAUNCH_ENV_FILE).exists()


# ── Stop ──────────────────────────────────────────────────────────────


class _Health:
    """Answers like an engine for ``answers`` calls, then not at all."""

    def __init__(self, answers: int) -> None:
        self.calls = 0
        self.answers = answers

    def __call__(self, port=runtime.DEFAULT_PORT, **kwargs):
        self.calls += 1
        return {"engine": "test", "ok": True} if self.calls <= self.answers else None


def test_stop_reports_stopped_only_when_the_process_has_ended(home, monkeypatch) -> None:
    runtime.write_pid(home, 4242)
    alive_checks = []

    def alive(pid):
        alive_checks.append(pid)
        return len(alive_checks) < 3  # the port closes at once, the process ends a little later

    monkeypatch.setattr(runtime, "health", _Health(answers=1))
    monkeypatch.setattr(runtime, "pid_alive", alive)
    monkeypatch.setattr(
        runtime, "_kill_pid", lambda pid: pytest.fail("a stopping engine must not be killed")
    )

    assert runtime.stop(home, 18999, wait_seconds=5) == "stopped"
    assert len(alive_checks) >= 3
    assert runtime.read_pid(home) is None


def test_stop_never_kills_an_engine_whose_port_closed(home, monkeypatch) -> None:
    runtime.write_pid(home, 4242)
    monkeypatch.setattr(runtime, "health", _Health(answers=1))
    monkeypatch.setattr(runtime, "pid_alive", lambda pid: True)
    killed = []
    monkeypatch.setattr(runtime, "_kill_pid", lambda pid: killed.append(pid))

    assert runtime.stop(home, 18999, wait_seconds=0.6) == "still_running"
    assert killed == []


def test_stop_kills_by_pid_only_while_the_engine_still_answers(home, monkeypatch) -> None:
    runtime.write_pid(home, 4242)
    monkeypatch.setattr(runtime, "health", lambda port=0, **k: {"engine": "test", "ok": True})
    killed = []
    monkeypatch.setattr(runtime, "_kill_pid", lambda pid: killed.append(pid))

    assert runtime.stop(home, 18999, wait_seconds=0.6) == "killed"
    assert killed == [4242]


def test_stop_when_nothing_answers_says_not_running(home, monkeypatch) -> None:
    runtime.write_pid(home, 4242)
    monkeypatch.setattr(runtime, "health", lambda port=0, **k: None)
    monkeypatch.setattr(runtime, "pid_alive", lambda pid: False)
    assert runtime.stop(home, 18999) == "not_running"
    assert runtime.read_pid(home) is None


def test_pid_alive_knows_this_process_and_an_unused_pid() -> None:
    import os

    assert runtime.pid_alive(os.getpid()) is True
    assert runtime.pid_alive(2**31 - 2) is False


# ── Exit ──────────────────────────────────────────────────────────────


def test_exit_now_flushes_output_and_ends_with_the_given_code() -> None:
    code = (
        "import sys\n"
        "from horizon.code_engine import runtime\n"
        "sys.stdout.write('flushed')\n"
        "runtime.exit_now(7)\n"
        "print('never reached')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=120, check=False
    )
    assert result.returncode == 7
    assert result.stdout == "flushed"


# ── What is saved for the engine ──────────────────────────────────────

SECRET = "sk-test-do-not-save-123"


def test_only_the_allowed_variables_are_saved_for_the_engine(home, monkeypatch) -> None:
    for name, value in {
        "ANTHROPIC_API_KEY": SECRET,
        "OPENAI_API_KEY": SECRET,
        "CONTEXTSHRINK_API_KEY": SECRET,
        "CONTEXTSHRINK_SERVICE_TOKEN": SECRET,
        "HORIZON_PROXY_TOKEN": SECRET,
        "HORIZON_CLIENT_EXE": "C:/stale/horizon.exe",
        "HORIZON_LAUNCHER_PID": "999",
        "HORIZON_SAVINGS": "max",
        "HTTPS_PROXY": "http://proxy.test:8080",
        "NO_PROXY": "localhost",
        "SSL_CERT_FILE": "C:/certs/corp.pem",
        "CONTEXTSHRINK_API_URL": "https://api.test",
        "PATH": "C:/Windows",
    }.items():
        monkeypatch.setenv(name, value)

    runtime._write_launch_env(home)

    raw = (home / runtime.LAUNCH_ENV_FILE).read_text(encoding="utf-8")
    assert SECRET not in raw, "a secret reached the saved environment"
    saved = json.loads(raw)["env"]
    assert saved["HORIZON_SAVINGS"] == "max"
    assert saved["HTTPS_PROXY"] == "http://proxy.test:8080"
    assert saved["NO_PROXY"] == "localhost"
    assert saved["SSL_CERT_FILE"] == "C:/certs/corp.pem"
    assert saved["CONTEXTSHRINK_API_URL"] == "https://api.test"
    for name in (
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "CONTEXTSHRINK_API_KEY",
        "CONTEXTSHRINK_SERVICE_TOKEN",
        "HORIZON_PROXY_TOKEN",
        "HORIZON_CLIENT_EXE",
        "HORIZON_LAUNCHER_PID",
        "PATH",
    ):
        assert name not in saved, name


@pytest.mark.parametrize(
    "name, allowed",
    [
        ("CONTEXTSHRINK_HOME", True),
        ("https_proxy", True),
        ("no_proxy", True),
        ("Http_Proxy", True),
        ("HORIZON_SAVINGS", True),
        ("HORIZON_MIN_TOKENS", False),  # the word TOKEN is excluded on purpose
        ("HORIZON_KEYS_PATH", False),
        ("HORIZON_LICENSE_KEY", False),
        ("HORIZON_CLIENT_EXE", False),
        ("ANTHROPIC_BASE_URL", False),
        ("ANTHROPIC_API_KEY", False),
        ("CONTEXTSHRINK_API_KEY", False),
        ("CONTEXTSHRINK_SERVICE_TOKEN", False),
    ],
)
def test_the_allowlist_names(name: str, allowed: bool) -> None:
    assert runtime.launch_environment_allowed(name) is allowed


def test_the_engine_applies_only_allowed_names_from_the_file(home) -> None:
    (home / runtime.LAUNCH_ENV_FILE).write_text(
        json.dumps(
            {"version": 1, "env": {"HTTPS_PROXY": "http://p:1", "ANTHROPIC_API_KEY": SECRET}}
        ),
        encoding="utf-8",
    )
    target: dict[str, str] = {}
    assert runtime.take_launch_environment(home, target) == 1
    assert target == {"HTTPS_PROXY": "http://p:1"}


def test_the_engine_deletes_the_saved_environment_when_it_starts(home) -> None:
    runtime._write_launch_env(home)
    assert (home / runtime.LAUNCH_ENV_FILE).exists()
    runtime.take_launch_environment(home, {})
    assert not (home / runtime.LAUNCH_ENV_FILE).exists()


def test_a_stale_saved_environment_is_not_applied_and_is_deleted(home, monkeypatch) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.test:8080")
    runtime._write_launch_env(home)
    hour_ago = time.time() - 3600
    os.utime(home / runtime.LAUNCH_ENV_FILE, (hour_ago, hour_ago))

    target: dict[str, str] = {}
    assert runtime.take_launch_environment(home, target) == 0
    assert target == {}
    assert not (home / runtime.LAUNCH_ENV_FILE).exists()


def test_the_starter_deletes_the_saved_environment_when_the_engine_never_answers(
    home, monkeypatch
) -> None:
    def start_that_saves_and_fails(home_dir, port, **kwargs):
        runtime._write_launch_env(home_dir)
        return "wmi"

    monkeypatch.setattr(runtime, "spawn_detached", start_that_saves_and_fails)
    monkeypatch.setattr(runtime, "health", lambda port=0, **k: None)

    assert runtime.ensure(home, 18999, wait_seconds=0.3) is False
    assert not (home / runtime.LAUNCH_ENV_FILE).exists()


def test_the_starter_deletes_the_saved_environment_when_the_engine_answers(
    home, monkeypatch
) -> None:
    answers = iter([None])

    def start_that_saves(home_dir, port, **kwargs):
        runtime._write_launch_env(home_dir)
        return "wmi"

    monkeypatch.setattr(runtime, "spawn_detached", start_that_saves)
    monkeypatch.setattr(
        runtime, "health", lambda port=0, **k: next(answers, {"engine": "test", "ok": True})
    )

    assert runtime.ensure(home, 18999, wait_seconds=5) is True
    assert not (home / runtime.LAUNCH_ENV_FILE).exists()
