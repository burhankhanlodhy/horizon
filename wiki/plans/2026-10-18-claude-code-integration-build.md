# Build spec: Claude Code and Desktop Code-tab integration (phases 1 and 2)

Follows `2026-10-18-claude-code-integration-beat-vachi.md`. The spikes on
2026-10-18 showed:

- **Terminal Claude Code** honors the `env` block of settings files, including
  `ANTHROPIC_BASE_URL`.
- **The Desktop Code tab ignores it.** A session in a folder routed to a logging
  relay sent nothing to the relay. The Code tab also does **not** render the
  `statusLine`.
- **How the Code tab gets its binary.** Desktop downloads Claude Code to
  `%LOCALAPPDATA%\Packages\Claude_*\LocalCache\Roaming\Claude\claude-code\<version>\<hash>\claude.exe`.
  Next to it are `.payload` (`{"sha256","size"}` of `claude.exe`) and
  `.verified` (the hash folder name).
- **How Vachi routes the Code tab.** It replaces `claude.exe` with a roughly 2 MB
  trampoline. On `disable` it put the real binary back and left
  `claude.exe.vachi-discard`. Desktop launched the trampoline fine, so it does
  not re-check the hash on every launch.
- **The Code tab's UI.** The documented route is a Claude Code **mod** (a plugin
  with a hooks module): AbovePrompt, PromptHint, Spinner or Pane, supported in
  the Desktop Code tab from Claude Code v2.1.286. A plugin **cannot** set the
  main `statusLine`. A user-level `statusLine` works for the terminal.
- **Claude Code sends `x-claude-code-session-id`** on `/v1/messages`.
- **The current forwarder** (`horizon/forwarder.py`, port 18788, relaying to
  `proxy.contextshrink.com`) is windowless, but it is a child of the tray app
  and dies with it. There is no autostart, no health route and no local stats.
  It passes `Authorization` (the OAuth bearer) through unchanged, and `/k/<id>`
  paths work.
- **The existing Connect code for `vscode-claude`**
  (`horizon/providers/claude/vscode.py`) already writes the user-level
  `~/.claude/settings.json` env with a state file, and restores only its own
  keys.
- **Bug:** `unwrap claude` (`wrap.py`) strips those user-level keys without
  checking that state, and the desktop app runs it whenever a console closes.

## Contracts shared by all work items

### Files under `%LOCALAPPDATA%\ContextShrink\`

**`claude-code\status.json`** is written atomically by the forwarder after
every `/v1/messages` response, and at least every 10 s as a heartbeat:

```json
{
  "v": 1,
  "updated_at": "ISO-8601",
  "forwarder": {"pid": 0, "version": "x.y.z", "port": 18788, "healthy": true},
  "paused": false,
  "sessions": {
    "<session id>": {
      "first_at": "", "last_at": "", "requests": 0, "model": "",
      "tokens_before": 0, "tokens_after": 0,
      "trimmed_list_usd": 0.0,
      "verified_saved_usd": 0.0, "verified_requests": 0,
      "cache": {"last_write_at": "", "ttl_s": 300, "keepalive": false}
    }
  },
  "today": {"...": "same counters, local date"},
  "last_7_days": {"...": "same counters"},
  "all_time": {"...": "same counters"}
}
```

Keep at most 200 sessions; drop the oldest by `last_at`.

**`claude-code\status.txt`** is one pre-rendered line for the latest session,
for example:

`ContextShrink ✓ trimmed 22% · $1.84 of tokens at list price · saved $0.41 verified · 18 req`

- When the heartbeat is older than 30 s, the line reads `ContextShrink off`.
- When paused, it reads `ContextShrink paused`.

**`claude-code\codetab.json`** is read by the Code-tab launcher:

```json
{"v": 1, "enabled": true, "base_url": "http://127.0.0.1:18788/k/<keepalive id>",
 "env": {"ENABLE_TOOL_SEARCH": "true"}, "health_port": 18788}
```

**`claude-code\codetab-state.json`** is the patcher's record: for each version
folder, the patched paths and the real binary's sha256.

### The three savings figures (owner decision, 2026-10-18)

1. **Tokens trimmed %:** `1 - tokens_after / tokens_before`, from the existing
   `x-horizon-tokens-before` and `x-horizon-tokens-after` response headers.
2. **"$ of tokens at list price"** (`trimmed_list_usd`): tokens saved × the
   model's list **input** price, from `horizon/pricing`. It is always labelled
   "at list price".
