from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from horizon.desktop_mcp import self_test_cli as cli
from horizon.desktop_mcp.self_test import Ledger, answer_digest, parse_utc
from horizon.desktop_mcp.self_test_session import session_ledger

REPO = Path(__file__).resolve().parents[2]
TEMPLATE = REPO / "desktop" / "mcp" / "self-test" / "pack.template.json"
WRAPPER = REPO / "desktop" / "mcp" / "self_test.py"

# Local-aware noon, so every record's local date stays put while the fake clock moves.
START = datetime(2026, 10, 15, 12, 0).astimezone()
MARKER = "zebra-marker-7731"


def make_pack(study_id: str = "st_test_study", groups: int = 12) -> dict[str, Any]:
    groups_out = []
    tasks = []
    for number in range(1, groups + 1):
        task_id = f"t_q{number:02d}"
        groups_out.append({"group_id": f"g{number:02d}", "tasks": [task_id]})
        tasks.append(
            {
                "task_id": task_id,
                "source_files": [f"file_{number:02d}.txt"],
                "question": f"Question? ({task_id})",
                "answer": {"kind": "text_contains", "expected": "ok"},
            }
        )
    return {
        "schema_version": 1,
        "kind": "self_test_pack",
        "study_id": study_id,
        "model_label": "Test model at standard effort",
        "minimums": {"paired_units": 10, "groups": 8},
        "quality_tolerance": 1,
        "groups": groups_out,
        "tasks": tasks,
    }


class FakeClock:
    def __init__(self, start: datetime) -> None:
        self.now = start
        self.slept: list[float] = []

    def __call__(self) -> datetime:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now = self.now + timedelta(seconds=seconds)


def _tt(cents: int) -> str:
    return f"{cents // 100}.{cents % 100:02d}"


class Owner:
    """Answers every wizard prompt like a careful owner, with a simulated Claude Desktop meter.

    Each group adds ``delta`` cents, shown on the "after" reading. The reconciliation line
    is the true span, so the date matches.
    """

    def __init__(
        self,
        *,
        delta: int = 10,
        void_group: str | None = None,
        stop_before_phase: int | None = None,
    ) -> None:
        self.harness: Harness | None = None
        self.meter = 100000  # cents
        self.first: int | None = None
        self.delta = delta
        self.void_group = void_group
        self.stop_before_phase = stop_before_phase
        self.after_pending = False
        self.voided = False
        self.desktop = ""

    def _phase(self) -> int:
        matches = re.findall(r"Phase (\d+) of \d+: method", self.harness.text)
        return int(matches[-1])

    def _group(self) -> str:
        matches = re.findall(r"GROUP (g\d+)  \|  TASK", self.harness.text)
        return matches[-1]

    def __call__(self, prompt: str) -> str:
        if prompt.startswith("Type yes"):
            return "q" if self.stop_before_phase == self._phase() else "yes"
        if prompt.startswith("Type the total"):
            if self.after_pending:
                self.meter += self.delta
                self.after_pending = False
            if self.first is None:
                self.first = self.meter
            return _tt(self.meter)
        if prompt.startswith("Press Enter to enter the total"):
            self.after_pending = True
            return ""
        if prompt.startswith("Paste the line"):
            if self.void_group and not self.voided and self._group() == self.void_group:
                self.voided = True
                return "v"
            return "ANSWER: ok"
        if prompt.startswith("Type 1, 2, 3 or 4"):
            return "1"
        if prompt.startswith("What does the Claude Desktop line"):
            self.desktop = _tt(self.meter - (self.first or 0))
            return self.desktop
        if prompt.startswith("Type the same amount again"):
            return self.desktop
        return ""


