from __future__ import annotations

import csv
import importlib.util
import json
import re
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from horizon.desktop_mcp.self_test import check_answer, validate_pack

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


@pytest.fixture(scope="module")
def dataset(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return _generate(tmp_path_factory.mktemp("dataset"))


@pytest.fixture(scope="module")
def pack(dataset: Path) -> dict[str, Any]:
    return json.loads((dataset / "pack.json").read_text(encoding="utf-8"))


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
    if task_id == "t_cfg_nested":
        config = json.loads(_doc(dataset, "platform_config.json"))
        return str(config["services"]["billing-core"]["limits"]["checkout"]["max_batch_items"])
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


def test_no_identical_consecutive_lines_outside_stack_frames(dataset: Path) -> None:
    lines = _doc(dataset, "service_events.txt").split("\n")
    for previous, current in zip(lines, lines[1:], strict=False):
        if previous == current and previous:
            assert not _TIMESTAMPED.match(current), current


def test_stack_trace_block_repeats_at_least_three_times(dataset: Path) -> None:
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
