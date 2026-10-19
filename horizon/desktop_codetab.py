"""Route Claude Desktop's Code tab through the ContextShrink forwarder.

Claude Desktop downloads Claude Code into versioned folders::

    <root>\\<version>\\<hash>\\claude.exe
    <root>\\<version>\\<hash>\\.payload      {"sha256": "...", "size": N}

The patcher moves the real ``claude.exe`` aside to ``claude.cs-real.exe`` and
puts ``claude-code-launcher.exe`` in its place as ``claude.exe``. The launcher
reads ``codetab.json`` and starts the real binary, adding the forwarder's
``ANTHROPIC_BASE_URL`` only when the forwarder answers its health route.

Safety rules, all enforced here:

- A real binary is never deleted. Renames are only done after its sha256 has
  been checked against ``.payload``, and re-checked just before the rename.
- ``claude.exe`` is only replaced when it is absent, holds the real binary
  being moved, or is a launcher copy this module recorded when it placed it.
  Any other file is left alone.
- A locked file skips that folder and is reported; the other folders still
  run, and ``watch`` retries on the next pass.
- A failed step rolls back to a runnable real ``claude.exe`` before returning.
- Reparse points (junctions and symlinks) are never followed.
- State and config writes take an exclusive lock file and use unique temp
  names, so a manual install cannot collide with a running ``--watch``.

Roots default to Claude Desktop's folders on Windows. Set
``CONTEXTSHRINK_CODETAB_ROOTS`` (``os.pathsep``-separated) to override them, and
``CONTEXTSHRINK_DATA_DIR`` to move the ``%LOCALAPPDATA%\\ContextShrink`` data
folder. Tests always pass explicit roots and data dirs.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import sys
import tempfile
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REAL_NAME = "claude.exe"
STASH_NAME = "claude.cs-real.exe"
PAYLOAD_NAME = ".payload"
DISPLACED_STEM = "claude.exe.cs-displaced"
LAUNCHER_NAME = "claude-code-launcher.exe"
CONFIG_NAME = "codetab.json"
STATE_NAME = "codetab-state.json"
LOCK_NAME = "codetab.lock"
DEFAULT_HEALTH_PORT = 18788
DEFAULT_BASE_URL = f"http://127.0.0.1:{DEFAULT_HEALTH_PORT}"
WATCH_INTERVAL_SECONDS = 10.0
#: The watcher re-verifies everything at least this often, even when nothing
#: it looks at has changed size or modification time.
WATCH_FULL_PASS_SECONDS = 6 * 3600.0
LOCK_TIMEOUT_SECONDS = 5.0
STALE_LOCK_SECONDS = 120.0
STATE_VERSION = 1
CONFIG_VERSION = 1
FILE_ATTRIBUTE_REPARSE_POINT = 0x400

ENV_ROOTS = "CONTEXTSHRINK_CODETAB_ROOTS"
ENV_DATA_DIR = "CONTEXTSHRINK_DATA_DIR"
ENV_LAUNCHER = "CONTEXTSHRINK_CODETAB_LAUNCHER"
#: Keep-sessions-lean: the compaction window the launcher may pass through ``env``.
LEAN_WINDOW_KEY = "CLAUDE_CODE_AUTO_COMPACT_WINDOW"
LEAN_WINDOW_MIN = 100_000
LEAN_WINDOW_MAX = 1_000_000

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_BASE_URL_RE = re.compile(r"^http://127\.0\.0\.1:(\d{1,5})(/[A-Za-z0-9/_.~%-]*)?$")


class CodetabError(RuntimeError):
    """A problem that stops the whole operation, or one folder's step."""


# ---------------------------------------------------------------------------
# Locations and discovery
# ---------------------------------------------------------------------------


def data_dir(environ: Mapping[str, str] | None = None) -> Path:
    """``%LOCALAPPDATA%\\ContextShrink``, or ``$CONTEXTSHRINK_DATA_DIR``."""
    env = os.environ if environ is None else environ
    override = (env.get(ENV_DATA_DIR) or "").strip()
    if override:
        return Path(override).expanduser()
    local = (env.get("LOCALAPPDATA") or "").strip()
    base = Path(local) if local else Path.home() / "AppData" / "Local"
    return base / "ContextShrink"


def claude_code_dir(environ: Mapping[str, str] | None = None) -> Path:
    return data_dir(environ) / "claude-code"


def default_launcher_path(environ: Mapping[str, str] | None = None) -> Path:
    env = os.environ if environ is None else environ
    override = (env.get(ENV_LAUNCHER) or "").strip()
    if override:
        return Path(override).expanduser()
    return data_dir(environ) / "bin" / LAUNCHER_NAME


