# Phase G pilot operator runbook

This runbook is the operator procedure for the Phase G pilot tools as they
exist on branch `claude/phase-g-hardening` (2026-10-14):

- `desktop/mcp/phase_g_pilot.py` (M6 assignment, scoring import, report),
- `desktop/mcp/pilot_metadata.py` (M7 local opt-in metadata store),
- `desktop/mcp/phase_g_economics.py` (M13 offline economics and administrator
  export provenance).

The tools do not start Claude Desktop tasks, call a model, read provider
billing, or export administrator data. They check, store and summarize what the
operator supplies. Nothing here is evidence until a consented, real cohort has
been run under sections 2 to 7. The committed fixtures are synthetic and may
only be used to check the software.

The commands were run against generated codes and synthetic observations in a
temporary folder. Procedural steps (who does what, when) are policy and are not
tested. Items marked **not run** were not exercised.

## Conventions

- Commands are PowerShell, run from the repository root.
- `$PY` is the primary checkout's Python. Set it once per session:

  ```powershell
  $PY = 'C:\Users\Burhan\OneDrive\Desktop\ContextShrink\.venv\Scripts\python.exe'
  ```

- `<STUDY>` is an operator folder outside the repository, outside any
  OneDrive-synced folder, and outside the report folder. Example:
  `D:\pilot-secure\<study-id>`. Every file this runbook names as an input or
  output lives there.
- Every `phase_g_pilot.py` command also writes its JSON to standard output.
  When you pass `--output`, add `> $null` so the output is not left in the
  terminal. This matters for `assign`, whose output is the full arm schedule.
- The CLIs insert the repository root into `sys.path` themselves, so
  `PYTHONPATH` is not needed.

---

## 1. Roles and separation

| Role | Who | Does | Must not |
|---|---|---|---|
| Study owner / data owner | One named person | Approves the predeclared bundle (section 2), consent wording, retention and deletion; holds the code-to-person mapping; receives incident reports; decides withdrawals and whether to reveal the seed | Run sessions or score tasks in the same study |
| Operator | One or more people | Generates codes, runs `assign`, keeps the seed and assignment file, runs sessions, writes observations, runs `report` | Score their own sessions; give scorers the assignment file or seed |
| Participant | Each consented person | Gives consent, runs the assigned tasks, may withdraw at any time | See other participants' assignments |
| Independent scorers | At least two people, not the operator for that task | Score blinded sheets from `score-sheet` | Receive `assignments.json`, the seed, observations or the identity map |
| Data owner (if separate) | Study owner | Decides retention and deletion (section 10) | - |

The code-to-person mapping is held by the study owner, not in `<STUDY>` and
never in the repository, a report, an export or the metadata store
(`desktop/mcp/README.md`).

---

## 2. Before the pilot

### 2.1 Predeclare the study plan

The study plan is the `study_plan` object of a bundle file. Real studies use
`"bundle_kind": "pilot_task_bundle"` (section 2.5). The synthetic
`"synthetic_task_bundle"` is for software checks only. The bundle fixes:

| Key | Rule enforced by code |
|---|---|
| `target_participants` | Integer 4 to 10000, and a multiple of 4 (counterbalance block) |
| `minimum_completed_per_arm` | Integer 1 to 100000. The savings gate needs at least `max(2, minimum_completed_per_arm)` pairs |
| `stopping_rule` | Must be exactly `complete_predeclared_bundle_or_stop_at_target` |
| `stratum_weights` | Decimal weights that sum to exactly 1; each task's `stratum` must be a key |
| `refresh_max_age_hours` | Integer 1 to 168. Admin windows older than this are stale |
| `quality_pass_criterion` (optional) | `correct_only` or `correct_complete_cited_no_unsupported`. Without it, `quality_pass_count` stays null |

Gates in use: the savings gate needs at least `max(2, minimum_completed_per_arm)`
pairs and at least `max(2, target_participants)` participants
(`pilot_study.build_report`). With a target of 8 participants, a 4-participant
cohort cannot produce a supported claim, whatever the data.

### 2.2 Why the plan must not change after results are seen

- The bundle's canonical digest is written as `bundle_sha256` in
  `assignments.json` and in every report. `verify-assignments`, `score-sheet`
  and `report` reject an assignment file whose digest differs from the bundle
  (`assignment_bundle_or_arm_mismatch`).
- The thresholds, pass criterion, strata and task set decide which results pass
  the gates. Changing any of them after an observation or score exists means
  the gate was chosen by looking at the result. The earlier result then cannot
  support a claim under the new rule.
