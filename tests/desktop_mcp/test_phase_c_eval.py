from __future__ import annotations

import json

from desktop.mcp.phase_c_eval import run_phase_c_eval


def test_phase_c_eval_preserves_synthetic_constraints_and_reports_unknown_cost(tmp_path) -> None:
    report_path = tmp_path / "phase-c-report.json"
    report = run_phase_c_eval(report_path)

    assert report["status"] == "passed"
    assert report["fixture_provenance"] == "synthetic_fixture_only"
    assert report["execution"]["model_api_calls"] == 0
    assert report["execution"]["network_calls"] == 0
    assert report["execution"]["user_sources_read"] is False
    assert report["cost_attribution"]["enterprise_billing"] == "not_measured"
    assert report["cost_attribution"]["usd_savings"] == "not_reported"
    assert report["claims"]["universal_positive_savings"] is False
    assert (
        report["measurement_scope"]["json_rpc_framing"] == "not captured by the in-memory transport"
    )
    assert report["measurement_scope"]["host_added_context"] == "unknown"
    assert all(
        case["missing_after_recovery_count"] == 0
        and case["recovery_targets_found_count"] == case["recovery_target_count"]
        for arm in report["arms"].values()
        for case in arm["quality"]
    )
    optimized = report["arms"]["optimized"]
    assert any(call["tool"] == "contextshrink_retrieve" for call in optimized["calls"])
    assert json.loads(report_path.read_text(encoding="utf-8")) == report
