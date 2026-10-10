"""Where coding-agent dollars go, and what two proxy-side levers would save.

Reads local Claude Code transcripts (one row per unique model response, priced
with that response's own model and usage) and reports:

1. the bill split: fresh input, cache reads, cache writes (5 min / 1 h), output;
2. cache-write duration: the same requests if every write used the 5-minute
   rate (1.25x) instead of the 1-hour rate (2x), charging a full prefix rewrite
   whenever the gap before a request was 5-60 minutes (a 5-minute entry would
   have expired; a 1-hour one would not);
3. observation masking: every M requests, replace tool results older than K
   requests with a short placeholder. Each pass saves the masked tokens on every
   later read but rewrites the cached context behind the oldest masked result.

Prints totals only. Usage: python cost_anatomy.py <claude_transcript_dir>
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

import litellm

PLACEHOLDER = 25  # tokens left in place of a masked result


def rates(model: str) -> dict[str, float]:
    info = litellm.model_cost.get(model) or litellm.model_cost.get(f"anthropic/{model}") or {}
    if not info.get("input_cost_per_token"):
        info = litellm.model_cost["claude-sonnet-5-5"]
    base = info["input_cost_per_token"]
    return {
        "in": base,
        "read": info.get("cache_read_input_token_cost") or base * 0.1,
        "w5": info.get("cache_creation_input_token_cost") or base * 1.25,
        "w1h": info.get("cache_creation_input_token_cost_above_1hr") or base * 2,
        "out": info["output_cost_per_token"],
    }


def session(path: Path):
    """Requests in order: dict(t, rates, fresh, read, w5, w1h, out, ctx, results_before)."""
    reqs, seen, pending = [], set(), []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        msg = row.get("message") or {}
        if row.get("type") == "system" and row.get("subtype") == "compact_boundary":
            reqs.append("compact")
        elif row.get("type") == "user" and isinstance(msg.get("content"), list):
            for b in msg["content"]:
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    c = b.get("content")
                    text = c if isinstance(c, str) else json.dumps(c)
                    pending.append(len(text) // 4)
        elif row.get("type") == "assistant" and msg.get("model") not in (None, "<synthetic>"):
            ident = msg.get("id") or row.get("uuid")
            if ident in seen:
                continue
            seen.add(ident)
            u = msg.get("usage") or {}
            cc = u.get("cache_creation") or {}
            w1h = cc.get("ephemeral_1h_input_tokens") or 0
            w5 = cc.get("ephemeral_5m_input_tokens") or max(
                0, (u.get("cache_creation_input_tokens") or 0) - w1h
            )
            fresh, read = u.get("input_tokens") or 0, u.get("cache_read_input_tokens") or 0
            reqs.append(
                {
                    "t": datetime.fromisoformat(
                        row["timestamp"].replace("Z", "+00:00")
                    ).timestamp(),
                    "r": rates(msg["model"]),
                    "fresh": fresh,
                    "read": read,
                    "w5": w5,
                    "w1h": w1h,
                    "out": u.get("output_tokens") or 0,
                    "ctx": fresh + read + w5 + w1h,
                    "results": pending,
                    "side": bool(row.get("isSidechain")),
                }
            )
            pending = []
    return reqs


def cost(q) -> float:
    r = q["r"]
    return (
        q["fresh"] * r["in"]
        + q["read"] * r["read"]
        + q["w5"] * r["w5"]
        + q["w1h"] * r["w1h"]
        + q["out"] * r["out"]
    )


def ttl_5m_delta(reqs) -> float:
    """Extra (+) or saved (-) dollars if every write were a 5-minute write."""
    delta, prev_t = 0.0, None
    for q in reqs:
        if q == "compact":
            continue
        r = q["r"]
        delta += q["w1h"] * (r["w5"] - r["w1h"])
        if prev_t is not None and 300 < q["t"] - prev_t <= 3600:
            delta += q["read"] * (r["w5"] - r["read"])  # the cached prefix would have expired
        prev_t = q["t"]
    return delta


def masking_delta(reqs, keep: int, every: int) -> tuple[float, float]:
    """(dollars saved by smaller reads, dollars spent rewriting the cache) for one session."""
    saved = spent = 0.0
    live: list[list[int]] = []  # [request index, tokens, masked?]
    masked_total, idx = 0, 0
    for q in reqs:
        if q == "compact":
            live, masked_total = [], 0
            continue
        idx += 1
        live += [[idx, t, False] for t in q["results"] if t >= 200]
        r = q["r"]
        saved += masked_total * r["read"]  # tokens this request no longer re-reads
        if idx % every == 0:
            fresh = [x for x in live if not x[2] and x[0] <= idx - keep]
            if fresh:
                gain = sum(x[1] - PLACEHOLDER for x in fresh)
                oldest = min(x[0] for x in fresh)
                behind = sum(x[1] for x in live if x[0] >= oldest)  # crude: results behind the edit
                tail = max(0, q["ctx"] * (idx - oldest) / max(idx, 1) - gain)
                spent += max(tail, behind - gain) * (r["w1h"] - r["read"])
                for x in fresh:
                    x[2] = True
                masked_total += gain
    return saved, spent


def main() -> None:
    sessions = [
        s
        for s in (session(p) for p in sorted(Path(sys.argv[1]).glob("*.jsonl")))
        if any(q != "compact" for q in s)
    ]
    reqs = [q for s in sessions for q in s if q != "compact"]
    bill = sum(cost(q) for q in reqs)
    parts = {
        "fresh input": sum(q["fresh"] * q["r"]["in"] for q in reqs),
        "cache reads": sum(q["read"] * q["r"]["read"] for q in reqs),
        "cache writes 5m": sum(q["w5"] * q["r"]["w5"] for q in reqs),
        "cache writes 1h": sum(q["w1h"] * q["r"]["w1h"] for q in reqs),
        "output + thinking": sum(q["out"] * q["r"]["out"] for q in reqs),
    }
    print(f"Claude Code: {len(sessions)} sessions, {len(reqs):,} model requests, bill ${bill:.2f}")
    for k, v in parts.items():
        print(f"  {k:18} ${v:8.2f}  {v / bill:5.1%}")
    d = sum(ttl_5m_delta(s) for s in sessions)
    print(
        f"\n5-minute writes instead of 1-hour: {'saves' if d < 0 else 'costs'} ${abs(d):.2f} = {abs(d) / bill:.1%} of the bill"
    )
    print(
        "\nObservation masking (keep last K requests' tool results, mask in passes every M requests):"
    )
    for keep, every in ((10, 10), (20, 20), (30, 30), (50, 50)):
        sv = sp = 0.0
        for s in sessions:
            a, b = masking_delta(s, keep, every)
            sv += a
            sp += b
        print(
            f"  K={keep:2} M={every:2}: reads saved ${sv:6.2f}, rewrites ${sp:6.2f}, net {(sv - sp) / bill:+.1%} of the bill"
        )


if __name__ == "__main__":
    main()


def compaction_threshold(reqs, limit: int, base: int = 30_000, summary: int = 15_000) -> float:
    """Simulated bill if the client compacted whenever context passed ``limit`` tokens.

    Replays each request's real growth. A compaction reads the whole context
    once (cache read), writes a ``summary``-token summary as output, and starts
    a new prefix of ``base`` + ``summary`` tokens written at the one-hour rate.
    Every request then reads its simulated context and writes its real new tokens.
    """
    total, ctx, prev_real = 0.0, None, None
    for q in reqs:
        if q == "compact":
            prev_real = None
            continue
        r = q["r"]
        if q["side"]:  # subagents keep their own small contexts: charge them as they were
            total += cost(q)
            continue
        grow = q["w5"] + q["w1h"] + q["fresh"]
        if ctx is None or prev_real is None:
            # Session start, or right after one of the client's own compactions.
            ctx = q["ctx"] if ctx is None else min(ctx, q["ctx"])
        else:
            ctx += max(0, q["ctx"] - prev_real)
        prev_real = q["ctx"]
        if ctx > limit:
            total += ctx * r["read"] + summary * r["out"] + (base + summary) * r["w1h"]
            ctx = base + summary + grow
        total += max(0, ctx - grow) * r["read"] + grow * r["w1h"] + q["out"] * r["out"]
    return total


if __name__ == "__main__" and len(sys.argv) > 2 and sys.argv[2] == "--compaction":
    biggest = max(Path(sys.argv[1]).glob("*.jsonl"), key=lambda p: p.stat().st_size)
    s = session(biggest)
    actual = sum(cost(q) for q in s if q != "compact")
    print(f"\nLargest session, actual bill ${actual:.2f}; simulated with compaction at:")
    no_extra = compaction_threshold(s, 10**9)
    print(f"  client's own compactions only (model check) ${no_extra:7.2f}")
    for limit in (150_000, 200_000, 300_000, 500_000):
        sim = compaction_threshold(s, limit)
        print(f"  {limit // 1000:4}k tokens  ${sim:7.2f}  ({sim / no_extra - 1:+.0%})")
