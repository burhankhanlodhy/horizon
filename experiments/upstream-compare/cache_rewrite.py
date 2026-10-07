"""What the pre-#3810 retrieval-tool injection costs on real Claude Code sessions.

Before the port, the proxy added ``horizon_retrieve`` to the tools array at a
session's first compression. Tools head Anthropic's cache key, so the whole
warm prefix was written again: prefix x (one-hour write - read) dollars. After
the port the tool goes in on the first request, inside the write the session
pays anyway (~119 tokens). The prefix is the previous request's cached input,
from the transcript's own usage records.
Usage: python cache_rewrite.py <corpus_dir>   (horizon on PYTHONPATH)
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", HORIZON_DETECT_BACKEND="rust")
from transcripts import read_rows, text_of  # noqa: E402

from horizon.compress import compress  # noqa: E402

WRITE_1H, READ = 4e-6, 0.2e-6
EAGER_TOOL_TOKENS = 119


def first_rewrite(path: Path) -> tuple[int, int] | None:
    """(request index, warm prefix tokens) at the first compression, else None."""
    names, seen, prefix, requests = {}, set(), 0, 0
    for row in read_rows(path):
        msg = row.get("message") or {}
        if row.get("type") == "assistant" and msg.get("model") != "<synthetic>":
            ident = msg.get("id") or row.get("uuid")
            if ident not in seen:
                seen.add(ident)
                requests += 1
                u = msg.get("usage") or {}
                prefix = sum(
                    u.get(k) or 0
                    for k in (
                        "input_tokens",
                        "cache_read_input_tokens",
                        "cache_creation_input_tokens",
                    )
                )
            for b in msg.get("content") or []:
                if isinstance(b, dict) and b.get("type") == "tool_use":
                    names[b.get("id")] = b.get("name", "?")
        elif row.get("type") == "user" and isinstance(msg.get("content"), list):
            for b in msg["content"]:
                if not (isinstance(b, dict) and b.get("type") == "tool_result"):
                    continue
                text = text_of(b.get("content"))
                if len(text) < 1000:
                    continue
                r = compress(
                    [
                        {"role": "user", "content": "Continue."},
                        {
                            "role": "assistant",
                            "content": [
                                {
                                    "type": "tool_use",
                                    "id": "t",
                                    "name": names.get(b.get("tool_use_id"), "?"),
                                    "input": {},
                                }
                            ],
                        },
                        {
                            "role": "user",
                            "content": [
                                {"type": "tool_result", "tool_use_id": "t", "content": text}
                            ],
                        },
                    ],
                    model="claude-sonnet-5-5",
                    protect_recent=0,
                )
                if r.tokens_saved > 0:
                    return requests, prefix
    return None


def main() -> None:
    from horizon.transforms.kompress_compressor import _load_kompress

    _load_kompress(allow_download=False)  # the proxy preloads it; compress() does not
    rows = []
    for path in sorted((Path(sys.argv[1]) / "claude").glob("*.jsonl")):
        hit = first_rewrite(path)
        if hit and hit[0] > 1:  # a first-request compression rewrites nothing
            rows.append(hit)
    lost = sum(p for _, p in rows)
    print(f"sessions whose first compression lands on a warm cache: {len(rows)}")
    if rows:
        print(f"  request where it lands: median {sorted(r for r, _ in rows)[len(rows) // 2]}")
        print(
            f"  prefix rewritten: {lost:,} tokens, median {sorted(p for _, p in rows)[len(rows) // 2]:,} per session"
        )
    print(
        f"  cost before the port: ${lost * (WRITE_1H - READ):.2f}  "
        f"(after: ~${len(rows) * EAGER_TOOL_TOKENS * WRITE_1H:.4f} for the tool on first requests)"
    )


if __name__ == "__main__":
    main()
