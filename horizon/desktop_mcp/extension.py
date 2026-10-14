"""Installed-extension runtime: write settings, ensure one broker, then serve stdio.

Claude Desktop launches only the MCP server process. This module lets that
process start the per-user broker itself, detached, when none is reachable.
Nothing here prints to stdout; stdout is the MCP protocol stream.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from .broker import BrokerClient, BrokerError
from .settings import (
    ConfigurationError,
    DesktopSettings,
    _overlaps_approved_roots,
    _validation_fields,
)

_EXTENSION_HOME_ENV = "CONTEXTSHRINK_EXTENSION_HOME"
_DEFAULT_BROKER_ID = "desktop-ext"
_EXTENSION_IDLE_SECONDS = 1800
_START_TIMEOUT_SECONDS = 15.0
_POLL_SECONDS = 0.2
# Only this transport code means "no broker is listening on the pipe".
_NOT_RUNNING_CODES = frozenset({"broker_unavailable"})
# Local transport failures that starting a broker cannot fix; surface them.
_FATAL_CODES = frozenset(
    {
        "windows_pipe_unavailable",
        "windows_pipe_dependency_unavailable",
        "identity_unavailable",
        "invalid_broker_id",
    }
)
_DETACHED_PROCESS = getattr(subprocess, "DETACHED_PROCESS", 0)
_CREATE_NEW_PROCESS_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

SpawnFunction = Callable[[list[str], Path], None]


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _extension_base_dir(base_dir: Path | None = None) -> Path:
    """The extension folder: explicit base, env override, or the per-user default."""
    if base_dir is not None:
        return Path(base_dir)
    if (override := os.environ.get(_EXTENSION_HOME_ENV)) and os.path.isabs(override):
        return Path(override)
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA")
        root = Path(local) if local else Path.home() / "AppData" / "Local"
        return root / "ContextShrink" / "extension"
    return Path.home() / ".local" / "state" / "contextshrink" / "extension"


def extension_config_path(broker_id: str, base_dir: Path | None = None) -> Path:
    """Return the settings file the extension writes for one broker identity."""
    return _extension_base_dir(base_dir) / f"{broker_id}.json"


def extension_stats_path(broker_id: str, base_dir: Path | None = None) -> Path:
    """Return the cumulative usage file for one broker identity.

    The rule is a sibling of the extension folder: with the default layout
    ``<ContextShrink>/extension/<id>.json`` the stats are at
    ``<ContextShrink>/stats/<id>.json``. The same parent is used when
    CONTEXTSHRINK_EXTENSION_HOME or ``base_dir`` overrides the folder.
    """
    return _extension_base_dir(base_dir).parent / "stats" / f"{broker_id}.json"


def write_extension_settings(settings: DesktopSettings, path: Path) -> bool:
    """Atomically write the settings file; return True only when content changed."""
    path = Path(path)
    payload = json.dumps(settings.model_dump(mode="json"), sort_keys=True, indent=2) + "\n"
    data = payload.encode("utf-8")
    try:
        if path.read_bytes() == data:
            return False
    except OSError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    return True


def broker_command(config_path: Path) -> list[str]:
    """Command line that starts the broker for an extension settings file."""
    if getattr(sys, "frozen", False):
        return [sys.executable, "broker", "--config", str(config_path)]
    return [sys.executable, "-m", "horizon.desktop_mcp", "broker", "--config", str(config_path)]


def _spawn_detached(command: list[str], cwd: Path) -> None:
    environment: dict[str, str] | None = None
    if not getattr(sys, "frozen", False):
        # `-m horizon.desktop_mcp` must resolve even when Claude Desktop launched
        # the parent with a minimal environment.
        environment = dict(os.environ)
        existing = environment.get("PYTHONPATH")
        environment["PYTHONPATH"] = os.pathsep.join(
            part for part in (str(_repo_root()), existing) if part
        )
    try:
        subprocess.Popen(
            command,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            creationflags=_DETACHED_PROCESS | _CREATE_NEW_PROCESS_GROUP | _CREATE_NO_WINDOW,
            env=environment,
        )
    except OSError:
        raise BrokerError("broker_start_failed") from None


def _probe(client: BrokerClient) -> bool:
    """True when a broker answers; False when no broker is listening."""
    try:
        client.stats()
    except BrokerError as exc:
        if exc.code in _NOT_RUNNING_CODES:
            return False
        if exc.code in _FATAL_CODES:
            raise
        # Any other protocol-level answer means a broker is reachable.
    return True


def ensure_broker(
    settings: DesktopSettings,
    config_path: Path,
    *,
    timeout: float = _START_TIMEOUT_SECONDS,
    spawn: SpawnFunction | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Make sure a broker serves this identity, starting one detached if needed.

    Two chats may both start a broker. Only one can own the pipe; the other
    exits, and this function then succeeds against the owner.
    """
    client = BrokerClient(settings)
    if _probe(client):
        return
    config_path = Path(config_path)
    (spawn or _spawn_detached)(broker_command(config_path), config_path.parent)
    deadline = clock() + timeout
    while True:
        if _probe(client):
            return
        if clock() >= deadline:
            raise BrokerError("broker_start_failed")
        sleep(_POLL_SECONDS)


def _extension_settings(args: Any) -> DesktopSettings:
    artifact_root = getattr(args, "artifact_root", None) or None
    approved_roots = list(getattr(args, "approved_roots", None) or [])
    broker_id = getattr(args, "broker_id", None) or _DEFAULT_BROKER_ID
    # Stats are skipped (not an error) when the stats folder sits inside an
    # approved root, e.g. a user who approved their whole profile folder.
    stats_path = extension_stats_path(broker_id)
    usage_stats_path = (
        None
        if _overlaps_approved_roots(stats_path, tuple(Path(str(root)) for root in approved_roots))
        else str(stats_path)
    )
    data: dict[str, Any] = {
        "profile": "local",
        "approved_roots": approved_roots,
        "broker_id": broker_id,
        "tool_profile": getattr(args, "tool_profile", None) or "source",
        "artifact_root": artifact_root,
        "usage_stats_path": usage_stats_path,
        "broker_idle_seconds": _EXTENSION_IDLE_SECONDS,
    }
    try:
        return DesktopSettings.model_validate(data)
    except ValidationError as error:
        raise ConfigurationError("config_invalid", _validation_fields(error)) from None


def run_extension(args: Any) -> None:
    """Build settings from extension flags, ensure the broker, then serve stdio."""
    from .server import serve_stdio

    settings = _extension_settings(args)
    config_path = extension_config_path(settings.broker_id)
    try:
        write_extension_settings(settings, config_path)
    except OSError:
        raise ConfigurationError("extension_config_unwritable") from None
    ensure_broker(settings, config_path)
    # A broker started from this process can end with the host's process job
    # when the chat that started it closes; restart it on demand.
    asyncio.run(serve_stdio(settings, recover_broker=lambda: ensure_broker(settings, config_path)))
