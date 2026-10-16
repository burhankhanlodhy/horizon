from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from horizon.desktop_mcp.answer_contract import instruction_pack
from horizon.desktop_mcp.self_test import (
    METHODS,
    PHASE_COUNT,
    Ledger,
    SelfTestError,
    StudyState,
    analyze,
    answer_digest,
    build_schedule,
    check_answer,
    default_state_dir,
    method_setup,
    new_seed_hex,
    pack_digest,
    pilot_changed,
    reconcile,
    render_prompt,
    study_started_fields,
    validate_pack,
)

REPO = Path(__file__).resolve().parents[2]
TEMPLATE = REPO / "desktop" / "mcp" / "self-test" / "pack.template.json"
SEED = "ab" * 32
OTHER_SEED = "cd" * 32
BASE = datetime(2026, 10, 15, 9, 0, tzinfo=timezone.utc)
START_DATE = "2026-10-15"


def make_pack(groups: int = 12, tasks_per_group: int = 1) -> dict[str, Any]:
    """Template-shaped pack: 12 groups of one task, minimums 10 and 8."""
    groups_out: list[dict[str, Any]] = []
    tasks: list[dict[str, Any]] = []
    for number in range(1, groups + 1):
        ids = [f"t_q{number:02d}_{letter}" for letter in "abcd"[:tasks_per_group]]
        groups_out.append({"group_id": f"g{number:02d}", "tasks": ids})
        for task_id in ids:
            tasks.append(
                {
                    "task_id": task_id,
                    "source_files": [f"file_{number:02d}.txt"],
                    "question": f"Question for {task_id}?",
                    "answer": {"kind": "text_contains", "expected": "ok"},
                }
            )
    minimums = (
        {"paired_units": 10, "groups": 8} if groups >= 12 else {"paired_units": 4, "groups": 3}
    )
    return {
        "schema_version": 1,
        "kind": "self_test_pack",
        "study_id": "st_test_study",
        "model_label": "Test model at standard effort",
        "minimums": minimums,
        "quality_tolerance": 1,
        "groups": groups_out,
        "tasks": tasks,
    }


def load_template() -> dict[str, Any]:
    return json.loads(TEMPLATE.read_text(encoding="utf-8"))


def rec(
    kind: str, local_date: str = START_DATE, utc: str | None = None, **fields: Any
) -> dict[str, Any]:
    return {
        "type": kind,
        "utc": utc or "2026-10-15T09:00:00Z",
        "local_date": local_date,
        "utc_offset": "+00:00",
        **fields,
    }


