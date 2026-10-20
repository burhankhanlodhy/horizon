# ContextShrink desktop launcher (Windows)

A Tauri app users download from the dashboard to run their own coding tools
through the hosted ContextShrink proxy. Supported tools: Claude Code, Codex and OpenCode.

## How it works

1. The user signs in with their ContextShrink email and password.
2. The app creates a proxy key for this computer (`POST /api/keys`, named
   `Desktop: <COMPUTERNAME>`) and hands it to the bundled Horizon client
   (`horizon vault set --stdin`), which keeps it in Windows Credential Manager.
   No key is embedded in the installer. Signing out revokes the device key; a
   key revoked from the dashboard is replaced automatically at next start.
3. The app runs the Horizon forwarder in the background on `127.0.0.1:18788`.
   It adds `X-Horizon-Proxy-Token` to model requests and relays them to
   `https://proxy.contextshrink.com` (the Pi 5's own Cloudflare Tunnel).
4. **Launch** opens a console in the chosen project folder running the tool's
   `horizon wrap` command against the forwarder, then `horizon unwrap` when the
   tool exits, restoring the tool's own config:
   - Claude Code: `wrap claude --no-proxy --no-mcp --code-memory none` (base URL
     in the project's `.claude/settings.local.json` for the session; unwrap uses
     `--keep-mcp` so the user's own MCP registrations are never removed).
   - Codex: `wrap codex --no-proxy --no-mcp --code-memory none` (base URL on the
     command line and in the environment; `~/.codex/config.toml` is never touched,
     and there is deliberately no unwrap: `unwrap codex` would strip the user's own
     marker-delimited Horizon MCP block). HTTP and WebSocket Responses are both
     relayed by the forwarder.
   - OpenCode: `wrap opencode --no-proxy --no-mcp --no-serena`, plus the bundled
     transport plugin, which reroutes every provider. The forwarder sends model
     calls to the proxy and everything else (sign-in, catalogues) direct.

The installer ships only ContextShrink's own pieces: the app and a frozen
Horizon client (Python runtime included). The tools it launches, such as
OpenCode, are never bundled; the app detects them on `PATH` and links to their
install page when missing.

## Layout

| Path | What |
|---|---|
| `client/horizon_client.py`, `client/build-client.ps1` | Frozen Horizon CLI (PyInstaller, one-folder) |
| `app/src-tauri/src/api.rs` | Control-plane API calls (login, device key, account) |
| `app/src-tauri/src/secrets.rs` | Session token and device-key ID in Credential Manager |
| `app/src-tauri/src/client.rs` | Bundled client: vault, forwarder, tool registry and launch |
| `app/src/App.tsx` | Sign-in and home screens |

Adding a tool: add an entry to `TOOLS` in `client.rs` with its `horizon wrap`
and `unwrap` arguments.

## Build

Requires the repo `.venv` (with PyInstaller), Rust, the MSVC build tools and
Node.js. From PowerShell (not Git Bash):

```powershell
.\desktop\build-installer.ps1
```

The GitHub Actions "Desktop app" workflow builds the same installer (job
`windows`, artifact `contextshrink-windows-x86_64`). When the Azure Artifact
Signing variables are set, the installer and helper exes are signed; see
[SIGNING.md](SIGNING.md) for the setup, local signing and verification.

Development run (uses the frozen client from `client\dist`):

```powershell
.\desktop\client\build-client.ps1
cd desktop\app; npm install; npx tauri dev
```

Set `CONTEXTSHRINK_APP_URL` / `CONTEXTSHRINK_PROXY_URL` to point the app at a
different dashboard origin or proxy.

## Publish to the Downloads page

```powershell
gh run download <run-id> -n contextshrink-windows-x86_64 -D desktop\dist-windows
.\desktop\publish-installer.ps1
```

The Windows installer is taken from `desktop\dist-windows` when the CI artifact
for this version is there, otherwise from the local build. Installers are never
hosted on GitHub; the artifact is only a hand-off to this script.

Uploads the installer and a `latest.json` (version, file, size, SHA-256, date)
to the Pi 4 and prints one `ssh -t ... /tmp/cs-publish-release.sh` command that
installs them into `/var/www/contextshrink/releases` (needs the Pi 4 sudo
password). The dashboard's **Downloads** page reads
`https://app.contextshrink.com/releases/latest.json`, so publishing a release
needs no dashboard rebuild. Earlier installers stay downloadable by file name.
