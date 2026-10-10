"""What clearing prior-turn thinking would have saved on a real Claude Code session.

Opus 4.5+ and Sonnet 4.6+ keep every earlier turn's thinking in context and
bill it as input on each later request (Anthropic: "Thinking block preservation
by model"). The clear_thinking_20251015 context-editing strategy can keep only
the current turn's thinking. Replays the main conversation using the billed
thinking tokens each response reports (usage.output_tokens_details):

- saving: every request stops re-reading thinking from earlier user turns, at
  that request's cache-read rate;
- cost: the first request of each new user turn rewrites the previous turn's
  remaining content into the cache (its thinking left the prefix), at the
  one-hour write rate instead of the read rate.

Client compactions reset the carried thinking. Prints totals only.
Usage: python thinking_clear.py <transcript.jsonl> [<transcript.jsonl> ...]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import litellm


def rates(model: str) -> tuple[float, float]:
    info = litellm.model_cost.get(model) or litellm.model_cost["claude-sonnet-5-5"]
    base = info["input_cost_per_token"]
    return (info.get("cache_read_input_token_cost") or base * 0.1,
            info.get("cache_creation_input_token_cost_above_1hr") or base * 2)


def events(path: Path):
    """("prompt",) for a real user prompt, ("compact",), or ("req", model, ctx, thinking, cost)."""
    seen = set()
    for line in path.open(encoding="utf-8", errors="replace"):
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if r.get("isSidechain"):
            continue
        m = r.get("message") or {}
        if r.get("type") == "system" and r.get("subtype") == "compact_boundary":
            yield ("compact",)
        elif r.get("type") == "user" and not r.get("isMeta"):
            c = m.get("content")
            if isinstance(c, str) or (isinstance(c, list) and c and not any(
                    isinstance(b, dict) and b.get("type") == "tool_result" for b in c)):
                yield ("prompt",)
        elif r.get("type") == "assistant" and m.get("model") not in (None, "<synthetic>"):
            if m.get("id") in seen:
                continue
            seen.add(m.get("id"))
            u = m.get("usage") or {}
            ctx = sum(u.get(k) or 0 for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
            yield ("req", m["model"], ctx, (u.get("output_tokens_details") or {}).get("thinking_tokens") or 0, u)


def replay(path: Path) -> dict:
    bill = saved = spent = 0.0
    carried = 0          # thinking from finished turns still in context (kept by default)
    turn_thinking = 0    # thinking produced in the current user turn
    turn_start_ctx = prev_turn_start_ctx = None
    prev_turn_thinking = 0
    new_turn = False
    reqs = prompts = 0
    peak_share = []
    for ev in events(path):
        if ev[0] == "compact":
            carried = turn_thinking = prev_turn_thinking = 0
            turn_start_ctx = prev_turn_start_ctx = None
            continue
        if ev[0] == "prompt":
            new_turn = True
            continue
        _, model, ctx, thinking, u = ev
        info = litellm.model_cost.get(model) or litellm.model_cost["claude-sonnet-5-5"]
        cc = u.get("cache_creation") or {}
        w1h_tokens = cc.get("ephemeral_1h_input_tokens") or 0
        w5_tokens = (u.get("cache_creation_input_tokens") or 0) - w1h_tokens
        read_rate, w1h = rates(model)
        bill += ((u.get("input_tokens") or 0) * info["input_cost_per_token"]
                 + (u.get("cache_read_input_tokens") or 0) * read_rate
                 + w5_tokens * (info.get("cache_creation_input_token_cost") or 0)
                 + w1h_tokens * w1h + (u.get("output_tokens") or 0) * info["output_cost_per_token"])
        reqs += 1
        if new_turn:
            prompts += 1
            prev_turn_thinking, carried = turn_thinking, carried + turn_thinking
            prev_turn_start_ctx, turn_start_ctx = turn_start_ctx, ctx
            turn_thinking = 0
            new_turn = False
            if prev_turn_start_ctx is not None:
                # The previous turn's non-thinking content is re-cached once.
                rewrite = max(0, ctx - prev_turn_start_ctx - prev_turn_thinking)
                spent += rewrite * (w1h - read_rate)
        saved += carried * read_rate
        if ctx:
            peak_share.append(carried / ctx)
        turn_thinking += thinking
    return {"bill": bill, "saved": saved, "spent": spent, "requests": reqs, "prompts": prompts,
            "mean_share": sum(peak_share) / max(len(peak_share), 1)}


def main() -> None:
    tot = {"bill": 0.0, "saved": 0.0, "spent": 0.0, "requests": 0, "prompts": 0}
    for p in sys.argv[1:]:
        r = replay(Path(p))
        for k in tot:
            tot[k] += r[k]
        print(f"{Path(p).name[:12]}: {r['requests']} requests, {r['prompts']} prompts, bill ${r['bill']:.2f}; "
              f"prior-turn thinking averaged {r['mean_share']:.0%} of context; "
              f"saved ${r['saved']:.2f}, rewrites ${r['spent']:.2f}, net {(r['saved'] - r['spent']) / max(r['bill'], 1e-9):+.1%}")
    if len(sys.argv) > 2:
        print(f"all: bill ${tot['bill']:.2f}, net saving ${tot['saved'] - tot['spent']:.2f} "
              f"({(tot['saved'] - tot['spent']) / tot['bill']:.1%})")


if __name__ == "__main__":
    main()
