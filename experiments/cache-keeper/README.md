# Cache keep-alive: live test (2026-10-08)

Feature: `horizon/proxy/cache_keeper.py`, off by default.

| Setting | Effect |
|---|---|
| `HORIZON_CACHE_KEEPALIVE=1` | While a session is idle, re-send its last forwarded request as a `max_tokens: 0` pre-warm shortly before the cache entry expires (4 min 15 s on the 5-minute lane, 55 min on the 1-hour lane). Stops after a price budget (5 pings / 9 pings), after `HORIZON_CACHE_KEEPALIVE_MAX_IDLE_S` (8 h), on any ping that had to write, or when `wrap` reports the tool exited (`POST /v1/horizon/keepalive/end`, loopback only). Ledger: `cache_keeper.jsonl` in the workspace. |
| `HORIZON_CACHE_TTL_UPGRADE=1h` | Move tool-carrying requests from the 5-minute to the 1-hour lane. |

## Method

`live_test.py`: three local proxies (`start.ps1`): control, keep-alive, TTL
upgrade. Each case: Claude Code 2.1.291 (Sonnet 5.5) reads two large files
(~92k-token context), exits, sits idle, then continues with `--resume`. The
resumed request's own usage in the proxy log is the measurement. Each case has
its own keep-alive id. `--resume` re-renders the end of the conversation, so
even a warm resume writes ~24k tokens; a live session that simply continues
would read nearly all of it.

## Test A: 5-minute lane (`FORCE_PROMPT_CACHING_5M`), 12-minute pause

| Arm | Resumed request: read / written | Cost, both runs |
|---|---|---|
| Control | 8,755 / 83,218 | $0.802 |
| Keep-alive (2 pings, 92,167 tokens read each) | 67,768 / 24,516 | $0.667 + $0.018 pings = $0.685 (-15%) |
| TTL upgrade | 67,750 / 23,899 | $0.933 (+16%) |

All three answered the follow-up question correctly. Findings:

- Pre-warm works with Claude Code's adaptive thinking: each ping read the full
  92k prefix and wrote nothing.
- The keeper's own ledger: avoided $0.163, pings $0.018, net $0.144.
- The TTL upgrade kept the cache too but paid the 2x write on everything, which
  cost more than one rewrite. It pays only across many pauses; keep it off by
  default and rely on keep-alive.
- Pings continue after the tool exits unless something reports the exit; the
  end endpoint stopped them (`{"ended": true}`).

## Test B: 1-hour lane (subscription default), 70-minute pause

| Arm | Resumed request: read / written | Resumed turn | Both runs |
|---|---|---|---|
| Control | 8,755 / 83,428 | $0.736 | $1.136 |
| Keep-alive (1 ping at 55 min, 92,465 tokens read, $0.009) | 67,776 / 24,631 | $0.551 + $0.009 = $0.560 (-24%) | $0.999 (-12%) |

Input side alone: 58,797 fewer tokens written and 59,021 more read = $0.229,
or $0.220 after the ping. Both runs answered correctly.

## Accounting check

The keeper's first ledger said $0.144 (A) and $0.255 (B) net. It overstated:
the control resumes still read 8,755 tokens, Claude Code's tool definitions,
which every session on the account shares and the concurrent pings kept warm.
The ledger now never credits the first request's warm prefix or the tool
definitions (estimated from their size), which removes that $0.035 on B. The
remaining gap to the total-cost difference is output length (the two answers
differed), which caching does not cause.

## What the test does not show

- A session that simply continues (no `--resume`) re-sends the same prefix, so
  a warm cache should be read almost entirely; here `--resume` re-rendered the
  last ~24k tokens in every arm.
- Savings scale with context: these sessions held ~92k tokens on Sonnet. The
  same pause on a 500k-token Opus session saves about $4 per avoided rewrite for
  a $0.10 ping (feature-research Round 3).
- Hosted proxy: covered since, below.

## Hosted proxy and review follow-up (2026-10-08)

Codex's review ([REVIEW.md](REVIEW.md)) found nine issues. What changed:

| Finding | Change |
|---|---|
| F1 ping spend incomplete | Every ping is priced from its full usage (read, write, uncached) at the model's own rates. Any write, a read of zero, a non-200 or a timeout stops the group. A timeout is billed as a full read of the context, labelled estimated. |
| F2 groups not tenant-scoped | A group is the liveness id hashed with the verified account UUID. On a hosted proxy a request without a liveness id is never kept warm, and neither is an account whose plan has paused savings. |
| F3 ended sessions revived | `end` drops the session's in-flight requests, and a completion for an ended group is ignored. |
| F4 races | A completion older than the current target is ignored. A ping that returns after a newer request replaced its target is billed but changes nothing. |
| F5 hosted lifecycle | `POST /v1/horizon/keepalive/end` is allowed by the Pi 5 gateway, needs the account key at the account middleware, and ends only that account's session. `wrap` calls it from `finally`, so Ctrl+C and crashes end the session too. A killed `wrap` still relies on the idle cap and the ping budget; no heartbeat lease, because a laptop that sleeps would then lose exactly the pauses keep-alive is for. |
| F6 mixed TTLs, late pings | The lane is the shortest marker's TTL, top-level automatic caching counts, and a body with no markers is skipped. A group whose window was missed is stopped instead of paying to rebuild. |
| F7 abandoned sessions | Each ping is its own account ledger row with negative savings; a resumed request carries the avoided rewrite. The dashboard total is therefore net, abandoned sessions included. |
| F8 operations | Each tick runs as its own task with a concurrency cap and a 60 s ping timeout, so a slow ping never delays another session. Idle, budget-spent and stopped groups drop their request and credentials. Held bodies are capped at 25M tokens (oldest released first) and end tombstones at 4,096. The Pi runs one proxy worker. |
| F9 prices | Rates come from the catalog per model. An unknown model falls back to $3/M and the row says `fallback`. The 0.5 return odds stay a fixed assumption until the hosted ledger has enough resumes and abandons to calibrate it. |

Dashboard: the Usage page shows rewrites avoided, ping spend and net once an
account has pings; Advanced Analytics has a "Cache kept warm (net)" card.
