"""How the Windows desktop client's own executable is seen from Python.

The desktop app runs ``horizon.exe`` in one of two builds:

- PyInstaller (``desktop/client/build-client.ps1``): ``horizon.exe`` is the
  frozen interpreter itself, so ``sys.frozen`` is set and ``sys.executable`` is
  the CLI.
- Embedded Python (``desktop/client/build-client-embed.ps1``): ``horizon.exe`` is
  a small launcher (``desktop/claude-code-tools/client-stub``) that runs the
  bundled ``python.exe``. The launcher exports two variables to that process:
  ``HORIZON_CLIENT_EXE`` (its own path) and ``HORIZON_LAUNCHER_PID`` (its PID).

A background start may run the embedded build's ``python\\pythonw.exe`` directly (the
GUI-subsystem interpreter, so no console window). Then the launcher and its variables
are absent, so the embedded build is recognised from its files instead
(``embedded_layout``). Environment variables never decide that a bundle is absent.

Nothing here changes behaviour outside those builds.
"""

import os
import sys
from pathlib import Path

#: Path of the launcher ``horizon.exe`` that started this process (embedded build).
CLIENT_EXE_ENV = "HORIZON_CLIENT_EXE"
#: PID of the launcher ``horizon.exe`` that started this process (embedded build).
LAUNCHER_PID_ENV = "HORIZON_LAUNCHER_PID"

# The embedded build writes python312._pth into its python folder (build-client-embed.ps1,
# for the client and the engine alike), and ships app\horizon_main.py beside that folder.
_EMBED_PTH_GLOB = "python*._pth"
_EMBED_SCRIPT = ("app", "horizon_main.py")
_CLI_FLAGS = ["-I", "-s", "-X", "utf8"]


def embedded_layout() -> tuple[Path, Path] | None:
    """``(python folder, app\\horizon_main.py)`` when this interpreter is the embedded build's, else ``None``.

    Decided from the files only: the interpreter's folder holds the ``python*._pth`` file,
    and the folder's parent holds ``app\\horizon_main.py``. No environment variable takes
    part, so no environment can make an embedded build count as a development install.
    """
    if getattr(sys, "frozen", False):
        return None
    try:
        folder = Path(sys.executable).resolve().parent
    except (OSError, RuntimeError):
        return None
    script = folder.parent.joinpath(*_EMBED_SCRIPT)
    if not script.is_file() or not any(folder.glob(_EMBED_PTH_GLOB)):
        return None
    return folder, script


def windowless_cli_command(folder: Path, script: Path) -> list[str]:
    """The embedded build's CLI through ``pythonw.exe`` (no console window), else ``python.exe``."""
    pythonw = folder / "pythonw.exe"
    interpreter = pythonw if pythonw.is_file() else folder / "python.exe"
    return [str(interpreter), *_CLI_FLAGS, str(script)]


def bundled_client_command() -> list[str] | None:
    """The bundled client's own command, or ``None`` outside the bundle.

    - The launcher ``horizon.exe`` named by ``HORIZON_CLIENT_EXE``, when the stub started
      this process (the desktop app's path). Subprocesses then run through the launcher.
    - The frozen executable, under PyInstaller (it is also the CLI).
    - The embedded build's own interpreter and script, when it runs without the launcher
      (for example ``pythonw.exe`` started by the engine's background launch).
    """
    exe = os.environ.get(CLIENT_EXE_ENV, "")
    if exe and os.path.isfile(exe):
        return [exe]
    if getattr(sys, "frozen", False):
        return [sys.executable]
    layout = embedded_layout()
    if layout is not None:
        return windowless_cli_command(*layout)
    return None


def is_bundled_client() -> bool:
    """True inside the shipped Windows client, either build, with or without the launcher."""
    return bundled_client_command() is not None or embedded_layout() is not None


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
