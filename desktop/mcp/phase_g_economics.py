"""Offline, synthetic-first Phase G M13 cost and output-contract evaluator.

The study bundle and assignment set are checked by ``horizon.desktop_mcp.pilot_study``
(the M6 contract); savings decisions and factorial effects come from
``evidence_stats`` and ``factorial``. Per-request/per-model token usage is a
separate M13 sidecar; admin usage windows are never split across tasks or
requests. No network, provider, or account credentials are used.

Run with ``PYTHONPATH`` set to the repository root, or from the repository
root directly; this file adds the root to ``sys.path`` itself.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import re
import sys
from collections import Counter, defaultdict
from copy import deepcopy
from decimal import Decimal, InvalidOperation, localcontext
from pathlib import Path
from typing import Any

_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(_REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPOSITORY_ROOT))

from horizon.desktop_mcp import pilot_study  # noqa: E402
from horizon.desktop_mcp.evidence_stats import (  # noqa: E402
    Interval,
    PairedUnit,
    paired_difference_interval,
    savings_decision,
)
from horizon.desktop_mcp.factorial import FactorialCell, FactorialError, analyze_factorial  # noqa: E402

SCHEMA_VERSION = "phase_g_economics/v1"
STUDY_SCHEMA_VERSION = "phase_g_study/v1"
ARMS = ("A", "B", "C_normal", "C_tuned")
# Fixed reporting constant for the cluster bootstrap. It is deliberately not the
# allocation seed: committed real cohorts never reveal theirs in this report.
BOOTSTRAP_SEED = 20261014
# The primary savings comparison: candidate B_full against the recorded normal workflow.
PRIMARY_REFERENCE = "C_normal"
PRIMARY_CANDIDATE = "B_full"
FACTORIAL_CONDITIONS = ("F00", "F10", "F01", "F11")
FACTORIAL_FLAGS = ("input_reduction", "output_contract")
RATE_SOURCES = {"placeholder", "published_price_list", "contract"}
BASE_CONDITIONS = {"A": "A", "B_full": "B", "C_normal": "C_normal", "C_tuned": "C_tuned"}
ABLATIONS = {
    "B_no_source",
    "B_no_preparation",
    "B_no_retrieval",
    "B_no_compute",
    "B_no_artifact",
}
CONDITIONS = set(BASE_CONDITIONS) | ABLATIONS
MECHANISMS = ("source_access", "preparation", "retrieval", "compute", "artifact")
PROFILE_FIELDS = (
    "condition_id",
    "model",
    "settings",
    "effort",
    "concise_instructions",
    "tool_settings",
    *MECHANISMS,
)
ABLATION_MECHANISM = {
    "B_no_source": "source_access",
    "B_no_preparation": "preparation",
    "B_no_retrieval": "retrieval",
    "B_no_compute": "compute",
    "B_no_artifact": "artifact",
}
USAGE_BUCKETS = (
    "uncached_input_tokens",
    "cache_write_5m_tokens",
    "cache_write_1h_tokens",
    "cache_read_tokens",
    "billed_output_tokens",
)
RATE_KEYS = {
    "uncached_input_tokens": "uncached_input",
    "cache_write_5m_tokens": "cache_write_5m",
    "cache_write_1h_tokens": "cache_write_1h",
    "cache_read_tokens": "cache_read",
    "billed_output_tokens": "billed_output",
}
MISSING_REASONS = {
    "not_collected",
    "not_available_from_export",
    "not_applicable",
    "counter_unavailable",
    "not_run",
    "measurement_error",
    "withheld",
}
OUTCOMES = {"completed", "failed", "unsupported", "bypassed", "refused", "timed_out", "not_run"}
SCORE_VALUES = {
    "correctness": {"correct", "partial", "incorrect", "unscored"},
    "completeness": {"complete", "partial", "incomplete", "unscored"},
    "citations": {"complete", "partial", "missing", "not_required", "unscored"},
    "unsupported_claim": {"yes", "no", "unknown", "unscored"},
}
SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$")
TASK_ID = re.compile(r"^t_[a-z0-9_]{1,40}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
DECIMAL_TEXT = re.compile(r"^(?:0|[1-9][0-9]{0,17})(?:\.[0-9]{1,8})?$")
CURRENCY = re.compile(r"^[A-Z]{3}$")
ISO_DATE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
SYNTHETIC_PARTICIPANT = re.compile(r"^syn-p-[a-z0-9]{4,24}$")
PSEUDONYM = re.compile(r"^p_[a-z2-7]{10}$")
# Admin exports are hashed, never parsed: a bounded read keeps memory fixed.
MAX_ADMIN_EXPORT_BYTES = 64 * 1024 * 1024
# Keys whose values are money amounts; placeholder rates relabel them as units.
PLACEHOLDER_AMOUNT_KEYS = frozenset(
    {
        "total_cost",
        "cost_per_assigned_task",
        "cost_per_successful_task",
        "weighted_cost_per_assigned_task",
        "weighted_cost_per_successful_task",
        "weighted_expected_cost_per_assigned_task",
        "signed_difference_vs_native_normal",
        "signed_candidate_minus_reference_cost_mean",
        "modeled_total_cost",
        "mean_cost",
        "estimate",
        "lower",
        "upper",
    }
)
# M6 contract error codes are reported with the economics vocabulary.
PILOT_ASSIGNMENT_CODES = {
    "assignment_order_seed_mismatch": "assignment_williams_order_invalid",
    "assignment_blind_code_seed_mismatch": "assignment_derived_id_mismatch",
    "assignment_id_invalid": "assignment_derived_id_mismatch",
    "pair_id_invalid": "assignment_derived_id_mismatch",
    "assignment_pair_missing": "assignment_participant_task_coverage_invalid",
    "assignment_arm_or_order_missing": "assignment_arm_set_invalid",
    "assignment_study_mismatch": "assignment_contract_mismatch",
    "assignment_bundle_or_arm_mismatch": "assignment_contract_mismatch",
}
MAX_INPUT_BYTES = 16 * 1024 * 1024
MAX_ASSIGNMENTS = 100_000
MAX_RUNS = 500_000
MAX_REQUESTS_PER_RUN = 10_000
MAX_COUNT = 10**12


class EconomicsError(ValueError):
    """Stable validation error that never includes input values."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _keys(value: Any, expected: set[str], code: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != expected:
        raise EconomicsError(code)
    return value


def _identifier(value: Any, code: str = "identifier_invalid") -> str:
    if not isinstance(value, str) or SAFE_ID.fullmatch(value) is None:
        raise EconomicsError(code)
    return value


def _integer(value: Any, *, high: int = MAX_COUNT, code: str = "integer_invalid") -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= high:
        raise EconomicsError(code)
    return value


def _decimal(value: Any, *, code: str = "decimal_invalid", high: Decimal | None = None) -> Decimal:
    if not isinstance(value, str) or DECIMAL_TEXT.fullmatch(value) is None:
        raise EconomicsError(code)
    try:
        parsed = Decimal(value)
    except InvalidOperation:
        raise EconomicsError(code) from None
    if not parsed.is_finite() or (high is not None and parsed > high):
        raise EconomicsError(code)
    return parsed


def _datetime_utc(value: Any):
    if not isinstance(value, str) or len(value) > 40:
        raise EconomicsError("timestamp_invalid")
    from datetime import datetime, timezone

    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
            raise EconomicsError("timestamp_invalid")
        return parsed.astimezone(timezone.utc)
    except (OverflowError, OSError, ValueError):
        raise EconomicsError("timestamp_invalid") from None


def _utc(value: Any) -> str:
    parsed = _datetime_utc(value)
    precision = "microseconds" if parsed.microsecond else "seconds"
    return parsed.isoformat(timespec=precision).replace("+00:00", "Z")


def _safe_load(path: Path) -> Any:
    try:
        with path.open("rb") as handle:
            raw = handle.read(MAX_INPUT_BYTES + 1)
    except OSError:
        raise EconomicsError("input_unavailable") from None
    if len(raw) > MAX_INPUT_BYTES:
        raise EconomicsError("input_too_large")
    depth = 0
    in_string = escaped = False
    for byte in raw:
        if in_string:
            if escaped:
                escaped = False
            elif byte == 0x5C:
                escaped = True
            elif byte == 0x22:
                in_string = False
        elif byte == 0x22:
            in_string = True
        elif byte in (0x7B, 0x5B):
            depth += 1
            if depth > 128:
                raise EconomicsError("input_invalid")
        elif byte in (0x7D, 0x5D):
            depth -= 1
            if depth < 0:
                raise EconomicsError("input_invalid")

    def unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate_key")
            result[key] = value
        return result

    try:
        return json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=unique_pairs,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError("invalid_number")),
        )
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise EconomicsError("input_invalid") from None


def _canonical_digest(value: Any) -> str:
    try:
        payload = json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError):
        raise EconomicsError("input_invalid") from None
    return hashlib.sha256(payload).hexdigest()


def _validate_usage(value: Any, reasons: Any, *, code: str) -> dict[str, int | None]:
    if (
        not isinstance(value, dict)
        or set(value) != set(USAGE_BUCKETS)
        or not isinstance(reasons, dict)
    ):
        raise EconomicsError(code)
    if set(reasons) - set(USAGE_BUCKETS):
        raise EconomicsError(code)
    normalized: dict[str, int | None] = {}
    for bucket in USAGE_BUCKETS:
        amount = value[bucket]
        if amount is None:
            reason = reasons.get(bucket)
            if not isinstance(reason, str) or reason not in MISSING_REASONS:
                raise EconomicsError("missing_usage_reason_required")
            normalized[bucket] = None
        else:
            normalized[bucket] = _integer(amount, code=code)
            if bucket in reasons:
                raise EconomicsError("present_usage_has_missing_reason")
    return normalized


def _validate_quality(value: Any) -> dict[str, str]:
    _keys(value, set(SCORE_VALUES), "quality_invalid")
    for key, choices in SCORE_VALUES.items():
        if not isinstance(value[key], str) or value[key] not in choices:
            raise EconomicsError("quality_invalid")
    return value


def _check_factorial_profile(profile: dict[str, Any]) -> None:
    """Per-cell check. Factorial IDs are F<input><output>: first digit input reduction,
    second output contract. Input reduction is on when preparation and retrieval are both
    enabled and off when both are disabled. The flags must be booleans matching the digits.
    Output contract is decided across cells by ``_check_factorial_cells``.
    """
    for flag in FACTORIAL_FLAGS:
        if type(profile[flag]) is not bool:
            raise EconomicsError("factorial_condition_invalid")
    condition = profile["condition_id"]
    input_on, output_on = condition[1] == "1", condition[2] == "1"
    if profile["input_reduction"] is not input_on or profile["output_contract"] is not output_on:
        raise EconomicsError("factorial_condition_invalid")
    input_state = "enabled" if input_on else "disabled"
    if profile["preparation"] != input_state or profile["retrieval"] != input_state:
        raise EconomicsError("factorial_condition_invalid")


def _check_factorial_cells(profiles: dict[str, dict[str, Any]]) -> None:
    """Cross-cell check for the 2x2. Output contract is the concise output contract.

    F00 and F10 (output contract off) share one concise_instructions value, F01 and F11
    (output contract on) share a different one, and every other setting is identical.
    Artifact rendering is a separate mechanism, held fixed across all four cells.
    """
    present = [condition for condition in FACTORIAL_CONDITIONS if condition in profiles]
    if not present:
        return
    if len(present) != len(FACTORIAL_CONDITIONS):
        raise EconomicsError("factorial_profile_missing")
    cells = [profiles[condition] for condition in FACTORIAL_CONDITIONS]
    for field in (
        "model",
        "settings",
        "effort",
        "artifact",
        "compute",
        "source_access",
        "tool_settings",
    ):
        if len({cell[field] for cell in cells}) != 1:
            raise EconomicsError("factorial_condition_invalid")
    concise = {
        condition: profiles[condition]["concise_instructions"] for condition in FACTORIAL_CONDITIONS
    }
    if (
        concise["F00"] != concise["F10"]
        or concise["F01"] != concise["F11"]
        or concise["F00"] == concise["F01"]
    ):
        raise EconomicsError("factorial_condition_invalid")


