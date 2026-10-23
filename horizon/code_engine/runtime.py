"""Process lifecycle for the engine: one instance, a PID file, detached start, stop.

The engine answers ``GET /__cs/health`` on the loopback port. Every command
here decides whether an engine is running from that answer, so a stale PID
file never makes a command kill the wrong process.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

from horizon.code_engine.store import remove

HOST = "127.0.0.1"
DEFAULT_PORT = 18793
LOCK_FILE = "engine.lock"
PID_FILE = "engine.pid"
STOP_FILE = "engine.stop"
LOG_FILE = "engine.log"
HEALTH_PATH = "/__cs/health"

_NO_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))
_DETACHED_PROCESS = 0x00000008
_CREATE_NEW_PROCESS_GROUP = 0x00000200
_CREATE_BREAKAWAY_FROM_JOB = 0x01000000


# ── Single instance ───────────────────────────────────────────────────


class InstanceLock:
    """An OS file lock that a second engine cannot take. Released when the process exits."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._handle: Any = None

    def acquire(self) -> bool:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self._path, "a+b")  # noqa: SIM115 - held open for the process lifetime
        try:
            if not _try_lock(handle):
                handle.close()
                return False
        except BaseException:
            handle.close()
            raise
        self._handle = handle
        return True

    def release(self) -> None:
        if self._handle is not None:
            try:
                _unlock(self._handle)
            finally:
                self._handle.close()
                self._handle = None


def _try_lock(handle: Any) -> bool:
    if sys.platform == "win32":
        import msvcrt

        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True
    import fcntl

    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def _unlock(handle: Any) -> None:
    if sys.platform == "win32":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return
    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


# ── PID file, stop request ────────────────────────────────────────────


def write_pid(home: Path, pid: int) -> None:
    (home / PID_FILE).write_text(str(pid), encoding="utf-8")


def read_pid(home: Path) -> int | None:
    try:
        text = (home / PID_FILE).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return int(text) if text.isdigit() else None


def remove_pid(home: Path) -> None:
    remove(home / PID_FILE)


def request_stop(home: Path) -> None:
    """Ask the running engine to shut down. It polls for this file once a second."""
    home.mkdir(parents=True, exist_ok=True)
    (home / STOP_FILE).write_text(str(time.time()), encoding="utf-8")


def consume_stop_request(home: Path) -> bool:
    """True once when a stop was requested (the file is removed)."""
    path = home / STOP_FILE
    if not path.exists():
        return False
    remove(path)
    return True


# ── Health probe ──────────────────────────────────────────────────────


def health(port: int = DEFAULT_PORT, *, timeout: float = 0.3) -> dict[str, Any] | None:
    """The engine's health body, or ``None`` when no engine answers on ``port``.

    Only a JSON body with an ``engine`` key counts, so another local service
    on the port is not mistaken for the engine.
    """
    url = f"http://{HOST}:{port}{HEALTH_PATH}"
    try:
        with _NO_PROXY.open(url, timeout=timeout) as response:
            if response.status != 200:
                return None
            body = json.loads(response.read(65536).decode("utf-8"))
    except (OSError, ValueError):
        return None
    if isinstance(body, dict) and "engine" in body:
        return body
    return None


# ── Detached start ────────────────────────────────────────────────────


def ensure(home: Path, port: int = DEFAULT_PORT, *, wait_seconds: float = 5.0) -> bool:
    """Start the engine detached unless it already answers. Returns True when it answers."""
    if health(port) is not None:
        return True
    spawn_detached(home, port)
    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        if health(port) is not None:
            return True
        time.sleep(0.1)
    return False


def spawn_detached(home: Path, port: int) -> None:
    """Run ``code-engine run`` in the background with no console window."""
    from horizon.bundled_client import cli_command

    home.mkdir(parents=True, exist_ok=True)
    argv = [*cli_command(), "code-engine", "run", "--port", str(port)]
    env = dict(os.environ)
    with open(home / LOG_FILE, "wb") as log:
        kwargs: dict[str, Any] = {
            "stdin": subprocess.DEVNULL,
            "stdout": log,
            "stderr": subprocess.STDOUT,
            "cwd": str(home),
            "env": env,
            "close_fds": True,
        }
        if sys.platform == "win32":
            flags = _DETACHED_PROCESS | _CREATE_NEW_PROCESS_GROUP
            try:
                # Breakaway keeps the engine alive when the hook's job object ends.
                subprocess.Popen(argv, creationflags=flags | _CREATE_BREAKAWAY_FROM_JOB, **kwargs)
                return
            except OSError:
                pass  # the job does not allow breakaway; start it in the job instead
            subprocess.Popen(argv, creationflags=flags, **kwargs)
        else:
            subprocess.Popen(argv, start_new_session=True, **kwargs)


# ── Stop and status ───────────────────────────────────────────────────


def stop(home: Path, port: int = DEFAULT_PORT, *, wait_seconds: float = 10.0) -> str:
    """Stop the engine on ``port``. Returns ``stopped``, ``not_running`` or ``killed``."""
    if health(port) is None:
        remove_pid(home)
        return "not_running"
    pid = read_pid(home)
    request_stop(home)
    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        if health(port) is None:
            return "stopped"
        time.sleep(0.2)
    # Last resort, by PID only. The health check above proved this port is the engine.
    if pid is not None:
        _kill_pid(pid)
        return "killed"
    return "still_running"


def _kill_pid(pid: int) -> None:
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            check=False,
            timeout=15,
        )
        return
    os.kill(pid, signal.SIGKILL)
