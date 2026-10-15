# Phase G M13 offline economics evaluator

`phase_g_economics.py` consumes a `phase_g_study/v1` bundle, assignments, observations, blind scores, optional administrator usage windows, explicit model rate cards, and an M13 per-request sidecar. It validates the study bundle and assignment set with `horizon/desktop_mcp/pilot_study.py` (the M6 contract), derives savings decisions and factorial effects with `horizon/desktop_mcp/evidence_stats.py` and `horizon/desktop_mcp/factorial.py`, and makes no provider or network calls. It does not allocate administrator billing windows across tasks or requests.

The checked-in `phase-g-economics-fixture.json` is deterministic synthetic software-test data. Run it from the repository root (or with `PYTHONPATH` set to it) with the repository's Python environment:

```powershell
& 'C:\Users\Burhan\OneDrive\Desktop\ContextShrink\.venv\Scripts\python.exe' desktop/mcp/phase_g_economics.py `
  --input desktop/mcp/phase-g-economics-fixture.json `
  --output "$env:TEMP\phase-g-economics-report.json"
```

Add `--admin-export PATH` once per administrator export file to record its digest (see Provenance below).

The generated output should match `phase-g-economics-report.json`. Its report marks enterprise observations pending, `enterprise_usage_observed` false, placeholder rates, and the savings claim `none`. The fixture is not participant evidence, observed spend, a pricing estimate, or a product-savings claim.

## Input rules

The top-level object uses `schema_version: "phase_g_economics/v1"` and includes the M6 bundle, assignment set, observations and scores alongside `condition_profiles`, `runs`, `rate_cards`, `sensitivity_scenarios`, and `usage_windows`. Input files are read with a 16 MiB cap; duplicate JSON keys, non-finite numbers, malformed UTF-8 and structures nested beyond 128 levels are rejected with stable sanitized error codes.

The bundle and assignment set follow the pilot contract. Task IDs match `^t_[a-z0-9_]{1,40}$`, and each task carries `synthetic_observed`. Pseudonymous participant codes match `^p_[a-z2-7]{10}$`; synthetic codes match `^syn-p-[a-z0-9]{4,24}$`. Synthetic evidence uses `seed_mode: public_synthetic` with an integer `seed`, and its allocation is recomputed from that seed. Consented evidence must use `seed_mode: committed_secret` with `seed: null` and a `seed_commitment`; its Williams order and blind aliases are structural-only here until the seed is revealed through `pilot_study`. The report states which mode applies in `sample.allocation_verification`.

`condition_profiles` is a complete, predeclared matrix for A, B full package, C-normal, C-tuned and each B ablation. For every condition, record the model and settings IDs, effort, concise-instruction profile, tool-settings profile, and whether source access, preparation, retrieval, deterministic compute and artifact rendering are enabled, disabled or not applicable. C-normal profile IDs describe the participant's recorded normal upload/retrieval/settings; C-tuned must match B on model, settings, effort and concise-instruction profile. Each B ablation must match B full package on every profile field except the single named mechanism, which it disables. The report includes this matrix and per-arm unsupported/bypassed shares over assigned tasks. These declarations make the contrast assumptions reviewable; the condition label by itself is not ablation evidence.

Use stable profile IDs that resolve to the exact configuration recorded in the study packet. For example, `settings-v1` identifies the recorded model settings, `concise-v1` the concise instruction set, and `tool-settings-v1` the enabled tool/schema configuration. Do not put participant prompts or source contents in these profile IDs. A changed model/settings/effort or instruction profile is a different condition and must be declared as such; the validator rejects an ablation that changes more than its named mechanism.

Each run has a condition, assignment, snapshot/model/settings/counter identifiers, outcome, retries and latency, quality fields, an output contract, incremental service costs, and its request sidecar. Every request lists a model plus all five provider usage buckets: uncached input, 5-minute cache write, 1-hour cache write, cache read and billed output. Requests are costed independently against that model's explicit rate card. Output-token estimates are diagnostic and are not added to billed output.

