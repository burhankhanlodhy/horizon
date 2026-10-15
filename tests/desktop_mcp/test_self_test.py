from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from horizon.desktop_mcp.answer_contract import instruction_pack
from horizon.desktop_mcp.self_test import (
    DAY_COUNT,
    METHODS,
    ROWS,
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
    reconcile,
    render_prompt,
    study_started_fields,
    validate_pack,
)

REPO = Path(__file__).resolve().parents[2]
TEMPLATE = REPO / "desktop" / "mcp" / "self-test" / "pack.template.json"
SEED = "ab" * 32
OTHER_SEED = "cd" * 32
FIXED = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)
START_DATE = "2026-10-15"


def make_pack() -> dict[str, Any]:
    groups = []
    tasks = []
    for number in range(1, 9):
        ids = [f"t_g{number}_a", f"t_g{number}_b"]
        groups.append({"group_id": f"g{number}", "tasks": ids})
        for task_id in ids:
            tasks.append(
                {
                    "task_id": task_id,
                    "source_files": [f"file_{number}.txt"],
                    "question": f"Question for {task_id}?",
                    "answer": {"kind": "text_contains", "expected": "ok"},
                }
            )
    return {
        "schema_version": 1,
        "kind": "self_test_pack",
        "study_id": "st_test_study",
        "model_label": "Test model at standard effort",
        "minimums": {"paired_units": 12, "groups": 6},
        "quality_tolerance": 1,
        "groups": groups,
        "tasks": tasks,
    }


def load_template() -> dict[str, Any]:
    return json.loads(TEMPLATE.read_text(encoding="utf-8"))


def rec(kind: str, local_date: str = START_DATE, **fields: Any) -> dict[str, Any]:
    return {
        "type": kind,
        "utc": "2026-10-15T12:00:00Z",
        "local_date": local_date,
        "utc_offset": "+00:00",
        **fields,
    }


def _tt(cents: int) -> str:
    return f"{cents // 100}.{cents % 100:02d}"


def start_study(pack: dict[str, Any], seed: str = SEED) -> tuple[StudyState, list[dict[str, Any]]]:
    state = StudyState.from_ledger(pack, [])
    record = rec("study_started", **study_started_fields(pack, seed))
    return state.with_record(record), [record]


def drive_day(
    state: StudyState,
    local_date: str,
    total: int,
    *,
    delta: Any = 15,
    gaps: dict[int, int] | None = None,
    wrong: Any = None,
    void_after: tuple[str, ...] = (),
    finish_date: str | None = None,
) -> tuple[StudyState, list[dict[str, Any]], int]:
    """Run the next measurement day the way the wizard would, from next_action()."""
    records: list[dict[str, Any]] = []
    gap_left = dict(gaps or {})
    gap_done: set[str] = set()
    voided: set[str] = set()
    current = total
    while True:
        action = state.next_action()
        kind = action["action"]
        day = action.get("day")
        method = state.schedule[day - 1]["method"] if day else None
        if kind == "start_day":
            record = rec(
                "day_started",
                local_date,
                day=day,
                round=action["round"],
                method=action["method"],
                setup_confirmed=True,
            )
        elif kind == "read_before":
            group_id = action["group_id"]
            order = [group["group_id"] for group in state.schedule[day - 1]["groups"]]
            index = order.index(group_id)
            if index in gap_left and group_id not in gap_done:
                current += gap_left[index]
                gap_done.add(group_id)
            record = rec("block_before", local_date, group_id=group_id, total=_tt(current))
        elif kind == "ask_task":
            is_wrong = bool(wrong and wrong(method, action["task_id"]))
            record = rec(
                "task_answer",
                local_date,
                task_id=action["task_id"],
                correct=not is_wrong,
                answer_sha256=answer_digest(action["task_id"]),
            )
        elif kind == "read_after":
            group_id = action["group_id"]
            current += delta(method, group_id) if callable(delta) else delta
            record = rec("block_after", local_date, group_id=group_id, total=_tt(current))
        elif kind == "finish_day":
            record = rec("day_finished", finish_date or local_date, status="complete")
        else:  # pragma: no cover - the driver only runs a day
            raise AssertionError(kind)
        state = state.with_record(record)
        records.append(record)
        if (
            kind == "read_after"
            and action["group_id"] in void_after
            and action["group_id"] not in voided
        ):
            voided.add(action["group_id"])
            void = rec(
                "block_voided", local_date, group_id=action["group_id"], reason="interrupted"
            )
            state = state.with_record(void)
            records.append(void)
        if kind == "finish_day":
            return state, records, current


