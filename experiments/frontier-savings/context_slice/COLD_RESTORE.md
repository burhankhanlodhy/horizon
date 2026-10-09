# Large cold-restore investigation — October 9, 2026

The new offline study uses this project's local Claude history to examine
100k, 250k and 500k text-token windows rather than only small source files.
Private history is processed in memory; only aggregate results are written.
There are no provider calls and no automatic context-rewrite deployment.

## Method

Three available client log files yield seven episodes separated by compaction
markers. The largest episode contains **526,074 text tokens** under cl100k_base.
Assistant response parts are deduplicated; tool-use/result records are grouped
atomically. These rendered text counts are not exact provider requests: hidden
system instructions, protocol fields and signatures are not modeled.

For each window and each of the seven source queries from `prototype.py`, the
experimental selector retains every visible user instruction, the latest twelve
atomic groups, and an exact source dependency slice with provenance. The current
source scope is also included in the full-history control. Known entrypoints
remain oracle inputs, not a demonstrated natural-language locator.

This is deliberately an aggressive upper-bound policy. Earlier assistant
decisions and old observations can be omitted. Consequently, even preserved user
instructions and complete tool pairs **do not establish semantic equivalence**.
No model has solved a long task with these reduced histories.

## Observed sizes

| Target history | Actual text window | Retained history + source receipts, across seven queries |
|---|---:|---:|
| 100k | 100,132 | 12,951–16,267 |
| 250k | 250,165 | 14,822–18,138 |
| 500k | 502,576 | 15,511–18,827 |

All 21 cases preserve the counted visible user-instruction groups and all
protected tool pairs. All source fact/support checks pass, but those checks only
cover the known source queries. They are not a task-success or history-recall
benchmark.

## Economic sensitivity, not actual savings

The two hypothetical profiles from the original experiment are reused. The
request stream includes twenty normal turns, shared instructions/new input/output,
and one recovery cycle retrieving 2% of the full pack. Results below are
conditional on that aggressive retained history being sufficient:

| History size | Modeled reduction versus full-history restoration, first profile | Second profile |
|---|---:|---:|
| 100k | 70.0–71.9% | 71.3–73.3% |
| 250k | 84.5–85.4% | 85.2–86.3% |
| 500k | 90.8–91.4% | 91.2–91.8% |

These large percentages come chiefly from **assuming most history can be
omitted without consequence**. They are not evidence that the selector is safe
or that users will save these amounts. The uncertainty is now about retained
knowledge and recovery, not whether fewer input tokens are mathematically cheaper.

Strong control: retain the same protected history and gather the same dependency
source with ordinary narrow reads, omitting receipts/labels. The new pack costs
approximately **0.3–0.9% more** than that already-narrow control. The current
prototype does not demonstrate an incremental economic advantage over an agent
that already builds the same working context.

The modeled gate rejects warm and unknown cache states, unknown prices, stale
source and incomplete protected tool pairs. Five gate checks pass. Those checks
exercise explicit flags; a deployed controller would have to establish their
truth from provider/client state, not assume it. Positive modeled savings do not
override the missing semantic quality evidence.

## Assessment

Large unavoidable restores are a plausible target because the absolute cost of
restoring hundreds of thousands of tokens can dominate small edit savings.
The useful research contribution would be a reliable **selection and recovery
policy** that identifies sufficient evidence automatically, with actual client
protocol compatibility and current per-model prices. Token-size arithmetic alone
is not that policy.

Before rollout:

1. Test recall of old constraints, resolved decisions, failed approaches and
   changed-source facts against independent questions not given to the selector.
2. Run long tasks with frozen source snapshots and held-out behavior checks.
   Include deployed proxy plus native compaction and narrow reads as controls.
3. Measure model reasoning, recovery calls, source rereads, cache writes and
   total cost per successful task. Account for restored tool-pair/signature rules.
4. Restrict rewrites to a verified cold/compaction boundary; preserve warm and
   unknown prefixes. Keep tenant/workspace provenance and full retrieval paths.

This study does not send the private logs to Claude. Any later live quality
test should use a separately approved, sanitized long-task corpus.

```powershell
.venv/Scripts/python.exe experiments/frontier-savings/context_slice/cold_restore.py
```

See `cold_restore_results.json` for the 21 cases, horizon/recovery sensitivity,
guards and corpus fingerprint. The active local history can grow, so later
replays may differ. The main [REPORT.md](../REPORT.md) describes prior art and
the initial source-slice study.
