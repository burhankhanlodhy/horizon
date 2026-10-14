from __future__ import annotations

import hashlib
import hmac
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from horizon.desktop_mcp.companion import CompanionError, SessionVault
from horizon.desktop_mcp.session_recovery import (
    LEASE_NAME,
    STATE_SLOTS,
    load_hmac_key,
    sign_state,
)
from horizon.desktop_mcp.settings import DesktopSettings
from horizon.desktop_mcp.staging_fs import PinnedStagingDirectory


def _manifest(
    fs: PinnedStagingDirectory,
    key: bytes,
    session_id: str,
    generation: int,
    entries: list[dict[str, object]],
) -> None:
    payload = sign_state(
        key,
        session_id=session_id,
        root_identity=fs.root_identity,
        directory_identity=fs.directory_identity,
        lease_identity=fs.lease_identity,
        generation=generation,
        entries=entries,
    )
    name = f"state-{uuid.uuid4().hex}.tmp"
    identity = fs.write_exclusive(name, payload)
    slot = STATE_SLOTS[generation % 2]
    assert fs.rename_owned_new(name, slot, identity)


def _orphan(
    root: Path,
    key: bytes,
    *,
    body: bytes = b"crash-owned plaintext",
    expires: float = 1.0,
    crash_partial: bool = False,
) -> tuple[str, str, tuple[int, int]]:
    session_id = uuid.uuid4().hex
    fs = PinnedStagingDirectory.open(root, f".contextshrink-session-{session_id}")
    assert fs.try_acquire_lease(LEASE_NAME, create=True)
    entry_id = uuid.uuid4().hex
    filename = f"context-{entry_id}.txt"
    identity = fs.create_empty_exclusive(filename)
    created = 0.0
    entry: dict[str, object] = {
        "id": entry_id,
        "name": filename,
        "state": "writing",
        "file_id": list(identity),
        "size": len(body),
        "sha256": hashlib.sha256(body).hexdigest(),
        "created_epoch": created,
        "expires_epoch": expires,
    }
    _manifest(fs, key, session_id, 1, [entry])
    if crash_partial:
        assert fs.write_owned(filename, identity, body[: max(1, len(body) // 2)])
    else:
        assert fs.write_owned(filename, identity, body)
        entry["state"] = "committed"
        _manifest(fs, key, session_id, 2, [entry])
    directory = fs.directory.name
    fs.close(remove_directory=False, delete_entries=False)
    return directory, filename, identity


def _temp_key(root: Path, tmp_path: Path) -> bytes:
    key, directory = load_hmac_key([root], tmp_path / "private-recovery-key")
    assert directory != root
    return key


def test_expired_orphan_is_removed_after_restart(tmp_path: Path) -> None:
    root = tmp_path / "approved"
    root.mkdir()
    key = _temp_key(root, tmp_path)
    directory, filename, _identity = _orphan(root, key)

    assert SessionVault.recover_expired_sessions(root, key, now=10.0) == 1
    assert not (root / directory).exists()


def test_interrupted_writing_state_removes_only_bounded_partial(tmp_path: Path) -> None:
    root = tmp_path / "approved"
    root.mkdir()
    key = _temp_key(root, tmp_path)
    directory, filename, _identity = _orphan(root, key, crash_partial=True)
    path = root / directory / filename
    assert 0 < path.stat().st_size < len(b"crash-owned plaintext")

    assert SessionVault.recover_expired_sessions(root, key, now=10.0) == 1
    assert not (root / directory).exists()


def test_process_termination_releases_lease_and_next_process_recovers(
    tmp_path: Path,
) -> None:
    root = tmp_path / "approved"
    root.mkdir()
    key_dir = tmp_path / "private-recovery-key"
    key, _ = load_hmac_key([root], key_dir)
    script = r"""
import hashlib, os, sys, time, uuid
from pathlib import Path
from horizon.desktop_mcp.session_recovery import LEASE_NAME, STATE_SLOTS, load_hmac_key, sign_state
from horizon.desktop_mcp.staging_fs import PinnedStagingDirectory
root, key_dir = Path(sys.argv[1]), Path(sys.argv[2])
key, _ = load_hmac_key([root], key_dir)
session_id = uuid.uuid4().hex
fs = PinnedStagingDirectory.open(root, f".contextshrink-session-{session_id}")
assert fs.try_acquire_lease(LEASE_NAME, create=True)
entry_id = uuid.uuid4().hex
filename = f"context-{entry_id}.txt"
data = b"synthetic process-crash plaintext"
identity = fs.create_empty_exclusive(filename)
entry = {"id": entry_id, "name": filename, "state": "writing",
         "file_id": list(identity), "size": len(data),
         "sha256": hashlib.sha256(data).hexdigest(),
         "created_epoch": time.time(), "expires_epoch": time.time() + 60}
manifest = sign_state(key, session_id=session_id,
                      root_identity=fs.root_identity,
                      directory_identity=fs.directory_identity,
                      lease_identity=fs.lease_identity,
                      generation=1, entries=[entry])
temp = f"state-{uuid.uuid4().hex}.tmp"
temp_id = fs.write_exclusive(temp, manifest)
assert fs.rename_owned_new(temp, STATE_SLOTS[1], temp_id)
assert fs.write_owned(filename, identity, data[:12])
os._exit(23)
"""
    crashed = subprocess.run(
        [sys.executable, "-c", script, str(root), str(key_dir)],
        cwd=Path.cwd(),
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert crashed.returncode == 23, crashed.stderr.decode(errors="replace")

    assert SessionVault.recover_expired_sessions(root, key, now=10**12) == 1
    assert not any(path.is_dir() for path in root.iterdir())


def test_manifest_rotation_stays_within_two_authenticated_slots(tmp_path: Path) -> None:
    root = tmp_path / "approved"
    root.mkdir()
    settings = DesktopSettings(profile="local", approved_roots=(root,))
    vault = SessionVault(settings, root, recovery_key_directory=tmp_path / "private-key")
    for _ in range(5):
        vault._persist_manifest()
    children = set(vault._filesystem.list_names())
    assert {name for name in children if name in STATE_SLOTS} == set(STATE_SLOTS)
    assert len([name for name in children if name.startswith("state-")]) == 0
    assert vault.close()


def test_rotated_slot_substitution_is_not_overwritten_or_deleted(tmp_path: Path) -> None:
    root = tmp_path / "approved"
    root.mkdir()
    settings = DesktopSettings(profile="local", approved_roots=(root,))
    vault = SessionVault(settings, root, recovery_key_directory=tmp_path / "private-key")
    # Generation 1 lives in slot B. Generation 2 fills slot A, so generation 3
    # would rotate slot B only after verifying its recorded file identity.
    vault._persist_manifest()
    slot = STATE_SLOTS[1]
    path = vault.directory / slot
    path.unlink()
    path.write_bytes(b"unowned replacement")

    with pytest.raises(CompanionError):
        vault._persist_manifest()
    assert path.read_bytes() == b"unowned replacement"
    assert not vault.close()


def test_failed_import_discard_keeps_authenticated_partial_for_recovery(
    tmp_path: Path,
) -> None:
    root = tmp_path / "approved"
    root.mkdir()
    settings = DesktopSettings(profile="local", approved_roots=(root,))
    vault = SessionVault(settings, root, recovery_key_directory=tmp_path / "private-key")
    entry_id = uuid.uuid4().hex
    filename = f"context-{entry_id}.txt"
    data = b"partial import retained until safe cleanup"
    identity = vault._filesystem.create_empty_exclusive(filename)
    expiry = 50.0
    vault._manifest_entries[entry_id] = {
        "id": entry_id,
        "name": filename,
        "state": "writing",
        "file_id": list(identity),
        "size": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "created_epoch": 0.0,
        "expires_epoch": expiry,
    }
    vault._persist_manifest()
    assert vault._filesystem.write_owned(filename, identity, data)
    hardlink = root / "external-link.txt"
    try:
        os.link(vault.directory / filename, hardlink)
    except (OSError, NotImplementedError):
        vault.close()
        pytest.skip("hard links are unavailable")

    vault._discard_and_update(entry_id, filename, identity)
    assert entry_id in vault._manifest_entries
    assert (vault.directory / filename).read_bytes() == data
    assert not vault.close()
    hardlink.unlink()


def test_active_companion_lease_protects_expired_files(tmp_path: Path) -> None:
    root = tmp_path / "approved"
    root.mkdir()
    key = _temp_key(root, tmp_path)
    session_id = uuid.uuid4().hex
    fs = PinnedStagingDirectory.open(root, f".contextshrink-session-{session_id}")
    assert fs.try_acquire_lease(LEASE_NAME, create=True)
    entry_id = uuid.uuid4().hex
    filename = f"context-{entry_id}.txt"
    identity = fs.create_empty_exclusive(filename)
    body = b"still active"
    assert fs.write_owned(filename, identity, body)
    entry = {
        "id": entry_id,
        "name": filename,
        "state": "committed",
        "file_id": list(identity),
        "size": len(body),
        "sha256": hashlib.sha256(body).hexdigest(),
        "created_epoch": 0.0,
        "expires_epoch": 1.0,
    }
    _manifest(fs, key, session_id, 1, [entry])

    assert SessionVault.recover_expired_sessions(root, key, now=10.0) == 0
    assert (fs.directory / filename).read_bytes() == body
    fs.close(remove_directory=False, delete_entries=False)


@pytest.mark.parametrize("kind", ["bad_hmac", "path_escape", "excessive_ttl"])
def test_untrusted_manifest_never_deletes_source_or_neighbor(tmp_path: Path, kind: str) -> None:
    root = tmp_path / "approved"
    root.mkdir()
    key = _temp_key(root, tmp_path)
    session_id = uuid.uuid4().hex
    fs = PinnedStagingDirectory.open(root, f".contextshrink-session-{session_id}")
    assert fs.try_acquire_lease(LEASE_NAME, create=True)
    staged = b"leave this plaintext untouched"
    entry_id = uuid.uuid4().hex
    filename = f"context-{entry_id}.txt"
    identity = fs.create_empty_exclusive(filename)
    assert fs.write_owned(filename, identity, staged)
    escaped = root / "neighbor.txt"
    escaped.write_bytes(b"neighbor")
    name = STATE_SLOTS[1]
    if kind == "bad_hmac":
        entry_name = filename
    else:
        entry_name = "../neighbor.txt"
    expires = 3601.0 if kind == "excessive_ttl" else 1.0
    body = {
        "schema_version": 1,
        "session_id": session_id,
        "root_identity": list(fs.root_identity),
        "directory_identity": list(fs.directory_identity),
        "lease_identity": list(fs.lease_identity),
        "generation": 1,
        "entries": [
            {
                "id": entry_id,
                "name": entry_name,
                "state": "committed",
                "file_id": list(identity),
                "size": len(staged),
                "sha256": hashlib.sha256(staged).hexdigest(),
                "created_epoch": 0.0,
                "expires_epoch": expires,
            }
        ],
    }
    body["hmac_sha256"] = hmac.new(
        key,
        json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii"),
        hashlib.sha256,
    ).hexdigest()
    if kind == "bad_hmac":
        body["hmac_sha256"] = "0" * 64
    fs.write_exclusive(name, json.dumps(body, sort_keys=True, separators=(",", ":")).encode())
    fs.close(remove_directory=False, delete_entries=False)

    assert SessionVault.recover_expired_sessions(root, key, now=10.0) == 0
    assert (root / f".contextshrink-session-{session_id}" / filename).read_bytes() == staged
    assert escaped.read_bytes() == b"neighbor"


def test_hardlinked_orphan_is_skipped(tmp_path: Path) -> None:
    root = tmp_path / "approved"
    root.mkdir()
    key = _temp_key(root, tmp_path)
    directory, filename, _identity = _orphan(root, key)
    source = root / directory / filename
    outside = root / "user-owned.txt"
    try:
        os.link(source, outside)
    except (OSError, NotImplementedError):
        pytest.skip("hard links are unavailable")

    assert SessionVault.recover_expired_sessions(root, key, now=10.0) == 0
    assert source.read_bytes() == b"crash-owned plaintext"
    assert outside.read_bytes() == b"crash-owned plaintext"


@pytest.mark.parametrize("slot", [STATE_SLOTS[0], STATE_SLOTS[1]])
def test_corrupt_either_manifest_generation_preserves_orphan(tmp_path: Path, slot: str) -> None:
    root = tmp_path / "approved"
    root.mkdir()
    key = _temp_key(root, tmp_path)
    directory, filename, _identity = _orphan(root, key)
    manifest_path = root / directory / slot
    damaged = bytearray(manifest_path.read_bytes())
    damaged[-1] ^= 1
    manifest_path.write_bytes(damaged)

    assert SessionVault.recover_expired_sessions(root, key, now=10.0) == 0
    assert (root / directory / filename).read_bytes() == b"crash-owned plaintext"


def test_changed_file_identity_preserves_replacement_and_manifest(tmp_path: Path) -> None:
    root = tmp_path / "approved"
    root.mkdir()
    key = _temp_key(root, tmp_path)
    directory, filename, _identity = _orphan(root, key)
    source = root / directory / filename
    replacement = source.with_name("replacement.tmp")
    replacement.write_bytes(b"replacement owned by someone else")
    os.replace(replacement, source)

    assert SessionVault.recover_expired_sessions(root, key, now=10.0) == 0
    assert source.read_bytes() == b"replacement owned by someone else"
    assert any((root / directory / slot).exists() for slot in STATE_SLOTS)


def test_replaced_lease_identity_is_not_deleted_or_trusted(tmp_path: Path) -> None:
    root = tmp_path / "approved"
    root.mkdir()
    key = _temp_key(root, tmp_path)
    directory, filename, _identity = _orphan(root, key)
    session_dir = root / directory
    lease = session_dir / LEASE_NAME
    lease.unlink()
    lease.write_bytes(b"")

    assert SessionVault.recover_expired_sessions(root, key, now=10.0) == 0
    assert lease.exists()
    assert (session_dir / filename).read_bytes() == b"crash-owned plaintext"


def test_unowned_temp_child_is_never_deleted(tmp_path: Path) -> None:
    root = tmp_path / "approved"
    root.mkdir()
    key = _temp_key(root, tmp_path)
    directory, _filename, _identity = _orphan(root, key)
    session_dir = root / directory
    unknown = session_dir / "unowned.tmp"
    unknown.write_bytes(b"unowned")

    assert SessionVault.recover_expired_sessions(root, key, now=10.0) == 1
    assert unknown.read_bytes() == b"unowned"


def test_failed_source_delete_keeps_manifest_for_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "approved"
    root.mkdir()
    key = _temp_key(root, tmp_path)
    directory, filename, _identity = _orphan(root, key)
    original = PinnedStagingDirectory.delete_owned

    def fail_source(self: PinnedStagingDirectory, name: str, identity: tuple[int, int]) -> bool:
        if name == filename:
            return False
        return original(self, name, identity)

    monkeypatch.setattr(PinnedStagingDirectory, "delete_owned", fail_source)
    assert SessionVault.recover_expired_sessions(root, key, now=10.0) == 0
    session_dir = root / directory
    assert (session_dir / filename).read_bytes() == b"crash-owned plaintext"
    assert any((session_dir / slot).exists() for slot in STATE_SLOTS)
