# ContextShrink Desktop MCP smoke test

Phase A exposes four read-only tools against three built-in synthetic fixtures. The stdio smoke test checks the MCP SDK handshake and tool list, exact open/repeat behavior, line retrieval, rejection of an unauthorized source and an unsupported mode, and raw-only stats. It does not read files from your machine, run compression, contact a model, require a key, or report a dollar amount.

Run the doctor and probe from the repository root with the project's virtual environment:

```powershell
.venv\Scripts\python.exe -m horizon.desktop_mcp doctor
.venv\Scripts\python.exe desktop\mcp\smoke_client.py
```

The smoke client launches the server as a child process with the current Python executable, uses the repository root as its working directory, applies a 15-second overall timeout, and closes the MCP session and child through the SDK stdio context manager. To save a report, choose a path explicitly:

```powershell
.venv\Scripts\python.exe desktop\mcp\smoke_client.py --output .\desktop-mcp-report.json
```

The report contains runtime and check metadata, not fixture content or credentials. `desktop_application_compatibility` remains `not_tested` even when the local stdio probe passes: a local SDK/client handshake does not prove that Claude Desktop or another application can start, list, or call the server.

The checked-in `phase-a-runtime-report.json` records the local integration
probe. `diagnostic-settings.json` and
`claude-desktop-config.example.json` supply a fixture-only registration example
for this checkout. The example is machine-specific and is not applied to your
existing Claude settings automatically.

## Manual Claude Desktop check

The workspace smoke test does not edit application settings. If you choose to try the server in Claude Desktop, open **Settings → Developer → Edit Config**, preserve existing entries, and add a server entry to that Desktop configuration. Replace the sample workspace and Python paths with the absolute paths on your machine. The launcher is independent of Claude Desktop's working directory.

```json
{
  "mcpServers": {
    "ContextShrink": {
      "command": "C:\\Users\\Burhan\\OneDrive\\Desktop\\ContextShrink\\.venv\\Scripts\\python.exe",
      "args": [
        "C:\\Users\\Burhan\\OneDrive\\Desktop\\ContextShrink\\desktop\\mcp\\launch_stdio.py"
      ]
    }
  }
}
```

Restart Claude Desktop after saving. Check that it shows `contextshrink_search`, `contextshrink_open`, `contextshrink_retrieve`, and `contextshrink_stats`, then call them only with the bundled `fixture:note`, `fixture:log`, and `fixture:json` entries. The exact open mode returns the fixture text as stored. Search and retrieval are diagnostic operations over the same fixtures; they are not compressed. Stats explicitly identify raw mode and do not include USD costs. This manual app check is separate from the local smoke test and must be performed in the target app to claim Desktop compatibility.

## Phase B local broker pilot

The local profile is opt-in; the default remains the synthetic diagnostic
profile. Start with the included synthetic file. Copy
`local-settings.example.json` to `local-settings.json`, then replace
`YOUR_USERNAME` with your Windows account name. The config explicitly grants
only the `desktop/mcp/sample-sources` folder. Keep real documents outside this
pilot grant until you choose to add another approved root yourself.

Start the broker from the repository root in its own terminal:

```powershell
.venv\Scripts\python.exe -m horizon.desktop_mcp broker --config desktop\mcp\local-settings.json
```

After the broker starts, add a separate Claude Desktop MCP entry using the
existing launcher and the local config path:

```json
{
  "mcpServers": {
    "contextshrink-local": {
      "command": "C:/path/to/ContextShrink/.venv/Scripts/python.exe",
      "args": [
        "C:/path/to/ContextShrink/desktop/mcp/launch_stdio.py",
        "--config",
        "C:/path/to/ContextShrink/desktop/mcp/local-settings.json"
      ]
    }
  }
}
```

The broker supervises a worker process that owns source readers and snapshots,
so a stalled file operation can be terminated and all handles cleared. This
process boundary is not an OS sandbox. The MCP process only sends bounded read
operations to the broker's same-user named pipe. Processes running as the
same Windows user share broker handles and the approved workspace. This
transport does not separate mutually untrusted processes under one user
account. The broker reloads its explicit config on
each request. Changing `approved_roots` rebuilds the source store and drops
existing handles; setting the list to `[]` revokes all roots. An unreadable or
invalid config fails closed. Changes to `broker_id` or the pipe's connection
limits require restarting the broker. Restarting the broker clears all
snapshots and handles. Snapshots also expire at the configured TTL. If an
opaque handle is unknown after expiry or worker restart, run a fresh search
for the approved file to obtain a new handle; opening an existing handle does
not create a new snapshot.

