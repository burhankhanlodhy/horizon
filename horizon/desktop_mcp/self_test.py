"""Single-account self test: pack, schedule, prompts, answer checks, ledger, state, analysis.

This module holds the logic only. It never prompts, never reads Claude Desktop,
and never stores answer text, file contents or paths. The terminal wizard
(``self_test_cli``) calls the functions below.

The study runs as eight phases. Each phase is one method over one half of the
groups, so every task runs once under every method (48 chats for the 12-group
template). Each group ("block") has a reading before and after it. Each phase
starts with its own reading, taken before the setup check, so every setup chat
falls between two readings.

Every write follows the same pattern: build a record, check it with
``state.with_record(record)``, then ``ledger.append_record(record)``.
"""

from __future__ import annotations

import copy
import hashlib
import itertools
import json
import math
import os
import random
import re
import secrets
from datetime import datetime, timedelta, timezone
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
HALVES: tuple[str, ...] = ("H1", "H2")
PHASE_COUNT = 2 * len(METHODS)  # upper bound for any study: each half runs every method once
# A pack may name a subset of METHODS; these two are always needed (B is the primary arm).
REQUIRED_METHODS: tuple[str, ...] = ("B", "C_normal")
PILOT_METHODS: dict[str, str] = {"A": "exact_only", "B": "normal"}
REVIEW_DELAY = timedelta(hours=4)

# Fixed seed for the bootstrap so that a given ledger always yields the same report.
REPORT_SEED = 20261015
RECONCILE_CENT = Decimal("0.01")
MAX_BLOCK_DELTA = Decimal("20.00")
MAX_READING = Decimal("100000.00")
MAX_QUALITY_TOLERANCE = 4
SOURCE_FORMATS: tuple[str, ...] = ("txt", "md", "json", "csv", "pdf", "docx")
ANSWER_KINDS: tuple[str, ...] = ("number", "text_contains", "date", "choice")
VOID_REASONS: tuple[str, ...] = ("interrupted", "chat_not_fresh", "setup_wrong", "other")

# Warnings keep the group in the results. Only voids and date checks exclude groups.
FLAG_SPEND_BETWEEN = "spend_between_blocks"
REASON_QUALITY = "quality_regression"
REASON_PROVENANCE = "provenance_unverified"
REASON_INCOMPLETE = "study_incomplete"

