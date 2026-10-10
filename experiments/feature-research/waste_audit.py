"""Paid-for waste in Claude Code sessions that a proxy could remove without changing the model.

1. Cold rewrites: requests whose cached prefix had expired or changed, so most
   of the context was written again (one-hour rate) instead of read. Split by
   the idle gap before them, and priced against a keep-alive that re-reads the
   prefix every 55 minutes until the user returns (up to a window).
2. Repeated text: lines of tool output (20+ characters) that already entered
   the context earlier in the same segment (since the last compaction), weighted
   by the requests that re-read them afterwards.

Uses each response's own model and usage. Prints totals only.
Usage: python waste_audit.py <transcript.jsonl> [...]
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

import litellm

PING_EVERY = 55 * 60
WINDOWS_H = (2, 4, 8, 12, 24)


def r_(model: str) -> dict:
    i = litellm.model_cost.get(model) or litellm.model_cost["claude-sonnet-5-5"]
    b = i["input_cost_per_token"]
    return {"read": i.get("cache_read_input_token_cost") or b * 0.1,
            "w1h": i.get("cache_creation_input_token_cost_above_1hr") or b * 2}


def rows(path: Path):
    for line in path.open(encoding="utf-8", errors="replace"):
        try:
            yield json.loads(line)
        except json.JSONDecodeError:
            continue


def ts(s: str) -> float:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def cold_rewrites(path: Path) -> dict:
    seen, prev_t = set(), None
    out = {"cold": 0, "cold_usd": 0.0, "by_gap": {"<5m": [0, 0.0], "5-60m": [0, 0.0], "1-2h": [0, 0.0],
                                                  "2-4h": [0, 0.0], "4-8h": [0, 0.0], ">8h": [0, 0.0]},
           "keepalive": {w: 0.0 for w in WINDOWS_H}}
    last = None
    for r in rows(path):
        m = r.get("message") or {}
        if r.get("isSidechain") or r.get("type") != "assistant" or m.get("model") in (None, "<synthetic>"):
            continue
        if m.get("id") in seen:
            continue
        seen.add(m.get("id"))
        u = m.get("usage") or {}
        t = ts(r["timestamp"])
        write = u.get("cache_creation_input_tokens") or 0
        read = u.get("cache_read_input_tokens") or 0
        ctx = write + read + (u.get("input_tokens") or 0)
        rate = r_(m["model"])
        if prev_t is not None and ctx > 50_000 and write > 0.5 * ctx:
            gap = t - prev_t
            usd = write * rate["w1h"]
            out["cold"] += 1
            out["cold_usd"] += usd
            key = ("<5m" if gap < 300 else "5-60m" if gap < 3600 else "1-2h" if gap < 7200
                   else "2-4h" if gap < 14400 else "4-8h" if gap < 28800 else ">8h")
            out["by_gap"][key][0] += 1
            out["by_gap"][key][1] += usd
            if gap >= 3600:
                for w in WINDOWS_H:
                    if gap <= w * 3600:  # pings would have kept it warm: pay reads, not the rewrite
                        pings = int(gap // PING_EVERY)
                        out["keepalive"][w] += usd - write * rate["read"] - pings * last["ctx"] * last["read"]
        prev_t = t
        last = {"ctx": ctx, "read": rate["read"]}
    # Pings after the session's last request are spent with no return.
    if last:
        for w in WINDOWS_H:
            out["keepalive"][w] -= int(w * 3600 // PING_EVERY) * last["ctx"] * last["read"]
    return out


def repeated_text(path: Path) -> dict:
    """Tool-output lines already present earlier in the segment, weighted by later re-reads."""
    seen_lines: set[str] = set()
    pending: list[tuple[int, int]] = []  # (request index when added, duplicate chars)
    req = 0
    total_chars = dup_chars = 0
    weighted = 0.0
    read_rate = None
    seen_ids = set()
    segment_start = 0
    reqs_rates = []
    for r in rows(path):
        m = r.get("message") or {}
        if r.get("isSidechain"):
            continue
        if r.get("type") == "system" and r.get("subtype") == "compact_boundary":
            for added, d in pending:
                weighted += d / 4 * sum(reqs_rates[added:req])
            pending, seen_lines, segment_start = [], set(), req
            continue
        content = m.get("content")
        if r.get("type") == "assistant" and m.get("model") not in (None, "<synthetic>"):
            if m.get("id") not in seen_ids:
                seen_ids.add(m.get("id"))
                req += 1
                read_rate = r_(m["model"])["read"]
                reqs_rates.append(read_rate)
            for b in content if isinstance(content, list) else []:
                if isinstance(b, dict) and b.get("type") == "tool_use":
                    for line in json.dumps(b.get("input")).replace("\\n", "\n").splitlines():
                        if len(line.strip()) >= 20:
                            seen_lines.add(line.strip())
        elif r.get("type") == "user" and isinstance(content, list):
            for b in content:
                if not (isinstance(b, dict) and b.get("type") == "tool_result"):
                    continue
                c = b.get("content")
                text = c if isinstance(c, str) else "\n".join(
                    x.get("text", "") for x in c if isinstance(x, dict)) if isinstance(c, list) else ""
                d = 0
                for line in text.splitlines():
                    s = line.strip()
                    total_chars += len(line) + 1
                    if len(s) >= 20:
                        if s in seen_lines:
                            d += len(line) + 1
                        seen_lines.add(s)
                dup_chars += d
                pending.append((req, d))
    for added, d in pending:
        weighted += d / 4 * sum(reqs_rates[added:req])
    return {"tool_chars": total_chars, "dup_chars": dup_chars, "reread_usd": weighted,
            "segment_start": segment_start}


def main() -> None:
    for p in map(Path, sys.argv[1:]):
        c = cold_rewrites(p)
        d = repeated_text(p)
        print(f"{p.name[:12]}: {c['cold']} cold rewrites costing ${c['cold_usd']:.2f}")
        for k, (n, usd) in c["by_gap"].items():
            if n:
                print(f"    idle gap {k:>5}: {n:3} rewrites  ${usd:7.2f}")
        for w, net in c["keepalive"].items():
            print(f"  keep-alive pings for up to {w} h idle: net {'saving' if net > 0 else 'cost'} ${abs(net):.2f}")
        share = d["dup_chars"] / max(d["tool_chars"], 1)
        print(f"  repeated tool-output text: {share:.0%} of tool-output characters; "
              f"its later re-reads cost ${d['reread_usd']:.2f} (plus its one-time cache write)")


if __name__ == "__main__":
    main()
