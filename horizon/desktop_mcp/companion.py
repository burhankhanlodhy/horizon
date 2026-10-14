"""Session-scoped local browser companion for request preparation.

The HTTP surface is deliberately narrow: a random bearer capability is sent
only in an initial URL fragment, then in an Authorization header. The server
serves static local assets and never accepts model-provided roles or paths.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import sys
import threading
import time
import uuid
import webbrowser
from collections.abc import Callable
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from .broker import BrokerClient, BrokerError
from .preparation import (
    PreparationError,
    ReferenceSource,
    prepare_packet,
)
from .session_recovery import (
    LEASE_NAME,
    STATE_SLOTS,
    RecoveryError,
    load_hmac_key,
    sign_state,
    verify_state,
)
from .settings import ConfigurationError, DesktopSettings, load_settings
from .staging_fs import PinnedStagingDirectory, StagingFilesystemError

_MAX_REQUEST_BYTES = 20 * 1024 * 1024
_MAX_TEXT_BYTES = 8 * 1024 * 1024
_MAX_STAGE_ITEMS = 32
_MAX_STAGE_BYTES = 64 * 1024 * 1024
_SAFE_UUID = re.compile(r"^[0-9a-f]{32}$")
_SAFE_SOURCE_ID = re.compile(r"^cs1_[0-9a-f]{32}$")
_STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
}
_SAFE_ARTIFACT_ID = re.compile(r"[0-9a-f]{32}")
_ARTIFACT_DOWNLOAD = re.compile(r"/api/artifacts/([^/]+)/download")
_ARTIFACT_ITEM = re.compile(r"/api/artifacts/([^/]+)")
_ARTIFACT_FORMATS = {
    "csv": ("text/csv; charset=utf-8", "csv"),
    "json": ("application/json; charset=utf-8", "json"),
    "markdown": ("text/markdown; charset=utf-8", "md"),
    "text": ("text/plain; charset=utf-8", "txt"),
}
_ARTIFACT_ERROR_STATUS = {
    "invalid_artifact_id": 400,
    "artifact_revoked": 404,
    "artifact_unknown": 404,
    "artifact_unavailable": 503,
    "artifacts_unavailable": 503,
    "broker_unavailable": 503,
}
_PASSTHROUGH_BROKER_CODES = frozenset(
    {"artifact_revoked", "artifact_unknown", "artifact_unavailable"}
)


class CompanionError(ValueError):
    """Sanitized local companion failure."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _strict_json(data: bytes) -> dict[str, Any]:
    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate_json_key")
            result[key] = value
        return result

    try:
        value = json.loads(
            data.decode("utf-8"),
            object_pairs_hook=unique,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError("invalid_number")),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError):
        raise CompanionError("invalid_json") from None
    if not isinstance(value, dict):
        raise CompanionError("invalid_json")
    return value


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode(
        "utf-8"
    )


