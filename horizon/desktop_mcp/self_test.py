"""Single-account self test: pack, schedule, prompts, answer checks, ledger, state, analysis.

This module holds the logic only. It never prompts, never reads Claude Desktop,
and never stores answer text, file contents or paths. The terminal wizard calls
the functions below; the typical flow is::

    pack = validate_pack(json.load(f))
    seed = new_seed_hex()
    ledger = Ledger(default_state_dir(pack["study_id"]) / "ledger.jsonl")
    state = StudyState.from_ledger(pack, ledger.load())
    record = ledger.build("study_started", **study_started_fields(pack, seed))
    state = state.with_record(record)        # raises SelfTestError if the rule breaks
    ledger.append_record(record)            # fsync'd, hash-chained

Every write follows the same pattern: build a record, check it with
``state.with_record(record)``, then ``ledger.append_record(record)``. Readings are
strings with exactly two decimals. Answers are represented by a sha256 digest.

Public API (see the docstrings for details):

- ``SelfTestError(code)``; ``code`` is always a stable, sanitised identifier.
- ``validate_pack``, ``pack_digest``, ``new_seed_hex``, ``build_schedule``.
- ``render_prompt``, ``method_setup``, ``check_answer``, ``answer_digest``.
- ``Ledger``, ``default_state_dir``, ``study_started_fields``.
- ``StudyState``: ``from_ledger``, ``with_record``, ``next_action`` and status properties.
- ``reconcile`` (dry-run result for one day), ``analyze`` (the report dict).
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import random
import re
import secrets
from datetime import datetime, timezone
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

from horizon.desktop_mcp.answer_contract import instruction_pack
from horizon.desktop_mcp.evidence_stats import (
    SAVINGS_RULE,
    SELF_METERED,
    PairedUnit,
    paired_difference_interval,
    savings_decision,
)

SCHEMA_VERSION = 1
PACK_KIND = "self_test_pack"
REPORT_KIND = "self_test_report"

METHODS: tuple[str, ...] = ("A", "B", "C_normal", "C_tuned")
ROUNDS = 2
DAY_COUNT = ROUNDS * len(METHODS)

# Williams-style balanced 4x4 Latin square over indexes into METHODS. Each row is a
# permutation, each column is a permutation, and every ordered pair of methods is
# adjacent exactly once across the four rows.
ROWS: tuple[tuple[int, ...], ...] = ((0, 1, 3, 2), (1, 2, 0, 3), (2, 3, 1, 0), (3, 0, 2, 1))

# Fixed seed for the bootstrap so that a given ledger always yields the same report.
REPORT_SEED = 20261015
RECONCILE_CENT = Decimal("0.01")
MAX_BLOCK_DELTA = Decimal("20.00")
MAX_READING = Decimal("100000.00")
MAX_QUALITY_TOLERANCE = 4
SOURCE_FORMATS: tuple[str, ...] = ("txt", "md", "json", "csv", "pdf", "docx")
ANSWER_KINDS: tuple[str, ...] = ("number", "text_contains", "date", "choice")
VOID_REASONS: tuple[str, ...] = ("interrupted", "chat_not_fresh", "setup_wrong", "other")

FLAG_SPEND_BETWEEN = "spend_between_blocks"
FLAG_BACKWARDS = "meter_went_backwards"
FLAG_BLOCK_VOIDED = "block_voided"
FLAG_OTHER_DATE = "finished_on_other_date"
REASON_QUALITY = "quality_regression"
REASON_PROVENANCE = "provenance_unverified"

SCOPE = {
    "label": "single_account_self_test",
    "meter_resolution": "$0.01",
    "statement": "results apply to this account, model setting and task set only",
}

COMPARISONS: tuple[tuple[str, str, str, bool], ...] = (
    ("B_vs_C_normal", "B", "C_normal", True),
    ("B_vs_A", "B", "A", False),
    ("B_vs_C_tuned", "B", "C_tuned", False),
    ("C_tuned_vs_C_normal", "C_tuned", "C_normal", False),
)

_STUDY_ID = re.compile(r"^st_[a-z0-9_]{1,40}$")
_TASK_ID = re.compile(r"^t_[a-z0-9_]{1,40}$")
_GROUP_ID = re.compile(r"^g[a-z0-9_]{1,40}$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_READING = re.compile(r"^[0-9]{1,6}\.[0-9]{2}$")
_DATE_TEXT = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
_UTC_TEXT = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")
_OFFSET_TEXT = re.compile(r"^[+-][0-9]{2}:[0-9]{2}$")
_NUMBER_EXPECTED = re.compile(r"^-?[0-9]{1,12}(?:\.[0-9]{1,6})?$")
_NUMBER_TOLERANCE = re.compile(r"^[0-9]{1,12}(?:\.[0-9]{1,6})?$")
_FILE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_ .-]{0,119}$")
_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{index}" for index in range(1, 10)}
    | {f"LPT{index}" for index in range(1, 10)}
)
_NUMERIC_TEXT = re.compile(r"^[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)$")
_ANSWER_LINE = re.compile(r"^\s*answer:\s*(.*)$", re.IGNORECASE)
_ISO_DATE = re.compile(r"^([0-9]{4})-([0-9]{2})-([0-9]{2})$")
_MONTH_DAY_YEAR = re.compile(r"^([A-Za-z]+) ([0-9]{1,2}), ([0-9]{4})$")
_DAY_MONTH_YEAR = re.compile(r"^([0-9]{1,2}) ([A-Za-z]+),? ([0-9]{4})$")
_MONTHS: dict[str, int] = {
    name: number
    for number, names in enumerate(
        [
            ("january", "jan"),
            ("february", "feb"),
            ("march", "mar"),
            ("april", "apr"),
            ("may", "may"),
            ("june", "jun"),
            ("july", "jul"),
            ("august", "aug"),
            ("september", "sep", "sept"),
            ("october", "oct"),
            ("november", "nov"),
            ("december", "dec"),
        ],
        start=1,
    )
    for name in names
}

_PACK_KEYS = frozenset(
    {
        "schema_version",
        "kind",
        "study_id",
        "model_label",
        "minimums",
        "quality_tolerance",
        "groups",
        "tasks",
    }
)
_ZERO_HASH = "0" * 64
_CHAIN_FIELDS = frozenset({"seq", "prev_sha256"})
_COMMON_FIELDS = frozenset({"type", "utc", "local_date", "utc_offset"})
_RECORD_FIELDS: dict[str, frozenset[str]] = {
    "study_started": frozenset({"pack_digest", "seed_hex", "schedule"}),
    "day_started": frozenset({"day", "round", "method", "setup_confirmed"}),
    "block_before": frozenset({"group_id", "total"}),
    "block_after": frozenset({"group_id", "total"}),
    "task_answer": frozenset({"task_id", "correct", "answer_sha256"}),
    "block_voided": frozenset({"group_id", "reason"}),
    "day_finished": frozenset({"status"}),
    "day_reconciled": frozenset({"desktop_total"}),
}
LEDGER_TYPES: tuple[str, ...] = tuple(_RECORD_FIELDS)

_ENDING = '\n\nEnd with one line that starts with "ANSWER:" and gives only the final answer.'


class SelfTestError(ValueError):
    """Validation or rule failure with a stable, sanitised code and no source data."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


