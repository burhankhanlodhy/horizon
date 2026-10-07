"""Paired analysis of the Codex output-shaper runs (results.jsonl + both proxy logs)."""

import json
import os
import statistics
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).parent
LOGS = Path(os.environ["LOCALAPPDATA"]) / "ContextShrinkExperiment" / "codex-run"
# gpt-6.1-sol list price per token (Horizon's catalog), cached input at the
# 0.5 fallback (cost.py); OpenAI has no cache-write charge.
PIN, POUT, READ = 2.0 / 1e6, 10.0 / 1e6, 0.5


def ts(s: str) -> float:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def load_requests(arm: str) -> list[dict]:
    out = []
    for line in (LOGS / arm / "requests.jsonl").read_text(encoding="utf-8").splitlines():
        if line.strip():
            r = json.loads(line)
            t = r["timestamp"]
            r["_t"] = ts(t if ("+" in t[10:] or t.endswith("Z")) else t + "+00:00")
            out.append(r)
    return out


runs = [json.loads(l) for l in (HERE / "results.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
reqs = {a: load_requests(a) for a in ("treatment", "control")}
for run in runs:
    lo, hi = ts(run["start"]) - 1, ts(run["end"]) + 1
    mine = [r for r in reqs[run["arm"]] if lo <= r["_t"] <= hi]
    run["requests"] = len(mine)
    run["out"] = sum(r.get("output_tokens") or 0 for r in mine)
    run["uncached"] = sum(r.get("uncached_input_tokens") or 0 for r in mine)
    run["read"] = sum(r.get("cache_read_tokens") or 0 for r in mine)
    run["cost"] = run["uncached"] * PIN + run["read"] * PIN * READ + run["out"] * POUT
    run["shaped"] = sum(any(t.startswith("output_shaper:verbosity") for t in r.get("transforms_applied", [])) for r in mine)

print(f"{'task':12}{'rep':>4} {'arm':10}{'pass':>5}{'secs':>7}{'reqs':>6}{'out tok':>9}{'in tok':>10}{'cost $':>9}{'answer':>8}")
for r in sorted(runs, key=lambda r: (r["task"], r["rep"], r["arm"])):
    print(f"{r['task']:12}{r['rep']:>4} {r['arm']:10}{'yes' if r['passed'] else 'NO':>5}{r['secs']:>7.0f}{r['requests']:>6}"
          f"{r['out']:>9}{r['uncached'] + r['read']:>10}{r['cost']:>9.4f}{r['answer_chars']:>8}")

pairs = {}
for r in runs:
    pairs.setdefault((r["task"], r["rep"]), {})[r["arm"]] = r
pairs = [p for p in pairs.values() if len(p) == 2]


def tot(arm, key):
    return sum(p[arm][key] for p in pairs)


print(f"\nPaired runs: {len(pairs)}")
for key, name in (("out", "output tokens"), ("uncached", "uncached input"), ("read", "cached input"),
                  ("requests", "requests"), ("secs", "seconds"), ("cost", "cost (USD)"), ("answer_chars", "final answer chars")):
    c, t = tot("control", key), tot("treatment", key)
    change = (t - c) / c if c else 0.0
    print(f"  {name:20} control {c:>12,.4f}  treatment {t:>12,.4f}  change {change:+.1%}")
passes = {a: sum(p[a]["passed"] for p in pairs) for a in ("control", "treatment")}
print(f"  tasks passed         control {passes['control']}/{len(pairs)}  treatment {passes['treatment']}/{len(pairs)}")
ratios = [p["treatment"]["out"] / p["control"]["out"] for p in pairs if p["control"]["out"]]
if ratios:
    print(f"  per-pair output ratio (treatment/control): median {statistics.median(ratios):.2f}, "
          f"lower in {sum(x < 1 for x in ratios)} of {len(ratios)} pairs")
print(f"  requests shaped in treatment: {sum(r['shaped'] for r in runs if r['arm'] == 'treatment')}"
      f" of {sum(r['requests'] for r in runs if r['arm'] == 'treatment')}")
