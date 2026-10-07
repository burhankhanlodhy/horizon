"""Time Horizon's compression pipeline on fixed tool outputs (same inputs on every machine).

Each payload is the newest tool result in a short agent conversation, which is
what the proxy compresses on a turn. Prints one JSON line per payload, then a
summary. Usage: python bench_compress.py <payload_dir> <label>
"""

import json
import os
import platform
import sys
import time
from pathlib import Path

from horizon.compress import compress


def conversation(tool_output: str) -> list[dict]:
    return [
        {"role": "user", "content": "Look into the failing build and summarise what you find."},
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "Let me look at the output."},
                {"type": "tool_use", "id": "toolu_bench", "name": "Bash", "input": {"command": "run"}},
            ],
        },
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "toolu_bench", "content": tool_output}],
        },
    ]


def main() -> None:
    payload_dir, label = Path(sys.argv[1]), sys.argv[2]
    files = sorted(p for p in payload_dir.iterdir() if p.is_file())
    kwargs = {"model": "claude-sonnet-5-5", "protect_recent": 0}

    # Load models the way the proxy's startup preload does (cache only), so
    # timings never include a load and Kompress is ready for every payload.
    from horizon.transforms.kompress_compressor import _load_kompress

    t = time.perf_counter()
    backend = _load_kompress(allow_download=False)[2]
    compress(conversation("warm up " * 400), **kwargs)
    warmup = time.perf_counter() - t

    rows = []
    for path in files:
        text = path.read_text(encoding="utf-8", errors="replace")
        t = time.perf_counter()
        result = compress(conversation(text), **kwargs)
        secs = time.perf_counter() - t
        row = {
            "machine": label,
            "payload": path.name,
            "chars": len(text),
            "tokens_before": result.tokens_before,
            "tokens_after": result.tokens_after,
            "saved_pct": round(100 * result.tokens_saved / max(result.tokens_before, 1), 1),
            "secs": round(secs, 2),
            "transforms": sorted({t.split(":")[0] for t in result.transforms_applied})[:6],
        }
        rows.append(row)
        print(json.dumps(row), flush=True)

    print(json.dumps({
        "machine": label,
        "summary": True,
        "cpu": platform.processor() or platform.machine(),
        "cores": os.cpu_count(),
        "kompress_backend": backend,
        "detect_backend": os.environ.get("HORIZON_DETECT_BACKEND", "default"),
        "warmup_secs": round(warmup, 1),
        "total_secs": round(sum(r["secs"] for r in rows), 1),
        "tokens_before": sum(r["tokens_before"] for r in rows),
        "tokens_saved": sum(r["tokens_before"] - r["tokens_after"] for r in rows),
        "over_5s": sum(r["secs"] > 5 for r in rows),
        "over_30s": sum(r["secs"] > 30 for r in rows),
    }), flush=True)


if __name__ == "__main__":
    main()