def study_delta(method: str, group_id: str) -> int:
    number = int(group_id[1:])
    return {"A": 25, "B": 18 + number % 3, "C_normal": 30 + number % 4, "C_tuned": 28}[method]


def run_study(
    pack: dict[str, Any],
    *,
    delta: Any = study_delta,
    wrong: Any = None,
    skip_reconcile: frozenset[int] = frozenset(),
    days: int = DAY_COUNT,
    seed: str = SEED,
) -> tuple[StudyState, list[dict[str, Any]]]:
    state, records = start_study(pack, seed)
    total = 1000
    for index in range(days):
        date = f"2026-10-{15 + index:02d}"
        day_start = total
        state, day_records, total = drive_day(state, date, total, delta=delta, wrong=wrong)
        records += day_records
        if index not in skip_reconcile:
            reconciled = rec("day_reconciled", date, desktop_total=_tt(total - day_start))
            state = state.with_record(reconciled)
            records.append(reconciled)
    return state, records


# ---------------------------------------------------------------- pack validation


def test_template_pack_validates_as_eight_groups_of_two() -> None:
    pack = validate_pack(load_template())
    assert len(pack["groups"]) == 8
    assert all(len(group["tasks"]) == 2 for group in pack["groups"])
    assert len(pack["tasks"]) == 16
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
    (lambda p: p["minimums"].update(groups=9), "minimums_unreachable"),
    (lambda p: p["minimums"].update(paired_units=17), "minimums_unreachable"),
    (lambda p: _set_groups(p, p["groups"][:3]), "groups_invalid"),
    (lambda p: p["groups"][0].update(tasks=[]), "groups_invalid"),
    (
        lambda p: p["groups"][0].update(tasks=["t_g1_a", "t_g1_b", "t_g2_a", "t_g2_b", "t_g3_a"]),
        "groups_invalid",
    ),
    (lambda p: p["groups"][1].update(tasks=["t_g1_a", "t_g2_b"]), "group_coverage_invalid"),
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
    (lambda p: p["tasks"][1].update(task_id="t_g1_a"), "task_id_duplicate"),
    (lambda p: p["groups"][1].update(group_id="g1"), "group_id_duplicate"),
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


def test_schedule_gives_each_method_once_per_round_and_eight_days() -> None:
    pack = make_pack()
    for seed in (SEED, OTHER_SEED, "01" * 32, "fe" * 32):
        days = build_schedule(pack, seed)
        assert len(days) == DAY_COUNT
        for round_no in (1, 2):
            methods = [day["method"] for day in days if day["round"] == round_no]
            assert sorted(methods) == sorted(METHODS)


def test_the_two_rounds_use_different_method_orders() -> None:
    pack = make_pack()
    for seed in (SEED, OTHER_SEED, "01" * 32, "fe" * 32, "3c" * 32):
        days = build_schedule(pack, seed)
        first = [day["method"] for day in days[:4]]
        second = [day["method"] for day in days[4:]]
        assert first != second


def test_schedule_is_deterministic_and_seed_sensitive() -> None:
    pack = make_pack()
    assert build_schedule(pack, SEED) == build_schedule(pack, SEED)
    assert any(
        build_schedule(pack, SEED) != build_schedule(pack, other)
        for other in ("01" * 32, "02" * 32)
    )


def test_every_group_and_task_appears_every_day() -> None:
    pack = make_pack()
    expected_groups = sorted(group["group_id"] for group in pack["groups"])
    expected_tasks = {group["group_id"]: sorted(group["tasks"]) for group in pack["groups"]}
    for day in build_schedule(pack, SEED):
        assert sorted(group["group_id"] for group in day["groups"]) == expected_groups
        for group in day["groups"]:
            assert sorted(group["tasks"]) == expected_tasks[group["group_id"]]


def test_williams_rows_are_balanced_latin_rows() -> None:
    for row in ROWS:
        assert sorted(row) == [0, 1, 2, 3]
    for column in range(4):
        assert sorted(row[column] for row in ROWS) == [0, 1, 2, 3]
    adjacent = [(row[index], row[index + 1]) for row in ROWS for index in range(3)]
    assert len(set(adjacent)) == 12 and all(a != b for a, b in adjacent)