The named pipe uses an explicit current-user and LocalSystem ACL, rejects
remote clients, checks the connected client SID after reading only its fixed
version/length prefix, and checks the server process SID before sending request
contents. It acknowledges responses after validating them before disconnecting.
Local tests cover processes under the current user; cross-SID ACL behavior
still needs a second Windows account to verify. Source text is untrusted data.
Search is limited to approved source discovery; exact reads return versioned content
without compression. Results larger than the configured response cap return
opaque source/version/line-count metadata so the caller can request exact line
ranges. No separate broker API grants roots, imports files, accesses the
clipboard, or performs writes.

### Phase C sample-source walk-through

The existing local settings grant the pilot `desktop/mcp/sample-sources`
folder. Keep that grant and Claude Desktop registration as they are. If the
broker is not running, start it from the repository root using the existing
local settings file:

```powershell
.venv\Scripts\python.exe -m horizon.desktop_mcp broker --config desktop\mcp\local-settings.json
```

In Claude Desktop, search for `phase-c-` and open these returned handles:

- `phase-c-repeated.log` in `compact` mode. The repeated block should use the
  `reversible_fold` fidelity label and remain below the configured 2,000-token
  estimate budget.
- `phase-c-constraints.md` in `focused` mode with query `retention legal hold
  7 years`. It should retain “exactly 7 years” and “must not be deleted,” and
  label its coverage partial. The escalation sentence is outside the focused
  excerpt; use an exact line selector on the same source handle to recover it.
- `phase-c-structured.json` in `compact` mode. Its JSON whitespace is
  normalized, not byte-lossless. A JSON pointer such as
  `/records/0/legal_hold/delete_allowed` returns the exact scalar `false`.
- `phase-c-table.csv` with a `rows` selector. Row 1 is the header, so rows 1–2
  return the header and `SYN-001` record.

The checked-in sample-source test verifies these outcomes and the 2,000-token
estimated budget locally. Tiny or unsupported inputs may fall back to exact
text, and exact-result metadata can be slightly larger than the source. If a
source handle expires or is unknown after a worker/broker restart, search
again and use the fresh returned handle; do not reuse an old identifier or
edit the external Desktop config for this walk-through. This local test does
not count as a live Claude Desktop Phase C check.

## Phase C local retrieval and measurement

The local profile adds exact, compact, focused, and overview presentations.
Exact mode preserves the extracted text in the requested scope and paginates
when needed. Compact mode is limited to transformations whose local verifier
accepts them; ambiguous duplicate-key JSON remains exact. Focused and overview
results are selections with partial coverage, not lossless summaries. Returned
source text remains untrusted data. Keep the source handle, version, policy
version, and line references when requesting an exact follow-up.

Token counts are estimates. When a verified local tiktoken `o200k_base` cache
is present, ContextShrink uses it as a stable stand-in for Claude. Anthropic's
Claude tokenizer is private, so these values are not exact Claude counts. If
the verified cache is absent or invalid, the local result identifies its
UTF-8-byte heuristic fallback. No tokenizer vocabulary is downloaded by the
desktop measurement path.

Per-result accounting separates source-text reduction from the complete
inner result JSON size and estimate. It excludes SDK/JSON-RPC wrappers,
registered tool schemas, Desktop-added context, and provider-hidden prompt
material unless an evaluation captures those separately. Host overhead and
Enterprise billing remain unknown. Local HMAC event signatures provide
integrity only while the worker's ephemeral key exists; the bounded event log
is lost on worker restart and is not provider or billing evidence.

Run the synthetic-only raw-versus-optimized MCP SDK evaluation from the
repository root:

```powershell
.venv\Scripts\python.exe desktop\mcp\phase_c_eval.py --output desktop\mcp\phase-c-eval-report.json
```

