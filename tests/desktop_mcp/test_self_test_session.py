from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from horizon.desktop_mcp.self_test import Ledger, SelfTestError, answer_digest
from horizon.desktop_mcp.self_test_session import (
    SESSION_METHODS,
    SESSION_REPORT_KIND,
    SessionState,
    analyze_session,
    session_ledger,
    session_order,
    session_pack_digest,
    session_prompt,
    session_setup,
    session_study_started_fields,
    validate_session_pack,
)

ENDING = '\n\nEnd with one line that starts with "ANSWER:" and gives only the final answer.'
DOCS = [
    "orders_export.json",
    "service_events.txt",
    "operations_handbook.md",
    "platform_config.json",
]
SEED = "ab" * 32
OTHER_SEED = "cd" * 32
START_DATE = "2026-10-15"
BASE = datetime(2026, 10, 15, 9, 0, tzinfo=timezone.utc)


def make_session_pack(tasks: int = 12) -> dict[str, Any]:
    """Session pack: four documents, each question on one of them, in file order."""
    items = []
    for number in range(1, tasks + 1):
        items.append(
            {
                "task_id": f"t_s{number:02d}",
                "source_files": [DOCS[(number - 1) % len(DOCS)]],
                "question": f"Question {number}?",
                "answer": {"kind": "text_contains", "expected": "ok"},
            }
        )
    return {
        "schema_version": 1,
        "kind": "self_test_session_pack",
        "study_id": "st_session_test",
        "model_label": "Test model at standard effort",
        "quality_tolerance": 1,
        "methods": ["B", "C_normal"],
        "documents": list(DOCS),
        "tasks": items,
    }


def rec(kind: str, local_date: str = START_DATE, utc: str | None = None, **fields: Any) -> dict:
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


def start(pack: dict[str, Any], seed: str = SEED) -> SessionState:
    state = SessionState(pack)
    return state.with_record(rec("study_started", **session_study_started_fields(pack, seed)))


def drive(
    pack: dict[str, Any],
    *,
    seed: str = SEED,
    cost: dict[str, int] | None = None,
    wrong: Any = None,
    void: tuple[str, int] | None = None,
    stop_after_chats: int | None = None,
    reconcile: bool = True,
    adjust: int = 0,
    max_steps: int = 500,
) -> tuple[SessionState, list[dict[str, Any]]]:
    """Run a whole session the way the wizard does, following next_action().

    ``cost`` is the spend of each chat in cents. ``wrong(method, number)`` marks answers
    incorrect. ``void`` voids the first time that question is reached, after half the
    chat's spend. The desktop line is the measured span plus ``adjust`` cents.
    """
    cost = cost or {"B": 1200, "C_normal": 1500}
    state = start(pack, seed)
    records = [rec("study_started", **session_study_started_fields(pack, seed))]
    clock = BASE
    meter = 100000
    voided_once = False

    def emit(kind: str, **fields: Any) -> None:
        nonlocal state, clock
        clock = clock + timedelta(seconds=30)
        record = rec(kind, utc=_stamp(clock), **fields)
        state = state.with_record(record)
        records.append(record)

    for _ in range(max_steps):
        if stop_after_chats is not None and len(state.done_chats()) >= stop_after_chats:
            break
        action = state.next_action()
        kind = action["action"]
        if kind == "start_chat":
            emit("chat_started", method=action["method"], total=_tt(meter), setup_confirmed=True)
        elif kind == "ask_question":
            method, number = action["method"], action["number"]
            if void == (method, number) and not voided_once:
                voided_once = True
                meter += cost[method] // 2
                emit("chat_voided", method=method, reason="interrupted")
                continue
            is_wrong = bool(wrong and wrong(method, number))
            emit(
                "task_answer",
                method=method,
                task_id=action["task_id"],
                correct=not is_wrong,
                answer_sha256=answer_digest(action["task_id"]),
            )
        elif kind == "read_after":
            meter += cost[action["method"]]
            emit("chat_ended", method=action["method"], total=_tt(meter))
        else:
            break

    if reconcile:
        for local_date in state.dates_with_readings():
            summary = state.date_summary(local_date)
            span = int(summary["span"] * 100)
            moment = _stamp(summary["ready_at"] + timedelta(minutes=1))
            record = rec(
                "day_reconciled",
                local_date=local_date,
                utc=moment,
                desktop_total=_tt(span + adjust),
            )
            state = state.with_record(record)
            records.append(record)
    return state, records


