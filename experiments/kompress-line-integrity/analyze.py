"""Compare two measure.py runs: savings lost vs structural safety gained.

argv: <base.json> <fix.json> [view: pipeline|kompress]
"""

from __future__ import annotations

import json
import random
import re
import sys
from collections import defaultdict
from pathlib import Path

base = json.loads(Path(sys.argv[1]).read_text())["rows"]
fix = json.loads(Path(sys.argv[2]).read_text())["rows"]
corpus = {(i["category"], i["id"]): i["text"] for i in json.loads(Path(sys.argv[3]).read_text())}
VIEW = sys.argv[4] if len(sys.argv) > 4 else "pipeline"

LS_ROW = re.compile(r"^[dlcbps-][rwxsStT-]{9}\S*\s+\d+\s+\S+\s+\S+\s+\d+\s")
MARKER = re.compile(r"horizon|compressed|retrieve|\.\.\.|…|\[\d+ (lines|items)", re.I)


def words(line: str) -> list[str]:
    return line.split()


def is_subseq(small: list[str], big: list[str]) -> bool:
    it = iter(big)
    return all(w in it for w in small)


def line_integrity(src: str, out: str) -> tuple[int, int, int]:
    """(exact, trimmed, fabricated) output lines against the source lines."""
    src_lines = [words(l) for l in src.splitlines() if l.strip()]
    exact_set = {" ".join(w) for w in src_lines}
    by_word: dict[str, list[list[str]]] = defaultdict(list)
    for w in src_lines:
        for token in set(w):
            by_word[token].append(w)
    exact = trimmed = fabricated = 0
    for line in out.splitlines():
        w = words(line)
        if not w or MARKER.search(line):
            continue
        if " ".join(w) in exact_set:
            exact += 1
        elif any(is_subseq(w, cand) for cand in by_word.get(w[0], [])):
            trimmed += 1
        else:
            fabricated += 1
    return exact, trimmed, fabricated


def facts(category: str, src: str) -> list[tuple[str, str, list[str]]]:
    """(a, b, source line words): pairs that must stay together on their own line."""
    out = []
    for line in src.splitlines():
        w = line.split()
        if LS_ROW.match(line) and len(w) >= 9:
            out.append((w[4], w[-1], w))  # size, name
        elif category == "pip_table" and len(w) == 2 and not set(line) <= set("- "):
            out.append((w[0], w[1], w))  # package, version
        elif category == "grep" and re.match(r"^[\w./-]+:\d+:", line) and len(w) >= 2:
            out.append((w[0], w[-1], w))  # path:line:, last token
        elif category == "git_log" and re.match(r"^ \S+\s+\|\s+\d+", line):
            out.append((w[0], w[2], w))  # file, change count
    return out


def retained(pairs, out: str) -> int:
    """A fact counts when its two values sit together on an output line with
    nothing from another source line between them (a glued line still counts
    if this fact's own span is intact)."""
    lines = [l.split() for l in out.splitlines()]
    n = 0
    for a, b, src_words in pairs:
        ok = False
        for o in lines:
            if a not in o or b not in o:
                continue
            i, j = o.index(a), len(o) - 1 - o[::-1].index(b)
            lo, hi = min(i, j), max(i, j)
            if is_subseq(o[lo:hi + 1], src_words):
                ok = True
                break
        n += ok
    return n


def table_rows(src: str, out: str) -> tuple[int, int, int]:
    """(intact, partial, dropped) ls -l rows."""
    out_norm = [words(l) for l in out.splitlines()]
    out_exact = {" ".join(w) for w in out_norm}
    intact = partial = dropped = 0
    for line in src.splitlines():
        if not LS_ROW.match(line):
            continue
        w = words(line)
        if " ".join(w) in out_exact:
            intact += 1
        elif any(len(o) >= 2 and is_subseq(o, w) for o in out_norm):
            partial += 1
        else:
            dropped += 1
    return intact, partial, dropped


