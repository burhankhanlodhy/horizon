"""Installed-extension runtime: write settings, ensure one broker, then serve stdio.

Claude Desktop launches only the MCP server process. This module lets that
process start the per-user broker itself, detached, when none is reachable.
Nothing here prints to stdout; stdout is the MCP protocol stream.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
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
from .version import VERSION

_EXTENSION_HOME_ENV = "CONTEXTSHRINK_EXTENSION_HOME"
_DEFAULT_BROKER_ID = "desktop-ext"
# Short idle: a broker outlives its chat only until the next heartbeat window.
_EXTENSION_IDLE_SECONDS = 120
_HEARTBEAT_SECONDS = 30.0
_START_TIMEOUT_SECONDS = 15.0
_RETIRE_WAIT_SECONDS = 10.0
_POLL_SECONDS = 0.2
_BUILD_ID = re.compile(r"\d+\.\d+\.\d+-[0-9a-f]{12}")
_STALE_TEMPORARY = re.compile(r"\d+\.\d+\.\d+-[0-9a-f]{12}\.tmp-[0-9a-f]+")
_STALE_TEMPORARY_SECONDS = 3600
_COMPLETE_MARKER = ".complete"
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400
_VERSION_PARTS = re.compile(r"\d+(\.\d+)*")
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


def runtime_root(base_dir: Path | None = None) -> Path:
    """Folder that holds one copied runtime per build: ``<ContextShrink>/runtime``."""
    return _extension_base_dir(base_dir).parent / "runtime"


def build_id() -> str:
    """``<VERSION>-<first 12 hex of sha256(executable bytes)>``, streamed from disk."""
    digest = hashlib.sha256()
    with open(sys.executable, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return f"{VERSION}-{digest.hexdigest()[:12]}"


def _is_complete(path: Path) -> bool:
    return (path / _COMPLETE_MARKER).is_file()


def ensure_runtime_copy(base_dir: Path | None = None) -> Path:
    """Copy the running one-folder build into the runtime root; return its executable.

    The copy is made beside its final name and renamed into place only once it
    has its ``.complete`` marker. If another process renames its copy first,
    that copy is used and this one is removed.
    """
    source = Path(sys.executable)
    root = runtime_root(base_dir)
    name = build_id()
    target = root / name
    if _is_complete(target):
        return target / source.name
    root.mkdir(parents=True, exist_ok=True)
    temporary = root / f"{name}.tmp-{secrets.token_hex(6)}"
    try:
        shutil.copytree(source.parent, temporary)
        (temporary / _COMPLETE_MARKER).write_text("complete\n", encoding="utf-8")
        if target.exists() and not _is_complete(target):
            # An interrupted earlier copy: it is never used, so it is replaced.
            shutil.rmtree(target, ignore_errors=True)
        try:
            os.replace(temporary, target)
        except OSError:
            if not _is_complete(target):
                raise
            # Another process renamed its complete copy into place first.
    finally:
        if temporary.exists():
            shutil.rmtree(temporary, ignore_errors=True)
    return target / source.name


def _is_link(path: Path) -> bool:
    """True for symlinks and junctions, and for anything that cannot be inspected."""
    try:
        info = os.lstat(path)
    except OSError:
        return True
    if stat.S_ISLNK(info.st_mode):
        return True
    is_junction = getattr(path, "is_junction", None)
    if is_junction is not None and is_junction():
        return True
    return bool(getattr(info, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT)


def _remove_folder(path: Path) -> bool:
    """Remove one runtime folder; False when it is in use or cannot be removed.

    The folder is first renamed to a generated temporary name. Windows refuses
    that rename while a DLL inside is loaded, so a running broker's folder is
    left untouched. A partly removed folder keeps its temporary name and a
    later cleanup retries it.
    """
    trash = path.with_name(f"{path.name}.tmp-{secrets.token_hex(4)}")
    try:
        os.replace(path, trash)
    except OSError:
        return False
    try:
        shutil.rmtree(trash, ignore_errors=True)
    except OSError:
        return False
    return not trash.exists()


def cleanup_runtimes(keep: str | None = None, base_dir: Path | None = None) -> list[str]:
    """Remove other build folders and stale temporary copies; return the removed names.

    Only generated names are considered. Links and junctions are never followed
    or removed. The folder named ``keep`` (the running build) is never touched.
    """
    root = runtime_root(base_dir)
    if not root.is_dir():
        return []
    removed: list[str] = []
    now = time.time()
    for entry in sorted(root.iterdir()):
        name = entry.name
        if name == keep:
            continue
        if _BUILD_ID.fullmatch(name) is None:
            if _STALE_TEMPORARY.fullmatch(name) is None:
                continue
            try:
                age = now - os.lstat(entry).st_mtime
            except OSError:
                continue
            if age < _STALE_TEMPORARY_SECONDS:
                continue
        if _is_link(entry):
            continue
        if _remove_folder(entry):
            removed.append(name)
    return removed


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
    """Command line that starts the broker for an extension settings file.

    A frozen build starts its broker from a per-build runtime copy, so the
    extension folder and the DLLs in it are never held open by the broker.
    """
    if getattr(sys, "frozen", False):
        try:
            executable = ensure_runtime_copy()
        except OSError:
            raise BrokerError("runtime_copy_failed") from None
        return [str(executable), "broker", "--config", str(config_path)]
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


def _is_older(value: object) -> bool:
    """True when a reported version is strictly older than this build.

    A missing or unparsable version counts as older.
    """
    if not isinstance(value, str) or _VERSION_PARTS.fullmatch(value) is None:
        return True
    found = tuple(int(part) for part in value.split("."))
    wanted = tuple(int(part) for part in VERSION.split("."))
    width = max(len(found), len(wanted))
    return found + (0,) * (width - len(found)) < wanted + (0,) * (width - len(wanted))


def _hand_over(
    client: BrokerClient, *, clock: Callable[[], float], sleep: Callable[[float], None]
) -> bool:
    """Retire an older broker; True when this process should start its own.

    An equal or newer broker is kept. A broker that cannot be retired (one from
    before the lifecycle operations existed) is kept as well.
    """
    try:
        older = _is_older(client.heartbeat().get("version"))
    except BrokerError as exc:
        if exc.code in _NOT_RUNNING_CODES:
            return True
        if exc.code in _FATAL_CODES:
            raise
        # Reachable but without heartbeat: it predates this build.
        older = True
    if not older:
        return False
    try:
        client.retire()
    except BrokerError as exc:
        if exc.code in _NOT_RUNNING_CODES:
            return True
        if exc.code in _FATAL_CODES:
            raise
        return False
    deadline = clock() + _RETIRE_WAIT_SECONDS
    while True:
        try:
            client.heartbeat()
        except BrokerError as exc:
            if exc.code in _NOT_RUNNING_CODES:
                return True
            if exc.code in _FATAL_CODES:
                raise
        if clock() >= deadline:
            return False
        sleep(_POLL_SECONDS)


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
    exits, and this function then succeeds against the owner. A broker from an
    older build is retired first, so the upgraded build takes over.
    """
    client = BrokerClient(settings)
    if _probe(client) and not _hand_over(client, clock=clock, sleep=sleep):
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


