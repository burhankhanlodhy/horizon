"""How the proxy's read protection treats Codex code-mode `exec` results, on local transcripts.

For every tool result in ~/.codex/sessions this applies the same gate the
proxy's Responses extraction uses (openai.py,
_compress_openai_responses_live_text_units_with_router): a `custom_tool_call`
whose script runs a read command (or a command that is not a string literal)
is protected from lossy compression unless its output is confidently non-code
data. Each result is weighted by how many later requests re-send it.
Only totals are printed. Usage: python exec_gate.py [sessions dir]
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

from horizon.transforms.content_router import (
    _custom_tool_call_commands,
    _is_read_command,
    _read_output_should_be_protected,
    _tool_call_command_text,
)

ROOT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path.home() / ".codex" / "sessions"


def output_text(output) -> str:
    if isinstance(output, str):
        return output
    if isinstance(output, list):
        return "".join(p.get("text", "") for p in output if isinstance(p, dict) and isinstance(p.get("text"), str))
    return ""


def read_command(item: dict) -> str:
    """The proxy's read detection for one tool-call item ('' when not a read)."""
    t = item.get("type")
    if t == "custom_tool_call":
        commands = _custom_tool_call_commands(item.get("input"))
        return next((c for c in commands if c is not None and _is_read_command(c)),
                    "exec_command(<cmd not a string literal>)" if None in commands else "")
    if t == "function_call":
        command = _tool_call_command_text(item.get("arguments"))
        return command if command and _is_read_command(command) else ""
    return ""


def main() -> None:
    carried = Counter()
    counts = Counter()
    for path in sorted(ROOT.glob("**/*.jsonl")):
        calls: dict[str, dict] = {}
        outputs: list[tuple[str, int]] = []
        for raw in path.open(encoding="utf-8", errors="replace"):
            try:
                pl = json.loads(raw).get("payload") or {}
            except json.JSONDecodeError:
                continue
            t = pl.get("type")
            if t in ("custom_tool_call", "function_call"):
                calls[pl.get("call_id", "")] = pl
            elif t in ("custom_tool_call_output", "function_call_output"):
                call = calls.get(pl.get("call_id", ""), {})
                text = output_text(pl.get("output"))
                tool = call.get("name", "?")
                rc = read_command(call)
                if len(text) < 1000:  # roughly the router's 250-token floor
                    verdict = "too small"
                elif rc and _read_output_should_be_protected(text):
                    verdict = "protected as read" + (" (script not analysable)" if rc.startswith("exec_command(<") else "")
                else:
                    verdict = "offered for compression"
                outputs.append((f"{tool}: {verdict}", len(text.encode("utf-8", errors="replace"))))
        for i, (key, size) in enumerate(outputs):
            later = len(outputs) - i - 1
            carried[key] += size / 4 * later
            counts[key] += 1

    total = sum(carried.values())
    print(f"Tool results: {sum(counts.values())}, re-sent context from them: {total / 1e6:.1f}M tokens")
    for key, v in carried.most_common():
        print(f"  {key:62} {counts[key]:5} results  {v / 1e6:6.1f}M  ({v / total:4.0%})")


if __name__ == "__main__":
    main()