def _withdrawn_participants(document: Any, study_id: str) -> frozenset[str]:
    """Validate a withdrawal file against the study id; no document means no withdrawals."""
    if document is None:
        return frozenset()
    try:
        return pilot_study.validate_withdrawals(document, study_id=study_id)
    except pilot_study.PilotStudyError:
        raise EconomicsError("withdrawals_invalid") from None


def _without(rows: Any, key: str, values: frozenset[str] | set[str]) -> Any:
    """Drop rows whose string ``key`` is in ``values``. Malformed rows are left for validation."""
    if not isinstance(rows, list):
        return rows
    return [
        row
        for row in rows
        if not (isinstance(row, dict) and isinstance(row.get(key), str) and row[key] in values)
    ]


def _validate_input(value: Any, withdrawal_doc: Any = None) -> dict[str, Any]:
    root = _keys(
        deepcopy(value),
        {
            "schema_version",
            "evidence_class",
            "as_of_utc",
            "study_bundle",
            "assignment_set",
            "observations",
            "scores",
            "usage_windows",
            "runs",
            "condition_profiles",
            "rate_cards",
            "sensitivity_scenarios",
        },
        "input_schema_invalid",
    )
    if root["schema_version"] != SCHEMA_VERSION:
        raise EconomicsError("schema_version_unsupported")
    if not isinstance(root["evidence_class"], str) or root["evidence_class"] not in {
        "synthetic",
        "consented_deidentified",
    }:
        raise EconomicsError("evidence_class_invalid")
    root["as_of_utc"] = _utc(root["as_of_utc"])

    bundle = _keys(
        root["study_bundle"],
        {"schema_version", "bundle_kind", "study_id", "study_plan", "tasks"},
        "study_bundle_invalid",
    )
    if bundle["schema_version"] != STUDY_SCHEMA_VERSION or bundle["bundle_kind"] not in {
        "synthetic_task_bundle",
        pilot_study.PILOT_BUNDLE_KIND,
    }:
        raise EconomicsError("study_schema_unsupported")
    _identifier(bundle["study_id"])
    if bundle["bundle_kind"] == pilot_study.PILOT_BUNDLE_KIND and (
        root["evidence_class"] == "synthetic"
    ):
        raise EconomicsError("pilot_bundle_requires_consented_evidence")
    withdrawn = _withdrawn_participants(withdrawal_doc, bundle["study_id"])
    plan = _keys(
        bundle["study_plan"],
        {
            "target_participants",
            "minimum_completed_per_arm",
            "stopping_rule",
            "stratum_weights",
            "refresh_max_age_hours",
        },
        "study_plan_invalid",
    )
    _integer(plan["target_participants"], high=10000)
    _integer(plan["minimum_completed_per_arm"], high=100000)
    if plan["stopping_rule"] != "complete_predeclared_bundle_or_stop_at_target":
        raise EconomicsError("stopping_rule_invalid")
    weights = plan["stratum_weights"]
    if not isinstance(weights, dict) or not weights or len(weights) > 64:
        raise EconomicsError("stratum_weights_invalid")
    weight_total = Decimal(0)
    for stratum, amount in weights.items():
        _identifier(stratum)
        weight_total += _decimal(amount, code="stratum_weight_invalid", high=Decimal(1))
    if weight_total != Decimal(1):
        raise EconomicsError("stratum_weights_must_sum_to_one")
    _integer(plan["refresh_max_age_hours"], high=168)
    tasks = bundle["tasks"]
    if not isinstance(tasks, list) or not tasks or len(tasks) > 500:
        raise EconomicsError("tasks_invalid")
    real_bundle = bundle["bundle_kind"] == pilot_study.PILOT_BUNDLE_KIND
    task_keys = (
        {"task_id", "stratum", "locale", "operator_instruction", "sources"}
        if real_bundle
        else {
            "task_id",
            "stratum",
            "locale",
            "operator_instruction",
            "synthetic_sources",
            "synthetic_check",
            "synthetic_observed",
        }
    )
    task_strata: dict[str, str] = {}
    for task in tasks:
        item = _keys(task, task_keys, "task_invalid")
        task_id = _identifier(item["task_id"], "task_id_invalid")
        if TASK_ID.fullmatch(task_id) is None:
            raise EconomicsError("task_id_invalid")
        stratum = _identifier(item["stratum"])
        _identifier(item["locale"])
        if task_id in task_strata or stratum not in weights:
            raise EconomicsError("task_duplicate_or_stratum_invalid")
        if (
            not isinstance(item["operator_instruction"], str)
            or not item["operator_instruction"].strip()
            or len(item["operator_instruction"]) > 1200
        ):
            raise EconomicsError("operator_instruction_invalid")
        if not real_bundle:
            # Synthetic sources carry their content; real sources are file descriptors whose
            # shape pilot_study.validate_bundle checks below.
            sources = pilot_study.task_source_snapshot(item)
            if not isinstance(sources, list) or len(sources) > 8:
                raise EconomicsError("synthetic_sources_invalid")
            source_ids: set[str] = set()
            for raw_source in sources:
                source = _keys(
                    raw_source, {"source_id", "format", "content"}, "synthetic_source_invalid"
                )
                source_id = _identifier(source["source_id"])
                if (
                    source_id in source_ids
                    or not isinstance(source["format"], str)
                    or source["format"] not in {"txt", "md", "json", "csv", "unsupported_fixture"}
                ):
                    raise EconomicsError("synthetic_source_invalid")
                if not isinstance(source["content"], str) or len(source["content"]) > 64_000:
                    raise EconomicsError("synthetic_source_content_invalid")
                source_ids.add(source_id)
            check = _keys(item["synthetic_check"], {"kind", "expected"}, "synthetic_check_invalid")
            if not isinstance(check["kind"], str) or check["kind"] not in {
                "integer_equal",
                "boolean_equal",
                "literal_equal",
            }:
                raise EconomicsError("synthetic_check_invalid")
            if check["kind"] == "integer_equal":
                _integer(check["expected"])
            elif check["kind"] == "boolean_equal":
                if type(check["expected"]) is not bool:
                    raise EconomicsError("synthetic_check_invalid")
            elif not isinstance(check["expected"], str) or len(check["expected"]) > 120:
                raise EconomicsError("synthetic_check_invalid")
        task_strata[task_id] = stratum

    try:
        pilot_study.validate_bundle(bundle)
    except pilot_study.PilotStudyError:
        raise EconomicsError("study_bundle_invalid") from None
    assignment_set = _keys(
        root["assignment_set"],
        {
            "schema_version",
            "study_id",
            "bundle_sha256",
            "seed_mode",
            "seed",
            "seed_commitment",
            "arms",
            "assignments",
        },
        "assignment_set_invalid",
    )
    if (
        assignment_set["schema_version"] != STUDY_SCHEMA_VERSION
        or assignment_set["study_id"] != bundle["study_id"]
        or assignment_set["arms"] != list(ARMS)
    ):
        raise EconomicsError("assignment_contract_mismatch")
    if assignment_set["seed_mode"] not in {"public_synthetic", "committed_secret"}:
        raise EconomicsError("seed_mode_invalid")
    assignment_rows = assignment_set["assignments"]
    if (
        not isinstance(assignment_rows, list)
        or not assignment_rows
        or len(assignment_rows) > MAX_ASSIGNMENTS
    ):
        raise EconomicsError("assignments_invalid")
    assignments: dict[str, dict[str, Any]] = {}
    pair_groups: dict[tuple[str, str], dict[str, str]] = defaultdict(dict)
    participant_kinds: set[str] = set()
    task_positions = {task_id: index for index, task_id in enumerate(task_strata)}
    for raw in assignment_rows:
        row = _keys(
            raw,
            {
                "assignment_id",
                "score_id",
                "participant_id",
                "participant_kind",
                "pair_id",
                "task_id",
                "stratum",
                "locale",
                "arm",
                "order",
                "blind_arm_code",
                "consent",
            },
            "assignment_invalid",
        )
        assignment_id = _identifier(row["assignment_id"])
        if assignment_id in assignments:
            raise EconomicsError("assignment_duplicate")
        for key in ("score_id", "participant_id", "pair_id", "task_id", "locale", "blind_arm_code"):
            _identifier(row[key])
        if not isinstance(row["participant_kind"], str) or row["participant_kind"] not in {
            "synthetic",
            "pseudonymous",
        }:
            raise EconomicsError("participant_kind_invalid")
        participant_kinds.add(row["participant_kind"])
        participant_pattern = (
            SYNTHETIC_PARTICIPANT if row["participant_kind"] == "synthetic" else PSEUDONYM
        )
        if participant_pattern.fullmatch(row["participant_id"]) is None:
            raise EconomicsError("participant_code_invalid")
        if (
            row["task_id"] not in task_strata
            or row["stratum"] != task_strata[row["task_id"]]
            or row["arm"] not in ARMS
        ):
            raise EconomicsError("assignment_task_arm_mismatch")
        _integer(row["order"], high=4)
        if row["order"] < 1:
            raise EconomicsError("assignment_order_invalid")
        _keys(
            row["consent"],
            {"status"}
            if row["participant_kind"] == "synthetic"
            else {"status", "consented_at_utc", "scope"},
            "consent_invalid",
        )
        if row["participant_kind"] == "synthetic":
            if row["consent"]["status"] != "synthetic_not_applicable":
                raise EconomicsError("consent_invalid")
        elif (
            row["consent"].get("status") != "consented"
            or row["consent"].get("scope") != STUDY_SCHEMA_VERSION
        ):
            raise EconomicsError("consent_required")
        if row["participant_kind"] == "pseudonymous":
            _utc(row["consent"].get("consented_at_utc"))
        group_key = (row["participant_id"], row["task_id"])
        if row["arm"] in pair_groups[group_key]:
            raise EconomicsError("assignment_arm_duplicate")
        pair_groups[group_key][row["arm"]] = assignment_id
        assignments[assignment_id] = row
    if len(participant_kinds) != 1 or any(set(arms) != set(ARMS) for arms in pair_groups.values()):
        raise EconomicsError("assignment_arm_set_invalid")
    if root["evidence_class"] == "synthetic" and participant_kinds != {"synthetic"}:
        raise EconomicsError("synthetic_evidence_contains_real_participants")
    # Synthetic seeds are public; real cohorts must use a committed secret seed.
    expected_seed_mode = (
        "public_synthetic" if root["evidence_class"] == "synthetic" else "committed_secret"
    )
    if assignment_set["seed_mode"] != expected_seed_mode:
        raise EconomicsError("seed_mode_evidence_mismatch")
    participant_ids = sorted({key[0] for key in pair_groups})
    expected_pairs = {
        (participant_id, task_id)
        for participant_id in participant_ids
        for task_id in task_positions
    }
    if set(pair_groups) != expected_pairs:
        raise EconomicsError("assignment_participant_task_coverage_invalid")
    for arm_to_id in pair_groups.values():
        group_rows = [assignments[assignment_id] for assignment_id in arm_to_id.values()]
        if {row["order"] for row in group_rows} != {1, 2, 3, 4}:
            raise EconomicsError("assignment_order_invalid")
    # Williams order, blind aliases, pair/assignment/score IDs and the bundle digest
    # are the M6 contract's job. Synthetic sets are recomputed from their public seed;
    # committed real sets are structural only until the seed is revealed.
    try:
        pilot_study.validate_assignment_set(bundle, assignment_set)
    except pilot_study.PilotStudyError as error:
        raise EconomicsError(
            PILOT_ASSIGNMENT_CODES.get(error.code, "assignment_contract_invalid")
        ) from None
    allocation_verification = (
        "public_seed_recomputed"
        if assignment_set["seed_mode"] == "public_synthetic"
        else "committed_seed_structural_only"
    )

    raw_profiles = root["condition_profiles"]
    if not isinstance(raw_profiles, list) or not (
        len(CONDITIONS) <= len(raw_profiles) <= len(CONDITIONS) + len(FACTORIAL_CONDITIONS)
    ):
        raise EconomicsError("condition_profiles_invalid")
    profiles: dict[str, dict[str, Any]] = {}
    for raw_profile in raw_profiles:
        is_factorial = isinstance(raw_profile, dict) and raw_profile.get("condition_id") in (
            FACTORIAL_CONDITIONS
        )
        expected_fields = set(PROFILE_FIELDS) | (set(FACTORIAL_FLAGS) if is_factorial else set())
        profile = _keys(raw_profile, expected_fields, "condition_profile_invalid")
        condition = profile["condition_id"]
        if (
            not isinstance(condition, str)
            or condition not in CONDITIONS | set(FACTORIAL_CONDITIONS)
            or condition in profiles
        ):
            raise EconomicsError("condition_profile_invalid")
        for field in ("model", "settings", "effort", "concise_instructions", "tool_settings"):
            value = profile[field]
            if value is not None:
                _identifier(value, "condition_profile_value_invalid")
        if profile["effort"] is not None and profile["effort"] not in {
            "low",
            "medium",
            "high",
            "max",
            "unknown",
            "not_applicable",
        }:
            raise EconomicsError("condition_profile_effort_invalid")
        for mechanism in MECHANISMS:
            value = profile[mechanism]
            if not isinstance(value, str) or value not in {
                "enabled",
                "disabled",
                "not_applicable",
            }:
                raise EconomicsError("condition_profile_mechanism_invalid")
        if is_factorial:
            _check_factorial_profile(profile)
        profiles[condition] = profile
    if not CONDITIONS <= set(profiles):
        raise EconomicsError("condition_profiles_incomplete")
    _check_factorial_cells(profiles)
    if any(profiles["B_full"][name] != "enabled" for name in MECHANISMS):
        raise EconomicsError("full_package_profile_incomplete")
    for condition, mechanism in ABLATION_MECHANISM.items():
        expected = dict(profiles["B_full"])
        expected["condition_id"] = condition
        expected[mechanism] = "disabled"
        if profiles[condition] != expected:
            raise EconomicsError("ablation_profile_not_single_mechanism")
    for field in ("model", "settings", "effort", "concise_instructions"):
        if profiles["C_tuned"][field] != profiles["B_full"][field]:
            raise EconomicsError("tuned_baseline_profile_mismatch")

    # Withdrawn participants keep their assignment rows (digest and count cover the full
    # cohort) but contribute no observations, scores, runs, windows or cells to any statistic.
    if not withdrawn <= set(participant_ids):
        raise EconomicsError("withdrawal_unknown_participant")
    all_assignments = assignments
    withdrawn_by_arm = Counter(
        row["arm"] for row in all_assignments.values() if row["participant_id"] in withdrawn
    )
    withdrawn_assignment_ids = frozenset(
        assignment_id
        for assignment_id, row in all_assignments.items()
        if row["participant_id"] in withdrawn
    )
    withdrawn_score_ids = frozenset(
        all_assignments[assignment_id]["score_id"] for assignment_id in withdrawn_assignment_ids
    )
    assignments = {
        assignment_id: row
        for assignment_id, row in all_assignments.items()
        if assignment_id not in withdrawn_assignment_ids
    }
    pair_groups = {key: group for key, group in pair_groups.items() if key[0] not in withdrawn}
    root["observations"] = _without(root["observations"], "assignment_id", withdrawn_assignment_ids)
    root["scores"] = _without(root["scores"], "score_id", withdrawn_score_ids)
    root["usage_windows"] = _without(root["usage_windows"], "participant_id", withdrawn)
    root["runs"] = _without(root["runs"], "assignment_id", withdrawn_assignment_ids)

    observations = root["observations"]
    if not isinstance(observations, list) or len(observations) != len(assignments):
        raise EconomicsError("observations_incomplete")
    observation_by_id: dict[str, dict[str, Any]] = {}
    for raw in observations:
        row = _keys(
            raw,
            {
                "assignment_id",
                "participant_id",
                "task_id",
                "pair_id",
                "arm",
                "status",
                "start_utc",
                "end_utc",
                "locale",
                "versions",
                "counter_version",
                "source_snapshot_sha256",
                "metrics",
                "metric_missing_reasons",
                "provider_usage",
                "usage_missing_reasons",
                "failure_code",
                "unsupported_code",
            },
            "observation_invalid",
        )
        assignment_id = row["assignment_id"]
        assigned = assignments.get(assignment_id)
        if assigned is None or assignment_id in observation_by_id:
            raise EconomicsError("observation_assignment_invalid")
        for key in ("participant_id", "task_id", "pair_id", "arm", "locale"):
            if row[key] != assigned[key]:
                raise EconomicsError("observation_assignment_mismatch")
        if not isinstance(row["status"], str) or row["status"] not in OUTCOMES:
            raise EconomicsError("outcome_status_invalid")
        if row["status"] == "not_run":
            if row["start_utc"] is not None or row["end_utc"] is not None:
                raise EconomicsError("not_run_window_invalid")
        else:
            start, end = _datetime_utc(row["start_utc"]), _datetime_utc(row["end_utc"])
            if end < start:
                raise EconomicsError("outcome_window_invalid")
        versions = _keys(
            row["versions"], {"desktop", "extension", "model", "settings"}, "versions_invalid"
        )
        for version in versions.values():
            if version is not None:
                _identifier(version)
        if row["counter_version"] is not None:
            _identifier(row["counter_version"])
        digest = row["source_snapshot_sha256"]
        if digest is not None and (not isinstance(digest, str) or SHA256.fullmatch(digest) is None):
            raise EconomicsError("source_digest_invalid")
        metrics = _keys(
            row["metrics"],
            {
                "latency_ms",
                "retry_count",
                "tool_call_count",
                "retrieval_bytes",
                "estimated_input_tokens",
                "estimated_output_tokens",
                "schema_instruction_tokens",
            },
            "metrics_invalid",
        )
        if not isinstance(row["metric_missing_reasons"], dict):
            raise EconomicsError("metric_missing_reasons_invalid")
        for key, amount in metrics.items():
            if amount is None:
                reason = row["metric_missing_reasons"].get(key)
                if not isinstance(reason, str) or reason not in MISSING_REASONS:
                    raise EconomicsError("metric_missing_reason_required")
            else:
                _integer(amount, high=604_800_000 if key == "latency_ms" else MAX_COUNT)
                if key in row["metric_missing_reasons"]:
                    raise EconomicsError("present_metric_has_missing_reason")
        usage = _validate_usage(
            row["provider_usage"], row["usage_missing_reasons"], code="provider_usage_invalid"
        )
        normalized = dict(row)
        normalized["provider_usage"] = usage
        observation_by_id[assignment_id] = normalized

    if set(observation_by_id) != set(assignments):
        raise EconomicsError("observations_incomplete")

    score_rows = root["scores"]
    if not isinstance(score_rows, list) or len(score_rows) > len(assignments):
        raise EconomicsError("scores_invalid")
    scores_by_assignment: dict[str, dict[str, str]] = {}
    score_status_by_assignment: dict[str, str] = {}
    assignment_by_score = {row["score_id"]: row for row in assignments.values()}
    for raw in score_rows:
        score_fields = {
            "score_id",
            "blinded_arm_code",
            "scorer_code",
            "status",
            *SCORE_VALUES,
            "omission_codes",
        }
        if not isinstance(raw, dict) or set(raw) not in {
            frozenset(score_fields),
            frozenset(score_fields | {"task_id"}),
        }:
            raise EconomicsError("score_invalid")
        score = raw
        assigned = assignment_by_score.get(score["score_id"])
        if (
            assigned is None
            or assigned["assignment_id"] in scores_by_assignment
            or score["blinded_arm_code"] != assigned["blind_arm_code"]
            or ("task_id" in score and score["task_id"] != assigned["task_id"])
        ):
            raise EconomicsError("score_assignment_invalid")
        _identifier(score["scorer_code"])
        if not isinstance(score["status"], str) or score["status"] not in {"scored", "no_score"}:
            raise EconomicsError("score_status_invalid")
        if assigned["participant_kind"] == "synthetic" and score["status"] == "scored":
            raise EconomicsError("synthetic_participant_quality_score_forbidden")
        quality = _validate_quality({key: score[key] for key in SCORE_VALUES})
        if not isinstance(score["omission_codes"], list) or len(score["omission_codes"]) > 32:
            raise EconomicsError("omission_codes_invalid")
        for code in score["omission_codes"]:
            _identifier(code)
        if score["status"] == "no_score" and any(value != "unscored" for value in quality.values()):
            raise EconomicsError("no_score_dimensions_invalid")
        if score["status"] == "scored" and any(value == "unscored" for value in quality.values()):
            raise EconomicsError("score_dimensions_incomplete")
        obs = observation_by_id[assigned["assignment_id"]]
        if obs["status"] != "completed" and score["status"] != "no_score":
            raise EconomicsError("noncompleted_task_scored")
        scores_by_assignment[assigned["assignment_id"]] = quality
        score_status_by_assignment[assigned["assignment_id"]] = score["status"]

    windows = root["usage_windows"]
    if not isinstance(windows, list) or len(windows) > 100_000:
        raise EconomicsError("usage_windows_invalid")
    if root["evidence_class"] == "synthetic" and windows:
        raise EconomicsError("synthetic_evidence_contains_admin_usage")
    # Keep administrator billing windows separate. M13 never allocates a window
    # across assignments or calls; the fixture report labels live evidence pending.
    window_ids: set[str] = set()
    for raw_window in windows:
        window = _keys(
            raw_window,
            {
                "window_id",
                "participant_id",
                "arm",
                "assignment_ids",
                "start_utc",
                "end_utc",
                "refreshed_at_utc",
                "source",
                "mapping_confidence",
                "exclusions_recorded",
                "unrelated_activity",
                "exclusion_codes",
                "bucket_missing_reasons",
                "usage_buckets",
                "cost_amount",
                "cost_missing_reason",
                "currency",
                "export_sha256",
            },
            "usage_window_invalid",
        )
        window_id = _identifier(window["window_id"])
        if window_id in window_ids:
            raise EconomicsError("usage_window_duplicate")
        window_ids.add(window_id)
        if root["evidence_class"] != "synthetic" and (
            not isinstance(window["export_sha256"], str)
            or SHA256.fullmatch(window["export_sha256"]) is None
        ):
            raise EconomicsError("usage_window_export_digest_required")
        _identifier(window["participant_id"])
        if (
            not isinstance(window["arm"], str)
            or window["arm"] not in ARMS
            or window["source"] != "admin_export"
        ):
            raise EconomicsError("usage_window_source_or_arm_invalid")
        if (
            not isinstance(window["mapping_confidence"], str)
            or window["mapping_confidence"] not in {"high", "medium", "low"}
            or type(window["exclusions_recorded"]) is not bool
        ):
            raise EconomicsError("usage_window_mapping_invalid")
        mapped = window["assignment_ids"]
        if not isinstance(mapped, list) or not mapped or len(mapped) > len(assignments):
            raise EconomicsError("usage_window_assignment_invalid")
        for assignment_id in mapped:
            _identifier(assignment_id)
        if len(set(mapped)) != len(mapped):
            raise EconomicsError("usage_window_assignment_invalid")
        for assignment_id in mapped:
            assigned = assignments.get(assignment_id)
            if (
                assigned is None
                or assigned["participant_id"] != window["participant_id"]
                or assigned["arm"] != window["arm"]
                or assigned["participant_kind"] != "pseudonymous"
            ):
                raise EconomicsError("usage_window_assignment_mismatch")
            if observation_by_id[assignment_id]["status"] == "not_run":
                raise EconomicsError("usage_window_mapped_task_not_observed")
        start, end, refreshed = map(
            _datetime_utc, (window["start_utc"], window["end_utc"], window["refreshed_at_utc"])
        )
        if end < start or refreshed < end:
            raise EconomicsError("usage_window_time_invalid")
        if (
            not isinstance(window["unrelated_activity"], str)
            or window["unrelated_activity"]
            not in {"none", "present_excluded", "present_unmapped", "unknown"}
            or not isinstance(window["exclusion_codes"], list)
            or len(window["exclusion_codes"]) > 32
        ):
            raise EconomicsError("usage_window_exclusion_metadata_invalid")
        for code in window["exclusion_codes"]:
            _identifier(code)
        for assignment_id in mapped:
            observation = observation_by_id[assignment_id]
            if (
                _datetime_utc(observation["start_utc"]) < start
                or _datetime_utc(observation["end_utc"]) > end
            ):
                raise EconomicsError("usage_window_does_not_contain_task")
            if _datetime_utc(assignments[assignment_id]["consent"]["consented_at_utc"]) > start:
                raise EconomicsError("usage_window_precedes_consent")
        _validate_usage(
            window["usage_buckets"],
            window["bucket_missing_reasons"],
            code="usage_window_buckets_invalid",
        )
        if window["cost_amount"] is None:
            if (
                not isinstance(window["cost_missing_reason"], str)
                or window["cost_missing_reason"]
                not in {"not_collected", "not_available_from_export"}
                or window["currency"] is not None
            ):
                raise EconomicsError("usage_window_cost_missing_reason_required")
        else:
            _decimal(
                window["cost_amount"],
                code="usage_window_cost_invalid",
                high=Decimal("1000000000000"),
            )
            if (
                window["cost_missing_reason"] is not None
                or not isinstance(window["currency"], str)
                or CURRENCY.fullmatch(window["currency"]) is None
            ):
                raise EconomicsError("usage_window_cost_invalid")

    rate_cards: dict[str, dict[str, Any]] = {}
    if not isinstance(root["rate_cards"], list) or len(root["rate_cards"]) > 256:
        raise EconomicsError("rate_cards_invalid")
    currencies: set[str] = set()
    rate_sources: set[str] = set()
    rate_card_meta: list[dict[str, Any]] = []
    for raw in root["rate_cards"]:
        card = _keys(
            raw,
            {"model", "currency", "denominator", "rates", "rate_source", "as_of"},
            "rate_card_invalid",
        )
        model = _identifier(card["model"])
        if model in rate_cards:
            raise EconomicsError("rate_card_invalid")
        if card["rate_source"] not in RATE_SOURCES:
            raise EconomicsError("rate_source_invalid")
        if not isinstance(card["as_of"], str) or ISO_DATE.fullmatch(card["as_of"]) is None:
            raise EconomicsError("rate_as_of_invalid")
        try:
            _dt.date.fromisoformat(card["as_of"])
        except ValueError:
            raise EconomicsError("rate_as_of_invalid") from None
        if card["rate_source"] == "placeholder":
            if card["currency"] is not None:
                raise EconomicsError("placeholder_rate_currency_forbidden")
        elif not isinstance(card["currency"], str) or CURRENCY.fullmatch(card["currency"]) is None:
            raise EconomicsError("rate_card_invalid")
        if card["denominator"] != "per_1m_tokens":
            raise EconomicsError("rate_denominator_invalid")
        rates = _keys(card["rates"], set(RATE_KEYS.values()), "rate_fields_invalid")
        parsed = {
            key: _decimal(rate, code="rate_invalid", high=Decimal("1000000"))
            for key, rate in rates.items()
        }
        rate_cards[model] = {
            "currency": card["currency"],
            "rates": parsed,
            "rate_source": card["rate_source"],
        }
        rate_sources.add(card["rate_source"])
        if card["currency"] is not None:
            currencies.add(card["currency"])
        rate_card_meta.append(
            {
                "model": model,
                "rate_source": card["rate_source"],
                "as_of": card["as_of"],
                "currency": card["currency"],
            }
        )
    rates_are_placeholder = "placeholder" in rate_sources
    if rates_are_placeholder and rate_sources != {"placeholder"}:
        # Placeholder units and priced money must never be summed in one report.
        raise EconomicsError("rate_source_mixed")
    if len(currencies) > 1:
        raise EconomicsError("rate_currency_mismatch")

    runs = root["runs"]
    if not isinstance(runs, list) or len(runs) > MAX_RUNS:
        raise EconomicsError("runs_invalid")
    run_index: dict[tuple[str, str], dict[str, Any]] = {}
    profile_deviations: dict[tuple[str, str], list[str]] = {}
    run_ids: set[str] = set()
    for raw in runs:
        required = {
            "run_id",
            "assignment_id",
            "condition_id",
            "source_snapshot_sha256",
            "model",
            "settings",
            "counter_version",
            "status",
            "retry_count",
            "retry_count_missing_reason",
            "latency_ms",
            "latency_missing_reason",
            "service_cost_status",
            "service_cost_missing_reason",
            "request_ledger_status",
            "request_ledger_missing_reason",
            "requests",
            "incremental_service_costs",
            "output",
            "quality",
        }
        run = _keys(raw, required, "run_invalid")
        run_id = _identifier(run["run_id"])
        assignment_id = _identifier(run["assignment_id"])
        condition_id = run["condition_id"]
        assigned = assignments.get(assignment_id)
        if (
            assigned is None
            or not isinstance(condition_id, str)
            or condition_id not in CONDITIONS | set(FACTORIAL_CONDITIONS)
        ):
            raise EconomicsError("run_assignment_or_condition_invalid")
        # Ablations and factorial cells attach to the block's B assignment.
        expected_arm = BASE_CONDITIONS.get(condition_id, "B")
        if assigned["arm"] != expected_arm:
            raise EconomicsError("run_arm_mismatch")
        key = (assignment_id, condition_id)
        if key in run_index or run_id in run_ids:
            raise EconomicsError("run_duplicate")
        run_ids.add(run_id)
        digest = run["source_snapshot_sha256"]
        if digest is not None and (not isinstance(digest, str) or SHA256.fullmatch(digest) is None):
            raise EconomicsError("run_source_digest_invalid")
        for field in ("model", "settings", "counter_version"):
            if run[field] is not None:
                _identifier(run[field])
        if not isinstance(run["status"], str) or run["status"] not in OUTCOMES:
            raise EconomicsError("run_status_invalid")
        if run["retry_count"] is None:
            if (
                not isinstance(run["retry_count_missing_reason"], str)
                or run["retry_count_missing_reason"] not in MISSING_REASONS
            ):
                raise EconomicsError("retry_count_missing_reason_required")
        else:
            _integer(run["retry_count"])
            if run["retry_count_missing_reason"] is not None:
                raise EconomicsError("retry_count_missing_reason_unexpected")
        if run["latency_ms"] is None:
            if (
                not isinstance(run["latency_missing_reason"], str)
                or run["latency_missing_reason"] not in MISSING_REASONS
            ):
                raise EconomicsError("latency_missing_reason_required")
        else:
            _integer(run["latency_ms"], high=604_800_000)
            if run["latency_missing_reason"] is not None:
                raise EconomicsError("latency_missing_reason_unexpected")
        if not isinstance(run["service_cost_status"], str) or run["service_cost_status"] not in {
            "complete",
            "unknown",
        }:
            raise EconomicsError("service_cost_status_invalid")
        if run["service_cost_status"] == "unknown":
            if (
                not isinstance(run["service_cost_missing_reason"], str)
                or run["service_cost_missing_reason"] not in MISSING_REASONS
            ):
                raise EconomicsError("service_cost_missing_reason_required")
        elif run["service_cost_missing_reason"] is not None:
            raise EconomicsError("service_cost_missing_reason_unexpected")
        if condition_id in FACTORIAL_CONDITIONS and condition_id not in profiles:
            raise EconomicsError("factorial_profile_missing")
        if condition_id in BASE_CONDITIONS:
            observation = observation_by_id[assignment_id]
            profile = profiles[condition_id]
            deviations: list[str] = []
            for field in ("model", "settings"):
                if profile[field] is None:
                    deviations.append(f"declared_{field}_profile_unknown")
                elif run[field] != profile[field]:
                    deviations.append(f"declared_{field}_profile_mismatch")
            profile_deviations[key] = deviations
            if run["status"] != observation["status"]:
                raise EconomicsError("run_observation_status_mismatch")
            if run["retry_count"] != observation["metrics"]["retry_count"]:
                raise EconomicsError("run_retry_count_mismatch")
            if run["latency_ms"] != observation["metrics"]["latency_ms"]:
                raise EconomicsError("run_latency_mismatch")
            if (
                run["model"] != observation["versions"]["model"]
                or run["settings"] != observation["versions"]["settings"]
            ):
                raise EconomicsError("run_observation_settings_mismatch")
            if (
                run["counter_version"] != observation["counter_version"]
                or run["source_snapshot_sha256"] != observation["source_snapshot_sha256"]
            ):
                raise EconomicsError("run_observation_counter_or_source_mismatch")

        if not isinstance(run["request_ledger_status"], str) or run[
            "request_ledger_status"
        ] not in {
            "complete",
            "unknown",
        }:
            raise EconomicsError("request_ledger_status_invalid")
        if run["request_ledger_status"] == "unknown":
            if (
                not isinstance(run["request_ledger_missing_reason"], str)
                or run["request_ledger_missing_reason"] not in MISSING_REASONS
            ):
                raise EconomicsError("request_ledger_missing_reason_required")
        elif run["request_ledger_missing_reason"] is not None:
            raise EconomicsError("request_ledger_missing_reason_unexpected")
        requests = run["requests"]
        if not isinstance(requests, list) or len(requests) > MAX_REQUESTS_PER_RUN:
            raise EconomicsError("requests_invalid")
        if (
            not requests
            and run["request_ledger_status"] == "complete"
            and run["status"]
            not in {
                "unsupported",
                "bypassed",
                "not_run",
            }
        ):
            raise EconomicsError("empty_request_ledger_for_model_run")
        request_ids: set[str] = set()
        normalized_requests: list[dict[str, Any]] = []
        request_totals = dict.fromkeys(USAGE_BUCKETS, 0)
        request_unknown = dict.fromkeys(USAGE_BUCKETS, False)
        for request in requests:
            item = _keys(
                request, {"request_id", "model", "usage", "missing_reasons"}, "request_invalid"
            )
            request_id, model = _identifier(item["request_id"]), _identifier(item["model"])
            if request_id in request_ids:
                raise EconomicsError("request_duplicate")
            usage = _validate_usage(
                item["usage"], item["missing_reasons"], code="request_usage_invalid"
            )
            for bucket, amount in usage.items():
                if amount is None:
                    request_unknown[bucket] = True
                else:
                    request_totals[bucket] += amount
                    if request_totals[bucket] > MAX_COUNT:
                        raise EconomicsError("request_usage_too_large")
            normalized_request = dict(item)
            normalized_request["normalized_usage"] = usage
            request_ids.add(request_id)
            normalized_requests.append(normalized_request)
        if condition_id in BASE_CONDITIONS and run["request_ledger_status"] == "complete":
            observed_usage = observation_by_id[assignment_id]["provider_usage"]
            for bucket, amount in observed_usage.items():
                if amount is not None and (
                    request_unknown[bucket] or request_totals[bucket] != amount
                ):
                    raise EconomicsError("request_observation_usage_mismatch")

        service_costs = run["incremental_service_costs"]
        if not isinstance(service_costs, list) or len(service_costs) > 32:
            raise EconomicsError("service_costs_invalid")
        service_ids: set[str] = set()
        for cost in service_costs:
            item = _keys(
                cost,
                {
                    "cost_id",
                    "category",
                    "amount",
                    "currency",
                    "missing_reason",
                    "included_in_provider_cost",
                },
                "service_cost_invalid",
            )
            cost_id = _identifier(item["cost_id"])
            if cost_id in service_ids:
                raise EconomicsError("service_cost_duplicate")
            service_ids.add(cost_id)
            if not isinstance(item["category"], str) or item["category"] not in {
                "preparation",
                "delegation",
                "hosting",
                "license",
                "local_execution",
                "other",
            }:
                raise EconomicsError("service_cost_category_invalid")
            if item["included_in_provider_cost"] is not False:
                raise EconomicsError("service_cost_double_count_guard")
            if rates_are_placeholder and item["amount"] is not None:
                raise EconomicsError("service_cost_requires_non_placeholder_rates")
            if item["amount"] is None:
                if (
                    not isinstance(item["missing_reason"], str)
                    or item["missing_reason"] not in MISSING_REASONS
                    or item["currency"] is not None
                ):
                    raise EconomicsError("service_cost_missing_reason_required")
            else:
                _decimal(
                    item["amount"],
                    code="service_cost_amount_invalid",
                    high=Decimal("1000000000000"),
                )
                if (
                    item["missing_reason"] is not None
                    or not isinstance(item["currency"], str)
                    or CURRENCY.fullmatch(item["currency"]) is None
                ):
                    raise EconomicsError("service_cost_currency_invalid")
                if currencies and item["currency"] not in currencies:
                    raise EconomicsError("service_cost_currency_mismatch")
                currencies.add(item["currency"])

        output = _keys(
            run["output"],
            {
                "mode",
                "generated_output_tokens",
                "generated_output_missing_reason",
                "followup_requests",
                "repair_requests",
                "artifact_delivery_attempted",
                "artifact_delivery_success",
                "artifact_reconstructible",
            },
            "output_contract_invalid",
        )
        if not isinstance(output["mode"], str) or output["mode"] not in {
            "generated",
            "deterministic_artifact",
        }:
            raise EconomicsError("output_mode_invalid")
        if output["generated_output_tokens"] is None:
            if (
                not isinstance(output["generated_output_missing_reason"], str)
                or output["generated_output_missing_reason"] not in MISSING_REASONS
            ):
                raise EconomicsError("output_tokens_missing_reason_required")
        else:
            _integer(output["generated_output_tokens"])
            if output["generated_output_missing_reason"] is not None:
                raise EconomicsError("output_tokens_missing_reason_unexpected")
        _integer(output["followup_requests"], high=10000)
        _integer(output["repair_requests"], high=10000)
        if type(output["artifact_delivery_attempted"]) is not bool:
            raise EconomicsError("artifact_delivery_state_invalid")
        if output["mode"] == "deterministic_artifact":
            if (
                output["artifact_delivery_attempted"] is not True
                or type(output["artifact_delivery_success"]) is not bool
                or type(output["artifact_reconstructible"]) is not bool
            ):
                raise EconomicsError("artifact_outcome_required")
            if output["artifact_delivery_success"] and not output["artifact_reconstructible"]:
                raise EconomicsError("artifact_reconstruction_failed")
        elif (
            output["artifact_delivery_attempted"]
            or output["artifact_delivery_success"] is not None
            or output["artifact_reconstructible"] is not None
        ):
            raise EconomicsError("artifact_fields_unexpected")
        quality = _validate_quality(run["quality"])
        if condition_id in BASE_CONDITIONS and scores_by_assignment.get(assignment_id) != quality:
            raise EconomicsError("run_score_mismatch")
        if run["status"] != "completed" and any(value != "unscored" for value in quality.values()):
            raise EconomicsError("noncompleted_run_scored")
        normalized = dict(run)
        normalized["requests"] = normalized_requests
        normalized["request_totals"] = request_totals
        normalized["request_unknown"] = request_unknown
        normalized["quality"] = quality
        run_index[key] = normalized

    # Every baseline row is present, including `not_run`; ablations are allowed
    # to be incomplete and their coverage is reported rather than filled in.
    for assignment_id, assigned in assignments.items():
        for condition_id, arm in BASE_CONDITIONS.items():
            if assigned["arm"] == arm and (assignment_id, condition_id) not in run_index:
                raise EconomicsError("baseline_run_missing")

    rate_cards_by_model = rate_cards

    scenarios = root["sensitivity_scenarios"]
    if not isinstance(scenarios, list) or len(scenarios) > 32:
        raise EconomicsError("sensitivity_scenarios_invalid")
    scenario_ids: set[str] = set()
    for scenario in scenarios:
        item = _keys(
            scenario,
            {"scenario_id", "adoption_fraction", "rate_multiplier", "cache_price_multiplier"},
            "sensitivity_scenario_invalid",
        )
        scenario_id = _identifier(item["scenario_id"])
        if scenario_id in scenario_ids:
            raise EconomicsError("sensitivity_scenario_duplicate")
        scenario_ids.add(scenario_id)
        _decimal(item["adoption_fraction"], code="adoption_fraction_invalid", high=Decimal(1))
        _decimal(item["rate_multiplier"], code="rate_multiplier_invalid", high=Decimal(10))
        _decimal(
            item["cache_price_multiplier"], code="cache_price_multiplier_invalid", high=Decimal(10)
        )

    root["_normalized"] = {
        "task_strata": task_strata,
        "assignments": assignments,
        "pair_groups": pair_groups,
        "observations": observation_by_id,
        "scores": scores_by_assignment,
        "score_status": score_status_by_assignment,
        "runs": run_index,
        "profile_deviations": profile_deviations,
        "condition_profiles": profiles,
        "rates": rate_cards_by_model,
        "rate_card_meta": rate_card_meta,
        "rates_are_placeholder": rates_are_placeholder,
        "weights": {key: _decimal(amount) for key, amount in weights.items()},
        "refresh_max_age_hours": plan["refresh_max_age_hours"],
        "plan": {
            "minimum_completed_per_arm": plan["minimum_completed_per_arm"],
            "target_participants": plan["target_participants"],
        },
        "allocation_verification": allocation_verification,
        "all_assignment_count": len(all_assignments),
        "withdrawn_participant_count": len(withdrawn),
        "withdrawn_by_arm": {arm: withdrawn_by_arm.get(arm, 0) for arm in ARMS},
    }
    return root