# Why a group run is not in the results.
OUT_INCLUDED = "included"
OUT_VOIDED = "voided"
OUT_IN_PROGRESS = "in_progress"
OUT_SPANS_MIDNIGHT = "spans_midnight"
OUT_DATE_PENDING = "date_pending"
OUT_DATE_FAILED = "date_failed"

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
_OPTIONAL_PACK_KEYS = frozenset({"methods"})
_ZERO_HASH = "0" * 64
_CHAIN_FIELDS = frozenset({"seq", "prev_sha256"})
_COMMON_FIELDS = frozenset({"type", "utc", "local_date", "utc_offset"})
_RECORD_FIELDS: dict[str, frozenset[str]] = {
    "study_started": frozenset({"pack_digest", "seed_hex", "schedule"}),
    "phase_started": frozenset({"phase", "half", "method", "setup_confirmed", "total"}),
    "block_before": frozenset({"group_id", "total"}),
    "task_answer": frozenset({"task_id", "correct", "answer_sha256"}),
    "block_after": frozenset({"group_id", "total"}),
    "block_voided": frozenset({"group_id", "reason"}),
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


def parse_utc(text: str) -> datetime:
    """Parse a ledger timestamp (``YYYY-MM-DDTHH:MM:SSZ``) into an aware datetime."""
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


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


def _methods(value: Any) -> list[str]:
    """Distinct method names, a subset of METHODS that includes REQUIRED_METHODS.

    Returned in METHODS order, so the same set always gives the same digest.
    """
    items = _list(value, 1, len(METHODS), "methods_invalid")
    if not all(isinstance(item, str) for item in items):
        raise _fail("methods_invalid")
    if any(item not in METHODS for item in items):
        raise _fail("method_unknown")
    if len(set(items)) != len(items):
        raise _fail("method_duplicate")
    if not set(REQUIRED_METHODS) <= set(items):
        raise _fail("methods_missing_core")
    return [method for method in METHODS if method in items]


def validate_pack(value: Any) -> dict[str, Any]:
    """Check a self-test pack and return a normalised copy.

    Raises ``SelfTestError`` with a code such as ``group_coverage_invalid``.
    A pack without ``methods`` runs every method in METHODS; the normalised copy then
    has no ``methods`` key, so existing packs keep their digest.
    """
    if (
        not isinstance(value, dict)
        or not _PACK_KEYS <= set(value) <= _PACK_KEYS | _OPTIONAL_PACK_KEYS
    ):
        raise _fail("pack_invalid")
    raw = value
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
    # Each group is one unit in the analysis, so the unit minimum cannot exceed the groups.
    if min_groups > len(groups) or paired_units > len(groups):
        raise _fail("minimums_unreachable")

    normalised: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "kind": PACK_KIND,
        "study_id": _text(raw["study_id"], _STUDY_ID, "study_id_invalid"),
        "model_label": _printable(raw["model_label"], 80, "model_label_invalid"),
        "minimums": {"paired_units": paired_units, "groups": min_groups},
        "quality_tolerance": tolerance,
        "groups": groups,
        "tasks": tasks,
    }
    if "methods" in raw:
        normalised["methods"] = _methods(raw["methods"])
    return normalised


def study_methods(pack: Any) -> tuple[str, ...]:
    """The methods this study runs: the pack's ``methods``, or all of METHODS."""
    return tuple(validate_pack(pack).get("methods", METHODS))


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


def _method_orders(methods: tuple[str, ...]) -> list[tuple[str, ...]]:
    """Every order of the study's methods; with A and B both present, they must be adjacent.

    For all four methods this is the 12 of the 24 permutations with A and B adjacent.
    """
    orders = list(itertools.permutations(methods))
    if "A" in methods and "B" in methods:
        orders = [order for order in orders if abs(order.index("A") - order.index("B")) == 1]
    return orders


def build_schedule(pack: Any, seed_hex: str) -> list[dict[str, Any]]:
    """Phases: H1 runs the method order O, H2 runs reverse(O).

    With all four methods there are eight phases. The groups are shuffled by seed and
    split into H1 (the first ``ceil(n/2)``) and H2. Each phase is
    ``{"phase", "half", "method", "groups": [{"group_id", "tasks"}]}`` and covers every
    group of its half, in its own seeded order. The first half of the phases is H1 and
    the rest is H2.
    """
    checked = validate_pack(pack)
    _seed(seed_hex)
    rng = random.Random(f"self-test-schedule:{seed_hex}")
    groups = list(checked["groups"])
    rng.shuffle(groups)
    split = math.ceil(len(groups) / 2)
    halves = {"H1": groups[:split], "H2": groups[split:]}
    order = rng.choice(_method_orders(study_methods(checked)))
    methods = {"H1": order, "H2": tuple(reversed(order))}

    phases: list[dict[str, Any]] = []
    for half in HALVES:
        for method in methods[half]:
            number = len(phases) + 1
            phase_rng = random.Random(f"self-test-phase:{seed_hex}:{number}")
            members = list(halves[half])
            phase_rng.shuffle(members)
            phase_groups = []
            for group in members:
                tasks = list(group["tasks"])
                phase_rng.shuffle(tasks)
                phase_groups.append({"group_id": group["group_id"], "tasks": tasks})
            phases.append({"phase": number, "half": half, "method": method, "groups": phase_groups})
    return phases


