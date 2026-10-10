"""Tests for horizon.vault (OS credential-store wrapper)."""

from __future__ import annotations

import pytest

import horizon.vault as vault
from horizon.vault import VaultError


class FakeKeyring:
    """In-memory stand-in for the platform keyring module."""

    def __init__(self) -> None:
        self.store: dict[tuple[str, str], str] = {}

    def set_password(self, service: str, name: str, value: str) -> None:
        self.store[(service, name)] = value

    def get_password(self, service: str, name: str) -> str | None:
        return self.store.get((service, name))

    def delete_password(self, service: str, name: str) -> None:
        self.store.pop((service, name), None)


@pytest.fixture()
def fake_keyring(monkeypatch: pytest.MonkeyPatch) -> FakeKeyring:
    fake = FakeKeyring()
    monkeypatch.setattr(vault, "_load_keyring", lambda: fake)
    return fake


def test_set_get_roundtrip(fake_keyring: FakeKeyring) -> None:
    vault.set_credential("hz_abc123")
    assert vault.get_credential() == "hz_abc123"
    assert vault.has_credential() is True


def test_set_rejects_empty(fake_keyring: FakeKeyring) -> None:
    with pytest.raises(VaultError, match="non-empty"):
        vault.set_credential("")
    with pytest.raises(VaultError, match="non-empty"):
        vault.set_credential("   ")


def test_get_without_entry_raises(fake_keyring: FakeKeyring) -> None:
    with pytest.raises(VaultError, match="horizon vault set"):
        vault.get_credential()
    assert vault.has_credential() is False


def test_get_strips_whitespace(fake_keyring: FakeKeyring) -> None:
    vault.set_credential("  hz_abc123 \n")
    assert vault.get_credential() == "hz_abc123"


def test_clear_tolerates_missing_entry(fake_keyring: FakeKeyring) -> None:
    vault.clear_credential()  # must not raise
    vault.set_credential("hz_abc123")
    vault.clear_credential()
    assert vault.has_credential() is False


def test_missing_keyring_module_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    import builtins

    real_import = builtins.__import__

    def _no_keyring(name: str, *args, **kwargs):
        if name == "keyring":
            raise ImportError("No module named 'keyring'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _no_keyring)
    with pytest.raises(VaultError, match=r"contextshrink\[hosted\]"):
        vault.get_credential()


def test_failing_backend_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    import keyring

    class _Fail:
        pass

    _Fail.__module__ = "keyring.backends.fail"  # matches _load_keyring's guard

    monkeypatch.setattr(keyring, "get_keyring", lambda: _Fail())
    with pytest.raises(VaultError, match="no usable OS credential-store backend"):
        vault.get_credential()


def test_cli_set_reads_key_from_stdin(fake_keyring: FakeKeyring) -> None:
    """The desktop app pipes the device key in; it must never need a TTY."""
    from click.testing import CliRunner

    from horizon.cli.vault import vault_set

    result = CliRunner().invoke(vault_set, ["--stdin"], input="cs_live_device123\n")
    assert result.exit_code == 0, result.output
    assert vault.get_credential() == "cs_live_device123"
