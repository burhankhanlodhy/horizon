import copy
import json

import pytest

from horizon.proxy.validation_feedback import (
    MARKER,
    MAX_INSPECTION_CHARS,
    MAX_INSPECTION_NODES,
    annotate_validation_results,
    command_text,
    feedback,
)


@pytest.mark.parametrize("dialect", ["anthropic", "chat", "responses"])
def test_empty_suite_feedback_preserves_tool_protocol_and_is_idempotent(dialect):
    command = "python -m unittest discover -v"
    output = "Ran 0 tests in 0.000s\n\nOK\n"
    if dialect == "anthropic":
        body = {
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "x",
                            "name": "Bash",
                            "input": {"command": command},
                        }
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "x",
                            "content": output,
                            "is_error": False,
                        }
                    ],
                },
            ]
        }
    elif dialect == "chat":
        body = {
            "messages": [
                {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "id": "x",
                            "function": {
                                "name": "shell",
                                "arguments": json.dumps({"command": command}),
                            },
                        }
                    ],
                },
                {"role": "tool", "tool_call_id": "x", "content": output},
            ]
        }
    else:
        body = {
            "input": [
                {
                    "type": "custom_tool_call",
                    "call_id": "x",
                    "name": "exec",
                    "input": 'text(await tools.exec_command({cmd: "python -m unittest discover -v"}));',
                },
                {
                    "type": "custom_tool_call_output",
                    "call_id": "x",
                    "output": json.dumps({"output": output, "exit_code": 0}),
                },
            ]
        }
    before = copy.deepcopy(body)
    assert annotate_validation_results(body) == 1
    assert MARKER in json.dumps(body)
    assert annotate_validation_results(body) == 0
    sequence = "input" if dialect == "responses" else "messages"
    assert body[sequence][0] == before[sequence][0]
    if dialect == "responses":
        result = json.loads(body["input"][-1]["output"])
        assert result["exit_code"] == 0 and result["output"].startswith(output)


def test_no_feedback_on_success_read_source_user_prompt_or_old_history():
    assert feedback("python -m unittest discover", "Ran 8 tests in 0.01s\nOK") is None
    assert feedback("cat tests.py", "Ran 0 tests in 0.000s\nOK") is None
    body = {"messages": [{"role": "user", "content": "Ran 0 tests in 0.000s\nOK"}]}
    assert annotate_validation_results(body) == 0
    body = {
        "messages": [
            {
                "role": "assistant",
                "tool_calls": [
                    {"id": "x", "function": {"arguments": '{"command":"python -m pytest"}'}}
                ],
            },
            {"role": "tool", "tool_call_id": "x", "content": "no tests ran in 0.01s"},
            {"role": "user", "content": "Explain the failure"},
        ]
    }
    before = copy.deepcopy(body)
    assert annotate_validation_results(body) == 0
    assert body == before


def test_failed_suite_survives_compound_command_success_signal():
    assert "failures" in feedback(
        "python -m unittest; git status", "FAILED (failures=1)\n\nclean working tree"
    )


@pytest.mark.parametrize("call_type", ["function_call", "custom_tool_call"])
def test_responses_feedback_uses_input_text_and_preserves_multimodal_blocks(call_type):
    call = {"type": call_type, "call_id": "x", "name": "shell"}
    if call_type == "function_call":
        call["arguments"] = json.dumps({"command": "python -m unittest discover"})
    else:
        call["input"] = 'tools.exec_command({cmd: "python -m unittest discover"})'
    blocks = [
        {"type": "input_text", "text": "Ran 0 tests in 0.00s\nOK"},
        {"type": "input_image", "image_url": "https://example.com/result.png"},
        {"type": "input_file", "file_id": "file_123"},
    ]
    body = {"input": [call, {"type": call_type + "_output", "call_id": "x", "output": blocks}]}
    before = copy.deepcopy(body)
    assert annotate_validation_results(body) == 1
    assert body["input"][0] == before["input"][0]
    output = body["input"][-1]["output"]
    assert output[:-1] == before["input"][-1]["output"]
    assert output[-1]["type"] == "input_text"
    assert MARKER in output[-1]["text"]
    assert annotate_validation_results(body) == 0


def test_anthropic_block_feedback_keeps_text_dialect():
    blocks = [{"type": "text", "text": "Ran 0 tests in 0.00s\nOK"}]
    body = {
        "messages": [
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "x",
                        "input": {"command": "python -m unittest discover"},
                    }
                ],
            },
            {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "x", "content": blocks}],
            },
        ]
    }
    assert annotate_validation_results(body) == 1
    output = body["messages"][-1]["content"][0]["content"]
    assert output[0] == blocks[0]
    assert output[-1]["type"] == "text"
    assert annotate_validation_results(body) == 0


@pytest.mark.parametrize(
    "output",
    [
        "[" * 1100 + "0" + "]" * 1100,
        "x" * MAX_INSPECTION_CHARS + "\nRan 0 tests in 0.01s\nOK",
        [{"text": "Ran 0 tests in 0.01s\nOK"}] + [None] * MAX_INSPECTION_NODES,
    ],
    ids=["deep-json", "oversized-text", "too-many-nodes"],
)
def test_inspection_budget_exhaustion_does_not_annotate_partial_results(output):
    assert feedback("python -m unittest discover", output) is None


def test_cyclic_and_deep_native_containers_are_safe():
    cyclic = {"stdout": "Ran 0 tests in 0.00s\nOK"}
    cyclic["output"] = cyclic
    assert feedback("python -m unittest discover", cyclic) is None
    assert command_text("[" * 1100 + "0" + "]" * 1100) == ""


def test_quoted_brackets_do_not_count_as_json_nesting():
    output = json.dumps(
        {
            "output": "brackets: " + "[" * 1100 + "\nRan 0 tests in 0.00s\nOK",
            "path": 'escaped " quote',
        }
    )
    assert MARKER in feedback("python -m unittest discover", output)