def test_schedule_rejects_bad_seed() -> None:
    with pytest.raises(SelfTestError) as caught:
        build_schedule(make_pack(), "xyz")
    assert caught.value.code == "seed_invalid"
    assert len(new_seed_hex()) == 64


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
    assert render_prompt(pack, "t_g1_a", "A") == ab + ENDING
    assert render_prompt(pack, "t_g1_a", "B") == ab + ENDING
    assert render_prompt(pack, "t_g1_a", "C_normal") == C_NORMAL_BODY + ENDING
    tuned = render_prompt(pack, "t_g1_a", "C_tuned")
    assert tuned.startswith(instruction_pack("v1")["text"] + "\n\n")
    assert tuned.endswith(C_NORMAL_BODY + ENDING)


def test_prompt_rejects_unknown_task_or_method() -> None:
    with pytest.raises(SelfTestError) as caught:
        render_prompt(_prompt_pack(), "t_missing", "A")
    assert caught.value.code == "task_unknown"
    with pytest.raises(SelfTestError) as caught:
        render_prompt(_prompt_pack(), "t_g1_a", "D")
    assert caught.value.code == "method_invalid"


def test_method_setup_steps_name_the_settings() -> None:
    pack = make_pack()
    a = " ".join(method_setup("A", pack))
    b = " ".join(method_setup("B", pack))
    c = " ".join(method_setup("C_normal", pack))
    tuned = " ".join(method_setup("C_tuned", pack))
    assert "exact_only" in a and "ON" in a
    assert "normal" in b and "ON" in b
    assert "OFF" in c and "paperclip" in c
    assert "OFF" in tuned and "answer rules" in tuned
    for method in METHODS:
        assert pack["model_label"] in " ".join(method_setup(method, pack))
        assert "fresh chat" in " ".join(method_setup(method, pack))


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


# ---------------------------------------------------------------- ledger


def _clock() -> datetime:
    return FIXED


def _three_records(ledger: Ledger) -> None:
    for index in range(3):
        ledger.append("block_before", group_id="g1", total=f"{index + 1}.00")


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
    assert records[0]["utc"] == "2026-10-15T12:00:00Z"
    lines = (tmp_path / "ledger.jsonl").read_bytes().split(b"\n")
    assert records[1]["prev_sha256"] == hashlib.sha256(lines[0]).hexdigest()


def test_ledger_missing_file_loads_empty(tmp_path: Path) -> None:
    assert Ledger(tmp_path / "none.jsonl").load() == []


def test_ledger_detects_edited_line(tmp_path: Path) -> None:
    path = tmp_path / "ledger.jsonl"
    ledger = Ledger(path, clock=_clock)
    _three_records(ledger)
    text = path.read_text(encoding="utf-8")
    path.write_text(text.replace('"total":"2.00"', '"total":"9.00"'), encoding="utf-8")
    with pytest.raises(SelfTestError) as caught:
        ledger.load()
    assert caught.value.code == "ledger_tampered"


def test_ledger_detects_deleted_line(tmp_path: Path) -> None:
    path = tmp_path / "ledger.jsonl"
    ledger = Ledger(path, clock=_clock)
    _three_records(ledger)
    lines = path.read_bytes().split(b"\n")
    path.write_bytes(b"\n".join([lines[0], lines[2], b""]))
    with pytest.raises(SelfTestError) as caught:
        ledger.load()
    assert caught.value.code == "ledger_tampered"


def test_ledger_detects_reordered_lines(tmp_path: Path) -> None:
    path = tmp_path / "ledger.jsonl"
    ledger = Ledger(path, clock=_clock)
    _three_records(ledger)
    lines = path.read_bytes().split(b"\n")
    path.write_bytes(b"\n".join([lines[1], lines[0], lines[2], b""]))
    with pytest.raises(SelfTestError) as caught:
        ledger.load()
    assert caught.value.code == "ledger_tampered"


