# Compact-edit tool pilot

This is a preview-only local MCP adapter and a paired Claude Code pilot.
It does not register a server in user settings or alter production source.

The follow-up [automatic proxy framework](PROXY_AUTOMATION_FRAMEWORK.md) documents
an always-active decision controller, eligible use cases, transparent native
tool translation, replay/streaming requirements, cost gates and account
measurement. Its bounded production adapter core is now implemented in
`horizon/proxy/compact_edits/`. A Claude cohort boundary and native-client
validation now exist; economic/full-pipeline qualification and deployment remain
pending. See the [cohort review](../cohort/REPORT.md) and the
[Claude Code / Codex implementation notes](../../../docs/compact-edit-adapters.md).

See [BEFORE_AFTER.md](BEFORE_AFTER.md) for current versus proposed capability,
the client-specific evidence, task costs and correctly weighted savings scenarios.

## Ready and locally validated

`adapter.py` binds receipts to file contents and supports a restricted grammar:
literal replacements, exact keyword replacements and literal-only ModelPricing
row clones. Unsafe renames and arbitrary replacement-function operations fall
back to native exact search/replace. A failed multi-operation preview publishes
no partial candidate. No source path can be supplied by the model.

`server.py` exposes `read_fixture`, `edit` and, in the compact arm,
`native_fallback`. It reads only `before.py` in an isolated run directory and
writes `candidate.py`; the original remains intact. `runs/` is ignored.

Local checks:

* Six supported fixture expansions match expected bytes.
* Eight rejection/atomicity checks pass.
* Native replacement produces the expected source.
* An actual MCP stdio client discovers the tools, reads a receipt, generates
  the correct candidate and observes an unknown-receipt rejection.

```powershell
.venv/Scripts/python.exe experiments/frontier-savings/pilot/validate.py
.venv/Scripts/python.exe experiments/frontier-savings/pilot/transport_check.py
```

## Prespecified live evaluation

Six tasks, three repetitions per task, two arms: **36 CLI sessions / 18 pairs**.
Arm order is randomized with seed 20261009. Tasks include tiny changes,
replace-all keyword edits, repeated row creation and unsupported new logic.
Both arms use the same source, goal, custom system prompt and receipt-based
preview interface. The native arm uses exact search/replace semantics, including
replace_all. The compact arm can use a native fallback.

The first session discovers the existing configured Claude model; subsequent
sessions pin that exact model. Credentials are inherited from the existing
Claude login. Local auth status identified a Claude Pro login: CLI USD figures
are API-equivalent estimates, not measured provider invoices. The same 2,048
output-token cap is applied to both arms, with configured reasoning behavior.

Each session is limited to six turns and a $0.12 CLI budget. A global $3 stop
ledger reserves $0.40 before starting another session. Unknown usage, unexpected
model changes and timeouts stop the study. CLI budget checks are client-side;
an in-flight request can exceed its session cap, so the total ledger uses actual
returned usage and keeps a reserve. A request exceeding $0.35 stops the study.
This pilot uses a bounded sequence rather than an uncontrolled concurrent batch.

Independent grading compares the complete candidate AST with a separately
authored expected fixture and checks that source stayed unchanged. This detects
unintended edits outside the requested target; it does not replace behavioral
tests or prove representative task quality.

Metrics: pass rate, tool rejection/fallback counts, emitted edit payload tokens,
total output tokens, all available input/cache categories, API-equivalent cost,
elapsed time and complete-pair cost differences. Failed attempts count in cost.
Raw CLI output stays in ignored run folders; public JSON contains numeric
results and task/model labels only. Do not use these results for billing.

## Live status

The user authorized the existing model with a $3 API-spend cap. Automatic
approval review rejected the initial live launch because it would send copies
of private repository source to Claude without approval for that payload and
destination. The user then explicitly approved live testing. The fixture
copies come from:

* `api/security.py`
* `horizon/pricing/anthropic_prices.py`
* `horizon/pricing/cache_ttl.py`
* `horizon/pricing/registry.py`

The approved live run uses these isolated source copies, with no private
conversation history sent. See [RESULTS.md](RESULTS.md) for outcome and
limitations; numeric observations are in `live_results.json` and
`analysis_results.json`. Existing source fixtures can be reviewed in
`../edit_codec/fixtures/`.

The primary run command is:

```powershell
.venv/Scripts/python.exe experiments/frontier-savings/pilot/run.py --pairs 3 --budget 3
```

The primary 2,048-token output limit truncated the ordinary twelve-row edit.
A separate paired sensitivity run raises that limit to 8,192 for both arms,
keeps the discovered model pinned, and uses only the remaining original $3
budget. This result is stored separately in `sensitivity_results.json`.
`analyze.py` retains the original strict AST grades and separately adjudicates
valid clone insertion positions that the task wording did not specify.

## Interpretation and next integration gate

This study evaluates actual model adoption of compact tools against a native
search/replace equivalent. It is not yet an end-to-end benchmark of the deployed
Pi 5 proxy and stock client approvals. Custom instructions, receipt wrapper and
the selected clone tasks limit generalization. A live win here is a prerequisite
for a later opt-in client adapter that returns the candidate through native
patch review and evaluates representative tasks against the existing proxy.

Codex can use the same stdio tools with an explicit experimental configuration;
this user-approved first pilot uses the existing Claude configuration because
its CLI offers a session budget limit. No Codex model request or persistent
configuration change is made. Official [Codex noninteractive documentation](https://learn.chatgpt.com/docs/non-interactive-mode)
describes structured events/output schemas for a later separate evaluator;
[Claude CLI documentation](https://code.claude.com/docs/en/cli-reference)
describes the print-mode, MCP and budget flags used here.