def pilot_changed(schedule: list[dict[str, Any]], phase: int) -> bool:
    """Whether the Pilot method must change before this phase, so Claude Desktop restarts.

    Only A and B use the Pilot method. The first such phase always needs the setting.
    """
    method = schedule[phase - 1]["method"]
    if method not in PILOT_METHODS:
        return False
    for slot in reversed(schedule[: phase - 1]):
        if slot["method"] in PILOT_METHODS:
            return slot["method"] != method
    return True


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


def method_setup(method: str, pack: Any, *, pilot_changed: bool = True) -> list[str]:
    """Plain-language setup steps for a phase. Restarts are named only when needed."""
    checked = validate_pack(pack)
    if method not in METHODS:
        raise _fail("method_invalid")
    steps = [
        f"Set the model to {checked['model_label']}. Keep this setting for every phase.",
        "Start a fresh chat for every task.",
        "Use no other Claude product while the test runs: no other Claude Desktop chats, Claude Code or Codex.",
    ]
    if method in PILOT_METHODS:
        value = PILOT_METHODS[method]
        if pilot_changed:
            steps.append(
                f"In the ContextShrink extension, set Pilot method to {value}. "
                "Then fully quit Claude Desktop from the tray and reopen it."
            )
        else:
            steps.append(
                f"Pilot method is still {value} from the last phase: no change and no restart."
            )
        steps.append("Turn the connector ON.")
        expected = "raw" if method == "A" else "optimized"
        steps.append(
            f'Check it in a new chat: ask Claude "show ContextShrink stats"; evaluation_arm must say {expected}.'
        )
    else:
        steps.append("Turn the ContextShrink connector OFF.")
        steps.append(
            "For each task, attach only that task's source files with the paperclip. The wizard shows their names."
        )
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
    spaces. Text matching is case-folded and whitespace-normalised, and whole-word.
    Dates accept ISO or English month-name forms only.
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
        elif record_type == "phase_started":
            _int(record["phase"], 1, PHASE_COUNT, "record_invalid")
            if record["half"] not in HALVES or record["method"] not in METHODS:
                raise _fail("record_invalid")
            if record["setup_confirmed"] is not True or not isinstance(record["total"], str):
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


# ----------------------------------------------------------------------------- state