def replay(pack: dict[str, Any], records: list[dict[str, Any]]) -> SessionState:
    return SessionState.from_ledger(pack, records)


# ------------------------------------------------------------------ pack validation


def test_valid_session_pack_is_normalised_and_digest_is_stable() -> None:
    pack = make_session_pack()
    checked = validate_session_pack(pack)
    assert checked["methods"] == ["B", "C_normal"]
    assert checked["documents"] == DOCS
    assert session_pack_digest(pack) == session_pack_digest(copy.deepcopy(pack))
    other = make_session_pack()
    other["model_label"] = "Another model"
    assert session_pack_digest(other) != session_pack_digest(pack)


@pytest.mark.parametrize("tasks", [4, 12, 40])
def test_task_count_bounds_are_inclusive(tasks: int) -> None:
    assert len(validate_session_pack(make_session_pack(tasks))["tasks"]) == tasks


@pytest.mark.parametrize(
    ("mutate", "code"),
    [
        (lambda p: p.update(methods=["B"]), "session_methods_invalid"),
        (lambda p: p.update(methods=["C_normal", "B", "A"]), "session_methods_invalid"),
        (lambda p: p.update(methods=["B", "C_tuned"]), "session_methods_invalid"),
        (lambda p: p.update(methods=["C_normal", "B"]), "session_methods_invalid"),
        (lambda p: p.update(kind="self_test_pack"), "pack_schema_invalid"),
        (lambda p: p.update(schema_version=2), "pack_schema_invalid"),
        (lambda p: p.update(quality_tolerance=5), "quality_tolerance_invalid"),
        (lambda p: p.update(study_id="bad id"), "study_id_invalid"),
        (lambda p: p.update(documents=DOCS[:3]), "documents_mismatch"),
        (lambda p: p.update(documents=DOCS + ["extra.txt"]), "documents_mismatch"),
        (lambda p: p.update(documents=list(reversed(DOCS))), "documents_mismatch"),
        (lambda p: p.update(documents=DOCS + [DOCS[0]]), "documents_mismatch"),
        (lambda p: p.update(documents=["../escape.txt"] + DOCS[1:]), "source_file_name_invalid"),
        (lambda p: p.update(tasks=p["tasks"][:3]), "tasks_invalid"),
        (lambda p: p.update(extra=1), "pack_invalid"),
        (lambda p: p.pop("documents"), "pack_invalid"),
    ],
)
def test_session_pack_rejects_invalid_values(mutate: Any, code: str) -> None:
    pack = make_session_pack()
    mutate(pack)
    with pytest.raises(SelfTestError) as caught:
        validate_session_pack(pack)
    assert caught.value.code == code


@pytest.mark.parametrize("tasks", [3, 41])
def test_task_count_outside_four_to_forty_is_refused(tasks: int) -> None:
    with pytest.raises(SelfTestError) as caught:
        validate_session_pack(make_session_pack(tasks))
    assert caught.value.code == "tasks_invalid"


def test_duplicate_task_ids_are_refused() -> None:
    pack = make_session_pack()
    pack["tasks"][1]["task_id"] = pack["tasks"][0]["task_id"]
    with pytest.raises(SelfTestError) as caught:
        validate_session_pack(pack)
    assert caught.value.code == "task_id_duplicate"


def test_answer_specs_and_task_files_reuse_the_single_question_rules() -> None:
    pack = make_session_pack()
    pack["tasks"][0]["answer"] = {"kind": "number", "expected": "1.5", "tolerance": "x"}
    with pytest.raises(SelfTestError) as caught:
        validate_session_pack(pack)
    assert caught.value.code == "answer_tolerance_invalid"


# -------------------------------------------------------------------- order and prompts


def test_chat_order_is_deterministic_and_seed_sensitive() -> None:
    assert session_order(SEED) == session_order(SEED)
    assert sorted(session_order(SEED)) == sorted(SESSION_METHODS)
    orders = {session_order(f"{number:064x}") for number in range(40)}
    assert orders == {("B", "C_normal"), ("C_normal", "B")}