def _stamp(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _tt(cents: int) -> str:
    return f"{cents // 100}.{cents % 100:02d}"


def start_study(pack: dict[str, Any], seed: str = SEED) -> tuple[StudyState, list[dict[str, Any]]]:
    state = StudyState.from_ledger(pack, [])
    record = rec("study_started", **study_started_fields(pack, seed))
    return state.with_record(record), [record]


def study_delta(method: str, group_id: str) -> int:
    """Cents per group. B is cheaper than C_normal in every group."""
    number = int(group_id[1:])
    return {"A": 25, "B": 18 + number % 3, "C_normal": 30 + number % 4, "C_tuned": 28}[method]


def run_study(
    pack: dict[str, Any],
    *,
    date_of: Any = None,
    delta: Any = study_delta,
    wrong: Any = None,
    gaps: dict[Any, int] | None = None,
    void_groups: tuple[str, ...] = (),
    reconcile_dates: bool = True,
    desktop_adjust: dict[str, int] | None = None,
    seed: str = SEED,
) -> tuple[StudyState, list[dict[str, Any]]]:
    """Drive a whole study the way the wizard does, following next_action().

    ``gaps`` adds spend (cents): key ``("start", phase)`` before a phase's first reading,
    ``("check", phase)`` after it (the setup check), or ``(phase, group_id)`` before a
    group's before-reading. Each date is then checked 4 hours after its last reading,
    with Claude Desktop's line equal to the measured span plus any adjustment.
    """
    state, records = start_study(pack, seed)
    clock = BASE
    meter = 100000  # cents: the running total starts at $1000.00
    pending_gaps = dict(gaps or {})
    voided: set[str] = set()
    date_for = date_of or (lambda phase: START_DATE)

    def emit(kind: str, local_date: str, utc: str | None = None, **fields: Any) -> None:
        nonlocal state, clock
        clock = clock + timedelta(seconds=30)
        record = {
            "type": kind,
            "utc": utc or _stamp(clock),
            "local_date": local_date,
            "utc_offset": "+00:00",
            **fields,
        }
        state = state.with_record(record)
        records.append(record)

    while True:
        action = state.next_action()
        kind = action["action"]
        if kind == "start_phase":
            phase = action["phase"]
            meter += pending_gaps.pop(("start", phase), 0)
            emit(
                "phase_started",
                date_for(phase),
                phase=phase,
                half=action["half"],
                method=action["method"],
                setup_confirmed=True,
                total=_tt(meter),
            )
            meter += pending_gaps.pop(("check", phase), 0)
        elif kind == "read_before":
            phase, group_id = action["phase"], action["group_id"]
            meter += pending_gaps.pop((phase, group_id), 0)
            emit("block_before", date_for(phase), group_id=group_id, total=_tt(meter))
        elif kind == "ask_task":
            phase = action["phase"]
            method = state.schedule[phase - 1]["method"]
            is_wrong = bool(wrong and wrong(method, action["task_id"]))
            emit(
                "task_answer",
                date_for(phase),
                task_id=action["task_id"],
                correct=not is_wrong,
                answer_sha256=answer_digest(action["task_id"]),
            )
        elif kind == "read_after":
            phase, group_id = action["phase"], action["group_id"]
            method = state.schedule[phase - 1]["method"]
            meter += delta(method, group_id)
            emit("block_after", date_for(phase), group_id=group_id, total=_tt(meter))
            if group_id in void_groups and group_id not in voided:
                voided.add(group_id)
                emit("block_voided", date_for(phase), group_id=group_id, reason="interrupted")
        else:
            break

    if reconcile_dates:
        adjust = desktop_adjust or {}
        for local_date in state.dates_with_block_readings():
            summary = state.date_summary(local_date)
            assert summary is not None
            span_cents = int(summary["span"] * 100)
            desktop = _tt(span_cents + adjust.get(local_date, 0))
            utc = _stamp(summary["ready_at"] + timedelta(minutes=1))
            emit("day_reconciled", local_date, utc=utc, desktop_total=desktop)
    return state, records


# ---------------------------------------------------------------- pack validation


def test_template_pack_validates_as_twelve_groups_of_one_task() -> None:
    pack = validate_pack(load_template())
    assert len(pack["groups"]) == 12
    assert all(len(group["tasks"]) == 1 for group in pack["groups"])
    assert len(pack["tasks"]) == 12
    assert pack["minimums"] == {"paired_units": 10, "groups": 8}
    assert pack["study_id"] == "st_example_replace"
    assert all(task["question"].startswith("EXAMPLE") for task in pack["tasks"])


def test_valid_test_pack_is_accepted_and_fills_number_tolerance() -> None:
    pack = make_pack()
    pack["tasks"][0]["answer"] = {"kind": "number", "expected": "5"}
    normalised = validate_pack(pack)
    assert normalised["tasks"][0]["answer"] == {"kind": "number", "expected": "5", "tolerance": "0"}


def _set_task_answer(pack: dict[str, Any], answer: Any) -> None:
    pack["tasks"][0]["answer"] = answer


def _set_groups(pack: dict[str, Any], groups: list[Any]) -> None:
    pack["groups"] = groups


BAD_PACKS: list[tuple[Any, str]] = [
    (lambda p: p.update(study_id="st-bad"), "study_id_invalid"),
    (lambda p: p["tasks"][0].update(task_id="T_bad"), "task_id_invalid"),
    (lambda p: p.update(extra=1), "pack_invalid"),
    (lambda p: p.update(schema_version=2), "pack_schema_invalid"),
    (lambda p: p.update(kind="other"), "pack_schema_invalid"),
    (lambda p: p.update(quality_tolerance=True), "quality_tolerance_invalid"),
    (lambda p: p.update(quality_tolerance=5), "quality_tolerance_invalid"),
    (lambda p: p["minimums"].update(paired_units=3), "minimums_invalid"),
    (lambda p: p["minimums"].update(groups=2), "minimums_invalid"),
    (lambda p: p["minimums"].update(groups=13), "minimums_unreachable"),
    (lambda p: p["minimums"].update(paired_units=13), "minimums_unreachable"),
    (lambda p: _set_groups(p, p["groups"][:3]), "groups_invalid"),
    (lambda p: p["groups"][0].update(tasks=[]), "groups_invalid"),
    (
        lambda p: p["groups"][0].update(
            tasks=["t_q01_a", "t_q02_a", "t_q03_a", "t_q04_a", "t_q05_a"]
        ),
        "groups_invalid",
    ),
    (lambda p: p["groups"][1].update(tasks=["t_q01_a"]), "group_coverage_invalid"),
    (lambda p: p["groups"][0].update(tasks=["t_nope"]), "group_coverage_invalid"),
    (lambda p: p["tasks"][0].update(source_files=["../secret.txt"]), "source_file_name_invalid"),
    (lambda p: p["tasks"][0].update(source_files=["CON.txt"]), "source_file_name_reserved"),
    (lambda p: p["tasks"][0].update(source_files=["file.exe"]), "source_format_mismatch"),
    (
        lambda p: p["tasks"][0].update(source_files=["a.txt", "b.txt", "c.txt", "d.txt", "e.txt"]),
        "source_files_invalid",
    ),
    (lambda p: p.update(model_label="x" * 81), "model_label_invalid"),
    (lambda p: p.update(model_label="bad\nlabel"), "model_label_invalid"),
    (lambda p: p["tasks"][0].update(question="x" * 2001), "question_invalid"),
    (lambda p: p["tasks"][0].update(question="ok\x00"), "question_invalid"),
    (lambda p: _set_task_answer(p, {"kind": "fuzzy", "expected": "x"}), "answer_kind_invalid"),
    (
        lambda p: _set_task_answer(p, {"kind": "number", "expected": "1,234.50"}),
        "answer_expected_invalid",
    ),
    (
        lambda p: _set_task_answer(p, {"kind": "date", "expected": "2026-02-30"}),
        "answer_expected_invalid",
    ),
    (lambda p: _set_task_answer(p, {"kind": "choice", "expected": []}), "answer_expected_invalid"),
    (
        lambda p: _set_task_answer(p, {"kind": "text_contains", "expected": "x", "tolerance": "1"}),
        "answer_invalid",
    ),
    (lambda p: p["tasks"][1].update(task_id="t_q01_a"), "task_id_duplicate"),
    (lambda p: p["groups"][1].update(group_id="g01"), "group_id_duplicate"),
    (lambda p: p["groups"][1].update(group_id="G1"), "group_id_invalid"),
]


@pytest.mark.parametrize(("mutate", "code"), BAD_PACKS)
def test_pack_rejects_invalid_values(mutate: Any, code: str) -> None:
    pack = make_pack()
    mutate(pack)
    with pytest.raises(SelfTestError) as caught:
        validate_pack(pack)
    assert caught.value.code == code


def test_pack_digest_is_canonical_and_sensitive_to_content() -> None:
    pack = make_pack()
    reordered = dict(reversed(list(pack.items())))
    assert pack_digest(pack) == pack_digest(reordered)
    changed = copy.deepcopy(pack)
    changed["tasks"][0]["answer"] = {"kind": "text_contains", "expected": "other"}
    assert pack_digest(changed) != pack_digest(pack)


# ---------------------------------------------------------------- schedule


def _order(schedule: list[dict[str, Any]]) -> tuple[str, ...]:
    return tuple(slot["method"] for slot in schedule[:4])


def test_schedule_has_eight_phases_in_two_halves() -> None:
    pack = make_pack()
    schedule = build_schedule(pack, SEED)
    assert [slot["phase"] for slot in schedule] == list(range(1, PHASE_COUNT + 1))
    assert [slot["half"] for slot in schedule] == ["H1"] * 4 + ["H2"] * 4
    h1 = {group["group_id"] for group in schedule[0]["groups"]}
    h2 = {group["group_id"] for group in schedule[4]["groups"]}
    assert len(h1) == 6 and len(h2) == 6
    assert h1 | h2 == {group["group_id"] for group in pack["groups"]}
    assert h1 & h2 == set()
    for slot in schedule[:4]:
        assert {group["group_id"] for group in slot["groups"]} == h1
    for slot in schedule[4:]:
        assert {group["group_id"] for group in slot["groups"]} == h2


def test_h2_methods_are_the_reverse_of_h1() -> None:
    for seed in (SEED, OTHER_SEED, "01" * 32, "fe" * 32, "3c" * 32):
        schedule = build_schedule(make_pack(), seed)
        assert _order(schedule[4:]) == tuple(reversed(_order(schedule)))


def test_method_order_is_a_permutation_with_a_and_b_adjacent() -> None:
    for seed in (SEED, OTHER_SEED, "01" * 32, "fe" * 32, "3c" * 32):
        order = list(_order(build_schedule(make_pack(), seed)))
        assert sorted(order) == sorted(METHODS)
        assert abs(order.index("A") - order.index("B")) == 1


def test_every_task_runs_once_under_every_method() -> None:
    pack = make_pack()
    for seed in (SEED, OTHER_SEED):
        schedule = build_schedule(pack, seed)
        for method in METHODS:
            tasks = [
                task
                for slot in schedule
                if slot["method"] == method
                for group in slot["groups"]
                for task in group["tasks"]
            ]
            assert sorted(tasks) == sorted(task["task_id"] for task in pack["tasks"])
        total = sum(len(group["tasks"]) for slot in schedule for group in slot["groups"])
        assert total == 48


def test_schedule_is_deterministic_and_seed_sensitive() -> None:
    pack = make_pack()
    assert build_schedule(pack, SEED) == build_schedule(pack, SEED)
    assert any(
        build_schedule(pack, SEED) != build_schedule(pack, other)
        for other in ("01" * 32, "02" * 32)
    )


def test_odd_group_count_splits_the_first_half_up() -> None:
    pack = make_pack(groups=5)
    schedule = build_schedule(pack, SEED)
    assert len(schedule[0]["groups"]) == 3
    assert len(schedule[4]["groups"]) == 2


def test_schedule_rejects_bad_seed() -> None:
    with pytest.raises(SelfTestError) as caught:
        build_schedule(make_pack(), "xyz")
    assert caught.value.code == "seed_invalid"
    assert len(new_seed_hex()) == 64


def test_pilot_changed_only_between_a_and_b_phases() -> None:
    schedule = [
        {"phase": 1, "method": "C_normal"},
        {"phase": 2, "method": "A"},
        {"phase": 3, "method": "C_tuned"},
        {"phase": 4, "method": "A"},
        {"phase": 5, "method": "B"},
    ]
    assert pilot_changed(schedule, 1) is False
    assert pilot_changed(schedule, 2) is True  # first A/B phase needs the setting
    assert pilot_changed(schedule, 4) is False  # A again after C, setting unchanged
    assert pilot_changed(schedule, 5) is True  # A to B changes it


# ---------------------------------------------------------------- prompts and setup


def _prompt_pack() -> dict[str, Any]:
    pack = make_pack()
    pack["tasks"][0]["question"] = "What is it?"
    pack["tasks"][0]["source_files"] = ["a.pdf", "b.txt"]
    return pack


C_NORMAL_BODY = "I have attached a.pdf, b.txt. Answer this: What is it?"
ENDING = '\n\nEnd with one line that starts with "ANSWER:" and gives only the final answer.'


def test_prompt_texts_for_each_method() -> None:
    pack = _prompt_pack()
    ab = (
        "Use the ContextShrink tools to read a.pdf, b.txt from my approved folders, "
        "then answer this: What is it?"
    )
    assert render_prompt(pack, "t_q01_a", "A") == ab + ENDING
    assert render_prompt(pack, "t_q01_a", "B") == ab + ENDING
    assert render_prompt(pack, "t_q01_a", "C_normal") == C_NORMAL_BODY + ENDING
    tuned = render_prompt(pack, "t_q01_a", "C_tuned")
    assert tuned.startswith(instruction_pack("v1")["text"] + "\n\n")
    assert tuned.endswith(C_NORMAL_BODY + ENDING)


def test_prompt_rejects_unknown_task_or_method() -> None:
    with pytest.raises(SelfTestError) as caught:
        render_prompt(_prompt_pack(), "t_missing", "A")
    assert caught.value.code == "task_unknown"
    with pytest.raises(SelfTestError) as caught:
        render_prompt(_prompt_pack(), "t_q01_a", "D")
    assert caught.value.code == "method_invalid"


def test_method_setup_names_the_settings_and_restarts_only_when_needed() -> None:
    pack = make_pack()
    a = " ".join(method_setup("A", pack, pilot_changed=True))
    b = " ".join(method_setup("B", pack, pilot_changed=True))
    c = " ".join(method_setup("C_normal", pack))
    tuned = " ".join(method_setup("C_tuned", pack))
    assert "exact_only" in a and "fully quit" in a and "raw" in a
    assert "normal" in b and "fully quit" in b and "optimized" in b
    assert "OFF" in c and "paperclip" in c and "fully quit" not in c
    assert "OFF" in tuned and "answer rules" in tuned
    unchanged = " ".join(method_setup("A", pack, pilot_changed=False))
    assert "no change and no restart" in unchanged and "fully quit" not in unchanged
    for method in METHODS:
        assert pack["model_label"] in " ".join(method_setup(method, pack))
        assert "fresh chat" in " ".join(method_setup(method, pack))
        assert "no other claude product" in " ".join(method_setup(method, pack)).lower()


# ---------------------------------------------------------------- answer checking


def _task(kind: str, expected: Any, **extra: str) -> dict[str, Any]:
    return {"answer": {"kind": kind, "expected": expected, **extra}}


@pytest.mark.parametrize(
    ("reply", "expected", "tolerance", "result"),
    [
        ("ANSWER: $1,234.50", "1234.50", "0", True),
        ("ANSWER: 1234.5", "1234.50", "0", True),
        ("ANSWER: 1234.51", "1234.50", "0.01", True),
        ("ANSWER: 1234.52", "1234.50", "0.01", False),
        ("ANSWER: 12%", "12", "0", True),
        ("ANSWER: 90 days", "90", "0", False),
        ("ANSWER: abc", "90", "0", False),
        ("The total is 1234.50 and that is all.", "1234.50", "0", False),
        ("ANSWER: 1234.50\nANSWER: 1,000", "1234.50", "0", False),
    ],
)
def test_number_answers(reply: str, expected: str, tolerance: str, result: bool) -> None:
    task = _task("number", expected, tolerance=tolerance)
    assert check_answer(task, reply) is result


@pytest.mark.parametrize(
    ("reply", "result"),
    [
        ("Some text.\nANSWER: The notice is 90 days.", True),
        ("ANSWER: 60 days\nreasoning\nANSWER: 90 days", True),
        ("ANSWER: 90 days\nANSWER: 60 days", False),
        ("answer:   90   DAYS ", True),
        ("The clause says ANSWER: 90 days.", True),
        ("The notice period is 60 days", False),
        ("ANSWER:", False),
        ("ANSWER: 900 days but 9 days", False),
    ],
)
def test_text_contains_uses_last_answer_line_or_whole_text(reply: str, result: bool) -> None:
    assert check_answer(_task("text_contains", "90 days"), reply) is result


@pytest.mark.parametrize(
    ("reply", "result"),
    [
        ("ANSWER: 2026-03-31", True),
        ("ANSWER: March 31, 2026", True),
        ("ANSWER: 31 March 2026", True),
        ("ANSWER: 31 Mar 2026", True),
        ("ANSWER: March 31, 2026.", True),
        ("ANSWER: 03/31/2026", False),
        ("ANSWER: 31/03/2026", False),
        ("ANSWER: 2026-02-30", False),
        ("ANSWER: April 1, 2026", False),
    ],
)
def test_date_answers_accept_iso_and_english_month_forms_only(reply: str, result: bool) -> None:
    assert check_answer(_task("date", "2026-03-31"), reply) is result


@pytest.mark.parametrize(
    ("reply", "result"),
    [
        ("ANSWER: yes", True),
        ("ANSWER:  NO ", True),
        ("ANSWER: Yes, mostly", False),
        ("ANSWER: Maybe", False),
    ],
)
def test_choice_answers_are_exact_after_case_folding(reply: str, result: bool) -> None:
    assert check_answer(_task("choice", ["Yes", "No"]), reply) is result


def test_check_answer_rejects_non_text_reply_and_bad_task() -> None:
    with pytest.raises(SelfTestError) as caught:
        check_answer(_task("text_contains", "x"), None)  # type: ignore[arg-type]
    assert caught.value.code == "reply_invalid"
    with pytest.raises(SelfTestError) as caught:
        check_answer({"answer": {"kind": "bad"}}, "ANSWER: x")
    assert caught.value.code == "answer_kind_invalid"


def test_answer_digest_hashes_the_reply_not_a_short_answer() -> None:
    reply = "ANSWER: 90 days"
    assert answer_digest(reply) == hashlib.sha256(reply.encode("utf-8")).hexdigest()
    assert answer_digest(reply) != hashlib.sha256(b"90 days").hexdigest()


def test_text_answers_match_whole_words_only() -> None:
    task = {"answer": {"kind": "text_contains", "expected": "90 days"}}
    assert check_answer(task, "Notice period is 90 days.\nANSWER: 90 days")
    assert check_answer(task, "ANSWER: (90 days)")
    assert not check_answer(task, "ANSWER: 190 days")
    assert not check_answer(task, "ANSWER: 90 days2")


# ---------------------------------------------------------------- ledger


def _clock() -> datetime:
    return BASE


def _three_records(ledger: Ledger) -> None:
    for index in range(3):
        ledger.append("block_before", group_id="g01", total=f"{index + 1}.00")


def _chain_line(seq: int, prev: str, body: dict[str, Any]) -> bytes:
    record = {"seq": seq, "prev_sha256": prev, **body}
    return json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


def test_ledger_appends_chained_records_and_loads_them(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "ledger.jsonl", clock=_clock)
    _three_records(ledger)
    records = ledger.load()
    assert [record["seq"] for record in records] == [1, 2, 3]
    assert records[0]["prev_sha256"] == "0" * 64
    assert records[0]["utc"] == "2026-10-15T09:00:00Z"
    lines = (tmp_path / "ledger.jsonl").read_bytes().split(b"\n")
    assert records[1]["prev_sha256"] == hashlib.sha256(lines[0]).hexdigest()


def test_ledger_missing_file_loads_empty(tmp_path: Path) -> None:
    assert Ledger(tmp_path / "none.jsonl").load() == []


@pytest.mark.parametrize("mode", ["edit", "delete", "reorder", "torn"])
def test_ledger_detects_tampering(tmp_path: Path, mode: str) -> None:
    path = tmp_path / "ledger.jsonl"
    ledger = Ledger(path, clock=_clock)
    _three_records(ledger)
    lines = path.read_bytes().split(b"\n")
    if mode == "edit":
        path.write_text(
            path.read_text(encoding="utf-8").replace('"total":"2.00"', '"total":"9.00"')
        )
    elif mode == "delete":
        path.write_bytes(b"\n".join([lines[0], lines[2], b""]))
    elif mode == "reorder":
        path.write_bytes(b"\n".join([lines[1], lines[0], lines[2], b""]))
    else:
        path.write_bytes(path.read_bytes()[:-1])
    with pytest.raises(SelfTestError) as caught:
        ledger.load()
    assert caught.value.code == "ledger_tampered"


def test_ledger_detects_unknown_type_and_non_increasing_seq(tmp_path: Path) -> None:
    path = tmp_path / "ledger.jsonl"
    body = {
        "type": "bogus",
        "utc": "2026-10-15T09:00:00Z",
        "local_date": START_DATE,
        "utc_offset": "+00:00",
    }
    path.write_bytes(_chain_line(1, "0" * 64, body) + b"\n")
    with pytest.raises(SelfTestError) as caught:
        Ledger(path).load()
    assert caught.value.code == "ledger_tampered"

    good = {
        "type": "block_before",
        "utc": "2026-10-15T09:00:00Z",
        "local_date": START_DATE,
        "utc_offset": "+00:00",
        "group_id": "g01",
        "total": "1.00",
    }
    one = _chain_line(1, "0" * 64, good)
    two = _chain_line(1, hashlib.sha256(one).hexdigest(), good)
    path.write_bytes(one + b"\n" + two + b"\n")
    with pytest.raises(SelfTestError) as caught:
        Ledger(path).load()
    assert caught.value.code == "ledger_tampered"


def test_ledger_rejects_unknown_type_and_extra_fields(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "ledger.jsonl", clock=_clock)
    with pytest.raises(SelfTestError) as caught:
        ledger.append("nonsense", group_id="g01")
    assert caught.value.code == "record_type_unknown"
    with pytest.raises(SelfTestError) as caught:
        ledger.append("block_before", group_id="g01", total="1.00", extra="x")
    assert caught.value.code == "record_invalid"


def test_ledger_local_date_override_and_default_state_dir(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "ledger.jsonl", clock=_clock)
    record = ledger.append("day_reconciled", local_date="2026-10-14", desktop_total="1.20")
    assert record["local_date"] == "2026-10-14"
    assert "utc_offset" in record
    path = default_state_dir("st_demo", env={"LOCALAPPDATA": str(tmp_path)})
    assert path == tmp_path / "ContextShrink" / "self-test" / "st_demo"


# ---------------------------------------------------------------- stepping helpers


def _begin_phase(state: StudyState, total: str = "1000.00", date: str = START_DATE) -> StudyState:
    action = state.next_action()
    assert action["action"] == "start_phase"
    return state.with_record(
        rec(
            "phase_started",
            date,
            phase=action["phase"],
            half=action["half"],
            method=action["method"],
            setup_confirmed=True,
            total=total,
        )
    )


def _finish_phase(state: StudyState, cents: int, date: str = START_DATE) -> tuple[StudyState, int]:
    """Run the open phase to its end: each group costs 10 cents. Returns the new meter."""
    while True:
        action = state.next_action()
        if action["action"] == "start_phase":
            return state, cents
        if action["action"] == "read_before":
            state = state.with_record(
                rec("block_before", date, group_id=action["group_id"], total=_tt(cents))
            )
        elif action["action"] == "ask_task":
            state = state.with_record(
                rec(
                    "task_answer",
                    date,
                    task_id=action["task_id"],
                    correct=True,
                    answer_sha256="a" * 64,
                )
            )
        else:
            cents += 10
            state = state.with_record(
                rec("block_after", date, group_id=action["group_id"], total=_tt(cents))
            )


# ---------------------------------------------------------------- state rules


def test_phase_must_start_in_order_with_the_scheduled_method() -> None:
    state, _ = start_study(make_pack())
    schedule = state.schedule
    wrong_phase = rec(
        "phase_started",
        phase=2,
        half=schedule[1]["half"],
        method=schedule[1]["method"],
        setup_confirmed=True,
        total="1000.00",
    )
    with pytest.raises(SelfTestError) as caught:
        state.with_record(wrong_phase)
    assert caught.value.code == "phase_out_of_order"
    mismatch = rec(
        "phase_started",
        phase=1,
        half="H1",
        method="C_tuned" if schedule[0]["method"] != "C_tuned" else "A",
        setup_confirmed=True,
        total="1000.00",
    )
    with pytest.raises(SelfTestError) as caught:
        state.with_record(mismatch)
    assert caught.value.code == "phase_mismatch"


def test_phase_start_needs_confirmed_setup() -> None:
    state, _ = start_study(make_pack())
    slot = state.schedule[0]
    record = rec(
        "phase_started",
        phase=1,
        half=slot["half"],
        method=slot["method"],
        setup_confirmed=False,
        total="1000.00",
    )
    with pytest.raises(SelfTestError) as caught:
        state.with_record(record)
    assert caught.value.code == "record_invalid"


def test_next_phase_waits_for_the_previous_one_and_never_drops_the_meter() -> None:
    state, _ = start_study(make_pack())
    state = _begin_phase(state, "1000.00")
    state, cents = _finish_phase(state, 100000)
    assert state.completed_phases == [1]
    slot = state.schedule[1]
    below = rec(
        "phase_started",
        phase=2,
        half=slot["half"],
        method=slot["method"],
        setup_confirmed=True,
        total=_tt(cents - 1),
    )
    with pytest.raises(SelfTestError) as caught:
        state.with_record(below)
    assert caught.value.code == "reading_decreased"
    ok = rec(
        "phase_started",
        phase=2,
        half=slot["half"],
        method=slot["method"],
        setup_confirmed=True,
        total=_tt(cents + 25),
    )
    assert state.with_record(ok).phases[1]["start_total"] == Decimal(_tt(cents + 25))


def test_spend_between_groups_of_a_phase_is_a_warning_not_an_exclusion() -> None:
    state, _ = run_study(make_pack(), gaps={(1, "g02"): 50})
    assert [w["reason"] for w in state.warnings] == ["spend_between_blocks"]
    assert state.warnings[0]["phase"] == 1 and state.warnings[0]["group_id"] == "g02"
    report = analyze(make_pack(), state)
    assert report["progress"]["group_runs_included"] == 48
    assert report["left_out"] == []


def test_spend_at_a_phase_boundary_is_expected_and_not_a_warning() -> None:
    gaps = {("start", 2): 40, ("check", 2): 25, ("start", 5): 10}
    state, _ = run_study(make_pack(), gaps=gaps)
    assert state.warnings == []


def test_a_voided_group_is_excluded_and_redone_without_touching_anything_else() -> None:
    pack = make_pack()
    state, _ = run_study(pack, void_groups=("g03",))
    assert [entry["group_id"] for entry in state.voided_blocks] == ["g03"]
    assert state.voided_blocks[0]["reason"] == "interrupted"
    assert state.warnings == []
    assert state.study_complete is True
    report = analyze(pack, state)
    assert report["progress"]["group_runs_included"] == 48
    assert report["progress"]["group_runs_recorded"] == 48
    voided = [item for item in report["left_out"] if item["reason"] == "voided"]
    assert len(voided) == 1 and voided[0]["group_id"] == "g03"


def test_redo_right_after_a_void_is_the_same_group() -> None:
    state, _ = start_study(make_pack())
    state = _begin_phase(state)
    state = state.with_record(rec("block_before", group_id="g01", total="1000.00"))
    state = state.with_record(
        rec("task_answer", task_id="t_q01_a", correct=True, answer_sha256="a" * 64)
    )
    state = state.with_record(rec("block_voided", group_id="g01", reason="chat_not_fresh"))
    assert state.next_action()["action"] == "read_before"
    assert state.next_action()["group_id"] == "g01"


def test_readings_must_be_two_decimal_non_negative_amounts() -> None:
    for total in ("1.5", "-1.00", "1e3", "100000.01", "12.345", " 1.00"):
        state, _ = start_study(make_pack())
        state = _begin_phase(state)
        with pytest.raises(SelfTestError) as caught:
            state.with_record(rec("block_before", group_id="g01", total=total))
        assert caught.value.code == "reading_invalid"


def test_block_after_cannot_go_below_before_or_add_more_than_twenty_dollars() -> None:
    state, _ = start_study(make_pack())
    state = _begin_phase(state, "10.00")
    state = state.with_record(rec("block_before", group_id="g01", total="10.00"))
    state = state.with_record(
        rec("task_answer", task_id="t_q01_a", correct=True, answer_sha256="a" * 64)
    )
    with pytest.raises(SelfTestError) as caught:
        state.with_record(rec("block_after", group_id="g01", total="9.00"))
    assert caught.value.code == "reading_decreased"
    with pytest.raises(SelfTestError) as caught:
        state.with_record(rec("block_after", group_id="g01", total="30.01"))
    assert caught.value.code == "reading_delta_too_large"
    state.with_record(rec("block_after", group_id="g01", total="30.00"))


def test_block_after_needs_every_answer_first() -> None:
    pack = make_pack(groups=4, tasks_per_group=2)
    state, _ = start_study(pack)
    state = _begin_phase(state, "10.00")
    first = state.schedule[0]["groups"][0]["group_id"]
    state = state.with_record(rec("block_before", group_id=first, total="10.00"))
    with pytest.raises(SelfTestError) as caught:
        state.with_record(rec("block_after", group_id=first, total="10.00"))
    assert caught.value.code == "block_incomplete"


def test_groups_run_in_schedule_order() -> None:
    state, _ = start_study(make_pack())
    state = _begin_phase(state)
    second = state.schedule[0]["groups"][1]["group_id"]
    with pytest.raises(SelfTestError) as caught:
        state.with_record(rec("block_before", group_id=second, total="10.00"))
    assert caught.value.code == "block_not_next"


def test_replayed_ledger_matches_the_live_state(tmp_path: Path) -> None:
    pack = make_pack()
    state, records = run_study(pack, reconcile_dates=False)
    ledger = Ledger(tmp_path / "ledger.jsonl", clock=_clock)
    for record in records:
        ledger.append_record(record)
    replayed = StudyState.from_ledger(pack, ledger.load())
    assert analyze(pack, replayed) == analyze(pack, state)


# ---------------------------------------------------------------- reconciliation


def test_reconciliation_waits_four_hours_after_the_last_reading() -> None:
    state, _ = run_study(make_pack(), reconcile_dates=False)
    ready = state.reconcile_ready_at(START_DATE)
    early = rec(
        "day_reconciled", START_DATE, utc=_stamp(ready - timedelta(minutes=1)), desktop_total="1.00"
    )
    with pytest.raises(SelfTestError) as caught:
        state.with_record(early)
    assert caught.value.code == "reconcile_too_early"
    on_time = rec("day_reconciled", START_DATE, utc=_stamp(ready), desktop_total="1.00")
    assert state.with_record(on_time).reconciled[START_DATE]["passed"] is False


def test_tolerance_boundary_on_one_date_with_all_group_runs() -> None:
    state, _ = run_study(make_pack(), reconcile_dates=False)
    span = state.date_summary(START_DATE)["span"]
    # 48 group runs on the date: tolerance is 0.01 x (48 + 1) = 0.49.
    assert state.date_summary(START_DATE)["tolerance"] == Decimal("0.49")
    assert reconcile(state, START_DATE, f"{span + Decimal('0.49'):.2f}")["passed"] is True
    assert reconcile(state, START_DATE, f"{span - Decimal('0.49'):.2f}")["passed"] is True
    failed = reconcile(state, START_DATE, f"{span + Decimal('0.50'):.2f}")
    assert failed["passed"] is False
    assert failed["tolerance"] == "0.49"


def test_reconciliation_needs_a_date_with_group_readings() -> None:
    state, _ = start_study(make_pack())
    state = _begin_phase(state)
    with pytest.raises(SelfTestError) as caught:
        reconcile(state, START_DATE, "1.00")
    assert caught.value.code == "day_not_found"


def test_a_checked_date_is_final_and_cannot_gain_readings() -> None:
    state, _ = run_study(make_pack())
    action_state = state
    assert action_state.next_action()["action"] == "done"
    with pytest.raises(SelfTestError) as caught:
        state.with_record(
            rec("phase_started", phase=1, half="H1", method="A", setup_confirmed=True, total="1.00")
        )
    assert caught.value.code in ("study_complete", "date_already_reconciled")


def test_two_dates_reconcile_separately_and_provenance_holds() -> None:
    def date_of(phase: int) -> str:
        return START_DATE if phase <= 4 else "2026-10-16"

    pack = make_pack()
    state, _ = run_study(pack, date_of=date_of)
    assert state.dates_with_block_readings() == [START_DATE, "2026-10-16"]
    assert state.reconciled[START_DATE]["passed"] and state.reconciled["2026-10-16"]["passed"]
    report = analyze(pack, state)
    assert [d["status"] for d in report["dates"]] == ["passed", "passed"]
    primary = next(item for item in report["comparisons"] if item["primary"])
    assert primary["decision"]["claim"] == "savings_supported"
    assert primary["decision"]["reasons"] == []


def test_a_failed_date_excludes_all_its_groups() -> None:
    def date_of(phase: int) -> str:
        return START_DATE if phase <= 4 else "2026-10-16"

    pack = make_pack()
    state, _ = run_study(pack, date_of=date_of, desktop_adjust={"2026-10-16": 50})
    assert state.reconciled["2026-10-16"]["passed"] is False
    report = analyze(pack, state)
    assert report["progress"]["group_runs_included"] == 24  # only H1, on the passing date
    assert {item["reason"] for item in report["left_out"]} == {"date_failed"}
    primary = next(item for item in report["comparisons"] if item["primary"])
    assert primary["interval"]["units"] == 6
    assert primary["decision"]["claim"] == "none"
    assert "below_min_units" in primary["decision"]["reasons"]


def test_an_unchecked_date_withholds_provenance() -> None:
    pack = make_pack()
    state, _ = run_study(pack, reconcile_dates=False)
    report = analyze(pack, state)
    assert report["pending_reconciliations"] == [START_DATE]
    primary = next(item for item in report["comparisons"] if item["primary"])
    assert primary["decision"]["claim"] == "none"
    assert "provenance_unverified" in primary["decision"]["reasons"]


# ---------------------------------------------------------------- full studies


def test_full_simulated_single_day_study_supports_b_over_c_normal() -> None:
    pack = make_pack()
    state, _ = run_study(pack)
    report = analyze(pack, state)
    comparisons = {item["key"]: item for item in report["comparisons"]}
    primary = comparisons["B_vs_C_normal"]
    assert primary["primary"] is True
    assert primary["decision"]["claim"] == "savings_supported"
    assert primary["decision"]["reasons"] == []
    assert primary["interval"]["units"] == 12
    assert primary["interval"]["clusters"] == 12
    assert report["progress"]["study_complete"] is True
    assert report["progress"]["chats_total"] == 48
    assert Decimal(report["methods"]["B"]["spend"]) < Decimal(
        report["methods"]["C_normal"]["spend"]
    )
    assert report["quality"]["regression"] is False
    assert report["warnings"] == []


def test_one_voided_group_still_meets_the_minimums() -> None:
    pack = make_pack()
    state, _ = run_study(pack, void_groups=("g05",))
    report = analyze(pack, state)
    primary = next(item for item in report["comparisons"] if item["primary"])
    assert primary["interval"]["units"] == 12
    assert primary["decision"]["claim"] == "savings_supported"


def test_too_many_wrong_b_answers_give_quality_regression() -> None:
    pack = make_pack()
    state, _ = run_study(pack, wrong=lambda method, task_id: method == "B")
    report = analyze(pack, state)
    primary = next(item for item in report["comparisons"] if item["key"] == "B_vs_C_normal")
    assert primary["decision"]["claim"] == "none"
    assert "quality_regression" in primary["decision"]["reasons"]
    assert report["quality"]["regression"] is True


def test_partial_study_reports_progress_without_a_claim() -> None:
    pack = make_pack()
    state, records = run_study(pack, reconcile_dates=False)
    cut = [i for i, r in enumerate(records) if r["type"] == "phase_started"][2]
    partial, _ = start_study(pack)
    for record in records[:cut]:
        if record["type"] != "study_started":
            partial = partial.with_record(record)
    report = analyze(pack, partial)
    assert report["progress"]["phases_complete"] == 2
    assert report["progress"]["current_phase"] == 3
    assert report["progress"]["study_complete"] is False
    assert all(item["decision"]["claim"] == "none" for item in report["comparisons"])
    assert [phase["status"] for phase in report["phases"]][:3] == [
        "complete",
        "complete",
        "not_started",
    ]


def test_report_uses_strings_for_money_and_leaks_no_names_or_answers() -> None:
    pack = make_pack()
    state, _ = run_study(pack)
    report = analyze(pack, state)
    text = json.dumps(report)
    assert "file_01.txt" not in text
    assert "ANSWER" not in text
    stack = [report]
    while stack:
        item = stack.pop()
        assert not isinstance(item, float)
        if isinstance(item, dict):
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
    assert report["methods"]["B"]["spend"].count(".") == 1
    assert report["scope"]["meter_resolution"] == "$0.01"
    assert "this account, model setting and task set only" in report["scope"]["statement"]
    assert report["phases"][0]["started_utc"].endswith("Z")


def test_analyze_rejects_a_different_pack() -> None:
    pack = make_pack()
    state, _ = start_study(pack)
    other = make_pack()
    other["study_id"] = "st_other_study"
    with pytest.raises(SelfTestError) as caught:
        analyze(other, state)
    assert caught.value.code == "pack_mismatch"
