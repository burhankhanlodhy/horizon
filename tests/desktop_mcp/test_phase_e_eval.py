from __future__ import annotations

import json
from typing import Any

from desktop.mcp.phase_e_eval import TASK_IDS, run_phase_e_eval


def _assert_no_usd_numbers(value: Any, path: str = "") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if "usd" in str(key).lower():
                assert not isinstance(child, (int, float)) or isinstance(child, bool), path + key
            _assert_no_usd_numbers(child, f"{path}{key}.")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _assert_no_usd_numbers(child, f"{path}{index}.")


def test_phase_e_eval_answers_match_and_reports_costs_honestly(tmp_path) -> None:
    report_path = tmp_path / "phase-e-report.json"
    report = run_phase_e_eval(report_path)

    assert report["status"] == "passed"
    assert report["all_answers_match"] is True
    assert list(report["tasks"]) == list(TASK_IDS)
    for task_id in TASK_IDS:
        task = report["tasks"][task_id]
        assert task["answers_match"] == {"raw": True, "optimized": True}
        for arm in ("raw", "optimized"):
            summary = task["arms"][arm]
            assert summary["total"] == (
                summary["argument_tokens"]
                + summary["result_tokens"]
                + summary["estimated_model_output_tokens"]
            )

    optimized_calls = report["tasks"]["1_grouped_exact_total"]["arms"]["optimized"]["calls_by_tool"]
    assert optimized_calls.get("contextshrink_analyze") == 1
    assert (
        report["tasks"]["2_full_table_deliverable"]["arms"]["optimized"]["calls_by_tool"].get(
            "contextshrink_render"
        )
        == 1
    )
    assert (
        report["tasks"]["3_six_exact_ranges"]["arms"]["optimized"]["calls_by_tool"].get(
            "contextshrink_retrieve_batch"
        )
        == 1
    )
    assert (
        report["tasks"]["3_six_exact_ranges"]["arms"]["raw"]["calls_by_tool"].get(
            "contextshrink_retrieve"
        )
        == 6
    )

    # Task 4 is recorded either way: the larger arm is whichever total is larger.
    task4 = report["tasks"]["4_tiny_no_gain_case"]
    raw_total = task4["arms"]["raw"]["total"]
    optimized_total = task4["arms"]["optimized"]["total"]
    assert task4["optimized_minus_raw_total"] == optimized_total - raw_total
    expected_larger = (
        "equal"
        if optimized_total == raw_total
        else ("optimized" if optimized_total > raw_total else "raw")
    )
    assert task4["larger_total_arm"] == expected_larger
    surface_difference = (
        report["arms"]["optimized"]["one_time_surface_token_estimate"]
        - report["arms"]["raw"]["one_time_surface_token_estimate"]
    )
    assert (
        task4["optimized_minus_raw_including_one_time_surface"]
        == task4["optimized_minus_raw_total"] + surface_difference
    )

    overall = report["overall"]
    assert overall["optimized_minus_raw"] == (
        report["arms"]["optimized"]["overall_total_token_estimate"]
        - report["arms"]["raw"]["overall_total_token_estimate"]
    )
    assert report["execution"]["model_api_calls"] == 0
    assert report["execution"]["network_calls"] == 0
    assert report["execution"]["user_sources_read"] is False
    assert report["cost_attribution"]["usd_savings"] == "not_reported"
    assert report["limitations"]

    _assert_no_usd_numbers(report)
    text = report_path.read_text(encoding="utf-8")
    assert str(tmp_path) not in text
    assert json.loads(text) == report
