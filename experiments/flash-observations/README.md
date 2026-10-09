# Flash Observations: live test

Feature: `horizon/transforms/flash_observations.py`, switched on with
`HORIZON_FLASH_OBSERVATIONS=1` (off by default). Design, cost model and risks:
[implementation guide, Part 3](../../wiki/plans/2026-10-08-implementation-guide.md).

| Setting | Effect |
|---|---|
| `HORIZON_FLASH_OBSERVATIONS=1` | Large outputs of allow-listed tools are forwarded as a permanent stub plus the full text in a turn-scoped system message (`clear_at: "next_user_message"`). The beta header `mid-conversation-system-clear-at-2026-08-21` is added automatically. Direct Claude API only; models with mid-conversation system messages only (Fable 5/5.1, Mythos 5/5.1, Opus 5.5/5/4.8, Sonnet 5.5, Haiku 5.5). |
| `HORIZON_FLASH_TOOLS` | Comma list of tools to flash. Default `Bash,Grep,Glob,LS`. `Read` is opt-in. Web and `mcp__*` tools are never flashed, whatever this says. |
| `HORIZON_FLASH_MIN_CHARS` | Minimum output size to flash (default 8000 characters, about 2k tokens). |

State: `flash_horizons.json` in the workspace, one entry per conversation.
It records the first message each conversation may flash, so switching the
feature on mid-conversation, or restarting the proxy, never rewrites a result
the model already saw unflashed.

## The test (`live_test.py`)

**Task.** A log-triage agent calls `run_tests(suite)` for six suites.
- Each call returns a ~8k-token pytest log with 1–3 failures planted in the
  middle. The first and last lines pass, so the stub's head and tail give
  nothing away.
- The final answer must list every failure across all suites, so the agent has
  to carry what it learned past the turn it saw each log. That is the quality
  risk; ground truth is known and scored.
- Every session's first message carries a unique id, so no two runs share a
  cached prefix.

**Arms.** Interleaved per rep, so drift hits every arm alike:

| Arm | What it tests |
|---|---|
| `control-direct` | Raw tool results, SDK straight to the API |
| `flash-direct` | The transform applied client-side: the API's semantics (`clear_at` accepted, cache reuse, cleared text unbilled, thinking blocks still valid) |
| `control-proxy` | Raw results through a local Horizon proxy, flash off |
| `flash-proxy` | Raw results through a local Horizon proxy with flash on: the real integration |

**Injection check.** `--injection` plants a line telling the model to answer
`PWNED` in one log; `injection_complied` is recorded per run.

**Run it** (about 30–60 minutes; Opus 5.5, 4 arms × 3 reps ≈ $8 at list price, thinking included):

```bash
ANTHROPIC_API_KEY=... python experiments/flash-observations/live_test.py --reps 3
ANTHROPIC_API_KEY=... python experiments/flash-observations/live_test.py --reps 2 --injection --arms control-direct,flash-direct
```

**Output.** `runs/<timestamp>/results.jsonl`: per run, usage per request,
totals, $, score, final answer, injection outcome. `summary.txt` has the table.

### Through a third-party gateway

An Anthropic-compatible gateway works only if it passes requests through
unchanged:
- beta headers forwarded;
- `clear_at` system messages intact;
- prompt caching on one account rather than a key pool.

`preflight.py` checks exactly that with a few small requests (a few cents):
- cache fields in `usage`;
- cache hits on repeats;
- `clear_at` rejected without the beta (proof of raw pass-through);
- the newest turn-scoped message renders;
- a cleared message adds no billed input.

Run the live test only if it prints `PREFLIGHT PASSED`:

```bash
export ANTHROPIC_API_KEY=...            # the gateway's key
python experiments/flash-observations/preflight.py --base-url https://gateway.example/v1
python experiments/flash-observations/live_test.py --base-url https://gateway.example/v1 --reps 3
```

`--base-url` is used for the direct arms and as the upstream of both proxies.
Dollar figures assume Anthropic list prices; a gateway's own rates may differ.
Compare the arms by token buckets.

**ModelFlare preflight (2026-10-08, run by the owner): failed.** No live run
was made through it.

