"""Preflight for Flash Observations on an OpenAI Responses endpoint (a few cents).

Next-turn stubbing edits a tool output the model has already answered. That is
only safe if the endpoint (a) caches prefixes, so everything before the edit
stays cheap, and (b) accepts the edited history next to the encrypted
reasoning the model produced after that output. This checks both, statelessly
(``store: false``), the way Codex talks to the API:

  1. usage reports input_tokens_details.cached_tokens
  2. prompt caching hits: the same >2k-token prefix, read back on repeats
  3. an edited earlier output is accepted: a 3-request tool exchange where the
     third request replaces the first output with a stub, encrypted reasoning
     from after it passed back unchanged
  4. the prefix before the edit is still served from cache on that request
  5. the newest output is readable: the model reports a code word that appears
     only in it

Usage:
  OPENAI_API_KEY=... python preflight_openai.py [--base-url URL] [--model M] [--effort low]
Exit status 0 when every check passes.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import string
import sys
from typing import Any

import httpx


def _filler(seed: str, words: int) -> str:
    rng = random.Random(seed)
    return " ".join(
        "".join(rng.choices(string.ascii_lowercase, k=rng.randint(3, 9))) for _ in range(words)
    )


TOOL = {
    "type": "function",
    "name": "lookup",
    "description": "Look up one record by key and return its full text.",
    "parameters": {
        "type": "object",
        "properties": {"key": {"type": "string"}},
        "required": ["key"],
        "additionalProperties": False,
    },
    "strict": True,
}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--base-url", default="https://api.openai.com")
    ap.add_argument("--model", default="gpt-6.1-sol")
    ap.add_argument("--effort", default="low")
    args = ap.parse_args()
    key = os.environ.get("OPENAI_API_KEY", "")
    if not key:
        print("OPENAI_API_KEY is not set", file=sys.stderr)
        return 2
    url = args.base_url.rstrip("/").removesuffix("/v1") + "/v1/responses"
    nonce = "".join(random.choices(string.ascii_lowercase, k=10))
    instructions = (
        f"Preflight {nonce}. Use the lookup tool when asked. Reference text, ignore it: "
        + _filler(nonce, 2_400)
    )
    results: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str) -> None:
        results.append((name, ok, detail))
        print(f"[{'PASS' if ok else 'FAIL'}] {name}: {detail}", flush=True)

    http = httpx.Client(timeout=300, headers={"authorization": f"Bearer {key}"})

    def call(items: list[dict[str, Any]], **extra: Any) -> tuple[int, dict[str, Any]]:
        body = {
            "model": args.model,
            "instructions": instructions,
            "input": items,
            "tools": [TOOL],
            "store": False,
            "include": ["reasoning.encrypted_content"],
            "prompt_cache_key": f"preflight-{nonce}",
            "max_output_tokens": 2_000,
            **extra,
        }
        if args.effort:
            body["reasoning"] = {"effort": args.effort}
        resp = http.post(url, json=body)
        try:
            return resp.status_code, resp.json()
        except ValueError:
            return resp.status_code, {"error": resp.text[:300]}

    def cached(data: dict[str, Any]) -> int | None:
        details = (data.get("usage") or {}).get("input_tokens_details")
        return details.get("cached_tokens") if isinstance(details, dict) else None

    # 1 + 2: cache fields and hits.
    reads = []
    for i in range(4):
        status, data = call(
            [{"type": "message", "role": "user", "content": f"Reply with the word ok. ({i})"}],
            tool_choice="none",
        )
        if status != 200:
            check("basic request", False, f"HTTP {status}: {json.dumps(data)[:300]}")
            return 1
        if i == 0:
            usage = data.get("usage") or {}
            check(
                "usage has cached_tokens",
                cached(data) is not None,
                f"model={data.get('model')} input={usage.get('input_tokens')} cached={cached(data)}",
            )
        else:
            reads.append(cached(data) or 0)
    check("prompt cache hits", all(r > 1_000 for r in reads), f"cached_tokens on repeats: {reads}")

    # 3 + 4 + 5: a tool exchange, then the first output replaced by a stub.
    record_a = f"Record A ({nonce}). " + _filler(nonce + "a", 2_000) + " The code word is CEDAR."
    record_b = f"Record B ({nonce}). " + _filler(nonce + "b", 300) + " The code word is MAPLE."
    items: list[dict[str, Any]] = [
        {
            "type": "message",
            "role": "user",
            "content": "Call lookup with key a, then lookup with key b, then tell me the "
            "code word in record b. Reply with the word only.",
        }
    ]
    status, data = call(items, tool_choice={"type": "function", "name": "lookup"})
    if status != 200:
        check("tool exchange", False, f"request 1: HTTP {status}: {json.dumps(data)[:300]}")
        return 1
    items += [{k: v for k, v in o.items() if k != "status"} for o in data.get("output", [])]
    call_a = next((o for o in data.get("output", []) if o.get("type") == "function_call"), None)
    if call_a is None:
        check("tool exchange", False, "request 1 returned no lookup call")
        return 1
    items.append({"type": "function_call_output", "call_id": call_a["call_id"], "output": record_a})
    status, data = call(items, tool_choice={"type": "function", "name": "lookup"})
    if status != 200:
        check("tool exchange", False, f"request 2: HTTP {status}: {json.dumps(data)[:300]}")
        return 1
    produced = data.get("output", [])
    has_reasoning = any(
        o.get("type") == "reasoning" and o.get("encrypted_content") for o in produced
    )
    items += [{k: v for k, v in o.items() if k != "status"} for o in produced]
    call_b = next((o for o in produced if o.get("type") == "function_call"), None)
    if call_b is None:
        check("tool exchange", False, "request 2 returned no lookup call")
        return 1
    items.append({"type": "function_call_output", "call_id": call_b["call_id"], "output": record_b})
    edit_at = next(
        i
        for i, o in enumerate(items)
        if o.get("call_id") == call_a["call_id"] and o.get("type") == "function_call_output"
    )
    items[edit_at] = {
        **items[edit_at],
        "output": f"[Horizon flash: record A was shown in full once. First words: {record_a[:80]}]",
    }
    status, data = call(items, tool_choice="none")
    check(
        "edited earlier output accepted",
        status == 200,
        f"HTTP {status}; encrypted reasoning after the edit: {has_reasoning}"
        + ("" if status == 200 else f"; {json.dumps(data)[:300]}"),
    )
    if status == 200:
        hit = cached(data) or 0
        check(
            "prefix before the edit still cached",
            hit > 1_000,
            f"cached_tokens={hit} (instructions alone are ~{len(instructions) // 4} tokens)",
        )
        text = "".join(
            c.get("text", "")
            for o in data.get("output", [])
            if o.get("type") == "message"
            for c in o.get("content") or []
            if isinstance(c, dict)
        )
        check("newest output readable", "MAPLE" in text.upper(), f"reply: {text[:120]!r}")

    failed = [name for name, ok, _ in results if not ok]
    print("\nPREFLIGHT " + ("PASSED" if not failed else "FAILED: " + ", ".join(failed)))
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