def test_order_needs_a_valid_seed() -> None:
    with pytest.raises(SelfTestError) as caught:
        session_order("zz")
    assert caught.value.code == "seed_invalid"


def test_first_message_for_b_names_the_files_and_attaches_nothing() -> None:
    pack = make_session_pack()
    text = session_prompt(pack, "B", 1)
    expected = ", ".join(DOCS)
    assert (
        f"The files {expected} are in my approved ContextShrink folder. "
        "I will ask you 12 questions about them, one at a time. "
        "Use the ContextShrink tools to answer each from the files."
    ) in text
    assert "to read" not in text
    assert "I have attached" not in text
    assert text.endswith("Question 1?" + ENDING)


def test_first_message_for_c_attaches_the_documents_and_names_no_mode() -> None:
    pack = make_session_pack()
    text = session_prompt(pack, "C_normal", 1)
    expected = ", ".join(DOCS)
    assert (
        f"I have attached {expected}. I will ask you 12 questions about them, one at a time. "
        "Answer each from the files."
    ) in text
    assert "ContextShrink" not in text
    assert "approved folder" not in text


def test_later_questions_carry_no_preamble() -> None:
    pack = make_session_pack()
    for method in SESSION_METHODS:
        assert session_prompt(pack, method, 2) == "Question 2?" + ENDING
    with pytest.raises(SelfTestError):
        session_prompt(pack, "B", 13)
    with pytest.raises(SelfTestError):
        session_prompt(pack, "A", 1)


def test_setup_for_b_turns_the_connector_on_and_checks_stats_before_reading() -> None:
    steps = session_setup("B", make_session_pack(), restart=False)
    text = " ".join(steps)
    assert "Turn the connector ON." in text
    assert "show ContextShrink stats" in text
    assert "before the total is read" in text
    assert "Fully quit" not in text


def test_setup_names_the_restart_only_when_the_setting_changed() -> None:
    pack = make_session_pack()
    assert "Fully quit Claude Desktop" in " ".join(session_setup("B", pack, restart=True))
    assert "Fully quit" in " ".join(session_setup("C_normal", pack, restart=True))
    plain = " ".join(session_setup("C_normal", pack, restart=False))
    assert "Turn the ContextShrink connector OFF." in plain
    assert "show ContextShrink stats" not in plain
    assert "Fully quit" not in plain


# ------------------------------------------------------------------------- ledger


def test_session_ledger_round_trips_and_detects_tampering(tmp_path: Path) -> None:
    pack = make_session_pack()
    path = tmp_path / "ledger.jsonl"
    ledger = session_ledger(path, clock=lambda: BASE)
    ledger.append_record(ledger.build("study_started", **session_study_started_fields(pack, SEED)))
    ledger.append("chat_started", method="B", total="1000.00", setup_confirmed=True)
    records = session_ledger(path).load()
    assert [r["type"] for r in records] == ["study_started", "chat_started"]
    path.write_text(path.read_text(encoding="utf-8").replace("1000.00", "9.00"), encoding="utf-8")
    with pytest.raises(SelfTestError) as caught:
        session_ledger(path).load()
    assert caught.value.code == "ledger_tampered"


def test_session_and_single_ledgers_refuse_each_others_record_types(tmp_path: Path) -> None:
    session = session_ledger(tmp_path / "s.jsonl", clock=lambda: BASE)
    with pytest.raises(SelfTestError) as caught:
        session.append("block_before", group_id="g01", total="1.00")
    assert caught.value.code == "record_type_unknown"
    single = Ledger(tmp_path / "single.jsonl", clock=lambda: BASE)
    with pytest.raises(SelfTestError) as caught:
        single.append("chat_started", method="B", total="1.00", setup_confirmed=True)
    assert caught.value.code == "record_type_unknown"


def test_chat_void_reason_is_limited_to_known_reasons() -> None:
    pack = make_session_pack()
    state = start(pack)
    method = state.order[0]
    state = state.with_record(
        rec("chat_started", method=method, total="10.00", setup_confirmed=True)
    )
    with pytest.raises(SelfTestError) as caught:
        state.with_record(rec("chat_voided", method=method, reason="bored"))
    assert caught.value.code == "record_invalid"


