# Step 2: zero-cost Claude proxy integration

October 9, 2026. Branch: `codex/compact-edit-adapters`.

## Result

The controlled single-worker integration passes the full request path through
`create_app()`: account authentication, cohort admission, stock compression and
prefix tracking, response caching, provider forwarding, outcome settlement and
the real SQLite account outbox. Provider HTTP uses a scripted `MockTransport`.
No model API keys or paid calls were used. Stock customer activation remains
unconfigured, with no changes to subscription fees or credited compact savings.

This is mechanical integration evidence. It does not replace a successful
native-script economic comparison, actual signed-thinking validation, or the
approved-commit deployment review. Codex continues using native tools.

## Problems found and fixed

1. **Admission ignored existing policy.** A populated resolver could inject a
   compact catalog despite a Free account cap, explicit passthrough/bypass, or
   `config.optimize=False`. The stock app now checks those policies before
   compact admission. Unsupported new sessions remain native. Already admitted
   sessions return an explicit recovery error when policy/route/transport
   changes; silently removing their catalog would invalidate replay. Operational
   draining remains separate work.
2. **Native Read newline rendering was too narrow.** Numbered Read output can
   omit the final empty line. Both renderings now match only when the decoded
   text, with the known terminal LF when necessary, hashes to the independently
   supplied complete snapshot. Partial reads still fail; no EOF inference is used.
3. **Cancellation could outlive durable work.** Cancelling `to_thread()` does
   not stop its worker. The conversation lock now remains held until journal
   preparation/publication finishes, including repeated cancellation. A timeout
   cannot free the lock while a reserved call is still being committed.
4. **Waiting was unbounded.** Each cohort has a 2-second queue wait, at most four
   waiting requests, and a 120-second operation budget. Matched request-body
   collection also has a 120-second deadline and 1,024-frame bound. These are
   separate stages, not a promise of a 120-second end-to-end maximum. Durable
   cancellation cleanup can extend response latency; SQLite's busy timeout is
   five seconds. Compiler/thread scheduling is not a hard wall-clock guarantee.
5. **Response integrity needed stricter gates.** Captured inner responses must
   have one start and a completed body. Expansion requires nonnegative integer
   input/output usage. Missing/invalid usage, wrong actual model and oversized
   responses publish no compact tool. Previously settled provider attempts are
   retained even if the outer client receives 502. Missing provider usage remains
   unknown/estimated in existing accounting; this code does not invent a measured
   cost or modify the billing estimator.

## Trusted session/source bridge

`horizon/proxy/compact_edits/registry.py` adds an explicit operator API:

- `managed_candidate()` accepts complete UTF-8/LF source bytes, an independently
  provided SHA-256 and collector provenance, trusted paths and intended ordered
  keys. It checks the bounded literal-clone compiler. No request/model-selected
  source path is read by the proxy.
- `ManagedClaudeRegistry.bind()` binds authenticated account, managed session,
  workspace, route, source, exact native contract and operator qualification.
  A durable binding accompanies the existing source/admission/call journal.
- `resolve()` uses the existing `X-Horizon-Session-Id` only to look up an already
  registered session within its authenticated account. Identity hints cannot
  create a binding or select another account. Exact project-prefixed route is
  bound; unqualified provider overrides are rejected for active sessions.
- `restore()` rebuilds a registered continuation from its durable admission,
  preserving the original source/catalog and actual native-result receipt.
  Missing journal state is an error, not evidence of a fresh cold boundary.
- `install()` checks the actual app worker count and refuses to replace another
  resolver. Constructor and installation require **one worker**; the registry
  defaults to at most 16 sessions and never evicts active state automatically.

There is no customer endpoint, default registration, or feature toggle. The
source bridge is a trusted Python embedding API, not a deployed client source
collector or cryptographic attestation service. A managed launcher must supply
and persist the genuine session/workspace manifest, enforce the session header,
capture complete bytes independently, certify the true cold boundary, and restore
every admitted binding before accepting resumed traffic. Losing the managed
manifest is not safe transparent passthrough. None of those certificates can be
minted from this test suite's synthetic economic bounds.

## Verified matrix

The new integration file contains **45 tests**. The final targeted regression
matrix contains **202 tests**, all passing, with one existing warning. Ruff
check/format and Python compilation pass for the changed feature/test modules.
The full repository suite, production build and Pi health checks were not run.