def _decimal_text(value: Decimal | None) -> str | None:
    if value is None:
        return None
    if value.as_tuple().exponent < -8:
        with localcontext() as context:
            context.prec = 80
            value = value.quantize(Decimal("0.00000001"))
    normalized = value.normalize()
    return format(normalized, "f") if normalized else "0"


def _run_cost(
    run: dict[str, Any],
    rates: dict[str, Any],
    *,
    rate_multiplier: Decimal = Decimal(1),
    cache_multiplier: Decimal = Decimal(1),
) -> tuple[Decimal | None, str | None, list[str]]:
    total = Decimal(0)
    known_currencies = {card["currency"] for card in rates.values()}
    currency: str | None = next(iter(known_currencies)) if len(known_currencies) == 1 else None
    unknown: list[str] = []
    if run["request_ledger_status"] == "unknown":
        unknown.append("request_ledger_unknown:" + run["request_ledger_missing_reason"])
    if (
        not run["requests"]
        and run["request_ledger_status"] == "complete"
        and run["status"]
        not in {
            "unsupported",
            "bypassed",
            "not_run",
        }
    ):
        unknown.append("empty_request_ledger")
    with localcontext() as context:
        context.prec = 60
        for request in run["requests"]:
            card = rates.get(request["model"])
            if card is None:
                unknown.append("rate_missing:" + request["model"])
                continue
            if currency is not None and currency != card["currency"]:
                unknown.append("mixed_currency")
                continue
            currency = card["currency"]
            usage = request["normalized_usage"]
            for bucket in USAGE_BUCKETS:
                amount = usage[bucket]
                if amount is None:
                    unknown.append("usage_missing:" + bucket)
                    continue
                rate = card["rates"][RATE_KEYS[bucket]] * rate_multiplier
                if bucket in {
                    "cache_write_5m_tokens",
                    "cache_write_1h_tokens",
                    "cache_read_tokens",
                }:
                    rate *= cache_multiplier
                total += Decimal(amount) * rate / Decimal(1_000_000)
        if run["service_cost_status"] == "unknown":
            unknown.append("service_cost_status_unknown:" + run["service_cost_missing_reason"])
        for item in run["incremental_service_costs"]:
            if item["amount"] is None:
                unknown.append("service_cost_missing:" + item["category"])
                continue
            if currency is not None and currency != item["currency"]:
                unknown.append("mixed_currency")
                continue
            currency = item["currency"]
            total += Decimal(item["amount"])
    return (None if unknown else total), currency, sorted(set(unknown))