def _utc(value: float) -> str:
    return datetime.fromtimestamp(value, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _artifact_file_name(title: Any, extension: str) -> str:
    """Return an ASCII-only download name; the title is untrusted model/user text."""
    raw = re.sub(r"\s+", " ", title if isinstance(title, str) else "")
    stem = re.sub(r"[^A-Za-z0-9 ._-]", "", raw)
    stem = re.sub(r" +", " ", stem).strip(" ._-")
    if stem.lower().endswith(f".{extension}"):
        stem = stem[: -(len(extension) + 1)].strip(" ._-")
    stem = stem[:80].strip(" ._-")
    return f"{stem or 'artifact'}.{extension}"


def _artifact_broker_error(error: BrokerError) -> CompanionError:
    code = error.code if error.code in _PASSTHROUGH_BROKER_CODES else "broker_unavailable"
    return CompanionError(code)


class SessionVault:
    """Own generated UTF-8 files under a configured approved root for one run."""

    def __init__(
        self,
        settings: DesktopSettings,
        staging_root: Path,
        *,
        recovery_key_directory: Path | None = None,
    ) -> None:
        if settings.profile != "local":
            raise CompanionError("local_profile_required")
        supplied = Path(staging_root)
        if not supplied.is_absolute():
            raise CompanionError("staging_root_not_approved")
        try:
            supplied_real = supplied.resolve(strict=True)
            approved = [Path(root).resolve(strict=True) for root in settings.approved_roots]
        except (OSError, RuntimeError, ValueError):
            raise CompanionError("staging_root_unavailable") from None
        if supplied_real not in approved:
            raise CompanionError("staging_root_not_approved")
        self.root = supplied_real
        try:
            self._recovery_key, _key_dir = load_hmac_key(approved, recovery_key_directory)
        except RecoveryError as error:
            raise CompanionError(error.code) from None
        self.recover_expired_sessions(self.root, self._recovery_key)
        self.session_id = uuid.uuid4().hex
        self.directory = self.root / f".contextshrink-session-{self.session_id}"
        try:
            self._filesystem = PinnedStagingDirectory.open(self.root, self.directory.name)
        except StagingFilesystemError:
            raise CompanionError("staging_root_unavailable") from None
        self.directory = self._filesystem.directory
        if not self._filesystem.try_acquire_lease(LEASE_NAME, create=True):
            self._filesystem.close(remove_directory=True)
            raise CompanionError("staging_unavailable")
        self._entries: dict[str, dict[str, Any]] = {}
        self._manifest_entries: dict[str, dict[str, Any]] = {}
        self._manifest_generation = 0
        self._manifest_slots: dict[str, tuple[tuple[int, int], int]] = {}
        self._expired_ids: list[str] = []
        self._bytes = 0
        self._lock = threading.RLock()
        self.ttl_seconds = max(1, min(settings.snapshot_ttl_seconds, 3600) - 5)
        self.max_bytes = min(
            _MAX_TEXT_BYTES, settings.max_source_bytes, settings.max_snapshot_bytes
        )
        self._closed = False
        self._cleanup_complete = True
        try:
            self._persist_manifest()
        except (OSError, RecoveryError, StagingFilesystemError, CompanionError):
            self._filesystem.close(remove_directory=False, delete_entries=False)
            raise CompanionError("staging_unavailable") from None

    @classmethod
    def recover_expired_sessions(cls, root: Path, key: bytes, *, now: float | None = None) -> int:
        """Remove authenticated expired orphan files after their process lease ends."""
        current = time.time() if now is None else now
        removed = 0
        try:
            names: list[str] = []
            with os.scandir(root) as children:
                for index, item in enumerate(children):
                    if index >= 4096 or len(names) >= 128:
                        break
                    if item.is_dir(follow_symlinks=False) and re.fullmatch(
                        r"\.contextshrink-session-[0-9a-f]{32}", item.name
                    ):
                        names.append(item.name)
            names.sort()
        except OSError:
            return 0
        for session_name in names:
            session_id = session_name.rsplit("-", 1)[-1]
            try:
                filesystem = PinnedStagingDirectory.open_existing(root, session_name)
            except (OSError, StagingFilesystemError):
                continue
            try:
                if not filesystem.try_acquire_lease(LEASE_NAME, create=False):
                    filesystem.close(remove_directory=False, delete_entries=False)
                    continue
                children = set(filesystem.list_names(limit=256))
                present_slots = [slot for slot in STATE_SLOTS if slot in children]
                if not present_slots:
                    filesystem.close(remove_directory=False, delete_entries=False)
                    continue
                states: list[tuple[str, tuple[int, int], dict[str, Any]]] = []
                invalid = False
                for slot in present_slots:
                    try:
                        payload, identity, links, _size = filesystem.read_existing(
                            slot, limit=64 * 1024
                        )
                        if links != 1:
                            invalid = True
                            break
                        state = verify_state(
                            payload,
                            key,
                            session_id=session_id,
                            root_identity=filesystem.root_identity or (-1, -1),
                            directory_identity=filesystem.directory_identity or (-1, -1),
                            lease_identity=filesystem.lease_identity or (-1, -1),
                        )
                        if not filesystem.adopt_existing(slot, identity):
                            invalid = True
                            break
                        states.append((slot, identity, state))
                    except (OSError, RecoveryError, StagingFilesystemError):
                        invalid = True
                        break
                if invalid or not cls._manifests_consistent([item[2] for item in states]):
                    filesystem.close(remove_directory=False, delete_entries=False)
                    continue
                latest = max((item[2] for item in states), key=lambda state: state["generation"])
                entries = latest["entries"]
                # If any authorized item is still within its advertised TTL,
                # preserve the whole session until a later launch.
                if any(
                    entry["created_epoch"] > current + 300
                    or entry["expires_epoch"] > current + 3600 + 300
                    or entry["expires_epoch"] > current
                    for entry in entries
                ):
                    filesystem.close(remove_directory=False, delete_entries=False)
                    continue
                candidates: list[tuple[str, tuple[int, int]]] = []
                for entry in entries:
                    try:
                        data, identity, links, size = filesystem.read_existing(
                            entry["name"], limit=8 * 1024 * 1024
                        )
                        if (
                            identity != tuple(entry["file_id"])
                            or links != 1
                            or size > entry["size"]
                        ):
                            invalid = True
                            break
                        if entry["state"] == "committed" and (
                            size != entry["size"]
                            or hashlib.sha256(data).hexdigest() != entry["sha256"]
                        ):
                            invalid = True
                            break
                        if entry["state"] == "writing" and size == 0:
                            # An authenticated writing record owns this exact
                            # empty or bounded partial file identity.
                            pass
                        if not filesystem.adopt_existing(entry["name"], identity):
                            invalid = True
                            break
                        candidates.append((entry["name"], identity))
                    except (OSError, StagingFilesystemError) as error:
                        if (
                            isinstance(error, FileNotFoundError)
                            or isinstance(error.__cause__, FileNotFoundError)
                            or getattr(error, "winerror", None) == 2
                        ):
                            continue
                        invalid = True
                        break
                if invalid:
                    filesystem.close(remove_directory=False, delete_entries=False)
                    continue
                # Unknown children and non-generated metadata are deliberately
                # left untouched; the final rmdir will fail safely if present.
                for filename, identity in candidates:
                    if not filesystem.delete_owned(filename, identity):
                        invalid = True
                        break
                    removed += 1
                if invalid:
                    filesystem.close(remove_directory=False, delete_entries=False)
                    continue
                metadata_removed = True
                for slot, identity, _state in states:
                    if not filesystem.delete_owned(slot, identity):
                        metadata_removed = False
                        break
                if not metadata_removed:
                    filesystem.close(remove_directory=False, delete_entries=False)
                    continue
                lease_identity = filesystem.lease_identity
                filesystem.release_lease()
                if lease_identity is not None:
                    if not filesystem.delete_owned(LEASE_NAME, lease_identity):
                        filesystem.close(remove_directory=False, delete_entries=False)
                        continue
                filesystem.close(remove_directory=True, delete_entries=False)
            except (OSError, RecoveryError, StagingFilesystemError):
                try:
                    filesystem.close(remove_directory=False, delete_entries=False)
                except (OSError, StagingFilesystemError):
                    pass
        return removed

    @staticmethod
    def _manifests_consistent(states: list[dict[str, Any]]) -> bool:
        if not states or len(states) > 2:
            return False
        ordered = sorted(states, key=lambda state: state["generation"])
        if len(ordered) == 1:
            return True
        old, new = ordered
        if new["generation"] != old["generation"] + 1:
            return False
        older = {entry["id"]: entry for entry in old["entries"]}
        newer = {entry["id"]: entry for entry in new["entries"]}
        for entry_id, entry in older.items():
            replacement = newer.get(entry_id)
            if replacement is None:
                continue
            if any(
                entry[key] != replacement[key]
                for key in ("name", "file_id", "size", "sha256", "created_epoch", "expires_epoch")
            ):
                return False
            if entry["state"] == "committed" and replacement["state"] != "committed":
                return False
        return True

    def _persist_manifest(self) -> None:
        generation = self._manifest_generation + 1
        data = sign_state(
            self._recovery_key,
            session_id=self.session_id,
            root_identity=self._filesystem.root_identity or (-1, -1),
            directory_identity=self._filesystem.directory_identity or (-1, -1),
            lease_identity=self._filesystem.lease_identity or (-1, -1),
            generation=generation,
            entries=list(self._manifest_entries.values()),
        )
        slot = STATE_SLOTS[generation % 2]
        temporary = f"state-{uuid.uuid4().hex}.tmp"
        identity = self._filesystem.write_exclusive(temporary, data)
        existing = self._manifest_slots.get(slot)
        if existing is not None:
            # The other slot contains the newest durable generation. Removing
            # only this verified older slot leaves that complete recovery point
            # intact until the new temp file is atomically promoted.
            other_slot = next(name for name in STATE_SLOTS if name != slot)
            if (
                existing[1] >= self._manifest_generation
                or other_slot not in self._manifest_slots
                or self._manifest_slots[other_slot][1] != self._manifest_generation
                or not self._filesystem.delete_owned(slot, existing[0])
            ):
                self._filesystem.delete_owned(temporary, identity)
                raise CompanionError("staging_unavailable")
            self._manifest_slots.pop(slot, None)
        if not self._filesystem.rename_owned_new(temporary, slot, identity):
            self._filesystem.delete_owned(temporary, identity)
            raise CompanionError("staging_unavailable")
        self._manifest_slots[slot] = (identity, generation)
        self._manifest_generation = generation

    @property
    def enabled(self) -> bool:
        return not self._closed

    def metadata(self) -> dict[str, Any]:
        with self._lock:
            self._expire_locked()
            expiry = min(
                (entry["expires_epoch"] for entry in self._entries.values()),
                default=time.time() + self.ttl_seconds,
            )
        return {
            "enabled": self.enabled,
            "display_root": str(self.root),
            "max_bytes": self.max_bytes,
            "max_items": _MAX_STAGE_ITEMS,
            "max_total_bytes": _MAX_STAGE_BYTES,
            "expires_at": _utc(expiry) if self.enabled else None,
            "lifetime": "companion_session_or_3600_seconds",
        }

    def _check_directory(self) -> None:
        try:
            self._filesystem.check_directory()
        except (OSError, RuntimeError, ValueError, StagingFilesystemError):
            raise CompanionError("staging_unavailable") from None

    def _expire_locked(self) -> None:
        now = time.time()
        expired = [
            entry_id for entry_id, item in self._entries.items() if item["expires_epoch"] <= now
        ]
        for entry_id in expired:
            self._delete_locked(entry_id)
            self._expired_ids.append(entry_id)
        if len(self._expired_ids) > 64:
            del self._expired_ids[:-64]

    def import_text(self, text: Any, config: Path) -> dict[str, Any]:
        if not isinstance(text, str):
            raise CompanionError("invalid_text")
        try:
            data = text.encode("utf-8", errors="strict")
        except UnicodeEncodeError:
            raise CompanionError("invalid_text") from None
        if not data or len(data) > self.max_bytes:
            raise CompanionError("text_size_invalid")
        with self._lock:
            self._expire_locked()
            self._check_directory()
            if len(self._entries) >= _MAX_STAGE_ITEMS or self._bytes + len(data) > _MAX_STAGE_BYTES:
                raise CompanionError("staging_full")
            entry_id = uuid.uuid4().hex
            filename = f"context-{entry_id}.txt"
            path = self.directory / filename
            expires_epoch = time.time() + self.ttl_seconds
            created_identity: tuple[int, int] | None = None
            try:
                created_identity = self._filesystem.create_empty_exclusive(filename)
                self._manifest_entries[entry_id] = {
                    "id": entry_id,
                    "name": filename,
                    "state": "writing",
                    "file_id": list(created_identity),
                    "size": len(data),
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "created_epoch": time.time(),
                    "expires_epoch": expires_epoch,
                }
                self._persist_manifest()
                if not self._filesystem.write_owned(filename, created_identity, data):
                    raise CompanionError("staging_write_failed")
                self._check_directory()
            except (OSError, RuntimeError, ValueError, StagingFilesystemError):
                self._discard_and_update(entry_id, filename, created_identity)
                raise CompanionError("staging_write_failed") from None
            except CompanionError:
                self._discard_and_update(entry_id, filename, created_identity)
                raise
            if created_identity is None:  # pragma: no cover - write_exclusive returns identity
                raise CompanionError("staging_write_failed")

            try:
                settings = load_settings(config)
                client = BrokerClient(settings)
                self._filesystem.check_directory()
                payload = client.open(
                    str(path),
                    # Mint the approved snapshot without asking the broker to
                    # materialize a bounded text page. Overview works for a
                    # valid single-line file larger than the exact-page cap.
                    mode="overview",
                    result_token_budget=min(8000, settings.result_token_budget),
                )
                expected_digest = hashlib.sha256(data).hexdigest()
                version = payload.get("version") if isinstance(payload, dict) else None
                if not isinstance(version, str) or not version.startswith(
                    f"sha256:{expected_digest}:"
                ):
                    raise CompanionError("staging_unavailable")
                source = self._source_payload(payload, path.name)
                self._filesystem.check_directory()
                self._filesystem.pin_owned_file(filename, created_identity, expected_digest)
                self._manifest_entries[entry_id]["state"] = "committed"
                self._persist_manifest()
            except BrokerError as error:
                self._discard_and_update(entry_id, filename, created_identity)
                code = error.code
                raise CompanionError(
                    code
                    if code in {"source_unapproved", "source_too_large"}
                    else "broker_unavailable"
                ) from None
            except CompanionError:
                self._discard_and_update(entry_id, filename, created_identity)
                raise
            except (ConfigurationError, OSError, RuntimeError, ValueError, TypeError, KeyError):
                self._discard_and_update(entry_id, filename, created_identity)
                raise CompanionError("staging_unavailable") from None

            metadata = {
                "id": entry_id,
                "name": filename,
                "utf8_bytes": len(data),
                "created_at": _utc(time.time()),
                "expires_at": _utc(expires_epoch),
                "expires_epoch": expires_epoch,
                "sha256": hashlib.sha256(data).hexdigest(),
                "identity": created_identity,
                "source": source,
            }
            self._entries[entry_id] = metadata
            self._bytes += len(data)
            return self.public_entry(metadata)

    @staticmethod
    def _source_payload(payload: Any, name: str) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise CompanionError("source_unavailable")
        source_id = payload.get("source_id")
        version = payload.get("version")
        kind = payload.get("kind")
        coverage = payload.get("coverage")
        selected = payload.get("selected_lines")
        if (
            not isinstance(source_id, str)
            or not _SAFE_SOURCE_ID.fullmatch(source_id)
            or not isinstance(version, str)
            or not version.startswith("sha256:")
            or not isinstance(kind, str)
            or not isinstance(coverage, str)
            or (selected is not None and not isinstance(selected, dict))
        ):
            raise CompanionError("source_unavailable")
        # Overview mints a handle without returning an excerpt. Do not turn
        # its null line range into a fictitious 1-1 selection in the UI.
        if selected is None:
            coverage = "metadata only; no source excerpt loaded"
        return {
            "source_id": source_id,
            "version": version,
            "kind": kind,
            "name": name,
            "coverage": coverage,
            "selected_lines": selected,
            "line_count": payload.get("line_count")
            if isinstance(payload.get("line_count"), int)
            and not isinstance(payload.get("line_count"), bool)
            else None,
            "more_available": payload.get("more_available") is True,
        }

    @staticmethod
    def public_entry(entry: dict[str, Any]) -> dict[str, Any]:
        return {
            key: entry[key]
            for key in ("id", "name", "utf8_bytes", "created_at", "expires_at", "source")
        }

    def list_entries(self) -> list[dict[str, Any]]:
        with self._lock:
            self._expire_locked()
            self._check_directory()
            return [self.public_entry(entry) for entry in self._entries.values()]

    def expire(self) -> None:
        with self._lock:
            self._expire_locked()

    def source_for(self, source_id: str) -> dict[str, Any] | None:
        with self._lock:
            self._expire_locked()
            for item in self._entries.values():
                if item["source"]["source_id"] == source_id:
                    return item["source"]
        return None

    def _discard(self, filename: str, identity: tuple[int, int]) -> bool:
        try:
            return self._filesystem.delete_owned(filename, identity)
        except StagingFilesystemError:
            # A changed identity fails closed. Do not follow or remove a path.
            return False

    def _discard_and_update(
        self, entry_id: str, filename: str, identity: tuple[int, int] | None
    ) -> None:
        # Keep signed ownership if deletion is blocked (for example by a
        # hardlink or a busy Windows handle); removing that record would strand
        # partial plaintext with no authenticated recovery path.
        if identity is None or self._discard(filename, identity):
            self._manifest_entries.pop(entry_id, None)
        try:
            self._persist_manifest()
        except (OSError, CompanionError, RecoveryError, StagingFilesystemError):
            pass

    def delete(self, entry_id: str) -> None:
        if not _SAFE_UUID.fullmatch(entry_id):
            raise CompanionError("invalid_staging_id")
        with self._lock:
            self._expire_locked()
            if entry_id not in self._entries:
                raise CompanionError(
                    "staging_expired" if entry_id in self._expired_ids else "not_found"
                )
            self._delete_locked(entry_id)

    def clear(self) -> None:
        with self._lock:
            for entry_id in tuple(self._entries):
                self._delete_locked(entry_id)
            # A failed import can be durably in `writing` state without a
            # broker source handle or public entry. Clean those exact owned
            # identities too; otherwise close could remove their manifests.
            for entry_id, item in tuple(self._manifest_entries.items()):
                if entry_id in self._entries:
                    continue
                self._check_directory()
                if not self._filesystem.delete_owned(item["name"], tuple(item["file_id"])):
                    raise CompanionError("staging_unavailable")
                self._manifest_entries.pop(entry_id, None)
                self._persist_manifest()

    def _delete_locked(self, entry_id: str) -> None:
        item = self._entries.get(entry_id)
        if item is None:
            return
        self._check_directory()
        try:
            deleted = self._filesystem.delete_owned(item["name"], item["identity"])
        except StagingFilesystemError:
            raise CompanionError("staging_unavailable") from None
        if not deleted:
            raise CompanionError("staging_unavailable")
        self._entries.pop(entry_id, None)
        self._manifest_entries.pop(entry_id, None)
        try:
            self._persist_manifest()
        except (OSError, CompanionError, RecoveryError, StagingFilesystemError):
            # The previous authenticated generation still owns the missing
            # identity. Recovery treats that as an already-completed delete.
            self._entries[entry_id] = item
            self._manifest_entries[entry_id] = {
                "id": entry_id,
                "name": item["name"],
                "state": "committed",
                "file_id": list(item["identity"]),
                "size": item["utf8_bytes"],
                "sha256": item["sha256"],
                "created_epoch": item["expires_epoch"] - self.ttl_seconds,
                "expires_epoch": item["expires_epoch"],
            }
            raise CompanionError("staging_unavailable") from None
        self._bytes -= item["utf8_bytes"]

    def close(self) -> bool:
        with self._lock:
            if self._closed:
                return self._cleanup_complete
            source_cleanup_complete = False
            try:
                self.clear()
                source_cleanup_complete = True
            except (OSError, CompanionError, RuntimeError, ValueError, StagingFilesystemError):
                # A changed root/identity fails closed. Never recursively
                # remove a path whose ownership or containment is uncertain.
                pass
            finally:
                try:
                    # If a source delete or durable state rotation failed, keep
                    # the authenticated manifest and files for next-launch
                    # recovery; only release the live lease and handles.
                    handles_closed = (
                        self._filesystem.close(
                            remove_directory=source_cleanup_complete,
                            delete_entries=source_cleanup_complete,
                        )
                        is not False
                    )
                    self._cleanup_complete = source_cleanup_complete and handles_closed
                except (OSError, RuntimeError, ValueError, StagingFilesystemError):
                    # If identity checks fail, the helper deliberately leaves
                    # the changed path untouched and reports no false success.
                    self._cleanup_complete = False
                self._closed = True
            return self._cleanup_complete


class PreparationCompanion:
    """Small owner for local preparation, broker reads, and stage lifecycle."""

    def __init__(
        self,
        config: Path | None,
        staging_root: Path | None,
        *,
        artifact_client: Any | None = None,
        recovery_key_directory: Path | None = None,
    ) -> None:
        self.config_path = Path(config).resolve(strict=True) if config is not None else None
        self.settings: DesktopSettings | None = None
        self.vault: SessionVault | None = None
        # Injected clients (tests) bypass the config requirement. Otherwise the
        # broker client is built lazily from settings on first artifact request.
        self.artifact_client: Any | None = artifact_client
        self.recovery_key_directory = recovery_key_directory
        if self.config_path is not None:
            self.settings = load_settings(self.config_path)
            if self.settings.profile != "local":
                raise CompanionError("local_profile_required")
            if staging_root is not None:
                self.vault = SessionVault(
                    self.settings,
                    staging_root,
                    recovery_key_directory=self.recovery_key_directory,
                )
        elif staging_root is not None:
            raise CompanionError("config_required_for_staging")

    def prepare(self, values: dict[str, Any]) -> dict[str, Any]:
        request = values.get("request")
        mode = values.get("mode")
        if not isinstance(request, str) or not isinstance(mode, str):
            raise CompanionError("invalid_request")
        context = values.get("context", "")
        query = values.get("query")
        source_id = values.get("source_id")
        if not isinstance(context, str) or (query is not None and not isinstance(query, str)):
            raise CompanionError("invalid_request")
        if mode in {"passthrough", "compact"}:
            if source_id is not None or query is not None:
                raise CompanionError("unexpected_source_options")
            source = None
        elif mode in {"focused", "reference", "reference_only"}:
            if not isinstance(source_id, str) or not _SAFE_SOURCE_ID.fullmatch(source_id):
                raise CompanionError("invalid_source_id")
            if context:
                raise CompanionError("unexpected_context_with_source")
            if self.settings is None or self.config_path is None:
                raise CompanionError("source_id_and_config_required")
            if mode == "focused" and (not isinstance(query, str) or not query.strip()):
                raise CompanionError("query_required")
            if mode != "focused" and query is not None:
                raise CompanionError("unexpected_query")
            source = self._read_source(source_id, reference_only=mode == "reference_only")
        else:
            raise CompanionError("unsupported_mode")

        packet = prepare_packet(
            request,
            context,
            mode=mode,
            source=source,
            query=query,
            packet_byte_budget=values.get("packet_byte_budget", 65536),
            packet_token_budget=values.get("packet_token_budget", 8000),
            max_input_bytes=_MAX_TEXT_BYTES,
        )
        raw = packet.packet_json.encode("utf-8")
        return {
            "status": "preview_ready",
            "mode": packet.mode,
            "fidelity": packet.fidelity,
            "coverage": packet.coverage,
            "measurements": packet.measurements,
            "provenance": packet.provenance,
            "fallback_reason": packet.fallback_reason,
            "packet_utf8_bytes": len(raw),
            "packet_sha256": hashlib.sha256(raw).hexdigest(),
            "packet_written": False,
            "packet_json": packet.packet_json,
        }

    def _read_source(self, source_id: str, *, reference_only: bool = False) -> ReferenceSource:
        assert self.config_path is not None and self.settings is not None
        try:
            client = BrokerClient(self.settings)
            payload = client.open(
                source_id,
                mode="overview" if reference_only else "exact",
                result_token_budget=min(8000, self.settings.result_token_budget),
            )
        except (BrokerError, OSError, RuntimeError, ValueError):
            raise CompanionError("source_unavailable") from None
        if not isinstance(payload, dict) or payload.get("source_id") != source_id:
            raise CompanionError("source_unavailable")
        text = payload.get("text", "" if reference_only else None)
        selected = payload.get("selected_lines")
        if not isinstance(text, str) or (selected is not None and not isinstance(selected, dict)):
            raise CompanionError("source_unavailable")
        if selected is None:
            if reference_only:
                coverage = "metadata only; no source excerpt loaded"
                selected = {"start": 1, "end": 1}
            else:
                selected = {"start": 1, "end": 1}
        start = selected.get("start", 1)
        if isinstance(start, bool) or not isinstance(start, int) or start < 1:
            raise CompanionError("source_unavailable")
        coverage = payload.get("coverage")
        if not isinstance(coverage, str):
            raise CompanionError("source_unavailable")
        if reference_only:
            coverage = (
                "metadata only; no source excerpt loaded; exact recovery uses existing handle"
            )
        elif payload.get("more_available") is True:
            coverage = f"{coverage}; first bounded exact page only; use the existing handle for remaining lines"
        try:
            return ReferenceSource(
                source_id=source_id,
                version=payload["version"],
                kind=payload["kind"],
                name="Approved source",
                coverage=coverage,
                evidence_text=text,
                selected_start_line=start,
                availability=(
                    "companion_session"
                    if self.vault is not None and self.vault.source_for(source_id) is not None
                    else "broker_snapshot_lifetime"
                ),
            )
        except (PreparationError, KeyError, TypeError):
            raise CompanionError("source_unavailable") from None

    def _artifact_broker(self) -> Any:
        if self.artifact_client is None:
            if self.settings is None:
                raise CompanionError("artifacts_unavailable")
            try:
                # Imported here so the companion still loads if the broker
                # client is absent or the broker module is unavailable.
                from .broker import CompanionBrokerClient

                self.artifact_client = CompanionBrokerClient(self.settings)
            except (
                ImportError,
                AttributeError,
                ConfigurationError,
                OSError,
                RuntimeError,
                ValueError,
                TypeError,
            ):
                raise CompanionError("artifacts_unavailable") from None
        return self.artifact_client

    def artifact_manifests(self) -> list[dict[str, Any]]:
        client = self._artifact_broker()
        try:
            manifests = client.list_artifacts()
        except BrokerError as error:
            raise _artifact_broker_error(error) from None
        except (OSError, RuntimeError, ValueError, TypeError):
            raise CompanionError("broker_unavailable") from None
        if not isinstance(manifests, list) or not all(isinstance(item, dict) for item in manifests):
            raise CompanionError("broker_unavailable")
        return manifests

    def artifact_download(self, artifact_id: str) -> tuple[bytes, str, str]:
        client = self._artifact_broker()
        if not _SAFE_ARTIFACT_ID.fullmatch(artifact_id):
            raise CompanionError("invalid_artifact_id")
        manifest = next(
            (item for item in self.artifact_manifests() if item.get("artifact_id") == artifact_id),
            None,
        )
        if manifest is None:
            raise CompanionError("artifact_unknown")
        try:
            data = client.read_artifact(artifact_id)
        except BrokerError as error:
            raise _artifact_broker_error(error) from None
        except (OSError, RuntimeError, ValueError, TypeError):
            raise CompanionError("broker_unavailable") from None
        if not isinstance(data, bytes):
            raise CompanionError("broker_unavailable")
        content_type, extension = _ARTIFACT_FORMATS.get(
            manifest.get("format"), ("application/octet-stream", "bin")
        )
        return data, content_type, _artifact_file_name(manifest.get("title"), extension)

    def artifact_delete(self, artifact_id: str) -> None:
        client = self._artifact_broker()
        if not _SAFE_ARTIFACT_ID.fullmatch(artifact_id):
            raise CompanionError("invalid_artifact_id")
        try:
            deleted = client.delete_artifact(artifact_id)
        except BrokerError as error:
            raise _artifact_broker_error(error) from None
        except (OSError, RuntimeError, ValueError, TypeError):
            raise CompanionError("broker_unavailable") from None
        if not deleted:
            raise CompanionError("artifact_unknown")

    def session_info(self) -> dict[str, Any]:
        return {
            "status": "ready",
            "modes": ["passthrough", "compact", "focused", "reference", "reference_only"],
            "staging": self.vault.metadata()
            if self.vault is not None
            else {
                "enabled": False,
                "display_root": None,
                "max_bytes": _MAX_TEXT_BYTES,
                "max_items": 0,
                "expires_at": None,
            },
        }


class _QuietHttpServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, server_address: Any, request_handler: Any) -> None:
        self._request_slots = threading.BoundedSemaphore(8)
        super().__init__(server_address, request_handler)

    def process_request(self, request: Any, client_address: Any) -> None:
        if not self._request_slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._request_slots.release()
            raise

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._request_slots.release()

    def handle_error(self, request: Any, client_address: Any) -> None:
        # Exceptions can contain user text or local paths; never log request data.
        return


