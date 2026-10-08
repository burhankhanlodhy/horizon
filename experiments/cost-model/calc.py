"""Cost model behind wiki/plans/2026-10-08-cost-savings-research.md.

Pure arithmetic over Anthropic list prices (Oct 2026) and third-party GPT-6.1 Sol
rates; no provider calls. Run: python experiments/cost-model/calc.py
"""

# $/MTok from platform.claude.com pricing (Oct 2026)
P = {
    "opus55": {"i": 4, "w5": 5, "w1": 8, "r": 0.20, "o": 20},
    "sonnet55": {"i": 2, "w5": 2.5, "w1": 4, "r": 0.10, "o": 10},
    "fable51": {"i": 10, "w5": 12.5, "w1": 20, "r": 0.25, "o": 50},
    "haiku55lo": {"i": 0.10, "w5": 0.125, "w1": 0.2, "r": 0.01, "o": 0.5},
    "haiku55hi": {"i": 0.50, "w5": 0.625, "w1": 1.0, "r": 0.05, "o": 2.5},
    "sonnet46": {"i": 3, "w5": 3.75, "w1": 6, "r": 0.30, "o": 15},
    "haiku45": {"i": 1, "w5": 1.25, "w1": 2, "r": 0.10, "o": 5},
}
M = 1e6


def turn(m, C, n, o, lane="w5"):
    p = P[m]
    return {"read": C * p["r"] / M, "write": n * p[lane] / M, "out": o * p["o"] / M}


print(
    "== 1. composition of one warm agent turn (C=context read, n=new tokens written, o=output+thinking)"
)
for m in ["opus55", "sonnet55", "fable51"]:
    for C, n, o in [(50e3, 3e3, 1.5e3), (150e3, 3e3, 1.5e3), (400e3, 3e3, 1.5e3)]:
        t = turn(m, C, n, o)
        s = sum(t.values())
        print(
            f"{m:9} C={C / 1e3:.0f}k  total ${s:.4f}  read {t['read'] / s:5.1%} write {t['write'] / s:5.1%} output {t['out'] / s:5.1%}"
        )
print()
print("== 2. cost of one cache bust vs one warm read of the context")
for m in ["opus55", "sonnet55", "fable51", "sonnet46"]:
    p = P[m]
    for C in [100e3, 300e3]:
        print(
            f"{m:9} C={C / 1e3:.0f}k bust(5m) ${C * p['w5'] / M:.3f} vs read ${C * p['r'] / M:.4f}  ratio {p['w5'] / p['r']:.0f}x (1h {p['w1'] / p['r']:.0f}x)"
        )
print()
print("== 3. per-turn effort switching: thinking saved vs message-cache rewrite")
for m in ["opus55", "sonnet55"]:
    p = P[m]
    for C in [80e3, 150e3]:
        saved = 500 * p["o"] / M  # assume lowering effort saves 500 output/thinking tokens
        cost = C * (p["w5"] - p["r"]) / M
        print(
            f"{m:9} C={C / 1e3:.0f}k save ${saved:.4f} per lowered turn, pay ${cost:.3f} per switch -> {cost / saved:.0f}x loss"
        )
print()
print(
    "== 4. when does a mid-session lossy rewrite (masking dC of C) pay?  need R remaining turns > (C-dC)*w / (dC*r)"
)
for m in ["opus55", "sonnet46", "fable51"]:
    p = P[m]
    for frac in [0.25, 0.5]:
        print(
            f"{m:9} remove {frac:.0%}: R > {(1 - frac) * p['w5'] / (frac * p['r']):.0f} turns (5m lane), {(1 - frac) * p['w1'] / (frac * p['r']):.0f} (1h lane)"
        )
print()
print("== 5. Haiku 5.5 100k cliff: request just above vs just below")
for C in [101e3, 120e3]:
    hi = C * P["haiku55hi"]["r"] / M + 2e3 * P["haiku55hi"]["o"] / M
    lo = 99e3 * P["haiku55lo"]["r"] / M + 2e3 * P["haiku55lo"]["o"] / M
    hiu = C * P["haiku55hi"]["i"] / M + 2e3 * P["haiku55hi"]["o"] / M
    lou = 99e3 * P["haiku55lo"]["i"] / M + 2e3 * P["haiku55lo"]["o"] / M
    print(
        f"C={C / 1e3:.0f}k cached: ${hi:.5f} vs 99k ${lo:.5f} ({hi / lo:.1f}x); uncached ${hiu:.4f} vs ${lou:.4f} ({hiu / lou:.1f}x)"
    )
