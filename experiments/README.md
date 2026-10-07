# Experiments: compression, cost and hosting

What has been tested, how, what came out, and what is still pending. Dates are
2026-10-06 unless stated. All runs used throwaway projects and isolated proxies;
the hosted proxy and the user's own tool settings were never involved.

## How the dashboard prices "saved" dollars

Every proxied request is priced on its own, with **its own model's** rates, so
mixing models (Sonnet, GPT-6.1, Luna, ...) in one account is fine:

1. The proxy measures the tokens it removed from the request
   (`horizon/proxy/outcome.py`). Only tokens removed **for the first time in
   that conversation** count (`conversation_savings.novel`), so a removed tool
   result is not counted again on every later turn. This applies only to OpenAI
   Responses traffic that carries the whole transcript. Codex's incremental
   frames are already per-request; Claude requests are not deduplicated at all
   (see caveats).
2. It prices them with `estimate_request_savings_usd`
   (`horizon/proxy/savings_tracker.py`, model in
   `horizon/pricing/counterfactual.py`):
   - rates come from LiteLLM's per-model catalog bundled with the proxy; a model
     missing from the catalog falls back to a flat $3 per million tokens
     (`pricing_basis` = `list`);
   - the price is **cache-aware**: compressed tokens come from the newest part of
     the request, which is never a cache read, so they are priced at the
     request's own uncached / cache-write mix; tool-schema savings sit in the
     cacheable prefix and are priced as cache reads first;
   - when the provider reports no cache breakdown, list price is used and labelled.
3. Each request becomes one event (`horizon/proxy/account_analytics.py`) with
   `savings_usd` = compression + tool-schema savings, `cost_usd`, the model and
   the `pricing_basis`. The dashboard and the Pro savings fee add up
   `savings_usd` over the period (`api/billing.py`).

Not included in `savings_usd`: output shaping, and the provider's own cache
discount (which compression does not cause).

Caveats worth knowing:

- **Wrong in both directions for long sessions (test 7).** A removed token stays
  out of every later request, where it would have been a cheap cache read.
  - **Codex:** each removal is counted once, so the repeat saving is missed
    (undercount, 1.5-2.3x).
  - **Claude:** the "once per conversation" rule never applies (no conversation
    key on that path). Every request reports its running saving, including about
    576 tokens of tool-definition trimming, all priced at the cache-write rate
    instead of the cache-read rate (overcount, 6-19x).
- **Catalog lag.** A brand-new model not yet in the bundled catalog is priced at
  the $3/M fallback until the proxy image is rebuilt with a newer catalog.
- **Subscriptions.** ChatGPT / Claude subscription users pay a flat fee, so their
  "savings" are API-equivalent dollars, not money back; the Pro fee (5% of
  savings above $20) is computed on those notional dollars. A pricing decision to
  revisit before launch.

## Completed tests

### 1. Compression speed: Raspberry Pi 5 vs PC

- **Question:** does the Pi's CPU lose compression?
- **Method:** the same 9 tool outputs (72,163 tokens: logs, git log, source, JSON,
  docs, search results) through Horizon's compression pipeline on the Pi 5 and on
  the PC (Ryzen 7 9800X3D). Script: `bench_compress.py`.
- **Result:** PC saved 32% in 11 s; Pi saved 20% in 91 s. On the Pi, Kompress (the
  ML text compressor) hit its time budget on the two largest inputs and passed
  them through; 5 of 9 inputs took over 5 s (Codex's WebSocket limit).
- **Live follow-up (Codex through a PC proxy, 22 requests):** PC compressed every
  Codex frame (0 failures, 28% of attempted tokens, slowest 3.2 s); the Pi had
  failed 7 of 18 at the 5 s limit.
- **Decision:** keep the Pi for now; host the proxy on the owner's PC before
  launch, later a dedicated mini PC.

### 2. Output shaper on Codex (paired)

- **Question:** does asking the model for shorter replies (verbosity level 2) cut cost?
- **Method:** 6 tasks x 2 repeats, each run once shaped and once unshaped through
  two otherwise identical proxies (`HORIZON_OUTPUT_HOLDOUT=0` / `1`), gpt-6.1-sol,
  graded by tests + hidden checks. Scripts: `codex-paired/`, `output-shaper/`.
- **Result (24 runs):** both arms 12/12 tasks passed; cost -1.9% (noise); output
  tokens -5.2%, but higher in 9 of 12 pairs; final answers 21% longer. Output was
  only ~6% of cost; input dominated.
- **Decision:** do not enable the shaper for Codex. "Effort routing" (lowering
  reasoning effort on routine turns) is described in the code but not implemented.

### 3. What fills Codex's context (local transcripts)

- **Method:** 26 local Codex sessions, each tool result weighted by how many later
  requests re-send it. Scripts: `codex-reads/exec_gate.py`, `exec_compress.py`.
- **Result:** Codex's code-mode `exec` tool is ~90% of re-sent context (results
  74%, script inputs 16%); file reads via shell ~1%. The proxy's read protection
  offers 87% of it for compression, and the pipeline removes 26%.

