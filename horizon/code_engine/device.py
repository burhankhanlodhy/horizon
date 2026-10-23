"""Device identity for the ContextShrink Code engine.

The machine id never leaves the computer. Only ``device_hash``, a salted
SHA-256 of it, is sent to the API. npx (Node) computes the same value, so the
two implementations must stay byte-identical; ``tests/test_code_engine_device.py``
pins the shared test vector.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from horizon.bundled_client import is_bundled_client
from horizon.code_engine.store import read_json

MACHINE_ID_ENV = "CONTEXTSHRINK_MACHINE_ID"
DEVICE_FILE = "device.json"
_HASH_SALT = b"contextshrink-device-v1\n"
_HEX64 = re.compile(r"[0-9a-f]{64}")
_MACOS_UUID = re.compile(r'"IOPlatformUUID"\s*=\s*"([^"]+)"')
_LINUX_IDS = ("/etc/machine-id", "/var/lib/dbus/machine-id")


class MachineIdError(RuntimeError):
    """No machine id could be read on this computer."""


def machine_id() -> str:
    """The raw machine id: the test override, else the OS identifier.

    The override is for tests only. Shipped engines ignore it, so a user's
    environment cannot change the device identity the server registered.
    """
    override = os.environ.get(MACHINE_ID_ENV, "")
    if override.strip() and not is_bundled_client():
        return override
    if sys.platform == "win32":
        value = _windows_machine_guid()
    elif sys.platform == "darwin":
        value = _macos_platform_uuid()
    else:
        value = _linux_machine_id()
    if not value or not value.strip():
        raise MachineIdError("this computer has no readable machine id")
    return value


def _windows_machine_guid() -> str:
    import winreg

    # The 64-bit view: a 32-bit process would otherwise read the WOW6432Node copy.
    with winreg.OpenKey(
        winreg.HKEY_LOCAL_MACHINE,
        r"SOFTWARE\Microsoft\Cryptography",
        0,
        winreg.KEY_READ | winreg.KEY_WOW64_64KEY,
    ) as key:
        value, _kind = winreg.QueryValueEx(key, "MachineGuid")
    return str(value)


def _macos_platform_uuid() -> str:
    result = subprocess.run(
        ["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    match = _MACOS_UUID.search(result.stdout)
    return match.group(1) if match else ""


def _linux_machine_id() -> str:
    for candidate in _LINUX_IDS:
        try:
            value = Path(candidate).read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if value:
            return value
    return ""


def device_hash(raw_machine_id: str) -> str:
    """Lowercase hex SHA-256 of the salted, normalized machine id."""
    normalized = raw_machine_id.strip().lower()
    return hashlib.sha256(_HASH_SALT + normalized.encode("utf-8")).hexdigest()


def local_device_hash() -> str:
    """``device_hash`` of this computer's machine id."""
    return device_hash(machine_id())


@dataclass(frozen=True)
class Device:
    """What ``device.json`` records about this computer's registration."""

    device_id: str
    device_key: str = field(repr=False)
    device_hash: str
    api_url: str
    tools: tuple[str, ...] = ()


def load_device(home: Path) -> Device | None:
    """The registration in ``device.json``, or ``None`` when absent or malformed."""
    data = read_json(home / DEVICE_FILE)
    if data is None or data.get("version") != 1:
        return None
    fields = ("device_id", "device_key", "device_hash", "api_url")
    if not all(isinstance(data.get(name), str) and data[name] for name in fields):
        return None
    if not data["device_key"].startswith("cs_dev_") or not _HEX64.fullmatch(data["device_hash"]):
        return None
    if not data["api_url"].startswith(("https://", "http://")):
        return None
    tools = data.get("tools")
    return Device(
        device_id=data["device_id"],
        device_key=data["device_key"],
        device_hash=data["device_hash"],
        api_url=data["api_url"].rstrip("/"),
        tools=tuple(t for t in tools if isinstance(t, str)) if isinstance(tools, list) else (),
    )
