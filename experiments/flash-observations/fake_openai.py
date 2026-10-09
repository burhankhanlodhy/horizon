"""A strict fake of the OpenAI Responses API for dry-running live_test_openai.py.

What it enforces and simulates:

* **Automatic prefix caching**, as OpenAI documents it: prompts of 1,024
  tokens or more are cached in 128-token steps, and the longest cached prefix
  is reported as ``usage.input_tokens_details.cached_tokens``. There is no
  write premium. Prefixes are keyed by the hash of everything before them
  (instructions, tools, then input items in order), so an edited item misses
  from that point on, exactly the cost Flash Observations must avoid.
* **Encrypted reasoning** must come back unmodified: each ``reasoning`` item
  carries ``encrypted_content`` signed over its own id; a changed or forged
  one is a 400. (Whether OpenAI also binds it to the surrounding context is
  what the live preflight checks; this fake does not assume it.)
* Every ``*_call_output`` must answer an earlier call with the same
  ``call_id``.

A scripted "model" runs one suite per turn and, like a real model, writes down
the failures it can see in the newest tool output. Its final answer is built
from its own earlier notes, so an output it never saw in full shows up as a
lower score.

Run: python fake_openai.py <port>
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import sys
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

SECRET = b"fake-openai-reasoning"
SUITE_RE = re.compile(r"Run these suites in order: (.*?)\.")
FAIL_RE = re.compile(r"::(test_\w+) FAILED - (AssertionError: expected \d+ got \d+)")
MIN_CACHE, CACHE_STEP = 1024, 128
OUTPUT_TYPES = {"function_call_output", "custom_tool_call_output", "local_shell_call_output"}

app = FastAPI()
CACHE: set[str] = set()
CALLS = {"n": 0}


def _sig(item_id: str) -> str:
    return "enc:" + hmac.new(SECRET, item_id.encode(), hashlib.sha256).hexdigest()[:32]


def _tokens(value: Any) -> int:
    return max(1, len(json.dumps(value, sort_keys=True)) // 4)


def _error(message: str) -> JSONResponse:
    return JSONResponse(
        status_code=400,
        content={"error": {"message": message, "type": "invalid_request_error", "code": None}},
    )


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(_text(v.get("text") if isinstance(v, dict) else v) for v in value)
    return ""


def _validate(items: list[dict[str, Any]]) -> str | None:
    calls: set[str] = set()
    for i, item in enumerate(items):
        kind = item.get("type")
        if kind == "reasoning":
            if item.get("encrypted_content") != _sig(str(item.get("id"))):
                return f"input[{i}]: encrypted content for reasoning item could not be verified"
        elif kind in ("function_call", "custom_tool_call", "local_shell_call"):
            calls.add(str(item.get("call_id")))
        elif kind in OUTPUT_TYPES:
            if str(item.get("call_id") or item.get("id")) not in calls:
                return (
                    f"input[{i}]: no tool call found for output with call_id {item.get('call_id')}"
                )
    return None


def _cache(body: dict[str, Any], items: list[dict[str, Any]]) -> tuple[int, int]:
    """(total input tokens, cached tokens) for this request; caches its prefixes."""
    parts = [[body.get("instructions"), body.get("tools")], *items]
    sizes = [_tokens(p) for p in parts]
    running = hashlib.sha256()
    keys = []
    for part in parts:
        running.update(json.dumps(part, sort_keys=True).encode())
        keys.append(running.copy().hexdigest())
    total = sum(sizes)
    cached = 0
    for k in range(len(parts)):
        if keys[k] in CACHE:
            cached = sum(sizes[: k + 1])
    cached = (cached // CACHE_STEP) * CACHE_STEP if cached >= MIN_CACHE else 0
    if total >= MIN_CACHE:
        CACHE.update(keys)
    return total, min(cached, total)


@app.post("/v1/responses")
async def responses(request: Request) -> JSONResponse:
    CALLS["n"] += 1
    n = CALLS["n"]
    body = await request.json()
    items = body.get("input")
    if isinstance(items, str):
        items = [{"type": "message", "role": "user", "content": items}]
    if not isinstance(items, list):
        return _error("input must be a string or a list")
    if body.get("previous_response_id"):
        return _error("this fake is stateless: send the full input with store=false")
    problem = _validate(items)
    if problem:
        return _error(problem)
    total, cached = _cache(body, items)

    first_user = next((i for i in items if i.get("role") == "user"), {})
    task = SUITE_RE.search(_text(first_user.get("content")))
    reasoning = {
        "type": "reasoning",
        "id": f"rs_{n:05d}",
        "summary": [],
        "encrypted_content": _sig(f"rs_{n:05d}"),
    }
    output: list[dict[str, Any]] = [reasoning]
    forced = body.get("tool_choice")
    if task is None and isinstance(forced, dict) and forced.get("name"):
        # A forced call (the preflight's lookup exchange): keys a, b, c, ...
        made = sum(1 for i in items if i.get("type") == "function_call")
        output.append(
            {
                "type": "function_call",
                "id": f"fc_{n:05d}",
                "call_id": f"call_{n:05d}",
                "name": forced["name"],
                "arguments": json.dumps({"key": "abcdefgh"[made % 8]}),
                "status": "completed",
            }
        )
    elif task is None:
        seen = " ".join(_text(i.get("content") or i.get("output")) for i in items)
        output.append(_message(n, "I can see: " + seen[-400:]))
    else:
        suites = task.group(1).split(", ")
        done = sum(1 for i in items if i.get("type") == "function_call")
        newest = next((i for i in reversed(items) if i.get("type") in OUTPUT_TYPES), None)
        found = FAIL_RE.findall(_text(newest.get("output"))) if newest and done else []
        note = "Failures: " + ("; ".join(f"{t} | {e}" for t, e in found) if found else "none")
        if done < len(suites):
            if done:
                output.append(_message(n, note))
            output.append(
                {
                    "type": "function_call",
                    "id": f"fc_{n:05d}",
                    "call_id": f"call_{n:05d}",
                    "name": "run_tests",
                    "arguments": json.dumps({"suite": suites[done]}),
                    "status": "completed",
                }
            )
        else:
            notes = [
                _text(c.get("text"))
                for i in items
                if i.get("role") == "assistant"
                for c in (i.get("content") or [])
                if isinstance(c, dict)
            ] + [note]
            answer = []
            for line in notes:
                for entry in line.removeprefix("Failures: ").split("; "):
                    if " | " in entry:
                        test, err = entry.split(" | ", 1)
                        answer.append({"suite": test.split("_")[1], "test": test, "error": err})
            output.append(_message(n, json.dumps(answer)))
    out_tokens = 40 + sum(_tokens(o) for o in output if o["type"] != "reasoning")
    return JSONResponse(
        {
            "id": f"resp_{n:05d}",
            "object": "response",
            "status": "completed",
            "model": body.get("model"),
            "output": output,
            "usage": {
                "input_tokens": total,
                "input_tokens_details": {"cached_tokens": cached},
                "output_tokens": out_tokens,
                "output_tokens_details": {"reasoning_tokens": 0},
                "total_tokens": total + out_tokens,
            },
        }
    )


def _message(n: int, text: str) -> dict[str, Any]:
    return {
        "type": "message",
        "id": f"msg_{n:05d}",
        "role": "assistant",
        "status": "completed",
        "content": [{"type": "output_text", "text": text, "annotations": []}],
    }


@app.post("/v1/chat/completions")
async def chat_completions(request: Request) -> JSONResponse:
    """Chat Completions with the same cache, validation and scripted model.

    Assistant messages may carry ``reasoning_content`` (DeepSeek style); like
    encrypted reasoning it must come back exactly as it was produced.
    """
    CALLS["n"] += 1
    n = CALLS["n"]
    body = await request.json()
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        return _error("messages must be a non-empty list")
    calls: set[str] = set()
    for i, msg in enumerate(messages):
        if msg.get("role") == "assistant":
            reasoning = msg.get("reasoning_content")
            if reasoning is not None and not str(reasoning).startswith(
                "rc:" + _sig(str(msg.get("tool_calls")))[4:16]
            ):
                return _error(f"messages[{i}].reasoning_content does not match what was produced")
            calls.update(str(c.get("id")) for c in msg.get("tool_calls") or [])
        elif msg.get("role") == "tool" and str(msg.get("tool_call_id")) not in calls:
            return _error(f"messages[{i}]: tool message without a matching tool call")
    total, cached = _cache({"instructions": None, "tools": body.get("tools")}, messages)

    first_user = next((m for m in messages if m.get("role") == "user"), {})
    task = SUITE_RE.search(_text(first_user.get("content")))
    forced = body.get("tool_choice")
    content: str | None = None
    tool_calls: list[dict[str, Any]] = []
    if task is None and (isinstance(forced, dict) or forced == "required"):
        made = sum(len(m.get("tool_calls") or []) for m in messages if m.get("role") == "assistant")
        name = (
            (forced.get("function") or {}).get("name", "lookup")
            if isinstance(forced, dict)
            else "lookup"
        )
        tool_calls.append(
            {
                "id": f"call_{n:05d}",
                "type": "function",
                "function": {"name": name, "arguments": json.dumps({"key": "abcdefgh"[made % 8]})},
            }
        )
    elif task is None:
        content = "I can see: " + " ".join(_text(m.get("content")) for m in messages)[-400:]
    else:
        suites = task.group(1).split(", ")
        done = sum(len(m.get("tool_calls") or []) for m in messages if m.get("role") == "assistant")
        newest = next((m for m in reversed(messages) if m.get("role") == "tool"), None)
        found = FAIL_RE.findall(_text(newest.get("content"))) if newest and done else []
        note = "Failures: " + ("; ".join(f"{t} | {e}" for t, e in found) if found else "none")
        if done < len(suites):
            content = note if done else None
            tool_calls.append(
                {
                    "id": f"call_{n:05d}",
                    "type": "function",
                    "function": {
                        "name": "run_tests",
                        "arguments": json.dumps({"suite": suites[done]}),
                    },
                }
            )
        else:
            notes = [_text(m.get("content")) for m in messages if m.get("role") == "assistant"] + [
                note
            ]
            answer = []
            for line in notes:
                for entry in line.removeprefix("Failures: ").split("; "):
                    if " | " in entry:
                        test, err = entry.split(" | ", 1)
                        answer.append({"suite": test.split("_")[1], "test": test, "error": err})
            content = json.dumps(answer)
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = tool_calls
    message["reasoning_content"] = "rc:" + _sig(str(tool_calls or None))[4:16] + " thinking"
    out_tokens = 40 + _tokens(message)
    return JSONResponse(
        {
            "id": f"chatcmpl_{n:05d}",
            "object": "chat.completion",
            "model": body.get("model"),
            "choices": [
                {
                    "index": 0,
                    "message": message,
                    "finish_reason": "tool_calls" if tool_calls else "stop",
                }
            ],
            "usage": {
                "prompt_tokens": total,
                "completion_tokens": out_tokens,
                "total_tokens": total + out_tokens,
                "prompt_tokens_details": {"cached_tokens": cached},
            },
        }
    )


@app.get("/healthz")
async def healthz() -> dict[str, bool]:
    return {"ok": True}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=int(sys.argv[1]), log_level="warning")
