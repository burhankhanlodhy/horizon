"""Extension 0.1.1 lifecycle: runtime copy, cleanup, heartbeat, retire and handover."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import pytest

from horizon.desktop_mcp import broker, extension
from horizon.desktop_mcp import broker_transport as transport
from horizon.desktop_mcp.broker import BrokerClient, BrokerError, BrokerService, _idle_watchdog
from horizon.desktop_mcp.broker_transport import BrokerTransportError
from horizon.desktop_mcp.settings import DesktopSettings, load_settings
from horizon.desktop_mcp.version import VERSION

WINDOWS = sys.platform == "win32"
BUILD_ID = re.compile(r"\d+\.\d+\.\d+-[0-9a-f]{12}")
SID = "S-1-5-21-unit-test"
CAPABILITY = "A" * 43
KEEP = "2.0.0-aaaaaaaaaaaa"
OLD = "0.1.0-bbbbbbbbbbbb"


def _settings(tmp_path: Path) -> DesktopSettings:
    return DesktopSettings.model_validate(
        {
            "profile": "local",
            "approved_roots": [str(tmp_path / "root")],
            "broker_id": "unit-life",
        }
    )


def _onedir(tmp_path: Path, payload: bytes = b"MZ-frozen") -> Path:
    onedir = tmp_path / "onedir"
    (onedir / "_internal").mkdir(parents=True)
    (onedir / "contextshrink-desktop.exe").write_bytes(payload)
    (onedir / "_internal" / "lib.dll").write_bytes(b"dll-bytes")
    return onedir


@pytest.fixture
def frozen(monkeypatch, tmp_path) -> Path:
    """A fake one-folder build, run as if it were the frozen executable."""
    onedir = _onedir(tmp_path)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(onedir / "contextshrink-desktop.exe"))
    monkeypatch.setenv(extension._EXTENSION_HOME_ENV, str(tmp_path / "ext"))
    return onedir


# --- build id and runtime copy ----------------------------------------------------


def test_build_id_is_version_and_exe_digest_and_changes_with_the_bytes(frozen) -> None:
    first = extension.build_id()
    assert BUILD_ID.fullmatch(first)
    assert first.startswith(f"{VERSION}-")
    assert extension.build_id() == first
    (frozen / "contextshrink-desktop.exe").write_bytes(b"MZ-other")
    assert extension.build_id() != first


def test_runtime_root_follows_the_extension_folder_parent(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(extension._EXTENSION_HOME_ENV, str(tmp_path / "ext"))
    assert extension.runtime_root() == tmp_path / "runtime"
    assert extension.runtime_root(tmp_path / "x" / "ext") == tmp_path / "x" / "runtime"


def test_ensure_runtime_copy_creates_a_complete_copy(frozen, tmp_path) -> None:
    exe = extension.ensure_runtime_copy()
    target = tmp_path / "runtime" / extension.build_id()
    assert exe == target / "contextshrink-desktop.exe"
    assert exe.read_bytes() == b"MZ-frozen"
    assert (target / "_internal" / "lib.dll").read_bytes() == b"dll-bytes"
    assert (target / ".complete").is_file()
    assert [item.name for item in (tmp_path / "runtime").iterdir()] == [target.name]


def test_ensure_runtime_copy_reuses_a_complete_copy(frozen) -> None:
    exe = extension.ensure_runtime_copy()
    sentinel = exe.parent / "sentinel.txt"
    sentinel.write_text("kept", encoding="utf-8")
    assert extension.ensure_runtime_copy() == exe
    assert sentinel.read_text(encoding="utf-8") == "kept"


def test_ensure_runtime_copy_ignores_an_incomplete_folder(frozen, tmp_path) -> None:
    target = tmp_path / "runtime" / extension.build_id()
    target.mkdir(parents=True)
    (target / "partial.dll").write_bytes(b"half")
    exe = extension.ensure_runtime_copy()
    assert exe == target / "contextshrink-desktop.exe"
    assert (target / ".complete").is_file()
    assert not (target / "partial.dll").exists()
    assert exe.read_bytes() == b"MZ-frozen"


def test_ensure_runtime_copy_uses_the_copy_another_process_renamed_first(
    frozen, tmp_path, monkeypatch
) -> None:
    real_copytree = shutil.copytree
    target = tmp_path / "runtime" / extension.build_id()
    state = {"outermost": True}

    def racing_copytree(source, destination, *args, **kwargs):
        # shutil.copytree recurses through the module name, so only the outer call races.
        if not state["outermost"]:
            return real_copytree(source, destination, *args, **kwargs)
        state["outermost"] = False
        result = real_copytree(source, destination, *args, **kwargs)
        # Another process finishes its own copy and renames it into place first.
        target.mkdir(parents=True)
        shutil.copy2(
            Path(source) / "contextshrink-desktop.exe", target / "contextshrink-desktop.exe"
        )
        (target / ".complete").write_text("other\n", encoding="utf-8")
        return result

    monkeypatch.setattr(extension.shutil, "copytree", racing_copytree)
    exe = extension.ensure_runtime_copy()
    assert exe == target / "contextshrink-desktop.exe"
    assert (target / ".complete").read_text(encoding="utf-8") == "other\n"
    assert [item.name for item in (tmp_path / "runtime").iterdir()] == [target.name]


def test_frozen_broker_command_runs_the_runtime_copy(frozen, tmp_path) -> None:
    config = tmp_path / "unit.json"
    command = extension.broker_command(config)
    runtime_exe = tmp_path / "runtime" / extension.build_id() / "contextshrink-desktop.exe"
    assert command == [str(runtime_exe), "broker", "--config", str(config)]
    assert runtime_exe.is_file()
    assert str(frozen) not in command[0]


def test_frozen_broker_command_reports_a_failed_copy(frozen, tmp_path, monkeypatch) -> None:
    def failing_copy() -> Path:
        raise OSError("disk full")

    monkeypatch.setattr(extension, "ensure_runtime_copy", failing_copy)
    with pytest.raises(BrokerError) as caught:
        extension.broker_command(tmp_path / "unit.json")
    assert caught.value.code == "runtime_copy_failed"


def test_frozen_broker_reports_its_own_build_id(frozen) -> None:
    assert broker._running_build_id() == extension.build_id()


def test_source_broker_reports_dev_build_id(monkeypatch) -> None:
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    assert broker._running_build_id() == "dev"


def test_frozen_broker_cleans_runtimes_keeping_its_own_folder(monkeypatch, tmp_path) -> None:
    seen: dict[str, Any] = {}

    def fake_cleanup(keep=None, base_dir=None) -> list[str]:
        seen["keep"] = keep
        return []

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(
        sys, "executable", str(tmp_path / "runtime" / KEEP / "contextshrink-desktop.exe")
    )
    monkeypatch.setattr(extension, "cleanup_runtimes", fake_cleanup)
    broker._clean_runtimes_for_this_build()
    assert seen == {"keep": KEEP}


# --- cleanup -------------------------------------------------------------------------


def _make_runtime(tmp_path: Path, *names: str) -> Path:
    root = tmp_path / "runtime"
    for name in names:
        (root / name).mkdir(parents=True, exist_ok=True)
        (root / name / "f.dll").write_bytes(b"x")
    return root


def test_cleanup_removes_old_builds_and_stale_copies_only(tmp_path) -> None:
    stale = "0.1.0-cccccccccccc.tmp-def456"
    root = _make_runtime(tmp_path, KEEP, OLD, f"{OLD}.tmp-abc123", stale, "notes")
    old_time = time.time() - 7200
    os.utime(root / stale, (old_time, old_time))
    removed = extension.cleanup_runtimes(keep=KEEP, base_dir=tmp_path / "ext")
    assert sorted(removed) == sorted([OLD, stale])
    assert sorted(item.name for item in root.iterdir()) == sorted(
        [KEEP, f"{OLD}.tmp-abc123", "notes"]
    )


def test_cleanup_without_a_runtime_folder_does_nothing(tmp_path) -> None:
    assert extension.cleanup_runtimes(keep=KEEP, base_dir=tmp_path / "ext") == []


@pytest.mark.skipif(not WINDOWS, reason="junctions are a Windows feature")
def test_cleanup_never_follows_or_removes_a_junction(tmp_path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("precious", encoding="utf-8")
    _make_runtime(tmp_path, KEEP)
    link = tmp_path / "runtime" / OLD
    made = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(outside)],
        capture_output=True,
        text=True,
        check=False,
    )
    if made.returncode != 0 or not link.exists():
        pytest.skip("this account cannot create a junction")
    assert extension.cleanup_runtimes(keep=KEEP, base_dir=tmp_path / "ext") == []
    assert (outside / "keep.txt").read_text(encoding="utf-8") == "precious"
    assert link.is_junction()


def test_cleanup_never_follows_or_removes_a_symlink(tmp_path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("precious", encoding="utf-8")
    _make_runtime(tmp_path, KEEP)
    link = tmp_path / "runtime" / OLD
    try:
        os.symlink(outside, link, target_is_directory=True)
    except OSError:
        pytest.skip("this account cannot create a symbolic link")
    assert extension.cleanup_runtimes(keep=KEEP, base_dir=tmp_path / "ext") == []
    assert (outside / "keep.txt").read_text(encoding="utf-8") == "precious"
    assert link.is_symlink()


@pytest.mark.skipif(not WINDOWS, reason="in-use DLL locks are a Windows behaviour")
def test_cleanup_leaves_a_folder_in_use_in_place(tmp_path) -> None:
    base = tmp_path / "ext"
    root = _make_runtime(tmp_path, KEEP, OLD)
    with (root / OLD / "f.dll").open("rb"):
        assert extension.cleanup_runtimes(keep=KEEP, base_dir=base) == []
        assert (root / OLD / "f.dll").read_bytes() == b"x"
    assert extension.cleanup_runtimes(keep=KEEP, base_dir=base) == [OLD]


def test_cleanup_survives_a_removal_failure(tmp_path, monkeypatch) -> None:
    root = _make_runtime(tmp_path, KEEP, OLD)

    def failing_rmtree(path, *args, **kwargs):
        raise PermissionError("in use")

    monkeypatch.setattr(extension.shutil, "rmtree", failing_rmtree)
    assert extension.cleanup_runtimes(keep=KEEP, base_dir=tmp_path / "ext") == []
    remaining = list(root.glob(f"{OLD}.tmp-*"))
    assert len(remaining) == 1
    assert (remaining[0] / "f.dll").read_bytes() == b"x"


# --- transport envelopes ----------------------------------------------------------


def test_heartbeat_and_retire_accept_only_their_fixed_envelopes() -> None:
    transport._validate_operation_args("heartbeat", {})
    transport._validate_operation_args("retire", {"reason": "upgrade"})
    rejected = [
        ("heartbeat", {"now": 1}),
        ("heartbeat", None),
        ("retire", {}),
        ("retire", {"reason": "shutdown"}),
        ("retire", {"reason": "upgrade", "extra": 1}),
        ("retire", None),
    ]
    for operation, args in rejected:
        with pytest.raises(BrokerTransportError) as caught:
            transport._validate_operation_args(operation, args)
        assert caught.value.code == "invalid_arguments"


def test_lifecycle_operations_refuse_a_presented_capability() -> None:
    for operation in ("heartbeat", "retire"):
        with pytest.raises(BrokerTransportError) as caught:
            transport._authorize_operation({"capability": CAPABILITY}, operation, CAPABILITY)
        assert caught.value.code == "operation_unauthorized"
    transport._authorize_operation({}, "heartbeat", None)
    transport._authorize_operation({}, "retire", None)


class _FakePipe:
    def __init__(self, frame: bytes) -> None:
        # The trailing ACK is what the client sends after reading the response.
        self.incoming = bytearray(frame + b"ACK\x01")
        self.written = bytearray()


def _frame(payload: dict[str, Any]) -> bytes:
    body = json.dumps(payload).encode("utf-8")
    return b"CSM\x01" + len(body).to_bytes(4, "big") + body


def _fake_read_exact(pipe: _FakePipe, size: int, _deadline: float) -> bytes:
    if len(pipe.incoming) < size:
        raise BrokerTransportError("pipe_closed")
    data = bytes(pipe.incoming[:size])
    del pipe.incoming[:size]
    return data


def _serve(
    monkeypatch: pytest.MonkeyPatch, operation: str, args: Any, companion: str | None
) -> tuple[dict[str, Any], list[tuple[str, Any]]]:
    monkeypatch.setattr(transport, "_read_exact", _fake_read_exact)
    monkeypatch.setattr(transport, "_client_sid", lambda _pipe: SID)
    monkeypatch.setattr(transport, "_await_client_close", lambda _pipe, _timeout: None)
    monkeypatch.setattr(
        transport, "_write_all", lambda pipe, data, _deadline: pipe.written.extend(data)
    )
    calls: list[tuple[str, Any]] = []

    def handler(name: str, arguments: Any) -> dict[str, Any]:
        calls.append((name, arguments))
        return {"handled": name}

    request = {"operation": operation, "args": args, "request_id": "req-1"}
    if companion is not None:
        request["capability"] = companion
    pipe = _FakePipe(_frame(request))
    transport._serve_request(pipe, handler, SID, 5, None)
    body = bytes(pipe.written)
    size = int.from_bytes(body[4:8], "big")
    return json.loads(body[8 : 8 + size].decode("utf-8"))["result"], calls


def test_heartbeat_reaches_the_handler_without_a_capability(monkeypatch) -> None:
    result, calls = _serve(monkeypatch, "heartbeat", {}, None)
    assert result == {"handled": "heartbeat"}
    assert calls == [("heartbeat", {})]


def test_presented_capability_on_heartbeat_is_unauthorized(monkeypatch) -> None:
    result, calls = _serve(monkeypatch, "heartbeat", {}, CAPABILITY)
    assert result == {"error": {"code": "operation_unauthorized"}}
    assert calls == []


def test_retire_with_the_fixed_reason_reaches_the_handler(monkeypatch) -> None:
    result, calls = _serve(monkeypatch, "retire", {"reason": "upgrade"}, None)
    assert result == {"handled": "retire"}
    assert calls == [("retire", {"reason": "upgrade"})]


def test_retire_with_other_arguments_is_invalid(monkeypatch) -> None:
    result, calls = _serve(monkeypatch, "retire", {"reason": "other"}, None)
    assert result == {"error": {"code": "invalid_arguments"}}
    assert calls == []


# --- broker service and client ---------------------------------------------------


class _Clock:
    def __init__(self, now: float = 0.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def _service(tmp_path: Path, clock: _Clock, idle: int = 60) -> BrokerService:
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps(
            {
                "profile": "local",
                "approved_roots": [str(tmp_path / "root")],
                "broker_id": "unit-life",
                "broker_idle_seconds": idle,
            }
        ),
        encoding="utf-8",
    )
    return BrokerService(config, clock=clock)


def test_heartbeat_is_answered_by_the_service_without_the_worker(tmp_path, monkeypatch) -> None:
    clock = _Clock(5.0)
    service = _service(tmp_path, clock)

    def unexpected(operation, args):
        raise AssertionError("heartbeat must not reach the worker")

    monkeypatch.setattr(service.worker, "request", unexpected)
    clock.now = 500.0
    assert service.handle("heartbeat", {}) == {"build_id": "dev", "version": VERSION}
    assert service._last_activity == 500.0


def test_retire_replies_then_stops_through_the_stop_path(tmp_path, monkeypatch) -> None:
    service = _service(tmp_path, _Clock())
    monkeypatch.setattr(broker, "_RETIRE_DELAY_SECONDS", 0.0)
    stopped = threading.Event()
    pipes: list[str] = []

    def fake_stop(pipe_name: str, event: threading.Event) -> None:
        pipes.append(pipe_name)
        event.set()
        stopped.set()

    def unexpected(operation, args):
        raise AssertionError("retire must not reach the worker")

    monkeypatch.setattr(broker, "stop_broker", fake_stop)
    monkeypatch.setattr(service.worker, "request", unexpected)
    service._pipe_name = "pipe-retire"
    assert service.handle("retire", {"reason": "upgrade"}) == {"retiring": True}
    assert stopped.wait(5)
    assert pipes == ["pipe-retire"]
    assert service._stop_event.is_set()


def test_heartbeat_keeps_an_idle_broker_running(tmp_path, monkeypatch) -> None:
    stopped: list[str] = []
    monkeypatch.setattr(broker, "stop_broker", lambda pipe, event: stopped.append(pipe))

    clock = _Clock(50.0)
    service = _service(tmp_path, clock, idle=60)
    service.handle("heartbeat", {})
    clock.now = 100.0
    done = threading.Event()
    watcher = threading.Thread(
        target=_idle_watchdog,
        kwargs={
            "idle_seconds": 60,
            "last_activity": lambda: service._last_activity,
            "clock": clock,
            "stop_event": threading.Event(),
            "done": done,
            "pipe_name": "p",
            "poll_seconds": 0.001,
        },
        daemon=True,
    )
    watcher.start()
    time.sleep(0.1)
    done.set()
    watcher.join(5)
    assert stopped == []

    # Control: with no heartbeat the same broker is past its idle limit.
    service._last_activity = 0.0
    watcher = threading.Thread(
        target=_idle_watchdog,
        kwargs={
            "idle_seconds": 60,
            "last_activity": lambda: service._last_activity,
            "clock": clock,
            "stop_event": threading.Event(),
            "done": threading.Event(),
            "pipe_name": "p",
            "poll_seconds": 0.001,
        },
        daemon=True,
    )
    watcher.start()
    watcher.join(5)
    assert stopped == ["p"]


def test_client_sends_heartbeat_and_retire_without_a_capability(monkeypatch, tmp_path) -> None:
    seen: list[tuple[str, Any, Any]] = []

    def fake_exchange(pipe_name, operation, args, *, timeout_seconds, capability=None):
        seen.append((operation, args, capability))
        if operation == "heartbeat":
            return {"build_id": "dev", "version": VERSION}
        return {"retiring": True}

    monkeypatch.setattr(broker, "current_user_sid", lambda: SID)
    monkeypatch.setattr(broker, "exchange", fake_exchange)
    client = BrokerClient(_settings(tmp_path))
    assert client.heartbeat() == {"build_id": "dev", "version": VERSION}
    assert client.retire() == {"retiring": True}
    assert seen == [("heartbeat", {}, None), ("retire", {"reason": "upgrade"}, None)]


@pytest.mark.skipif(not WINDOWS, reason="named-pipe broker is Windows-only")
def test_retire_stops_a_real_broker_through_its_pipe(tmp_path) -> None:
    broker_id = f"life-{uuid.uuid4().hex[:10]}"
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps(
            {
                "profile": "local",
                "approved_roots": [str(tmp_path / "root")],
                "broker_id": broker_id,
                "broker_idle_seconds": 0,
            }
        ),
        encoding="utf-8",
    )
    service = BrokerService(config)
    thread = threading.Thread(
        target=service.serve,
        kwargs={"capability_base_dir": tmp_path / "caps"},
        daemon=True,
    )
    thread.start()
    client = BrokerClient(load_settings(config))
    deadline = time.monotonic() + 15
    while True:
        try:
            answer = client.heartbeat()
            break
        except BrokerError as exc:
            if exc.code != "broker_unavailable" or time.monotonic() > deadline:
                raise
            time.sleep(0.1)
    assert answer == {"build_id": "dev", "version": VERSION}
    assert client.retire() == {"retiring": True}
    thread.join(15)
    assert not thread.is_alive()


# --- handover --------------------------------------------------------------------


class _Timeline:
    def __init__(self) -> None:
        self.now = 0.0

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class _ScriptedBroker:
    """Stands in for BrokerClient; heartbeat answers come from a script."""

    def __init__(self, heartbeats: list[Any], retire: Any = None) -> None:
        self.heartbeats = list(heartbeats)
        self.retire_outcome = retire
        self.calls: list[str] = []

    def stats(self) -> dict[str, Any]:
        self.calls.append("stats")
        return {"ok": True}

    def heartbeat(self) -> dict[str, Any]:
        self.calls.append("heartbeat")
        item = self.heartbeats.pop(0) if len(self.heartbeats) > 1 else self.heartbeats[0]
        if isinstance(item, BaseException):
            raise item
        return item

    def retire(self) -> dict[str, Any]:
        self.calls.append("retire")
        if isinstance(self.retire_outcome, BaseException):
            raise self.retire_outcome
        return {"retiring": True}


def _ensure(monkeypatch, tmp_path, client: _ScriptedBroker) -> tuple[list[Any], _Timeline]:
    spawned: list[Any] = []
    timeline = _Timeline()
    monkeypatch.setattr(extension, "BrokerClient", lambda settings: client)
    extension.ensure_broker(
        _settings(tmp_path),
        tmp_path / "unit.json",
        spawn=lambda command, cwd: spawned.append(command),
        clock=timeline.clock,
        sleep=timeline.sleep,
    )
    return spawned, timeline


UNAVAILABLE = BrokerError("broker_unavailable")


def test_an_older_broker_is_retired_before_the_new_one_starts(monkeypatch, tmp_path) -> None:
    client = _ScriptedBroker([{"build_id": "x", "version": "0.0.9"}, UNAVAILABLE])
    spawned, _timeline = _ensure(monkeypatch, tmp_path, client)
    # After the spawn, the readiness probe runs again.
    assert client.calls == ["stats", "heartbeat", "retire", "heartbeat", "stats"]
    assert len(spawned) == 1


def test_an_equal_broker_is_kept(monkeypatch, tmp_path) -> None:
    client = _ScriptedBroker([{"build_id": "x", "version": VERSION}])
    spawned, _timeline = _ensure(monkeypatch, tmp_path, client)
    assert client.calls == ["stats", "heartbeat"]
    assert spawned == []


def test_a_newer_broker_is_kept(monkeypatch, tmp_path) -> None:
    client = _ScriptedBroker([{"build_id": "x", "version": "9.9.9"}])
    spawned, _timeline = _ensure(monkeypatch, tmp_path, client)
    assert client.calls == ["stats", "heartbeat"]
    assert spawned == []


def test_a_broker_without_a_version_is_retired(monkeypatch, tmp_path) -> None:
    client = _ScriptedBroker([{"build_id": "x"}, UNAVAILABLE])
    spawned, _timeline = _ensure(monkeypatch, tmp_path, client)
    assert "retire" in client.calls
    assert len(spawned) == 1


def test_a_broker_that_predates_heartbeat_is_kept_when_it_cannot_retire(
    monkeypatch, tmp_path
) -> None:
    unavailable_operation = BrokerError("operation_unavailable")
    client = _ScriptedBroker([unavailable_operation], retire=unavailable_operation)
    spawned, _timeline = _ensure(monkeypatch, tmp_path, client)
    assert client.calls == ["stats", "heartbeat", "retire"]
    assert spawned == []


def test_a_broker_that_does_not_stop_is_kept_after_the_wait(monkeypatch, tmp_path) -> None:
    client = _ScriptedBroker([{"version": "0.0.9"}])
    spawned, timeline = _ensure(monkeypatch, tmp_path, client)
    assert client.calls[:3] == ["stats", "heartbeat", "retire"]
    assert spawned == []
    assert timeline.now >= extension._RETIRE_WAIT_SECONDS


@pytest.mark.parametrize(
    ("value", "older"),
    [
        ("0.0.9", True),
        ("0.1.0", True),
        ("0.1", True),
        ("0.1.1", True),
        ("0.1.1.0", True),
        ("0.1.2", True),
        ("0.1.3", True),
        ("0.1.4", False),
        ("0.1.4.0", False),
        ("0.2", False),
        (None, True),
        ("", True),
        ("junk", True),
        (7, True),
    ],
)
def test_version_comparison_treats_unparsable_as_older(value, older) -> None:
    assert extension._is_older(value) is older


def test_extension_constants_match_the_spec() -> None:
    assert extension._EXTENSION_IDLE_SECONDS == 120
    assert extension._HEARTBEAT_SECONDS == 30.0


# --- heartbeat thread -------------------------------------------------------------


def test_heartbeat_loop_sends_each_interval_and_ignores_broker_errors(capsys) -> None:
    waits: list[float] = []
    attempts: list[int] = []

    def wait(interval: float) -> bool:
        waits.append(interval)
        return len(waits) > 3

    def send() -> dict[str, Any]:
        attempts.append(len(attempts))
        if len(attempts) == 1:
            raise BrokerError("broker_unavailable")
        return {}

    extension._heartbeat_loop(send, wait, 30.0)
    assert waits == [30.0] * 4
    assert len(attempts) == 3
    assert capsys.readouterr().out == ""