### 4. Codex wire snapshots (what the proxy really forwards)

- **Method:** 3 tasks through a proxy with `HORIZON_CODEX_WIRE_DEBUG=1`, comparing
  each request as received with the request as forwarded (45 frame pairs).
- **Result:** `exec` results shrank 114 KB -> 50.5 KB (-56%); Codex's own
  instructions (130 KB) untouched; tool definitions 135 -> 131 KB. 42 of 45 frames
  were incremental (only new items sent), so a result compressed once stays
  compressed. Each session carries ~20k tokens of fixed instructions + tool
  definitions, which dominates short sessions.
- **Conclusion:** Codex compression works; low headline percentages come from
  the fixed overhead in short sessions and from Pi timeouts, not from `exec`.
  (Two earlier claims made from reading code alone were wrong; verify with wire
  snapshots before changing compressor behaviour.)

### 5. What fills Claude Code's context (local transcripts)

- **Method:** 26 local Claude Code sessions, same weighting; compression measured
  with the pipeline. Script: `claude-code/context_mix.py`; maturation simulation
  via `horizon.audit.maturation.simulate_maturation`.
- **Result:** Bash 62% of re-sent tool output (13% compressed), Read 25% (0%:
  Read, Grep, Edit, Write, WebSearch, WebFetch are excluded from compression by
  default so edits keep exact file text), 8% overall. Simulation: 58.5% of big
  reads are never touched again; at quiesce 5, 13% of edits hit a matured file.
  (The data includes one very long development session, which inflates Bash.)

### 6. Read maturation on Claude Code (paired)

- **Method:** a combined four-part task (fix bugs planted in copies of `shlex.py`
  and `fnmatch.py`, add `textwrap.wrap_paragraphs`, extend
  `configparser.getboolean`) in one session, 4 runs per arm, Sonnet, control
  proxy vs `--read-maturation` (`HORIZON_ROLLOUT_CHANNEL=beta`). Scripts:
  `claude-code/read-maturation/`.
- **Result:** both arms 4/4 passed; maturation changed **0** requests. Claude Code
  reads in small slices after Grep/Glob (234 and 337 characters in the captured
  session), below maturation's 2 KB minimum. The cost gap ($0.288 vs $0.254) is
  prompt-cache noise.
- **Also found:** in cache mode the proxy freezes everything but the newest
  message, so the default **read lifecycle** (compress reads of files that were
  later edited) never applies: the log shows "skipping N stale/superseded
  replacements in frozen prefix" on most requests.
- **Decision:** do not enable read maturation.

### 7. Uncounted repeat savings (offline, Step 1)

- **Method:** the 32 saved experiment runs, plus local compression replay of
  26 Codex and 26 Claude transcript files. Use historical cache-read prices;
  deduplicate model responses and stop transcript retention at recorded
  compactions. No paid agent runs or provider calls.
- **Result:** Codex's 24 runs counted $0.281805; the estimated later-read
  component adds $0.109527 (+38.9%), assuming removals persist. Transcript
  repeat components are 1.13x the modeled first-removal value for Codex and
  13.71x for Claude. Transcript dollars are counterfactual benchmark values,
  not recorded account savings.
- **Accounting finding:** Claude's saved wire counts include recurring schema
  reductions and some retained payload reductions already counted on later
  requests. Weighting those counts as fresh removals would double-count them.
  The blanket novel-only assumption above needs this caveat.
