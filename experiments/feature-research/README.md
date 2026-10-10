# Feature research: where coding-agent dollars go (2026-10-07)

Question: can ContextShrink add features that save a $200-300/month coding-agent
user enough to matter (and to justify Pro)? Method: price every model request in
local Claude Code and Codex transcripts with its own model and usage
(`cost_anatomy.py`), replay the biggest sessions under alternative policies, and
compare with published studies. The transcript snapshot was deleted afterwards.

## Where the money goes

Claude Code, 26 sessions, 1,347 requests, $224.67 at API prices:

| Part of the bill | Share |
|---|---|
| Cache reads (re-reading the context every request) | 58.8% |
| Cache writes, 1-hour | 30.3% |
| Output + thinking | 10.4% |
| Fresh input (where compression acts first) | 0.5% |

98% of it is one long Opus session: 1,244 requests, median context 538k tokens,
peak 965k (1M-context model; the client compacted only near the limit). Its
context was roughly 40% thinking blocks, 25-40% tool results, 13-22% tool inputs
(Write/Edit/Bash).

A published Claude Code study with billed costs (Weinberger & Hozez, 2026)
found the same shape: cache writes 44%, cache reads 35%, output 10%, tool
outputs about 3% of cost, and estimates about 5% as the ceiling for compressing
visible tokens. It measured Headroom v0.27.0 (our upstream's ancestor) at +48%
billed cost, and a tool-output compressor that removed 38% of tool tokens at
+6.8% cost.

## Levers tested on the data

| Lever | Result | Verdict |
|---|---|---|
| More tool-output compression | Ceiling ~1-5% of the bill (tests 1-9) | Already near the limit |
| 5-minute instead of 1-hour cache writes | **+29% cost**: 5-60 minute pauses force full rewrites | Only for headless/CI runs (≤11% there) |
| Mask old tool results (K=M=10..50) | Net -0.8% to +1.2%: each pass rewrites the cache | Not worth it alone |
| **Compact earlier (context budget)** | Largest Claude session: -28% at 500k, -39% at 300k, **-43% at 200k** (replay matches the real bill within 0.4%) | **Main lever** for long Claude Code sessions |
| Compact earlier, Codex | Largest Codex session (410 requests, peak 243k): -5% to -8% | Small; Codex already compacts |

Compaction replays charge every compaction a full context read, a 15k-token
summary as output and a fresh cached prefix, but not the quality cost of more
frequent summaries (detail lost, files re-read). "The Complexity Trap"
(Lindenbauer et al., NeurIPS 2025) found masking and summarization each cut
SWE-agent cost by about half with solve rate maintained; AgentDiet (FSE 2026)
reports 21-36% lower cost from trajectory reduction.

## How the product could deliver it

- **Codex:** `wrap` can set `model_auto_compact_token_limit` (documented
  config key). Small gain.
- **Claude Code:** `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE` is undocumented and
  reported as a no-op since v2.1.220 (July 2026), so the proxy would compact:
  Anthropic's server-side compaction (`compact-2026-09-04`, on demand, can keep
  recent turns, can run in the background) returns a summary block; the proxy
  splices it in place of the older history on later requests, the way it already
  replays a frozen prefix. Needs per-session state, the preserved-thinking rules
  for kept turns on Opus/Sonnet 5.5, and tool_use/tool_result pairing.
- **Measuring savings:** unlike compression, this saving is a counterfactual
  (what the uncompacted context would have cost); the dashboard would need a
  replay model like `compaction_threshold()` to show it.

## Paired compaction experiment, round 1 (2026-10-07)

`../compaction-paired/`: Claude Code 2.1.291 on Sonnet 5.5 (1M window), one
11-part task (6 bugs, 4 features, an architecture doc over three large files),
3 pairs; treatment sets `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE=10` (compact at ~100k;
a probe confirmed the override works), control uses the client default.

| Arm | All checks passed | Mean cost | Turns | Peak context | Compactions |
|---|---|---|---|---|---|
| Control | 3/3 | $0.64 | 44 | 91k | 0 |
| Treatment | 3/3 | $0.94 | 45 | 87k | 0.7 |

Paired cost change: +133%, -46%, +152% (mean +80%). The task was too short to
test the lever: runs took 2-3 minutes and 26-40 requests and context peaked at
60-150k, so a compaction (summary request plus a fresh cache write) cost more
than the smaller context saved. Quality was unaffected. Conclusion so far:
forcing early compaction only pays in sessions that are already long; it costs
money in short ones, so any product version must trigger only on large contexts.

## Round 2: levers with large, measurable savings (2026-10-08)