class Harness:
    """Drives main() with scripted input (or a responder) and records all output."""

    def __init__(self, clock: FakeClock | None = None, platform: str = "linux") -> None:
        self.clock = clock if clock is not None else FakeClock(START)
        self.platform = platform
        self.out: list[str] = []
        self.prompts: list[str] = []
        self.inputs: list[str] = []
        self.responder: Any = None

    def run(self, argv: list[str], inputs: list[str] | None = None, responder: Any = None) -> int:
        self.inputs = list(inputs or [])
        self.responder = responder
        if responder is not None:
            responder.harness = self
        console = cli.Console(
            read=self._read,
            write=self._write,
            clock=self.clock,
            sleep=self.clock.sleep,
            platform=self.platform,
        )
        return cli.main(argv, console=console)

    def _read(self, prompt: str = "") -> str:
        self.prompts.append(prompt)
        if self.responder is not None:
            return self.responder(prompt)
        if not self.inputs:
            raise EOFError
        return self.inputs.pop(0)

    def _write(self, text: str = "") -> None:
        self.out.append(str(text))

    @property
    def text(self) -> str:
        return "\n".join(self.out)


def write_pack(directory: Path, pack: dict[str, Any]) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "my_pack.json"
    path.write_text(json.dumps(pack), encoding="utf-8")
    return path


def init_study(h: Harness, state: Path, pack: dict[str, Any] | None = None) -> Path:
    pack_path = write_pack(state.parent / "packs", pack or make_pack())
    assert h.run(["init", "--pack", str(pack_path), "--state-dir", str(state)]) == 0
    return state


def ledger_records(state: Path) -> list[dict[str, Any]]:
    return Ledger(state / "ledger.jsonl").load()


def _utc(text: str) -> datetime:
    return parse_utc(text)


# ---------------------------------------------------------------- init


def test_init_refuses_example_template(tmp_path: Path) -> None:
    h = Harness()
    code = h.run(["init", "--pack", str(TEMPLATE), "--state-dir", str(tmp_path / "st")])
    assert code == 1
    assert "EXAMPLE" in h.text
    assert not (tmp_path / "st" / "ledger.jsonl").exists()


def test_init_refuses_the_template_model_placeholder(tmp_path: Path) -> None:
    pack = json.loads(TEMPLATE.read_text(encoding="utf-8"))
    pack["study_id"] = "st_placeholder"
    for task in pack["tasks"]:
        task["question"] = task["question"].replace("EXAMPLE, replace before use:", "").strip()
    h = Harness()
    code = h.run(
        ["init", "--pack", str(write_pack(tmp_path, pack)), "--state-dir", str(tmp_path / "st")]
    )
    assert code == 1
    assert "model_label" in h.text
    assert not (tmp_path / "st" / "ledger.jsonl").exists()


def test_init_prints_eight_phases_and_the_chat_count(tmp_path: Path) -> None:
    h = Harness()
    state = init_study(h, tmp_path / "st")
    records = ledger_records(state)
    assert [r["type"] for r in records] == ["study_started"]
    assert len(records[0]["schedule"]) == 8
    assert "The plan: 8 phases, 48 chats in total" in h.text
    assert "Phase 1" in h.text and "Phase 8" in h.text
    assert str(state) in h.text
    assert "self-test run --state-dir" in h.text


def test_init_refuses_other_pack_in_same_folder(tmp_path: Path) -> None:
    state = init_study(Harness(), tmp_path / "st")
    before = len(ledger_records(state))
    h = Harness()
    other = make_pack()
    other["model_label"] = "Different model at high effort"
    other_path = write_pack(tmp_path / "other", other)
    assert h.run(["init", "--pack", str(other_path), "--state-dir", str(state)]) == 1
    assert "different pack" in h.text
    assert len(ledger_records(state)) == before


# ---------------------------------------------------------------- full run


