# Five experimental savings methods for ContextShrink

Research started October 7; completed and rerun October 9, 2026.

Follow-up on the user's selected priorities: [compact-edit pilot](pilot/README.md)
and [large cold-restore investigation](context_slice/COLD_RESTORE.md). These
separate documents track the new adapter, evaluation status and larger histories.

## Conclusion

The most promising next experiment is **compact, guarded edit instructions**:
have the chosen coding model emit a small operation and expand it locally into
the exact patch. This targets expensive output tokens without changing the
user's model. **Dependency slices at a cold context boundary** are the second
candidate, particularly for large contexts where an agent otherwise rereads
whole files. Neither has yet demonstrated additional monthly savings over the
deployed ContextShrink pipeline.

I investigated and implemented five local prototypes. The experiments establish
mechanical feasibility and expose where costs increase; they do not establish
five production algorithms with substantial savings. Existing research closely
overlaps every underlying technique. The proposed combinations below are
engineering research hypotheses, **not verified first inventions**. A search
cannot establish that nobody has invented a method, and renaming an existing
method does not make it new.

The cache-keeper review was committed separately as **326b0f5**, containing only
`experiments/cache-keeper/REVIEW.md`. It remains an ancestor of the current HEAD.
These new experiments are left for review, without production deployment.

## Results at a glance

All percentages below have different denominators. Do not add them.

| Method | What was actually measured | Favorable result | Strong control / failure | Recommendation |
|---|---|---|---|---|
| 1. Guarded edit instructions | Nine hypothetical edits to copies of real repository files; exact byte reconstruction and parsing | Fixed codec: 44.9% less edit output than the smallest tested native format. Ideal adaptive fallback: 85.6% | One new-logic edit requires 1,168 recipe tokens versus 51 native tokens. Extra reasoning reverses modeled savings | First client-tool pilot, narrowly scoped |
| 2. Dependency slices at cold boundaries | Seven known-entrypoint source queries, 30 factual/support checks | 27,806 source tokens become 10,207; 30/30 checks; 7.8–8.5% modeled 20-turn task saving versus whole source | Same narrow evidence without receipts needs 9,137 tokens; slicing adds no incremental reduction over that control. Warm-prefix rewrite loses money in one profile | Research large cold restores; retain existing narrow reads |
| 3. Guarded tool plans | Eight repository inspection plans, exact evidence equality, five invalidation cases | 16.6% modeled reduction versus sequential tools; 5.5% versus dependency-wave batching at a 20k prefix | About 3.6% more expensive than an existing single script; 700 extra output tokens per plan erase the batching advantage | Use only where clients truly make avoidable model round trips |
| 4. Observation deltas with receipts | 140 controlled observations, three reconstruction guards; separate real transcript census | 22–63% modeled stream reduction on repetitive large synthetic observations versus full repeats | Ordinary diffs are smaller. Actual census: 1.4% emitted-token ceiling among eligible reads, before existing proxy optimization | Low priority for this workload |
| 5. Bounded local repair search | Ten authored toy bugs; selection on visible examples, separate withheld evaluation | Six repairs pass withheld examples; three cases fall back | One unique visible-test candidate fails withheld behavior | Suggestion mode only; no automatic application |

### What these measurements mean

* **Measured:** local token lengths, exact decoded bytes, static evidence checks,
  executed toy examples, guard rejection and local runtime.
* **Modeled:** dollar costs under explicitly hypothetical price profiles, cache
  behavior, recovery overhead and avoided model requests.
* **Not measured:** provider invoices, model adoption of new tool formats,
  reasoning-token changes, representative task solve rates, general novelty,
  or savings beyond today's production pipeline.

There were **zero provider/model calls** in this research. `cl100k_base` is a
comparative tokenizer, not a promise about Claude's or a current Codex model's
billable token count. The output in the results files is reproducible local
evidence, not a savings ledger.

The repository changed during the interruption. The October 9 rerun uses HEAD
`a8020e6` plus the existing working tree. It reads source copies without importing
application code. The context fixture originally expected `liveness_id[:100]`;
the current owner-scoped implementation uses `[:200]`, so that fixture assertion
was updated. Results in this report are the rerun, not the older October 7 totals.

## 1. Guarded edit instructions

