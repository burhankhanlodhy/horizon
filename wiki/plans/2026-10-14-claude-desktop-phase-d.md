---
title: "ContextShrink Desktop MCP: Phase D local companion and CLI"
date: 2026-10-14
status: local-companion-and-cli-implemented-target-app-validation-pending
phase: D
milestones: [M9]
---

# Phase D: prepare the current request before submission

M9 now has two user-operated local routes: a browser-based graphical companion
for editing, previewing, copying, and explicitly staging context, plus a
one-shot CLI for file-based preparation. Both keep the current request intact,
label supporting context as untrusted, expose local estimates and coverage, and
leave the final paste/submission to the user. Neither reads the clipboard,
calls a model, sends a chat message, nor edits Claude Desktop settings.

The graphical companion is a standalone Python HTTP server bound to loopback;
it does not require Tauri, hosted login, network access, or app packaging. A
random launch capability is delivered in the URL fragment, removed from the
address bar at page startup, and held only in tab memory. Authenticated local
JSON calls use exact Host/Origin checks and the capability header. The
companion does not accept arbitrary paths or grant roots.

## Preparation modes

Both front ends use `horizon.desktop_mcp.preparation.prepare_packet`:

| Mode | Behavior |
|---|---|
| `passthrough` | Keep the explicitly entered request and supporting context unchanged. |
| `compact` | Apply only verified deterministic transforms when the complete packet meets gain and size gates; otherwise fall back to pass-through. |
| `focused` | Select exact passages from one existing broker handle, label coverage partial, and retain an exact-recovery reference. |
| `reference` | Include a bounded exact page and its existing handle/version. |
| `reference_only` | Include the existing handle/version for later exact recovery without embedding its source body. |

The browser companion defaults to compact. It shows the unchanged request,
coverage, fidelity, fallback reason, measurements, and exact packet before
enabling a user-clicked Copy action. A stale preview is invalidated after any
input, mode, query, or handle change. Clipboard access is attempted only after
the Copy click; when browser permission is unavailable, the user can select
the packet and press Ctrl+C.

Inline passthrough/compact preparation needs no broker config. Source-backed
modes use only an existing handle with the matching local config; the companion
omits inline context for those requests. `reference_only` avoids reading
source text while preparing its packet. All estimates are local and do not
include host schemas, hidden context, provider usage, or billing.

## Explicit pasted-context recovery

The optional staging action requires a displayed approved-root grant, a
per-action consent checkbox, and a separate Save click. The server creates a
generated UTF-8 file under a session-owned child of the configured root, then
uses the broker's existing read-only `open` operation to mint a normal
versioned source handle. The staging record ID is not the broker source ID.
No new MCP import/search operation is added. The UI displays the approved
folder before consent and does not accept caller-selected paths.

Staged files and their handles exist only for the foreground companion
session, subject to the configured broker TTL (at most one hour). Keep both
the broker and companion terminals open while Claude needs exact recovery.
Removing a staged file blocks later exact recovery through that handle; the
broker may retain an inaccessible snapshot in RAM until its normal TTL or
restart. Authenticated Clear session and quit removes owned files; closing a
browser tab alone does not stop the companion. A process crash can bypass
graceful file cleanup, so session storage is not a durable archive.

Choose a dedicated unsynced folder where possible. If the approved folder is
inside OneDrive or another cloud-synced location, the operating system may
sync staged text as well. The app displays this path before each save.

To let Claude recover a staged source, manually merge an MCP server entry that
exposes `contextshrink_open` into the existing Claude config, preserving its
other entries. Point it at `desktop/mcp/launch_stdio.py` and the same local
config used by the foreground broker. Restart Claude Desktop and use the exact
displayed `cs1_…` source handle with `contextshrink_open`. Inline preparation
does not require this MCP registration.

## Launch from the named worktree

From the named worktree root, use the primary checkout's environment so its
installed dependencies are available while imports resolve from this checkout:

```powershell
Set-Location 'C:\Users\Burhan\OneDrive\Desktop\ContextShrink\.claude\worktrees\phase-c-review-orchestrate-54e903'
$python = 'C:\Users\Burhan\OneDrive\Desktop\ContextShrink\.venv\Scripts\python.exe'
& $python desktop\mcp\prepare_companion.py
```

Inline preparation opens without config or staging. For source handles and
recoverable pasted context, create `desktop/mcp/companion-settings.json` from
[`companion-settings.example.json`](../../desktop/mcp/companion-settings.example.json),
set its one approved root to a dedicated folder, and start the broker and
companion in separate terminals using that same config:

```powershell
$python = 'C:\Users\Burhan\OneDrive\Desktop\ContextShrink\.venv\Scripts\python.exe'
$vaultRoot = Join-Path $env:LOCALAPPDATA 'ContextShrink\prepared-vault'
New-Item -ItemType Directory -Force $vaultRoot | Out-Null
& $python -m horizon.desktop_mcp broker --config desktop\mcp\companion-settings.json
```

In a second terminal, from the same worktree:

```powershell
$python = 'C:\Users\Burhan\OneDrive\Desktop\ContextShrink\.venv\Scripts\python.exe'
$vaultRoot = Join-Path $env:LOCALAPPDATA 'ContextShrink\prepared-vault'
& $python desktop\mcp\prepare_companion.py `
  --config desktop\mcp\companion-settings.json `
  --staging-root $vaultRoot
```

The UI guide at [`desktop/mcp/companion-ui/README.md`](../../desktop/mcp/companion-ui/README.md)
includes the manual Claude config example and complete lifecycle steps.

## Local verification and limits

The scoped desktop MCP/dependency compatibility gate reports **321 passed and
1 skipped**; scoped Ruff lint/format checks passed. The repeatable real-process
integration probe is:

```powershell
& $python desktop\mcp\phase_d_companion_probe.py
```

Its checked-in [metadata report](../../desktop/mcp/phase-d-companion-report.json)
records 11 passing checks against a real Windows named-pipe broker subprocess
and authenticated local HTTP companion: consented source import, safe metadata,
reference-only body omission, exact recovery, focused partial coverage,
256 KiB single-line import, TTL cleanup, source removal, and graceful session
cleanup. The probe uses synthetic text and temporary config/source roots. It
does not expose source text, handles, temp paths, launch tokens, or URLs in its
report.

Local browser review confirmed preparation, copy, explicit staging, handle
selection, reference-only, and focused previews. The follow-up inline removal
confirmation change passed JavaScript syntax and static checks; its fresh
browser click was not verified after the review browser remained blocked by an
earlier native-dialog state. The real HTTP probe covers delete/recovery
revocation. The user reports that a Claude Desktop compact-mode check returned
`alpha`, `ready`, `periodic-heartbeat`, and `48` for the bundled repeated-log
fixture. Separately, the user reports that Claude Desktop opened source version
`ddb02b…` and returned Alex with deadline `2026-11-30`. A later open returned
`source_unavailable`. That result is consistent with revocation after removal,
but the removal action was not independently confirmed and the result does not
establish why the source became unavailable. These are user-reported Desktop
observations, separate from the synthetic SDK and HTTP reports; they are not
automated assertions or broad client compatibility evidence. The local CLI and
companion do not establish that Claude displays every packet in a particular
way, reduce Claude usage, or save Enterprise billing. MCP Apps, hosted billing,
native Tauri integration, runtime packaging, and clean-machine installation
remain separate optional or external gates.
