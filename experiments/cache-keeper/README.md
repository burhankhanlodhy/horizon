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
- Hosted proxy: the end call is loopback-only, so on the Pi it needs a route
  through the gateway and account auth; until then the idle cap and the price
  budget bound wasted pings.
