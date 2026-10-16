"""The compact measurement script runs on the session dataset and reports each file."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SELF_TEST = REPO_ROOT / "desktop" / "mcp" / "self-test"
SESSION_DOCUMENTS = (
    "operations_handbook.md",
    "orders_export.json",
    "platform_config.json",
    "service_events.txt",
)


def _load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def session_documents(tmp_path_factory) -> Path:
    generator = _load("make_dataset_for_measure", SELF_TEST / "make_dataset.py")
    out = tmp_path_factory.mktemp("session") / "dataset"
    assert generator.main(["--out", str(out), "--profile", "session"]) == 0
    return out / "documents"


@pytest.fixture(scope="module")
def measure_compact() -> ModuleType:
    return _load("measure_compact", SELF_TEST / "measure_compact.py")


def _rows_by_name(output: str) -> dict[str, list[str]]:
    lines = output.strip().splitlines()
    assert lines[0].startswith("tokens: ")
    header = lines[1].split()
    assert header[:3] == ["document", "raw_bytes", "compact_bytes"]
    return {line.split()[0]: line.split() for line in lines[2:]}


def test_measure_compact_reports_every_session_document(
    measure_compact, session_documents: Path, capsys
) -> None:
    assert measure_compact.main(["--documents", str(session_documents)]) == 0

    rows = _rows_by_name(capsys.readouterr().out)
    assert sorted(rows) == sorted(SESSION_DOCUMENTS)
    for name in SESSION_DOCUMENTS:
        assert "error:" not in " ".join(rows[name]), name
        raw_bytes, compact_bytes = int(rows[name][1]), int(rows[name][2])
        assert 0 < compact_bytes <= raw_bytes, name


def test_session_json_is_compacted_with_a_stated_ratio(
    measure_compact, session_documents: Path, capsys
) -> None:
    assert measure_compact.main(["--documents", str(session_documents)]) == 0

    rows = _rows_by_name(capsys.readouterr().out)
    orders = rows["orders_export.json"]
    # Columns: document, raw_bytes, compact_bytes, raw_tok, compact_tok, view_tok, ratio, ...
    assert orders[7].startswith("json")
    assert 0 < float(orders[6]) < 1
    assert int(orders[2]) < int(orders[1])


def test_measure_compact_rejects_a_missing_folder(measure_compact, tmp_path: Path, capsys) -> None:
    assert measure_compact.main(["--documents", str(tmp_path / "absent")]) == 2
    assert "not a folder" in capsys.readouterr().err