def test_full_scripted_study_runs_all_phases_with_the_ninety_second_wait(tmp_path: Path) -> None:
    state = init_study(Harness(), tmp_path / "st")
    h = Harness()
    owner = Owner()
    assert h.run(["run", "--state-dir", str(state)], responder=owner) == 0
    records = ledger_records(state)
    counts = {t: sum(1 for r in records if r["type"] == t) for t in {r["type"] for r in records}}
    assert counts["phase_started"] == 8
    assert counts["block_before"] == 48 and counts["block_after"] == 48
    assert counts["task_answer"] == 48
    assert "All 8 phases are recorded." in h.text
    # Every group's "after" reading came at least 90 seconds after its last answer.
    last_answer = None
    for record in records:
        if record["type"] == "task_answer":
            last_answer = _utc(record["utc"])
        if record["type"] == "block_after":
            assert (_utc(record["utc"]) - last_answer).total_seconds() >= 90
    assert "Waiting 90 seconds" in h.text
    assert sum(h.clock.slept) >= 90 * 48


def test_phase_change_after_a_stop_resumes_at_the_new_phase(tmp_path: Path) -> None:
    state = init_study(Harness(), tmp_path / "st")
    owner = Owner(stop_before_phase=2)
    first = Harness()
    assert first.run(["run", "--state-dir", str(state)], responder=owner) == 0
    assert "Stopped" in first.text
    assert [r["type"] for r in ledger_records(state)].count("phase_started") == 1

    owner.stop_before_phase = None
    second = Harness()
    assert second.run(["run", "--state-dir", str(state)], responder=owner) == 0
    assert "Phase 2 of 8: method" in second.text
    schedule = ledger_records(state)[0]["schedule"]
    method = schedule[1]["method"]
    if method in ("A", "B"):
        assert "The ContextShrink check chat happens now" in second.text
        assert second.text.index("Phase 2 of 8") < second.text.index("check chat happens now")
    assert [r["phase"] for r in ledger_records(state) if r["type"] == "phase_started"] == [
        1,
        2,
        3,
        4,
        5,
        6,
        7,
        8,
    ]


def test_phase_screen_shows_the_setup_checklist_before_yes(tmp_path: Path) -> None:
    state = init_study(Harness(), tmp_path / "st")
    h = Harness()
    h.run(["run", "--state-dir", str(state)], responder=Owner(stop_before_phase=1))
    text = h.text
    assert text.index("Phase 1 of 8: method") < text.index("Step 2") < text.index("Set the model")
    assert text.index("Step 1") < text.index("Step 2")


def test_run_warns_when_less_than_four_hours_remain_before_midnight(tmp_path: Path) -> None:
    late = FakeClock(datetime(2026, 10, 15, 21, 0).astimezone())
    state = init_study(Harness(late), tmp_path / "st")
    h = Harness(late)
    h.run(["run", "--state-dir", str(state)], responder=Owner(stop_before_phase=1))
    assert "Less than 4 hours remain before local midnight" in h.text
    assert "each date is checked separately" in h.text


def test_no_midnight_warning_at_noon(tmp_path: Path) -> None:
    state = init_study(Harness(), tmp_path / "st")
    h = Harness()
    h.run(["run", "--state-dir", str(state)], responder=Owner(stop_before_phase=1))
    assert "Less than 4 hours" not in h.text


def test_void_in_the_run_is_redone_straight_away(tmp_path: Path) -> None:
    state = init_study(Harness(), tmp_path / "st")
    h = Harness()
    assert h.run(["run", "--state-dir", str(state)], responder=Owner(void_group="g05")) == 0
    records = ledger_records(state)
    voided = [r for r in records if r["type"] == "block_voided"]
    assert len(voided) == 1 and voided[0]["group_id"] == "g05"
    assert "is voided and left out. It is redone straight away." in h.text
    assert "All 8 phases are recorded." in h.text


# ---------------------------------------------------------------- reconcile


