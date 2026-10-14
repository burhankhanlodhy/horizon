---
title: "ContextShrink Desktop MCP: M6 pilot harness and report contract"
type: feature-report
status: engineering-harness
date: 2026-10-14
phase: G
milestone: M6
schema: phase_g_study/v1
---

# M6 Enterprise pilot harness

This stage supplies a deterministic, offline collection and reporting contract
for a human-run Claude Desktop pilot. It does not launch Claude Desktop tasks,
call a model, read provider billing APIs, or automate an administrator export.
The committed fixture is synthetic software-validation data only. It cannot
stand in for participant quality, Enterprise usage, or financial results.

## Run the synthetic workflow

Use the primary checkout's Python 3.12 environment from the repository root:

```powershell
& 'C:\Users\Burhan\OneDrive\Desktop\ContextShrink\.venv\Scripts\python.exe' -B desktop/mcp/phase_g_pilot.py fixture-report --output "$env:TEMP\phase-g-m6-fixture-report.json"
& 'C:\Users\Burhan\OneDrive\Desktop\ContextShrink\.venv\Scripts\python.exe' -B desktop/mcp/phase_g_pilot.py task-book --output "$env:TEMP\phase-g-m6-task-book.json"
```

`fixture-report` validates the committed synthetic bundle and produces stable
example records for completed, failed, unsupported, bypassed and not-run
outcomes, including positive and negative paired estimates. Every row is
fabricated for fixture coverage and explicitly marked synthetic; none is a
Desktop execution, participant score, provider usage or cost observation.
`task-book` exports runnable synthetic operator instructions and source
snippets without answer/check fields. The instructions and sources are
authored test material, not participant prompts. Neither command uses the
network or a provider key.

For a real, consented cohort, generate all participant codes in one invocation
so the predeclared four-arm Williams blocks balance across participant and
task positions. Real participant codes accept `p_<12..32 lowercase hex>` and
the M7-generated `P-<8 uppercase hex>` format; synthetic codes use
`syn-p-...`. Consent timestamps are required for real codes and must precede
every task start. Keep the identity-to-code map
outside the report directory and outside this repository.

```powershell
& 'C:\Users\Burhan\OneDrive\Desktop\ContextShrink\.venv\Scripts\python.exe' -B desktop/mcp/phase_g_pilot.py assign --participant-code p_0123456789ab --participant-code p_1123456789ab --participant-code p_2123456789ab --participant-code p_3123456789ab --seed 20261014 --consent-at-utc 2026-10-14T09:00:00Z --output "$env:TEMP\phase-g-assignments.json"
& 'C:\Users\Burhan\OneDrive\Desktop\ContextShrink\.venv\Scripts\python.exe' -B desktop/mcp/phase_g_pilot.py score-sheet --assignments "$env:TEMP\phase-g-assignments.json" --output "$env:TEMP\phase-g-blind-scores.json"
```

The operator uses each assignment with its matching task card and records an
allowlisted observation after the task. `score-sheet` emits a fully shaped,
blinded worksheet initialized to `no_score`/`unscored`; the scorer fills only
the declared fields and replaces `scorer_code: unassigned`. The resulting
score-sheet JSON can be passed directly to `report --scores` without manual
conversion. `report` validates assignment, observation, blind-score and
optional admin-window JSON and emits the report.
The participant should review the collection purpose, fields, destination,
retention, and withdrawal/deletion procedure before consenting. Stop collection
if that local study-owner process has not been approved and communicated.

```powershell
& 'C:\Users\Burhan\OneDrive\Desktop\ContextShrink\.venv\Scripts\python.exe' -B desktop/mcp/phase_g_pilot.py report --assignments "$env:TEMP\phase-g-assignments.json" --observations "$env:TEMP\phase-g-observations.json" --scores "$env:TEMP\phase-g-blind-scores.json" --usage-windows "$env:TEMP\phase-g-admin-windows.json" --as-of-utc 2026-10-15T12:00:00Z --output "$env:TEMP\phase-g-m6-report.json"
```

