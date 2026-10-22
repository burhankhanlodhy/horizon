"""Local-graph retrieval diagnostics, recorded when consumed by Codex.

The alternative search size is client-measured, not provider-verified. Actual
tool-output tokens are recounted here; no source text enters the account ledger.
This does not establish whole-session savings, so it carries no dollar credit.
"""

from __future__ import annotations

import json
import math
import re
from uuid import UUID, uuid5

from horizon.pricing.litellm_pricing import get_model_pricing

MARKER = re.compile(r'"contextshrink_graph_estimate"\s*:\s*(\{)')
HEX = re.compile(r"[a-f0-9]{64}\Z")
TOOL = re.compile(r"(?:^|__|\.)graph_search\Z")
WRAPPER = re.compile(r"\btools\.(?:mcp__contextshrink_graph__)?graph_search\s*\(")
OVERHEAD = 1024


def _receipt(text: str) -> dict | None:
    match = MARKER.search(text)
    if not match:
        return None
    try:
        value, _ = json.JSONDecoder().raw_decode(text[match.start(1) :])
        if not isinstance(value, dict):
            return None
        if (
            value.get("v") != 1
            or value.get("basis") != "bounded-literal-search-v1"
            or value.get("tokenizer") != "o200k_base"
        ):
            return None
        if not all(
            isinstance(value.get(k), str) and HEX.fullmatch(value[k]) for k in ("id", "snapshot")
        ):
            return None
        if not re.fullmatch(r"[a-f0-9]{16}", value.get("project", "")):
            return None
        n = value.get("baseline_tokens")
        if type(n) is not int or not 0 < n <= 4000 or value.get("overhead_tokens") != OVERHEAD:
            return None
        return {
            k: value[k]
            for k in ("v", "id", "snapshot", "project", "baseline_tokens", "basis", "tokenizer")
        }
    except (ValueError, TypeError):
        return None


def _texts(value, depth=0):
    if depth > 5:
        return
    if isinstance(value, str):
        if len(value) > 65536:
            return
        yield value
        # MCP envelope text may be JSON-escaped once or twice by a code-mode wrapper.
        try:
            parsed = json.loads(value)
        except (ValueError, TypeError):
            return
        if isinstance(parsed, (dict, list)):
            yield from _texts(parsed, depth + 1)
    elif isinstance(value, list):
        for item in value[:100]:
            yield from _texts(item, depth + 1)
    elif isinstance(value, dict):
        for key in ("text", "content", "output", "result"):
            if key in value:
                yield from _texts(value[key], depth + 1)


def extract(items) -> dict[str, dict]:
    """Only paired graph tool outputs; user messages and unrelated tools cannot claim."""
    if not isinstance(items, list):
        return {}
    calls = {}
    for item in items:
        if not isinstance(item, dict) or item.get("type") not in {
            "function_call",
            "custom_tool_call",
        }:
            continue
        name = item.get("name", "")
        allowed = isinstance(name, str) and TOOL.search(name)
        if name in {"exec", "functions.exec"}:
            allowed = bool(WRAPPER.search(str(item.get("input", ""))))
        call_id = item.get("call_id") or item.get("id")
        if allowed and isinstance(call_id, str) and call_id:
            serialized_call = json.dumps(item, ensure_ascii=False, separators=(",", ":"))
            if len(serialized_call.encode()) <= 16384:
                calls[call_id] = serialized_call
    found = {}
    for item in items:
        if (
            not isinstance(item, dict)
            or item.get("type") not in {"function_call_output", "custom_tool_call_output"}
            or not calls.get(item.get("call_id"))
        ):
            continue
        raw = item.get("output")
        serialized = raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False)
        if len(serialized.encode()) + len(calls[item["call_id"]].encode()) > 60000:
            continue
        for text in _texts(raw):
            receipt = _receipt(text)
            if receipt:
                receipt["output"] = serialized
                receipt["call"] = calls[item["call_id"]]
                found[receipt["id"]] = receipt
                if len(found) >= 20:
                    return found
    return found


def events(context, outcome):
    if (
        not context.graph_estimates_supported
        or not context.graph_estimates
        or not 200 <= outcome.status_code < 300
        or outcome.from_response_cache
    ):
        return []
    import tiktoken

    encoder = tiktoken.get_encoding("o200k_base")
    pricing = get_model_pricing(outcome.model)
    input_rate = pricing.input_cost_per_1m if pricing else None
    cached_rate = pricing.cache_read_per_1m if pricing else None
    rate = (
        min(input_rate, cached_rate) if input_rate is not None and cached_rate is not None else None
    )
    if rate is not None and (not math.isfinite(rate) or rate < 0):
        rate = None
    rows = []
    for receipt in context.graph_estimates.values():
        actual = (
            len(encoder.encode(receipt["output"], disallowed_special=()))
            + len(encoder.encode(receipt["call"], disallowed_special=()))
            + OVERHEAD
        )
        avoided = receipt["baseline_tokens"] - actual
        # Retain pricing context for diagnostics, not a monetary claim: neither
        # signed retrieval difference establishes a whole-session gain or loss.
        selected_rate = rate if avoided >= 0 else input_rate
        if selected_rate is not None and (not math.isfinite(selected_rate) or selected_rate < 0):
            selected_rate = None
        rows.append(
            {
                "event_id": str(uuid5(UUID(context.user_id), "graph-estimate-v1:" + receipt["id"])),
                "kind": "graph_estimate",
                "request_id": "graph_" + receipt["id"],
                "provider": str(outcome.provider)[:100],
                "model": str(outcome.model)[:200],
                "project": "graph/" + receipt["project"],
                "agent": "codex-graph",
                "status": 200,
                "savings_usd": 0,
                "cost_usd": 0,
                "pricing_basis": "graph-estimate:retrieval-only:v1",
                "graph_tokens_avoided": avoided,
                "graph_estimate": {k: v for k, v in receipt.items() if k not in {"output", "call"}}
                | {
                    "actual_tokens": actual,
                    "overhead_tokens": OVERHEAD,
                    "rate_per_million": selected_rate,
                    "confidence": "client-measured-counterfactual",
                },
            }
        )
    return rows
