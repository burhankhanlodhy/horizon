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

**Run it** (about 30–60 minutes; Opus 5.5, 4 arms × 3 reps ≈ $4–7 at list price, thinking included):

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
