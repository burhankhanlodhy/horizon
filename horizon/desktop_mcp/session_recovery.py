"""Authenticated metadata for bounded recovery of crashed companion sessions.

The module stores no staged source text. Its manifest is bound to the approved
root and generated session directory identities; an HMAC key is protected by
the current Windows user with DPAPI and kept outside every approved source root.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import re
import secrets
import stat
from pathlib import Path
from typing import Any

_KEY_FILE = "protectedsecret.bin"
_KEY_MAGIC = b"CSRK1\x00"
_KEY_BYTES = 32
_MAX_PROTECTED_KEY_BYTES = 4096
_MAX_MANIFEST_BYTES = 64 * 1024
_MAX_ENTRIES = 32
_UUID = re.compile(r"^[0-9a-f]{32}$")
_FILENAME = re.compile(r"^context-([0-9a-f]{32})\.txt$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_MANIFEST_KEYS = {
    "schema_version",
    "session_id",
    "root_identity",
    "directory_identity",
    "lease_identity",
    "generation",
    "entries",
    "hmac_sha256",
}
_ENTRY_KEYS = {"id", "name", "state", "file_id", "size", "sha256", "created_epoch", "expires_epoch"}
STATE_SLOTS = ("recovery-a.json", "recovery-b.json")
LEASE_NAME = "session.lease"


class RecoveryError(ValueError):
    """Sanitized failure while loading recovery state or the HMAC key."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def default_key_directory() -> Path:
    """Return the per-user key directory without creating it."""
    if os.name == "nt":
        local_app_data = os.environ.get("LOCALAPPDATA")
        if not local_app_data:
            raise RecoveryError("recovery_key_location_unavailable")
        return Path(local_app_data) / "ContextShrink" / "recovery-keys"
    return Path.home() / ".local" / "state" / "contextshrink" / "recovery-keys"


def _resolved(path: Path) -> Path:
    try:
        return Path(path).resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        raise RecoveryError("recovery_key_location_unavailable") from None


def _inside(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _check_key_location(key_directory: Path, approved_roots: list[Path]) -> Path:
    key_dir = _resolved(key_directory)
    for root in approved_roots:
        resolved_root = _resolved(Path(root))
        if _inside(key_dir, resolved_root):
            raise RecoveryError("recovery_key_inside_approved_root")
    return key_dir


def _secure_windows_acl(path: Path) -> None:
    """Restrict recovery material to the current owner and LocalSystem."""
    if os.name != "nt":
        return
    import ctypes
    import ctypes.wintypes

    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    descriptor = ctypes.c_void_p()
    convert = advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW
    convert.argtypes = [
        ctypes.wintypes.LPCWSTR,
        ctypes.wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.wintypes.DWORD),
    ]
    convert.restype = ctypes.wintypes.BOOL
    # Owner Rights (OW) and LocalSystem only; protected DACL prevents inherited
    # broad access from a parent profile directory.
    if not convert("D:P(A;OICI;FA;;;OW)(A;OICI;FA;;;SY)", 1, ctypes.byref(descriptor), None):
        raise RecoveryError("recovery_key_permissions_unavailable")
    try:
        set_security = advapi.SetFileSecurityW
        set_security.argtypes = [ctypes.wintypes.LPCWSTR, ctypes.wintypes.DWORD, ctypes.c_void_p]
        set_security.restype = ctypes.wintypes.BOOL
        if not set_security(str(path), 0x00000004, descriptor):
            raise RecoveryError("recovery_key_permissions_unavailable")
    finally:
        kernel.LocalFree.argtypes = [ctypes.c_void_p]
        kernel.LocalFree.restype = ctypes.c_void_p
        kernel.LocalFree(descriptor)


