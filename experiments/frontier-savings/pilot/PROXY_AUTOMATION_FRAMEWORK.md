# Automatic compact edits in the ContextShrink proxy

October 9, 2026. Architecture review and implementation framework.

**Status: adapter core implemented; native handler integration and deployment
remain pending.** `horizon/proxy/compact_edits/` contains the compiler,
Claude/Codex complete-JSON adapters, selective controller and durable replay
journal. Proxy startup/outcome/stats include content-free observation. Stock
forwarding remains native; no client route is certified or activated. See
[implementation notes](../../../docs/compact-edit-adapters.md).
The live MCP pilot proves adoption of a small grammar. It does not yet prove
native approval compatibility, transparent client execution or savings against
native scripts. Streaming/recovery/fleet behavior below remains a release design.

## 1. Recommendation

Keep an automatic decision controller active in the proxy, with **no end-user
feature toggle and no feature-specific confirmation**. It evaluates eligibility
and serves ordinary edits whenever compact generation is unsuitable. An operator
can disable or drain a faulty release. Existing client permissions and file-edit
approvals continue to apply; this feature cannot promise zero approval prompts
when the user's coding client normally requires them.

The first production candidate is **repeated literal catalog-row creation**.
Tiny replacements, replace-all changes and new logic use native tools. The
live results justify this distinction:

| Live task | Compact cost relative to exact search/replace |
|---|---:|
| Four cloned catalog rows | 40.2% lower |
| Twelve rows, adequate output limit | 60.7% lower |
| Four other task types combined | 13.7% higher |

These are selected-task API-equivalent estimates from a Claude Pro login, not
observed user invoices. There were three repetitions per task. Full-workflow
economics and native scripted transformations remain necessary controls.

**A proxy-only route is plausible for a compatible native edit tool.** The
proxy must change what the provider sees *before generation*, then expand the
compact operation into an ordinary client tool call. Compressing an already
generated edit does not recover its output cost.

This extends the pilot's earlier client-MCP recommendation: a managed local
bridge is one implementation; a transparent protocol adapter is another. The
second fits the requirement for automatic operation without user configuration.
Its bounded adapter core now exists; neither bridge is integrated into live
customer forwarding yet.

## 2. Three roles, with file execution staying on the client

| Component | Responsibility |
|---|---|
| Pi 5 proxy | Admission, source snapshots from traffic, provider tool schema, deterministic expansion, protocol translation, replay journal, costs and automatic fallback |
| Provider model | Emit a short supported operation or choose the existing native edit tool |
| Existing coding client | Show the ordinary expanded diff, enforce its normal permissions, apply the native edit and report actual success/failure |

Pi 5 normally has no access to a user's local workspace. The proxy neither runs
filesystem commands on that device nor invents successful tool results. If it
cannot expand a change against a trustworthy observed snapshot, it cannot
perform the optimization for that change.

Suggested first adapter: a **certified Claude Code `Read` + `Edit` contract**.
Names alone or a user-agent string are insufficient. Match a versioned schema
fingerprint and verify actual read decoding, edit matching, failure and approval
behavior in an integration benchmark. MCP tools named `Edit` may behave
differently; unknown contracts get ordinary passthrough.

Implemented core provider-facing virtual tool: `horizon_compact_edit_v1`. Keep the
client's native tools available. Only the provider sees this additional tool;
successful calls are translated back into an existing client `Edit` call.
The client must never receive an unregistered virtual tool call.

## 3. Automatic use-case policy

All rows below are subject to protocol, source, quality and cost gates. The
existence of a matching keyword in a user request is only a candidate signal.