def test_reconcile_waits_four_hours_after_the_last_reading(tmp_path: Path) -> None:
    clock = FakeClock(START)
    state = init_study(Harness(clock), tmp_path / "st")
    owner = Owner()
    Harness(clock).run(["run", "--state-dir", str(state)], responder=owner)
    last_reading = max(_utc(r["utc"]) for r in ledger_records(state) if r["type"] == "block_after")

    clock.now = last_reading + timedelta(hours=3, minutes=59)
    early = Harness(clock)
    assert early.run(["reconcile", "--state-dir", str(state)], []) == 0
    assert "can be checked after about" in early.text
    assert not any(r["type"] == "day_reconciled" for r in ledger_records(state))

    clock.now = last_reading + timedelta(hours=4, minutes=1)
    late = Harness(clock)
    assert late.run(["reconcile", "--state-dir", str(state)], responder=owner) == 0
    assert "Matches." in late.text
    reconciled = [r for r in ledger_records(state) if r["type"] == "day_reconciled"]
    assert len(reconciled) == 1

    again = Harness(clock)
    again.run(["reconcile", "--state-dir", str(state)], [])
    assert not any("Claude Desktop line" in p for p in again.prompts)


def test_a_mismatched_date_is_reported_and_recorded(tmp_path: Path) -> None:
    clock = FakeClock(START)
    state = init_study(Harness(clock), tmp_path / "st")
    owner = Owner()
    Harness(clock).run(["run", "--state-dir", str(state)], responder=owner)
    last_reading = max(_utc(r["utc"]) for r in ledger_records(state) if r["type"] == "block_after")
    clock.now = last_reading + timedelta(hours=4, minutes=1)
    h = Harness(clock)
    h.run(
        ["reconcile", "--state-dir", str(state)],
        responder=lambda p: "9.99" if ("line show" in p or "same amount" in p) else owner(p),
    )
    assert "Does not match." in h.text
    assert "left out of the results" in h.text


# ---------------------------------------------------------------- scripted details


def test_answer_text_never_reaches_the_ledger(tmp_path: Path) -> None:
    state = init_study(Harness(), tmp_path / "st")
    h = Harness()
    line = f"ANSWER: ok {MARKER}"
    inputs = ["1000.00", "yes", "1000.00", "just ok", line, "", "1000.10"]
    h.run(["run", "--state-dir", str(state)], inputs)
    assert "should start with ANSWER" in h.text
    raw = (state / "ledger.jsonl").read_text(encoding="utf-8")
    assert MARKER not in raw
    assert "just ok" not in raw
    answers = [r for r in ledger_records(state) if r["type"] == "task_answer"]
    assert answers[0]["correct"] is True
    assert answers[0]["answer_sha256"] == answer_digest(line)


def test_wrong_answer_is_recorded_as_incorrect(tmp_path: Path) -> None:
    state = init_study(Harness(), tmp_path / "st")
    h = Harness()
    h.run(["run", "--state-dir", str(state)], ["1000.00", "yes", "1000.00", "ANSWER: nope"])
    answers = [r for r in ledger_records(state) if r["type"] == "task_answer"]
    assert answers[0]["correct"] is False
    assert "Not correct." in h.text


def test_clipboard_offer_calls_clip_only_when_c_is_pressed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = init_study(Harness(), tmp_path / "st")
    calls: list[tuple[Any, dict[str, Any]]] = []

    def fake_run(args: Any, **kwargs: Any) -> None:
        calls.append((args, kwargs))

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    h = Harness(platform="win32")
    inputs = ["1000.00", "yes", "1000.00", "c", "ANSWER: ok", "", "1000.10"]
    h.run(["run", "--state-dir", str(state)], inputs)
    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args == ["clip"]
    assert kwargs.get("shell") is not True
    text = kwargs["input"].decode("utf-16-le")[1:]
    assert "Question? (t_q" in text
    assert text.endswith("gives only the final answer.")
    assert "Copied to the clipboard." in h.text


def test_quit_at_any_prompt_saves_what_was_entered(tmp_path: Path) -> None:
    state = init_study(Harness(), tmp_path / "st")
    h = Harness()
    assert h.run(["run", "--state-dir", str(state)], ["1000.00"]) == 0
    assert "Stopped. Everything entered so far is saved." in h.text
    assert ledger_records(state)[-1]["type"] == "study_started"