def test_ledger_detects_unknown_type_and_non_increasing_seq(tmp_path: Path) -> None:
    path = tmp_path / "ledger.jsonl"
    body = {
        "type": "bogus",
        "utc": "2026-10-15T12:00:00Z",
        "local_date": START_DATE,
        "utc_offset": "+00:00",
    }
    first = _chain_line(1, "0" * 64, body)
    path.write_bytes(first + b"\n")
    with pytest.raises(SelfTestError) as caught:
        Ledger(path).load()
    assert caught.value.code == "ledger_tampered"

    good = {
        "type": "block_before",
        "utc": "2026-10-15T12:00:00Z",
        "local_date": START_DATE,
        "utc_offset": "+00:00",
        "group_id": "g1",
        "total": "1.00",
    }
    one = _chain_line(1, "0" * 64, good)
    two = _chain_line(1, hashlib.sha256(one).hexdigest(), good)
    path.write_bytes(one + b"\n" + two + b"\n")
    with pytest.raises(SelfTestError) as caught:
        Ledger(path).load()
    assert caught.value.code == "ledger_tampered"


def test_ledger_detects_torn_final_line(tmp_path: Path) -> None:
    path = tmp_path / "ledger.jsonl"
    ledger = Ledger(path, clock=_clock)
    _three_records(ledger)
    path.write_bytes(path.read_bytes()[:-1])
    with pytest.raises(SelfTestError) as caught:
        ledger.load()
    assert caught.value.code == "ledger_tampered"


def test_ledger_rejects_unknown_type_and_extra_fields(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "ledger.jsonl", clock=_clock)
    with pytest.raises(SelfTestError) as caught:
        ledger.append("nonsense", group_id="g1")
    assert caught.value.code == "record_type_unknown"
    with pytest.raises(SelfTestError) as caught:
        ledger.append("block_before", group_id="g1", total="1.00", extra="x")
    assert caught.value.code == "record_invalid"


def test_ledger_local_date_override_and_default_state_dir(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "ledger.jsonl", clock=_clock)
    record = ledger.append("day_reconciled", local_date="2026-10-14", desktop_total="1.20")
    assert record["local_date"] == "2026-10-14"
    assert "utc_offset" in record
    path = default_state_dir("st_demo", env={"LOCALAPPDATA": str(tmp_path)})
    assert path == tmp_path / "ContextShrink" / "self-test" / "st_demo"


# ---------------------------------------------------------------- state rules


def test_state_rejects_days_out_of_order_and_mismatched_method() -> None:
    state, _ = start_study(make_pack())
    wrong_day = rec(
        "day_started",
        day=2,
        round=state.schedule[1]["round"],
        method=state.schedule[1]["method"],
        setup_confirmed=True,
    )
    with pytest.raises(SelfTestError) as caught:
        state.with_record(wrong_day)
    assert caught.value.code == "day_out_of_order"
    mismatch = rec(
        "day_started",
        day=1,
        round=1,
        method="C_tuned" if state.schedule[0]["method"] != "C_tuned" else "A",
        setup_confirmed=True,
    )
    with pytest.raises(SelfTestError) as caught:
        state.with_record(mismatch)
    assert caught.value.code == "day_mismatch"


def test_state_rejects_unconfirmed_setup() -> None:
    state, _ = start_study(make_pack())
    day = state.schedule[0]
    record = rec(
        "day_started", day=1, round=day["round"], method=day["method"], setup_confirmed=False
    )
    with pytest.raises(SelfTestError) as caught:
        state.with_record(record)
    assert caught.value.code == "record_invalid"


def test_one_measurement_day_per_local_date() -> None:
    pack = make_pack()
    state, _ = start_study(pack)
    state, _, _ = drive_day(state, "2026-10-15", 1000)
    action = state.next_action()
    record = rec(
        "day_started",
        "2026-10-15",
        day=2,
        round=action["round"],
        method=action["method"],
        setup_confirmed=True,
    )
    with pytest.raises(SelfTestError) as caught:
        state.with_record(record)
    assert caught.value.code == "one_day_per_local_date"


def test_spend_between_blocks_flags_the_day_and_it_is_not_complete() -> None:
    state, _ = start_study(make_pack())
    state, _, _ = drive_day(state, "2026-10-15", 1000, gaps={1: 50})
    assert [flag["reason"] for flag in state.flags] == ["spend_between_blocks"]
    assert state.completed_days == []
    assert state.current_day == 1


def test_meter_going_backwards_flags_the_day() -> None:
    state, _ = start_study(make_pack())
    state, _, _ = drive_day(state, "2026-10-15", 1000, gaps={1: -20})
    assert [flag["reason"] for flag in state.flags] == ["meter_went_backwards"]
    assert state.completed_days == []


