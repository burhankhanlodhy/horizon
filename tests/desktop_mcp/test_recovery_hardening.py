"""Hardening checks for recovery key ACLs, key-file links and bounded recovery scans."""

from __future__ import annotations

import os
import secrets
import sys
import uuid
from pathlib import Path
from typing import Any

import pytest

from horizon.desktop_mcp.companion import SessionVault
from horizon.desktop_mcp.session_recovery import RecoveryError, load_hmac_key
from horizon.desktop_mcp.staging_fs import PinnedStagingDirectory

_KEY_FILE = "protectedsecret.bin"
_OWNER_RIGHTS_SID = "S-1-3-4"
_LOCAL_SYSTEM_SID = "S-1-5-18"
_SE_DACL_PROTECTED = 0x1000
_SCAN_LIMIT = 128


def _approved_root(tmp_path: Path) -> Path:
    root = tmp_path / "approved"
    root.mkdir()
    return root


@pytest.mark.skipif(sys.platform != "win32", reason="Windows DACL")
def test_recovery_key_directory_and_file_grant_only_owner_rights_and_system(
    tmp_path: Path,
) -> None:
    win32security = pytest.importorskip("win32security")
    key_dir = tmp_path / "recovery-keys"
    load_hmac_key([_approved_root(tmp_path)], key_dir)

    for path in (key_dir, key_dir / _KEY_FILE):
        descriptor = win32security.GetFileSecurity(
            str(path), win32security.DACL_SECURITY_INFORMATION
        )
        dacl = descriptor.GetSecurityDescriptorDacl()
        sids = {
            win32security.ConvertSidToStringSid(dacl.GetAce(index)[2])
            for index in range(dacl.GetAceCount())
        }
        control, _revision = descriptor.GetSecurityDescriptorControl()
        assert dacl.GetAceCount() == 2, path
        assert sids == {_OWNER_RIGHTS_SID, _LOCAL_SYSTEM_SID}, path
        assert control & _SE_DACL_PROTECTED, f"DACL must be protected: {path}"


def test_key_file_symlink_is_refused_and_not_followed(tmp_path: Path) -> None:
    approved = _approved_root(tmp_path)
    # A valid protected key that a symlink could point at.
    elsewhere = tmp_path / "elsewhere"
    load_hmac_key([approved], elsewhere)
    target = elsewhere / _KEY_FILE
    key_dir = tmp_path / "recovery-keys"
    key_dir.mkdir()
    link = key_dir / _KEY_FILE
    try:
        os.symlink(target, link)
    except (OSError, NotImplementedError) as error:
        pytest.skip(f"symlink creation not permitted on this machine: {error}")

    with pytest.raises(RecoveryError) as raised:
        load_hmac_key([approved], key_dir)

    assert raised.value.code == "recovery_key_unavailable"
    # The link is left in place and the target key is neither replaced nor removed.
    assert link.is_symlink()
    assert target.is_file()


def test_recovery_scan_inspects_at_most_128_session_folders(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _approved_root(tmp_path)
    key = secrets.token_bytes(32)
    names = [f".contextshrink-session-{uuid.uuid4().hex}" for _ in range(200)]
    garbage: dict[str, bytes] = {}
    for name in names:
        folder = root / name
        folder.mkdir()
        # Without a live lease, no state file is read; these files must stay put.
        payload = b"not a signed generation " + name.encode("ascii")
        (folder / "recovery-a.json").write_bytes(payload)
        garbage[name] = payload
    # A folder whose name does not match the generated pattern is never inspected.
    (root / ".contextshrink-session-not-generated").mkdir()

    inspected: list[str] = []
    real_open_existing = PinnedStagingDirectory.open_existing

    def counting_open_existing(approved_root: Path, session_name: str) -> Any:
        inspected.append(session_name)
        return real_open_existing(approved_root, session_name)

    monkeypatch.setattr(
        PinnedStagingDirectory, "open_existing", staticmethod(counting_open_existing)
    )

    removed = SessionVault.recover_expired_sessions(root, key, now=1_000.0)

    assert removed == 0
    assert len(inspected) <= _SCAN_LIMIT
    assert len(inspected) == _SCAN_LIMIT
    assert len(set(inspected)) == len(inspected)
    assert all(name in names for name in inspected)
    for name in names:
        assert (root / name).is_dir(), name
        assert (root / name / "recovery-a.json").read_bytes() == garbage[name]
    assert (root / ".contextshrink-session-not-generated").is_dir()
