"""JSON compaction must preserve tool protocol and every JSON lexeme."""

import copy
import json

import pytest

from horizon.proxy.output_shaper import OutputShaperSettings, shape_request
from horizon.proxy.tool_json_compaction import compact_json_lexemes, compact_tool_results


def _pretty():
    return json.dumps(
        {"rows": [{"id": n, "text": "keep  spaces\n\t雪", "negative": -1} for n in range(25)]},
        indent=4,
        ensure_ascii=False,
    )


def test_compaction_preserves_order_duplicates_escapes_and_number_lexemes():
    raw = (
        '{  "duplicate": 1e400, "duplicate": -0.00, "escaped": "a  b\\n\\"c"'
        + ', "rows": [ 1, 2 ]' * 30
        + " }"
    )
    result = compact_json_lexemes(raw)
    assert "1e400" in result and "-0.00" in result
    assert result.count('"rows"') == 30
    assert '"a  b\\n\\"c"' in result
    assert json.loads(result, object_pairs_hook=list) == json.loads(raw, object_pairs_hook=list)


@pytest.mark.parametrize(
    "raw",
    [
        "{ invalid }" * 100,
        '{"a": NaN}' + " " * 600,
        "plain text " * 80,
        _pretty() * 2,
        '"scalar"' + " " * 600,
    ],
)
def test_non_json_and_invalid_payloads_are_untouched(raw):
    assert compact_json_lexemes(raw) == raw


def test_only_tool_results_are_copied_and_error_results_are_preserved():
    text = _pretty()
    body = {
        "messages": [
            {
                "role": "assistant",
                "content": [{"type": "tool_use", "id": "id", "input": {"amount": 1250}}],
            },
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": text},
                    {"type": "tool_result", "tool_use_id": "id", "content": text},
                    {
                        "type": "tool_result",
                        "tool_use_id": "error",
                        "is_error": True,
                        "content": text,
                    },
                ],
            },
        ],
        "tools": [{"name": "refund"}],
    }
    before = copy.deepcopy(body)
    old_message = body["messages"][1]
    assert compact_tool_results(body, len) == 1
    assert old_message == before["messages"][1]
    assert body["messages"][0] == before["messages"][0]
    blocks = body["messages"][1]["content"]
    assert blocks[0] == before["messages"][1]["content"][0]
    assert blocks[1]["tool_use_id"] == "id"
    assert json.loads(blocks[1]["content"]) == json.loads(text)
    assert blocks[2] == before["messages"][1]["content"][2]
    assert body["tools"] == before["tools"]
    assert compact_tool_results(body, len) == 0


def test_counter_regression_or_failure_keeps_original():
    body = {"messages": [{"role": "tool", "tool_call_id": "x", "content": _pretty()}]}
    before = copy.deepcopy(body)
    assert compact_tool_results(body, lambda s: 1) == 0

    def fail(s):
        raise ValueError("no tokenizer")

    assert compact_tool_results(body, fail) == 0
    assert body == before


def test_responses_call_output_support_preserves_call_envelope():
    text = _pretty()
    body = {
        "input": [
            {"type": "function_call_output", "call_id": "x", "output": text},
            {"type": "function_call", "arguments": text},
        ]
    }
    assert compact_tool_results(body, len) == 1
    assert body["input"][0]["call_id"] == "x"
    assert body["input"][1]["arguments"] == text


def test_compaction_is_opt_in_and_is_not_an_output_savings_label(monkeypatch):
    body = {"messages": [{"role": "tool", "content": _pretty()}]}
    before = copy.deepcopy(body)
    assert not shape_request(body, OutputShaperSettings(enabled=True, verbosity_level=0)).changed
    assert body == before
    monkeypatch.setattr(
        "horizon.proxy.output_shaper.apply_tool_json_compaction",
        lambda body: compact_tool_results(body, len),
    )
    result = shape_request(
        body,
        OutputShaperSettings(enabled=True, verbosity_level=0, tool_json_compaction_enabled=True),
    )
    assert result.labels == ["input_compaction:tool_json:1"]
