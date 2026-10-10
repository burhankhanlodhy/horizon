"""OS credential-store access for the local forwarder.

The PC-side forwarder relays loopback traffic to a remote Horizon proxy and
attaches the caller's per-user ``hz_...`` key. Storing that key in a
plaintext config file would defeat the point of per-user keys, so it lives
in the OS credential store instead (Windows Credential Manager, macOS
Keychain, Secret Service on Linux) via ``keyring``.

``keyring`` is an optional dependency (``pip install "contextshrink[hosted]"``);
every function raises :class:`VaultError` with an actionable message when it
is missing or the platform backend misbehaves.

Entries: the desktop app's key is ``remote-api-key``; the signed-in CLI keeps
its own (``horizon login``), selected per process by ``HORIZON_VAULT_CREDENTIAL``,
so the two never overwrite each other. ``CONTEXTSHRINK_API_KEY`` supplies the
key directly (CI, containers). A machine with no credential-store service
(a headless Linux server) can keep entries in an owner-only file instead, but
only when the caller asks for it (``allow_file``).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

SERVICE_NAME = "horizon"
CREDENTIAL_NAME = "remote-api-key"
#: Which entry this process's key lives in (default: the desktop app's).
CREDENTIAL_ENV = "HORIZON_VAULT_CREDENTIAL"
#: The key itself, for machines without a credential store (CI, containers).
KEY_ENV = "CONTEXTSHRINK_API_KEY"


class VaultError(RuntimeError):
    """Raised when the credential store is unavailable or the entry is absent."""


def _load_keyring():
    try:
        import keyring
    except ImportError as exc:
        raise VaultError(
            'keyring is not installed - install it with: pip install "contextshrink[hosted]"'
        ) from exc
    backend = keyring.get_keyring()
    if "fail" in type(backend).__module__:
        raise VaultError(
            "no usable OS credential-store backend found (keyring backend: "
            f"{type(backend).__name__})"
        )
    return keyring


def _entry(name: str | None) -> str:
    return name or os.environ.get(CREDENTIAL_ENV) or CREDENTIAL_NAME


# -- owner-only file fallback -------------------------------------------------


def fallback_path() -> Path:
    from horizon import paths

    return paths.config_dir() / "credentials.json"


def _read_file() -> dict[str, str]:
    try:
        data = json.loads(fallback_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}


def _write_file(data: dict[str, str]) -> None:
    path = fallback_path()
    if not data:
        try:
            path.unlink()
        except OSError:
            pass
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


# -- generic secrets ------------------------------------------------------------


def set_secret(name: str, value: str, *, allow_file: bool = False) -> str:
    """Store ``value`` under ``name``; returns ``"keyring"`` or ``"file"``."""
    if not value or not value.strip():
        raise VaultError("credential value must be non-empty")
    try:
        keyring = _load_keyring()
        keyring.set_password(SERVICE_NAME, name, value.strip())
        return "keyring"
    except VaultError:
        if not allow_file:
            raise
    except Exception as exc:  # noqa: BLE001 - a backend that fails at write time
        if not allow_file:
            raise VaultError(f"could not write to the credential store: {exc}") from exc
    data = _read_file()
    data[name] = value.strip()
    _write_file(data)
    return "file"


def get_secret(name: str) -> str | None:
    """The stored value, from the credential store or the fallback file; None if absent."""
    try:
        value = _load_keyring().get_password(SERVICE_NAME, name)
    except Exception:  # noqa: BLE001 - fall through to the file
        value = None
    return value or _read_file().get(name) or None


def clear_secret(name: str) -> None:
    """Delete ``name`` from the credential store and the fallback file."""
    try:
        _load_keyring().delete_password(SERVICE_NAME, name)
    except Exception:  # noqa: BLE001 - deleting a missing entry is fine
        pass
    data = _read_file()
    if data.pop(name, None) is not None:
        _write_file(data)


# -- the forwarder key ------------------------------------------------------------


def set_credential(value: str, *, name: str | None = None, allow_file: bool = False) -> str:
    """Store the remote per-user key; returns ``"keyring"`` or ``"file"``."""
    return set_secret(_entry(name), value, allow_file=allow_file)


def get_credential(name: str | None = None) -> str:
    """Return the per-user key, raising :class:`VaultError` if absent.

    ``CONTEXTSHRINK_API_KEY`` wins, then the credential store, then the
    fallback file.
    """
    env_key = os.environ.get(KEY_ENV, "").strip()
    if env_key:
        return env_key
    entry = _entry(name)
    try:
        value = _load_keyring().get_password(SERVICE_NAME, entry)
    except VaultError as exc:
        value = _read_file().get(entry)
        if not value:
            raise exc
    if not value:
        value = _read_file().get(entry)
    if not value:
        raise VaultError("no Horizon credential stored - set one with: horizon vault set")
    return value


def has_credential(name: str | None = None) -> bool:
    """Return True when a credential is available (without revealing it)."""
    try:
        get_credential(name)
    except VaultError:
        return False
    return True


def clear_credential(name: str | None = None) -> None:
    """Delete the stored credential. Absent entries are tolerated."""
    clear_secret(_entry(name))