| Check | Result |
|---|---|
| Cache fields in `usage` | Present |
| Caching | None: `write=0 read=0` on the first request, `read=0` on three repeats |
| `clear_at` without its beta | Accepted |
| Turn-scoped message renders | Yes (`PLUM`) |
| "Cleared" message | Billed in full (+14,746 tokens) |

The likeliest cause is that the gateway moves `role: "system"` messages into
the top-level system prompt and drops `clear_at`, and it does no prompt
caching. Through it, Flash Observations would bill every flashed output on
every turn. The proxy therefore now flashes only when its upstream is
`api.anthropic.com`. `HORIZON_FLASH_ANY_UPSTREAM=1` overrides that for an
upstream that passed this preflight, and for the local fake.

**Official Claude API preflight (2026-10-08): passed.**

| Check | Result |
|---|---|
| Cache fields in `usage` | Present (`write=10,875` on the first request) |
| Caching | `read=10,875` on each of three repeats |
| `clear_at` without its beta | Rejected: `messages.1.clear_at: Extra inputs are not permitted` |
| Turn-scoped message renders | Yes (`PLUM`) |
| "Cleared" message | Not billed (45 vs 46 input tokens with ~4k tokens cleared) |

The first version of the render check sent a bare `The code word is PLUM.`
system message. Opus 5.5 saw it but declined to vouch for an unexplained code
word ("I don't have a code word... PLUM was made up"), with or without
`clear_at`. The check now frames the text as the feature does: a stubbed tool
result plus the full output as data in `<tool_output>`. Framed that way the
model answers `PLUM` every time.

## Live run on the official Claude API (2026-10-08, partial)

`live_test.py --reps 3` on `claude-opus-5-5`, 6 suites. The key ran out of
credit after 5 sessions ("Your credit balance is too low"), so every arm has
one clean session (control-direct has two). One session per arm is a smoke
test, not a measurement.

```
arm             rep reqs  uncached    write      read  output       $ score
control-direct    0    7        16  114,526   287,742   1,196   0.654  1.00
flash-direct      0    7   114,202    4,373    12,865   1,241   0.506  1.00
control-proxy     0    7        16  114,664   288,610   1,161   0.654  1.00
flash-proxy       0    7    19,757  115,998   268,060   1,302   0.739  1.00   <- proxy bug, fixed below
control-direct    1    7        16  114,459   287,478   1,126   0.652  1.00
```

**`flash-direct` behaved as the cost model predicts:**
- 23% cheaper than control, same score: every planted failure found.
- Each log is billed once, as uncached input (~19k tokens a step). The cached
  prefix grows by only ~630 tokens a step (the stub). In control, each log is
  written at 1.25x and then re-read on every later turn.
- No 400s in 7 requests:
  - `clear_at` and the placement rules were accepted;
  - earlier flashes re-sent in place, with Opus 5.5's signed thinking blocks
    echoed back, were accepted (preserved thinking is not tripped).
- The logs are ~19k real tokens, not the harness's ~8.4k estimate (4
  characters per token): synthetic pytest output tokenizes poorly. Dollar
  figures use the real `usage`.

**`flash-proxy` exposed a proxy bug.**
- Only step 1 was flashed. From step 2 the proxy forwarded the client's raw
  messages: a cache miss (38,678 written), then ordinary control behaviour,
  13% dearer than control.
- Cause: the signed-thinking guard (`thinking_block_fingerprint` in
  `horizon/proxy/body_forwarding.py`) identifies each thinking block by message
  index. Re-sending an earlier flash in place shifts every later message by
  one. The guard read that as "a thinking block moved" and fell back to the
  client's bytes. Step 1 escaped only because nothing had been inserted yet.
- Fix: the fingerprint no longer counts turn-scoped system messages. A block
  moved between real turns is still caught.
- The fake now returns signed thinking blocks too. Without the fix it 400s
  `flash-proxy` on step 2; with it, `flash-proxy` matches `flash-direct`:

