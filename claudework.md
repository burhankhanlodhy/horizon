# Claude work log

Sessions: 2026-10-01 to 2026-10-06. Review, deployment, domain/email setup, the Free
compression cap, Stripe billing, the Windows desktop launcher and the move to
per-Pi Cloudflare Tunnels for ContextShrink.
No credentials, tokens or passwords are recorded here.

## Current state

| Piece | Where | Status |
|---|---|---|
| Landing page | Pi 4, static | https://contextshrink.com |
| Customer dashboard + Advanced Analytics page | Pi 4, static | https://app.contextshrink.com |
| Control-plane API | Pi 5, loopback `127.0.0.1:8788` | https://api.contextshrink.com via the Pi 5 tunnel and gateway |
| Horizon proxy | Pi 5, loopback `127.0.0.1:8787` | https://proxy.contextshrink.com via the Pi 5 tunnel and gateway |
| Pi 5 gateway (Caddy) | Pi 5, loopback `:8790` proxy, `:8791` API | Allowlists routes, strips edge headers |
| Public access | Cloudflare Tunnels `pi4-web` (Pi 4) and `pi5-proxy` (Pi 5) | No port forwarding; home IP never exposed |
| DNS / SSL | Cloudflare (free plan) | HTTPS via Cloudflare certificates |
| Email | Cloudflare Email Routing | `support@contextshrink.com` forwards to the owner's inbox |
| Billing | Stripe sandbox (test mode) | Pro checkout, webhooks, savings fee, unpaid-fee handling |
| Desktop launcher | Windows installer and Linux .deb/.rpm/AppImage (Tauri), Downloads page on the dashboard | 0.5.0: 12 terminal tools and 4 editors through the proxy |

## Topology

```
contextshrink.com, app.contextshrink.com -> Cloudflare -> tunnel pi4-web (Pi 4)  -> Caddy: static files only
api.contextshrink.com                    -> Cloudflare -> tunnel pi5-proxy (Pi 5) -> gateway 127.0.0.1:8791 -> API 127.0.0.1:8788
proxy.contextshrink.com                  -> Cloudflare -> tunnel pi5-proxy (Pi 5) -> gateway 127.0.0.1:8790 -> proxy 127.0.0.1:8787
```

- The Pi 4 never connects to the Pi 5. The dashboard and the Advanced Analytics page
  call `api.contextshrink.com` from the browser; the API's CORS allows
  `https://app.contextshrink.com` (plus localhost dev origins).
- Every ContextShrink service on the Pi 5 listens on loopback only (proxy 8787, API
  8788, gateway 8790/8791, Postgres 5432). Verified unreachable from the LAN.
- Tunnels must target the gateway listeners, never the API or proxy directly: the
  gateway hides `/internal/*`, `/docs`, `/openapi.json`, `/stats`, admin and settings,
  and strips Cloudflare/forwarding headers (`CDN-Loop`, `CF-*`, `Via`,
  `X-Forwarded-*`) before provider calls.
- `app.contextshrink.com/proxy`, `/api` and `/account-api` are retired and answer 404
  with a message naming the new hostname.
- The owner's personal services on the Pi 5 (Hermes Agent dashboard, a Python web
  server, xrdp, rpcbind, SSH) stay LAN-reachable by choice; they are not part of
  ContextShrink and are not internet-exposed.

## Pricing model (as implemented)

| | Free | Pro | Team |
|---|---|---|---|
| Card | No | Yes (Stripe Checkout) | Coming soon |
| Compression | Until $20 saved per UTC month, then passthrough | Unlimited | — |
| Fee | $0 | $0 base + 5% of the whole month's savings when they exceed $20 | — |
| Advanced Analytics | No | Yes | — |

## What was done

### Infrastructure (2026-10-01)

1. **Code review** of the repo. Remaining findings are under "Deferred".
2. **Cloudflare DNS** and tunnel `pi4-web` for the landing page and dashboard.
3. **Email**: Cloudflare Email Routing `support@` -> owner's inbox; MX/SPF/DKIM verified.
   iCloud test mail is rejected by Cloudflare ("bare CR in DATA line"); other providers work.

