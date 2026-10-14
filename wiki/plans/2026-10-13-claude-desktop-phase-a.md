---
title: "ContextShrink Desktop MCP: Phase A delivery"
date: 2026-10-13
status: local-and-basic-desktop-verification-passed
phase: A
---

# Phase A: runtime and contract foundation

Phase A implements the safe capability spike in the comprehensive build plan.
The initial server operates on synthetic fixtures only. It establishes the
stdio transport, settings and tool contracts before production source access
and compression are added.

## Delivery phases

| Phase | Scope | Main milestones |
|---|---|---|
| A | Diagnostic runtime, contracts, raw fixture baseline, compatibility probes | M0 |
| B | Approved source access, immutable snapshots, shared per-user broker and client authority | M1, M8 |
| C | Exact retrieval, compression/focused policy, broader reversible formats, signed payload evaluation | M2, M3, M10 |
| D | Current-message preparation, companion preview/copy, conditional MCP App interface | M9 |
| E | Batched query/aggregate work, concise instruction pack, source-driven artifacts | M11, M12 |
| F | Digital PDF/DOCX fidelity, packaging and clean-machine deployment | M4, M5 |
| G | Enterprise full-task evaluation, beta hardening and rollout | M6, M7, M13, M14 |
| H | Optional media/ML/delegation, upstream adapters and hosted deployment | Capability-gated extensions |

Phase boundaries do not override dependencies. In particular, Phase B's broker
precedes cross-process sources/companion testing, and extraction fidelity must
pass before the relevant document-saving claim.

## Phase A acceptance

- Separate `horizon.desktop_mcp` entrypoint, without the proxy CLI lifecycle.
- Four stable read-only source tool contracts.
- Strict settings and sanitized errors.
- Exact raw synthetic fixtures, repeat reads, and line retrieval.
- No model-origin filesystem reads or current-message capture.
- Unsupported compression modes return an explicit error.
- No token savings or Enterprise billing claims from raw fixture results.
- Real installed MCP SDK initialization/list/call/error/close testing.
- An absolute-path launcher usable from Claude Desktop's arbitrary working directory.
- Explicit runtime/capability report; untested Desktop/Apps/billing remain unverified.

## Runtime evidence

Initial environment discovery found Python 3.12.14, MCP SDK 1.30.0, Pydantic
2.13.5 and an available Horizon native extension. This is runtime metadata,
not evidence that the Claude Desktop client negotiated the protocol.

Integration review and local verification passed:

- 50 tests passed: the Phase A suite plus the existing MCP dependency-pin and
  Streamable HTTP compatibility tests.
- Ruff lint and format checks passed for all new Python files.
- The real SDK subprocess negotiated protocol `2025-11-25` with
  `contextshrink-desktop` version `0.1.0`.
- Empty search discovers all three synthetic fixtures.
- Repeated open returns the same full exact fixture; line retrieval matches
  the requested raw lines and immutable fixture version.
- Unauthorized identifiers, unsupported compact mode, invalid input and
  oversized results produce explicit errors.
- A launcher started from an unrelated directory succeeds, and closed stdin
  shuts it down without non-protocol stdout.
- CLI/report failures return nonzero status with sanitized diagnostics.

The recorded report is `desktop/mcp/phase-a-runtime-report.json`. It is SDK-only
evidence and remains unchanged; it does not record a live Claude Desktop run.

Three GPT-6 Luna subagents implemented settings/contracts, the diagnostic
server, and the probe slice. The orchestrator reviewed and integrated their
changes, fixed duplicate result representations and failure handling, and ran
the final verification.

## Implementation and commands

Added `horizon/desktop_mcp/` with strict settings/contracts, the fixture backend,
stdio server, CLI, runtime doctor and smoke probe. Added
`tests/desktop_mcp/` and the source-checkout launcher/config examples under
`desktop/mcp/`.

```powershell
.venv\Scripts\python.exe -m horizon.desktop_mcp doctor
.venv\Scripts\python.exe desktop\mcp\smoke_client.py --output desktop\mcp\phase-a-runtime-report.json
.venv\Scripts\python.exe -m pytest tests\desktop_mcp tests\test_mcp_dependency_contract.py tests\test_ccr_mcp_http.py -q
```

This is a diagnostic fixture baseline, not production compression. Its results
contain a single JSON text envelope, raw-mode metadata, unknown token counts
and no dollar-saving estimate. Token-budget application is explicitly false
until Phase C introduces measured response policies.

## Live Claude Desktop verification

The user reports a basic live check in Claude Desktop's Developer page. The
server appeared as **Running** with the expected Python executable, launcher,
and configuration. Tool search returned the synthetic fixture tools. Opening
`fixture:note` returned exactly:

```text
# Synthetic note

This note exists only to exercise the diagnostic desktop MCP.
```

The result metadata reported `synthetic-fixtures`, exact mode, no compression,
unknown token counts, and no production sources. This closes the basic live
MCP discovery-and-open check based on user-reported evidence. It does not
establish repeated-read/line-retrieval behavior or live error handling, so the
broader Desktop contract check remains partial. MCP Apps compatibility and
Enterprise billing behavior are still unknown and unverified.

## Desktop verification gate

The workspace includes a diagnostic settings file and a machine-specific
Claude Desktop registration example. It is not installed into the user's
Claude configuration automatically.

Use Claude Desktop's Developer **Edit Config** action to locate its actual
configuration, merge the example entry without replacing other servers, and
restart/reload according to the Desktop UI. The diagnostic entry exposes
synthetic note/log/record sources only.

Verify tool listing, opening the same synthetic source twice, exact line
retrieval, and explicit errors for unsupported modes or non-fixture paths.
Record the actual client/build and observed behavior. SDK subprocess smoke
tests alone cannot close this live Desktop gate.

Phase A's local code gate and basic live Claude Desktop discovery/open check are
complete. The broader target-client contract check remains partial. Actual
compression, private documents, broker IPC, pre-send UI, output offloading and
Enterprise spending comparisons belong to subsequent phases.
