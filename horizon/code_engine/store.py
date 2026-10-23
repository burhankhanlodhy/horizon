"""Local files shared by the engine: the home directory and atomic private writes.

Everything the engine keeps lives under ``~/.contextshrink/`` (or
``CONTEXTSHRINK_HOME``). Files are written atomically and, on POSIX, are
readable by the user only.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

HOME_ENV = "CONTEXTSHRINK_HOME"


def home_dir() -> Path:
    """The ContextShrink home directory, created on first write."""
    override = os.environ.get(HOME_ENV, "").strip()
    return Path(override).expanduser() if override else Path.home() / ".contextshrink"


def read_json(path: Path) -> dict[str, Any] | None:
    """A JSON object from ``path``, or ``None`` when it is missing or unreadable."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def write_private_json(path: Path, data: dict[str, Any]) -> None:
    """Write ``data`` to ``path`` atomically: temp file, fsync, then rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        if os.name != "nt":
            os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def remove(path: Path) -> None:
    """Delete ``path`` if it exists."""
    with contextlib.suppress(FileNotFoundError):
        path.unlink()
