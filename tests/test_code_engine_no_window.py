"""The background engine, its launcher, the SessionStart hook and the Startup shortcut never show a window.

Windows only. A watcher polls every visible top-level window (EnumWindows, IsWindowVisible,
GetWindowThreadProcessId) while the steps run, and fails on any window that is new during
the run and owned by a console or script host (horizon.exe, python(w).exe, powershell.exe,
cmd.exe, conhost.exe, taskkill.exe). The test's own console is in the baseline.

Temporary CONTEXTSHRINK_HOME and profile directories and a non-default port are used.
Engines are ended with runtime.stop, never by image name.
"""

from __future__ import annotations

import base64
import contextlib
import ctypes
import os
import socket
import subprocess
import sys
import threading
import time
from ctypes import wintypes
from pathlib import Path

import pytest

from horizon.code_engine import runtime
from horizon.code_engine.store import HOME_ENV

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="window checks are Windows-only")

WATCHED = frozenset(
    {
        "horizon.exe",
        "python.exe",
        "pythonw.exe",
        "powershell.exe",
        "cmd.exe",
        "conhost.exe",
        "taskkill.exe",
    }
)
CREATE_NO_WINDOW = 0x08000000

if sys.platform == "win32":
    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _ENUM_PROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    _user32.EnumWindows.argtypes = [_ENUM_PROC, wintypes.LPARAM]
    _user32.IsWindowVisible.argtypes = [wintypes.HWND]
    _user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    ]
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]


def _image_name(pid: int) -> str:
    handle = _kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return f"pid {pid}"
    try:
        size = wintypes.DWORD(1024)
        buffer = ctypes.create_unicode_buffer(1024)
        if not _kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return f"pid {pid}"
        return Path(buffer.value).name.lower()
    finally:
        _kernel32.CloseHandle(handle)


def _visible_windows() -> set[tuple[int, str]]:
    """(window handle, owner image name) for every visible top-level window."""
    found: set[tuple[int, str]] = set()

    def visit(hwnd, _lparam):
        if _user32.IsWindowVisible(hwnd):
            owner = wintypes.DWORD(0)
            _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
            found.add((int(hwnd), _image_name(owner.value)))
        return True

    callback = _ENUM_PROC(visit)
    _user32.EnumWindows(callback, 0)
    return found


class WindowWatch:
    """Records the visible windows that appear while the block runs."""

    def __init__(self) -> None:
        self._baseline: set[tuple[int, str]] = set()
        self._new: set[tuple[int, str]] = set()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def __enter__(self) -> WindowWatch:
        self._baseline = _visible_windows()
        self._thread = threading.Thread(target=self._poll, daemon=True)
        self._thread.start()
        return self

    def _poll(self) -> None:
        while not self._stop.is_set():
            self._new |= _visible_windows() - self._baseline
            self._stop.wait(0.02)

    def __exit__(self, *exc) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(10)
        self._new |= _visible_windows() - self._baseline

    def offenders(self) -> list[tuple[int, str]]:
        return sorted(item for item in self._new if item[1] in WATCHED)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _sandbox(monkeypatch, tmp_path: Path) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    profile = tmp_path / "profile"
    profile.mkdir()
    monkeypatch.setenv(HOME_ENV, str(home))
    monkeypatch.setenv("HOME", str(profile))
    monkeypatch.setenv("USERPROFILE", str(profile))
    monkeypatch.setenv("APPDATA", str(profile / "AppData" / "Roaming"))
    monkeypatch.setenv("LOCALAPPDATA", str(profile / "AppData" / "Local"))
    return home


def _answering(port: int) -> bool:
    return runtime.health(port, timeout=1.0) is not None


def _wait_until(condition, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.2)
    return condition()


def _powershell() -> str:
    root = os.environ.get("SystemRoot") or "C:\\Windows"
    return os.path.join(root, "System32", "WindowsPowerShell", "v1.0", "powershell.exe")


def _write_shortcut(path: Path, target: str, arguments: str, workdir: Path) -> None:
    """Writes a .lnk the way the npx installer does, into a temporary folder."""
    script = "\n".join(
        [
            "$ErrorActionPreference = 'Stop'",
            "$shell = New-Object -ComObject WScript.Shell",
            "$link = $shell.CreateShortcut($env:CS_LNK)",
            "$link.TargetPath = $env:CS_TARGET",
            "$link.Arguments = $env:CS_ARGS",
            "$link.WorkingDirectory = $env:CS_WORKDIR",
            "$link.WindowStyle = 7",
            "$link.Save()",
        ]
    )
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    env = {
        **os.environ,
        "CS_LNK": str(path),
        "CS_TARGET": target,
        "CS_ARGS": arguments,
        "CS_WORKDIR": str(workdir),
    }
    subprocess.run(
        [
            _powershell(),
            "-NoProfile",
            "-NonInteractive",
            "-WindowStyle",
            "Hidden",
            "-EncodedCommand",
            encoded,
        ],
        env=env,
        capture_output=True,
        timeout=120,
        check=True,
        creationflags=CREATE_NO_WINDOW,
    )


def test_the_watch_sees_a_console_that_is_opened_on_purpose() -> None:
    """A control: a console opened with CREATE_NEW_CONSOLE must be detected, or the other tests prove nothing."""
    create_new_console = 0x00000010
    with WindowWatch() as watch:
        child = subprocess.Popen(
            ["cmd.exe", "/c", "ping -n 4 127.0.0.1 >nul"], creationflags=create_new_console
        )
        assert _wait_until(lambda: bool(watch.offenders()), 20), (
            "the watch did not see a visible console"
        )
        child.wait(timeout=60)
    assert any(name in {"cmd.exe", "conhost.exe"} for _, name in watch.offenders())


def test_the_hook_and_the_launcher_show_no_window(tmp_path, monkeypatch) -> None:
    home = _sandbox(monkeypatch, tmp_path)
    port = _free_port()
    hook = [*runtime.background_command(), "code-engine", "ensure", "--port", str(port)]
    try:
        with WindowWatch() as watch:
            # The SessionStart hook: started the way Claude Code starts a hook (no window flags).
            subprocess.run(hook, cwd=str(home), capture_output=True, timeout=200, check=False)
            assert _wait_until(lambda: _answering(port), 150), "the hook did not start the engine"
            assert runtime.stop(home, port, wait_seconds=30) == "stopped"
        assert watch.offenders() == [], f"windows appeared: {watch.offenders()}"
    finally:
        runtime.stop(home, port, wait_seconds=30)


def test_the_startup_shortcut_shows_no_window(tmp_path, monkeypatch) -> None:
    home = _sandbox(monkeypatch, tmp_path)
    port = _free_port()
    command = runtime.background_command()
    shortcut = tmp_path / "ContextShrink Engine.lnk"
    arguments = subprocess.list2cmdline([*command[1:], "code-engine", "run", "--port", str(port)])
    _write_shortcut(shortcut, command[0], arguments, home)
    try:
        with WindowWatch() as watch:
            # What logon does with the Startup shortcut: ShellExecute on the .lnk.
            os.startfile(str(shortcut))
            assert _wait_until(lambda: _answering(port), 150), (
                "the shortcut did not start the engine"
            )
            assert runtime.stop(home, port, wait_seconds=30) == "stopped"
        assert watch.offenders() == [], f"windows appeared: {watch.offenders()}"
    finally:
        with contextlib.suppress(OSError):
            shortcut.unlink()
        runtime.stop(home, port, wait_seconds=30)
