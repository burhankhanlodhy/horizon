"""Compress the same inputs with one Horizon/Headroom build and record what it removes.

Run once per build (each in its own interpreter, since both ship a native
extension), then ``report.py`` puts the runs side by side. Inputs:

- the fixed benchmark payloads (``bench_compress.py``'s set), one at a time as
  the newest tool result of a short conversation;
- a frozen copy of local Codex and Claude Code transcripts: every tool result
  the proxy would offer for compression (1,000+ characters, Codex reads passed
  through the build's own read gate), weighted by the deduplicated model
  requests that re-send it until the next compaction (rules in transcripts.py).

Dollars use fixed rate profiles, so builds are compared on equal terms.
Prints nothing per output; writes results/<label>.json.
Usage: python compare.py <package: horizon|headroom> <label> <payload_dir> <corpus_dir>
"""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import sys
import time
from pathlib import Path

PACKAGE, LABEL, PAYLOADS, CORPUS = sys.argv[1], sys.argv[2], Path(sys.argv[3]), Path(sys.argv[4])
for prefix in ("HORIZON", "HEADROOM"):
    os.environ.setdefault(f"{prefix}_DETECT_BACKEND", "rust")
os.environ.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", LITELLM_LOCAL_MODEL_COST_MAP="True")

# Import the build under test before anything else touches sys.path.
compress = importlib.import_module(f"{PACKAGE}.compress").compress
router = importlib.import_module(f"{PACKAGE}.transforms.content_router")
load_kompress = importlib.import_module(f"{PACKAGE}.transforms.kompress_compressor")._load_kompress
build_file = importlib.import_module(PACKAGE).__file__

from transcripts import claude_session, codex_session, read_rows  # noqa: E402

HERE = Path(__file__).parent
PROFILE = {"codex": (2.25e-6, 0.1e-6, "gpt-6.1-sol"), "claude": (4e-6, 0.2e-6, "claude-sonnet-5-5")}


def conversation(text: str, tool: str) -> list[dict]:
    return [
        {"role": "user", "content": "Continue the task."},
        {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "t", "name": tool, "input": {}}],
        },
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t", "content": text}]},
    ]


def timed(text: str, tool: str, model: str) -> tuple[int, int, float]:
    t0 = time.perf_counter()
    r = compress(conversation(text, tool), model=model, protect_recent=0)
    return r.tokens_before, max(0, r.tokens_saved), time.perf_counter() - t0


def read_command(call: dict) -> bool:
    if call.get("type") == "custom_tool_call":
        return any(
            c is not None and router._is_read_command(c)
            for c in router._custom_tool_call_commands(call.get("input"))
        )
    if call.get("type") == "function_call":
        try:
            args = json.loads(call.get("arguments") or "{}")
        except json.JSONDecodeError:
            return False
        command = args.get("cmd") or args.get("command")
        if isinstance(command, list):
            command = " ".join(map(str, command))
        return bool(command) and router._is_read_command(command)
    return False


def bench() -> list[dict]:
    rows = []
    for path in sorted(p for p in PAYLOADS.iterdir() if p.is_file()):
        before, saved, secs = timed(
            path.read_text(encoding="utf-8", errors="replace"), "Bash", "claude-sonnet-5-5"
        )
        rows.append(
            {
                "payload": path.name,
                "tokens_before": before,
                "tokens_saved": saved,
                "seconds": round(secs, 2),
            }
        )
    return rows


def transcripts() -> dict:
    out = {}
    for client in ("codex", "claude"):
        first_rate, read_rate, model = PROFILE[client]
        t = {
            "sessions": 0,
            "offered": 0,
            "offered_tokens": 0,
            "compressed": 0,
            "first_tokens": 0,
            "repeat_token_requests": 0,
            "seconds": 0.0,
            "max_seconds": 0.0,
            "over_5s": 0,
        }
        cache: dict[str, tuple[int, int, float]] = {}
        for path in sorted((CORPUS / client).rglob("*.jsonl")):
            events, _, _ = (codex_session if client == "codex" else claude_session)(read_rows(path))
            if not any(k == "request" for k, _ in events):
                continue
            t["sessions"] += 1
            for i, (kind, item) in enumerate(events):
                if kind != "result":
                    continue
                call, text, tool = item
                later = 0
                for k, _ in events[i + 1 :]:
                    if k == "reset":
                        break
                    later += k == "request"
                if len(text) < 1000 or not later:
                    continue
                if (
                    client == "codex"
                    and read_command(call)
                    and router._read_output_should_be_protected(text)
                ):
                    continue
                key = hashlib.sha256(f"{tool}\0{text}".encode()).hexdigest()
                if key not in cache:
                    cache[key] = timed(text, tool, model)
                    secs = cache[key][2]
                    t["seconds"] += secs
                    t["max_seconds"] = max(t["max_seconds"], secs)
                    t["over_5s"] += secs > 5
                before, saved, _ = cache[key]
                t["offered"] += 1
                t["offered_tokens"] += before
                t["compressed"] += saved > 0
                t["first_tokens"] += saved
                t["repeat_token_requests"] += saved * (later - 1)
        t.update(
            first_usd=t["first_tokens"] * first_rate,
            repeat_usd=t["repeat_token_requests"] * read_rate,
            unique_outputs=len(cache),
            seconds=round(t["seconds"], 1),
            max_seconds=round(t["max_seconds"], 2),
        )
        out[client] = t
    return out


def main() -> None:
    backend = load_kompress(allow_download=False)[2]
    timed("warm up " * 400, "Bash", "claude-sonnet-5-5")
    result = {
        "label": LABEL,
        "package": PACKAGE,
        "build": build_file,
        "kompress_backend": str(backend),
        "bench": bench(),
        "transcripts": transcripts(),
    }
    (HERE / "results").mkdir(exist_ok=True)
    (HERE / "results" / f"{LABEL}.json").write_text(json.dumps(result, indent=1))
    print(f"{LABEL}: done ({build_file}, kompress={backend})")


if __name__ == "__main__":
    main()