- Before the first session, record the `bundle_sha256` from `assignments.json` in
  the study packet, with a date. If anything in the plan must change, stop,
  record the reason, and start a new study with a new bundle and new codes. The
  study owner decides how earlier data is reported.

### 2.3 Consent

- Consent is per participant and is recorded in two places: `consented_at_utc`
  in the assignment rows (one value per `assign` call, see 3.3), and the
  metadata store's consent (4.5). The store consent is one timestamp for the
  whole store.
- The text the participant reads must cover the purpose, the fields, the
  retention period, the export destination and the withdrawal procedure. The
  study owner approves that wording before the first consent. The tool shows its
  own terms (purpose, stored fields, excluded items, retention) only when
  `pilot_metadata.py consent --confirm` runs, and that same command enables
  collection. So the approved text must be shown to the participant before
  `--confirm` is run.
- Do not start collection if the study owner has not approved the process and
  told participants about it (`desktop/mcp/README.md`).

### 2.4 Synthetic dry run (software check only)

```powershell
& $PY -B desktop\mcp\phase_g_pilot.py fixture-report --output "$env:TEMP\pilot-fixture-report.json" > $null
& $PY -B desktop\mcp\phase_g_pilot.py task-book --output "$env:TEMP\pilot-task-book.json" > $null
```

`fixture-report` shows `evidence_class: synthetic`, `claims.savings_claim: none`
and `enterprise_observed.enterprise_usage_observed: false`. Its
`fixture_rule_checks` are not participant quality. The task book contains the
operator instructions and synthetic sources, with no answer keys.

### 2.5 Real task bundle (authoring)

A real bundle uses `"bundle_kind": "pilot_task_bundle"`. Each task has
`task_id` (format `t_` plus lowercase letters, digits or underscores), `stratum`,
`locale`, `operator_instruction` and `sources`. Each source is one file that
the operator keeps in the study's sources folder:

```json
{"source_id": "src_t_lookup", "format": "csv", "file_name": "lookup.csv",
 "sha256": "<64 lowercase hex>", "bytes": 1234}
```

- `format` is one of `txt`, `md`, `json`, `csv`, `pdf`, `docx`, and the file
  name must end with the same suffix. A file name is a base name only: no
  folders, no `..`, no reserved Windows names (`CON`, `NUL`, `COM1`...), at most
  120 characters, and no name used by another source.
- `bytes` is between 1 and 26214400 (25 MiB). Compute both values from the file
  itself, for example:

```powershell
(Get-FileHash -Algorithm SHA256 "<SOURCES>\lookup.csv").Hash.ToLower()
(Get-Item "<SOURCES>\lookup.csv").Length
```

- Source content is never embedded in the bundle. A bundle that contains
  `content`, `synthetic_*` or answer fields is rejected.

**Answer key.** The expected answers live in a separate file,
`answer-key.json`, not in the bundle and not in any file given to participants
or to the task book:

```json
{"schema_version": "phase_g_study/v1", "study_id": "<STUDY_ID>",
 "bundle_sha256": "<canonical digest of the bundle>",
 "keys": [{"task_id": "t_lookup", "kind": "integer_equal", "expected": 42}]}
```

Each task needs exactly one key. `kind` is `integer_equal`, `boolean_equal` or
`literal_equal`. The key's `bundle_sha256` must match the bundle, so a
changed bundle invalidates the key. Store the key with the scorer's materials,
not with the operator cards. The report only checks the key's digest and task
coverage (`--answer-key`) and never records its values. Scorers use the key
locally; scores are entered on the blinded sheet.

### 2.6 Verify sources before any session

Run this after copying the source files into the sources folder, and again
before the first session and after any file change:

```powershell
& $PY -B desktop\mcp\phase_g_pilot.py verify-sources --bundle "<STUDY>\bundle.json" --sources-dir "<STUDY>\sources" --output "<STUDY>\sources-verified.json"
```

The command checks that each named file sits directly in the folder, is not a
symbolic link, junction or hard link, and matches its declared size and
SHA-256. It exits `2` and lists each failure (`source_missing`,
`source_size_mismatch`, `source_hash_mismatch`, `source_link_refused`, ...) if
anything fails. Do not start sessions until it reports `verified`.

---

## 3. Cohort setup

### 3.1 Generate participant codes

```powershell
& $PY -B desktop\mcp\phase_g_pilot.py generate-participant-codes --count 8 > "<STUDY>\codes.txt"
```

- Count is 1 to 500. Use the bundle's `target_participants`.
- A real code is `p_` followed by 10 characters from `a-z` and `2-7`, for
  example `p_d67rrbqutu`. `pilot_metadata.py participant-code` produces the same
  format.