| Lever | Evidence | Saving |
|---|---|---|
| **Model routing: Opus -> Sonnet 5.5** | Same 11-part task, 3 runs each, all 18 checks passed in every run: Opus alone $1.75 mean (1.61-1.98), Sonnet alone $0.64 (0.45-0.97). Sonnet 5.5's rates are exactly half of Opus 5.5's on every token class; the 1,332-request real session repriced at Sonnet rates: $244.56 -> $123.09 | **-50% to -63%** |
| Sonnet with an Opus advisor (`--advisor opus`, `advisorModel`) | Same task: $1.67 mean; when Sonnet consults, each call sends the whole context to Opus at full input price ($0.81-0.85 per run); a probe showed $0.16 per call at 37k context | ~-5% vs Opus; not worth it at large contexts |
| Context budget (`autoCompactWindow`, Claude Code 2.1.288+, 100k-1M) | Replay of the real session (see above); short tasks pay more | -28% to -43% in long sessions only |
| Clear prior-turn thinking (`clear_thinking_20251015`) | Real session: earlier turns' thinking averaged 12% of context; net after cache rewrites | +2.5% |
| OpenAI flex (`service_tier: "flex"`) | Batch rates (half price), beta, limited models, 429s when capacity is short | -50% on background runs only |

## Round 3: cache lifecycle waste, model unchanged (2026-10-08)

Routing was rejected as a headline: users choose Opus for quality, and a
per-request switch costs a full context rewrite (caches and thinking blocks are
per model). `waste_audit.py` on the real 1,332-request Opus session ($246):

| Waste | Amount |
|---|---|
| Cache rewrites after the prefix expired or changed | 13 rewrites, $56.33 (23% of the bill; $56 of the $68 spent on cache writes) |
| ... after a 1-2 h idle gap | 6 rewrites, $31.10 |
| ... after more than 8 h | 4 rewrites, $19.04 |
| Repeated lines in tool output | 9% of tool-output characters, $2.18 of later re-reads |
| Codex (938 requests, $18.40): cold re-sends | $0.97 (5%) |

Levers, model and conversation unchanged:

| Lever | Saving on this session |
|---|---|
| Keep-alive: re-read the prefix every 55 min while idle (reads refresh the TTL at no extra cost; `max_tokens: 0` pre-warm bills reads only), 2-24 h window, pings after the session ends counted as waste | $28-33 (12-13%) |
| Shrink the context at restarts after >2 h idle (the rewrite is paid anyway) by 30-50% | $29-48 |
| One-hour cache entries for API-key users (Claude Code defaults them to 5 min; this session repriced at 5 min: $325.93, +32%) | $79.51 (24% of the API-key bill) |

For an API-key user doing this session: $326 by default, about $170-190 with all
three (-42% to -48%), on the same Opus model. Subscription users already get
one-hour entries and pay no per-token bill; for them the gain is plan headroom.
Unverified: pre-warm with adaptive thinking (only `thinking.type: "enabled"` is
documented as rejected), and one session, with long idle gaps, is the sample.

Product shape (routing): route by default to the cheaper model of the same family and
escalate on demand (sticky per session: Sonnet 5.5 cannot read Opus 5.5 thinking
blocks, so a mid-session switch drops them and restarts the cache). The saving is
exact and auditable per request: actual tokens priced at the requested model
minus the served model. Combine with a context budget for long sessions.
Quality risk: one moderate task here; harder tasks need their own paired runs
(`compaction-paired/routing.py`).

## Sources

- [Token Reduction Is Not Cost Reduction](https://arxiv.org/abs/2607.12161) (arXiv 2607.12161)
- [The Complexity Trap](https://arxiv.org/abs/2508.21433) (arXiv 2508.21433)
- [AgentDiet summary](https://getnadir.com/blog/trajectory-reduction-agent-token-cost/) (arXiv 2509.23586)
- [SWE-Pruner](https://www.arxiv.org/pdf/2601.16746), [CoACT](https://arxiv.org/html/2607.02911v1)
- [Anthropic compaction](https://platform.claude.com/docs/en/build-with-claude/compaction), [context editing](https://platform.claude.com/docs/en/build-with-claude/context-editing)
- [Codex sample config](https://developers.openai.com/codex/config-sample), [Codex compaction tuning](https://codex.danielvaughan.com/2026/04/16/codex-cli-context-compaction-tuning-long-sessions/)
- [CLAUDE_AUTOCOMPACT_PCT_OVERRIDE no-op report](https://claudeissues.com/issue/82761-claude-autocompact-pct-override-silently-stopped-taking-effect-after-2026-07-14)