### Free compression cap

- `/internal/proxy/authorize` returns `plan` and `compression_allowed`; Free (and any
  non-active paid plan) compresses until `FREE_SAVINGS_CAP_USD` ($20) is saved in the
  current UTC month, then gets passthrough on every provider path. Responses carry
  `X-ContextShrink-Compression: paused`; the dashboard shows a notice from 75%.

### Stripe billing (option A: app computes the fee, Stripe invoices it)

- `api/billing.py`: Checkout ($0/month Pro, card saved), Customer Portal, Renew,
  invoices, and a signature-verified, deduplicated webhook. Plan state is written only
  by webhooks, which re-read the subscription from Stripe.
- Savings fee: at each renewal the ended period's savings are summed from
  `metrics.proxy_events`; above $20 a separate 5% invoice is finalized.
  `billing.savings_fees` plus idempotency keys prevent double charges.
- Unpaid fee: 7-day grace (`FEE_GRACE_DAYS`) with a banner, then a background check
  cancels the subscription (tagged `cancel_reason=unpaid_savings_fee`, no extra final
  fee) and the account drops to Free; upgrading is blocked until the fee is paid.
- Fixes found in live testing: StripeObject is not a dict in stripe-python 16;
  the Customer Portal schedules cancellations with `cancel_at`.
- Webhook endpoint: `https://api.contextshrink.com/stripe/webhook`
  (`we_1ULtUHELD2bV0TgCnEZ9t8Hc`, 10 events). Sandbox product `prod_VMcjRMhAtLT65O`,
  price lookup key `contextshrink_pro_monthly`, portal config `bpc_1ULtUGELD2bV0TgChfFP043K`.
- Scenarios tested on test clocks: upgrade, cancel/renew, $20.00 (no fee) vs $20.01
  ($1.00), declined fee with grace, grace expiry downgrade, fee paid. Test data removed;
  `test1@contextshrink.com` kept as a clean Free account.

### Desktop launcher (`desktop/`)

- Tauri app, Windows first, per-user NSIS installer (~65 MB). Ships only ContextShrink's
  own pieces: the app plus a frozen Horizon client (PyInstaller, Python runtime and
  package data included). Wrapped tools (Claude Code, OpenCode) are never bundled; the
  app detects them on PATH and links to their install pages.
- Sign-in with email/password creates a per-device proxy key (`Desktop: <PC name>`),
  stored via `horizon vault set --stdin` in Windows Credential Manager. Sign-out revokes
  it; a key revoked from the dashboard is replaced on next start.
- A background forwarder (`127.0.0.1:18788`) adds the device key to model calls and
  relays them to `proxy.contextshrink.com`. Requests rerouted by the OpenCode transport
  plugin that are not model calls (sign-in, catalogues) go straight to their real
  destination without the key.
- Launch opens a console in the chosen project folder running `horizon wrap ... --no-proxy`
  and `horizon unwrap` on exit:
  - Claude Code: `--no-mcp --code-memory none`; unwrap with `--keep-mcp` so the user's
    own MCP registrations are never removed.
  - OpenCode: `--no-mcp --no-serena` plus the bundled transport plugin, so custom
    providers (e.g. OneProvider) are routed too.
- Fixes found while testing: Tauri's `\\?\` paths broke `cmd.exe`; PyInstaller omitted
  the plugin `.js`; a frozen client must call itself in hooks (`resolve_horizon_command`);
  Cloudflare/forwarding headers made Cloudflare-fronted providers (chatgpt.com) refuse
  requests with an HTML 403. The proxy now logs a redacted preview of upstream error
  bodies.

### Desktop app 0.2.x to 0.4.0 (2026-10-03 to 2026-10-06)

