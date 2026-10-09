# More ways for Horizon to save money: research and analysis (2026-10-08)

What Horizon already does, where an agent session's money goes at October 2026
prices, and 13 new options: what each one does, the evidence behind it, the
expected saving, how it affects the prompt cache, the risks, and where it would
go in the code. Some come from published work. Others are new and come with
their own cost math. All numbers come from `experiments/cost-model/calc.py`
unless a source is cited.

> **Status and corrections (later on 2026-10-08).** Options 1 (telemetry
> part), 2, 3, 4, 5 (OpenAI Flex) and the keep-alive fix in 7 are implemented
> and off by default; see [the implementation guide](2026-10-08-implementation-guide.md),
> which also describes a new algorithm (Flash Observations). Corrections to
> this report:
> - The per-turn effort warning applies to the top-level `output_config.effort`
>   only. On Fable 5.1, Mythos 5.1, Opus 5.5, Opus 5, Sonnet 5.5 and Haiku 5.5,
>   per-message effort (an effort-only system message, beta) changes effort
>   and keeps the cache, so per-turn effort routing is viable there.
> - Stable tool order (option 1) already exists (`_sort_tools_deterministically`).
> - Modernizing to the 5.5 models must carry thinking and effort over: Haiku
>   and Sonnet 5.5 think by default, and Opus 5.5 always thinks and defaults to
>   `medium`. The shipped mapper does this.

## 1. What Horizon does today

Horizon is a proxy between coding agents (Claude Code, Codex, Copilot, OpenCode,
...) and the providers. Today it saves money in these ways:

| Lever | Module(s) | State |
|---|---|---|
| Tool-output compression (SmartCrusher, Kompress, code/log/diff/search) | `transforms/`, `crates/horizon-core` | On; 26-56% of Codex `exec`, ~13% of Claude Bash |
| Frozen prefix in cache mode (newest turn only) | `cache_control.rs`, handlers | On (default `--mode cache`) |
| Cache aligner (dates and other dynamic text moved out of the prefix) | `transforms/cache_aligner.py` | On |
| CCR (lossy compression you can undo with a retrieve tool) | `ccr/` | On |
| Tool-schema compaction | `proxy/tool_schema_compaction.py` | On (layers 2-3 opt-in) |
| Cache keep-alive (`max_tokens: 0` pre-warm) | `proxy/cache_keeper.py` | Opt-in; -15% / -24% on the resumed turn |
| TTL upgrade to 1h | handlers | Opt-in; measured +16% (do not enable) |
| Cross-turn verbatim dedup, read lifecycle, read maturation | `transforms/` | Exist; maturation changed 0 requests; lifecycle blocked by the frozen prefix |
| Cold-prefix rewrites (rewrite only once the cache has expired) | `transforms/cold_prefix.py` | Exists; triggered by idle time only |
| Thinking compaction | `transforms/thinking_compactor.py` | Flag-gated |
| Output shaper (verbosity) | `proxy/output_shaper.py` | Tested on Codex: no gain |
| Effort routing | `proxy/output_shaper.py` docstring | **Described, not built** |
| Model router (static rules: token count, has tools) | `proxy/model_router.py`, `route_advice.py` | Opt-in, rules only, no learning |
| Exact-match response cache | `proxy/semantic_cache.py` | On, hash-based |

Almost everything so far shrinks the input. That made sense while cache reads
cost 0.1x. Section 2 shows that the 2026 price list makes some other levers
more valuable.

## 2. Where the money goes at 2026 prices

Anthropic's current price list ([pricing](https://platform.claude.com/docs/en/about-claude/pricing)):