def discover_roots(environ: Mapping[str, str] | None = None) -> list[Path]:
    """Claude Desktop's ``claude-code`` roots that exist on this machine."""
    env = os.environ if environ is None else environ
    override = env.get(ENV_ROOTS)
    if override is not None:
        candidates = [Path(part) for part in override.split(os.pathsep) if part.strip()]
    else:
        candidates = []
        if sys.platform == "win32":
            local = (env.get("LOCALAPPDATA") or "").strip()
            if local:
                packages = Path(local) / "Packages"
                try:
                    matches = sorted(packages.glob("Claude_*"))
                except OSError:
                    matches = []
                for package in matches:
                    candidates.append(package / "LocalCache" / "Roaming" / "Claude" / "claude-code")
            appdata = (env.get("APPDATA") or "").strip()
            if appdata:
                candidates.append(Path(appdata) / "Claude" / "claude-code")
    return [path for path in candidates if path.is_dir()]


def is_reparse_point(path: Path) -> bool:
    """True for symlinks, junctions and any other reparse point. A missing path is not one."""
    try:
        info = os.lstat(path)
    except OSError:
        return False
    if stat.S_ISLNK(info.st_mode):
        return True
    return bool(getattr(info, "st_file_attributes", 0) & FILE_ATTRIBUTE_REPARSE_POINT)


def _inside_roots_without_links(folder: Path, roots: list[Path]) -> bool:
    """True when ``folder`` is strictly below a root and no component on the way is a link."""
    for root in roots:
        try:
            relative = folder.relative_to(root)
        except ValueError:
            continue
        if not relative.parts:
            continue
        current = root
        for part in relative.parts:
            current = current / part
            if is_reparse_point(current):
                return False
        return True
    return False


def scan_folders(roots: list[Path]) -> tuple[list[Path], list[FolderReport]]:
    """Every ``<root>\\<version>\\<hash>`` folder that holds a Claude binary.

    Symlinks and junctions are not followed. Each one is returned as a skipped
    report, so the caller can show it.
    """
    folders: list[Path] = []
    skipped: list[FolderReport] = []
    for root in roots:
        if is_reparse_point(root):
            skipped.append(FolderReport(root, "reparse", "skipped", "root is a link; not followed"))
            continue
        try:
            versions = sorted(p for p in root.iterdir())
        except OSError:
            continue
        for version in versions:
            if is_reparse_point(version):
                skipped.append(FolderReport(version, "reparse", "skipped", "link; not followed"))
                continue
            if not version.is_dir():
                continue
            try:
                hashes = sorted(p for p in version.iterdir())
            except OSError:
                continue
            for folder in hashes:
                if is_reparse_point(folder):
                    skipped.append(
                        FolderReport(
                            folder, "reparse", "skipped", "junction or symlink; not followed"
                        )
                    )
                    continue
                if not folder.is_dir():
                    continue
                if any((folder / name).exists() for name in (REAL_NAME, STASH_NAME, PAYLOAD_NAME)):
                    folders.append(folder)
    return folders, skipped


def iter_folders(roots: list[Path]) -> list[Path]:
    """The folders :func:`scan_folders` finds, without the skipped reports."""
    return scan_folders(roots)[0]


# ---------------------------------------------------------------------------
# Hashing and payloads
# ---------------------------------------------------------------------------


def sha256_file(path: Path) -> str:
    """Hex sha256 of a file. Raises ``PermissionError`` for a locked file."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True)
class Payload:
    sha256: str
    size: int | None


def read_payload(folder: Path) -> Payload | None:
    """The ``.payload`` record, or ``None`` when it is missing or malformed."""
    try:
        data = json.loads((folder / PAYLOAD_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    sha = str(data.get("sha256", "")).lower()
    if not _SHA256_RE.match(sha):
        return None
    size = data.get("size")
    return Payload(sha256=sha, size=size if isinstance(size, int) else None)


# ---------------------------------------------------------------------------
# Locking, state and config files
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write_text_atomic(path: Path, text: str) -> None:
    """Write through a uniquely named temp file in the same folder, then replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    """Write ``payload`` unless the file already holds exactly that text.

    The watcher calls this on every pass, so a pass with nothing new writes
    nothing (security tools watch for repeated file churn).
    """
    text = json.dumps(payload, indent=2) + "\n"
    try:
        if path.is_file() and path.read_text(encoding="utf-8") == text:
            return
    except OSError:
        pass
    _write_text_atomic(path, text)


def _lock_is_stale(lock: Path) -> bool:
    try:
        return time.time() - lock.stat().st_mtime > STALE_LOCK_SECONDS
    except OSError:
        return False


