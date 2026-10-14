"""Broker-issued companion capability for the companion-only operations (Phase E3).

The broker generates a random secret, writes it to a file only the current user
can read, and sends it with companion requests. A capability is a secret the
broker issues, not a role a client asserts, so request frames still carry no
role field. The MCP adapter's ``BrokerClient`` never loads or sends it.

The capability file lives under a per-user directory and is named from a
SHA-256 of the user identity and ``broker_id``, so neither is readable from the
name. Windows files get a protected DACL with only the user SID and LocalSystem.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import sys
from pathlib import Path
from typing import Any

from .broker_transport import (
    COMPANION_OPERATIONS,
    BrokerTransportError,
    _windows_modules,
    current_user_sid,
    is_valid_capability,
)
from .settings import DesktopSettings

__all__ = [
    "COMPANION_OPERATIONS",
    "capability_path",
    "issue_capability",
    "load_capability",
    "revoke_capability",
]

_TOKEN_BYTES = 32
_MAX_READ_BYTES = 128
_SYSTEM_ERROR_EXISTS = (80, 183)  # ERROR_FILE_EXISTS, ERROR_ALREADY_EXISTS
_SE_DACL_PROTECTED = 0x1000
_GENERIC_ALL = 0x10000000


def _unavailable() -> Exception:
    # Imported here: broker imports this module, so a top-level import would cycle.
    from .broker import BrokerError

    return BrokerError("companion_capability_unavailable")


def _default_base_dir() -> Path:
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA")
        root = Path(local) if local else Path.home() / "AppData" / "Local"
        return root / "ContextShrink" / "broker"
    return Path.home() / ".local" / "state" / "contextshrink" / "broker"


def capability_path(settings: DesktopSettings, *, base_dir: Path | None = None) -> Path:
    """Path of this broker's capability file. ``base_dir`` exists for tests."""
    base = _default_base_dir() if base_dir is None else Path(base_dir)
    identity = current_user_sid() if sys.platform == "win32" else str(os.getuid())
    digest = hashlib.sha256(f"{identity}\0{settings.broker_id}".encode()).hexdigest()
    return base / f"{digest}.cap"


def _remove_stale(path: Path) -> None:
    try:
        os.remove(path)
    except FileNotFoundError:
        pass


def _windows_attributes(token_sid: str) -> Any:
    pywintypes, _, win32con, _, _, win32security = _windows_modules()
    descriptor = win32security.SECURITY_DESCRIPTOR()
    acl = win32security.ACL()
    user_sid = win32security.ConvertStringSidToSid(token_sid)
    system_sid = win32security.CreateWellKnownSid(win32security.WinLocalSystemSid, None)
    mask = getattr(win32con, "GENERIC_ALL", _GENERIC_ALL)
    acl.AddAccessAllowedAce(win32security.ACL_REVISION, mask, user_sid)
    acl.AddAccessAllowedAce(win32security.ACL_REVISION, mask, system_sid)
    descriptor.SetSecurityDescriptorDacl(1, acl, 0)
    # Protected: no inherited ACEs can widen access to the capability file.
    descriptor.SetSecurityDescriptorControl(_SE_DACL_PROTECTED, _SE_DACL_PROTECTED)
    attributes = pywintypes.SECURITY_ATTRIBUTES()
    attributes.bInheritHandle = False
    attributes.SECURITY_DESCRIPTOR = descriptor
    return attributes


def _create_windows(path: Path, token: str) -> None:
    pywintypes, _, win32con, win32file, _, _ = _windows_modules()
    attributes = _windows_attributes(current_user_sid())

    def create() -> Any:
        return win32file.CreateFile(
            str(path),
            win32con.GENERIC_READ | win32con.GENERIC_WRITE,
            0,
            attributes,
            win32con.CREATE_NEW,
            win32con.FILE_ATTRIBUTE_NORMAL,
            None,
        )

    try:
        handle = create()
    except pywintypes.error as exc:
        # A racing file appeared after the stale-file removal; retry once.
        if exc.winerror not in _SYSTEM_ERROR_EXISTS:
            raise
        _remove_stale(path)
        handle = create()
    try:
        win32file.WriteFile(handle, token.encode("ascii"))
        win32file.FlushFileBuffers(handle)
    finally:
        handle.Close()


def _create_posix(path: Path, token: str) -> None:
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags, 0o600)
    except FileExistsError:
        # Another writer raced in after the stale-file removal; retry once.
        _remove_stale(path)
        fd = os.open(path, flags, 0o600)
    try:
        os.write(fd, token.encode("ascii"))
    finally:
        os.close(fd)


def issue_capability(settings: DesktopSettings, *, base_dir: Path | None = None) -> str:
    """Generate and persist a new capability, replacing any stale file."""
    path = capability_path(settings, base_dir=base_dir)
    token = secrets.token_urlsafe(_TOKEN_BYTES)
    try:
        os.makedirs(path.parent, mode=0o700, exist_ok=True)
        _remove_stale(path)
        if sys.platform == "win32":
            _create_windows(path, token)
        else:
            os.chmod(path.parent, 0o700)
            _create_posix(path, token)
    except Exception:
        # Fail closed. pywintypes.error is not an OSError, so catch broadly and
        # keep the cause out of the sanitized error.
        raise _unavailable() from None
    return token


def load_capability(settings: DesktopSettings, *, base_dir: Path | None = None) -> str:
    """Read the current capability; any missing or malformed file is unavailable."""
    try:
        path = capability_path(settings, base_dir=base_dir)
        with open(path, "rb") as handle:
            data = handle.read(_MAX_READ_BYTES)
        token = data.decode("ascii").strip()
    except (OSError, UnicodeDecodeError, BrokerTransportError):
        raise _unavailable() from None
    if not is_valid_capability(token):
        raise _unavailable()
    return token


def revoke_capability(
    settings: DesktopSettings, token: str, *, base_dir: Path | None = None
) -> None:
    """Delete the capability file only while it still holds ``token``."""
    if not is_valid_capability(token):
        return
    try:
        path = capability_path(settings, base_dir=base_dir)
        with open(path, "rb") as handle:
            current = handle.read(_MAX_READ_BYTES)
    except (OSError, BrokerTransportError):
        return
    if not hmac.compare_digest(current, token.encode("ascii")):
        return
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
