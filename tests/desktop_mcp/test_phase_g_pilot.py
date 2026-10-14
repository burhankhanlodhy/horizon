from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from desktop.mcp.phase_g_pilot import (
    _read_json,
    main,
    make_fixture_report,
    make_score_sheet,
    make_task_book,
)
from horizon.desktop_mcp.pilot_study import (
    ARM_IDS,
    PilotStudyError,
    build_assignments,
    build_report,
    digest_json,
    validate_assignment_set,
    validate_observations,
    validate_scores,
)

BUNDLE_PATH = Path(__file__).parents[2] / "desktop" / "mcp" / "phase_g_pilot_fixtures.json"
GOLDEN_REPORT_PATH = Path(__file__).parents[2] / "desktop" / "mcp" / "phase-g-pilot-report.json"
BUNDLE = json.loads(BUNDLE_PATH.read_text(encoding="utf-8"))
AS_OF = "2026-10-15T12:00:00Z"


def _assignments(participant: str = "syn-p-0001"):
    consent = (
        {participant: "2026-10-13T12:00:00Z"} if participant.startswith(("p_", "P-")) else None
    )
    return build_assignments(BUNDLE, [participant], seed=20261014, consented_at_utc=consent)


def _record(assignment: dict, *, status: str = "completed", input_tokens: int = 100) -> dict:
    failed = status in {"failed", "timed_out"}
    unsupported = status == "unsupported"
    return {
        "assignment_id": assignment["assignment_id"],
        "participant_id": assignment["participant_id"],
        "task_id": assignment["task_id"],
        "pair_id": assignment["pair_id"],
        "arm": assignment["arm"],
        "status": status,
        "start_utc": "2026-10-14T10:00:00Z",
        "end_utc": "2026-10-14T10:01:00Z",
        "locale": assignment["locale"],
        "versions": {
            "desktop": "test-desktop",
            "extension": "0.1.1",
            "model": "test-model",
            "settings": "default",
        },
        "counter_version": "fixture-counter-v1",
        "source_snapshot_sha256": digest_json(
            next(t for t in BUNDLE["tasks"] if t["task_id"] == assignment["task_id"])[
                "synthetic_sources"
            ]
        ),
        "metrics": {
            "latency_ms": 60000,
            "retry_count": 0,
            "tool_call_count": 1,
            "retrieval_bytes": 100,
            "estimated_input_tokens": input_tokens,
            "estimated_output_tokens": 10,
            "schema_instruction_tokens": 5,
        },
        "metric_missing_reasons": {},
        "provider_usage": {
            "uncached_input_tokens": 100,
            "cache_write_5m_tokens": 0,
            "cache_write_1h_tokens": 0,
            "cache_read_tokens": 0,
            "billed_output_tokens": 10,
        },
        "usage_missing_reasons": {},
        "failure_code": "synthetic_failure" if failed else None,
        "unsupported_code": "unsupported_fixture" if unsupported else None,
    }


def _scores(assignments: dict, observations: list[dict]) -> list[dict]:
    by_id = {row["assignment_id"]: row["status"] for row in observations}
    result = []
    for assignment in assignments["assignments"]:
        completed = by_id.get(assignment["assignment_id"]) == "completed"
        quality_allowed = assignment["participant_kind"] == "pseudonymous"
        scored = completed and quality_allowed
        result.append(
            {
                "score_id": assignment["score_id"],
                "blinded_arm_code": assignment["blind_arm_code"],
                "scorer_code": "scorer_a",
                "status": "scored" if scored else "no_score",
                "correctness": "correct" if scored else "unscored",
                "completeness": "complete" if scored else "unscored",
                "citations": "complete" if scored else "unscored",
                "unsupported_claim": "no" if scored else "unscored",
                "omission_codes": [],
            }
        )
    return result


