# Phase G M13 offline economics evaluator

`phase_g_economics.py` consumes a `phase_g_study/v1` bundle, assignments, observations, blind scores, optional administrator usage windows, explicit model rate cards, and an M13 per-request sidecar. It is a standard-library-only offline evaluator. It imports no M6 implementation, makes no provider/network calls, and does not allocate administrator billing windows across tasks or requests.

The checked-in `phase-g-economics-fixture.json` is deterministic synthetic software-test data. Run it with the repository's Python 3.12 environment:

```powershell
& 'C:\Users\Burhan\OneDrive\Desktop\ContextShrink\.venv\Scripts\python.exe' desktop/mcp/phase_g_economics.py `
  --input desktop/mcp/phase-g-economics-fixture.json `
  --output "$env:TEMP\phase-g-economics-report.json"
```

The generated output should match `phase-g-economics-report.json`. Its report marks enterprise observations pending, `enterprise_usage_observed` false, and the savings claim `none`. The fixture is not participant evidence, observed spend, a pricing estimate, or a product-savings claim.

## Input rules

The top-level object uses `schema_version: "phase_g_economics/v1"` and includes the M6 bundle, assignment set, observations and scores alongside `condition_profiles`, `runs`, `rate_cards`, `sensitivity_scenarios`, and `usage_windows`. The M6 bundle digest must match the assignment set. The independent validator accepts the stable `phase_g_study/v1` JSON contract without importing its Python module. Input files are read with a 16 MiB cap; duplicate JSON keys, non-finite numbers, malformed UTF-8 and structures nested beyond 128 levels are rejected with stable sanitized error codes.

`condition_profiles` is a complete, predeclared matrix for A, B full package, C-normal, C-tuned and each B ablation. For every condition, record the model and settings IDs, effort, concise-instruction profile, tool-settings profile, and whether source access, preparation, retrieval, deterministic compute and artifact rendering are enabled, disabled or not applicable. C-normal profile IDs describe the participant's recorded normal upload/retrieval/settings; C-tuned must match B on model, settings, effort and concise-instruction profile. Each B ablation must match B full package on every profile field except the single named mechanism, which it disables. The report includes this matrix and per-arm unsupported/bypassed shares over assigned tasks. These declarations make the contrast assumptions reviewable; the condition label by itself is not ablation evidence.

Use stable profile IDs that resolve to the exact configuration recorded in the study packet. For example, `settings-v1` identifies the recorded model settings, `concise-v1` the concise instruction set, and `tool-settings-v1` the enabled tool/schema configuration. Do not put participant prompts or source contents in these profile IDs. A changed model/settings/effort or instruction profile is a different condition and must be declared as such; the validator rejects an ablation that changes more than its named mechanism.

Each run has a condition, assignment, snapshot/model/settings/counter identifiers, outcome, retries and latency, quality fields, an output contract, incremental service costs, and its request sidecar. Every request lists a model plus all five provider usage buckets: uncached input, 5-minute cache write, 1-hour cache write, cache read and billed output. Requests are costed independently against that model's explicit rate card. Rates declare currency and `per_1m_tokens`; output-token estimates are diagnostic and are not added to billed output.

`service_cost_status` is `complete` when all incremental services are known, including a known zero-cost list, or `unknown` with a bounded `service_cost_missing_reason`. A service item must explicitly declare `included_in_provider_cost: false`; an already-included amount is rejected. Unknown rates, usage buckets or service charges propagate as unavailable totals, never zero. Currencies must be consistent within modeled costs. Failed and retried runs retain their incurred cost. No live prices or fallback rates are built in.

For baseline conditions, task-level M6 usage is reconciled against the sum of the M13 request sidecar when the M6 bucket is present. Mixed-model tasks may use a null M6 model and still list each request model in M13. Pairwise deltas require equal non-null source snapshot, model, settings and counter version; mismatch removes the pair delta only, not either run from cost coverage. Signed differences retain negative values.

Administrator windows follow the stable M6 fields, including exact mapped assignment IDs, `source: "admin_export"`, UTC start/end/refresh, confidence, explicit unrelated-activity and exclusion metadata, bucket missing-reason maps, and cost/currency. A window must map consented pseudonymous observed assignments, contain their full task windows, and be refreshed within the declared study-plan age. Attribution requires medium/high mapping confidence, recorded exclusions, and unrelated activity classified as `none` or `present_excluded` with at least one exclusion code. `unknown` or `present_unmapped` activity withholds normalized arm costs. Multiple currencies are never summed across arms; a single arm may retain its own currency and total. Windows remain intact and are reported by arm; they are never divided among tasks, requests or models.

## Report interpretation

The report keeps modeled request-level economics separate from observed administrator-window evidence. Modeled cost per successful task uses total assigned-task spend, including failed attempts, divided by the declared completed-and-quality-passing task count. The stratum-weighted estimate is weighted cost per assigned task divided by weighted success rate; a zero-success stratum contributes zero to the rate and its costs remain in the numerator. A zero overall success rate or missing cost/assignment evidence yields `null`, not zero.

Observed window cost is summarized by arm and kept in the administrator's currency. Cost per assigned task divides that arm's complete mapped window total by its assigned count. Cost per successful task is emitted only when blind scoring is complete and at least one quality success exists. Partial buckets, missing/stale windows, incomplete mapping, unrecorded or unreviewed unrelated activity, conflicting currencies within an arm, or incomplete scoring prevent an available status and leave the affected values null.

The report does not assert causality, additive mechanism gains, universal savings, or an Enterprise result. `enterprise_usage_observed` indicates that real pseudonymous admin-window records were imported, while status and coverage explain whether they are sufficient for full observed summaries. `causal_claim` is always false and `savings_claim` is always `none`.

The evidence class is enforced: synthetic inputs cannot contain pseudonymous assignments or administrator usage windows, and synthetic score rows cannot be marked `scored`. Synthetic quality-rule checks remain separate diagnostics and do not count as participant-quality successes or observed Enterprise data.

## Validation

```powershell
& 'C:\Users\Burhan\OneDrive\Desktop\ContextShrink\.venv\Scripts\python.exe' -m pytest -q -p no:cacheprovider tests/desktop_mcp/test_phase_g_economics.py
& 'C:\Users\Burhan\OneDrive\Desktop\ContextShrink\.venv\Scripts\python.exe' -m ruff check --no-cache desktop/mcp/phase_g_economics.py tests/desktop_mcp/test_phase_g_economics.py
```

Real enterprise and blinded-quality evidence still requires an approved consented pilot and refreshed administrator export. This evaluator only calculates with records an operator has explicitly supplied.
