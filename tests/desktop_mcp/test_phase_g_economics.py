from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from horizon.desktop_mcp import pilot_study

MODULE_PATH = Path(__file__).parents[2] / "desktop" / "mcp" / "phase_g_economics.py"
spec = importlib.util.spec_from_file_location("phase_g_economics", MODULE_PATH)
assert spec is not None and spec.loader is not None
economics = importlib.util.module_from_spec(spec)
spec.loader.exec_module(economics)

REPO = Path(__file__).parents[2]
FIXTURE_PATH = REPO / "desktop" / "mcp" / "phase-g-economics-fixture.json"
REPORT_PATH = REPO / "desktop" / "mcp" / "phase-g-economics-report.json"

GOOD = {
    "correctness": "correct",
    "completeness": "complete",
    "citations": "not_required",
    "unsupported_claim": "no",
}
UNSCORED = dict.fromkeys(GOOD, "unscored")
ARMS = ["A", "B", "C_normal", "C_tuned"]
ARM_CONDITION = {"A": "A", "B": "B_full", "C_normal": "C_normal", "C_tuned": "C_tuned"}
ZERO_USAGE = {
    "uncached_input_tokens": 100,
    "cache_write_5m_tokens": 20,
    "cache_write_1h_tokens": 0,
    "cache_read_tokens": 30,
    "billed_output_tokens": 50,
}
PARTICIPANTS = ("p_aaaaaaaaaa", "p_bbbbbbbbbb", "p_cccccccccc", "p_dddddddddd")
REAL_SEED_HEX = "ab" * 32
CONSENT_AT = "2026-10-14T11:00:00Z"
EXPORT_BYTES = b"fixture admin export v1\n"
EXPORT_SHA256 = hashlib.sha256(EXPORT_BYTES).hexdigest()
EXPORTS = [{"sha256": EXPORT_SHA256, "bytes": len(EXPORT_BYTES)}]


def amount(value: Any) -> Any:
    """Money is a placeholder-unit label under placeholder rates and a plain string otherwise."""
    return value["placeholder_units"] if isinstance(value, dict) else value


def _bundle(*, minimum: int = 1, target: int = 4) -> dict[str, Any]:
    return {
        "schema_version": "phase_g_study/v1",
        "bundle_kind": "synthetic_task_bundle",
        "study_id": "economics-test",
        "study_plan": {
            "target_participants": target,
            "minimum_completed_per_arm": minimum,
            "stopping_rule": "complete_predeclared_bundle_or_stop_at_target",
            "stratum_weights": {"simple": "0.5", "complex": "0.5"},
            "refresh_max_age_hours": 24,
        },
        "tasks": [
            {
                "task_id": f"t_{stratum}",
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
                "synthetic_observed": n,
            }
            for stratum, n in (("simple", 7), ("complex", 13))
        ],
    }


def _source_digest(task_id: str) -> str:
    return hashlib.sha256(f"source-for-{task_id}".encode()).hexdigest()


def _rate_card(
    model: str = "model-alpha",
    *,
    source: str = "placeholder",
    currency: str = "USD",
    rate: str = "1",
) -> dict[str, Any]:
    return {
        "model": model,
        "currency": None if source == "placeholder" else currency,
        "denominator": "per_1m_tokens",
        "rates": dict.fromkeys(
            ("uncached_input", "cache_write_5m", "cache_write_1h", "cache_read", "billed_output"),
            rate,
        ),
        "rate_source": source,
        "as_of": "2026-10-14",
    }