| Model | Input | 5m write | 1h write | Cache read | Output |
|---|---|---|---|---|---|
| Fable 5.1 | $10 | $12.50 | $20 | $0.25 (0.025x) | $50 |
| Opus 5.5 | $4 | $5 | $8 | $0.20 (0.05x) | $20 |
| Sonnet 5.5 | $2 | $2.50 | $4 | $0.10 (0.05x) | $10 |
| Sonnet 4.6 | $3 | $3.75 | $6 | $0.30 (0.1x) | $15 |
| Haiku 5.5, prompt <=100k | $0.10 | $0.125 | $0.20 | $0.01 | $0.50 |
| Haiku 5.5, prompt >100k | $0.50 | $0.625 | $1.00 | $0.05 | $2.50 |
| Haiku 4.5 | $1 | $1.25 | $2 | $0.10 | $5 |

Other facts that matter here: Batch is -50% and stacks with caching. Fast mode
is 2x on Opus 5.5. `inference_geo: "us"` is 1.1x. Models from 4.7 on use a
tokenizer that makes about 30% more tokens. Opus and Sonnet 4.6+ have no
long-context surcharge, but Haiku 5.5 has a 5x price cliff at 100k. GPT-6.1 Sol
(third-party listings) costs $2 / $0.10 cached / $10 below 272k input tokens
and about 2x input / 1.5x output for the whole request above it.

**Cost of one warm agent turn** (context *C* read from cache, 3k new tokens
written, 1.5k output and thinking):

| Model | C | Read | Write | Output |
|---|---|---|---|---|
| Opus / Sonnet 5.5 | 50k | 18% | 27% | **55%** |
| Opus / Sonnet 5.5 | 150k | 40% | 20% | 40% |
| Opus / Sonnet 5.5 | 400k | 64% | 12% | 24% |
| Fable 5.1 | 150k | 25% | 25% | **50%** |

**Cost of one cache miss** compared with reading the same context:
25x on the 5-minute lane and 40x on the 1-hour lane (Opus/Sonnet 5.5), 50x and
80x on Fable 5.1, against 12x and 20x on the old 0.1x models. A 300k Opus 5.5
miss costs $1.50; reading the same context costs $0.06.

Three conclusions follow:

1. **Cache misses are now the most expensive event in a session.** Each one
   costs as much as 25-80 warm turns of context reading. Preventing misses
   (options 1, 6, 7, 8) is worth more per token than compressing more.
2. **Output and thinking are 40-55% of a turn** on small and medium contexts.
   Output-side levers deserve attention. But they must not cause cache misses
   (option 1, warning on effort routing).
3. **A lossy rewrite in the middle of a session pays less than before.**
   Masking a fraction *f* of the context costs one rewrite. It pays only if
   more than `(1-f)·w / (f·r)` turns remain: 25 turns to remove half on Opus
   5.5 (12 on Sonnet 4.6), and 75 turns to remove a quarter. So rewrites should
   happen when a miss is already unavoidable (option 6).

For flat-fee subscription users (Claude, ChatGPT), "dollars" means usage-limit
headroom. How each provider counts cache reads against those limits is not
published, so subscription gains need their own check.

---

## 3. The options

Each option has a confidence (**High** = documented provider behaviour plus
simple math; **Medium** = published research, not yet tested on Horizon
traffic; **Bet** = new design that needs an experiment) and an expected saving.

### Option 1. Cache-miss firewall: keep request settings the same for a whole session — High, quick win

**Problem.** According to Anthropic's caching docs, these changes all cause a
cache miss:
- changing `output_config.effort`, thinking mode or `budget_tokens`, or
  `tool_choice` drops the cached **messages**;
- changing `speed` (fast mode), web search or citations drops the cached
  **system prompt and messages**;
- changing any tool definition drops **everything**;
- adding or removing images drops the cached messages.