3. **"Saved $ (verified)"** (`verified_saved_usd`): the proxy's cache-aware
   per-request saving, the same number its savings ledger records.
   - For streaming responses the headers are already sent, so the proxy adds
     **one SSE comment line** just before the stream ends:
     `: x-horizon-verified-savings-usd=<float>`.
   - It does so **only** when the request carries
     `x-horizon-forwarder: claude-code`, a header the forwarder adds.
   - The forwarder parses that line and **strips it** before the client sees
     it.
   - For non-streaming responses it is a response header with the same name.
   - Only figure 3 feeds the fee, the Free cap and any "you saved" claim.

### Session id

- Taken from the `x-claude-code-session-id` request header.
- Falls back to the `/k/<id>` keep-alive id, then to `unknown`.

## W1: Python client and proxy (agent A)

1. **Forwarder** (`horizon/forwarder.py`, CLI `horizon forward start`):
   - `GET /__cs/health` is handled **before** the catch-all relay. It returns
     `{"ok", "version", "pid", "port", "remote", "started_at"}` and never
     relays.
   - It adds `x-horizon-forwarder: claude-code` to relayed `/v1/messages`
     requests.
   - The stats writer follows the contracts above:
     - it reads the `x-horizon-tokens-before`, `-after` and `-saved` response
       headers;
     - it parses and strips the verified-savings SSE comment;
     - it writes `status.json` and `status.txt` atomically (tempfile then
       `os.replace`), with the 10 s heartbeat;
     - it never logs or stores prompt content or auth values.
   - `--pause-file`: while `claude-code\paused` exists, the forwarder adds
     `x-horizon-bypass: 1` so the proxy passes requests through, and the status
     reads paused.
2. **Claude Code Connect** (`horizon desktop connect claude-code` and
   `disconnect claude-code`):
   - generalize `vscode.py` so `claude-code` and `vscode-claude` share one state
     file and cannot fight over it;
   - take the settings lock that `wrap.py` uses;
   - set `env.ANTHROPIC_BASE_URL=http://127.0.0.1:18788/k/<id>`, where the id is
     per machine and stored in the state file;
   - set `env.ENABLE_TOOL_SEARCH=true` only after confirming that the hosted
     proxy forwards `tool_reference`, `defer_loading` and the tool-search betas
     unchanged; otherwise keep `false` and report why;
   - **install** a user-level `statusLine` that runs
     `%LOCALAPPDATA%\ContextShrink\bin\contextshrink-status.exe`, only if the
     user has no `statusLine` (otherwise leave theirs and report it), with
     restore on disconnect;
   - **install the plugin** from a local directory marketplace
     (`extraKnownMarketplaces` `contextshrink` with source type `directory`,
     plus `enabledPlugins["contextshrink@contextshrink"]`), with restore on
     disconnect.
3. **Fix `unwrap claude`.** It must not remove user-level keys owned by a live
   Connect state. Add a regression test for it.
4. **Proxy:** emit the verified-savings figure as specified (the SSE comment,
   or the header when not streaming), gated on `x-horizon-forwarder`. Find the
   cache-aware per-request saving the savings ledger uses (`horizon/proxy/cost.py`,
   `prometheus_metrics.estimate_request_savings_usd`, `savings_ledger.py`) and
   emit **that** value. Add tests to show:
   - Claude Code's SSE parsing sees no change once the forwarder strips the
     line;
   - nothing is emitted without the header.

## W2: Tauri desktop app (agent B, `desktop/app/src-tauri`)

1. **The forwarder runs as its own background process.**
   - Spawn it with `CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP |
     CREATE_BREAKAWAY_FROM_JOB`.
   - Do **not** kill it on app exit. Kill it only on logout, explicit
     Disconnect of every tool that needs it, update, or uninstall.
   - Adopt a running forwarder only when `/__cs/health` answers and the version
     matches; otherwise replace it.
   - A watchdog checks it every 30 s and restarts it.
2. **Autostart.** When any tool is connected, register an HKCU `Run` entry
   that starts the tray app with `--background` (no window, tray only, and it
   starts the forwarder). Remove the entry when nothing is connected and on
   uninstall (`windows/hooks.nsh`).