def _dpapi(data: bytes, *, protect: bool) -> bytes:
    """Protect/unprotect bytes with the current user's Windows DPAPI profile."""
    if os.name != "nt":
        raise RecoveryError("recovery_key_protection_unavailable")
    import ctypes
    import ctypes.wintypes

    class _DataBlob(ctypes.Structure):
        _fields_ = [("cbData", ctypes.wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]

    source = ctypes.create_string_buffer(data)
    source_blob = _DataBlob(len(data), ctypes.cast(source, ctypes.POINTER(ctypes.c_ubyte)))
    output_blob = _DataBlob()
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    if protect:
        function = crypt32.CryptProtectData
        function.argtypes = [
            ctypes.POINTER(_DataBlob),
            ctypes.wintypes.LPCWSTR,
            ctypes.POINTER(_DataBlob),
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.wintypes.DWORD,
            ctypes.POINTER(_DataBlob),
        ]
    else:
        function = crypt32.CryptUnprotectData
        function.argtypes = [
            ctypes.POINTER(_DataBlob),
            ctypes.POINTER(ctypes.wintypes.LPWSTR),
            ctypes.POINTER(_DataBlob),
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.wintypes.DWORD,
            ctypes.POINTER(_DataBlob),
        ]
    function.restype = ctypes.wintypes.BOOL
    if not function(
        ctypes.byref(source_blob),
        "ContextShrink session recovery" if protect else None,
        None,
        None,
        None,
        0,
        ctypes.byref(output_blob),
    ):
        raise RecoveryError("recovery_key_protection_unavailable")
    try:
        return ctypes.string_at(output_blob.pbData, output_blob.cbData)
    finally:
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        kernel32.LocalFree.restype = ctypes.c_void_p
        kernel32.LocalFree(output_blob.pbData)


def _read_regular_file(path: Path, *, limit: int) -> bytes:
    """Read a bounded regular single-link file without following symlinks."""
    if os.name == "nt":
        import ctypes
        import ctypes.wintypes
        import msvcrt

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create = kernel32.CreateFileW
        create.argtypes = [
            ctypes.wintypes.LPCWSTR,
            ctypes.wintypes.DWORD,
            ctypes.wintypes.DWORD,
            ctypes.c_void_p,
            ctypes.wintypes.DWORD,
            ctypes.wintypes.DWORD,
            ctypes.wintypes.HANDLE,
        ]
        create.restype = ctypes.wintypes.HANDLE
        # OPEN_EXISTING | OPEN_REPARSE_POINT. The handle is read-only and
        # allows other readers but no writer/deleter while bytes are checked.
        handle = create(str(path), 0x80000000 | 0x00000080, 0x00000001, None, 3, 0x00200000, None)
        if handle == ctypes.wintypes.HANDLE(-1).value:
            raise RecoveryError("recovery_key_unavailable")
        descriptor = None
        try:
            descriptor = msvcrt.open_osfhandle(int(handle), os.O_RDONLY | os.O_BINARY)
            handle = ctypes.wintypes.HANDLE(-1).value
            with os.fdopen(descriptor, "rb") as stream:
                descriptor = None
                info = os.fstat(stream.fileno())
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_nlink != 1
                    or info.st_size > limit
                    or (hasattr(os, "getuid") and info.st_uid != os.getuid())
                    or (hasattr(os, "getuid") and info.st_mode & 0o077)
                ):
                    raise RecoveryError("recovery_key_unavailable")
                data = stream.read(limit + 1)
                if len(data) > limit:
                    raise RecoveryError("recovery_key_unavailable")
                return data
        except RecoveryError:
            raise
        except OSError:
            raise RecoveryError("recovery_key_unavailable") from None
        finally:
            if descriptor is not None:
                os.close(descriptor)
            if handle != ctypes.wintypes.HANDLE(-1).value:
                kernel32.CloseHandle(handle)
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        raise RecoveryError("recovery_key_unavailable") from None
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_size > limit
            or (hasattr(os, "getuid") and info.st_uid != os.getuid())
            or (hasattr(os, "getuid") and info.st_mode & 0o077)
        ):
            raise RecoveryError("recovery_key_unavailable")
        data = os.read(descriptor, limit + 1)
        if len(data) > limit:
            raise RecoveryError("recovery_key_unavailable")
        return data
    except RecoveryError:
        raise
    except OSError:
        raise RecoveryError("recovery_key_unavailable") from None
    finally:
        os.close(descriptor)