- 0.2.x: Codex (HTTP and WebSocket Responses relayed by the forwarder; no unwrap, which
  would strip the user's own Horizon MCP block), minimise to tray, tool logos.
- 0.3.0, terminal tools: Aider, Copilot CLI (BYOK Anthropic key only), Goose, Grok CLI,
  Kimi CLI, Mistral Vibe, Oh My Pi, OpenClaude, OpenHands. Every wrap was run against a
  sandboxed profile with stand-in binaries; only Oh My Pi writes a file (`models.yml`,
  restored byte-for-byte by unwrap).
  - All wrap binary lookups go through `_resolve_windows_launcher` (npm's extensionless
    sh shims fail with WinError 193 on Windows).
  - Grok, Kimi and Vibe talk to providers the proxy is not configured for, so each gets
    its own forwarder (`horizon forward start --upstream`, ports 18791/18789/18790)
    that tags model calls with `x-horizon-base-url` / `x-horizon-original-path`.
    Verified live: each provider answered a fake-key request with its own auth error.
  - Grok Build (the `grok-build` model) runs inside Grok CLI, so it is covered there.
- 0.4.0, Editors section:
  - Claude Code for VS Code: Connect/Disconnect via `horizon desktop connect|disconnect
    vscode-claude`, which edits `~/.claude/settings.json` key by key and restores it.
    The app disconnects on quit and sign-out and reconnects at the next sign-in. It
    never takes over a setup made by the user's own `horizon wrap vscode-claude`.
  - Cline, Continue, ZCode: configured in their own settings; the app shows the base URL
    to paste. Continue needs `http://127.0.0.1:18788/v1/` (it resolves paths against
    `apiBase`).
- Not supported, and why:
  - **Cursor** sends custom-base-URL requests from its own servers (api2.cursor.sh), so
    a loopback forwarder is unreachable. It would need the public proxy URL plus a way
    to carry the account key alongside the provider key in one Authorization header.
  - **Copilot in VS Code** (and Copilot CLI with a subscription sign-in) needs a local
    proxy holding the user's GitHub Copilot token.
  - **OpenClaw**: its plugin package (`horizon-openclaw`) is not published on npm and its
    source is not in this repo.

### Linux desktop app, 0.5.0 (2026-10-06)

- x86_64 `.deb`, `.rpm` and AppImage, built in an Ubuntu 22.04 container
  (`desktop/linux/build-in-container.sh`, Podman in WSL) so they run on Ubuntu 22.04,
  Debian 12, Fedora 38 and newer. `install-deps.sh` is shared with the planned GitHub
  Actions build.
- Platform differences in the app (`src-tauri/src/unix.rs`):
  - tools open in the user's terminal (`$CONTEXTSHRINK_TERMINAL`, `$TERMINAL`,
    `x-terminal-emulator`, GNOME Terminal, Konsole, ... xterm) via a `launch-<tool>.sh`;
  - PATH is read from the login shell at startup (nvm, `~/.local/bin`, `~/.npm-global`);
  - secrets in the Secret Service keyring; the app creates the default collection
    when a session has none (WSL, minimal desktops), which shows the keyring's prompt;
  - minimise does not hide to the tray (stock GNOME has no tray icons);
  - WebKit's DMA-BUF renderer is off (blank windows without a GPU), and under WSL the
    app uses X11 (WSLg's Wayland bridge mis-placed the window).
- Checked in WSL (Ubuntu 26.04): install, keyring, forwarder to the live proxy, VS Code
  Claude connect/restore, all 12 tool wraps in a sandbox, sign-in, and Claude Code
  through `ANTHROPIC_BASE_URL=http://127.0.0.1:18788`. Not yet checked: real GNOME/KDE
  desktops in VMs, Fedora, the AppImage.
- `latest.json` now has an `assets` list (os, arch, kind, file, size, sha256); the old
  top-level Windows fields stay. Downloads has Windows/Linux tabs.

### Per-Pi tunnels and LAN lockdown (2026-10-02)

- Tunnel `pi5-proxy` on the Pi 5 with hostnames `proxy` (-> 127.0.0.1:8790) and `api`
  (-> 127.0.0.1:8791). The proxy no longer depends on the Pi 4 or shares the dashboard
  origin.
- The Advanced Analytics page is shipped with the dashboard build
  (`dashboard/scripts/copy-analytics-page.mjs`) and served statically by the Pi 4.
- The Pi 4 Caddyfile serves static files only; the Pi 5 gateway LAN listener was
  removed and the API bound to loopback (`API_BIND=127.0.0.1`).

## Deploying

### Pi 5 (API, proxy, gateway)

```bash
cd ~/horizon && git pull --ff-only origin main
docker compose -f docker-compose.controlplane.yml up -d --build api
docker compose build horizon-proxy && docker compose up -d horizon-proxy
# The gateway Caddyfile is a single-file bind mount: recreate after a pull.
docker compose --profile dashboard up -d --force-recreate horizon-dashboard-gateway
```

### Pi 4 (landing, dashboard)

Build in **PowerShell**, not Git Bash (Git Bash rewrites `/api`-style values into
Windows paths). Check the bundle contains the expected URLs.

```powershell
cd dashboard
$env:VITE_LANDING_URL = "https://contextshrink.com"
$env:VITE_API_URL = "https://api.contextshrink.com"
npm run build   # also copies the Advanced Analytics page to dist\dashboard\
```

Upload `dist` to a temp directory on the Pi 4 and install with `sudo` (swap the
directory, keep the old one in `/var/backups/contextshrink/`). `sudo` on the Pi 4 needs
the owner's password, so the owner runs the final command.

### Desktop installer

```powershell
.\desktop\build-installer.ps1
wsl -d Ubuntu -u root -- bash desktop/linux/build-in-container.sh   # from the repo, in WSL paths
.\desktop\publish-installer.ps1   # stages all four; then: ssh -t raspberrypi4@192.168.0.64 /tmp/cs-publish-release.sh
```

Outputs: `desktop\app\src-tauri\target\release\bundle\nsis\ContextShrink_<version>_x64-setup.exe`
and `desktop\dist-linux\` (`.deb`, `.rpm`, `.AppImage`).
Cloudflare caches installers by file name, so every release needs a version bump.

## Pending / next steps

- **Before live payments**: live-mode Stripe keys, rerun `stripe_setup.py` in live mode,
  decide on **Stripe Tax**, and enable Stripe's failed-payment customer emails.
- Landing page: contact address plus Terms, Privacy and Refund pages for Stripe review.
- Delete `~/stripe_temp/keys.txt` on the Pi 5 (keys live in `.env`).
- Desktop app: code signing with Azure Artifact Signing (owner to set up the account
  and identity validation), auto-update, start at login (editors only work while
  the app runs), Cursor support (see above).
- Linux: test on GNOME/KDE VMs and Fedora; move the build to GitHub Actions before
  launch; ARM64; trim numpy/OpenBLAS (~50 MB) from the frozen client; a terminal-only
  `contextshrink` command for servers (key file or env var when there is no keyring).
- macOS after Linux (Keychain, Terminal.app, notarization with an Apple Developer account).
- **Team plan**: organisations, invitations, combined analytics, per-seat billing.
- Rotate the Pi password (it was shared in chat) and move both Pis to SSH key-only login.

## Deferred (from the original review)

Resolved: plain HTTP between the Pis (removed), API reachable on the LAN (loopback only),
proxy/dashboard shared origin (separate hostnames), period_source bug.

Still open:
1. argon2 hashing runs on the event loop (signup and login); no rate limiting on `/auth/*`.
2. The dashboard session token is in `localStorage`; consider a CSP and HttpOnly cookies.
3. Gateway route tests for `/admin/*`, `/stats`, `/settings` and encoded paths.
4. A database write on every authenticated request and proxy authorize call;
   per-event ownership query in ingest and unbounded integer fields; no purge of
   expired sessions; schema drift on the `seat_count` CHECK; hardcoded legacy partitions
   through Nov 2026; API container runs as root with no healthcheck.