# ------------------------------------------------------------------- state rules


def test_chat_order_and_method_are_enforced() -> None:
    pack = make_session_pack()
    state = start(pack)
    wrong = "C_normal" if state.order[0] == "B" else "B"
    with pytest.raises(SelfTestError) as caught:
        state.with_record(rec("chat_started", method=wrong, total="10.00", setup_confirmed=True))
    assert caught.value.code == "method_out_of_order"


def test_study_started_must_match_the_seed_order() -> None:
    pack = make_session_pack()
    fields = session_study_started_fields(pack, SEED)
    fields["order"] = list(reversed(fields["order"]))
    with pytest.raises(SelfTestError) as caught:
        SessionState(pack).with_record(rec("study_started", **fields))
    assert caught.value.code == "order_mismatch"


def test_questions_must_be_answered_in_order_and_chat_must_be_complete() -> None:
    pack = make_session_pack()
    state = start(pack)
    method = state.order[0]
    state = state.with_record(
        rec("chat_started", method=method, total="10.00", setup_confirmed=True)
    )
    with pytest.raises(SelfTestError) as caught:
        state.with_record(
            rec("task_answer", method=method, task_id="t_s02", correct=True, answer_sha256="a" * 64)
        )
    assert caught.value.code == "task_out_of_order"
    with pytest.raises(SelfTestError) as caught:
        state.with_record(rec("chat_ended", method=method, total="11.00"))
    assert caught.value.code == "chat_incomplete"


def test_readings_never_fall_and_a_chat_adds_at_most_fifty_dollars() -> None:
    pack = make_session_pack(4)
    state = start(pack)
    method = state.order[0]
    state = state.with_record(
        rec("chat_started", method=method, total="100.00", setup_confirmed=True)
    )
    for number in range(1, 5):
        state = state.with_record(
            rec(
                "task_answer",
                method=method,
                task_id=f"t_s{number:02d}",
                correct=True,
                answer_sha256="a" * 64,
            )
        )
    with pytest.raises(SelfTestError) as caught:
        state.with_record(rec("chat_ended", method=method, total="99.00"))
    assert caught.value.code == "reading_decreased"
    with pytest.raises(SelfTestError) as caught:
        state.with_record(rec("chat_ended", method=method, total="151.00"))
    assert caught.value.code == "reading_delta_too_large"


def test_full_study_follows_the_order_and_reports_savings() -> None:
    pack = make_session_pack()
    state, _ = drive(pack)
    assert state.study_complete
    assert [chat["method"] for chat in state.done_chats()] == list(state.order)
    assert state.next_action() == {"action": "done"}


def test_savings_verdict_when_b_costs_less_and_quality_holds() -> None:
    pack = make_session_pack()
    state, _ = drive(pack, cost={"B": 1200, "C_normal": 1500})
    report = analyze_session(pack, state)
    assert report["kind"] == SESSION_REPORT_KIND
    assert report["verdict"] == "savings_observed"
    assert report["reasons"] == []
    assert report["methods"]["B"] == {
        "spend": "12.00",
        "correct": 12,
        "tasks": 12,
        "cost_per_correct": "1.00",
    }
    assert report["methods"]["C_normal"]["spend"] == "15.00"
    assert report["B_minus_C"] == "-3.00"
    assert report["B_vs_C_percent"] == "-20.0"
    assert report["reconciliation"]["status"] == "passed"
    assert report["scope"].startswith("one paired session on this account")
    assert "no confidence interval" in report["scope"]


def test_cost_increase_verdict_when_b_costs_more() -> None:
    pack = make_session_pack()
    state, _ = drive(pack, cost={"B": 1500, "C_normal": 1200})
    report = analyze_session(pack, state)
    assert report["verdict"] == "cost_increase_observed"
    assert report["reasons"] == []
    assert report["B_minus_C"] == "3.00"


def test_no_difference_verdict_when_costs_are_equal_to_the_cent() -> None:
    pack = make_session_pack()
    state, _ = drive(pack, cost={"B": 1234, "C_normal": 1234})
    report = analyze_session(pack, state)
    assert report["verdict"] == "no_difference"
    assert report["B_minus_C"] == "0.00"


