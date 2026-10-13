"""Read plans must point to exact source and preserve request protocol."""

import copy

import pytest

from horizon.proxy.output_shaper import OutputShaperSettings, shape_request
from horizon.proxy.symbol_read_plan import apply_symbol_read_plan, build_symbol_read_plan


def test_read_plan_includes_helper_and_referenced_constant(tmp_path):
    path = tmp_path / "module.py"
    path.write_text(
        "from collections.abc import Mapping\nLIMIT = 5\n\ndef helper(x):\n    return isinstance(x, Mapping)\n\ndef target(x):\n    return helper(x) and len(x) < LIMIT\n",
        encoding="utf-8",
    )
    plan = build_symbol_read_plan(tmp_path, "module.py", "target")
    assert plan["offset"] == 2
    assert plan["limit"] == 7
    assert "from collections.abc import Mapping" in plan["text"]
    assert "Expand the read" in plan["text"]
    assert len(plan["source_sha256"]) == 64


def test_read_plan_rejects_outside_root_and_unknown_symbols(tmp_path):
    inner = tmp_path / "inside"
    inner.mkdir()
    (tmp_path / "outside.py").write_text("def target(): return 1\n")
    with pytest.raises(ValueError, match="inside"):
        build_symbol_read_plan(inner, "../outside.py", "target")
    (inner / "a.py").write_text("def other(): return 1\n")
    with pytest.raises(ValueError, match="top-level"):
        build_symbol_read_plan(inner, "a.py", "target")


def test_plan_is_idempotent_and_does_not_change_arguments_or_history():
    body = {
        "system": [{"type": "text", "text": "existing", "cache_control": {"type": "ephemeral"}}],
        "messages": [
            {"role": "assistant", "content": [{"type": "tool_use", "input": {"offset": 142}}]}
        ],
        "tools": [{"name": "Read"}],
    }
    before = copy.deepcopy(body)
    assert apply_symbol_read_plan(body, "plan")
    assert not apply_symbol_read_plan(body, "plan")
    assert body["system"][0] == before["system"][0]
    assert body["messages"] == before["messages"] and body["tools"] == before["tools"]


def test_production_shaper_accepts_explicit_plan_without_output_savings_credit():
    body = {"system": "existing", "messages": [{"role": "user", "content": "task"}]}
    result = shape_request(
        body, OutputShaperSettings(enabled=True, verbosity_level=0, symbol_read_plan="plan")
    )
    assert result.labels == ["input_planning:symbol_reads"]
    assert body["system"] == "existing\n\nplan"
