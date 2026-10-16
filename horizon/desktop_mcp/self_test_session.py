"""Long-session self test: two chats, one with ContextShrink and one without, many questions each.

This module holds the logic only. It never prompts, never reads Claude Desktop, and
never stores answer text, file contents or paths. The terminal wizard (``self_test_cli``)
calls the functions below.

The unit is one chat. A seeded coin decides which of the two chats runs first. Each chat
has a reading before its first question and one after its last. Dates are checked
against Claude Desktop's daily line exactly as in ``self_test``: the span of a date's
readings must match the line within one cent per chat on that date, plus one cent.

Every write follows the same pattern as ``self_test``: build a record, check it with
``SessionState.with_record``, then ``Ledger.append_record``.
"""

from __future__ import annotations

import copy
import hashlib
import random
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

from horizon.desktop_mcp.self_test import (
    _ENDING,
    _HEX64,
    _STUDY_ID,
    _TASK_ID,
    MAX_QUALITY_TOLERANCE,
    REASON_INCOMPLETE,
    REASON_QUALITY,
    RECONCILE_CENT,
    REVIEW_DELAY,
    SCHEMA_VERSION,
    VOID_REASONS,
    Ledger,
    SelfTestError,
    _canonical,
    _check_record_shape,
    _fail,
    _int,
    _list,
    _money,
    _printable,
    _reading,
    _source_file_name,
    _task,
    _text,
    parse_utc,
)

SESSION_PACK_KIND = "self_test_session_pack"
SESSION_REPORT_KIND = "self_test_session_report"
SESSION_METHODS: tuple[str, ...] = ("B", "C_normal")
CHATS_TOTAL = len(SESSION_METHODS)
SESSION_TASKS_MIN = 4
SESSION_TASKS_MAX = 40
SESSION_DOCUMENTS_MAX = 64
# Void reasons: the single-question reasons, plus one the wizard records by itself when a
# chat's readings fall on two local dates (such a chat cannot be measured).
SESSION_VOID_REASONS: tuple[str, ...] = VOID_REASONS + ("spans_midnight",)
# A chat's spend is far larger than a single question's; this only catches typos.
MAX_CHAT_DELTA = Decimal("50.00")
SESSION_SCOPE = (
    "one paired session on this account, model setting and task set; no confidence interval"
)

REASON_PENDING = "reconciliation_pending"
REASON_FAILED = "reconciliation_failed"
VERDICT_SAVINGS = "savings_observed"
VERDICT_INCREASE = "cost_increase_observed"
VERDICT_NO_DIFFERENCE = "no_difference"
VERDICT_NONE = "none"

SESSION_RECORD_FIELDS: dict[str, frozenset[str]] = {
    "study_started": frozenset({"pack_digest", "seed_hex", "order"}),
    "chat_started": frozenset({"method", "total", "setup_confirmed"}),
    "task_answer": frozenset({"method", "task_id", "correct", "answer_sha256"}),
    "chat_ended": frozenset({"method", "total"}),
    "chat_voided": frozenset({"method", "reason"}),
    "day_reconciled": frozenset({"desktop_total"}),
}

_SESSION_PACK_KEYS = frozenset(
    {
        "schema_version",
        "kind",
        "study_id",
        "model_label",
        "quality_tolerance",
        "methods",
        "documents",
        "tasks",
    }
)


# ----------------------------------------------------------------------------- pack


