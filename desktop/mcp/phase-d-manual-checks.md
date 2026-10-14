# Phase D manual CLI check

This check exercises compact request preparation with a synthetic request and
the bundled repeated-log fixture. It does not measure Claude usage or
Enterprise billing.

## Prepare, preview, and export

Run these commands in PowerShell from the named worktree. The output check
prevents the export from replacing an existing file.

```powershell
Set-Location 'C:\Users\Burhan\OneDrive\Desktop\ContextShrink\.claude\worktrees\phase-c-review-orchestrate-54e903'
$python = 'C:\Users\Burhan\OneDrive\Desktop\ContextShrink\.venv\Scripts\python.exe'
$checkDir = Join-Path (Get-Location) 'phase-d-manual-check'
New-Item -ItemType Directory -Force $checkDir | Out-Null
$requestPath = Join-Path $checkDir 'request.txt'
$packetPath = Join-Path $checkDir 'prepared-packet.json'
[System.IO.File]::WriteAllText(
  $requestPath,
  'From the log, report the worker, status, event, and total record count. Give those four fields only.',
  [System.Text.UTF8Encoding]::new($false)
)
$contextPath = Join-Path (Get-Location) 'desktop\mcp\sample-sources\phase-c-repeated.log'
if (Test-Path -LiteralPath $packetPath) {
  throw "Packet already exists; choose a new output path: $packetPath"
}

# Preview the packet locally and inspect that the request is exact, the
# context is marked untrusted, and the reversible repeat marker says 48.
& $python desktop\mcp\prepare_context.py `
  --request-file $requestPath `
  --context-file $contextPath `
  --mode compact `
  --show-packet
if ($LASTEXITCODE -ne 0) { throw 'Preparation preview failed.' }

# Export only after reviewing the preview. --confirm is the final argument.
& $python desktop\mcp\prepare_context.py `
  --request-file $requestPath `
  --context-file $contextPath `
  --mode compact `
  --output $packetPath `
  --confirm
