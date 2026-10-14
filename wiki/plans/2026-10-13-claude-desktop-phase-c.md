---
title: "ContextShrink Desktop MCP: Phase C implementation and evidence"
date: 2026-10-13
status: local-implementation-complete-external-validation-pending
phase: C
milestones: [M2, M3, M10]
---

# Phase C: retrieval, reversible presentations, and payload evaluation

Phase C is the local implementation of M2 (tools and policies), M3
(evaluation and metadata), and M10 (broader reversible formats). Its user
value is useful source evidence with explicit coverage and exact recovery. It
does not make ordinary Claude Desktop input interception available, change
Claude's model or effort setting, or establish lower Enterprise billing.

The policy layer distinguishes four guarantees:

| Mode | Contract |
|---|---|
| Exact | Complete extracted text for the requested scope; original line references and same-version retrieval; paginate or return an actionable size error. |
| Compact | Deterministic verified representation for supported formats; report whether it is reversible or normalized and the verifier/policy version. |
| Focused | Exact selected passages with source references and `partial` coverage; omitted material remains recoverable through explicit exact selectors. |
| Overview | Section/headings inventory only; partial coverage and not a substitute for source evidence. |

Focused selection is extractive and deterministic. It must preserve source
wording, negation, numbers, IDs, and constraints in included passages. This is
a selection-quality check, not a proof that an omitted passage is irrelevant.
Source text remains untrusted input and cannot authorize tools or alter
service behavior.

## Measurement contract

The local counter uses a SHA-256-verified cached `o200k_base` vocabulary when
available. It is a stable stand-in for the private Claude tokenizer and is
reported as `estimate_not_claude_count`. The fallback is a labeled UTF-8-byte
heuristic, with a separate hard byte limit. Neither value is provider usage.
The counter reads only an existing verified cache and does not download a
vocabulary or call a model.

Each measured result separates:

- source-text bytes and estimated tokens before and after its presentation;
- a same-scope raw-equivalent result payload from the complete delivered inner
  result JSON payload, including coverage, provenance, selector/cursor, and
  accounting metadata;
- retrieval payload accounting from source compression accounting.

The inner result estimate excludes MCP SDK/JSON-RPC framing, tool schemas,
Desktop-added instructions or context, and provider-hidden prompt content
unless an evaluation separately captures them. The local usage ledger stores
bounded metadata only: safe tool/mode/status labels, version digests, policy
and counter versions, per-call byte/token estimates, latency, and sanitized
error codes. It stores no paths, source text, queries, or raw arguments. Its
ephemeral HMAC is local integrity evidence only; the signing key is not
reported, records disappear with the worker, and neither the MAC nor estimates
prove Anthropic usage or Enterprise billing.

Per-call raw-equivalent counts are counterfactual estimates for that result.
They are not task-level savings: pages, retries, errors, additional tool calls,
enabled schemas, instructions, and input handling must all be counted for a
whole-task comparison. No USD field is shown until participant/task-window
billing data supports attribution. Small or source-free requests may have no
useful reduction or may grow after metadata and schema overhead.

## Synthetic evaluation

`desktop/mcp/phase_c_eval.py` runs bundled synthetic fixtures through paired
raw and optimized in-memory MCP Python SDK sessions. It counts each arm's
enabled tool schemas and server instructions once, then every serialized tool
argument and typed SDK `CallToolResult` (including search, errors, and exact
recovery calls); inner `TextContent.text` and per-result raw-equivalent
accounting are reported separately to avoid conflating scopes. The SDK result
serialization is not the JSON-RPC frame, which the in-memory transport does
not expose. Desktop-added context and provider-hidden prompt inputs remain
unknown. The synthetic constraints check negation, numeric limits, JSON
number lexemes, duplicate-key fallback, a tiny no-gain input, and repeated-log
folding. This is deterministic fixture coverage, not model answer faithfulness,
blind quality scoring, or a Claude Desktop compatibility pass. No API/model or
network calls occur, and no user-approved source is read.

## Exit gate

The bounded local implementation, synthetic SDK evaluation, and sample-source
checks pass locally. On October 13, 2026, `pytest tests/desktop_mcp -q` reported
**155 passed, 1 skipped**. The recorded synthetic A/B report is
[`desktop/mcp/phase-c-eval-report.json`](../../desktop/mcp/phase-c-eval-report.json).
With the same schemas and instructions counted once per arm, the example task
recorded **4,827 estimated o200k tokens** for the raw arm and **5,169** for the
optimized arm, across all captured arguments and SDK result objects. That is
342 estimated tokens (about 7.1%) more in this task; the optimized arm made
one additional exact recovery call after a deliberately omitted focused
excerpt. A reversible repeated-log fold reduced its full inner-result
estimate by 230 tokens, while JSON stayed exact because the encoding hint and
gain gate did not justify normalization. This demonstrates why source-level
compaction or per-result counterfactuals do not establish task savings. The
report counts estimates, not Claude usage; its SDK serialization excludes
stdio JSON-RPC framing and hidden host/provider context. Billing and USD remain
unmeasured.