- Rejected for real cohorts: `P-XXXXXXXX` (the M7 format), `p_` followed by hex,
  and any other string (`participant_code_invalid`). `syn-p-...` codes are for
  synthetic data only.
- The study owner writes the code-to-person mapping offline, in a location that
  is not `<STUDY>`.

### 3.2 Generate scorer codes

```powershell
& $PY -B desktop\mcp\phase_g_pilot.py generate-scorer-codes --count 2 > "<STUDY>\scorers.txt"
```

Use at least two scorer codes. Each code is `s_` plus 10 characters from the
same alphabet. Give each code to one scorer and keep the mapping offline.

### 3.3 Assign in one call

```powershell
$codes = Get-Content "<STUDY>\codes.txt"
$pc = foreach ($c in $codes) { '--participant-code'; $c }
& $PY -B desktop\mcp\phase_g_pilot.py assign --bundle "<STUDY>\bundle.json" $pc --consent-at-utc 2026-10-20T09:00:00Z --seed-out "<STUDY>\seed.txt" --output "<STUDY>\assignments.json" > $null
```

Rules enforced by the code, or that the operator must follow:

- **One call for the whole cohort.** Each call draws a new secret seed and a new
  allocation. A second call for the same cohort produces a different schedule.
  Do not run `assign` twice.
- **`--seed-out` is required for real codes and must be a new file.** An existing
  file fails with `seed_file_exists`. It must not equal `--output`
  (`seed_output_conflict`). `--seed` is rejected with real codes
  (`seed_not_applicable_to_real_cohort`), because the seed is secret.
- **`--consent-at-utc` is required and is one value for the call.** Every task
  start must be after it (`consent_after_observation_start` is checked when
  observations are imported). Use the time at which the last participant's
  consent was given, and make sure every session starts after it.
- **Duplicate codes in one call are rejected** (`participant_code_duplicate`).
- **Cohort size is not checked against `target_participants` at `assign`.** The
  bundle's target must be a multiple of 4; the operator must make the number of
  codes equal to the target.
- The command writes `assignments.json` containing the arm schedule, the
  `seed_commitment` and no seed. Keep it out of scorers' hands.

### 3.4 Publish the commitment

Copy the `seed_commitment` value from `assignments.json` into the study packet,
with the date. The commitment is the SHA-256 of the 64-character seed text. It
is the only value to publish before the study ends. Do not publish
`assignments.json` or `seed.txt` before reveal (section 6).

### 3.5 Seed custody

- `seed.txt` contains 64 hex characters and a newline. The operator keeps it in
  a secure location, separate from `assignments.json` and from the identity
  mapping. Restrict access to the operator and the study owner. Do not place it
  in the repository, the report folder, or a synced folder. The tool does not
  encrypt it.
- Anyone holding both the seed and `assignments.json` can recompute the schedule
  and map blinded codes to arms (section 5.4).

### 3.6 Structure check before sessions

```powershell
& $PY -B desktop\mcp\phase_g_pilot.py score-sheet --bundle "<STUDY>\bundle.json" --assignments "<STUDY>\assignments.json" --output "<STUDY>\score-sheet-template.json" > $null
```

This validates the assignment file's structure and writes a blank score sheet
(section 5). Before reveal, Williams order and blinded codes are checked only for
structure. They are recomputed when the seed is revealed (section 6).

---

## 4. Running sessions

### 4.1 Per participant, before the first task

1. Confirm the consent record for that participant (3.3). Do not start a task
   before the consent time.
2. If metadata collection is used, confirm the store is enabled for the Windows
   profile that records the data (4.5).
3. Print only that participant's task cards. Generate the cards once:

   ```powershell
   & $PY -B desktop\mcp\phase_g_pilot.py task-book --bundle "<STUDY>\bundle.json" --output "<STUDY>\cards\task-book.json" > $null
   ```

   The participant gets only the cards for their own assignments, in their
   `order`. Never give them `assignments.json`.

### 4.2 For each assignment, in its order

1. Look up the row in `assignments.json` with the participant's code. Take its
   `task_id`, `arm` and `order`. The arm is the only thing that changes between
   tasks.
2. Open a fresh Claude Desktop conversation. This is an operator rule; the
   tools do not enforce it.
3. Configure the arm as declared in the study packet's condition profile for
   `A`, `B`, `C_normal` or `C_tuned` (`desktop/mcp/phase-g-economics.md`). The
   tools record the arm; they do not configure Desktop.
4. Place only the task's synthetic sources in an approved root. The observation
   carries `source_snapshot_sha256`, which must equal the digest of the bundle's
   sources for that task (`source_snapshot_mismatch`). The harness checks the
   declared digest only. It cannot see what was actually loaded.