def _successful(status: str, quality: dict[str, str]) -> bool:
    return (
        status == "completed"
        and quality["correctness"] == "correct"
        and quality["completeness"] == "complete"
        and quality["citations"] in {"complete", "not_required"}
        and quality["unsupported_claim"] == "no"
    )


def _aggregate_costs(
    run_rows: list[tuple[dict[str, Any], dict[str, Any]]],
    rates: dict[str, Any],
    *,
    rate_multiplier: Decimal = Decimal(1),
    cache_multiplier: Decimal = Decimal(1),
) -> dict[str, Any]:
    costs: list[Decimal | None] = []
    currencies: set[str] = set()
    reasons: Counter[str] = Counter()
    successful = 0
    completed = 0
    statuses: Counter[str] = Counter()
    retry_total = 0
    latency: list[int] = []
    followups = repairs = billed_output = 0
    artifact_attempts = artifact_success = artifact_reconstructed = 0
    output_tokens_known = 0
    output_tokens_sum = 0
    request_ledger_complete = True
    actual_request_count = 0
    for run, _assignment in run_rows:
        cost, currency, unknown = _run_cost(
            run, rates, rate_multiplier=rate_multiplier, cache_multiplier=cache_multiplier
        )
        costs.append(cost)
        if currency is not None:
            currencies.add(currency)
        reasons.update(unknown)
        statuses[run["status"]] += 1
        request_ledger_complete = (
            request_ledger_complete and run["request_ledger_status"] == "complete"
        )
        actual_request_count += len(run["requests"])
        if run["status"] == "completed":
            completed += 1
        if _successful(run["status"], run["quality"]):
            successful += 1
        if run["retry_count"] is not None:
            retry_total += run["retry_count"]
        if run["latency_ms"] is not None:
            latency.append(run["latency_ms"])
        output = run["output"]
        followups += output["followup_requests"]
        repairs += output["repair_requests"]
        if output["generated_output_tokens"] is not None:
            output_tokens_known += 1
            output_tokens_sum += output["generated_output_tokens"]
        if output["mode"] == "deterministic_artifact":
            artifact_attempts += 1
            artifact_success += int(output["artifact_delivery_success"] is True)
            artifact_reconstructed += int(output["artifact_reconstructible"] is True)
        billed_output += sum(
            request["normalized_usage"]["billed_output_tokens"] or 0 for request in run["requests"]
        )
        if any(
            request["normalized_usage"]["billed_output_tokens"] is None
            for request in run["requests"]
        ):
            reasons["usage_missing:billed_output_tokens"] += 1
    complete_costs = len(costs) == 0 or all(cost is not None for cost in costs)
    total = (
        sum((cost for cost in costs if cost is not None), Decimal(0)) if complete_costs else None
    )
    assigned = len(run_rows)
    currency = next(iter(currencies)) if len(currencies) == 1 else None
    return {
        "assigned_count": assigned,
        "completed_count": completed,
        "request_ledger_status": "complete" if request_ledger_complete else "partial_or_unknown",
        "request_count": actual_request_count,
        "successful_count": successful,
        "outcomes": dict(sorted(statuses.items())),
        "retry_count": retry_total
        if all(run["retry_count"] is not None for run, _ in run_rows)
        else None,
        "retry_count_observed": sum(run["retry_count"] is not None for run, _ in run_rows),
        "latency_ms_total": sum(latency) if latency and len(latency) == assigned else None,
        "latency_observation_count": len(latency),
        "followup_requests": followups,
        "repair_requests": repairs,
        "generated_output_tokens_total": output_tokens_sum
        if output_tokens_known == assigned
        else None,
        "generated_output_tokens_observation_count": output_tokens_known,
        "billed_output_tokens_total": billed_output
        if not any("usage_missing:billed_output_tokens" in reason for reason in reasons)
        else None,
        "artifact_delivery_attempts": artifact_attempts,
        "artifact_delivery_successes": artifact_success,
        "artifact_reconstruction_successes": artifact_reconstructed,
        "cost_coverage_count": sum(cost is not None for cost in costs),
        "currency": currency,
        "total_cost": _decimal_text(total),
        "cost_per_assigned_task": _decimal_text(total / Decimal(assigned))
        if total is not None and assigned
        else None,
        "cost_per_successful_task": _decimal_text(total / Decimal(successful))
        if total is not None and successful
        else None,
        "cost_per_successful_unavailable_reason": "no_successful_tasks"
        if successful == 0
        else None,
        "cost_per_assigned_task_basis": _point_basis(assigned),
        "cost_per_successful_task_basis": _point_basis(successful),
        "unknown_cost_inputs": dict(sorted(reasons.items())),
    }


