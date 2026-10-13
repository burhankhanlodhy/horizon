"""Opt-in whitespace compaction of JSON tool results, without reserializing values."""

from __future__ import annotations

import json
from collections.abc import Callable
from functools import lru_cache
from typing import Any


def _reject_constant(value: str) -> None:
    raise ValueError(value)


@lru_cache(maxsize=64)
def compact_json_lexemes(text: str) -> str:
    """Keep every non-whitespace lexeme, including duplicates and numeric spelling.

    Plain prose, scalar JSON, malformed JSON and oversized/deep payloads stay
    unchanged. String contents, order, escape sequences and duplicate keys are
    never normalized. This preserves JSON semantics, not pretty-print layout.
    """
    if not 512 <= len(text) <= 200_000 or not text.lstrip().startswith(("{", "[")):
        return text
    try:
        json.loads(text, parse_constant=_reject_constant)
    except (ValueError, RecursionError):
        return text
    quoted = escaped = False
    out = []
    for char in text:
        if quoted:
            out.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
            out.append(char)
        elif char not in " \t\r\n":
            out.append(char)
    return "".join(out)


def compact_tool_results(body: dict[str, Any], count: Callable[[str], int]) -> int:
    """Copy only changed tool-result fields, and require >=20% counted savings.

    Counter accuracy depends on the selected tokenizer. This gate is an input
    candidate filter, not a claim about provider output or entire-request savings.
    Error results and protocol IDs/arguments/schemas are left intact.
    """
    changed = 0

    def text(value):
        nonlocal changed
        if not isinstance(value, str):
            return value
        candidate = compact_json_lexemes(value)
        if candidate == value:
            return value
        try:
            before, after = count(value), count(candidate)
        except Exception:
            return value
        if before <= 0 or after > before * 0.8:
            return value
        changed += 1
        return candidate

    def content(value):
        if isinstance(value, str):
            return text(value)
        if isinstance(value, list):
            return [
                {**block, "text": text(block["text"])}
                if isinstance(block, dict) and block.get("type") == "text" and "text" in block
                else block
                for block in value
            ]
        return value

    messages = body.get("messages")
    if isinstance(messages, list):
        updated = []
        for msg in messages:
            if not isinstance(msg, dict):
                updated.append(msg)
            elif msg.get("role") == "tool" and "tool_calls" not in msg and not msg.get("is_error"):
                updated.append({**msg, "content": content(msg.get("content"))})
            elif msg.get("role") == "user" and isinstance(msg.get("content"), list):
                updated.append(
                    {
                        **msg,
                        "content": [
                            {**block, "content": content(block.get("content"))}
                            if isinstance(block, dict)
                            and block.get("type") == "tool_result"
                            and not block.get("is_error")
                            else block
                            for block in msg["content"]
                        ],
                    }
                )
            else:
                updated.append(msg)
        if changed:
            body["messages"] = updated
    inputs = body.get("input")
    if isinstance(inputs, list):
        before = changed
        updated = [
            {**item, "output": content(item.get("output"))}
            if isinstance(item, dict)
            and item.get("type") == "function_call_output"
            and not item.get("is_error")
            else item
            for item in inputs
        ]
        if changed > before:
            body["input"] = updated
    return changed


def apply_tool_json_compaction(body: dict[str, Any]) -> int:
    from horizon.tokenizers import get_tokenizer

    return compact_tool_results(body, get_tokenizer(str(body.get("model") or "")).count_text)