5. Keep unrelated activity out of the time window. Record any that happened (4.4,
   and section 7 for the usage window).
6. Record `start_utc` and `end_utc` in UTC with a `Z` suffix. `end_utc` must not be
   before `start_utc` (`outcome_window_invalid`).
7. Keep failed, timed-out and unsupported attempts. Do not repeat a task to
   replace a result. The report counts one observation per assignment. Any
   repeat needs the study owner's decision first.

### 4.3 Observation file

`<STUDY>\observations.json` is a JSON array with at most one object per
assignment. Assignments with no object count as `not_run` in the denominators.
Each object needs:

- `assignment_id`, `participant_id`, `task_id`, `pair_id`, `arm`, `locale`:
  copied from the assignment row (any mismatch is rejected).
- `status`: `completed`, `failed`, `unsupported`, `bypassed`, `refused`,
  `timed_out` or `not_run`. `not_run` requires `start_utc` and `end_utc` to be
  null.
- `start_utc`, `end_utc`, `counter_version`, `source_snapshot_sha256`.
- `versions`: an object with `desktop`, `extension`, `model`, `settings`. A null
  model means mixed or unknown models.
- `metrics`: `latency_ms`, `retry_count`, `tool_call_count`, `retrieval_bytes`,
  `estimated_input_tokens`, `estimated_output_tokens`, `schema_instruction_tokens`.
  Each null needs a `metric_missing_reasons` entry from `not_collected`,
  `not_available_from_export`, `not_applicable`, `not_run`, `counter_unavailable`.
  Token estimates require a `counter_version`.
- `provider_usage`: `uncached_input_tokens`, `cache_write_5m_tokens`,
  `cache_write_1h_tokens`, `cache_read_tokens`, `billed_output_tokens`, each
  null with a `usage_missing_reasons` entry. Do not split a combined
  cache-write count into 5-minute and 1-hour values.
- `failure_code` (required for `failed` and `timed_out`) and `unsupported_code`
  (required for `unsupported`). Each is a safe identifier matching
  `^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$`. It is not an allowlist.

Estimated tokens are participant-supplied inner-payload estimates. They are not
billing data.

### 4.4 Usage window and unrelated activity

For each (participant, arm) pair, the operator records one administrator usage
window in the window file (section 7). The `unrelated_activity` field takes
`none`, `present_excluded`, `present_unmapped` or `unknown`. Record exclusions and
a mapping confidence (`high`, `medium`, `low`).

### 4.5 Metadata store (optional, M7)

The store is per Windows profile, at `%LOCALAPPDATA%\ContextShrink\pilot\metadata.json`
(`DATA-STORES.md`). There is no `--store` option for production use; that
option is a hidden test hook.

```powershell
& $PY -B desktop\mcp\pilot_metadata.py status
& $PY -B desktop\mcp\pilot_metadata.py consent --retention-days 30          # without --confirm: prints "Cancelled", writes nothing
& $PY -B desktop\mcp\pilot_metadata.py consent --retention-days 30 --confirm  # enables collection and prints the terms
& $PY -B desktop\mcp\pilot_metadata.py record --input "<STUDY>\record-001.json"
& $PY -B desktop\mcp\pilot_metadata.py preview
```

- Retention is 1 to 90 days (default 30). One consent timestamp covers the whole
  store, and every record carries it. Records from several participants in one
  store therefore share one consent time.
- A record is one JSON object (at most 64 KiB) with `participant_code`
  (`p_` format), `task_id` (`t_...`), `arm`, `scorer_status` (`pending`,
  `scored`, `withheld`, `unavailable`), `task_status` (`success`, `failure`,
  `unsupported`, `bypassed`, `timeout`), `source_format`, and every metric key
  present. A null metric needs a `missing_reasons` entry from `not_reported`,
  `not_available`, `not_applicable`, `withheld` or `measurement_error`
  (`not_collected` is rejected). Optional fields: `model` (allowlisted prefixes
  such as `claude`, `gpt`, `gemini`), `effort`, `tool_profile`, `locale`,
  version strings, `pair_id` (`pair_` plus 20 hex), `failure_code` and similar
  codes from a fixed list.
- Rejected: free-text model names, a name-like `task_id`, an extra field such as
  `prompt`, and the M7 `P-` code (`record_invalid`).
- The metadata export is not an input to `phase_g_pilot.py report`. It has a
  different schema and is rejected there (`input_array_required`). Build the
  observation file by hand from the same measurements.
- `preview` shows the complete stored content before any export.

### 4.6 Stop conditions

Stop the session and follow section 9 if: a participant withdraws; a source is
revoked; a run starts with the wrong arm; the machine crashes; or any output
contains something that should not be there.

