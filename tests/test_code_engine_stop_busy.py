"""A busy engine is stopped, not reported as not running (engine 0.8.3).

The fake engine is a real child process with the engine's command line
(``code-engine run --port P --home H``). It answers ``/__cs/health`` after a delay
(``FAKE_ENGINE_HEALTH_DELAY``), or not at all (``FAKE_ENGINE_SERVE=0``), and it exits when
the stop request file appears, as the real engine does. Every test uses a temporary home
and a free port; processes are ended through ``runtime.stop``, or by their own Popen handle.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

import pytest
from click.testing import CliRunner

from horizon.cli.main import main
from horizon.code_engine import runtime
from horizon.code_engine.store import HOME_ENV

FAKE_ENGINE = """\
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

args = sys.argv[1:]
port = int(args[args.index("--port") + 1])
home = Path(args[args.index("--home") + 1])
delay = float(os.environ.get("FAKE_ENGINE_HEALTH_DELAY", "0"))
serve = os.environ.get("FAKE_ENGINE_SERVE", "1") == "1"


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != "/__cs/health":
            self.send_error(404)
            return
        time.sleep(delay)
        body = json.dumps({"engine": "fake", "ok": False}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


if serve:
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()

stop_file = home / "engine.stop"
while not stop_file.exists():
    time.sleep(0.05)
stop_file.unlink()
os._exit(0)
"""


@pytest.fixture
def home(tmp_path: Path) -> Path:
    path = tmp_path / "home"
    path.mkdir()
    return path


@pytest.fixture
def fake_script(tmp_path: Path) -> Path:
    path = tmp_path / "fake_engine.py"
    path.write_text(FAKE_ENGINE, encoding="utf-8")
    return path


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _wait_until(condition: Callable[[], bool], seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.1)
    return condition()


def _start_fake_engine(
    fake_script: Path, home: Path, port: int, *, health_delay: float = 0.0, serve: bool = True
) -> subprocess.Popen:
    """A fake engine process whose PID is written to the home, as the real engine does."""
    env = {
        **os.environ,
        "FAKE_ENGINE_HEALTH_DELAY": str(health_delay),
        "FAKE_ENGINE_SERVE": "1" if serve else "0",
    }
    argv = [
        sys.executable,
        str(fake_script),
        "code-engine",
        "run",
        "--port",
        str(port),
        "--home",
        str(home),
    ]
    proc = subprocess.Popen(argv, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    runtime.write_pid(home, proc.pid)
    return proc


def _end(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        proc.kill()
    proc.wait(timeout=30)


# ── Pure decisions ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("command", "port", "expected"),
    [
        ("python.exe -m horizon.cli code-engine run --port 18799 --home C:\\h", 18799, True),
        ("python -m horizon.cli code-engine run --port=18799", 18799, True),
        ("python -m horizon.cli code-engine run --port 18799", 18793, False),
        ("python -m horizon.cli code-engine run", 18793, True),  # the default port
        ("python -m horizon.cli code-engine run", 18799, False),
        ("python -m horizon.cli code-engine stop --port 18799", 18799, False),
        ("notepad.exe report-run.txt --port 18799", 18799, False),
    ],
)
def test_is_engine_command_matches_only_the_engine_on_its_port(command, port, expected) -> None:
    assert runtime.is_engine_command(command, port) is expected


def test_process_command_line_reads_the_command_line_of_a_live_process() -> None:
    port = _free_port()
    argv = [sys.executable, "-c", "import time; time.sleep(60)", "code-engine", "run"]
    proc = subprocess.Popen([*argv, "--port", str(port)])
    try:
        command = runtime.process_command_line(proc.pid)
        assert command is not None
        assert "code-engine run --port" in command
        assert str(port) in command
        assert runtime.pid_is_engine(proc.pid, port) is True
        assert runtime.pid_is_engine(proc.pid, port + 1) is False
    finally:
        _end(proc)


def test_pid_file_of_a_live_process_that_is_not_the_engine_is_kept(home, monkeypatch) -> None:
    port = _free_port()
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        runtime.write_pid(home, proc.pid)
        assert runtime.stop(home, port, wait_seconds=1) == "not_running"
        assert runtime.read_pid(home) == proc.pid, "a live process's PID file must not be deleted"
        assert runtime.pid_alive(proc.pid)
    finally:
        _end(proc)


def test_stale_pid_file_with_no_engine_says_not_running_and_removes_the_file(home) -> None:
    port = _free_port()
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait(timeout=30)  # a PID that has ended
    runtime.write_pid(home, proc.pid)

    assert runtime.stop(home, port, wait_seconds=1) == "not_running"
    assert runtime.read_pid(home) is None


def test_ensure_does_not_spawn_while_a_live_engine_is_busy(home, fake_script, monkeypatch) -> None:
    port = _free_port()
    monkeypatch.setattr(
        runtime,
        "spawn_detached",
        lambda *a, **k: pytest.fail("ensure started a second engine while one was busy"),
    )
    proc = _start_fake_engine(fake_script, home, port, health_delay=1.0)
    try:
        assert runtime.ensure(home, port, wait_seconds=6) is True
    finally:
        runtime.stop(home, port, wait_seconds=15)
        _end(proc)


def test_ensure_waits_on_a_live_engine_that_never_answers_without_spawning(
    home, fake_script, monkeypatch
) -> None:
    port = _free_port()
    spawned = []
    monkeypatch.setattr(runtime, "spawn_detached", lambda *a, **k: spawned.append(a))
    proc = _start_fake_engine(fake_script, home, port, serve=False)
    try:
        assert runtime.ensure(home, port, wait_seconds=0.5) is False
        assert spawned == []
    finally:
        runtime.stop(home, port, wait_seconds=15)
        _end(proc)


def test_ensure_starts_an_engine_when_the_pid_file_is_stale(home, monkeypatch) -> None:
    port = _free_port()
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait(timeout=30)
    runtime.write_pid(home, proc.pid)
    spawned = []
    monkeypatch.setattr(runtime, "spawn_detached", lambda *a, **k: spawned.append(a))

    assert runtime.ensure(home, port, wait_seconds=0.3) is False
    assert len(spawned) == 1


# ── Real fake-engine processes ────────────────────────────────────────


def test_stop_ends_an_engine_whose_health_answers_slowly(home, fake_script) -> None:
    port = _free_port()
    proc = _start_fake_engine(fake_script, home, port, health_delay=1.0)
    try:
        assert _wait_until(lambda: runtime.patient_health(port) is not None, 30)
        # The quick probe alone would call this engine not running: that is the bug.
        assert runtime.health(port) is None

        assert runtime.stop(home, port, wait_seconds=15) == "stopped"
        assert proc.wait(timeout=30) == 0, "the engine process must have ended"
        assert runtime.read_pid(home) is None
        assert runtime.health(port, timeout=0.5) is None
    finally:
        _end(proc)


def test_stop_ends_a_live_engine_that_does_not_answer_health(home, fake_script) -> None:
    port = _free_port()
    proc = _start_fake_engine(fake_script, home, port, serve=False)
    try:
        assert runtime.stop(home, port, wait_seconds=15) == "stopped"
        assert proc.wait(timeout=30) == 0
        assert runtime.read_pid(home) is None
    finally:
        _end(proc)


def test_status_counts_a_busy_engine_as_running(home, fake_script, monkeypatch) -> None:
    port = _free_port()
    monkeypatch.setenv(HOME_ENV, str(home))
    proc = _start_fake_engine(fake_script, home, port, health_delay=1.0)
    try:
        assert _wait_until(lambda: runtime.patient_health(port) is not None, 30)
        result = CliRunner().invoke(
            main, ["code-engine", "status", "--port", str(port), "--json"], catch_exceptions=False
        )
        assert result.exit_code == 0
        assert json.loads(result.output)["running"] is True
    finally:
        runtime.stop(home, port, wait_seconds=15)
        _end(proc)
