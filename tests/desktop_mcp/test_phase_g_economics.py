from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest

MODULE_PATH = Path(__file__).parents[2] / "desktop" / "mcp" / "phase_g_economics.py"
spec = importlib.util.spec_from_file_location("phase_g_economics", MODULE_PATH)
assert spec is not None and spec.loader is not None
economics = importlib.util.module_from_spec(spec)
spec.loader.exec_module(economics)

GOOD = {
    "correctness": "correct",
    "completeness": "complete",
    "citations": "not_required",
    "unsupported_claim": "no",
}
UNSCORED = dict.fromkeys(GOOD, "unscored")
ARMS = ["A", "B", "C_normal", "C_tuned"]
WILLIAMS = (
    ("A", "B", "C_tuned", "C_normal"),
    ("B", "C_normal", "A", "C_tuned"),
    ("C_normal", "C_tuned", "B", "A"),
    ("C_tuned", "A", "C_normal", "B"),
)
ZERO_USAGE = {
    "uncached_input_tokens": 100,
    "cache_write_5m_tokens": 20,
    "cache_write_1h_tokens": 0,
    "cache_read_tokens": 30,
    "billed_output_tokens": 50,
}


def fixture() -> dict[str, Any]:
    bundle = {
        "schema_version": "phase_g_study/v1",
        "bundle_kind": "synthetic_task_bundle",
        "study_id": "economics-test",
        "study_plan": {
            "target_participants": 4,
            "minimum_completed_per_arm": 1,
            "stopping_rule": "complete_predeclared_bundle_or_stop_at_target",
            "stratum_weights": {"simple": "0.5", "complex": "0.5"},
            "refresh_max_age_hours": 24,
        },
        "tasks": [
            {
                "task_id": f"task-{stratum}",
                "stratum": stratum,
                "locale": "en-US",
                "operator_instruction": "Summarize the fictional fixture and extract its stated value.",
                "synthetic_sources": [
                    {
                        "source_id": "source-1",
                        "format": "unsupported_fixture" if stratum == "complex" else "txt",
                        "content": f"Fixture value: {n}.",
                    }
                ],
                "synthetic_check": {"kind": "integer_equal", "expected": n},
            }
            for stratum, n in (("simple", 7), ("complex", 13))
        ],
    }
    canonical = json.dumps(
        bundle, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    assignments = []
    observations = []
    scores = []
    runs = []
    now_start, now_end = "2026-10-14T12:00:00Z", "2026-10-14T12:01:00Z"
    arms = ARMS
    quality = copy.deepcopy(UNSCORED)
    for task_position, stratum in enumerate(("simple", "complex")):
        task_id = f"task-{stratum}"
        participant = "syn-p-abcd"
        pair_id = f"pair-{stratum}"
        assignment_ids: dict[str, str] = {}
        ordered_arms = WILLIAMS[(task_position + 42) % 4]
        source_rows = next(
            task["synthetic_sources"] for task in bundle["tasks"] if task["task_id"] == task_id
        )
        source_digest = hashlib.sha256(
            json.dumps(
                source_rows, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
        ).hexdigest()
        for order, arm in enumerate(ordered_arms, 1):
            assignment_id = (
                "asg_"
                + hashlib.sha256(
                    f"phase_g_study/v1|{bundle['study_id']}|{participant}|{task_id}|{arm}".encode()
                ).hexdigest()[:24]
            )
            assignment_ids[arm] = assignment_id
            pair_id = (
                "pair_"
                + hashlib.sha256(
                    f"{bundle['study_id']}|{participant}|{task_id}".encode()
                ).hexdigest()[:20]
            )
            score_id = "score_" + hashlib.sha256(assignment_id.encode("utf-8")).hexdigest()[:24]
            blind_code = (
                "blind_"
                + hashlib.sha256(
                    f"{bundle['study_id']}|{participant}|{task_id}|42|blind|{arm}".encode()
                ).hexdigest()[:8]
            )
            assignments.append(
                {
                    "assignment_id": assignment_id,
                    "score_id": score_id,
                    "participant_id": participant,
                    "participant_kind": "synthetic",
                    "pair_id": pair_id,
                    "task_id": task_id,
                    "stratum": stratum,
                    "locale": "en-US",
                    "arm": arm,
                    "order": order,
                    "blind_arm_code": blind_code,
                    "consent": {"status": "synthetic_not_applicable"},
                }
            )
            observations.append(
                {
                    "assignment_id": assignment_id,
                    "participant_id": participant,
                    "task_id": task_id,
                    "pair_id": pair_id,
                    "arm": arm,
                    "status": "completed",
                    "start_utc": now_start,
                    "end_utc": now_end,
                    "locale": "en-US",
                    "versions": {
                        "desktop": "0.1.1",
                        "extension": "0.1.1",
                        "model": "model-alpha",
                        "settings": "settings-v1",
                    },
                    "counter_version": "counter-v1",
                    "source_snapshot_sha256": source_digest,
                    "metrics": {
                        "latency_ms": 1200,
                        "retry_count": 0,
                        "tool_call_count": 1,
                        "retrieval_bytes": 128,
                        "estimated_input_tokens": 100,
                        "estimated_output_tokens": 50,
                        "schema_instruction_tokens": 10,
                    },
                    "metric_missing_reasons": {},
                    "provider_usage": copy.deepcopy(ZERO_USAGE),
                    "usage_missing_reasons": {},
                    "failure_code": None,
                    "unsupported_code": None,
                }
            )
            scores.append(
                {
                    "score_id": score_id,
                    "blinded_arm_code": blind_code,
                    "scorer_code": "scorer-01",
                    "status": "no_score",
                    **copy.deepcopy(quality),
                    "omission_codes": [],
                }
            )

        def make_run(
            assignment_id: str,
            condition: str,
            suffix: str,
            mode: str = "generated",
            *,
            task_stratum: str = stratum,
            task_source_digest: str = source_digest,
        ) -> dict[str, Any]:
            is_artifact = mode == "deterministic_artifact"
            return {
                "run_id": f"run-{task_stratum}-{suffix}",
                "assignment_id": assignment_id,
                "condition_id": condition,
                "source_snapshot_sha256": task_source_digest,
                "model": "model-alpha",
                "settings": "settings-v1",
                "counter_version": "counter-v1",
                "status": "completed",
                "retry_count": 0,
                "retry_count_missing_reason": None,
                "latency_ms": 1200,
                "latency_missing_reason": None,
                "service_cost_status": "complete",
                "service_cost_missing_reason": None,
                "request_ledger_status": "complete",
                "request_ledger_missing_reason": None,
                "requests": [
                    {
                        "request_id": f"req-{task_stratum}-{suffix}",
                        "model": "model-alpha",
                        "usage": copy.deepcopy(ZERO_USAGE),
                        "missing_reasons": {},
                    }
                ],
                "incremental_service_costs": [],
                "output": {
                    "mode": mode,
                    "generated_output_tokens": 50,
                    "generated_output_missing_reason": None,
                    "followup_requests": 0,
                    "repair_requests": 0,
                    "artifact_delivery_attempted": is_artifact,
                    "artifact_delivery_success": True if is_artifact else None,
                    "artifact_reconstructible": True if is_artifact else None,
                },
                "quality": copy.deepcopy(quality),
            }

        for arm, condition in (
            ("A", "A"),
            ("B", "B_full"),
            ("C_normal", "C_normal"),
            ("C_tuned", "C_tuned"),
        ):
            mode = "deterministic_artifact" if condition == "B_full" else "generated"
            runs.append(make_run(assignment_ids[arm], condition, condition, mode))
        for condition in (
            "B_no_source",
            "B_no_preparation",
            "B_no_retrieval",
            "B_no_compute",
            "B_no_artifact",
        ):
            runs.append(
                make_run(
                    assignment_ids["B"],
                    condition,
                    condition,
                    "generated"
                    if condition in {"B_no_source", "B_no_artifact"}
                    else "deterministic_artifact",
                )
            )

    full_profile = {
        "condition_id": "B_full",
        "model": "model-alpha",
        "settings": "settings-v1",
        "effort": "medium",
        "concise_instructions": "concise-v1",
        "tool_settings": "tool-settings-v1",
        **dict.fromkeys(economics.MECHANISMS, "enabled"),
    }
    profiles = [
        {
            **full_profile,
            "condition_id": "A",
            "concise_instructions": "bounded-raw-v1",
            "tool_settings": "raw-read-tools-v1",
            "preparation": "disabled",
            "compute": "disabled",
            "artifact": "disabled",
        },
        full_profile,
        {
            **full_profile,
            "condition_id": "C_normal",
            "concise_instructions": "recorded-normal-v1",
            "tool_settings": "recorded-native-tools-v1",
            "preparation": "disabled",
            "retrieval": "disabled",
            "compute": "disabled",
            "artifact": "disabled",
        },
        {
            **full_profile,
            "condition_id": "C_tuned",
            "tool_settings": "native-tuned-tools-v1",
            "source_access": "enabled",
            "preparation": "disabled",
            "retrieval": "disabled",
            "compute": "disabled",
            "artifact": "disabled",
        },
        *[
            {
                **full_profile,
                "condition_id": condition,
                economics.ABLATION_MECHANISM[condition]: "disabled",
            }
            for condition in sorted(economics.ABLATIONS)
        ],
    ]

    return {
        "schema_version": "phase_g_economics/v1",
        "evidence_class": "synthetic",
        "as_of_utc": "2026-10-14T13:00:00Z",
        "study_bundle": bundle,
        "assignment_set": {
            "schema_version": "phase_g_study/v1",
            "study_id": bundle["study_id"],
            "bundle_sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
            "seed": 42,
            "arms": arms,
            "assignments": assignments,
        },
        "observations": observations,
        "scores": scores,
        "usage_windows": [],
        "runs": runs,
        "condition_profiles": profiles,
        "rate_cards": [
            {
                "model": "model-alpha",
                "currency": "USD",
                "denominator": "per_1m_tokens",
                "rates": dict.fromkeys(
                    (
                        "uncached_input",
                        "cache_write_5m",
                        "cache_write_1h",
                        "cache_read",
                        "billed_output",
                    ),
                    "1",
                ),
            }
        ],
        "sensitivity_scenarios": [
            {
                "scenario_id": "base-adoption",
                "adoption_fraction": "0.5",
                "rate_multiplier": "1",
                "cache_price_multiplier": "1",
            }
        ],
    }


def real_evidence_fixture() -> dict[str, Any]:
    data = fixture()
    data["evidence_class"] = "consented_deidentified"
    old_assignments = list(data["assignment_set"]["assignments"])
    old_to_new: dict[str, dict[str, Any]] = {}
    for task_position, task_id in enumerate(("task-simple", "task-complex")):
        old_rows = [row for row in old_assignments if row["task_id"] == task_id]
        pair_id = (
            "pair_"
            + hashlib.sha256(
                f"{data['study_bundle']['study_id']}|P-1234ABCD|{task_id}".encode()
            ).hexdigest()[:20]
        )
        for order, arm in enumerate(WILLIAMS[(task_position + 42) % 4], 1):
            old = next(row for row in old_rows if row["arm"] == arm)
            assignment_id = (
                "asg_"
                + hashlib.sha256(
                    f"phase_g_study/v1|{data['study_bundle']['study_id']}|P-1234ABCD|{task_id}|{arm}".encode()
                ).hexdigest()[:24]
            )
            blind_code = (
                "blind_"
                + hashlib.sha256(
                    f"{data['study_bundle']['study_id']}|P-1234ABCD|{task_id}|42|blind|{arm}".encode()
                ).hexdigest()[:8]
            )
            old_to_new[old["assignment_id"]] = {
                **old,
                "assignment_id": assignment_id,
                "score_id": "score_"
                + hashlib.sha256(assignment_id.encode("utf-8")).hexdigest()[:24],
                "participant_id": "P-1234ABCD",
                "participant_kind": "pseudonymous",
                "pair_id": pair_id,
                "order": order,
                "blind_arm_code": blind_code,
                "consent": {
                    "status": "consented",
                    "consented_at_utc": "2026-10-14T11:00:00Z",
                    "scope": "phase_g_study/v1",
                },
            }
    data["assignment_set"]["assignments"] = list(old_to_new.values())
    for observation in data["observations"]:
        replacement = old_to_new[observation["assignment_id"]]
        observation.update(
            {
                "assignment_id": replacement["assignment_id"],
                "participant_id": "P-1234ABCD",
                "pair_id": replacement["pair_id"],
            }
        )
    old_score_assignment = {row["score_id"]: row for row in old_assignments}
    for score in data["scores"]:
        replacement = old_to_new[old_score_assignment[score["score_id"]]["assignment_id"]]
        score.update(
            {
                "score_id": replacement["score_id"],
                "blinded_arm_code": replacement["blind_arm_code"],
                "status": "scored",
                **copy.deepcopy(GOOD),
            }
        )
    for run in data["runs"]:
        run["assignment_id"] = old_to_new[run["assignment_id"]]["assignment_id"]
        run["quality"] = copy.deepcopy(GOOD)
    data["usage_windows"] = []
    for arm in ("A", "B", "C_normal", "C_tuned"):
        data["usage_windows"].append(
            {
                "window_id": f"window-{arm}",
                "participant_id": "P-1234ABCD",
                "arm": arm,
                "assignment_ids": [
                    row["assignment_id"]
                    for row in data["assignment_set"]["assignments"]
                    if row["arm"] == arm
                ],
                "start_utc": "2026-10-14T11:59:00Z",
                "end_utc": "2026-10-14T12:02:00Z",
                "refreshed_at_utc": "2026-10-14T12:30:00Z",
                "source": "admin_export",
                "mapping_confidence": "high",
                "exclusions_recorded": True,
                "unrelated_activity": "none",
                "exclusion_codes": [],
                "bucket_missing_reasons": {},
                "usage_buckets": copy.deepcopy(ZERO_USAGE),
                "cost_amount": "2.00",
                "cost_missing_reason": None,
                "currency": "EUR",
            }
        )
    return data


def _assignment_for(data: dict[str, Any], task_id: str, arm: str) -> str:
    return next(
        row["assignment_id"]
        for row in data["assignment_set"]["assignments"]
        if row["task_id"] == task_id and row["arm"] == arm
    )


def test_report_has_all_cost_conditions_output_modes_and_no_enterprise_claim() -> None:
    report = economics.build_report(fixture())
    assert set(report["modeled_economics"]["conditions"]) == {
        "A",
        "B_full",
        "C_normal",
        "C_tuned",
        "B_no_source",
        "B_no_preparation",
        "B_no_retrieval",
        "B_no_compute",
        "B_no_artifact",
    }
    assert report["modeled_economics"]["conditions"]["B_full"]["costs"]["total_cost"] == "0.0004"
    assert (
        report["modeled_economics"]["output_contract_comparison"]["deterministic_artifact"][
            "artifact_reconstruction_successes"
        ]
        == 8
    )
    assert report["enterprise_observed"]["status"] == "pending"
    assert report["enterprise_observed"]["enterprise_usage_observed"] is False
    assert report["enterprise_observed"]["savings_claim"] == "none"
    assert report["savings_claim"] == "none"
    assert report["condition_profiles"]["C_normal"]["concise_instructions"] == "recorded-normal-v1"
    assert report["condition_profiles"]["C_normal"]["tool_settings"] == "recorded-native-tools-v1"
    assert report["sample"]["workload_outcomes_by_arm"]["B"]["unsupported_or_bypassed_share"] == "0"


def test_ablation_profile_must_change_only_its_predeclared_mechanism() -> None:
    data = fixture()
    profile = next(row for row in data["condition_profiles"] if row["condition_id"] == "B_no_compute")
    profile["preparation"] = "disabled"
    with pytest.raises(economics.EconomicsError, match="ablation_profile_not_single_mechanism"):
        economics.build_report(data)


def test_tuned_native_profile_matches_b_model_settings_effort_and_conciseness() -> None:
    data = fixture()
    profile = next(row for row in data["condition_profiles"] if row["condition_id"] == "C_tuned")
    profile["effort"] = "high"
    with pytest.raises(economics.EconomicsError, match="tuned_baseline_profile_mismatch"):
        economics.build_report(data)


def test_unhashable_mechanism_profile_has_sanitized_validation_error() -> None:
    data = fixture()
    profile = next(row for row in data["condition_profiles"] if row["condition_id"] == "A")
    profile["source_access"] = []
    with pytest.raises(economics.EconomicsError, match="condition_profile_mechanism_invalid"):
        economics.build_report(data)


def test_assignment_orders_and_derived_blind_aliases_are_recomputed() -> None:
    data = fixture()
    rows = data["assignment_set"]["assignments"]
    rows[0]["order"] = rows[1]["order"]
    with pytest.raises(economics.EconomicsError, match="assignment_order_invalid"):
        economics.build_report(data)

    data = fixture()
    data["assignment_set"]["assignments"][0]["blind_arm_code"] = "blind_00000000"
    with pytest.raises(economics.EconomicsError, match="assignment_derived_id_mismatch"):
        economics.build_report(data)

    data = fixture()
    data["assignment_set"]["seed"] += 1
    with pytest.raises(economics.EconomicsError, match="assignment_williams_order_invalid"):
        economics.build_report(data)


def test_assignment_set_requires_every_participant_task_pair() -> None:
    data = fixture()
    task_id = data["study_bundle"]["tasks"][0]["task_id"]
    data["assignment_set"]["assignments"] = [
        row for row in data["assignment_set"]["assignments"] if row["task_id"] != task_id
    ]
    with pytest.raises(
        economics.EconomicsError, match="assignment_participant_task_coverage_invalid"
    ):
        economics.build_report(data)


def test_synthetic_assignment_cannot_be_given_participant_quality_score() -> None:
    data = fixture()
    score = data["scores"][0]
    score.update({"status": "scored", **GOOD})
    with pytest.raises(
        economics.EconomicsError, match="synthetic_participant_quality_score_forbidden"
    ):
        economics.build_report(data)


def test_score_sheet_task_id_must_match_assignment_when_present() -> None:
    data = fixture()
    data["scores"][0]["task_id"] = "other-task"
    with pytest.raises(economics.EconomicsError, match="score_assignment_invalid"):
        economics.build_report(data)


def test_report_build_is_repeatable_and_does_not_mutate_input() -> None:
    data = fixture()
    before = json.dumps(data, sort_keys=True, separators=(",", ":"))
    first = economics.build_report(data)
    second = economics.build_report(data)
    assert first == second
    assert before == json.dumps(data, sort_keys=True, separators=(",", ":"))


def test_missing_request_ledger_does_not_turn_empty_completed_run_into_zero() -> None:
    data = fixture()
    run = next(row for row in data["runs"] if row["condition_id"] == "A")
    run["requests"] = []
    with pytest.raises(economics.EconomicsError, match="empty_request_ledger_for_model_run"):
        economics.build_report(data)

    run["request_ledger_status"] = "unknown"
    run["request_ledger_missing_reason"] = "measurement_error"
    report = economics.build_report(data)
    summary = report["modeled_economics"]["conditions"]["A"]["costs"]
    assert summary["total_cost"] is None
    assert summary["request_ledger_status"] == "partial_or_unknown"
    assert summary["unknown_cost_inputs"]["request_ledger_unknown:measurement_error"] == 1


def test_missing_service_cost_invalidates_cost_instead_of_becoming_zero() -> None:
    data = fixture()
    run = next(
        row
        for row in data["runs"]
        if row["condition_id"] == "B_full"
        and row["assignment_id"] == _assignment_for(data, "task-simple", "B")
    )
    run["service_cost_status"] = "unknown"
    run["service_cost_missing_reason"] = "not_available_from_export"
    report = economics.build_report(data)
    summary = report["modeled_economics"]["conditions"]["B_full"]["costs"]
    assert summary["cost_coverage_count"] == 1
    assert summary["total_cost"] is None
    assert (
        summary["unknown_cost_inputs"]["service_cost_status_unknown:not_available_from_export"] == 1
    )


def test_decimal_aggregation_preserves_tiny_charge_next_to_maximum_amount() -> None:
    data = fixture()
    run = next(row for row in data["runs"] if row["condition_id"] == "A")
    run["incremental_service_costs"] = [
        {
            "cost_id": "large-charge",
            "category": "hosting",
            "amount": "1000000000000",
            "currency": "USD",
            "missing_reason": None,
            "included_in_provider_cost": False,
        },
        {
            "cost_id": "tiny-charge",
            "category": "local_execution",
            "amount": "0.00000001",
            "currency": "USD",
            "missing_reason": None,
            "included_in_provider_cost": False,
        },
    ]
    report = economics.build_report(data)
    assert report["modeled_economics"]["conditions"]["A"]["costs"]["total_cost"] == (
        "1000000000000.00040001"
    )


def test_mixed_currency_service_charge_is_rejected() -> None:
    data = fixture()
    run = next(row for row in data["runs"] if row["condition_id"] == "A")
    run["incremental_service_costs"] = [
        {
            "cost_id": "compute-1",
            "category": "local_execution",
            "amount": "1",
            "currency": "EUR",
            "missing_reason": None,
            "included_in_provider_cost": False,
        }
    ]
    with pytest.raises(economics.EconomicsError, match="service_cost_currency_mismatch"):
        economics.build_report(data)


def test_weighted_cost_success_rate_includes_zero_success_stratum() -> None:
    data = real_evidence_fixture()
    failed_task = "task-simple"
    for assignment in data["assignment_set"]["assignments"]:
        if assignment["task_id"] != failed_task:
            continue
        assignment_id = assignment["assignment_id"]
        observation = next(
            row for row in data["observations"] if row["assignment_id"] == assignment_id
        )
        observation["status"] = "failed"
        observation["failure_code"] = "fixture_failure"
        observation["metrics"]["retry_count"] = 2
        score = next(row for row in data["scores"] if row["score_id"] == assignment["score_id"])
        score.update(UNSCORED)
        score["status"] = "no_score"
        for run in data["runs"]:
            if run["assignment_id"] == assignment_id:
                run["status"] = "failed"
                run["retry_count"] = 2
                run["quality"] = copy.deepcopy(UNSCORED)
    report = economics.build_report(data)
    weighted = report["modeled_economics"]["conditions"]["C_normal"]["weighted"]
    assert weighted["by_stratum"]["simple"]["successful_count"] == 0
    assert weighted["weighted_success_rate"] == "0.5"
    assert weighted["weighted_cost_per_successful_task"] == "0.0004"


def test_pair_mismatch_excludes_delta_but_keeps_cost_coverage() -> None:
    data = fixture()
    assignment_id = _assignment_for(data, "task-simple", "A")
    obs = next(row for row in data["observations"] if row["assignment_id"] == assignment_id)
    obs["versions"]["settings"] = "settings-other"
    run = next(
        row
        for row in data["runs"]
        if row["assignment_id"] == assignment_id and row["condition_id"] == "A"
    )
    run["settings"] = "settings-other"
    report = economics.build_report(data)
    paired = next(
        row
        for row in report["modeled_economics"]["paired_comparisons"]
        if row["reference_condition"] == "A"
    )
    assert paired["pair_count"] == 2
    assert paired["matched_pair_count"] == 1
    assert paired["mismatch_or_unavailable_reasons"]["settings_mismatch"] == 1
    assert report["modeled_economics"]["conditions"]["A"]["costs"]["cost_coverage_count"] == 2


def test_missing_source_snapshot_suppresses_pair_but_keeps_cost_coverage() -> None:
    data = fixture()
    assignment_id = _assignment_for(data, "task-simple", "A")
    observation = next(row for row in data["observations"] if row["assignment_id"] == assignment_id)
    observation["source_snapshot_sha256"] = None
    run = next(
        row
        for row in data["runs"]
        if row["assignment_id"] == assignment_id and row["condition_id"] == "A"
    )
    run["source_snapshot_sha256"] = None
    report = economics.build_report(data)
    paired = next(
        row
        for row in report["modeled_economics"]["paired_comparisons"]
        if row["reference_condition"] == "A"
    )
    assert paired["matched_pair_count"] == 1
    assert paired["mismatch_or_unavailable_reasons"]["source_snapshot_mismatch"] == 1
    assert report["modeled_economics"]["conditions"]["A"]["costs"]["cost_coverage_count"] == 2


def test_more_than_one_model_is_fully_costed_by_its_own_card() -> None:
    data = fixture()
    assignment_id = _assignment_for(data, "task-simple", "C_normal")
    run = next(
        row
        for row in data["runs"]
        if row["assignment_id"] == assignment_id and row["condition_id"] == "C_normal"
    )
    run["requests"].append(
        {
            "request_id": "second-call",
            "model": "model-beta",
            "usage": copy.deepcopy(ZERO_USAGE),
            "missing_reasons": {},
        }
    )
    run["requests"][0]["usage"] = dict.fromkeys(ZERO_USAGE, 0)
    obs = next(row for row in data["observations"] if row["assignment_id"] == assignment_id)
    obs["versions"]["model"] = None
    run["model"] = None
    next(row for row in data["condition_profiles"] if row["condition_id"] == "C_normal")["model"] = None
    # M6 reports the task-level sum, while the per-request sidecar preserves each model's units.
    obs["provider_usage"] = copy.deepcopy(ZERO_USAGE)
    data["rate_cards"].append(
        {
            "model": "model-beta",
            "currency": "USD",
            "denominator": "per_1m_tokens",
            "rates": dict.fromkeys(
                (
                    "uncached_input",
                    "cache_write_5m",
                    "cache_write_1h",
                    "cache_read",
                    "billed_output",
                ),
                "2",
            ),
        }
    )
    report = economics.build_report(data)
    costs = report["modeled_economics"]["conditions"]["C_normal"]["costs"]
    assert costs["cost_coverage_count"] == 2
    assert costs["total_cost"] == "0.0006"
    paired = next(
        row
        for row in report["modeled_economics"]["paired_comparisons"]
        if row["reference_condition"] == "C_normal"
    )
    assert paired["matched_pair_count"] == 0
    assert paired["mismatch_or_unavailable_reasons"]["condition_profile_unknown"] == 2


def test_unknown_model_rate_never_becomes_zero() -> None:
    data = fixture()
    run = next(row for row in data["runs"] if row["condition_id"] == "B_full")
    run["requests"][0]["model"] = "unpriced-model"
    report = economics.build_report(data)
    costs = report["modeled_economics"]["conditions"]["B_full"]["costs"]
    assert costs["total_cost"] is None
    assert costs["unknown_cost_inputs"]["rate_missing:unpriced-model"] == 1


def test_failed_attempt_costs_remain_in_assigned_and_successful_denominators() -> None:
    data = real_evidence_fixture()
    assignment_id = _assignment_for(data, "task-simple", "B")
    observation = next(row for row in data["observations"] if row["assignment_id"] == assignment_id)
    observation["status"] = "failed"
    observation["failure_code"] = "fixture_failure"
    observation["metrics"]["retry_count"] = 2
    score = next(
        row
        for row in data["scores"]
        if row["score_id"]
        == next(
            item["score_id"]
            for item in data["assignment_set"]["assignments"]
            if item["assignment_id"] == assignment_id
        )
    )
    score.update(UNSCORED)
    score["status"] = "no_score"
    run = next(
        row
        for row in data["runs"]
        if row["assignment_id"] == assignment_id and row["condition_id"] == "B_full"
    )
    run["status"] = "failed"
    run["retry_count"] = 2
    run["quality"] = copy.deepcopy(UNSCORED)
    report = economics.build_report(data)
    costs = report["modeled_economics"]["conditions"]["B_full"]["costs"]
    assert costs["assigned_count"] == 2
    assert costs["successful_count"] == 1
    assert costs["total_cost"] == "0.0004"
    assert costs["cost_per_successful_task"] == "0.0004"
    assert costs["retry_count"] == 2


def test_paired_cost_difference_keeps_negative_sign() -> None:
    data = fixture()
    assignment_id = _assignment_for(data, "task-simple", "C_normal")
    observation = next(row for row in data["observations"] if row["assignment_id"] == assignment_id)
    run = next(
        row
        for row in data["runs"]
        if row["assignment_id"] == assignment_id and row["condition_id"] == "C_normal"
    )
    usage = copy.deepcopy(ZERO_USAGE)
    usage["uncached_input_tokens"] = 500
    observation["provider_usage"] = usage
    run["requests"][0]["usage"] = usage
    report = economics.build_report(data)
    paired = next(
        row
        for row in report["modeled_economics"]["paired_comparisons"]
        if row["reference_condition"] == "C_normal"
    )
    assert paired["signed_candidate_minus_reference_cost_mean"] == "-0.0002"


def test_zero_success_denominator_is_unavailable_not_zero() -> None:
    data = fixture()
    for assignment in data["assignment_set"]["assignments"]:
        assignment_id = assignment["assignment_id"]
        observation = next(
            row for row in data["observations"] if row["assignment_id"] == assignment_id
        )
        observation["status"] = "failed"
        observation["failure_code"] = "fixture_failure"
        score = next(row for row in data["scores"] if row["score_id"] == assignment["score_id"])
        score.update(UNSCORED)
        score["status"] = "no_score"
        for run in data["runs"]:
            if run["assignment_id"] == assignment_id:
                run["status"] = "failed"
                run["quality"] = copy.deepcopy(UNSCORED)
    report = economics.build_report(data)
    costs = report["modeled_economics"]["conditions"]["A"]["costs"]
    assert costs["total_cost"] == "0.0004"
    assert costs["cost_per_successful_task"] is None
    assert costs["cost_per_successful_unavailable_reason"] == "no_successful_tasks"


def test_loader_enforces_byte_limit_and_recursion_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(economics, "MAX_INPUT_BYTES", 8)
    too_large = tmp_path / "large.json"
    too_large.write_bytes(b"{" + b" " * 8 + b"}")
    with pytest.raises(economics.EconomicsError, match="input_too_large"):
        economics._safe_load(too_large)
    monkeypatch.setattr(economics, "MAX_INPUT_BYTES", 16 * 1024 * 1024)
    nested = tmp_path / "recursive.json"
    nested.write_text("[" * 1200 + "0" + "]" * 1200, encoding="utf-8")
    with pytest.raises(economics.EconomicsError, match="input_invalid"):
        economics._safe_load(nested)


def test_duplicate_json_keys_and_nonfinite_numbers_have_sanitized_errors(tmp_path: Path) -> None:
    for text in ('{"x":1,"x":2}', '{"x":NaN}'):
        path = tmp_path / "bad.json"
        path.write_text(text, encoding="utf-8")
        with pytest.raises(economics.EconomicsError, match="input_invalid"):
            economics._safe_load(path)


def test_real_admin_windows_report_observed_per_arm_cost_without_allocating_tasks() -> None:
    report = economics.build_report(real_evidence_fixture())
    observed = report["enterprise_observed"]
    assert observed["status"] == "available_with_attribution_limits"
    assert observed["enterprise_usage_observed"] is True
    assert observed["causal_claim"] is False
    assert observed["savings_claim"] == "none"
    assert observed["by_arm"]["B"]["assigned_count"] == 2
    assert observed["by_arm"]["B"]["successful_quality_scored_count"] == 2
    assert observed["by_arm"]["B"]["window_cost_total"] == "2"
    assert observed["by_arm"]["B"]["cost_per_assigned_task"] == "1"
    assert observed["by_arm"]["B"]["cost_per_successful_task"] == "1"
    assert observed["by_arm"]["B"]["usage_bucket_totals"]["uncached_input_tokens"] == "100"
    assert "participant_id" not in json.dumps(observed)


def test_missing_real_window_or_blind_scoring_never_gets_zero_cost_per_success() -> None:
    data = real_evidence_fixture()
    data["usage_windows"].pop()
    report = economics.build_report(data)
    assert report["enterprise_observed"]["status"] == "pending"
    assert report["enterprise_observed"]["by_arm"]["C_tuned"]["window_cost_total"] is None

    data = real_evidence_fixture()
    assignment = data["assignment_set"]["assignments"][0]
    observation = next(
        row for row in data["observations"] if row["assignment_id"] == assignment["assignment_id"]
    )
    observation["status"] = "completed"
    score = next(row for row in data["scores"] if row["score_id"] == assignment["score_id"])
    score.update(UNSCORED)
    score["status"] = "no_score"
    run = next(
        row
        for row in data["runs"]
        if row["assignment_id"] == assignment["assignment_id"]
        and row["condition_id"] == assignment["arm"]
    )
    run["quality"] = copy.deepcopy(UNSCORED)
    report = economics.build_report(data)
    observed = report["enterprise_observed"]
    assert observed["status"] == "partial"
    assert observed["reason"] == "blind_scoring_incomplete"
    assert observed["by_arm"][assignment["arm"]]["cost_per_successful_task"] is None
    assert (
        observed["by_arm"][assignment["arm"]]["cost_per_successful_unavailable_reason"]
        == "blind_scoring_incomplete"
    )


def test_mixed_currencies_across_arms_remain_separate() -> None:
    data = real_evidence_fixture()
    data["usage_windows"][0]["currency"] = "USD"
    report = economics.build_report(data)
    observed = report["enterprise_observed"]
    assert observed["status"] == "available_with_attribution_limits"
    assert observed["currency"] is None
    assert observed["by_arm"]["A"]["window_cost_total"] == "2"
    assert observed["by_arm"]["A"]["currency"] == "USD"


def test_documented_excluded_unrelated_activity_allows_limited_arm_cost() -> None:
    data = real_evidence_fixture()
    data["usage_windows"][0]["unrelated_activity"] = "present_excluded"
    data["usage_windows"][0]["exclusion_codes"] = ["unrelated_admin_activity"]
    report = economics.build_report(data)
    observed = report["enterprise_observed"]
    assert observed["status"] == "available_with_attribution_limits"
    assert observed["by_arm"]["A"]["attribution_documented"] is True
    assert observed["by_arm"]["A"]["window_cost_total"] == "2"


def test_usage_window_cannot_precede_consent() -> None:
    data = real_evidence_fixture()
    assignment_id = data["usage_windows"][0]["assignment_ids"][0]
    assignment = next(
        row
        for row in data["assignment_set"]["assignments"]
        if row["assignment_id"] == assignment_id
    )
    assignment["consent"]["consented_at_utc"] = "2026-10-14T12:00:00.250000Z"
    with pytest.raises(economics.EconomicsError, match="usage_window_precedes_consent"):
        economics.build_report(data)


def test_unreviewed_unrelated_activity_withholds_observed_arm_cost() -> None:
    data = real_evidence_fixture()
    data["usage_windows"][0]["unrelated_activity"] = "unknown"
    report = economics.build_report(data)
    observed = report["enterprise_observed"]
    assert observed["status"] == "pending"
    assert observed["reason"] == "mapping_or_exclusion_evidence_incomplete"
    assert observed["by_arm"]["A"]["window_cost_total"] is None


def test_fractional_timestamp_boundary_is_not_truncated() -> None:
    data = real_evidence_fixture()
    data["usage_windows"][0]["start_utc"] = "2026-10-14T12:00:00.500000Z"
    with pytest.raises(economics.EconomicsError, match="usage_window_does_not_contain_task"):
        economics.build_report(data)


def test_fixture_report_golden() -> None:
    fixture_path = Path(__file__).parents[2] / "desktop" / "mcp" / "phase-g-economics-fixture.json"
    report_path = Path(__file__).parents[2] / "desktop" / "mcp" / "phase-g-economics-report.json"
    data = json.loads(fixture_path.read_text(encoding="utf-8"))
    actual = json.dumps(economics.build_report(data), sort_keys=True, indent=2) + "\n"
    assert actual == report_path.read_text(encoding="utf-8")