def validate_session_pack(value: Any) -> dict[str, Any]:
    """Check a session pack and return a normalised copy.

    Raises ``SelfTestError`` with a stable code. ``documents`` must equal, in order, the
    union of the tasks' ``source_files`` (first appearance order), so every document is
    attached and every attachment is a document.
    """
    if not isinstance(value, dict) or set(value) != _SESSION_PACK_KEYS:
        raise _fail("pack_invalid")
    _int(value["schema_version"], SCHEMA_VERSION, SCHEMA_VERSION, "pack_schema_invalid")
    if value["kind"] != SESSION_PACK_KIND:
        raise _fail("pack_schema_invalid")
    if value["methods"] != list(SESSION_METHODS):
        raise _fail("session_methods_invalid")
    tolerance = _int(
        value["quality_tolerance"], 0, MAX_QUALITY_TOLERANCE, "quality_tolerance_invalid"
    )
    tasks = [
        _task(item)
        for item in _list(value["tasks"], SESSION_TASKS_MIN, SESSION_TASKS_MAX, "tasks_invalid")
    ]
    task_ids = [task["task_id"] for task in tasks]
    if len(set(task_ids)) != len(task_ids):
        raise _fail("task_id_duplicate")

    needed: list[str] = []
    for task in tasks:
        for name in task["source_files"]:
            if name not in needed:
                needed.append(name)
    documents = [
        _source_file_name(name)
        for name in _list(value["documents"], 1, SESSION_DOCUMENTS_MAX, "documents_invalid")
    ]
    if documents != needed:
        raise _fail("documents_mismatch")

    return {
        "schema_version": SCHEMA_VERSION,
        "kind": SESSION_PACK_KIND,
        "study_id": _text(value["study_id"], _STUDY_ID, "study_id_invalid"),
        "model_label": _printable(value["model_label"], 80, "model_label_invalid"),
        "quality_tolerance": tolerance,
        "methods": list(SESSION_METHODS),
        "documents": documents,
        "tasks": tasks,
    }


def session_pack_digest(pack: Any) -> str:
    """Canonical JSON sha256 of the validated session pack."""
    return hashlib.sha256(_canonical(validate_session_pack(pack))).hexdigest()


def session_order(seed_hex: Any) -> tuple[str, ...]:
    """Which chat runs first, from the seed: a deterministic shuffle of the two methods."""
    text = _text(seed_hex, _HEX64, "seed_invalid")
    rng = random.Random(f"self-test-session-order:{text}")
    order = list(SESSION_METHODS)
    rng.shuffle(order)
    return tuple(order)


def session_study_started_fields(pack: Any, seed_hex: str) -> dict[str, Any]:
    """Fields for the session ``study_started`` record: digest, seed and chat order."""
    checked = validate_session_pack(pack)
    return {
        "pack_digest": session_pack_digest(checked),
        "seed_hex": _text(seed_hex, _HEX64, "seed_invalid"),
        "order": list(session_order(seed_hex)),
    }


# ---------------------------------------------------------------------------- prompts


def session_setup(method: str, pack: Any, *, restart: bool) -> list[str]:
    """Setup steps for one chat. ``restart`` is True only when the setting changed."""
    checked = validate_session_pack(pack)
    if method not in SESSION_METHODS:
        raise _fail("method_invalid")
    steps = [
        f"Set the model to {checked['model_label']}. Keep this setting for both chats.",
        "Use no other Claude product while the test runs: no other Claude Desktop chats, "
        "Claude Code or Codex.",
    ]
    if method == "B":
        steps.append("In the ContextShrink extension, set Pilot method to normal.")
        steps.append("Turn the connector ON.")
    else:
        steps.append("Turn the ContextShrink connector OFF.")
    if restart:
        steps.append(
            "Fully quit Claude Desktop from the tray and reopen it. The connector setting "
            "changed since the last chat."
        )
    if method == "B":
        steps.append(
            'Check it in a new chat: ask Claude "show ContextShrink stats"; evaluation_arm '
            "must say optimized. Do this before the total is read: its spend is not measured."
        )
    return steps


def session_prompt(pack: Any, method: str, number: int) -> str:
    """The exact text for question ``number`` (1-based) in a chat under ``method``.

    Question 1 carries the preamble. For method C_normal the preamble comes with every
    document attached; method B attaches nothing and names the files to read. Neither
    preamble names a ContextShrink mode.
    """
    checked = validate_session_pack(pack)
    if method not in SESSION_METHODS:
        raise _fail("method_invalid")
    tasks = checked["tasks"]
    if not 1 <= number <= len(tasks):
        raise _fail("task_unknown")
    text = tasks[number - 1]["question"]
    if number == 1:
        docs = ", ".join(checked["documents"])
        count = len(tasks)
        if method == "B":
            # Says where the files are without asking for them to be read up front, so
            # the preamble mirrors C's "attached" and leaves the reading strategy to Claude.
            preamble = (
                f"The files {docs} are in my approved ContextShrink folder. "
                f"I will ask you {count} questions about them, one at a time. "
                "Use the ContextShrink tools to answer each from the files."
            )
        else:
            preamble = (
                f"I have attached {docs}. I will ask you {count} questions about them, "
                "one at a time. Answer each from the files."
            )
        text = preamble + "\n\n" + text
    return text + _ENDING