Input JSON is bounded to 8 MiB. Invalid records fail with a stable validation
code; they are not silently dropped. Missing measurements require an explicit
reason and stay null. Missing or failed tasks remain in assigned denominators.
The harness does not persist or export raw participant prompts, source text,
file paths, credentials, clipboard content, or arbitrary tool arguments.

## `phase_g_study/v1` input contract

The executable schema and validators live in
`horizon/desktop_mcp/pilot_study.py`; the CLI lives in
`desktop/mcp/phase_g_pilot.py`. This version is intentionally self-contained
and has no import-time dependency on the M7, M14, or M13 stage branches.

An assignment envelope includes `schema_version`, `study_id`, `bundle_sha256`,
`seed`, `arms`, and `assignments`. Each row binds one task/participant to one
arm and includes `assignment_id`, `score_id`, `pair_id`, `participant_id`,
`participant_kind`, `task_id`, `stratum`, `locale`, one-based `order`, derived
`blind_arm_code`, and consent status/time/scope. Arm IDs are exactly `A`, `B`,
`C_normal`, and `C_tuned`. Assignment imports recompute IDs, Williams order,
and the blind alias from the study seed. The planned cohort target is a
multiple of four.

Each observation contains the assignment keys, an outcome `status`,
`start_utc`/`end_utc`, locale, `versions` (`desktop`, `extension`, `model`,
`settings`), `counter_version`, and the SHA-256 `source_snapshot_sha256` of
the task's canonical synthetic source list. Versions and snapshot must match
the assignment's declared task and comparison requirements. `metrics` holds
latency, retries, tool calls, retrieval bytes, estimated input/output tokens,
and schema/instruction tokens. `provider_usage` holds task-level aggregate
uncached input, 5-minute and 1-hour cache writes, cache reads, and billed
output buckets when known. Every null has a bounded `metric_missing_reasons`
or `usage_missing_reasons` entry. `failure_code` and `unsupported_code` are
bounded allowlisted identifiers required only for the matching status.

The M6 `provider_usage` record is an aggregate for the whole assigned task,
not a per-request or per-model ledger. Set `versions.model` only when all
provider requests for that assignment use the same configured model; set it
to null for a mixed-model task. No admin time-window bucket is split across
requests. M13 may add a separate request-level, per-model economics sidecar
keyed by `assignment_id` and must reconcile it to these task totals when the
M6 usage counters are present.

M7's `P-<8 uppercase hex>` participant code is accepted by this schema. An
operator adapting M7-local collection records must explicitly map only these
allowlisted metadata keys into M6; raw prompts, source text, local paths,
credentials and arbitrary tool arguments are never accepted. If an upstream
counter only reports combined cache-write tokens without 5-minute versus
1-hour TTL, leave both M6 fields null with a missing reason instead of
splitting or estimating the value.

Blind score records contain `score_id`, derived `blinded_arm_code`,
`scorer_code`, `status` (`scored` or `no_score`), correctness, completeness,
citations, unsupported-claim judgment, and bounded omission codes. Every
assignment remains in the score sheet; a non-completed task must be `no_score`.
Synthetic assignments cannot receive a participant-quality score. The fixture
rule checks only exercise the harness's deterministic input contract.

Optional admin-window records contain `window_id`, real `participant_id`,
`arm`, the exact observed `assignment_ids` mapped to the window, UTC start/end
and refresh timestamps, `source: admin_export`, mapping confidence,
`exclusions_recorded`, allowlisted `unrelated_activity` category and bounded `exclusion_codes`,
usage buckets plus per-field missing reasons, and cost amount/reason/currency.
Every mapped assignment must have a consented observed task entirely inside
the mapped time window. Mappings must cover each participant/arm's observed
assignment set exactly. Synthetic assignments cannot import usage windows.
Freshness is checked against `study_plan.refresh_max_age_hours`; time-window
data is observational and does not create a causal claim. All-null usage is
unavailable; partial bucket or cost coverage remains explicitly partial.

For M13, these stable record keys can be consumed without importing the M6
Python module. This excerpt shows the task-level usage shape; valid imports
also require all assignment keys, metrics, statuses and missing-reason maps:

