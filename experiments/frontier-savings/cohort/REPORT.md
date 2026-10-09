# Claude Code cohort validation and release review

October 9, 2026. Branch: `codex/compact-edit-adapters`.

## Decision

**Continue controlled Claude Code validation. Customer activation is not approved
by this evidence. Codex remains native until a managed local file guard is ready.**

The controller is always present. Unsupported/unqualified new requests use the
existing native tools automatically, with no new user setting. The stock proxy
has no cohort resolver configured, so this branch does not rewrite customer
traffic. Nothing was merged or deployed to Pi 5 for this validation.

The user approved the existing model and a shared $3 ceiling. We used isolated
pricing-source fixtures and the installed Claude Code client. Evaluation
qualifications have explicitly synthetic cost bounds; they are not production
certificates and must never be copied into the operator registry.

## What changed

1. Added `horizon/proxy/compact_edits/cohort.py`, a bounded Claude Messages HTTP
   boundary. Account authentication runs outside it; compression, prefix/cache
   decisions, provider forwarding and normal attempt settlement run inside it.
   The virtual catalog is installed before generation, not by a late response hook.
2. Eligible client streaming requests use complete upstream JSON and buffered
   downstream JSON/SSE. Virtual argument fragments never reach the native client.
   This trades first-token latency for validation before publication. Request and
   response limits are 2 MB and 4 MB; one conversation is serialized at a time.
3. A native numbered Read must match the independently managed complete source
   snapshot's SHA-256. A later partial, failed, pending or unfamiliar Read
   supersedes older evidence. Numbered output alone does not prove EOF.
4. Invalid unpublished compact previews receive at most one internal native
   retry, with the same catalog. It requests Read without attested source, or
   Edit with attested source. The rejected preview is never fabricated as an
   executed client call. Both provider attempts retain normal cost accounting.
5. Published compact operations are reserved durably before delivery. Actual
   native results are acknowledged by digest and reported error flag. Replayed
   results are idempotent; changed results fail. Missing results stop another
   model call until delivery/execution is reconciled. There is no blind resend.
6. Fixed first-admission failure handling: a journal capacity/write failure now
   uses native forwarding. Unsupported history is checked before durable
   admission is created. Existing admitted sessions still require recovery on
   a state error rather than silently losing their provider replay catalog.
7. Missing journal state no longer implies a cold boundary. The managed registry
   must attest a new/cold session or require continuation restoration. Returned
   model identity must match the qualified route before any expansion.
8. Added native-client harnesses, numeric results, tests and this review.
   Compression savings, Profile estimates and fee-bearing billing values were
   not changed or credited with experimental output savings.

## Validation evidence

### Automated checks

The final targeted suite contains **104 tests**, including **38 new cohort/core
checks**, existing Anthropic auto-mode/wire checks, account compression-cap checks
and hosted keepalive checks. All passed. Ruff check/format and Python compilation
also passed for the feature files. This is not a claim that the full repository
test suite or a production build was run.

Coverage includes compiler bounds and duplicate targets, unknown contracts,
negative economics, complete JSON/SSE translation, native fallback, authenticated
middleware ordering, tenant mismatch, replay/signature preservation, durable
reservation, restart restoration, changed replay arguments/results, uncertain
delivery, missing journal state, partial reads, route mismatch and recovery
exhaustion. Codex custom/function envelope tests isolate wire behavior from
execution; they do not certify a Codex client or its filesystem guard.

### Installed Claude Code, zero external calls

Client: **Claude Code 2.1.295**. Scripted provider bound to loopback only, dummy
key, `--bare`, disposable source copies. Five of five cases passed:

| Case | Observation |
|---|---|
| Native control | Native Edit produced the expected expanded source |
| Compact roundtrip | Expanded native Edit matched expected bytes; replay restored the provider's compact call |
| File changed after Read | Native client rejected the stale edit and preserved the changed file |
| Whitespace changed before Read | Complete original `old_string` did not match; native client rejected the edit |
| Explicit Edit deny | Native permission denial remained effective; file unchanged |

The observed Edit definition fingerprint is
`d63f080b4e6fcaea3ce999035cfdf0fd4ac3c7cec435e0a4c21c9064b3ba54e4`.
This is evidence for that installed definition, not permission to certify all
Claude Code versions. The harness drives the adapter core and client directly;
it does not exercise the full stock Horizon compression pipeline.