### Algorithm and proposed extension

The model emits a bounded intent such as:

```json
{"r":"r0","ops":[["literal","new_session_token",32,48,1]]}
```

A client tool expands this into an ordinary patch. A receipt binds the operation
to one immutable file snapshot. The target must be unique, the occurrence count
must match, and the output must parse. The prototype supports scoped identifier
replacement, literal/keyword changes and cloning dictionary rows. Unsupported
logic uses the ordinary patch format.

The research extension worth testing is a **cost gate before generation**:
identify an edit class from its intent and observed file structure, estimate the
native and operation lengths, and expose the short operation only when its
expected benefit exceeds instruction, reasoning and retry costs. The proxy must
never silently broaden the meaning of an operation or apply a fuzzy fallback.
The current adaptive comparison knows both outputs and is an oracle ceiling;
an actual agent must choose successfully without that hindsight.

This saves output only if the model emits the short representation originally.
Compressing an already generated patch cannot refund its output charge.

### Prior art and novelty boundary

[Aider edit formats](https://aider.chat/docs/more/edit-formats.html) already avoid
whole-file generation. [AdaEdit, BlockDiff and FuncDiff](https://aclanthology.org/2026.findings-acl.1483/)
study structure-aware formats and adaptive format selection. Therefore neither
diff editing nor adaptive choice is a new invention. [Cascaded Code Editing](https://arxiv.org/abs/2604.19201)
uses large-model sketches and a smaller model for application; our prototype
instead uses deterministic expansion of a small supported grammar.
[EfficientEdit](https://arxiv.org/abs/2506.02780) uses speculative decoding,
which requires inference machinery this hosted-model proxy does not control.
[Mekugi](https://github.com/yusing/mekugi) already provides compact, recoverable
tools around stock Codex, a close implementation precedent for client tooling.

Snapshot receipts plus strict operation counts and economic gating are the
proposed integration experiment. Their independent novelty is unestablished.

### Experiment

The fixtures cover literals, a parameter rename, several parameter uses,
repeated keyword updates, one/four/twelve cloned catalog rows, and a new logic
expression. The model did not generate these intents; they were authored.

* Nine expansions match the independently specified expected bytes and parse.
* Stale snapshot, wrong occurrence count and unknown operation are rejected.
* Best baseline is the smallest of native patch, unified diff and exact
  search/replace, including `replace_all` for repeated identical changes.
* Baseline output: **2,743 tokens**. Fixed codec: **1,512**.
* Adaptive size oracle with native fallback: **395 tokens**.
* Added tool instructions: **209 tokens**. Fixed-mode receipts: **144 tokens**.
* New logic loses badly: **1,168 vs 51** tokens; send a native patch there.

In a shared nine-edit stream with a 20k common prefix and hypothetical rates of
$3.75/M write, $0.30/M read and $15/M output:

| Stream | Modeled USD | Change versus native |
|---|---:|---:|
| Native | 0.166453 | baseline |
| Fixed codec | 0.148043 | 11.1% lower |
| Adaptive oracle | 0.131228 | 21.2% lower |
| Adaptive plus 300 extra reasoning/output tokens per edit | 0.174968 | 5.1% higher |

These streams exclude shared discovery, test output and final prose. Complete
task percentages would be smaller. The three clone fixtures favor the codec
and are not a representative frequency estimate. Scoped token renaming is not
binding-aware refactoring; nested shadowing and dynamic references need stronger
checks or fallback. Parsing does not prove semantic correctness.

### Implementation route

A local tool adapter must hold source receipts, expand operations, preserve
native approval/review and return the ordinary diff. Keep IDs scoped to account,
workspace and session. The Pi 5 proxy can advertise/route this tool, but it cannot
edit a user's local repository by itself. Both Codex and Claude Code need an
opt-in client tool integration and compatibility evaluation. General code
completion that creates new logic will frequently use the native fallback.

## 2. Dependency slices at a cold context boundary

### Algorithm and proposed extension

At an already necessary context rebuild, retain exact source for the active
symbol and its statically identified dependencies. Attach path, source hash and
line range; list omitted symbols as retrievable. Preserve user requirements,
decisions and active tool pairs separately. Rehydrate when a required dependency
is unresolved, dynamic or changed.

The proposed controller selects a slice only when:

```text
expected avoided input/write + later cache reads
    > changed-prefix writes + recovery calls + extra output + local work
```

That gate must distinguish a cold prefix from a warm one and use this account's
actual model/cache categories. A blanket context rewrite can convert cheap cache
reads into costly writes. Source hashes certify byte provenance and the edges
that were checked; they do not certify that all information required to solve
the task is present.

### Prior art and novelty boundary

[ReCAP](https://arxiv.org/abs/2609.40118) persists importance and dependency links
for history selection at cold restoration. Its native attention interface
requires query/key information or hidden states. We cannot assume those signals
are exposed by hosted Claude or OpenAI models. This prototype instead follows
visible Python AST names/imports, using known entrypoints. It is not a
reproduction of ReCAP or evidence of equivalent quality. The integration
question is whether inexpensive source provenance and observed tool dependencies
can support a useful economic gate without provider attention access.

### Experiment and controls

Seven real source entrypoints are given to the selector: keepalive eligibility,
identity, cache-region pricing, response identity, stratification, schema
headline and TTL break-even. Entry selection is an oracle input; there is no
natural-language localization benchmark.

* Whole-source packs: **27,806 tokens**.
* Dependency slice plus metadata: **10,207** (63.3% less).
* **30/30** literal/support checks; entry-only selection gets **19/30**.
* Exact snippets and changed-file hash invalidation checked for all seven tasks.
* The same narrow selected source without receipts needs **9,137** tokens.
  The proposed pack adds **11.7%** metadata overhead to this strong control.

Two hypothetical cost profiles are included, with USD/M rates:

| Profile label, not billed model | Write | Read | Fresh | Output |
|---|---:|---:|---:|---:|
| `anthropic_like_1h` | 6.00 | 0.30 | 3.00 | 15.00 |
| `openai_like_4x_cache_discount` | 2.00 | 0.50 | 2.00 | 8.00 |

The 20-turn model includes 4,096 common instruction tokens, 256 new input and
400 output per normal turn, plus one charged recovery cycle retrieving 10% of
full source and generating 100 tokens. Earlier appended content is reread.

| Scenario | First profile | Second profile |
|---|---:|---:|
| Cold boundary, versus whole source | 7.8% lower | 8.5% lower |
| Replace an already warm source pack | 0.8% higher | 6.2% lower |
| Cold boundary with three recovery cycles | 1.2% lower | approximately break-even |
| Receipts versus already narrow evidence, no recovery in either arm | 0.8% higher | 0.9% higher |

The actual dependencies are a partial static closure: attribute dispatch,
reflection, decorators and runtime configuration can escape it. No model has
solved a task with the slices. Large histories are the plausible opportunity,
but these small source fixtures do not establish savings on a 500k-token session.

### Implementation route

The proxy can select visible history snippets, but reliable workspace lookup and
rehydration need a client index/tool. Build on existing CCR and read-lifecycle
machinery rather than duplicate it. Preserve frozen cache prefixes until a
documented rebuild boundary. Test separately with Codex and Claude clients;
already narrow reads should remain the default strong baseline.

## 3. Guarded tool plans

### Algorithm and proposed extension

An agent emits one small read-only plan: locate a symbol, fetch its definition,
find explicitly referencing tests, then retrieve selected test bodies. A local
interpreter executes dependencies and returns one evidence bundle. File hashes
and a manifest receipt detect concurrent changes; ambiguity returns a replan
request. No mutation operations are in this prototype's grammar.

The proposed extension is to combine bounded execution with snapshot validation
and an account-specific round-trip cost gate. Select it only if the current
client would otherwise return to the model between dependent read operations.
Already batched scripts are a valid baseline, not a second savings opportunity.

### Prior art and novelty boundary

[LLMCompiler](https://arxiv.org/abs/2312.04511) plans and schedules function calls
with dependencies. [Anthropic Programmatic Tool Calling](https://www.anthropic.com/engineering/advanced-tool-use)
lets a model orchestrate tools in code and restrict what reaches its context.
Batching and compilation are established techniques. The specific snapshot
receipt and fail-closed evidence wrapper are the proposed product experiment,
not a claim to invent programmatic tool use.

### Experiment

Eight real repository queries produce identical evidence and structured answers
under sequential and compiled execution. Five guard cases block results: source
change, deletion, a new test file, unknown symbol and duplicate symbol. The test
finder only recognizes explicit substring references; it does not find every
semantically relevant test.

At a 20k common prefix, warm after the first request, using hypothetical rates
$3/M uncached input, $0.30/M read, $3.75/M write and $15/M output:

| Strategy over eight queries | Modeled USD |
|---|---:|
| Sequential | 0.875063 |
| Dependency-wave batching | 0.772064 |
| Guarded plan | 0.729484 |
| Already one script | 0.703984 |

The plan is **16.6%** below sequential and **5.5%** below wave batching,
but **3.6% above** one script. Add 700 hypothetical planning/reasoning output
tokens per query and it becomes **5.4% more expensive than wave batching** even
without guard failures. Smaller prefixes also weaken the benefit. The no-cache
scenario produces a larger saving against sequential calls, but still fails to
beat one script. Do not assume all current coding traffic is uncached.

### Implementation route

Use a client-side bounded tool interpreter with operation allowlists and
explicit branch/size limits. Stream only verified results. Preserve permissions
for the underlying tools. A remote proxy cannot execute local read plans alone.
Codex workflows that already compose operations in one script have little
incremental economic opportunity here; Claude workflows need the same baseline
check. The test did not measure actual model-generated plans or hidden reasoning.

## 4. Observation deltas with state receipts

### Algorithm and proposed extension

Keep the first observation intact. For a subsequent observation of the same
resource, send changed line ranges with a base hash and reconstruction hash.
Verify reconstruction locally, retain retrievable full versions and send a full
base after context compaction. Preserve historical messages already in the
provider cache; introduce deltas only for newly appended observations.

The proposed controller gates on repeated-resource frequency, patch length,
base availability, cache lifetime and the measured recovery rate. Sending an
opaque reference without available source would transfer work to retrieval
rather than eliminate it. A receiver that expands the packet into full text
before the model sees it saves transport bytes, not model input tokens.

### Prior art and novelty boundary

Delta encoding and ordinary diffs are established. [DTOC](https://arxiv.org/abs/2609.26121)
provides externally stored, retrievable tool outputs and agent-controlled
visibility. Its small benchmark also reports different behavior across model
families, including regressions. This supports testing retrieval cost and quality
rather than assuming every smaller context is beneficial. Our extension is
append-only version deltas and base-validity receipts, not a new invention of
external memory or reversible compression.

The existing `horizon/transforms/read_lifecycle.py` already handles stale and
superseded reads while protecting frozen prefixes. Its documentation records a
previous repeat-dedup prototype removed after a low observed duplicate rate.
That is direct local prior work and a reason to demand a new workload census.

### Controlled experiment

There are 20 observations each for three repository copies with synthetic
one-line revisions, 300-row test diagnostics, diagnostics with resets every
five observations, tiny outputs and high-churn random output. They are not
recordings of genuine agent behavior. **140/140** reconstructed exactly; missing
base, stale base and corrupt hash are rejected.

The stream model uses a 20k prefix, 400 output tokens per observation, warm
cache after first/reset, and $3.75/M write, $0.30/M read and $15/M output.

* Large repeat scenarios: **22–63%** below full-observation streams.
* Native unified diffs are **1.3–4.2% cheaper than receipt deltas** on these
  repeat scenarios. Receipts buy verification, not superior compression.
* Tiny/full-churn controls increase cost approximately **0.7–1.9%**.
* Sensitivity charges 50 extra output tokens each turn and a full retrieval
  every fifth turn; those overheads reduce favorable savings considerably.
  These are assumed recovery rates, not measured model behavior.

### Actual local transcript census

The available ContextShrink Claude project folder contained **three JSONL
files**, including nested sessions, with **2,009 unique tool results** and
**736,152 emitted result tokens**. This is a new local sample, not the earlier
52-transcript corpus. The script exports aggregate counts only.

Among `Read`, `Grep` and `Glob` observations:

* 103 eligible observations; **121,341 tokens**.
* Five repetitions of the same tool name/arguments, of which three returned
  identical text. Compaction boundaries reset base availability.
* Always wrapping in receipt packets would increase these tokens to **140,548**.
* An oracle choosing the smaller of raw/packet emits **119,657**: just
  **1,684 tokens / 1.4%** fewer among eligible reads, or **0.23%** of all
  emitted tool-result tokens in this sample.

This is not the provider request history and does not subtract current proxy
compression. Overlapping reads with different arguments are missed, and
repeated later cache-read exposure is not priced. It is a workload suitability
census, not a dollar estimate. It contradicts a general claim of substantial
additional repeat-read savings for this local sample.

### Implementation route

Proxy-only text transformation is possible for visible tool results, but reliable
base storage, rehydration and client awareness are required. Integrate with CCR
and current read lifecycle. Both clients can be investigated; neither has been
shown to reason reliably from these packets. Prioritize only customers whose
diagnostic/source observations are demonstrably repetitive.

## 5. Bounded local repair search

### Algorithm and proposed extension

For a narrow failure class, enumerate a capped set of one-site literal/operator
mutations locally, execute visible tests and return a candidate only when one
candidate passes. Send a short receipt or fallback to the agent. A future
controller should stop searching when local compute, verification and review
exceed the expected avoided model retry cost.

This replaces some inference-based micro-repairs with cheap deterministic work;
it does not perform arbitrary code completion. The experiment is suggestion
generation only. The selected candidate still needs agent/user review and
adequate independent verification.

### Prior art and novelty boundary

[Counterexample-guided program repair](https://arxiv.org/abs/2502.07786) combines
fault localization, synthesis and a verification loop. [RepairAgent](https://arxiv.org/abs/2403.17134)
investigates autonomous tool-using LLM repair. Local mutation search and finite
test validation have extensive prior art. The proposed contribution is a
restricted, economically bounded repair tool beside this proxy, not an
invention of program repair or a proof from passing tests.

### Experiment

Ten authored toy functions cover a discount denominator, milliseconds conversion,
percentage uplift, area arithmetic, two boundary comparisons, multi-edit/new
logic cases, ambiguity and an overfitting control. They intentionally favor
simple mutation repair and do not estimate the prevalence of these bugs.

* **83 candidate executions**; cap 80 candidates per function.
* Six unique candidates pass the separate withheld examples.
* Three cases fall back because no candidate or multiple candidates pass.
* **One unique candidate passes visible tests but fails withheld behavior**:
  changing `/10` to `/100` matches positive examples but misses a requirement
  to clamp negative inputs. Uniqueness is not correctness.

An early rerun exposed Python AST operator singleton aliasing, which could
change two `+` sites together. The prototype now changes one AST field by its
path. The unsupported multi-edit case then correctly falls back. The recorded
results are from the corrected enumerator.

The optimistic retry model assumes each of the six genuinely correct repairs
avoids one request containing 40k cached tokens, 500 new tokens and 600 output
tokens. At the hypothetical rates used above, one retry is **$0.022875**.
Ten baseline retries would cost **$0.22875**; six avoided retries minus the
receipt write cost yield **$0.13485** before local compute, review, additional
context exposure and the false-acceptance loss. This **58.95% retry-component
ceiling is not a measured task/bill reduction**, and its assumed baseline has
not been validated with a model.

### Implementation route

Run beside the user's repository through an authorized client companion.
Production evaluation needs isolated processes, timeouts, resource limits,
clean snapshots and a read-only candidate workflow. This toy executor is for
its own authored arithmetic functions; it is not a sandbox for arbitrary code.
The Pi 5 proxy cannot run a user's test suite without that companion. Codex and
Claude integrations are both possible research directions, with the same
verification limitations.

## Product priorities and genuine dollar opportunity

1. **Pilot edit instructions first.** Restrict to binding-safe literal changes
   and repeated catalog/template edits. Keep ordinary patches available. Measure
   complete API usage, including reasoning and failed-format retries.
2. **Evaluate cold-boundary slices on long tasks.** Compare against existing
   compaction and already narrow dependency reads, not only whole history.
3. **Offer guarded plans selectively.** Retain one-script execution as control.
   Plans should justify themselves with real avoided model calls.
4. **Deprioritize generic repeat deltas in this sample.** The census does not
   show enough repeated identical requests. Diagnostic-heavy customers may differ.
5. **Keep repair search experimental.** A demonstrated false acceptance rules
   out automatic rollout based only on unique passing candidates.

For any feature, estimate monthly impact using:

```text
eligible component spend × eligible workload fraction × net component reduction
    − additional API calls − local infrastructure − recovery/review cost
```

Example only: if edit output is 10% of a $200 API bill, half of it is eligible
and the net edit-output reduction is 60%, the gross opportunity is **$6**, not
$120. To save $40 on that bill, the feature must act on a much larger component
or eliminate full requests. Component token percentages alone cannot support
a headline about the user's total dollars.

The existing local feature-research report points toward large history/cache
spend in one long Claude session. That motivates cold-boundary research, but
its earlier results are not evidence that this new slice controller succeeds.
An exact response cache already exists in `horizon/proxy/semantic_cache.py`;
proposing ordinary response caching again would not be additional functionality.

Do not feed these modeled amounts into `savings_usd`, subscription estimates or
Pro invoices. A rollout needs separate measured usage and labeled counterfactual
estimates, with no duplicate credit for overlapping methods. Charge any repeat
input benefit at its actual request category; do not accumulate removed tokens
at full input price. No savings or billing code was changed in this work.

## Next evaluation that would justify implementation

Prepare a fixed task set spanning simple edits, repeated structural edits,
cross-file refactors, new logic, dynamic dependencies and long sessions. Keep
the same model/configuration and starting repository in each arm. Establish the
deployed proxy as the baseline; include native compact edits, narrow reads and
one-script tool execution. Measure success with tests unavailable to selectors,
plus independent review of requirements not captured in those tests.

Use several repeats, prespecified quality gates and complete usage categories.
Report cost per successful task, regressions, recovery calls, cache misses and
billable reasoning. Isolate each feature before testing a combined policy.
Long-context cost effects need long tasks; short tasks can make rewrites look
worse simply because there are too few later requests to amortize them.

No paid/live run was started in the initial five-method study. Before a live study, specify its task set,
models, maximum dollar budget and provider credentials through secure
environment configuration. This is the remaining evidence needed for a claim
of substantial user savings, not a missing offline implementation step.

### Follow-up experiments

The user subsequently authorized an existing-model compact-edit pilot with a
$3 total cap and explicitly approved live testing of the prepared source
fixtures. See [pilot/README.md](pilot/README.md) and
[pilot/RESULTS.md](pilot/RESULTS.md) for the live outcome, baseline limitations
and API-equivalent cost accounting. These later calls are separate from the
offline results above.

The [large cold-restore investigation](context_slice/COLD_RESTORE.md) examines
100k/250k/500k local history windows. It remains offline: economic projections
do not demonstrate long-task correctness or incremental savings over narrow
context retrieval.

## Reproduce and review

From the repository root with the existing Python environment and cached
`tiktoken` assets:

```powershell
.venv/Scripts/python.exe experiments/frontier-savings/edit_codec/prototype.py
.venv/Scripts/python.exe experiments/frontier-savings/context_slice/prototype.py
.venv/Scripts/python.exe experiments/frontier-savings/tool_plans/run.py
.venv/Scripts/python.exe experiments/frontier-savings/observation_delta/run.py
.venv/Scripts/python.exe experiments/frontier-savings/repair_search/run.py
.venv/Scripts/python.exe experiments/frontier-savings/audit.py
# Optional private census: aggregate only; requires local Claude project logs.
.venv/Scripts/python.exe experiments/frontier-savings/observation_delta/transcript_census.py
```

Each prototype writes `results.json` inside its own directory. `audit.py` writes
`audit_results.json`; the census writes `transcript_results.json`. Edit fixtures
are copies of repository source, not production modifications. Corpus hashes
and source hashes identify the observed inputs; changing the repository/logs
changes results. Edit results retain their fixture creation date of October 7;
the report and source hashes describe the October 9 rerun.

For Claude's review, scrutinize: the oracle entrypoints and format selector;
whether dynamic dependencies invalidate source coverage; singleton-safe mutation
enumeration; representative workload frequency; the gap between byte correctness
and task correctness; cache-write assumptions; reasoning and recovery overhead;
and comparison with the already deployed pipeline.
