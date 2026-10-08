"""Session cost of Flash Observations vs the status quo (pure arithmetic, no API calls).

Flash Observations: a large tool output is forwarded as a short permanent stub
in the ``tool_result`` plus the full output in a turn-scoped system message
(``clear_at: "next_user_message"``, beta ``mid-conversation-system-clear-at-
2026-08-21``). The model sees the full output on the one request that follows
it; from the next request on the message is cleared (renders nothing, costs no
input tokens) and the cached prefix up to the stub stays valid.

Run: python experiments/cost-model/flash_observations.py
"""

from __future__ import annotations

import random
from dataclasses import dataclass

M = 1e6
# Opus 5.5 list prices, $/MTok (platform.claude.com pricing, Oct 2026)
PRICE = {"in": 4.0, "w": 5.0, "r": 0.20, "out": 20.0}


@dataclass
class Params:
    turns: int = 150
    system_tokens: int = 30_000  # system prompt + tools, cached from turn 1
    assistant_tokens: int = 1_500  # output per turn incl. thinking (also enters history)
    big_threshold: int = 2_000  # outputs at least this large are flashed
    stub_tokens: int = 250  # permanent stub: what ran, size, head/tail, retrieval key
    renote_tokens: int = 0  # extra output the model spends noting a flashed result
    reneed: float = 0.0  # chance a flashed output is needed again later
    compress_keep: float = 0.74  # status-quo compression keeps 74% (26% removed)
    seed: int = 7


def tool_outputs(p: Params) -> list[int]:
    """Heavy-tailed tool outputs: most small, some very large (file reads, logs, tests)."""
    rng = random.Random(p.seed)
    out = []
    for _ in range(p.turns):
        if rng.random() < 0.55:
            out.append(rng.randint(80, 1_500))
        else:
            out.append(int(min(40_000, rng.lognormvariate(8.6, 0.8))))  # median ~5.4k
    return out


def session_cost(p: Params, scheme: str) -> dict[str, float]:
    rng = random.Random(p.seed + 1)
    outputs = tool_outputs(p)
    ctx = p.system_tokens  # cached context the next request reads
    cost = {"read": 0.0, "write": 0.0, "uncached": 0.0, "out": 0.0}
    cost["write"] += p.system_tokens * PRICE["w"] / M
    for t, f in enumerate(outputs):
        remaining = p.turns - t
        # One request: read the cached context, write what was appended, generate.
        cost["read"] += ctx * PRICE["r"] / M
        out_tokens = p.assistant_tokens
        if scheme == "raw":
            appended = p.assistant_tokens + f
        elif scheme == "compress":
            appended = p.assistant_tokens + int(f * p.compress_keep)
        elif scheme == "flash":
            if f >= p.big_threshold:
                cost["uncached"] += f * PRICE["in"] / M  # rendered once, uncached
                appended = p.assistant_tokens + p.stub_tokens
                out_tokens += p.renote_tokens
                if remaining > 1 and rng.random() < p.reneed:
                    # Needed again later: one extra round trip that re-reads the
                    # context and flashes the output a second time.
                    cost["read"] += (ctx + appended) * PRICE["r"] / M
                    cost["out"] += 300 * PRICE["out"] / M
                    cost["write"] += (300 + p.stub_tokens) * PRICE["w"] / M
                    cost["uncached"] += f * PRICE["in"] / M
                    ctx += 300 + p.stub_tokens
            else:
                appended = p.assistant_tokens + f
        else:
            raise ValueError(scheme)
        cost["write"] += appended * PRICE["w"] / M
        cost["out"] += out_tokens * PRICE["out"] / M
        ctx += appended
    cost["total"] = sum(cost.values())
    cost["final_context"] = ctx
    return cost


def main() -> None:
    print("Opus 5.5, heavy-tailed tool outputs; $ per session (share of raw total)\n")
    for turns in (50, 150, 300):
        base = Params(turns=turns)
        raw = session_cost(base, "raw")
        print(
            f"== {turns} turns: raw ${raw['total']:.2f} (final context {raw['final_context']:,.0f})"
        )
        rows = [
            ("compression at first sight (26% removed)", session_cost(base, "compress")),
            ("flash, never needed again", session_cost(base, "flash")),
            (
                "flash, 15% needed again, +150 note tokens",
                session_cost(Params(turns=turns, reneed=0.15, renote_tokens=150), "flash"),
            ),
            (
                "flash, 30% needed again, +300 note tokens",
                session_cost(Params(turns=turns, reneed=0.30, renote_tokens=300), "flash"),
            ),
        ]
        for name, c in rows:
            print(
                f"   {name:44} ${c['total']:7.2f} ({c['total'] / raw['total'] - 1:+.0%})"
                f"  context {c['final_context']:>9,.0f}"
            )
        print()
    print("Break-even visibility k (turns a flashed output may stay visible by re-appending)")
    for remaining in (5, 20, 50, 100):
        k = (PRICE["w"] + PRICE["r"] * remaining) / PRICE["in"]
        print(f"   {remaining:3} turns left: flash while visible <= {k:.2f} turns")


if __name__ == "__main__":
    main()
