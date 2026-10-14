"""Installed-extension runtime: frozen worker, idle shutdown, settings, detached broker."""

from __future__ import annotations

import asyncio
import io
import json
import os
import subprocess
import sys
import threading
import time
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from horizon.desktop_mcp import __main__ as cli
from horizon.desktop_mcp import backend_worker, broker, extension
from horizon.desktop_mcp.broker import BrokerError, BrokerService, _BackendWorker
from horizon.desktop_mcp.settings import (
    _CONFIG_FIELDS,
    ConfigurationError,
    DesktopSettings,
    load_settings,
)
from horizon.desktop_mcp.version import VERSION

REPO_ROOT = Path(__file__).resolve().parents[2]


def _settings(tmp_path: Path, **updates: Any) -> DesktopSettings:
    data: dict[str, Any] = {
        "profile": "local",
        "approved_roots": [str(tmp_path / "root")],
        "broker_id": "unit-ext",
    }
    data.update(updates)
    return DesktopSettings.model_validate(data)


# --- worker subcommand dispatch -------------------------------------------------


def test_worker_subcommand_passes_remaining_arguments_verbatim(monkeypatch) -> None:
    seen: dict[str, Any] = {}

    def fake_worker_main(argv=None) -> int:
        seen["argv"] = argv
        return 7

    monkeypatch.setattr(backend_worker, "main", fake_worker_main)
    arguments = ["--config", "c.json", "--expected-broker-id", "x", "--expected-timeout", "10"]
    assert cli.main(["worker", *arguments]) == 7
    assert seen["argv"] == arguments


# --- frozen broker worker command ----------------------------------------------


class _FakeProcess:
    def __init__(self) -> None:
        self.stdin = None
        self.stdout = io.BytesIO(b"")

    def poll(self) -> None:
        return None

    def kill(self) -> None:
        return None

    def wait(self, timeout: float | None = None) -> int:
        return 0


def _captured_worker_command(monkeypatch, tmp_path: Path) -> tuple[list[str], dict[str, Any]]:
    captured: dict[str, Any] = {}

    def fake_popen(command, **kwargs):
        captured["command"] = list(command)
        captured.update(kwargs)
        return _FakeProcess()

    monkeypatch.setattr(broker.subprocess, "Popen", fake_popen)
    config = tmp_path / "config.json"
    config.write_text("{}", encoding="utf-8")
    worker = _BackendWorker(config, 10, broker_id="unit-ext", max_clients=8)
    worker._start()
    worker._stop()
    return captured["command"], captured


