"""A strict fake of the Messages API for dry-running live_test.py without a key.

It enforces the documented rules Flash Observations depends on and returns a
400 when one is broken:

* ``clear_at`` needs the ``mid-conversation-system-clear-at-2026-08-21`` beta;
* a system message with content must follow a user turn and be last or be
  followed by an assistant turn (effort-only messages are exempt);
* no ``cache_control`` on a turn-scoped message;
* an edit to an earlier message of the same conversation (preserved thinking,
  enforced strictly here) is a 400.

It renders what the model would see (cleared turn-scoped messages render
nothing), simulates the prefix cache (automatic breakpoint on the last
non-turn-scoped message; entries keyed by rendered-prefix hash) and bills
usage from that. The scripted "model" runs one suite per turn and, like a real
model, writes down the failures it can SEE in the newest tool output; its
final answer is built from its own earlier notes. So a flash that never
rendered, or a stub the model had to rely on, shows up as a lower score.

Run: python fake_upstream.py <port>
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

FLASH_BETA = "mid-conversation-system-clear-at-2026-08-21"
SUITE_RE = re.compile(r"Run these suites in order: (.*?)\.")
FAIL_RE = re.compile(r"::(test_\w+) FAILED - (AssertionError: expected \d+ got \d+)")

app = FastAPI()
CACHE: set[str] = set()
HISTORY: dict[str, list[str]] = {}
CALLS = {"n": 0}


def _strip(node: Any) -> Any:
    if isinstance(node, dict):
        return {k: _strip(v) for k, v in node.items() if k != "cache_control"}
    if isinstance(node, list):
        return [_strip(v) for v in node]
    return node


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(_strip(value), sort_keys=True).encode()).hexdigest()


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    out = []
    for block in content or []:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            out.append(block.get("text", ""))
        elif block.get("type") == "tool_result":
            out.append(_text(block.get("content")))
        elif block.get("type") == "tool_use":
            out.append(json.dumps(block.get("input")))
    return "\n".join(out)


def _error(message: str) -> JSONResponse:
    return JSONResponse(
        status_code=400,
        content={"type": "error", "error": {"type": "invalid_request_error", "message": message}},
    )


def _validate(body: dict[str, Any], betas: set[str]) -> str | None:
    messages = body.get("messages") or []
    for i, msg in enumerate(messages):
        if msg.get("role") != "system":
            continue
        effort_only = not msg.get("content") and isinstance(msg.get("output_config"), dict)
        if "clear_at" in msg:
            if FLASH_BETA not in betas:
                return f"messages.{i}.clear_at: Extra inputs are not permitted"
            if "cache_control" in json.dumps(msg):
                return f"messages.{i}.content.0: cache_control is not permitted on a turn-scoped system message"
        if effort_only:
            continue
        prev = messages[i - 1] if i > 0 else None
        nxt = messages[i + 1] if i + 1 < len(messages) else None
        if i == 0 or not prev or prev.get("role") not in ("user", "system"):
            return f"messages.{i}: system message must follow a user turn"
        if nxt is not None and nxt.get("role") not in ("assistant", "system"):
            return f"messages.{i}: system message must be last or followed by an assistant turn"
    return None


def _rendered(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for i, msg in enumerate(messages):
        if msg.get("clear_at") == "next_user_message" and any(
            m.get("role") == "user" for m in messages[i + 1 :]
        ):
            continue  # cleared: renders nothing, costs nothing
        out.append(msg)
    return out


def _tokens(value: Any) -> int:
    return max(1, len(json.dumps(_strip(value))) // 4)


@app.post("/v1/messages")
async def messages_endpoint(request: Request) -> JSONResponse:
    CALLS["n"] += 1
    body = await request.json()
    betas = {b.strip() for b in request.headers.get("anthropic-beta", "").split(",") if b.strip()}
    problem = _validate(body, betas)
    if problem:
        return _error(problem)
    messages = body["messages"]

    # Preserved thinking, enforced strictly: earlier messages may never change.
    conv = _digest(messages[0])
    digests = [_digest(m) for m in messages]
    seen = HISTORY.get(conv, [])
    if digests[: len(seen)] != seen:
        first = next(i for i, (a, b) in enumerate(zip(digests, seen, strict=False)) if a != b)
        return _error(f"messages.{first}: earlier message edited (preserved thinking check)")
    HISTORY[conv] = digests

    # Prefix cache: system + tools, then rendered messages; breakpoint on the
    # last message that is not turn-scoped.
    rendered = _rendered(messages)
    head = [body.get("tools"), body.get("system")]
    breakpoint_at = max(
        (i for i, m in enumerate(rendered) if m.get("clear_at") != "next_user_message"), default=-1
    )
    sizes = [_tokens(head)] + [_tokens(m) for m in rendered]
    prefix_keys, running = [], hashlib.sha256()
    for part in [head, *rendered]:
        running.update(_digest(part).encode())
        prefix_keys.append(running.copy().hexdigest())
    read_upto = 0
    for k in range(breakpoint_at + 2):
        if prefix_keys[k] in CACHE:
            read_upto = k + 1
    cache_read = sum(sizes[:read_upto])
    cache_write = sum(sizes[read_upto : breakpoint_at + 2])
    uncached = sum(sizes[breakpoint_at + 2 :])
    CACHE.update(prefix_keys[: breakpoint_at + 2])

    # Scripted model.
    suites = SUITE_RE.search(_text(messages[0].get("content"))).group(1).split(", ")
    done = sum(
        1
        for m in messages
        if m.get("role") == "assistant"
        for b in m.get("content", [])
        if isinstance(b, dict) and b.get("type") == "tool_use"
    )
    visible = _text(rendered[-1].get("content")) if rendered else ""
    if rendered and rendered[-1].get("role") == "user":
        visible = _text(rendered[-1].get("content"))
    found = FAIL_RE.findall(visible)
    note = "Failures: " + "; ".join(f"{t} | {e}" for t, e in found) if found else "Failures: none"
    content: list[dict[str, Any]] = []
    if done:
        content.append({"type": "text", "text": note})
    if done < len(suites):
        content.append(
            {
                "type": "tool_use",
                "id": f"toolu_{CALLS['n']:05d}",
                "name": "run_tests",
                "input": {"suite": suites[done]},
            }
        )
        stop = "tool_use"
    else:
        notes = [
            b.get("text", "")
            for m in messages
            if m.get("role") == "assistant"
            for b in m.get("content", [])
            if isinstance(b, dict) and b.get("type") == "text"
        ] + [note]
        answer = []
        for n in notes:
            for item in n.removeprefix("Failures: ").split("; "):
                if " | " in item:
                    test, err = item.split(" | ", 1)
                    suite = test.split("_")[1]
                    answer.append({"suite": suite, "test": test, "error": err})
        content = [{"type": "text", "text": json.dumps(answer)}]
        stop = "end_turn"
    return JSONResponse(
        {
            "id": f"msg_{CALLS['n']:05d}",
            "type": "message",
            "role": "assistant",
            "model": body.get("model"),
            "content": content,
            "stop_reason": stop,
            "stop_sequence": None,
            "usage": {
                "input_tokens": uncached,
                "cache_creation_input_tokens": cache_write,
                "cache_read_input_tokens": cache_read,
                "output_tokens": 60 + 20 * len(found),
            },
        }
    )


@app.get("/healthz")
async def healthz() -> dict[str, bool]:
    return {"ok": True}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=int(sys.argv[1]), log_level="warning")
