from __future__ import annotations

import asyncio
import json

from horizon.desktop_mcp import capabilities
from horizon.desktop_mcp.capabilities import build_runtime_report, run_doctor, run_probe


def test_runtime_report_is_metadata_only_and_does_not_claim_desktop_compatibility() -> None:
    report = build_runtime_report()

    assert report["phase"] == "C"
    assert report["runtime"]["python_version"]
    assert report["runtime"]["platform"]
    assert report["runtime"]["mcp_sdk"]["installed"] is True
    assert report["runtime"]["mcp_sdk"]["version"]
    assert report["runtime"]["mcp_sdk"]["desktop_application_compatibility"] == "not_tested"
    assert report["capabilities"]["protocol_compatibility"] == "installed_sdk_only"
    assert report["capabilities"]["claude_desktop"] == "not_tested"
    assert report["capabilities"]["desktop_apps"] == "not_tested"
    assert report["capabilities"]["billing"] == "unknown"
    assert report["capabilities"]["filesystem_access"] == "available_in_local_profile"
    assert report["capabilities"]["compression"].startswith("local_profile_only")
    assert report["capabilities"]["token_counting"] == "offline_estimate_not_claude_count"
    assert report["capabilities"]["payload_accounting"] == (
        "inner_result_json_only; sdk_and_host_overhead_unknown"
    )
    assert report["capabilities"]["usage_ledger"] == "ephemeral_local_integrity_only"
    assert report["capabilities"]["enterprise_savings"] == "not_measured"
    assert report["capabilities"]["broker"] == "available_in_local_profile"
    assert report["capabilities"]["model_required"] is False
    assert report["capabilities"]["api_key_required"] is False


def test_doctor_only_writes_to_an_explicit_output_path(tmp_path) -> None:
    target = tmp_path / "report.json"

    report = run_doctor(target)

    assert report["check"] == "doctor"
    assert json.loads(target.read_text(encoding="utf-8")) == report
    assert not list(tmp_path.glob("**/*config*"))


def test_probe_failure_report_does_not_echo_exception_details(monkeypatch, tmp_path) -> None:
    async def fail_with_sensitive_detail() -> dict:
        raise RuntimeError(f"private fixture text at {tmp_path}")

    monkeypatch.setattr(capabilities, "_probe_session", fail_with_sensitive_detail)
    report = asyncio.run(run_probe())

    serialized = json.dumps(report)
    assert report["status"] == "failed"
    assert report["error"] == "probe_failed:RuntimeError"
    assert str(tmp_path) not in serialized
    assert "private fixture text" not in serialized


def test_probe_timeout_is_reported_without_exception_details(monkeypatch) -> None:
    async def wait_forever() -> dict:
        await asyncio.sleep(1)
        return {}

    monkeypatch.setattr(capabilities, "_probe_session", wait_forever)
    monkeypatch.setattr(capabilities, "_PROBE_TIMEOUT_SECONDS", 0.01)
    report = asyncio.run(run_probe())

    assert report["status"] == "failed"
    assert report["error"] == "probe_timeout"


def test_report_write_errors_do_not_echo_explicit_path(monkeypatch, tmp_path) -> None:
    doctor = run_doctor(tmp_path)

    async def successful_probe() -> dict:
        return {"check": "stdio_probe", "status": "passed"}

    monkeypatch.setattr(capabilities, "_probe_session", successful_probe)
    probe = asyncio.run(run_probe(tmp_path))

    for report in (doctor, probe):
        assert report["output_error"].startswith("report_write_failed:")
        assert str(tmp_path) not in json.dumps(report)