### Re-verification after implementation pause

An independent re-verification on the same date, after an implementation
pause, confirmed the local Phase C state without code changes:

- `pytest tests/desktop_mcp -q` reported **156 passed, 1 skipped**.
- `ruff check` and `ruff format --check` passed for all desktop MCP sources,
  scripts, and tests.
- The synthetic A/B evaluation was re-run and regenerated
  `desktop/mcp/phase-c-eval-report.json` with `status: "passed"`. The fresh
  run recorded **4,834** raw and **5,181** optimized estimated o200k tokens
  (versus 4,827/5,169 in the earlier capture). The small run-to-run delta is
  consistent with cursor/nonce and accounting metadata variation; the
  qualitative result is unchanged: the optimized arm remains slightly higher
  on this synthetic task because of the extra exact recovery call, and no
  task-level savings claim follows.
- All Phase C implementation remains uncommitted working-tree state pending
  review; no live Claude Desktop Phase C walk-through or Enterprise billing
  evidence has been captured.

### Review on October 14, 2026

A review found that a compact-mode cursor issued by the exact-page fallback
was misrouted on continuation into the overview branch, returning
`cursor_mismatch` (or a heading inventory) instead of the next exact page. The
fix resumes exact pages under the compact binding. Regression tests walk every
page and reject the cursor in other modes. `pytest tests/desktop_mcp -q` now
reports **158 passed, 1 skipped**. The review also listed untested checklist
items: cursor tamper/mismatch per bound field and caller/workspace binding;
source prompt-injection fixtures; root prefix collision; oversized single
line/JSON string/CSV cell in exact mode; blank CSV rows and leading-zero IDs;
source row/field/line bounds; desktop-path round trips for log, search and path
folds; the inflation-guard boundary; JSON null/escape/order/big-integer round
trips; UNC/device paths; change between search/open/retrieve; and a log-capture
check for source text and queries.

Those historical gaps are now covered by `test_cursor_binding.py`,
`test_source_hardening.py` and `test_format_fidelity.py`, which found and
fixed three more defects: `open` and `retrieve` accepted each other's cursors
(line-number vs unit-index offsets, so continuations skipped or escaped the
bound scope); an empty cursor silently restarted at page one; and a valid CSV
cell over 128 KiB was reported as `csv_invalid`. Follow-up hardening rejects
multiply-linked files on source reads and snapshot access. CSV exact selection
now caps logical rows and columns, caps field character count, and restores the
process-wide CSV field limit after each parse. These are per-input parsing
bounds; they do not claim to bound total worker RSS. Cursor ownership is
limited to the current same-user broker model: the broker checks the Windows
user SID and rechecks each source grant, while separate per-user workspace or
caller identities are not implemented. Expired and missing snapshots both
return `snapshot_unknown`; a source-byte ceiling remains, with no separate
per-line source bound.

A local in-process benchmark on a synthetic 7.65 MB / 150,000-line log
returned the first bounded exact page in 1.176 seconds and a subsequent cursor
page in 0.656 seconds. A separate synthetic 8.1 MB broker/SDK check using the
default 64 KiB response cap and 10-second broker deadline completed in 2.03
seconds. Both are machine-specific local timings, not Claude Desktop latency.

M2/M3/M10 implementation is locally complete and locally verified;
**external Phase C validation remains pending** for a live Claude Desktop
walk-through and separate whole-task Enterprise usage/billing evidence.
Phase B's reported live basic flow does not count as Phase C application
validation. The remaining named gates are environmental/product validation,
not a claim that the local synthetic suite proves Claude usage, billing, or
production readiness. See the
[comprehensive build plan](2026-10-13-claude-desktop-enterprise-mcp-build-plan.md)
and [Phase B notes](2026-10-13-claude-desktop-phase-b.md).

The latest combined local verification from the named worktree root used the
primary checkout's Python executable and reported **309 passed, 1 skipped**
across the desktop MCP suite, MCP dependency-contract test, and CCR MCP HTTP
compatibility test. Ruff lint passed and all 56 scoped files passed format
checks. The skipped test is platform-specific. The remaining Phase C exit
items are a live Claude Desktop Phase C walk-through following the
`desktop/mcp/README.md` sample-source steps, cross-SID and cloud-provider
validation where those environments are available, and separate whole-task
Enterprise usage/billing evidence.

```powershell
C:\Users\Burhan\OneDrive\Desktop\ContextShrink\.venv\Scripts\python.exe -m pytest tests\desktop_mcp tests\test_mcp_dependency_contract.py tests\test_ccr_mcp_http.py -q
```

The report continues to show a larger estimated optimized task payload than
the raw arm on its captured synthetic example. This local gate proves neither
Claude Desktop compatibility nor lower provider usage or billing.