def serve_companion(
    *,
    config: Path | None = None,
    staging_root: Path | None = None,
    open_browser: bool = True,
    port: int = 0,
    on_ready: Callable[[str, str], None] | None = None,
    artifact_client: Any | None = None,
) -> int:
    """Run until Ctrl+C or the authenticated explicit quit endpoint."""
    companion = PreparationCompanion(config, staging_root, artifact_client=artifact_client)
    token = secrets.token_urlsafe(32)
    stopped = threading.Event()
    server_ref: dict[str, _QuietHttpServer] = {}

    class Handler(BaseHTTPRequestHandler):
        server_version = "ContextShrinkLocal/1"
        sys_version = ""

        def log_message(self, format: str, *args: Any) -> None:
            return

        def setup(self) -> None:
            super().setup()
            self.connection.settimeout(5.0)

        @property
        def expected_host(self) -> str:
            return f"127.0.0.1:{self.server.server_port}"

        @property
        def expected_origin(self) -> str:
            return f"http://127.0.0.1:{self.server.server_port}"

        def _send(
            self,
            status: int,
            value: Any,
            content_type: str = "application/json; charset=utf-8",
            extra_headers: tuple[tuple[str, str], ...] = (),
        ) -> None:
            body = value if isinstance(value, bytes) else _json_bytes(value)
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Cross-Origin-Resource-Policy", "same-origin")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'",
            )
            # Extra headers are additive only; the security headers above are never replaced.
            for name, header_value in extra_headers:
                self.send_header(name, header_value)
            self.end_headers()
            self.wfile.write(body)

        def _host_ok(self) -> bool:
            hosts = self.headers.get_all("Host", [])
            return len(hosts) == 1 and hosts[0] == self.expected_host

        def _authorized(self, *, mutate: bool) -> bool:
            if not self._host_ok():
                return False
            if len(self.headers.get_all("Authorization", [])) != 1:
                return False
            if len(self.headers.get_all("Origin", [])) > 1:
                return False
            if len(self.headers.get_all("Content-Length", [])) > 1:
                return False
            if self.headers.get("Transfer-Encoding"):
                return False
            auth = self.headers.get("Authorization", "")
            prefix = "Bearer "
            if not auth.startswith(prefix) or not hmac.compare_digest(auth[len(prefix) :], token):
                return False
            if self.headers.get("Cookie"):
                return False
            origin = self.headers.get("Origin")
            if origin is not None and origin != self.expected_origin:
                return False
            if mutate and origin != self.expected_origin:
                return False
            if self.headers.get("Sec-Fetch-Site", "same-origin") not in {"same-origin", "none"}:
                return False
            return True

        def _auth_status(self) -> int:
            origin = self.headers.get("Origin")
            if origin is not None and origin != self.expected_origin:
                return 403
            if self.headers.get("Cookie"):
                return 403
            if self.headers.get("Sec-Fetch-Site", "same-origin") not in {"same-origin", "none"}:
                return 403
            return 401

        def _artifact_error(self, error: CompanionError) -> None:
            self._api_error(error, _ARTIFACT_ERROR_STATUS.get(error.code, 503))

        def _body(self) -> dict[str, Any]:
            if self.headers.get_content_type() != "application/json":
                raise CompanionError("json_required")
            raw_length = self.headers.get("Content-Length")
            try:
                length = int(raw_length or "")
            except ValueError:
                raise CompanionError("invalid_content_length") from None
            if length < 0 or length > _MAX_REQUEST_BYTES:
                raise CompanionError("request_too_large")
            data = self.rfile.read(length)
            if len(data) != length:
                raise CompanionError("invalid_content_length")
            return _strict_json(data)

        def _api_error(self, error: CompanionError, status: int = 400) -> None:
            self._send(status, {"error": error.code})

        def _contains_token(self, value: Any) -> bool:
            if isinstance(value, str):
                return token in value
            if isinstance(value, dict):
                return any(self._contains_token(item) for item in value.values())
            if isinstance(value, list):
                return any(self._contains_token(item) for item in value)
            return False

        def do_OPTIONS(self) -> None:  # noqa: N802
            self._send(403, {"error": "forbidden"})

        def do_GET(self) -> None:  # noqa: N802
            if not self._host_ok():
                self._send(421, {"error": "host_rejected"})
                return
            parsed = urlsplit(self.path)
            path = parsed.path
            if parsed.query or parsed.fragment:
                self._send(400, {"error": "invalid_url"})
                return
            asset = _STATIC.get(path)
            if asset is not None:
                try:
                    content = (Path(__file__).parent / "companion_ui" / asset[0]).read_bytes()
                except OSError:
                    self._send(503, {"error": "ui_unavailable"})
                    return
                self._send(200, content, asset[1])
                return
            if not path.startswith("/api/") or not self._authorized(mutate=False):
                self._send(self._auth_status(), {"error": "unauthorized"})
                return
            download = _ARTIFACT_DOWNLOAD.fullmatch(path)
            if path == "/api/session":
                try:
                    self._send(200, companion.session_info())
                except CompanionError as error:
                    self._api_error(error, 503)
            elif path == "/api/staging":
                if companion.vault is None:
                    self._send(200, {"items": []})
                else:
                    try:
                        self._send(200, {"items": companion.vault.list_entries()})
                    except CompanionError as error:
                        self._api_error(error, 503)
            elif path == "/api/artifacts":
                try:
                    self._send(200, {"items": companion.artifact_manifests()})
                except CompanionError as error:
                    self._artifact_error(error)
            elif download is not None:
                try:
                    data, content_type, name = companion.artifact_download(download.group(1))
                except CompanionError as error:
                    self._artifact_error(error)
                else:
                    self._send(
                        200,
                        data,
                        content_type,
                        extra_headers=(("Content-Disposition", f'attachment; filename="{name}"'),),
                    )
            else:
                self._send(404, {"error": "not_found"})

        def do_POST(self) -> None:  # noqa: N802
            if not self._authorized(mutate=True):
                self._send(self._auth_status(), {"error": "unauthorized"})
                return
            parsed = urlsplit(self.path)
            path = parsed.path
            if parsed.query or parsed.fragment:
                self._send(400, {"error": "invalid_url"})
                return
            try:
                values = self._body()
                if self._contains_token(values):
                    raise CompanionError("session_token_in_input")
                if path == "/api/prepare":
                    if set(values) - {
                        "request",
                        "context",
                        "mode",
                        "source_id",
                        "query",
                        "packet_byte_budget",
                        "packet_token_budget",
                    }:
                        raise CompanionError("invalid_request")
                    result = companion.prepare(values)
                    if token in result["packet_json"]:
                        raise CompanionError("packet_contains_session_token")
                    self._send(200, result)
                elif path == "/api/staging":
                    if (
                        set(values) != {"text", "consent"}
                        or values.get("consent") != "store_for_session_recovery"
                    ):
                        raise CompanionError("invalid_request")
                    if companion.vault is None or companion.config_path is None:
                        raise CompanionError("staging_unavailable")
                    item = companion.vault.import_text(values.get("text"), companion.config_path)
                    self._send(201, {"status": "imported", **item})
                else:
                    self._send(404, {"error": "not_found"})
            except CompanionError as error:
                self._api_error(error)
            except PreparationError as error:
                self._api_error(CompanionError(error.code))
            except (BrokerError, ConfigurationError):
                self._api_error(CompanionError("broker_unavailable"), 503)

        def do_DELETE(self) -> None:  # noqa: N802
            if not self._authorized(mutate=True):
                self._send(self._auth_status(), {"error": "unauthorized"})
                return
            parsed = urlsplit(self.path)
            path = parsed.path
            if parsed.query or parsed.fragment or self.headers.get("Transfer-Encoding"):
                self._send(400, {"error": "invalid_url"})
                return
            if path == "/api/session":
                self._send(200, {"status": "revoking"})

                def shutdown() -> None:
                    time.sleep(0.05)
                    server_ref["server"].shutdown()

                threading.Thread(target=shutdown, daemon=True).start()
                return
            artifact = _ARTIFACT_ITEM.fullmatch(path)
            if artifact is not None:
                try:
                    companion.artifact_delete(artifact.group(1))
                    self._send(200, {"status": "deleted", "id": artifact.group(1)})
                except CompanionError as error:
                    self._artifact_error(error)
                return
            prefix = "/api/staging/"
            if path.startswith(prefix) and companion.vault is not None:
                entry_id = unquote(path[len(prefix) :])
                try:
                    companion.vault.delete(entry_id)
                    self._send(200, {"status": "deleted", "id": entry_id})
                except CompanionError as error:
                    self._api_error(error)
                return
            self._send(404, {"error": "not_found"})

    try:
        server = _QuietHttpServer(("127.0.0.1", port), Handler)
    except OSError:
        if companion.vault is not None:
            if not companion.vault.close():
                print(
                    "Session cleanup was incomplete; verify the approved staging folder before reuse.",
                    file=sys.stderr,
                )
        raise CompanionError("listener_unavailable") from None
    server_ref["server"] = server
    local_url = f"http://127.0.0.1:{server.server_port}/"
    launch_url = f"{local_url}#{token}"
    print(f"ContextShrink preparation companion is ready at {local_url}")
    print(
        "Keep this terminal open while the companion is in use. Press Ctrl+C to revoke the session and clean staged files."
    )
    if companion.vault is None:
        print(
            "Pasted-context recovery is disabled; start with an approved local config and staging root to enable it."
        )
    try:
        if on_ready is not None:
            on_ready(local_url, token)
        if open_browser:
            webbrowser.open(launch_url, new=2)
        if companion.vault is not None:
            expiry_stop = threading.Event()

            def expire_loop() -> None:
                while not expiry_stop.wait(2.0):
                    try:
                        companion.vault.expire()
                    except CompanionError:
                        # A changed root fails closed; request handlers report it.
                        pass

            sweeper = threading.Thread(
                target=expire_loop, name="contextshrink-stage-expiry", daemon=True
            )
            sweeper.start()
        else:
            expiry_stop = None
            sweeper = None
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        if expiry_stop is not None:
            expiry_stop.set()
        if sweeper is not None:
            sweeper.join(timeout=2.5)
        stopped.set()
        server.server_close()
        if companion.vault is not None:
            if not companion.vault.close():
                print(
                    "Session cleanup was incomplete; verify the approved staging folder before reuse.",
                    file=sys.stderr,
                )
    return 0


__all__ = ["CompanionError", "PreparationCompanion", "SessionVault", "serve_companion"]