`service_cost_status` is `complete` when all incremental services are known, including a known zero-cost list, or `unknown` with a bounded `service_cost_missing_reason`. A service item must explicitly declare `included_in_provider_cost: false`; an already-included amount is rejected. Unknown rates, usage buckets or service charges propagate as unavailable totals, never zero. Currencies must be consistent within modeled costs. Failed and retried runs retain their incurred cost. No live prices or fallback rates are built in.

The factorial conditions `F00`, `F10`, `F01` and `F11` are optional run condition IDs. Each attaches to its participant-task block's B assignment. Without them, `factorial` is `{"status": "not_run"}`.

Every factorial ID that a run uses must have a `condition_profiles` entry, or the run is rejected with `factorial_profile_missing`. The 2x2 is build plan section 20's input reduction by concise output contract. The ID digits fix each cell: the first digit is input reduction and the second is output contract (`F10` = input on, output off). Factorial profiles carry the usual fields plus two JSON booleans, `input_reduction` and `output_contract`, which must match the digits. The rule is checked and any violation is rejected with `factorial_condition_invalid`:

- Input reduction is on when `preparation` and `retrieval` are both `enabled`, and off when both are `disabled`.
- Output contract is decided by `concise_instructions`. `F00` and `F10` (output off) share one value, `F01` and `F11` (output on) share a different value, and any other pattern is rejected.
- Every other setting is identical across the four cells: `model`, `settings`, `effort`, `artifact`, `compute`, `source_access` and `tool_settings`. `artifact` is a separate mechanism and is held fixed, so artifact rendering is never the output factor.

The checked-in fixture uses `synthetic-recorded-normal-v1` for `F00`/`F10` and `synthetic-concise-v1` for `F01`/`F11`, with artifact disabled in all four cells.

For baseline conditions, task-level M6 usage is reconciled against the sum of the M13 request sidecar when the M6 bucket is present. Mixed-model tasks may use a null M6 model and still list each request model in M13. Pairwise deltas require equal non-null source snapshot, model, settings and counter version; mismatch removes the pair delta only, not either run from cost coverage. Signed differences retain negative values.

Administrator windows follow the stable M6 fields, including exact mapped assignment IDs, `source: "admin_export"`, UTC start/end/refresh, confidence, explicit unrelated-activity and exclusion metadata, bucket missing-reason maps, cost/currency and `export_sha256`. A window must map consented pseudonymous observed assignments, contain their full task windows, and be refreshed within the declared study-plan age. Attribution requires medium/high mapping confidence, recorded exclusions, and unrelated activity classified as `none` or `present_excluded` with at least one exclusion code. `unknown` or `present_unmapped` activity withholds normalized arm costs. Multiple currencies are never summed across arms; a single arm may retain its own currency and total. Windows remain intact and are reported by arm; they are never divided among tasks, requests or models.

## Real bundles and withdrawals

A `pilot_task_bundle` (real tasks, with `sources` file descriptors and no synthetic fields) is accepted. It requires consented de-identified evidence, and its sources are validated by `pilot_study.validate_bundle`. Source content is never embedded.

`--withdrawals PATH` takes a withdrawal file validated by `pilot_study.validate_withdrawals` against the study id. Withdrawn participants' observations, scores, runs, usage windows and factorial cells are removed before any statistic or decision, and their assignments are removed from the analysis set. The assignment digest and `sample.assignment_count` still cover the full cohort. Withdrawn participants count toward neither `min_units` nor `min_clusters`. The report shows only `sample.withdrawn_participants` and `sample.withdrawn_assignments_by_arm`, never participant codes. A withdrawal code that is not in the cohort is rejected with `withdrawal_unknown_participant`.

## Provenance and rates

**Admin exports.** `--admin-export PATH` can be repeated. Each file is read in bounded chunks (64 MiB cap), and its SHA-256 and byte size are recorded under `provenance.admin_exports`. Paths never appear in the report. Every usage window's `export_sha256` must match a supplied export for the window to be verified. If any window is unverified, `provenance_verified` is false, `enterprise_usage_observed` is false, and the enterprise status is `admin_export_unverified`. Synthetic input is never provenance-verified. This is integrity and traceability, not proof that an export is genuine: a matching digest shows the window and the supplied file are the same bytes, nothing more.

