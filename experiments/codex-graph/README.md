# Code graph retrieval and estimated-ledger experiment

## Selective preview 0.2.4

[SELECTIVE-LIVE-RESULTS.md](SELECTIVE-LIVE-RESULTS.md) now reports an actual
paired five-turn chat pilot. The selective policy used one graph overview,
then source-only focused work and tool-free synthesis. Successful-turn input
was 5.56% higher and main-model API-price-equivalent cost 58.34% higher with
the graph. A service-capacity interruption/retry and changed cache mix confound
the full-session cost gap; even the first two uninterrupted turns cost 8.77%
more. This does not establish reliable graph savings or a causal version effect.
See [SELECTIVE-LIVE-PROTOCOL.md](SELECTIVE-LIVE-PROTOCOL.md),
[selective-live-results.json](selective-live-results.json) and
[selective-live-quality.json](selective-live-quality.json) for the preserved
interruption, usage integrity, answer review and limitations.

The implementation now advertises three graph tools, defaults to five search
results / 3,000 response bytes, and routes known-file/symbol tasks directly to
source. Native trace failures suggest source fallback rather than retries.
[selective-overhead-results.json](selective-overhead-results.json) measures
544 versus 706 locally serialized schema/guidance tokens (22.95% less). This
is a static overhead measurement, not a new live whole-session savings result.

Graph retrieval estimates remain in the existing ledger as diagnostics with
zero dollar credit. Historical graph estimates are excluded from monetary
totals and new fee calculations, without rewriting stored evidence or issued
invoices. Re-run the local interop proof with `verify_ledger.py --output
experiments/codex-graph/selective-ledger-result.json` to preserve the earlier
ledger report. Existing experimental reports describe their historical code.

## Actual published Graphify comparison

[GRAPHIFY-RESULTS.md](GRAPHIFY-RESULTS.md) measures published Graphify 0.9.93
with its unmodified Codex skill and native routing against eight new paired
ordinary-retrieval chats. Its code-only graph was built once with zero model
tokens; saved answers and reflections persisted. Total input rose 107.19%
and main-model API-price-equivalent cost rose 35.94%. One of eight pairs was
cheaper, with a coverage caveat. Tool-output tokens alone fell 21.59%.
This is a local code-only workflow test, not Graphify Cloud or semantic enrichment.

- Fixed scope and limitations: [GRAPHIFY-PROTOCOL.md](GRAPHIFY-PROTOCOL.md).
- Full data: [graphify-results.json](graphify-results.json).
- Limited manual coverage review: [graphify-quality.json](graphify-quality.json).
- Runner: `graphify-benchmark.cjs`; analyzer: `analyze-graphify.cjs`.
- Rerunning the live runner consumes account usage. Preserve existing evidence.

## Reusing the graph across fresh chats

[FRESH-CHAT-RESULTS.md](FRESH-CHAT-RESULTS.md) reports eight live paired fresh
chats (four tasks, two repetitions) against the same frozen repository. The graph
was built once and reused. Overall, preview 0.2.3 used 35.97% more input tokens
and cost 23.68% more in main-model API-price equivalents; one of eight pairs cost
less with the graph. This experiment does not support a whole-chat savings claim.

- Protocol and limits: [FRESH-CHAT-PROTOCOL.md](FRESH-CHAT-PROTOCOL.md).
- Full totals, per-pair usage and source integrity: [fresh-chat-results.json](fresh-chat-results.json).
- Manual coverage review: [fresh-chat-quality.json](fresh-chat-quality.json).
- Runner: `node experiments/codex-graph/fresh-chat-benchmark.cjs prepare`, then
  `run`. It requires the prior frozen fixture, installed engine and authorized
  account usage. Do not overwrite existing evidence. After an inspected failed
  attempt, `archive-failed` preserves its logs and usage before explicit retry.
- Analyze: `node experiments/codex-graph/analyze-fresh-chats.cjs` after all chats
  complete and temporary sign-in copies have been removed.

## Live source-review session comparison

`live-session-benchmark.cjs` runs the installed Codex engine against isolated,
frozen source copies. Arm A uses ordinary retrieval; arm B has the actual 0.2.3
project graph integration. Both use gpt-6.1-sol, high reasoning, the same initial
architecture-review prompt and 20 identical follow-ups, and alternating arm
order at each step. The prompts are recorded in the runner. No production proxy
or ledger participates. Graph indexing itself makes no model calls.

