# Compact edit instructions: live pilot and cold-restore follow-up

October 9, 2026. Prepared for independent review, including Claude.

## Decision

**Continue with compact instructions for repeated structured edits only.** The
live model used the codec successfully, and row creation reduced measured
API-equivalent cost. Small edits and unsupported logic showed no economic win.
Do not enable this for every edit or use these results to alter savings billing.

The context-slice investigation is complete as an offline feasibility study.
It demonstrates smaller candidate contexts, but not preservation of long-task
knowledge. It needs a separate quality evaluation before any live context rewrite.

## Authorization, execution and spend

The user approved the existing configured model with a **$3 total cap**, then
explicitly approved live testing after the initial launch was rejected by
automatic approval review for sharing private source fixtures. Actual requests
started only after that approval. Only the prepared source copies were sent;
private conversation histories were not sent to the provider.

The existing Claude Code login is Claude Pro. The first session reported
`claude-sonnet-5-5`; subsequent sessions pinned that exact identifier. CLI
`total_cost_usd` is an **API-equivalent estimate**, not an observed provider
invoice. The experiment consumed subscription usage; this is not proof of an
actual API charge or dollar savings on the user's subscription.

| Study | Sessions / complete pairs | API-equivalent cost |
|---|---:|---:|
| Primary, six tasks × three repeats × two arms | 36 / 18 | $0.9199594 |
| Higher output limit, twelve-row task × three repeats × two arms | 6 / 3 | $0.1938510 |
| **Total** | **42 / 21** | **$1.1138104 of $3** |

The secondary run used the original remaining budget of $2.0800406, not a new
$3 allowance. Every failed attempt and retry is included. No further live run
is needed to reach the conclusions below.

## What was compared

Both arms use identical fixture source, task goal, custom system instructions,
model, read operation and preview result format. The native arm uses exact
search/replace, including `replace_all`; the compact arm adds restricted
literal/keyword/row-clone operations plus native fallback. Arm order is
randomized with a fixed seed. Runs write only isolated `candidate.py` previews.

The adapter verifies content-bound receipts, types, counts and allowed operation
forms. Multi-operation failures publish no partial candidate. Local validation
covered six fixture expansions, eight rejection/atomicity guards and an actual
MCP stdio transport round trip before provider calls.

Six task types cover a function literal, five repeated keyword changes, four
and twelve catalog clones, a module constant, and unsupported new logic.
Only these authored Python tasks were evaluated. Whole AST grading checks the
requested change and absence of other AST changes; it does not execute the
candidate or verify preservation of comments/formatting.

This is **tool-adoption testing**, not an end-to-end experiment through the
deployed Pi 5 proxy or a representative full Claude Code/Codex workflow. Native
shell/script tools were disabled in both arms. In particular, an ordinary
Python script could also clone repetitive rows without emitting their complete
text. This study does not establish an advantage over that stronger baseline.

## Corrections needed to interpret the primary result

### Unspecified insertion position

The primary strict AST grader marked all three native four-row edits wrong.
They actually preserved all fields and row orders, placing the new rows beside
the template instead of at the dictionary's end. The task did not require an
end-of-dictionary position. The higher-limit twelve-row trials did likewise.

`analyze.py` separately adjudicates these six cases. It permits only the new
entries' insertion position to vary, verifies existing-row and new-row order
independently, and requires the full remaining AST to match. Original strict
grades remain in both raw result JSON files. This is an explicitly **post hoc
grading correction**, not a new model attempt or a hidden removal of failures.

### Output truncation

The primary 2,048-token response cap made all three native twelve-row runs fail
with an explicit output-token maximum error. Their aggregate 24,684 output
tokens include repeated unsuccessful generation. Their cost remains counted.
The compact arm completed those edits, but treating the resulting apparent
85.4% cost reduction as ordinary edit savings would exaggerate the evidence.

A separate sensitivity run raised the cap to **8,192 for both arms**, kept the
same model and tasks, and included three pairs. Both arms then passed all three
tasks after insertion-position adjudication. The compact arm had one invalid
operation and recovered; its extra call and tokens are included below.

## Results with successful task comparisons

Each row totals three repetitions. The twelve-row row uses only the higher-limit
study; the others use the primary study. Negative cost change means cheaper.

| Task | Native cost | Compact cost | Compact cost change | Task pass rate, native / compact |
|---|---:|---:|---:|---:|
| One function literal | $0.033260 | $0.033946 | +2.1% | 3/3 / 3/3 |
| Five repeated keywords | $0.042906 | $0.043652 | +1.7% | 3/3 / 3/3 |
| Four catalog rows | $0.075939 | $0.045377 | **−40.2%** | 3/3 / 3/3 |
| Twelve catalog rows, higher limit | $0.139185 | $0.054666 | **−60.7%** | 3/3 / 3/3 |
| One module constant | $0.050479 | $0.068679 | +36.1% | 3/3 / 3/3 |
| New logic through native fallback | $0.066187 | $0.072886 | +10.1% | 3/3 / 3/3 |

Four-row edits reduced total output from **2,805 to 576 tokens** (79.5%). The
higher-limit twelve-row edits reduced output from **7,319 to 1,147** (84.3%).
Average absolute savings were about **$0.0102 and $0.0282 per task**, respectively.
These are pennies on isolated edits, not measured substantial monthly savings.
Workload frequency and alternative native strategies determine the user benefit.