def _create_protected_key(path: Path, key: bytes) -> None:
    protected = _KEY_MAGIC + (_dpapi(key, protect=True) if os.name == "nt" else key)
    if len(protected) > _MAX_PROTECTED_KEY_BYTES:
        raise RecoveryError("recovery_key_protection_unavailable")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError:
        raise
    except OSError:
        raise RecoveryError("recovery_key_unavailable") from None
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise RecoveryError("recovery_key_unavailable")
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(protected)
            stream.flush()
            os.fsync(stream.fileno())
        if os.name != "nt":
            os.chmod(path, 0o600)
        else:
            _secure_windows_acl(path)
    except RecoveryError:
        raise
    except OSError:
        raise RecoveryError("recovery_key_unavailable") from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def load_hmac_key(
    approved_roots: list[Path], key_directory: Path | None = None
) -> tuple[bytes, Path]:
    """Load or create the per-user signing key outside all approved roots.

    ``key_directory`` is an injection point for tests; production callers omit
    it and use LocalAppData (Windows) or the user's private state directory.
    """
    key_dir = _check_key_location(key_directory or default_key_directory(), approved_roots)
    try:
        key_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name != "nt":
            os.chmod(key_dir, 0o700)
        else:
            _secure_windows_acl(key_dir)
        info = key_dir.lstat()
        if not stat.S_ISDIR(info.st_mode) or getattr(info, "st_nlink", 1) < 1:
            raise RecoveryError("recovery_key_unavailable")
        if hasattr(os, "getuid") and (info.st_uid != os.getuid() or info.st_mode & 0o077):
            raise RecoveryError("recovery_key_permissions_unavailable")
    except RecoveryError:
        raise
    except (OSError, RuntimeError, ValueError):
        raise RecoveryError("recovery_key_location_unavailable") from None

    key_path = key_dir / _KEY_FILE
    try:
        protected = _read_regular_file(key_path, limit=_MAX_PROTECTED_KEY_BYTES)
    except RecoveryError as error:
        if error.code != "recovery_key_unavailable" or key_path.exists() or key_path.is_symlink():
            raise
        key = secrets.token_bytes(_KEY_BYTES)
        try:
            _create_protected_key(key_path, key)
        except FileExistsError:
            protected = _read_regular_file(key_path, limit=_MAX_PROTECTED_KEY_BYTES)
        else:
            return key, key_dir
    if not protected.startswith(_KEY_MAGIC) or len(protected) <= len(_KEY_MAGIC):
        raise RecoveryError("recovery_key_unavailable")
    key = (
        _dpapi(protected[len(_KEY_MAGIC) :], protect=False)
        if os.name == "nt"
        else protected[len(_KEY_MAGIC) :]
    )
    if len(key) != _KEY_BYTES:
        raise RecoveryError("recovery_key_unavailable")
    return key, key_dir


def _canonical(value: dict[str, Any]) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode(
        "ascii"
    )


def sign_state(
    key: bytes,
    *,
    session_id: str,
    root_identity: tuple[int, int],
    directory_identity: tuple[int, int],
    lease_identity: tuple[int, int],
    generation: int,
    entries: list[dict[str, Any]],
) -> bytes:
    """Serialize one bounded, authenticated ownership generation."""
    if len(key) != _KEY_BYTES or not _UUID.fullmatch(session_id):
        raise RecoveryError("recovery_state_invalid")
    if not 0 <= generation <= 2**63 - 1 or len(entries) > _MAX_ENTRIES:
        raise RecoveryError("recovery_state_invalid")
    body: dict[str, Any] = {
        "schema_version": 1,
        "session_id": session_id,
        "root_identity": list(root_identity),
        "directory_identity": list(directory_identity),
        "lease_identity": list(lease_identity),
        "generation": generation,
        "entries": [_validate_entry(item) for item in entries],
    }
    if len({entry["id"] for entry in body["entries"]}) != len(body["entries"]):
        raise RecoveryError("recovery_state_invalid")
    mac = hmac.new(key, _canonical(body), hashlib.sha256).hexdigest()
    body["hmac_sha256"] = mac
    data = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode(
        "ascii"
    )
    if len(data) > _MAX_MANIFEST_BYTES:
        raise RecoveryError("recovery_state_invalid")
    return data