def _profiles() -> list[dict[str, Any]]:
    full_profile = {
        "condition_id": "B_full",
        "model": "model-alpha",
        "settings": "settings-v1",
        "effort": "medium",
        "concise_instructions": "concise-v1",
        "tool_settings": "tool-settings-v1",
        **dict.fromkeys(economics.MECHANISMS, "enabled"),
    }
    return [
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


def _observation(row: dict[str, Any], *, usage: dict[str, int]) -> dict[str, Any]:
    return {
        "assignment_id": row["assignment_id"],
        "participant_id": row["participant_id"],
        "task_id": row["task_id"],
        "pair_id": row["pair_id"],
        "arm": row["arm"],
        "status": "completed",
        "start_utc": "2026-10-14T12:00:00Z",
        "end_utc": "2026-10-14T12:01:00Z",
        "locale": "en-US",
        "versions": {
            "desktop": "0.1.1",
            "extension": "0.1.1",
            "model": "model-alpha",
            "settings": "settings-v1",
        },
        "counter_version": "counter-v1",
        "source_snapshot_sha256": _source_digest(row["task_id"]),
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
        "provider_usage": copy.deepcopy(usage),
        "usage_missing_reasons": {},
        "failure_code": None,
        "unsupported_code": None,
    }


def _score(row: dict[str, Any], quality: dict[str, str], status: str) -> dict[str, Any]:
    return {
        "score_id": row["score_id"],
        "blinded_arm_code": row["blind_arm_code"],
        "scorer_code": "scorer-01",
        "status": status,
        **copy.deepcopy(quality),
        "omission_codes": [],
    }


def _base_run(
    row: dict[str, Any],
    condition: str,
    usage: dict[str, int],
    mode: str = "generated",
    quality: dict[str, str] = UNSCORED,
) -> dict[str, Any]:
    is_artifact = mode == "deterministic_artifact"
    return {
        "run_id": f"run-{row['assignment_id']}-{condition}",
        "assignment_id": row["assignment_id"],
        "condition_id": condition,
        "source_snapshot_sha256": _source_digest(row["task_id"]),
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
                "request_id": f"req-{row['assignment_id']}-{condition}",
                "model": "model-alpha",
                "usage": copy.deepcopy(usage),
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


def _windows(
    rows: list[dict[str, Any]], cost_for: Any, *, export_sha256: str = EXPORT_SHA256
) -> list[dict[str, Any]]:
    windows = []
    for participant in sorted({row["participant_id"] for row in rows}):
        for arm in ARMS:
            windows.append(
                {
                    "window_id": f"window-{participant}-{arm}",
                    "participant_id": participant,
                    "arm": arm,
                    "assignment_ids": [
                        row["assignment_id"]
                        for row in rows
                        if row["participant_id"] == participant and row["arm"] == arm
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
                    "cost_amount": cost_for(participant, arm),
                    "cost_missing_reason": None,
                    "currency": "EUR",
                    "export_sha256": export_sha256,
                }
            )
    return windows


def fixture() -> dict[str, Any]:
    bundle = _bundle()
    assignment_set = pilot_study.build_assignments(bundle, ["syn-p-abcd"], seed=42)
    rows = assignment_set["assignments"]
    observations: list[dict[str, Any]] = []
    scores: list[dict[str, Any]] = []
    runs: list[dict[str, Any]] = []
    for task in bundle["tasks"]:
        task_id, stratum = task["task_id"], task["stratum"]
        arm_rows = {row["arm"]: row for row in rows if row["task_id"] == task_id}
        for arm in ARMS:
            observations.append(_observation(arm_rows[arm], usage=ZERO_USAGE))
            scores.append(_score(arm_rows[arm], UNSCORED, "no_score"))

        def make_run(
            assignment_id: str,
            condition: str,
            mode: str = "generated",
            *,
            task_id: str = task_id,
            stratum: str = stratum,
        ) -> dict[str, Any]:
            run = _base_run(
                {"assignment_id": assignment_id, "task_id": task_id}, condition, ZERO_USAGE, mode
            )
            run["run_id"] = f"run-{stratum}-{condition}"
            run["requests"][0]["request_id"] = f"req-{stratum}-{condition}"
            return run

        for arm, condition in (
            ("A", "A"),
            ("B", "B_full"),
            ("C_normal", "C_normal"),
            ("C_tuned", "C_tuned"),
        ):
            mode = "deterministic_artifact" if condition == "B_full" else "generated"
            runs.append(make_run(arm_rows[arm]["assignment_id"], condition, mode))
        for condition in (
            "B_no_source",
            "B_no_preparation",
            "B_no_retrieval",
            "B_no_compute",
            "B_no_artifact",
        ):
            runs.append(
                make_run(
                    arm_rows["B"]["assignment_id"],
                    condition,
                    "generated"
                    if condition in {"B_no_source", "B_no_artifact"}
                    else "deterministic_artifact",
                )
            )

    return {
        "schema_version": "phase_g_economics/v1",
        "evidence_class": "synthetic",
        "as_of_utc": "2026-10-14T13:00:00Z",
        "study_bundle": bundle,
        "assignment_set": assignment_set,
        "observations": observations,
        "scores": scores,
        "usage_windows": [],
        "runs": runs,
        "condition_profiles": _profiles(),
        "rate_cards": [_rate_card()],
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
    """The synthetic fixture re-keyed to one committed-secret pseudonymous participant."""
    data = fixture()
    data["evidence_class"] = "consented_deidentified"
    participant = "p_abcdefghjk"
    cohort = pilot_study.build_assignments(
        data["study_bundle"],
        [participant],
        seed_hex=REAL_SEED_HEX,
        consented_at_utc={participant: CONSENT_AT},
    )
    new_rows = {(row["task_id"], row["arm"]): row for row in cohort["assignments"]}
    old_rows = data["assignment_set"]["assignments"]
    old_to_new = {old["assignment_id"]: new_rows[(old["task_id"], old["arm"])] for old in old_rows}
    data["assignment_set"] = cohort
    for observation in data["observations"]:
        replacement = old_to_new[observation["assignment_id"]]
        observation.update(
            {
                "assignment_id": replacement["assignment_id"],
                "participant_id": participant,
                "pair_id": replacement["pair_id"],
            }
        )
    old_by_score = {row["score_id"]: row for row in old_rows}
    for score in data["scores"]:
        replacement = old_to_new[old_by_score[score["score_id"]]["assignment_id"]]
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
    data["usage_windows"] = _windows(cohort["assignments"], lambda _participant, _arm: "2.00")
    return data


def cohort_input(
    deltas: tuple[int, ...] = (-1, -1, -1, -1),
    *,
    minimum: int = 2,
    target: int = 4,
    export_sha256: str = EXPORT_SHA256,
) -> dict[str, Any]:
    """Consented committed-secret cohort. Candidate B uses (2 + delta) million tokens per task.

    Reference C_normal uses 2 million tokens per task, so each participant's
    (candidate - reference) difference is ``delta`` per task.
    """
    participants = PARTICIPANTS[: len(deltas)]
    bundle = _bundle(minimum=minimum, target=target)
    cohort = pilot_study.build_assignments(
        bundle,
        list(participants),
        seed_hex=REAL_SEED_HEX,
        consented_at_utc=dict.fromkeys(participants, CONSENT_AT),
    )
    usage_millions: dict[str, dict[str, int]] = {}
    for participant, delta in zip(participants, deltas):
        usage_millions[participant] = {
            "A": 2_000_000,
            "C_normal": 2_000_000,
            "C_tuned": 2_000_000,
            "B": 2_000_000 + delta * 1_000_000,
        }
    observations: list[dict[str, Any]] = []
    scores: list[dict[str, Any]] = []
    runs: list[dict[str, Any]] = []
    for row in cohort["assignments"]:
        usage = {
            "uncached_input_tokens": usage_millions[row["participant_id"]][row["arm"]],
            "cache_write_5m_tokens": 0,
            "cache_write_1h_tokens": 0,
            "cache_read_tokens": 0,
            "billed_output_tokens": 0,
        }
        observations.append(_observation(row, usage=usage))
        scores.append(_score(row, GOOD, "scored"))
        runs.append(_base_run(row, ARM_CONDITION[row["arm"]], usage, quality=GOOD))

    def cost_for(participant: str, arm: str) -> str:
        tokens = 2 * usage_millions[participant][arm]
        return format(Decimal(tokens) / Decimal(1_000_000), "f")

    return {
        "schema_version": "phase_g_economics/v1",
        "evidence_class": "consented_deidentified",
        "as_of_utc": "2026-10-14T13:00:00Z",
        "study_bundle": bundle,
        "assignment_set": cohort,
        "observations": observations,
        "scores": scores,
        "usage_windows": _windows(cohort["assignments"], cost_for, export_sha256=export_sha256),
        "runs": runs,
        "condition_profiles": _profiles(),
        "rate_cards": [_rate_card(source="contract", currency="USD")],
        "sensitivity_scenarios": [
            {
                "scenario_id": "base-adoption",
                "adoption_fraction": "0.5",
                "rate_multiplier": "1",
                "cache_price_multiplier": "1",
            }
        ],
    }


def _assignment_for(data: dict[str, Any], task_id: str, arm: str) -> str:
    return next(
        row["assignment_id"]
        for row in data["assignment_set"]["assignments"]
        if row["task_id"] == task_id and row["arm"] == arm
    )


def _paired(report: dict[str, Any], reference: str) -> dict[str, Any]:
    return next(
        row
        for row in report["modeled_economics"]["paired_comparisons"]
        if row["reference_condition"] == reference
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
    assert (
        amount(report["modeled_economics"]["conditions"]["B_full"]["costs"]["total_cost"])
        == "0.0004"
    )
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
    profile = next(
        row for row in data["condition_profiles"] if row["condition_id"] == "B_no_compute"
    )
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

    # One participant's seeded permutation is trivial, so a seed change only moves the blind aliases.
    data = fixture()
    data["assignment_set"]["seed"] += 1
    with pytest.raises(economics.EconomicsError, match="assignment_derived_id_mismatch"):
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


def test_synthetic_evidence_cannot_claim_real_participant_or_admin_data() -> None:
    data = real_evidence_fixture()
    data["evidence_class"] = "synthetic"
    with pytest.raises(
        economics.EconomicsError, match="synthetic_evidence_contains_real_participants"
    ):
        economics.build_report(data)

    data = fixture()
    data["usage_windows"] = [{"window_id": "synthetic-window"}]
    with pytest.raises(economics.EconomicsError, match="synthetic_evidence_contains_admin_usage"):
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
        and row["assignment_id"] == _assignment_for(data, "t_simple", "B")
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
    data["rate_cards"] = [_rate_card(source="contract", currency="USD")]
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
    data["rate_cards"] = [_rate_card(source="contract", currency="USD")]
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
    failed_task = "t_simple"
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
    assert amount(weighted["weighted_cost_per_successful_task"]) == "0.0004"


def test_pair_mismatch_excludes_delta_but_keeps_cost_coverage() -> None:
    data = fixture()
    assignment_id = _assignment_for(data, "t_simple", "A")
    obs = next(row for row in data["observations"] if row["assignment_id"] == assignment_id)
    obs["versions"]["settings"] = "settings-other"
    run = next(
        row
        for row in data["runs"]
        if row["assignment_id"] == assignment_id and row["condition_id"] == "A"
    )
    run["settings"] = "settings-other"
    report = economics.build_report(data)
    paired = _paired(report, "A")
    assert paired["pair_count"] == 2
    assert paired["matched_pair_count"] == 1
    assert paired["mismatch_or_unavailable_reasons"]["settings_mismatch"] == 1
    assert report["modeled_economics"]["conditions"]["A"]["costs"]["cost_coverage_count"] == 2


def test_missing_source_snapshot_suppresses_pair_but_keeps_cost_coverage() -> None:
    data = fixture()
    assignment_id = _assignment_for(data, "t_simple", "A")
    observation = next(row for row in data["observations"] if row["assignment_id"] == assignment_id)
    observation["source_snapshot_sha256"] = None
    run = next(
        row
        for row in data["runs"]
        if row["assignment_id"] == assignment_id and row["condition_id"] == "A"
    )
    run["source_snapshot_sha256"] = None
    report = economics.build_report(data)
    paired = _paired(report, "A")
    assert paired["matched_pair_count"] == 1
    assert paired["mismatch_or_unavailable_reasons"]["source_snapshot_mismatch"] == 1
    assert report["modeled_economics"]["conditions"]["A"]["costs"]["cost_coverage_count"] == 2


def test_more_than_one_model_is_fully_costed_by_its_own_card() -> None:
    data = fixture()
    assignment_id = _assignment_for(data, "t_simple", "C_normal")
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
    next(row for row in data["condition_profiles"] if row["condition_id"] == "C_normal")[
        "model"
    ] = None
    # M6 reports the task-level sum, while the per-request sidecar preserves each model's units.
    obs["provider_usage"] = copy.deepcopy(ZERO_USAGE)
    data["rate_cards"].append(_rate_card("model-beta", rate="2"))
    report = economics.build_report(data)
    costs = report["modeled_economics"]["conditions"]["C_normal"]["costs"]
    assert costs["cost_coverage_count"] == 2
    assert amount(costs["total_cost"]) == "0.0006"
    paired = _paired(report, "C_normal")
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
    assignment_id = _assignment_for(data, "t_simple", "B")
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
    assert amount(costs["total_cost"]) == "0.0004"
    assert amount(costs["cost_per_successful_task"]) == "0.0004"
    assert costs["retry_count"] == 2


def test_point_estimate_cost_ratios_carry_their_denominator_count() -> None:
    data = real_evidence_fixture()
    costs = economics.build_report(data)["modeled_economics"]["conditions"]["B_full"]["costs"]
    assert costs["cost_per_successful_task_basis"] == {"kind": "point_estimate", "n": 2}
    assert costs["cost_per_assigned_task_basis"] == {"kind": "point_estimate", "n": 2}


def test_paired_cost_difference_keeps_negative_sign() -> None:
    data = fixture()
    assignment_id = _assignment_for(data, "t_simple", "C_normal")
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
    paired = _paired(report, "C_normal")
    assert amount(paired["signed_candidate_minus_reference_cost_mean"]) == "-0.0002"


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
    assert amount(costs["total_cost"]) == "0.0004"
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
    report = economics.build_report(real_evidence_fixture(), EXPORTS)
    observed = report["enterprise_observed"]
    assert observed["status"] == "available_with_attribution_limits"
    assert observed["enterprise_usage_observed"] is True
    assert observed["provenance_verified"] is True
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
    report = economics.build_report(data, EXPORTS)
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
        and row["condition_id"] == ARM_CONDITION[assignment["arm"]]
    )
    run["quality"] = copy.deepcopy(UNSCORED)
    report = economics.build_report(data, EXPORTS)
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
    report = economics.build_report(data, EXPORTS)
    observed = report["enterprise_observed"]
    assert observed["status"] == "available_with_attribution_limits"
    assert observed["currency"] is None
    assert observed["by_arm"]["A"]["window_cost_total"] == "2"
    assert observed["by_arm"]["A"]["currency"] == "USD"


def test_documented_excluded_unrelated_activity_allows_limited_arm_cost() -> None:
    data = real_evidence_fixture()
    data["usage_windows"][0]["unrelated_activity"] = "present_excluded"
    data["usage_windows"][0]["exclusion_codes"] = ["unrelated_admin_activity"]
    report = economics.build_report(data, EXPORTS)
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


# --- Statistical decision, provenance, rates, factorial, and pilot contract ----------------


def test_pilot_contract_shapes_validate_for_fixture_and_cohort() -> None:
    for data in (fixture(), cohort_input()):
        bundle = data["study_bundle"]
        pilot_study.validate_bundle(bundle)
        pilot_study.validate_assignment_set(bundle, data["assignment_set"])
        for task in bundle["tasks"]:
            assert pilot_study.TASK_ID.fullmatch(task["task_id"])
    cohort = cohort_input()
    assert cohort["assignment_set"]["seed_mode"] == "committed_secret"
    assert cohort["assignment_set"]["seed"] is None
    assert all(
        pilot_study.PSEUDONYM.fullmatch(row["participant_id"])
        for row in cohort["assignment_set"]["assignments"]
    )


def test_task_ids_outside_the_pilot_pattern_are_rejected() -> None:
    data = fixture()
    data["study_bundle"]["tasks"][0]["task_id"] = "task-simple"
    with pytest.raises(economics.EconomicsError, match="task_id_invalid"):
        economics.build_report(data)


def test_synthetic_fixture_decision_is_none_with_synthetic_reason() -> None:
    report = economics.build_report(fixture())
    decision = _paired(report, "C_normal")["decision"]
    assert decision["claim"] == "none"
    assert "synthetic_evidence" in decision["reasons"]
    assert report["savings_claim"] == "none"
    assert report["enterprise_observed"]["savings_claim"] == "none"


def test_consented_cohort_with_verified_exports_supports_savings() -> None:
    report = economics.build_report(cohort_input((-1, -1, -1, -1)), EXPORTS)
    decision = _paired(report, "C_normal")["decision"]
    assert decision == {
        "claim": "savings_supported",
        "reasons": [],
        "rule": decision["rule"],
    }
    assert report["provenance"]["provenance_verified"] is True
    assert report["savings_claim"] == "savings_supported"
    assert report["enterprise_observed"]["savings_claim"] == "savings_supported"
    assert report["enterprise_observed"]["enterprise_usage_observed"] is True
    assert report["modeled_economics"]["currency"] == "USD"


def test_unverified_or_missing_export_blocks_savings_and_enterprise_usage() -> None:
    cohort = cohort_input((-1, -1, -1, -1))
    missing = economics.build_report(cohort)
    decision = _paired(missing, "C_normal")["decision"]
    assert decision["claim"] == "none"
    assert "provenance_unverified" in decision["reasons"]
    assert missing["provenance"]["provenance_verified"] is False
    observed = missing["enterprise_observed"]
    assert observed["status"] == "admin_export_unverified"
    assert observed["enterprise_usage_observed"] is False
    assert observed["savings_claim"] == "none"
    assert missing["savings_claim"] == "none"

    other = [{"sha256": hashlib.sha256(b"other").hexdigest(), "bytes": 5}]
    mismatched = economics.build_report(cohort, other)
    assert mismatched["provenance"]["unverified_window_count"] == len(cohort["usage_windows"])
    assert mismatched["enterprise_observed"]["status"] == "admin_export_unverified"


def test_too_few_pairs_or_participants_name_the_gate() -> None:
    too_few_pairs = economics.build_report(cohort_input((-1, -1, -1, -1), minimum=100), EXPORTS)
    decision = _paired(too_few_pairs, "C_normal")["decision"]
    assert decision["claim"] == "none"
    assert "below_min_units" in decision["reasons"]

    too_few_participants = economics.build_report(cohort_input((-1, -1, -1, -1), target=8), EXPORTS)
    decision = _paired(too_few_participants, "C_normal")["decision"]
    assert decision["claim"] == "none"
    assert "below_min_clusters" in decision["reasons"]
    assert too_few_participants["savings_claim"] == "none"


def test_interval_spanning_zero_is_inconclusive() -> None:
    report = economics.build_report(cohort_input((-1, -1, 1, 1)), EXPORTS)
    decision = _paired(report, "C_normal")["decision"]
    assert decision["claim"] == "inconclusive"
    assert "interval_includes_zero" in decision["reasons"]
    interval = _paired(report, "C_normal")["interval"]
    assert Decimal(interval["lower"]) < 0 < Decimal(interval["upper"])
    assert report["savings_claim"] == "inconclusive"


def test_placeholder_rates_emit_no_currency_or_usd_anywhere() -> None:
    report = economics.build_report(fixture())
    text = json.dumps(report)
    assert "USD" not in text
    modeled = report["modeled_economics"]
    assert modeled["rates_are_placeholder"] is True
    assert modeled["amount_unit"] == "placeholder_units"
    assert modeled["currency"] is None
    assert all(card["currency"] is None for card in modeled["rate_cards"])
    assert modeled["conditions"]["B_full"]["costs"]["currency"] is None
    assert "placeholder_units" in modeled["conditions"]["B_full"]["costs"]["total_cost"]


def test_real_rates_carry_their_currency() -> None:
    report = economics.build_report(cohort_input(), EXPORTS)
    modeled = report["modeled_economics"]
    assert modeled["rates_are_placeholder"] is False
    assert modeled["amount_unit"] == "currency"
    assert modeled["currency"] == "USD"
    total = modeled["conditions"]["B_full"]["costs"]["total_cost"]
    assert isinstance(total, str) and total != ""
    assert modeled["rate_cards"] == [
        {
            "model": "model-alpha",
            "rate_source": "contract",
            "as_of": "2026-10-14",
            "currency": "USD",
        }
    ]


def test_rate_cards_require_source_and_as_of() -> None:
    data = fixture()
    del data["rate_cards"][0]["as_of"]
    with pytest.raises(economics.EconomicsError, match="rate_card_invalid"):
        economics.build_report(data)

    data = fixture()
    data["rate_cards"][0]["rate_source"] = "web_scrape"
    with pytest.raises(economics.EconomicsError, match="rate_source_invalid"):
        economics.build_report(data)

    data = fixture()
    data["rate_cards"][0]["as_of"] = "2026-13-40"
    with pytest.raises(economics.EconomicsError, match="rate_as_of_invalid"):
        economics.build_report(data)


def test_placeholder_card_cannot_carry_currency_and_cannot_mix_with_priced_cards() -> None:
    data = fixture()
    data["rate_cards"][0]["currency"] = "USD"
    with pytest.raises(economics.EconomicsError, match="placeholder_rate_currency_forbidden"):
        economics.build_report(data)

    data = fixture()
    data["rate_cards"].append(_rate_card("model-beta", source="contract", currency="USD"))
    with pytest.raises(economics.EconomicsError, match="rate_source_mixed"):
        economics.build_report(data)


def test_priced_service_cost_is_rejected_under_placeholder_rates() -> None:
    data = fixture()
    run = next(row for row in data["runs"] if row["condition_id"] == "A")
    run["incremental_service_costs"] = [
        {
            "cost_id": "hosting-1",
            "category": "hosting",
            "amount": "1",
            "currency": "USD",
            "missing_reason": None,
            "included_in_provider_cost": False,
        }
    ]
    with pytest.raises(
        economics.EconomicsError, match="service_cost_requires_non_placeholder_rates"
    ):
        economics.build_report(data)


def test_usage_window_requires_an_export_digest_for_real_evidence() -> None:
    data = real_evidence_fixture()
    data["usage_windows"][0]["export_sha256"] = None
    with pytest.raises(economics.EconomicsError, match="usage_window_export_digest_required"):
        economics.build_report(data)


def test_seed_mode_must_match_evidence_class() -> None:
    data = real_evidence_fixture()
    data["assignment_set"]["seed_mode"] = "public_synthetic"
    data["assignment_set"]["seed"] = 42
    data["assignment_set"]["seed_commitment"] = None
    with pytest.raises(economics.EconomicsError, match="seed_mode_evidence_mismatch"):
        economics.build_report(data)


def test_factorial_slice_in_fixture_reports_known_effects() -> None:
    data = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    factorial = economics.build_report(data)["factorial"]
    assert factorial["status"] == "analysed"
    assert factorial["n_units_analysed"] == 2
    assert factorial["arms"]["F00"]["n"] == 2
    effects = factorial["effects"]
    assert amount(effects["input_reduction"]["interval"]["estimate"]) == "-0.4"
    assert amount(effects["output_contract"]["interval"]["estimate"]) == "-0.2"
    assert Decimal(amount(effects["interaction"]["interval"]["estimate"])) == 0
    assert effects["interaction"]["decision"]["claim"] == "none"
    assert effects["input_reduction"]["decision"]["claim"] == "none"


def test_factorial_is_not_run_without_condition_runs() -> None:
    data = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    data["runs"] = [row for row in data["runs"] if not row["condition_id"].startswith("F")]
    assert economics.build_report(data)["factorial"] == {"status": "not_run"}


def _fixture_with_factorial_profile(condition: str) -> dict[str, Any]:
    data = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    return data, next(row for row in data["condition_profiles"] if row["condition_id"] == condition)


def test_mislabelled_factorial_profile_is_rejected() -> None:
    data, profile = _fixture_with_factorial_profile("F10")
    profile["input_reduction"] = False
    profile["preparation"] = "disabled"
    profile["retrieval"] = "disabled"
    with pytest.raises(economics.EconomicsError, match="factorial_condition_invalid"):
        economics.build_report(data)

    data, profile = _fixture_with_factorial_profile("F11")
    profile["output_contract"] = False  # flag contradicts the second digit
    with pytest.raises(economics.EconomicsError, match="factorial_condition_invalid"):
        economics.build_report(data)

    data, profile = _fixture_with_factorial_profile("F11")
    profile["output_contract"] = "yes"  # not a boolean
    with pytest.raises(economics.EconomicsError, match="factorial_condition_invalid"):
        economics.build_report(data)


def test_artifact_must_be_held_fixed_across_factorial_cells() -> None:
    data, profile = _fixture_with_factorial_profile("F10")
    profile["artifact"] = "enabled"
    with pytest.raises(economics.EconomicsError, match="factorial_condition_invalid"):
        economics.build_report(data)


def test_concise_instructions_must_partition_by_the_output_digit() -> None:
    data, profile = _fixture_with_factorial_profile("F10")
    profile["concise_instructions"] = _fixture_with_factorial_profile("F01")[1][
        "concise_instructions"
    ]
    with pytest.raises(economics.EconomicsError, match="factorial_condition_invalid"):
        economics.build_report(data)

    data, profile = _fixture_with_factorial_profile("F01")
    profile["concise_instructions"] = _fixture_with_factorial_profile("F00")[1][
        "concise_instructions"
    ]
    with pytest.raises(economics.EconomicsError, match="factorial_condition_invalid"):
        economics.build_report(data)


def test_model_or_effort_difference_across_factorial_cells_is_rejected() -> None:
    data, profile = _fixture_with_factorial_profile("F11")
    profile["model"] = "synthetic-other-model"
    with pytest.raises(economics.EconomicsError, match="factorial_condition_invalid"):
        economics.build_report(data)

    data, profile = _fixture_with_factorial_profile("F00")
    profile["effort"] = "high"
    with pytest.raises(economics.EconomicsError, match="factorial_condition_invalid"):
        economics.build_report(data)


def test_factorial_run_without_its_profile_is_rejected() -> None:
    data = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    data["condition_profiles"] = [
        row for row in data["condition_profiles"] if row["condition_id"] != "F10"
    ]
    with pytest.raises(economics.EconomicsError, match="factorial_profile_missing"):
        economics.build_report(data)


def test_admin_export_digest_is_recorded_without_path_and_through_cli(tmp_path: Path) -> None:
    export = tmp_path / "admin-export-private-name.csv"
    export.write_bytes(EXPORT_BYTES)
    input_path = tmp_path / "input-cohort.json"
    input_path.write_text(json.dumps(cohort_input((-1, -1, -1, -1))), encoding="utf-8")
    output = tmp_path / "report-out.json"
    assert (
        economics.main(
            ["--input", str(input_path), "--output", str(output), "--admin-export", str(export)]
        )
        == 0
    )
    text = output.read_text(encoding="utf-8")
    report = json.loads(text)
    assert report["provenance"]["admin_exports"] == [
        {"sha256": EXPORT_SHA256, "bytes": len(EXPORT_BYTES)}
    ]
    assert report["savings_claim"] == "savings_supported"
    assert str(tmp_path) not in text
    assert export.name not in text


def test_admin_export_read_is_bounded_and_missing_file_is_sanitized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    export = tmp_path / "export.csv"
    export.write_bytes(b"0123456789")
    monkeypatch.setattr(economics, "MAX_ADMIN_EXPORT_BYTES", 4)
    with pytest.raises(economics.EconomicsError, match="admin_export_too_large"):
        economics._read_admin_export(export)
    monkeypatch.setattr(economics, "MAX_ADMIN_EXPORT_BYTES", 64 * 1024 * 1024)
    with pytest.raises(economics.EconomicsError, match="admin_export_unavailable"):
        economics._read_admin_export(tmp_path / "missing.csv")


def test_fixture_report_golden() -> None:
    data = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    actual = json.dumps(economics.build_report(data), sort_keys=True, indent=2) + "\n"
    assert actual == REPORT_PATH.read_text(encoding="utf-8")
