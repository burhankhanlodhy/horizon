"""Side-by-side table of compare.py runs. Usage: python report.py <label> [<label> ...]"""

import json
import sys
from pathlib import Path

runs = [
    json.loads((Path(__file__).parent / "results" / f"{label}.json").read_text())
    for label in sys.argv[1:]
]
labels = [r["label"] for r in runs]
w = max(14, *(len(label) + 2 for label in labels))

print("Benchmark payloads: % of tokens removed (seconds)")
print(f"  {'payload':26}" + "".join(f"{label:>{w}}" for label in labels))
for i, row in enumerate(runs[0]["bench"]):
    cells = []
    for r in runs:
        b = r["bench"][i]
        cells.append(f"{b['tokens_saved'] / b['tokens_before']:.0%} ({b['seconds']:.1f}s)")
    print(f"  {row['payload'][:26]:26}" + "".join(f"{c:>{w}}" for c in cells))
cells = []
for r in runs:
    before = sum(b["tokens_before"] for b in r["bench"])
    cells.append(
        f"{sum(b['tokens_saved'] for b in r['bench']) / before:.1%} ({sum(b['seconds'] for b in r['bench']):.0f}s)"
    )
print(f"  {'TOTAL':26}" + "".join(f"{c:>{w}}" for c in cells))

for client in ("codex", "claude"):
    print(f"\n{client.capitalize()} transcripts (tool results offered for compression)")
    rows = [
        ("outputs offered", lambda t: f"{t['offered']:,}"),
        ("tokens offered", lambda t: f"{t['offered_tokens']:,}"),
        ("tokens removed (once)", lambda t: f"{t['first_tokens']:,}"),
        (
            "% of offered removed",
            lambda t: f"{t['first_tokens'] / max(t['offered_tokens'], 1):.1%}",
        ),
        ("first-removal $", lambda t: f"${t['first_usd']:.3f}"),
        ("repeat (cache-read) $", lambda t: f"${t['repeat_usd']:.3f}"),
        ("total $", lambda t: f"${t['first_usd'] + t['repeat_usd']:.3f}"),
        ("compress time", lambda t: f"{t['seconds']:.0f}s"),
        ("slowest output", lambda t: f"{t['max_seconds']:.1f}s"),
        ("outputs over 5 s", lambda t: f"{t['over_5s']}"),
    ]
    print(f"  {'':26}" + "".join(f"{label:>{w}}" for label in labels))
    for name, fmt in rows:
        print(f"  {name:26}" + "".join(f"{fmt(r['transcripts'][client]):>{w}}" for r in runs))
