# ContextShrink for Claude Code (terminal and Desktop Code tab): beat Vachi

Status: plan (2026-10-18). The owner's goal is to beat Vachi on ease of setup,
on visible savings and on real savings.

## What Vachi does (observed on the owner's PC, 2026-10-18)

- **One install command**, `npx vachi-claude`. It signs in and detects the
  Claude Code CLI, the VS Code extension and the Claude Desktop app.
- **A hosted gateway.** It writes `ANTHROPIC_BASE_URL` and a key header into
  `~/.claude/settings.json`. For the Desktop **Code tab**, it puts a small
  launcher inside Desktop's own copy of Claude Code. If the launcher's config is
  missing, the launcher starts Claude Code normally.
- **It runs in the background, with no window**, plus a daily auto-update task.
- **In Claude Code:**
  - a status-line script draws the bottom bar, with its message and its cache
    indicator;
  - a plugin adds the Code-tab chip and `/vachi-savings`, `/vachi-pause` and
    `/vachi-report`;
  - a log of savings per request.
- **Savings shown at once, but inflated:**
  - The dollars are priced as if the trimmed tokens were uncached (about $4.2
    per million). Real cache reads cost about a tenth of input.
  - The installer moves auto-compaction to 1M tokens, so sessions grow far
    larger. Measured: about 900k tokens per turn, of which it trims about 19%.
  - The account-level A/B on the Enterprise laptop is pending, because Desktop
    takes about 4 hours to publish Code usage.

## Where we stand

- **The desktop app** (Tauri tray app `contextshrink-desktop.exe` plus the
  `horizon.exe` client) runs a loopback forwarder on `127.0.0.1:18788` that
  relays to the hosted proxy (`proxy.contextshrink.com`). Claude Code is a
  `Kind::Terminal` tool, so it is **opened in a console through `horizon
  wrap`**. That console is what the owner wants gone. The app already supports
  `Kind::Connect`: it writes a tool's settings and restores them later.
- **The hosted proxy already has:**
  - cache keep-alive, live on the Pi 5;
  - compression of tool output;
  - CCR (reversible compression with a retrieve path);
  - an honest, cache-aware savings ledger.
- **Claude Code savings measured earlier were 0.5–2.5%, from compression
  alone.**
- **The biggest real lever is the cache.** On the owner's real 1,332-request
  Opus session, re-creating expired caches was 23% of the bill. Keep-alive
  would have saved 12–13%, and shrinking the context when a session resumes
  after a long idle another 12–20% (see `cache-keepalive-feature`).

## Plan

### Phase 1: setup in one click, no window (target: as easy as Vachi)

1. **Claude Code becomes a `Connect` tool.** "Connect Claude Code" in the tray
   app, also offered on first run, does the following:
   - writes the user-level `~/.claude/settings.json` `env.ANTHROPIC_BASE_URL`,
     pointing at the forwarder. The path includes a session-agnostic keep-alive
     id (`/k/<id>`), as Codex uses today;
   - sets `ENABLE_TOOL_SEARCH=true`. It does **not** change the compaction
     window;
   - backs up the original file and records every key it added.
     "Disconnect" removes exactly those keys and nothing else, even if the user
     edited the file in between.
   - It detects the Claude Code CLI and the Claude Desktop Code tab, and shows
     both as connected.
2. **The Desktop Code tab.**
   - **Spike first:** does the Code tab honor `~/.claude/settings.json` `env`?
     If it does, nothing else is needed.
   - If it does not, add a launcher like Vachi's, which starts Claude Code
     normally when ContextShrink is off or unhealthy. Re-apply it when Claude
     Desktop updates its bundled Claude Code (watch the folder), and remove it
     on Disconnect.