# ---------------------------------------------------------------------------- ledger


def _method_field(value: Any) -> None:
    if value not in SESSION_METHODS:
        raise _fail("record_invalid")


def _check_session_fields(record_type: str, record: dict[str, Any]) -> None:
    """Field checks for the session record types (called by the shared ledger check)."""
    try:
        if record_type == "study_started":
            _text(record["pack_digest"], _HEX64, "record_invalid")
            _text(record["seed_hex"], _HEX64, "record_invalid")
            order = record["order"]
            if order != list(SESSION_METHODS) and order != list(reversed(SESSION_METHODS)):
                raise _fail("record_invalid")
        elif record_type == "chat_started":
            _method_field(record["method"])
            if record["setup_confirmed"] is not True or not isinstance(record["total"], str):
                raise _fail("record_invalid")
        elif record_type == "task_answer":
            _method_field(record["method"])
            _text(record["task_id"], _TASK_ID, "record_invalid")
            _text(record["answer_sha256"], _HEX64, "record_invalid")
            if not isinstance(record["correct"], bool):
                raise _fail("record_invalid")
        elif record_type == "chat_ended":
            _method_field(record["method"])
            if not isinstance(record["total"], str):
                raise _fail("record_invalid")
        elif record_type == "chat_voided":
            _method_field(record["method"])
            if record["reason"] not in SESSION_VOID_REASONS:
                raise _fail("record_invalid")
        elif record_type == "day_reconciled":
            if not isinstance(record["desktop_total"], str):
                raise _fail("record_invalid")
    except KeyError as error:  # pragma: no cover - guarded by the required-field check
        raise _fail("record_invalid") from error


def session_ledger(path: str | Path, *, clock: Any = None) -> Ledger:
    """A ledger that accepts the session record types (same file format and chain)."""
    return Ledger(
        path,
        clock=clock,
        record_fields=SESSION_RECORD_FIELDS,
        check_fields=_check_session_fields,
    )


def check_session_shape(record: Any) -> None:
    """Structural check of one session record (used by the ledger and the state)."""
    _check_record_shape(record, SESSION_RECORD_FIELDS, _check_session_fields)


# ------------------------------------------------------------------------------ state


def _counts(chat: dict[str, Any]) -> bool:
    """A chat that is finished, not voided, and whose readings are on one local date."""
    return (
        not chat["voided"]
        and chat["after"] is not None
        and chat["before_date"] == chat["after_date"]
    )


