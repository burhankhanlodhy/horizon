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
        matches = re.findall(r"Phase (\d) of 8: method", self.harness.text)
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
