"""Claude Code read-maturation experiment: per-run and per-arm comparison."""

import json
import os
import statistics
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).parent
LOGS = Path(os.environ["LOCALAPPDATA"]) / "ContextShrinkExperiment" / "claude-run"


def ts(s: str) -> float:
    return datetime.fromisoformat(s if ("+" in s[10:] or s.endswith("Z")) else s + "+00:00").timestamp()


reqs = {}
for arm in ("control", "treatment"):
    rows = []
    for line in (LOGS / arm / "requests.jsonl").read_text(encoding="utf-8").splitlines():
        if line.strip():
            r = json.loads(line)
            r["_t"] = ts(r["timestamp"])
            rows.append(r)
    reqs[arm] = rows

runs = [json.loads(l) for l in (HERE / "results.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
for run in runs:
    lo, hi = ts(run["start"]) - 1, ts(run["end"]) + 1
    mine = [r for r in reqs[run["arm"]] if lo <= r["_t"] <= hi]
    run["proxy_requests"] = len(mine)
    run["orig"] = sum(r.get("input_tokens_original") or 0 for r in mine)
    run["opt"] = sum(r.get("input_tokens_optimized") or 0 for r in mine)
    run["matured"] = sum(any("matur" in t.lower() for t in r.get("transforms_applied", [])) for r in mine)
    run["labels"] = sorted({t.split(":")[0] for r in mine for t in r.get("transforms_applied", []) if "matur" in t.lower()})

print(f"{'arm':10}{'pass':>5}{'secs':>6}{'turns':>6}{'cost $':>8}{'in(uncached)':>13}{'cache rd':>10}{'cache wr':>10}{'out':>7}{'proxy -in%':>11}{'matured':>8}")
for r in sorted(runs, key=lambda r: (r["arm"], r["rep"])):
    cut = 1 - r["opt"] / r["orig"] if r["orig"] else 0
    print(f"{r['arm']:10}{'yes' if r['passed'] else 'NO':>5}{r['secs']:>6.0f}{r['num_turns'] or 0:>6}{r['claude_cost_usd'] or 0:>8.3f}"
          f"{r['input_tokens'] or 0:>13}{r['cache_read'] or 0:>10}{r['cache_write'] or 0:>10}{r['output_tokens'] or 0:>7}{cut:>10.1%}{r['matured']:>8}")

print()
for arm in ("control", "treatment"):
    rs = [r for r in runs if r["arm"] == arm]
    if not rs:
        continue
    m = lambda k: statistics.mean([r[k] or 0 for r in rs])
    print(f"{arm:10} runs {len(rs)}  passed {sum(r['passed'] for r in rs)}/{len(rs)}  mean cost ${m('claude_cost_usd'):.3f}  "
          f"turns {m('num_turns'):.1f}  cache read {m('cache_read'):,.0f}  cache write {m('cache_write'):,.0f}  "
          f"output {m('output_tokens'):,.0f}  secs {m('secs'):.0f}  proxy input cut "
          f"{1 - sum(r['opt'] for r in rs) / max(sum(r['orig'] for r in rs), 1):.1%}  matured requests {sum(r['matured'] for r in rs)}")
labels = sorted({l for r in runs for l in r["labels"]})
print("maturation labels seen:", labels or "none")
