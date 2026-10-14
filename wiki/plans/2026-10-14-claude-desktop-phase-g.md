---
title: "ContextShrink Desktop MCP: Phase G suite hardening, pilot and beta"
type: feature-plan
status: proposed
date: 2026-10-14
phase: G
milestones: [M6, M7, M13, M14]
base: origin/main at 6a4b765856a91c5849ff8d10583fd45ed6c3bf15
---

# Phase G: suite hardening, pilot and beta

Phase G completes the local beta-readiness work and produces evidence-ready
Enterprise pilot and economics workflows for M6, M7, M13 and M14. It builds on
the Phase A-F implementation on `main`, including Desktop extension 0.1.1.
It does not claim clean-machine signing, live Enterprise results, or beta
release until the owner supplies the required machines, account access,
participants, and review.

The plan is divided into four independently reviewable stages. Each stage is a
separate pull request based on `main`; do not push directly to `main`.

| Stage | Milestone | Deliverable | Exit condition |
|---|---|---|---|
| 1 | M7 beta hardening | Local hardening, consent and retention behavior, quotas/failure handling, safe metadata export, supported-scope and operator docs | Local gates pass and every external release gate has a named owner/status; beta remains blocked by unresolved external gates |
| 2 | M14 suite hardening | Lifecycle/security regression coverage and reproducible manual validation checklist | Local gates pass; external clean-machine/multi-user checks are explicitly recorded as pending until performed |
| 3 | M6 Enterprise pilot | Reproducible task-bundle runner, collection schema and blinded scoring/report pipeline | Harness validates against committed synthetic fixtures; report identifies the missing account/participant evidence and makes no observed-savings claim |
| 4 | M13 expanded economics | Native tuned baseline, output-contract/ablation/full-package study runner and attribution report | Fixture-backed accounting invariants pass; live economics remain pending until refreshed Enterprise data and blinded participant scoring are supplied |

## Scope and operating rules

Reuse the existing 0.1.1 extension/runtime, phase C/E evaluation scripts and
reports, and local event measurement implementation. Keep the independent
`horizon.desktop_mcp.version.VERSION == "0.1.1"` source, the build ID based on
version plus executable digest, staged runtime copy with completion marker,
30-second host heartbeats, 120-second extension broker idle, and monotonic
version handover. Preserve the corrected exact-read accounting: requested
source bytes are counted once, returned bytes are counted once, and an exact
read does not claim avoided savings by comparing the same bytes to themselves.
Any change to this foundation must have a focused regression test and retain
the old lifecycle guarantees.

Phase G may add local configuration, reports, fixture data, scripts and tests.
It does not add a public HTTP service, a new cloud API, a new account key in the
employee extension, an inference provider call, or model-facing telemetry.
Existing Claude Desktop tool names and argument/result contracts stay stable.
Any unavoidable compatibility change requires its own reviewed contract
decision and migration note.

The default collection state is off. Before a pilot task starts, the
participant explicitly consents to the task bundle and local metadata
collection. Use a random participant code generated for the study; keep the
identity-to-code mapping with the study owner and outside reports. Do not
collect raw prompts, source text, file paths, credentials, arbitrary tool
arguments, or clipboard content. The study harness may hold a task prompt in
memory to execute a declared fixture but must not persist or export it.
Participants can inspect the metadata preview, cancel, and delete local pilot
records. Export requires a separate explicit user action. Reports and fixtures
must contain only synthetic data or consented, reviewed and de-identified
records.

Retain metadata needed to compare task outcomes: participant code, task and
pair ID, arm, consent state/time, locale, UTC task boundaries, Desktop and
extension versions, model and settings/effort, cache input/read/write usage
when available, uncached input, billed output, retries, tool/call counts,
payload byte/token estimates with counter version, per-step and total latency,
quality score and scorer status, task success/failure, fallback/unsupported
reason, and declared source-format stratum. Missing fields stay null with a
reason; never impute them as zero. Keep source/version handles local. Use
allowlisted field names and bounded strings/numbers when exporting. The consent
screen states the purpose, fields, retention period, export destination and
withdrawal/deletion path. Choose the shortest useful default retention and
make the period configurable by the study owner.

## Stage 1: M7 beta hardening

### Work

- Review configuration defaults, file permissions, quotas, retention and
  cleanup across broker, snapshots, artifacts, companion staging and pilot
  metadata. Document the data owner and deletion behavior for each local store.
