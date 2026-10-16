from __future__ import annotations

import csv
import hashlib
import importlib.util
import json
import re
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from horizon.desktop_mcp.self_test import check_answer, study_methods, validate_pack

REPO_ROOT = Path(__file__).resolve().parents[2]
GENERATOR = REPO_ROOT / "desktop" / "mcp" / "self-test" / "make_dataset.py"
SEED = 20261015
TARGETS = {
    "operations_handbook.md": 60 * 1024,
    "orders_export.json": 120 * 1024,
    "service_events.txt": 100 * 1024,
    "platform_config.json": 40 * 1024,
    "contract_summary.md": 3 * 1024,
    "team_contacts.csv": 3 * 1024,
}
NOTICE_PREFIX = "Fictional test data"
SUPPORTED = {"txt", "md", "json", "csv", "pdf", "docx"}

# Session profile: default seed, the spec sizes (KB) and the total limit.
SESSION_SEED = 20261016
SESSION_DOCS = [
    "orders_export.json",
    "service_events.txt",
    "operations_handbook.md",
    "platform_config.json",
]
SESSION_SIZES_KB = {
    "orders_export.json": 140,
    "service_events.txt": 140,
    "operations_handbook.md": 90,
    "platform_config.json": 45,
}
SESSION_TOTAL_LIMIT = 430 * 1024

# Single profile output, captured before the session profile was added (sha256).
# The documents are the same for every variant below except the seed-7 run.
GOLDEN_SINGLE_DOCUMENTS = {
    "contract_summary.md": "dd8efa12dc3b0a2c773816565484bc424f53ed8df2e20013cbc58975b285bd20",
    "operations_handbook.md": "806a8b1f7ce36d7835f9feb4d649bc9c76183d48f08a7245dfa3af68629bbb7f",
    "orders_export.json": "7db589207b0acb75157e28dd67ae7f0e04127873569fb7412ba516692d581431",
    "platform_config.json": "108ae6843d6ccf9c16cd5edf61b03f7da2a8636e3f96abf06dc8aceaf97f39c2",
    "service_events.txt": "d4b36db9bd17c9085094cca4e097b4813716ed2034731113d5eff46e0081cefd",
    "team_contacts.csv": "3d409bca2e568333d6d2ec559e61efd2ce487b1ffbf5ce88126f760902e06d17",
}
GOLDEN_SINGLE_PACKS = {
    "default": "0e3f20d8457eabd9aa8f9190d3bc72bf4e79878577c31a8f375d8f7d9d36eb4f",
    "study_st_other_v1": "72b67af9367bd2555d780d0064eeb67a5168c3e574d66925f18433c8d5707574",
    "methods_B_C_normal": "a983c379930b7f2f67dbd4ebdc5f4f9cf550e5ff5cc06531d626c51b0df44eae",
    "methods_B_C_normal_A_study_st_x1": "a6286abfcb0309b285e51e874f8c07b3fc4718a401bdbfa23133d35ffc395d2e",
    "seed_7": "72ac7326b2c5037e8b45a9803f1dc947b2ea73691ebcca5b087fb9285b275e22",
}


def _load_generator() -> Any:
    spec = importlib.util.spec_from_file_location("make_dataset", GENERATOR)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


make_dataset = _load_generator()


def _generate(root: Path, seed: int = SEED) -> Path:
    out = root / f"out-{seed}"
    make_dataset.generate(out, seed)
    return out