def test_resume_can_void_an_open_group(tmp_path: Path) -> None:
    state = init_study(Harness(), tmp_path / "st")
    Harness().run(["run", "--state-dir", str(state)], ["1000.00", "yes", "1000.00", "ANSWER: ok"])
    second = Harness()
    second.run(["run", "--state-dir", str(state)], ["v", "", "1000.00"])
    assert "was in progress" in second.text
    voided = [r for r in ledger_records(state) if r["type"] == "block_voided"]
    assert len(voided) == 1


# ---------------------------------------------------------------- status and dispatch


def test_status_shows_phases_and_the_next_step(tmp_path: Path) -> None:
    state = init_study(Harness(), tmp_path / "st")
    h = Harness()
    assert h.run(["status", "--state-dir", str(state)]) == 0
    assert "Phases done: 0 of 8" in h.text
    assert "Phase 1 of 8" in h.text and "not started" in h.text
    assert "Next step:" in h.text


def _run_module(args: list[str]) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "PYTHONPATH": str(REPO)}
    return subprocess.run(
        [sys.executable, "-m", "horizon.desktop_mcp", "self-test", *args],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_python_m_dispatches_self_test_status(tmp_path: Path) -> None:
    pack_path = write_pack(tmp_path / "packs", make_pack(study_id="st_dispatch"))
    state = tmp_path / "st"
    init = _run_module(["init", "--pack", str(pack_path), "--state-dir", str(state)])
    assert init.returncode == 0, init.stdout + init.stderr
    status = _run_module(["status", "--state-dir", str(state)])
    assert status.returncode == 0, status.stdout + status.stderr
    assert "Phases done: 0 of 8" in status.stdout


def test_python_m_self_test_reports_a_missing_study(tmp_path: Path) -> None:
    result = _run_module(["status", "--state-dir", str(tmp_path / "missing")])
    assert result.returncode == 1
    assert "No study was found" in result.stdout


def test_development_wrapper_runs_the_same_cli(tmp_path: Path) -> None:
    pack_path = write_pack(tmp_path / "packs", make_pack(study_id="st_wrapper"))
    state = tmp_path / "st"
    env = {**os.environ, "PYTHONPATH": str(REPO)}
    init = subprocess.run(
        [sys.executable, str(WRAPPER), "init", "--pack", str(pack_path), "--state-dir", str(state)],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert init.returncode == 0, init.stdout + init.stderr
    assert "The plan: 8 phases" in init.stdout


def test_subset_study_runs_four_phases_and_reports_only_b_and_c_normal(tmp_path: Path) -> None:
    pack = make_pack()
    pack["methods"] = ["B", "C_normal"]
    state = init_study(Harness(), tmp_path / "st", pack)
    h = Harness()
    assert h.run(["run", "--state-dir", str(state)], responder=Owner()) == 0
    records = ledger_records(state)
    counts = {t: sum(1 for r in records if r["type"] == t) for t in {r["type"] for r in records}}
    # Two methods on two halves of six groups each: 24 group runs, one chat per task each.
    assert counts["phase_started"] == 4
    assert counts["block_before"] == 24 and counts["block_after"] == 24
    assert counts["task_answer"] == 24
    assert "All 4 phases are recorded." in h.text

    report = Harness()
    assert report.run(["report", "--state-dir", str(state)]) == 0
    assert "Phases complete: 4 of 4." in report.text
    assert "Groups in the results: 0 of 24." in report.text
    assert "B (ContextShrink, normal): 0 group(s)" in report.text
    assert "C_normal (connector off, files attached): 0 group(s)" in report.text
    assert "(ContextShrink, compression off)" not in report.text
    assert "C_tuned" not in report.text
    assert "B vs A" not in report.text
    assert "Main question: does B (ContextShrink normal) cost less than C_normal?" in report.text


# ---------------------------------------------------------------- long-session study (CLI)


def session_records(state: Path) -> list[dict[str, Any]]:
    return session_ledger(state / "ledger.jsonl").load()


def make_session_pack(study_id: str = "st_session_cli") -> dict[str, Any]:
    docs = [
        "orders_export.json",
        "service_events.txt",
        "operations_handbook.md",
        "platform_config.json",
    ]
    tasks = [
        {
            "task_id": f"t_s{number:02d}",
            "source_files": [docs[(number - 1) % 4]],
            "question": f"Session question {number}?",
            "answer": {"kind": "text_contains", "expected": "ok"},
        }
        for number in range(1, 13)
    ]
    return {
        "schema_version": 1,
        "kind": "self_test_session_pack",
        "study_id": study_id,
        "model_label": "Test model at standard effort",
        "quality_tolerance": 1,
        "methods": ["B", "C_normal"],
        "documents": docs,
        "tasks": tasks,
    }


class SessionOwner:
    """Answers the session wizard's prompts with a simulated Claude Desktop meter.

    Chat spend is ``cost`` cents per method. ``quit_after`` types q once that many answers
    are in. ``void`` is a (method, question) pair voided once, after half the chat's spend.
    The desktop line is the measured span plus ``adjust`` cents.
    """

    def __init__(
        self,
        *,
        cost: dict[str, int] | None = None,
        wrong: Any = None,
        quit_after: int | None = None,
        void: tuple[str, int] | None = None,
        adjust: int = 0,
    ) -> None:
        self.harness: Harness | None = None
        self.cost = cost or {"B": 1200, "C_normal": 1500}
        self.wrong = wrong
        self.quit_after = quit_after
        self.void = void
        self.adjust = adjust
        self.meter = 100000
        self.first: int | None = None
        self.answers = 0
        self.voided = False
        self.desktop = ""

    def _method(self) -> str:
        labels = re.findall(r"connector (ON|OFF)", self.harness.text)
        return "B" if labels[-1] == "ON" else "C_normal"

    def _question(self) -> int:
        return int(re.findall(r"QUESTION (\d+) of", self.harness.text)[-1])

    def __call__(self, prompt: str) -> str:
        if prompt.startswith("Type yes"):
            return "yes"
        if prompt.startswith("Type the total spent right now"):
            if self.first is None:
                self.first = self.meter
            return _tt(self.meter)
        if prompt.startswith("Read the total in Settings"):
            self.meter += self.cost[self._method()]
            return _tt(self.meter)
        if prompt.startswith("Paste the line that starts"):
            method, number = self._method(), self._question()
            if self.quit_after is not None and self.answers >= self.quit_after:
                return "q"
            if self.void == (method, number) and not self.voided:
                self.voided = True
                self.meter += self.cost[method] // 2
                return "v"
            self.answers += 1
            return "ANSWER: nope" if self.wrong and self.wrong(method, number) else "ANSWER: ok"
        if prompt.startswith("Enter to continue"):
            return ""
        if prompt.startswith("Type 1, 2, 3 or 4"):
            return "1"
        if prompt.startswith("What does the Claude Desktop line"):
            self.desktop = _tt(self.meter - (self.first or 0) + self.adjust)
            return self.desktop
        if prompt.startswith("Type the same amount again"):
            return self.desktop
        return ""


def init_session(h: Harness, state: Path, pack: dict[str, Any] | None = None) -> Path:
    pack_path = write_pack(state.parent / "packs", pack or make_session_pack())
    assert h.run(["init", "--pack", str(pack_path), "--state-dir", str(state)]) == 0
    return state


def _last_reading(state: Path) -> datetime:
    return max(_utc(r["utc"]) for r in session_records(state) if r["type"] == "chat_ended")


def test_session_init_dispatches_on_the_pack_kind(tmp_path: Path) -> None:
    h = Harness()
    state = init_session(h, tmp_path / "st")
    records = session_records(state)
    assert [r["type"] for r in records] == ["study_started"]
    assert len(records[0]["order"]) == 2 and "schedule" not in records[0]
    assert "2 chats of 12 questions each" in h.text
    assert "Chat 1:" in h.text and "Chat 2:" in h.text
    status = Harness()
    assert status.run(["status", "--state-dir", str(state)]) == 0
    assert "Chats complete: 0 of 2." in status.text
    assert "Next step:" in status.text


def test_session_init_refuses_a_different_pack_in_the_same_folder(tmp_path: Path) -> None:
    state = init_session(Harness(), tmp_path / "st")
    other = make_session_pack()
    other["model_label"] = "Another model at high effort"
    h = Harness()
    code = h.run(
        ["init", "--pack", str(write_pack(tmp_path / "o", other)), "--state-dir", str(state)]
    )
    assert code == 1
    assert "different pack" in h.text
    assert len(session_records(state)) == 1


def test_session_full_run_reconcile_and_report_end_to_end(tmp_path: Path) -> None:
    clock = FakeClock(START)
    state = init_session(Harness(clock), tmp_path / "st")
    owner = SessionOwner()
    h = Harness(clock)
    assert h.run(["run", "--state-dir", str(state)], responder=owner) == 0
    records = session_records(state)
    counts = {t: sum(1 for r in records if r["type"] == t) for t in {r["type"] for r in records}}
    assert counts["chat_started"] == 2 and counts["chat_ended"] == 2
    assert counts["task_answer"] == 24
    assert "Both chats are recorded (2 of 2)." in h.text
    assert "Waiting" not in h.text
    assert clock.slept == []
    assert "Next: question 1, in a NEW chat." in h.text
    assert "Attach ALL these files" in h.text

    clock.now = _last_reading(state) + timedelta(hours=4, minutes=1)
    done = Harness(clock)
    assert done.run(["reconcile", "--state-dir", str(state)], responder=owner) == 0
    assert "Matches." in done.text
    assert any(r["type"] == "day_reconciled" for r in session_records(state))

    report_path = tmp_path / "report.json"
    report = Harness(clock)
    assert report.run(["report", "--state-dir", str(state), "--output", str(report_path)]) == 0
    assert "Verdict: B cost less than C_normal in this session." in report.text
    assert "Reconciliation: passed." in report.text
    data = json.loads(report_path.read_text(encoding="utf-8"))
    assert data["kind"] == "self_test_session_report"
    assert data["verdict"] == "savings_observed"
    assert data["progress"]["chats_complete"] == 2
    assert data["methods"]["B"]["spend"] == "12.00"
    assert data["methods"]["C_normal"]["spend"] == "15.00"


def test_session_void_then_redo_in_a_new_chat(tmp_path: Path) -> None:
    clock = FakeClock(START)
    state = init_session(Harness(clock), tmp_path / "st")
    first_method = session_records(state)[0]["order"][0]
    h = Harness(clock)
    owner = SessionOwner(void=(first_method, 5))
    assert h.run(["run", "--state-dir", str(state)], responder=owner) == 0
    records = session_records(state)
    voided = [r for r in records if r["type"] == "chat_voided"]
    assert len(voided) == 1 and voided[0]["method"] == first_method
    assert voided[0]["reason"] == "interrupted"
    assert sum(1 for r in records if r["type"] == "chat_started") == 3
    assert "is voided and its answers are discarded." in h.text
    assert "Both chats are recorded (2 of 2)." in h.text

    clock.now = _last_reading(state) + timedelta(hours=4, minutes=1)
    assert Harness(clock).run(["reconcile", "--state-dir", str(state)], responder=owner) == 0
    report = Harness(clock)
    assert report.run(["report", "--state-dir", str(state)]) == 0
    assert "Chats complete: 2 of 2." in report.text
    assert "Verdict: B cost less than C_normal in this session." in report.text


def test_session_quit_mid_chat_resumes_at_the_next_question(tmp_path: Path) -> None:
    clock = FakeClock(START)
    state = init_session(Harness(clock), tmp_path / "st")
    first = Harness(clock)
    assert first.run(["run", "--state-dir", str(state)], responder=SessionOwner(quit_after=4)) == 0
    assert "Stopped. Everything entered so far is saved." in first.text
    assert sum(1 for r in session_records(state) if r["type"] == "task_answer") == 4

    second = Harness(clock)
    assert second.run(["run", "--state-dir", str(state)], responder=SessionOwner()) == 0
    assert (
        "was in progress when the wizard last stopped (4 of 12 questions answered)" in second.text
    )
    assert "Both chats are recorded (2 of 2)." in second.text
    records = session_records(state)
    assert sum(1 for r in records if r["type"] == "task_answer") == 24
    assert sum(1 for r in records if r["type"] == "chat_started") == 2
    assert not any(r["type"] == "chat_voided" for r in records)


def test_session_reconcile_mismatch_gives_no_verdict(tmp_path: Path) -> None:
    clock = FakeClock(START)
    state = init_session(Harness(clock), tmp_path / "st")
    owner = SessionOwner(adjust=10)
    assert Harness(clock).run(["run", "--state-dir", str(state)], responder=owner) == 0
    clock.now = _last_reading(state) + timedelta(hours=4, minutes=1)
    checked = Harness(clock)
    assert checked.run(["reconcile", "--state-dir", str(state)], responder=owner) == 0
    assert "Does not match." in checked.text
    assert "the report will give no verdict" in checked.text
    report = Harness(clock)
    assert report.run(["report", "--state-dir", str(state)]) == 0
    assert "Verdict: No verdict yet." in report.text
    assert "Why: a date did not match Claude Desktop" in report.text


def test_session_report_before_the_end_says_what_is_missing(tmp_path: Path) -> None:
    clock = FakeClock(START)
    state = init_session(Harness(clock), tmp_path / "st")
    Harness(clock).run(["run", "--state-dir", str(state)], responder=SessionOwner(quit_after=0))
    report = Harness(clock)
    assert report.run(["report", "--state-dir", str(state)]) == 0
    assert "Why: both chats are not finished yet" in report.text
    assert "not in the results yet" in report.text


# ---------------------------------------------------------------- kit builder


def _load_build_kit() -> Any:
    import importlib.util

    path = REPO / "desktop" / "mcp" / "self-test" / "build_kit.py"
    spec = importlib.util.spec_from_file_location("build_kit_under_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_kit_defaults_to_the_session_profile_and_keeps_the_single_kit(tmp_path: Path) -> None:
    kit = _load_build_kit()
    assert kit.DEFAULT_PROFILE == "session"
    session = kit.dataset_args("session", 7, tmp_path)
    assert session[session.index("--profile") + 1] == "session"
    assert session[session.index("--study-id") + 1] == "st_session_v1"
    assert "--methods" not in session
    single = kit.dataset_args("single", 7, tmp_path)
    assert "--profile" not in single
    assert single[single.index("--study-id") + 1] == "st_generated_v2"
    assert single[single.index("--methods") + 1] == "B,C_normal"
    # No seed given: each profile keeps its own default, the dataset the tests check.
    assert "--seed" not in kit.dataset_args("session", None, tmp_path)
    assert "--seed" not in kit.dataset_args("single", None, tmp_path)


def test_session_guide_covers_the_two_chats_the_check_and_the_documents() -> None:
    kit = _load_build_kit()
    text = kit.GUIDE_SESSION.format(version="0.1.5", model_label="Test model", tasks="12")
    assert "{" not in text
    assert "two chats" in text
    assert "12 questions" in text
    assert "1 to 1.5 hours" in text
    assert "about 4 hours" in text and "after your last" in text
    assert "attach all four documents" in text and "first message only" in text
    assert "Approve the" in text and "for ContextShrink" in text
    assert "Start Self Test.cmd" in text


def test_single_guide_text_is_unchanged_apart_from_its_name() -> None:
    kit = _load_build_kit()
    assert "Measures, on your own Claude Enterprise account" in kit.GUIDE_SINGLE
    assert "24 chats" not in kit.GUIDE_SESSION