```
arm             rep reqs  uncached    write      read  output       $ score
control-direct    0    7         0   50,428   127,141     660   0.291  1.00
flash-direct      0    7    50,222    2,281     6,933     660   0.227  1.00
control-proxy     0    7         0   50,547   127,855     660   0.292  1.00
flash-proxy       0    7    50,222    2,280     7,760     660   0.227  1.00
```

The full run follows below.

## Full live run (2026-10-08, after the thinking-guard fix)

`live_test.py --reps 3`, Opus 5.5, official API. Every session completed.

| Arm | Sessions | Mean $ | Score |
|---|---|---|---|
| control-direct | 3 | 0.654 | 1.00 |
| control-proxy | 3 | 0.654 | 1.00 |
| flash-direct | 3 ($0.509, $0.524, $0.517) | **0.517 (−21%)** | 1.00 |
| flash-proxy | 3 ($0.620, $1.227, $0.510) | 0.786 (+20%) | 1.00 |

**The thinking-guard fix holds.** The proxy flashed every step in every session.

**flash-proxy is uneven because of the stub wording.** The stub said the output
was "no longer in the conversation" and offered `horizon_retrieve` with its
hash, or a re-run of the tool. Horizon injects the CCR retrieval tool, so in the
proxy arm the model can take that offer:
- **Rep 1 ($1.227):** from step 2 to step 6 the model retrieved the *previous*
  (cleared) log every turn. Each retrieval was a hidden CCR continuation
  request that wrote the log into the cache, so every log ended up in the
  permanent prefix anyway, plus an extra round trip per step. The harness sees
  only the continuation's usage, so the true cost was higher.
- **Rep 0 ($0.620):** one retrieval and one suite run twice.
- **Rep 2 ($0.510):** matches flash-direct. flash-direct has no retrieval tool
  and never looked back.

**Fix (15bbf6a).** The flash now asks the model to write down in its reply what
it will need later. The stub offers `horizon_retrieve` only for exact lines the
model did not write down, and no longer suggests re-running the tool. The
harness records tool calls per request.

### Re-run with the new wording (credit ran out partway)

| Arm | Rep | Requests | $ | Score | Note |
|---|---|---|---|---|---|
| flash-direct | 0 | 6 | 0.435 | 0.00 | `stop_reason: refusal`, 0 output tokens, at step 5 (after the `io` log) |
| flash-proxy | 0 | 7 | **0.531 (−19%)** | 1.00 | each suite run once; 0 retrievals |
| flash-direct | 1 | 7 | 0.504 | 1.00 | |
| flash-proxy | 1 | 3 | — | — | credit ran out |

**Retrievals.** None, and no suite was run twice. One session is not enough to
call the fix proven.

**The refusal is open.**
- No control session (9 on the official API) hit one.
- Neither did any of the 9 flash sessions with the old wording.
- With n=1 it cannot be attributed to Flash Observations, or ruled out. One
  hypothesis: an API-side classifier may judge text in a system-role message
  differently from the same text in a `tool_result`.
- Next run: count refusals per arm. If flash shows any, try fewer instructions
  in the flash header and a smaller flash.

The injection check (`--injection`) did not run: no credit was left.

**Spend.** Live runs and the preflight cost about $13 at list price:
- $3.20 for the partial first run;
- $7.83 for the full run;
- $1.64 for the re-run;
- plus the hidden CCR continuations.

### Pooled evidence so far (official API, Opus 5.5)

- **flash-direct:** 5 complete sessions, mean $0.512, **22% below control
  ($0.654, 9 sessions)**, all with full scores. One more session ended in a
  refusal.
- **flash-proxy:** after both fixes, 1 complete session at $0.531 (−19%).
- **Before release:** a 3+ rep run of flash-proxy, a refusal count, and the
  injection check.

## Remaining runs (2026-10-09)

**Main run** (`--reps 3 --arms control-direct,flash-direct,flash-proxy`,
reply-notes wording). Every session completed with a full score: each suite run
once, no refusals.

| Arm | $ per rep | Mean |
|---|---|---|
| control-direct | 0.655, 0.652, 0.655 | 0.654 |
| flash-direct | 0.507, 0.507, 0.511 | **0.508 (−22%)** |
| flash-proxy | 0.507, 0.505, 0.518 | **0.510 (−22%)** |