| Use case | Automatic policy | Evidence / reason |
|---|---|---|
| Add 4–16 rows by copying an existing literal-only `ModelPricing` template; change only each key and `model` field | First release candidate, after native integration and economic certification | The only substantial live positive case; exact deterministic expansion exists |
| Repeated JSON/YAML configuration records, route declarations or simple fixture rows | Observe and qualify later; ordinary tools initially | Plausible analogous benefit, but grammar, comments, order and native-script baseline were not tested |
| One literal or module constant | Ordinary edit | Native tool payload is already short; live costs increased |
| Change five identical keyword values | Native replace-all | Compact added no total-cost advantage in the pilot |
| New algorithm, function body or behavior | Native patch/edit | No deterministic shorthand for novel logic; fallback adds overhead |
| Cross-file rename, imports with dynamic behavior, computed templates | Native tools | Current grammar cannot establish semantics or a safe expansion |
| Truncated/partial/annotated read with no certified decoder; binary file; unknown encoding | Native tools | Exact source is unavailable |
| File changed since observation; concurrent writes; duplicate target keys; ambiguous template | Native tools or bounded automatic recovery before publication | Snapshot or operation cannot be verified |
| Existing native script already makes the same repetitive change cheaply | Prefer the existing workflow | A scripted clone is a stronger baseline than emitting repeated source text |
| Unknown client, unsupported tool grammar, unsupported transport or model | Ordinary passthrough | No compatibility guarantee |
| Long-history cold restore | Separate research policy; no automatic slicing in this release | Old-decision/task-quality preservation remains unproven |

Do not extrapolate the pilot's `ModelPricing` codec to arbitrary constructors.
Initial eligibility must verify its literal template/import structure, unique
keys and bounded clone count. Other forms need their own certified compiler.
The model chooses the user-requested values; the proxy must not derive new
business logic or alter unrelated fields.

## 4. Two admission decisions, made at different times

### A. Session admission: before changing the provider tool catalog

Only admit a new session or a verified cold boundary when:

1. Its authenticated account, conversation and workspace scope are identifiable.
2. The provider, model, client tool contract and transport have a certified
   adapter. The selected model must be the actual routed model.
3. The route has a validated, positive full-session cost profile, including
   sessions that never use the compact tool. A first user request describing a
   repetitive task can strengthen admission, but cannot establish quality alone.
4. Tool-schema and receipt overhead, recovery risk and any prefix rewrite cost
   fit a conservative expected savings margin.
5. The replay journal can be retained for the supported session/resume lifetime.

Pin the virtual tool definition and codec version once admitted. The proxy's
existing eager/sticky retrieval-tool policy recognizes that adding tools midway
through a warm Anthropic session can invalidate its prefix. Do not repeat that
mistake here. A newly noticed opportunity in an unadmitted warm session normally
waits for the next genuine boundary; idle duration alone does not prove one.

Admission need not mean use on every turn. Stable tool availability and selective
operation use are separate. Count the schema cost even when the model never
chooses compact edits. If that cost defeats sparse-use sessions, do not admit
that workload. Do not assume provider tool deferral works on every route.

### B. Operation eligibility: after a source read, before generation

Capture the original client read result before compression. A certified decoder
must recover exact source bytes and path provenance, not guess through line
numbers or truncated output. Protect the snapshot from existing source-folding
transforms. Preserve source text; provider-only receipt metadata is extra input
whose cost is counted.

Resolve the target path only from the receipt's observed read record. Bind
receipts to authenticated tenant, conversation, workspace, source hash and codec
version; model-supplied account identifiers or alternate paths cannot override
that binding. Do not infer workspace identity merely from a repeated filename.

Use a deterministic candidate detector: supported template, bounded repeated
request, unique targets and a certified native edit destination. Unknown intent
uses native tools. Do not add a paid classifier call merely to select the codec.

Give the model concise guidance to use the compact tool only for an eligible,
profitable repetition. Never force every turn's `tool_choice` to compact: that
would interfere with reads, other tools and ordinary answers.

The model can still ignore the hint or attempt an ineligible operation. The
proxy must handle both. This policy is a routing aid, not a guarantee that a
model will select the cheapest possible tool every time.

If the model has already emitted a valid compact edit whose estimated benefit
is now small, expansion usually remains the cheapest remaining action. Do not
spend another model call regenerating native text solely to enforce a cost
threshold after generation. Record the miss and adjust future admission/use.

## 5. Successful request lifecycle