class SessionState:
    """The session study replayed from its ledger records.

    Rules enforced:

    - the two chats run in the seeded order; a chat starts only after the previous one is
      finished (or voided), with a reading taken before its first question;
    - questions are answered in order; a chat ends with a reading after its last answer;
    - readings are two-decimal, never fall below the previous reading, and a chat adds at
      most $50.00;
    - a voided chat is excluded and redone from its setup in a new chat; nothing else is
      excluded;
    - a chat whose readings fall on two local dates is voided by the wizard at once;
    - each local date is checked against Claude Desktop's daily line once it is 4 hours
      past its last reading. A checked date is final.
    """

    def __init__(self, pack: Any) -> None:
        self.pack = validate_session_pack(pack)
        self.pack_sha256 = session_pack_digest(self.pack)
        self.started = False
        self.seed_hex: str | None = None
        self.order: tuple[str, ...] = ()
        self.chats: list[dict[str, Any]] = []
        self.readings: list[dict[str, Any]] = []
        self.reconciled: dict[str, dict[str, Any]] = {}

    @classmethod
    def from_ledger(cls, pack: Any, records: list[dict[str, Any]]) -> SessionState:
        state = cls(pack)
        for record in records:
            try:
                state = state.with_record(record)
            except SelfTestError as error:
                raise _fail("ledger_tampered") from error
        return state

    def with_record(self, record: dict[str, Any]) -> SessionState:
        """Return the state after ``record``. Raises ``SelfTestError`` if a rule breaks."""
        check_session_shape(record)
        new = copy.deepcopy(self)
        new._apply(record)
        return new

    # ---- status

    def done_chats(self) -> list[dict[str, Any]]:
        return [chat for chat in self.chats if _counts(chat)]

    @property
    def study_complete(self) -> bool:
        return len(self.done_chats()) == CHATS_TOTAL

    def open_chat(self) -> dict[str, Any] | None:
        if self.chats and not self.chats[-1]["voided"] and self.chats[-1]["after"] is None:
            return self.chats[-1]
        return None

    def needs_void(self) -> bool:
        """The last chat ended on a different date from its start: the wizard voids it."""
        if not self.chats:
            return False
        last = self.chats[-1]
        return (
            not last["voided"]
            and last["after"] is not None
            and last["before_date"] != last["after_date"]
        )

    def has_open_block(self) -> bool:
        """Name used by the shared ``reconcile`` rule: no date is checked mid-chat."""
        return self.open_chat() is not None or self.needs_void()

    def reading_floor(self) -> Decimal | None:
        return self.readings[-1]["total"] if self.readings else None

    def dates_with_readings(self) -> list[str]:
        return sorted({reading["local_date"] for reading in self.readings})

    def pending_reconciliations(self) -> list[str]:
        return [d for d in self.dates_with_readings() if d not in self.reconciled]

    def date_status(self, local_date: str) -> str:
        entry = self.reconciled.get(local_date)
        if entry is None:
            return "pending"
        return "passed" if entry["passed"] else "failed"

    def date_summary(self, local_date: str) -> dict[str, Any] | None:
        """Meter change across a local date (last reading minus first) and its tolerance.

        Tolerance is one cent times (chats touching the date + 1). Same rule as
        ``self_test.StudyState.date_summary``.
        """
        readings = [r for r in self.readings if r["local_date"] == local_date]
        if not readings:
            return None
        chats_on = sum(
            1 for chat in self.chats if local_date in (chat["before_date"], chat["after_date"])
        )
        return {
            "span": readings[-1]["total"] - readings[0]["total"],
            "tolerance": RECONCILE_CENT * (chats_on + 1),
            "blocks": chats_on,
            "ready_at": readings[-1]["utc"] + REVIEW_DELAY,
        }

    def reconcile_ready_at(self, local_date: str):
        summary = self.date_summary(local_date)
        return None if summary is None else summary["ready_at"]

    def chat_status(self, chat: dict[str, Any]) -> str:
        if chat["voided"]:
            return "voided"
        if chat["after"] is None:
            return "in_progress"
        if chat["before_date"] != chat["after_date"]:
            return "spans_midnight"
        status = self.date_status(chat["before_date"])
        return {"pending": "date_pending", "failed": "date_failed", "passed": "included"}[status]

    def next_action(self) -> dict[str, Any]:
        """What the wizard should ask for next."""
        if not self.started:
            return {"action": "start_study"}
        chat = self.open_chat()
        if chat is not None:
            tasks = self.pack["tasks"]
            number = len(chat["answers"]) + 1
            if number <= len(tasks):
                return {
                    "action": "ask_question",
                    "chat_number": len(self.done_chats()) + 1,
                    "method": chat["method"],
                    "number": number,
                    "task_id": tasks[number - 1]["task_id"],
                }
            return {
                "action": "read_after",
                "chat_number": len(self.done_chats()) + 1,
                "method": chat["method"],
            }
        if self.needs_void():
            return {"action": "void_midnight", "method": self.chats[-1]["method"]}
        done = len(self.done_chats())
        if done < CHATS_TOTAL:
            method = self.order[done]
            previous = self.chats[-1]["method"] if self.chats else None
            return {
                "action": "start_chat",
                "chat_number": done + 1,
                "method": method,
                "restart": previous is not None and previous != method,
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
        if kind == "chat_started":
            self._chat_started(record)
        elif kind == "task_answer":
            self._task_answer(record)
        elif kind == "chat_ended":
            self._chat_ended(record)
        elif kind == "chat_voided":
            self._chat_voided(record)
        else:
            self._reconciled_date(record)

    def _start(self, record: dict[str, Any]) -> None:
        if self.started:
            raise _fail("study_already_started")
        if record["pack_digest"] != self.pack_sha256:
            raise _fail("pack_mismatch")
        order = session_order(record["seed_hex"])
        if tuple(record["order"]) != order:
            raise _fail("order_mismatch")
        self.started = True
        self.seed_hex = record["seed_hex"]
        self.order = order

    def _chat_started(self, record: dict[str, Any]) -> None:
        if self.open_chat() is not None:
            raise _fail("chat_open")
        if self.needs_void():
            raise _fail("void_required")
        done = len(self.done_chats())
        if done >= CHATS_TOTAL:
            raise _fail("study_complete")
        method = self.order[done]
        if record["method"] != method:
            raise _fail("method_out_of_order")
        total = self._new_reading(record, floor=self.reading_floor())
        self.chats.append(
            {
                "method": method,
                "before": total,
                "before_date": record["local_date"],
                "answers": [],
                "after": None,
                "after_date": None,
                "voided": False,
                "void_reason": None,
            }
        )

    def _task_answer(self, record: dict[str, Any]) -> None:
        chat = self.open_chat()
        if chat is None:
            raise _fail("chat_not_open")
        if record["method"] != chat["method"]:
            raise _fail("method_mismatch")
        tasks = self.pack["tasks"]
        position = len(chat["answers"])
        if position >= len(tasks) or record["task_id"] != tasks[position]["task_id"]:
            raise _fail("task_out_of_order")
        chat["answers"].append(
            {
                "task_id": record["task_id"],
                "correct": record["correct"],
                "answer_sha256": record["answer_sha256"],
            }
        )

    def _chat_ended(self, record: dict[str, Any]) -> None:
        chat = self.open_chat()
        if chat is None:
            raise _fail("chat_not_open")
        if record["method"] != chat["method"]:
            raise _fail("method_mismatch")
        if len(chat["answers"]) != len(self.pack["tasks"]):
            raise _fail("chat_incomplete")
        self._new_reading(
            record,
            floor=chat["before"],
            ceiling=chat["before"] + MAX_CHAT_DELTA,
        )
        chat["after"] = _reading(record["total"])
        chat["after_date"] = record["local_date"]

    def _chat_voided(self, record: dict[str, Any]) -> None:
        if not self.chats:
            raise _fail("void_target_invalid")
        chat = self.chats[-1]
        if chat["voided"] or record["method"] != chat["method"]:
            raise _fail("void_target_invalid")
        for local_date in (chat["before_date"], chat["after_date"]):
            if local_date is not None and local_date in self.reconciled:
                raise _fail("date_already_reconciled")
        chat["voided"] = True
        chat["void_reason"] = record["reason"]

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
                "local_date": record["local_date"],
                "utc": parse_utc(record["utc"]),
                "total": total,
            }
        )
        return total


