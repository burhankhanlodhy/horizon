"""Consent, retention and allowlisted export for local pilot metadata."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest

from horizon.desktop_mcp.pilot_metadata import (
    PilotMetadataError,
    PilotMetadataStore,
    main,
    new_participant_code,
)


def _record(**overrides: object) -> dict[str, object]:
    numeric = {
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
    result: dict[str, object] = {
        "participant_code": new_participant_code(),
        "task_id": "doc-qa-01",
        "pair_id": "pair-01",
        "arm": "B",
        "locale": "en-US",
        "desktop_version": "1.0.0",
        "extension_version": "0.1.1",
        "model": "claude-sonnet-4-0",
        "effort": "medium",
        "tool_profile": "compute",
        "cache_input_tokens": None,
        "cache_read_tokens": None,
        "cache_write_tokens": None,
        "uncached_input_tokens": 1500,
        "billed_output_tokens": None,
        "retries": 0,
        "call_count": 3,
        "payload_estimated_bytes": 2400,
        "payload_estimated_tokens": 700,
        "counter_version": "tiktoken-0.9.0",
        "step_latency_ms": [100, 200, 300],
        "total_latency_ms": 600,
        "quality_score": None,
        "scorer_status": "pending",
        "task_status": "success",
        "failure_code": None,
        "fallback_code": None,
        "unsupported_code": None,
        "source_format": "pdf",
        "task_started_at": "2026-10-14T10:00:00Z",
        "task_ended_at": "2026-10-14T10:01:00Z",
        "missing_reasons": {
            key: "not_reported"
            for key in numeric
            if key
            in {
                "cache_input_tokens",
                "cache_read_tokens",
                "cache_write_tokens",
                "billed_output_tokens",
                "quality_score",
            }
        },
    }
    result.update(overrides)
    return result


def test_collection_is_off_by_default_and_cancel_writes_nothing(tmp_path: Path) -> None:
    path = tmp_path / "pilot" / "metadata.json"
    store = PilotMetadataStore(path)
    assert store.status() == {
        "enabled": False,
        "records": 0,
        "retention_days": None,
        "expires_at": None,
    }
    assert store.consent(accepted=False) == {"enabled": False, "cancelled": True, "writes": 0}
    assert not path.exists()

    assert main(["--store", str(path), "consent"]) == 0
    assert not path.exists()


def test_consented_records_are_bounded_previewable_exportable_and_deletable(tmp_path: Path) -> None:
    path = tmp_path / "pilot" / "metadata.json"
    output = tmp_path / "review" / "metadata-export.json"
    store = PilotMetadataStore(path)
    store.consent(accepted=True, retention_days=7)
    result = store.add_record(_record())
    assert result == {"recorded": True, "records": 1}

    preview = store.preview()
    assert len(preview["records"]) == 1
    row = preview["records"][0]
    assert row["consent_state"] == "consented"
    assert row["extension_version"] == "0.1.1"
    assert row["missing_reasons"]["billed_output_tokens"] == "not_reported"

    result = store.export(output)
    assert result["exported"] is True
    exported = json.loads(output.read_text(encoding="utf-8"))
    assert exported["evidence_class"] == "pilot_metadata"
    assert len(exported["records"]) == 1
    with pytest.raises(PilotMetadataError, match="export_exists"):
        store.export(output)
    assert store.delete() == {"deleted": True, "enabled": False}
    assert not path.exists()
    assert store.status()["enabled"] is False


@pytest.mark.parametrize(
    "changes",
    [
        {"prompt": "raw user prompt"},
        {"source_path": "C:/private/source.pdf"},
        {"tool_arguments": {"query": "secret"}},
        {"task_id": "full raw prompt with spaces"},
        {"participant_code": "Burhan"},
        {"effort": "set administrator instructions"},
        {"call_count": True},
    ],
)
def test_unallowlisted_or_unbounded_values_are_rejected(
    tmp_path: Path, changes: dict[str, object]
) -> None:
    store = PilotMetadataStore(tmp_path / "metadata.json")
    with pytest.raises(PilotMetadataError, match="record_invalid"):
        store.consent(accepted=True)
        store.add_record(_record(**changes))


def test_missing_numbers_need_reasons_and_expiry_deletes_records(tmp_path: Path) -> None:
    now = [1000.0]
    store = PilotMetadataStore(tmp_path / "metadata.json", clock=lambda: now[0])
    store.consent(accepted=True, retention_days=1)
    record = _record()
    reasons = record["missing_reasons"]
    assert isinstance(reasons, dict)
    reasons.pop("billed_output_tokens")
    with pytest.raises(PilotMetadataError, match="record_invalid"):
        store.add_record(record)

    record = _record()
    store.add_record(record)
    assert store.preview()["records"][0]["recorded_at"] == "1970-01-01T00:16:40Z"
    now[0] += 86_401
    assert store.status()["enabled"] is False
    assert not (tmp_path / "metadata.json").exists()


def test_consent_retention_and_export_limits_are_validated(tmp_path: Path) -> None:
    store = PilotMetadataStore(tmp_path / "metadata.json")
    with pytest.raises(PilotMetadataError, match="retention_invalid"):
        store.consent(accepted=True, retention_days=0)
    store.consent(accepted=True)
    with pytest.raises(PilotMetadataError, match="export_destination_invalid"):
        store.export(store.path)


def test_existing_consent_is_not_silently_replaced(tmp_path: Path) -> None:
    store = PilotMetadataStore(tmp_path / "metadata.json")
    store.consent(accepted=True)
    with pytest.raises(PilotMetadataError, match="already_enabled"):
        store.consent(accepted=True, retention_days=7)
    assert store.status()["retention_days"] == 30


def test_edited_future_consent_cannot_extend_retention(tmp_path: Path) -> None:
    path = tmp_path / "metadata.json"
    store = PilotMetadataStore(path, clock=lambda: 1_800_000_000.0)
    store.consent(accepted=True, retention_days=30)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["consent"]["consented_at"] = "2099-01-01T00:00:00Z"
    data["consent"]["expires_epoch"] = 4_070_937_600.0 + 30 * 86_400
    path.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(PilotMetadataError, match="store_invalid"):
        store.status()


def test_export_does_not_replace_existing_file_without_explicit_overwrite(tmp_path: Path) -> None:
    store = PilotMetadataStore(tmp_path / "metadata.json")
    store.consent(accepted=True)
    target = tmp_path / "existing.json"
    target.write_text("keep", encoding="utf-8")
    with pytest.raises(PilotMetadataError, match="export_exists"):
        store.export(target)
    assert target.read_text(encoding="utf-8") == "keep"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows file DACL check")
def test_windows_store_file_has_protected_user_and_system_dacl(tmp_path: Path) -> None:
    import win32security

    from horizon.desktop_mcp.broker_transport import current_user_sid

    path = tmp_path / "metadata.json"
    PilotMetadataStore(path).consent(accepted=True)
    descriptor = win32security.GetNamedSecurityInfo(
        str(path), win32security.SE_FILE_OBJECT, win32security.DACL_SECURITY_INFORMATION
    )
    dacl = descriptor.GetSecurityDescriptorDacl()
    assert dacl is not None
    trustees = {
        win32security.ConvertSidToStringSid(dacl.GetAce(index)[2])
        for index in range(dacl.GetAceCount())
    }
    system_sid = win32security.CreateWellKnownSid(win32security.WinLocalSystemSid, None)
    assert trustees == {
        current_user_sid(),
        win32security.ConvertSidToStringSid(system_sid),
    }
    control, _revision = descriptor.GetSecurityDescriptorControl()
    assert control & 0x1000  # SE_DACL_PROTECTED


def test_restart_removes_only_old_store_temporary_files(tmp_path: Path) -> None:
    path = tmp_path / "metadata.json"
    stale = tmp_path / ".metadata.json.abc12345.tmp"
    unrelated = tmp_path / ".metadata.json.notes.tmp"
    fresh = tmp_path / ".metadata.json.fresh123.tmp"
    stale.write_text("interrupted record", encoding="utf-8")
    unrelated.write_text("keep", encoding="utf-8")
    fresh.write_text("active", encoding="utf-8")
    old = time.time() - 3601
    os.utime(stale, (old, old))

    PilotMetadataStore(path)

    assert not stale.exists()
    assert unrelated.read_text(encoding="utf-8") == "keep"
    assert fresh.read_text(encoding="utf-8") == "active"


def test_cli_record_flow_requires_consent_and_only_ingests_allowlisted_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "metadata.json"
    record_path = tmp_path / "record.json"
    output = tmp_path / "export.json"
    record_path.write_text(json.dumps(_record()), encoding="utf-8")

    assert main(["--store", str(path), "record", "--input", str(record_path)]) == 1
    assert json.loads(capsys.readouterr().out)["error"] == "collection_off"
    assert not path.exists()

    assert main(["--store", str(path), "consent"]) == 0
    assert not path.exists()
    capsys.readouterr()
    assert main(["--store", str(path), "consent", "--confirm"]) == 0
    capsys.readouterr()

    assert main(["--store", str(path), "record", "--input", str(record_path)]) == 0
    assert json.loads(capsys.readouterr().out)["records"] == 1
    assert main(["--store", str(path), "preview"]) == 0
    assert len(json.loads(capsys.readouterr().out)["records"]) == 1
    assert main(["--store", str(path), "export", "--output", str(output)]) == 0
    assert json.loads(capsys.readouterr().out)["exported"] is True
    assert main(["--store", str(path), "delete"]) == 0
    capsys.readouterr()
    assert path.exists()
    assert main(["--store", str(path), "delete", "--confirm"]) == 0
    assert not path.exists()