def test_clean_day_completes_and_days_must_follow_order() -> None:
    state, _ = start_study(make_pack())
    state, _, total = drive_day(state, "2026-10-15", 1000)
    assert state.completed_days == [1]
    assert state.current_day == 2
    assert total == 1000 + 8 * 15


def test_day_finished_on_another_date_is_flagged() -> None:
    state, _ = start_study(make_pack())
    state, _, _ = drive_day(state, "2026-10-15", 1000, finish_date="2026-10-16")
    assert any(flag["reason"] == "finished_on_other_date" for flag in state.flags)
    assert state.completed_days == []


def test_typo_delta_over_twenty_dollars_is_rejected() -> None:
    state, _ = start_study(make_pack())
    action = state.next_action()
    state = state.with_record(
        rec(
            "day_started",
            day=1,
            round=action["round"],
            method=action["method"],
            setup_confirmed=True,
        )
    )
    group = state.schedule[0]["groups"][0]
    state = state.with_record(rec("block_before", group_id=group["group_id"], total="10.00"))
    for task in group["tasks"]:
        state = state.with_record(
            rec("task_answer", task_id=task, correct=True, answer_sha256="a" * 64)
        )
    with pytest.raises(SelfTestError) as caught:
        state.with_record(rec("block_after", group_id=group["group_id"], total="31.00"))
    assert caught.value.code == "reading_delta_too_large"
    state.with_record(rec("block_after", group_id=group["group_id"], total="30.00"))


def test_block_after_cannot_go_below_before_and_must_follow_answers() -> None:
    state, _ = start_study(make_pack())
    action = state.next_action()
    state = state.with_record(
        rec(
            "day_started",
            day=1,
            round=action["round"],
            method=action["method"],
            setup_confirmed=True,
        )
    )
    group = state.schedule[0]["groups"][0]
    state = state.with_record(rec("block_before", group_id=group["group_id"], total="10.00"))
    with pytest.raises(SelfTestError) as caught:
        state.with_record(rec("block_after", group_id=group["group_id"], total="10.00"))
    assert caught.value.code == "block_incomplete"
    for task in group["tasks"]:
        state = state.with_record(
            rec("task_answer", task_id=task, correct=True, answer_sha256="a" * 64)
        )
    with pytest.raises(SelfTestError) as caught:
        state.with_record(rec("block_after", group_id=group["group_id"], total="9.00"))
    assert caught.value.code == "reading_decreased"


@pytest.mark.parametrize("total", ["1.5", "-1.00", "1e3", "100000.01", "12.345", " 1.00"])
def test_readings_must_be_two_decimal_non_negative_amounts(total: str) -> None:
    state, _ = start_study(make_pack())
    action = state.next_action()
    state = state.with_record(
        rec(
            "day_started",
            day=1,
            round=action["round"],
            method=action["method"],
            setup_confirmed=True,
        )
    )
    with pytest.raises(SelfTestError) as caught:
        state.with_record(
            rec("block_before", group_id=state.schedule[0]["groups"][0]["group_id"], total=total)
        )
    assert caught.value.code == "reading_invalid"


def test_task_answers_follow_schedule_order_and_block_order() -> None:
    state, _ = start_study(make_pack())
    action = state.next_action()
    state = state.with_record(
        rec(
            "day_started",
            day=1,
            round=action["round"],
            method=action["method"],
            setup_confirmed=True,
        )
    )
    group = state.schedule[0]["groups"][0]
    state = state.with_record(rec("block_before", group_id=group["group_id"], total="10.00"))
    other_group = state.schedule[0]["groups"][1]["group_id"]
    with pytest.raises(SelfTestError) as caught:
        state.with_record(rec("block_before", group_id=other_group, total="10.00"))
    assert caught.value.code == "block_open"
    wrong_task = group["tasks"][1]
    with pytest.raises(SelfTestError) as caught:
        state.with_record(
            rec("task_answer", task_id=wrong_task, correct=True, answer_sha256="a" * 64)
        )
    assert caught.value.code == "task_out_of_order"