---

## 5. Scoring

### 5.1 Blinded sheets

```powershell
& $PY -B desktop\mcp\phase_g_pilot.py score-sheet --bundle "<STUDY>\bundle.json" --assignments "<STUDY>\assignments.json" --output "<STUDY>\score-sheet.json" > $null
```

Each row has `score_id`, `blinded_arm_code`, `task_id`, `scorer_code`
(`unassigned`), `status` (`no_score`) and dimension fields set to `unscored`. The
sheet holds no participant code and no arm name.

### 5.2 Per scorer

1. Copy the sheet once per scorer. In each copy, set `scorer_code` to that
   scorer's `s_` code on every row.
2. Score only rows whose task was `completed`. For those, set `status` to
   `scored` and fill every dimension:
   - `correctness`: `correct`, `partial`, `incorrect`
   - `completeness`: `complete`, `partial`, `incomplete`
   - `citations`: `complete`, `partial`, `missing`, `not_required`
   - `unsupported_claim`: `yes`, `no`, `unknown`
   - `omission_codes`: a list of safe identifiers
3. Set `status` to `no_score` for any other row. Keep every dimension `unscored`.
4. Do not change `score_id` or `blinded_arm_code`.

### 5.3 Combine

Each assignment needs one row per scorer, with the same `score_id` and
different `scorer_code`s. Two rows from the same scorer for one assignment are
rejected (`duplicate_scorer_for_assignment`). At most 8 rows per assignment. Save
the combined file as the score-sheet dict: `schema_version` and `study_id` copied
from `score-sheet.json`, and `scores` as the list of rows.

Non-completed tasks may only be `no_score` (`noncompleted_task_must_be_unscored`).

### 5.4 Agreement and blinding

- The report gives percent agreement for each binary field over every pair of
  scorers on one assignment. Cohen's kappa is computed only when exactly two
  distinct scorers appear in the file.
- The code has no agreement threshold. The study owner must predeclare what
  agreement is acceptable. Without one, do not claim scorer reliability.
- Scorers must not receive `assignments.json`, the seed, observations or the
  identity mapping. Scores must be final before reveal, because after reveal the
  blinded codes map to arms (section 6).

---

## 6. After sessions: reveal and verify

Do this only when observations, scores and usage windows are final (section 7).

1. Verify the seed against the commitment and recompute the schedule:

   ```powershell
   & $PY -B desktop\mcp\phase_g_pilot.py verify-assignments --bundle "<STUDY>\bundle.json" --assignments "<STUDY>\assignments.json" --seed-file "<STUDY>\seed.txt"
   ```

   Expected: `"status": "verified"`, `"seed_mode": "committed_secret"`, and the
   same `seed_commitment` as in the study packet. Possible failures include
   `seed_commitment_mismatch` (wrong seed or edited assignments) and
   `assignment_order_seed_mismatch` or `assignment_blind_code_seed_mismatch`
   (the schedule was altered).
2. Produce the report with the revealed seed. The report then shows
   `allocation.verification: verified_with_revealed_seed`. Without
   `--seed-file` it shows `deferred_until_seed_reveal`.

   ```powershell
   & $PY -B desktop\mcp\phase_g_pilot.py report --bundle "<STUDY>\bundle.json" --assignments "<STUDY>\assignments.json" --observations "<STUDY>\observations.json" --scores "<STUDY>\scores-combined.json" --usage-windows "<STUDY>\admin-windows.json" --seed-file "<STUDY>\seed.txt" --as-of-utc 2026-10-27T12:00:00Z --output "<STUDY>\pilot-report.json"
   ```

   `--as-of-utc` is the time the report is produced. Freshness is measured from
   it. `--usage-windows` is optional; omit it and the enterprise status is
   `pending`.

3. Keep the seed separately after reveal. Anyone with the seed and the
   assignment file can unblind the arms.

---

## 7. Billing evidence (Enterprise usage)

### 7.1 Two paths, and only one of them checks provenance

| Path | Input | Provenance check | Savings decision |
|---|---|---|---|
| `phase_g_pilot.py report --usage-windows` | JSON array of windows (`source: admin_export`) | None. No export digest is read | Always `provenance_verified: false`, so the pilot never produces a supported claim |
| `phase_g_economics.py --admin-export` | M13 input file with `export_sha256` on each window | SHA-256 of each supplied export must equal a window's `export_sha256` | Gated by provenance, evidence class, minimums and the interval |

Use the pilot window file to check mapping and coverage. Use the M13 economics path
for any claim about cost.

### 7.2 Wait for the refreshed export

- Request an administrator export that covers the study windows after the last
  task's `end_utc`.
