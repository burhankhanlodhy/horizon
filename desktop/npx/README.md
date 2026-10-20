# contextshrink-claude

Connects Claude Code (in a terminal or VS Code) to the
[ContextShrink](https://contextshrink.com) hosted proxy, which compresses what
Claude Code sends and keeps its prompt cache warm. It is plain JavaScript run by
Node: no dependencies, no background process. In a terminal setup it changes three
keys in Claude Code's settings file and nothing else. On Windows it also sets up the
Claude Desktop Code tab, which is described below.

## Use

Requires Node 18 or newer and a ContextShrink account.

```
npx contextshrink-claude              # sign in, then connect
npx contextshrink-claude status       # connected or not, the proxy host, tool search
npx contextshrink-claude disconnect   # put back the settings it changed
npx contextshrink-claude logout       # disconnect, then revoke this computer's account key
```

Start a new Claude Code session afterwards; open sessions keep the old settings.

Other commands:

- `npx contextshrink-claude login` makes a new account key for this computer and connects
  it. It also revokes the key that connect made before.
- `npx contextshrink-claude login --key` reads an account key from stdin instead of
  signing in. A key added this way is not revoked by logout.
- `--force` replaces another gateway or an existing header. Disconnect restores
  the old value.
- `--no-code-tab` (connect and login) skips the Claude Desktop Code tab on Windows.

Sign-in asks for your email and password. The password is not shown as you type.
When stdin is a pipe, the answers are read in order, one per line.

## What it changes

Claude Code reads its user settings from `$CLAUDE_CONFIG_DIR/settings.json`, or
`~/.claude/settings.json` when that variable is not set. Connect adds three keys
under `env`:

| Key | Value |
| --- | --- |
| `ANTHROPIC_BASE_URL` | `https://proxy.contextshrink.com/k/<keep-alive id>` |
| `ANTHROPIC_CUSTOM_HEADERS` | `X-Horizon-Proxy-Token: <account key>` |
| `ENABLE_TOOL_SEARCH` | `true`, only when the key is not set yet |

With a proxy in `ANTHROPIC_BASE_URL`, Claude Code turns its own MCP tool search
off. `ENABLE_TOOL_SEARCH=true` keeps it on, as it is without a proxy. The rules:

- Not set yet: connect sets it to `true` and records that it did.
- Already `true`: connect leaves it, and disconnect keeps it.
- Set to anything else (for example `false`): connect leaves your value and prints
  one line saying tool search is off by your choice.

Connect also writes two small files:

- `.contextshrink-npx.json` next to `settings.json`. It records the previous value
  of each key and the values connect wrote, so disconnect can restore them.
- `~/.contextshrink/npx.json` (owner-only where the OS allows it). It holds the
  keep-alive id and the account key's id, never the key itself.

Everything else in `settings.json` is kept, including key order and indentation.

## Claude Desktop Code tab (Windows)

Claude Desktop's Code tab ignores `settings.json`, so on Windows connect also routes
it through ContextShrink. To do that it changes files that Claude Desktop uses:

- In each Claude Desktop Claude Code folder (under `%LOCALAPPDATA%\Packages\Claude_*`
  and `%APPDATA%\Claude`), `claude.exe` is replaced by a small launcher with the
  publisher's signature. The real binary is kept beside it as `claude.cs-real.exe`.
  Nothing is deleted.
- `%LOCALAPPDATA%\ContextShrink\claude-code\codetab.json` holds the proxy address and
  the account key, in plain text. The launcher reads it.
- `%LOCALAPPDATA%\ContextShrink\claude-code\npx-bin\` holds the launcher and
  `contextshrink-codetab.exe`, the helper that patches and restores the folders.
- A shortcut in your Startup folder runs the helper at sign-in. A Claude Desktop
  update creates a new folder, and the helper patches it then.

The two files come from `app.contextshrink.com`. Each one must match the SHA-256 pinned
in this package and carry the publisher's Authenticode signature before it is used.

When Claude Desktop is not installed, or on a system other than Windows, connect
prints one line saying the Code tab is not set up, and exits normally.

To undo it, run `npx contextshrink-claude disconnect`. It restores the real binaries,
removes the startup shortcut and the files above, and leaves other files alone. If
Claude Desktop is running a session, a Claude Code folder can be in use. Disconnect
then keeps the Code tab files, says so, and you run disconnect again after you close
Claude Desktop.

If the ContextShrink desktop app manages the Code tab (its `codetab.json` does not have
`"owner": "npx"`), connect leaves the Code tab alone, and disconnect does too. Use
`--no-code-tab` to skip the Code tab on connect or login.

## How to undo it

Run `npx contextshrink-claude disconnect`. It restores each key only if its value is still
the one this tool wrote; a key you edited since is left alone, and the output says
so. If the settings file did not exist before connect, disconnect removes it. On
Windows it also restores the Code tab, as described above.

To revoke the account key as well, run `npx contextshrink-claude logout`. It asks you to
sign in again, because revoking a key needs your account session. You can also
revoke keys on the API Keys page of the ContextShrink dashboard.

## Security

The account key is stored in plain text in `settings.json`, and a copy is in
`.contextshrink-npx.json` next to it. On Windows it is also in the Code tab's
`codetab.json`. Anyone who can read those files can use the key, and any program you
run as your Windows user can read them. If any of these files leaks, revoke the key on
the API Keys page.

## When it refuses

Connect changes nothing, and says why, when:

- the ContextShrink desktop app already manages Claude Code on this computer;
- `ANTHROPIC_BASE_URL` points at another host;
- `ANTHROPIC_CUSTOM_HEADERS` is already set;
- `settings.json` is not valid JSON. The file is never overwritten.

`--force` overrides the second and third cases.

On Windows, the Code tab part stops with an error, and the terminal part stays
connected, when `codetab.json` exists but is not valid JSON, or when a download, the
hash check or the signature check fails. Nothing is installed in those cases.

## Limitations

- The Code tab is set up on Windows only. On macOS and Linux, use the desktop app.
- Until the first release of the Code tab files is published, connect sets up the
  terminal part and says the Code tab is not released yet.
- There is no live savings bar in Claude Code or in the Code tab. Your savings are on
  the ContextShrink dashboard.
- The keep-alive id is one per computer, shared by its Claude Code sessions and by the
  Code tab.

## Development

Tests use `node:test`. They run the CLI against a fake API on 127.0.0.1, with
HOME, USERPROFILE and CLAUDE_CONFIG_DIR in a temp folder, and never read the real
`~/.claude` or call the real services.

```
npm test
```

Set `CONTEXTSHRINK_API_URL` and `CONTEXTSHRINK_PROXY_URL` to point the CLI at other
hosts. The Code tab tests use test-only overrides (`CONTEXTSHRINK_NPX_TEST=1` and
`CONTEXTSHRINK_NPX_*`); the CLI ignores them unless that variable is set. `e2e/run-live-test.ps1` (in the repository) runs a live test on Windows in an
isolated Claude Code folder.