def test_none_with_study_incomplete_when_only_one_chat_is_done() -> None:
    pack = make_session_pack()
    state, _ = drive(pack, stop_after_chats=1)
    report = analyze_session(pack, state)
    assert report["verdict"] == "none"
    assert report["reasons"] == ["study_incomplete"]
    assert report["methods"]["C_normal"]["spend"] is None
    assert report["B_minus_C"] is None


def test_none_with_reconciliation_pending_when_no_date_was_checked() -> None:
    pack = make_session_pack()
    state, _ = drive(pack, reconcile=False)
    report = analyze_session(pack, state)
    assert report["verdict"] == "none"
    assert report["reasons"] == ["reconciliation_pending"]
    assert report["reconciliation"]["status"] == "pending"


def test_none_with_reconciliation_failed_when_the_line_is_far_off() -> None:
    pack = make_session_pack()
    state, _ = drive(pack, adjust=10)
    report = analyze_session(pack, state)
    assert report["verdict"] == "none"
    assert report["reasons"] == ["reconciliation_failed"]
    assert report["reconciliation"]["status"] == "failed"


def test_a_date_within_tolerance_passes() -> None:
    pack = make_session_pack()
    state, _ = drive(pack, adjust=2)
    report = analyze_session(pack, state)
    assert report["reconciliation"]["status"] == "passed"
    assert report["verdict"] == "savings_observed"


def test_none_with_quality_regression_when_b_is_cheaper_but_loses_answers() -> None:
    pack = make_session_pack()
    state, _ = drive(pack, wrong=lambda method, number: method == "B" and number <= 3)
    report = analyze_session(pack, state)
    assert report["quality"]["regression"] is True
    assert report["verdict"] == "none"
    assert report["reasons"] == ["quality_regression"]


def test_quality_regression_does_not_block_a_cost_increase_verdict() -> None:
    pack = make_session_pack()
    state, _ = drive(
        pack,
        cost={"B": 1500, "C_normal": 1200},
        wrong=lambda method, number: method == "B" and number <= 3,
    )
    report = analyze_session(pack, state)
    assert report["verdict"] == "cost_increase_observed"


def test_a_voided_chat_is_excluded_and_redone_in_a_new_chat() -> None:
    pack = make_session_pack()
    state, _ = drive(pack, void=("B", 5))
    voided = [chat for chat in state.chats if chat["voided"]]
    assert len(state.chats) == 3
    assert [chat["method"] for chat in voided] == ["B"]
    assert voided[0]["void_reason"] == "interrupted"
    report = analyze_session(pack, state)
    assert report["verdict"] == "savings_observed"
    # The voided chat's half spend stays in the date span, so the desktop line still matches.
    assert report["reconciliation"]["status"] == "passed"
    assert report["methods"]["B"]["correct"] == 12
    assert report["methods"]["B"]["spend"] == "12.00"
    statuses = [row["status"] for row in report["chats"]]
    if state.order[0] == "B":
        assert statuses == ["voided", "included", "included"]
    else:
        assert statuses == ["included", "voided", "included"]


def test_voiding_a_checked_date_is_refused() -> None:
    pack = make_session_pack()
    state, _ = drive(pack)
    method = state.chats[-1]["method"]
    with pytest.raises(SelfTestError) as caught:
        state.with_record(rec("chat_voided", method=method, reason="other"))
    assert caught.value.code == "date_already_reconciled"


def test_a_chat_that_crosses_midnight_must_be_voided_then_redone() -> None:
    pack = make_session_pack()
    state = start(pack)
    method = state.order[0]
    state = state.with_record(
        rec("chat_started", method=method, total="10.00", setup_confirmed=True)
    )
    for number in range(1, 13):
        state = state.with_record(
            rec(
                "task_answer",
                method=method,
                task_id=f"t_s{number:02d}",
                correct=True,
                answer_sha256="a" * 64,
            )
        )
    state = state.with_record(
        rec("chat_ended", local_date="2026-10-16", method=method, total="12.00")
    )
    assert state.next_action() == {"action": "void_midnight", "method": method}
    state = state.with_record(rec("chat_voided", method=method, reason="spans_midnight"))
    assert state.next_action()["action"] == "start_chat"
    assert state.next_action()["method"] == method
    report = analyze_session(pack, state)
    assert report["progress"]["chats_complete"] == 0