@contextmanager
def exclusive_lock(data_folder: Path, timeout: float = LOCK_TIMEOUT_SECONDS) -> Iterator[None]:
    """Hold ``codetab.lock`` in the data folder for the duration of a write.

    Waits up to ``timeout`` seconds, then raises :class:`CodetabError`. A lock
    older than ``STALE_LOCK_SECONDS`` is taken over, because its owner is gone.
    """
    data_folder.mkdir(parents=True, exist_ok=True)
    lock = data_folder / LOCK_NAME
    deadline = time.monotonic() + timeout
    while True:
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            if _lock_is_stale(lock):
                try:
                    lock.unlink()
                except FileNotFoundError:
                    pass
                continue
            if time.monotonic() >= deadline:
                raise CodetabError(
                    f"another codetab operation holds {lock}; try again, or delete the "
                    "file if no install is running",
                ) from None
            time.sleep(0.1)
            continue
        os.write(fd, f"{os.getpid()} {_now_iso()}\n".encode("ascii"))
        os.close(fd)
        break
    try:
        yield
    finally:
        try:
            lock.unlink()
        except FileNotFoundError:
            pass


def load_state(data_folder: Path) -> dict[str, Any]:
    """The patcher's record. A malformed file stops the run instead of being
    overwritten, because it is the only record of what was patched."""
    path = data_folder / STATE_NAME
    empty: dict[str, Any] = {"v": STATE_VERSION, "launcher_sha256": [], "folders": {}}
    if not path.exists():
        return empty
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CodetabError(f"{path} is unreadable ({exc}); fix or remove it first") from exc
    if (
        not isinstance(state, dict)
        or state.get("v") != STATE_VERSION
        or not isinstance(state.get("launcher_sha256"), list)
        or not isinstance(state.get("folders"), dict)
    ):
        raise CodetabError(f"{path} has an unsupported shape; refusing to change it")
    return state


def save_state(data_folder: Path, state: dict[str, Any]) -> Path:
    path = data_folder / STATE_NAME
    if not state["folders"] and not state["launcher_sha256"]:
        if path.exists():
            path.unlink()
        return path
    _write_json_atomic(path, state)
    return path


