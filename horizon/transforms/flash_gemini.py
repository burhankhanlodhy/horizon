"""Flash Observations for the native Gemini API: stub on the next turn.

Same design as :mod:`horizon.transforms.flash_openai`, for
``generateContent`` / ``streamGenerateContent`` bodies (Gemini CLI, the
google-genai SDK, Vertex AI):

* **Turn T.** The newest large tool output goes out in full, with a one-line
  notice that it is shown for this turn only.
* **Turn T+1 on.** The client re-sends it, and Horizon forwards a short stub
  (first and last lines) in its place.

The edit lands just after the newest model turn, so the provider's prefix
cache still matches everything before it.

Format: a tool output is a ``functionResponse`` part in a ``user`` content,
``{"name": ..., "response": {...}}``. Only responses holding a single string
field are stubbed (Gemini CLI sends ``{"output": "..."}``); the stub keeps that
field's name, so the structure the model sees is unchanged. Outputs are keyed
by position (content index, part index), which is stable because the client
re-sends its history unchanged; a ``functionResponse`` follows the
``functionCall`` it answers in the same order.

CONTRACT (as for the OpenAI form)
    * Deterministic: a stub is a pure function of the client's own output.
    * Only outputs the model has already answered are stubbed, and only at or
      after the conversation's horizon (the start of the newest tool-output
      tail when Horizon first saw the conversation).
    * The model's own parts, including thought signatures, are never touched.

Opt-in: ``HORIZON_FLASH_GEMINI=1`` (``HORIZON_SAVINGS=auto`` sets it). The
stub is ordinary history, so no API feature is needed and any Gemini-format
upstream works; one that rejects a flashed request is retried unflashed and
switched off (:mod:`horizon.proxy.flash_guard`). Never raises: on any error the
contents are returned unchanged.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Iterable
from typing import Any

from horizon.transforms.flash_observations import (
    FlashPolicy,
    FlashResult,
    build_stub,
    call_signature,
    ccr_key,
    first_rerun,
)
from horizon.transforms.flash_openai import NOTICE, _strip_notice

logger = logging.getLogger(__name__)

#: Shell, search and listing tools of Gemini CLI and similar agents.
#: ``read_file`` is opt-in, as ``Read`` is for Claude.
DEFAULT_GEMINI_TOOLS = (
    "run_shell_command",
    "search_file_content",
    "glob",
    "list_directory",
    "shell",
    "bash",
    "grep",
    "ls",
)


def _function_response(part: Any) -> dict[str, Any] | None:
    if isinstance(part, dict):
        value = part.get("functionResponse")
        if isinstance(value, dict):
            return value
    return None


def _text_field(response: Any) -> tuple[str, str] | None:
    """``(field, text)`` of a response holding exactly one string field."""
    if isinstance(response, dict) and len(response) == 1:
        ((field, value),) = response.items()
        if isinstance(value, str):
            return field, value
    return None


def _parts(content: Any) -> list[Any]:
    if isinstance(content, dict) and isinstance(content.get("parts"), list):
        return content["parts"]
    return []


def tail_start(contents: list[Any]) -> int:
    """Index just after the newest ``model`` content: where the newest outputs begin."""
    for index in range(len(contents) - 1, -1, -1):
        content = contents[index]
        if isinstance(content, dict) and content.get("role") == "model":
            return index + 1
    return 0


def conversation_key(contents: Iterable[Any]) -> str:
    """Hash of the first user content (its parts)."""
    for content in contents:
        if isinstance(content, dict) and content.get("role", "user") == "user":
            raw = json.dumps(content.get("parts"), sort_keys=True, ensure_ascii=False, default=str)
            return hashlib.blake2b(raw.encode("utf-8", "ignore"), digest_size=12).hexdigest()
    return ""


def _key(index: int, part_index: int) -> str:
    return f"{index}:{part_index}"


def _calls_and_outputs(
    contents: list[Any],
) -> tuple[dict[str, tuple[int, str]], dict[str, str]]:
    """Call signatures and output texts, both keyed by the output's position."""
    calls: dict[str, tuple[int, str]] = {}
    outputs: dict[str, str] = {}
    pending: list[tuple[int, str]] = []
    for index, content in enumerate(contents):
        if not isinstance(content, dict):
            continue
        if content.get("role") == "model":
            pending = []
            for part in _parts(content):
                call = part.get("functionCall") if isinstance(part, dict) else None
                if isinstance(call, dict):
                    pending.append((index, call_signature(str(call.get("name")), call.get("args"))))
            continue
        for part_index, part in enumerate(_parts(content)):
            response = _function_response(part)
            if response is None:
                continue
            key = _key(index, part_index)
            if pending:
                calls[key] = pending.pop(0)
            field = _text_field(response.get("response"))
            if field is not None:
                outputs[key] = _strip_notice(field[1])
    return calls, outputs


def apply_gemini(contents: list[Any], *, horizon: int, policy: FlashPolicy) -> FlashResult:
    """Stub answered outputs at or after ``horizon``; mark the unanswered ones."""
    try:
        return _apply(contents, horizon, policy)
    except Exception:
        logger.debug("flash (gemini) skipped", exc_info=True)
        return FlashResult(messages=list(contents))


def _apply(contents: list[Any], horizon: int, policy: FlashPolicy) -> FlashResult:
    answered_before = tail_start(contents)
    eligible: dict[str, tuple[int, int, str, str, str]] = {}
    for index, content in enumerate(contents):
        if index < horizon or not isinstance(content, dict) or content.get("role") == "model":
            continue
        for part_index, part in enumerate(_parts(content)):
            response = _function_response(part)
            if response is None or not policy.allows(str(response.get("name") or "")):
                continue
            field = _text_field(response.get("response"))
            if field is None:
                continue
            original = _strip_notice(field[1])
            if len(original) < policy.min_chars:
                continue
            eligible[_key(index, part_index)] = (
                index,
                part_index,
                str(response.get("name")),
                field[0],
                original,
            )

    result = FlashResult(messages=[])
    calls, outputs = _calls_and_outputs(contents)
    result.paused_at = first_rerun(
        calls, outputs, {key: entry[0] for key, entry in eligible.items()}
    )
    by_content: dict[int, list[tuple[int, str, str, str]]] = {}
    for index, part_index, name, field_name, original in eligible.values():
        if result.paused_at is not None and index > result.paused_at:
            continue
        by_content.setdefault(index, []).append((part_index, name, field_name, original))

    for index, content in enumerate(contents):
        targets = by_content.get(index)
        if not targets:
            result.messages.append(content)
            continue
        parts = list(_parts(content))
        for part_index, name, field_name, original in targets:
            part = parts[part_index]
            response = _function_response(part) or {}
            if index < answered_before:
                stub = build_stub(name, original)
                text = stub
                result.stubbed += 1
                result.chars_kept_out += len(original)
                result.keys.append((ccr_key(original), original))
                result.cleared.append((original, stub))
            else:
                text = original + NOTICE
                result.flashed += 1
            parts[part_index] = {
                **part,
                "functionResponse": {**response, "response": {field_name: text}},
            }
        result.messages.append({**content, "parts": parts})
    return result


def flash_gemini_enabled() -> bool:
    from horizon.proxy import runtime_env

    value = runtime_env.getenv("HORIZON_FLASH_GEMINI", "") or ""
    return value.strip().lower() in ("1", "true", "yes", "on")