def test_fixture_report_is_deterministic_and_makes_no_participant_or_savings_claim() -> None:
    first = make_fixture_report(BUNDLE)
    second = make_fixture_report(BUNDLE)
    assert first == second
    assert first == json.loads(GOLDEN_REPORT_PATH.read_text(encoding="utf-8"))
    assert first["schema_version"] == "phase_g_study/v1"
    assert first["evidence_class"] == "synthetic"
    assert first["arm_ids"] == list(ARM_IDS)
    assert first["fixture_rule_checks"]["status"] == "passed"
    assert first["blind_scoring"]["blind_scores_imported"] is False
    assert first["blind_scoring"]["quality_method"] == "not_scored"
    assert first["claims"]["savings_claim"] == "none"
    assert first["enterprise_observed"]["status"] == "pending"
    assert first["enterprise_observed"]["enterprise_usage_observed"] is False
    assert first["execution"]["model_api_calls"] == first["execution"]["network_calls"] == 0
    assert first["payload_estimate"]["paired_comparison"]["signed_token_delta_a_minus_b"] == 260
    assert (
        first["payload_estimate"]["paired_comparison"]["incomparable_reasons"]["arm_not_completed"]
        == 2
    )
    assert first["arms"]["A"]["outcomes"]["completed"] > 0
    assert first["arms"]["B"]["outcomes"]["failed"] == 1
    assert first["arms"]["A"]["outcomes"]["unsupported"] > 0
    assert first["arms"]["C_normal"]["outcomes"]["bypassed"] == 1
    assert first["arms"]["C_tuned"]["outcomes"]["not_run"] == 1
    assert first["arms"]["A"]["quality_pass_count"] is None
    assert first["arms"]["A"]["completed"] == first["arms"]["A"]["outcomes"]["completed"]
    assert "successful" not in first["arms"]["A"]


def test_fixture_cli_stdout_and_saved_report_match_committed_golden(tmp_path: Path, capsys) -> None:
    output_path = tmp_path / "fixture-report.json"
    assert main(["fixture-report", "--output", str(output_path)]) == 0
    expected = json.loads(GOLDEN_REPORT_PATH.read_text(encoding="utf-8"))
    assert json.loads(capsys.readouterr().out) == expected
    assert json.loads(output_path.read_text(encoding="utf-8")) == expected


def test_assignments_are_deterministic_paired_and_counterbalanced() -> None:
    first = _assignments()
    second = _assignments()
    assert first == second
    rows = first["assignments"]
    assert len(rows) == len(BUNDLE["tasks"]) * len(ARM_IDS)
    for task in BUNDLE["tasks"]:
        group = [row for row in rows if row["task_id"] == task["task_id"]]
        assert {row["arm"] for row in group} == set(ARM_IDS)
        assert {row["order"] for row in group} == {1, 2, 3, 4}
        assert len({row["pair_id"] for row in group}) == 1
    assert all(row["participant_kind"] == "synthetic" for row in rows)
    assert "prompt" not in json.dumps(first).casefold()


def test_real_participant_assignment_requires_explicit_consent() -> None:
    with pytest.raises(PilotStudyError, match="consent_required"):
        build_assignments(BUNDLE, ["p_0123456789ab"], seed=7)
    result = build_assignments(
        BUNDLE,
        ["p_0123456789ab"],
        seed=7,
        consented_at_utc={"p_0123456789ab": "2026-10-14T09:00:00Z"},
    )
    assert all(row["consent"]["status"] == "consented" for row in result["assignments"])
    assert all(row["consent"]["scope"] == "phase_g_study/v1" for row in result["assignments"])


def test_m7_generated_pseudonym_format_is_accepted_with_consent() -> None:
    participant = "P-12ABCDEF"
    result = build_assignments(
        BUNDLE,
        [participant],
        seed=11,
        consented_at_utc={participant: "2026-10-14T09:00:00Z"},
    )
    assert all(row["participant_kind"] == "pseudonymous" for row in result["assignments"])
    assert all(row["consent"]["status"] == "consented" for row in result["assignments"])