```json
{
  "arm": "B",
  "assignment_id": "asg_0123456789abcdef01234567",
  "pair_id": "pair_0123456789abcdef0123",
  "participant_id": "p_0123456789ab",
  "task_id": "numeric_lookup",
  "status": "completed",
  "start_utc": "2026-10-14T10:00:00Z",
  "end_utc": "2026-10-14T10:01:00Z",
  "versions": {"desktop": "1.2.3", "extension": "0.1.1", "model": "model-label", "settings": "effort-medium"},
  "counter_version": "counter-v1",
  "source_snapshot_sha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
  "metrics": {"estimated_input_tokens": 120, "estimated_output_tokens": 45, "schema_instruction_tokens": 20},
  "provider_usage": {"uncached_input_tokens": 120, "cache_write_5m_tokens": 0, "cache_write_1h_tokens": 0, "cache_read_tokens": 0, "billed_output_tokens": 45}
}
```

`provider_usage` covers all provider requests for this task. A separate M13
request ledger may enumerate models and requests, but must not allocate an
admin-window total among them. Null task model means mixed or unknown models;
M13 should retain the request ledger and mark model-rate comparisons unmatched.

## Report semantics and evidence boundaries

The output retains three separate sections: `payload_estimate`,
`modeled_economics`, and `enterprise_observed`. `payload_estimate` contains
per-arm token estimate coverage and an A-minus-B paired comparison. A pair is
included only when both tasks completed and model, settings, source snapshot,
and counter version match. Mismatches and failed pairs are counted by reason;
the signed delta is preserved, including zero or negative results. These are
participant-supplied inner-payload estimates, not provider billing totals.
An outcome marked `completed` means only that the assigned task produced a
recorded completion; it does not establish answer quality. Reports keep
`quality_pass_count` null until the owner predeclares a pass criterion and
qualified independent blind scores establish it.

`modeled_economics` remains unavailable because this M6 harness accepts no
rate card or service-cost assumption. `enterprise_observed` requires consented
real participant assignments, fresh administrator windows, exact task
mapping, high/medium mapping confidence, documented exclusions and observed
usage. Missing costs, null usage buckets, or stale windows cannot become zero.
Observed time-window totals remain separate from token estimates and state
`causal_claim: false`. The fixture report always states
`evidence_class: synthetic`, `enterprise_usage_observed: false`, and
`claims.savings_claim: none`.

## Operator and evidence gates

Before collecting a participant cohort, the study owner must declare the task
weights, target sample, minimum completed count per arm, stopping rule,
model/settings, supported Desktop build and source access, consent language,
retention/deletion process, and independent scorer. During collection, retain
all failures, bypasses, unsupported tasks and no-score outcomes. Export only
the allowlisted JSON after explicit review and participant consent. Obtain a
fresh administrator export covering the isolated study windows and document
unrelated activity, exclusions and mapping confidence. Do not put provider
keys in the employee extension or harness.

The report may claim Enterprise usage was observed only when the imported
window data validates. It still cannot claim the product caused a difference
or saved money. M6 remains evidence-pending until real consented tasks receive
completed blind scoring and refreshed attributable Enterprise data. Synthetic
fixture passage establishes harness behavior only.

## Verification

Run the focused contract tests and both offline CLI paths from the repository
root:

```powershell
& 'C:\Users\Burhan\OneDrive\Desktop\ContextShrink\.venv\Scripts\python.exe' -B -m pytest -p no:cacheprovider tests/desktop_mcp/test_phase_g_pilot.py -q
& 'C:\Users\Burhan\OneDrive\Desktop\ContextShrink\.venv\Scripts\python.exe' -B desktop/mcp/phase_g_pilot.py fixture-report --output "$env:TEMP\phase-g-m6-fixture-report.json"
& 'C:\Users\Burhan\OneDrive\Desktop\ContextShrink\.venv\Scripts\python.exe' -B desktop/mcp/phase_g_pilot.py task-book --output "$env:TEMP\phase-g-m6-task-book.json"
```

The human-run protocol, real participant cohort, administrator export, scorer
results and provider freshness remain external gates. This stage does not
assert that they have been performed.