class StudyState:
    """The study replayed from its ledger records.

    Changes go through ``with_record``, which returns a new state or raises
    ``SelfTestError``. ``next_action()`` says what the wizard asks next.

    Rules enforced:

    - phases run in schedule order; a phase starts only after the previous one is
      complete, with a reading taken before its setup check;
    - groups run in schedule order inside a phase; a group's tasks are answered in
      order before its "after" reading; readings are two-decimal, never fall below the
      previous anchor, and a group adds at most $20.00;
    - spend between two groups of the same phase is a warning, not an exclusion. A
      phase boundary is expected to carry spend (the setup check);
    - a voided group is excluded and redone at once; nothing else is excluded;
    - each local date is checked against Claude Desktop's daily line once it is at
      least 4 hours past its last reading. A date that fails excludes its groups, and
      a date stays final once checked (no readings may be added to it afterwards).
    """

    def __init__(self, pack: dict[str, Any]) -> None:
        self.pack = pack
        self.pack_sha256 = pack_digest(pack)
        self.methods = study_methods(pack)
        self.phase_count = 2 * len(self.methods)
        self.started = False
        self.seed_hex: str | None = None
        self.schedule: list[dict[str, Any]] = []
        self.phases: list[dict[str, Any]] = []
        self.readings: list[dict[str, Any]] = []
        self.reconciled: dict[str, dict[str, Any]] = {}
        self.warnings: list[dict[str, Any]] = []

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
    def current_phase(self) -> int | None:
        if not self.started:
            return None
        if self.phases and not self._phase_complete(self.phases[-1]):
            return self.phases[-1]["phase"]
        if len(self.phases) < self.phase_count:
            return len(self.phases) + 1
        return None

    @property
    def completed_phases(self) -> list[int]:
        return [phase["phase"] for phase in self.phases if self._phase_complete(phase)]

    @property
    def study_complete(self) -> bool:
        return len(self.completed_phases) == self.phase_count

    @property
    def group_runs_total(self) -> int:
        return sum(len(slot["groups"]) for slot in self.schedule)

    @property
    def chats_total(self) -> int:
        return sum(len(group["tasks"]) for slot in self.schedule for group in slot["groups"])

    def recorded_group_runs(self) -> int:
        return sum(
            1
            for phase in self.phases
            for block in phase["blocks"]
            if not block["voided"] and block["after"] is not None
        )

    @property
    def current_block(self) -> str | None:
        block = self._open_block(self.phases[-1]) if self.phases else None
        return block["group_id"] if block is not None else None

    @property
    def next_task(self) -> str | None:
        if not self.phases:
            return None
        phase = self.phases[-1]
        block = self._open_block(phase)
        if block is None:
            return None
        pending = self._pending_tasks(phase, block)
        return pending[0] if pending else None

    @property
    def voided_blocks(self) -> list[dict[str, Any]]:
        return [
            {"phase": phase["phase"], "group_id": block["group_id"], "reason": block["void_reason"]}
            for phase in self.phases
            for block in phase["blocks"]
            if block["voided"]
        ]

    def reading_floor(self) -> Decimal | None:
        """The lowest total the next reading may show (None before the first phase)."""
        if not self.phases:
            return None
        return self._anchor(self.phases[-1])

    def has_open_block(self) -> bool:
        return any(self._open_block(phase) is not None for phase in self.phases)

    def dates_with_block_readings(self) -> list[str]:
        return sorted({r["local_date"] for r in self.readings if r["kind"] == "block"})

    def pending_reconciliations(self) -> list[str]:
        return [d for d in self.dates_with_block_readings() if d not in self.reconciled]

    def date_status(self, local_date: str) -> str:
        entry = self.reconciled.get(local_date)
        if entry is None:
            return "pending"
        return "passed" if entry["passed"] else "failed"

    def date_summary(self, local_date: str) -> dict[str, Any] | None:
        """Meter change across a local date (last reading minus first) and its tolerance.

        Readings are phase-start and group readings dated ``local_date``. The tolerance is
        $0.01 times (number of groups with a reading that date + 1). None when the date
        has no group reading.
        """
        readings = [r for r in self.readings if r["local_date"] == local_date]
        if not any(r["kind"] == "block" for r in readings):
            return None
        blocks = self._blocks_on(local_date)
        return {
            "span": readings[-1]["total"] - readings[0]["total"],
            "tolerance": RECONCILE_CENT * (blocks + 1),
            "blocks": blocks,
            "ready_at": readings[-1]["utc"] + REVIEW_DELAY,
        }

    def reconcile_ready_at(self, local_date: str) -> datetime | None:
        """When a date may be checked: 4 hours after its last reading."""
        summary = self.date_summary(local_date)
        return None if summary is None else summary["ready_at"]

    def block_outcome(self, phase: dict[str, Any], block: dict[str, Any]) -> str:
        if block["voided"]:
            return OUT_VOIDED
        if block["after"] is None:
            return OUT_IN_PROGRESS
        if block["before_date"] != block["after_date"]:
            return OUT_SPANS_MIDNIGHT
        status = self.date_status(block["before_date"])
        if status == "pending":
            return OUT_DATE_PENDING
        if status == "failed":
            return OUT_DATE_FAILED
        return OUT_INCLUDED

    def next_action(self) -> dict[str, Any]:
        """What the wizard should ask for next."""
        if not self.started:
            return {"action": "start_study"}
        if self.phases:
            phase = self.phases[-1]
            block = self._open_block(phase)
            if block is not None:
                pending = self._pending_tasks(phase, block)
                if pending:
                    return {
                        "action": "ask_task",
                        "phase": phase["phase"],
                        "group_id": block["group_id"],
                        "task_id": pending[0],
                    }
                return {
                    "action": "read_after",
                    "phase": phase["phase"],
                    "group_id": block["group_id"],
                }
            group_id = self._next_group(phase)
            if group_id is not None:
                return {"action": "read_before", "phase": phase["phase"], "group_id": group_id}
        if len(self.phases) < self.phase_count:
            slot = self.schedule[len(self.phases)]
            return {
                "action": "start_phase",
                "phase": slot["phase"],
                "half": slot["half"],
                "method": slot["method"],
            }
        return {"action": "done"}

    # ---- rule application

    def _apply(self, record: dict[str, Any]) -> None:
        kind = record["type"]
        if kind == "study_started":
            self._start(record)
            return
        if not self.started:
            raise _fail("study_not_started")
        if kind == "phase_started":
            self._phase_started(record)
        elif kind == "block_before":
            self._block_before(record)
        elif kind == "task_answer":
            self._task_answer(record)
        elif kind == "block_after":
            self._block_after(record)
        elif kind == "block_voided":
            self._block_voided(record)
        else:
            self._reconciled_date(record)

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

    def _phase_started(self, record: dict[str, Any]) -> None:
        if self.has_open_block():
            raise _fail("block_open")
        if len(self.phases) >= self.phase_count:
            raise _fail("study_complete")
        if self.phases and not self._phase_complete(self.phases[-1]):
            raise _fail("phase_incomplete")
        number = len(self.phases) + 1
        if record["phase"] != number:
            raise _fail("phase_out_of_order")
        slot = self.schedule[number - 1]
        if (record["half"], record["method"]) != (slot["half"], slot["method"]):
            raise _fail("phase_mismatch")
        total = self._new_reading(record, kind="phase", floor=self.reading_floor())
        self.phases.append(
            {
                "phase": number,
                "half": slot["half"],
                "method": slot["method"],
                "started_utc": record["utc"],
                "start_total": total,
                "blocks": [],
            }
        )

    def _block_before(self, record: dict[str, Any]) -> None:
        phase = self._open_phase()
        if self._open_block(phase) is not None:
            raise _fail("block_open")
        group_id = self._next_group(phase)
        if record["group_id"] != group_id:
            raise _fail("block_not_next")
        anchor = self._anchor(phase)
        previous = phase["blocks"][-1] if phase["blocks"] else None
        total = self._new_reading(record, kind="block", floor=anchor)
        if previous is not None and not previous["voided"] and total > anchor:
            self.warnings.append(
                {
                    "phase": phase["phase"],
                    "group_id": record["group_id"],
                    "local_date": record["local_date"],
                    "reason": FLAG_SPEND_BETWEEN,
                }
            )
        phase["blocks"].append(
            {
                "group_id": record["group_id"],
                "before": total,
                "before_date": record["local_date"],
                "after": None,
                "after_date": None,
                "after_utc": None,
                "voided": False,
                "void_reason": None,
                "answers": [],
            }
        )

    def _task_answer(self, record: dict[str, Any]) -> None:
        phase = self._open_phase()
        block = self._open_block(phase)
        if block is None:
            raise _fail("block_not_open")
        pending = self._pending_tasks(phase, block)
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
        phase = self._open_phase()
        block = self._open_block(phase)
        if block is None or record["group_id"] != block["group_id"]:
            raise _fail("block_not_open")
        if len(block["answers"]) != len(self._group_tasks(phase, block["group_id"])):
            raise _fail("block_incomplete")
        total = self._new_reading(
            record,
            kind="block",
            floor=block["before"],
            ceiling=block["before"] + MAX_BLOCK_DELTA,
        )
        block["after"] = total
        block["after_date"] = record["local_date"]
        block["after_utc"] = record["utc"]

    def _block_voided(self, record: dict[str, Any]) -> None:
        if not self.phases or not self.phases[-1]["blocks"]:
            raise _fail("void_target_invalid")
        block = self.phases[-1]["blocks"][-1]
        if block["voided"] or block["group_id"] != record["group_id"]:
            raise _fail("void_target_invalid")
        for local_date in (block["before_date"], block["after_date"]):
            if local_date is not None and local_date in self.reconciled:
                raise _fail("date_already_reconciled")
        block["voided"] = True
        block["void_reason"] = record["reason"]

    def _reconciled_date(self, record: dict[str, Any]) -> None:
        local_date = record["local_date"]
        if local_date in self.reconciled:
            raise _fail("date_already_reconciled")
        if self.has_open_block():
            raise _fail("block_open")
        summary = self.date_summary(local_date)
        if summary is None:
            raise _fail("day_not_found")
        if parse_utc(record["utc"]) < summary["ready_at"]:
            raise _fail("reconcile_too_early")
        desktop = _reading(record["desktop_total"])
        passed = abs(desktop - summary["span"]) <= summary["tolerance"]
        self.reconciled[local_date] = {
            "desktop": desktop,
            "span": summary["span"],
            "tolerance": summary["tolerance"],
            "passed": passed,
        }

    def _new_reading(
        self,
        record: dict[str, Any],
        *,
        kind: str,
        floor: Decimal | None = None,
        ceiling: Decimal | None = None,
    ) -> Decimal:
        total = _reading(record["total"])
        if record["local_date"] in self.reconciled:
            raise _fail("date_already_reconciled")
        if floor is not None and total < floor:
            raise _fail("reading_decreased")
        if ceiling is not None and total > ceiling:
            raise _fail("reading_delta_too_large")
        self.readings.append(
            {
                "kind": kind,
                "local_date": record["local_date"],
                "utc": parse_utc(record["utc"]),
                "total": total,
            }
        )
        return total

    # ---- helpers

    def _open_phase(self) -> dict[str, Any]:
        if not self.phases or self._phase_complete(self.phases[-1]):
            raise _fail("no_open_phase")
        return self.phases[-1]

    @staticmethod
    def _open_block(phase: dict[str, Any] | None) -> dict[str, Any] | None:
        if phase is not None and phase["blocks"]:
            last = phase["blocks"][-1]
            if last["after"] is None and not last["voided"]:
                return last
        return None

    @staticmethod
    def _anchor(phase: dict[str, Any]) -> Decimal:
        """The last total seen in this phase, or its starting reading."""
        if not phase["blocks"]:
            return phase["start_total"]
        last = phase["blocks"][-1]
        return last["after"] if last["after"] is not None else last["before"]

    def _group_done(self, phase: dict[str, Any], group_id: str) -> bool:
        return any(
            block["group_id"] == group_id and not block["voided"] and block["after"] is not None
            for block in phase["blocks"]
        )

    def _phase_complete(self, phase: dict[str, Any]) -> bool:
        slot = self.schedule[phase["phase"] - 1]
        return all(self._group_done(phase, group["group_id"]) for group in slot["groups"])

    def _next_group(self, phase: dict[str, Any]) -> str | None:
        for group in self.schedule[phase["phase"] - 1]["groups"]:
            if not self._group_done(phase, group["group_id"]):
                return group["group_id"]
        return None

    def _group_tasks(self, phase: dict[str, Any], group_id: str) -> list[str]:
        for group in self.schedule[phase["phase"] - 1]["groups"]:
            if group["group_id"] == group_id:
                return list(group["tasks"])
        raise _fail("group_unknown")

    def _pending_tasks(self, phase: dict[str, Any], block: dict[str, Any]) -> list[str]:
        answered = {answer["task_id"] for answer in block["answers"]}
        return [
            task for task in self._group_tasks(phase, block["group_id"]) if task not in answered
        ]

    def _blocks_on(self, local_date: str) -> int:
        return sum(
            1
            for phase in self.phases
            for block in phase["blocks"]
            if local_date in (block["before_date"], block["after_date"])
        )