def test_resume_mid_chat_continues_at_the_next_question(tmp_path: Path) -> None:
    pack = make_session_pack()
    path = tmp_path / "ledger.jsonl"
    ledger = session_ledger(path, clock=lambda: BASE)
    fields = session_study_started_fields(pack, SEED)
    ledger.append_record(ledger.build("study_started", **fields))
    method = fields["order"][0]
    ledger.append("chat_started", method=method, total="10.00", setup_confirmed=True)
    for number in range(1, 4):
        ledger.append(
            "task_answer",
            method=method,
            task_id=f"t_s{number:02d}",
            correct=True,
            answer_sha256="a" * 64,
        )
    # A new process: nothing kept in memory, only the ledger file.
    restored = SessionState.from_ledger(pack, session_ledger(path).load())
    action = restored.next_action()
    assert action["action"] == "ask_question"
    assert action["number"] == 4
    assert action["method"] == method
    assert restored.open_chat() is not None


def test_reconciliation_waits_four_hours_and_is_final() -> None:
    pack = make_session_pack()
    state, records = drive(pack, reconcile=False)
    local_date = state.dates_with_readings()[0]
    summary = state.date_summary(local_date)
    early = rec(
        "day_reconciled",
        local_date=local_date,
        utc=_stamp(summary["ready_at"] - timedelta(minutes=1)),
        desktop_total="1.00",
    )
    with pytest.raises(SelfTestError) as caught:
        state.with_record(early)
    assert caught.value.code == "reconcile_too_early"

    late = rec(
        "day_reconciled",
        local_date=local_date,
        utc=_stamp(summary["ready_at"] + timedelta(minutes=1)),
        desktop_total=str(summary["span"]),
    )
    checked = state.with_record(late)
    assert checked.date_status(local_date) == "passed"
    with pytest.raises(SelfTestError) as caught:
        checked.with_record(late)
    assert caught.value.code == "date_already_reconciled"


def test_reconciliation_refused_while_a_chat_is_open() -> None:
    pack = make_session_pack()
    state = start(pack)
    state = state.with_record(rec("chat_started", method="B", total="10.00", setup_confirmed=True))
    with pytest.raises(SelfTestError) as caught:
        state.with_record(rec("day_reconciled", utc="2026-10-15T20:00:00Z", desktop_total="0.00"))
    assert caught.value.code == "block_open"


def test_replayed_state_equals_the_live_state() -> None:
    pack = make_session_pack()
    live, records = drive(pack, void=("C_normal", 3))
    restored = replay(pack, records)
    assert analyze_session(pack, restored) == analyze_session(pack, live)


def test_analysis_refuses_a_different_pack_and_an_unstarted_study() -> None:
    pack = make_session_pack()
    state, _ = drive(pack)
    other = make_session_pack()
    other["model_label"] = "Different model"
    with pytest.raises(SelfTestError) as caught:
        analyze_session(other, state)
    assert caught.value.code == "pack_mismatch"
    with pytest.raises(SelfTestError) as caught:
        analyze_session(pack, SessionState(pack))
    assert caught.value.code == "study_not_started"


def test_report_holds_no_answer_text_or_file_names_beyond_the_scope() -> None:
    pack = make_session_pack()
    state, _ = drive(pack)
    text = repr(analyze_session(pack, state))
    assert "ANSWER" not in text
    assert "Question 1?" not in text
    assert "orders_export" not in text
    assert answer_digest("t_s01") not in text


def test_session_result_is_stable_for_the_same_seed() -> None:
    pack = make_session_pack()
    first = analyze_session(pack, drive(pack, seed=OTHER_SEED)[0])
    second = analyze_session(pack, drive(pack, seed=OTHER_SEED)[0])
    assert first == second
    assert first["order"] == list(session_order(OTHER_SEED))
    assert (
        first["seed_commitment"]
        != analyze_session(pack, drive(pack, seed=SEED)[0])["seed_commitment"]
    )