def test_import_retains_failures_bypasses_unsupported_and_signed_estimate_regressions() -> None:
    assignments = _assignments()
    observations = []
    bases = {"A": 100, "B": 70, "C_normal": 120, "C_tuned": 90}
    for assignment in assignments["assignments"]:
        status = "completed"
        if assignment["task_id"] == "numeric_lookup" and assignment["arm"] == "A":
            status = "failed"
        elif assignment["task_id"] == "small_no_gain" and assignment["arm"] == "B":
            status = "unsupported"
        elif assignment["task_id"] == "source_free" and assignment["arm"] == "C_normal":
            status = "bypassed"
        observations.append(
            _record(assignment, status=status, input_tokens=bases[assignment["arm"]])
        )
    scores = _scores(assignments, observations)
    report = build_report(BUNDLE, assignments, observations, scores, as_of_utc=AS_OF)
    assert report["arms"]["A"]["outcomes"]["failed"] == 1
    assert report["arms"]["B"]["outcomes"]["unsupported"] == 1
    assert report["arms"]["C_normal"]["outcomes"]["bypassed"] == 1
    assert report["payload_estimate"]["paired_comparison"]["signed_token_delta_a_minus_b"] == 30 * (
        len(BUNDLE["tasks"]) - 2
    )
    assert report["payload_estimate"]["paired_comparison"]["incomparable_pair_count"] == 2
    assert report["arms"]["A"]["quality"]["correctness"]["unscored"] == len(BUNDLE["tasks"])
    assert report["modeled_economics"]["status"] == "unavailable"
    assert report["enterprise_observed"]["status"] == "pending"


def test_observation_schema_rejects_raw_fields_and_missing_values_without_reason() -> None:
    assignments = _assignments()
    row = _record(assignments["assignments"][0])
    with pytest.raises(PilotStudyError, match="observation_invalid"):
        validate_observations(BUNDLE, assignments, [{**row, "raw_prompt": "secret"}])
    broken = copy.deepcopy(row)
    broken["metrics"]["estimated_output_tokens"] = None
    with pytest.raises(PilotStudyError, match="missing_value_requires_reason"):
        validate_observations(BUNDLE, assignments, [broken])


def test_blind_score_import_rejects_wrong_alias_and_keeps_no_score() -> None:
    assignments = _assignments()
    observations = [
        _record(row, status="failed" if row["arm"] == "A" else "completed")
        for row in assignments["assignments"]
    ]
    scores = _scores(assignments, observations)
    validated = validate_scores(BUNDLE, assignments, observations, scores)
    assert sum(row["status"] == "no_score" for row in validated) == len(BUNDLE["tasks"]) * len(
        ARM_IDS
    )
    invalid = copy.deepcopy(scores)
    invalid[0]["blinded_arm_code"] = "A"
    with pytest.raises(PilotStudyError, match="score_assignment_or_blind_code_invalid"):
        validate_scores(BUNDLE, assignments, observations, invalid)


def test_usage_windows_need_fresh_explicit_mappings_and_currency_for_cost() -> None:
    assignments = _assignments("p_0123456789ab")
    observations = [
        _record(row, input_tokens=70 if row["arm"] == "B" else 100)
        for row in assignments["assignments"]
    ]
    scores = _scores(assignments, observations)
    windows = []
    for arm in ARM_IDS:
        arm_rows = [row for row in assignments["assignments"] if row["arm"] == arm]
        windows.append(
            {
                "window_id": f"window_{arm}",
                "participant_id": "p_0123456789ab",
                "arm": arm,
                "assignment_ids": [row["assignment_id"] for row in arm_rows],
                "start_utc": "2026-10-14T10:00:00Z",
                "end_utc": "2026-10-14T10:10:00Z",
                "refreshed_at_utc": "2026-10-15T10:00:00Z",
                "source": "admin_export",
                "mapping_confidence": "high",
                "exclusions_recorded": True,
                "unrelated_activity": "none",
                "exclusion_codes": [],
                "bucket_missing_reasons": {},
                "usage_buckets": {
                    "uncached_input_tokens": 1000,
                    "cache_write_5m_tokens": 0,
                    "cache_write_1h_tokens": 0,
                    "cache_read_tokens": 0,
                    "billed_output_tokens": 100,
                },
                "cost_amount": "1.25",
                "cost_missing_reason": None,
                "currency": "USD",
            }
        )
    report = build_report(
        BUNDLE, assignments, observations, scores, as_of_utc=AS_OF, usage_windows=windows
    )
    assert report["enterprise_observed"]["status"] == "available_with_attribution_limits"
    assert report["enterprise_observed"]["enterprise_usage_observed"] is True
    assert report["enterprise_observed"]["causal_claim"] is False
    stale = copy.deepcopy(windows)
    stale[0]["refreshed_at_utc"] = "2026-10-14T10:30:00Z"
    stale_report = build_report(
        BUNDLE, assignments, observations, scores, as_of_utc=AS_OF, usage_windows=stale
    )
    assert stale_report["enterprise_observed"]["status"] == "pending"
    assert stale_report["enterprise_observed"]["enterprise_usage_observed"] is False


