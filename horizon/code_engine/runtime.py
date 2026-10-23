"""Process lifecycle for the engine: one instance, a PID file, start, stop.

The engine answers ``GET /__cs/health`` on the loopback port. Every command
here decides whether an engine is running from that answer, so a stale PID
file never makes a command kill the wrong process.

The engine must outlive whatever started it (a terminal, the npx run, a Claude
Code hook). On Windows it is therefore created by WMI (``Win32_Process.Create``),
which creates it under the WMI provider host: outside the caller's job object and
console. The detached Popen start is the fallback, and the log says which one ran.
"""

from __future__ import annotations

import base64
import json
import os
import signal
import subprocess
import sys
import time
import urllib.request
from collections.abc import Callable, MutableMapping
from pathlib import Path
from typing import Any

from horizon.code_engine.store import HOME_ENV, read_json, remove, write_private_json

HOST = "127.0.0.1"
DEFAULT_PORT = 18793
LOCK_FILE = "engine.lock"
PID_FILE = "engine.pid"
STOP_FILE = "engine.stop"
LOG_FILE = "engine.log"
LAUNCH_ENV_FILE = "launch-env.json"
# A saved environment older than this is never applied: the engine reads it at once.
LAUNCH_ENV_MAX_AGE_SECONDS = 300
# The only environment that is saved for the engine. The starter's full environment can
# hold API keys and tokens, and the file stays on disk until the engine reads it, so
# nothing outside this list is written. Matched case-insensitively for the proxy names.
#   CONTEXTSHRINK_HOME, CONTEXTSHRINK_API_URL, CONTEXTSHRINK_MACHINE_ID: the engine's
#     home, the API address and the device-id override (horizon/code_engine, hosted.py).
#   HTTP_PROXY, HTTPS_PROXY, NO_PROXY: the engine's upstream and API connections.
#   SSL_CERT_FILE, SSL_CERT_DIR, REQUESTS_CA_BUNDLE, CURL_CA_BUNDLE: CA bundles for them.
#   USERPROFILE, HOME, APPDATA, LOCALAPPDATA, TEMP, TMP: profile and temp locations
#     (paths, not secrets). The proxy writes its logs under the profile.
#   HORIZON_* (except names with a secret-like part, and HORIZON_CLIENT_EXE and
#     HORIZON_LAUNCHER_PID, which describe the starter): the proxy's settings. There are
#     about 400 of them, so they are matched by prefix rather than listed.
LAUNCH_ENV_NAMES = frozenset(
    {
        "CONTEXTSHRINK_HOME",
        "CONTEXTSHRINK_API_URL",
        "CONTEXTSHRINK_MACHINE_ID",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "REQUESTS_CA_BUNDLE",
        "CURL_CA_BUNDLE",
        "USERPROFILE",
        "HOME",
        "APPDATA",
        "LOCALAPPDATA",
        "TEMP",
        "TMP",
    }
)
_LAUNCH_ENV_PROXY_NAMES = frozenset({"HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY"})
_LAUNCH_ENV_HORIZON_PREFIX = "HORIZON_"
_LAUNCH_ENV_HORIZON_EXCLUDED = (
    "KEY",
    "TOKEN",
    "SECRET",
    "PASSWORD",
    "CREDENTIAL",
    "AUTH",
    "LICENSE",
    "CLIENT_EXE",
    "LAUNCHER_PID",
)
HEALTH_PATH = "/__cs/health"

_NO_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))
_DETACHED_PROCESS = 0x00000008
_CREATE_NEW_PROCESS_GROUP = 0x00000200
_CREATE_BREAKAWAY_FROM_JOB = 0x01000000
_CREATE_NO_WINDOW = 0x08000000
WMI_START_TIMEOUT_SECONDS = 60
# Characters that cmd.exe or the quoting around the WMI command line cannot carry.
_WMI_UNSAFE = set('"%^&|<>')
_STILL_ACTIVE = 259
_ERROR_ACCESS_DENIED = 5

_WMI_SCRIPT = "\n".join(
    [
        "$ErrorActionPreference = 'Stop'",
        "$startup = New-CimInstance -ClassName Win32_ProcessStartup -Property @{ ShowWindow = [uint16]0 } -ClientOnly",
        "$result = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{ "
        "CommandLine = $env:CONTEXTSHRINK_ENGINE_COMMAND; CurrentDirectory = $env:CONTEXTSHRINK_ENGINE_DIR; "
        "ProcessStartupInformation = $startup }",
        "if ($result.ReturnValue -ne 0) { throw ('Win32_Process.Create returned ' + $result.ReturnValue) }",
        "Write-Output $result.ProcessId",
    ]
)


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