def test_frozen_broker_starts_worker_through_its_worker_subcommand(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(
        sys, "executable", "C:/Program Files/contextshrink/contextshrink-desktop.exe"
    )
    command, kwargs = _captured_worker_command(monkeypatch, tmp_path)
    assert command[:2] == [sys.executable, "worker"]
    assert "-I" not in command and "-c" not in command
    assert command[2:4] == ["--config", str((tmp_path / "config.json").resolve())]
    assert kwargs["env"] == broker._worker_environment()


def test_source_broker_keeps_the_bootstrap_worker_command(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    command, kwargs = _captured_worker_command(monkeypatch, tmp_path)
    assert command[1:3] == ["-I", "-c"]
    assert "from horizon.desktop_mcp.backend_worker import main" in command[3]
    assert kwargs["env"] == broker._worker_environment()


# --- idle shutdown --------------------------------------------------------------


class _Clock:
    def __init__(self, now: float = 0.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def test_idle_watchdog_stops_through_stop_path_once_idle_long_enough(monkeypatch) -> None:
    stopped: list[str] = []
    stop_event = threading.Event()
    done = threading.Event()

    def fake_stop_broker(pipe_name: str, event: threading.Event) -> None:
        stopped.append(pipe_name)
        event.set()

    monkeypatch.setattr(broker, "stop_broker", fake_stop_broker)
    broker._idle_watchdog(
        idle_seconds=60,
        last_activity=lambda: 0.0,
        clock=_Clock(100.0),
        stop_event=stop_event,
        done=done,
        pipe_name="pipe-x",
        poll_seconds=0.001,
    )
    assert stopped == ["pipe-x"]
    assert stop_event.is_set()


def test_idle_watchdog_exits_without_stopping_when_serving_ends(monkeypatch) -> None:
    stopped: list[str] = []
    done = threading.Event()
    done.set()
    monkeypatch.setattr(broker, "stop_broker", lambda *_: stopped.append("stop"))
    broker._idle_watchdog(
        idle_seconds=60,
        last_activity=lambda: 0.0,
        clock=_Clock(10_000.0),
        stop_event=threading.Event(),
        done=done,
        pipe_name="p",
        poll_seconds=0.001,
    )
    assert stopped == []


def _service_with_fake_pipe(monkeypatch, tmp_path: Path, idle: int, clock: _Clock):
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps(
            {
                "profile": "local",
                "approved_roots": [str(tmp_path / "root")],
                "broker_id": "unit-ext",
                "broker_idle_seconds": idle,
            }
        ),
        encoding="utf-8",
    )
    service = BrokerService(config, clock=clock)
    monkeypatch.setattr(broker, "current_user_sid", lambda: "S-1-5-21-unit-test")
    monkeypatch.setattr(broker, "_IDLE_POLL_CAP_SECONDS", 0.001)
    return service


def test_serve_stops_a_broker_that_has_been_idle_past_its_limit(monkeypatch, tmp_path) -> None:
    clock = _Clock(0.0)
    service = _service_with_fake_pipe(monkeypatch, tmp_path, 60, clock)
    stopped: list[str] = []

    def fake_serve_pipe(pipe_name, handler, *, stop_event, **_kwargs) -> None:
        clock.now = 1_000.0  # idle far beyond the 60 second limit
        assert stop_event.wait(timeout=5)

    def fake_stop_broker(pipe_name: str, event: threading.Event) -> None:
        stopped.append(pipe_name)
        event.set()

    monkeypatch.setattr(broker, "serve_pipe", fake_serve_pipe)
    monkeypatch.setattr(broker, "stop_broker", fake_stop_broker)
    service.serve()
    assert len(stopped) == 1


def test_handled_requests_reset_the_idle_clock(monkeypatch, tmp_path) -> None:
    clock = _Clock(5.0)
    service = _service_with_fake_pipe(monkeypatch, tmp_path, 60, clock)
    monkeypatch.setattr(service.worker, "request", lambda operation, args: {"ok": True})
    clock.now = 500.0
    service.handle("stats", {})
    assert service._last_activity == 500.0


def test_idle_zero_never_stops_the_broker(monkeypatch, tmp_path) -> None:
    clock = _Clock(0.0)
    service = _service_with_fake_pipe(monkeypatch, tmp_path, 0, clock)
    stopped: list[str] = []
    started: list[str] = []

    def fake_serve_pipe(pipe_name, handler, **_kwargs) -> None:
        clock.now = 10**9

    monkeypatch.setattr(broker, "serve_pipe", fake_serve_pipe)
    monkeypatch.setattr(broker, "stop_broker", lambda *_: stopped.append("stop"))
    monkeypatch.setattr(broker.threading.Thread, "start", lambda self: started.append(self.name))
    service.serve()
    assert stopped == []
    assert "contextshrink-broker-idle" not in started


# --- settings -------------------------------------------------------------------


@pytest.mark.parametrize("value", [0, 60, 1800, 86400])
def test_broker_idle_seconds_accepts_zero_or_bounded_range(value: int, tmp_path) -> None:
    assert _settings(tmp_path, broker_idle_seconds=value).broker_idle_seconds == value


@pytest.mark.parametrize("value", [-1, 1, 59, 86401, 10**6])
def test_broker_idle_seconds_rejects_out_of_range(value: int, tmp_path) -> None:
    with pytest.raises(ValidationError):
        _settings(tmp_path, broker_idle_seconds=value)


def test_broker_idle_seconds_is_a_strict_integer(tmp_path) -> None:
    with pytest.raises(ValidationError):
        _settings(tmp_path, broker_idle_seconds="60")
    with pytest.raises(ValidationError):
        _settings(tmp_path, broker_idle_seconds=True)


def test_broker_idle_seconds_is_a_loadable_config_field(tmp_path) -> None:
    assert "broker_idle_seconds" in _CONFIG_FIELDS
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"profile": "local", "broker_idle_seconds": 90}), encoding="utf-8")
    assert load_settings(config).broker_idle_seconds == 90
    config.write_text(json.dumps({"profile": "local", "broker_idle_seconds": 5}), encoding="utf-8")
    with pytest.raises(ConfigurationError) as caught:
        load_settings(config)
    assert caught.value.fields == ("broker_idle_seconds",)