Temporary sign-in copies stay outside source trees and reports and are removed
on completion. The runner uses Codex's documented automatic approval review,
bounded local reads, no edits, no web access and no agent delegation. A first
pilot that blocked all retrieval is preserved separately and excluded from the
comparison; its reported model cost is disclosed separately.

Run `node experiments/codex-graph/live-session-benchmark.cjs prepare` once, then
`node experiments/codex-graph/live-session-benchmark.cjs run`. The state and raw
logs are in ignored `desktop/dist-windows/npx-test/live-graph-21-turns/`. Resume
with `run` rather than overwriting existing sessions. Inspect the fixed paths
and pricing constants before running on another machine. The real test consumes
account quota; the earlier scripted retrieval tests do not.

After both arms finish, run
`node experiments/codex-graph/analyze-live-session.cjs`. It checks that source
hashes are unchanged and per-turn usage deltas sum to the final engine totals,
then writes `live-results.json` and `LIVE-RESULTS.md`. Resumed CLI usage is
cumulative: summing its raw turn-completed values would overcount history.

Dollar figures are standard API-price equivalents using actual reported cache
hits. Cached input is part of input; reasoning output is part of output. They
are not subscription invoices. Cache state is not forced equal, and one pair
of stochastic sessions does not prove equal answer quality or future savings.
Citation validation checks source file/line existence, not claim correctness.
Auxiliary approval-review usage is not exposed in the main-model totals.

Run from the worktree root after installing `desktop/npx-codex` dependencies:

```powershell
node experiments/codex-graph/benchmark.cjs
node experiments/codex-graph/session-benchmark.cjs
.\.venv\Scripts\python.exe experiments/codex-graph/verify_ledger.py
```

The ten-step `session-benchmark.cjs` exercises three discoveries, six focused
source operations, and one reuse step on this repository. It charges the
actual graph schemas plus both project and MCP guidance once, and includes serialized
retrieval calls and outputs. It retains both broad-search and focused-definition
alternatives; the latter can be cheaper. `session-results.json` records results.
Routes are prescribed by the harness. This is not an autonomous live-model
session: it excludes repeated history input, cache effects, reasoning and
answers, and makes no claim of equivalent answer quality or total session cost.
No account quota is used and no production ledger is changed.

`session-results-0.2.2.json` preserves the earlier regression result. Version
0.2.3 compacts search nodes and uses distinct project guidance and short MCP
safety notes. `search-compaction-results.json` compares the same saved 0.2.2
outputs before and after projection, including receipts and preserving all
ordered source citations. This isolates formatting gains from corpus changes.
The current workflow report is `session-results.json`; the repository snapshot
is recorded because source changes can also change search sizes.

The MCP search calls in these experiments explicitly declare discovery intent.
Lookup and file-scoped search calls no longer create estimate receipts.

The benchmark queries the real persistent repository graph for compression,
pricing, and existing-chat migration entry points. It counts with `o200k_base`
and verifies each returned symbol against its current source citation. Both
arms include the same nine-line source verification excerpt. The report lists
the excerpt count separately; add it to each arm for total task-context counts.

Two alternatives are reported: a bounded broad literal search and a narrower
definition search. Both have a 4,000-token output cap. The graph arm includes
the actual tool invocation, returned JSON including estimation metadata, and a
1,024-token allowance per query for its complete tool schemas and project
guidance. Recharging this allowance on every query is conservative. Definition
search includes all matching definitions up to the output cap, not just a
short favorable prefix.

`results.json` records measured retrieval counts and the source snapshot. These
are deterministic retrieval tests, not autonomous live-model A/B tests or a
promise of end-to-end savings. Focused searches can be cheaper than graph use.
Source citation checks do not prove that a model would produce an equally good
answer. The benchmark consumes no ChatGPT quota.

The second script passes the actual Node-generated receipts into the Python
proxy, recounts output and invocation tokens, exercises its durable SQLite
outbox, and validates emitted rows against the control-plane API schema. It
replays the history twice and checks that three graph receipts produce only
three graph rows. `ledger-result.json` reports the result. Temporary accounts
are local fixtures: no production account, invoice, or ledger is changed.

The local alternative used by ledger receipts is deliberately more bounded
than the broad benchmark: it stops after 4,000 tokens or 16,000 characters of
matches, and scans at most 10,000 eligible files / 64 MiB. These limits can make
the ledger estimate smaller than the benchmark's reported broad-search delta.
No full-file byte count is credited as token savings.