# --------------------------------------------------------------------------- helpers


def _fail(code: str) -> SelfTestError:
    return SelfTestError(code)


def _exact_keys(value: Any, keys: frozenset[str], code: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise _fail(code)
    return value


def _int(value: Any, low: int, high: int, code: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise _fail(code)
    return value


def _text(value: Any, pattern: re.Pattern[str], code: str) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise _fail(code)
    return value


def _list(value: Any, low: int, high: int, code: str) -> list[Any]:
    if not isinstance(value, list) or not low <= len(value) <= high:
        raise _fail(code)
    return value


def _printable(value: Any, max_chars: int, code: str) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > max_chars
        or not all(char.isprintable() for char in value)
    ):
        raise _fail(code)
    return value


def _plain_text(value: Any, max_chars: int, code: str) -> str:
    """Text that may contain newlines and tabs but no other control characters."""
    if not isinstance(value, str) or not value.strip() or len(value) > max_chars:
        raise _fail(code)
    for char in value:
        if (ord(char) < 32 and char not in "\n\t") or ord(char) == 127:
            raise _fail(code)
    return value


def _valid_date_text(value: Any) -> str | None:
    """Return a YYYY-MM-DD string that is a real calendar date, else None."""
    if not isinstance(value, str) or not _DATE_TEXT.fullmatch(value):
        return None
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        return None
    return value


def _reading(value: Any, code: str = "reading_invalid") -> Decimal:
    """A dollar reading: exactly two decimals, 0 to 100,000.00 inclusive."""
    if not isinstance(value, str) or not _READING.fullmatch(value):
        raise _fail(code)
    amount = Decimal(value)
    if amount > MAX_READING:
        raise _fail(code)
    return amount


def _money(value: Decimal, rounding: str = ROUND_HALF_UP) -> str:
    """Format a Decimal as a 2-decimal string. Negative zero is shown as 0.00."""
    quantized = value.quantize(RECONCILE_CENT, rounding=rounding)
    if quantized == 0:
        quantized = abs(quantized)
    return str(quantized)


def _seed(seed_hex: Any) -> int:
    return int(_text(seed_hex, _HEX64, "seed_invalid"), 16)


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def _source_file_name(value: Any) -> str:
    """Safe base name with a supported suffix. Mirrors pilot_study's file-name rules."""
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 120
        or not _FILE_NAME.fullmatch(value)
        or value.endswith((".", " "))
        or ".." in value
    ):
        raise _fail("source_file_name_invalid")
    if value.split(".", 1)[0].rstrip(" ").upper() in _RESERVED_NAMES:
        raise _fail("source_file_name_reserved")
    stem, dot, suffix = value.rpartition(".")
    if not dot or not stem or suffix.lower() not in SOURCE_FORMATS:
        raise _fail("source_format_mismatch")
    return value


