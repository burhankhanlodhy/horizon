# contextshrink-claude

ContextShrink Code for Claude Code. It trims what Claude Code resends on every
turn, so each 5-hour window on your Claude plan goes further. It runs on your
computer. Windows is supported today; macOS and Linux are coming.

## Use

```
npx contextshrink-claude
```

Your browser opens so you can approve this computer (sign in to ContextShrink, then
approve the code shown). Then the command installs a local engine, points Claude
Code at it, and sets up the Claude Desktop Code tab if Claude Desktop is installed.
Start a new Claude Code session afterwards.

You need Windows 10 or 11, Node.js 18 or newer, and Claude Code. Your Claude sign-in
is not touched.

## Price and devices

- 7 days free, no card needed. Then $5 a month. Cancel anytime.
- Without an active free week or subscription, compression stops and Claude works
  normally. Claude never stops working.
- Up to 2 devices per account. A third device is refused with the two connected
  devices named. Disconnect one, then run the command again.

## Privacy

Your prompts never leave your computer. Compression runs on your machine, and
requests go straight to Anthropic. ContextShrink never sees or stores your prompts,
your code, or your Claude sign-in. It only receives the device registration, plan
checks, and hourly usage counts (numbers only).

## Commands

```
npx contextshrink-claude [connect] [--force] [--no-code-tab]
npx contextshrink-claude status
npx contextshrink-claude uninstall
npx contextshrink-claude lean on [--force]
npx contextshrink-claude lean off
npx contextshrink-claude lean status
```

- `connect` (the default) signs this computer in and sets everything up. `--force`
  replaces another gateway or an existing `ANTHROPIC_CUSTOM_HEADERS`; uninstall puts
  them back. `--no-code-tab` skips the Claude Desktop Code tab.
- `status` shows this device, the plan and free-week days left, whether the engine is
  running, and the usage saved today.
- `uninstall` restores Claude Code's settings and the Code tab. When no ContextShrink
  tool is left on this computer, it also releases the device, stops the engine and
  deletes the engine files. `disconnect` and `logout` do the same.

## Keep sessions lean

`lean on` caps Claude Code's auto-compact window at 200,000 tokens. A long session is
then summarised instead of resending about 1M tokens of context on every turn, so a
subscription plan goes further. The cost: older history becomes a summary. The value is
set in `settings.json` and, when npx owns the Code tab, in `codetab.json`. Start a new
Claude Code session (or restart Claude Desktop) afterwards.

- `lean off` puts back the value that was there before. `uninstall` does the same.
- A `CLAUDE_CODE_AUTO_COMPACT_WINDOW` you set yourself is refused unless you add `--force`.
- Connect does not change this setting. The desktop app's Code tab is never changed.

## What it changes

- `~/.claude/settings.json`: `ANTHROPIC_BASE_URL` points at the local engine
  (`http://127.0.0.1:18793`); `ENABLE_TOOL_SEARCH` is set to `"true"` unless you set
  it; a SessionStart hook starts the engine; the status line shows usage saved today.
- A Startup shortcut, `ContextShrink Engine.lnk`, starts the engine when you sign in.
- The engine, in `%LOCALAPPDATA%\ContextShrink\engine\<version>`.
- Device files in `~/.contextshrink`: `device.json` (the device key, readable only by
  you), `license.json` (the plan note) and `usage-pending.json` (usage counts not yet
  sent).
- On Windows with Claude Desktop, the Code tab routing (see `codetab.json`). If the
  ContextShrink desktop app is installed but its Code-tab routing is off, connect routes
  the Code tab through the engine. Uninstall hands the desktop app's `codetab.json` back
  byte for byte (`npx-takeover.json` keeps the copy), unless the app changed it since.

Every setting connect changes is recorded, so uninstall restores exactly what it
changed. A value you edit after connecting is left alone, and uninstall says so.

## Upgrading from 0.2.x

Run `npx contextshrink-claude` once. It signs this computer in and replaces the
account key in settings with the engine address. The old account key is no longer
used. Revoke it on the API Keys page.

## When it refuses

- Not Windows: ContextShrink Code runs on Windows today.
- Another gateway is set in `ANTHROPIC_BASE_URL`, or `ANTHROPIC_CUSTOM_HEADERS` is
  set: use `--force` to replace it, or remove it yourself.
- Claude Code settings changed since connect: run `uninstall` to restore them, then
  connect again.
- Two devices are already connected (exit code 3): run `uninstall` on one of them, or
  remove it at https://code.contextshrink.com/account.
- The engine download does not match its published size and SHA-256, or its Windows
  signature is not valid: nothing is installed.
- The engine does not start: Claude Code's settings are not changed.

## Development

Tests use `node:test`, with `npm test`. They run against a fake ContextShrink API and
a fake engine on 127.0.0.1. Every path lives in a temp folder, and the Windows steps
(browser, signature check, unzip, shortcut) use a fake adapter, so no test reads your
real `~/.claude` or runs PowerShell. Test runs are serialized (`--test-concurrency=1`,
Node 20.10 or newer).

Set `CONTEXTSHRINK_API_URL`, `CONTEXTSHRINK_ENGINE_URL` and `CONTEXTSHRINK_HOME` to
point the CLI elsewhere. Test-only overrides (`CONTEXTSHRINK_NPX_TEST=1` and
`CONTEXTSHRINK_NPX_*`) are ignored unless `CONTEXTSHRINK_NPX_TEST=1` is set.