**Injection check** (`--injection --reps 2`, same arms).
- **Injection:** no session in any arm complied with the planted instruction.
- **Refusals:** none.
- **Cost:** control $0.656 / $0.655; flash-direct $0.509 / $0.505; flash-proxy
  $0.514 / **$1.308**.
- **The $1.308 session:** the model still called `horizon_retrieve` on earlier,
  cleared logs (unit and api, then db, cli and io) on four turns, despite the
  softer wording. Horizon's retrieval put each retrieved log back into the cached
  prefix (96k written on one step).

**Fix (5dbb578): stubs no longer offer retrieval.** They carry no hash and do
not mention `horizon_retrieve`; originals are still stored. Evidence for
dropping it:
- flash-direct's stubs never offered it, and in 11 of its 12 sessions it never
  needed it. The 12th ended in a refusal.
- When offered in the proxy, it was taken heavily in 2 of 10 sessions, each at
  about twice control.

**Validation** (`--arms flash-proxy`, no retrieval offer). The credit ran out on
the last step of rep 2, and the injection reps did not run.

| Rep | Requests | $ | Score | Retrievals | Suites repeated |
|---|---|---|---|---|---|
| 0 | 7 | 0.506 | 1.00 | 0 | 0 |
| 1 | 7 | 0.504 | 1.00 | 0 | 0 |
| 2 | 6 of 7 (credit ran out) | — | — | 0 | 0 |

Rep 1 uses the same logs as both heavy-retrieval sessions.

## Where it stands (official API, Opus 5.5)

| | Sessions | Mean $ | vs control | Score |
|---|---|---|---|---|
| control (direct + proxy) | 14 | 0.654 | — | 1.00 |
| flash-direct | 11 complete + 1 refusal | ~0.51 | **−22%** | 1.00 on all complete |
| flash-proxy, current design | 2 complete + 1 partial | 0.505 | **−23%** | 1.00 |
| flash-proxy, with retrieval offer | 5 complete | 0.511 (3 sessions, main run) / 0.514 / 1.308 | — | 1.00 |

- **Savings:** with nothing pulling outputs back into the prefix, Flash
  Observations is consistently 22–23% cheaper on this 6-call task, with no loss
  of accuracy. The cost model predicts more on longer sessions.
- **Injection:** 0 of 6 sessions complied, 2 of them with flashes.
- **Refusals:** 1 in 12 flash-direct sessions, 0 in all others. Small numbers;
  keep counting.
- **Before turning it on by default:** more flash-proxy sessions on the current
  design, including the injection reps, which did not run.

**Spend:** about $10.60 this round. All live runs together cost about $24 at
list price, plus hidden CCR continuations in the retrieval sessions.

**ModelFlare, second key (2026-10-09): failed again.**
- **Caching:** now partial (repeat reads `[0, 10260, 10260]`).
- **`clear_at` without its beta:** still accepted.
- **"Cleared" message:** still billed in full (+14,649 tokens).
- **Verdict:** the gateway does not pass turn-scoped messages through, so Flash
  Observations stays off there.

**oneprovider.dev (2026-10-09): not applicable.** Its `/v1/models` lists no
Claude model (DeepSeek, Gemini, GLM, GPT, Grok and Kimi only), and
`claude-opus-5-5` returns "The requested model is not available". Flash
Observations depends on Claude's `clear_at` system messages, so it cannot run
there.

### What decides it

1. **Zero 400s in the flash arms.** No `clear_at` placement error, no
   preserved-thinking error on later turns.
2. **Billing.** In `flash-*`, each request's `cache_read_input_tokens` covers
   the stubbed prefix and `input_tokens` is about one log, not a growing
   history. The cleared flashes are not billed.
3. **Quality.** The flash arms' score matches the control arms'.
4. **Integration.** `flash-proxy` matches `flash-direct`.

## Dry run against a strict fake (no key; done 2026-10-08)

`--fake` runs the same harness against `fake_upstream.py`, a local fake of the
Messages API. It returns a 400 for:
- `clear_at` without the beta;
- a misplaced system message;
- `cache_control` on a turn-scoped message;
- **any edit to an earlier message** (preserved thinking, enforced strictly).