The four non-clone tasks collectively cost **13.7% more** with the compact arm
($0.219164 versus $0.192832), with both arms passing all twelve task attempts.
The five primary constant-edit rejections were `scope missing or ambiguous`:
the model tried unsupported scope names, and the tool description did not
explain that `*` is the codec's module-wide scope. Each constant task eventually
used native fallback. That is a fixable interface deficiency, but its cost is
part of this version's observed result.

Across all 42 sessions, compact passed **21/21** and native passed **18/21**
after adjudication. All three remaining native failures are the primary
truncation cases. Every copied `before.py` remained unchanged. Successful
non-truncated comparisons passed in both arms; no quality superiority has
been demonstrated for those comparisons.

## Why the aggregate headline is misleading

The complete primary run reports **48.3% less cost and 84.6% less output**, but
its largest native task failed from truncation. Excluding that task leaves
only **1.6% aggregate cost reduction**, despite 22.5% less output. The clone
task savings almost cancel the overhead on other tasks in that small suite.

All input/cache categories are counted, not only edit payload tokens:

| Primary usage category | Native | Compact |
|---|---:|---:|
| Regular input | 120 | 118 |
| Cache read | 120,407 | 150,930 |
| Cache creation | 71,794 | 59,445 |
| Output | 29,474 | 4,552 |
| Edit payload, cl100k estimate | 2,215 | 1,073 |

Payload counts use a local tokenizer and are not provider billable token counts.
CLI output includes more than the serialized edit. Tool definitions, rejections,
reasoning behavior and cache state affect the complete result.

Three repetitions per authored task are insufficient for broad statistical
claims. Identical inputs may reuse provider caches, and some outputs are
identical across repeats. These are not independent workload samples. No claim
of cross-model, cross-language or representative monthly savings is justified.

## Implementation path for Claude Code and Codex

1. Use an opt-in client MCP adapter to emit a compact edit operation against a
   freshly read, content-bound source receipt.
2. Expand the operation deterministically in a controlled local workspace.
   Return the resulting ordinary diff to the client's existing review/approval
   and application mechanism. The pilot currently stops at preview creation;
   that production approval bridge is not implemented.
3. Offer compact operations selectively for repetition/template creation.
   Keep ordinary patch/edit tools for tiny replacements and new logic. Measure
   the cost of selection itself; no oracle-selected production win is established.
4. Explain module scope and represent the operation grammar in a structured
   schema. Add ambiguity and changed-source cases to the next quality benchmark.
5. Benchmark against native scripted transformations as well as native edits,
   then evaluate full tasks through the existing proxy with per-account usage.

Codex can be evaluated using the same local stdio service with a client adapter;
its native patch and approval integration needs a separate implementation and
benchmark. **No live Codex request was run in this pilot.** An ordinary transparent
proxy cannot get these output savings merely by shrinking already-generated
tool arguments: the model must emit compact instructions and a trusted client
must understand and expand them. Native tool contracts and approvals matter.

No production routing, account analytics, savings accounting, billing or Pi
deployment was changed by this experiment.

### Follow-up: automatic proxy use

The user's subsequent requirement is automatic proxy operation without an
end-user toggle. [PROXY_AUTOMATION_FRAMEWORK.md](PROXY_AUTOMATION_FRAMEWORK.md)
reviews a transparent adapter that presents compact tools only to the provider
and expands their output into the client's existing native edit contract. It
specifies automatic skips, stable catalogs, durable history mapping, buffered
translation, native version guards and estimated savings accounting. This is
an alternative to the managed local MCP bridge above, conditional on proving
compatibility for each route; it has not been implemented or live-tested.

## Large cold restores: follow-up outcome

See [COLD_RESTORE.md](../context_slice/COLD_RESTORE.md) and its aggregate JSON.
Three local history files yielded seven episodes; the largest contained
526,074 rendered text tokens. Twenty-one source-query/window cases investigated
approximately 100k, 250k and 500k histories. The retained packs ranged from
12,951 to 18,827 tokens, preserving counted visible user instructions, recent
atomic tool groups and the known source-query dependencies.

The economic scenarios show 70–92% possible reduction versus full restoration,
**conditional on omitted history being unnecessary**. Older assistant decisions
and observations can be lost. No live task or old-decision recall test verifies
that assumption. Compared with the same selected history plus ordinary narrow
source reads, receipts add roughly 0.3–0.9% cost instead of saving more.

Next research gate: use a separately approved sanitized long-task corpus with
held-out old constraints, decisions and changed-source questions; compare full
restore, native compaction/narrow retrieval and the slice policy. Measure recovery
and total cost per successful task. Keep warm/unknown prefixes untouched and
establish an actual cold boundary before considering a rewrite.

## Reproduction and review artifacts

* [README.md](README.md): adapter, safety scope and run design.
* [live_results.json](live_results.json): immutable primary observations.
* [sensitivity_results.json](sensitivity_results.json): higher-limit observations.
* [analysis_results.json](analysis_results.json): totals and separate adjudication.
* `adapter.py`, `server.py`, `run.py`, `analyze.py`: implementation and analysis.
* `validation_results.json`, `transport_results.json`: local checks.

Raw provider responses and tool contents remain under ignored `runs/`. The public
JSON exports numeric results and task/model labels, not raw prompts or secrets.
To regenerate the analysis locally, with the ignored candidates still available:

```powershell
.venv/Scripts/python.exe experiments/frontier-savings/pilot/analyze.py
```

This is not an instruction to repeat paid calls. Future live experiments must
carry forward the original spend if they use this same $3 authorization.