This uses only bundled synthetic text, makes no model or network calls, and
does not read approved user sources. It captures the same tool schemas and
server instructions once per arm, serialized arguments and typed SDK
`CallToolResult` values for every search/open/retrieval/error call, and inner
result text separately. It includes exact recovery calls in both task totals.
The in-memory SDK transport does not expose stdio JSON-RPC frames, Claude
Desktop-added context, or provider-hidden prompt content; those remain unknown.
Deterministic checks preserve required numeric and negated constraints or
recover them through exact selectors. The report includes a tiny no-gain case,
duplicate-key and JSON lexeme fallbacks, and a repeated-log compact case. It is
not a Claude Desktop compatibility test, a blind model-answer trial, or an
Enterprise savings measurement. Small inputs can have zero or negative savings
after metadata, tool calls, and schemas; no positive result is promised for
every task.

The checked-in synthetic output is
[`phase-c-eval-report.json`](phase-c-eval-report.json). In this captured run,
the estimated observed MCP surface was 4,827 `o200k_base` tokens for raw and
5,169 for optimized, including one more exact recovery call in the optimized
arm. The 342-token (about 7.1%) increase is specific to this synthetic task;
it is not a Claude count, observed Enterprise usage, task-window billing, or a
savings claim. On October 13, 2026, `pytest tests/desktop_mcp -q` reported 155
passed and 1 skipped. A separate synthetic 8.1 MB broker/SDK check completed in 2.03
seconds with the default 64 KiB response cap and 10-second deadline. All timings
are machine-local and do not establish Claude Desktop latency. Local
implementation and tests are complete; live Phase C Desktop validation and
billing attribution remain pending.

## Phase D current-request preparation

Phase D provides a standalone graphical companion and a one-shot CLI. Both
prepare an inspectable packet before submission; the user reviews and pastes it
into an existing chat. The companion runs a small Python HTTP server on
`127.0.0.1`, opens a browser tab, and requires no Tauri shell, hosted login,
network access, model call, or Claude configuration change for inline use.
Neither route reads the clipboard, sends a message, or changes Desktop config.

From the named worktree, use the primary checkout's Python environment:

```powershell
Set-Location 'C:\Users\Burhan\OneDrive\Desktop\ContextShrink\.claude\worktrees\phase-c-review-orchestrate-54e903'
$python = 'C:\Users\Burhan\OneDrive\Desktop\ContextShrink\.venv\Scripts\python.exe'
& $python desktop\mcp\prepare_companion.py
```

The browser companion has separate Request and Supporting context fields,
defaults to Compact, and previews the complete exact packet, coverage, fidelity,
fallback reason, and estimates. Any edited input invalidates that preview.
Copy uses browser clipboard only after the user clicks Copy; if permission is
unavailable, the user can select the visible packet and press Ctrl+C. The CLI
route remains available as `python -m horizon.desktop_mcp prepare`; it accepts
explicit file paths and displays the packet body only with `--show-packet`.

Modes are `passthrough`, `compact`, `focused`, `reference`, and
`reference_only`. The last three use an existing broker handle and matching
local config; focused mode also requires a query. Reference-only keeps the
source handle/version without embedding source text. Focused coverage is
partial. No route imports arbitrary paths or changes approved roots.

Optional pasted-context recovery uses a dedicated approved folder. Copy
`companion-settings.example.json` to the ignored local
`companion-settings.json`, set its only approved root, and run the broker and
companion with that same config in separate foreground terminals. The UI
discloses the approved folder and requires an explicit checkbox and separate
Save action. The server writes a generated file under its session-owned child
and asks the existing broker `open` operation for a normal versioned
`cs1_…` source handle. The staging record ID is separate. A dedicated local,
non-synced folder is recommended; OneDrive or other OS sync can copy staged
text if the user chooses a synced root.

Staging lasts only for the running companion session and broker TTL (at most
one hour). Keep both terminals open while the handle is needed. Removing its
staged file blocks exact recovery; an inaccessible broker snapshot may remain
in memory until TTL/restart. Clear session and quit performs graceful cleanup;
closing the tab does not stop the terminal server. The complete config and
manual Claude MCP recovery example are in
[`companion-ui/README.md`](companion-ui/README.md).

The local suite reports 321 passed and 1 skipped. A repeatable, synthetic-only
real broker plus authenticated HTTP probe is checked in at
[`phase_d_companion_probe.py`](phase_d_companion_probe.py); its metadata report
records 11 passing checks. The Phase D note lists exact commands and
limitations. Browser review does not establish Claude Desktop compatibility,
MCP Apps support, reduced Claude usage, or Enterprise billing savings. Native
Tauri integration, app packaging, and clean-machine installation remain
separate later work.
