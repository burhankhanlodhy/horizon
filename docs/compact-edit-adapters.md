# Compact edit adapters: Claude Code and Codex

October 9, 2026. Implementation notes for ContextShrink and subsequent Claude review.

## Status

The production package `horizon/proxy/compact_edits/` now contains a deterministic
compiler, complete-JSON wire adapters, a bounded durable replay journal and a
selective admission controller. It imports no experimental code or optional
dependencies. `HorizonProxy` always constructs the controller; the outcome
funnel feeds content-free client-family observations to it, and `/stats` exposes
its status. No end-user feature toggle was added.

**Native forwarding remains the stock runtime behavior.** The Anthropic and
OpenAI handlers have not been connected to a qualified translating route. No
native client version is certified, no client-local Codex SHA fence has been
installed, no provider schema is injected into ordinary traffic, and nothing has
been deployed to Pi 5 during this change. This is an implemented adapter core,
not an activated customer feature. Runtime observations do not establish client
compatibility or candidate frequency.

Checks for this change are Python syntax compilation, Ruff lint/format checks
for the adapter package and source/diff inspection.
There were no added or executed test suites, native-client integration runs or
new model calls. Earlier MCP pilot results do not validate these wire adapters.

## Supported formats

| Client contract | Provider-facing compact call | Client-facing native call | Replay |
|---|---|---|---|
| Claude Code-compatible Messages `Edit` | `tool_use`, name `horizon_compact_edit_v1`, JSON `input` | `tool_use`, name `Edit`, `file_path` / `old_string` / `new_string`, optional `replace_all=false` | Restore original compact input; retain `tool_result` IDs, contents and errors |
| Codex Responses custom `apply_patch` grammar | `function_call` with JSON `arguments` | `custom_tool_call`, name `apply_patch`, patch text in `input` | Restore original function call; convert actual `custom_tool_call_output` to `function_call_output` |
| Codex Responses function `apply_patch` | `function_call` with JSON `arguments` | `function_call`, name `apply_patch`, patch in the certified argument property | Restore original compact function arguments; retain actual function output |

Tool names alone are insufficient. `NativeContract.validate()` matches the
entire native definition's canonical SHA-256 fingerprint and checks the basic
argument shape. Certification must separately establish the grammar, native
matching, permissions, failure behavior and installed version. Namespace tools,
JavaScript/shell wrappers, the OpenAI built-in `apply_patch_call` operation,
streaming SSE, WebSockets, incremental `previous_response_id`/conversation and
background requests are outside this implementation. They use native tools.

