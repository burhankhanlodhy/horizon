"""Compress the corpus with one Horizon build; record savings and outputs.

Two views per item:
- ``pipeline``: the full compress() pipeline, the item as the newest Bash tool
  result of a short conversation (what the proxy compresses on a turn);
- ``kompress``: Kompress alone on the item, to see its behaviour on content
  the router may route elsewhere.
argv: <build dir> <label> <corpus.json> <out dir>
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

BUILD, LABEL, CORPUS, OUT = Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3]), Path(sys.argv[4])
sys.path.insert(0, str(BUILD))
os.environ.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", LITELLM_LOCAL_MODEL_COST_MAP="True")
os.environ.setdefault("HORIZON_DETECT_BACKEND", "rust")

import horizon  # noqa: E402
from horizon.compress import compress  # noqa: E402
from horizon.transforms.kompress_compressor import (  # noqa: E402
    KompressCompressor,
    _load_kompress,
)

assert Path(horizon.__file__).resolve().is_relative_to(BUILD.resolve()), horizon.__file__


def conversation(text: str) -> list[dict]:
    return [
        {"role": "user", "content": "Look into this and summarise what you find."},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "t", "name": "Bash", "input": {}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t", "content": text}]},
    ]


def tool_result_text(messages: list[dict]) -> str:
    content = messages[-1]["content"][-1]["content"]
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content if isinstance(b, dict))
    return str(content)


def main() -> None:
    backend = _load_kompress(allow_download=False)[2]
    compress(conversation("warm up " * 400), model="claude-sonnet-5-5", protect_recent=0)
    kompress = KompressCompressor()
    rows = []
    for item in json.loads(CORPUS.read_text()):
        t0 = time.perf_counter()
        r = compress(conversation(item["text"]), model="claude-sonnet-5-5", protect_recent=0)
        secs = time.perf_counter() - t0
        k = kompress.compress(item["text"])
        rows.append({
            "category": item["category"],
            "id": item["id"],
            "pipeline": {
                "tokens_before": r.tokens_before,
                "tokens_saved": max(0, r.tokens_saved),
                "transforms": list(r.transforms_applied),
                "output": tool_result_text(r.messages),
                "seconds": round(secs, 3),
            },
            "kompress": {
                "original_tokens": k.original_tokens,
                "compressed_tokens": k.compressed_tokens,
                "output": k.compressed,
            },
        })
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / f"{LABEL}.json").write_text(json.dumps({"label": LABEL, "backend": str(backend), "rows": rows}))
    print(f"{LABEL}: {len(rows)} items, kompress backend={backend}")


if __name__ == "__main__":
    main()
