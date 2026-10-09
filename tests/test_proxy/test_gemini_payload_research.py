"""No network/model execution: fixture grading is not client qualification."""

import ast
import importlib.util
import json
from pathlib import Path

import pytest


@pytest.fixture
def research(tmp_path):
    module_path = (
        Path(__file__).resolve().parents[2] / "experiments/frontier-savings/gemini/fixtures.py"
    )
    spec = importlib.util.spec_from_file_location("gemini_fixture_test", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.RUNS = tmp_path / "runs"
    module.RESULTS = tmp_path / "results.json"
    module.RUNS.mkdir()
    return module


@pytest.mark.parametrize("count", [4, 12])
def test_independent_ast_matches_compiler(research, count):
    cand = research.candidate(count)
    actual = research.compile_candidate(cand).after
    assert ast.dump(ast.parse(actual), include_attributes=False) == research.independent_expected(
        cand
    )


def grade_call(research, name, args):
    work = research.RUNS / "test"
    work.mkdir()
    (work / "response.json").write_text(
        json.dumps(
            {
                "choices": [
                    {
                        "message": {
                            "tool_calls": [
                                {"function": {"name": name, "arguments": json.dumps(args)}}
                            ]
                        }
                    }
                ]
            }
        )
    )
    research.RESULTS.write_text(
        json.dumps({"rows": [{"id": "test", "phase": "fixture", "status": 200, "count": 4}]})
    )
    research.grade()
    return json.loads(research.RESULTS.read_text())["rows"][0], work


def test_bound_compact_payload_passes(research):
    cand = research.candidate(4)
    row, _ = grade_call(
        research,
        "horizon_compact_edit_v1",
        {
            "receipt": research.receipt(cand),
            "table": cand.table,
            "template": cand.template,
            "keys": list(cand.keys),
        },
    )
    assert row["passed"]


def test_changed_receipt_rejected(research):
    cand = research.candidate(4)
    row, _ = grade_call(
        research,
        "horizon_compact_edit_v1",
        {
            "receipt": "wrong",
            "table": cand.table,
            "template": cand.template,
            "keys": list(cand.keys),
        },
    )
    assert not row["passed"]


def test_native_exact_edit_passes(research):
    cand = research.candidate(4)
    row, _ = grade_call(
        research,
        "Edit",
        {
            "file_path": "catalog.py",
            "old_string": cand.snapshot.source,
            "new_string": research.compile_candidate(cand).after,
        },
    )
    assert row["passed"]


def test_foreign_native_path_rejected(research):
    cand = research.candidate(4)
    row, _ = grade_call(
        research,
        "Edit",
        {
            "file_path": "outside.py",
            "old_string": cand.snapshot.source,
            "new_string": research.compile_candidate(cand).after,
        },
    )
    assert not row["passed"]


def test_script_requires_review_and_is_never_executed(research):
    row, work = grade_call(
        research, "Bash", {"command": "python -c 'raise RuntimeError(\"must not execute\")'"}
    )
    assert not row["passed"] and row["script_pending_review"]
    assert (work / "script.py").exists() and not (work / "catalog.py").exists()