def _heartbeat_loop(
    send: Callable[[], Any], wait: Callable[[float], bool], interval: float
) -> None:
    """Send heartbeats until ``wait`` reports stop. Nothing is written to stdout."""
    while not wait(interval):
        try:
            send()
        except BrokerError:
            # The broker may be restarting; the next heartbeat tries again.
            pass


_PILOT_METHOD_ARMS = {"normal": "optimized", "exact_only": "raw"}


def _pilot_eval_arm(value: object) -> str:
    """Map the Pilot method setting to the backend evaluation arm.

    ``exact_only`` (self-test method A) is ``raw``: every presentation returns
    exact text. A missing value means ``normal``. Anything else is rejected
    without echoing the value.
    """
    if value is None:
        value = "normal"
    if not isinstance(value, str) or value not in _PILOT_METHOD_ARMS:
        raise ConfigurationError("extension_pilot_method_invalid")
    return _PILOT_METHOD_ARMS[value]


def _extension_settings(args: Any) -> DesktopSettings:
    artifact_root = getattr(args, "artifact_root", None) or None
    approved_roots = list(getattr(args, "approved_roots", None) or [])
    broker_id = getattr(args, "broker_id", None) or _DEFAULT_BROKER_ID
    eval_arm = _pilot_eval_arm(getattr(args, "pilot_method", None))
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
        "eval_arm": eval_arm,
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
    # Keeps the broker alive while this chat is open; it idles out afterwards.
    stop_heartbeat = threading.Event()
    threading.Thread(
        target=_heartbeat_loop,
        args=(BrokerClient(settings).heartbeat, stop_heartbeat.wait, _HEARTBEAT_SECONDS),
        daemon=True,
        name="contextshrink-heartbeat",
    ).start()
    try:
        # A broker started from this process can end with the host's process job
        # when the chat that started it closes; restart it on demand.
        asyncio.run(
            serve_stdio(settings, recover_broker=lambda: ensure_broker(settings, config_path))
        )
    finally:
        stop_heartbeat.set()
