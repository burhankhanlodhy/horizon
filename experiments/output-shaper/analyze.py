"""Compare the output-shaper experiment's two arms from the proxy request log.

The proxy assigns each conversation at random to `treatment` (shaped) or
`control` (unshaped) with HORIZON_OUTPUT_HOLDOUT=0.5, and labels every request
with its arm and conversation. This prices each request from the provider's own
usage numbers (uncached input, cache writes, cache reads, output) and compares
the arms per conversation, the unit that was randomized.

Usage: python analyze.py [requests.jsonl]
"""

from __future__ import annotations

import json
import random
import statistics
import sys
from collections import defaultdict
from pathlib import Path

LOG = Path(sys.argv[1]) if len(sys.argv) > 1 else Path.home() / "AppData/Local/ContextShrinkExperiment/requests.jsonl"

# (cache read, cache write) as fractions of the input price, for catalogs with
# no cache prices (the fallbacks in horizon/proxy/cost.py). OpenAI has no
# cache-write charge, and the proxy logs its written tokens as uncached input
# too, so writes are not priced again there.
CACHE = {"anthropic": (0.1, 1.25), "openai": (0.5, 0.0)}


def price(model: str) -> tuple[float, float] | None:
    from horizon.pricing.litellm_pricing import get_model_pricing

    p = get_model_pricing(model)
    if p is None or not getattr(p, "input_cost_per_1m", None):
        return None
    return p.input_cost_per_1m / 1e6, p.output_cost_per_1m / 1e6


def label(transforms: list[str], prefix: str) -> str | None:
    return next((t[len(prefix):] for t in transforms if t.startswith(prefix)), None)


def main() -> None:
    if not LOG.exists():
        sys.exit(f"No log yet at {LOG}. Use the tools through the experiment proxy first.")
    convs: dict[str, dict] = {}
    unpriced: set[str] = set()
    unlabeled = 0
    for line in LOG.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        t = r.get("transforms_applied") or []
        conv = label(t, "output_shaper:conv:")
        arm = "treatment" if label(t, "output_shaper:stratum:") else "control" if label(t, "output_shaper:control:") else None
        if conv is None or arm is None:
            unlabeled += 1
            continue
        c = convs.setdefault(conv, {"arm": arm, "requests": 0, "cost": 0.0, "out": 0, "in": 0, "kinds": defaultdict(int)})
        c["requests"] += 1
        out = r.get("output_tokens") or 0
        uncached = r.get("uncached_input_tokens") or 0
        read = r.get("cache_read_tokens") or 0
        write = r.get("cache_write_tokens") or 0
        if not (uncached or read or write):
            uncached = r.get("input_tokens_optimized") or 0
        c["out"] += out
        c["in"] += uncached + read + write
        p = price(r.get("model") or "")
        if p is None:
            unpriced.add(r.get("model") or "?")
            continue
        pin, pout = p
        rd, wr = CACHE.get(r.get("provider") or "", (0.5, 1.0))
        c["cost"] += uncached * pin + read * pin * rd + write * pin * wr + out * pout

    arms = {a: [c for c in convs.values() if c["arm"] == a] for a in ("control", "treatment")}
    print(f"Log: {LOG}")
    print(f"Conversations: control {len(arms['control'])}, treatment {len(arms['treatment'])}"
          f"  (requests without an arm label: {unlabeled})")
    if unpriced:
        print(f"No price for: {', '.join(sorted(unpriced))} (left out of dollar totals)")
    print()
    print(f"{'':28}{'control':>14}{'treatment':>14}")
    rows = [
        ("requests per conversation", lambda c: c["requests"], "{:.1f}"),
        ("output tokens per request", lambda c: c["out"] / c["requests"], "{:.0f}"),
        ("input tokens per request", lambda c: c["in"] / c["requests"], "{:.0f}"),
        ("cost per request (USD)", lambda c: c["cost"] / c["requests"], "{:.4f}"),
        ("cost per conversation (USD)", lambda c: c["cost"], "{:.3f}"),
    ]
    for name, fn, fmt in rows:
        vals = [fmt.format(statistics.mean(map(fn, arms[a]))) if arms[a] else "-" for a in ("control", "treatment")]
        print(f"{name:28}{vals[0]:>14}{vals[1]:>14}")

    # Difference in cost per request, with a bootstrap 95% interval over conversations.
    ctl, trt = arms["control"], arms["treatment"]
    if len(ctl) >= 3 and len(trt) >= 3:
        def per_req(cs):
            return sum(c["cost"] for c in cs) / max(sum(c["requests"] for c in cs), 1)

        base = per_req(ctl)
        diffs = []
        rng = random.Random(7)
        for _ in range(2000):
            a = [rng.choice(ctl) for _ in ctl]
            b = [rng.choice(trt) for _ in trt]
            if per_req(a):
                diffs.append(1 - per_req(b) / per_req(a))
        diffs.sort()
        lo, hi = diffs[int(0.025 * len(diffs))], diffs[int(0.975 * len(diffs))]
        est = 1 - per_req(trt) / base if base else 0
        print()
        print(f"Shaped conversations cost {est:+.1%} less per request (95% interval {lo:+.1%} to {hi:+.1%}).")
        print("An interval that includes 0% means there isn't enough data yet to tell.")
    else:
        print("\nNeed at least 3 conversations in each arm for a comparison.")


if __name__ == "__main__":
    main()