- Each window's `refreshed_at_utc` must be on or after its `end_utc` (pilot:
  `usage_window_time_invalid`). It must also be within
  `refresh_max_age_hours` of the report's as-of time (pilot: `--as-of-utc`;
  economics: `as_of_utc` in the input). Older windows are `stale_or_future`, and
  the status stays `pending`.
- If a newer export replaces an older one, keep both, and record which one was
  used.

### 7.3 Window mapping rules

- Pilot: exactly one window per (participant, arm). Its `assignment_ids` must equal
  the set of that participant's observed assignments for that arm
  (`usage_window_mapping_incomplete` otherwise). Mapping confidence `low`, or
  `exclusions_recorded: false`, leaves the status `pending`.
- Economics: the window must map consented, pseudonymous, observed assignments and
  contain the full time of each mapped task. Mapping confidence must be `medium` or
  `high`. `unrelated_activity` must be `none`, or `present_excluded` with at least
  one exclusion code. `unknown` or `present_unmapped` withholds normalized arm costs.
- A window is never divided among tasks, requests or models. Costs are reported per
  arm.
- Do not sum amounts in different currencies. A single arm keeps its own currency.
- The pilot does not check `unrelated_activity`. Apply the economics rule by hand
  when reading a pilot report.

### 7.4 Export handling and the M13 run

1. Keep each export file exactly as downloaded. Any byte change changes the
   digest. Record its SHA-256 in the study packet:

   ```powershell
   Get-FileHash -Algorithm SHA256 "<STUDY>\exports\enterprise-2026-10-20.csv"
   ```

2. Each export must be 64 MiB or smaller (`admin_export_too_large` otherwise).
   Pass one `--admin-export` per file.
3. Build the M13 input JSON by hand from the study's files. The repository has no
   generator for it. Its top level is `schema_version: "phase_g_economics/v1"`,
   plus `as_of_utc`, `evidence_class`, `study_bundle`, `assignment_set`,
   `observations`, `scores`, `condition_profiles`, `runs`, `rate_cards`,
   `sensitivity_scenarios` and `usage_windows`. `phase-g-economics.md` gives the
   rules for each part. Input is limited to 16 MiB.
4. Run:

   ```powershell
   & $PY -B desktop\mcp\phase_g_economics.py --input "<STUDY>\m13-input.json" --output "<STUDY>\economics-report.json" --admin-export "<STUDY>\exports\enterprise-2026-10-20.csv"
   ```

   Repeat `--admin-export` for each export file. The report records each file's
   SHA-256 and byte size under `provenance.admin_exports`, never its path.
   **Not run:** a real window with a matching digest. The only run I made used the
   synthetic fixture with a dummy export, which showed `verified_window_count: 0`
   and `provenance_verified: false`.

### 7.5 What makes provenance verified

`provenance.provenance_verified` is true only when all of these hold:

- the evidence class is not synthetic;
- at least one usage window exists;
- every window's `export_sha256` equals the SHA-256 of a file passed with
  `--admin-export`.

Otherwise it is false. The enterprise status then becomes `admin_export_unverified`
and no savings claim is made.

A matching digest shows that the window and the supplied file are the same bytes.
It does not show that the export is genuine, who produced it, or that the
administrator console was the source. The study packet must record how each
export was downloaded and by whom.

---

## 8. Reading the report

### 8.1 Pilot report (`phase_g_pilot.py report`)

| Field | Meaning |
|---|---|
| `claims.savings_claim` | The decision for the candidate-minus-reference comparison. In the pilot it is always `none` in practice, because provenance is never verified |
| `payload_estimate.paired_comparison.statistical_comparison.decision` | `claim` and `reasons` for B minus A, the candidate minus the reference |
| `...statistical_comparison.interval` | 95% cluster-bootstrap bounds for the mean of B minus A, per participant-task pair. Negative means B used fewer estimated tokens |
| `payload_estimate.paired_comparison.signed_token_delta_a_minus_b` | Sum of A minus B. Opposite sign to the interval; do not mix them |
| `enterprise_observed.status` and `reason` | `pending`, `partial` or `available_with_attribution_limits`, with the first failed gate |
| `blind_scoring.agreement` | Percent agreement per field; kappa only with exactly two scorers |
| `arms.<arm>.quality_pass_count` | Null unless the bundle declares a pass criterion |

### 8.1a Economics report (`phase_g_economics.py`)