3. **The background service.**
   - The forwarder runs with `CREATE_NO_WINDOW`, starts when the user signs in
     to Windows (the tray app's autostart), and is restarted if it stops.
   - A health endpoint lets the status line show "ContextShrink off" in red
     when the forwarder is down, with a one-click Disconnect in the tray.
   - Updates come from the existing desktop-app updater (private R2); there is
     no separate scheduled task.
4. **The `wrap` console stays only as an optional "open a terminal session"
   button.** It is no longer the default.

### Phase 2: live savings where the user looks (target: better than Vachi's display)

1. **Bottom bar (Claude Code `statusLine`).**
   - A tiny native helper, `contextshrink-status.exe`, built with the tray app.
     It does **not** need Node, which Desktop-only users may not have.
   - It reads the session id from the status-line JSON on stdin, and a local
     stats file the forwarder updates after every response. It prints within
     about 20 ms.
   - Example: `ContextShrink ✓ saved $1.24 (22%) this session · cache warm 3:41
     · 18 requests`.
   - States:
     - `paused`;
     - `off` (forwarder down);
     - `cache expired, next turn re-creates it`, so the user knows a pause cost
       money;
     - `keep-alive on`.
2. **The plugin (`contextshrink`), installed and removed by Connect:**
   - `/contextshrink-savings`: this session, today, the last 7 days and all
     time;
   - `/contextshrink-pause` and `/contextshrink-resume`;
   - `/contextshrink-report`: sends a diagnostic snapshot, with no prompt
     content unless the user opts in.
   - A Code-tab chip, in whatever form Desktop renders a plugin's status, the
     same mechanism Vachi uses.
3. **Numbers shown at once, and true.**
   - The proxy already computes, for every request, what the unmodified request
     would have cost given the real cache state, priced at real rates: cache
     read, 5-minute and 1-hour writes, input and output.
   - The forwarder sums this per Claude Code session (the
     `x-claude-code-session-id` header) and per day.
   - Shown immediately, with no 4-hour wait: the three figures (see Risks and
     decisions), which are tokens trimmed %, the list-price value of the
     trimmed tokens, and the verified saving.
   - A "verified" mark when the session's usage comes from Anthropic's own
     `usage` fields, which is always the case for proxied requests.
4. **The tray and dashboard** show the same numbers, with a per-session list.

### Phase 3: the savings engine (target: more real dollars saved than Vachi)

In order of expected real value, each one measured before it is switched on by
default:

1. **Cache keep-alive** for Claude Code sessions, turned on through Connect.
   It is already built and live on the Pi 5.
2. **Shrinking the context on resume after a long idle.** When the cache has
   expired anyway, the next request pays to re-create it. That is the cheapest
   moment to fold the history, because breaking the cache costs nothing extra
   then.
3. **Cache-aware paging of stale context**, our version of Vachi's paging:
   - Lossless folds first:
     - file contents from `Read` that are superseded by a later `Edit` or
       `Write`;
     - test and build output from finished runs;
     - old diffs;
     - duplicate reads.
   - Folded spans keep a one-line stub, with CCR retrieval handled inside the
     proxy, so Claude can recover details.
   - Folds are applied in batches, only when this holds:
     `Δtokens × cache-read rate × expected remaining turns > rewritten tokens ×
     write rate`.
4. **Compression of tool output as it enters**, which already exists: logs,
   JSON, test output and grep.
5. **Tool search on.** Tool definitions are about 20% of the bill in Claude Code
   (measured before).
6. **Keep Claude Code's normal compaction window.** We never inflate the
   context the way the 1M setting does.

### Phase 4: proof and release gate

- Use the coding kit (`desktop/coding-kit`, 10 tasks, 29 hidden tests) with
  three arms: plain, ContextShrink and Vachi.
- Our arm is measured exactly per session through the forwarder. The plain arm
  goes through the forwarder in pass-through mode for exact numbers. All three
  are also checked against the account usage total later.
- **Release only when** ContextShrink costs less in real dollars than both plain
  and Vachi, with the same test pass rate. Repeat it in reverse order on another
  day.

## Risks and decisions

- **Anthropic's terms.** Routing subscription or Enterprise sign-in traffic
  through a proxy that changes requests may conflict with Anthropic's terms.
  Vachi does it commercially; it is the owner's risk decision. Pass-through is
  the least concerning step.
- **The savings display (decided 2026-10-18): three figures.**
  1. **Tokens trimmed %:** the same kind of metric as Vachi's percentage.
  2. **"$X of tokens trimmed at API list price":** tokens removed × the API
     input price, the same method as Vachi's dollar figure, and labelled as
     such wherever it is shown.
  3. **"You saved $Y (verified)":** the real difference at actual cache rates.
  - Only figure 3 feeds the Pro fee (5% of savings), the Free cap and any
    "you saved" claim in marketing.
  - The bottom bar may lead with figure 1 or 2, but the label must state the
    basis.
- **Privacy is the same as Vachi's.** Traffic goes through our hosted proxy. A
  later option is a local-only mode, running the proxy on the user's PC, as a
  differentiator.
- **Desktop updates** may move its bundled Claude Code. A folder watcher
  re-applies the launcher, and the status line shows when routing is missing.
- **The forwarder being down breaks sessions.** Mitigations are autostart, a
  watchdog, the red status, one-click Disconnect, and, for Desktop, a launcher
  that starts Claude Code normally when the forwarder is unhealthy.

## Order of work

1. **Spikes (1 day):**
   - does the Code tab honor the `settings.json` env;
   - does a plugin status line show in the Code tab;
   - does Enterprise or subscription sign-in pass through our forwarder and
     hosted proxy unchanged.
2. **Phase 1 and Phase 2** together, as one desktop-app release.
3. **Phase 3**, one lever at a time, each measured with the kit.
4. **Phase 4**, then release.