def saved(row: dict) -> int:
    v = row[VIEW]
    return v["tokens_saved"] if VIEW == "pipeline" else v["original_tokens"] - v["compressed_tokens"]


def before(row: dict) -> int:
    v = row[VIEW]
    return v["tokens_before"] if VIEW == "pipeline" else v["original_tokens"]


cats: dict[str, dict] = defaultdict(lambda: defaultdict(int))
deltas: list[tuple[str, int]] = []
for b, f in zip(base, fix):
    assert (b["category"], b["id"]) == (f["category"], f["id"])
    src = corpus[(b["category"], b["id"])]
    c = cats[b["category"]]
    c["items"] += 1
    c["tokens"] += before(b)
    c["saved_base"] += saved(b)
    c["saved_fix"] += saved(f)
    c["changed"] += b[VIEW]["output"] != f[VIEW]["output"]
    elided = "dense machine-generated" in b[VIEW]["output"]
    if elided and "dense machine-generated" not in f[VIEW]["output"] or (
        b[VIEW]["output"].count("dense machine-generated") > f[VIEW]["output"].count("dense machine-generated")
    ):
        c["elision_items"] += 1
        c["elision_lost"] += saved(b) - saved(f)
    deltas.append((b["category"], saved(b) - saved(f)))
    pairs = facts(b["category"], src)
    c["facts"] += len(pairs)
    for label, row in (("base", b), ("fix", f)):
        out = row[VIEW]["output"]
        e, t, fab = line_integrity(src, out)
        c[f"exact_{label}"] += e
        c[f"trimmed_{label}"] += t
        c[f"fabricated_{label}"] += fab
        c[f"items_fabricated_{label}"] += fab > 0
        c[f"facts_{label}"] += retained(pairs, out)
        i, p, d = table_rows(src, out)
        c[f"rows_intact_{label}"] += i
        c[f"rows_partial_{label}"] += p
        c[f"rows_dropped_{label}"] += d

print(f"view={VIEW}")
hdr = ("category", "n", "tokens", "saved base", "saved fix", "lost", "changed",
       "fab lines b/f", "items w/ fab b/f", "facts kept b/f", "ls rows partial b/f")
print(" | ".join(hdr))
tot = defaultdict(int)
for name, c in sorted(cats.items()):
    for k, v in c.items():
        tot[k] += v
for name, c in [*sorted(cats.items()), ("TOTAL", tot)]:
    lost = c["saved_base"] - c["saved_fix"]
    print(" | ".join(map(str, [
        name, c["items"], c["tokens"],
        f'{c["saved_base"]} ({c["saved_base"] / max(1, c["tokens"]):.1%})',
        f'{c["saved_fix"]} ({c["saved_fix"] / max(1, c["tokens"]):.1%})',
        f'{lost} ({lost / max(1, c["saved_base"]):.1%} of savings)',
        c["changed"],
        f'{c["fabricated_base"]}/{c["fabricated_fix"]}',
        f'{c["items_fabricated_base"]}/{c["items_fabricated_fix"]}',
        f'{c["facts_base"]}/{c["facts_fix"]} of {c["facts"]}',
        f'{c["rows_partial_base"]}/{c["rows_partial_fix"]}',
        f'elision-unlocked loss {c["elision_lost"]} in {c["elision_items"]} items',
    ])))

# Bootstrap: is the savings loss consistent, or driven by a few items?
rng = random.Random(7)
vals = [d for _, d in deltas]
boots = sorted(sum(rng.choice(vals) for _ in vals) for _ in range(5000))
print(f"total tokens lost: {sum(vals)}  95% CI [{boots[125]}, {boots[4875]}]  "
      f"items losing savings: {sum(v > 0 for v in vals)}, gaining: {sum(v < 0 for v in vals)}, "
      f"same: {sum(v == 0 for v in vals)}")
top = sorted(deltas, key=lambda x: -x[1])[:5]
print("largest per-item losses:", top, "share of loss from top 5:",
      f"{sum(d for _, d in top) / max(1, sum(vals)):.0%}")