# --- extension argument handling -------------------------------------------------


def test_extension_parser_collects_multiple_roots_and_defaults_to_none() -> None:
    args = cli._parser().parse_args(
        [
            "serve",
            "--extension",
            "--approved-roots",
            "C:/one",
            "C:/two",
            "--tool-profile",
            "compute",
        ]
    )
    assert args.extension is True
    assert args.approved_roots == ["C:/one", "C:/two"]
    assert args.tool_profile == "compute"
    assert args.artifact_root is None
    assert args.broker_id is None
    assert args.config is None


def test_extension_settings_map_empty_artifact_root_to_none(tmp_path) -> None:
    root = tmp_path / "root"
    args = cli._parser().parse_args(
        ["serve", "--extension", "--approved-roots", str(root), "--artifact-root", ""]
    )
    settings = extension._extension_settings(args)
    assert settings.artifact_root is None
    assert settings.profile == "local"
    assert settings.broker_id == "desktop-ext"
    assert settings.tool_profile == "source"
    assert settings.broker_idle_seconds == 120
    assert settings.approved_roots == (root,)


def test_extension_settings_reject_relative_roots_with_sanitized_fields() -> None:
    args = cli._parser().parse_args(["serve", "--extension", "--approved-roots", "relative/dir"])
    with pytest.raises(ConfigurationError) as caught:
        extension._extension_settings(args)
    assert caught.value.code == "config_invalid"
    assert caught.value.fields == ("approved_roots",)


def test_config_and_extension_are_mutually_exclusive(tmp_path, capsys) -> None:
    config = tmp_path / "config.json"
    config.write_text("{}", encoding="utf-8")
    assert cli.main(["serve", "--extension", "--config", str(config)]) == 2
    streams = capsys.readouterr()
    assert json.loads(streams.err) == {"error": "config_and_extension_conflict"}
    assert streams.out == ""


@pytest.mark.parametrize(
    "flags",
    [
        ["--approved-roots", "C:/one"],
        ["--tool-profile", "source"],
        ["--artifact-root", ""],
        ["--broker-id", "x"],
    ],
)
def test_extension_flags_without_extension_are_rejected(flags, capsys) -> None:
    assert cli.main(["serve", *flags]) == 2
    streams = capsys.readouterr()
    assert json.loads(streams.err) == {"error": "extension_flags_require_extension"}
    assert streams.out == ""


# --- settings file ---------------------------------------------------------------


def test_extension_config_path_honours_base_dir_and_env(monkeypatch, tmp_path) -> None:
    assert extension.extension_config_path("unit", base_dir=tmp_path) == tmp_path / "unit.json"
    monkeypatch.setenv("CONTEXTSHRINK_EXTENSION_HOME", str(tmp_path / "home"))
    assert extension.extension_config_path("unit") == tmp_path / "home" / "unit.json"


def test_write_extension_settings_is_atomic_and_skips_unchanged_content(tmp_path) -> None:
    path = tmp_path / "ext" / "unit.json"
    settings = _settings(tmp_path)
    assert extension.write_extension_settings(settings, path) is True
    first = path.read_bytes()
    assert load_settings(path) == settings
    assert extension.write_extension_settings(settings, path) is False
    assert path.read_bytes() == first
    changed = _settings(tmp_path, broker_idle_seconds=120)
    assert extension.write_extension_settings(changed, path) is True
    assert load_settings(path).broker_idle_seconds == 120
    assert sorted(item.name for item in path.parent.iterdir()) == ["unit.json"]