Clients do change these mid-session: `/effort`, `/fast`, MCP servers
connecting late, the tool list being reordered. Claude Code also puts
`git status` in the system prompt, which costs about 6k written tokens per
session (reported in a [Claude Code issue](https://claudeissues.com/issue/47107-bug-uncachable-system-prompt-caused-by-includegitinstructions-claude-code-disabl)).
"Don't Break the Cache" (arXiv 2601.06007) names changes to the tool list
(dynamic MCP tool discovery) as the most common cause of misses.

**What Horizon can do.** It already keeps beta headers the same for a session
(`HORIZON_BETA_HEADER_STICKY`). Extend that to a session record holding the
model, effort, thinking settings, speed, tool order and server-tool flags:
- **Detect.** Compare each request with the session record. Log a predicted
  miss and its price, `C × (w − r)`. This is cheap telemetry, and it tells the
  dashboard which client setting is costing money.
- **Normalise (off by default, per setting).** Sort tools into a fixed order,
  keeping each tool's bytes unchanged. Add newly appearing tools after the
  existing ones instead of re-sorting. Move `git status` and similar volatile
  system-prompt parts to the end, as the cache aligner already does for dates.
- **Never** silently undo a change the user asked for (effort, speed). Report
  its price instead, and use the change as a coalescing point (option 6).

**Saving.** One avoided miss on a 150k Opus 5.5 context saves $0.72. If
telemetry shows even one miss per session caused by a settings change, this is
the cheapest dollar on the list.

**Built-in design rule: per-turn effort routing loses money.** The planned
effort routing (lower `effort` on mechanical turns) changes `effort` from turn
to turn, and each change drops the message cache. Lowering effort on one turn
saves about 500 thinking tokens ($0.01 on Opus 5.5) but costs a $0.38-$0.72
rewrite. That is a 38-72x loss. Effort, thinking budget and speed may change
only (a) once at session start, or (b) on a request that misses the cache
anyway (option 6). Write this into the module before anyone builds it.

### Option 2. Model modernization: map old model ids to newer, cheaper ones — High, quick win, opt-in per org

**Problem.** Many clients, MCP tools and scripts hard-code older model ids
(Horizon's own code mentions `claude-haiku-4-5` in 10+ places). The newer
models cost less and read from cache at half the rate:

| Mapping | Per-turn change (tokenizer +30% counted) |
|---|---|
| Haiku 4.5 → Haiku 5.5 (side calls, subagents, <100k) | **-87% to -90%** |
| Sonnet 4.6 → Sonnet 5.5 | **-31%** |
| Opus 4.x → Opus 5.5 | about -20% on price, plus 0.05x reads (before the tokenizer effect) |

**Design.** Use the existing `ModelRouter` with a `from_models` rule. Apply it
only to the **first request of a session** (or a session's first turn on that
model), because switching models mid-session is a full miss. Log it in
`routing_stats`. Keep a 5% holdout per org so quality can be compared on real
traffic.

**Risks.** Output and tool-use behaviour change. The +30% tokenizer effect
varies with content. Prompts above 100k on Haiku 5.5 hit the 5x tier (route
those elsewhere, or use option 3). Use the hosted ledger first to find which
old models the traffic actually uses; that bounds the saving before any code is
written.

### Option 3. Price-cliff guard — High, quick win

**Problem.** Two prices are charged on the whole request once a threshold is
crossed. Haiku 5.5 above 100k costs **5.1x** more for a 101k request than for
99k. GPT-6.1 Sol above 272k costs **1.9x** more per cached turn (280k against
265k). Once a growing session crosses the line, every later request pays the
higher price.

**Design.** Before forwarding, compare the projected input size with the
model's threshold. Inside a margin (for example within 8% of the line), run
compression up to a token target instead of a ratio, using the existing
`adaptive_sizer` and target hooks. If the request is still over:
- for a request that misses the cache anyway (subagent spawn, first turn, idle
  resume), apply a stronger lossy level (masking, below);
- otherwise, check the rewrite cost against the remaining turns, as in
  conclusion 3. Crossing a 5x cliff easily pays for one rewrite.

**Codex note.** Codex keeps its conversation state on OpenAI's side and sends
only new items, so the proxy cannot shrink the old history there. On Codex the
guard can only keep new items small, or ask the client to run its own
`/compact`.

**Accounting bug found during this research.** `pricing/counterfactual.py` and
`proxy/cost.py` use one global `200_000` threshold. Haiku 5.5 (100k) and
GPT-6.1 Sol (272k) are therefore priced at the wrong tier on the dashboard and
in the Pro fee. Filed as a separate task.

### Option 4. Fast-mode governor — High, quick win

Opus fast mode costs 2x ($8 / $40 on Opus 5.5), and switching it on or off
drops the system and message caches. Policy:
- strip `speed: "fast"` from headless sessions (`claude -p`, `codex exec`, CI)
  and from subagent and side calls, where nobody is waiting;
- in interactive sessions, keep the mode the session started with (option 1),
  and enforce an optional daily fast-mode budget per user.

The saving is 50% of those requests. It is cheap to build: the handler already
classifies headless sessions and side calls for other features.

### Option 5. Cheaper service tiers for non-interactive traffic — High (OpenAI), Medium (Anthropic)

- **OpenAI Flex:** `service_tier: "flex"` on a normal synchronous request costs
  the same as Batch (about -50%), but answers more slowly and may return 429 when
  capacity is short ([flex guide](https://developers.openai.com/api/docs/guides/flex-processing)).
  Horizon can set it for headless or CI Codex sessions and for the
  `/v1/compress` and LiteLLM gateway traffic. On a 429, retry once without the
  tier. The saving is up to 50% on that traffic, about the size of all current
  compression gains.
- **Anthropic Batch:** -50%, stacks with caching, but runs asynchronously, so
  it cannot serve an agent loop that waits on every step. It can serve eval
  runs, summarisation side jobs, and `horizon learn` or memory-extraction LLM
  calls.
- **`inference_geo: "us"` (1.1x):** report it on the dashboard. Remove it only
  if the org has opted in, because data residency may be a compliance
  requirement.

### Option 6. Miss coalescing: save lossy rewrites for requests that miss anyway — Bet, high value (new)

**Idea.** Every lossy rewrite of history costs one cache miss. Horizon already
waits for misses caused by idle time (`cold_prefix.py`). Extend this to
**every predictable miss**:

| Predicted-miss trigger | Detectable at the proxy? |
|---|---|
| Idle gap > TTL (and keep-alive stopped or never ran) | Yes (exists) |
| Effort / thinking / speed / `tool_choice` change | Yes (option 1) |
| Tool list change (MCP connect or disconnect) | Yes |
| Model change, client `/compact`, `--resume` re-render | Yes (prefix hash breaks) |
| Image added or removed | Yes |

On such a request the cache is about to be rewritten anyway, so every pending
rewrite costs nothing extra in cache terms. Apply all of them in one pass:
- **observation masking** of old tool results (replace with a CCR stub);
- stale and superseded reads (`read_lifecycle`, which the frozen prefix blocks
  today, as experiment 6 found);
- cross-turn dedup over the whole prefix;
- thinking-block drop.

**Evidence that masking is safe.** JetBrains' "The Complexity Trap" (arXiv
2508.21433, NeurIPS 2025 workshop) found that simple observation masking
**halves cost** relative to the raw agent on SWE-bench Verified, with a solve
rate equal to (sometimes slightly above) LLM summarisation. A hybrid saved a
further 7-11%. Anthropic ships the same mechanism as `clear_tool_uses`
context editing.

**Decision rule.** On a predicted miss, the rewrite is free in cache terms;
apply it. On a warm request, rewrite only if `R_expected · ΔC · r > (C − ΔC) · (w − r)`,
where *R* comes from a per-user estimate of turns remaining in the session.

**Measure.** Use the experiment 8 setup (wire persistence, 100 requests) with
injected triggers. Count rewritten bytes, and check that the request after
each trigger reads the new prefix from cache.

### Option 7. Keep-alive as a ski-rental problem, with learned gap lengths — Bet (new)

The keeper (option `HORIZON_CACHE_KEEPALIVE`) stops after a fixed budget,
assuming a fixed 0.5 chance that the user returns. This is the classic
ski-rental problem: each ping is renting, a miss is buying.
- **Learned gaps.** Fit each user's (or tool's) pause-length distribution from
  the ledger (`cache_keeper.jsonl`, `ttl_observations`). Keep pinging while the
  chance the user returns in the next window, times `C·(w − r)`, is greater than
  the cost of one ping, `C·r`.
- **Immediate fix, no learning needed.** `cache_keeper.py:59` uses a fixed
  `READ_MULTIPLIER = 0.1` in `max_pings`. On 0.05x models (Opus and Sonnet 5.5)
  the keeper's own formula gives **12 / 19 pings** (5m / 1h lane) instead of
  5 / 9, and on Fable 5.1 (0.025x) it gives 24 / 39. So the keeper gives up on
  pauses of about 20-50 minutes that are still worth bridging. Read the
  multiplier per model from the catalog, as the ping pricing already does.
- **Lane choice per session.** On Opus 5.5 at 150k, the 1h lane costs about
  $0.45 extra over a session (0.75x on everything written). Keep-alive pings
  for a 30-minute gap cost $0.21. Keep-alive is better for occasional pauses.
  The 1h lane pays only for users with several pauses of 5-60 minutes per
  session, which their own gap history shows.
- **Without a learned model,** the randomized ski-rental bound still caps the
  worst case at e/(e−1) ≈ 1.58x of the best possible spend. The current fixed
  budget has no such bound.

This does not affect quality. The saving is a share of the rewrites still
happening; the 1,332-request Opus session cited in `cache_keeper.py` spent 23%
of its bill on rewrites.

### Option 8. One cache for the whole company — Medium (needs a test), new for Horizon

Anthropic isolates caches **per workspace** (Bedrock and Vertex: per
organization). A company VPS where many employees go through **one company API
key** therefore shares one cache. If every employee's Claude Code sends the
same system prompt and tool prefix (about 20-33k tokens), only the first
session start of the day writes it and every other start reads it:
$0.14 saved per Opus 5.5 session start ($0.23 on the 1h lane).

That only works if the prefixes are byte-identical, and today they are not,
because of per-user parts (cwd, git status, CLAUDE.md, MCP tool sets, client
version). Steps:
1. Hash the prefix up to each cache marker per account, to measure how much
   overlap exists today (telemetry only).
2. Move per-user dynamic sections after the shared part (cache-aligner
   extension) and use option 1's stable tool order.
3. One keep-alive ping can then keep the shared prefix warm for the whole
   company (the keeper's ledger already avoids crediting this).

Does not apply to subscription OAuth traffic (each user is a separate org).
Anthropic says organizations never share caches.

### Option 9. Loop and stall breaker, plus a per-session budget — Medium

"When Agents Do Not Stop" (arXiv 2607.01641) confirmed 68 unbounded-loop bugs
in 47 agent projects. API cost exhaustion appeared in 95.6% of them.
Practitioner reports describe 43-47 identical retries. At the proxy:
- hash each `(tool_name, args)` and its result; after N identical
  failing calls in a window, add a short note to the next tool result (the same
  channel as CCR markers); after M, return a clear error;
- add a per-session `$` ceiling (the per-proxy `--budget` exists; make it per
  session and per key, and show it in `wrap`).

The average saving is small. The worst-case saving is large: it prevents the
one $50+ runaway that destroys an org's trust in the tool.

### Option 10. Start cheap, escalate on evidence (session-level routing) — Medium/Bet

Research since 2024:
- **RouteLLM** (ICLR 2025): 2x+ cheaper at 95% of GPT-4 quality on MT-Bench.
- **FrugalGPT:** cascades, up to 98% cheaper on easy tasks (59-98% across
  datasets).
- **Coding agents in 2026:**
  - **SWE-Router** (arXiv 2607.00053): runs the cheap model for K turns, then a
    value head decides from the partial trajectory whether to escalate. Route-AUC
    0.55 → 0.70 on SWE-bench Verified compared with routing on the issue text
    alone.
  - **Scrouting** (2608.04804): 159 against 158 solves at **about 1/5 of the
    cost per solve**.
  - **TwinRouterBench** (2605.18859) is the warning: on SWE-bench, every
    *step-level* router lost money, because one under-powered step wrecks the
    run.

**Cache-aware analysis (new).** Switching models misses the cache for the new
model. Moving Opus 5.5 → Haiku 5.5 for *k* mechanical turns and back
(keep-alive holding the Opus cache):

| Context | k=1 | k=3 | k=6 |
|---|---|---|---|
| 60k (Haiku cheap tier) | -24% | -47% | -54% |
| 150k (Haiku 5x tier) | **+103%** | -2% | -28% |

So routing per step is worth trying only below the Haiku cliff and for runs of
mechanical turns. One-shot switches cost more than they save. Other open
risks: prior thinking blocks are signed by the original model and may be
rejected or re-billed by another model (needs a wire test), and quality
(TwinRouterBench).

**Recommendation.** Route **per session** (at session start, or one
escalation after K turns, SWE-Router style). This is one miss in total. Extend
`ModelRoute` with trajectory features the proxy already computes (error rate
of tool results, files touched, turn count), and add a holdout arm to
`routing_stats`.

### Option 11. Opus quality at Sonnet prices with the advisor tool — Medium

Anthropic's advisor tool (`advisor_20260301`) lets a Sonnet or Haiku model ask
Opus for advice within one request. Advisor tokens are billed at Opus prices.
Anthropic reports **+2.7 points on SWE-bench Multilingual and -11.9% cost per
task** against Sonnet alone, and Haiku with an Opus advisor at **85% lower
cost** than Sonnet alone ([blog](https://claude.com/blog/the-advisor-strategy)).
Opus 5.5 costs exactly 2x Sonnet 5.5. An opt-in Horizon policy could serve a
session that asks for Opus as "Sonnet 5.5 + Opus 5.5 advisor (`max_uses` N)".
This is set at session start, because a mid-session switch misses the cache.
Needs a paired test like experiments 2 and 6, plus a review of what the
advisor reads (the full transcript, so its input cost grows with the session).

### Option 12. Caches that hit for agents — Medium

The existing exact-hash `SemanticCache` almost never hits on agent turns.
Two narrower targets:
- **Side calls.** Small, repetitive client requests (title generation, topic
  checks, command-prefix classification) often repeat exactly across sessions.
  Key them by `(model, system hash, last user text)`, with a short TTL and only
  for non-tool requests. Measure the hit rate from the ledger first.
- **Plan caching.** "Agentic Plan Caching" (arXiv 2506.14852, NeurIPS 2025)
  caches plan templates taken from finished runs and has a small model adapt
  them: -50% cost, -27% latency, 96.6% of best performance. It fits repeated
  workflows (CI agents, `codex exec` jobs) better than interactive coding.
  Treat it as a research bet.

### Option 13. Tool-definition deferral for clients that lack it — Medium

Anthropic's tool search with `defer_loading` cuts tool-definition tokens by
about 85% on MCP-heavy setups and improved tool selection
(49→74% on an MCP benchmark). Claude Code already does this. Codex, OpenCode,
Cline and LiteLLM-gateway clients may not. Horizon could defer MCP tools behind
a proxy-side search tool, as CCR's retrieve tool works today. Watch the cache:
the tool list must not change between turns (option 1). Load deferred tools by
adding them, never by reordering.

---

## 4. Not recommended (and why)

| Idea | Why not |
|---|---|
| Per-turn effort, thinking-budget or speed routing | Each change drops the message cache: 38-72x loss (option 1) |
| Blanket 1h TTL upgrade | Measured +16%; pick the lane per user instead (option 7) |
| Per-step model routing above 100k context | Miss plus the Haiku tier: +103% at k=1 (option 10) |
| Lossy rewrites on warm requests | Needs 25-150 remaining turns at 0.05x reads; wait for a miss (option 6) |
| Anthropic Batch for interactive agent loops | Asynchronous; the loop waits on every step |
| Output verbosity shaping on Codex | Measured no gain (experiment 2); Claude Code still untested |

## 5. Suggested order

1. **Telemetry first (1-2 days):** predicted-miss detector with its causes
   (option 1), per-model request mix (option 2), requests near price cliffs
   (option 3), headless share (options 4-5), prefix overlap across employees
   (option 8). All logging only; it sizes every other option from real traffic.
2. **Quick wins:** keep-alive read multiplier per model (option 7), model
   modernization (opt-in), cliff guard and pricing threshold fix, fast-mode governor, Flex for headless OpenAI traffic, stable
   tool order, effort-routing design rule.
3. **Inventions with experiments:** miss coalescing with masking (experiment 8
   setup), ski-rental keep-alive (cache-keeper `live_test.py`), shared company
   prefix (two-key test against one workspace).
4. **Research bets:** session-level escalation routing, advisor substitution,
   plan caching.

Measure each one the way `experiments/README.md` already does: a paired control
and treatment proxy, wire snapshots before any change in compressor behaviour,
and the counterfactual priced with the request's own cache mix.

## Sources

- Anthropic pricing: https://platform.claude.com/docs/en/about-claude/pricing
- Anthropic prompt caching (invalidation table, workspace isolation, pre-warm): https://platform.claude.com/docs/en/build-with-claude/prompt-caching
- Anthropic context engineering cookbook (clearing, compaction): https://platform.claude.com/cookbook/tool-use-context-engineering-context-engineering-tools
- Advisor strategy: https://claude.com/blog/the-advisor-strategy
- OpenAI prompt caching: https://developers.openai.com/api/docs/guides/prompt-caching
- OpenAI Flex: https://developers.openai.com/api/docs/guides/flex-processing
- GPT-6.1 Sol pricing (third-party): https://anotherwrapper.com/tools/llm-pricing/gpt-6.1-sol
- GPT long-context billing discussion: https://community.openai.com/t/pricing-by-context-does-context-short-long-context-includes-complete-tokens/1381331/3
- RouteLLM: https://arxiv.org/abs/2406.18665 · https://lmsys.org/blog/2024-07-01-routellm
- FrugalGPT: https://arxiv.org/abs/2305.05176
- SWE-Router: https://arxiv.org/abs/2607.00053
- Scrouting: https://arxiv.org/html/2608.04804
- TwinRouterBench: https://arxiv.org/html/2605.18859v2
- AgentRouter: https://arxiv.org/abs/2609.22951
- The Complexity Trap (observation masking): https://arxiv.org/abs/2508.21433
- ACON: https://arxiv.org/abs/2510.00615
- Don't Break the Cache: https://arxiv.org/abs/2601.06007
- Agentic Plan Caching: https://arxiv.org/abs/2506.14852
- When Agents Do Not Stop: https://arxiv.org/pdf/2607.01641
- SelfBudgeter: https://arxiv.org/abs/2505.11274
- Chain of Draft: https://arxiv.org/abs/2502.18600
- LLMLingua-2: https://arxiv.org/abs/2403.12968
- MeanCache (semantic cache false hits): https://arxiv.org/pdf/2403.02694
- Tool search / defer_loading write-up: https://dev.to/oldeucryptoboi/how-tool-search-defers-tools-to-save-tokens-3ln5
- Claude Code git-status cache issue: https://claudeissues.com/issue/47107-bug-uncachable-system-prompt-caused-by-includegitinstructions-claude-code-disabl
- Gemini caching: https://cloud.google.com/vertex-ai/generative-ai/docs/context-cache/context-cache-overview
- DeepSeek peak pricing (2026): https://www.aipricing.guru/news/deepseek-api-pricing-update-peak-off-peak-august-2026/
