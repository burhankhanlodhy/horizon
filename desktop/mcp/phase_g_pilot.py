"""Offline assignment, import-validation and report CLI for the Phase G M6 pilot.

No model, network or provider API is called. Input files are bounded JSON
metadata contracts; the CLI has no fields for prompts or source content.

Real cohorts: `assign` writes the secret seed to `--seed-out` (created new,
never overwritten) and records only its commitment in the assignment file.
Reveal the seed after collection with `verify-assignments --seed-file`.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import sys
from pathlib import Path
from typing import Any

_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(_REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPOSITORY_ROOT))

from horizon.desktop_mcp.pilot_study import (  # noqa: E402
    PILOT_BUNDLE_KIND,
    SCHEMA_VERSION,
    SEED_HEX,
    PilotStudyError,
    build_assignments,
    build_committed_assignments,
    build_report,
    digest_json,
    generate_participant_codes,
    generate_scorer_codes,
    participant_code_kind,
    synthetic_rule_check,
    task_source_snapshot,
    validate_answer_key,
    validate_assignment_set,
    validate_bundle,
    validate_withdrawals,
)

_DEFAULT_BUNDLE = Path(__file__).with_name("phase_g_pilot_fixtures.json")
_MAX_INPUT_BYTES = 8 * 1024 * 1024
_MAX_SEED_FILE_BYTES = 256
_SYNTHETIC_AS_OF = "2026-10-14T12:00:00Z"
_HASH_CHUNK_BYTES = 1024 * 1024
_REPARSE_POINT_ATTRIBUTE = 0x400


def _read_json(path: Path) -> Any:
    try:
        with path.open("rb") as stream:
            data = stream.read(_MAX_INPUT_BYTES + 1)
    except OSError:
        raise PilotStudyError("input_unavailable") from None
    if len(data) > _MAX_INPUT_BYTES:
        raise PilotStudyError("input_too_large")

    def reject_constant(_value: str) -> Any:
        raise ValueError("non_finite_number")

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate_key")
            result[key] = value
        return result

    try:
        decoded = data.decode("utf-8")
        depth = 0
        in_string = False
        escaped = False
        for char in decoded:
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
            elif char == '"':
                in_string = True
            elif char in "[{":
                depth += 1
                if depth > 64:
                    raise ValueError("json_nesting_too_deep")
            elif char in "]}":
                depth -= 1
        return json.loads(decoded, parse_constant=reject_constant, object_pairs_hook=unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError):
        raise PilotStudyError("input_json_invalid") from None


def _read_seed_file(path: Path) -> str:
    try:
        with path.open("rb") as stream:
            data = stream.read(_MAX_SEED_FILE_BYTES + 1)
    except OSError:
        raise PilotStudyError("seed_file_unavailable") from None
    if len(data) > _MAX_SEED_FILE_BYTES:
        raise PilotStudyError("seed_file_invalid")
    try:
        text = data.decode("ascii").strip()
    except UnicodeDecodeError:
        raise PilotStudyError("seed_file_invalid") from None
    if not SEED_HEX.fullmatch(text):
        raise PilotStudyError("seed_file_invalid")
    return text


def _write_seed_file(path: Path, seed_hex: str) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("x", encoding="ascii", newline="\n") as stream:
            stream.write(seed_hex + "\n")
    except FileExistsError:
        raise PilotStudyError("seed_file_exists") from None
    except OSError:
        raise PilotStudyError("seed_output_unavailable") from None


def _write_json(value: Any, path: Path | None) -> str:
    encoded = (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n"
    )
    if path is not None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(encoded, encoding="utf-8", newline="\n")
        except OSError:
            raise PilotStudyError("output_unavailable") from None
    return encoded


def make_score_sheet(bundle: Any, assignment_set: Any) -> dict[str, Any]:
    assignments = validate_assignment_set(bundle, assignment_set)
    return {
        "schema_version": SCHEMA_VERSION,
        "study_id": assignments["study_id"],
        "scores": [
            {
                "score_id": row["score_id"],
                "blinded_arm_code": row["blind_arm_code"],
                "task_id": row["task_id"],
                "scorer_code": "unassigned",
                "status": "no_score",
                "correctness": "unscored",
                "completeness": "unscored",
                "citations": "unscored",
                "unsupported_claim": "unscored",
                "omission_codes": [],
            }
            for row in assignments["assignments"]
        ],
    }


def make_task_book(bundle: Any) -> dict[str, Any]:
    """Export operator cards. Answer keys are never read; real bundles export source metadata only."""
    manifest = validate_bundle(bundle)
    real = manifest["bundle_kind"] == PILOT_BUNDLE_KIND
    tasks = []
    for task in manifest["tasks"]:
        card: dict[str, Any] = {
            "task_id": task["task_id"],
            "stratum": task["stratum"],
            "locale": task["locale"],
            "operator_instruction": task["operator_instruction"],
            "source_snapshot_sha256": digest_json(task_source_snapshot(task)),
        }
        if real:
            card["sources"] = task["sources"]
        else:
            card["synthetic_sources"] = task["synthetic_sources"]
        tasks.append(card)
    return {
        "schema_version": SCHEMA_VERSION,
        "study_id": manifest["study_id"],
        "task_book_kind": "pilot_operator_cards" if real else "synthetic_operator_cards",
        "tasks": tasks,
    }


def _is_link_or_reparse(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & _REPARSE_POINT_ATTRIBUTE
    )


def _check_source_file(path: Path, expected: dict[str, Any]) -> str | None:
    """Return a bounded failure code, or None when the file matches its declaration."""
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return "source_missing"
    except OSError:
        return "source_unavailable"
    if _is_link_or_reparse(info):
        return "source_link_refused"
    if not stat.S_ISREG(info.st_mode):
        return "source_not_regular_file"
    if getattr(info, "st_nlink", 1) != 1:
        return "source_hardlink_refused"
    if info.st_size != expected["bytes"]:
        return "source_size_mismatch"
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return "source_unavailable"
    try:
        opened = os.fstat(descriptor)
        same_entry = not info.st_ino or (opened.st_dev, opened.st_ino) == (
            info.st_dev,
            info.st_ino,
        )
        if opened.st_size != info.st_size or not same_entry:
            return "source_changed_during_check"
        digest = hashlib.sha256()
        total = 0
        while True:
            chunk = os.read(descriptor, _HASH_CHUNK_BYTES)
            if not chunk:
                break
            total += len(chunk)
            digest.update(chunk)
    except OSError:
        return "source_unavailable"
    finally:
        os.close(descriptor)
    if total != expected["bytes"]:
        return "source_size_mismatch"
    if digest.hexdigest() != expected["sha256"]:
        return "source_hash_mismatch"
    return None


def verify_sources(bundle: Any, sources_dir: Path) -> dict[str, Any]:
    """Check each declared source directly inside sources_dir; links and reparse points fail."""
    manifest = validate_bundle(bundle)
    if manifest["bundle_kind"] != PILOT_BUNDLE_KIND:
        raise PilotStudyError("verify_sources_requires_pilot_bundle")
    try:
        directory = os.lstat(sources_dir)
    except FileNotFoundError:
        raise PilotStudyError("sources_dir_missing") from None
    except OSError:
        raise PilotStudyError("sources_dir_unavailable") from None
    if _is_link_or_reparse(directory):
        raise PilotStudyError("sources_dir_link_refused")
    if not stat.S_ISDIR(directory.st_mode):
        raise PilotStudyError("sources_dir_invalid")
    failures: list[dict[str, str]] = []
    checked = 0
    for task in manifest["tasks"]:
        for source in task["sources"]:
            checked += 1
            code = _check_source_file(sources_dir / source["file_name"], source)
            if code is not None:
                failures.append({"source_id": source["source_id"], "code": code})
    return {
        "schema_version": SCHEMA_VERSION,
        "study_id": manifest["study_id"],
        "bundle_sha256": digest_json(manifest),
        "status": "verified" if not failures else "failed",
        "sources_checked": checked,
        "failures": failures,
    }


def make_fixture_report(bundle: Any | None = None) -> dict[str, Any]:
    manifest = validate_bundle(bundle if bundle is not None else _read_json(_DEFAULT_BUNDLE))
    assignment_set = build_assignments(manifest, ["syn-p-0001"], seed=20261014)
    observations = []
    input_by_arm = {"A": 100, "B": 70, "C_normal": 120, "C_tuned": 90}
    for assignment in assignment_set["assignments"]:
        task_id, arm = assignment["task_id"], assignment["arm"]
        status = "completed"
        if task_id == "t_unsupported_format":
            status = "unsupported"
        elif task_id == "t_long_prose" and arm == "B":
            status = "failed"
        elif task_id == "t_source_free" and arm == "C_normal":
            status = "bypassed"
        elif task_id == "t_repeated_sources" and arm == "C_tuned":
            status = "not_run"
        input_tokens = 140 if task_id == "t_rare_outlier" and arm == "B" else input_by_arm[arm]
        row_metrics = {
            "latency_ms": None if status == "not_run" else 1000 + input_by_arm[arm],
            "retry_count": None if status == "not_run" else 0,
            "tool_call_count": None if status == "not_run" else 1,
            "retrieval_bytes": None if status == "not_run" else 64,
            "estimated_input_tokens": None if status == "not_run" else input_tokens,
            "estimated_output_tokens": None if status == "not_run" else 10,
            "schema_instruction_tokens": None if status == "not_run" else 5,
        }
        metric_reasons = {key: "not_run" for key, value in row_metrics.items() if value is None}
        usage = {
            "uncached_input_tokens": None,
            "cache_write_5m_tokens": None,
            "cache_write_1h_tokens": None,
            "cache_read_tokens": None,
            "billed_output_tokens": None,
        }
        usage_reasons = dict.fromkeys(
            usage, "not_run" if status == "not_run" else "not_available_from_export"
        )
        task = next(item for item in manifest["tasks"] if item["task_id"] == task_id)
        observations.append(
            {
                "assignment_id": assignment["assignment_id"],
                "participant_id": assignment["participant_id"],
                "task_id": task_id,
                "pair_id": assignment["pair_id"],
                "arm": arm,
                "status": status,
                "start_utc": None if status == "not_run" else "2026-10-14T10:00:00Z",
                "end_utc": None if status == "not_run" else "2026-10-14T10:01:00Z",
                "locale": assignment["locale"],
                "versions": {
                    "desktop": "synthetic-desktop",
                    "extension": "0.1.1",
                    "model": "synthetic-model",
                    "settings": "synthetic-default",
                },
                "counter_version": "synthetic-counter-v1",
                "source_snapshot_sha256": digest_json(task["synthetic_sources"]),
                "metrics": row_metrics,
                "metric_missing_reasons": metric_reasons,
                "provider_usage": usage,
                "usage_missing_reasons": usage_reasons,
                "failure_code": "synthetic_fixture_failure" if status == "failed" else None,
                "unsupported_code": "synthetic_fixture_unsupported"
                if status == "unsupported"
                else None,
            }
        )
    report = build_report(
        manifest, assignment_set, observations, [], as_of_utc=_SYNTHETIC_AS_OF, usage_windows=[]
    )
    results = [
        synthetic_rule_check(
            task["synthetic_check"]["kind"],
            task["synthetic_check"]["expected"],
            task["synthetic_observed"],
        )
        for task in manifest["tasks"]
    ]
    passed = sum(results)
    report["fixture_provenance"] = "committed_synthetic_bundle_only"
    report["fixture_sha256"] = digest_json(manifest)
    report["fixture_rule_checks"] = {
        "status": "passed" if all(results) else "failed",
        "count": len(results),
        "passed_count": passed,
        "failed_count": len(results) - passed,
        "method": "fixture_observed_answer_vs_expected_contract_check",
        "participant_quality_claim": False,
    }
    report["evidence_class"] = "synthetic"
    report["claims"] = {"savings_claim": "none"}
    report["enterprise_observed"]["enterprise_usage_observed"] = False
    report["enterprise_observed"]["status"] = "pending"
    report["enterprise_observed"]["reason"] = "synthetic_fixture_has_no_enterprise_usage"
    return report


def make_import_report(
    bundle: Any,
    assignment_set: Any,
    observations: Any,
    scores: Any,
    *,
    as_of_utc: str,
    usage_windows: Any = None,
    seed_hex: str | None = None,
    withdrawn: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    return build_report(
        bundle,
        assignment_set,
        observations,
        scores,
        as_of_utc=as_of_utc,
        usage_windows=usage_windows,
        seed_hex=seed_hex,
        withdrawn=withdrawn,
    )


def _load_many(path: Path) -> Any:
    value = _read_json(path)
    if not isinstance(value, list):
        raise PilotStudyError("input_array_required")
    return value


def _assign(args: argparse.Namespace) -> dict[str, Any]:
    bundle = validate_bundle(_read_json(args.bundle))
    # Codes are often pasted from a file; surrounding whitespace (including a
    # Windows carriage return) is not part of a code. The format check still applies.
    codes = [code.strip() for code in args.participant_code]
    kinds = {participant_code_kind(code) for code in codes}
    if kinds == {"synthetic"}:
        if args.seed is None:
            raise PilotStudyError("synthetic_seed_required")
        if args.seed_out is not None:
            raise PilotStudyError("seed_out_not_applicable_to_synthetic")
        return build_assignments(bundle, codes, seed=args.seed)
    if kinds != {"pseudonymous"}:
        raise PilotStudyError("participant_kind_mixed")
    if args.seed is not None:
        raise PilotStudyError("seed_not_applicable_to_real_cohort")
    if args.seed_out is None:
        raise PilotStudyError("seed_out_required")
    if args.seed_out.resolve() == args.output.resolve():
        raise PilotStudyError("seed_output_conflict")
    consent = dict.fromkeys(codes, args.consent_at_utc) if args.consent_at_utc else {}
    assignments, seed_hex = build_committed_assignments(bundle, codes, consented_at_utc=consent)
    _write_seed_file(args.seed_out, seed_hex)
    return assignments


def _verify(args: argparse.Namespace) -> dict[str, Any]:
    bundle = validate_bundle(_read_json(args.bundle))
    assignments = _read_json(args.assignments)
    seed_hex = _read_seed_file(args.seed_file)
    validated = validate_assignment_set(bundle, assignments, seed_hex=seed_hex)
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "verified",
        "study_id": validated["study_id"],
        "seed_mode": validated["seed_mode"],
        "seed_commitment": validated["seed_commitment"],
        "assignment_count": len(validated["assignments"]),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    fixture = sub.add_parser(
        "fixture-report", help="validate the committed synthetic bundle offline"
    )
    fixture.add_argument("--bundle", type=Path, default=_DEFAULT_BUNDLE)
    fixture.add_argument("--output", type=Path)

    task_book = sub.add_parser(
        "task-book", help="export operator cards (instructions and sources), never answer keys"
    )
    task_book.add_argument("--bundle", type=Path, default=_DEFAULT_BUNDLE)
    task_book.add_argument("--output", type=Path, required=True)

    codes = sub.add_parser(
        "generate-participant-codes", help="print random p_ participant codes, one per line"
    )
    codes.add_argument("--count", type=int, required=True, help="1 to 500")

    scorer_codes = sub.add_parser(
        "generate-scorer-codes", help="print random s_ scorer codes, one per line"
    )
    scorer_codes.add_argument("--count", type=int, required=True, help="1 to 500")

    assign = sub.add_parser(
        "assign",
        help="create consent-gated assignments; real cohorts get a committed secret seed",
    )
    assign.add_argument("--bundle", type=Path, default=_DEFAULT_BUNDLE)
    assign.add_argument("--participant-code", action="append", required=True)
    assign.add_argument("--seed", type=int, help="public integer seed; synthetic codes only")
    assign.add_argument("--consent-at-utc", help="required for real p_ participant codes")
    assign.add_argument(
        "--seed-out",
        type=Path,
        help="new file for the secret seed of a real cohort; keep it outside the report folder",
    )
    assign.add_argument("--output", type=Path, required=True)

    verify = sub.add_parser(
        "verify-assignments",
        help="after seed reveal, recompute the allocation and check the commitment",
    )
    verify.add_argument("--bundle", type=Path, default=_DEFAULT_BUNDLE)
    verify.add_argument("--assignments", type=Path, required=True)
    verify.add_argument("--seed-file", type=Path, required=True)

    sources = sub.add_parser(
        "verify-sources",
        help="check real source files in a folder against the bundle's names, sizes and SHA-256",
    )
    sources.add_argument("--bundle", type=Path, required=True)
    sources.add_argument("--sources-dir", type=Path, required=True)
    sources.add_argument("--output", type=Path)

    sheet = sub.add_parser(
        "score-sheet", help="export a blinded score worksheet without arm or participant IDs"
    )
    sheet.add_argument("--bundle", type=Path, default=_DEFAULT_BUNDLE)
    sheet.add_argument("--assignments", type=Path, required=True)
    sheet.add_argument("--output", type=Path, required=True)

    report = sub.add_parser("report", help="validate imported observations and blind scores")
    report.add_argument("--bundle", type=Path, default=_DEFAULT_BUNDLE)
    report.add_argument("--assignments", type=Path, required=True)
    report.add_argument("--observations", type=Path, required=True)
    report.add_argument("--scores", type=Path, required=True)
    report.add_argument("--usage-windows", type=Path)
    report.add_argument(
        "--seed-file", type=Path, help="revealed seed; enables allocation recheck in the report"
    )
    report.add_argument(
        "--withdrawals", type=Path, help="withdrawal file; withdrawn rows are dropped first"
    )
    report.add_argument(
        "--answer-key", type=Path, help="scorer-only key; only its digest and coverage are checked"
    )
    report.add_argument("--as-of-utc", required=True)
    report.add_argument("--output", type=Path)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "generate-participant-codes":
            sys.stdout.write("\n".join(generate_participant_codes(args.count)) + "\n")
            return 0
        if args.command == "generate-scorer-codes":
            sys.stdout.write("\n".join(generate_scorer_codes(args.count)) + "\n")
            return 0
        output: Path | None = getattr(args, "output", None)
        if args.command == "fixture-report":
            result = make_fixture_report(_read_json(args.bundle))
        elif args.command == "task-book":
            result = make_task_book(_read_json(args.bundle))
        elif args.command == "assign":
            result = _assign(args)
        elif args.command == "verify-assignments":
            result = _verify(args)
        elif args.command == "verify-sources":
            result = verify_sources(_read_json(args.bundle), args.sources_dir)
            sys.stdout.write(_write_json(result, output))
            return 0 if result["status"] == "verified" else 2
        elif args.command == "score-sheet":
            bundle = validate_bundle(_read_json(args.bundle))
            assignments = _read_json(args.assignments)
            result = make_score_sheet(bundle, assignments)
        else:
            bundle = validate_bundle(_read_json(args.bundle))
            assignments = _read_json(args.assignments)
            usage = _load_many(args.usage_windows) if args.usage_windows else []
            seed_hex = _read_seed_file(args.seed_file) if args.seed_file else None
            score_input = _read_json(args.scores)
            if isinstance(score_input, dict):
                if set(score_input) != {"schema_version", "study_id", "scores"}:
                    raise PilotStudyError("score_sheet_schema_invalid")
                if (
                    score_input["schema_version"] != SCHEMA_VERSION
                    or score_input["study_id"] != bundle["study_id"]
                ):
                    raise PilotStudyError("score_sheet_study_mismatch")
                score_rows = score_input["scores"]
            elif isinstance(score_input, list):
                score_rows = score_input
            else:
                raise PilotStudyError("input_array_or_score_sheet_required")
            withdrawn = (
                validate_withdrawals(_read_json(args.withdrawals), study_id=bundle["study_id"])
                if args.withdrawals
                else frozenset()
            )
            result = make_import_report(
                bundle,
                assignments,
                _load_many(args.observations),
                score_rows,
                as_of_utc=args.as_of_utc,
                usage_windows=usage,
                seed_hex=seed_hex,
                withdrawn=withdrawn,
            )
            if args.answer_key:
                key = validate_answer_key(_read_json(args.answer_key), bundle)
                result["answer_key_check"] = {
                    "status": "verified",
                    "task_count": len(key["keys"]),
                    "values_recorded": False,
                }
        sys.stdout.write(_write_json(result, output))
        return 0
    except PilotStudyError as error:
        sys.stderr.write(f"phase-g-pilot: {error.code}\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