An initial permissions check mistakenly treated `--allowedTools Read` as an
explicit Edit denial. That assumption was wrong. The final check uses an actual
`permissions.deny: ["Edit"]` policy. No permission bypass was introduced.

### Live model pair

Existing configured model: `claude-sonnet-5-5`, existing Claude login. Both arms
had native Read/Edit/Bash in their catalog and normal native permissions. One
four-row pricing catalog task was run native first, compact second.

| Arm | Task result | CLI API-equivalent cost | Provider calls | Elapsed |
|---|---|---:|---:|---:|
| Native editing | Passed | $0.0377830 | 3 | 8.667 s |
| Compact editing | Passed | $0.0309524 | 3 | 6.067 s |
| Explicit native-script control | Incomplete / failed | $0.0175930 | 2 paid calls recorded | 5.206 s |

The successful pair saved **$0.0068306, or 18.1%**, for this one fixture. The
model chose native Edit in the control and the virtual compact tool in the
compact arm. Compact generated edit arguments were expanded through the actual
cohort engine, and native execution/replay completed without a recovery.

The explicit script arm read the file, attempted Bash and received a native
permission denial. The harness rejected subsequent requests and the client
ended with an error. Numeric logs retain exception classes, not sufficient
detail to assign a more specific cause. **Its lower partial cost is not a
successful baseline and cannot establish that compact beats native scripts.**
No safety/approval permission was disabled to rescue the comparison.

This live relay exercises the cohort engine and actual client against the real
provider. It does not exercise the full stock proxy's other optimizations,
account outbox, project routing, cache continuation or Pi 5 deployment. One pair
with fixed ordering provides no confidence bounds, fleet savings percentage or
production qualification. It cannot replace a representative replicated study.

### Shared spend ledger

| Item | USD |
|---|---:|
| Earlier pilot API-equivalent usage | 1.1138104 |
| New completed live calls, including failed script | 0.0863284 |
| Shared completed API-equivalent usage | **1.2001388** |
| New conservative usage upper bound | 0.8632840 |
| Prior usage plus new conservative bound | **1.9770944** |
| Authorized shared ceiling | **3.0000000** |

The conservative envelope deliberately exceeds CLI quotes and reserves each
request before HTTP. It is a stop-budget safeguard, not a current public tariff
or a savings estimate. Prior usage is the earlier pilot's returned estimate,
not a reconstructed conservative bound for those earlier calls. These are
API-equivalent figures from a subscription login, not audited provider invoices.
The failed comparison remains charged to the research ledger. Rerunning does not
reset it. No further paid comparisons were made after that failure.

## Review findings and remaining approval requirements

### Claude Code: mechanical evidence, incomplete economic qualification

The guard, permissions and replay evidence support further controlled evaluation
of four-row literal catalog clones. Production still needs:

- A successful native-script comparison under legitimate normal permissions,
  representative repeated fixtures, randomized arm order, non-use admissions,
  cache warmth, recovery overhead, latency and conservative economic bounds.
  The provisional $0.003/15% gate cannot be justified from one pair's mean.
- A managed operator registry/resolver with authenticated account, durable
  conversation/workspace identity, exact tool/client version, actual route,
  source snapshot provenance, intended keys and true cold-boundary evidence.
  The stock resolver is `None`; there is no production source collector.
- Full-pipeline replay/cache and exactly-once account settlement tests, including
  paid previews followed by recovery, response caching, missing usage,
  disconnects, concurrent workers, restart and actual upstream signed thinking.
  Unit signatures are opaque fixtures, not real signed-thinking validation.
- Operator admission-stop/drain/reconciliation and retention controls, journal
  loss detection, queue/timeout bounds and candidate retirement after a native
  edit. A SQLite reservation is not an execution acknowledgement.
- Existing global bypass, plan-cap and operator routing policy must govern
  admission and active-session draining. Current regressions cover stock native
  account policy; they do not certify a populated production cohort registry.

Supported compiler scope remains only 4–16 literal `ModelPricing` row clones.
Live evidence covers four rows; other counts and workloads are not financially
qualified. New logic, tiny edits, wrappers, unknown tool schemas, incremental
Codex histories, WebSockets and other unsupported new requests stay native.
Anthropic safeguard/auto-mode capability requests bypass the adapter unchanged.