def _generate_session(root: Path, seed: int | None = None) -> Path:
    out = root / f"session-{SESSION_SEED if seed is None else seed}"
    make_dataset.generate(out, seed, profile="session")
    return out


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture(scope="module")
def dataset(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return _generate(tmp_path_factory.mktemp("dataset"))


@pytest.fixture(scope="module")
def pack(dataset: Path) -> dict[str, Any]:
    return json.loads((dataset / "pack.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def session(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return _generate_session(tmp_path_factory.mktemp("session"))


@pytest.fixture(scope="module")
def session_pack(session: Path) -> dict[str, Any]:
    return json.loads((session / "pack.json").read_text(encoding="utf-8"))


def _doc(dataset: Path, name: str) -> str:
    return (dataset / "documents" / name).read_text(encoding="utf-8")


def _task(pack: dict[str, Any], task_id: str) -> dict[str, Any]:
    matches = [task for task in pack["tasks"] if task["task_id"] == task_id]
    assert len(matches) == 1
    return matches[0]


def _log_lines(dataset: Path) -> list[tuple[str, str, str, str]]:
    """Independent parse of service_events.txt: (timestamp, level, service, message)."""
    rows = []
    for line in _doc(dataset, "service_events.txt").split("\n"):
        if not _TIMESTAMPED.match(line):
            continue  # notice comment and untimestamped stack-trace frames
        match = re.fullmatch(r"(\S+) (\S+) (\S+) (.*)", line)
        assert match, line
        rows.append(match.groups())
    return rows


_TIMESTAMPED = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z ")


def _frame_runs(dataset: Path) -> list[tuple[str, ...]]:
    """Maximal runs of consecutive untimestamped lines (stack-trace blocks)."""
    runs: list[tuple[str, ...]] = []
    current: list[str] = []
    for line in _doc(dataset, "service_events.txt").split("\n"):
        if line and not line.startswith("#") and not _TIMESTAMPED.match(line):
            current.append(line)
            continue
        if current:
            runs.append(tuple(current))
            current = []
    return runs


# --------------------------------------------------------------- independent derivations


def _derive(dataset: Path, pack: dict[str, Any], task_id: str) -> str:
    """Re-derive one task's answer by parsing the written files, not the generator."""
    question = _task(pack, task_id)["question"]
    if task_id == "t_hb_dock_hold":
        found = re.findall(
            r"dock reconciliation hold lasts (\d+) hours", _doc(dataset, "operations_handbook.md")
        )
        assert len(found) == 1
        return found[0]
    if task_id == "t_hb_quarantine_code":
        weight = re.search(r"over (\d+) kg", question).group(1)  # type: ignore[union-attr]
        found = re.findall(
            rf"quarantine bay code for pallets over {weight} kg is (BAY-[A-Z]\d{{3}})",
            _doc(dataset, "operations_handbook.md"),
        )
        assert len(found) == 1
        return found[0]
    if task_id == "t_hb_audit_freeze":
        found = re.findall(
            r"annual audit freeze begins on (\d{4}-\d{2}-\d{2}),",
            _doc(dataset, "operations_handbook.md"),
        )
        assert len(found) == 1
        return found[0]
    if task_id in {"t_ord_unit_price", "t_ord_region", "t_ord_created"}:
        orders = json.loads(_doc(dataset, "orders_export.json"))["orders"]
        order_id = re.search(r"order (NW-\d+)", question).group(1)  # type: ignore[union-attr]
        matches = [order for order in orders if order["order_id"] == order_id]
        assert len(matches) == 1
        order = matches[0]
        if task_id == "t_ord_region":
            return order["region"]
        if task_id == "t_ord_created":
            return order["created"]
        sku = re.search(r"SKU (\S+) in order", question).group(1)  # type: ignore[union-attr]
        items = [item for item in order["items"] if item["sku"] == sku]
        assert len(items) == 1
        return items[0]["unit_price"]
    if task_id == "t_log_first_error":
        service = re.search(r"for service (\S+)\?", question).group(1)  # type: ignore[union-attr]
        first = next(row for row in _log_lines(dataset) if row[1] == "ERROR" and row[2] == service)
        return first[0]
    if task_id == "t_log_warn_code":
        service = re.search(r"for service (\S+) that", question).group(1)  # type: ignore[union-attr]
        found = [
            row
            for row in _log_lines(dataset)
            if row[1] == "WARN" and row[2] == service and "backlog" in row[3]
        ]
        assert len(found) == 1
        return re.search(r"code=(\S+)", found[0][3]).group(1)  # type: ignore[union-attr]
    if task_id == "t_log_restart_count":
        service = re.search(r"for service (\S+)\?", question).group(1)  # type: ignore[union-attr]
        return str(
            sum(
                1
                for row in _log_lines(dataset)
                if row[2] == service and row[3].startswith("RESTART")
            )
        )
    if task_id.startswith("t_cfg_"):
        path = re.search(r"value of (.+)\? Answer", question).group(1).split(" -> ")  # type: ignore[union-attr]
        node: Any = json.loads(_doc(dataset, "platform_config.json"))
        for key in path:
            node = node[key]
        return str(node)
    if task_id == "t_contract_notice":
        found = re.findall(
            r"at least (\d+) days before the end of the current term",
            _doc(dataset, "contract_summary.md"),
        )
        assert len(found) == 1
        return found[0]
    if task_id == "t_contact_extension":
        name = re.search(r"extension of (.+)\? Answer", question).group(1)  # type: ignore[union-attr]
        lines = [
            line
            for line in _doc(dataset, "team_contacts.csv").split("\n")
            if not line.startswith("#")
        ]
        rows = [row for row in csv.DictReader(lines) if row["name"] == name]
        assert len(rows) == 1
        return rows[0]["extension"]
    raise AssertionError(f"no derivation for {task_id}")


def _near_miss(answer: dict[str, Any]) -> str:
    expected = answer["expected"]
    if answer["kind"] == "number":
        step = Decimal("0.01") if "." in expected else Decimal(1)
        return str(Decimal(expected) + step)
    if answer["kind"] == "date":
        return (date.fromisoformat(expected) + timedelta(days=1)).isoformat()
    digits = [index for index, char in enumerate(expected) if char.isdigit()]
    if digits:
        index = digits[-1]
        bumped = (int(expected[index]) + 1) % 10
        return expected[:index] + str(bumped) + expected[index + 1 :]
    return "Not the answer"


TASK_IDS = [
    "t_hb_dock_hold",
    "t_hb_quarantine_code",
    "t_hb_audit_freeze",
    "t_ord_unit_price",
    "t_ord_region",
    "t_ord_created",
    "t_log_first_error",
    "t_log_warn_code",
    "t_log_restart_count",
    "t_cfg_nested",
    "t_contract_notice",
    "t_contact_extension",
]


# ------------------------------------------------------------------------- tests


def test_same_seed_gives_identical_bytes(tmp_path: Path) -> None:
    first = _generate(tmp_path / "a", SEED)
    second = _generate(tmp_path / "b", SEED)
    names = sorted(p.name for p in (first / "documents").iterdir())
    assert names == sorted(p.name for p in (second / "documents").iterdir())
    for name in names:
        assert (first / "documents" / name).read_bytes() == (
            second / "documents" / name
        ).read_bytes()
    assert (first / "pack.json").read_bytes() == (second / "pack.json").read_bytes()


def test_different_seed_gives_different_files(tmp_path: Path) -> None:
    first = _generate(tmp_path / "a", SEED)
    other = _generate(tmp_path / "b", SEED + 1)
    differing = [
        name
        for name in TARGETS
        if (first / "documents" / name).read_bytes() != (other / "documents" / name).read_bytes()
    ]
    assert set(differing) == set(TARGETS)


def test_output_uses_lf_only(dataset: Path) -> None:
    for name in TARGETS:
        assert b"\r" not in (dataset / "documents" / name).read_bytes(), name


def test_pack_passes_validate_pack(pack: dict[str, Any]) -> None:
    normalised = validate_pack(pack)
    assert normalised["study_id"] == "st_generated_v1"
    assert normalised["model_label"] == "Claude Opus, Medium effort"
    assert normalised["minimums"] == {"paired_units": 10, "groups": 8}
    assert normalised["quality_tolerance"] == 1
    assert len(normalised["groups"]) == 12
    assert all(len(group["tasks"]) == 1 for group in normalised["groups"])
    assert len(normalised["tasks"]) == 12


def test_pack_uses_only_supported_suffixes(pack: dict[str, Any], dataset: Path) -> None:
    for task in pack["tasks"]:
        for name in task["source_files"]:
            assert name.rsplit(".", 1)[-1] in SUPPORTED
            assert (dataset / "documents" / name).is_file()


@pytest.mark.parametrize("name", sorted(TARGETS))
def test_file_size_within_30_percent(dataset: Path, name: str) -> None:
    size = (dataset / "documents" / name).stat().st_size
    target = TARGETS[name]
    assert 0.7 * target <= size <= 1.3 * target, (name, size, target)


@pytest.mark.parametrize("task_id", TASK_IDS)
def test_answer_rederived_from_files(dataset: Path, pack: dict[str, Any], task_id: str) -> None:
    task = _task(pack, task_id)
    derived = _derive(dataset, pack, task_id)
    assert derived == task["answer"]["expected"]


@pytest.mark.parametrize("task_id", TASK_IDS)
def test_check_answer_accepts_correct_and_rejects_near_miss(
    dataset: Path, pack: dict[str, Any], task_id: str
) -> None:
    task = _task(pack, task_id)
    correct = _derive(dataset, pack, task_id)
    assert check_answer(task, f"Reasoning first.\nANSWER: {correct}")
    miss = _near_miss(task["answer"])
    assert miss != correct
    assert not check_answer(task, f"ANSWER: {miss}")


def test_handbook_fact_phrases_appear_once(dataset: Path) -> None:
    text = _doc(dataset, "operations_handbook.md")
    for phrase in (
        "dock reconciliation hold lasts",
        "quarantine bay code for pallets over",
        "annual audit freeze begins on",
    ):
        assert text.count(phrase) == 1, phrase


def test_order_lookup_keys_are_unique(dataset: Path, pack: dict[str, Any]) -> None:
    text = _doc(dataset, "orders_export.json")
    orders = json.loads(text)["orders"]
    assert len(orders) == 350
    assert all(re.fullmatch(r"NW-1\d{4}", order["order_id"]) for order in orders)
    for task_id in ("t_ord_unit_price", "t_ord_region", "t_ord_created"):
        question = _task(pack, task_id)["question"]
        order_id = re.search(r"order (NW-\d+)", question).group(1)  # type: ignore[union-attr]
        assert text.count(f'"order_id": "{order_id}"') == 1
        assert sum(1 for order in orders if order["order_id"] == order_id) == 1
    price_question = _task(pack, "t_ord_unit_price")["question"]
    sku, order_id = re.search(r"SKU (\S+) in order (NW-\d+)", price_question).groups()  # type: ignore[union-attr]
    order = next(order for order in orders if order["order_id"] == order_id)
    assert sum(1 for item in order["items"] if item["sku"] == sku) == 1


def test_fictional_notice_in_every_file(dataset: Path) -> None:
    for name in TARGETS:
        text = _doc(dataset, name)
        first_lines = text.split("\n")[:2]
        if name.endswith(".json"):
            assert json.loads(text)["_notice"].startswith(NOTICE_PREFIX), name
        else:
            assert any(NOTICE_PREFIX in line for line in first_lines), name
        assert text.count(NOTICE_PREFIX) == 1, name


def test_refuses_non_empty_directory(tmp_path: Path) -> None:
    out = tmp_path / "busy"
    out.mkdir()
    (out / "keep.txt").write_text("do not touch", encoding="utf-8")
    with pytest.raises(make_dataset.DatasetError):
        make_dataset.generate(out, SEED)
    assert (out / "keep.txt").read_text(encoding="utf-8") == "do not touch"
    assert not (out / "pack.json").exists()


def test_accepts_empty_existing_directory(tmp_path: Path) -> None:
    out = tmp_path / "empty"
    out.mkdir()
    assert make_dataset.main(["--out", str(out)]) == 0
    assert make_dataset.main(["--out", str(out)]) == 2


def _assert_no_identical_consecutive_lines(dataset: Path) -> None:
    lines = _doc(dataset, "service_events.txt").split("\n")
    for previous, current in zip(lines, lines[1:], strict=False):
        if previous == current and previous:
            assert not _TIMESTAMPED.match(current), current


def _assert_stack_trace_blocks_repeat(dataset: Path) -> None:
    runs = _frame_runs(dataset)
    counts: dict[tuple[str, ...], int] = {}
    for run in runs:
        counts[run] = counts.get(run, 0) + 1
    repeated = {run: count for run, count in counts.items() if count >= 3}
    assert repeated, "no stack-trace block repeats three or more times"
    for run in repeated:
        assert 6 <= len(run) <= 10
        assert run[0].startswith("java.lang.")
        assert all(line.startswith("\tat ") for line in run[1:])


def test_no_identical_consecutive_lines_outside_stack_frames(dataset: Path) -> None:
    _assert_no_identical_consecutive_lines(dataset)


def test_stack_trace_block_repeats_at_least_three_times(dataset: Path) -> None:
    _assert_stack_trace_blocks_repeat(dataset)


def test_subset_methods_and_study_id_are_written_to_the_pack(tmp_path: Path) -> None:
    out = tmp_path / "subset"
    pack = make_dataset.generate(out, SEED, "st_generated_v2", ["B", "C_normal"])
    stored = json.loads((out / "pack.json").read_text(encoding="utf-8"))
    assert stored == pack
    assert stored["study_id"] == "st_generated_v2"
    assert stored["methods"] == ["B", "C_normal"]
    assert study_methods(stored) == ("B", "C_normal")
    assert len(stored["tasks"]) == 12


def test_subset_keeps_the_same_documents_as_the_default(tmp_path: Path) -> None:
    default = _generate(tmp_path / "default", SEED)
    subset = tmp_path / "subset"
    make_dataset.generate(subset, SEED, "st_generated_v2", ["B", "C_normal"])
    for name in TARGETS:
        assert (default / "documents" / name).read_bytes() == (
            subset / "documents" / name
        ).read_bytes()


def test_default_pack_has_no_methods_key(dataset: Path) -> None:
    assert "methods" not in json.loads((dataset / "pack.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    "args",
    [
        ["--methods", "B"],
        ["--methods", "B,C_normal,X"],
        ["--methods", "B,C_normal,B"],
        ["--methods", "A,C_normal"],
        ["--study-id", "st-bad"],
    ],
)
def test_bad_dataset_options_are_refused(tmp_path: Path, args: list[str]) -> None:
    out = tmp_path / "refused"
    assert make_dataset.main(["--out", str(out), *args]) == 2
    assert not out.exists()


def test_kit_plan_numbers_come_from_the_pack() -> None:
    spec = importlib.util.spec_from_file_location(
        "build_kit", REPO_ROOT / "desktop" / "mcp" / "self-test" / "build_kit.py"
    )
    assert spec is not None and spec.loader is not None
    build_kit = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(build_kit)
    subset = {"tasks": [{}] * 12, "methods": ["B", "C_normal"]}
    full = {"tasks": [{}] * 12}
    assert build_kit._plan_numbers(subset) == {
        "tasks": "12",
        "methods": "2",
        "phases": "4",
        "chats": "24",
        "hours": "1.5",
    }
    assert build_kit._plan_numbers(full) == {
        "tasks": "12",
        "methods": "4",
        "phases": "8",
        "chats": "48",
        "hours": "3",
    }


# ------------------------------------------------------------------- single profile unchanged


def test_single_profile_output_is_byte_identical_to_before(tmp_path: Path) -> None:
    """Hashes captured from the generator before the session profile was added."""
    default = tmp_path / "default"
    assert make_dataset.main(["--out", str(default)]) == 0
    for name, digest in GOLDEN_SINGLE_DOCUMENTS.items():
        assert _sha256(default / "documents" / name) == digest, name
    assert _sha256(default / "pack.json") == GOLDEN_SINGLE_PACKS["default"]
    variants = {
        "study_st_other_v1": ["--study-id", "st_other_v1"],
        "methods_B_C_normal": ["--methods", "B,C_normal"],
        "methods_B_C_normal_A_study_st_x1": ["--methods", "B,C_normal,A", "--study-id", "st_x1"],
        "seed_7": ["--seed", "7"],
    }
    for key, args in variants.items():
        out = tmp_path / key
        assert make_dataset.main(["--out", str(out), *args]) == 0
        assert _sha256(out / "pack.json") == GOLDEN_SINGLE_PACKS[key], key


def test_single_profile_is_the_default_profile(tmp_path: Path) -> None:
    implicit = tmp_path / "implicit"
    explicit = tmp_path / "explicit"
    assert make_dataset.main(["--out", str(implicit)]) == 0
    assert make_dataset.main(["--out", str(explicit), "--profile", "single"]) == 0
    assert (implicit / "pack.json").read_bytes() == (explicit / "pack.json").read_bytes()
    assert (
        json.loads((implicit / "pack.json").read_text(encoding="utf-8"))["kind"] == "self_test_pack"
    )


# ------------------------------------------------------------------- session profile

SESSION_TASK_IDS = [
    "t_ord_unit_price",
    "t_log_first_error",
    "t_hb_dock_hold",
    "t_cfg_nested",
    "t_ord_region",
    "t_log_warn_code",
    "t_hb_quarantine_code",
    "t_cfg_port",
    "t_ord_created",
    "t_log_restart_count",
    "t_hb_audit_freeze",
    "t_cfg_search",
]


def test_session_documents_are_exactly_the_four_files(session: Path) -> None:
    assert sorted(p.name for p in (session / "documents").iterdir()) == sorted(SESSION_DOCS)
    assert sorted(p.name for p in session.iterdir()) == ["documents", "pack.json"]


@pytest.mark.parametrize("name", SESSION_DOCS)
def test_session_file_size_within_10_percent(session: Path, name: str) -> None:
    size = (session / "documents" / name).stat().st_size
    target = SESSION_SIZES_KB[name] * 1024
    assert 0.9 * target <= size <= 1.1 * target, (name, size, target)


def test_session_total_size_within_limit(session: Path) -> None:
    total = sum((session / "documents" / name).stat().st_size for name in SESSION_DOCS)
    assert total <= SESSION_TOTAL_LIMIT, total


def test_session_pack_format(session_pack: dict[str, Any]) -> None:
    assert set(session_pack) == {
        "schema_version",
        "kind",
        "study_id",
        "model_label",
        "quality_tolerance",
        "methods",
        "documents",
        "tasks",
    }
    assert session_pack["schema_version"] == 1
    assert session_pack["kind"] == "self_test_session_pack"
    assert session_pack["study_id"] == "st_session_v1"
    assert session_pack["model_label"] == "Claude Opus, Medium effort"
    assert session_pack["quality_tolerance"] == 1
    assert session_pack["methods"] == ["B", "C_normal"]
    assert session_pack["documents"] == SESSION_DOCS


def test_session_tasks_are_twelve_and_interleaved_by_file(session_pack: dict[str, Any]) -> None:
    tasks = session_pack["tasks"]
    assert len(tasks) == 12
    assert [task["task_id"] for task in tasks] == [
        "t_ord_unit_price",
        "t_log_first_error",
        "t_hb_dock_hold",
        "t_cfg_nested",
        "t_ord_region",
        "t_log_warn_code",
        "t_hb_quarantine_code",
        "t_cfg_port",
        "t_ord_created",
        "t_log_restart_count",
        "t_hb_audit_freeze",
        "t_cfg_search",
    ]
    assert [task["source_files"] for task in tasks] == [[name] for name in SESSION_DOCS * 3]
    for task in tasks:
        assert set(task) == {"task_id", "source_files", "question", "answer"}
        assert task["answer"]["kind"] in {"number", "date", "text_contains"}


@pytest.mark.parametrize("task_id", SESSION_TASK_IDS)
def test_session_answer_rederived_from_files(
    session: Path, session_pack: dict[str, Any], task_id: str
) -> None:
    task = _task(session_pack, task_id)
    assert _derive(session, session_pack, task_id) == task["answer"]["expected"]


@pytest.mark.parametrize("task_id", SESSION_TASK_IDS)
def test_session_check_answer_accepts_correct_and_rejects_near_miss(
    session: Path, session_pack: dict[str, Any], task_id: str
) -> None:
    task = _task(session_pack, task_id)
    correct = _derive(session, session_pack, task_id)
    assert check_answer(task, f"Reasoning first.\nANSWER: {correct}")
    miss = _near_miss(task["answer"])
    assert miss != correct
    assert not check_answer(task, f"ANSWER: {miss}")


def test_session_order_lookup_keys_occur_once(session: Path, session_pack: dict[str, Any]) -> None:
    text = _doc(session, "orders_export.json")
    orders = json.loads(text)["orders"]
    assert len(orders) == 389
    for task_id in ("t_ord_unit_price", "t_ord_region", "t_ord_created"):
        order_id = re.search(r"order (NW-\d+)", _task(session_pack, task_id)["question"]).group(1)  # type: ignore[union-attr]
        assert text.count(f'"order_id": "{order_id}"') == 1, order_id
        assert sum(1 for order in orders if order["order_id"] == order_id) == 1
    question = _task(session_pack, "t_ord_unit_price")["question"]
    sku, order_id = re.search(r"SKU (\S+) in order (NW-\d+)", question).groups()  # type: ignore[union-attr]
    order = next(order for order in orders if order["order_id"] == order_id)
    assert sum(1 for item in order["items"] if item["sku"] == sku) == 1


def test_session_config_service_keys_occur_once(
    session: Path, session_pack: dict[str, Any]
) -> None:
    text = _doc(session, "platform_config.json")
    for task_id in ("t_cfg_nested", "t_cfg_port", "t_cfg_search"):
        path = re.search(r"value of (.+)\? Answer", _task(session_pack, task_id)["question"])
        service = path.group(1).split(" -> ")[1]  # type: ignore[union-attr]
        assert text.count(f'"{service}": {{') == 1, service


def test_session_handbook_fact_phrases_occur_once(session: Path) -> None:
    text = _doc(session, "operations_handbook.md")
    for phrase in (
        "dock reconciliation hold lasts",
        "quarantine bay code for pallets over",
        "annual audit freeze begins on",
    ):
        assert text.count(phrase) == 1, phrase


def test_session_log_answers_match_a_full_scan(session: Path, session_pack: dict[str, Any]) -> None:
    text = _doc(session, "service_events.txt")
    # First ERROR for ledger-sync: every such line, in file order; the first is the earliest.
    errors = re.findall(r"^(\S+) ERROR ledger-sync ", text, re.M)
    assert len(errors) >= 2
    assert errors == sorted(errors)
    first = _task(session_pack, "t_log_first_error")["answer"]["expected"]
    assert first == errors[0]
    assert text.count(first) == 1
    # WARN line for cache-gateway that reports a backlog: exactly one, and its code is unique.
    codes = re.findall(r"^\S+ WARN cache-gateway (?=.*backlog).*code=(\S+)$", text, re.M)
    assert len(codes) == 1
    assert codes[0] == _task(session_pack, "t_log_warn_code")["answer"]["expected"]
    assert text.count(codes[0]) == 1
    # RESTART events for queue-relay: the only queue-relay lines are the RESTART lines.
    restart_lines = re.findall(r"^\S+ \w+ queue-relay .*$", text, re.M)
    assert restart_lines and all(" RESTART " in line for line in restart_lines)
    assert text.count(" queue-relay ") == len(restart_lines)
    expected = _task(session_pack, "t_log_restart_count")["answer"]["expected"]
    assert len(restart_lines) == int(expected)


def test_session_timestamps_are_unique_and_increasing(session: Path) -> None:
    stamps = re.findall(
        r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z) ", _doc(session, "service_events.txt"), re.M
    )
    assert len(stamps) == len(set(stamps))
    assert stamps == sorted(stamps)


def test_session_no_identical_consecutive_lines(session: Path) -> None:
    _assert_no_identical_consecutive_lines(session)


def test_session_stack_trace_blocks_repeat(session: Path) -> None:
    _assert_stack_trace_blocks_repeat(session)


def test_session_notice_in_every_file(session: Path) -> None:
    for name in SESSION_DOCS:
        text = _doc(session, name)
        if name.endswith(".json"):
            assert json.loads(text)["_notice"].startswith(NOTICE_PREFIX), name
        else:
            assert any(NOTICE_PREFIX in line for line in text.split("\n")[:2]), name
        assert text.count(NOTICE_PREFIX) == 1, name


def test_session_output_uses_lf_only(session: Path) -> None:
    for name in SESSION_DOCS:
        assert b"\r" not in (session / "documents" / name).read_bytes(), name


def test_session_same_seed_gives_identical_bytes(tmp_path: Path) -> None:
    first = _generate_session(tmp_path / "a")
    second = _generate_session(tmp_path / "b")
    for rel in [*(f"documents/{name}" for name in SESSION_DOCS), "pack.json"]:
        assert (first / rel).read_bytes() == (second / rel).read_bytes(), rel


def test_session_different_seed_changes_every_document(tmp_path: Path) -> None:
    first = _generate_session(tmp_path / "a", SESSION_SEED)
    other = _generate_session(tmp_path / "b", SESSION_SEED + 1)
    for name in SESSION_DOCS:
        assert (first / "documents" / name).read_bytes() != (
            other / "documents" / name
        ).read_bytes()


def test_session_defaults_to_its_seed_and_study_id(tmp_path: Path) -> None:
    default = tmp_path / "default"
    pack = make_dataset.generate(default, profile="session")
    explicit = tmp_path / "explicit"
    make_dataset.generate(
        explicit, seed=SESSION_SEED, study_id="st_session_v1", methods=None, profile="session"
    )
    assert pack["study_id"] == "st_session_v1"
    assert (default / "pack.json").read_bytes() == (explicit / "pack.json").read_bytes()
    for name in SESSION_DOCS:
        assert (default / "documents" / name).read_bytes() == (
            explicit / "documents" / name
        ).read_bytes()


def test_session_cli_writes_documents_and_pack(tmp_path: Path) -> None:
    out = tmp_path / "cli"
    assert make_dataset.main(["--out", str(out), "--profile", "session"]) == 0
    assert sorted(p.name for p in (out / "documents").iterdir()) == sorted(SESSION_DOCS)
    stored = json.loads((out / "pack.json").read_text(encoding="utf-8"))
    assert stored["kind"] == "self_test_session_pack"
    assert stored["study_id"] == "st_session_v1"


def test_session_study_id_can_be_overridden(tmp_path: Path) -> None:
    out = tmp_path / "study"
    assert (
        make_dataset.main(["--out", str(out), "--profile", "session", "--study-id", "st_s2"]) == 0
    )
    assert json.loads((out / "pack.json").read_text(encoding="utf-8"))["study_id"] == "st_s2"


@pytest.mark.parametrize(
    "args",
    [
        ["--methods", "B,C_normal"],
        ["--methods", "B"],
        ["--study-id", "st-bad"],
    ],
)
def test_session_bad_options_are_refused(tmp_path: Path, args: list[str]) -> None:
    out = tmp_path / "refused"
    assert make_dataset.main(["--out", str(out), "--profile", "session", *args]) == 2
    assert not out.exists()


def test_session_refuses_methods_through_generate(tmp_path: Path) -> None:
    out = tmp_path / "refused"
    with pytest.raises(make_dataset.DatasetError):
        make_dataset.generate(out, profile="session", methods=["B", "C_normal"])
    assert not out.exists()