- Add explicit opt-in and consent records for pilot metadata. Keep normal
  extension operation independent from pilot collection. Add a reviewable
  preview, cancel, export and delete flow; do not persist study identity data
  alongside extension telemetry.
- Apply bounded record counts, byte sizes, durations and export sizes. On a
  limit, fail closed with a stable error or pause collection; never truncate a
  source/result silently. Preserve existing source quotas and worker deadline
  behavior.
- Harden expected operational failures: unavailable broker, expired/revoked
  source, corrupted/unsupported document, disk-full/permission errors,
  interrupted export, companion disconnect, restart, stale runtime and
  rollback. Report actionable status without sensitive details.
- Publish the supported Windows/Claude Desktop/source-format/task scope and
  the current limits, including no ordinary-composer interception, no OCR,
  no inferred history, no hard output cap, and no general savings guarantee.
- Add operator instructions for cohort setup, consent, task assignment,
  scoring, collection windows, data refresh, deletion and incident handling.

### Acceptance and verification

- Tests prove collection is off by default, opt-in is explicit, cancel writes
  nothing, exported fields are allowlisted, and no prompt/source/path/argument
  content enters ordinary logs, reports or telemetry.
- Tests cover size/count limits, retention expiry, explicit deletion, failed
  export recovery and stop/restart cleanup without deleting unrelated files.
- Existing source, artifact, companion and broker security tests continue to
  pass; errors remain bounded and do not leak exception data or secrets.
- Validate the local beta hardening set with:

  ```powershell
  .\.venv\Scripts\python.exe -m pytest tests/desktop_mcp tests/test_mcp_dependency_contract.py tests/test_ccr_mcp_http.py -q
  .\.venv\Scripts\python.exe -m ruff check horizon/desktop_mcp desktop/mcp tests/desktop_mcp
  .\.venv\Scripts\python.exe -m ruff format --check horizon/desktop_mcp desktop/mcp tests/desktop_mcp
  ```

- Rebuild the release candidate with the primary repo's venv:

  ```powershell
  .\.venv\Scripts\python.exe desktop/mcp/extension/build_extension.py
  ```

  Record the generated MCPB path, version, executable digest/build ID, and
  smoke outcome. This local build does not establish signing or clean-machine
  installation.

### Exit

Stage 1 exits when all local checks pass, the release candidate builds, and a
beta readiness report marks every product, security, signing and target-app
gate as passed, failed, or pending with evidence and owner. A pending external
gate is not represented as completed M7 or as authorization to distribute.

## Stage 2: M14 suite hardening

### Work

- Build a lifecycle matrix for install, first launch, two chats, chat close,
  Desktop exit, broker idle, Desktop restart, MCP adapter restart, update from
  0.1.0 and 0.1.1, equal/newer/older broker versions, worker crash, source
  mutation/revocation, expiry, companion exit, uninstall and rollback.
- Verify runtime-copy cleanup, heartbeat and retire operations, capability
  issuance/revocation, artifact cleanup, source handle invalidation and
  recovery behavior under clean stop and crash. Preserve normal native Claude
  workflow when the extension is unavailable or removed.
- Check operation-scoped authority: MCP clients cannot call companion-only
  artifact operations; user-interface capability cannot authorize unrelated
  broker operations; stale capabilities and source handles fail closed.
- Cover same-user and second-user boundaries, two simultaneous chats, two
  broker contenders, paths with reparse points/symlinks, changed roots, hostile
  filenames/titles, malformed IPC frames and truncated/oversized requests.
  Keep the same-user-code limitation explicit in the threat model.
- Review clipboard paths in preparation workflows: only explicit user action
  may paste/copy. There is no background clipboard polling or persistence.
- Provide a clean-machine manual checklist that captures exact OS, Desktop,
  extension and runtime versions, test account type, steps, observed result,
  logs/report IDs and cleanup. It must include install, approved-root setup,
  two-chat search/open/retrieve, broker close/update, uninstall and rollback.

### Acceptance and verification

- Automated lifecycle tests exercise old/new/equal version interactions,
  heartbeat timeout, normal retire cleanup, worker failure, capability
  revocation and on-demand restart. They assert the 0.1.1 contract stays
  monotonic and leaves no owned runtime, capability or artifact residue after
  uninstall/cleanup.