For an already admitted conversation, identity/catalog corruption, uncertain
delivery or missing replay state cannot safely become stateless passthrough.
It returns an accurate recovery error; operator reconciliation is required.
This exception prevents duplicate execution and invalid provider history.

### Codex: keep native until a real local guard exists

The custom/function wire adapters and replay conversions pass mechanical tests.
**No managed Codex local compare-and-apply guard is implemented or installed.**
An ordinary fuzzy patch, a proxy-side hash, or a local preflight followed by an
unlocked write cannot close the race with an editor or file replacement. Codex
requires an executor that checks the bound source version and applies within
the same protected native approval/execution path. Its tests must cover stale
source, rename/symlink races, concurrent writers, permissions, restart and actual
custom/function result replay. Do not set `atomic_client_sha256` readiness merely
because the wire mapping works. Stock Codex traffic remains native.

### Savings and billing

Measured incremental customer savings and newly credited billing savings are
**zero** because this feature is not activated. The overall before/after proxy
percentage is unknown. The 18.1% fixture result and earlier 40.2–60.7% MCP pilot
results have different controls; none can be added to an account's prior savings
percentage. Native scripts might remove the advantage entirely.

## Approved-commit deployment sequence, later

The user's sequence is preserved: review findings, approve a specific commit,
merge, deploy to Pi 5, restart, then check health/replay/recovery with rollback
available. This report does not execute that later sequence.

1. Record the actual running Pi 5 commit, service unit, launch command,
   environment/dependency version and health response. Retain a runnable copy
   of that installation; do not assume local `main` equals the deployed version.
2. Review/approve the exact feature commit and its activation scope. Merge after
   qualification requirements are satisfied. Codex activation remains separate.
3. Stage the approved build on Pi 5 while preserving the previous installation,
   config, service environment and a consistent secured journal backup. Run the
   targeted checks there before switching the service to the approved revision.
4. Restart the identified proxy service. Check `/livez`, `/readyz`, `/health` and
   `/stats`, authenticated native forwarding and per-account attribution.
5. With an isolated validation account/workspace, check a guarded compact edit,
   actual result replay, restart/resume, an invalid private preview's native
   recovery, permission denial and unsupported native forwarding. Check a
   disconnect after reservation does not automatically re-execute the operation.
6. On failed health, integrity, replay or accounting, stop new admission, drain
   or reconcile active conversations, restore the recorded previous installation
   and restart/recheck. Preserve the journal. Rolling back to code that cannot
   normalize an admitted conversation is not a safe transparent recovery.

No Pi 5 service unit or running revision was inspected in this turn. The previous
adapter branch base, `136baf209a79b7a0474b18aac20e55447e0eecd8`, is only a source
reference, not a verified production rollback version.

At the final GitHub check, `main` was `9a0daac72a4c6bd4097e93e6d6989ec4a01650ae`.
The working feature branch already contained a separate ledger-pricing commit,
`e0b0e70`, equivalent to `62d2fd2` on main. That existing work was preserved;
only the cohort implementation/validation files were staged for this change.
Integration with the latest main and regression checks on the eventual merge
commit remain required before deployment.

## Evidence and reproduction

- [Numeric native results](native_results.json)
- [Live usage and budget ledger](live_results.json)
- [Native client harness](native_claude.py)
- [Live relay harness](live_claude.py)
- [Adapter architecture](../../../docs/compact-edit-adapters.md)
- [Earlier before/after analysis](../pilot/BEFORE_AFTER.md)

Raw CLI output, source copies and SQLite journals live only in ignored `runs/`.
No account keys, provider headers or private conversation histories were added
to the committed reports. Local zero-cost reproduction:

```powershell
.venv/Scripts/python.exe -m pytest tests/test_proxy/test_compact_edit_cohort.py tests/test_anthropic_auto_mode_passthrough.py tests/test_account_compression_cap.py tests/test_keepalive_hosted.py -q --disable-warnings
.venv/Scripts/python.exe experiments/frontier-savings/cohort/native_claude.py
```

The live harness refuses to reset an existing spend ledger. Its explicit script
control has already run once and must not be rerun by deleting the summary.
After review the harness also serializes HTTP handling/escrow updates and stops
new paid attempts after an error. Those refinements were linted/compiled, not
rerun against the provider; the recorded measurements remain from the earlier
completed calls.