def test_block_before_must_be_the_next_group() -> None:
    state, _ = start_study(make_pack())
    action = state.next_action()
    state = state.with_record(
        rec(
            "day_started",
            day=1,
            round=action["round"],
            method=action["method"],
            setup_confirmed=True,
        )
    )
    second = state.schedule[0]["groups"][1]["group_id"]
    with pytest.raises(SelfTestError) as caught:
        state.with_record(rec("block_before", group_id=second, total="10.00"))
    assert caught.value.code == "block_not_next"


def test_day_cannot_finish_with_missing_blocks() -> None:
    state, _ = start_study(make_pack())
    action = state.next_action()
    state = state.with_record(
        rec(
            "day_started",
            day=1,
            round=action["round"],
            method=action["method"],
            setup_confirmed=True,
        )
    )
    with pytest.raises(SelfTestError) as caught:
        state.with_record(rec("day_finished", status="complete"))
    assert caught.value.code == "day_incomplete"


def test_void_and_redo_flags_the_day_but_keeps_the_study_moving() -> None:
    state, _ = start_study(make_pack())
    state, _, _ = drive_day(state, "2026-10-15", 1000, void_after=("g3",))
    assert [entry["group_id"] for entry in state.voided_blocks] == ["g3"]
    assert state.voided_blocks[0]["reason"] == "interrupted"
    assert any(flag["reason"] == "block_voided" for flag in state.flags)
    assert state.completed_days == []
    assert state.current_day == 1


def test_an_abandoned_day_is_redone_on_another_date() -> None:
    state, _ = start_study(make_pack())
    action = state.next_action()
    state = state.with_record(
        rec(
            "day_started",
            day=1,
            round=action["round"],
            method=action["method"],
            setup_confirmed=True,
        )
    )
    state = state.with_record(rec("day_finished", status="abandoned"))
    assert state.current_day == 1
    state, _, _ = drive_day(state, "2026-10-16", 1000)
    assert state.completed_days == [1]


def test_pack_and_schedule_in_study_started_are_checked() -> None:
    pack = make_pack()
    fields = study_started_fields(pack, SEED)
    state = StudyState.from_ledger(pack, [])
    with pytest.raises(SelfTestError) as caught:
        state.with_record(rec("study_started", **{**fields, "pack_digest": "e" * 64}))
    assert caught.value.code == "pack_mismatch"
    changed = list(fields["schedule"])
    changed[0] = {**changed[0], "method": "A" if changed[0]["method"] != "A" else "B"}
    with pytest.raises(SelfTestError) as caught:
        state.with_record(rec("study_started", **{**fields, "schedule": changed}))
    assert caught.value.code == "schedule_mismatch"


def test_next_action_walks_one_group_then_finishes() -> None:
    state, _ = start_study(make_pack())
    assert state.next_action()["action"] == "start_day"
    action = state.next_action()
    state = state.with_record(
        rec(
            "day_started",
            day=1,
            round=action["round"],
            method=action["method"],
            setup_confirmed=True,
        )
    )
    step = state.next_action()
    assert step["action"] == "read_before"
    assert step["group_id"] == state.schedule[0]["groups"][0]["group_id"]
    state = state.with_record(rec("block_before", group_id=step["group_id"], total="10.00"))
    assert state.current_block == step["group_id"]
    assert state.next_task == state.schedule[0]["groups"][0]["tasks"][0]


def test_replayed_ledger_matches_the_live_state(tmp_path: Path) -> None:
    pack = make_pack()
    state, records = run_study(pack, days=3)
    ledger = Ledger(tmp_path / "ledger.jsonl", clock=_clock)
    for record in records:
        ledger.append_record(record)
    replayed = StudyState.from_ledger(pack, ledger.load())
    assert analyze(pack, replayed) == analyze(pack, state)


# ---------------------------------------------------------------- reconciliation


def test_reconciliation_passes_at_the_tolerance_boundary() -> None:
    pack = make_pack()
    state, _ = start_study(pack)
    state, _, total = drive_day(state, "2026-10-15", 1000)
    span = total - 1000
    assert span == 120
    # tolerance is 0.01 x (8 groups + 1) = 0.09
    assert reconcile(state, "2026-10-15", "1.29")["passed"] is True
    assert reconcile(state, "2026-10-15", "1.11")["passed"] is True
    failed = reconcile(state, "2026-10-15", "1.30")
    assert failed["passed"] is False
    assert failed["tolerance"] == "0.09"
    assert failed["difference"] == "0.10"


