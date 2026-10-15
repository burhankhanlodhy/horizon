from __future__ import annotations

import importlib.util
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from horizon.desktop_mcp.self_test import Ledger, answer_digest

REPO = Path(__file__).resolve().parents[2]
CLI_PATH = REPO / "desktop" / "mcp" / "self_test.py"
TEMPLATE = REPO / "desktop" / "mcp" / "self-test" / "pack.template.json"

_spec = importlib.util.spec_from_file_location("desktop_self_test_cli", CLI_PATH)
assert _spec is not None and _spec.loader is not None
cli = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cli)

# Local-aware noon, so the local date stays put while the fake clock moves by hours.
START = datetime(2026, 10, 15, 12, 0).astimezone()
MARKER = "zebra-marker-7731"


def make_pack(study_id: str = "st_test_study", question: str = "Question?") -> dict[str, Any]:
    groups = []
    tasks = []
    for number in range(1, 5):
        ids = [f"t_g{number}_a", f"t_g{number}_b"]
        groups.append({"group_id": f"g{number}", "tasks": ids})
        for task_id in ids:
            tasks.append(
                {
                    "task_id": task_id,
                    "source_files": [f"file_{number}.txt"],
                    "question": f"{question} ({task_id})",
                    "answer": {"kind": "text_contains", "expected": "ok"},
                }
            )
    return {
        "schema_version": 1,
        "kind": "self_test_pack",
        "study_id": study_id,
        "model_label": "Test model at standard effort",
        "minimums": {"paired_units": 4, "groups": 3},
        "quality_tolerance": 1,
        "groups": groups,
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


class Harness:
    """Drives main() with scripted input and records every prompt and line of output."""

    def __init__(self, clock: FakeClock | None = None, platform: str = "linux") -> None:
        self.clock = clock if clock is not None else FakeClock(START)
        self.platform = platform
        self.out: list[str] = []
        self.prompts: list[str] = []
        self.inputs: list[str] = []

    def run(self, argv: list[str], inputs: list[str] | None = None) -> int:
        self.inputs = list(inputs or [])
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


def day_inputs(start: str = "1000.00", groups: int = 4, answers=("ANSWER: ok", "ANSWER: ok")):
    """Inputs for one full day with every answer correct and +0.05 per group."""
    inputs = ["yes"]
    total = Decimal(start)
    for _ in range(groups):
        inputs.append(f"{total:.2f}")  # before
        inputs.extend(answers)  # one answer per task
        inputs.append("")  # Enter after the countdown
        total += Decimal("0.05")
        inputs.append(f"{total:.2f}")  # after
    return inputs


def finish_time(state: Path) -> datetime:
    record = [r for r in ledger_records(state) if r["type"] == "day_finished"][-1]
    return datetime.strptime(record["utc"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def test_init_refuses_example_template(tmp_path: Path) -> None:
    h = Harness()
    code = h.run(["init", "--pack", str(TEMPLATE), "--state-dir", str(tmp_path / "st")])
    assert code == 1
    assert "EXAMPLE" in h.text
    assert not (tmp_path / "st" / "ledger.jsonl").exists()


def test_init_accepts_real_pack_and_prints_plan(tmp_path: Path) -> None:
    h = Harness()
    state = tmp_path / "st"
    init_study(h, state)
    records = ledger_records(state)
    assert [r["type"] for r in records] == ["study_started"]
    assert len(records[0]["schedule"]) == 8
    assert (state / "pack.json").exists()
    assert "Day 1" in h.text and "Day 8" in h.text
    assert str(state) in h.text
    assert "run --state-dir" in h.text


def test_init_refuses_other_pack_in_same_folder(tmp_path: Path) -> None:
    state = tmp_path / "st"
    init_study(Harness(), state)
    before = len(ledger_records(state))
    h = Harness()
    other = write_pack(tmp_path / "other", make_pack(question="Different?"))
    assert h.run(["init", "--pack", str(other), "--state-dir", str(state)]) == 1
    assert "different pack" in h.text
    assert len(ledger_records(state)) == before


def test_full_scripted_day_writes_expected_records(tmp_path: Path) -> None:
    h = Harness()
    state = init_study(h, tmp_path / "st")
    h2 = Harness()
    assert h2.run(["run", "--state-dir", str(state)], day_inputs()) == 0
    kinds = [r["type"] for r in ledger_records(state)]
    expected_group = ["block_before", "task_answer", "task_answer", "block_after"]
    assert kinds == ["study_started", "day_started"] + expected_group * 4 + ["day_finished"]
    befores = [r["total"] for r in ledger_records(state) if r["type"] == "block_before"]
    afters = [r["total"] for r in ledger_records(state) if r["type"] == "block_after"]
    assert befores == ["1000.00", "1000.05", "1000.10", "1000.15"]
    assert afters == ["1000.05", "1000.10", "1000.15", "1000.20"]
    answers = [r for r in ledger_records(state) if r["type"] == "task_answer"]
    assert all(r["correct"] is True for r in answers)
    assert "This group cost $0.05" in h2.text
    assert "reconcile" in h2.text
    assert "clipboard" not in " ".join(h2.prompts)  # not offered off Windows
    assert sum(h2.clock.slept) >= 120
    assert "Waiting 120 seconds" in h2.text


def test_bad_reading_format_and_decrease_are_asked_again(tmp_path: Path) -> None:
    state = init_study(Harness(), tmp_path / "st")
    h = Harness()
    inputs = ["yes", "12.5", "1000.00", "ANSWER: ok", "ANSWER: ok", "", "999.00", "1000.10"]
    h.run(["run", "--state-dir", str(state)], inputs)
    assert "exactly two decimals" in h.text
    assert "lower than the total before" in h.text
    records = ledger_records(state)
    before = [r for r in records if r["type"] == "block_before"][0]
    after = [r for r in records if r["type"] == "block_after"][0]
    assert before["total"] == "1000.00"
    assert after["total"] == "1000.10"
    assert "This group cost $0.10" in h.text


def test_quit_mid_block_then_resume_offers_void_and_redo(tmp_path: Path) -> None:
    state = init_study(Harness(), tmp_path / "st")
    first = Harness()
    assert first.run(["run", "--state-dir", str(state)], ["yes", "1000.00", "ANSWER: ok"]) == 0
    assert "Stopped" in first.text
    assert [r["type"] for r in ledger_records(state)][-2:] == ["block_before", "task_answer"]

    second = Harness()
    inputs = ["v", "", "1000.00", "ANSWER: ok", "ANSWER: ok"]
    second.run(["run", "--state-dir", str(state)], inputs)
    assert "was in progress" in second.text
    records = ledger_records(state)
    voided = [r for r in records if r["type"] == "block_voided"]
    assert len(voided) == 1
    assert voided[0]["group_id"] == records[2]["group_id"]
    assert voided[0]["reason"] == "interrupted"
    assert [r["type"] for r in records].count("block_before") == 2


def test_resume_can_continue_without_voiding(tmp_path: Path) -> None:
    state = init_study(Harness(), tmp_path / "st")
    Harness().run(["run", "--state-dir", str(state)], ["yes", "1000.00", "ANSWER: ok"])
    h = Harness()
    h.run(["run", "--state-dir", str(state)], ["", "ANSWER: ok"])
    assert not [r for r in ledger_records(state) if r["type"] == "block_voided"]
    assert [r["type"] for r in ledger_records(state)].count("task_answer") == 2


def test_one_day_per_date_is_refused(tmp_path: Path) -> None:
    state = init_study(Harness(), tmp_path / "st")
    Harness().run(["run", "--state-dir", str(state)], day_inputs())
    before = len(ledger_records(state))
    h = Harness()
    assert h.run(["run", "--state-dir", str(state)], []) == 0
    assert "already have a measurement day" in h.text
    assert not any("Type yes" in prompt for prompt in h.prompts)
    assert len(ledger_records(state)) == before


def test_reconcile_prompt_only_after_four_hours(tmp_path: Path) -> None:
    clock = FakeClock(START)
    state = init_study(Harness(clock), tmp_path / "st")
    Harness(clock).run(["run", "--state-dir", str(state)], day_inputs())
    done_at = finish_time(state)

    clock.now = done_at + timedelta(hours=3, minutes=59)
    early = Harness(clock)
    assert early.run(["reconcile", "--state-dir", str(state)], []) == 0
    assert not any("Claude Desktop line" in p for p in early.prompts)
    assert "not ready" in early.text

    clock.now = done_at + timedelta(hours=4, minutes=1)
    late = Harness(clock)
    assert late.run(["reconcile", "--state-dir", str(state)], ["0.20"]) == 0
    assert any("Claude Desktop line" in p for p in late.prompts)
    assert "Matches" in late.text
    reconciled = [r for r in ledger_records(state) if r["type"] == "day_reconciled"]
    assert [r["desktop_total"] for r in reconciled] == ["0.20"]

    again = Harness(clock)
    again.run(["reconcile", "--state-dir", str(state)], [])
    assert not any("Claude Desktop line" in p for p in again.prompts)


def test_status_and_report_text_and_json(tmp_path: Path) -> None:
    clock = FakeClock(START)
    state = init_study(Harness(clock), tmp_path / "st")
    Harness(clock).run(["run", "--state-dir", str(state)], day_inputs())

    status = Harness(clock)
    assert status.run(["status", "--state-dir", str(state)]) == 0
    assert "Days done: 1 of 8" in status.text
    assert "Days remaining: 7" in status.text
    assert "Next step:" in status.text

    out = tmp_path / "report.json"
    report = Harness(clock)
    assert report.run(["report", "--state-dir", str(state), "--output", str(out)]) == 0
    assert "Verdict:" in report.text
    assert "Days left out:" in report.text
    assert "Results apply to this account, model setting and task set only." in report.text
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["kind"] == "self_test_report"
    assert data["progress"]["days_complete"] == 1


def test_clipboard_offer_calls_clip_only_when_c_is_pressed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = init_study(Harness(), tmp_path / "st")
    calls: list[tuple[Any, dict[str, Any]]] = []

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    h = Harness(platform="win32")
    inputs = ["yes", "1000.00", "c", "ANSWER: ok", "", "ANSWER: ok"]
    h.run(["run", "--state-dir", str(state)], inputs)
    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args == ["clip"]
    assert kwargs.get("shell") is not True
    text = kwargs["input"].decode("utf-16-le")[1:]
    # Day 1's method is random per study, so do not depend on the A/B or C wording.
    assert "Question? (t_g" in text
    assert text.endswith("gives only the final answer.")
    assert "Copied to the clipboard." in h.text


def test_clipboard_not_called_when_enter_is_pressed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = init_study(Harness(), tmp_path / "st")
    calls: list[Any] = []
    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **k: calls.append(a))
    h = Harness(platform="win32")
    h.run(["run", "--state-dir", str(state)], ["yes", "1000.00", "", "ANSWER: ok", ""])
    assert calls == []
    assert any("Press C then Enter" in prompt for prompt in h.prompts)


def test_answer_text_never_reaches_the_ledger(tmp_path: Path) -> None:
    state = init_study(Harness(), tmp_path / "st")
    h = Harness()
    line = f"ANSWER: ok {MARKER}"
    inputs = ["yes", "1000.00", "just ok", line, ""]
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
    h.run(["run", "--state-dir", str(state)], ["yes", "1000.00", "ANSWER: nope"])
    answers = [r for r in ledger_records(state) if r["type"] == "task_answer"]
    assert answers[0]["correct"] is False
    assert "Not correct." in h.text


def test_init_refuses_the_template_model_placeholder(tmp_path: Path) -> None:
    pack = json.loads(TEMPLATE.read_text(encoding="utf-8"))
    pack["study_id"] = "st_placeholder"
    for task in pack["tasks"]:
        task["question"] = task["question"].replace("EXAMPLE, replace before use:", "").strip()
    assert "<" in pack["model_label"]
    h = Harness()
    code = h.run(
        ["init", "--pack", str(write_pack(tmp_path, pack)), "--state-dir", str(tmp_path / "st")]
    )
    assert code == 1
    assert "model_label" in h.text
    assert not (tmp_path / "st" / "ledger.jsonl").exists()