def test_write_extension_settings_leaves_no_temporary_file_on_failure(
    monkeypatch, tmp_path
) -> None:
    path = tmp_path / "unit.json"

    def failing_replace(source, target):
        raise OSError("disk full")

    monkeypatch.setattr(extension.os, "replace", failing_replace)
    with pytest.raises(OSError):
        extension.write_extension_settings(_settings(tmp_path), path)
    assert not path.exists()
    assert list(tmp_path.iterdir()) == []


# --- broker startup ---------------------------------------------------------------


class _FakeClient:
    def __init__(self, outcomes: list[Any]) -> None:
        self.outcomes = outcomes
        self.calls = 0

    def stats(self) -> dict[str, Any]:
        self.calls += 1
        outcome = self.outcomes[min(self.calls - 1, len(self.outcomes) - 1)]
        if isinstance(outcome, Exception):
            raise outcome
        return {"ok": True}

    def heartbeat(self) -> dict[str, Any]:
        # Same build as this process, so ensure_broker never hands over.
        return {"build_id": "dev", "version": VERSION}


class _Timeline:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def _ensure(monkeypatch, tmp_path, client: _FakeClient, spawn, timeline: _Timeline, timeout=3.0):
    monkeypatch.setattr(extension, "BrokerClient", lambda settings: client)
    extension.ensure_broker(
        _settings(tmp_path),
        tmp_path / "unit.json",
        timeout=timeout,
        spawn=spawn,
        clock=timeline.clock,
        sleep=timeline.sleep,
    )


def test_ensure_broker_does_not_spawn_when_one_is_running(monkeypatch, tmp_path) -> None:
    spawned: list[Any] = []
    client = _FakeClient([{"ok": True}])
    _ensure(monkeypatch, tmp_path, client, lambda *args: spawned.append(args), _Timeline())
    assert spawned == []
    assert client.calls == 1


def test_ensure_broker_spawns_once_and_waits_until_ready(monkeypatch, tmp_path) -> None:
    unavailable = BrokerError("broker_unavailable")
    spawned: list[tuple[list[str], Path]] = []
    client = _FakeClient([unavailable, unavailable, {"ok": True}])
    timeline = _Timeline()
    _ensure(
        monkeypatch,
        tmp_path,
        client,
        lambda command, cwd: spawned.append((command, cwd)),
        timeline,
    )
    assert len(spawned) == 1
    command, cwd = spawned[0]
    assert command == extension.broker_command(tmp_path / "unit.json")
    assert cwd == tmp_path
    assert client.calls == 3
    assert timeline.sleeps


def test_ensure_broker_reports_start_failure_when_never_ready(monkeypatch, tmp_path) -> None:
    unavailable = BrokerError("broker_unavailable")
    spawned: list[Any] = []
    client = _FakeClient([unavailable])
    with pytest.raises(BrokerError) as caught:
        _ensure(monkeypatch, tmp_path, client, lambda *args: spawned.append(args), _Timeline())
    assert caught.value.code == "broker_start_failed"
    assert len(spawned) == 1


def test_ensure_broker_surfaces_local_transport_failures_without_spawning(
    monkeypatch, tmp_path
) -> None:
    spawned: list[Any] = []
    client = _FakeClient([BrokerError("windows_pipe_dependency_unavailable")])
    with pytest.raises(BrokerError) as caught:
        _ensure(monkeypatch, tmp_path, client, lambda *args: spawned.append(args), _Timeline())
    assert caught.value.code == "windows_pipe_dependency_unavailable"
    assert spawned == []


def test_broker_command_matches_frozen_and_source_layouts(monkeypatch, tmp_path) -> None:
    config = tmp_path / "unit.json"
    runtime_exe = tmp_path / "runtime" / "contextshrink-desktop.exe"
    monkeypatch.setattr(extension, "ensure_runtime_copy", lambda: runtime_exe)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert extension.broker_command(config) == [
        str(runtime_exe),
        "broker",
        "--config",
        str(config),
    ]
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    assert extension.broker_command(config) == [
        sys.executable,
        "-m",
        "horizon.desktop_mcp",
        "broker",
        "--config",
        str(config),
    ]