| Field | Meaning |
|---|---|
| `savings_claim` (top level) | The decision for `B_full` against `C_normal`, with its reasons. Gated by evidence class, `provenance.provenance_verified`, minimums and the interval |
| `provenance.provenance_verified` and `provenance.admin_exports` | Whether every window's `export_sha256` matched a supplied export; the supplied files' digests and sizes (no paths) |
| `provenance.verified_window_count` and `unverified_window_count` | Counts behind `provenance_verified` |
| `enterprise_observed.status` and `reason` | Observed window status. Unverified exports give `admin_export_unverified` |
| `modeled_economics` | Costs in `placeholder_units` with `rates_are_placeholder: true` for placeholder cards |
| `factorial` | Section 8.6, or `{"status": "not_run"}` |

### 8.2 Decision reasons

Each `reasons` entry is a gate that failed. Gates are checked in this order:
`synthetic_evidence` or `evidence_not_consented_deidentified`, then
`provenance_unverified`, `below_min_units`, `below_min_clusters`,
`insufficient_interval` (fewer than 2 pairs or 2 participants).
`interval_includes_zero` appears only when every gate passed.

| `claim` | When |
|---|---|
| `none` | Any gate failed. The reasons list says which |
| `inconclusive` | All gates passed and the interval includes zero |
| `savings_supported` | All gates passed and the upper bound is below zero |
| `cost_increase_supported` | All gates passed and the lower bound is above zero |

### 8.3 Interval meaning

- A 95% percentile interval from a cluster bootstrap over participants,
  10,000 resamples. It is deterministic.
- The bootstrap seed is the fixed reporting constant `20261014`, not the
  allocation seed. Committed cohorts keep their secret.
- Bounds are decimal strings. An interval is an estimate of uncertainty, not a
  guarantee.

### 8.4 Placeholder and priced rates

- In `phase-g-economics.md` terms, a card with `rate_source: placeholder` gives
  `currency: null`, amounts as `placeholder_units`, and `rates_are_placeholder:
  true`. Do not convert these to dollars or quote them.
- Money appears only for `published_price_list` or `contract` cards, with an
  `as_of` date. Placeholder and priced cards cannot be mixed in one report.
- The pilot has no rate cards. Its `modeled_economics.status` is `unavailable`.

### 8.5 Estimates are not savings

- A cost-per-task figure is a point estimate. It carries `kind: point_estimate`
  and its denominator `n`. Do not quote it as a saving.
- The interval `estimate` is the mean difference, not a saving. A saving is claimed
  only when `savings_claim` is `savings_supported`.
- Token estimates from the pilot are participant-supplied and are not billing data.

### 8.6 Factorial effects (economics report, `factorial`)

Each participant-task block gives one unit. The effects are:

- input reduction: `((F10 - F00) + (F11 - F01)) / 2`;
- output contract: `((F01 - F00) + (F11 - F10)) / 2`;
- interaction: `F11 - F10 - F01 + F00`.

Input reduction and output contract get savings decisions. The interaction is
reported with an interval and a `none` claim. With no `F00`, `F10`, `F01` or `F11`
runs, `factorial` is `{"status": "not_run"}`. The validation rules for factorial
condition profiles are being finished separately. Re-read `phase-g-economics.md`
before relying on those checks.

---

## 9. Incident handling

### 9.1 Suspected data exposure

1. Stop. Do not run more `record`, `export` or sharing commands. Do not start more
   sessions on that machine.
2. Note the affected file paths. Do not copy or paste contents into any message.
3. Tell the study owner immediately.
4. Remove what the owner approves, in this order:
   - metadata store: `& $PY -B desktop\mcp\pilot_metadata.py delete --confirm`
     (removes all records and the consent for that profile);
   - exports and copies: delete by hand, since the tools never remove exports;
   - staged text and artifact folders: follow `DATA-STORES.md` "Delete everything"
     steps 1 to 4, with no companion or broker running;
   - recovery key: last, as `DATA-STORES.md` step 5 says. Deleting it first leaves
     recovery folders unverifiable.

### 9.2 Wrong arm run

The harness cannot detect this. An observation must match its assignment's arm,
so a wrong-arm run cannot be imported under that assignment
(`observation_assignment_mismatch`). The steps below are procedure, not code:

1. Stop the session. Write the deviation, with timestamps, in the study log.
2. Do not relabel the run as the arm that was actually used.
3. Record the assignment as `not_run` (start and end null) in `observations.json`.
   It stays in the assigned count.
4. Do not score it. Scores are only allowed for `completed` observations.
5. The study owner decides whether any rerun is part of the study, and records it.

### 9.3 Crash mid-session

- **Metadata store:** writes are atomic (a temporary file, then a replace or
  link). A crash leaves the last complete store, and a half-written record never
  appears. The interrupted task has no record. Record its outcome from your notes
  as `failed` or `timed_out` with a `failure_code` in `observations.json`, so it
  stays in the denominator.
