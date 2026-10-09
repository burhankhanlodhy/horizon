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
