"""Factual feedback for failed/empty test runs in the latest native tool result.

Only known test commands qualify. Original result content, tool ids, arguments,
roles and status fields stay intact. This does not execute validation commands
or claim a passed run proves correctness.
"""

from __future__ import annotations

import ast
import json
import re
from typing import Any

MARKER = "[horizon-validation]"
# Inspection is optional: exceeding a budget leaves the observation intact.
MAX_INSPECTION_CHARS = 200_000
MAX_INSPECTION_DEPTH = 32
MAX_INSPECTION_NODES = 10_000


class _InspectionLimit(ValueError):
    pass


def load_inspection_json(value: str) -> Any:
    """Decode bounded JSON containers; return None for non-JSON text.

    Raises ValueError when inspection exceeds the size or nesting budget.
    Shared with Responses turn classification, which runs before shaping.
    """
    if len(value) > MAX_INSPECTION_CHARS:
        raise _InspectionLimit
    if not value.lstrip().startswith(("{", "[")):
        return None
    depth = 0
    quoted = escaped = False
    for char in value:
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char in "[{":
            depth += 1
            if depth > MAX_INSPECTION_DEPTH:
                raise _InspectionLimit
        elif char in "]}":
            depth -= 1
    try:
        return json.loads(value)
    except RecursionError as exc:
        raise _InspectionLimit from exc
    except ValueError:
        return None


_TEST_COMMAND = re.compile(
    r"(?:\s-m\s+(?:unittest|pytest)\b|(?:^|[;&|]\s*|\s)(?:pytest|jest|vitest)(?:\.exe)?\s|\b(?:cargo|go)\s+test\b|\b(?:npm|pnpm|yarn)\s+(?:run\s+)?test\b)"
)
_ZERO = re.compile(
    r"(?im)^(?:Ran\s+0\s+tests?\b|no tests (?:ran|found)\b|collected\s+0\s+items\b|Test (?:Files|Suites):\s+0\b)"
)
_FAILED = re.compile(
    r"(?im)^(?:FAILED\s*\(|FAILURES?\b|=+.*\b[1-9]\d* failed\b|Test (?:Files|Suites):.*\b[1-9]\d* failed\b)"
)
_CMD_LITERAL = re.compile(
    r"""["']?(?:cmd|command)["']?\s*:\s*("(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')"""
)


def command_text(value: Any) -> str:
    if isinstance(value, dict):
        commands = [value[k] for k in ("command", "cmd") if isinstance(value.get(k), str)]
        if sum(map(len, commands)) > MAX_INSPECTION_CHARS:
            return ""
        return "\n".join(commands)
    if not isinstance(value, str):
        return ""
    try:
        decoded = load_inspection_json(value)
    except _InspectionLimit:
        return ""
    if isinstance(decoded, dict):
        return command_text(decoded)
    if "exec_command" not in value:
        return ""
    commands = []
    for match in _CMD_LITERAL.finditer(value):
        try:
            command = ast.literal_eval(match[1])
        except (ValueError, SyntaxError, RecursionError):
            continue
        if isinstance(command, str):
            commands.append(command)
    return "\n".join(commands)


def result_text(value: Any) -> str:
    # Iterative traversal also handles cyclic/malformed SDK containers safely.
    pending = [(value, 0)]
    pieces: list[str] = []
    nodes = chars = 0
    while pending:
        part, depth = pending.pop()
        nodes += 1
        if nodes > MAX_INSPECTION_NODES or depth > MAX_INSPECTION_DEPTH:
            raise _InspectionLimit
        if isinstance(part, str):
            chars += len(part) + 1
            if chars > MAX_INSPECTION_CHARS:
                raise _InspectionLimit
            decoded = load_inspection_json(part)
            if isinstance(decoded, (dict, list)):
                pending.append((decoded, depth + 1))
            else:
                pieces.append(part)
        elif isinstance(part, list):
            if nodes + len(pending) + len(part) > MAX_INSPECTION_NODES:
                raise _InspectionLimit
            pending.extend((child, depth + 1) for child in reversed(part))
        elif isinstance(part, dict):
            pending.extend(
                (part[key], depth + 1)
                for key in reversed(("output", "stdout", "stderr", "text", "content"))
                if key in part
            )
    return "\n".join(pieces)


def feedback(command: str, output: Any) -> str | None:
    if not _TEST_COMMAND.search(command):
        return None
    try:
        text = result_text(output)
    except _InspectionLimit:
        return None
    if MARKER in text:
        return None
    if _ZERO.search(text):
        return f"{MARKER} A test-runner summary reports zero tests. This is not a passing verification; check discovery/framework selection and run the intended suite."
    if _FAILED.search(text):
        return f"{MARKER} A test-runner summary reports failures. Do not treat a later command's zero exit code as verification; repair the failing checks and rerun them."
    return None


def _annotate(carrier: dict, key: str, command: str, *, text_type: str = "text") -> bool:
    value = carrier.get(key)
    note = feedback(command, value)
    if not note:
        return False
    # Keep JSON envelopes valid by placing notes inside their output field.
    # Append a distinct protocol-specific block for native block-list carriers;
    # plain stdout remains its original prefix.
    if isinstance(value, str):
        if value.lstrip().startswith(("{", "[")):
            try:
                decoded = load_inspection_json(value)
            except _InspectionLimit:
                return False
            if isinstance(decoded, dict):
                if isinstance(decoded.get("output"), str):
                    decoded["output"] += "\n\n" + note
                    carrier[key] = json.dumps(decoded, ensure_ascii=False)
                    return True
                return False
            if isinstance(decoded, list):
                return False
        carrier[key] = value + "\n\n" + note
        return True
    if isinstance(value, list) and all(isinstance(p, dict) for p in value):
        carrier[key] = [*value, {"type": text_type, "text": note}]
        return True
    return False


def annotate_validation_results(body: dict) -> int:
    """Inspect latest tool observations, never user prompts or earlier cache history."""
    items = body.get("messages")
    responses = False
    if not isinstance(items, list):
        items = body.get("input")
        responses = True
    if not isinstance(items, list) or not items:
        return 0
    commands = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        if responses and item.get("type") in {"function_call", "custom_tool_call"}:
            commands[item.get("call_id")] = command_text(item.get("arguments", item.get("input")))
        for call in item.get("tool_calls", []) if isinstance(item.get("tool_calls"), list) else []:
            if isinstance(call, dict):
                function = call.get("function")
                if isinstance(function, dict):
                    commands[call.get("id")] = command_text(function.get("arguments"))
        for block in item.get("content", []) if isinstance(item.get("content"), list) else []:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                commands[block.get("id")] = command_text(block.get("input"))
    count = 0
    last = items[-1]
    if not isinstance(last, dict):
        return 0
    if responses:
        for item in reversed(items):
            if not isinstance(item, dict) or item.get("type") not in {
                "function_call_output",
                "custom_tool_call_output",
            }:
                break
            count += _annotate(
                item, "output", commands.get(item.get("call_id"), ""), text_type="input_text"
            )
    elif last.get("role") == "tool":
        count += _annotate(last, "content", commands.get(last.get("tool_call_id"), ""))
    elif last.get("role") == "user" and isinstance(last.get("content"), list):
        for block in last["content"]:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                count += _annotate(block, "content", commands.get(block.get("tool_use_id"), ""))
    return count