def test_task_book_has_runnable_synthetic_cards_without_answer_keys() -> None:
    book = make_task_book(BUNDLE)
    assert len(book["tasks"]) == len(BUNDLE["tasks"])
    assert all(
        task["operator_instruction"] and "source_snapshot_sha256" in task for task in book["tasks"]
    )
    assert all(
        "synthetic_check" not in task and "expected" not in json.dumps(task)
        for task in book["tasks"]
    )


def test_assignment_import_rechecks_williams_order_and_seeded_blind_alias() -> None:
    assignments = _assignments()
    tampered = copy.deepcopy(assignments)
    rows = [
        row for row in tampered["assignments"] if row["task_id"] == BUNDLE["tasks"][0]["task_id"]
    ]
    rows[0]["order"], rows[1]["order"] = rows[1]["order"], rows[0]["order"]
    with pytest.raises(PilotStudyError, match="assignment_order_seed_mismatch"):
        validate_assignment_set(BUNDLE, tampered)
    tampered = copy.deepcopy(assignments)
    tampered["assignments"][0]["blind_arm_code"] = "blind_forged"
    with pytest.raises(PilotStudyError, match="assignment_blind_code_seed_mismatch"):
        validate_assignment_set(BUNDLE, tampered)


def test_consent_must_precede_observation_and_import_rejects_unhashable_reason() -> None:
    participant = "p_0123456789ab"
    assignments = build_assignments(
        BUNDLE, [participant], seed=19, consented_at_utc={participant: "2026-10-14T11:00:00Z"}
    )
    row = _record(assignments["assignments"][0])
    with pytest.raises(PilotStudyError, match="consent_after_observation_start"):
        validate_observations(BUNDLE, assignments, [row])
    assignments = _assignments()
    row = _record(assignments["assignments"][0])
    row["metrics"]["estimated_output_tokens"] = None
    row["metric_missing_reasons"]["estimated_output_tokens"] = ["not_available_from_export"]
    with pytest.raises(PilotStudyError, match="missing_value_requires_reason"):
        validate_observations(BUNDLE, assignments, [row])


def test_synthetic_quality_and_usage_are_never_enterprise_evidence() -> None:
    assignments = _assignments()
    observations = [_record(row) for row in assignments["assignments"]]
    scores = _scores(assignments, observations)
    scores[0].update(
        status="scored",
        correctness="correct",
        completeness="complete",
        citations="complete",
        unsupported_claim="no",
    )
    with pytest.raises(PilotStudyError, match="synthetic_participant_quality_score_forbidden"):
        build_report(BUNDLE, assignments, observations, scores, as_of_utc=AS_OF)
    no_scores = _scores(assignments, observations)
    window = {
        "window_id": "window_syn",
        "participant_id": "syn-p-0001",
        "arm": "A",
        "assignment_ids": [
            row["assignment_id"] for row in assignments["assignments"] if row["arm"] == "A"
        ],
        "start_utc": "2026-10-14T09:00:00Z",
        "end_utc": "2026-10-14T12:00:00Z",
        "refreshed_at_utc": "2026-10-15T10:00:00Z",
        "source": "admin_export",
        "mapping_confidence": "high",
        "exclusions_recorded": True,
        "unrelated_activity": "none",
        "exclusion_codes": [],
        "bucket_missing_reasons": {},
        "usage_buckets": dict.fromkeys(
            (
                "uncached_input_tokens",
                "cache_write_5m_tokens",
                "cache_write_1h_tokens",
                "cache_read_tokens",
                "billed_output_tokens",
            ),
            0,
        ),
        "cost_amount": "0",
        "cost_missing_reason": None,
        "currency": "USD",
    }
    with pytest.raises(PilotStudyError, match="synthetic_usage_window_forbidden"):
        build_report(
            BUNDLE, assignments, observations, no_scores, as_of_utc=AS_OF, usage_windows=[window]
        )