- Automated security tests cover unauthorized user/role, source revocation,
  malformed IPC and hostile path/input cases; they verify no cross-chat source
  substitution and no silent version substitution.
- Run the Stage 1 pytest/Ruff commands, then the existing extension package
  checks and local frozen MCPB smoke flow. Record exact results and platform
  skips in the stage report.
- The second-account/clean-machine F3 checks require a separate Windows
  account or machine and the owner's Claude Desktop environment. Until the
  checklist is executed, report those checks as pending; local simulation is
  not evidence that Claude Desktop kills detached child processes or that
  cross-SID ACLs work in production.

### Exit

Stage 2 exits when local lifecycle/security coverage passes and the manual
checklist is ready. M14 remains externally pending until the clean-machine,
multi-user and update/rollback checks are executed and attached to the report.

## Stage 3: M6 Enterprise pilot harness and report

### Study design

Provide four arms, with arm order randomized or counterbalanced across fresh
conversations:

| Arm | Configuration | Purpose |
|---|---|---|
| A: raw gateway | Same approved source access, exact bounded reads, equivalent safety limits | Isolate the retrieval/compression effect |
| B: ContextShrink | Candidate source, compute, preparation and artifact profile | Measure the complete product path |
| C-normal: native Claude | Participant's recorded normal upload/retrieval/settings | Measure practical baseline value |
| C-tuned: native Claude | Same concise instructions and model/effort settings as B | Isolate product-specific contribution |

Use a fixed versioned task bundle with task-specific ground truth and declared
supported workload strata. Initial fixtures cover exact numeric lookup,
exhaustive rows, long-prose questions, cross-document evidence,
negations/exceptions, rare outliers, unrelated follow-ups, repeated sources,
small/no-gain tasks, source-free prompts, calculations, full deliverables,
multilingual content, updated/revoked sources, and unsupported formats.
Fixtures establish harness correctness only; they do not stand in for human
participants or real Enterprise use.

The pilot runner must identify every assigned task and preserve failures,
timeouts, refusals, unsupported/bypass tasks and zero/negative-gain tasks.
Score correctness and required citations against task-specific ground truth
using an independent blinded scorer. Similarity and keyword measures are
diagnostics only. Record task success and quality alongside latency, retries,
calls, retrieval bytes and all estimated delivered payload. A raw baseline
uses the same source snapshot and safe output limits; it must not receive
unbounded responses or different source access.

### Measurement and collection

Keep three data products separate:

1. Estimated delivered-payload reduction: raw-arm estimate minus all B-arm
   delivered input/output and enabled schemas/instructions, with counter and
   coverage identified.
2. Modeled economic opportunity: explicitly parameterized model rates,
   cache mix, output, additional rounds and local/service cost. Show a range or
   mark unavailable when settings/rates are unknown.
3. Observed Enterprise consumption difference: refreshed participant/task
   window consumption normalized by successful tasks, with attribution limits.

The administrator collector, if available, runs outside employee extensions
and stores only scoped usage/cost buckets, source refresh timestamp, model,
product and time window fields necessary for the study. Never distribute an
Analytics key to a participant extension. Prefer documented admin exports
when no collector is configured. Map time buckets to isolated task bundles;
report coarser attribution when there is no MCP-call-to-inference join. Wait
for provider report freshness and recheck revised totals before freezing a
report. Record known unrelated activity and exclusions without discarding an
assigned task's failure.

Cost calculations include uncached input, cache writes/reads, billed output,
all models and all requests, plus incremental preparation/delegation/service
cost not already included. Do not count included output/reasoning/tool
arguments twice. Report cost per assigned task, cost per successful task
including failed-attempt spend, latency and quality. Keep signed differences;
never clamp regressions to zero. Do not add marginal mechanism percentages.

### Acceptance and verification

- A committed synthetic fixture run produces byte-stable structured output
  for arm assignment, pairing, task accounting, scoring fields and failure
  retention. Validate it against known expected records and ledger arithmetic.
- Tests reject duplicate/missing task IDs, arm mismatch, incomplete windows,
  invalid consent, unknown cost fields and attempts to treat missing billing
  values as zero. Ensure report generation does not require network access or
  credentials.
