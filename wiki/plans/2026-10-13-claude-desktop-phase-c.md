---
title: "ContextShrink Desktop MCP: Phase C implementation and evidence"
date: 2026-10-13
status: in-progress
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

A local in-process benchmark on a synthetic 7.65 MB / 150,000-line log
returned the first bounded exact page in 1.176 seconds and a subsequent cursor
page in 0.656 seconds. A separate synthetic 8.1 MB broker/SDK check using the
default 64 KiB response cap and 10-second broker deadline completed in 2.03
seconds. Both are machine-specific local timings, not Claude Desktop latency.

M2/M3/M10 implementation is locally verified; **Phase C remains in progress**
pending a live Claude Desktop Phase C walk-through and separate whole-task
Enterprise usage/billing evidence. Phase B's reported live basic flow does not
count as Phase C application validation. See the
[comprehensive build plan](2026-10-13-claude-desktop-enterprise-mcp-build-plan.md)
and [Phase B notes](2026-10-13-claude-desktop-phase-b.md).

The independent re-verification above (156 tests, lint/format clean, eval
regenerated with `status: "passed"`) confirms the local implementation is
complete and reproducible after the implementation pause. The remaining Phase
C exit items are unchanged: a live Claude Desktop Phase C walk-through
following the `desktop/mcp/README.md` sample-source steps, and separate
whole-task Enterprise usage/billing evidence.
