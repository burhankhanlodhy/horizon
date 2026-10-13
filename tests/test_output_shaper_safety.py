"""Optional shaping must preserve forwarding after failed or bounded work."""

import copy
import json

import pytest

from horizon.proxy import output_shaper as shaper
from horizon.proxy.validation_feedback import MARKER


def _request(dialect, output="Ran 0 tests in 0.00s\nOK"):
    command = {"command": "python -m unittest discover"}
    if dialect == "anthropic":
        return shaper.shape_request, {
            "system": [
                {"type": "text", "text": "Original system", "cache_control": {"type": "ephemeral"}}
            ],
            "messages": [
                {
                    "role": "assistant",
                    "content": [{"type": "tool_use", "id": "x", "name": "Bash", "input": command}],
                },
                {
                    "role": "user",
                    "content": [{"type": "tool_result", "tool_use_id": "x", "content": output}],
                },
            ],
        }
    if dialect == "chat":
        return shaper.shape_openai_chat_request, {
            "messages": [
                {"role": "system", "content": [{"type": "text", "text": "Original system"}]},
                {
                    "role": "assistant",
                    "tool_calls": [
                        {"id": "x", "function": {"name": "shell", "arguments": json.dumps(command)}}
                    ],
                },
                {"role": "tool", "tool_call_id": "x", "content": output},
            ]
        }
    return shaper.shape_openai_responses_request, {
        "model": "gpt-5",
        "instructions": "Original system",
        "input": [
            {
                "type": "function_call",
                "call_id": "x",
                "name": "shell",
                "arguments": json.dumps(command),
            },
            {"type": "function_call_output", "call_id": "x", "output": output},
        ],
    }


@pytest.mark.parametrize("dialect", ["anthropic", "chat", "responses"])
@pytest.mark.parametrize("failure_stage", ["_compact_tool_json", "_validation_feedback"])
def test_transform_failure_rolls_back_nested_changes_and_labels(
    monkeypatch, caplog, dialect, failure_stage
):
    shape, body = _request(dialect)
    before = copy.deepcopy(body)
    original_stage = getattr(shaper, failure_stage)
    originals = dict(body)

    def fail(staged, *args):
        original_stage(staged, *args)
        # Simulate a transform failing after modifying nested request data.
        sequence = staged.get("messages", staged.get("input"))
        sequence[0]["corrupted"] = True
        raise RuntimeError("private-request-secret")

    monkeypatch.setenv("HORIZON_VALIDATION_FEEDBACK", "1")
    monkeypatch.setattr(shaper, failure_stage, fail)
    result = shape(
        body, shaper.OutputShaperSettings(enabled=True, tool_json_compaction_enabled=True)
    )
    assert body == before
    assert all(body[key] is original for key, original in originals.items())
    assert result.changed is False and result.labels == []
    assert "private-request-secret" not in caplog.text
    assert "RuntimeError" in caplog.text


@pytest.mark.parametrize("dialect", ["anthropic", "chat", "responses"])
def test_deep_tool_result_string_cannot_break_shaping(dialect):
    deep = "[" * 1100 + "0" + "]" * 1100
    shape, body = _request(dialect, deep)
    result = shape(body, shaper.OutputShaperSettings(enabled=True))
    assert result.changed
    assert deep in json.dumps(body)
    assert not any(label.startswith("validation_feedback:") for label in result.labels)


@pytest.mark.parametrize("dialect", ["anthropic", "chat", "responses"])
def test_successful_feedback_and_shaping_are_idempotent(dialect):
    shape, body = _request(dialect)
    body["tools"] = [{"name": "shell", "input_schema": {"type": "object"}}]
    tools = body["tools"]
    settings = shaper.OutputShaperSettings(enabled=True)
    assert shape(body, settings).changed
    assert MARKER in json.dumps(body)
    once = copy.deepcopy(body)
    assert not shape(body, settings).changed
    assert body == once
    assert body["tools"] is tools


@pytest.mark.parametrize("dialect", ["anthropic", "chat", "responses"])
def test_staging_depth_budget_preserves_original_request(dialect):
    shape, body = _request(dialect)
    metadata = {}
    cursor = metadata
    for _ in range(100):
        cursor["next"] = {}
        cursor = cursor["next"]
    body["metadata"] = metadata
    before = copy.deepcopy(body)
    result = shape(body, shaper.OutputShaperSettings(enabled=True))
    assert not result.changed and result.labels == []
    assert body == before and body["metadata"] is metadata


def test_staging_node_budget_preserves_original_request(monkeypatch):
    shape, body = _request("anthropic")
    before = copy.deepcopy(body)
    monkeypatch.setattr(shaper, "_MAX_STAGING_NODES", 5)
    assert not shape(body, shaper.OutputShaperSettings(enabled=True)).changed
    assert body == before


def test_disabled_shaping_does_not_stage_the_request(monkeypatch):
    def fail(body):
        raise AssertionError("disabled shaping must be free")

    monkeypatch.setattr(shaper, "_stage_body", fail)
    result = shaper.shape_request({}, shaper.OutputShaperSettings(enabled=False))
    assert not result.changed and result.labels == []


@pytest.mark.parametrize("kind", ["deep", "oversized"])
def test_responses_pre_shape_classification_is_bounded(kind):
    if kind == "deep":
        output = '{"output":' + "[" * 1100 + "0" + "]" * 1100 + "}"
    else:
        output = json.dumps({"output": "x" * 200_001})
    _, body = _request("responses", output)
    assert shaper.classify_responses_turn(body["input"]) == shaper.TurnKind.MECHANICAL_CONTINUATION


def test_responses_http_ws_shared_shaping_helper_rolls_back_failure(monkeypatch):
    from horizon.proxy.handlers.openai import _shape_openai_responses_payload

    monkeypatch.setenv("HORIZON_OUTPUT_SHAPER", "1")
    monkeypatch.setenv("HORIZON_OUTPUT_HOLDOUT", "0")
    _, body = _request("responses")
    before = copy.deepcopy(body)
    real_feedback = shaper._validation_feedback

    def fail(staged, result):
        real_feedback(staged, result)
        raise RuntimeError("failure after feedback")

    monkeypatch.setattr(shaper, "_validation_feedback", fail)
    labels, changed = _shape_openai_responses_payload(body, model="gpt-5", request_id="rollback")
    assert not changed
    assert body == before
    assert not any(
        label.startswith(("validation_feedback:", "output_shaper:verbosity:")) for label in labels
    )