def test_reconciliation_needs_a_finished_clean_day() -> None:
    state, _ = start_study(make_pack())
    with pytest.raises(SelfTestError) as caught:
        reconcile(state, "2026-10-15", "1.00")
    assert caught.value.code == "day_not_found"
    action = state.next_action()
    state = state.with_record(
        rec(
            "day_started",
            day=1,
            round=action["round"],
            method=action["method"],
            setup_confirmed=True,
        )
    )
    with pytest.raises(SelfTestError) as caught:
        reconcile(state, START_DATE, "1.00")
    assert caught.value.code == "day_not_reconcilable"


# ---------------------------------------------------------------- full studies


def test_full_simulated_study_supports_b_over_c_normal() -> None:
    pack = make_pack()
    state, _ = run_study(pack)
    report = analyze(pack, state)
    comparisons = {item["key"]: item for item in report["comparisons"]}
    primary = comparisons["B_vs_C_normal"]
    assert primary["primary"] is True
    assert primary["decision"]["claim"] == "savings_supported"
    assert primary["decision"]["reasons"] == []
    assert primary["interval"]["units"] == 16
    assert primary["interval"]["clusters"] == 8
    assert report["progress"]["study_complete"] is True
    assert Decimal(report["methods"]["B"]["spend"]) < Decimal(
        report["methods"]["C_normal"]["spend"]
    )
    assert report["quality"]["regression"] is False
    assert report["flagged_days"] == []


def test_one_unreconciled_day_withholds_the_claim_and_drops_units() -> None:
    pack = make_pack()
    b_round_one = next(
        index
        for index, day in enumerate(build_schedule(pack, SEED))
        if day["round"] == 1 and day["method"] == "B"
    )
    state, _ = run_study(pack, skip_reconcile=frozenset({b_round_one}))
    report = analyze(pack, state)
    primary = next(item for item in report["comparisons"] if item["key"] == "B_vs_C_normal")
    assert primary["decision"]["claim"] == "none"
    assert "provenance_unverified" in primary["decision"]["reasons"]
    assert "below_min_units" in primary["decision"]["reasons"]
    assert primary["interval"]["units"] == 8
    assert len(report["pending_reconciliations"]) == 1


def test_too_many_wrong_b_answers_give_quality_regression() -> None:
    pack = make_pack()
    state, _ = run_study(
        pack, wrong=lambda method, task_id: method == "B" and task_id.endswith("_a")
    )
    report = analyze(pack, state)
    primary = next(item for item in report["comparisons"] if item["key"] == "B_vs_C_normal")
    assert primary["decision"]["claim"] == "none"
    assert "quality_regression" in primary["decision"]["reasons"]
    assert report["quality"]["regression"] is True
    assert report["methods"]["B"]["correct"] < report["methods"]["C_normal"]["correct"]


def test_partial_study_reports_progress_without_a_claim() -> None:
    pack = make_pack()
    state, _ = run_study(pack, days=2)
    report = analyze(pack, state)
    assert report["progress"]["days_complete"] == 2
    assert report["progress"]["current_day"] == 3
    assert report["progress"]["study_complete"] is False
    assert all(item["decision"]["claim"] == "none" for item in report["comparisons"])
    statuses = [day["status"] for day in report["days"]]
    assert statuses[:2] == ["complete", "complete"]
    assert statuses[2:] == ["not_started"] * 6


def test_report_uses_strings_for_money_and_leaks_no_names_or_answers() -> None:
    pack = make_pack()
    state, _ = run_study(pack)
    report = analyze(pack, state)
    text = json.dumps(report)
    assert "file_1.txt" not in text
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
    assert report["comparisons"][0]["interval"]["lower"].count(".") == 1


def test_analyze_rejects_a_different_pack() -> None:
    pack = make_pack()
    state, _ = start_study(pack)
    other = make_pack()
    other["study_id"] = "st_other_study"
    with pytest.raises(SelfTestError) as caught:
        analyze(other, state)
    assert caught.value.code == "pack_mismatch"


def test_text_answers_match_whole_words_only() -> None:
    task = {"answer": {"kind": "text_contains", "expected": "90 days"}}
    assert check_answer(task, "Notice period is 90 days.\nANSWER: 90 days")
    assert check_answer(task, "ANSWER: (90 days)")
    assert not check_answer(task, "ANSWER: 190 days")
    assert not check_answer(task, "ANSWER: 90 days2")