def _point_basis(n: int) -> dict[str, Any]:
    """Cost ratios are point estimates; their denominator count is always reported."""
    return {"kind": "point_estimate", "n": n}


def _run_rows_for_condition(
    normalized: dict[str, Any], condition: str, *, stratum: str | None = None
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    rows: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for (assignment_id, condition_id), run in normalized["runs"].items():
        if condition_id != condition:
            continue
        assignment = normalized["assignments"][assignment_id]
        if stratum is None or assignment["stratum"] == stratum:
            rows.append((run, assignment))
    return rows


def _comparability(left: dict[str, Any], right: dict[str, Any]) -> tuple[bool, list[str]]:
    fields = (
        ("source_snapshot_sha256", "source_snapshot_mismatch"),
        ("model", "model_mismatch"),
        ("settings", "settings_mismatch"),
        ("counter_version", "counter_version_mismatch"),
    )
    mismatch = [
        label
        for field, label in fields
        if not left.get(field) or left.get(field) != right.get(field)
    ]
    return not mismatch, mismatch


def _gate_units(minimum: int) -> int:
    """Predeclared pair minimum. evidence_stats needs at least two units for any interval."""
    return max(2, minimum)


def _gate_clusters(minimum: int) -> int:
    """Predeclared participant target. evidence_stats needs at least two clusters."""
    return max(2, minimum)


def _interval_json(interval: Interval) -> dict[str, Any]:
    return {
        "estimate": _decimal_text(interval.estimate),
        "lower": _decimal_text(interval.lower),
        "upper": _decimal_text(interval.upper),
        "confidence": _decimal_text(interval.confidence),
        "method": interval.method,
        "resamples": interval.resamples,
        "seed": interval.seed,
        "n_units": interval.n_units,
        "n_clusters": interval.n_clusters,
    }


def _savings_block(
    units: list[PairedUnit],
    normalized: dict[str, Any],
    *,
    evidence_class: str,
    provenance_verified: bool,
) -> dict[str, Any]:
    """Cluster-bootstrap interval and the savings decision for (candidate - reference) cost."""
    interval = paired_difference_interval(units, seed=BOOTSTRAP_SEED)
    decision = savings_decision(
        interval,
        min_units=_gate_units(normalized["plan"]["minimum_completed_per_arm"]),
        min_clusters=_gate_clusters(normalized["plan"]["target_participants"]),
        evidence_class=evidence_class,
        provenance_verified=provenance_verified,
    )
    return {"interval": _interval_json(interval), "decision": decision}


def _paired_summary(
    normalized: dict[str, Any],
    reference: str,
    candidate: str,
    rates: dict[str, Any],
    *,
    evidence_class: str,
    provenance_verified: bool,
) -> dict[str, Any]:
    compared = matched = 0
    differences: list[Decimal] = []
    units: list[PairedUnit] = []
    reasons: Counter[str] = Counter()
    profile_unknown_fields = [
        f"{condition}.{field}"
        for condition in (reference, candidate)
        for field in ("model", "settings", "effort", "concise_instructions")
        if normalized["condition_profiles"][condition][field] in {None, "unknown"}
    ]
    for group in normalized["pair_groups"].values():
        arm_ids = {
            normalized["assignments"][assignment_id]["arm"]: assignment_id
            for assignment_id in group.values()
        }
        ref_arm = BASE_CONDITIONS.get(reference, "B")
        cand_arm = BASE_CONDITIONS.get(candidate, "B")
        ref_id, cand_id = arm_ids.get(ref_arm), arm_ids.get(cand_arm)
        if ref_id is None or cand_id is None:
            continue
        ref = normalized["runs"].get((ref_id, reference))
        cand = normalized["runs"].get((cand_id, candidate))
        if ref is None or cand is None:
            continue
        compared += 1
        if profile_unknown_fields:
            reasons["condition_profile_unknown"] += 1
            continue
        profile_mismatch = list(normalized["profile_deviations"].get((ref_id, reference), []))
        profile_mismatch.extend(normalized["profile_deviations"].get((cand_id, candidate), []))
        comparable, mismatch = _comparability(ref, cand)
        if profile_mismatch or not comparable:
            reasons.update(profile_mismatch)
            reasons.update(mismatch)
            continue
        ref_cost, _, ref_unknown = _run_cost(ref, rates)
        cand_cost, _, cand_unknown = _run_cost(cand, rates)
        if ref_cost is None or cand_cost is None:
            reasons.update(ref_unknown + cand_unknown)
            reasons["pair_cost_unavailable"] += 1
            continue
        difference = cand_cost - ref_cost
        differences.append(difference)
        units.append(
            PairedUnit(
                cluster_id=normalized["assignments"][ref_id]["participant_id"],
                difference=difference,
            )
        )
        matched += 1
    mean = sum(differences, Decimal(0)) / Decimal(matched) if matched else None
    return {
        "reference_condition": reference,
        "candidate_condition": candidate,
        "pair_count": compared,
        "matched_pair_count": matched,
        "mismatch_or_unavailable_reasons": dict(sorted(reasons.items())),
        "condition_profile_unknown_fields": profile_unknown_fields,
        "signed_candidate_minus_reference_cost_mean": _decimal_text(mean),
        "currency": next(iter({card["currency"] for card in rates.values()}), None),
        **_savings_block(
            units,
            normalized,
            evidence_class=evidence_class,
            provenance_verified=provenance_verified,
        ),
    }


def _weighted_summary(
    normalized: dict[str, Any], condition: str, rates: dict[str, Any]
) -> dict[str, Any]:
    strata: dict[str, Any] = {}
    weighted_cost_per_assigned = Decimal(0)
    weighted_success_rate = Decimal(0)
    missing_cost: list[str] = []
    missing_success_rate: list[str] = []
    for stratum, weight in normalized["weights"].items():
        rows = _run_rows_for_condition(normalized, condition, stratum=stratum)
        summary = _aggregate_costs(rows, rates)
        strata[stratum] = {**summary, "declared_weight": _decimal_text(weight)}
        count = summary["assigned_count"]
        if summary["total_cost"] is None or count == 0:
            missing_cost.append(stratum)
        else:
            weighted_cost_per_assigned += weight * Decimal(summary["total_cost"]) / Decimal(count)
        if count == 0:
            missing_success_rate.append(stratum)
        else:
            weighted_success_rate += weight * Decimal(summary["successful_count"]) / Decimal(count)
    weighted_assigned = None if missing_cost else weighted_cost_per_assigned
    weighted_success = None
    success_unavailable_reason = None
    if missing_success_rate:
        success_unavailable_reason = "missing_stratum_assignments"
    elif weighted_success_rate == 0:
        success_unavailable_reason = "no_successful_tasks"
    elif weighted_assigned is None:
        success_unavailable_reason = "cost_unavailable"
    else:
        weighted_success = weighted_assigned / weighted_success_rate
    return {
        "by_stratum": strata,
        "weighted_success_rate": _decimal_text(
            None if missing_success_rate else weighted_success_rate
        ),
        "weighted_cost_per_assigned_task": _decimal_text(weighted_assigned),
        "weighted_cost_per_successful_task": _decimal_text(weighted_success),
        "weighted_assigned_unavailable_strata": missing_cost,
        "weighted_success_unavailable_reason": success_unavailable_reason,
        "weighted_cost_per_successful_task_basis": _point_basis(
            sum(entry["successful_count"] for entry in strata.values())
        ),
        "currency": next(iter({card["currency"] for card in rates.values()}), None),
    }


def _payload_estimate(normalized: dict[str, Any]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for condition in BASE_CONDITIONS:
        rows = _run_rows_for_condition(normalized, condition)
        estimates: dict[str, list[int]] = {
            key: []
            for key in (
                "estimated_input_tokens",
                "estimated_output_tokens",
                "schema_instruction_tokens",
                "tool_call_count",
                "retrieval_bytes",
            )
        }
        counters: set[str] = set()
        for run, _assignment in rows:
            observation = normalized["observations"][run["assignment_id"]]
            for key in estimates:
                value = observation["metrics"][key]
                if value is not None:
                    estimates[key].append(value)
            counter = observation["counter_version"]
            if counter is not None:
                counters.add(counter)
        output[condition] = {
            "assignment_count": len(rows),
            "counter_versions": sorted(counters),
            "metrics": {
                key: {
                    "sum": sum(values) if len(values) == len(rows) else None,
                    "observed_count": len(values),
                }
                for key, values in estimates.items()
            },
            "evidence_note": "Token and payload estimates are diagnostic and are not modeled costs or provider billing.",
        }
    return output


def _output_contracts(normalized: dict[str, Any]) -> dict[str, Any]:
    groups: dict[str, list[tuple[dict[str, Any], dict[str, Any]]]] = defaultdict(list)
    for run in normalized["runs"].values():
        assignment = normalized["assignments"][run["assignment_id"]]
        groups[run["output"]["mode"]].append((run, assignment))
    report: dict[str, Any] = {}
    for mode, rows in sorted(groups.items()):
        aggregate = _aggregate_costs(rows, normalized["rates"])
        report[mode] = {
            "run_count": len(rows),
            "completed_count": aggregate["completed_count"],
            "successful_count": aggregate["successful_count"],
            "followup_requests": aggregate["followup_requests"],
            "repair_requests": aggregate["repair_requests"],
            "billed_output_tokens_total": aggregate["billed_output_tokens_total"],
            "generated_output_tokens_total": aggregate["generated_output_tokens_total"],
            "artifact_delivery_attempts": aggregate["artifact_delivery_attempts"],
            "artifact_delivery_successes": aggregate["artifact_delivery_successes"],
            "artifact_reconstruction_successes": aggregate["artifact_reconstruction_successes"],
            "modeled_total_cost": aggregate["total_cost"],
            "currency": aggregate["currency"],
            "note": "Artifact output does not imply free model output; all billed output tokens and incremental service costs remain in task costs.",
        }
    return report


def _sensitivity(
    normalized: dict[str, Any], scenarios: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    rates = normalized["rates"]
    for scenario in scenarios:
        adoption = Decimal(scenario["adoption_fraction"])
        rate_mult = Decimal(scenario["rate_multiplier"])
        cache_mult = Decimal(scenario["cache_price_multiplier"])
        weighted_base = Decimal(0)
        weighted_package = Decimal(0)
        unavailable: set[str] = set()
        for stratum, weight in normalized["weights"].items():
            base_rows = _run_rows_for_condition(normalized, "C_normal", stratum=stratum)
            package_rows = _run_rows_for_condition(normalized, "B_full", stratum=stratum)
            base_costs = [
                _run_cost(run, rates, rate_multiplier=rate_mult, cache_multiplier=cache_mult)[0]
                for run, _ in base_rows
            ]
            package_costs = [
                _run_cost(run, rates, rate_multiplier=rate_mult, cache_multiplier=cache_mult)[0]
                for run, _ in package_rows
            ]
            if (
                not base_costs
                or len(base_costs) != len(package_costs)
                or any(cost is None for cost in base_costs + package_costs)
            ):
                unavailable.add(stratum)
                continue
            base_mean = sum(
                (cost for cost in base_costs if cost is not None), Decimal(0)
            ) / Decimal(len(base_costs))
            package_mean = sum(
                (cost for cost in package_costs if cost is not None), Decimal(0)
            ) / Decimal(len(package_costs))
            weighted_base += weight * base_mean
            weighted_package += weight * package_mean
        expected = (Decimal(1) - adoption) * weighted_base + adoption * weighted_package
        out.append(
            {
                "scenario_id": scenario["scenario_id"],
                "adoption_fraction": scenario["adoption_fraction"],
                "rate_multiplier": scenario["rate_multiplier"],
                "cache_price_multiplier": scenario["cache_price_multiplier"],
                "weighted_expected_cost_per_assigned_task": _decimal_text(
                    None if unavailable else expected
                ),
                "signed_difference_vs_native_normal": _decimal_text(
                    None if unavailable else expected - weighted_base
                ),
                "unavailable_strata": sorted(unavailable),
                "currency": next(iter({card["currency"] for card in rates.values()}), None),
                "scenario_only": True,
            }
        )
    return out


def _enterprise_units(windows: list[dict[str, Any]]) -> list[PairedUnit]:
    """Per-participant (candidate - reference) window cost, when both arms have a priced window."""
    by_pair = {(window["participant_id"], window["arm"]): window for window in windows}
    units: list[PairedUnit] = []
    for participant in sorted({key[0] for key in by_pair}):
        reference = by_pair.get((participant, BASE_CONDITIONS[PRIMARY_REFERENCE]))
        candidate = by_pair.get((participant, BASE_CONDITIONS[PRIMARY_CANDIDATE]))
        if reference is None or candidate is None:
            continue
        if reference["cost_amount"] is None or candidate["cost_amount"] is None:
            continue
        if reference["currency"] is None or reference["currency"] != candidate["currency"]:
            continue
        units.append(
            PairedUnit(
                cluster_id=participant,
                difference=Decimal(candidate["cost_amount"]) - Decimal(reference["cost_amount"]),
            )
        )
    return units


def _enterprise_observed(
    data: dict[str, Any],
    normalized: dict[str, Any],
    export_digests: set[str],
    provenance_verified: bool,
) -> dict[str, Any]:
    assignments = normalized["assignments"]
    observations = normalized["observations"]
    windows = data["usage_windows"]
    real_ids = {
        row["participant_id"]
        for row in assignments.values()
        if row["participant_kind"] == "pseudonymous"
    }
    expected: dict[tuple[str, str], set[str]] = defaultdict(set)
    all_pairs: set[tuple[str, str]] = set()
    assigned_by_arm: Counter[str] = Counter()
    successful_by_arm: Counter[str] = Counter()
    scores_complete = bool(real_ids)
    for assignment_id, assignment in assignments.items():
        arm = assignment["arm"]
        assigned_by_arm[arm] += 1
        key = (assignment["participant_id"], arm)
        all_pairs.add(key)
        if observations[assignment_id]["status"] != "not_run":
            expected[key].add(assignment_id)
        if assignment["participant_kind"] == "pseudonymous":
            score_status = normalized["score_status"].get(assignment_id)
            if (
                score_status is None
                or (
                    observations[assignment_id]["status"] == "completed"
                    and score_status != "scored"
                )
                or (
                    observations[assignment_id]["status"] != "completed"
                    and score_status != "no_score"
                )
            ):
                scores_complete = False
            elif score_status == "scored" and _successful(
                observations[assignment_id]["status"], normalized["scores"][assignment_id]
            ):
                successful_by_arm[arm] += 1

    windows_by_pair: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    freshness: list[bool] = []
    window_assignment_ids: set[str] = set()
    for window in windows:
        pair = (window["participant_id"], window["arm"])
        windows_by_pair[pair].append(window)
        window_assignment_ids.update(window["assignment_ids"])
        age_seconds = (
            _datetime_utc(data["as_of_utc"]) - _datetime_utc(window["refreshed_at_utc"])
        ).total_seconds()
        freshness.append(0 <= age_seconds <= normalized["refresh_max_age_hours"] * 3600)

    mapping_complete = (
        bool(windows)
        and set(windows_by_pair) == all_pairs
        and all(
            len(windows_by_pair[key]) == 1
            and set(windows_by_pair[key][0]["assignment_ids"]) == expected.get(key, set())
            for key in all_pairs
        )
    )
    no_duplicate_assignments = sum(len(window["assignment_ids"]) for window in windows) == len(
        window_assignment_ids
    )
    fresh = bool(windows) and len(freshness) == len(windows) and all(freshness)

    def window_attribution_documented(window: dict[str, Any]) -> bool:
        activity_classified = window["unrelated_activity"] == "none" or (
            window["unrelated_activity"] == "present_excluded" and bool(window["exclusion_codes"])
        )
        return (
            window["mapping_confidence"] in {"high", "medium"}
            and window["exclusions_recorded"] is True
            and activity_classified
        )

    attribution_documented = bool(windows) and all(
        window_attribution_documented(window) for window in windows
    )
    values_available = bool(windows) and all(
        any(value is not None for value in window["usage_buckets"].values()) for window in windows
    )
    values_complete = bool(windows) and all(
        all(value is not None for value in window["usage_buckets"].values()) for window in windows
    )
    costs_complete = bool(windows) and all(window["cost_amount"] is not None for window in windows)
    costs_currency_consistent = all(
        len(
            {
                window["currency"]
                for window in windows
                if window["arm"] == arm and window["currency"] is not None
            }
        )
        <= 1
        for arm in ARMS
    )
    costs_complete = costs_complete and costs_currency_consistent

    by_arm: dict[str, Any] = {}
    for arm in ARMS:
        rows = [window for window in windows if window["arm"] == arm]
        arm_pairs = {pair for pair in all_pairs if pair[1] == arm}
        arm_mapping_complete = bool(arm_pairs) and all(
            len(windows_by_pair[pair]) == 1
            and set(windows_by_pair[pair][0]["assignment_ids"]) == expected.get(pair, set())
            for pair in arm_pairs
        )
        arm_fresh = bool(rows) and all(
            0
            <= (
                _datetime_utc(data["as_of_utc"]) - _datetime_utc(window["refreshed_at_utc"])
            ).total_seconds()
            <= normalized["refresh_max_age_hours"] * 3600
            for window in rows
        )
        arm_attribution_documented = bool(rows) and all(
            window_attribution_documented(window) for window in rows
        )
        arm_evidence_usable = arm_mapping_complete and arm_fresh and arm_attribution_documented
        count = assigned_by_arm[arm]
        currency_set = {window["currency"] for window in rows if window["currency"] is not None}
        cost_total = None
        if (
            arm_evidence_usable
            and rows
            and all(window["cost_amount"] is not None for window in rows)
            and len(currency_set) == 1
        ):
            cost_total = sum((Decimal(window["cost_amount"]) for window in rows), Decimal(0))
        usage_totals: dict[str, str | None] = {}
        usage_coverage: dict[str, int] = {}
        for bucket in USAGE_BUCKETS:
            coverage = sum(window["usage_buckets"][bucket] is not None for window in rows)
            usage_coverage[bucket] = coverage
            usage_totals[bucket] = (
                str(sum(window["usage_buckets"][bucket] for window in rows))
                if arm_evidence_usable and rows and coverage == len(rows)
                else None
            )
        successes = successful_by_arm[arm]
        by_arm[arm] = {
            "assigned_count": count,
            "successful_quality_scored_count": successes,
            "window_count": len(rows),
            "mapping_complete": arm_mapping_complete,
            "freshness_complete": arm_fresh,
            "attribution_documented": arm_attribution_documented,
            "usage_bucket_totals": usage_totals,
            "usage_bucket_window_coverage": usage_coverage,
            "window_cost_total": _decimal_text(cost_total),
            "currency": next(iter(currency_set)) if len(currency_set) == 1 else None,
            "cost_per_assigned_task": _decimal_text(cost_total / Decimal(count))
            if cost_total is not None and count
            else None,
            "cost_per_successful_task": _decimal_text(cost_total / Decimal(successes))
            if cost_total is not None and scores_complete and successes
            else None,
            "cost_per_successful_task_basis": _point_basis(successes),
            "cost_per_successful_unavailable_reason": (
                "blind_scoring_incomplete"
                if not scores_complete
                else "no_successful_tasks"
                if successes == 0
                else None
            ),
        }

    if data["evidence_class"] == "synthetic":
        status, reason = "pending", "synthetic_fixture_has_no_enterprise_usage"
    elif not real_ids:
        status, reason = "pending", "synthetic_fixture_has_no_enterprise_usage"
    elif not windows or not fresh:
        status, reason = "pending", "fresh_admin_usage_windows_missing_or_stale"
    elif not mapping_complete or not no_duplicate_assignments:
        status, reason = "pending", "usage_window_mapping_incomplete"
    elif not attribution_documented:
        status, reason = "pending", "mapping_or_exclusion_evidence_incomplete"
    elif not provenance_verified:
        status, reason = "admin_export_unverified", "admin_export_unverified"
    elif not values_available:
        status, reason = "pending", "usage_measurements_unavailable"
    elif not values_complete:
        status, reason = "partial", "usage_measurements_incomplete"
    elif not costs_complete:
        status, reason = "partial", "cost_measurements_unavailable"
    elif not scores_complete:
        status, reason = "partial", "blind_scoring_incomplete"
    else:
        status, reason = "available_with_attribution_limits", None
    currencies = {window["currency"] for window in windows if window["currency"] is not None}
    usable = status == "available_with_attribution_limits"
    decision = savings_decision(
        paired_difference_interval(_enterprise_units(windows), seed=BOOTSTRAP_SEED),
        min_units=_gate_units(normalized["plan"]["minimum_completed_per_arm"]),
        min_clusters=_gate_clusters(normalized["plan"]["target_participants"]),
        evidence_class=data["evidence_class"],
        provenance_verified=provenance_verified,
    )
    if not usable:
        decision = {
            "claim": "none",
            "reasons": [*decision["reasons"], "enterprise_observed_incomplete"],
            "rule": decision["rule"],
        }
    return {
        "status": status,
        "enterprise_usage_observed": bool(real_ids)
        and provenance_verified
        and status in {"partial", "available_with_attribution_limits"},
        "provenance_verified": provenance_verified,
        "reason": reason,
        "window_count": len(windows),
        "fresh_window_count": sum(freshness),
        "mapping_complete": mapping_complete,
        "blind_scoring_complete": scores_complete,
        "data_coverage": "complete"
        if values_complete
        else "partial"
        if values_available
        else "unavailable",
        "cost_coverage": "complete"
        if costs_complete
        else "partial"
        if any(window["cost_amount"] is not None for window in windows)
        else "unavailable",
        "currency": next(iter(currencies)) if len(currencies) == 1 else None,
        "by_arm": by_arm,
        "windows": [
            {
                "arm": window["arm"],
                "assignment_count": len(window["assignment_ids"]),
                "start_utc": window["start_utc"],
                "end_utc": window["end_utc"],
                "refreshed_at_utc": window["refreshed_at_utc"],
                "mapping_confidence": window["mapping_confidence"],
                "exclusions_recorded": window["exclusions_recorded"],
                "unrelated_activity": window["unrelated_activity"],
                "exclusion_codes": list(window["exclusion_codes"]),
                "usage_buckets": dict(window["usage_buckets"]),
                "cost_amount": window["cost_amount"],
                "cost_missing_reason": window["cost_missing_reason"],
                "currency": window["currency"],
                "export_verified": window["export_sha256"] in export_digests,
            }
            for window in windows
        ],
        "causal_claim": False,
        "savings_claim": decision["claim"],
        "savings_decision": decision,
        "allocation_note": "Admin window totals remain per window and are not split across requests or tasks.",
    }


def _export_records(value: Any) -> list[dict[str, Any]]:
    """Admin export digests supplied by the caller; only sha256 and byte size are kept."""
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > 256:
        raise EconomicsError("admin_exports_invalid")
    records: list[dict[str, Any]] = []
    for raw in value:
        item = _keys(raw, {"sha256", "bytes"}, "admin_exports_invalid")
        if not isinstance(item["sha256"], str) or SHA256.fullmatch(item["sha256"]) is None:
            raise EconomicsError("admin_exports_invalid")
        _integer(item["bytes"], high=MAX_ADMIN_EXPORT_BYTES, code="admin_exports_invalid")
        records.append({"sha256": item["sha256"], "bytes": item["bytes"]})
    return sorted(records, key=lambda record: (record["sha256"], record["bytes"]))


def _read_admin_export(path: Path) -> dict[str, Any]:
    """Hash an admin export in bounded chunks. The path is never returned or stored."""
    digest = hashlib.sha256()
    size = 0
    try:
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_ADMIN_EXPORT_BYTES:
                    raise EconomicsError("admin_export_too_large")
                digest.update(chunk)
    except OSError:
        raise EconomicsError("admin_export_unavailable") from None
    return {"sha256": digest.hexdigest(), "bytes": size}


def _label_placeholder_units(value: Any) -> Any:
    """Relabel every money amount as placeholder units and drop currency fields."""
    if isinstance(value, dict):
        labelled: dict[str, Any] = {}
        for key, item in value.items():
            if key == "currency":
                labelled[key] = None
            elif key in PLACEHOLDER_AMOUNT_KEYS and (item is None or isinstance(item, str)):
                labelled[key] = None if item is None else {"placeholder_units": item}
            else:
                labelled[key] = _label_placeholder_units(item)
        return labelled
    if isinstance(value, list):
        return [_label_placeholder_units(item) for item in value]
    return value


def _factorial_section(
    normalized: dict[str, Any], *, evidence_class: str, provenance_verified: bool
) -> dict[str, Any]:
    cells: list[FactorialCell] = []
    for (assignment_id, condition_id), run in normalized["runs"].items():
        if condition_id not in FACTORIAL_CONDITIONS:
            continue
        assignment = normalized["assignments"][assignment_id]
        cost, _currency, _unknown = _run_cost(run, normalized["rates"])
        cells.append(
            FactorialCell(
                unit_id=assignment["pair_id"],
                cluster_id=assignment["participant_id"],
                arm=condition_id,
                cost=cost,
                success=_successful(run["status"], run["quality"]),
            )
        )
    if not cells:
        return {"status": "not_run"}
    try:
        return analyze_factorial(
            cells,
            seed=BOOTSTRAP_SEED,
            min_units=_gate_units(normalized["plan"]["minimum_completed_per_arm"]),
            min_clusters=_gate_clusters(normalized["plan"]["target_participants"]),
            evidence_class=evidence_class,
            provenance_verified=provenance_verified,
        )
    except FactorialError:
        raise EconomicsError("factorial_invalid") from None


def _build_report(
    value: Any, admin_exports: list[dict[str, Any]], withdrawal_doc: Any = None
) -> dict[str, Any]:
    input_digest = _canonical_digest(value)
    records = _export_records(admin_exports)
    export_digests = {record["sha256"] for record in records}
    data = _validate_input(value, withdrawal_doc)
    normalized = data.pop("_normalized")
    rates = normalized["rates"]
    placeholder = normalized["rates_are_placeholder"]
    evidence_class = data["evidence_class"]
    windows = data["usage_windows"]
    window_verified = [window["export_sha256"] in export_digests for window in windows]
    provenance_verified = evidence_class != "synthetic" and bool(windows) and all(window_verified)
    conditions: dict[str, Any] = {}
    for condition in sorted(CONDITIONS):
        rows = _run_rows_for_condition(normalized, condition)
        if not rows:
            continue
        conditions[condition] = {
            "costs": _aggregate_costs(rows, rates),
            "weighted": _weighted_summary(normalized, condition, rates),
        }
    paired = [
        _paired_summary(
            normalized,
            reference,
            candidate,
            rates,
            evidence_class=evidence_class,
            provenance_verified=provenance_verified,
        )
        for reference, candidate in (
            (PRIMARY_REFERENCE, PRIMARY_CANDIDATE),
            ("C_tuned", PRIMARY_CANDIDATE),
            ("A", PRIMARY_CANDIDATE),
        )
    ]
    for ablation in sorted(ABLATIONS):
        paired.append(
            _paired_summary(
                normalized,
                ablation,
                PRIMARY_CANDIDATE,
                rates,
                evidence_class=evidence_class,
                provenance_verified=provenance_verified,
            )
        )
    primary = paired[0]
    savings_claim = primary["decision"]["claim"]
    workload_outcomes: dict[str, dict[str, int | str]] = {}
    for arm in ARMS:
        rows = [
            (assignment_id, assignment)
            for assignment_id, assignment in normalized["assignments"].items()
            if assignment["arm"] == arm
        ]
        counts = Counter(
            normalized["observations"][assignment_id]["status"]
            for assignment_id, _assignment in rows
        )
        assigned = len(rows)
        unsupported = counts["unsupported"] + counts["bypassed"]
        workload_outcomes[arm] = {
            "assigned_count": assigned,
            "unsupported_count": counts["unsupported"],
            "bypassed_count": counts["bypassed"],
            "unsupported_or_bypassed_share": _decimal_text(
                Decimal(unsupported) / Decimal(assigned) if assigned else None
            ),
        }
    currency = next(iter({card["currency"] for card in rates.values()}), None)
    modeled = {
        "rates_are_placeholder": placeholder,
        "amount_unit": "placeholder_units" if placeholder else "currency",
        "currency": currency,
        "rate_cards": normalized["rate_card_meta"],
        "rate_denominator": "per_1m_tokens",
        "success_rule": "completed_and_correct_complete_cited_without_unsupported_claim",
        "conditions": conditions,
        "paired_comparisons": paired,
        "output_contract_comparison": _output_contracts(normalized),
        "sensitivity_scenarios": _sensitivity(normalized, data["sensitivity_scenarios"]),
        "mechanism_effects_are_not_additive": True,
    }
    factorial = _factorial_section(
        normalized, evidence_class=evidence_class, provenance_verified=provenance_verified
    )
    if placeholder:
        modeled = _label_placeholder_units(modeled)
        factorial = _label_placeholder_units(factorial)
    return {
        "schema_version": SCHEMA_VERSION,
        "evidence_class": evidence_class,
        "study_id": data["study_bundle"]["study_id"],
        "input_sha256": input_digest,
        "report_as_of_utc": data["as_of_utc"],
        "provenance": {
            "provenance_verified": provenance_verified,
            "admin_exports": records,
            "verified_window_count": sum(window_verified),
            "unverified_window_count": len(window_verified) - sum(window_verified),
            "note": "Digests show that the supplied admin exports match the windows. They do not prove the exports are genuine.",
        },
        "sample": {
            "task_count": len(normalized["task_strata"]),
            "assignment_count": normalized["all_assignment_count"],
            "condition_run_count": len(normalized["runs"]),
            "withdrawn_participants": normalized["withdrawn_participant_count"],
            "withdrawn_assignments_by_arm": normalized["withdrawn_by_arm"],
            "participant_kind": next(
                iter({row["participant_kind"] for row in normalized["assignments"].values()}),
                None,
            ),
            "allocation_verification": normalized["allocation_verification"],
            "stratum_weights": {
                key: _decimal_text(value) for key, value in sorted(normalized["weights"].items())
            },
            "workload_outcomes_by_arm": workload_outcomes,
        },
        "condition_profiles": {
            key: dict(normalized["condition_profiles"][key])
            for key in sorted(normalized["condition_profiles"])
        },
        "payload_estimate": _payload_estimate(normalized),
        "modeled_economics": modeled,
        "factorial": factorial,
        "enterprise_observed": _enterprise_observed(
            data, normalized, export_digests, provenance_verified
        ),
        "assumptions_and_limits": [
            "Modeled costs use only the explicitly supplied per-model rate cards and M13 request sidecar; no live pricing or universal fallback rate is used.",
            "Placeholder rate cards are unit labels, not prices: every modeled amount is reported as placeholder_units and currency is null.",
            "Every request and model listed in the M13 sidecar contributes its uncached input, cache-write, cache-read, and billed-output buckets.",
            "Billed output is inclusive; visible output estimates are diagnostic and are never added to billed output a second time.",
            "Incremental service costs are added only when explicitly supplied and explicitly marked as not already included in provider cost.",
            "A missing rate, token bucket, or service charge keeps task cost and aggregate totals unavailable instead of becoming zero.",
            "Paired deltas require matching source snapshot, model, settings, and counter version; unmatched pairs remain in cost coverage and are excluded from paired attribution.",
            "Cost per successful task includes all assigned-task costs, including failed attempts, divided by the declared completed-and-quality-passing task count; it is a point estimate reported with its n.",
            "Savings decisions use a cluster bootstrap over participants with a fixed reporting seed, and claim support only with verified provenance and the predeclared minimum pairs and participants.",
            "Artifact delivery is evaluated for correctness and completeness; deterministic rendering does not imply free model output or zero service cost.",
            "Sensitivity scenarios are parameterized what-if calculations, not observed Enterprise spend, forecasts, or a savings promise.",
            "Admin usage windows are aggregated by arm and are never split across individual requests or tasks.",
            "Factorial effects are per participant-task block with a cluster bootstrap; the interaction term never makes a savings claim.",
        ],
        "savings_claim": savings_claim,
    }


def build_report(
    value: Any,
    admin_exports: list[dict[str, Any]] | None = None,
    withdrawals: Any = None,
) -> dict[str, Any]:
    # Aggregate sums and weighted ratios must retain the full precision of the
    # bounded input amounts; the Decimal default context can erase tiny charges
    # beside a large amount before serialization.
    with localcontext() as context:
        context.prec = 80
        return _build_report(value, admin_exports or [], withdrawals)


def run(
    input_path: Path,
    output_path: Path | None = None,
    admin_export_paths: list[Path] | tuple[Path, ...] = (),
    withdrawals_path: Path | None = None,
) -> dict[str, Any]:
    exports = [_read_admin_export(Path(path)) for path in admin_export_paths]
    withdrawals = _safe_load(withdrawals_path) if withdrawals_path is not None else None
    report = build_report(_safe_load(input_path), exports, withdrawals)
    payload = (
        json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n"
    )
    if output_path is not None:
        try:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(payload, encoding="utf-8", newline="\n")
        except OSError:
            raise EconomicsError("output_unavailable") from None
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build an offline Phase G M13 economics report.")
    parser.add_argument(
        "--input",
        type=Path,
        required=True,
        help="M13 JSON with M6 study contract and economics sidecar",
    )
    parser.add_argument("--output", type=Path, required=True, help="explicit report JSON path")
    parser.add_argument(
        "--admin-export",
        type=Path,
        action="append",
        default=[],
        metavar="PATH",
        help="administrator export file; repeat for each export. Only its SHA-256 and size are reported.",
    )
    parser.add_argument(
        "--withdrawals",
        type=Path,
        default=None,
        metavar="PATH",
        help="participant withdrawal file; withdrawn rows are removed before any statistic",
    )
    args = parser.parse_args(argv)
    try:
        report = run(args.input, args.output, args.admin_export, args.withdrawals)
    except EconomicsError as error:
        print(json.dumps({"error": error.code}, separators=(",", ":")), file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "report": str(args.output),
                "input_sha256": report["input_sha256"],
                "evidence_class": report["evidence_class"],
            },
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