```mermaid
sequenceDiagram
    participant C as Coding client
    participant P as Pi 5 proxy
    participant M as Provider model
    C->>P: Request with native tools and Read result
    P->>P: Capture exact snapshot; check eligibility and cost
    P->>M: Stable native + compact tools; receipt for observed source
    M->>P: Compact operation against receipt
    P->>P: Buffer, validate, expand; durably journal both representations
    P->>C: Ordinary native Edit with expanded source and same call ID
    C->>C: Normal permissions, review and file application
    C->>P: Actual Edit success or error
    P->>P: Invalidate snapshot; reconcile replay and all costs
    P->>M: Provider's original compact call history + actual tool result
```

Example provider-only operation, conceptually:

```json
{"receipt":"r7","operation":"clone_model_pricing","template":"claude-3-5-sonnet-latest","keys":["new-a","new-b","new-c","new-d"]}
```

The client sees an ordinary call with its existing `file_path`, `old_string`,
`new_string` and `replace_all:false` contract. Those keys are an adapter example,
not a universal schema. The native client performs the edit; the proxy only
prepares the arguments.

For initial safety, expand to a **whole-file exact replacement guard** when
the certified native matcher and payload limit support it. Matching the complete
observed file prevents application after an unseen external change and protects
key uniqueness checked against that snapshot. A receipt hash by itself does not
check the current disk. If the client uses fuzzy/normalized matching that cannot
enforce the necessary guard, this transparent adapter is ineligible; a managed
local bridge with an actual version check is required.

Sending the larger expanded arguments over the user's network does not incur
model output tokens. Replayed expanded arguments would incur future input cost,
so the upstream replay translator is essential. Its benefit must be compared
with the minimal native edit or script, never with an artificially inflated
whole-file replacement baseline.

Only one target file and one published native edit are supported initially.
Parallel dependent edits, whole-file `Write` without a version guard, batched
application and multi-file transactions remain ordinary workflows. After any
successful edit, invalidate the old receipt and require a new observed read for
a further compact edit. Do not assume success proves what bytes are now on disk.

## 6. Replay journal and provider/client history separation

The client records the expanded native call; the provider originally generated
the compact call. Keep both in a durable journal, bound to:

* authenticated account/tenant and key context;
* conversation identity, workspace scope and adapter version;
* provider, actual model, response ID and tool call ID;
* source hash, operation, expanded-arguments digest and execution state;
* exact provider-facing catalog and history segment needed for replay.

Before request compression, recognize only an exact journaled client call and
restore its provider representation. Never heuristically turn arbitrary past
edits into compact operations. Keep call IDs and real tool results paired.
Normalize this history before prefix comparison/freezing and replay; otherwise
existing cache machinery will freeze the wrong representation or undo the map.
Capture raw source before any compression while measuring costs on the final
provider-facing request. This requires explicit handler integration, not a
late hook that modifies an already frozen prefix.

Persist the journal before exposing a translated call. A restarted proxy must
still be able to replay it. Active mappings cannot be evicted merely because an
LRU or TTL expired. Stop admitting sessions when state capacity is insufficient;
drain existing ones with their catalog and journal intact.

Unexpected history mutation, branched conversation or missing journal requires
an explicit recovery policy. A validated full-history cold rebase to native
forms may be possible on a supported stateless route, with its rewrite cost
counted. It is not generally safe for signed/opaque or server-side histories.
Such routes must be excluded until journal loss/resume recovery is demonstrated.
Never silently reconstruct encrypted reasoning or invent missing tool results.

## 7. Streaming and automatic error recovery

### First implementation: buffered activated turns