| Area | Scripted evidence |
|---|---|
| Authentication | Invalid/revoked/unavailable authorization stops before registry/provider; account credentials and identity hints never go upstream |
| Account isolation | Other accounts cannot select the candidate; ordinary native response cache remains tenant-partitioned |
| Managed sources | Bad digest, encoding, terminal newline and provenance reject; duplicate bindings and multiple workers reject |
| HTTP JSON/SSE | Upstream complete JSON becomes validated native Edit; downstream SSE contains no virtual argument fragments |
| Compression/prefix | Normal optimization enabled; provider compact history reaches prefix tracking before client publication |
| Response cache | Provider compact response is stored; an unanswered published call cannot be retransmitted from cache |
| Continuation | Actual native results and permission errors normalize back to provider call IDs/arguments before cache/prefix processing |
| Replay | Changed result is rejected; identical completed continuation can hit cache with zero additional provider cost |
| Restart | Fresh `create_app`, empty in-memory response/prefix caches, reopened SQLite journal and new analytics runtime resume the admitted history |
| Recovery | Invalid private preview retries once with native Edit or Read; both completed provider attempts are retained, including a final client 502 |
| Accounting | One durable row per completed inner request; unique IDs for recovery; mocked usage prices to $0.000456 per attempt, $0.000912 for two; cached continuation costs zero |
| Outbox | Reopen preserves rows; duplicate event enqueue is idempotent; fresh runtime creates a distinct event |
| Concurrency | Concurrent requests publish at most one compact operation; queue wait times out; cancellation holds lock until journal mutation settles |
| Disconnect | Failed client send after reservation preserves mapping and already settled cost; no execution acknowledgement or automatic retransmission is invented |
| Policy/failures | Cap/bypass/disabled optimization prevent admission; active changes, missing journal, wrong model, invalid usage and buffer overflow fail closed |

These prices are expected values from synthetic usage and a scripted rate lookup
matching the repository fixture, not model measurements, actual expenditure,
savings or invoices. Without that explicit lookup, the installed LiteLLM map did
not resolve this older alias and the existing estimator fell back to $0.000510
per attempt. The test makes its arithmetic oracle deterministic; it does not
repair or certify real tariff resolution. The
outbox delivery worker/control-plane HTTP acknowledgement is outside this test
harness. Fresh-app reconstruction is not an OS process-kill or Pi service test.
The previously recorded installed-Claude native permission/file-guard checks
remain separate evidence; this phase does not drive the actual CLI through the
stock proxy. Fake signatures cannot certify real signed-thinking compatibility.

The final integration harness rejects external socket connections and forces
LiteLLM's local price catalog. Initial harness setup exposed LiteLLM's remote
price-map lookup; the harness was corrected to use the bundled map. No paid
model request occurred during any iteration.

## Still required before customer activation

1. Repair the legitimate native-script comparison and run representative,
   balanced complete-workflow economic tests. Include unused catalog admissions,
   cache warmth, recovery and latency. Earlier 18.1% is still one fixture pair.
2. Integrate and certify the actual managed launcher/source collector and durable
   manifest restoration. The operator API is now implemented; that automatic
   production source collection and qualification are not.
3. Add operator admission-stop/drain/reconciliation and safe journal retention,
   source-candidate retirement, and rollback of active admitted conversations.
4. Keep one worker until a real shared admission/generation lease is built and
   validated. SQLite publication reservation prevents duplicate compact calls;
   it does not certify independent workers generating against one conversation.
5. Test actual native-client full-proxy execution, real signed thinking, service
   restart/kill and delivery recovery, latest-main integration and the exact Pi 5
   deployment/rollback candidate. No merge/deploy was performed in this phase.
6. Keep Codex native until its protected local compare-and-apply guard is ready.

OneProvider Gemini's earlier unknown charge and reported output-cap mismatch
remain unresolved. This phase changes neither that ledger nor the shared $3
authorization and makes no additional paid requests.

## Reproduce without external model calls

```powershell
$env:LITELLM_LOCAL_MODEL_COST_MAP = 'True'
.venv/Scripts/python.exe -m pytest tests/test_proxy/test_compact_edit_pipeline.py tests/test_proxy/test_compact_edit_cohort.py tests/test_proxy/test_gemini_payload_research.py tests/test_anthropic_auto_mode_passthrough.py tests/test_account_compression_cap.py tests/test_keepalive_hosted.py tests/test_proxy_response_cache_replay.py tests/test_proxy_anthropic_cache_stability.py tests/test_5xx_accounting_all_providers.py -q --disable-warnings
```
