"""What fills Claude Code's re-sent context, and how much Horizon would compress it.

Replays local Claude Code transcripts (~/.claude/projects/**/*.jsonl). Every tool
result stays in the conversation and is re-sent on each later request, so each
result is weighted by the number of later model requests (assistant turns). Big
results are run through Horizon's compression pipeline as the newest tool
result of a short conversation, the way the proxy first sees them.
Prints totals only. Usage: python context_mix.py [projects dir] [--no-compress]
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(next((a for a in sys.argv[1:] if not a.startswith("--")), Path.home() / ".claude" / "projects"))
COMPRESS = "--no-compress" not in sys.argv
FLOOR = 1000  # characters, roughly the router's 250-token minimum


def text_of(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    return ""


def sessions():
    for path in sorted(ROOT.glob("**/*.jsonl")):
        tool_names: dict[str, str] = {}
        events = []  # ("request",) | ("result", tool, text)
        for raw in path.open(encoding="utf-8", errors="replace"):
            try:
                entry = json.loads(raw)
            except json.JSONDecodeError:
                continue
            msg = entry.get("message") or {}
            content = msg.get("content")
            if entry.get("type") == "assistant":
                events.append(("request", None, ""))
                for b in content if isinstance(content, list) else []:
                    if isinstance(b, dict) and b.get("type") == "tool_use":
                        tool_names[b.get("id", "")] = b.get("name", "?")
            elif entry.get("type") == "user" and isinstance(content, list):
                for b in content:
                    if isinstance(b, dict) and b.get("type") == "tool_result":
                        events.append(("result", tool_names.get(b.get("tool_use_id", ""), "?"), text_of(b.get("content"))))
        yield path, events


def main() -> None:
    compress = None
    if COMPRESS:
        from horizon.compress import compress
        from horizon.transforms.kompress_compressor import _load_kompress

        _load_kompress(allow_download=False)

    carried, saved, count = Counter(), Counter(), Counter()
    n_sessions = n_requests = 0
    for _, events in sessions():
        total_requests = sum(e[0] == "request" for e in events)
        if not total_requests:
            continue
        n_sessions += 1
        n_requests += total_requests
        seen = 0
        for kind, tool, text in events:
            if kind == "request":
                seen += 1
                continue
            later = total_requests - seen
            tokens = len(text) / 4
            name = tool if tool and not tool.startswith("mcp__") else "MCP tools"
            carried[name] += tokens * later
            count[name] += 1
            if compress and len(text) >= FLOOR and later:
                r = compress([
                    {"role": "user", "content": "Continue."},
                    {"role": "assistant", "content": [{"type": "tool_use", "id": "t", "name": tool or "Bash", "input": {}}]},
                    {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t", "content": text}]},
                ], model="claude-sonnet-5-5", protect_recent=0)
                saved[name] += r.tokens_saved * later
    total = sum(carried.values())
    print(f"Sessions: {n_sessions}, model requests: {n_requests}, tool results: {sum(count.values())}")
    print(f"Tool results re-sent across requests: {total / 1e6:.1f}M tokens")
    print(f"  {'tool':16}{'results':>8}{'re-sent':>10}{'share':>7}" + (f"{'compressed away':>17}" if compress else ""))
    for name, v in carried.most_common(12):
        extra = f"{saved[name] / 1e6:>10.1f}M ({saved[name] / v:3.0%})" if compress and v else ""
        print(f"  {name:16}{count[name]:>8}{v / 1e6:>9.1f}M{v / total:>7.0%}   {extra}")
    if compress:
        s = sum(saved.values())
        print(f"Compression would remove {s / 1e6:.1f}M of {total / 1e6:.1f}M re-sent tool-result tokens ({s / total:.0%}).")


if __name__ == "__main__":
    main()
