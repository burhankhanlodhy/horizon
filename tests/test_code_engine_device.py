"""Device identity for the ContextShrink Code engine (spec section 1)."""

from __future__ import annotations

import json
import sys
from types import SimpleNamespace

import pytest

from horizon import bundled_client
from horizon.bundled_client import CLIENT_EXE_ENV
from horizon.code_engine import device as device_mod
from horizon.code_engine.device import (
    MACHINE_ID_ENV,
    Device,
    MachineIdError,
    device_hash,
    load_device,
    machine_id,
)

# Shared with npx: both implementations must produce this exact value.
TEST_VECTOR_MACHINE_ID = "  ABC-123  "
TEST_VECTOR_HASH = "fb7d953bd09b6360c7f94e789471ddf1ce6973baefe7b6da392179260b08c323"


def test_device_hash_matches_shared_test_vector() -> None:
    assert device_hash(TEST_VECTOR_MACHINE_ID) == TEST_VECTOR_HASH


def test_device_hash_normalizes_case_and_whitespace() -> None:
    assert device_hash("abc-123") == TEST_VECTOR_HASH
    assert device_hash("\tAbC-123\n") == TEST_VECTOR_HASH


def test_machine_id_env_override_wins(monkeypatch) -> None:
    monkeypatch.delenv(CLIENT_EXE_ENV, raising=False)
    monkeypatch.setenv(MACHINE_ID_ENV, TEST_VECTOR_MACHINE_ID)
    assert machine_id() == TEST_VECTOR_MACHINE_ID


def test_machine_id_override_is_ignored_in_the_embedded_build(monkeypatch, tmp_path) -> None:
    launcher = tmp_path / "horizon.exe"
    launcher.write_bytes(b"")
    monkeypatch.setenv(CLIENT_EXE_ENV, str(launcher))
    monkeypatch.setenv(MACHINE_ID_ENV, TEST_VECTOR_MACHINE_ID)
    monkeypatch.setattr(device_mod, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(device_mod, "_linux_machine_id", lambda: "from-os")
    assert machine_id() == "from-os"


def test_machine_id_override_is_ignored_in_the_pyinstaller_build(monkeypatch) -> None:
    monkeypatch.delenv(CLIENT_EXE_ENV, raising=False)
    monkeypatch.setenv(MACHINE_ID_ENV, TEST_VECTOR_MACHINE_ID)
    monkeypatch.setattr(bundled_client.sys, "frozen", True, raising=False)
    monkeypatch.setattr(device_mod, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(device_mod, "_linux_machine_id", lambda: "from-os")
    assert machine_id() == "from-os"


def test_a_client_exe_path_that_is_not_a_file_does_not_count_as_bundled(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv(CLIENT_EXE_ENV, str(tmp_path / "missing.exe"))
    monkeypatch.setenv(MACHINE_ID_ENV, TEST_VECTOR_MACHINE_ID)
    assert machine_id() == TEST_VECTOR_MACHINE_ID


def test_blank_override_is_ignored(monkeypatch) -> None:
    monkeypatch.setenv(MACHINE_ID_ENV, "   ")
    monkeypatch.setattr(device_mod, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(device_mod, "_linux_machine_id", lambda: "from-os")
    assert machine_id() == "from-os"


def test_missing_machine_id_is_an_error(monkeypatch) -> None:
    monkeypatch.delenv(MACHINE_ID_ENV, raising=False)
    monkeypatch.setattr(device_mod, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(device_mod, "_linux_machine_id", lambda: "")
    with pytest.raises(MachineIdError):
        machine_id()


def test_linux_machine_id_reads_first_non_empty_file(monkeypatch, tmp_path) -> None:
    empty = tmp_path / "empty"
    empty.write_text("\n")
    real = tmp_path / "dbus"
    real.write_text("0123abcd\n")
    monkeypatch.setattr(device_mod, "_LINUX_IDS", (str(empty), str(real)))
    assert device_mod._linux_machine_id() == "0123abcd"


@pytest.mark.skipif(sys.platform != "win32", reason="reads the Windows registry")
def test_windows_machine_guid_is_the_64_bit_registry_value(monkeypatch) -> None:
    import winreg

    monkeypatch.delenv(MACHINE_ID_ENV, raising=False)
    with winreg.OpenKey(
        winreg.HKEY_LOCAL_MACHINE,
        r"SOFTWARE\Microsoft\Cryptography",
        0,
        winreg.KEY_READ | winreg.KEY_WOW64_64KEY,
    ) as key:
        expected, _ = winreg.QueryValueEx(key, "MachineGuid")
    assert machine_id() == str(expected)


def test_macos_uuid_is_parsed_from_ioreg_output(monkeypatch) -> None:
    class _Done:
        stdout = '    "IOPlatformUUID" = "11111111-2222-3333-4444-555555555555"\n'

    monkeypatch.setattr(device_mod.subprocess, "run", lambda *a, **k: _Done())
    assert device_mod._macos_platform_uuid() == "11111111-2222-3333-4444-555555555555"


def _write_device(home, **overrides) -> None:
    data = {
        "version": 1,
        "api_url": "https://api.contextshrink.com",
        "device_id": "dev-1",
        "device_key": "cs_dev_secret",
        "device_hash": TEST_VECTOR_HASH,
        "tools": ["claude"],
        "created_at": "2026-10-22T00:00:00+00:00",
        **overrides,
    }
    home.mkdir(parents=True, exist_ok=True)
    (home / "device.json").write_text(json.dumps(data), encoding="utf-8")


def test_load_device_reads_a_valid_file(tmp_path) -> None:
    _write_device(tmp_path, api_url="https://api.contextshrink.com/")
    loaded = load_device(tmp_path)
    assert loaded == Device(
        device_id="dev-1",
        device_key="cs_dev_secret",
        device_hash=TEST_VECTOR_HASH,
        api_url="https://api.contextshrink.com",
        tools=("claude",),
    )


def test_device_key_is_not_shown_in_repr(tmp_path) -> None:
    _write_device(tmp_path)
    assert "cs_dev_secret" not in repr(load_device(tmp_path))


@pytest.mark.parametrize(
    "overrides",
    [
        {"version": 2},
        {"device_key": "not-a-device-key"},
        {"device_hash": "XYZ"},
        {"api_url": "ftp://example"},
        {"device_id": ""},
    ],
)
def test_load_device_rejects_malformed_files(tmp_path, overrides) -> None:
    _write_device(tmp_path, **overrides)
    assert load_device(tmp_path) is None


def test_load_device_is_none_when_absent(tmp_path) -> None:
    assert load_device(tmp_path) is None