**Rate cards.** Each rate card requires `rate_source` (`placeholder`, `published_price_list` or `contract`) and `as_of` (`YYYY-MM-DD`). A placeholder card has `currency: null`. If any card is a placeholder, the report emits no currency and no money: every modeled amount is labelled `{"placeholder_units": "<decimal>"}`, currency fields are null, `amount_unit` is `placeholder_units` and `rates_are_placeholder` is true. Placeholder and priced cards cannot be mixed in one report, and priced incremental service costs are rejected under placeholder rates. Money only appears for `published_price_list` and `contract` rates. The synthetic fixture uses placeholder rates.

## Report interpretation

The report keeps modeled request-level economics separate from observed administrator-window evidence. Modeled cost per successful task uses total assigned-task spend, including failed attempts, divided by the declared completed-and-quality-passing task count. The stratum-weighted estimate is weighted cost per assigned task divided by weighted success rate; a zero-success stratum contributes zero to the rate and its costs remain in the numerator. A zero overall success rate or missing cost/assignment evidence yields `null`, not zero. Cost-per-task ratios are point estimates: each carries `kind: point_estimate` and its denominator `n` in a `cost_per_*_basis` object.

**Savings decision.** Each paired comparison builds one unit per matched pair, with the participant as the cluster and candidate-minus-reference cost as the difference. `paired_difference_interval` gives a 95% cluster percentile bootstrap. Its seed is a fixed reporting constant (`BOOTSTRAP_SEED = 20261014`), not the allocation seed, so committed cohorts keep their secret. `savings_decision` gates each claim on evidence class, verified provenance, at least `minimum_completed_per_arm` pairs, and at least `target_participants` participants. Both minimums are floored at 2 because `evidence_stats` needs two units and two clusters for any interval. The top-level `savings_claim` is the decision for `B_full` against `C_normal`. The enterprise decision uses per-participant B minus C-normal window costs, and it is `none` unless the enterprise status is `available_with_attribution_limits`. Each decision lists its reasons, such as `synthetic_evidence`, `provenance_unverified`, `below_min_units`, `below_min_clusters`, `interval_includes_zero` or `insufficient_interval`.

**Factorial.** Runs under `F00`, `F10`, `F01` and `F11` form one unit per participant-task block with cluster = participant and cost from the same per-run cost. They go through `analyze_factorial` with the same gates. The checked-in slice has two blocks with costs 1.0, 0.6, 0.8 and 0.4 token-units, so the effects are input reduction -0.4, output contract -0.2 and interaction 0. Synthetic success is never scored, so its success rates are 0 and are not evidence. The interaction never makes a savings claim.

The report does not assert causality, additive mechanism gains, universal savings, or an Enterprise result. `enterprise_usage_observed` indicates that real pseudonymous admin-window records were imported and verified, while status and coverage explain whether they are sufficient for full observed summaries. `causal_claim` is always false. `savings_claim` is the computed decision above, and it is `none` for every synthetic input.

Observed window cost is summarized by arm and kept in the administrator's currency. Cost per assigned task divides that arm's complete mapped window total by its assigned count. Cost per successful task is emitted only when blind scoring is complete and at least one quality success exists. Partial buckets, missing or stale windows, incomplete mapping, unrecorded or unreviewed unrelated activity, conflicting currencies within an arm, unverified exports or incomplete scoring prevent an available status and leave the affected values null.

The evidence class is enforced: synthetic inputs cannot contain pseudonymous assignments or administrator usage windows, and synthetic score rows cannot be marked `scored`. Synthetic quality-rule checks remain separate diagnostics and do not count as participant-quality successes or observed Enterprise data.

## Validation

```powershell
& 'C:\Users\Burhan\OneDrive\Desktop\ContextShrink\.venv\Scripts\python.exe' -m pytest -q -p no:cacheprovider tests/desktop_mcp/test_phase_g_economics.py
& 'C:\Users\Burhan\OneDrive\Desktop\ContextShrink\.venv\Scripts\python.exe' -m ruff check --no-cache desktop/mcp/phase_g_economics.py tests/desktop_mcp/test_phase_g_economics.py
```

Real enterprise and blinded-quality evidence still requires an approved consented pilot and refreshed administrator export. This evaluator only calculates with records an operator has explicitly supplied.
