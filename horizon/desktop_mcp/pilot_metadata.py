"""Opt-in, local-only store for bounded Enterprise pilot metadata.

This store is separate from extension usage counters. It never accepts prompt,
source, path, clipboard, credential, or arbitrary tool-argument fields. A file
is created only after explicit consent and is removed on expiry or deletion.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import secrets
import stat
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_SCHEMA_VERSION = 1
_DEFAULT_RETENTION_DAYS = 30
_MAX_RETENTION_DAYS = 90
_MAX_RECORDS = 10_000
_MAX_STORE_BYTES = 8 * 1024 * 1024
_MAX_EXPORT_BYTES = 16 * 1024 * 1024
_MAX_RECORD_INPUT_BYTES = 64 * 1024
_STALE_TEMP_SECONDS = 3600
_PARTICIPANT_ALPHABET = "abcdefghijklmnopqrstuvwxyz234567"
# Participant codes are generated here or by the study harness; never a real name.
_PARTICIPANT_RE = re.compile(r"^p_[a-z2-7]{10}$")
# Task ids are the bundle's task_id values. The bundle validator (pilot_study.SAFE_ID)
# accepts a broader character set; this store deliberately narrows it to an opaque
# "t_" token so that plain names ("john_smith"), dotted handles ("burhan.lodhi") and
# uppercase machine names ("BURHAN-PC") cannot pass. Bundle task ids must use this
# format before they reach the store. A name that happens to fit the pattern cannot
# be detected generally; that residual risk is accepted and documented here.
_TASK_ID_RE = re.compile(r"^t_[a-z0-9_]{1,40}$")
# pilot_study._hash_id("pair_", ..., 20): "pair_" plus 20 lowercase hex characters.
_PAIR_ID_RE = re.compile(r"^pair_[a-f0-9]{20}$")
# Allowlist of known model-id shapes; anything else (including free text) is rejected.
_MODEL_RE = re.compile(r"^(?:claude|gpt|gemini|o[0-9]|synthetic)[a-z0-9._-]{0,63}$")
_VERSION_RE = re.compile(r"^[0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,4}(?:[-+][a-z0-9.]{1,16})?$")
_COUNTER_RE = re.compile(r"^[a-z][a-z0-9]{0,31}(?:-[a-z0-9.]{1,31}){0,3}$")
_ARMS = frozenset({"A", "B", "C_normal", "C_tuned"})
_TASK_STATES = frozenset({"success", "failure", "unsupported", "bypassed", "timeout"})
_SCORER_STATES = frozenset({"pending", "scored", "withheld", "unavailable"})
_EFFORTS = frozenset({"low", "medium", "high", "max", "unknown"})
_TOOL_PROFILES = frozenset({"source", "compute", "unknown"})
_MISSING_REASONS = frozenset(
    {"not_reported", "not_available", "not_applicable", "withheld", "measurement_error"}
)
_REASON_CODES = frozenset(
    {
        "none",
        "unsupported_format",
        "source_unavailable",
        "source_revoked",
        "broker_unavailable",
        "timeout",
        "user_bypass",
        "budget_exceeded",
        "parse_error",
        "quality_failure",
        "other_bounded",
    }
)
_FORMATS = frozenset(
    {
        "txt",
        "md",
        "json",
        "jsonl",
        "log",
        "csv",
        "tsv",
        "yaml",
        "ini",
        "toml",
        "diff",
        "pdf",
        "docx",
        "none",
        "unsupported",
    }
)
_NUMERIC_FIELDS = frozenset(
    {
        "cache_input_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
        "uncached_input_tokens",
        "billed_output_tokens",
        "retries",
        "call_count",
        "payload_estimated_bytes",
        "payload_estimated_tokens",
        "total_latency_ms",
        "quality_score",
    }
)
_MISSING_FIELDS = _NUMERIC_FIELDS | {"step_latency_ms"}
_RECORD_FIELDS = frozenset(
    {
        "participant_code",
        "task_id",
        "pair_id",
        "arm",
        "locale",
        "desktop_version",
        "extension_version",
        "model",
        "effort",
        "tool_profile",
        "cache_input_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
        "uncached_input_tokens",
        "billed_output_tokens",
        "retries",
        "call_count",
        "payload_estimated_bytes",
        "payload_estimated_tokens",
        "counter_version",
        "step_latency_ms",
        "total_latency_ms",
        "quality_score",
        "scorer_status",
        "task_status",
        "failure_code",
        "fallback_code",
        "unsupported_code",
        "source_format",
        "task_started_at",
        "task_ended_at",
        "recorded_at",
        "consent_state",
        "consent_at",
        "missing_reasons",
    }
)


class PilotMetadataError(ValueError):
    """Stable, safe error raised by the pilot metadata store."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def default_store_path() -> Path:
    """Per-user store location; no directory or file is created by this call."""
    local = os.environ.get("LOCALAPPDATA")
    if os.name == "nt":
        base = Path(local) if local else Path.home() / "AppData" / "Local"
        return base / "ContextShrink" / "pilot" / "metadata.json"
    return Path.home() / ".local" / "state" / "contextshrink" / "pilot" / "metadata.json"


