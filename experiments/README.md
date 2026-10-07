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
   result is not counted again on every later turn.
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

- **Conservative for long sessions.** A token removed at turn 10 would otherwise
  have been re-sent (as a cheap cache read) on every later turn; that repeat
  saving is not counted. In long agent sessions the real saving is higher than
  shown.
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

## Pending tests

| Test | Why | Status |
|---|---|---|
| macOS app on the owner's Mac | Sign-in, Keychain, Terminal launch, Homebrew/nvm PATH, menu-bar icon | Waiting for the Mac's update to macOS 12+ |
| Linux on real desktops | Ubuntu 24.04 GNOME and Fedora KDE VMs: tray, keyring, terminal, `.rpm`, AppImage | Not started (WSL covered `.deb` only) |
| Output shaper on Claude Code | Claude's replies are wordier than Codex's; same paired method as test 2 | Not started |
| Stale reads in the frozen prefix | Let read lifecycle replace reads of already-edited files even when cached; pays only if the read is large and the session long | Needs a code change + paired test |
| Long real sessions | Confirm that tool-output savings (26-56% on Codex `exec`) show in total cost of long sessions | Not started |
| Proxy host before launch | Same speed/live checks as test 1 on the PC host, then the mini PC | Before launch |
| Savings pricing for long sessions | Measure how much the once-per-conversation count understates real savings | Not started |

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
