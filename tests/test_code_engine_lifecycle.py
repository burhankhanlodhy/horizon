"""The engine's lifetime, with a real engine process on a non-default port.

Every test uses a temporary CONTEXTSHRINK_HOME and a temporary profile, so the
real home and %LOCALAPPDATA%\\ContextShrink are never touched. Processes are
ended through ``runtime.stop`` (the health check and PID file), never by image name.
"""

from __future__ import annotations

import socket
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from horizon.code_engine import runtime
from horizon.code_engine.store import HOME_ENV

STARTER_SOURCE = """\
import sys
import time
from pathlib import Path

from horizon.code_engine import runtime

sys.stdin.readline()  # the test puts this process into a job object first
answered = runtime.ensure(Path(sys.argv[1]), int(sys.argv[2]), wait_seconds=150)
print("ANSWERED" if answered else "NOT_ANSWERED", flush=True)
time.sleep(600)  # still inside the job when the test closes it
"""


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _sandbox(monkeypatch, tmp_path: Path) -> Path:
    """A temporary home and profile for this test, in the environment the engine will inherit."""
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


def _wait_until(condition: Callable[[], bool], seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.2)
    return condition()


def _answering(port: int) -> bool:
    return runtime.health(port, timeout=1.0) is not None


# ── Windows: the engine outlives the job object that started it ───────

if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000

    class _IoCounters(ctypes.Structure):
        _fields_ = [
            (name, ctypes.c_ulonglong)
            for name in (
                "ReadOperationCount",
                "ReadTransferCount",
                "WriteOperationCount",
                "WriteTransferCount",
                "OtherOperationCount",
                "OtherTransferCount",
            )
        ]

    class _BasicLimits(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _ExtendedLimits(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _BasicLimits),
            ("IoInfo", _IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    _kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
    _kernel32.SetInformationJobObject.restype = wintypes.BOOL
    _kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    _kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    _kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    _kernel32.CloseHandle.restype = wintypes.BOOL
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

    def _kill_on_close_job() -> int:
        """A job whose members are killed when its last handle closes, with no breakaway allowed."""
        job = _kernel32.CreateJobObjectW(None, None)
        if not job:
            raise OSError(ctypes.get_last_error(), "CreateJobObjectW failed")
        info = _ExtendedLimits()
        info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not _kernel32.SetInformationJobObject(
            job, _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION, ctypes.byref(info), ctypes.sizeof(info)
        ):
            raise OSError(ctypes.get_last_error(), "SetInformationJobObject failed")
        return job

    def _put_in_job(job: int, process: subprocess.Popen) -> None:
        if not _kernel32.AssignProcessToJobObject(job, wintypes.HANDLE(process._handle)):
            raise OSError(ctypes.get_last_error(), "AssignProcessToJobObject failed")


@pytest.mark.skipif(sys.platform != "win32", reason="job objects are Windows-only")
def test_engine_started_through_the_launcher_outlives_the_job_that_started_it(
    tmp_path, monkeypatch
) -> None:
    home = _sandbox(monkeypatch, tmp_path)
    port = _free_port()
    starter = tmp_path / "starter.py"
    starter.write_text(STARTER_SOURCE, encoding="utf-8")
    job = _kill_on_close_job()
    proc = subprocess.Popen(
        [sys.executable, str(starter), str(home), str(port)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=str(tmp_path),
    )
    pid = None
    try:
        _put_in_job(job, proc)
        proc.stdin.write(b"go\n")
        proc.stdin.flush()
        answer = proc.stdout.readline().decode("utf-8", "replace").strip()
        assert answer == "ANSWERED", proc.stderr.read().decode("utf-8", "replace")[-2000:]
        assert _answering(port), "the engine did not answer before the job closed"
        pid = runtime.read_pid(home)
        assert pid is not None

        # Closing the last handle kills the starter (in the job) and anything still in it.
        _kernel32.CloseHandle(job)
        job = 0
        proc.wait(timeout=60)  # the starter was still sleeping inside the job

        time.sleep(3)
        assert _answering(port), "the engine died when the job that started it closed"
        assert runtime.pid_alive(pid), "the engine process ended with the job"
    finally:
        if job:
            _kernel32.CloseHandle(job)
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        runtime.stop(home, port, wait_seconds=30)


def test_stop_ends_the_process_across_repeated_starts(tmp_path, monkeypatch) -> None:
    home = _sandbox(monkeypatch, tmp_path)
    port = _free_port()
    for cycle in range(4):
        try:
            runtime.spawn_detached(home, port)
            assert _wait_until(lambda: _answering(port), 150), (
                f"cycle {cycle}: the engine did not answer"
            )
            pid = runtime.read_pid(home)
            assert pid is not None, f"cycle {cycle}: the engine wrote no PID file"
            # Stop straight away, while the engine is still warming up its models.
            assert runtime.stop(home, port, wait_seconds=15) == "stopped", f"cycle {cycle}"
            assert _wait_until(lambda pid=pid: not runtime.pid_alive(pid), 20), (
                f"cycle {cycle}: process {pid} still alive"
            )
            assert not _answering(port), f"cycle {cycle}: the port still answers"
        finally:
            runtime.stop(home, port, wait_seconds=15)
