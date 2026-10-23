"""How the Windows desktop client's own executable is seen from Python.

The desktop app runs ``horizon.exe`` in one of two builds:

- PyInstaller (``desktop/client/build-client.ps1``): ``horizon.exe`` is the
  frozen interpreter itself, so ``sys.frozen`` is set and ``sys.executable`` is
  the CLI.
- Embedded Python (``desktop/client/build-client-embed.ps1``): ``horizon.exe`` is
  a small launcher (``desktop/claude-code-tools/client-stub``) that runs the
  bundled ``python.exe``. The launcher exports two variables to that process:
  ``HORIZON_CLIENT_EXE`` (its own path) and ``HORIZON_LAUNCHER_PID`` (its PID).

Nothing here changes behaviour outside those two builds.
"""

import os
import sys

#: Path of the launcher ``horizon.exe`` that started this process (embedded build).
CLIENT_EXE_ENV = "HORIZON_CLIENT_EXE"
#: PID of the launcher ``horizon.exe`` that started this process (embedded build).
LAUNCHER_PID_ENV = "HORIZON_LAUNCHER_PID"


def bundled_client_command() -> list[str] | None:
    """The bundled client's own executable, or ``None`` outside the bundle.

    Under the embedded build this is the launcher, so subprocesses run the CLI
    through the same entry point the desktop app uses. Under PyInstaller it is
    the frozen executable, which is also the CLI.
    """
    exe = os.environ.get(CLIENT_EXE_ENV, "")
    if exe and os.path.isfile(exe):
        return [exe]
    if getattr(sys, "frozen", False):
        return [sys.executable]
    return None


def is_bundled_client() -> bool:
    """True inside the shipped Windows client, either build (see ``bundled_client_command``)."""
    return bundled_client_command() is not None


def cli_command() -> list[str]:
    """The argv prefix that runs the Horizon CLI in this build."""
    return bundled_client_command() or [sys.executable, "-m", "horizon.cli"]


def serving_pid() -> int:
    """The PID the desktop app stops this process by.

    The app knows the launcher, not the Python process under it: it starts the
    launcher, and it identifies a process by an image named ``horizon.exe``. So
    when this process was started directly by the launcher, it reports the
    launcher's PID. Killing the launcher ends this process too (job object).
    Anywhere else it is this process's own PID.
    """
    raw = os.environ.get(LAUNCHER_PID_ENV, "")
    if raw.isdigit() and int(raw) == os.getppid():
        return int(raw)
    return os.getpid()
