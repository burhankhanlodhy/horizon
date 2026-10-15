---
title: "ContextShrink Desktop MCP: Phase G evidence hardening"
date: 2026-10-14
status: in-progress
phase: G
milestones: [M6, M7, M13, M14]
---

# Phase G evidence hardening

A post-merge review of Phase G (PRs #9-#12) found that the pilot and
economics tools, which will process real Enterprise data, could let a
misleading result through. This follow-up fixes those problems before any
real participant data is used, and closes the plan gaps.

## Findings being fixed

1. **No statistical gate.** `savings_claim` is the literal `"none"` and
   nothing computes an interval or checks sample size. Build-plan section 13
   requires a positive lower confidence bound on the predeclared mean net
   benefit against a named baseline, plus a sufficient sample.
2. **The pilot rule check compares the expected value with itself**
   (`phase_g_pilot.py`), so it always reports `passed`.
3. **The synthetic economics report prints USD amounts from placeholder
   rates.** Every fixture rate is `1` USD per million tokens, unlabelled.
4. **"Observed Enterprise usage" can be produced from a hand-written bundle.**
   `usage_windows[].source == "admin_export"` is a plain label, with no export
   file, digest or ID.
5. **M7 export fields accept free-form slugs**, so `task_id`, `pair_id` and
   `model` can carry a person's or machine's name.
6. **Allocation and blinding are weak.** Participant codes are chosen by the
   operator, the arm order follows from the sorted codes plus the seed (so
   it is predictable and can be steered), `scorer_code` can be
   `"unassigned"`, and only one score per assignment is supported.
7. **Plan gaps:**
   - the section 20 input-reduction × output-contract factorial
     (F00/F10/F01/F11) is missing;
   - the per-store data-owner and deletion document, the operator runbook
     and the published supported scope are missing;
   - some tests are missing (export failures, recovery key DACL, reparse
     points, recovery bounds);
   - two files fail `ruff format --check`.

## Shared interfaces

### `horizon/desktop_mcp/evidence_stats.py` (new)

```python
@dataclass(frozen=True)
class PairedUnit:
    cluster_id: str        # participant code; pairs from one participant are correlated
    difference: Decimal    # candidate minus reference cost for one matched pair; negative means the candidate is cheaper

@dataclass(frozen=True)
class Interval:
    estimate: Decimal | None   # exact Decimal mean of all differences
    lower: Decimal | None
    upper: Decimal | None
    confidence: Decimal        # e.g. Decimal("0.95")
    method: str                # "cluster_percentile_bootstrap" or "insufficient_data"
    resamples: int
    seed: int
    n_units: int
    n_clusters: int

def paired_difference_interval(units, *, confidence=Decimal("0.95"), resamples=10000, seed) -> Interval
def savings_decision(interval, *, min_units, min_clusters, evidence_class, provenance_verified) -> dict
```

- **The interval** comes from a cluster bootstrap: resample clusters with
  replacement using `random.Random(seed)`, take the mean of every unit
  difference in the resample, then use percentile bounds. Floats are fine
  inside the resampling loop; bounds are reported as Decimal strings with 10
  significant digits. With fewer than 2 units or 2 clusters, `lower` and
  `upper` are `None` and `method` is `"insufficient_data"`. The result is
  deterministic for a given seed.
- **The decision** returns `{"claim", "reasons", "rule"}`, where `claim` is
  one of `"none"`, `"inconclusive"`, `"savings_supported"` or
  `"cost_increase_supported"`.
  - It is `"none"` with explicit reasons when the evidence class is not
    `consented_deidentified`, provenance is not verified, the sample is below
    `min_units` or `min_clusters`, or the interval is insufficient.
  - It is `"savings_supported"` only when every gate passes and `upper < 0`,
    which means the lower bound on savings (−difference) is positive.
  - It is `"cost_increase_supported"` when every gate passes and `lower > 0`.
  - Otherwise it is `"inconclusive"`.
  - `rule` is a fixed text that cites the build-plan rule.

### `horizon/desktop_mcp/factorial.py` (new)

```python
@dataclass(frozen=True)
class FactorialCell:
    unit_id: str      # one participant-task block
    cluster_id: str   # participant code
    arm: str          # "F00" | "F10" | "F01" | "F11" (first digit: input reduction, second: output contract)
    cost: Decimal | None
    success: bool

def analyze_factorial(cells, *, seed, confidence=Decimal("0.95"), resamples=10000,
                      min_units, min_clusters, evidence_class, provenance_verified) -> dict
```

- Only units with all four arms and non-null costs are analysed. Others are
  counted under `excluded_units` with a reason (`missing_arm` or
  `missing_cost`). Duplicate arms in one unit are a `FactorialError`.
- Per-unit effects:
  - input reduction = ((F10 − F00) + (F11 − F01)) / 2;
  - output contract = ((F01 − F00) + (F11 − F10)) / 2;
  - interaction = F11 − F10 − F01 + F00.
- Each effect gets a `paired_difference_interval`. Input and output get a
  `savings_decision`; the interaction is reported with its interval and a
  claim of `"none"`.
- It also reports per-arm counts, mean cost and success rate.

### Economics provenance and rates (`desktop/mcp/phase_g_economics.py`)

- **Admin exports.** `--admin-export PATH` can be repeated. Each file is read
  bounded, and its SHA-256 and size are recorded in the report; paths never
  are. Every `usage_window` needs an `export_sha256` that matches a supplied
  file. If none match, the windows are unverified, `provenance_verified` is
  false, `enterprise_usage_observed` is false, and the status says why. This
  is integrity and traceability, not proof that an export is genuine.
- **Rate cards.** Each rate card needs `rate_source`, one of `placeholder`,
  `published_price_list` or `contract`, plus `as_of` (`YYYY-MM-DD`). With
  placeholder rates, no money amounts or currency are emitted: amounts are
  reported as `placeholder_units`, with `rates_are_placeholder: true`. The
  synthetic fixture uses placeholder rates.
- **Savings decision.** `savings_claim` comes from `savings_decision`, using
  paired units by participant against the named baseline, and the study
  plan's predeclared minimums.
- **Factorial.** Runs under condition IDs `F00`/`F10`/`F01`/`F11` feed
  `analyze_factorial`. Without them, `factorial` is `{"status": "not_run"}`.

## Work split

| Round | Agent | Owns | Scope |
|---|---|---|---|
| 1 | Statistics | `evidence_stats.py`, `factorial.py`, their tests | Interfaces above |
| 1 | Pilot | `pilot_study.py`, `phase_g_pilot.py`, the pilot fixture, report and tests | Real rule check, generated participant codes, committed secret allocation seed with a later reveal and verification, generated scorer codes, multiple scorers and agreement |
| 1 | Metadata | `pilot_metadata.py` and its tests | Generated-code formats for IDs, a model-name allowlist pattern, failure-injection and cap tests |
| 1 | Docs and tests | `desktop/mcp/DATA-STORES.md`, `desktop/mcp/SUPPORTED-SCOPE.md`, the M14 checklist rows, recovery tests | Store owner/deletion table, supported scope, and DACL/reparse/bounds tests |
| 2 | Economics | `phase_g_economics.py`, its fixture, report, notes and tests | Statistics wiring, provenance, rate labelling, factorial, formatting |
| 2 | Pilot (continued) | Pilot files | `savings_decision` for the pilot's comparisons |
| 2 | Runbook | `desktop/mcp/PILOT-RUNBOOK.md` | The operator procedure for the final tools |

## Round 3: real task bundles and participant withdrawal

The runbook pass found two gaps that block a real pilot.

1. **Only synthetic bundles load.** `validate_bundle` accepts only
   `synthetic_task_bundle`, so real tasks cannot be loaded.
2. **There is no per-participant withdrawal.** Deleting pilot metadata
   removes the whole store, and there is no way to drop one participant's
   data from the evidence.

### `pilot_task_bundle` (`pilot_study.py`)

- Same root keys and `study_plan` rules as the synthetic bundle, with
  `bundle_kind: "pilot_task_bundle"`.
- Each task has `task_id`, `stratum`, `locale`, `operator_instruction` and
  `sources`. Each source is `{source_id, format, file_name, sha256, bytes}`.
  - `format` is one of txt, md, json, csv, pdf or docx.
  - `file_name` is a safe base name only: no separators, no `..`, and no
    Windows reserved name. It is a supported suffix that matches `format`,
    and at most 120 characters.
  - `bytes` is between 1 and 25 MiB.
  - Source content is never embedded.
- There are no `synthetic_*` fields, and answer keys are not in the bundle.
- **Answer key.** It is a separate scorer-only file:
  `{schema_version, study_id, bundle_sha256, keys: [{task_id, kind,
  expected}]}`. `bundle_sha256` is the canonical digest of the bundle, and
  `kind` uses the existing typed check kinds. `validate_answer_key(key,
  bundle)` checks that every task has exactly one key and that the digest
  matches.
- **Sources.** `verify-sources --bundle B --sources-dir D` checks that each
  named file exists directly in D (no links or reparse points) with the
  declared size and SHA-256.
- `task-book` and `score-sheet` never include answer keys.
- Real bundles never produce `fixture_rule_checks`. Quality comes only
  from blinded scores.

### Withdrawal file (shared)

```json
{"schema_version": 1, "study_id": "...",
 "withdrawals": [{"participant_id": "p_xxxxxxxxxx", "withdrawn_at_utc": "2026-10-20T09:00:00Z"}]}
```

- `validate_withdrawals(value, *, study_id) -> frozenset[str]` lives in
  `pilot_study.py`. It rejects synthetic or unknown-format codes and
  duplicates, and requires `study_id` to match.
- **Pilot report.** `report --withdrawals PATH` removes every observation,
  score and usage row for withdrawn participants before any statistics,
  marks their assignments `withdrawn`, and reports only
  `withdrawn_participants` (a count). `verify-assignments` is unchanged.
- **Economics.** `--withdrawals PATH` works the same way, and withdrawn
  participants count toward neither `min_clusters` nor `min_units`.
- **Metadata store.** `pilot_metadata withdraw --participant-code CODE
  --confirm` deletes only that participant's records, keeps the others, and
  records nothing about the withdrawn person beyond a count.
