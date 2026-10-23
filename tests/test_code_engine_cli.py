"""``horizon code-engine`` commands and the process runtime behind them."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from horizon import bundled_client
from horizon.cli.main import main
from horizon.code_engine import runtime
from horizon.code_engine.store import HOME_ENV

# A port nothing listens on, so the health probe fails fast and deterministically.
PORT = "18999"


@pytest.fixture
def home(tmp_path, monkeypatch):
    path = tmp_path / "home"
    path.mkdir()
    monkeypatch.setenv(HOME_ENV, str(path))
    return path


def _invoke(*args: str):
    return CliRunner().invoke(main, ["code-engine", *args], catch_exceptions=False)


def test_status_json_reports_nothing_running_on_a_fresh_home(home) -> None:
    result = _invoke("status", "--port", PORT, "--json")
    assert result.exit_code == 0
    report = json.loads(result.output)
    assert report["running"] is False
    assert report["compression_licensed"] is False
    assert report["device_connected"] is False
    assert report["plan"] is None


def test_stop_when_not_running_says_so(home) -> None:
    result = _invoke("stop", "--port", PORT)
    assert result.exit_code == 0
    assert "not running" in result.output


def test_ensure_always_exits_zero_even_if_the_start_fails(home, monkeypatch) -> None:
    def boom(*args, **kwargs):
        raise OSError("cannot start")

    monkeypatch.setattr(runtime, "spawn_detached", boom)
    result = _invoke("ensure", "--port", PORT)
    assert result.exit_code == 0


def test_ensure_does_not_start_a_second_engine(home, monkeypatch) -> None:
    started = []
    monkeypatch.setattr(runtime, "health", lambda port=18793, **k: {"engine": "x", "ok": True})
    monkeypatch.setattr(runtime, "spawn_detached", lambda *a, **k: started.append(a))
    assert _invoke("ensure", "--port", PORT).exit_code == 0
    assert started == []


def test_second_run_exits_at_once_when_the_lock_is_held(home) -> None:
    holder = runtime.InstanceLock(home / runtime.LOCK_FILE)
    assert holder.acquire()
    try:
        result = _invoke("run", "--port", PORT)
        assert result.exit_code == 0
        assert "already running" in result.output
    finally:
        holder.release()


def test_instance_lock_allows_one_holder(home) -> None:
    path = home / runtime.LOCK_FILE
    first = runtime.InstanceLock(path)
    second = runtime.InstanceLock(path)
    assert first.acquire()
    assert not second.acquire()
    first.release()
    assert second.acquire()
    second.release()


def test_stop_request_is_consumed_once(home) -> None:
    runtime.request_stop(home)
    assert runtime.consume_stop_request(home) is True
    assert runtime.consume_stop_request(home) is False


def test_pid_file_round_trip(home) -> None:
    runtime.write_pid(home, 4242)
    assert runtime.read_pid(home) == 4242
    runtime.remove_pid(home)
    assert runtime.read_pid(home) is None


def test_spawn_relaunches_through_the_bundled_client(home, monkeypatch) -> None:
    captured = {}

    class _Popen:
        def __init__(self, argv, **kwargs):
            captured["argv"] = argv
            captured["kwargs"] = kwargs

    monkeypatch.setattr(bundled_client, "cli_command", lambda: ["C:/ContextShrink/horizon.exe"])
    monkeypatch.setattr(runtime.subprocess, "Popen", _Popen)
    runtime.spawn_detached(home, 18793)
    assert captured["argv"] == [
        "C:/ContextShrink/horizon.exe",
        "code-engine",
        "run",
        "--port",
        "18793",
    ]
    assert captured["kwargs"]["stdin"] == runtime.subprocess.DEVNULL
    assert captured["kwargs"]["cwd"] == str(home)


def test_spawn_on_windows_is_detached_without_a_console(home, monkeypatch) -> None:
    captured = {}

    class _Popen:
        def __init__(self, argv, **kwargs):
            captured.setdefault("flags", []).append(kwargs.get("creationflags", 0))

    monkeypatch.setattr(runtime, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(bundled_client, "cli_command", lambda: ["horizon.exe"])
    monkeypatch.setattr(runtime.subprocess, "Popen", _Popen)
    runtime.spawn_detached(home, 18793)
    flags = captured["flags"][0]
    assert flags & runtime._DETACHED_PROCESS
    assert flags & runtime._CREATE_NEW_PROCESS_GROUP


def test_health_ignores_services_that_are_not_the_engine(monkeypatch) -> None:
    class _Response:
        status = 200

        def read(self, n):
            return b'{"ok": true, "version": "forwarder"}'

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(runtime._NO_PROXY, "open", lambda url, timeout: _Response())
    assert runtime.health(18999) is None
