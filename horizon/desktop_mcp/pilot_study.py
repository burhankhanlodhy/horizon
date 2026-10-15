"""Versioned, metadata-only contract for the Phase G Enterprise pilot harness.

Allocation has two modes:

- ``committed_secret`` (real cohorts): the operator generates a 32-byte seed
  (64 hex characters), keeps it in a separate seed file, and the assignment
  file stores only ``seed_commitment``, the SHA-256 of the seed's hex text.
  Participant order is a permutation drawn from ``random.Random(int(seed_hex, 16))``
  over the sorted participant codes. Williams rows and blind aliases derive from
  the seed. Before the seed is revealed, import checks structure only; after
  reveal, ``validate_assignment_set(..., seed_hex=...)`` recomputes everything
  and checks the commitment.
- ``public_synthetic`` (fixtures only): an integer seed that is public. Synthetic
  seeds are not secret; anyone can recompute their allocation.
"""

from __future__ import annotations

import hashlib
import json
import random
import re
import secrets
from datetime import datetime, timezone
from decimal import Decimal
from fractions import Fraction
from itertools import combinations
from typing import Any

from horizon.desktop_mcp.evidence_stats import (
    CONSENTED_DEIDENTIFIED,
    SYNTHETIC,
    Interval,
    PairedUnit,
    paired_difference_interval,
    savings_decision,
)

SCHEMA_VERSION = "phase_g_study/v1"
# Build plan section 11 ("Three baselines"): A is the raw gateway with the same
# tools and limits, B is ContextShrink. A-vs-B isolates the compression and
# selection effect; marketed savings claims compare against C_normal or C_tuned
# in the economics harness, not here. The difference is B - A; the existing
# paired_A_minus_B field keeps its original sign.
CANDIDATE_ARM = "B"
REFERENCE_ARM = "A"
INTERVAL_SEED = 20261014
SYNTHETIC_VALUE_LABEL = "synthetic_fixture_values, not measurements"
PARTICIPANT_VALUE_LABEL = "participant_supplied_inner_payload_estimate, not billing data"
ARM_IDS = ("A", "B", "C_normal", "C_tuned")
OUTCOME_STATUSES = (
    "completed",
    "failed",
    "unsupported",
    "bypassed",
    "refused",
    "timed_out",
    "not_run",
)
SCORE_STATUSES = ("scored", "no_score")
ALLOCATION_METHOD = "williams_seeded_permutation_v2"
SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
TASK_ID = re.compile(r"^t_[a-z0-9_]{1,40}$")
PSEUDONYM = re.compile(r"^p_[a-z2-7]{10}$")
SYNTHETIC_ID = re.compile(r"^syn-p-[a-z0-9]{4,24}$")
SCORER_CODE = re.compile(r"^s_[a-z2-7]{10}$")
SEED_HEX = re.compile(r"^[0-9a-f]{64}$")
CODE_ALPHABET = "abcdefghijklmnopqrstuvwxyz234567"
UNASSIGNED_SCORER = "unassigned"
MAX_SCORE_ROWS_PER_ASSIGNMENT = 8
SAFE_LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:+-]{0,63}$")
DECIMAL = re.compile(r"^-?(?:0|[1-9][0-9]{0,17})(?:\.[0-9]{1,8})?$")
CURRENCY = re.compile(r"^[A-Z]{3}$")
METRIC_KEYS = (
    "latency_ms",
    "retry_count",
    "tool_call_count",
    "retrieval_bytes",
    "estimated_input_tokens",
    "estimated_output_tokens",
    "schema_instruction_tokens",
)
USAGE_KEYS = (
    "uncached_input_tokens",
    "cache_write_5m_tokens",
    "cache_write_1h_tokens",
    "cache_read_tokens",
    "billed_output_tokens",
)
MISSING_REASONS = {
    "not_collected",
    "not_available_from_export",
    "not_applicable",
    "not_run",
    "counter_unavailable",
}
AGREEMENT_FIELDS = (
    "correctness_pass",
    "completeness_pass",
    "citations_pass",
    "no_unsupported_claim_pass",
)
PASS_CRITERIA: dict[str, tuple[str, ...]] = {
    "correct_only": ("correctness_pass",),
    "correct_complete_cited_no_unsupported": AGREEMENT_FIELDS,
}
PLAN_KEYS = {
    "target_participants",
    "minimum_completed_per_arm",
    "stopping_rule",
    "stratum_weights",
    "refresh_max_age_hours",
}
OPTIONAL_PLAN_KEYS = {"quality_pass_criterion"}