def resolve_base_url(
    health_port: int,
    explicit: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> str:
    """The Code tab's ``base_url``.

    An explicit value must be loopback. Otherwise the user's Claude settings
    ``env.ANTHROPIC_BASE_URL`` is used when it is a loopback URL on the same
    port (it carries the per-machine keep-alive id), and the plain forwarder
    URL is the fallback.
    """
    if explicit:
        if not _BASE_URL_RE.match(explicit):
            raise CodetabError(f"base URL must be http://127.0.0.1:<port>[/path]: {explicit}")
        return explicit
    settings_url = _settings_base_url(environ)
    if settings_url:
        match = _BASE_URL_RE.match(settings_url)
        if match and int(match.group(1)) == health_port:
            return settings_url
    return f"http://127.0.0.1:{health_port}"


def _settings_base_url(environ: Mapping[str, str] | None) -> str | None:
    """Read-only lookup of the user's Claude settings. With an explicit
    ``environ`` it never falls back to the real home folder."""
    env = os.environ if environ is None else environ
    config_dir = (env.get("CLAUDE_CONFIG_DIR") or "").strip()
    if config_dir:
        settings = Path(config_dir).expanduser() / "settings.json"
    else:
        home = (env.get("USERPROFILE") or env.get("HOME") or "").strip()
        if not home and environ is not None:
            return None
        base = Path(home) if home else Path.home()
        settings = base / ".claude" / "settings.json"
    try:
        data = json.loads(settings.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("env"), dict):
        return None
    value = data["env"].get("ANTHROPIC_BASE_URL")
    return value if isinstance(value, str) else None


def build_config(
    *,
    base_url: str,
    health_port: int,
    tool_search: bool,
    enabled: bool = True,
    lean_window: int | None = None,
) -> dict[str, Any]:
    """The ``codetab.json`` the launcher reads. ``env`` holds only
    ``ENABLE_TOOL_SEARCH`` (``"true"`` or ``"false"``) and, when lean is on,
    ``CLAUDE_CODE_AUTO_COMPACT_WINDOW`` (a digits-only string, see the launcher)."""
    env: dict[str, str] = {}
    if tool_search:
        env["ENABLE_TOOL_SEARCH"] = "true"
    if lean_window is not None:
        env[LEAN_WINDOW_KEY] = str(lean_window)
    return {
        "v": CONFIG_VERSION,
        "enabled": enabled,
        "base_url": base_url,
        "env": env,
        "health_port": health_port,
    }


def _lean_from_env(env: Mapping[str, Any]) -> int | None:
    """The compaction window in ``env`` when it is valid, else ``None``.

    Mirrors the launcher's rule: ASCII digits, no leading zero, 100000..1000000.
    """
    text = env.get(LEAN_WINDOW_KEY)
    if not isinstance(text, str) or not re.fullmatch(r"[1-9][0-9]*", text):
        return None
    value = int(text)
    return value if LEAN_WINDOW_MIN <= value <= LEAN_WINDOW_MAX else None


def _read_config(path: Path) -> dict[str, Any]:
    """The saved ``codetab.json`` as a dict. Raises when it cannot be read or is not an object."""
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CodetabError(f"{path} is unreadable ({exc}); fix or remove it first") from exc
    if not isinstance(config, dict):
        raise CodetabError(f"{path} has an unsupported shape; refusing to change it")
    return config


def code_tab_lean(
    *, data: Path | None = None, environ: Mapping[str, str] | None = None
) -> dict[str, Any]:
    """Keep-sessions-lean state of the Code tab: ``state`` and ``window``.

    ``state`` is ``on`` (installed, enabled, valid window), ``off`` (installed and
    enabled, no window) or ``not-installed`` (no config, or it is disabled).
    """
    data_folder = data if data is not None else claude_code_dir(environ)
    config_path = data_folder / CONFIG_NAME
    if not config_path.is_file():
        return {"state": "not-installed", "window": None}
    try:
        config = _read_config(config_path)
    except CodetabError:
        return {"state": "not-installed", "window": None}
    env = config.get("env") if isinstance(config.get("env"), dict) else {}
    if config.get("enabled") is not True:
        return {"state": "not-installed", "window": None}
    window = _lean_from_env(env)
    return {"state": "on" if window is not None else "off", "window": window}


def set_code_tab_lean(
    window: int | None,
    *,
    data: Path | None = None,
    environ: Mapping[str, str] | None = None,
    lock_timeout: float = LOCK_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Set the Code tab's compaction window in ``codetab.json``, or clear it with ``None``.

    Only an installed, enabled config is changed; otherwise the result is
    ``not-installed`` and nothing is written. The rest of the config is kept.
    """
    if window is not None and not (LEAN_WINDOW_MIN <= window <= LEAN_WINDOW_MAX):
        raise CodetabError(f"compaction window out of range: {window}")
    data_folder = data if data is not None else claude_code_dir(environ)
    config_path = data_folder / CONFIG_NAME
    if not config_path.is_file():
        return {"state": "not-installed", "window": None}
    with exclusive_lock(data_folder, lock_timeout):
        config = _read_config(config_path)
        if config.get("enabled") is not True:
            return {"state": "not-installed", "window": None}
        env = config.get("env")
        if env is None:
            env = {}
        if not isinstance(env, dict):
            raise CodetabError(f"{config_path} has a non-object 'env'; refusing to change it")
        if window is None:
            env.pop(LEAN_WINDOW_KEY, None)
        else:
            env[LEAN_WINDOW_KEY] = str(window)
        config["env"] = env
        _write_config(config_path, config)
    return {"state": "off" if window is None else "on", "window": window}


# ---------------------------------------------------------------------------
# Per-folder operations
# ---------------------------------------------------------------------------


@dataclass
class FolderReport:
    folder: Path
    status: str
    action: str
    detail: str = ""

    def line(self) -> str:
        suffix = f" - {self.detail}" if self.detail else ""
        return f"{self.action:<10} {self.status:<10} {self.folder}{suffix}"


def _current_sha(path: Path) -> str | None:
    """Hex sha256 of ``path``, or ``None`` when it is absent or a link.
    Raises ``PermissionError`` when the file is in use."""
    if not path.is_file() or is_reparse_point(path):
        return None
    return sha256_file(path)


def _replace_checked(source: Path, target: Path, expect: str | None) -> None:
    """Rename ``source`` over ``target`` only if ``target`` still holds ``expect``
    (``None`` means absent). Never overwrites anything else."""
    if _current_sha(target) != expect:
        raise CodetabError(f"{target} changed while it was being patched; not overwriting it")
    os.replace(source, target)


def _place_launcher(folder: Path, launcher: Path, launcher_sha: str, expect: str | None) -> None:
    """Install the launcher as ``claude.exe``.

    ``expect`` is what ``claude.exe`` must still hold: ``None`` when it was moved
    aside, or the recorded launcher hash when an older launcher is replaced.
    The copy goes to a unique temp name, which is verified before the rename.
    """
    fd, tmp_name = tempfile.mkstemp(dir=folder, prefix=".cs-launcher-", suffix=".tmp")
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        shutil.copyfile(launcher, tmp)
        if sha256_file(tmp) != launcher_sha:
            raise CodetabError(f"copied launcher does not match the source for {folder}")
        _replace_checked(tmp, folder / REAL_NAME, expect)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def _restore_real(stash: Path, real: Path, payload_sha: str) -> None:
    """Roll back: put the verified real binary back at ``claude.exe``."""
    if real.exists():
        return
    if not stash.is_file() or sha256_file(stash) != payload_sha:
        raise CodetabError(
            f"could not roll back: claude.exe is missing and {stash.name} is not verified; "
            f"rename {stash} to {real} by hand"
        )
    os.replace(stash, real)


def _classify(folder: Path, known_launchers: set[str]) -> tuple[str, str]:
    payload = read_payload(folder)
    if payload is None:
        return "no-payload", ".payload is missing or malformed"
    real = folder / REAL_NAME
    stash = folder / STASH_NAME
    try:
        real_sha = _current_sha(real)
        stash_sha = _current_sha(stash)
    except PermissionError:
        return "locked", "a file is in use; will retry"
    stash_ok = stash_sha == payload.sha256

    if real_sha is None:
        if stash_ok:
            return "missing", "claude.exe missing; the verified real binary is claude.cs-real.exe"
        return "missing", "claude.exe is missing"
    if real_sha == payload.sha256:
        if stash.exists():
            return "conflict", "a second real copy exists as claude.cs-real.exe"
        return "real", "unpatched"
    if real_sha in known_launchers:
        if stash_ok:
            return "patched", "routed through the launcher"
        return "conflict", "launcher present but claude.cs-real.exe does not match .payload"
    return "foreign", "claude.exe is another program's trampoline or file"


def _install_folder(
    folder: Path,
    *,
    launcher: Path,
    launcher_sha: str,
    known_launchers: set[str],
    state: dict[str, Any],
    dry_run: bool,
) -> FolderReport:
    """Patch one folder. Any failure is reported for this folder only."""
    try:
        return _install_folder_checked(
            folder,
            launcher=launcher,
            launcher_sha=launcher_sha,
            known_launchers=known_launchers,
            state=state,
            dry_run=dry_run,
        )
    except PermissionError as exc:
        return FolderReport(folder, "locked", "skipped", f"a file is in use; will retry ({exc})")
    except (CodetabError, OSError) as exc:
        return FolderReport(folder, "error", "skipped", str(exc))


def _install_folder_checked(
    folder: Path,
    *,
    launcher: Path,
    launcher_sha: str,
    known_launchers: set[str],
    state: dict[str, Any],
    dry_run: bool,
) -> FolderReport:
    payload = read_payload(folder)
    if payload is None:
        return FolderReport(folder, "no-payload", "skipped", ".payload is missing or malformed")

    real = folder / REAL_NAME
    stash = folder / STASH_NAME
    key = str(folder)
    if is_reparse_point(real) or is_reparse_point(stash):
        return FolderReport(
            folder, "reparse", "skipped", "claude.exe or its copy is a link; not followed"
        )

    real_sha = _current_sha(real)
    stash_sha = _current_sha(stash)
    stash_exists = stash.exists()
    stash_ok = stash_sha == payload.sha256
    record: dict[str, Any] = {
        "real_sha256": payload.sha256,
        "launcher_sha256": launcher_sha,
        "patched_at": _now_iso(),
    }

    # Case 1: the real binary is in place. Move it aside, then put the launcher there.
    if real_sha == payload.sha256:
        if stash_exists:
            return FolderReport(
                folder,
                "conflict",
                "skipped",
                "claude.cs-real.exe already exists; left both files alone",
            )
        if dry_run:
            return FolderReport(folder, "real", "would-patch")
        os.replace(real, stash)
        try:
            _place_launcher(folder, launcher, launcher_sha, expect=None)
        except BaseException:
            _restore_real(stash, real, payload.sha256)
            raise
        state["folders"][key] = record
        return FolderReport(folder, "real", "patched")

    # Case 2: claude.exe is missing but the verified real binary is stashed.
    if real_sha is None:
        if not stash_ok:
            return FolderReport(folder, "missing", "skipped", "no verified real binary")
        if dry_run:
            return FolderReport(folder, "missing", "would-patch")
        _place_launcher(folder, launcher, launcher_sha, expect=None)
        state["folders"][key] = record
        return FolderReport(folder, "patched", "patched", "restored the launcher")

    # Case 3: our launcher is in place and the real binary is stashed.
    if real_sha in known_launchers and stash_ok:
        if real_sha == launcher_sha:
            state["folders"].setdefault(key, record)
            return FolderReport(folder, "patched", "up-to-date")
        if dry_run:
            return FolderReport(folder, "patched", "would-update")
        # A locked launcher raises PermissionError here, and the old launcher keeps running.
        _place_launcher(folder, launcher, launcher_sha, expect=real_sha)
        previous = state["folders"].get(key, {})
        record["displaced"] = previous.get("displaced", [])
        state["folders"][key] = record
        return FolderReport(folder, "patched", "updated", "replaced an older launcher")

    if real_sha in known_launchers:
        return FolderReport(
            folder,
            "conflict",
            "skipped",
            "launcher present but claude.cs-real.exe does not match .payload",
        )

    # Case 4: a foreign file sits in claude.exe. Only accepted when the real binary
    # is stashed and verified. The foreign file is renamed aside, never deleted.
    if not stash_ok:
        return FolderReport(
            folder,
            "foreign",
            "skipped",
            "claude.exe is not the real binary and claude.cs-real.exe is not verified",
        )
    if dry_run:
        return FolderReport(folder, "foreign", "would-displace")
    displaced = _next_displaced_name(folder)
    os.replace(real, folder / displaced)
    try:
        _place_launcher(folder, launcher, launcher_sha, expect=None)
    except BaseException:
        # Rename the verified real binary back, so claude.exe runs again at once.
        _restore_real(stash, real, payload.sha256)
        raise
    record["displaced"] = [displaced]
    state["folders"][key] = record
    return FolderReport(
        folder, "patched", "patched", f"moved the foreign claude.exe to {displaced}"
    )


def _next_displaced_name(folder: Path) -> str:
    index = 1
    while (folder / f"{DISPLACED_STEM}-{index}").exists():
        index += 1
    return f"{DISPLACED_STEM}-{index}"


def _remove_folder(
    folder: Path,
    *,
    state: dict[str, Any],
    dry_run: bool,
) -> FolderReport:
    try:
        return _remove_folder_checked(folder, state=state, dry_run=dry_run)
    except PermissionError as exc:
        return FolderReport(folder, "locked", "skipped", f"a file is in use; will retry ({exc})")
    except (CodetabError, OSError) as exc:
        return FolderReport(folder, "error", "skipped", str(exc))


def _remove_folder_checked(folder: Path, *, state: dict[str, Any], dry_run: bool) -> FolderReport:
    payload = read_payload(folder)
    if payload is None:
        return FolderReport(folder, "no-payload", "skipped", ".payload is missing or malformed")
    real = folder / REAL_NAME
    stash = folder / STASH_NAME
    key = str(folder)
    if is_reparse_point(real) or is_reparse_point(stash):
        return FolderReport(
            folder, "reparse", "skipped", "claude.exe or its copy is a link; not followed"
        )

    real_sha = _current_sha(real)
    if not stash.is_file():
        if real_sha == payload.sha256:
            state["folders"].pop(key, None)
            return FolderReport(folder, "real", "up-to-date")
        return FolderReport(folder, "missing", "skipped", "claude.cs-real.exe is missing")
    stash_sha = _current_sha(stash)

    if stash_sha != payload.sha256:
        return FolderReport(
            folder,
            "conflict",
            "skipped",
            "claude.cs-real.exe does not match .payload; not restoring",
        )
    if real_sha == payload.sha256:
        return FolderReport(
            folder,
            "conflict",
            "skipped",
            "claude.exe is already the real binary; claude.cs-real.exe left in place",
        )
    # claude.exe counts as ours only when it matches the launcher hash this
    # install recorded for this folder, and that hash is in the state file.
    record = state["folders"].get(key, {})
    recorded = record.get("launcher_sha256")
    ours = real_sha is not None and real_sha == recorded and real_sha in state["launcher_sha256"]
    if real_sha is not None and not ours:
        return FolderReport(
            folder,
            "foreign",
            "skipped",
            "claude.exe is not a launcher this install placed; left as is",
        )
    if dry_run:
        return FolderReport(folder, "patched", "would-restore")
    # Replaces only the launcher copy this module placed, after re-checking its hash.
    _replace_checked(stash, real, expect=real_sha)
    if _current_sha(real) != payload.sha256:
        raise CodetabError(f"restored {real} does not match .payload")
    state["folders"].pop(key, None)
    return FolderReport(folder, "real", "restored")


@contextmanager
def _nullcontext() -> Iterator[None]:
    yield


# ---------------------------------------------------------------------------
# Public operations
# ---------------------------------------------------------------------------


@dataclass
class InstallResult:
    roots: list[Path]
    launcher: Path
    config_path: Path
    state_path: Path | None
    folders: list[FolderReport] = field(default_factory=list)
    config_written: bool = False


@dataclass
class RemoveResult:
    roots: list[Path]
    folders: list[FolderReport] = field(default_factory=list)
    config_removed: bool = False
    config_disabled: bool = False


@dataclass
class StatusResult:
    roots: list[Path]
    folders: list[FolderReport]
    launcher: Path
    launcher_present: bool
    config: dict[str, Any] | None
    config_path: Path
    state_folders: int


def _write_config(path: Path, config: dict[str, Any]) -> None:
    _write_json_atomic(path, config)


def _saved_lean_window(config_path: Path) -> int | None:
    """The valid lean window already in ``codetab.json``, or ``None``."""
    if not config_path.is_file():
        return None
    try:
        config = _read_config(config_path)
    except CodetabError:
        return None
    env = config.get("env")
    return _lean_from_env(env) if isinstance(env, dict) else None


def install(
    *,
    roots: list[Path] | None = None,
    data: Path | None = None,
    launcher: Path | None = None,
    base_url: str | None = None,
    health_port: int = DEFAULT_HEALTH_PORT,
    tool_search: bool = False,
    dry_run: bool = False,
    environ: Mapping[str, str] | None = None,
    lock_timeout: float = LOCK_TIMEOUT_SECONDS,
) -> InstallResult:
    """Patch every eligible folder and write ``codetab.json``.

    Nothing is changed when the launcher is missing or the state file is
    unreadable. A folder whose step fails (for example a locked ``claude.exe``)
    is reported and the rest still run.
    """
    if not (1 <= health_port <= 65535):
        raise CodetabError(f"health port out of range: {health_port}")
    roots = discover_roots(environ) if roots is None else list(roots)
    data_folder = data if data is not None else claude_code_dir(environ)
    launcher_path = launcher if launcher is not None else default_launcher_path(environ)
    if not launcher_path.is_file():
        raise CodetabError(
            f"launcher not found: {launcher_path}. Build desktop/claude-code-tools "
            "and copy claude-code-launcher.exe there, or pass --launcher."
        )
    launcher_sha = sha256_file(launcher_path)
    resolved_url = resolve_base_url(health_port, base_url, environ)

    result = InstallResult(
        roots=roots,
        launcher=launcher_path,
        config_path=data_folder / CONFIG_NAME,
        state_path=data_folder / STATE_NAME,
    )
    lock = _nullcontext() if dry_run else exclusive_lock(data_folder, lock_timeout)
    with lock:
        state = load_state(data_folder)
        known = set(state["launcher_sha256"]) | {launcher_sha}
        folders, skipped = scan_folders(roots)
        result.folders.extend(skipped)
        for folder in folders:
            report = _install_folder(
                folder,
                launcher=launcher_path,
                launcher_sha=launcher_sha,
                known_launchers=known,
                state=state,
                dry_run=dry_run,
            )
            # Recorded only after a launcher copy has actually been placed.
            if report.action in ("patched", "updated"):
                if launcher_sha not in state["launcher_sha256"]:
                    state["launcher_sha256"].append(launcher_sha)
            result.folders.append(report)
        if not dry_run:
            # A valid keep-sessions-lean window survives a re-install or a --watch pass.
            _write_config(
                result.config_path,
                build_config(
                    base_url=resolved_url,
                    health_port=health_port,
                    tool_search=tool_search,
                    lean_window=_saved_lean_window(result.config_path),
                ),
            )
            result.config_written = True
            result.state_path = save_state(data_folder, state)
    return result


def remove(
    *,
    roots: list[Path] | None = None,
    data: Path | None = None,
    dry_run: bool = False,
    environ: Mapping[str, str] | None = None,
    lock_timeout: float = LOCK_TIMEOUT_SECONDS,
) -> RemoveResult:
    """Restore the real ``claude.exe`` everywhere and delete the patcher's files.

    Only folders inside the discovered roots, reached without crossing a link,
    are touched. ``codetab.json`` is kept, disabled, while any folder is still
    recorded as patched.
    """
    roots = discover_roots(environ) if roots is None else list(roots)
    data_folder = data if data is not None else claude_code_dir(environ)
    result = RemoveResult(roots=roots)
    lock = _nullcontext() if dry_run else exclusive_lock(data_folder, lock_timeout)
    with lock:
        state = load_state(data_folder)
        folders, skipped = scan_folders(roots)
        result.folders.extend(skipped)
        seen = {str(folder) for folder in folders}
        for key in list(state["folders"]):
            if key in seen:
                continue
            candidate = Path(key)
            if not _inside_roots_without_links(candidate, roots):
                result.folders.append(
                    FolderReport(
                        candidate,
                        "outside-roots",
                        "skipped",
                        "state entry is outside the roots; not touched",
                    )
                )
                continue
            folders.append(candidate)
            seen.add(key)

        for folder in folders:
            result.folders.append(_remove_folder(folder, state=state, dry_run=dry_run))

        if not dry_run:
            if not state["folders"]:
                state["launcher_sha256"] = []
            config_path = data_folder / CONFIG_NAME
            if state["folders"]:
                # Something is still patched or locked: keep the config but disable it.
                if config_path.exists():
                    config = json.loads(config_path.read_text(encoding="utf-8"))
                    if isinstance(config, dict) and config.get("enabled") is not False:
                        config["enabled"] = False
                        _write_config(config_path, config)
                        result.config_disabled = True
            elif config_path.exists():
                config_path.unlink()
                result.config_removed = True
            save_state(data_folder, state)
    return result


def status(
    *,
    roots: list[Path] | None = None,
    data: Path | None = None,
    launcher: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> StatusResult:
    roots = discover_roots(environ) if roots is None else list(roots)
    data_folder = data if data is not None else claude_code_dir(environ)
    launcher_path = launcher if launcher is not None else default_launcher_path(environ)
    state = load_state(data_folder)
    known = set(state["launcher_sha256"])
    if launcher_path.is_file():
        known.add(sha256_file(launcher_path))
    folders: list[FolderReport] = []
    scanned, skipped = scan_folders(roots)
    folders.extend(skipped)
    for folder in scanned:
        folder_status, detail = _classify(folder, known)
        folders.append(FolderReport(folder, folder_status, "-", detail))
    config_path = data_folder / CONFIG_NAME
    config: dict[str, Any] | None = None
    if config_path.exists():
        try:
            loaded = json.loads(config_path.read_text(encoding="utf-8"))
            config = loaded if isinstance(loaded, dict) else None
        except (OSError, ValueError):
            config = None
    return StatusResult(
        roots=roots,
        folders=folders,
        launcher=launcher_path,
        launcher_present=launcher_path.is_file(),
        config=config,
        config_path=config_path,
        state_folders=len(state["folders"]),
    )


Fingerprint = tuple[tuple[str, int | None, int | None], ...]


def pass_fingerprint(
    *,
    roots: list[Path] | None = None,
    data: Path | None = None,
    launcher: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> Fingerprint:
    """A cheap snapshot of everything an install pass reads: the Claude Code
    folders and, in each, the size and modification time of ``claude.exe``, the
    stashed real binary and ``.payload``, plus the launcher and our own files.

    Only directory listings and ``stat`` calls: no file is opened or hashed.
    """
    roots = discover_roots(environ) if roots is None else list(roots)
    data_folder = data if data is not None else claude_code_dir(environ)
    launcher_path = launcher if launcher is not None else default_launcher_path(environ)
    folders, skipped = scan_folders(roots)
    paths = [folder / name for folder in folders for name in (REAL_NAME, STASH_NAME, PAYLOAD_NAME)]
    paths += [launcher_path, data_folder / STATE_NAME, data_folder / CONFIG_NAME]
    entries: list[tuple[str, int | None, int | None]] = []
    for path in paths:
        try:
            info = path.stat()
        except OSError:
            entries.append((str(path), None, None))
        else:
            entries.append((str(path), info.st_size, info.st_mtime_ns))
    entries += [(f"skipped:{report.folder}", None, None) for report in skipped]
    return tuple(entries)


class QuietInstaller:
    """``install`` for the watcher: a pass runs only when something it reads changed.

    Without this the watcher hashed every ``claude.exe`` (hundreds of MB each)
    and took the lock every few seconds. Now an idle pass is a directory listing
    and a few ``stat`` calls. A pass that hit a locked or failing folder is
    retried on the next tick, and everything is re-verified at least every
    ``full_pass_seconds``. Returns ``None`` for a skipped pass.
    """

    def __init__(
        self,
        *,
        full_pass_seconds: float = WATCH_FULL_PASS_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        **install_kwargs: Any,
    ) -> None:
        self._kwargs = install_kwargs
        self._full_pass_seconds = full_pass_seconds
        self._clock = clock
        self._last: Fingerprint | None = None
        self._last_full = 0.0

    def _fingerprint(self) -> Fingerprint:
        return pass_fingerprint(
            roots=self._kwargs.get("roots"),
            data=self._kwargs.get("data"),
            launcher=self._kwargs.get("launcher"),
            environ=self._kwargs.get("environ"),
        )

    def __call__(self) -> InstallResult | None:
        now = self._clock()
        due = self._last is None or now - self._last_full >= self._full_pass_seconds
        if not due and self._fingerprint() == self._last:
            return None
        self._last = None  # stays unset unless this pass completes cleanly
        result = install(**self._kwargs)
        if not any(report.status in ("locked", "error") for report in result.folders):
            # Taken after the pass, so the pass's own changes do not trigger another.
            self._last = self._fingerprint()
            self._last_full = now
        return result


def watch(
    run: Callable[[], InstallResult | None],
    *,
    interval: float = WATCH_INTERVAL_SECONDS,
    iterations: int | None = None,
    sleep: Callable[[float], None] = time.sleep,
    on_result: Callable[[InstallResult], None] | None = None,
    on_error: Callable[[Exception], None] | None = None,
) -> None:
    """Call ``run`` every ``interval`` seconds, so new Desktop versions get patched.

    A pass that raises (for example a lock held by a manual install) is reported
    through ``on_error`` and retried on the next pass; the watcher keeps running.
    ``iterations`` bounds the loop for tests; ``None`` runs until interrupted.
    """
    count = 0
    while iterations is None or count < iterations:
        try:
            result = run()
        except (CodetabError, OSError) as exc:
            if on_error is not None:
                on_error(exc)
        else:
            if on_result is not None and result is not None:
                on_result(result)
        count += 1
        if iterations is None or count < iterations:
            sleep(interval)