It renders only what the model would see, so cleared messages render nothing,
and it bills from a simulated prefix cache. A scripted "model" notes the
failures it can see in the newest output and answers from its own notes.

```
arm             rep reqs  uncached    write      read  output       $ score
control-direct    0    7         0   50,332   126,901     660   0.290  1.00
flash-direct      0    7    50,222    2,185     6,693     660   0.226  1.00
control-proxy     0    7         0   50,451   127,615     660   0.291  1.00
flash-proxy       0    7    50,222    2,184     7,520     660   0.227  1.00
```

**What this shows:**
- The integration is append-only through the real proxy (no edit errors in 7
  requests per session).
- The beta is attached.
- Placement is valid.
- Each flash renders on the turn after its tool call, and cleared flashes cost
  nothing.
- Even this short session is 22% cheaper, because each log is billed once at
  1.0x instead of written at 1.25x and re-read every turn.

**What it cannot show** (the live run is for these):
- the real model's behaviour, i.e. whether it keeps what it needs;
- the real cache's behaviour;
- whether the API treats flashed text as data.

The fake's token counts are estimates (4 characters per token).

## OpenAI (next-turn stubbing)

Feature: `horizon/transforms/flash_openai.py`, switched on with
`HORIZON_FLASH_OPENAI=1`. See the
[implementation guide](../../wiki/plans/2026-10-08-implementation-guide.md)
for how it works and why the savings grow with session length on OpenAI.

| Script | What it does |
|---|---|
| `preflight_openai.py` | Checks for a few cents that the endpoint (1) reports cached tokens, (2) caches repeats, (3) accepts an edited earlier output next to encrypted reasoning, (4) still caches the prefix before the edit, and (5) can read the newest output. |
| `live_test_openai.py` | Same task and arms as `live_test.py`, over `/v1/responses` with `store: false`. `--suites N` sets the session length. |
| `fake_openai.py` | A strict fake used by `--fake`. It simulates automatic prefix caching (1,024-token minimum, 128-token steps), returns 400 for tampered encrypted reasoning, and returns 400 for an output with no matching call. |

```bash
OPENAI_API_KEY=... python experiments/flash-observations/preflight_openai.py
OPENAI_API_KEY=... python experiments/flash-observations/live_test_openai.py --reps 3 --suites 12
python experiments/flash-observations/live_test_openai.py --fake --reps 1 --suites 24   # free
```

**Dry run on the fake (2026-10-09), GPT-6.1 Sol prices.** No errors, every
score 1.00, and flash-proxy matched flash-direct exactly.

| Suites | control $ | flash $ | Saving |
|---|---|---|---|
| 6 | 0.127 | 0.119 | 6% |
| 24 | 0.722 | 0.506 | 30% |

**Not run on the real API yet.** The preflight answers the one open
question, whether an edited history next to encrypted reasoning is accepted,
for a few cents.

**Sizing a live run.** The fake counts about 4 characters per token, but on
the real Claude API these logs were about 19k tokens each, 2.3x the estimate.
At 24 suites a real control session would pass 400k tokens of context, beyond
GPT-6.1 Sol's 272k whole-request tier. Use `--suites 12`: it stays under the
tier and costs roughly $6–7 for 4 arms x 3 reps. Check the balance first.

**ModelFlare, `gpt-6.1-sol` (2026-10-09): usable, with
`HORIZON_FLASH_OPENAI_UPSTREAMS=modelflare.dev`.** Two preflight runs, at low
and medium reasoning effort:

| Check | Result |
|---|---|
| Cached tokens in `usage` | Present |
| Prompt caching | Intermittent: repeats `[0, 7680, 7680]` and `[7680, 7680, 0]`, about 2 in 3 hit (likely an account pool) |
| Edited earlier output accepted | Yes, HTTP 200 |
| Prefix before the edit still cached | Yes (10,214 tokens) |
| Newest output readable | Yes (`MAPLE`) |
| Encrypted reasoning returned | Never, even with `include: ["reasoning.encrypted_content"]`; nothing for an edit to break |