# ------------------------------------------------------------------- reconciliation


def reconcile(state: StudyState, local_date: str, desktop_total: str) -> dict[str, Any]:
    """Compare one date's meter change with Claude Desktop's line for that date.

    Returns a result without changing the state. Record a ``day_reconciled`` entry to
    keep it; the ledger then enforces the 4-hour wait.
    """
    if _valid_date_text(local_date) is None:
        raise _fail("record_invalid")
    desktop = _reading(desktop_total)
    if local_date in state.reconciled:
        raise _fail("date_already_reconciled")
    if state.has_open_block():
        raise _fail("block_open")
    summary = state.date_summary(local_date)
    if summary is None:
        raise _fail("day_not_found")
    difference = desktop - summary["span"]
    tolerance = summary["tolerance"]
    return {
        "local_date": local_date,
        "desktop_total": _money(desktop),
        "measured_total": _money(summary["span"]),
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
    """Build the study report. Money is a 2-decimal string and no floats are used.

    Units are groups: for each method pair, one unit per group where both methods have
    an included group run (cluster = group). Only included runs count: not voided, not
    spanning midnight, and on a date that passed its check. Provenance needs the whole
    study complete and every date with a non-voided group checked (pass or fail; a
    failed date's groups are simply left out). The primary comparison (B vs C_normal)
    is also withheld when B has more than ``quality_tolerance`` fewer correct answers.
    """
    checked = validate_pack(pack)
    if state.pack_sha256 != pack_digest(checked):
        raise _fail("pack_mismatch")
    if not state.started:
        raise _fail("study_not_started")
    methods = study_methods(checked)

    group_ids = [group["group_id"] for group in checked["groups"]]
    tasks = [task["task_id"] for task in checked["tasks"]]
    included: dict[tuple[str, str], dict[str, Any]] = {}
    left_out: list[dict[str, Any]] = []
    needed_dates: set[str] = set()
    for phase in state.phases:
        for block in phase["blocks"]:
            outcome = state.block_outcome(phase, block)
            if outcome == OUT_INCLUDED:
                included[(phase["method"], block["group_id"])] = block
            else:
                left_out.append(
                    {"phase": phase["phase"], "group_id": block["group_id"], "reason": outcome}
                )
            if not block["voided"]:
                needed_dates.add(block["before_date"])
                if block["after_date"] is not None:
                    needed_dates.add(block["after_date"])

    def cost(block: dict[str, Any]) -> Decimal:
        return block["after"] - block["before"]

    spend: dict[str, Decimal] = {}
    correct: dict[str, int] = {}
    attempted: dict[str, int] = {}
    group_counts: dict[str, int] = {}
    per_task: dict[str, dict[str, list[int]]] = {
        task: {method: [0, 0] for method in methods} for task in tasks
    }
    for method in methods:
        blocks = [included[(method, g)] for g in group_ids if (method, g) in included]
        answers = [answer for block in blocks for answer in block["answers"]]
        group_counts[method] = len(blocks)
        spend[method] = sum((cost(block) for block in blocks), Decimal(0))
        correct[method] = sum(1 for answer in answers if answer["correct"])
        attempted[method] = len(answers)
        for answer in answers:
            counts = per_task[answer["task_id"]][method]
            counts[1] += 1
            if answer["correct"]:
                counts[0] += 1

    methods_out = {}
    for method in methods:
        per_correct = None
        if correct[method]:
            per_correct = _money(spend[method] / Decimal(correct[method]))
        methods_out[method] = {
            "groups": group_counts[method],
            "spend": _money(spend[method]),
            "tasks_attempted": attempted[method],
            "correct": correct[method],
            "cost_per_correct": per_correct,
        }

    study_complete = state.study_complete
    provenance = (
        study_complete
        and bool(needed_dates)
        and all(state.date_status(d) != "pending" for d in needed_dates)
    )
    tolerance = checked["quality_tolerance"]
    regression = correct["B"] < correct["C_normal"] - tolerance

    comparisons = []
    for key, candidate, reference, primary in COMPARISONS:
        if candidate not in methods or reference not in methods:
            continue
        units: list[PairedUnit] = []
        for group_id in group_ids:
            cand = included.get((candidate, group_id))
            ref = included.get((reference, group_id))
            if cand is not None and ref is not None:
                units.append(PairedUnit(cluster_id=group_id, difference=cost(cand) - cost(ref)))
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
        if not study_complete and REASON_INCOMPLETE not in decision["reasons"]:
            decision["reasons"].append(REASON_INCOMPLETE)
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

    phases_out = []
    for slot in state.schedule:
        phase = next((p for p in state.phases if p["phase"] == slot["phase"]), None)
        if phase is None:
            status, started, ended = "not_started", None, None
        elif state._phase_complete(phase):
            status = "complete"
            started, ended = phase["started_utc"], phase["blocks"][-1]["after_utc"]
        else:
            status, started, ended = "in_progress", phase["started_utc"], None
        phases_out.append(
            {
                "phase": slot["phase"],
                "half": slot["half"],
                "method": slot["method"],
                "status": status,
                "started_utc": started,
                "ended_utc": ended,
            }
        )

    dates_out = []
    for local_date in state.dates_with_block_readings():
        runs = [
            block
            for phase in state.phases
            for block in phase["blocks"]
            if local_date in (block["before_date"], block["after_date"])
        ]
        dates_out.append(
            {
                "local_date": local_date,
                "status": state.date_status(local_date),
                "group_runs": len(runs),
                "included_group_runs": sum(
                    1 for block in runs if state.block_outcome(None, block) == OUT_INCLUDED
                ),
            }
        )

    warnings_out = [
        {
            "phase": w["phase"],
            "group_id": w["group_id"],
            "local_date": w["local_date"],
            "reason": w["reason"],
        }
        for w in state.warnings
    ]

    quality_correct = {method: correct[method] for method in methods}
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": REPORT_KIND,
        "study_id": checked["study_id"],
        "model_label": checked["model_label"],
        "pack_sha256": state.pack_sha256,
        "seed_commitment": hashlib.sha256((state.seed_hex or "").encode("ascii")).hexdigest(),
        "scope": dict(SCOPE),
        "progress": {
            "phases_total": state.phase_count,
            "phases_started": len(state.phases),
            "phases_complete": len(state.completed_phases),
            "current_phase": state.current_phase,
            "study_complete": study_complete,
            "group_runs_total": state.group_runs_total,
            "group_runs_recorded": state.recorded_group_runs(),
            "group_runs_included": len(included),
            "chats_total": state.chats_total,
        },
        "methods": methods_out,
        "per_task": {
            task: {
                method: {
                    "correct": per_task[task][method][0],
                    "attempted": per_task[task][method][1],
                }
                for method in methods
            }
            for task in tasks
        },
        "quality": {
            "tolerance": tolerance,
            "correct": quality_correct,
            "regression": regression,
        },
        "comparisons": comparisons,
        "phases": phases_out,
        "dates": dates_out,
        "pending_reconciliations": state.pending_reconciliations(),
        "warnings": warnings_out,
        "left_out": left_out,
    }


__all__ = [
    "ANSWER_KINDS",
    "COMPARISONS",
    "HALVES",
    "LEDGER_TYPES",
    "METHODS",
    "PHASE_COUNT",
    "PILOT_METHODS",
    "REPORT_SEED",
    "REVIEW_DELAY",
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
    "parse_utc",
    "pack_digest",
    "pilot_changed",
    "reconcile",
    "render_prompt",
    "study_methods",
    "study_started_fields",
    "validate_pack",
]