def test_detached_spawn_uses_windows_flags_neutral_cwd_and_null_stdio(
    monkeypatch, tmp_path
) -> None:
    captured: dict[str, Any] = {}

    def fake_popen(command, **kwargs):
        captured["command"] = command
        captured.update(kwargs)

    monkeypatch.setattr(extension.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    extension._spawn_detached(["py", "-m", "x"], tmp_path)
    assert captured["cwd"] == tmp_path
    assert captured["stdin"] == subprocess.DEVNULL
    assert captured["stdout"] == subprocess.DEVNULL
    assert captured["stderr"] == subprocess.DEVNULL
    assert captured["close_fds"] is True
    assert captured["creationflags"] == (
        getattr(subprocess, "DETACHED_PROCESS", 0)
        | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        | getattr(subprocess, "CREATE_NO_WINDOW", 0)
    )
    assert str(REPO_ROOT) in captured["env"]["PYTHONPATH"]


# --- real Windows end to end -----------------------------------------------------


def _broker_pids(config: Path) -> list[int]:
    """PIDs of broker processes started for this test's settings file."""
    quoted = "'" + str(config).replace("'", "''") + "'"
    script = (
        "Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'python.exe' -and "
        "$_.ProcessId -ne $PID -and $_.CommandLine -and "
        f"$_.CommandLine.Contains({quoted}) -and $_.CommandLine.Contains(' broker ') }} "
        "| ForEach-Object { $_.ProcessId }"
    )
    completed = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    return [int(line) for line in completed.stdout.split() if line.strip().isdigit()]


@pytest.mark.skipif(sys.platform != "win32", reason="named-pipe broker is Windows-only")
def test_extension_serve_starts_a_broker_and_answers_a_search(tmp_path) -> None:
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    broker_id = f"e2e-{uuid.uuid4().hex[:10]}"
    root = tmp_path / "root"
    root.mkdir()
    (root / "note.txt").write_text("extension smoke fixture\n", encoding="utf-8")
    home = tmp_path / "home"
    config_path = extension.extension_config_path(broker_id, base_dir=home)
    environment = dict(os.environ)
    environment["PYTHONPATH"] = os.pathsep.join(
        part for part in (str(REPO_ROOT), environment.get("PYTHONPATH")) if part
    )
    environment["CONTEXTSHRINK_EXTENSION_HOME"] = str(home)

    async def exercise() -> tuple[list[str], dict[str, Any], list[int]]:
        parameters = StdioServerParameters(
            command=sys.executable,
            args=[
                "-m",
                "horizon.desktop_mcp",
                "serve",
                "--extension",
                "--approved-roots",
                str(root),
                "--broker-id",
                broker_id,
            ],
            cwd=REPO_ROOT,
            env=environment,
        )
        async with stdio_client(parameters) as (read_stream, write_stream):
            async with ClientSession(
                read_stream, write_stream, read_timeout_seconds=timedelta(seconds=30)
            ) as session:
                await session.initialize()
                listed = await session.list_tools()
                result = await session.call_tool("contextshrink_search", {"query": "note"})
                brokers = _broker_pids(config_path)
        text = "".join(getattr(item, "text", "") for item in result.content)
        names = [tool.name for tool in listed.tools]
        return names, {"is_error": bool(result.isError), "text": text}, brokers

    try:
        names, outcome, brokers = asyncio.run(exercise())
        assert "contextshrink_search" in names
        assert outcome["is_error"] is False
        assert "note.txt" in outcome["text"]
        # serve --extension auto-started the broker it searched through.
        assert brokers
    finally:
        # Under the MCP SDK stdio host the broker is killed with the serving
        # process (kill-on-close job, no breakaway), so this only mops up any
        # broker that outlived the session.
        for pid in _broker_pids(config_path):
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                check=False,
                timeout=60,
            )
    # taskkill returns before the process table drops the entry; allow a moment.
    for _ in range(50):
        if not _broker_pids(config_path):
            break
        time.sleep(0.2)
    assert _broker_pids(config_path) == []