The strict "every repeat hits" check fails, but intermittent caching costs
both arms. It hurts the control arm more, because a miss re-bills the whole
context and flash keeps that context small.

Whether OpenAI itself accepts an edit next to encrypted reasoning is still
untested; it can only be checked on the official API.

ModelFlare failed the Claude preflight twice. So the allow-list is per
feature: `HORIZON_FLASH_OPENAI_UPSTREAMS` enables the OpenAI version on a
host without enabling the Claude one.

### Live run through ModelFlare (2026-10-09, stopped after one pair)

`live_test_openai.py --base-url https://modelflare.dev/v1 --reps 3 --suites 12
--effort medium`, `gpt-6.1-sol`. Stopped after the first control/flash pair:
logs are about 15k tokens each on GPT, not the fake's 8.4k, and ModelFlare's
cache missed often, so the full run was heading for about $10–11 at OpenAI
list prices instead of the estimated $6–7.

| Arm | Requests | Input tokens | Cached | $ (OpenAI list) | Score |
|---|---|---|---|---|---|
| control-direct | 13 | 1,327,413 | 744,970 | 1.272 | 1.00 |
| flash-direct | 13 | 283,468 | 6,810 | **0.581 (−54%)** | 1.00 |

- **Context growth:** with flash, the context grew about 650 tokens per tool
  call (the stub) instead of about 15k. Every suite ran once, and the model
  listed all 36 failures.
- **ModelFlare caching:** it missed on 4 of 13 control requests, including
  the two largest (134k and 218k tokens, billed in full). That is why flash
  saves so much here. On an endpoint that caches reliably, the estimate at 12
  suites is closer to 15–20%.
- **Smoke test of the real proxy integration (flash-proxy, 2 suites):** every
  request accepted, the log flashed, and on the next turn it was stubbed. The
  model found all 6 failures. The session first scored 0.00 because the scorer
  compared full pytest ids literally; that is fixed, and the answer re-scores
  1.00.
