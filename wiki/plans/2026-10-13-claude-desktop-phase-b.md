---
title: "ContextShrink Desktop MCP: Phase B plan"
date: 2026-10-13
status: in-progress
phase: B
milestones: [M1, M8]
---

# Phase B: approved sources, snapshots, and shared broker

Phase B implementation is delivered locally, with its broader verification
gates underway. The local profile adds approved-root reads, immutable versioned
snapshots, and a same-user Windows named-pipe broker shared by separate MCP
processes. A user-reported live Claude Desktop check now confirms the basic
local-profile search, exact open, line retrieval, and stats flow; it does not
establish the remaining cross-SID or enterprise gates below.

## Scope

### M1: safe source and snapshot layer

- Support explicitly approved local roots and enforce authorization on every
  operation. Reject paths outside grants and avoid model-origin arbitrary
  filesystem reads.
- Build bounded, versioned source snapshots with immutable blocks, stable
  references, expiry, and actionable sanitized errors.
- Preserve exact source content for same-version recovery. Do not rely on
  implicit cross-chat or process-local "already seen" state.
- Establish resource, size, and operation limits before adding broader
  retrieval, compression, or document formats.

### M8: per-user shared broker and IPC

- Provide one same-user broker for approved-root settings and snapshots, shared
  by separate MCP processes. This phase exposes consume-only operations; it
  has no role field, grant API, or privileged operation.
- Resolve the same opaque source handles and versions across MCP processes
  while checking the Windows client SID, approved-root access, and lifecycle.
- Bound IPC frames, source/result sizes, and operation deadlines. A separate
  worker process lets the broker kill stalled source I/O; the process boundary
  is not an OS sandbox. A source-byte quota bounds accepted file content, not
  total process memory use.
- Keep the broker local and independent of hosted sign-in or model API keys.
- Keep this delivery read-only and consume-only. Privileged companion APIs and
  operation-scoped authorization capabilities are future Phase D work, not
  part of the current broker.

## Acceptance gates

M1 passes when approved roots are enforced on every access; unauthorized paths
are denied; reads are bounded; source versions and immutable blocks are
reproducible; and exact same-version recovery succeeds without silent
truncation or hidden cross-chat state.

M8 passes when separate MCP processes resolve identical authorized versions
through the shared broker; unknown operations and extra/privileged fields are
rejected; client SID, deadlines, limits, and lifecycle behavior are enforced;
and handles stop resolving after grant revocation or snapshot expiry. The
current integration tests cover same-user processes; cross-SID ACL behavior
still needs validation under a second Windows account.

The diagnostic profile remains unchanged and is still the default. The local
profile is a reduced-scope pilot: it has no grant/import/clipboard/write API,
no companion or MCP App authorization capability, and no claim of full
Enterprise production readiness. If a source operation exceeds its deadline,
the broker kills and restarts the worker; this clears every shared snapshot
and opaque handle. The source-byte quota is not an RSS or total-memory bound.

### Live basic check

The user reports that Claude Desktop's `contextshrink-local` profile searched
the synthetic `pilot.md`, returned a fresh `cs1_` handle, opened it exactly,
and retrieved raw text for lines 1–3. Stats reported
`approved-local-files`, one catalog entry, `catalog_truncated=false`,
`skipped=0`, two snapshots, 296 source bytes, a 3600-second TTL, exact mode,
compression disabled, and unknown token counts. The two snapshots reflected
the diagnostic probe and the user's search; opening the existing handle did
not create another snapshot. A separate live broker diagnostic also consumed
a fresh handle from a second client. An earlier handle was unknown after a
lost state generation; its precise trigger was not recorded. These checks show
the basic live path and fresh-handle cross-client use, not persistence through
broker/worker loss or recovery guarantees.

Phase B exits only when both milestone gates pass together, including
cross-process and unauthorized-access checks. These gates are the prerequisite
for Phase C retrieval and compression and Phase D current-input preparation.
No savings claim follows from merely adding the source or broker plumbing.

## Status and dependencies

**Status: in progress; local implementation and verification passed.** The
desktop MCP test suite reports 91 passed and 1 skipped; the independent
combined verification reports 93 passed and 1 skipped. The synthetic-only
Phase B smoke report records 11 passing checks for broker startup, filename
discovery, approved/open and cross-process exact retrieval, mutation/version
behavior, oversized pagination metadata, root revocation, and outside-path
denial. See `desktop/mcp/phase-b-runtime-report.json`; it contains runtime and
check metadata, not source contents. The report does not establish cross-SID
ACL behavior under a second Windows account, OneDrive/cloud placeholder
behavior, Enterprise billing, MCP Apps compatibility, companion authorization
scope, or production-readiness. The basic live local-profile check is user-
reported; no independently captured Desktop transcript is included. M1/M8 are
implemented in the local pilot, but these external and target-environment gates
remain before Phase B exit. See the
[comprehensive build plan](2026-10-13-claude-desktop-enterprise-mcp-build-plan.md)
for architecture, sequencing, and evaluation requirements.