# ── Start ─────────────────────────────────────────────────────────────


def ensure(home: Path, port: int = DEFAULT_PORT, *, wait_seconds: float = 5.0) -> bool:
    """Start the engine in the background unless it already answers. Returns True when it answers.

    The saved launch environment is deleted when the wait ends, whether or not the engine
    answered: an engine that has not read it by then is not going to.
    """
    if health(port) is not None:
        return True
    try:
        spawn_detached(home, port)
        deadline = time.monotonic() + wait_seconds
        while time.monotonic() < deadline:
            if health(port) is not None:
                return True
            time.sleep(0.1)
        return False
    finally:
        remove(home / LAUNCH_ENV_FILE)


def spawn_detached(
    home: Path,
    port: int,
    *,
    wmi_start: Callable[[str, str], None] | None = None,
    popen: Callable[..., Any] | None = None,
) -> str:
    """Start ``code-engine run`` in the background. Returns ``"wmi"`` or ``"detached"``.

    On Windows the engine is created by WMI when that works; the detached Popen start
    runs only when WMI fails, and the log's first line says which start was used.
    ``wmi_start(command, cwd)`` and ``popen`` replace the real calls in tests.
    """
    from horizon.bundled_client import cli_command

    home.mkdir(parents=True, exist_ok=True)
    argv = [*cli_command(), "code-engine", "run", "--port", str(port)]
    if sys.platform != "win32":
        _start_detached(argv, home, "launcher: detached", popen)
        return "detached"
    try:
        _start_with_wmi(argv, home, wmi_start or _wmi_create)
        return "wmi"
    except Exception as exc:  # noqa: BLE001 - any WMI failure falls back to the detached start
        remove(home / LAUNCH_ENV_FILE)
        reason = f"{type(exc).__name__}: {exc}"
        _start_detached(argv, home, f"launcher: detached (WMI start failed: {reason})", popen)
        return "detached"


def _start_with_wmi(argv: list[str], home: Path, create: Callable[[str, str], None]) -> None:
    command = wmi_command_line(argv, home)
    _write_launch_env(home)
    _open_log(home, "launcher: WMI Win32_Process.Create").close()
    create(command, str(home))


def wmi_command_line(argv: list[str], home: Path) -> str:
    """The command line WMI runs: cmd.exe sets the home for the engine and appends its output to the log."""
    log = home / LOG_FILE
    for text in (str(home), str(log), *argv):
        if any(ch in text for ch in _WMI_UNSAFE):
            raise ValueError("a path or argument has a character the WMI command line cannot carry")
    comspec = os.environ.get("ComSpec") or os.path.join(
        os.environ.get("SystemRoot") or "C:\\Windows", "System32", "cmd.exe"
    )
    inner = f'set "{HOME_ENV}={home}" && {subprocess.list2cmdline(argv)} >> "{log}" 2>&1'
    return f'"{comspec}" /d /s /c "{inner}"'


def launch_environment_allowed(name: str) -> bool:
    """True when ``name`` is one of the variables saved for the engine (see ``LAUNCH_ENV_NAMES``)."""
    upper = name.upper()
    if name in LAUNCH_ENV_NAMES or upper in _LAUNCH_ENV_PROXY_NAMES:
        return True
    if not upper.startswith(_LAUNCH_ENV_HORIZON_PREFIX):
        return False
    return not any(part in upper for part in _LAUNCH_ENV_HORIZON_EXCLUDED)


def _write_launch_env(home: Path) -> None:
    """Save the engine's environment for the engine: WMI does not pass it on.

    Only ``LAUNCH_ENV_NAMES`` and the allowed ``HORIZON_*`` settings are written. The
    engine applies them and deletes the file; the starter deletes it if the engine has
    not done so when its wait ends.
    """
    values = {
        key: value
        for key, value in os.environ.items()
        if isinstance(value, str) and launch_environment_allowed(key)
    }
    write_private_json(home / LAUNCH_ENV_FILE, {"version": 1, "env": values})