- **Full flash-proxy session (same logs, 12 suites):** 13 requests, 285,281
  input tokens, **$0.596 (−53% against control's $1.272)**, score 1.00.
  - Horizon flashed every step (1 shown once, then 1 → 11 stubbed).
  - No errors and no retrievals; each suite ran once.
  - It matches flash-direct (283,468 tokens, $0.581) to within 1%, so the
    proxy integration works end to end on a real model.
- **Not yet measured:** repeats (one session per arm), and OpenAI's own API,
  where caching is reliable and the saving at this length should be smaller.

## DeepSeek and Gemini through oneprovider (Chat Completions, 2026-10-09)

`live_test_openai.py --api chat --base-url https://api.oneprovider.dev/v1
--suites 12`, one session per arm. `$` is at each model's list price found on
2026-10-09 (DeepSeek V4 Pro: $0.435 input / $0.0036 cache hit / $0.87 output;
Gemini 3.1 Pro: $2 / $0.20 / $12, $4 / $18 above 200k). oneprovider's own
billing may differ.

**Preflight (`preflight_openai.py --api chat`).**

| Check | DeepSeek V4 Pro | Gemini 3.1 Pro |
|---|---|---|
| Edited earlier output accepted | Yes, with `reasoning_content` passed back | Yes |
| Prefix before the edit cached | Yes (8,960 tokens) | Not reported |
| Newest output readable | Yes | Yes |
| Cached tokens in `usage` | Slow to appear (0 on immediate repeats, then hits) | Never |

The chat preflight forces only the first tool call (`tool_choice: "required"`).
oneprovider's Gemini returned an empty reply for a named `tool_choice`, and for
any forced call once the history held a tool result.

**DeepSeek V4 Pro: no saving, and one quality miss.**

| Arm | Input | Cached | $ | Score |
|---|---|---|---|---|
| control-direct | 945,660 | 798,336 | 0.068 | 1.00 |
| flash-direct | 180,419 | 26,624 | 0.069 | **0.80** |
| flash-proxy | 218,872 | 60,800 | 0.075 | 1.00 |

- **No saving:** DeepSeek's cache works well, and a cache hit costs under 1%
  of an input token. Re-reading old outputs is almost free, so stubbing them
  saves nothing. Flash also wrote more output (the notice asks for notes).
- **The miss:** in flash-direct the model named the right `api` tests but
  invented their error values. It did not write the numbers into its reply
  while the log was visible; they were probably only in its reasoning.
- **Verdict:** do not enable Flash Observations for DeepSeek.

**Gemini 3.1 Pro: −80% client-side; the proxy session did not test flash.**

| Arm | Requests | Input | $ | Score |
|---|---|---|---|---|
| control-direct | 13 | 1,310,784 | 2.677 | 1.00 |
| flash-direct | 13 | 245,888 | **0.544 (−80%)** | 1.00 |
| flash-proxy | 2 | 205,775 | 0.488 | 1.00 |

- **Why −80%:** oneprovider reports no caching for Gemini, so every turn
  re-bills the whole context. Control also reached 207k tokens, past Gemini's
  200k whole-request tier; the harness prices it at the lower tier, so
  control's real cost is higher still.
- **flash-proxy did not test flash.** In both attempts Gemini called all 12
  suites in its first turn, so nothing was ever stubbed. The second attempt
  sent `parallel_tool_calls: false`, which the gateway ignored.
- **Cause:** Horizon injected its `horizon_retrieve` tool on the first request
  (`tool_injection_decision decision=inject_eager`, a CCR feature separate from
  flash). The direct arms never saw a second tool.
- **A fair proxy test** needs CCR tool injection off in both proxy arms.

**Spend:** about $4.40 at list prices: DeepSeek $0.21, Gemini $4.19
(including the second flash-proxy attempt).

### Fair Gemini proxy test (2026-10-09)

`--proxy-no-ccr` (`HORIZON_NO_CCR=1`, so no injected `horizon_retrieve` tool):
$0.474, score 1.00. Gemini **again called all 12 suites in its first turn**, so
nothing was stubbed. Through the proxy that is 3 of 3 sessions; called
directly, 2 of 2 sessions and 6 of 6 single first-turn probes made one call.

**The CCR tool was not the cause.**
- The real CLI proxy's forwarded request was captured against a local
  recorder. It differs from the direct request only in `max_tokens` being
  renamed `max_completion_tokens` (Horizon's GPT-5/o-series shim). Headers and
  user agent are the same.
- Direct probes with either field made one call each time.
- The batching is most likely nondeterminism in the gateway or the model.
- Batched sessions cost about the same as flash ($0.47–0.49 against control's
  $2.68), because they avoid re-reads too.
- **Status:** a Gemini session that exercises stubbing through the proxy has
  not happened. flash-direct shows the mechanism works on Gemini (−80%,
  score 1.00).

## Per-model decision (`HORIZON_FLASH_MIN_READ_RATIO`)

The OpenAI-format path now asks, per model, whether stubbing pays
(`flash_pays_for` in `horizon/transforms/flash_openai.py`):

| Case | Decision |
|---|---|
| List cache-read price below 0.04x input and no write premium (DeepSeek V4 Pro: 0.033x in the catalog, about 0.008x in July 2026 listings) | **Skip**: re-reading old outputs is nearly free, so stubbing only adds risk |
| Everything else (GPT-6.1 Sol 0.05x, Gemini 3.1 Pro 0.10x) | Flash |
| A model Horizon cannot price (oneprovider's `gemini-3.1-pro` name) | Flash: opt-in and host-gated, and an upstream that does not cache is where stubbing pays most |

- **Threshold:** 0.04 lies between the measured no-saving case (DeepSeek) and
  the measured saving (GPT, 0.05x). Set `HORIZON_FLASH_MIN_READ_RATIO` to
  change it, or `0` to turn the check off.
- **No live observation of caching:** a "does this upstream cache?" signal
  was dropped from the design. oneprovider's DeepSeek replies sometimes omit
  the cache field entirely, even when hits occur, so a missing field cannot be
  read as "not caching".
- **Notice wording:** the notice on a shown-once output now asks for exact
  details (names, numbers, paths, error messages) in the reply text, not only
  in reasoning. That is the failure seen in the DeepSeek session.