def test_enterprise_usage_is_not_available_when_all_measurements_are_missing() -> None:
    assignments = _assignments("p_0123456789ab")
    observations = [_record(row) for row in assignments["assignments"]]
    scores = _scores(assignments, observations)
    windows = []
    for arm in ARM_IDS:
        arm_rows = [row for row in assignments["assignments"] if row["arm"] == arm]
        keys = (
            "uncached_input_tokens",
            "cache_write_5m_tokens",
            "cache_write_1h_tokens",
            "cache_read_tokens",
            "billed_output_tokens",
        )
        windows.append(
            {
                "window_id": f"window_{arm}",
                "participant_id": "p_0123456789ab",
                "arm": arm,
                "assignment_ids": [row["assignment_id"] for row in arm_rows],
                "start_utc": "2026-10-14T10:00:00Z",
                "end_utc": "2026-10-14T10:10:00Z",
                "refreshed_at_utc": "2026-10-15T10:00:00Z",
                "source": "admin_export",
                "mapping_confidence": "high",
                "exclusions_recorded": True,
                "unrelated_activity": "none",
                "exclusion_codes": [],
                "bucket_missing_reasons": dict.fromkeys(keys, "not_available_from_export"),
                "usage_buckets": dict.fromkeys(keys),
                "cost_amount": None,
                "cost_missing_reason": "not_available_from_export",
                "currency": None,
            }
        )
    report = build_report(
        BUNDLE, assignments, observations, scores, as_of_utc=AS_OF, usage_windows=windows
    )
    assert report["enterprise_observed"]["status"] == "pending"
    assert report["enterprise_observed"]["enterprise_usage_observed"] is False
    assert report["enterprise_observed"]["reason"] == "usage_measurements_unavailable"


def test_payload_comparison_withholds_delta_when_model_snapshot_or_counter_differs() -> None:
    assignments = _assignments("p_0123456789ab")
    observations = [
        _record(row, input_tokens=70 if row["arm"] == "B" else 100)
        for row in assignments["assignments"]
    ]
    for row in observations:
        if row["arm"] == "B" and row["task_id"] == BUNDLE["tasks"][0]["task_id"]:
            row["versions"]["model"] = "other-model"
        if row["arm"] == "B" and row["task_id"] == BUNDLE["tasks"][1]["task_id"]:
            row["counter_version"] = "other-counter"
    report = build_report(
        BUNDLE, assignments, observations, _scores(assignments, observations), as_of_utc=AS_OF
    )
    comparison = report["payload_estimate"]["paired_comparison"]
    assert comparison["comparable_pair_count"] == len(BUNDLE["tasks"]) - 2
    assert comparison["incomparable_reasons"] == {
        "model_or_settings_mismatch": 1,
        "counter_version_mismatch": 1,
    }
    assert comparison["signed_token_delta_a_minus_b"] == 30 * (len(BUNDLE["tasks"]) - 2)
    tampered = copy.deepcopy(observations)
    tampered[0]["source_snapshot_sha256"] = "0" * 64
    with pytest.raises(PilotStudyError, match="source_snapshot_mismatch"):
        build_report(
            BUNDLE, assignments, tampered, _scores(assignments, observations), as_of_utc=AS_OF
        )


def test_cli_json_loader_rejects_duplicate_nonfinite_deep_and_oversized_inputs(
    tmp_path: Path,
) -> None:
    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text('{"a":1,"a":2}', encoding="utf-8")
    with pytest.raises(PilotStudyError, match="input_json_invalid"):
        _read_json(duplicate)
    nonfinite = tmp_path / "nonfinite.json"
    nonfinite.write_text('{"a":NaN}', encoding="utf-8")
    with pytest.raises(PilotStudyError, match="input_json_invalid"):
        _read_json(nonfinite)
    deeply_nested = tmp_path / "deep.json"
    deeply_nested.write_text("[" * 2000 + "0" + "]" * 2000, encoding="utf-8")
    with pytest.raises(PilotStudyError, match="input_json_invalid"):
        _read_json(deeply_nested)
    oversized = tmp_path / "oversized.json"
    oversized.write_bytes(b" " * (8 * 1024 * 1024 + 1))
    with pytest.raises(PilotStudyError, match="input_too_large"):
        _read_json(oversized)