def new_participant_code() -> str:
    """Return a random pseudonymous study code; identity mapping stays elsewhere."""
    return "p_" + "".join(secrets.choice(_PARTICIPANT_ALPHABET) for _ in range(10))


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _utc_from_epoch(epoch: float) -> str:
    try:
        return (
            datetime.fromtimestamp(epoch, tz=timezone.utc)
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z")
        )
    except (OSError, OverflowError, ValueError):
        raise PilotMetadataError("record_invalid") from None


def _parse_time(value: Any, field: str) -> datetime:
    if not isinstance(value, str) or len(value) > 40:
        raise PilotMetadataError("record_invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise PilotMetadataError("record_invalid")
        return parsed.astimezone(timezone.utc)
    except (OSError, OverflowError, ValueError):
        raise PilotMetadataError("record_invalid") from None


def _matches(value: Any, pattern: re.Pattern[str]) -> bool:
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def _is_choice(value: Any, choices: frozenset[str]) -> bool:
    return isinstance(value, str) and value in choices


def validate_record(
    values: dict[str, Any], *, consent_at: str, recorded_at: str | None = None
) -> dict[str, Any]:
    """Validate and normalize one record against the export allowlist."""
    if not isinstance(values, dict) or set(values) - (
        _RECORD_FIELDS - {"consent_state", "consent_at", "recorded_at"}
    ):
        raise PilotMetadataError("record_invalid")
    record: dict[str, Any] = dict.fromkeys(_RECORD_FIELDS)
    record.update(values)
    record["consent_state"] = "consented"
    record["consent_at"] = consent_at
    record["recorded_at"] = recorded_at if recorded_at is not None else _utc_now()

    if not _matches(record["participant_code"], _PARTICIPANT_RE):
        raise PilotMetadataError("record_invalid")
    if not _matches(record["task_id"], _TASK_ID_RE):
        raise PilotMetadataError("record_invalid")
    if record["pair_id"] is not None and not _matches(record["pair_id"], _PAIR_ID_RE):
        raise PilotMetadataError("record_invalid")
    if not _is_choice(record["arm"], _ARMS):
        raise PilotMetadataError("record_invalid")
    if record["locale"] is not None and not (
        isinstance(record["locale"], str)
        and re.fullmatch(r"[a-z]{2,3}(?:-[A-Za-z0-9]{2,8}){0,2}", record["locale"], re.IGNORECASE)
    ):
        raise PilotMetadataError("record_invalid")
    for key in ("desktop_version", "extension_version"):
        if record[key] is not None and not _matches(record[key], _VERSION_RE):
            raise PilotMetadataError("record_invalid")
    if record["model"] is not None and not _matches(record["model"], _MODEL_RE):
        raise PilotMetadataError("record_invalid")
    if record["effort"] is not None and not _is_choice(record["effort"], _EFFORTS):
        raise PilotMetadataError("record_invalid")
    if record["tool_profile"] is not None and not _is_choice(
        record["tool_profile"], _TOOL_PROFILES
    ):
        raise PilotMetadataError("record_invalid")
    if record["counter_version"] is not None and not _matches(
        record["counter_version"], _COUNTER_RE
    ):
        raise PilotMetadataError("record_invalid")

    for key in _NUMERIC_FIELDS:
        value = record[key]
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise PilotMetadataError("record_invalid")
        if key == "quality_score":
            if not 0 <= value <= 5:
                raise PilotMetadataError("record_invalid")
        elif not isinstance(value, int) or not 0 <= value <= 10**12:
            raise PilotMetadataError("record_invalid")
    if record["step_latency_ms"] is not None:
        steps = record["step_latency_ms"]
        if (
            not isinstance(steps, list)
            or len(steps) > 256
            or any(
                isinstance(item, bool) or not isinstance(item, int) or not 0 <= item <= 86_400_000
                for item in steps
            )
        ):
            raise PilotMetadataError("record_invalid")
    if not _is_choice(record["scorer_status"], _SCORER_STATES):
        raise PilotMetadataError("record_invalid")
    if not _is_choice(record["task_status"], _TASK_STATES):
        raise PilotMetadataError("record_invalid")
    if not _is_choice(record["source_format"], _FORMATS):
        raise PilotMetadataError("record_invalid")
    for key in ("failure_code", "fallback_code", "unsupported_code"):
        if record[key] is not None and not _is_choice(record[key], _REASON_CODES):
            raise PilotMetadataError("record_invalid")
    _parse_time(consent_at, "consent_at")
    _parse_time(record["recorded_at"], "recorded_at")
    if record["task_started_at"] is not None and record["task_ended_at"] is not None:
        if _parse_time(record["task_ended_at"], "task_ended_at") < _parse_time(
            record["task_started_at"], "task_started_at"
        ):
            raise PilotMetadataError("record_invalid")
    for key in ("task_started_at", "task_ended_at"):
        if record[key] is not None:
            _parse_time(record[key], key)

    reasons = record["missing_reasons"]
    if reasons is None:
        reasons = {}
    if not isinstance(reasons, dict) or len(reasons) > len(_MISSING_FIELDS):
        raise PilotMetadataError("record_invalid")
    for key, reason in reasons.items():
        if (
            key not in _MISSING_FIELDS
            or not _is_choice(reason, _MISSING_REASONS)
            or record[key] is not None
        ):
            raise PilotMetadataError("record_invalid")
    for key in _NUMERIC_FIELDS:
        if record[key] is None and key not in reasons:
            raise PilotMetadataError("record_invalid")
    if record["step_latency_ms"] is None and "step_latency_ms" not in reasons:
        raise PilotMetadataError("record_invalid")
    record["missing_reasons"] = reasons
    return record


class PilotMetadataStore:
    """Small bounded JSON store whose data exists only after explicit consent."""

    def __init__(self, path: Path | None = None, *, clock: Any = time.time) -> None:
        self.path = Path(path) if path is not None else default_store_path()
        self._clock = clock
        self._cleanup_stale_temporary_files()

    @property
    def enabled(self) -> bool:
        return self._load() is not None

    def consent(
        self, *, accepted: bool, retention_days: int = _DEFAULT_RETENTION_DAYS
    ) -> dict[str, Any]:
        if accepted is not True:
            return {"enabled": False, "cancelled": True, "writes": 0}
        if (
            isinstance(retention_days, bool)
            or not isinstance(retention_days, int)
            or not 1 <= retention_days <= _MAX_RETENTION_DAYS
        ):
            raise PilotMetadataError("retention_invalid")
        if self._load() is not None:
            raise PilotMetadataError("already_enabled")
        now = self._clock()
        consented_at = _utc_from_epoch(now)
        data = {
            "schema_version": _SCHEMA_VERSION,
            "consent": {
                "state": "consented",
                "consented_at": consented_at,
                "retention_days": retention_days,
                "expires_epoch": now + retention_days * 86400,
            },
            "records": [],
        }
        self._write(data)
        return self.status()

    def status(self) -> dict[str, Any]:
        data = self._load()
        if data is None:
            return {"enabled": False, "records": 0, "retention_days": None, "expires_at": None}
        consent = data["consent"]
        return {
            "enabled": True,
            "records": len(data["records"]),
            "retention_days": consent["retention_days"],
            "expires_at": datetime.fromtimestamp(consent["expires_epoch"], tz=timezone.utc)
            .isoformat()
            .replace("+00:00", "Z"),
            "consented_at": consent["consented_at"],
        }

    def add_record(self, values: dict[str, Any]) -> dict[str, Any]:
        data = self._load()
        if data is None:
            raise PilotMetadataError("collection_off")
        if len(data["records"]) >= _MAX_RECORDS:
            raise PilotMetadataError("record_limit")
        record = validate_record(
            values,
            consent_at=data["consent"]["consented_at"],
            recorded_at=_utc_from_epoch(self._clock()),
        )
        data["records"].append(record)
        self._write(data)
        return {"recorded": True, "records": len(data["records"])}

    def preview(self) -> dict[str, Any]:
        data = self._load()
        if data is None:
            raise PilotMetadataError("collection_off")
        return {"consent": data["consent"], "records": data["records"]}

    def export(self, destination: Path, *, overwrite: bool = False) -> dict[str, Any]:
        data = self._load()
        if data is None:
            raise PilotMetadataError("collection_off")
        destination = Path(destination).expanduser().absolute()
        if destination == self.path.absolute() or self.path.absolute() in destination.parents:
            raise PilotMetadataError("export_destination_invalid")
        payload = (
            json.dumps(
                {"evidence_class": "pilot_metadata", "schema_version": _SCHEMA_VERSION, **data},
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
            ).encode("utf-8")
            + b"\n"
        )
        if len(payload) > _MAX_EXPORT_BYTES:
            raise PilotMetadataError("export_too_large")
        if destination.exists() and not overwrite:
            raise PilotMetadataError("export_exists")
        try:
            _atomic_write(destination, payload, overwrite=overwrite)
        except OSError:
            raise PilotMetadataError("export_failed") from None
        return {"exported": True, "records": len(data["records"]), "bytes": len(payload)}

    def delete(self) -> dict[str, Any]:
        try:
            self.path.unlink(missing_ok=True)
        except OSError:
            raise PilotMetadataError("delete_failed") from None
        return {"deleted": True, "enabled": False}

    def _cleanup_stale_temporary_files(self) -> None:
        """Remove only old regular atomic-write temporaries for this exact store."""
        try:
            entries = tuple(self.path.parent.iterdir())
        except OSError:
            return
        prefix = f".{self.path.name}."
        suffix_pattern = re.compile(r"^[A-Za-z0-9_-]{8}\.tmp$")
        now = self._clock()
        for entry in entries:
            if not entry.name.startswith(prefix) or not suffix_pattern.fullmatch(
                entry.name[len(prefix) :]
            ):
                continue
            try:
                info = os.lstat(entry)
                if not stat.S_ISREG(info.st_mode) or info.st_mtime > now - _STALE_TEMP_SECONDS:
                    continue
                entry.unlink()
            except OSError:
                continue

    def _load(self) -> dict[str, Any] | None:
        try:
            with self.path.open("rb") as handle:
                raw = handle.read(_MAX_STORE_BYTES + 1)
        except FileNotFoundError:
            return None
        except OSError:
            raise PilotMetadataError("store_unavailable") from None
        if len(raw) > _MAX_STORE_BYTES:
            raise PilotMetadataError("store_invalid")
        try:

            def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
                result: dict[str, Any] = {}
                for key, value in pairs:
                    if key in result:
                        raise ValueError("duplicate_key")
                    result[key] = value
                return result

            data = json.loads(
                raw.decode("utf-8"),
                object_pairs_hook=unique,
                parse_constant=lambda _value: (_ for _ in ()).throw(ValueError("invalid_number")),
            )
        except (UnicodeDecodeError, ValueError, RecursionError):
            raise PilotMetadataError("store_invalid") from None
        if (
            not isinstance(data, dict)
            or set(data) != {"schema_version", "consent", "records"}
            or data.get("schema_version") != _SCHEMA_VERSION
            or not isinstance(data.get("consent"), dict)
            or not isinstance(data.get("records"), list)
            or len(data["records"]) > _MAX_RECORDS
        ):
            raise PilotMetadataError("store_invalid")
        consent = data["consent"]
        if (
            set(consent) != {"state", "consented_at", "retention_days", "expires_epoch"}
            or consent.get("state") != "consented"
            or isinstance(consent.get("retention_days"), bool)
            or not isinstance(consent.get("retention_days"), int)
            or not 1 <= consent["retention_days"] <= _MAX_RETENTION_DAYS
            or isinstance(consent.get("expires_epoch"), bool)
            or not isinstance(consent.get("expires_epoch"), (int, float))
            or not math.isfinite(consent.get("expires_epoch"))
        ):
            raise PilotMetadataError("store_invalid")
        consented = _parse_time(consent["consented_at"], "consented_at")
        now = self._clock()
        if consented.timestamp() > now + 300:
            raise PilotMetadataError("store_invalid")
        expected_expiry = consented.timestamp() + consent["retention_days"] * 86400
        if abs(consent["expires_epoch"] - expected_expiry) > 1.0:
            raise PilotMetadataError("store_invalid")
        if consent["expires_epoch"] <= now:
            self.delete()
            return None
        for record in data["records"]:
            if (
                not isinstance(record, dict)
                or set(record) != _RECORD_FIELDS
                or record.get("consent_state") != "consented"
                or record.get("consent_at") != consent["consented_at"]
            ):
                raise PilotMetadataError("store_invalid")
            # Validate again on read/export so edited/corrupt files never broaden exports.
            supplied = {
                key: value
                for key, value in record.items()
                if key not in {"consent_state", "consent_at", "recorded_at"}
            }
            checked = validate_record(
                supplied,
                consent_at=consent["consented_at"],
                recorded_at=record.get("recorded_at"),
            )
            if checked != record:
                raise PilotMetadataError("store_invalid")
        return data

    def _write(self, data: dict[str, Any]) -> None:
        payload = json.dumps(
            data, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        if len(payload) > _MAX_STORE_BYTES:
            raise PilotMetadataError("store_too_large")
        try:
            _atomic_write(self.path, payload, overwrite=True)
        except OSError:
            raise PilotMetadataError("store_unavailable") from None


def _atomic_write(path: Path, payload: bytes, *, overwrite: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    fd_open = True
    identity: tuple[int, int] | None = None
    try:
        info = os.fstat(fd)
        identity = (info.st_dev, info.st_ino)
        if os.name == "nt":
            _protect_windows_file(Path(temporary))
        else:
            os.chmod(temporary, 0o600)
        handle = os.fdopen(fd, "wb")
        fd_open = False
        with handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        if overwrite:
            os.replace(temporary, path)
        else:
            # An exclusive hard link makes the no-overwrite contract atomic.
            os.link(temporary, path)
            os.unlink(temporary)
    except BaseException:
        if fd_open:
            try:
                os.close(fd)
            except OSError:
                pass
        if identity is not None:
            try:
                current = os.lstat(temporary)
                if stat.S_ISREG(current.st_mode) and (current.st_dev, current.st_ino) == identity:
                    os.unlink(temporary)
            except OSError:
                pass
        raise


def _protect_windows_file(path: Path) -> None:
    """Set a protected DACL granting access only to this user and LocalSystem."""
    if os.name != "nt":
        return
    try:
        from .broker_transport import _windows_modules, current_user_sid

        _pywintypes, _pipe, _con, _file, _api, security = _windows_modules()
        acl = security.ACL()
        user_sid = security.ConvertStringSidToSid(current_user_sid())
        system_sid = security.CreateWellKnownSid(security.WinLocalSystemSid, None)
        mask = 0x10000000  # GENERIC_ALL
        acl.AddAccessAllowedAce(security.ACL_REVISION, mask, user_sid)
        acl.AddAccessAllowedAce(security.ACL_REVISION, mask, system_sid)
        protected_dacl = getattr(security, "PROTECTED_DACL_SECURITY_INFORMATION", 0x80000000)
        flags = security.DACL_SECURITY_INFORMATION | protected_dacl
        security.SetNamedSecurityInfo(
            str(path), security.SE_FILE_OBJECT, flags, None, None, acl, None
        )
    except Exception:
        raise PilotMetadataError("file_protection_failed") from None


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Manage explicitly consented local pilot metadata."
    )
    parser.add_argument("--store", type=Path, default=default_store_path(), help=argparse.SUPPRESS)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status", help="show whether local collection is enabled")
    commands.add_parser(
        "participant-code", help="generate a random study code without storing identity data"
    )
    consent = commands.add_parser(
        "consent", help="enable collection for a bounded retention period"
    )
    consent.add_argument("--retention-days", type=int, default=_DEFAULT_RETENTION_DAYS)
    consent.add_argument(
        "--confirm",
        action="store_true",
        help="confirm the displayed collection and retention terms",
    )
    commands.add_parser("preview", help="review stored metadata before export")
    export = commands.add_parser("export", help="export allowlisted metadata to a selected file")
    export.add_argument("--output", type=Path, required=True)
    export.add_argument("--overwrite", action="store_true")
    record = commands.add_parser("record", help="add one allowlisted metadata record after consent")
    record.add_argument(
        "--input", type=Path, required=True, help="JSON file containing one bounded metadata record"
    )
    delete = commands.add_parser("delete", help="delete local pilot records and withdraw consent")
    delete.add_argument("--confirm", action="store_true", help="confirm deletion")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    store = PilotMetadataStore(args.store)
    try:
        if args.command == "status":
            result = store.status()
        elif args.command == "participant-code":
            result = {"participant_code": new_participant_code()}
        elif args.command == "consent":
            if not args.confirm:
                print("Cancelled. Collection remains off; no file was written.")
                return 0
            print("Purpose: compare consented pilot task outcomes and bounded usage metadata.")
            print(
                "Stored fields: study code, task/arm, versions/settings, token and call counts, latency, quality status and outcome codes."
            )
            print(
                "Excluded: prompts, source text, file paths, credentials, tool arguments and clipboard data."
            )
            print(
                f"Retention: {args.retention_days} days. Export destination and deletion are separate user actions."
            )
            result = store.consent(accepted=True, retention_days=args.retention_days)
        elif args.command == "preview":
            result = store.preview()
        elif args.command == "record":
            if not store.enabled:
                raise PilotMetadataError("collection_off")
            result = store.add_record(_read_record_input(args.input))
        elif args.command == "export":
            result = store.export(args.output, overwrite=args.overwrite)
        elif args.command == "delete":
            if not args.confirm:
                print("Cancelled. No records were deleted.")
                return 0
            result = store.delete()
        else:  # pragma: no cover
            return 2
    except PilotMetadataError as error:
        print(json.dumps({"error": error.code}, sort_keys=True))
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


def _read_record_input(path: Path) -> dict[str, Any]:
    """Read one bounded JSON object; invalid contents never enter an error message."""
    try:
        with Path(path).open("rb") as handle:
            raw = handle.read(_MAX_RECORD_INPUT_BYTES + 1)
        if len(raw) > _MAX_RECORD_INPUT_BYTES:
            raise PilotMetadataError("record_input_too_large")

        def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
            result: dict[str, Any] = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("duplicate_key")
                result[key] = value
            return result

        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=unique,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError("invalid_number")),
        )
    except PilotMetadataError:
        raise
    except (OSError, UnicodeDecodeError, ValueError, RecursionError):
        raise PilotMetadataError("record_input_invalid") from None
    if not isinstance(value, dict):
        raise PilotMetadataError("record_input_invalid")
    return value


__all__ = [
    "PilotMetadataError",
    "PilotMetadataStore",
    "default_store_path",
    "main",
    "new_participant_code",
    "validate_record",
]