3. **Tool entry** "Claude Code (terminal and Desktop Code tab)", kind `Connect`:
   - Connect runs `horizon desktop connect claude-code`, then `horizon codetab
     install` (from W3), and shows the result for each target: terminal,
     Code tab, status line, plugin.
   - Disconnect reverses all of it.
   - Keep `vscode-claude` working. Both show as connected, because they share
     state.
4. **Desktop launches.** The Terminal launcher must use the fixed `unwrap`
   from W1, so it never strips a Connect route.
5. **The tray menu** shows the current `status.txt` line and offers Pause and
   Resume (creating or removing the pause file).
6. **Version 0.7.0.** `cargo check` and existing tests pass.

## W3: Code-tab launcher, status helper and plugin (agent C)

1. **New Rust crate** `desktop/claude-code-tools/` (its own small workspace,
   not part of the root workspace), with two binaries:
   - **`claude-code-launcher.exe`**, which replaces Desktop's `claude.exe` as a
     trampoline:
     - it reads `codetab.json`;
     - when enabled and a TCP connect to `127.0.0.1:<health_port>` succeeds
       within 150 ms, it sets `ANTHROPIC_BASE_URL` and the `env` entries for
       the child;
     - it starts `claude.cs-real.exe` from the same folder with the same
       arguments, inherited stdio and the same working directory, waits, and
       exits with the child's exit code;
     - if anything fails (no config, malformed config, health check fails), it
       starts the real binary unchanged, failing open. If the real binary is
       missing, it exits with a clear message on stderr;
     - it is a console subsystem binary like the original, so the stdio
       contract is identical;
     - target size is 1 MB or less, release profile with `opt-level="z"`,
       `lto`, `strip`.
   - **`contextshrink-status.exe`**: reads the statusLine JSON on stdin (it may
     be empty), prints the `status.json` line for that `session_id` (or
     `status.txt` as a fallback), and exits 0. It must finish within 20 ms. It
     has a `--detail` flag that prints the session, today, the last 7 days and
     all time as a small table, for the savings command.
2. **Patcher** (`horizon codetab install|remove|status`, in a new module
   `horizon/desktop_codetab.py` registered with a small edit to the CLI):
   - It finds Desktop's `claude-code` roots: MSIX
     `%LOCALAPPDATA%\Packages\Claude_*\LocalCache\Roaming\Claude\claude-code`
     and classic `%APPDATA%\Claude\claude-code`.
   - For every `<version>\<hash>\` folder: if `claude.exe`'s sha256 equals
     `.payload`, rename it to `claude.cs-real.exe` and copy the launcher in as
     `claude.exe`.
   - If `claude.exe` is not the real binary (another trampoline, such as
     Vachi's, or an earlier ContextShrink one), use the existing
     `claude.cs-real.exe` when its sha matches `.payload`. Otherwise refuse that
     folder and report it.
   - A locked file (a running session) is skipped, reported, and retried by the
     `--watch` mode.
   - It never deletes a real binary.
   - It writes `codetab.json` and `codetab-state.json`.
   - `remove` restores the real `claude.exe` everywhere from `claude.cs-real.exe`
     after checking its sha, and deletes only files it placed.
   - `status` reports each folder: patched, real, foreign, or locked.
   - `--watch` polls the roots every 10 s, so new versions Desktop downloads
     are patched; the tray app runs it in the background.
   - Tests use fake roots, payload files and a stand-in launcher.
3. **Plugin** `desktop/claude-code-plugin/` (`contextshrink@contextshrink`,
   with a directory marketplace), containing:
   - `.claude-plugin/plugin.json` and the marketplace manifest;
   - **commands or skills:**
     - `/contextshrink-savings` runs `contextshrink-status.exe --detail` and
       presents it;
     - `/contextshrink-pause` and `/contextshrink-resume` create or remove the
       pause file;
     - `/contextshrink-report` collects `status.json` plus the forwarder health
       into a diagnostics file, with no prompt content;
   - **a mod** (hooks module) that shows the `status.txt` line as a
     **PromptHint**, or an **AbovePrompt** band if a hint cannot hold it,
     refreshed when a response arrives. Follow the official mods reference at
     `https://code.claude.com/docs/en/plugins/mods/reference`. Use only
     documented APIs, and test it in the terminal Claude Code at least.

## Out of scope here

The savings engine (phase 3), the measurement (phase 4), macOS, and deploying
the proxy change to the Pi 5. The owner deploys that; the verified figure reads
`pending` until it is live.