def take_launch_environment(home: Path, environ: MutableMapping[str, str]) -> int:
    """Apply the environment the starter saved (see ``_write_launch_env``), then delete it.

    The file is deleted whatever it holds. A file older than ``LAUNCH_ENV_MAX_AGE_SECONDS``
    is not applied. Returns how many variables were applied. A missing or malformed file
    applies nothing.
    """
    path = home / LAUNCH_ENV_FILE
    try:
        written_at = path.stat().st_mtime
    except OSError:
        return 0
    data = read_json(path)
    remove(path)
    if time.time() - written_at > LAUNCH_ENV_MAX_AGE_SECONDS:
        return 0
    if not data or not isinstance(data.get("env"), dict):
        return 0
    applied = 0
    for key, value in data["env"].items():
        if isinstance(key, str) and launch_environment_allowed(key) and isinstance(value, str):
            environ[key] = value
            applied += 1
    return applied


def _wmi_create(command: str, cwd: str) -> None:
    """Asks WMI to create the process. Raises OSError when WMI refuses or PowerShell fails."""
    encoded = base64.b64encode(_WMI_SCRIPT.encode("utf-16-le")).decode("ascii")
    system_root = os.environ.get("SystemRoot") or "C:\\Windows"
    powershell = os.path.join(
        system_root, "System32", "WindowsPowerShell", "v1.0", "powershell.exe"
    )
    env = {**os.environ, "CONTEXTSHRINK_ENGINE_COMMAND": command, "CONTEXTSHRINK_ENGINE_DIR": cwd}
    try:
        result = subprocess.run(
            [powershell, "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
            env=env,
            capture_output=True,
            text=True,
            timeout=WMI_START_TIMEOUT_SECONDS,
            creationflags=_CREATE_NO_WINDOW,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise OSError("PowerShell did not answer in time") from exc
    if result.returncode != 0 or not result.stdout.strip().isdigit():
        raise OSError(f"PowerShell exited with {result.returncode}")


def _open_log(home: Path, first_line: str):
    """The engine log, truncated, with its first line saying how the engine was started."""
    log = open(home / LOG_FILE, "wb")  # noqa: SIM115 - returned open to the caller
    log.write((first_line + "\n").encode("utf-8"))
    log.flush()
    return log


def _start_detached(
    argv: list[str], home: Path, first_line: str, popen: Callable[..., Any] | None
) -> None:
    """The fallback start: a Popen in the background, with its output in the log."""
    start = popen or subprocess.Popen
    with _open_log(home, first_line) as log:
        kwargs: dict[str, Any] = {
            "stdin": subprocess.DEVNULL,
            "stdout": log,
            "stderr": subprocess.STDOUT,
            "cwd": str(home),
            "env": dict(os.environ),
            "close_fds": True,
        }
        if sys.platform == "win32":
            flags = _DETACHED_PROCESS | _CREATE_NEW_PROCESS_GROUP
            try:
                # Breakaway keeps the engine alive when the hook's job object ends.
                start(argv, creationflags=flags | _CREATE_BREAKAWAY_FROM_JOB, **kwargs)
                return
            except OSError:
                pass  # the job does not allow breakaway; start it in the job instead
            start(argv, creationflags=flags, **kwargs)
        else:
            start(argv, start_new_session=True, **kwargs)


# ── Stop and status ───────────────────────────────────────────────────


def stop(home: Path, port: int = DEFAULT_PORT, *, wait_seconds: float = 10.0) -> str:
    """Stop the engine on ``port``. Returns ``stopped``, ``not_running``, ``killed`` or ``still_running``.

    ``stopped`` means the engine's process has ended, not only that its port closed: the
    engine removes its PID file as its last step, after the final usage upload.
    """
    if health(port) is None:
        remove_pid(home)
        return "not_running"
    pid = read_pid(home)
    request_stop(home)
    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        if health(port) is None and not (pid is not None and pid_alive(pid)):
            remove_pid(home)
            return "stopped"
        time.sleep(0.2)
    if health(port) is None:
        # The port closed but the process did not end. It is not killed: its PID may be stale.
        return "still_running"
    # Last resort, by PID only. The health check above proved this port is the engine.
    if pid is not None:
        _kill_pid(pid)
        return "killed"
    return "still_running"


def pid_alive(pid: int) -> bool:
    """True when a process with this PID exists and has not exited."""
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return ctypes.get_last_error() == _ERROR_ACCESS_DENIED
        try:
            code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True
            return code.value == _STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


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


def exit_now(code: int) -> None:
    """End this process without interpreter shutdown.

    Interpreter shutdown can wait forever: a daemon thread that is still importing
    (the proxy's background model warm-up) keeps an import lock, and a destructor that
    imports then blocks the exit. Callers must have finished their own shutdown work
    (the usage upload, the PID file) first. Logging is flushed here.
    """
    import logging

    logging.shutdown()
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream is not None:
                stream.flush()
        except (OSError, ValueError):
            pass
    os._exit(code)