Anthropic tool arguments arrive as partial JSON; the complete input is assembled
when the tool block closes. Thinking signatures are separate integrity data.
The proxy must preserve them and all opaque fields. These constraints are
documented in [Claude streaming](https://platform.claude.com/docs/en/build-with-claude/streaming)
and [fine-grained tool streaming](https://platform.claude.com/docs/en/agents-and-tools/tool-use/fine-grained-tool-streaming).

Initially buffer the **entire activated response** until all translated calls
are validated. Return its ordinary client JSON or reconstructed SSE envelope.
This allows recovery before any client tool execution. It adds first-token
latency; measure and bound it rather than calling it invisible. Set response
size, timeout and concurrency limits before admitting the route.

Never forward compact argument fragments to a native client. Never send both
compact deltas and an expanded final item. Preserve block indexes, IDs, stop
reasons and provider usage. Future per-block translation needs a separate
certification for partial exposure, parallel calls and disconnects. Existing
`stream_safe=True` request hooks are insufficient: they have no streamed
response-expansion guarantee.

### Failure rules

| Failure | Automatic behavior |
|---|---|
| Ineligible before provider request | Ordinary native path, no repair call |
| Model chooses a native tool | Ordinary passthrough; record compact schema overhead |
| Valid compact operation | Validate, journal, expand and return native call |
| Invalid compact operation, still entirely private | At most one internal recovery call if the reserved recovery/latency budget permits; request native fallback |
| Recovery succeeds | Publish only the final valid native response; count both provider calls |
| Recovery is exhausted or unavailable | Return an accurate tool/protocol failure; never leak an unknown virtual call or invent a safe edit |
| Client refuses or fails the native edit | Return the actual result upstream; invalidate receipt; use ordinary recovery thereafter |
| Client disconnects before publication | Do not initiate file execution; count provider cost already incurred |
| Client disconnects after publication | Execution state is unknown until real client results arrive; never automatically reissue the edit |

Internal recovery must append the original provider assistant turn and a valid
error tool result with the same call ID. Any hidden rounds must remain in the
provider replay journal in their exact order. Use the pinned tool catalog for
recovery; changing it could add a cache rewrite. No extra filesystem tool is
executed by the proxy. Count every internal attempt, including errors.

This is a major unimplemented part of the design. Network or exhausted-budget
failures can still surface as normal client errors; no architecture can promise
unconditional silent success. A cohort with frequent repair is automatically
removed from new admission. Existing sessions retain translation while draining.

## 8. Cost gate: dollars per successful workflow

Use current provider/model/service-tier prices from the pricing registry and
actual upstream usage categories. Unknown or stale prices block new admission.
Estimate before generation using a calibrated workload profile:

```text
expected_net_savings =
    expected_cost_of_best_available_native_workflow
    - expected_cost_of_compact_workflow

compact_workflow cost includes:
    normal input + schema/receipt input at their actual cache category
    + output and any billable reasoning
    + recovery calls + cache rewrites + future replay input
```

Native cost must include cheap replace-all and scripted transformations where
the client can use them. No flat 40–61% discount is applied to every request.
No full-price re-counting of historical removals is allowed.

Suggested initial admission margins, **design values requiring calibration**:

* A conservative lower estimate of net benefit exceeds **$0.003 per candidate**
  and **15% of that native workflow's cost**.
* Session benefit also exceeds all stable catalog costs on non-use turns.
* A route-specific recovery reserve and latency bound are available.
* Quality and economics are certified for this exact model/adapter/task class.
  The current three-repeat pilot cannot certify these confidence bounds.

These values must be recomputed per model and price tier. Four rows is not a
universal economic threshold. A cheap model, a small row, extra reasoning or a
native one-command clone can eliminate the benefit. A controller that often
skips is working as intended if it avoids raising the user's bill.

### Illustrative dollar benefit

At the pilot's API-equivalent costs, one four-row task saved approximately
$0.0102; one twelve-row task saved $0.0282. **1,000 tasks resembling the latter
would imply about $28.17 of avoided API-equivalent cost**, before differences in
real session admission, scripts, cache state and production recovery. This is a
scenario, not a forecast or an amount to add to an account's savings.

For a metered API customer, a validated reduction in provider-billed tokens can
reduce spend. For a fixed-price coding subscription, it may reduce quota usage
or delays without reducing the subscription invoice. Account dashboards need
to distinguish those outcomes rather than presenting both as realized dollars.

## 9. Measurement, account attribution and billing

Measure actual provider spend for every turn, whether the feature succeeds,
fails or is never used. Expanded client-side text is **not provider-generated
output** and must not overwrite upstream usage or an output estimator. Otherwise
the proxy would charge/report tokens the provider never generated.

Capture usage and any fallback output estimates from the **original upstream
response before expansion**, then carry those immutable measurements through
translation. If provider usage is missing, count upstream compact text, not
the re-rendered expanded SSE body. Mark the estimate's basis explicitly.

Maintain separate signed observations for:

1. Provider actual cost and token/cache categories.
2. Compact versus expanded payload token counts, labeled local estimates.
3. Estimated native-workflow counterfactual and its confidence/quality basis.
4. Feature overhead and recovery/cache costs, including losses.
5. Net estimated savings and confirmed edit outcome.

Expanded-versus-compact payload counts do not measure what the model would have
generated natively, including reasoning or scripts. The current output-savings
module already recognizes this counterfactual problem. Preserve that distinction.
Sum signed differences before reporting totals; clamping losing requests to
zero would manufacture savings.

Use tenant-bound event IDs and the existing outcome funnel. Add an explicitly
estimated `compact_edits` attribution with `realized=False, estimated=True`
until the measurement standard is established. Attribution is descriptive;
`savings_attribution.py` does not itself add to headline totals. The current
account ledger sums compression, retained, keepalive and priced policy amounts;
a new feature requires deliberate integration, not an assumed automatic credit.

There is a relevant integration gap: `record_account_outcome` currently leaves
`cost_usd` and successful usage fields at zero when the client-facing HTTP
status is 400 or higher. That cannot represent a failed compact/recovery
workflow that already consumed charged provider output. Add actual internal-call
cost events or equivalent canonical aggregation with exactly-once semantics,
regardless of the final client status. Only charge known usage; missing usage is
unknown, not automatically zero. Do not double-count an internal call both as
its own event and inside a final aggregated outcome.

Because the Pro fee follows reported savings, **do not place these experimental
output estimates in `savings_usd`, Profile Est. savings or the billing base**.
First establish the estimation/qualification contract and reconcile end-to-end
account events. Avoid overlap with output shaping, schema compression and
retained input; one avoided token/cost component receives one attribution.
Negative overhead remains visible and must reduce any eventual net benefit.

Suggested operational counters: eligible candidates, admission/skips by reason,
native selections, compact selections, validation failures, real edit failures,
recovery attempts/cost, journal misses, cache writes, latency and signed net cost.
Use bounded labels. Do not put account IDs or file contents in global metrics.
Store only the source state required for translation, with tenant isolation and
bounded retention; logs use digests and counts.

## 10. Integration points found in this repository

| Existing code | Proposed integration / limit |
|---|---|
| `horizon/proxy/extensions.py` | Operator-installed controller extension is a possible packaging route; current extensions are explicitly enabled by operators |
| `horizon/proxy/turn_hooks.py` | Buffered request/response hooks can host initial schema/expansion orchestration; exception-skipping alone cannot recover a compact-only response |
| `horizon/proxy/handlers/anthropic.py` | Raw-read capture, history normalization before cache decisions, actual routed-model gate, response translation and internal-call usage accounting |
| `horizon/proxy/handlers/openai.py` | Separate Responses/function/custom/WS adapters; current handling of call IDs is useful but does not implement compact edits |
| `horizon/proxy/anthropic_wire.py` | Envelope parsing/rendering preserves opaque SSE fields; validate before relying on it for rewritten tool input |
| `horizon/proxy/session_engine.py` | Freeze/replay contract; must operate on normalized provider history, not expanded client calls |
| `horizon/proxy/tool_injection_policy.py`, `tool_injection_tracker.py` | Sticky/eager catalog concepts to reuse; new state additionally needs tenant/workspace identity and durable replay |
| `horizon/proxy/outcome.py`, `account_analytics.py` | Canonical usage/events; price upstream compact generation plus all internal attempts and keep estimated savings separate |
| `horizon/proxy/savings_attribution.py`, `output_savings.py` | Descriptive estimated attribution and counterfactual methodology; not automatic billing integration |
| `experiments/frontier-savings/pilot/adapter.py` | Restricted preview codec to extract into a pure compiler; lacks native tool execution/approval, durable state and streaming translation |

Proposed new modules: `compact_edit_policy`, `compact_edit_snapshots`,
`compact_edit_journal`, `compact_edit_compiler`, `compact_edit_wire`, and
`compact_edit_accounting`. Separate pure eligibility/expansion logic from
provider/client envelopes. Keep a versioned compatibility registry and an
operator-controlled admission/rollback mechanism. These names are a plan;
they are not existing production modules.

Do not register this as an unrestricted response-mutating middleware alone.
It needs ordering before cache preparation and coordination with final usage,
history snapshots, model routing and existing transforms.

## 11. Codex support

OpenAI function tools emit JSON arguments; custom tools emit string input and
may be constrained by a grammar. Responses streams also carry complete tool
items in addition to deltas; reasoning items returned alongside tools must be
preserved on continuation. See the official
[function-calling documentation](https://developers.openai.com/api/docs/guides/function-calling).

This establishes protocol requirements, not approval of response rewriting by
every Codex client. A Codex `apply_patch` custom grammar, function tool and
code-mode `exec` wrapper require different adapters. A function call cannot be
silently handed to a client expecting a custom string call. Reconcile event
types, item IDs, `call_id`, delta/done/final representations and output types.

Initial Codex policy is native passthrough. Later qualify one observed contract
at a time; do not execute or rewrite arbitrary JavaScript/shell text to simulate
an edit. `previous_response_id`, server-side conversations and WebSockets have
additional state/resume semantics and are excluded initially. A managed local
bridge may be needed where native version guarding or replay compatibility
cannot be established. It can be product-managed without an end-user toggle,
but it is still a real dependency that cannot be supplied by the proxy alone.

## 12. Implementation and release sequence

1. **Passive qualification:** capture bounded metadata/snapshots locally;
   identify actual tool contracts and candidate frequency. No schema rewrite,
   model call or cost saving is attributed to this phase.
2. **Native integration benchmark:** the same full tasks through the proxy and
   real client, including native scripts and edits. Cover approval denial,
   changed files, missing/truncated reads, duplicate keys, failed/truncated
   tool output, multiple calls, disconnects, restart and journal loss. Check
   byte preservation and independent behavioral outcomes, not AST alone.
3. **Buffered Claude cohort:** operator-approved narrow cohort after positive
   full-session economics, latency and quality. Automatic admission only for
   certified cases. Use conversation-stable controls on an approved evaluation
   corpus/cohort; never run duplicate paid customer calls just to form a baseline.
4. **Automatic monitoring:** per-model/adapter cost and failure monitoring,
   immediate admission stop on any integrity/tenant leak, and drain existing
   state. Disable new admission when recovery/failure or signed cost trends
   invalidate the qualified benefit. Specify operational thresholds from the
   benchmark; small pilot results are not statistical guarantees.
5. **Expand only after qualification:** additional grammars, efficient stream
   translation, then selected Codex contracts. Fleet activation follows positive
   representative economics rather than the size of a synthetic template win.
6. **Separate cold-restore project:** use a sanitized approved corpus and test
   older constraint/decision recall plus actual task completion against native
   compaction/narrow reads. No automatic history slicing based only on the
   70–92% economic scenarios in `COLD_RESTORE.md`.

Acceptance means the feature cuts total provider cost per successful workflow,
preserves the client's permission/application behavior, survives replay/resume,
and accounts for every failure and overhead component. There is no need for an
end-user switch, but there is a need for automatic conservative skips and
operator recovery controls.

## 13. Review questions for Claude / implementer

* Does the exact installed client's native matcher provide the whole-snapshot
  guard, including encoding/newline handling? If not, what local bridge is needed?
* Can the proxy identify a durable conversation/workspace without heuristic
  cross-session merging? What is the journal retention/resume contract?
* Where are provider-view assistant calls and client-view calls captured, and
  will prefix replay ever restore the wrong one?
* Do hidden recovery turns preserve reasoning, tool pairs and billing counters
  through errors, disconnects and process restart?
* Does an ordinary native script eliminate the observed output advantage?
* How often do real sessions use the tool, and does schema cost negate the gain?
* Are estimated output benefits isolated from the fee-bearing savings ledger?

Supporting evidence: [live pilot](RESULTS.md), [raw numeric analysis](analysis_results.json),
[cold restores](../context_slice/COLD_RESTORE.md), and [five-method research](../REPORT.md).