if ($LASTEXITCODE -ne 0) { throw 'Packet export failed.' }
```

Open `prepared-packet.json` in a local text editor and manually copy the entire
JSON packet. In an existing Claude Desktop chat, paste only that packet; do
not paste the raw fixture first. Check that Claude returns `alpha`, `ready`,
`periodic-heartbeat`, and `48`. Compare the three fields and the 48 repeated
records with the local fixture at
`desktop/mcp/sample-sources/phase-c-repeated.log`. If compact mode falls back
to passthrough, the preview will say so; continue the same check with the
preserved source context. In one local run, the complete packet measured 616
bytes and an estimated 137 tokens versus 2,843 bytes and an estimated 696
tokens for the raw packet; these are illustrative measurements for this
fixture and software version. Local verification confirmed the exact request,
untrusted-context label, reversible `repeated 48 times` marker, and 48 source
records. These figures do not predict Claude's input count or Enterprise
billing. The latest local CLI and same-user broker checks are recorded in the
[Phase D runtime report](phase-d-runtime-report.json); Claude Desktop UI
validation remains manual.

## Source-backed `focused` or `reference` check

This separate check exercises an already-approved sample-source folder. Run
it only if you want to configure the optional local MCP source. The following
configuration is dedicated to this test: it grants only the bundled sample
folder and uses the distinct broker ID `phase-d-pilot`.

```powershell
Set-Location 'C:\Users\Burhan\OneDrive\Desktop\ContextShrink\.claude\worktrees\phase-c-review-orchestrate-54e903'
New-Item -ItemType Directory -Force .\phase-d-manual-check | Out-Null
```

Create `phase-d-manual-check\settings.json` with this content:

```json
{
  "profile": "local",
  "approved_roots": [
    "C:/Users/Burhan/OneDrive/Desktop/ContextShrink/.claude/worktrees/phase-c-review-orchestrate-54e903/desktop/mcp/sample-sources"
  ],
  "broker_id": "phase-d-pilot",
  "result_token_budget": 2000,
  "max_result_bytes": 65536,
  "max_input_bytes": 32768,
  "max_source_bytes": 8388608,
  "max_snapshot_bytes": 16777216,
  "max_store_bytes": 134217728,
  "max_snapshots": 64,
  "max_broker_clients": 8,
  "broker_timeout_seconds": 10,
  "snapshot_ttl_seconds": 3600
}
```

In a dedicated PowerShell terminal, start the broker in the foreground from
this worktree; leave this terminal open during the check and stop it with
Ctrl+C afterward:

```powershell
Set-Location 'C:\Users\Burhan\OneDrive\Desktop\ContextShrink\.claude\worktrees\phase-c-review-orchestrate-54e903'
$python = 'C:\Users\Burhan\OneDrive\Desktop\ContextShrink\.venv\Scripts\python.exe'
& $python -m horizon.desktop_mcp broker --config .\phase-d-manual-check\settings.json
```

To let Claude Desktop obtain a handle from this broker, manually merge this
entry into its existing MCP configuration. Preserve every existing server
entry and other setting. This guide does not edit the actual configuration.

```json
{
  "mcpServers": {
    "contextshrink-phase-d-pilot": {
      "command": "C:/Users/Burhan/OneDrive/Desktop/ContextShrink/.venv/Scripts/python.exe",
      "args": [
        "C:/Users/Burhan/OneDrive/Desktop/ContextShrink/.claude/worktrees/phase-c-review-orchestrate-54e903/desktop/mcp/launch_stdio.py",
        "--config",
        "C:/Users/Burhan/OneDrive/Desktop/ContextShrink/.claude/worktrees/phase-c-review-orchestrate-54e903/phase-d-manual-check/settings.json"
      ]
    }
  }
}
```

Restart Claude Desktop after choosing to add that entry. Ask it: “Use
`contextshrink_search` from `contextshrink-phase-d-pilot` to find
`phase-c-repeated.log` and return the `source_id`.” Name the server because
other ContextShrink entries may expose the same tool names. Copy the returned
handle. In a separate PowerShell terminal from the worktree, run `reference`
mode with that handle and the same config. Use a new output filename if one
already exists.

```powershell
Set-Location 'C:\Users\Burhan\OneDrive\Desktop\ContextShrink\.claude\worktrees\phase-c-review-orchestrate-54e903'
$python = 'C:\Users\Burhan\OneDrive\Desktop\ContextShrink\.venv\Scripts\python.exe'
$requestPath = '.\phase-d-manual-check\request.txt'
$configPath = '.\phase-d-manual-check\settings.json'
$packetPath = '.\phase-d-manual-check\reference-packet.json'
if (Test-Path -LiteralPath $packetPath) {
  throw "Packet already exists; choose a new output path: $packetPath"
}
& $python desktop\mcp\prepare_context.py `
  --request-file $requestPath `
  --mode reference `
  --source-id 'PASTE_THE_RETURNED_cs1_HANDLE_HERE' `
  --config $configPath `
  --show-packet
if ($LASTEXITCODE -ne 0) { throw 'Reference preview failed.' }
& $python desktop\mcp\prepare_context.py `
  --request-file $requestPath `
  --mode reference `
  --source-id 'PASTE_THE_RETURNED_cs1_HANDLE_HERE' `
  --config $configPath `
  --output $packetPath `
  --confirm
if ($LASTEXITCODE -ne 0) { throw 'Reference export failed.' }
```

Review the reference packet's source/version/coverage and page-boundary labels
against the fixture. Manually paste only this packet into an existing chat,
then ask Claude to use `contextshrink_open` from
`contextshrink-phase-d-pilot` with the same handle and `mode: exact` to check
the source version, worker, status, event, and 48-record count. This path does
not import a file, make a handle, or grant a source root; the handle must come
from the running broker. `focused` also requires `--query 'periodic-heartbeat'`.
It labels results partial when the selection omits source content, so don't
expect a whole-source count from a partial packet.

## Remaining product work

The CLI check does not provide the planned native request/context editor,
secure pasted-context import, or integrated paste experience. Those are
engineering work, not additional user acceptance steps for this CLI. MCP App
input remains optional and capability-gated; it is not required for either
manual check.