def _answer_spec(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _fail("answer_invalid")
    kind = value.get("kind")
    if kind not in ANSWER_KINDS:
        raise _fail("answer_kind_invalid")
    if kind == "number":
        if not set(value) <= {"kind", "expected", "tolerance"} or "expected" not in value:
            raise _fail("answer_invalid")
        expected = _text(value["expected"], _NUMBER_EXPECTED, "answer_expected_invalid")
        tolerance = _text(
            value.get("tolerance", "0"), _NUMBER_TOLERANCE, "answer_tolerance_invalid"
        )
        return {"kind": kind, "expected": expected, "tolerance": tolerance}
    raw = _exact_keys(value, frozenset({"kind", "expected"}), "answer_invalid")
    if kind == "text_contains":
        return {
            "kind": kind,
            "expected": _printable(raw["expected"], 200, "answer_expected_invalid"),
        }
    if kind == "date":
        expected = _valid_date_text(raw["expected"])
        if expected is None:
            raise _fail("answer_expected_invalid")
        return {"kind": kind, "expected": expected}
    choices = _list(raw["expected"], 1, 10, "answer_expected_invalid")
    return {
        "kind": kind,
        "expected": [_printable(item, 200, "answer_expected_invalid") for item in choices],
    }


def _task(value: Any) -> dict[str, Any]:
    raw = _exact_keys(
        value, frozenset({"task_id", "source_files", "question", "answer"}), "task_invalid"
    )
    files = [
        _source_file_name(name) for name in _list(raw["source_files"], 1, 4, "source_files_invalid")
    ]
    if len(set(files)) != len(files):
        raise _fail("source_files_invalid")
    return {
        "task_id": _text(raw["task_id"], _TASK_ID, "task_id_invalid"),
        "source_files": files,
        "question": _plain_text(raw["question"], 2000, "question_invalid"),
        "answer": _answer_spec(raw["answer"]),
    }


def _group(value: Any) -> dict[str, Any]:
    raw = _exact_keys(value, frozenset({"group_id", "tasks"}), "groups_invalid")
    tasks = [
        _text(item, _TASK_ID, "task_id_invalid")
        for item in _list(raw["tasks"], 1, 4, "groups_invalid")
    ]
    if len(set(tasks)) != len(tasks):
        raise _fail("group_coverage_invalid")
    return {"group_id": _text(raw["group_id"], _GROUP_ID, "group_id_invalid"), "tasks": tasks}


# ------------------------------------------------------------------------------- pack


def validate_pack(value: Any) -> dict[str, Any]:
    """Check a self-test pack and return a normalised copy (defaults filled in).

    Raises ``SelfTestError`` with a code such as ``group_coverage_invalid``.
    """
    raw = _exact_keys(value, _PACK_KEYS, "pack_invalid")
    _int(raw["schema_version"], SCHEMA_VERSION, SCHEMA_VERSION, "pack_schema_invalid")
    if raw["kind"] != PACK_KIND:
        raise _fail("pack_schema_invalid")
    minimums = _exact_keys(
        raw["minimums"], frozenset({"paired_units", "groups"}), "minimums_invalid"
    )
    paired_units = _int(minimums["paired_units"], 4, 10**6, "minimums_invalid")
    min_groups = _int(minimums["groups"], 3, 10**6, "minimums_invalid")
    tolerance = _int(
        raw["quality_tolerance"], 0, MAX_QUALITY_TOLERANCE, "quality_tolerance_invalid"
    )

    tasks = [_task(item) for item in _list(raw["tasks"], 1, 64, "tasks_invalid")]
    task_ids = [task["task_id"] for task in tasks]
    if len(set(task_ids)) != len(task_ids):
        raise _fail("task_id_duplicate")

    groups = [_group(item) for item in _list(raw["groups"], 4, 16, "groups_invalid")]
    group_ids = [group["group_id"] for group in groups]
    if len(set(group_ids)) != len(group_ids):
        raise _fail("group_id_duplicate")
    assigned = [task_id for group in groups for task_id in group["tasks"]]
    if len(assigned) != len(set(assigned)) or set(assigned) != set(task_ids):
        raise _fail("group_coverage_invalid")
    if min_groups > len(groups) or paired_units > ROUNDS * len(groups):
        raise _fail("minimums_unreachable")

    return {
        "schema_version": SCHEMA_VERSION,
        "kind": PACK_KIND,
        "study_id": _text(raw["study_id"], _STUDY_ID, "study_id_invalid"),
        "model_label": _printable(raw["model_label"], 80, "model_label_invalid"),
        "minimums": {"paired_units": paired_units, "groups": min_groups},
        "quality_tolerance": tolerance,
        "groups": groups,
        "tasks": tasks,
    }


def pack_digest(pack: Any) -> str:
    """Canonical JSON sha256 of the validated pack."""
    return hashlib.sha256(_canonical(validate_pack(pack))).hexdigest()


def new_seed_hex() -> str:
    """A fresh 256-bit seed for a study, as 64 lowercase hex characters."""
    return secrets.token_hex(32)


def study_started_fields(pack: Any, seed_hex: str) -> dict[str, Any]:
    """Fields for the ``study_started`` record: pack digest, seed and schedule."""
    checked = validate_pack(pack)
    return {
        "pack_digest": pack_digest(checked),
        "seed_hex": _text(seed_hex, _HEX64, "seed_invalid"),
        "schedule": build_schedule(checked, seed_hex),
    }


# --------------------------------------------------------------------------- schedule


def build_schedule(pack: Any, seed_hex: str) -> list[dict[str, Any]]:
    """Eight measurement days: two rounds of the four methods in a seeded balanced order.

    Each day is ``{"round", "method", "groups": [{"group_id", "tasks": [...]}]}``.
    Round 1 uses a seeded Latin-square row and round 2 a different row. Day ``index``
    (0 to 7) shuffles groups and tasks with ``random.Random(seed + index)``.
    """
    checked = validate_pack(pack)
    seed = _seed(seed_hex)
    chooser = random.Random(f"self-test-rows:{seed_hex}")
    first = chooser.randrange(len(ROWS))
    second = chooser.choice([index for index in range(len(ROWS)) if index != first])
    rows = {1: ROWS[first], 2: ROWS[second]}

    days: list[dict[str, Any]] = []
    for index in range(DAY_COUNT):
        round_no = index // len(METHODS) + 1
        method = METHODS[rows[round_no][index % len(METHODS)]]
        rng = random.Random(seed + index)
        order = list(checked["groups"])
        rng.shuffle(order)
        groups = []
        for group in order:
            tasks = list(group["tasks"])
            rng.shuffle(tasks)
            groups.append({"group_id": group["group_id"], "tasks": tasks})
        days.append({"round": round_no, "method": method, "groups": groups})
    return days


# ---------------------------------------------------------------------------- prompts


def _task_by_id(pack: dict[str, Any], task_id: str) -> dict[str, Any]:
    for task in pack["tasks"]:
        if task["task_id"] == task_id:
            return task
    raise _fail("task_unknown")


def render_prompt(pack: Any, task_id: str, method: str) -> str:
    """The exact prompt text for one task under one method."""
    checked = validate_pack(pack)
    task = _task_by_id(checked, task_id)
    if method not in METHODS:
        raise _fail("method_invalid")
    files = ", ".join(task["source_files"])
    question = task["question"]
    if method in ("A", "B"):
        body = f"Use the ContextShrink tools to read {files} from my approved folders, then answer this: {question}"
    else:
        body = f"I have attached {files}. Answer this: {question}"
    if method == "C_tuned":
        return instruction_pack("v1")["text"] + "\n\n" + body + _ENDING
    return body + _ENDING


def method_setup(method: str, pack: Any) -> list[str]:
    """Plain-language setup steps to confirm before a method's day starts."""
    checked = validate_pack(pack)
    if method not in METHODS:
        raise _fail("method_invalid")
    steps = [
        f"Set the model to {checked['model_label']}. Keep this model setting for all eight days.",
        "Start a fresh chat for every task.",
        "Read the running total in Claude Desktop settings before and after each group, and enter both readings.",
    ]
    if method == "A":
        steps += [
            "In the ContextShrink extension, set Pilot method to exact_only.",
            "If you changed Pilot method, fully quit Claude Desktop from the tray and reopen it.",
            "Turn the connector ON.",
            'Check it: ask Claude "show ContextShrink stats"; evaluation_arm must say raw.',
            "Do the check in its own chat before the first reading, so it is not measured.",
        ]
    elif method == "B":
        steps += [
            "In the ContextShrink extension, set Pilot method to normal.",
            "If you changed Pilot method, fully quit Claude Desktop from the tray and reopen it.",
            "Turn the connector ON.",
            'Check it: ask Claude "show ContextShrink stats"; evaluation_arm must say optimized.',
            "Do the check in its own chat before the first reading, so it is not measured.",
        ]
    else:
        steps += [
            "Turn the ContextShrink connector OFF.",
            "For each task, attach only that task's source files with the paperclip. The wizard shows their names.",
        ]
        if method == "C_tuned":
            steps.append(
                "Send each prompt unchanged. It begins with the ContextShrink answer rules."
            )
    return steps


# ---------------------------------------------------------------------------- answers


def _answer_candidate(reply_text: str) -> str:
    """The last line starting with ANSWER: (any case), else the whole reply."""
    candidate: str | None = None
    for line in reply_text.splitlines():
        match = _ANSWER_LINE.match(line)
        if match:
            candidate = match.group(1)
    if candidate is None:
        return reply_text.strip()
    return candidate.strip()


def _normal_words(text: str) -> str:
    return " ".join(text.split()).casefold()


def _parse_answer_date(text: str) -> str | None:
    """ISO, 'Month D, YYYY' or 'D Month YYYY' in English. Other forms are rejected."""
    text = " ".join(text.split())
    if text.endswith(".") and not _ISO_DATE.fullmatch(text):
        text = text[:-1]
    iso = _ISO_DATE.fullmatch(text)
    if iso:
        return _valid_date_text(text)
    month_day = _MONTH_DAY_YEAR.fullmatch(text)
    day_month = _DAY_MONTH_YEAR.fullmatch(text)
    if month_day:
        month_name, day, year = month_day.group(1), month_day.group(2), month_day.group(3)
    elif day_month:
        day, month_name, year = day_month.group(1), day_month.group(2), day_month.group(3)
    else:
        return None
    month = _MONTHS.get(month_name.lower())
    if month is None:
        return None
    return _valid_date_text(f"{int(year):04d}-{month:02d}-{int(day):02d}")


def check_answer(task: Any, reply_text: str) -> bool:
    """Whether a reply's answer matches the task's key.

    The answer is the last line starting with ``ANSWER:`` (case-insensitive), or the
    whole reply when there is no such line. Numbers ignore ``$``, ``,``, ``%`` and
    spaces. Text matching is case-folded and whitespace-normalised. Dates accept ISO
    or English month-name forms only.
    """
    if not isinstance(reply_text, str):
        raise _fail("reply_invalid")
    if not isinstance(task, dict):
        raise _fail("task_invalid")
    spec = _answer_spec(task.get("answer"))
    candidate = _answer_candidate(reply_text)
    kind = spec["kind"]
    if kind == "number":
        normalised = re.sub(r"[\s$,%]", "", candidate)
        if not _NUMERIC_TEXT.fullmatch(normalised):
            return False
        difference = abs(Decimal(normalised) - Decimal(spec["expected"]))
        return difference <= Decimal(spec["tolerance"])
    if kind == "text_contains":
        found = _normal_words(candidate)
        expected = _normal_words(spec["expected"])
        # Whole-word match: "90 days" must not be found inside "190 days".
        pattern = r"(?<!\w)" + re.escape(expected) + r"(?!\w)"
        return bool(found) and re.search(pattern, found) is not None
    if kind == "date":
        return _parse_answer_date(candidate) == spec["expected"]
    found = _normal_words(candidate)
    return any(found == _normal_words(choice) for choice in spec["expected"])


def answer_digest(reply_text: str) -> str:
    """sha256 of the whole reply. The answer text itself is never stored."""
    if not isinstance(reply_text, str):
        raise _fail("reply_invalid")
    return hashlib.sha256(reply_text.encode("utf-8")).hexdigest()


# ------------------------------------------------------------------------- ledger


def _check_record_shape(record: Any) -> None:
    """Structural check shared by the ledger and the state. Chain fields are optional."""
    if not isinstance(record, dict):
        raise _fail("record_invalid")
    record_type = record.get("type")
    if record_type not in _RECORD_FIELDS:
        raise _fail("record_type_unknown")
    required = _COMMON_FIELDS | _RECORD_FIELDS[record_type]
    keys = set(record)
    if not required <= keys or not keys <= required | _CHAIN_FIELDS:
        raise _fail("record_invalid")
    if not isinstance(record["utc"], str) or not _UTC_TEXT.fullmatch(record["utc"]):
        raise _fail("record_invalid")
    if _valid_date_text(record["local_date"]) is None:
        raise _fail("record_invalid")
    if not isinstance(record["utc_offset"], str) or not _OFFSET_TEXT.fullmatch(
        record["utc_offset"]
    ):
        raise _fail("record_invalid")
    if _CHAIN_FIELDS & keys:
        if isinstance(record.get("seq"), bool) or not isinstance(record.get("seq"), int):
            raise _fail("record_invalid")
        if not isinstance(record.get("prev_sha256"), str) or not _HEX64.fullmatch(
            record["prev_sha256"]
        ):
            raise _fail("record_invalid")
    _check_type_fields(record_type, record)


def _check_type_fields(record_type: str, record: dict[str, Any]) -> None:
    try:
        if record_type == "study_started":
            _text(record["pack_digest"], _HEX64, "record_invalid")
            _text(record["seed_hex"], _HEX64, "record_invalid")
            if not isinstance(record["schedule"], list):
                raise _fail("record_invalid")
        elif record_type == "day_started":
            _int(record["day"], 1, DAY_COUNT, "record_invalid")
            _int(record["round"], 1, ROUNDS, "record_invalid")
            if record["method"] not in METHODS or record["setup_confirmed"] is not True:
                raise _fail("record_invalid")
        elif record_type in ("block_before", "block_after"):
            _text(record["group_id"], _GROUP_ID, "record_invalid")
            if not isinstance(record["total"], str):
                raise _fail("record_invalid")
        elif record_type == "task_answer":
            _text(record["task_id"], _TASK_ID, "record_invalid")
            _text(record["answer_sha256"], _HEX64, "record_invalid")
            if not isinstance(record["correct"], bool):
                raise _fail("record_invalid")
        elif record_type == "block_voided":
            _text(record["group_id"], _GROUP_ID, "record_invalid")
            if record["reason"] not in VOID_REASONS:
                raise _fail("record_invalid")
        elif record_type == "day_finished":
            if record["status"] not in ("complete", "abandoned"):
                raise _fail("record_invalid")
        elif record_type == "day_reconciled":
            if not isinstance(record["desktop_total"], str):
                raise _fail("record_invalid")
    except KeyError as error:  # pragma: no cover - guarded by the required-field check
        raise _fail("record_invalid") from error


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _offset_text(moment: datetime) -> str:
    offset = moment.utcoffset()
    if offset is None:
        raise _fail("clock_invalid")
    minutes = int(offset.total_seconds() // 60)
    sign = "+" if minutes >= 0 else "-"
    minutes = abs(minutes)
    return f"{sign}{minutes // 60:02d}:{minutes % 60:02d}"


class Ledger:
    """Append-only, hash-chained JSON Lines file.

    Each line is ``{"seq", "prev_sha256", "type", "utc", "local_date", "utc_offset", ...}``.
    ``prev_sha256`` is the sha256 of the previous line's bytes without its newline,
    and it is 64 zeros for the first line. ``load()`` verifies the whole chain. Any
    edit, deletion, reordering, unknown type or torn final line raises
    ``SelfTestError("ledger_tampered")``.
    """

    def __init__(self, path: str | os.PathLike[str], *, clock: Any = None) -> None:
        self.path = Path(path)
        self._clock = clock if clock is not None else _utc_now

    def build(self, record_type: str, **fields: Any) -> dict[str, Any]:
        """A record with utc, local_date and utc_offset filled from the clock.

        Explicit ``local_date`` or ``utc_offset`` fields override the clock values.
        ``day_reconciled`` uses ``local_date`` for the measured day.
        """
        if record_type not in _RECORD_FIELDS:
            raise _fail("record_type_unknown")
        moment = self._clock()
        if not isinstance(moment, datetime) or moment.tzinfo is None:
            raise _fail("clock_invalid")
        local = moment.astimezone()
        record: dict[str, Any] = {
            "type": record_type,
            "utc": moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "local_date": local.strftime("%Y-%m-%d"),
            "utc_offset": _offset_text(local),
        }
        record.update(fields)
        _check_record_shape(record)
        return record

    def append(self, record_type: str, **fields: Any) -> dict[str, Any]:
        """Build and append a record. Use ``append_record`` after a state check instead."""
        return self.append_record(self.build(record_type, **fields))

    def append_record(self, record: dict[str, Any]) -> dict[str, Any]:
        """Verify the existing chain, then append one record with flush and fsync."""
        if not isinstance(record, dict):
            raise _fail("record_invalid")
        body = {key: value for key, value in record.items() if key not in _CHAIN_FIELDS}
        _check_record_shape(body)
        entries = self._read()
        previous = hashlib.sha256(entries[-1][1]).hexdigest() if entries else _ZERO_HASH
        stored = {"seq": len(entries) + 1, "prev_sha256": previous, **body}
        line = json.dumps(
            stored, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("ab") as handle:
            handle.write(line + b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        return stored

    def load(self) -> list[dict[str, Any]]:
        """All records in order, after verifying the chain."""
        return [record for record, _ in self._read()]

    def _read(self) -> list[tuple[dict[str, Any], bytes]]:
        if not self.path.exists():
            return []
        data = self.path.read_bytes()
        if not data:
            return []
        if not data.endswith(b"\n"):
            raise _fail("ledger_tampered")
        entries: list[tuple[dict[str, Any], bytes]] = []
        previous_hash = _ZERO_HASH
        previous_seq = 0
        for line in data[:-1].split(b"\n"):
            try:
                record = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, ValueError) as error:
                raise _fail("ledger_tampered") from error
            if not isinstance(record, dict):
                raise _fail("ledger_tampered")
            seq = record.get("seq")
            if isinstance(seq, bool) or not isinstance(seq, int) or seq != previous_seq + 1:
                raise _fail("ledger_tampered")
            if record.get("prev_sha256") != previous_hash:
                raise _fail("ledger_tampered")
            body = {key: value for key, value in record.items() if key not in _CHAIN_FIELDS}
            try:
                _check_record_shape(body)
            except SelfTestError as error:
                raise _fail("ledger_tampered") from error
            entries.append((record, line))
            previous_hash = hashlib.sha256(line).hexdigest()
            previous_seq = seq
        return entries


def default_state_dir(study_id: str, *, env: Any = None) -> Path:
    """``%LOCALAPPDATA%\\ContextShrink\\self-test\\<study_id>``. Readings never leave it."""
    study = _text(study_id, _STUDY_ID, "study_id_invalid")
    source = os.environ if env is None else env
    base = source.get("LOCALAPPDATA")
    root = Path(base) if base else Path.home() / "AppData" / "Local"
    return root / "ContextShrink" / "self-test" / study


# --------------------------------------------------------------------------- state


class StudyState:
    """The study replayed from its ledger records.

    Changes go through ``with_record``, which returns a new state or raises
    ``SelfTestError``. Use ``next_action()`` to decide what the wizard asks next.

    Rules enforced:

    - days run in schedule order, one per local date, each finished on its start date;
    - blocks run in schedule order; each block's tasks are answered in order before
      the block closes; readings are two-decimal and non-negative; ``after >= before``
      and a delta of at most $20.00;
    - a block's ``before`` must equal the previous reading. A higher value flags
      ``spend_between_blocks`` and a lower one flags ``meter_went_backwards``. After a
      voided block, the redo's ``before`` only has to be at least the voided block's
      last reading.

    Any flag makes the day unclean: it is excluded from claims and must be redone on
    another local date.
    """

    def __init__(self, pack: dict[str, Any]) -> None:
        self.pack = pack
        self.pack_sha256 = pack_digest(pack)
        self.started = False
        self.seed_hex: str | None = None
        self.schedule: list[dict[str, Any]] = []
        self.attempts: list[dict[str, Any]] = []
        self.desktop_totals: dict[str, Decimal] = {}

    @classmethod
    def from_ledger(cls, pack: Any, records: list[dict[str, Any]]) -> StudyState:
        """Replay verified ledger records. Any rule violation is ``ledger_tampered``."""
        state = cls(validate_pack(pack))
        for record in records:
            try:
                state = state.with_record(record)
            except SelfTestError as error:
                raise _fail("ledger_tampered") from error
        return state

    def with_record(self, record: dict[str, Any]) -> StudyState:
        """Return the state after ``record``. Raises ``SelfTestError`` if a rule breaks."""
        _check_record_shape(record)
        new = copy.deepcopy(self)
        new._apply(record)
        return new

    # ---- status

    @property
    def current_day(self) -> int | None:
        """The day to run (1 to 8), or None when every day is complete."""
        if not self.started:
            return None
        open_attempt = self._open_attempt()
        if open_attempt is not None:
            return open_attempt["day"]
        for day in range(1, DAY_COUNT + 1):
            if self.clean_attempt(day) is None:
                return day
        return None

    @property
    def current_block(self) -> str | None:
        """Group id of the block in progress, if any."""
        attempt = self._open_attempt()
        block = self._open_block(attempt) if attempt is not None else None
        return block["group_id"] if block is not None else None

    @property
    def next_task(self) -> str | None:
        """The next task to ask inside the block in progress."""
        attempt = self._open_attempt()
        block = self._open_block(attempt) if attempt is not None else None
        if block is None:
            return None
        pending = self._pending_tasks(attempt, block)
        return pending[0] if pending else None

    @property
    def completed_days(self) -> list[int]:
        """Days with a clean, finished attempt (no flags)."""
        return [day for day in range(1, DAY_COUNT + 1) if self.clean_attempt(day) is not None]

    @property
    def study_complete(self) -> bool:
        return len(self.completed_days) == DAY_COUNT

    @property
    def voided_blocks(self) -> list[dict[str, Any]]:
        return [
            {"day": attempt["day"], "group_id": block["group_id"], "reason": block["void_reason"]}
            for attempt in self.attempts
            for block in attempt["blocks"]
            if block["voided"]
        ]

    @property
    def flags(self) -> list[dict[str, Any]]:
        return [
            {"day": attempt["day"], "local_date": attempt["local_date"], "reason": reason}
            for attempt in self.attempts
            for reason in attempt["flags"]
        ]

    @property
    def pending_reconciliations(self) -> list[str]:
        """Local dates of clean days whose desktop total is missing or does not match."""
        return [
            attempt["local_date"]
            for attempt in self.attempts
            if attempt["status"] == "complete" and self.reconciliation(attempt) is not True
        ]

    def reconciliation_tolerance(self) -> Decimal:
        return RECONCILE_CENT * (len(self.pack["groups"]) + 1)

    def clean_attempt(self, day: int) -> dict[str, Any] | None:
        for attempt in self.attempts:
            if attempt["day"] == day and attempt["status"] == "complete":
                return attempt
        return None

    def attempt_for_date(self, local_date: str) -> dict[str, Any] | None:
        for attempt in self.attempts:
            if attempt["local_date"] == local_date:
                return attempt
        return None

    def span(self, attempt: dict[str, Any]) -> Decimal | None:
        """Meter change across the whole day: last reading minus first reading.

        Includes voided blocks and any flagged gaps. On a clean day it equals the sum
        of the group deltas.
        """
        blocks = attempt["blocks"]
        if not blocks or blocks[-1]["after"] is None:
            return None
        return blocks[-1]["after"] - blocks[0]["before"]

    def reconciliation(self, attempt: dict[str, Any]) -> bool | None:
        """None when no desktop total is recorded, else whether it matches the span."""
        desktop = self.desktop_totals.get(attempt["local_date"])
        if desktop is None:
            return None
        measured = self.span(attempt)
        if measured is None:
            return False
        return abs(desktop - measured) <= self.reconciliation_tolerance()

    def valid_block(self, attempt: dict[str, Any], group_id: str) -> dict[str, Any] | None:
        """The finished, non-voided block for a group on a day, if there is one."""
        for block in attempt["blocks"]:
            if block["group_id"] == group_id and not block["voided"] and block["after"] is not None:
                return block
        return None

    def next_action(self) -> dict[str, Any]:
        """What the wizard should ask for next."""
        if not self.started:
            return {"action": "start_study"}
        attempt = self._open_attempt()
        if attempt is None:
            day = self.current_day
            if day is None:
                return {"action": "done"}
            slot = self.schedule[day - 1]
            return {
                "action": "start_day",
                "day": day,
                "round": slot["round"],
                "method": slot["method"],
            }
        day = attempt["day"]
        block = self._open_block(attempt)
        if block is not None:
            pending = self._pending_tasks(attempt, block)
            if pending:
                return {
                    "action": "ask_task",
                    "day": day,
                    "group_id": block["group_id"],
                    "task_id": pending[0],
                }
            return {"action": "read_after", "day": day, "group_id": block["group_id"]}
        group_id = self._next_group(attempt)
        if group_id is None:
            return {"action": "finish_day", "day": day}
        return {"action": "read_before", "day": day, "group_id": group_id}

    # ---- rule application

    def _apply(self, record: dict[str, Any]) -> None:
        kind = record["type"]
        if kind == "study_started":
            self._start(record)
            return
        if not self.started:
            raise _fail("study_not_started")
        if kind == "day_started":
            self._day_started(record)
        elif kind == "block_before":
            self._block_before(record)
        elif kind == "task_answer":
            self._task_answer(record)
        elif kind == "block_after":
            self._block_after(record)
        elif kind == "block_voided":
            self._block_voided(record)
        elif kind == "day_finished":
            self._day_finished(record)
        else:
            self._day_reconciled(record)

    def _start(self, record: dict[str, Any]) -> None:
        if self.started:
            raise _fail("study_already_started")
        if record["pack_digest"] != self.pack_sha256:
            raise _fail("pack_mismatch")
        schedule = build_schedule(self.pack, record["seed_hex"])
        if record["schedule"] != schedule:
            raise _fail("schedule_mismatch")
        self.started = True
        self.seed_hex = record["seed_hex"]
        self.schedule = schedule

    def _day_started(self, record: dict[str, Any]) -> None:
        if self._open_attempt() is not None:
            raise _fail("day_already_open")
        current = self.current_day
        if current is None:
            raise _fail("study_complete")
        if record["day"] != current:
            raise _fail("day_out_of_order")
        slot = self.schedule[current - 1]
        if (record["round"], record["method"]) != (slot["round"], slot["method"]):
            raise _fail("day_mismatch")
        if self.attempt_for_date(record["local_date"]) is not None:
            raise _fail("one_day_per_local_date")
        self.attempts.append(
            {
                "day": current,
                "round": slot["round"],
                "method": slot["method"],
                "local_date": record["local_date"],
                "utc_offset": record["utc_offset"],
                "status": "open",
                "flags": [],
                "blocks": [],
            }
        )

    def _block_before(self, record: dict[str, Any]) -> None:
        attempt = self._require_open()
        if self._open_block(attempt) is not None:
            raise _fail("block_open")
        if record["group_id"] != self._next_group(attempt):
            raise _fail("block_not_next")
        total = _reading(record["total"])
        if attempt["blocks"]:
            last = attempt["blocks"][-1]
            anchor = last["after"] if last["after"] is not None else last["before"]
            if last["voided"]:
                if total < anchor:
                    _add_flag(attempt, FLAG_BACKWARDS)
            elif total > anchor:
                _add_flag(attempt, FLAG_SPEND_BETWEEN)
            elif total < anchor:
                _add_flag(attempt, FLAG_BACKWARDS)
        attempt["blocks"].append(
            {
                "group_id": record["group_id"],
                "before": total,
                "after": None,
                "voided": False,
                "void_reason": None,
                "answers": [],
            }
        )

    def _task_answer(self, record: dict[str, Any]) -> None:
        attempt = self._require_open()
        block = self._open_block(attempt)
        if block is None:
            raise _fail("block_not_open")
        pending = self._pending_tasks(attempt, block)
        if not pending or record["task_id"] != pending[0]:
            raise _fail("task_out_of_order")
        block["answers"].append(
            {
                "task_id": record["task_id"],
                "correct": record["correct"],
                "answer_sha256": record["answer_sha256"],
            }
        )

    def _block_after(self, record: dict[str, Any]) -> None:
        attempt = self._require_open()
        block = self._open_block(attempt)
        if block is None or record["group_id"] != block["group_id"]:
            raise _fail("block_not_open")
        if len(block["answers"]) != len(self._group_tasks(attempt, block["group_id"])):
            raise _fail("block_incomplete")
        total = _reading(record["total"])
        if total < block["before"]:
            raise _fail("reading_decreased")
        if total - block["before"] > MAX_BLOCK_DELTA:
            raise _fail("reading_delta_too_large")
        block["after"] = total

    def _block_voided(self, record: dict[str, Any]) -> None:
        attempt = self._require_open()
        if not attempt["blocks"]:
            raise _fail("void_target_invalid")
        block = attempt["blocks"][-1]
        if block["voided"] or block["group_id"] != record["group_id"]:
            raise _fail("void_target_invalid")
        block["voided"] = True
        block["void_reason"] = record["reason"]
        _add_flag(attempt, FLAG_BLOCK_VOIDED)

    def _day_finished(self, record: dict[str, Any]) -> None:
        attempt = self._require_open()
        if record["status"] == "abandoned":
            attempt["status"] = "abandoned"
            return
        if self._open_block(attempt) is not None:
            raise _fail("block_open")
        if self._next_group(attempt) is not None:
            raise _fail("day_incomplete")
        if record["local_date"] != attempt["local_date"]:
            _add_flag(attempt, FLAG_OTHER_DATE)
        attempt["status"] = "flagged" if attempt["flags"] else "complete"

    def _day_reconciled(self, record: dict[str, Any]) -> None:
        attempt = self.attempt_for_date(record["local_date"])
        if attempt is None:
            raise _fail("day_not_found")
        if attempt["status"] not in ("complete", "flagged"):
            raise _fail("day_not_reconcilable")
        self.desktop_totals[record["local_date"]] = _reading(record["desktop_total"])

    # ---- helpers

    def _open_attempt(self) -> dict[str, Any] | None:
        for attempt in self.attempts:
            if attempt["status"] == "open":
                return attempt
        return None

    def _require_open(self) -> dict[str, Any]:
        attempt = self._open_attempt()
        if attempt is None:
            raise _fail("no_open_day")
        return attempt

    @staticmethod
    def _open_block(attempt: dict[str, Any]) -> dict[str, Any] | None:
        if attempt["blocks"]:
            last = attempt["blocks"][-1]
            if last["after"] is None and not last["voided"]:
                return last
        return None

    def _next_group(self, attempt: dict[str, Any]) -> str | None:
        for group in self.schedule[attempt["day"] - 1]["groups"]:
            if self.valid_block(attempt, group["group_id"]) is None:
                return group["group_id"]
        return None

    def _group_tasks(self, attempt: dict[str, Any], group_id: str) -> list[str]:
        for group in self.schedule[attempt["day"] - 1]["groups"]:
            if group["group_id"] == group_id:
                return list(group["tasks"])
        raise _fail("group_unknown")

    def _pending_tasks(self, attempt: dict[str, Any], block: dict[str, Any]) -> list[str]:
        answered = {answer["task_id"] for answer in block["answers"]}
        return [
            task for task in self._group_tasks(attempt, block["group_id"]) if task not in answered
        ]


def _add_flag(attempt: dict[str, Any], reason: str) -> None:
    if reason not in attempt["flags"]:
        attempt["flags"].append(reason)


# ------------------------------------------------------------------- reconciliation


def reconcile(state: StudyState, local_date: str, desktop_total: str) -> dict[str, Any]:
    """Compare one finished day's meter change with Claude Desktop's line for that date.

    Returns a result without changing the state. Record a ``day_reconciled`` entry to
    keep it. The tolerance is $0.01 times (number of groups + 1).
    """
    if _valid_date_text(local_date) is None:
        raise _fail("record_invalid")
    desktop = _reading(desktop_total)
    attempt = state.attempt_for_date(local_date)
    if attempt is None:
        raise _fail("day_not_found")
    if attempt["status"] != "complete":
        raise _fail("day_not_reconcilable")
    measured = state.span(attempt)
    if measured is None:  # pragma: no cover - a complete day always has a closing reading
        raise _fail("day_not_reconcilable")
    difference = desktop - measured
    tolerance = state.reconciliation_tolerance()
    return {
        "local_date": local_date,
        "desktop_total": _money(desktop),
        "measured_total": _money(measured),
        "difference": _money(difference),
        "tolerance": _money(tolerance),
        "passed": abs(difference) <= tolerance,
    }


# ------------------------------------------------------------------------- analysis


def _interval_out(interval: Any) -> dict[str, Any]:
    return {
        "method": interval.method,
        "estimate": None if interval.estimate is None else _money(interval.estimate),
        "lower": None if interval.lower is None else _money(interval.lower, ROUND_FLOOR),
        "upper": None if interval.upper is None else _money(interval.upper, ROUND_CEILING),
        "confidence": str(interval.confidence),
        "units": interval.n_units,
        "clusters": interval.n_clusters,
        "resamples": interval.resamples,
        "seed": interval.seed,
    }


def analyze(pack: Any, state: StudyState) -> dict[str, Any]:
    """Build the study report. All money is a 2-decimal string and no floats are used.

    Only clean, reconciled days are included. A comparison needs every one of the eight
    days to be included (provenance) and must clear the pack's minimums. The primary
    comparison (B vs C_normal) is also withheld when B has more than
    ``quality_tolerance`` fewer correct answers than C_normal.
    """
    checked = validate_pack(pack)
    if state.pack_sha256 != pack_digest(checked):
        raise _fail("pack_mismatch")
    if not state.started:
        raise _fail("study_not_started")

    included: dict[int, dict[str, Any]] = {}
    for day in range(1, DAY_COUNT + 1):
        attempt = state.clean_attempt(day)
        if attempt is not None and state.reconciliation(attempt) is True:
            included[day] = attempt
    day_of = {
        (slot["round"], slot["method"]): index + 1 for index, slot in enumerate(state.schedule)
    }
    group_ids = [group["group_id"] for group in checked["groups"]]
    tasks = [task["task_id"] for task in checked["tasks"]]

    def cost(day: int, group_id: str) -> Decimal:
        block = state.valid_block(included[day], group_id)
        assert block is not None and block["after"] is not None
        return block["after"] - block["before"]

    spend: dict[str, Decimal] = {}
    correct: dict[str, int] = {}
    attempted: dict[str, int] = {}
    days_by_method: dict[str, list[int]] = {}
    per_task: dict[str, dict[str, list[int]]] = {task: {} for task in tasks}
    for method in METHODS:
        days = sorted(day for day in included if state.schedule[day - 1]["method"] == method)
        days_by_method[method] = days
        spend[method] = sum((state.span(included[day]) or Decimal(0) for day in days), Decimal(0))
        correct[method] = 0
        attempted[method] = 0
        for task in tasks:
            per_task[task][method] = [0, 0]
        for day in days:
            for group_id in group_ids:
                block = state.valid_block(included[day], group_id)
                assert block is not None
                for answer in block["answers"]:
                    attempted[method] += 1
                    per_task[answer["task_id"]][method][1] += 1
                    if answer["correct"]:
                        correct[method] += 1
                        per_task[answer["task_id"]][method][0] += 1

    methods_out = {}
    for method in METHODS:
        per_correct = None
        if correct[method]:
            ratio = spend[method] / Decimal(correct[method])
            per_correct = _money(ratio)
        methods_out[method] = {
            "days": len(days_by_method[method]),
            "spend": _money(spend[method]),
            "tasks_attempted": attempted[method],
            "correct": correct[method],
            "cost_per_correct": per_correct,
        }

    provenance = len(included) == DAY_COUNT
    tolerance = checked["quality_tolerance"]
    regression = correct["B"] < correct["C_normal"] - tolerance

    comparisons = []
    for key, candidate, reference, primary in COMPARISONS:
        units: list[PairedUnit] = []
        for round_no in range(1, 3):
            cand_day = day_of[(round_no, candidate)]
            ref_day = day_of[(round_no, reference)]
            if cand_day not in included or ref_day not in included:
                continue
            for group_id in group_ids:
                units.append(
                    PairedUnit(
                        cluster_id=group_id,
                        difference=cost(cand_day, group_id) - cost(ref_day, group_id),
                    )
                )
        interval = paired_difference_interval(
            units, confidence=Decimal("0.95"), resamples=10000, seed=REPORT_SEED
        )
        decision = savings_decision(
            interval,
            min_units=checked["minimums"]["paired_units"],
            min_clusters=checked["minimums"]["groups"],
            evidence_class=SELF_METERED,
            provenance_verified=provenance,
            eligible_classes=frozenset({SELF_METERED}),
        )
        if primary and regression:
            if REASON_QUALITY not in decision["reasons"]:
                decision["reasons"].append(REASON_QUALITY)
            if decision["claim"] == "savings_supported":
                decision["claim"] = "none"
        if not provenance and REASON_PROVENANCE not in decision["reasons"]:
            decision["reasons"].append(REASON_PROVENANCE)
        comparisons.append(
            {
                "key": key,
                "candidate": candidate,
                "reference": reference,
                "primary": primary,
                "interval": _interval_out(interval),
                "decision": {
                    "claim": decision["claim"],
                    "reasons": decision["reasons"],
                    "rule": SAVINGS_RULE,
                },
            }
        )

    days_out = []
    for day in range(1, DAY_COUNT + 1):
        slot = state.schedule[day - 1]
        attempts = [attempt for attempt in state.attempts if attempt["day"] == day]
        reasons: list[str] = []
        for attempt in attempts:
            for reason in attempt["flags"]:
                if reason not in reasons:
                    reasons.append(reason)
        latest = attempts[-1] if attempts else None
        if latest is None:
            status = "not_started"
        else:
            status = latest["status"]
            if latest["status"] == "abandoned":
                reasons.append("abandoned")
            if latest["status"] == "complete":
                reconciled = state.reconciliation(latest)
                if reconciled is not True:
                    reasons.append(
                        "reconciliation_failed" if reconciled is False else "reconciliation_pending"
                    )
        days_out.append(
            {
                "day": day,
                "round": slot["round"],
                "method": slot["method"],
                "status": status,
                "attempts": len(attempts),
                "included": day in included,
                "reasons": reasons,
            }
        )

    quality_correct = {method: correct[method] for method in METHODS}
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": REPORT_KIND,
        "study_id": checked["study_id"],
        "model_label": checked["model_label"],
        "pack_sha256": state.pack_sha256,
        "seed_commitment": hashlib.sha256((state.seed_hex or "").encode("ascii")).hexdigest(),
        "scope": dict(SCOPE),
        "progress": {
            "days_total": DAY_COUNT,
            "days_complete": len(state.completed_days),
            "days_included": len(included),
            "current_day": state.current_day,
            "study_complete": state.study_complete,
        },
        "methods": methods_out,
        "per_task": {
            task: {
                method: {
                    "correct": per_task[task][method][0],
                    "attempted": per_task[task][method][1],
                }
                for method in METHODS
            }
            for task in tasks
        },
        "quality": {
            "tolerance": tolerance,
            "correct": quality_correct,
            "regression": regression,
        },
        "comparisons": comparisons,
        "days": days_out,
        "flagged_days": [day for day in days_out if day["reasons"]],
        "pending_reconciliations": state.pending_reconciliations,
    }


__all__ = [
    "ANSWER_KINDS",
    "COMPARISONS",
    "DAY_COUNT",
    "LEDGER_TYPES",
    "METHODS",
    "REPORT_SEED",
    "SCOPE",
    "SelfTestError",
    "StudyState",
    "Ledger",
    "analyze",
    "answer_digest",
    "build_schedule",
    "check_answer",
    "default_state_dir",
    "method_setup",
    "new_seed_hex",
    "pack_digest",
    "reconcile",
    "render_prompt",
    "study_started_fields",
    "validate_pack",
]