def verify_state(
    data: bytes,
    key: bytes,
    *,
    session_id: str,
    root_identity: tuple[int, int],
    directory_identity: tuple[int, int],
    lease_identity: tuple[int, int],
) -> dict[str, Any]:
    """Parse and authenticate a fixed-schema recovery generation."""
    if len(data) > _MAX_MANIFEST_BYTES or len(key) != _KEY_BYTES:
        raise RecoveryError("recovery_state_invalid")
    try:
        value = json.loads(data, object_pairs_hook=_unique_object)
    except RecoveryError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError):
        raise RecoveryError("recovery_state_invalid") from None
    if not isinstance(value, dict) or set(value) != _MANIFEST_KEYS:
        raise RecoveryError("recovery_state_invalid")
    mac = value.pop("hmac_sha256")
    if not isinstance(mac, str) or not _DIGEST.fullmatch(mac):
        raise RecoveryError("recovery_state_invalid")
    expected_mac = hmac.new(key, _canonical(value), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(mac, expected_mac):
        raise RecoveryError("recovery_state_invalid")
    if (
        value.get("schema_version") != 1
        or value.get("session_id") != session_id
        or value.get("root_identity") != list(root_identity)
        or value.get("directory_identity") != list(directory_identity)
        or value.get("lease_identity") != list(lease_identity)
        or type(value.get("generation")) is not int
        or value["generation"] < 0
        or value["generation"] > 2**63 - 1
        or not isinstance(value.get("entries"), list)
        or len(value["entries"]) > _MAX_ENTRIES
    ):
        raise RecoveryError("recovery_state_invalid")
    value["entries"] = [_validate_entry(item) for item in value["entries"]]
    ids = [entry["id"] for entry in value["entries"]]
    if len(set(ids)) != len(ids):
        raise RecoveryError("recovery_state_invalid")
    value["hmac_sha256"] = mac
    return value


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise RecoveryError("recovery_state_invalid")
        result[key] = value
    return result


def _validate_entry(entry: Any) -> dict[str, Any]:
    if not isinstance(entry, dict) or set(entry) != _ENTRY_KEYS:
        raise RecoveryError("recovery_state_invalid")
    entry_id = entry.get("id")
    name = entry.get("name")
    state = entry.get("state")
    file_id = entry.get("file_id")
    size = entry.get("size")
    digest = entry.get("sha256")
    expires = entry.get("expires_epoch")
    created = entry.get("created_epoch")
    match = _FILENAME.fullmatch(name) if isinstance(name, str) else None
    if (
        not isinstance(entry_id, str)
        or not _UUID.fullmatch(entry_id)
        or not match
        or match.group(1) != entry_id
        or not isinstance(state, str)
        or state not in {"writing", "committed"}
        or not isinstance(file_id, list)
        or len(file_id) != 2
        or any(type(part) is not int or part < 0 for part in file_id)
        or type(size) is not int
        or not 0 < size <= 8 * 1024 * 1024
        or not isinstance(digest, str)
        or not _DIGEST.fullmatch(digest)
        or type(expires) not in {int, float}
        or type(created) not in {int, float}
        or not math.isfinite(float(expires))
        or not math.isfinite(float(created))
        or not 0 <= created <= expires <= 2**53
        or expires - created > 3600
    ):
        raise RecoveryError("recovery_state_invalid")
    return {
        "id": entry_id,
        "name": name,
        "state": state,
        "file_id": file_id,
        "size": size,
        "sha256": digest,
        "created_epoch": float(created),
        "expires_epoch": float(expires),
    }
