"""Run Horizon's compression pipeline on the Codex tool results the proxy would offer.

Companion to exec_gate.py: for each result offered for compression, compress it
as the newest tool result of a short conversation (how the proxy sees it) and
weight the saving by how many later requests re-send it. Prints totals only.
Usage: python exec_compress.py [sessions dir]
"""

import json
import sys
import time
from collections import Counter
from pathlib import Path

from exec_gate import ROOT, output_text, read_command
from horizon.compress import compress
from horizon.transforms.content_router import _read_output_should_be_protected


def as_conversation(text: str) -> list[dict]:
    return [
        {"role": "user", "content": "Continue the task."},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "exec", "input": {}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": text}]},
    ]


from horizon.transforms.kompress_compressor import _load_kompress

_load_kompress(allow_download=False)
weight_before = weight_after = 0.0
by_kind = Counter(); kind_n = Counter(); slow = 0; n = 0
for path in sorted(ROOT.glob("**/*.jsonl")):
    calls, outs = {}, []
    for raw in path.open(encoding="utf-8", errors="replace"):
        try:
            pl = json.loads(raw).get("payload") or {}
        except json.JSONDecodeError:
            continue
        if pl.get("type") in ("custom_tool_call", "function_call"):
            calls[pl.get("call_id", "")] = pl
        elif pl.get("type") in ("custom_tool_call_output", "function_call_output"):
            outs.append((calls.get(pl.get("call_id", ""), {}), output_text(pl.get("output"))))
    for i, (call, text) in enumerate(outs):
        later = len(outs) - i - 1
        if len(text) < 1000 or later == 0:
            continue
        rc = read_command(call)
        if rc and _read_output_should_be_protected(text):
            continue
        t0 = time.perf_counter()
        r = compress(as_conversation(text), model="gpt-6.1-sol", protect_recent=0)
        secs = time.perf_counter() - t0
        slow += secs > 5
        n += 1
        before, after = r.tokens_before, r.tokens_after
        weight_before += before * later
        weight_after += after * later
        kind = "json" if text.lstrip()[:1] in "[{" else "script status" if text.startswith(("Script completed", "Script failed")) else "text"
        by_kind[kind] += (before - after) * later
        kind_n[kind] += 1
saved = weight_before - weight_after
print(f"Offered results compressed: {n} ({slow} took over 5 s, Codex's WebSocket limit)")
print(f"Re-sent tokens: {weight_before / 1e6:.1f}M -> {weight_after / 1e6:.1f}M, saving {saved / 1e6:.1f}M ({saved / weight_before:.0%})")
for k, v in by_kind.most_common():
    print(f"  saved from {k:14} results: {v / 1e6:5.1f}M  ({kind_n[k]} results)")