print(
    "GPT-6.1 Sol 272k cliff (3rd-party rates: <272k in 2/cached .10/out 10; >272k in 4/cached .20/out 15)"
)
for C in [280e3]:
    hi = C * 0.20 / M + 1.5e3 * 15 / M
    lo = 265e3 * 0.10 / M + 1.5e3 * 10 / M
    print(f"  C=280k cached turn ${hi:.4f} vs 265k ${lo:.4f} ({hi / lo:.2f}x)")
print()
print(
    "== 6. model alias modernization (same tokens; newer tokenizer ~1.3x tokens per Anthropic, applied to 4.6->5.5)"
)
for a, b, tk in [("sonnet46", "sonnet55", 1.3), ("haiku45", "haiku55lo", 1.0)]:
    # use turn C=80k n=3k o=1.5k
    ta = sum(turn(a, 80e3, 3e3, 1.5e3).values())
    tb = sum(turn(b, 80e3 * tk, 3e3 * tk, 1.5e3 * tk).values())
    print(f"{a}->{b}: ${ta:.4f} -> ${tb:.4f} per turn = {tb / ta - 1:+.0%}")
# haiku45 uses old tokenizer? 4.5 is pre-4.7 so old tokenizer -> apply 1.3
ta = sum(turn("haiku45", 20e3, 2e3, 300).values())
tb = sum(turn("haiku55lo", 20e3 * 1.3, 2e3 * 1.3, 300 * 1.3).values())
print(
    f"haiku45->haiku55 side call (20k ctx), tokenizer 1.3x: ${ta:.5f} -> ${tb:.5f} = {tb / ta - 1:+.0%}"
)
print()
print("== 7. step routing Opus5.5 -> Haiku5.5 for k mechanical turns then back (C=60k / 150k)")


def opus_only(C, k, n=3e3, o=1.5e3):
    return sum(sum(turn("opus55", C + i * (n + o), n, o).values()) for i in range(k))


def routed(C, k, n=3e3, o=1.5e3):
    tot = 0
    for i in range(k):
        Ci = C + i * (n + o)
        h = "haiku55hi" if Ci > 100e3 else "haiku55lo"
        p = P[h]
        tot += (Ci * p["w5"] if i == 0 else Ci * p["r"]) / M + n * p["w5"] / M + o * p["o"] / M
    # return to opus: write k*(n+o) new tokens at opus write; opus prefix assumed warm (<5min or keepalive ping)
    tot += (
        k * (n + o) * P["opus55"]["w5"] / M + C * P["opus55"]["r"] / M
    )  # + one keepalive-ish read
    return tot


for C in [60e3, 150e3]:
    for k in [1, 3, 6]:
        a = opus_only(C, k)
        b = routed(C, k)
        print(f"C={C / 1e3:.0f}k k={k}: opus ${a:.3f} routed ${b:.3f} ({b / a - 1:+.0%})")
print()
print("== 8. 1h-lane vs keep-alive pings for a gap g (Opus 5.5, C=150k, tokens written so far W=C)")
C = 150e3
p = P["opus55"]
print(
    f" 1h upgrade extra cost for whole session ~ 0.75*base*W = ${(p['w1'] - p['w5']) * C / M:.3f}; one avoided 5m rewrite = ${C * (p['w5'] - p['r']) / M:.3f}"
)
for g in [10, 30, 55]:
    pings = int(g // 4.25)
    print(f" gap {g}min: keepalive {pings} pings = ${pings * C * p['r'] / M:.3f}")
print()
print("== 9. cross-user prefix pooling: shared 30k system+tools prefix, cold session start")
for m in ["opus55", "sonnet55"]:
    p = P[m]
    print(
        f"{m}: per session start saved ${30e3 * (p['w5'] - p['r']) / M:.3f}; 1h-lane ${30e3 * (p['w1'] - p['r']) / M:.3f}"
    )