- **Companion and broker:** before the next session, start the companion again
  with the same staging root. Recovery removes only expired session folders whose
  manifests verify, whose lease is free, and whose files match
  (`DATA-STORES.md`, "Staged text and recovery"). Live text stays until its TTL
  expires, at most 3,600 seconds. A stale broker capability can remain after a
  crash until the next issue replaces it.
- Afterwards, check each approved root for `.contextshrink-session-*` and
  `.contextshrink-artifacts-*`.

### 9.4 Revoked source

Record the task as `failed` with `failure_code: source_revoked` (the metadata
store's fixed reason list includes this code). Keep its start and end times. Do not
replace the source with another one. The bundle's `t_updated_revoked` task
describes the same case in the synthetic bundle. Whether a revoked-source attempt
is counted as a failure is the study owner's decision; this runbook records it
as one.

### 9.5 Consent withdrawal

1. Stop sessions for that participant at once.
2. Write the withdrawal file `<STUDY>\withdrawals.json`. It names only the
   pseudonymous code and the time the withdrawal was received:

   ```json
   {"schema_version": 1, "study_id": "<STUDY_ID>",
    "withdrawals": [{"participant_id": "p_xxxxxxxxxx", "withdrawn_at_utc": "2026-10-20T09:00:00Z"}]}
   ```

   The file must list each withdrawn code once. Synthetic codes and unknown
   formats are rejected.
3. Metadata store: `pilot_metadata.py withdraw --participant-code CODE --confirm`
   deletes only that participant's records and keeps the others. Without
   `--confirm` it writes nothing. Do not edit the JSON by hand. (`pilot_metadata.py
   delete --confirm` still removes the whole store and is not a withdrawal.)
4. Study files: rerun the report with the withdrawal file:

   ```powershell
   & $PY -B desktop\mcp\phase_g_pilot.py report --bundle "<STUDY>\bundle.json" --assignments "<STUDY>\assignments.json" --observations "<STUDY>\observations.json" --scores "<STUDY>\scores-combined.json" --withdrawals "<STUDY>\withdrawals.json" --as-of-utc <UTC> --output "<STUDY>\report.json"
   ```

   The report drops every observation, score and usage row for withdrawn
   participants before any statistics. It marks their assignments as
   `withdrawn` per arm and shows only the count `withdrawn_participants`. Their
   rows also leave `min_units` and `min_clusters`. The assignment file itself
   stays unchanged, so `verify-assignments` still works.
5. Remove that participant's rows from `observations.json`, `scores-combined.json`
   and the usage-window file as well, if you keep those files for other uses.
6. The study owner removes the code-to-person entry.
7. Record the withdrawal date, and what was removed and what was kept, in the study
   packet.

---

## 10. Data refresh, retention and end-of-pilot deletion

### 10.1 Retention rules the code enforces

- Metadata consent: 1 to 90 days, default 30. The store deletes itself the next time
  it is read after expiry; it does not delete on a timer. Use
  `pilot_metadata.py status` to see `expires_at`.
- Pilot exports (`pilot_metadata.py export`) are never deleted by the tool. Their
  size limit is 16 MiB.
- Pilot study files (`assignments.json`, `seed.txt`, `observations.json`, score
  files, window files, reports, administrator exports, the identity mapping) have
  no retention code. The study owner sets retention for each and records the date.

### 10.2 Refresh

- Admin usage refresh: at most `refresh_max_age_hours` (1 to 168) before the report's
  as-of time, and no earlier than the last task's end time. A stale window stops the
  enterprise status from becoming available.

### 10.3 End-of-pilot checklist

1. Write the final `report` and, if used, the M13 economics report. Record every
   export's SHA-256.
2. The study owner signs off the retention decision for each file in 10.1.
3. Metadata: `preview`, then `export --output` to an approved location if the
   owner needs the data, then `delete --confirm`. Check that `status` reports
   `enabled: false`.
4. Remove working copies: task-book cards, temporary files in `%TEMP%`, and any
   file under `<STUDY>` the owner does not keep.
5. Identity mapping: destroy it on the owner's schedule.
6. Exports: delete by hand.
7. On every machine used, follow `DATA-STORES.md` "Delete everything" steps 1 to 6.
8. Seed: keep until the owner's retention decision for it, separate from the
   assignment file, or destroy it. Keep the commitment in the study packet.
9. Check the repository has no study files: `git status` must show none of the
   `<STUDY>` artifacts.
10. Record completion, the date and the data owner's approval in the study packet.

See also `DATA-STORES.md` (what each store holds and how it is removed) and
`SUPPORTED-SCOPE.md` (the source and output limits).