- A clearly labeled fixture report records `evidence_class: synthetic`,
  `enterprise_usage_observed: false`, and `savings_claim: none`; it includes
  version, fixture digest, counter version, arm configuration, failures and
  quality/latency outputs. No fake participant or financial claim is allowed.
- Harness CLI invocation, output schema and expected fixture validation are
  documented in the stage report. Preserve Phase C/E report conventions where
  useful and link to existing reports instead of rewriting them.

### Exit

Stage 3 exits when the harness/report contract is reproducible on committed
fixtures and the owner has a concrete checklist for the account key or export,
cohort, model/settings, predeclared task weights/sample/stopping rule and
blinded scoring. M6 evidence remains pending until a consented real pilot has
refreshed usage/cost data and completed blind scoring.

## Stage 4: M13 expanded economics harness and report

### Work

- Add an explicit output-contract comparison that distinguishes generated
  output from deterministic server-rendered artifacts. Include correctness,
  completeness, requests for follow-up/repair, output tokens, retries,
  latency and artifact delivery success.
- Compare C-normal, C-tuned, B full package, and predeclared ablations for
  preparation, retrieval/compression, deterministic computation and artifact
  rendering. Keep settings matched where the comparison requires them and
  declare each mismatch.
- Measure no-gain and adverse strata, tool-schema/instruction overhead,
  unsupported/bypassed workload share, additional model rounds, cache state
  and failure costs. Reconcile per-task values to complete task-bundle values.
- Report stratified and weighted results with predeclared workload frequencies
  and sensitivity to uncertain cost, cache and adoption assumptions. Preserve
  absolute units and denominators; do not present a combined percentage by
  summing overlapping mechanism estimates.

### Acceptance and verification

- Synthetic fixtures include full-table deliverable, deterministic aggregate,
  repeated-source retrieval, current-input preparation, output-sensitive task,
  tiny/no-gain case, unsupported/bypass and failure/retry case.
- Arithmetic tests verify all-request totals, cache classes, output and
  service costs, paired task differences, signed regressions, cost per
  successful task and weighted aggregation. Missing rates/data produce an
  unavailable estimate or an explicitly bounded scenario, never a universal
  fallback rate or fabricated observed amount.
- The report schema separates `payload_estimate`, `modeled_economics`, and
  `enterprise_observed`; includes evidence class, sample counts, failed and
  bypassed tasks, quality, latency, data freshness, attribution confidence,
  estimator version, assumptions and limitations. Synthetic output is marked
  `enterprise_observed: false` and carries no dollar-savings claim.
- A fixture report validates reproducibility and is reviewed with the M6
  report. Real economics remain pending until study owner supplies eligible
  Enterprise data, scorer results and declared cohort/task weighting.

### Exit

Stage 4 exits when the expanded runner and report pass synthetic invariants,
the assumptions and external data requirements are explicit, and all evidence
claims are tied to their source records. M13 is not called complete on fixture
evidence alone.

## Pull request and release gates

Open one pull request per stage, each against `main`, in dependency order:
M7 local hardening, M14 suite hardening, M6 pilot harness, M13 expanded
economics. Each PR contains the phase spec updates necessary for its stage,
focused code/docs/tests, report schema examples, test/build results, known
external blockers, and a reviewer checklist. Do not combine unrelated stages
or land directly on `main`.

Before a stage PR is ready, run the targeted pytest/Ruff commands above and
the existing stage-specific evaluator. For any build or report, retain the
command, build/version/digest, fixture digest, exact test summary, skips,
platform and evidence class. Existing top-level suites are run only when
required by the repository's release workflow or when a changed dependency
affects them.

Beta release requires all of the following to be explicitly recorded:

- supported platform, Desktop version, runtime and source-format matrix;
- consent, retention, export and deletion review;
- local test and frozen-build results;
- clean-machine install, approved-root, two-chat lifecycle, uninstall and
  rollback evidence;
- second-user/cross-SID, revocation, crash/update, companion and artifact
  cleanup results;
- signing and approved distribution evidence;
- M6/M13 study design, scorer, cost/usage source freshness and attribution
  status; and
- accurate zero/negative results, failures, bypass rates and limitations.

If an external item is unavailable, label the beta or milestone as pending and
publish no claim that depends on it. Synthetic fixtures may validate software
and reporting arithmetic, but cannot establish Claude Desktop compatibility,
participant quality, Enterprise consumption, or financial savings.