def test_cli_reports_malformed_import_as_bounded_code(tmp_path: Path, capsys) -> None:
    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text(
        '{"schema_version":"phase_g_study/v1","schema_version":"bad"}', encoding="utf-8"
    )
    assert main(["fixture-report", "--bundle", str(duplicate)]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "phase-g-pilot: input_json_invalid\n"


def test_cli_report_import_matches_library_report_golden(tmp_path: Path, capsys) -> None:
    participant = "p_0123456789ab"
    assignments = _assignments(participant)
    observations = [
        _record(row, input_tokens=70 if row["arm"] == "B" else 100)
        for row in assignments["assignments"]
    ]
    scores = _scores(assignments, observations)
    bundle_path = tmp_path / "bundle.json"
    assignments_path = tmp_path / "assignments.json"
    observations_path = tmp_path / "observations.json"
    scores_path = tmp_path / "scores.json"
    output_path = tmp_path / "report.json"
    for path, value in (
        (bundle_path, BUNDLE),
        (assignments_path, assignments),
        (observations_path, observations),
        (scores_path, scores),
    ):
        path.write_text(json.dumps(value), encoding="utf-8")
    exit_code = main(
        [
            "report",
            "--bundle",
            str(bundle_path),
            "--assignments",
            str(assignments_path),
            "--observations",
            str(observations_path),
            "--scores",
            str(scores_path),
            "--as-of-utc",
            AS_OF,
            "--output",
            str(output_path),
        ]
    )
    assert exit_code == 0
    cli_report = json.loads(capsys.readouterr().out)
    library_report = build_report(BUNDLE, assignments, observations, scores, as_of_utc=AS_OF)
    assert cli_report == library_report
    assert json.loads(output_path.read_text(encoding="utf-8")) == cli_report


def test_cli_assign_applies_consent_to_m7_pseudonym_code(tmp_path: Path, capsys) -> None:
    output_path = tmp_path / "m7-assignments.json"
    assert (
        main(
            [
                "assign",
                "--participant-code",
                "P-12ABCDEF",
                "--seed",
                "5",
                "--consent-at-utc",
                "2026-10-14T09:00:00Z",
                "--output",
                str(output_path),
            ]
        )
        == 0
    )
    result = json.loads(capsys.readouterr().out)
    assert all(row["participant_kind"] == "pseudonymous" for row in result["assignments"])
    assert all(row["consent"]["status"] == "consented" for row in result["assignments"])


def test_generated_score_sheet_is_fillable_and_report_accepts_it_directly(
    tmp_path: Path, capsys
) -> None:
    participant = "P-12ABCDEF"
    assignments = _assignments(participant)
    observations = [_record(row) for row in assignments["assignments"]]
    sheet = make_score_sheet(BUNDLE, assignments)
    assert all(row["status"] == "no_score" for row in sheet["scores"])
    assert all(row["correctness"] == "unscored" for row in sheet["scores"])
    bundle_path = tmp_path / "bundle.json"
    assignments_path = tmp_path / "assignments.json"
    observations_path = tmp_path / "observations.json"
    score_path = tmp_path / "scores.json"
    for path, value in (
        (bundle_path, BUNDLE),
        (assignments_path, assignments),
        (observations_path, observations),
        (score_path, sheet),
    ):
        path.write_text(json.dumps(value), encoding="utf-8")
    assert (
        main(
            [
                "report",
                "--bundle",
                str(bundle_path),
                "--assignments",
                str(assignments_path),
                "--observations",
                str(observations_path),
                "--scores",
                str(score_path),
                "--as-of-utc",
                AS_OF,
            ]
        )
        == 0
    )
    report = json.loads(capsys.readouterr().out)
    assert report["blind_scoring"]["unscored_count"] == len(sheet["scores"])
