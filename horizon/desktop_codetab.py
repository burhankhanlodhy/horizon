"""Route Claude Desktop's Code tab through the ContextShrink forwarder.

Claude Desktop downloads Claude Code into versioned folders::

    <root>\\<version>\\<hash>\\claude.exe
    <root>\\<version>\\<hash>\\.payload      {"sha256": "...", "size": N}

The patcher moves the real ``claude.exe`` aside to ``claude.cs-real.exe`` and
puts ``claude-code-launcher.exe`` in its place as ``claude.exe``. The launcher
reads ``codetab.json`` and starts the real binary, adding the forwarder's
``ANTHROPIC_BASE_URL`` only when the forwarder answers.

Safety rules, all enforced here:

- A real binary is never deleted. Renames are only done after its sha256 has
  been checked against ``.payload``.
- Only files this module placed are removed, and only after their sha256 has
  matched a launcher this module installed.
- A locked file is skipped and reported; ``watch`` retries it.
- A foreign ``claude.exe`` (another trampoline) is refused unless
  ``claude.cs-real.exe`` matches ``.payload``. Even then the foreign file is
  renamed aside, not deleted.

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
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REAL_NAME = "claude.exe"
STASH_NAME = "claude.cs-real.exe"
PAYLOAD_NAME = ".payload"
TEMP_NAME = "claude.exe.cs-tmp"
DISPLACED_STEM = "claude.exe.cs-displaced"
LAUNCHER_NAME = "claude-code-launcher.exe"
CONFIG_NAME = "codetab.json"
STATE_NAME = "codetab-state.json"
DEFAULT_HEALTH_PORT = 18788
DEFAULT_BASE_URL = f"http://127.0.0.1:{DEFAULT_HEALTH_PORT}"
WATCH_INTERVAL_SECONDS = 10.0
STATE_VERSION = 1
CONFIG_VERSION = 1

ENV_ROOTS = "CONTEXTSHRINK_CODETAB_ROOTS"
ENV_DATA_DIR = "CONTEXTSHRINK_DATA_DIR"
ENV_LAUNCHER = "CONTEXTSHRINK_CODETAB_LAUNCHER"

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_BASE_URL_RE = re.compile(r"^http://127\.0\.0\.1:(\d{1,5})(/[A-Za-z0-9/_.~%-]*)?$")
_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")


class CodetabError(RuntimeError):
    """A problem that stops the whole operation before anything is changed."""


# ---------------------------------------------------------------------------
# Locations
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


def iter_folders(roots: list[Path]) -> list[Path]:
    """Every ``<root>\\<version>\\<hash>`` folder that holds a Claude binary."""
    folders: list[Path] = []
    for root in roots:
        try:
            versions = sorted(p for p in root.iterdir() if p.is_dir())
        except OSError:
            continue
        for version in versions:
            try:
                hashes = sorted(p for p in version.iterdir() if p.is_dir())
            except OSError:
                continue
            for folder in hashes:
                if any((folder / name).exists() for name in (REAL_NAME, STASH_NAME, PAYLOAD_NAME)):
                    folders.append(folder)
    return folders


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
# State and config files
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


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


def _remember_launcher(state: dict[str, Any], sha: str) -> None:
    if sha not in state["launcher_sha256"]:
        state["launcher_sha256"].append(sha)


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
) -> dict[str, Any]:
    env: dict[str, str] = {}
    if tool_search:
        env["ENABLE_TOOL_SEARCH"] = "true"
    return {
        "v": CONFIG_VERSION,
        "enabled": True,
        "base_url": base_url,
        "env": env,
        "health_port": health_port,
    }


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


def _place_launcher(folder: Path, launcher: Path, launcher_sha: str) -> None:
    """Copy the launcher to a temp name, verify it, then move it to claude.exe.

    The only file ever replaced here is ``claude.exe``, and the caller has
    already moved any real or foreign binary out of the way.
    """
    tmp = folder / TEMP_NAME
    shutil.copyfile(launcher, tmp)
    try:
        if sha256_file(tmp) != launcher_sha:
            raise CodetabError(f"copied launcher does not match the source in {folder}")
        os.replace(tmp, folder / REAL_NAME)
    except BaseException:
        if tmp.exists():
            tmp.unlink()
        raise


def _classify(folder: Path, known_launchers: set[str]) -> tuple[str, str]:
    payload = read_payload(folder)
    if payload is None:
        return "no-payload", ".payload is missing or malformed"
    real = folder / REAL_NAME
    stash = folder / STASH_NAME
    try:
        real_sha = sha256_file(real) if real.is_file() else None
        stash_sha = sha256_file(stash) if stash.is_file() else None
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
    payload = read_payload(folder)
    if payload is None:
        return FolderReport(folder, "no-payload", "skipped", ".payload is missing or malformed")

    real = folder / REAL_NAME
    stash = folder / STASH_NAME
    key = str(folder)
    try:
        real_sha = sha256_file(real) if real.is_file() else None
        stash_exists = stash.exists()
        stash_sha = sha256_file(stash) if stash.is_file() else None
    except PermissionError:
        return FolderReport(folder, "locked", "skipped", "a file is in use; will retry")
    stash_ok = stash_sha == payload.sha256
    record: dict[str, Any] = {
        "real_sha256": payload.sha256,
        "launcher_sha256": launcher_sha,
        "patched_at": _now_iso(),
    }

    # Case 1: the real binary is in place. Move it aside, then put the launcher
    # where it was. Refused when a second copy already exists.
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
        try:
            os.replace(real, stash)
        except PermissionError:
            return FolderReport(folder, "locked", "skipped", "claude.exe is in use; will retry")
        try:
            _place_launcher(folder, launcher, launcher_sha)
        except BaseException:
            # Put the real binary back before reporting. Verified first.
            if sha256_file(stash) == payload.sha256 and not real.exists():
                os.replace(stash, real)
            raise
        state["folders"][key] = record
        _remember_launcher(state, launcher_sha)
        return FolderReport(folder, "real", "patched")

    # Case 2: claude.exe is missing but the verified real binary is stashed.
    if real_sha is None:
        if not stash_ok:
            return FolderReport(folder, "missing", "skipped", "no verified real binary")
        if dry_run:
            return FolderReport(folder, "missing", "would-patch")
        _place_launcher(folder, launcher, launcher_sha)
        state["folders"][key] = record
        _remember_launcher(state, launcher_sha)
        return FolderReport(folder, "patched", "patched", "restored the launcher")

    # Case 3: our launcher is in place and the real binary is stashed.
    if real_sha in known_launchers and stash_ok:
        _remember_launcher(state, launcher_sha)
        if real_sha == launcher_sha:
            state["folders"].setdefault(key, record)
            return FolderReport(folder, "patched", "up-to-date")
        if dry_run:
            return FolderReport(folder, "patched", "would-update")
        _place_launcher(folder, launcher, launcher_sha)
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

    # Case 4: a foreign file sits in claude.exe. Only accepted when the real
    # binary is stashed and verified. The foreign file is renamed aside, not
    # deleted.
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
    try:
        os.replace(real, folder / displaced)
    except PermissionError:
        return FolderReport(folder, "locked", "skipped", "claude.exe is in use; will retry")
    _place_launcher(folder, launcher, launcher_sha)
    record["displaced"] = [displaced]
    state["folders"][key] = record
    _remember_launcher(state, launcher_sha)
    return FolderReport(
        folder,
        "patched",
        "patched",
        f"moved the foreign claude.exe to {displaced}",
    )


def _next_displaced_name(folder: Path) -> str:
    index = 1
    while (folder / f"{DISPLACED_STEM}-{index}").exists():
        index += 1
    return f"{DISPLACED_STEM}-{index}"


def _remove_folder(
    folder: Path,
    *,
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
    try:
        real_sha = sha256_file(real) if real.is_file() else None
        if not stash.is_file():
            if real_sha == payload.sha256:
                state["folders"].pop(key, None)
                return FolderReport(folder, "real", "up-to-date")
            return FolderReport(folder, "missing", "skipped", "claude.cs-real.exe is missing")
        stash_sha = sha256_file(stash)
    except PermissionError:
        return FolderReport(folder, "locked", "skipped", "a file is in use; will retry")

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
    if real_sha is not None and real_sha not in known_launchers:
        return FolderReport(
            folder,
            "foreign",
            "skipped",
            "claude.exe is not ContextShrink's launcher; left as is",
        )
    if dry_run:
        return FolderReport(folder, "patched", "would-restore")
    try:
        # Replaces only the launcher copy this module placed. Verified above.
        os.replace(stash, real)
    except PermissionError:
        return FolderReport(folder, "locked", "skipped", "claude.exe is in use; will retry")
    if sha256_file(real) != payload.sha256:
        raise CodetabError(f"restored {real} does not match .payload")
    state["folders"].pop(key, None)
    return FolderReport(folder, "real", "restored")


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


@dataclass
class StatusResult:
    roots: list[Path]
    folders: list[FolderReport]
    launcher: Path
    launcher_present: bool
    config: dict[str, Any] | None
    config_path: Path
    state_folders: int


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
) -> InstallResult:
    """Patch every eligible folder and write ``codetab.json``.

    Nothing is changed when the launcher is missing or the state file is
    unreadable.
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
    state = load_state(data_folder)
    known = set(state["launcher_sha256"]) | {launcher_sha}

    result = InstallResult(
        roots=roots,
        launcher=launcher_path,
        config_path=data_folder / CONFIG_NAME,
        state_path=data_folder / STATE_NAME,
    )
    for folder in iter_folders(roots):
        result.folders.append(
            _install_folder(
                folder,
                launcher=launcher_path,
                launcher_sha=launcher_sha,
                known_launchers=known,
                state=state,
                dry_run=dry_run,
            )
        )
    if not dry_run:
        resolved = resolve_base_url(health_port, base_url, environ)
        _write_json_atomic(
            result.config_path,
            build_config(base_url=resolved, health_port=health_port, tool_search=tool_search),
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
) -> RemoveResult:
    """Restore the real ``claude.exe`` everywhere and delete the patcher's files."""
    roots = discover_roots(environ) if roots is None else list(roots)
    data_folder = data if data is not None else claude_code_dir(environ)
    state = load_state(data_folder)
    known = set(state["launcher_sha256"])

    folders = iter_folders(roots)
    seen = {str(folder) for folder in folders}
    for key in list(state["folders"]):
        if key not in seen and Path(key).is_dir():
            folders.append(Path(key))
            seen.add(key)

    result = RemoveResult(roots=roots)
    for folder in folders:
        result.folders.append(
            _remove_folder(folder, known_launchers=known, state=state, dry_run=dry_run)
        )
    if not state["folders"]:
        # Every patched folder is restored, so no launcher copy remains to recognise.
        state["launcher_sha256"] = []
    if not dry_run:
        config_path = data_folder / CONFIG_NAME
        if config_path.exists():
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
    folders = []
    for folder in iter_folders(roots):
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


def watch(
    run: Callable[[], InstallResult],
    *,
    interval: float = WATCH_INTERVAL_SECONDS,
    iterations: int | None = None,
    sleep: Callable[[float], None] = time.sleep,
    on_result: Callable[[InstallResult], None] | None = None,
) -> None:
    """Call ``run`` every ``interval`` seconds, so new Desktop versions get patched.

    ``iterations`` bounds the loop for tests; ``None`` runs until interrupted.
    Locked folders are retried on the next pass.
    """
    count = 0
    while iterations is None or count < iterations:
        result = run()
        if on_result is not None:
            on_result(result)
        count += 1
        if iterations is None or count < iterations:
            sleep(interval)