# ---------------------------------------------------------------------------- analysis


def analyze_session(pack: Any, state: SessionState) -> dict[str, Any]:
    """The session report. Money is a 2-decimal string; no floats are used.

    Verdict, in order:

    - any reason (study incomplete, a date not yet checked, a date that failed, or B with
      fewer correct answers than C_normal beyond the tolerance while B is cheaper) gives
      ``none`` with every reason that applies;
    - otherwise B minus C_normal decides: above zero is ``cost_increase_observed``, zero is
      ``no_difference``, below zero is ``savings_observed``.
    """
    checked = validate_session_pack(pack)
    if state.pack_sha256 != session_pack_digest(checked):
        raise _fail("pack_mismatch")
    if not state.started:
        raise _fail("study_not_started")
    task_count = len(checked["tasks"])
    done = state.done_chats()
    by_method = {chat["method"]: chat for chat in done}
    complete = state.study_complete

    spend: dict[str, Decimal | None] = {}
    correct: dict[str, int | None] = {}
    for method in SESSION_METHODS:
        chat = by_method.get(method)
        if chat is None:
            spend[method], correct[method] = None, None
            continue
        spend[method] = chat["after"] - chat["before"]
        correct[method] = sum(1 for answer in chat["answers"] if answer["correct"])

    pending = state.pending_reconciliations()
    failed = sorted(d for d, entry in state.reconciled.items() if not entry["passed"])
    tolerance = checked["quality_tolerance"]
    regression = None
    difference: Decimal | None = None
    if complete:
        regression = correct["B"] < correct["C_normal"] - tolerance
        difference = spend["B"] - spend["C_normal"]

    reasons: list[str] = []
    if not complete:
        reasons.append(REASON_INCOMPLETE)
    if pending:
        reasons.append(REASON_PENDING)
    if failed:
        reasons.append(REASON_FAILED)
    if complete and difference < 0 and regression:
        reasons.append(REASON_QUALITY)
    if reasons:
        verdict = VERDICT_NONE
    elif difference > 0:
        verdict = VERDICT_INCREASE
    elif difference == 0:
        verdict = VERDICT_NO_DIFFERENCE
    else:
        verdict = VERDICT_SAVINGS

    percent = None
    if complete and spend["C_normal"]:
        ratio = difference / spend["C_normal"] * 100
        percent = str(ratio.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))

    if not state.readings:
        reconciliation = "pending"
    elif failed:
        reconciliation = "failed"
    elif pending:
        reconciliation = "pending"
    else:
        reconciliation = "passed"

    dates_out = []
    for local_date in state.dates_with_readings():
        summary = state.date_summary(local_date)
        entry = state.reconciled.get(local_date)
        row: dict[str, Any] = {
            "local_date": local_date,
            "status": state.date_status(local_date),
            "measured_total": _money(summary["span"]),
            "tolerance": _money(summary["tolerance"]),
        }
        if entry is not None:
            row["desktop_total"] = _money(entry["desktop"])
            row["difference"] = _money(entry["desktop"] - entry["span"])
        dates_out.append(row)

    methods_out = {}
    for method in SESSION_METHODS:
        if spend[method] is None:
            methods_out[method] = {
                "spend": None,
                "correct": None,
                "tasks": task_count,
                "cost_per_correct": None,
            }
            continue
        per_correct = None
        if correct[method]:
            per_correct = _money(spend[method] / Decimal(correct[method]))
        methods_out[method] = {
            "spend": _money(spend[method]),
            "correct": correct[method],
            "tasks": task_count,
            "cost_per_correct": per_correct,
        }

    return {
        "schema_version": SCHEMA_VERSION,
        "kind": SESSION_REPORT_KIND,
        "study_id": checked["study_id"],
        "model_label": checked["model_label"],
        "pack_sha256": state.pack_sha256,
        "seed_commitment": hashlib.sha256((state.seed_hex or "").encode("ascii")).hexdigest(),
        "scope": SESSION_SCOPE,
        "order": list(state.order),
        "progress": {
            "chats_total": CHATS_TOTAL,
            "chats_started": len(state.chats),
            "chats_complete": len(done),
            "study_complete": complete,
            "tasks_per_chat": task_count,
        },
        "chats": [
            {"number": index + 1, "method": chat["method"], "status": state.chat_status(chat)}
            for index, chat in enumerate(state.chats)
        ],
        "methods": methods_out,
        "B_minus_C": None if difference is None else _money(difference),
        "B_vs_C_percent": percent,
        "reconciliation": {"status": reconciliation, "dates": dates_out},
        "quality": {
            "tolerance": tolerance,
            "correct": {method: correct[method] for method in SESSION_METHODS},
            "regression": regression,
        },
        "verdict": verdict,
        "reasons": reasons,
    }


__all__ = [
    "CHATS_TOTAL",
    "MAX_CHAT_DELTA",
    "REASON_FAILED",
    "REASON_PENDING",
    "SESSION_METHODS",
    "SESSION_PACK_KIND",
    "SESSION_RECORD_FIELDS",
    "SESSION_REPORT_KIND",
    "SESSION_SCOPE",
    "SESSION_VOID_REASONS",
    "SessionState",
    "analyze_session",
    "session_ledger",
    "session_order",
    "session_pack_digest",
    "session_prompt",
    "session_setup",
    "session_study_started_fields",
    "validate_session_pack",
]
