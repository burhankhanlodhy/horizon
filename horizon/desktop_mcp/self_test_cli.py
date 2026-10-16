"""Terminal wizard for the single-account self test (ContextShrink Desktop MCP).

Run from the repository root::

    python -m horizon.desktop_mcp self-test init --pack my_pack.json
    python -m horizon.desktop_mcp self-test run --state-dir "<folder printed by init>"

The wizard only asks questions. The rules live in horizon/desktop_mcp/self_test.py.
Every answer is saved to the ledger as soon as it is given, so you can type q at any
prompt and carry on later with the same command.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import subprocess
import sys
import time
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from horizon.desktop_mcp.self_test import (
    MAX_BLOCK_DELTA,
    REVIEW_DELAY,
    Ledger,
    SelfTestError,
    StudyState,
    analyze,
    answer_digest,
    check_answer,
    default_state_dir,
    method_setup,
    new_seed_hex,
    pack_digest,
    parse_utc,
    pilot_changed,
    reconcile,
    render_prompt,
    study_started_fields,
    validate_pack,
)

LEDGER_FILE = "ledger.jsonl"
PACK_FILE = "pack.json"
SETTLE_SECONDS = 90
MAX_READING = Decimal("100000.00")
READING = re.compile(r"^[0-9]{1,6}\.[0-9]{2}$")
ANSWER_LINE = re.compile(r"^\s*answer:\s*\S", re.IGNORECASE)
VOID_CHOICES = {"1": "interrupted", "2": "chat_not_fresh", "3": "setup_wrong", "4": "other"}
RETYPE_CODES = frozenset({"reading_decreased", "reading_delta_too_large"})
# Rough planning figures per chat and per phase, for the init estimate only.
MINUTES_PER_CHAT = (3, 4)
MINUTES_PER_PHASE = (2, 3)
RULE = "=" * 64
THIN = "-" * 64

METHOD_NAMES = {
    "A": "ContextShrink, compression off",
    "B": "ContextShrink, normal",
    "C_normal": "connector off, files attached",
    "C_tuned": "connector off, files attached, answer rules first",
}

ERROR_TEXT = {
    "ledger_tampered": (
        "The study record in this folder was changed or damaged, so the wizard stopped. "
        "Nothing more was written. Do not edit files in the folder; keep it and ask for help."
    ),
    "pack_mismatch": "The pack copy in this folder does not match the study record.",
    "pack_copy_missing": "The study folder has no pack copy. Ask for help before going further.",
    "study_not_started": "The study has not started yet. Run init first.",
    "block_incomplete": "Not every task in this group has an answer yet.",
    "date_already_reconciled": (
        "That date has already been checked, so its readings cannot change. Nothing was saved."
    ),
    "reconcile_too_early": (
        "That date is not ready to check yet. Wait 4 hours after its last reading."
    ),
    "reading_decreased": (
        "The total is lower than the total before this point, which cannot happen. "
        "Check the reading and type it again."
    ),
    "reading_delta_too_large": (
        "This group shows more than $20.00 of spend, which looks like a typo. "
        "Check the reading and type it again."
    ),
}

REASON_TEXT = {
    "spend_between_blocks": "Claude Desktop spent money between two groups in the same phase",
    "voided": "the group was voided and redone",
    "in_progress": "the group was not finished",
    "spans_midnight": "the group's readings fall on two different dates",
    "date_pending": "its date has not been checked against Claude Desktop yet",
    "date_failed": "its date did not match Claude Desktop's daily line",
    "quality_regression": "method B had fewer correct answers than C_normal beyond the tolerance",
    "provenance_unverified": "not every date is checked and matched yet",
    "study_incomplete": "not all eight phases have run yet",
    "below_min_units": "too few paired groups so far",
    "below_min_clusters": "too few groups so far",
    "insufficient_interval": "not enough data for a range yet",
    "evidence_class_not_eligible": "the evidence is not eligible for a claim",
    "synthetic_evidence": "the evidence is marked synthetic",
}

CLAIM_TEXT = {
    "savings_supported": "B cost less than C_normal on this account (the whole 95% range is below 0).",
    "cost_increase_supported": "B cost more than C_normal on this account (the whole 95% range is above 0).",
    "inconclusive": "No clear difference yet: the 95% range includes 0.",
    "none": "No claim can be made yet.",
}


class QuitRequested(Exception):
    """The user typed q or input ended. Everything already entered is saved."""


class _Stop(Exception):
    """A plain-language stop with a message for the user."""

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


def _system_clock() -> datetime:
    return datetime.now(timezone.utc)


class Console:
    """Input, output, clock and sleep, injectable so tests can script a whole session."""

    def __init__(
        self,
        *,
        read: Any = None,
        write: Any = None,
        clock: Any = None,
        sleep: Any = None,
        platform: str | None = None,
    ) -> None:
        self._read = read if read is not None else input
        self._write = write if write is not None else print
        self._clock = clock if clock is not None else _system_clock
        self._sleep = sleep if sleep is not None else time.sleep
        self.platform = platform if platform is not None else sys.platform

    def clock(self) -> datetime:
        return self._clock()

    def sleep(self, seconds: float) -> None:
        self._sleep(seconds)

    def say(self, text: str = "") -> None:
        self._write(text)

    def ask(self, prompt: str) -> str:
        """Read one line. q quits; end of input also quits safely."""
        try:
            raw = self._read(prompt)
        except EOFError as error:
            raise QuitRequested() from error
        value = raw.strip()
        if value.lower() == "q":
            raise QuitRequested()
        return value


def copy_to_clipboard(text: str) -> None:
    """Put text on the Windows clipboard with clip.exe. The text goes on stdin, no shell."""
    data = b"\xff\xfe" + text.encode("utf-16-le")
    subprocess.run(["clip"], input=data, check=True)


def _clock_text(moment: datetime) -> str:
    return moment.astimezone().strftime("%H:%M")


def _dollars(text: str) -> str:
    return f"-${text[1:]}" if text.startswith("-") else f"${text}"


def _program() -> str:
    if getattr(sys, "frozen", False):
        return f"{Path(sys.executable).name} self-test"
    return "python -m horizon.desktop_mcp self-test"


def _command(verb: str, state_dir: Path) -> str:
    return f'{_program()} {verb} --state-dir "{state_dir}"'


def _load_pack_copy(state_dir: Path) -> dict[str, Any]:
    path = state_dir / PACK_FILE
    if not path.exists():
        raise SelfTestError("pack_copy_missing")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise SelfTestError("pack_copy_missing") from error
    return validate_pack(raw)


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temporary.replace(path)


class Session:
    """The study: its pack copy, ledger and replayed state. Writes go through write()."""

    def __init__(self, state_dir: Path, console: Console) -> None:
        self.state_dir = state_dir
        self.console = console
        self.pack = _load_pack_copy(state_dir)
        self.ledger = Ledger(state_dir / LEDGER_FILE, clock=console.clock)
        self.records = self.ledger.load()
        self.state = StudyState.from_ledger(self.pack, self.records)

    def write(self, record_type: str, **fields: Any) -> dict[str, Any]:
        """Check the rule first, then append the record. Raises SelfTestError on a breach."""
        record = self.ledger.build(record_type, **fields)
        self.state = self.state.with_record(record)
        self.ledger.append_record(record)
        self.records.append(record)
        return record

    def last_answer_time(self) -> datetime | None:
        for record in reversed(self.records):
            if record["type"] == "task_answer":
                return parse_utc(record["utc"])
        return None

    def reconcile_queue(self, now: datetime) -> tuple[list[str], list[tuple[str, datetime]]]:
        """Dates waiting for a check: (due now, not due until a later time)."""
        due: list[str] = []
        upcoming: list[tuple[str, datetime]] = []
        for local_date in self.state.pending_reconciliations():
            ready = self.state.reconcile_ready_at(local_date)
            if now >= ready:
                due.append(local_date)
            else:
                upcoming.append((local_date, ready))
        return due, upcoming


def _open_session(state_dir_arg: str | None, console: Console) -> Session | None:
    state_dir = _resolve_state_dir(state_dir_arg, console)
    if state_dir is None:
        return None
    if not (state_dir / LEDGER_FILE).exists():
        console.say(f"No study was found in {state_dir}.")
        console.say("Run init first, with your pack file, or check the folder name.")
        return None
    return Session(state_dir, console)


def _resolve_state_dir(state_dir_arg: str | None, console: Console) -> Path | None:
    if state_dir_arg:
        return Path(state_dir_arg)
    # default_state_dir needs a study id; its parent is the shared self-test root.
    root = default_state_dir("st_probe").parent
    found = sorted(path.parent for path in root.glob(f"*/{LEDGER_FILE}")) if root.exists() else []
    if len(found) == 1:
        return found[0]
    if not found:
        console.say("No study was found on this computer. Run init first, with your pack file.")
    else:
        console.say("More than one study was found. Pick one and add --state-dir:")
        for path in found:
            console.say(f"  {path}")
    return None


def _read_json_file(path: Path) -> Any:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise _Stop(f"Could not read the pack file: {path}") from error
    try:
        return json.loads(text)
    except ValueError as error:
        raise _Stop(f"The pack file is not valid JSON: {path}") from error


def _has_example_questions(raw: Any) -> bool:
    tasks = raw.get("tasks") if isinstance(raw, dict) else None
    if not isinstance(tasks, list):
        return False
    return any(
        isinstance(task, dict)
        and isinstance(task.get("question"), str)
        and task["question"].startswith("EXAMPLE")
        for task in tasks
    )


def _plan_lines(schedule: list[dict[str, Any]]) -> list[str]:
    lines = []
    for slot in schedule:
        groups = slot["groups"]
        chats = sum(len(group["tasks"]) for group in groups)
        lines.append(
            f"  Phase {slot['phase']}  {slot['half']}  method {slot['method']} "
            f"({METHOD_NAMES[slot['method']]}): {len(groups)} groups, {chats} chats"
        )
    return lines


def _estimate_text(chats: int, phases: int) -> str:
    low = (chats * MINUTES_PER_CHAT[0] + phases * MINUTES_PER_PHASE[0]) / 60
    high = (chats * MINUTES_PER_CHAT[1] + phases * MINUTES_PER_PHASE[1]) / 60
    return f"about {low:.1f} to {high:.1f} hours"


def _group_position(state: StudyState, phase: int, group_id: str) -> tuple[int, int]:
    groups = state.schedule[phase - 1]["groups"]
    for index, group in enumerate(groups, start=1):
        if group["group_id"] == group_id:
            return index, len(groups)
    raise SelfTestError("group_unknown")


def _group_tasks(state: StudyState, phase: int, group_id: str) -> list[str]:
    for group in state.schedule[phase - 1]["groups"]:
        if group["group_id"] == group_id:
            return list(group["tasks"])
    raise SelfTestError("group_unknown")


def _task_by_id(pack: dict[str, Any], task_id: str) -> dict[str, Any]:
    for task in pack["tasks"]:
        if task["task_id"] == task_id:
            return task
    raise SelfTestError("task_unknown")


def _ask_reading(
    console: Console,
    prompt: str,
    *,
    floor: Decimal | None = None,
    ceiling: Decimal | None = None,
) -> str:
    """Ask for a dollar total with exactly two decimals. Re-asks until it is valid."""
    while True:
        cleaned = console.ask(prompt).replace("$", "").replace(",", "").strip()
        if not (READING.fullmatch(cleaned) and Decimal(cleaned) <= MAX_READING):
            console.say("That is not an amount with exactly two decimals, such as 20.23.")
            console.say("Type it again, or q to stop.")
            continue
        value = Decimal(cleaned)
        if floor is not None and value < floor:
            console.say(ERROR_TEXT["reading_decreased"])
            continue
        if ceiling is not None and value > ceiling:
            console.say(ERROR_TEXT["reading_delta_too_large"])
            continue
        return cleaned


def _record_reading(
    session: Session,
    record_type: str,
    group_id: str,
    prompt: str,
    *,
    floor: Decimal | None = None,
    ceiling: Decimal | None = None,
) -> str:
    """Ask for a total and record it. Readings that break a rule are re-asked, not lost."""
    while True:
        total = _ask_reading(session.console, prompt, floor=floor, ceiling=ceiling)
        try:
            session.write(record_type, group_id=group_id, total=total)
            return total
        except SelfTestError as error:
            if error.code not in RETYPE_CODES:
                raise
            session.console.say(ERROR_TEXT[error.code])


def _start_phase(session: Session, action: dict[str, Any]) -> None:
    console = session.console
    state = session.state
    phase, half, method = action["phase"], action["half"], action["method"]
    console.say("")
    console.say(RULE)
    console.say(f"Phase {phase} of {state.phase_count}: method {method}")
    console.say(
        f"Method: {METHOD_NAMES[method]}  ({half}, {len(state.schedule[phase - 1]['groups'])} groups)"
    )
    console.say(f"Model setting (keep it for every phase): {session.pack['model_label']}")
    console.say(THIN)
    console.say("Step 1: read the running total in Claude Desktop settings now, before any change.")
    total = _ask_reading(
        console, "Type the total spent right now (for example 20.23): ", floor=state.reading_floor()
    )
    console.say("Step 2: set Claude Desktop up like this:")
    changed = pilot_changed(state.schedule, phase)
    for number, step in enumerate(
        method_setup(method, session.pack, pilot_changed=changed), start=1
    ):
        console.say(f"  {number}. {step}")
    if method in ("A", "B"):
        console.say("The ContextShrink check chat happens now, before this phase's first group.")
        console.say("Its spend is expected here and is not measured.")
    console.say(RULE)
    while True:
        answer = console.ask("Type yes when Claude Desktop is set up and any check is done: ")
        if answer.lower() == "yes":
            break
        console.say("Not started yet. Set Claude Desktop up as listed above, then type yes.")
    session.write(
        "phase_started", phase=phase, half=half, method=method, setup_confirmed=True, total=total
    )
    local_date = console.clock().astimezone().strftime("%Y-%m-%d")
    console.say(f"Phase {phase} started on {local_date}. Next: the first group's before reading.")


def _read_before(session: Session, action: dict[str, Any]) -> None:
    console = session.console
    phase, group_id = action["phase"], action["group_id"]
    index, count = _group_position(session.state, phase, group_id)
    console.say("")
    console.say(THIN)
    console.say(f"PHASE {phase} of {session.state.phase_count}  |  GROUP {index} of {count}")
    console.say("Close or ignore other Claude chats.")
    console.say("Open Claude Desktop Settings -> Usage and read the total spent right now.")
    warnings_before = len(session.state.warnings)
    _record_reading(
        session,
        "block_before",
        group_id,
        "Type the total spent right now (for example 20.23): ",
        floor=session.state.reading_floor(),
    )
    for warning in session.state.warnings[warnings_before:]:
        reason = REASON_TEXT.get(warning["reason"], warning["reason"])
        console.say(f"Warning: {reason}.")
        console.say("This group still counts; it is listed as a warning in the report.")


def _ask_answer_or_void(console: Console) -> str | None:
    """The ANSWER line, or None when the owner types V to void this group."""
    while True:
        line = console.ask(
            "Paste the line that starts with ANSWER: here, or type V to void this group and redo it: "
        )
        if line.lower() == "v":
            return None
        if ANSWER_LINE.match(line):
            return line
        console.say("That line should start with ANSWER: followed by the answer.")
        console.say("Copy it from the end of Claude's reply and paste it again.")


def _ask_task(session: Session, action: dict[str, Any]) -> None:
    console = session.console
    phase, group_id, task_id = action["phase"], action["group_id"], action["task_id"]
    state = session.state
    method = state.schedule[phase - 1]["method"]
    tasks = _group_tasks(state, phase, group_id)
    task = _task_by_id(session.pack, task_id)
    prompt = render_prompt(session.pack, task_id, method)
    console.say("")
    console.say(RULE)
    console.say(
        f"PHASE {phase}  |  GROUP {group_id}  |  TASK {tasks.index(task_id) + 1} of {len(tasks)}"
    )
    console.say(THIN)
    console.say(prompt)
    console.say(RULE)
    if method in ("C_normal", "C_tuned"):
        console.say("Attach these files with the paperclip: " + ", ".join(task["source_files"]))
    else:
        console.say("Do not attach files for this method.")
    if console.platform == "win32":
        choice = console.ask(
            "Press C then Enter to copy the prompt to the clipboard, or just Enter: "
        )
        if choice.lower() == "c":
            try:
                copy_to_clipboard(prompt)
                console.say("Copied to the clipboard.")
            except (OSError, subprocess.CalledProcessError):
                console.say("Could not copy. Select the prompt text above and copy it by hand.")
    console.say("Start a NEW chat in Claude Desktop, paste the prompt, attach files if this")
    console.say("method says so, and wait for the full reply.")
    line = _ask_answer_or_void(console)
    if line is None:
        reason = _ask_void_reason(console)
        session.write("block_voided", group_id=group_id, reason=reason)
        console.say(f"Group {group_id} is voided and left out. It is redone straight away.")
        return
    correct = check_answer(task, line)
    session.write(
        "task_answer", task_id=task_id, correct=correct, answer_sha256=answer_digest(line)
    )
    console.say("Correct." if correct else "Not correct.")


def _wait_after_answers(session: Session) -> None:
    """Visible countdown from the last task answer. Enter is only accepted after it ends."""
    console = session.console
    last = session.last_answer_time()
    if last is None:
        return
    # Ledger times are whole seconds, so allow one extra second of margin.
    ready_at = last + timedelta(seconds=SETTLE_SECONDS + 1)
    console.say(f"Waiting {SETTLE_SECONDS} seconds so Claude Desktop can update its total.")
    while True:
        left = (ready_at - console.clock()).total_seconds()
        if left <= 0:
            break
        whole = math.ceil(left)
        if whole % 10 == 0 or whole <= 5:
            console.say(f"  {whole} seconds left")
        console.sleep(min(1.0, left))
    console.say("Countdown finished.")
    console.ask("Press Enter to enter the total (or q to stop): ")


def _read_after(session: Session, action: dict[str, Any]) -> None:
    console = session.console
    phase, group_id = action["phase"], action["group_id"]
    before = session.state.phases[phase - 1]["blocks"][-1]["before"]
    completed_before = len(session.state.completed_phases)
    console.say("")
    console.say(f"GROUP {group_id} is done. Now read the total again.")
    _wait_after_answers(session)
    total = _record_reading(
        session,
        "block_after",
        group_id,
        "Type the total spent right now (for example 20.23): ",
        floor=before,
        ceiling=before + MAX_BLOCK_DELTA,
    )
    console.say(f"This group cost ${Decimal(total) - before:.2f}. Running total now: ${total}.")
    if len(session.state.completed_phases) > completed_before:
        console.say(f"Phase {phase} is complete.")


def _offer_resume(session: Session) -> None:
    """A group left open by a quit or crash: continue it, or void it and redo it."""
    console = session.console
    group_id = session.state.current_block
    if group_id is None:
        return
    phase = session.state.current_phase
    console.say(RULE)
    console.say(f"Group {group_id} in phase {phase} was in progress when the wizard last stopped.")
    console.say("Press Enter to continue it where you left off (usually the right choice).")
    console.say("Or type V to void this group and redo it. Only this group is affected.")
    if console.ask("Enter to continue, or V to void: ").lower() != "v":
        return
    session.write("block_voided", group_id=group_id, reason=_ask_void_reason(console))
    console.say(f"Group {group_id} is voided. The wizard will run it again from the start.")


def _ask_void_reason(console: Console) -> str:
    console.say("Why are you voiding it?")
    console.say("  1 = interrupted (default)   2 = the chat was not new")
    console.say("  3 = the setup was wrong     4 = other")
    while True:
        choice = console.ask("Type 1, 2, 3 or 4 (Enter = 1): ")
        if choice == "":
            return "interrupted"
        if choice in VOID_CHOICES:
            return VOID_CHOICES[choice]
        console.say("Type 1, 2, 3 or 4.")


def _ask_desktop_line(console: Console, local_date: str) -> str:
    """The Claude Desktop daily line for a date, typed twice so a typo is caught."""
    prompt = f"What does the Claude Desktop line show for {local_date}? (for example 0.20): "
    first = _ask_reading(console, prompt)
    while True:
        again = _ask_reading(console, "Type the same amount again to confirm: ")
        if again == first:
            return first
        console.say("The two amounts differ. Start again.")
        first = _ask_reading(console, prompt)


def _reconcile_one(session: Session, local_date: str) -> None:
    console = session.console
    console.say("")
    console.say(THIN)
    console.say(f"CHECK DATE {local_date} against Claude Desktop")
    console.say(
        "In Settings -> Usage, open the daily breakdown by product and find the Claude Desktop line."
    )
    desktop = _ask_desktop_line(console, local_date)
    result = reconcile(session.state, local_date, desktop)
    session.write("day_reconciled", local_date=local_date, desktop_total=desktop)
    if result["passed"]:
        console.say(
            f"Matches. Your readings add up to ${result['measured_total']}; Claude Desktop shows "
            f"${result['desktop_total']}. The difference of ${result['difference']} is within the "
            f"allowed ${result['tolerance']}."
        )
    else:
        console.say(
            f"Does not match. Your readings add up to ${result['measured_total']}; Claude Desktop shows "
            f"${result['desktop_total']} (difference ${result['difference']}, allowed ${result['tolerance']})."
        )
        console.say("The groups on this date are left out of the results.")


def _reconcile_due(session: Session) -> int:
    """Check each date that is at least 4 hours past its last reading."""
    console = session.console
    if session.state.has_open_block() and session.state.pending_reconciliations():
        console.say("Finish the group in progress before checking a date.")
        return 0
    due, upcoming = session.reconcile_queue(console.clock())
    for local_date in due:
        _reconcile_one(session, local_date)
    for local_date, ready in upcoming:
        console.say(
            f"Date {local_date} can be checked after about {_clock_text(ready)} (local time), "
            "4 hours after its last reading."
        )
    return len(due)


def _midnight_warning(console: Console) -> None:
    now = console.clock().astimezone()
    midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    if midnight - now < REVIEW_DELAY:
        console.say("Less than 4 hours remain before local midnight.")
        console.say("You can pause and finish tomorrow: each date is checked separately.")


def _show_done(session: Session) -> None:
    console = session.console
    state = session.state
    console.say("")
    console.say(f"All {state.phase_count} phases are recorded.")
    for local_date in state.pending_reconciliations():
        ready = state.reconcile_ready_at(local_date)
        console.say(
            f"Check date {local_date} after about {_clock_text(ready)} (local time), "
            "when Claude Desktop's daily line shows. Then run:"
        )
        console.say("  " + _command("reconcile", session.state_dir))
    console.say("Then run the report:")
    console.say("  " + _command("report", session.state_dir))


def _next_step(session: Session) -> str:
    action = session.state.next_action()
    kind = action["action"]
    if kind == "start_study":
        return "Run init first."
    if kind == "start_phase":
        return f"Run: {_command('run', session.state_dir)}  (starts phase {action['phase']}, method {action['method']})"
    if kind == "done":
        return f"Run: {_command('report', session.state_dir)}"
    return f"Run: {_command('run', session.state_dir)}  (continues the phase in progress)"


def _phase_status(state: StudyState, number: int) -> str:
    if number in state.completed_phases:
        return "done"
    if any(phase["phase"] == number for phase in state.phases):
        return "in progress"
    return "not started"


# ----------------------------------------------------------------------------- commands


def cmd_init(args: argparse.Namespace, console: Console) -> int:
    pack_path = Path(args.pack)
    raw = _read_json_file(pack_path)
    if _has_example_questions(raw):
        console.say("This is the example template. Its questions start with EXAMPLE, so it")
        console.say("cannot be used for a study. Copy it, replace the study id, questions,")
        console.say("answers and file names with your own, then run init again.")
        return 1
    pack = validate_pack(raw)
    if "<" in pack["model_label"] or ">" in pack["model_label"]:
        console.say("Set model_label to the real model and effort you will use every day,")
        console.say('for example "Claude Opus at standard effort", then run init again.')
        return 1
    state_dir = Path(args.state_dir) if args.state_dir else default_state_dir(pack["study_id"])
    ledger = Ledger(state_dir / LEDGER_FILE, clock=console.clock)
    records = ledger.load()
    digest = pack_digest(pack)
    started = [record for record in records if record["type"] == "study_started"]
    if started:
        if started[0]["pack_digest"] != digest:
            console.say("This folder already holds a study made from a different pack.")
            console.say("Use another --state-dir, or the original pack file. Nothing was changed.")
            return 1
        console.say(f"This study is already set up in {state_dir}. Nothing was changed.")
        console.say("Plan:")
        console.say("\n".join(_plan_lines(started[0]["schedule"])))
        console.say("Next step: " + _command("run", state_dir))
        return 0

    fields = study_started_fields(pack, new_seed_hex())
    state_dir.mkdir(parents=True, exist_ok=True)
    _write_json(state_dir / PACK_FILE, pack)
    record = ledger.build("study_started", **fields)
    StudyState(pack).with_record(record)
    ledger.append_record(record)

    schedule = fields["schedule"]
    chats = sum(len(group["tasks"]) for slot in schedule for group in slot["groups"])
    console.say(f"Study {pack['study_id']} is set up.")
    console.say(f"Model setting for every phase: {pack['model_label']}")
    console.say(f"Your study folder: {state_dir}")
    console.say("It stays on this computer. Do not edit the files in it.")
    console.say("")
    console.say(
        f"The plan: {len(schedule)} phases, {chats} chats in total (every task once per method), "
        f"{_estimate_text(chats, len(schedule))} of work."
    )
    console.say("Each date is checked against Claude Desktop about 4 hours after its last reading.")
    console.say("\n".join(_plan_lines(schedule)))
    console.say("")
    console.say("Next step:")
    console.say("  " + _command("run", state_dir))
    return 0


def cmd_run(args: argparse.Namespace, console: Console) -> int:
    session = _open_session(args.state_dir, console)
    if session is None:
        return 1
    state = session.state
    console.say(
        f"Study {session.pack['study_id']}: {len(state.completed_phases)} of {state.phase_count} phases complete."
    )
    if not state.study_complete:
        _midnight_warning(console)
    _offer_resume(session)
    _reconcile_due(session)
    while True:
        action = session.state.next_action()
        kind = action["action"]
        if kind == "start_phase":
            _start_phase(session, action)
        elif kind == "read_before":
            _read_before(session, action)
        elif kind == "ask_task":
            _ask_task(session, action)
        elif kind == "read_after":
            _read_after(session, action)
        else:
            _show_done(session)
            return 0


def cmd_reconcile(args: argparse.Namespace, console: Console) -> int:
    session = _open_session(args.state_dir, console)
    if session is None:
        return 1
    if _reconcile_due(session) == 0 and not session.state.pending_reconciliations():
        console.say("No date with group readings is waiting to be checked.")
    return 0


def cmd_status(args: argparse.Namespace, console: Console) -> int:
    session = _open_session(args.state_dir, console)
    if session is None:
        return 1
    state = session.state
    console.say(f"Study {session.pack['study_id']}  |  {session.pack['model_label']}")
    console.say(
        f"Phases done: {len(state.completed_phases)} of {state.phase_count}.  "
        f"Groups recorded: {state.recorded_group_runs()} of {state.group_runs_total}."
    )
    for slot in state.schedule:
        console.say(
            f"  Phase {slot['phase']} of {state.phase_count}: method {slot['method']} ({slot['half']})  "
            f"{_phase_status(state, slot['phase'])}"
        )
    if state.warnings:
        console.say("Warnings (the groups still count):")
        for warning in state.warnings:
            reason = REASON_TEXT.get(warning["reason"], warning["reason"])
            console.say(f"  Phase {warning['phase']}, group {warning['group_id']}: {reason}")
    if state.voided_blocks:
        console.say(f"Voided and redone: {len(state.voided_blocks)} group(s).")
    now = console.clock()
    for local_date in state.pending_reconciliations():
        ready = state.reconcile_ready_at(local_date)
        if now >= ready and not state.has_open_block():
            console.say(f"Waiting for your Claude Desktop line: {local_date}")
        else:
            console.say(f"Not ready to check: {local_date} (after about {_clock_text(ready)}).")
    console.say("Next step: " + _next_step(session))
    return 0


def _report_lines(report: dict[str, Any]) -> list[str]:
    progress = report["progress"]
    lines = [
        f"Self-test report for {report['study_id']}  |  {report['model_label']}",
        f"Phases complete: {progress['phases_complete']} of {progress['phases_total']}.  "
        f"Groups in the results: {progress['group_runs_included']} of {progress['group_runs_total']}.",
        "",
        "Per method (groups in the results only):",
    ]
    for method, row in report["methods"].items():
        per = f"${row['cost_per_correct']}" if row["cost_per_correct"] is not None else "n/a"
        lines.append(
            f"  {method} ({METHOD_NAMES[method]}): {row['groups']} group(s), spend "
            f"${row['spend']}, correct {row['correct']} of {row['tasks_attempted']}, "
            f"cost per correct answer {per}"
        )
    primary = next(item for item in report["comparisons"] if item["primary"])
    decision = primary["decision"]
    interval = primary["interval"]
    lines += ["", "Main question: does B (ContextShrink normal) cost less than C_normal?"]
    lines.append("Verdict: " + CLAIM_TEXT.get(decision["claim"], decision["claim"]))
    if interval["estimate"] is not None:
        lines.append(
            f"  Average difference per group, B minus C_normal: {_dollars(interval['estimate'])} "
            f"(95% range {_dollars(interval['lower'])} to {_dollars(interval['upper'])}, "
            f"from {interval['units']} paired groups)."
        )
    for reason in decision["reasons"]:
        lines.append("  Why: " + REASON_TEXT.get(reason, reason))
    lines += ["", "Other comparisons:"]
    for item in report["comparisons"]:
        if item["primary"]:
            continue
        lines.append(
            f"  {item['candidate']} vs {item['reference']}: "
            f"{CLAIM_TEXT.get(item['decision']['claim'], item['decision']['claim'])}"
        )
    lines += ["", "Phase timing (UTC):"]
    for phase in report["phases"]:
        started = phase["started_utc"] or "not started"
        ended = phase["ended_utc"] or phase["status"].replace("_", " ")
        lines.append(
            f"  Phase {phase['phase']} ({phase['half']}, method {phase['method']}): "
            f"started {started}, ended {ended}"
        )
    lines += ["", "Dates:"]
    for item in report["dates"]:
        lines.append(
            f"  {item['local_date']}: {item['status']}, {item['included_group_runs']} of "
            f"{item['group_runs']} group run(s) in the results"
        )
    if report["warnings"]:
        lines += ["", "Warnings (the groups still count):"]
        for warning in report["warnings"]:
            reason = REASON_TEXT.get(warning["reason"], warning["reason"])
            lines.append(f"  Phase {warning['phase']}, group {warning['group_id']}: {reason}")
    if report["left_out"]:
        lines += ["", "Left out of the results:"]
        for item in report["left_out"]:
            lines.append(
                f"  Phase {item['phase']}, group {item['group_id']}: "
                f"{REASON_TEXT.get(item['reason'], item['reason'])}"
            )
    lines += ["", report["scope"]["statement"].capitalize() + "."]
    return lines


def cmd_report(args: argparse.Namespace, console: Console) -> int:
    session = _open_session(args.state_dir, console)
    if session is None:
        return 1
    report = analyze(session.pack, session.state)
    for line in _report_lines(report):
        console.say(line)
    if args.output:
        Path(args.output).write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        console.say("")
        console.say(f"Full report written to {args.output}.")
    return 0


_COMMANDS = {
    "init": cmd_init,
    "run": cmd_run,
    "reconcile": cmd_reconcile,
    "status": cmd_status,
    "report": cmd_report,
}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="self-test",
        description="Single-account self test for ContextShrink Desktop MCP. Type q at any "
        "prompt to stop safely.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="set up a study from a pack file")
    init.add_argument("--pack", required=True, help="path to your pack JSON file")
    init.add_argument("--state-dir", help="folder for the study record (default: LOCALAPPDATA)")
    for name, text in (
        ("run", "walk through the next step (resumable)"),
        ("reconcile", "check finished dates against Claude Desktop's daily line"),
        ("status", "show progress and the next step"),
    ):
        command = commands.add_parser(name, help=text)
        command.add_argument("--state-dir", help="the study folder printed by init")
    report = commands.add_parser("report", help="summarise the study")
    report.add_argument("--state-dir", help="the study folder printed by init")
    report.add_argument("--output", help="also write the full JSON report to this file")
    return parser


def main(argv: Sequence[str] | None = None, console: Console | None = None) -> int:
    args = _parser().parse_args(None if argv is None else list(argv))
    active = console if console is not None else Console()
    try:
        return _COMMANDS[args.command](args, active)
    except QuitRequested:
        active.say("")
        active.say("Stopped. Everything entered so far is saved.")
        active.say("Run the same command again to carry on.")
        return 0
    except KeyboardInterrupt:
        active.say("")
        active.say("Stopped. Everything entered so far is saved.")
        return 0
    except _Stop as stop:
        active.say(stop.message)
        return 1
    except SelfTestError as error:
        active.say(ERROR_TEXT.get(error.code, _default_error(error.code)))
        return 1
    except OSError as error:
        active.say(f"Could not read or write a file: {error.filename or error}")
        return 1


def _default_error(code: str) -> str:
    return f"The study rules did not allow that step ({code}). Nothing was saved."


if __name__ == "__main__":
    raise SystemExit(main())