- **Pricing finding (`repeat-savings/pricing_gap.py`):** these figures re-price
  each logged request with the proxy's own estimator, so counted values match the
  logged `cost_effective_usd` to the cent. Retained and first removals are then
  priced separately, the way the counterfactual model intends.
  - **Claude is overcounted 6.4x** ($0.2212 counted vs $0.0345 actual).
  - **95% of that is tool-definition trimming** (~576 tokens per request,
    `handlers/anthropic.py:2922`). Its saving lands in `tokens_saved`, so it is
    priced as live-zone content at the one-hour write rate ($4/M) on every
    request. Tool definitions are the cached prefix ($0.20/M). The correctly
    priced `tool_schema` layer is fed only by deferral tags.
  - Retained payload is overpriced the same way ($0.0088 counted vs $0.0004).
  - **Codex is undercounted:** actual is 1.5x counted in the logs (0.67x) and
    2.3x in long transcripts (0.43x, median 0.55x). This is the same direction
    as Step 1's +38.9%. The difference: here, removals still kept out of a
    request with no cache hit are priced at that request's write/uncached rate,
    not the read rate.
  - Long Claude transcripts: 19.4x overall, median 9.2x per session, if the ledger
    behaves as in the logs.
- **Reconciliation of the two analyses:** the $0.2212 comparison includes one
  pilot alongside the 8 formal Claude sessions. Formal sessions alone record
  $0.211957 versus $0.031862 when the first schema trim is priced as fresh
  (6.65x). All 8 formal first requests already report cache reads; using the
  existing prefix allocation rule throughout gives $0.014352. These are
  historical pricing scenarios with inferred components, not measured customer
  bills. On the deduplicated transcript snapshot, the corresponding ratios are
  18.71x for Claude and 0.47x for Codex. Pricing later Codex requests with their
  observed cache mix gives +48.0%, compared with the read-only +38.9% scenario.
  See [the pricing correction](repeat-savings/PRICING_REVIEW.md) for assumptions,
  per-request reconciliation and the required accounting changes.
- **Decision:** the gap warrants the persistence check in Step 2 before changing
  savings accounting or fees. No production or dashboard changes made. Fix the
  Claude tool-schema pricing before any real Pro fee is charged: it applies to
  every Claude Code request on the hosted proxy today.
- **Report, per-session results and reproducible script:**
  [repeat-savings/REPORT.md](repeat-savings/REPORT.md).

### 8. Persistence on the forwarded wire (Step 2)

- **Method:** 100 increasing full-history requests through an isolated real
  local proxy, followed by 4 boundary checks. Capture actual inbound and
  serialized outbound bytes. A local provider stub supplies synthetic cache
  usage; the normal proxy pipeline and trackers run unchanged.
- **Result:** 99 tool results compressed; 4,851 later appearances kept exactly
  the same forwarded hashes; 0 restorations during append-only turns. Editing
  earlier history restored 98 unchanged older results. Compaction retired the
  old results; a process restart and tracker expiry each restored another result.
- **Accounting finding:** the Claude ledger summed 8,679,708 token occurrences,
  including 173,579 first removals and 8,506,129 retained occurrences. Retained
  reductions are already counted on this path. A blanket second savings term
  would double-count them; first separate and price the components correctly.
- **Live limitation:** the real Claude Code attempt completed only 2 provider
  requests before HTTP 429s, with the five-hour window reported at 99% usage.
  The controlled check establishes wire persistence, not real provider cache
  economics or a completed 100-request live session. Billing is unchanged.
- **Report and reproducible scripts:**
  [repeat-savings/persistence/REPORT.md](repeat-savings/persistence/REPORT.md).

### 9. Upstream headroom v0.40.0 vs Horizon (2026-10-07)

- **Question:** does the latest upstream release (our fork is headroom 0.39.1)
  compress or save more?
