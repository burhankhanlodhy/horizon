# Local request preparation companion

The companion opens a small browser UI on `127.0.0.1`. It prepares a bounded
JSON packet for you to review and paste into an existing chat. It does not call
a model, inspect a native app, read the clipboard, or send a message.

## Start it

For this named worktree, use the primary checkout's Python environment. The
worktree has source changes but not the full installed dependency set. In a
different checkout, point `$python` at an environment with this project's
dependencies and use that checkout's root instead.

```powershell
Set-Location 'C:\Users\Burhan\OneDrive\Desktop\ContextShrink\.claude\worktrees\phase-c-review-orchestrate-54e903'
$python = 'C:\Users\Burhan\OneDrive\Desktop\ContextShrink\.venv\Scripts\python.exe'
& $python desktop\mcp\prepare_companion.py
```

The launcher prints its local address and opens a tab with a temporary session
token in the URL fragment. The page removes the fragment immediately and keeps
the token in memory for this tab only. Do not share that browser URL. If you
close the tab, the companion remains running in the visible terminal; use
**Clear session and quit** or press Ctrl+C there to stop it.

## Use saved sources and recovery

To use an existing local MCP source handle, or to save pasted context as a
recoverable source, configure one dedicated staging folder. Create
`desktop/mcp/companion-settings.json` from the example only when the local file
does not already exist; this guard preserves settings you have already chosen:

```powershell
if (Test-Path .\desktop\mcp\companion-settings.json) {
  Write-Error 'companion-settings.json already exists; edit it in place if needed.'
} else {
  Copy-Item .\desktop\mcp\companion-settings.example.json .\desktop\mcp\companion-settings.json
}
```

Replace `YOUR_USERNAME` and make `approved_roots` the absolute path to a new,
dedicated folder. The example uses `%LOCALAPPDATA%` so the default vault is
outside the OneDrive-synced checkout; if you choose a synced location, your
operating system may sync the staged text too. The UI shows the approved path
before you authorize a save. Create that folder before starting the companion. The
`--staging-root` argument must resolve to exactly the same folder listed in
`approved_roots`.

The example points inside `%LOCALAPPDATA%`, outside the OneDrive-synced
checkout. Replace `YOUR_USERNAME` in the JSON with your Windows account name.
In the first PowerShell terminal, define the worktree, Python, and vault
variables, create the folder, and start the broker:

```powershell
Set-Location 'C:\Users\Burhan\OneDrive\Desktop\ContextShrink\.claude\worktrees\phase-c-review-orchestrate-54e903'
$python = 'C:\Users\Burhan\OneDrive\Desktop\ContextShrink\.venv\Scripts\python.exe'
$vaultRoot = Join-Path $env:LOCALAPPDATA 'ContextShrink\prepared-vault'
New-Item -ItemType Directory -Force $vaultRoot | Out-Null
& $python -m horizon.desktop_mcp broker --config .\desktop\mcp\companion-settings.json
```

Keep that terminal open. In a second PowerShell terminal, repeat these
definitions (PowerShell variables do not carry between terminals), then start
the companion with the same configuration and exact staging root:

```powershell
Set-Location 'C:\Users\Burhan\OneDrive\Desktop\ContextShrink\.claude\worktrees\phase-c-review-orchestrate-54e903'
$python = 'C:\Users\Burhan\OneDrive\Desktop\ContextShrink\.venv\Scripts\python.exe'
$vaultRoot = Join-Path $env:LOCALAPPDATA 'ContextShrink\prepared-vault'
& $python desktop\mcp\prepare_companion.py `
  --config .\desktop\mcp\companion-settings.json `
  --staging-root $vaultRoot
```

The companion accepts only the path already approved in the config. It does
not add roots, accept arbitrary paths from the browser, or change a Claude
Desktop configuration. The example config grants only the dedicated staging
folder. Add other source folders only by editing your own local config after
you choose to approve them.

## Prepare and paste

Enter the request and supporting context in their separate fields. The text is
sent exactly as entered; changing to a source-backed mode disables and omits
the inline context. **Compact** is selected by default. It uses a verified
local transform when the complete packet meets its configured size and gain
limits, and falls back to pass-through otherwise. The other modes are
**Passthrough**, **Focused**, **Reference**, and **Reference only**. The last
three require an existing broker-issued `cs1_…` source handle; Focused also
requires a query.

Choose **Prepare preview** to see the exact request, full packet, source
coverage, fidelity, fallback reason, packet size, and local token estimate.
Review the JSON, then choose **Copy packet**. If browser clipboard permission
is unavailable, select the packet and press Ctrl+C. Paste it into your
existing chat yourself. The packet labels supporting context as untrusted
data. Estimates are local and do not represent model usage or billing.

## Save pasted context for later recovery

Saving is optional. Check the authorization box after reviewing the displayed
approved folder, then choose **Save context and create recovery handle**. This
explicit action writes one generated UTF-8 text file under a session-owned
folder inside that approved root and asks the local broker for a real,
versioned source handle. The displayed UUID is only the companion's staging
record ID; use the separate `cs1_…` handle for source recovery.

The UI lists the approved folder, handle, source version, coverage, and exact
expiry time. The handle and staged file are available only while the
companion and broker session remain alive, and expire within one hour. The tab may close without
stopping the foreground companion. Keep both terminals running while an
existing chat needs exact recovery. **Remove saved source** deletes the
session-owned file and blocks later exact recovery through that handle; a
bounded broker snapshot may remain inaccessible in memory until its normal TTL
or broker restart. **Clear session and quit** stops the companion and removes
all files owned by that session.

To let Claude Desktop call `contextshrink_open` for a copied recovery handle,
manually merge an MCP server entry like this into its existing config. Keep
all existing entries and point the command, launcher, and settings file at
your own checkout:

```json
{
  "mcpServers": {
    "contextshrink-companion": {
      "command": "C:/path/to/ContextShrink/.venv/Scripts/python.exe",
      "args": [
        "C:/path/to/ContextShrink/desktop/mcp/launch_stdio.py",
        "--config",
        "C:/path/to/ContextShrink/desktop/mcp/companion-settings.json"
      ]
    }
  }
}
```

After restarting Claude Desktop, use the named `contextshrink-companion`
server for `contextshrink_open` with the displayed source handle. The broker
must still be running with the same local config. Removing a staged source or
letting it expire ends that source's recoverability. No Claude Desktop
configuration change is needed for inline passthrough or compact previews.

## Browse artifacts

The **Artifacts** section lists generated results that the local broker holds
for this session. Each row shows the title, format, row count, size, and
expiry time. **Preview** shows the first rows the broker recorded, as plain
text in a table, without a download. **Download** fetches the artifact through
the companion with the session credential, then starts a save through your
browser. Your browser's Save dialog chooses the destination; the companion
never writes a file path you supply. **Delete** asks for confirmation, then
removes the artifact from the broker.

Artifacts expire automatically and disappear when the broker revokes them,
for example when the source they were derived from is removed. A revoked or
expired artifact returns a clear message, and **Refresh** reloads the list.
Without an approved local broker config, the section reports that artifact
browsing is unavailable.

The MCP adapter that Claude Desktop uses cannot list, read, or delete
artifacts. Only this companion can, through its own broker client and the
session credential.
