"""Transcript parsing shared by compare.py and cache_rewrite.py.

One "request" per unique model response
(Codex token_usage_record ids or changed token_count totals; Claude message ids,
synthetic responses excluded), "result" per tool output, "reset" at compaction.
"""

from __future__ import annotations

import json


def read_rows(path, data=None):
    rows = []
    if data is None:
        data = path.read_bytes()
    for line in data.decode("utf-8", errors="replace").splitlines():
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            pass  # A currently open transcript can end in an incomplete line.
    return rows


def text_of(value):
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(x.get("text", "") for x in value if isinstance(x, dict))
    return ""


def codex_session(rows):
    calls, events = {}, []
    has_usage = any(r.get("type") == "token_usage_record" for r in rows)
    seen_usage, previous_total = set(), None
    raw_outputs = 0
    for row in rows:
        pl = row.get("payload") or {}
        typ = pl.get("type")
        if row.get("type") == "compacted":
            events.append(("reset", None))
        if row.get("type") == "token_usage_record":
            identity = pl.get("response_id") or (row.get("timestamp"), row.get("ordinal"))
            if identity not in seen_usage:
                seen_usage.add(identity)
                events.append(("request", None))
        elif not has_usage and row.get("type") == "event_msg" and typ == "token_count":
            info = pl.get("info") or {}
            total = json.dumps(info.get("total_token_usage"), sort_keys=True)
            if info and total != previous_total:
                events.append(("request", None))
                previous_total = total
        if typ in ("custom_tool_call", "function_call"):
            calls[pl.get("call_id")] = pl
        elif typ in ("custom_tool_call_output", "function_call_output"):
            raw_outputs += 1
            events.append(
                ("result", (calls.get(pl.get("call_id"), {}), text_of(pl.get("output")), "exec"))
            )
    return events, raw_outputs, "response_id" if has_usage else "changed token_count total"


def claude_session(rows):
    calls, events, seen = {}, [], set()
    raw_requests = 0
    for row in rows:
        msg = row.get("message") or {}
        content = msg.get("content") or []
        if row.get("type") == "system" and row.get("subtype") == "compact_boundary":
            events.append(("reset", None))
        if row.get("type") == "assistant":
            raw_requests += 1
            identity = msg.get("id") or row.get("uuid")
            if identity not in seen and msg.get("model") != "<synthetic>":
                seen.add(identity)
                events.append(("request", None))
            for block in content if isinstance(content, list) else []:
                if block.get("type") == "tool_use":
                    calls[block.get("id")] = block.get("name", "?")
        elif row.get("type") == "user" and isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    events.append(
                        (
                            "result",
                            (
                                {},
                                text_of(block.get("content")),
                                calls.get(block.get("tool_use_id"), "?"),
                            ),
                        )
                    )
    return events, raw_requests, "unique assistant message.id"