- **Method:** six upstream fixes ported (#3810 retrieval tool injected on the
  first request, #3880 record-bearing JSON kept out of Kompress, #3938 one
  compression deadline per Codex request, #3932 quarantine at half the pool,
  #3881 code fallback judged in tokens, #3770 SmartCrusher recursion). The same
  9 benchmark payloads and a frozen copy of 26 Codex + 29 Claude Code
  transcripts compressed by three builds: ours before the ports, ours after,
  headroom-ai 0.40.0 from PyPI. Scripts: `upstream-compare/`.
- **Result:**

  | Build | Benchmark | Codex output removed | Claude output removed |
  |---|---|---|---|
  | Ours before ports | 31.8% | 39.4% | 12.8% |
  | Ours with ports | 31.8% | 41.5% (10% faster) | 12.8% |
  | headroom 0.40.0 | 26.6% | 39.2% | 9.5% |

  0.40.0 compresses less: its Kompress keeps line boundaries (#3119), e.g. a
  passing pytest log goes 2,894 -> 2,822 tokens there vs 158 in ours.
- **Cache rewrite (#3810):** before the port, the first compression in a Claude
  Code session added the retrieval tool to a warm prefix and Anthropic re-wrote
  the whole cache. In the frozen transcripts 3 sessions hit it (148k tokens,
  $0.56, about a fifth of all Claude savings in the set); in production it was a
  single 60k-token rewrite worth 12x that session's real savings.
- **Decision:** keep our Kompress behaviour; ship the six ports. Revisit #3119
  only with a task-success test, since it trades about a quarter of Claude
  savings for line fidelity.

## Pending tests

| Test | Why | Status |
|---|---|---|
| macOS app on the owner's Mac | Sign-in, Keychain, Terminal launch, Homebrew/nvm PATH, menu-bar icon | Waiting for the Mac's update to macOS 12+ |
| Linux on real desktops | Ubuntu 24.04 GNOME and Fedora KDE VMs: tray, keyring, terminal, `.rpm`, AppImage | Not started (WSL covered `.deb` only) |
| Output shaper on Claude Code | Claude's replies are wordier than Codex's; same paired method as test 2 | Not started |
| Stale reads in the frozen prefix | Let read lifecycle replace reads of already-edited files even when cached; pays only if the read is large and the session long | Needs a code change + paired test |
| Long real sessions | Confirm that tool-output savings (26-56% on Codex `exec`) show in total cost of long sessions | Not started |
| Proxy host before launch | Same speed/live checks as test 1 on the PC host, then the mini PC | Before launch |
| Savings pricing for long sessions | Separate new and retained removals; validate persistence and cache pricing | Step 1 complete; controlled Step 2 passed; full live check blocked by 429s |

### Planned: uncounted repeat savings

**Step 1 review:** the paragraph below is a persistence hypothesis, not a
validated statement about every provider's counted savings. The saved Claude
logs already count some retained reductions repeatedly. The measured historical
fresh/write-to-read rate ratios are about 22.5 for Codex and 20 for Claude's
one-hour writes, rather than the illustrative 12-turn crossover below. See test
7 and its report before using this hypothesis for billing.

A token removed at request *i* stays out of every later request in the same
session, because cache mode replays the forwarded (compressed) prefix byte for
byte. Codex over WebSocket gets the same effect: OpenAI keeps the compressed
context on its side. Only request *i* is priced, at the uncached/write rate.
Each later request saves the same tokens again at the cache-read rate (about
0.1x), and none of that is counted. With a 1.25x cache write, the uncounted part
passes the counted part after about 12 later requests. A long Claude Code session
has hundreds.

1. **Estimate from data we already have (free).** For each session, add up
   `removed_i x later_requests_i x cache-read rate` and compare it with the
   counted `savings_usd`. Inputs:
   - the 8 Claude and 24 Codex experiment runs: requests.jsonl, grouped by run
     time window;
   - the 26 + 26 real transcripts, using the re-send weighting that
     `context_mix.py` and `exec_compress.py` already apply.
2. **Check that the removals really persist (cheap).** Run one long session
   (about 100 requests) through a local proxy and record each request's size as
   the client sent it and as it was forwarded. Every removed tool result should
   stay out of later forwarded requests. Count where it comes back: a client
   compaction, an edited earlier message, a proxy restart or a stale session
   tracker. Codex already has `HORIZON_CODEX_WIRE_DEBUG`; Claude needs a small
   size log in `debug_proxy.py`.
3. **Skip a paired cost A/B.** Tests 2 and 6 showed that agent path noise is
   larger than this effect.

If the gap is large, add a second term per request: tokens still kept out of
this request x its cache-read rate, measured on the request rather than assumed.
Show it separately ("new removals" vs "kept out of later turns"). Do not go back
to summing cumulative `tokens_saved` at full price, which
`conversation_savings.py` was written to stop. Raising measured savings raises
the Pro fee, so the dashboard must show users how both parts were worked out.

## Re-running

- Proxies for experiments run locally from the repo `.venv` with
  `PYTHONPATH=<repo>`, `HORIZON_DETECT_BACKEND=rust` and `--code-aware`, on ports
  1880x / 1881x, each with its own `HORIZON_WORKSPACE_DIR` and `--log-file`.
- `codex-paired/run.py <reps>` expects arms on 18801 (treatment) / 18802 (control);
  `claude-code/read-maturation/run.py <reps> combined` expects 18811 (control) /
  18812 (treatment); run `make_template.py` first for the Claude test.
- Runs launch tools non-interactively (`codex exec`, `claude -p`) and use the
  owner's Codex / Claude allowance; outputs go to `runs/` and `results.jsonl`
  (git-ignored).
