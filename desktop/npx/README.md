# contextshrink (npx installer, prototype)

Connects Claude Code in a terminal to the ContextShrink hosted proxy. It is one
plain JavaScript file run by Node, with no dependencies, no unsigned binary and
no background process. It writes two keys into Claude Code's settings file and
nothing else.

This package is a prototype. It is marked `private`, so it is not published to npm.

## Use

Requires Node 18 or newer and a ContextShrink account.

```
npx contextshrink            # sign in, then connect
contextshrink status         # connected or not, host and keep-alive id prefix
contextshrink disconnect     # restore the settings it changed
contextshrink logout         # disconnect, then revoke this computer's account key
```

Other commands:

- `contextshrink login` makes a new account key for this computer and connects it.
  It also revokes the key that connect made before.
- `contextshrink login --key` reads an account key from stdin instead of signing in.
  A key pasted this way is not revoked by logout.
- `--force` replaces another gateway or an existing header. Disconnect restores
  the old value.

Sign-in asks for an email and a password. The password is not echoed. When stdin
is a pipe, the answers are read in order, one per line.

## What it changes

Claude Code reads its user settings from `$CLAUDE_CONFIG_DIR/settings.json`, or
`~/.claude/settings.json` when that variable is not set. Connect adds two keys under `env`:

| Key | Value |
| --- | --- |
| `ANTHROPIC_BASE_URL` | `https://proxy.contextshrink.com/k/<keep-alive id>` |
| `ANTHROPIC_CUSTOM_HEADERS` | `X-Horizon-Proxy-Token: <account key>` |

The proxy reads the key from that header (`horizon/proxy/server.py`, `read_proxy_token`).
The `/k/<id>` segment is the keep-alive route the desktop app also uses. The
gateway allow-list (`deploy/pi5-webtier/Caddyfile`) accepts it.

Connect also writes two more files:

- `.contextshrink-npx.json` next to `settings.json`. It records the previous
  value of each key and the values connect wrote. Disconnect uses it to restore.
- `~/.contextshrink/npx.json`, with mode 0600 where the OS allows it. It holds
  the keep-alive id and the device key id. It never holds the key itself.

Everything else in `settings.json` is kept, including key order and indentation.

## How to undo it

Run `contextshrink disconnect`. It restores each key only if its value is still
the one this tool wrote. A key you edited is left alone, and the output says so.
If the settings file did not exist before connect, disconnect removes it again.

To revoke the account key too, run `contextshrink logout`. It needs a sign-in,
because revoking a key requires the account session. You can also revoke it on the
API Keys page of the dashboard.

## Trade-offs

The account key is stored in plain text in `settings.json`, and a copy is in
`.contextshrink-npx.json` next to it. Anyone who can read those files can use the
key. Revoke it on the API Keys page if either file leaks.

Connect refuses to run, and changes nothing, when:

- the ContextShrink desktop app already manages the connection
  (`.horizon-vscode-claude.json` exists next to `settings.json`);
- `ANTHROPIC_BASE_URL` points at another host;
- `ANTHROPIC_CUSTOM_HEADERS` is already set;
- `settings.json` is not valid JSON. The file is never overwritten.

`--force` overrides the second and third cases.

## Limitations

- Works for Claude Code in a terminal and in VS Code. The Claude Desktop Code tab
  still needs the desktop app.
- There is no live savings bar. The desktop app's verified-savings figure comes
  from its forwarder. This installer sends requests directly, so that figure is
  not produced for them.
- The keep-alive id is one per computer. The desktop forwarder splits keep-alive
  groups per session. Whether that difference matters is not tested.
- Sign-in uses the same endpoints as the desktop app. Email verification may be
  required before a key can be made.

## Development

Tests use `node:test`. They run the CLI in a child process against a fake API on
127.0.0.1, with HOME, USERPROFILE and CLAUDE_CONFIG_DIR pointing at a temp folder.
They never read the real `~/.claude` or call the real services.

```
node --test "desktop/npx/test/*.test.js"    # from the repository root
npm test                                    # from desktop/npx
```

Set `CONTEXTSHRINK_API_URL` and `CONTEXTSHRINK_PROXY_URL` to point the CLI at other hosts.