class PilotStudyError(ValueError):
    """Bounded, user-safe study contract validation error."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def digest_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def _keys(value: Any, expected: set[str], code: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != expected:
        raise PilotStudyError(code)
    return value


def _int(value: Any, low: int = 0, high: int = 10**12) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise PilotStudyError("integer_invalid")
    return value


def _id(value: Any, pattern: re.Pattern[str] = SAFE_ID) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise PilotStudyError("identifier_invalid")
    return value


def _utc(value: Any, code: str = "timestamp_invalid") -> datetime:
    if not isinstance(value, str) or len(value) > 40:
        raise PilotStudyError(code)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise PilotStudyError(code) from None
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise PilotStudyError(code)
    return parsed.astimezone(timezone.utc)


def _missing_values(values: Any, reasons: Any, keys: tuple[str, ...]) -> dict[str, int | None]:
    if not isinstance(values, dict) or set(values) != set(keys) or not isinstance(reasons, dict):
        raise PilotStudyError("measurement_fields_invalid")
    if set(reasons) - set(keys):
        raise PilotStudyError("missing_reasons_invalid")
    result: dict[str, int | None] = {}
    for key in keys:
        value = values[key]
        if value is None:
            missing_reason = reasons.get(key)
            if not isinstance(missing_reason, str) or missing_reason not in MISSING_REASONS:
                raise PilotStudyError("missing_value_requires_reason")
            result[key] = None
        else:
            result[key] = _int(value, high=604800000 if key == "latency_ms" else 10**12)
            if key in reasons:
                raise PilotStudyError("present_value_has_missing_reason")
    return result


def _typed_check_value(kind: Any, value: Any) -> None:
    if kind == "integer_equal":
        _int(value)
    elif kind == "boolean_equal":
        if type(value) is not bool:
            raise PilotStudyError("synthetic_check_invalid")
    elif kind == "literal_equal":
        if not isinstance(value, str) or len(value) > 120:
            raise PilotStudyError("synthetic_check_invalid")
    else:
        raise PilotStudyError("synthetic_check_kind_invalid")


def validate_bundle(bundle: Any) -> dict[str, Any]:
    root = _keys(
        bundle,
        {"schema_version", "bundle_kind", "study_id", "study_plan", "tasks"},
        "bundle_schema_invalid",
    )
    if root["schema_version"] != SCHEMA_VERSION:
        raise PilotStudyError("schema_version_unsupported")
    if root["bundle_kind"] != "synthetic_task_bundle":
        raise PilotStudyError("bundle_kind_unsupported")
    _id(root["study_id"])
    plan = root["study_plan"]
    if (
        not isinstance(plan, dict)
        or not PLAN_KEYS <= set(plan)
        or set(plan) - PLAN_KEYS - OPTIONAL_PLAN_KEYS
    ):
        raise PilotStudyError("study_plan_invalid")
    if "quality_pass_criterion" in plan and (
        not isinstance(plan["quality_pass_criterion"], str)
        or plan["quality_pass_criterion"] not in PASS_CRITERIA
    ):
        raise PilotStudyError("quality_pass_criterion_invalid")
    target = _int(plan["target_participants"], 4, 10000)
    if target % 4:
        raise PilotStudyError("target_must_complete_counterbalance_block")
    _int(plan["minimum_completed_per_arm"], 1, 100000)
    _int(plan["refresh_max_age_hours"], 1, 168)
    if plan["stopping_rule"] != "complete_predeclared_bundle_or_stop_at_target":
        raise PilotStudyError("stopping_rule_invalid")
    weights = plan["stratum_weights"]
    if not isinstance(weights, dict) or not weights or len(weights) > 64:
        raise PilotStudyError("stratum_weights_invalid")
    total = Decimal(0)
    for stratum, weight in weights.items():
        _id(stratum)
        if not isinstance(weight, str) or not DECIMAL.fullmatch(weight) or Decimal(weight) <= 0:
            raise PilotStudyError("stratum_weight_invalid")
        total += Decimal(weight)
    if total != Decimal(1):
        raise PilotStudyError("stratum_weights_must_sum_to_one")
    tasks = root["tasks"]
    if not isinstance(tasks, list) or not tasks or len(tasks) > 500:
        raise PilotStudyError("tasks_invalid")
    seen: set[str] = set()
    for task in tasks:
        item = _keys(
            task,
            {
                "task_id",
                "stratum",
                "locale",
                "operator_instruction",
                "synthetic_sources",
                "synthetic_check",
                "synthetic_observed",
            },
            "task_invalid",
        )
        task_id = _id(item["task_id"], TASK_ID)
        stratum = _id(item["stratum"])
        _id(item["locale"])
        if (
            not isinstance(item["operator_instruction"], str)
            or not item["operator_instruction"].strip()
            or len(item["operator_instruction"]) > 1200
        ):
            raise PilotStudyError("operator_instruction_invalid")
        sources = item["synthetic_sources"]
        if not isinstance(sources, list) or len(sources) > 8:
            raise PilotStudyError("synthetic_sources_invalid")
        source_ids: set[str] = set()
        for source in sources:
            source_item = _keys(
                source, {"source_id", "format", "content"}, "synthetic_source_invalid"
            )
            source_id = _id(source_item["source_id"])
            if source_id in source_ids or source_item["format"] not in {
                "txt",
                "md",
                "json",
                "csv",
                "unsupported_fixture",
            }:
                raise PilotStudyError("synthetic_source_invalid")
            if not isinstance(source_item["content"], str) or len(source_item["content"]) > 64000:
                raise PilotStudyError("synthetic_source_content_invalid")
            source_ids.add(source_id)
        if task_id in seen or stratum not in weights:
            raise PilotStudyError("task_id_duplicate_or_stratum_unknown")
        seen.add(task_id)
        check = _keys(item["synthetic_check"], {"kind", "expected"}, "synthetic_check_invalid")
        _typed_check_value(check["kind"], check["expected"])
        _typed_check_value(check["kind"], item["synthetic_observed"])
    return root


def _participant_kind(code: Any) -> str:
    if isinstance(code, str) and SYNTHETIC_ID.fullmatch(code):
        return "synthetic"
    if isinstance(code, str) and PSEUDONYM.fullmatch(code):
        return "pseudonymous"
    raise PilotStudyError("participant_code_invalid")


def participant_code_kind(code: Any) -> str:
    """Return the bounded participant-code class for CLI consent handling."""
    return _participant_kind(code)


def _random_code(prefix: str) -> str:
    return prefix + "".join(secrets.choice(CODE_ALPHABET) for _ in range(10))


def _count_argument(count: Any) -> int:
    if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 500:
        raise PilotStudyError("code_count_invalid")
    return count


def generate_participant_codes(count: int) -> list[str]:
    """Return unique random participant codes matching ``^p_[a-z2-7]{10}$``."""
    target = _count_argument(count)
    codes: list[str] = []
    seen: set[str] = set()
    while len(codes) < target:
        code = _random_code("p_")
        if code not in seen:
            seen.add(code)
            codes.append(code)
    return codes


def generate_scorer_codes(count: int) -> list[str]:
    """Return unique random scorer codes matching ``^s_[a-z2-7]{10}$``."""
    target = _count_argument(count)
    codes: list[str] = []
    seen: set[str] = set()
    while len(codes) < target:
        code = _random_code("s_")
        if code not in seen:
            seen.add(code)
            codes.append(code)
    return codes


def seed_commitment(seed_hex: str) -> str:
    """SHA-256 of the 64-character lowercase hex seed text."""
    return hashlib.sha256(seed_hex.encode("ascii")).hexdigest()


def _synthetic_seed_hex(seed: int) -> str:
    return format(seed, "064x")


def _hash_id(prefix: str, value: str, length: int) -> str:
    return prefix + hashlib.sha256(value.encode("utf-8")).hexdigest()[:length]


def _williams_arms(offset: int) -> tuple[str, ...]:
    # Williams design for four treatments: every arm appears in each position,
    # and each directed carry-over pair is balanced across the four sequences.
    sequences = (
        ("A", "B", "C_tuned", "C_normal"),
        ("B", "C_normal", "A", "C_tuned"),
        ("C_normal", "C_tuned", "B", "A"),
        ("C_tuned", "A", "C_normal", "B"),
    )
    return sequences[offset % 4]


def allocation_offsets(seed_hex: str, participant_ids: list[str]) -> dict[str, int]:
    """Position of each participant in a seeded permutation of the sorted cohort."""
    if not isinstance(seed_hex, str) or not SEED_HEX.fullmatch(seed_hex):
        raise PilotStudyError("seed_hex_invalid")
    permuted = sorted(set(participant_ids))
    random.Random(int(seed_hex, 16)).shuffle(permuted)
    return {participant: index for index, participant in enumerate(permuted)}


def _blind_code(study_id: str, participant: str, task_id: str, seed_hex: str, arm: str) -> str:
    material = f"{SCHEMA_VERSION}|{study_id}|{participant}|{task_id}|{seed_hex}|blind|{arm}"
    return "blind_" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


def _assignment_id(study: str, participant: str, task: str, arm: str) -> str:
    return _hash_id("asg_", f"{SCHEMA_VERSION}|{study}|{participant}|{task}|{arm}", 24)


def build_assignments(
    bundle: Any,
    participant_ids: list[str],
    *,
    seed: int | None = None,
    consented_at_utc: dict[str, str] | None = None,
    seed_hex: str | None = None,
) -> dict[str, Any]:
    """Build an assignment set.

    Synthetic codes take an integer ``seed`` (public). Real codes take a
    ``seed_hex`` (see ``build_committed_assignments`` to generate one); the
    returned file contains only ``seed_commitment``.
    """
    manifest = validate_bundle(bundle)
    if not isinstance(participant_ids, list) or not participant_ids or len(participant_ids) > 1000:
        raise PilotStudyError("participants_invalid")
    if not all(isinstance(value, str) for value in participant_ids):
        raise PilotStudyError("participant_code_invalid")
    if len(set(participant_ids)) != len(participant_ids):
        raise PilotStudyError("participant_code_duplicate")
    kinds = {_participant_kind(participant) for participant in participant_ids}
    if len(kinds) != 1:
        raise PilotStudyError("participant_kind_mixed")
    kind = kinds.pop()
    consent_map = consented_at_utc or {}
    if not isinstance(consent_map, dict) or set(consent_map) - set(participant_ids):
        raise PilotStudyError("consent_map_invalid")
    if kind == "synthetic":
        if seed is None:
            raise PilotStudyError("synthetic_seed_required")
        if seed_hex is not None:
            raise PilotStudyError("secret_seed_not_applicable_to_synthetic")
        if consent_map:
            raise PilotStudyError("synthetic_consent_not_applicable")
        public_seed = _int(seed, high=2**32 - 1)
        allocation_seed = _synthetic_seed_hex(public_seed)
        seed_mode, seed_field, commitment = "public_synthetic", public_seed, None
    else:
        if seed is not None:
            raise PilotStudyError("seed_not_applicable_to_real_cohort")
        if seed_hex is None:
            raise PilotStudyError("secret_seed_required")
        if not isinstance(seed_hex, str) or not SEED_HEX.fullmatch(seed_hex):
            raise PilotStudyError("seed_hex_invalid")
        allocation_seed = seed_hex
        seed_mode, seed_field, commitment = "committed_secret", None, seed_commitment(seed_hex)
    consents: dict[str, dict[str, str]] = {}
    for participant in participant_ids:
        if kind == "pseudonymous":
            consents[participant] = {
                "status": "consented",
                "consented_at_utc": _utc(consent_map.get(participant), "consent_required")
                .isoformat()
                .replace("+00:00", "Z"),
                "scope": SCHEMA_VERSION,
            }
        else:
            consents[participant] = {"status": "synthetic_not_applicable"}
    offsets = allocation_offsets(allocation_seed, participant_ids)
    rows: list[dict[str, Any]] = []
    for participant in sorted(participant_ids):
        for task_position, task in enumerate(manifest["tasks"]):
            task_id = task["task_id"]
            pair_id = _hash_id("pair_", f"{manifest['study_id']}|{participant}|{task_id}", 20)
            ordered = _williams_arms(offsets[participant] + task_position)
            for position, arm in enumerate(ordered, 1):
                assignment_id = _assignment_id(manifest["study_id"], participant, task_id, arm)
                rows.append(
                    {
                        "assignment_id": assignment_id,
                        "score_id": _hash_id("score_", assignment_id, 24),
                        "participant_id": participant,
                        "participant_kind": kind,
                        "pair_id": pair_id,
                        "task_id": task_id,
                        "stratum": task["stratum"],
                        "locale": task["locale"],
                        "arm": arm,
                        "order": position,
                        "blind_arm_code": _blind_code(
                            manifest["study_id"], participant, task_id, allocation_seed, arm
                        ),
                        "consent": dict(consents[participant]),
                    }
                )
    rows.sort(key=lambda x: (x["participant_id"], x["task_id"], x["order"]))
    return {
        "schema_version": SCHEMA_VERSION,
        "study_id": manifest["study_id"],
        "bundle_sha256": digest_json(manifest),
        "seed_mode": seed_mode,
        "seed": seed_field,
        "seed_commitment": commitment,
        "arms": list(ARM_IDS),
        "assignments": rows,
    }


def build_committed_assignments(
    bundle: Any,
    participant_ids: list[str],
    *,
    consented_at_utc: dict[str, str] | None = None,
) -> tuple[dict[str, Any], str]:
    """Generate a 32-byte secret seed and build a real-cohort assignment set.

    Returns ``(assignment_set, seed_hex)``. The caller must store ``seed_hex``
    outside the assignment file; the assignment set holds only the commitment.
    """
    seed_hex = secrets.token_hex(32)
    assignments = build_assignments(
        bundle,
        participant_ids,
        seed_hex=seed_hex,
        consented_at_utc=consented_at_utc,
    )
    return assignments, seed_hex


def _verification_seed(root: dict[str, Any], seed_hex: str | None) -> str | None:
    mode = root["seed_mode"]
    if mode == "public_synthetic":
        if root["seed_commitment"] is not None or root["seed"] is None:
            raise PilotStudyError("seed_fields_invalid")
        derived = _synthetic_seed_hex(_int(root["seed"], high=2**32 - 1))
        if seed_hex is not None and seed_hex != derived:
            raise PilotStudyError("seed_mismatch")
        return derived
    if mode == "committed_secret":
        commitment = root["seed_commitment"]
        if root["seed"] is not None:
            raise PilotStudyError("seed_fields_invalid")
        if not isinstance(commitment, str) or not SEED_HEX.fullmatch(commitment):
            raise PilotStudyError("seed_commitment_invalid")
        if seed_hex is None:
            return None
        if not isinstance(seed_hex, str) or not SEED_HEX.fullmatch(seed_hex):
            raise PilotStudyError("seed_hex_invalid")
        if seed_commitment(seed_hex) != commitment:
            raise PilotStudyError("seed_commitment_mismatch")
        return seed_hex
    raise PilotStudyError("seed_mode_invalid")


def validate_assignment_set(
    bundle: Any, value: Any, *, seed_hex: str | None = None
) -> dict[str, Any]:
    """Validate an assignment set.

    Without ``seed_hex`` a committed-secret set is checked structurally only;
    Williams order and blind aliases are verified once the seed is revealed.
    Synthetic sets always recompute from their public seed.
    """
    manifest = validate_bundle(bundle)
    root = _keys(
        value,
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
    if root["schema_version"] != SCHEMA_VERSION or root["study_id"] != manifest["study_id"]:
        raise PilotStudyError("assignment_study_mismatch")
    if root["bundle_sha256"] != digest_json(manifest) or root["arms"] != list(ARM_IDS):
        raise PilotStudyError("assignment_bundle_or_arm_mismatch")
    verify_seed = _verification_seed(root, seed_hex)
    rows = root["assignments"]
    if not isinstance(rows, list) or not rows or len(rows) > 2_000_000:
        raise PilotStudyError("assignments_invalid")
    task_by_id = {task["task_id"]: task for task in manifest["tasks"]}
    seen: set[str] = set()
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in rows:
        item = _keys(
            row,
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
        kind = _participant_kind(item["participant_id"])
        if kind != item["participant_kind"]:
            raise PilotStudyError("participant_kind_invalid")
        if not isinstance(item["task_id"], str) or not isinstance(item["arm"], str):
            raise PilotStudyError("assignment_task_or_arm_invalid")
        task = task_by_id.get(item["task_id"])
        arm = item["arm"]
        if (
            task is None
            or arm not in ARM_IDS
            or task["stratum"] != item["stratum"]
            or task["locale"] != item["locale"]
        ):
            raise PilotStudyError("assignment_task_or_arm_invalid")
        expected_id = _assignment_id(
            manifest["study_id"], item["participant_id"], item["task_id"], arm
        )
        if item["assignment_id"] != expected_id or item["score_id"] != _hash_id(
            "score_", expected_id, 24
        ):
            raise PilotStudyError("assignment_id_invalid")
        if item["pair_id"] != _hash_id(
            "pair_", f"{manifest['study_id']}|{item['participant_id']}|{item['task_id']}", 20
        ):
            raise PilotStudyError("pair_id_invalid")
        _int(item["order"], 1, len(ARM_IDS))
        _id(item["blind_arm_code"])
        consent = item["consent"]
        if kind == "synthetic":
            _keys(consent, {"status"}, "consent_invalid")
            if consent["status"] != "synthetic_not_applicable":
                raise PilotStudyError("consent_invalid")
        else:
            _keys(consent, {"status", "consented_at_utc", "scope"}, "consent_invalid")
            if consent["status"] != "consented" or consent["scope"] != SCHEMA_VERSION:
                raise PilotStudyError("consent_required")
            _utc(consent["consented_at_utc"], "consent_timestamp_invalid")
        if expected_id in seen:
            raise PilotStudyError("assignment_duplicate")
        seen.add(expected_id)
        groups.setdefault((item["participant_id"], item["task_id"]), []).append(item)
    if len(groups) != len({x["participant_id"] for x in rows}) * len(manifest["tasks"]):
        raise PilotStudyError("assignment_pair_missing")
    if len({x["participant_kind"] for x in rows}) != 1:
        raise PilotStudyError("participant_kind_mixed")
    participants = sorted({row["participant_id"] for row in rows})
    task_positions = {task["task_id"]: position for position, task in enumerate(manifest["tasks"])}
    offsets = allocation_offsets(verify_seed, participants) if verify_seed is not None else None
    for (participant, task_id), group in groups.items():
        if {x["arm"] for x in group} != set(ARM_IDS) or {x["order"] for x in group} != set(
            range(1, 5)
        ):
            raise PilotStudyError("assignment_arm_or_order_missing")
        if offsets is None:
            continue
        by_order = sorted(group, key=lambda row: row["order"])
        expected_order = _williams_arms(offsets[participant] + task_positions[task_id])
        if tuple(row["arm"] for row in by_order) != expected_order:
            raise PilotStudyError("assignment_order_seed_mismatch")
        for row in group:
            expected_blind = _blind_code(
                manifest["study_id"], participant, task_id, verify_seed, row["arm"]
            )
            if row["blind_arm_code"] != expected_blind:
                raise PilotStudyError("assignment_blind_code_seed_mismatch")
    return root


def validate_observations(bundle: Any, assignment_set: Any, value: Any) -> list[dict[str, Any]]:
    assignments = validate_assignment_set(bundle, assignment_set)["assignments"]
    index = {row["assignment_id"]: row for row in assignments}
    if not isinstance(value, list) or len(value) > len(index):
        raise PilotStudyError("observations_invalid")
    seen: set[str] = set()
    output: list[dict[str, Any]] = []
    for row in value:
        item = _keys(
            row,
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
                "metrics",
                "metric_missing_reasons",
                "provider_usage",
                "usage_missing_reasons",
                "source_snapshot_sha256",
                "failure_code",
                "unsupported_code",
            },
            "observation_invalid",
        )
        if not isinstance(item["assignment_id"], str):
            raise PilotStudyError("observation_assignment_unknown_or_duplicate")
        assigned = index.get(item["assignment_id"])
        if assigned is None or item["assignment_id"] in seen:
            raise PilotStudyError("observation_assignment_unknown_or_duplicate")
        seen.add(item["assignment_id"])
        for key in ("participant_id", "task_id", "pair_id", "arm", "locale"):
            if item[key] != assigned[key]:
                raise PilotStudyError("observation_assignment_mismatch")
        if not isinstance(item["status"], str) or item["status"] not in OUTCOME_STATUSES:
            raise PilotStudyError("outcome_status_invalid")
        if item["status"] == "not_run":
            if item["start_utc"] is not None or item["end_utc"] is not None:
                raise PilotStudyError("not_run_has_window")
        else:
            start, end = _utc(item["start_utc"]), _utc(item["end_utc"])
            if end < start:
                raise PilotStudyError("outcome_window_invalid")
            if (
                assigned["participant_kind"] == "pseudonymous"
                and _utc(assigned["consent"]["consented_at_utc"]) > start
            ):
                raise PilotStudyError("consent_after_observation_start")
        versions = _keys(
            item["versions"], {"desktop", "extension", "model", "settings"}, "versions_invalid"
        )
        for version in versions.values():
            if version is not None:
                _id(version, SAFE_LABEL)
        counter_version = item["counter_version"]
        if counter_version is not None:
            _id(counter_version, SAFE_LABEL)
        metrics = _missing_values(item["metrics"], item["metric_missing_reasons"], METRIC_KEYS)
        usage = _missing_values(item["provider_usage"], item["usage_missing_reasons"], USAGE_KEYS)
        if (
            metrics["estimated_input_tokens"] is not None
            or metrics["estimated_output_tokens"] is not None
        ) and counter_version is None:
            raise PilotStudyError("counter_version_required")
        snapshot = item["source_snapshot_sha256"]
        if not isinstance(snapshot, str) or not re.fullmatch(r"[a-f0-9]{64}", snapshot):
            raise PilotStudyError("source_snapshot_digest_invalid")
        task = next(
            candidate
            for candidate in validate_bundle(bundle)["tasks"]
            if candidate["task_id"] == item["task_id"]
        )
        expected_snapshot = digest_json(task["synthetic_sources"])
        if snapshot != expected_snapshot:
            raise PilotStudyError("source_snapshot_mismatch")
        for key in ("failure_code", "unsupported_code"):
            raw = item[key]
            if raw is not None:
                _id(raw)
        if item["status"] in {"failed", "timed_out"} and item["failure_code"] is None:
            raise PilotStudyError("failure_code_required")
        if item["status"] == "unsupported" and item["unsupported_code"] is None:
            raise PilotStudyError("unsupported_code_required")
        if item["status"] not in {"failed", "timed_out"} and item["failure_code"] is not None:
            raise PilotStudyError("failure_code_unexpected")
        if item["status"] != "unsupported" and item["unsupported_code"] is not None:
            raise PilotStudyError("unsupported_code_unexpected")
        output.append(
            {
                **item,
                "counter_version": counter_version,
                "metrics": metrics,
                "provider_usage": usage,
            }
        )
    return output


def _check_scorer_code(code: Any, status: str, participant_kind: str) -> None:
    if code == UNASSIGNED_SCORER:
        if status != "no_score":
            raise PilotStudyError("scorer_code_unassigned")
        return
    if participant_kind == "synthetic":
        _id(code)
        return
    if not isinstance(code, str) or not SCORER_CODE.fullmatch(code):
        raise PilotStudyError("scorer_code_invalid")


def validate_scores(
    bundle: Any, assignment_set: Any, observations: list[dict[str, Any]], value: Any
) -> list[dict[str, Any]]:
    assignments = validate_assignment_set(bundle, assignment_set)["assignments"]
    index = {row["score_id"]: row for row in assignments}
    observation_by_id = {row["assignment_id"]: row for row in observations}
    if not isinstance(value, list) or len(value) > len(index) * MAX_SCORE_ROWS_PER_ASSIGNMENT:
        raise PilotStudyError("scores_invalid")
    seen: set[tuple[str, str]] = set()
    out: list[dict[str, Any]] = []
    allowed = {
        "correctness": {"correct", "partial", "incorrect", "unscored"},
        "completeness": {"complete", "partial", "incomplete", "unscored"},
        "citations": {"complete", "partial", "missing", "not_required", "unscored"},
        "unsupported_claim": {"yes", "no", "unknown", "unscored"},
    }
    for row in value:
        score_keys = {
            "score_id",
            "blinded_arm_code",
            "scorer_code",
            "status",
            "correctness",
            "completeness",
            "citations",
            "unsupported_claim",
            "omission_codes",
        }
        if not isinstance(row, dict):
            raise PilotStudyError("score_invalid")
        row_keys = frozenset(row)
        if row_keys not in {frozenset(score_keys), frozenset(score_keys | {"task_id"})}:
            raise PilotStudyError("score_invalid")
        item = row
        if not isinstance(item["score_id"], str) or not isinstance(item["blinded_arm_code"], str):
            raise PilotStudyError("score_assignment_or_blind_code_invalid")
        assigned = index.get(item["score_id"])
        if (
            assigned is None
            or item["blinded_arm_code"] != assigned["blind_arm_code"]
            or ("task_id" in item and item["task_id"] != assigned["task_id"])
        ):
            raise PilotStudyError("score_assignment_or_blind_code_invalid")
        if not isinstance(item["status"], str) or item["status"] not in SCORE_STATUSES:
            raise PilotStudyError("score_status_invalid")
        if assigned["participant_kind"] == "synthetic" and item["status"] == "scored":
            raise PilotStudyError("synthetic_participant_quality_score_forbidden")
        if not isinstance(item["scorer_code"], str):
            raise PilotStudyError("scorer_code_invalid")
        _check_scorer_code(item["scorer_code"], item["status"], assigned["participant_kind"])
        pair = (item["score_id"], item["scorer_code"])
        if pair in seen:
            raise PilotStudyError("duplicate_scorer_for_assignment")
        seen.add(pair)
        observation = observation_by_id.get(assigned["assignment_id"])
        if observation is None or observation["status"] != "completed":
            if item["status"] != "no_score":
                raise PilotStudyError("noncompleted_task_must_be_unscored")
        dimensions = ("correctness", "completeness", "citations", "unsupported_claim")
        for dimension in dimensions:
            if not isinstance(item[dimension], str) or item[dimension] not in allowed[dimension]:
                raise PilotStudyError("score_dimension_invalid")
        codes = item["omission_codes"]
        if not isinstance(codes, list) or len(codes) > 32:
            raise PilotStudyError("omission_codes_invalid")
        for code in codes:
            _id(code)
        if item["status"] == "no_score" and any(item[key] != "unscored" for key in dimensions):
            raise PilotStudyError("no_score_dimensions_invalid")
        if item["status"] == "scored" and any(item[key] == "unscored" for key in dimensions):
            raise PilotStudyError("scored_dimensions_invalid")
        out.append(item)
    return out


def validate_usage_windows(
    bundle: Any,
    assignment_set: Any,
    observations: list[dict[str, Any]],
    value: Any,
    *,
    as_of_utc: str,
) -> list[dict[str, Any]]:
    manifest = validate_bundle(bundle)
    assignments = validate_assignment_set(manifest, assignment_set)["assignments"]
    index = {row["assignment_id"]: row for row in assignments}
    observation_index = {row["assignment_id"]: row for row in observations}
    as_of = _utc(as_of_utc)
    rows = [] if value is None else value
    if not isinstance(rows, list) or len(rows) > 100000:
        raise PilotStudyError("usage_windows_invalid")
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    max_age_hours = _int(manifest["study_plan"].get("refresh_max_age_hours", 24), 1, 168)
    for row in rows:
        item = _keys(
            row,
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
            },
            "usage_window_invalid",
        )
        _id(item["window_id"])
        if item["window_id"] in seen:
            raise PilotStudyError("usage_window_duplicate")
        seen.add(item["window_id"])
        participant_kind = _participant_kind(item["participant_id"])
        if participant_kind == "synthetic":
            raise PilotStudyError("synthetic_usage_window_forbidden")
        if (
            not isinstance(item["arm"], str)
            or item["arm"] not in ARM_IDS
            or item["source"] != "admin_export"
        ):
            raise PilotStudyError("usage_source_or_arm_invalid")
        if (
            not isinstance(item["mapping_confidence"], str)
            or item["mapping_confidence"] not in {"high", "medium", "low"}
            or type(item["exclusions_recorded"]) is not bool
        ):
            raise PilotStudyError("mapping_metadata_invalid")
        mapped = item["assignment_ids"]
        if (
            not isinstance(mapped, list)
            or not mapped
            or not all(isinstance(x, str) for x in mapped)
            or len(set(mapped)) != len(mapped)
        ):
            raise PilotStudyError("usage_window_mapping_invalid")
        for assignment_id in mapped:
            assigned = index.get(assignment_id)
            if (
                assigned is None
                or assigned["participant_id"] != item["participant_id"]
                or assigned["arm"] != item["arm"]
            ):
                raise PilotStudyError("usage_window_assignment_mismatch")
            observation = observation_index.get(assignment_id)
            if (
                observation is None
                or observation["status"] == "not_run"
                or observation["start_utc"] is None
            ):
                raise PilotStudyError("usage_window_mapped_task_not_observed")
        start, end, refreshed = map(
            _utc, (item["start_utc"], item["end_utc"], item["refreshed_at_utc"])
        )
        if end < start or refreshed < end:
            raise PilotStudyError("usage_window_time_invalid")
        if not isinstance(item["unrelated_activity"], str) or item["unrelated_activity"] not in {
            "none",
            "present_excluded",
            "present_unmapped",
            "unknown",
        }:
            raise PilotStudyError("unrelated_activity_invalid")
        if not isinstance(item["exclusion_codes"], list) or len(item["exclusion_codes"]) > 32:
            raise PilotStudyError("exclusion_codes_invalid")
        for code in item["exclusion_codes"]:
            _id(code)
        for assignment_id in mapped:
            observation = observation_index[assignment_id]
            task_start = _utc(observation["start_utc"])
            task_end = _utc(observation["end_utc"])
            if task_start < start or task_end > end:
                raise PilotStudyError("usage_window_does_not_contain_task")
        buckets = _missing_values(item["usage_buckets"], item["bucket_missing_reasons"], USAGE_KEYS)
        cost = item["cost_amount"]
        currency = item["currency"]
        if cost is None:
            if (
                currency is not None
                or not isinstance(item["cost_missing_reason"], str)
                or item["cost_missing_reason"] not in {"not_collected", "not_available_from_export"}
            ):
                raise PilotStudyError("cost_missing_reason_required")
        elif (
            not isinstance(cost, str)
            or not DECIMAL.fullmatch(cost)
            or Decimal(cost) < 0
            or not isinstance(currency, str)
            or not CURRENCY.fullmatch(currency)
            or item["cost_missing_reason"] is not None
        ):
            raise PilotStudyError("cost_amount_invalid")
        age = (as_of - refreshed).total_seconds()
        result.append(
            {
                **item,
                "freshness": "fresh" if 0 <= age <= max_age_hours * 3600 else "stale_or_future",
                "normalized_usage_buckets": buckets,
                "data_coverage": "complete"
                if all(value is not None for value in buckets.values())
                else "partial"
                if any(value is not None for value in buckets.values())
                else "unavailable",
            }
        )
    return result


def _binary_pass(row: dict[str, Any]) -> dict[str, bool]:
    return {
        "correctness_pass": row["correctness"] == "correct",
        "completeness_pass": row["completeness"] == "complete",
        "citations_pass": row["citations"] in {"complete", "not_required"},
        "no_unsupported_claim_pass": row["unsupported_claim"] == "no",
    }


def cohens_kappa(pairs: list[tuple[bool, bool]]) -> Fraction | None:
    """Cohen's kappa for two raters' binary labels; None when chance agreement is 1."""
    total = len(pairs)
    if total == 0:
        return None
    both = sum(1 for first, second in pairs if first and second)
    first_only = sum(1 for first, second in pairs if first and not second)
    second_only = sum(1 for first, second in pairs if not first and second)
    neither = total - both - first_only - second_only
    observed = Fraction(both + neither, total)
    first_yes = Fraction(both + first_only, total)
    second_yes = Fraction(both + second_only, total)
    expected = first_yes * second_yes + (1 - first_yes) * (1 - second_yes)
    if expected == 1:
        return None
    return (observed - expected) / (1 - expected)


def _decimal_text(value: Fraction) -> str:
    return str((Decimal(value.numerator) / Decimal(value.denominator)).quantize(Decimal("0.0001")))


def _agreement_summary(items: list[list[dict[str, Any]]], distinct_scorers: int) -> dict[str, Any]:
    if not items:
        return {
            "status": "not_applicable",
            "reason": "no_assignment_has_two_scorers",
            "items_with_two_or_more_scorers": 0,
            "distinct_scorer_count": distinct_scorers,
            "fields": {},
        }
    fields: dict[str, Any] = {}
    for field in AGREEMENT_FIELDS:
        agreeing = 0
        pairs = 0
        kappa_pairs: list[tuple[bool, bool]] = []
        for group in items:
            ordered = sorted(group, key=lambda row: row["scorer_code"])
            values = [_binary_pass(row)[field] for row in ordered]
            for i, j in combinations(range(len(values)), 2):
                pairs += 1
                agreeing += values[i] == values[j]
            if distinct_scorers == 2 and len(values) == 2:
                kappa_pairs.append((values[0], values[1]))
        entry: dict[str, Any] = {
            "pair_count": pairs,
            "agreeing_pair_count": agreeing,
            "percent_agreement": _decimal_text(Fraction(agreeing, pairs)) if pairs else None,
            "cohens_kappa": None,
            "kappa_status": "requires_exactly_two_scorers",
        }
        if distinct_scorers == 2:
            kappa = cohens_kappa(kappa_pairs)
            if kappa is None:
                entry["kappa_status"] = "undefined_chance_agreement_is_one"
            else:
                entry["cohens_kappa"] = _decimal_text(kappa)
                entry["kappa_status"] = "computed"
        fields[field] = entry
    return {
        "status": "computed",
        "items_with_two_or_more_scorers": len(items),
        "distinct_scorer_count": distinct_scorers,
        "fields": fields,
    }


def _quality_status(has_scores: bool, criterion: str | None) -> str:
    if not has_scores:
        return "not_scored"
    return "scored" if criterion else "pending_pass_criterion"


def _interval_json(interval: Interval) -> dict[str, Any]:
    def text(value: Decimal | None) -> str | None:
        return None if value is None else str(value)

    return {
        "estimate": text(interval.estimate),
        "lower": text(interval.lower),
        "upper": text(interval.upper),
        "confidence": text(interval.confidence),
        "method": interval.method,
        "resamples": interval.resamples,
        "seed": interval.seed,
        "n_units": interval.n_units,
        "n_clusters": interval.n_clusters,
    }


def _allocation_verification(seed_mode: str, seed_revealed: bool) -> str:
    if seed_mode == "public_synthetic":
        return "public_synthetic_seed_verified"
    return "verified_with_revealed_seed" if seed_revealed else "deferred_until_seed_reveal"


def build_report(
    bundle: Any,
    assignment_set: Any,
    observations: Any,
    scores: Any,
    *,
    as_of_utc: str,
    usage_windows: Any = None,
    seed_hex: str | None = None,
) -> dict[str, Any]:
    manifest = validate_bundle(bundle)
    assignments = validate_assignment_set(manifest, assignment_set, seed_hex=seed_hex)
    observed = validate_observations(manifest, assignments, observations)
    scored = validate_scores(manifest, assignments, observed, scores)
    windows = validate_usage_windows(
        manifest, assignments, observed, usage_windows, as_of_utc=as_of_utc
    )
    criterion = manifest["study_plan"].get("quality_pass_criterion")
    by_observation = {row["assignment_id"]: row for row in observed}
    scored_by_score: dict[str, list[dict[str, Any]]] = {}
    for row in scored:
        if row["status"] == "scored":
            scored_by_score.setdefault(row["score_id"], []).append(row)
    score_dimensions = {
        "correctness": ("correct", "partial", "incorrect", "unscored"),
        "completeness": ("complete", "partial", "incomplete", "unscored"),
        "citations": ("complete", "partial", "missing", "not_required", "unscored"),
        "unsupported_claim": ("yes", "no", "unknown", "unscored"),
    }
    per_assignment = [
        {
            "score_id": row["score_id"],
            "task_id": row["task_id"],
            "arm": row["arm"],
            "status": "scored" if scored_by_score.get(row["score_id"]) else "no_score",
            "scored_row_count": len(scored_by_score.get(row["score_id"], [])),
        }
        for row in assignments["assignments"]
    ]
    arms: dict[str, Any] = {}
    for arm in ARM_IDS:
        assigned = [row for row in assignments["assignments"] if row["arm"] == arm]
        outcomes = dict.fromkeys(OUTCOME_STATUSES, 0)
        score_counts = dict.fromkeys(SCORE_STATUSES, 0)
        dimension_counts = {
            key: dict.fromkeys(labels, 0) for key, labels in score_dimensions.items()
        }
        arm_scored: list[list[dict[str, Any]]] = []
        for assignment in assigned:
            observation = by_observation.get(assignment["assignment_id"])
            outcomes[observation["status"] if observation else "not_run"] += 1
            rows = scored_by_score.get(assignment["score_id"], [])
            score_counts["scored" if rows else "no_score"] += 1
            if rows:
                arm_scored.append(rows)
                for row in rows:
                    for dimension in score_dimensions:
                        dimension_counts[dimension][row[dimension]] += 1
            else:
                for dimension in score_dimensions:
                    dimension_counts[dimension]["unscored"] += 1
        pass_count = None
        if criterion:
            fields = PASS_CRITERIA[criterion]
            pass_count = sum(
                all(all(_binary_pass(row)[field] for field in fields) for row in rows)
                for rows in arm_scored
            )
        arms[arm] = {
            "assigned": len(assigned),
            "observed": len(assigned) - outcomes["not_run"],
            "outcomes": outcomes,
            "scores": score_counts,
            "quality": dimension_counts,
            "completed": outcomes["completed"],
            "quality_status": _quality_status(bool(arm_scored), criterion),
            "quality_scored_assignments": len(arm_scored),
            "quality_pass_count": pass_count,
            "quality_pass_criterion": criterion or "not_declared",
        }

    distribution = {"0": 0, "1": 0, "2_or_more": 0}
    for item in per_assignment:
        count = item["scored_row_count"]
        distribution["0" if count == 0 else "1" if count == 1 else "2_or_more"] += 1
    scorer_codes = {row["scorer_code"] for row in scored if row["status"] == "scored"}
    limitations = ["single_scorer"] if distribution["1"] else []
    agreement_items = [rows for rows in scored_by_score.values() if len(rows) >= 2]

    def payload_total(row: dict[str, Any]) -> int | None:
        fields = ("estimated_input_tokens", "estimated_output_tokens", "schema_instruction_tokens")
        values = [row["metrics"][key] for key in fields]
        return sum(values) if all(value is not None for value in values) else None

    is_synthetic = not any(
        row["participant_kind"] == "pseudonymous" for row in assignments["assignments"]
    )
    value_label = SYNTHETIC_VALUE_LABEL if is_synthetic else PARTICIPANT_VALUE_LABEL
    payload_arm: dict[str, Any] = {}
    for arm in ARM_IDS:
        rows = [row for row in observed if row["arm"] == arm]
        complete = [payload_total(row) for row in rows]
        complete = [value for value in complete if value is not None]
        payload_arm[arm] = {
            "total_estimated_tokens": sum(complete) if complete else None,
            "estimate_count": len(complete),
            "observed_count": len(rows),
            "counter_versions": sorted(
                {row["counter_version"] for row in rows if row["counter_version"]}
            ),
            "value_label": value_label,
        }

    pair_index: dict[tuple[str, str], dict[str, dict[str, Any]]] = {}
    for row in observed:
        pair_index.setdefault((row["participant_id"], row["task_id"]), {})[row["arm"]] = row
    comparable_pairs = []
    incomparable_reasons: dict[str, int] = {}
    for key, arms_for_task in sorted(pair_index.items()):
        a, b = arms_for_task.get("A"), arms_for_task.get("B")
        reason = None
        if a is None or b is None:
            reason = "arm_observation_missing"
        elif a["status"] != "completed" or b["status"] != "completed":
            reason = "arm_not_completed"
        elif payload_total(a) is None or payload_total(b) is None:
            reason = "estimate_incomplete"
        else:
            comparable_fields = ("model", "settings")
            if any(
                a["versions"][field] is None
                or b["versions"][field] is None
                or a["versions"][field] != b["versions"][field]
                for field in comparable_fields
            ):
                reason = "model_or_settings_mismatch"
            elif a["source_snapshot_sha256"] != b["source_snapshot_sha256"]:
                reason = "source_snapshot_mismatch"
            elif a["counter_version"] is None or a["counter_version"] != b["counter_version"]:
                reason = "counter_version_mismatch"
        if reason:
            incomparable_reasons[reason] = incomparable_reasons.get(reason, 0) + 1
        else:
            comparable_pairs.append(
                {
                    "participant_id": key[0],
                    "task_id": key[1],
                    "a_estimated_tokens": payload_total(a),
                    "b_estimated_tokens": payload_total(b),
                    "signed_delta_a_minus_b": payload_total(a) - payload_total(b),
                }
            )
    evidence_class = SYNTHETIC if is_synthetic else CONSENTED_DEIDENTIFIED
    units = [
        PairedUnit(
            cluster_id=row["participant_id"],
            difference=Decimal(row["b_estimated_tokens"] - row["a_estimated_tokens"]),
        )
        for row in comparable_pairs
    ]
    min_units = max(2, manifest["study_plan"]["minimum_completed_per_arm"])
    min_clusters = max(2, manifest["study_plan"]["target_participants"])
    interval = paired_difference_interval(units, seed=INTERVAL_SEED)
    decision = savings_decision(
        interval,
        min_units=min_units,
        min_clusters=min_clusters,
        evidence_class=evidence_class,
        # Pilot token counts are participant-supplied estimates, not billing data.
        provenance_verified=False,
    )
    payload_comparison = {
        "comparison": "paired_A_minus_B",
        "status": "available_estimate" if comparable_pairs else "unavailable",
        "comparable_pair_count": len(comparable_pairs),
        "incomparable_pair_count": sum(incomparable_reasons.values()),
        "incomparable_reasons": incomparable_reasons,
        "signed_token_delta_a_minus_b": sum(
            row["signed_delta_a_minus_b"] for row in comparable_pairs
        )
        if comparable_pairs
        else None,
        "paired_estimates": comparable_pairs,
        "scope": "completed_pairs_matching_source_snapshot_model_settings_and_counter_version",
        "value_label": value_label,
        "statistical_comparison": {
            "comparison": f"paired_{CANDIDATE_ARM}_minus_{REFERENCE_ARM}_candidate_minus_reference",
            "candidate_arm": CANDIDATE_ARM,
            "reference_arm": REFERENCE_ARM,
            "cluster": "participant",
            "interval": _interval_json(interval),
            "min_units": min_units,
            "min_clusters": min_clusters,
            "decision": decision,
            "decision_scope": "pilot token estimates are not billing data; provenance_unverified"
            " blocks any savings claim",
        },
    }
    pair_arms = {(r["participant_id"], r["arm"]) for r in assignments["assignments"]}
    window_arms = {(r["participant_id"], r["arm"]) for r in windows}
    window_pair_count = len(windows)
    fresh = [r for r in windows if r["freshness"] == "fresh"]
    expected_map: dict[tuple[str, str], set[str]] = {}
    for row in assignments["assignments"]:
        observation = by_observation.get(row["assignment_id"])
        if observation is not None and observation["status"] != "not_run":
            expected_map.setdefault((row["participant_id"], row["arm"]), set()).add(
                row["assignment_id"]
            )
    mappings_ok = all(
        set(row["assignment_ids"]) == expected_map.get((row["participant_id"], row["arm"]), set())
        for row in windows
    )
    real_participant_ids = {
        row["participant_id"]
        for row in assignments["assignments"]
        if row["participant_kind"] == "pseudonymous"
    }
    observed_ids = {row["assignment_id"] for row in observed if row["status"] != "not_run"}
    mapped_ids = {assignment_id for row in windows for assignment_id in row["assignment_ids"]}
    values_available = bool(windows) and all(
        any(value is not None for value in row["normalized_usage_buckets"].values())
        for row in windows
    )
    values_complete = bool(windows) and all(row["data_coverage"] == "complete" for row in windows)
    costs_available = bool(windows) and all(row["cost_amount"] is not None for row in windows)
    if not real_participant_ids:
        enterprise_status, reason = "pending", "synthetic_fixture_has_no_enterprise_usage"
    elif not windows:
        enterprise_status, reason = "pending", "fresh_admin_usage_windows_missing"
    elif len(fresh) != len(windows):
        enterprise_status, reason = "pending", "usage_windows_stale_or_future"
    elif (
        window_arms != pair_arms
        or not mappings_ok
        or window_pair_count != len(window_arms)
        or not mapped_ids.issubset(observed_ids)
    ):
        enterprise_status, reason = "pending", "usage_window_mapping_incomplete"
    elif any(
        row["mapping_confidence"] == "low" or not row["exclusions_recorded"] for row in windows
    ):
        enterprise_status, reason = "pending", "mapping_or_exclusion_evidence_incomplete"
    elif not values_available:
        enterprise_status, reason = "pending", "usage_measurements_unavailable"
    elif not values_complete:
        enterprise_status, reason = "partial", "usage_measurements_incomplete"
    elif not costs_available:
        enterprise_status, reason = "partial", "cost_measurements_unavailable"
    else:
        enterprise_status, reason = "available_with_attribution_limits", None
    return {
        "schema_version": SCHEMA_VERSION,
        "study_id": manifest["study_id"],
        "bundle_sha256": digest_json(manifest),
        "assignment_sha256": digest_json(assignments),
        "evidence_class": "participant_import" if real_participant_ids else "synthetic",
        "as_of_utc": _utc(as_of_utc).isoformat().replace("+00:00", "Z"),
        "arm_ids": list(ARM_IDS),
        "study_plan": manifest["study_plan"],
        "allocation": {
            "method": ALLOCATION_METHOD,
            "seed_mode": assignments["seed_mode"],
            "seed_commitment": assignments["seed_commitment"],
            "verification": _allocation_verification(
                assignments["seed_mode"], seed_hex is not None
            ),
        },
        "assignment_count": len(assignments["assignments"]),
        "arms": arms,
        "blind_scoring": {
            "score_count": len(scored),
            "scored_row_count": sum(row["status"] == "scored" for row in scored),
            "blind_scores_imported": bool(scored),
            "unscored_count": sum(row["status"] == "no_score" for row in scored),
            "quality_method": "independent_blind_score_import" if scored else "not_scored",
            "quality_status": _quality_status(bool(scored_by_score), criterion),
            "quality_pass_criterion": criterion or "not_declared",
            "scorer_count": len(scorer_codes),
            "scored_count_distribution": distribution,
            "per_assignment": per_assignment,
            "agreement": _agreement_summary(agreement_items, len(scorer_codes)),
            "limitations": limitations,
            "synthetic_rule_checks_are_participant_quality": False,
        },
        "payload_estimate": {
            "status": "available_estimate"
            if any(item["estimate_count"] for item in payload_arm.values())
            else "unavailable",
            "by_arm": payload_arm,
            "coverage": len(observed),
            "paired_comparison": payload_comparison,
            "counter_scope": "participant_supplied_inner_payload_estimates",
        },
        "modeled_economics": {
            "status": "unavailable",
            "reason": "rates_cache_mix_and_incremental_service_cost_not_supplied",
            "currency": None,
        },
        "enterprise_observed": {
            "status": enterprise_status,
            "enterprise_usage_observed": bool(real_participant_ids)
            and enterprise_status in {"available_with_attribution_limits", "partial"},
            "reason": reason,
            "usage_window_count": len(windows),
            "fresh_window_count": len(fresh),
            "usage_windows": [
                {
                    "participant_id": row["participant_id"],
                    "arm": row["arm"],
                    "assignment_count": len(row["assignment_ids"]),
                    "start_utc": row["start_utc"],
                    "end_utc": row["end_utc"],
                    "refreshed_at_utc": row["refreshed_at_utc"],
                    "freshness": row["freshness"],
                    "mapping_confidence": row["mapping_confidence"],
                    "exclusions_recorded": row["exclusions_recorded"],
                    "unrelated_activity": row["unrelated_activity"],
                    "exclusion_codes": row["exclusion_codes"],
                    "data_coverage": row["data_coverage"],
                    "cost_amount": row["cost_amount"],
                    "cost_missing_reason": row["cost_missing_reason"],
                    "currency": row["currency"],
                    "usage_buckets": row["normalized_usage_buckets"],
                }
                for row in windows
            ],
            "causal_claim": False,
        },
        "execution": {
            "model_api_calls": 0,
            "network_calls": 0,
            "raw_prompts_or_source_data_persisted": False,
        },
        "claims": {"savings_claim": decision["claim"]},
        "limitations": [
            "token and payload counts are estimates, not Claude usage",
            "synthetic checks validate harness rules only and are not participant quality scores",
            "Enterprise time buckets require explicit mapping and remain observational",
            "modeled economics need supplied rates, cache mix and incremental service costs",
        ],
    }


def synthetic_rule_check(kind: str, expected: Any, actual: Any) -> bool:
    """Compare a synthetic observed answer with the expected answer for one task kind."""
    if kind == "integer_equal":
        return type(actual) is int and actual == expected
    if kind == "boolean_equal":
        return type(actual) is bool and actual is expected
    if kind == "literal_equal":
        return type(actual) is str and actual == expected
    raise PilotStudyError("synthetic_check_kind_invalid")
