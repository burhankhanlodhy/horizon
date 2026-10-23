# contextshrink-codex

ContextShrink Code for Codex. It trims what Codex resends on every turn, so each
5-hour window on your ChatGPT plan goes further. It runs on your computer. Windows
is supported today; macOS and Linux are coming.

## Use

```
npx contextshrink-codex
```

Your browser opens so you can approve this computer (sign in to ContextShrink, then
approve the code shown). Then the command installs a local engine and points Codex
at it. Restart the Codex or ChatGPT desktop app fully, and start a **new** Codex chat.

You need Windows 10 or 11, Node.js 18 or newer, and Codex. Your ChatGPT sign-in is
kept as it is. Ordinary ChatGPT chats and cloud tasks are not changed.

## Price and devices

- 7 days free, no card needed. Then $5 a month. Cancel anytime.
- Without an active trial or subscription, compression stops and Codex works normally.
  Codex never stops working.
- Up to 2 devices per account. A third device is refused with the two connected
  devices named. Disconnect one, then run the command again.

## Privacy

Your prompts never leave your computer. Compression runs on your machine, and
requests go straight to OpenAI. ContextShrink never sees or stores your prompts, your
code, or your ChatGPT sign-in. It only receives the device registration, plan checks,
and hourly usage counts (numbers only).

## Commands

```
npx contextshrink-codex [connect]
npx contextshrink-codex status
npx contextshrink-codex savings
npx contextshrink-codex uninstall
```

- `connect` (the default) signs this computer in and points Codex at the engine.
- `status` shows this device, the plan and trial days left, whether the engine is
  running, and the usage saved today. It makes no model requests.
- `savings` prints the usage saved today and the trial days left.
- `uninstall` restores the Codex configuration. When no ContextShrink tool is left on
  this computer, it also releases the device, stops the engine and deletes the engine
  files. `disconnect` and `logout` do the same.

## What it changes

- `config.toml` in your Codex folder (`CODEX_HOME`, default `~/.codex`): a
  ContextShrink model provider at `http://127.0.0.1:18793/v1`, and
  `model_provider` selects it. Your other settings and comments are kept, and
  uninstall restores the file exactly.
- A Startup shortcut, `ContextShrink Engine.lnk`, starts the engine when you sign in.
- The engine, in `%LOCALAPPDATA%\ContextShrink\engine\<version>`.
- Device files in `~/.contextshrink`: `device.json` (the device key, readable only by
  you), `license.json` (the plan note) and `usage-pending.json`.

The same device and engine serve Claude Code. Uninstalling here keeps them while
`contextshrink-claude` is still connected.

## Upgrading from 0.2.x

Run `npx contextshrink-codex` once. It signs this computer in and moves Codex to the
engine. The old account key is no longer used. Revoke it on the API Keys page.

## When it refuses

- Not Windows: ContextShrink Code runs on Windows today.
- Your Codex config has routing that this connector does not manage: it is left as
  it was, and nothing is signed in.
- Codex is set up with an API key rather than ChatGPT sign-in: this connector does
  not change it.
- Two devices are already connected (exit code 3): run `uninstall` on one of them, or
  remove it at https://code.contextshrink.com/account.
- The engine download does not match its published size and SHA-256, or its Windows
  signature is not valid: nothing is installed.
- The engine does not start: the Codex configuration is not changed.

## Existing local chats

These commands need **Node.js 22.13 or newer** (built-in SQLite). Close Codex and all
Codex CLI sessions first. The `chats` commands preview by default; add `--apply` to
change chats.

```powershell
npx contextshrink-codex chats list
npx contextshrink-codex chats migrate CHAT_ID
npx contextshrink-codex chats migrate CHAT_ID --apply
npx contextshrink-codex chats restore CHAT_ID --apply
```

## Live savings window (Windows)

```powershell
npx contextshrink-codex savings-window
```

A small window shows the usage saved today and the trial days left. It refreshes every
30 seconds, and shows "Savings unavailable" on errors or stale data.

## Development

Tests use `node:test`, with `npm test` (serialized, `--test-concurrency=1`, Node 20.10
or newer). They run against a fake ContextShrink API and a fake engine on 127.0.0.1.
Every path lives in a temp folder, and the Windows steps use a fake adapter, so no test
reads your real `~/.codex` or runs PowerShell.

Set `CONTEXTSHRINK_API_URL`, `CONTEXTSHRINK_ENGINE_URL`, `CONTEXTSHRINK_HOME` and
`CODEX_HOME` to point the CLI elsewhere.