The custom and function tool distinction follows the
[OpenAI function calling guide](https://developers.openai.com/api/docs/guides/function-calling).
The built-in patch operation has a separate envelope documented in
[OpenAI apply patch](https://developers.openai.com/api/docs/guides/tools-apply-patch).
Claude's call/result pairing follows
[Anthropic tool handling](https://platform.claude.com/docs/en/agents-and-tools/tool-use/handle-tool-calls).

## Compiler and use case

Only one file and one operation are allowed per admitted conversation: clone
**4–16 literal `ModelPricing` rows**, changing only each dictionary key and its
`model` string. A subsequent candidate needs a newly qualified conversation or
boundary. The original receipt-bound tool definition stays unchanged throughout
the admitted conversation, including after its operation is reserved.

The caller supplies a complete immutable `SourceSnapshot`, a workspace-relative
patch path, a separately trusted native edit path and a `Candidate` containing
the requested table, template and ordered keys. These are not model-selected
paths. Source comes from a certified complete-read decoder or managed local
bridge; arbitrary formatted/truncated `Read` or shell output is not an exact
snapshot. No source collector or heuristic intent classifier is activated by
this change.

The compiler accepts bounded UTF-8 LF Python source with a final newline. It
checks one top-level `dict[str, ModelPricing]` annotation, the known constructor
import, unique literal dictionary keys and scalar keyword values, matching
template key/model strings, no duplicate targets, no unpacking, no detected
rebinding, suitable comma/indent/closing-brace formatting and a bounded template.
It preserves original text and inserts copies with only two literal substitutions
per row. UTF-8 byte-based AST columns are converted to character offsets.

The expanded module must equal the independently constructed permitted AST.
The module contract still needs operator certification: AST checks do not prove
constructor purity, arbitrary import identity, comments' behavior or runtime
correctness. The core neither imports/evaluates candidate Python nor reads,
writes or executes a client's file.

Claude expansion uses the **complete original source** as `old_string`, with
the expanded source as `new_string`. Its certified matcher must reject any
stale source; whitespace/fuzzy fallback is insufficient. Codex receives a
workspace-relative `*** Begin Patch` / `*** Update File` patch. Its fuzzy patch
matcher alone does not establish a source guard: the managed client must compare
the SHA-256 and apply atomically under its normal edit/approval path. A proxy-side
hash or read immediately before an unlocked write is insufficient. This local
Codex guard is a remaining dependency.

## Admission and economics

`CompactEditController.prepare()` returns ordinary native input unless all of
these conditions hold for a new admission:

1. Account, conversation, workspace, provider and actual routed model have a
   trusted `Scope`. Request headers/user-agent strings do not establish it.
2. A current operator `Qualification` covers that exact route, tool fingerprint,
   workspace, relative and native paths, module contract and execution guard.
   The benchmark must
   include normal client approvals, source decoding, native replay and recovery.
3. The managed route is ready to enforce the certified guard when applying the
   native edit. `execution_guard_ready` is internal attestation, never a client
   HTTP parameter or model argument.
4. The transport is explicitly non-streaming complete JSON, full history is
   available, native tools are retained and parallel tool execution is disabled.
5. Source and intended keys pass the compiler; the reserved virtual tool name is
   absent from the client's catalog; tool selection is automatic.
6. Admission occurs at a certified cold boundary. Durable state preserves the
   candidate/catalog across later turns and process restart.
7. Full-session native **lower** cost minus compact **upper** cost is at least
   **$0.003 and 15%**. Bounds include schema/receipt input, unused admissions,
   catalog/cache changes, native result replay, guard and bounded recovery costs.
   A native script is part of the comparison. Pilot means are not such bounds.

The proxy never invents a qualification from an application name or a savings
estimate. Missing evidence means native operation. Failure during an already
admitted session raises a recovery requirement instead of silently removing its
catalog or replay state. A `resume_required` flag from the managed session
registry detects missing journal state; that registry is a future integration
requirement, not a client-controlled flag.

## Replay and delivery state

`ReplayJournal` requires an explicit absolute SQLite path in an existing secured
directory. Its default limits are 2,000 entries / 32 MB of payload, with a 2.1 MB
per-entry bound. It uses full synchronous commits and transactions. Live entries
are never evicted automatically. WAL/database overhead is additional to the
payload bound. The directory, database, WAL and backups contain source/native
arguments and need the application's normal tenant isolation, permissions and
retention controls. No journal is opened by stock startup or stateless operation.

Admission persists the original snapshot and stable catalog receipt. Before
publishing an expanded response, `record_call()` atomically reserves the one
operation and commits its provider/client mapping. Identity is scoped by account,
conversation, workspace, provider and model. A second reservation is rejected
even under concurrent workers. `restore_admission()` can reconstruct the original
candidate/contract after restart; it does **not** authorize re-execution.

`normalize_replay()` restores only journaled native calls whose operation
signature matches exactly. It preserves surrounding text, reasoning/encrypted
content, actual native output and call IDs. Complete Responses input item arrays
and Messages content arrays are supported. Mismatched arguments stop processing.
The Codex custom result type is translated back to the provider's function result
type. Client and provider prefixes must remain separate in the handler caches.

Journal commit proves the call was reserved, **not delivered or executed**. A
disconnect/crash between commit, delivery and native result needs reconciliation
with the real client. There is no automatic resend, reservation reset, fabricated
success, execution acknowledgement, session eviction or production drain manager
in this core. Operator cleanup is allowed only after the conversation is closed
and native execution is reconciled.

## Handler integration order still required

1. Obtain authenticated scope, actual routed model, an exact source snapshot and
   requested candidate; restore durable session state before handling continuations.
2. Run `prepare()` **before** session-engine prefix freezing, tool compaction and
   provider request/cache construction. It normalizes native replay and appends
   the stable provider-only tool. Store client/provider views separately.
3. Send the provider request through ordinary provider accounting. Buffer the
   entire response; expose no partial virtual tool or delta to the client.
4. Run `translate_response()` before client serialization. Preserve provider
   usage and original provider-view assistant items for cache finalization;
   publish only the expanded client view after durable reservation.
5. On invalid receipt/arguments, mixed or parallel calls, truncation, repeated
   operation or journal failure, recover internally within a qualified cost
   budget. Preserve reasoning/tool pairing. If recovery cannot complete, report
   an accurate failure. Never let a generic hook swallow the exception and
   forward the provider's unknown virtual tool. A recovery orchestrator and its
   all-attempt usage settlement are not implemented here.
6. Let the client enforce normal approval and the native source-version fence.
   Subsequent native results go through replay normalization. Reconcile uncertain
   delivery/execution before further edits and preserve the journal while active.

SQLite/compiler work is synchronous; an ASGI route must dispatch bounded work
off the event loop and serialize preparation/publication per conversation. The
generic late turn hooks cannot supply this ordering or durable fail-closed
recovery on their own. No late hook has been registered by this change.

## Savings and next qualification

No new billed or measured customer saving is credited. The earlier Claude MCP
pilot found 40.2% and 60.7% lower API-equivalent task costs for selected four- and
twelve-row cases; it did not measure native adapter execution, Codex, total
proxy improvement or invoices. The $3 research budget is unchanged; this change
made no model calls. See the pilot's `RESULTS.md` and `BEFORE_AFTER.md`.

Next work is a native-client benchmark and handler integration: exact installed
tool schemas, approved/read/edit behavior, stale files, partial reads, cost and
native-script controls, signatures/reasoning, custom/function result pairing,
restart, replay mismatch, concurrent calls and delivery uncertainty. Codex needs
the managed local source fence. SSE/WS need whole-envelope/event translation and
cache-continuation qualification; they must not simply reuse the JSON adapter.
Once those route qualifications exist, admission can be automatic for supported
cases while other editing continues through native tools.
