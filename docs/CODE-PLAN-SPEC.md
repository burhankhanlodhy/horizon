# ContextShrink Code: subscription product spec (shared contract for every work package)

ContextShrink Code is a flat-fee product for people who use Claude Code or
Codex with a Claude or ChatGPT subscription. It sells extra usage from the plan
they already pay for, not dollar savings.

- Website and sign-up: `https://code.contextshrink.com`. Accounts are the same
  `core.users` accounts as app.contextshrink.com.
- Price: a 7-day free trial (no card), then $5.00 a month (Stripe, monthly
  only). Without an active trial or subscription, compression stops and
  requests pass through unchanged. Claude and Codex never stop working.
- Compression runs locally, in the "engine" on the user's computer. Requests go
  from the engine straight to the provider (api.anthropic.com, chatgpt.com,
  api.openai.com). ContextShrink servers never see prompts, responses, or the
  user's Claude/ChatGPT login. The only things sent to ContextShrink are:
  device registration, plan-note refreshes, and hourly usage totals (counts).
- Installs: `npx contextshrink-claude` and `npx contextshrink-codex`. Both use
  one shared device registration and one shared engine. Windows first.
- Device limit: 2 active devices per account. A third device is refused with an
  error that names the two connected devices and says how to disconnect one.
  Uninstalling (`npx contextshrink-claude uninstall` /
  `npx contextshrink-codex uninstall`) releases the device once no ContextShrink
  tool is left on it. The account page can also disconnect a lost device.

## 1. Device identity (must be byte-identical in Node and Python)

```
machine_id  = Windows: MachineGuid value of HKLM\SOFTWARE\Microsoft\Cryptography (64-bit registry view)
              Linux:   first non-empty of /etc/machine-id, /var/lib/dbus/machine-id
              macOS:   IOPlatformUUID from `ioreg -rd1 -c IOPlatformExpertDevice`
              Tests only: env CONTEXTSHRINK_MACHINE_ID overrides the above.
              Shipped engines (the bundled client, see horizon/bundled_client.py)
              ignore that override, so a user's environment cannot change the device.
normalized  = machine_id.strip().lower()
device_hash = lowercase hex sha256 of UTF-8 bytes "contextshrink-device-v1\n" + normalized
```

The device_hash is the only machine identifier sent to the server.

Test vector, which both implementations must assert: machine_id `"  ABC-123  "`
gives device_hash
`fb7d953bd09b6360c7f94e789471ddf1ce6973baefe7b6da392179260b08c323`.

## 2. Local files (all under `~/.contextshrink/`, user-only permissions)

`device.json` is written by npx and read by the engine:
```json
{
  "version": 1,
  "api_url": "https://api.contextshrink.com",
  "device_id": "<uuid>",
  "device_key": "cs_dev_<random>",
  "device_hash": "<hex>",
  "tools": ["claude", "codex"],
  "created_at": "<iso8601>"
}
```

- `license.json` is written by whoever last refreshed the note (npx at connect,
  then the engine hourly): `{"version":1,"token":"<plan note>","fetched_at":"<iso>"}`.
- `usage-pending.json` is used by the engine only: hourly buckets not yet uploaded.

The engine's home dir is `~/.contextshrink/` unless env `CONTEXTSHRINK_HOME` is
set (tests use that).

Engine install dir (Windows): `%LOCALAPPDATA%\ContextShrink\engine\<engine version>\`.
It contains `horizon.exe` (signed stub) plus the embedded Python client. The npx
packages pin the engine in `lib/engine-manifest.json`:
`{"version","url","sha256","size"}`.

## 3. Plan note (entitlement token)

Format: `base64url(payload_json_utf8) + "." + base64url(ed25519_signature(payload_bytes))`.
Base64url has no padding. The signature covers the exact payload bytes before
encoding.

```json
{"v":1,"iss":"contextshrink-code","sub":"<user uuid>","dev":"<device uuid>",
 "dh":"<device_hash>","plan":"trial"|"active","iat":<unix>,"exp":<unix>,"until":<unix>}
```

- `until` is when the entitlement ends: the trial end, or `current_period_end` + 3 days of grace. `until`, `iat` and `exp` are integer Unix seconds.
- `exp` = min(iat + 72h, until). The engine refreshes every hour, so 72h is the offline grace.
- Signing key: the API reads env `CODE_ENTITLEMENT_PRIVATE_KEY`, a standard base64
  32-byte raw Ed25519 seed. Never commit any private key. When it is unset,
  `/code/entitlement` returns 503.
- Verify key, compiled into the engine with **no env override**:
  `CODE_ENTITLEMENT_PUBLIC_KEY_B64 = "jys7sL/fRV7Z+9xWRUZygtzpFGV9os/ns4QwrFzqJD0="`.
  Tests generate their own keypair and monkeypatch the module constant.
- The engine treats a note as valid only when all of these hold:
  - the signature verifies;
  - `v == 1` and `iss` matches;
  - `dh` equals the locally computed device_hash;
  - `iat - 300 <= now < exp`;
  - `plan` is `trial` or `active`.

## 4. API (FastAPI, new module `api/code_plan.py`, installed from `api/main.py`)

Every route below must also be allowed in `deploy/pi5-webtier/Caddyfile`
(`api_routes`). New modules must be added to the COPY line in `api/Dockerfile`
(`api/tests/test_dockerfile.py` checks this). Add `cryptography` (pinned) to
`api/requirements.txt`.

Auth kinds:
- **session**: `Authorization: Bearer <session token>`, via the existing `current_user`.
- **device**: `Authorization: Bearer cs_dev_...`. Only the sha256 hash is stored
  (use `security.hash_token`). The device must not be revoked.

| Method/path | Auth | Purpose |
|---|---|---|
| POST `/code/cli/start` | none (rate-limited per client IP: 10/min, in memory) | body `{tool:"claude"\|"codex", device_hash, device_name (<=60), platform:"windows"\|"macos"\|"linux"}`. Returns `{request_id, poll_secret, user_code:"ABCD-EFGH", verify_url:"<CODE_SITE_URL>/connect?code=ABCD-EFGH", interval:3, expires_in:600}`. Store only hashes of `poll_secret` and `user_code`. user_code alphabet has no ambiguous chars (no 0/O/1/I). |
| GET `/code/cli/request?code=` | session | Shows the browser what it is approving: `{device_name, platform, tool, device_limit:{reached:bool, devices:[{id,name,platform,last_seen_at}]}}`. 404 when the code is unknown or expired. |
| POST `/code/cli/approve` | session | body `{user_code}`. Registers or reuses the device for this user, enforcing the limit (see below). On limit, 409 `{"detail":{"code":"device_limit","devices":[{id,name,platform,last_seen_at}]}}`. Starts the trial on the first device ever (see Trial). |
| POST `/code/cli/poll` | none | body `{request_id, poll_secret}`. Returns `{status:"pending"}`; or `{status:"device_limit", devices:[{id,name,platform,last_seen_at}]}` while the browser's approval was refused by the device limit (the account's current active devices; the request stays open until a later approval succeeds or it expires); or `{status:"approved", device_id, device_key, api_url}` (once only; the device key is minted at this poll and only its hash is stored); or `{status:"expired"}` (also once the request expires, even while it was at the device limit). |
| GET `/code/account` | session | `{plan:{state:"trial"\|"active"\|"past_due"\|"canceled"\|"none", trial_ends_at, current_period_end, cancel_at_period_end, entitled:bool}, devices:[{id,name,platform,tools,created_at,last_seen_at}], device_limit:2}` (active devices only) |
| DELETE `/code/devices/{id}` | session | Revokes one of the user's own devices (204, 404 if not theirs). |
| DELETE `/code/device` | device | Uninstall: revokes the calling device (204). Idempotent. |
| POST `/code/entitlement` | device | body `{device_hash}`. Must match the device's stored hash, else 403. Updates `last_seen_at`. Entitled: `{token, plan, until}`. Not entitled: 402 `{"detail":{"code":"plan_required","trial_ended_at":...}}` |
| POST `/code/usage` | device | body `{buckets:[{hour:"<iso hour, UTC>", tool:"claude"\|"codex", requests, tokens_before, tokens_after}]}` (<=500 buckets, non-negative ints, hour within the last 35 days and not in the future). Upsert **replaces** values per (device, hour, tool), so it is idempotent. |
| GET `/code/usage/summary` | device or session | `{today:{requests,tokens_before,tokens_after,tokens_saved,pct}, last_7d:{...}, last_30d:{...}}` per account. tokens_saved = before-after (floored at 0); `pct` = tokens_saved/tokens_before*100 (0 when there is no data). |
| POST `/code/checkout` | session | Stripe Checkout (mode subscription, price lookup key `contextshrink_code_monthly`, customer shared with `core.subscriptions.stripe_customer_id` via billing's `_ensure_customer`, `subscription_data.metadata={"product":"code","user_id":...}`). success_url `CODE_SITE_URL/account?checkout=success`, cancel_url `CODE_SITE_URL/account?checkout=cancelled`. 409 if already entitled by a subscription. |
| POST `/code/portal` | session | Stripe billing portal, return_url `CODE_SITE_URL/account`. |

`CODE_SITE_URL` env defaults to `https://code.contextshrink.com`.

**Device limit:**
- Active means `revoked_at IS NULL`.
- On approve, if the user already has an active device with the same device_hash,
  reuse it: rotate its key, add the tool, and don't count it again.
- Otherwise, if the user has 2 or more active devices, return 409 device_limit.
- A device revoked by uninstall or the account page frees the slot immediately.

**Trial:**
- Lasts 7 days from the first approval on an account.
- Only one trial per account, and one per device_hash across all accounts. If a
  device_hash already started a trial for another account, the new account gets
  no trial (state "none").

**Entitled** means one of these holds:
- `now < trial_ends_at`;
- the Stripe status is `active` or `trialing`;
- the Stripe status is `past_due` and `now < current_period_end + 3 days`.

**Stripe webhooks:**
- Route them in `billing.handle_event`. A subscription whose `metadata.product == "code"`
  (or whose price has the code lookup key) is synced into `core.code_plans` and
  must never reach the savings-fee code: no `charge_savings_fee`, and no change
  to `core.subscriptions.plan`.
- Invoices for a code subscription are not savings-fee invoices. In `/billing/invoices`, an invoice with no description is labelled "ContextShrink Code subscription" when its `parent.subscription_details.metadata.product` is `code`, and "ContextShrink Pro subscription" otherwise.
- Keep the change to `billing.py` minimal (a few lines that delegate to
  `code_plan`). Another branch edits billing.py too.

**Schema** (append to `api/schema.sql`, idempotent `IF NOT EXISTS`):
- `core.code_plans` (user_id PK, trial_started_at, trial_ends_at, stripe_subscription_id UNIQUE, status, current_period_end, cancel_at_period_end, updated_at)
- `core.code_devices` (id, user_id, name, platform, device_hash, key_hash UNIQUE, tools TEXT[], created_at, last_seen_at, revoked_at). Partial unique on (user_id, device_hash) WHERE revoked_at IS NULL.
- `core.code_trial_devices` (device_hash PK, user_id, started_at)
- `core.code_cli_requests` (id, user_code_hash UNIQUE, poll_secret_hash, tool, device_hash, device_name, platform, created_at, expires_at, approved_user_id, device_id, consumed_at, limit_user_id). `limit_user_id` (added with `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`) is set when an approval was refused by the device limit; poll then reports `device_limit` for that account's devices. No device key is stored on the request: the key is minted at poll time, and only its hash is kept on the device row.
- `metrics.code_usage_hourly` (user_id, device_id, hour, tool, requests, tokens_before, tokens_after, PK(device_id,hour,tool))

**Stripe setup:** `api/stripe_setup.py` also creates product "ContextShrink Code"
with a monthly price of 500 USD cents, lookup key `contextshrink_code_monthly`.

**Savings-fee fairness:** the existing Pro 5% fee must exclude subscription-login
traffic. The hosted proxy adds `"auth_mode": "payg"|"oauth"|"subscription"` to
each proxy event's `data`. `billing.ledger_savings` sums only rows where
`COALESCE(data->>'auth_mode','payg') = 'payg'`, using a new
`FEE_ELIGIBLE_SAVINGS_USD` constant in `api/savings_ledger.py`. Analytics totals
are unchanged.

## 5. Engine (Python, new package `horizon/code_engine/`)

- `horizon code-engine run [--port 18793]`:
  - runs the normal local proxy on `127.0.0.1:18793` with the `max` savings
    profile and the license gate on. The engine forces `HORIZON_SAVINGS=max` and
    `HORIZON_MODEL_MODERNIZE=0` (set, not defaulted), so the model the user picked is
    never replaced by a successor model, even if their environment sets
    `HORIZON_MODEL_MODERNIZE=1`;
  - is a single instance (a lock file, so a second run exits 0 and prints "already running");
  - writes a PID file.
- `horizon code-engine ensure`: starts `run` detached (no window) if
  `/__cs/health` doesn't answer within 300 ms, then waits up to 5 s. Used by
  Claude Code's SessionStart hook and the Startup shortcut. Always exits 0
  quickly, so a hook never blocks Claude.
- `horizon code-engine stop` and `horizon code-engine status` (JSON with `--json`).
- **License gate** (on in `code-engine run`):
  - At start, and then hourly, POST `/code/entitlement` with the device key from
    `device.json`, then write `license.json`.
  - Compression is applied only while the cached note is valid (section 3).
    Otherwise every request is forwarded **unchanged** (pass-through), and
    nothing errors because of the license.
  - With no `device.json`, run pass-through only.
- `GET /__cs/health` returns 200 `{"ok": <licensed>, "engine": "<version>", "plan": "trial"|"active"|null}`.
  The Code-tab launcher only routes through the engine when `ok` is true, so an
  unlicensed engine leaves the Code tab on plain Claude.
- **Usage:**
  - Each real model generation adds to an in-memory hourly bucket: requests,
    tokens_before (input tokens before compression), tokens_after (after
    compression), and tool. `claude` is `POST /v1/messages`. `codex` is a
    Responses turn from the Codex client on `/v1/responses`,
    `/v1/codex/responses`, `/backend-api/responses`, `/backend-api/codex/responses`
    or `/responses` (HTTP or WebSocket).
  - Not counted: auxiliary routes (`/v1/messages/count_tokens`, batches, model
    listing, `/v1/responses/*` subpaths such as compaction, `/v1/compress`), response-cache
    hits, and any turn whose token counts are unknown. A zero-token request is never
    recorded just to count it.
  - Buckets are flushed to `/code/usage` every 15 min and on stop, and persisted
    to `usage-pending.json` until the upload succeeds.
  - Only these counts are sent: no model names, no paths, nothing else.
- **Privacy test:** a test runs a request through the licensed engine with a
  fake upstream and a fake API. It asserts that the only requests to the API
  host are `/code/entitlement` and `/code/usage`, and that no request body
  contains any substring of the prompt.
- The engine never sends anything to the hosted proxy (proxy.contextshrink.com)
  and never forwards the provider credential anywhere except the provider.

## 6. npx (`desktop/npx` = contextshrink-claude, `desktop/npx-codex` = contextshrink-codex)

**`connect` (default command):**
1. On non-Windows, print "ContextShrink Code runs on Windows today. macOS and
   Linux are coming." and exit 1.
2. Read `device.json`. If there is none, or the server answers 401 for it, do the
   browser login:
   - `/code/cli/start`, then print the URL and code;
   - open the browser (`start ""`);
   - poll every `interval` seconds until approved, denied or expired.
   - On `device_limit`, print once: `This account already has 2 devices connected: <A>, <B>.
     Disconnect one first: run "npx contextshrink-claude uninstall" on that
     device, or remove it at https://code.contextshrink.com/account. Then run this
     command again.` and then the line `Or disconnect one in the browser window and
     approve again; this command is still waiting.` Keep polling. If the request is
     then approved, continue normally. If it expires while the last poll said
     `device_limit`, exit 3 (the message says the sign-in expired at the limit).
3. Write or update `device.json` and add this tool to `tools`.
4. Install the engine if the pinned version is missing:
   - download to a temp file;
   - check the size and sha256 against the manifest;
   - check that `horizon.exe` has a Valid Authenticode signature
     (`Get-AuthenticodeSignature`) from the ContextShrink publisher: the signer
     subject and the certificate authority are pinned to the same values the
     Code tab launcher uses (`lib/code-publisher.js`, shared by both packages);
   - extract with PowerShell `Expand-Archive` into the versioned dir, then swap
     into place atomically.
5. Fetch the plan note (`/code/entitlement`), save `license.json`, and print the
   plan state: "Free trial: N days left" / "Subscribed" / "Your trial has ended.
   Subscribe for $5/month at https://code.contextshrink.com/account. Until then,
   Claude works normally without compression."
6. Start the engine (`horizon.exe code-engine ensure`) and create the Startup
   shortcut `ContextShrink Engine.lnk` (runs `code-engine run` hidden).
7. Point the tool at the engine:
   - **Claude:**
     - `~/.claude/settings.json` env `ANTHROPIC_BASE_URL=http://127.0.0.1:18793`,
       plus `ENABLE_TOOL_SEARCH=true` as today. Remove any `ANTHROPIC_CUSTOM_HEADERS`
       that the old npx wrote.
     - Add a `SessionStart` hook running `"<engine>\horizon.exe" code-engine ensure`.
     - Code tab: `codetab.json` in the launcher's **forwarder** format (no `mode`):
       `{"v":1,"owner":"npx","enabled":true,"base_url":"http://127.0.0.1:18793","health_port":18793,"env":{"ENABLE_TOOL_SEARCH":"true"}}`.
       The launcher routes only when `v` is 1 and `enabled` is true, and it checks
       `GET /__cs/health` on `health_port`, which must answer one plain HTTP/1.1
       response with a `Content-Length` body.
       The rest of codetab install stays as today.
       If `codetab.json` belongs to the desktop app and is switched off (`enabled` is
       not `true`), connect takes it over and records its exact bytes in
       `claude-code\npx-takeover.json`. If the desktop app's file routes, it is left
       alone. Uninstall hands the file back only while it is still npx's text.
   - **Codex:** the ContextShrink model provider's `base_url` is
     `http://127.0.0.1:18793/v1`, with no `X-Horizon-Proxy-Token` header.
     ChatGPT auth is preserved as today.
   - Keep the existing state/restore machinery so uninstall restores exactly.

**`uninstall` (new; `disconnect` and `logout` become aliases):**
- Restore this tool's settings, as disconnect does today.
- Remove the tool from `device.json`.
- If no tools are left:
  - DELETE `/code/device` (ignore network errors, but say so);
  - `code-engine stop`;
  - remove the Startup shortcut;
  - delete the engine dir, `device.json`, `license.json` and `usage-pending.json`;
  - print "This device is disconnected. You can connect another device now."

**`status`:** device connected (name), plan state and trial days left, engine
running, and today's usage saved (`/code/usage/summary`).

**Status line / savings bar:**
- Shows `ContextShrink · usage saved 23% today · trial 5d left`, from
  `/code/usage/summary` with the device key.
- No dollar amounts for subscription users.

`login`, `--key` and email/password prompts are removed from both packages.

Both packages require Node 20.10 or later (`engines.node` is `>=20.10`; the tests use `--test-concurrency`).

## 7. Website (`codesite/`, Vite + React + TS + Tailwind 4, like `dashboard/`)

Routes:
- **`/`**: the landing page.
  - Headline: "Get more usage from the Claude and ChatGPT plans you already pay for."
  - Subhead: ContextShrink trims what Claude Code and Codex resend every turn,
    so each 5-hour window goes further.
  - Privacy section: "Your prompts never leave your computer. Compression runs
    on your machine; requests go straight to Anthropic or OpenAI. We never see
    or store your prompts, code or Claude/ChatGPT login. We only receive counts."
  - Install commands, and pricing ("7 days free, then $5/month. Cancel anytime.
    Up to 2 devices.").
  - FAQ.
  - No numeric multiplier claim (no "2X") until measured; keep a single constant
    `USAGE_CLAIM` that is currently null.
  - Do not use "Claude" or "Anthropic" in the product name or logo. Plain-text
    "works with Claude Code and Codex" is fine.
- **`/signup`, `/login`**: the same API as the dashboard (`/auth/signup` with
  Turnstile, `/auth/login`). The session token is in localStorage
  (`cs_session_token`, same as the dashboard). `?next=` is supported.
- **`/connect?code=`**:
  - Requires login (redirect with next).
  - Shows the device and tool from `/code/cli/request`, with an "Approve this
    computer" button.
  - On device_limit, it lists the devices with Disconnect buttons, then allows
    approving.
- **`/account`**:
  - Plan state with trial days left.
  - A Subscribe button ($5/month → `/code/checkout`) or a Manage billing button (`/code/portal`).
  - A device list with Disconnect.
  - Usage saved (from `/code/usage/summary`, session auth).
- **`/privacy`**: a short plain-language page with the same facts.

Deployment:
- Add `code` to `deploy/cloudflare-pages/deploy.ps1` (project
  `contextshrink-code`, dir `codesite`).
- The API needs `https://code.contextshrink.com` in `CORS_ORIGINS` (Pi `.env`,
  operator step).

## 8. Engine packaging (CI + R2)

- `.github/workflows/desktop.yml` (windows job):
  - after the signed client build, zip the embedded client dir as
    `contextshrink-engine-windows-x86_64.zip`, with `horizon.exe` at the zip root;
  - upload it as artifact `contextshrink-engine-windows`, with a
    `engine-manifest.json` (`version` = desktop version, sha256, size, url
    `https://app.contextshrink.com/releases/engine/<ver>/contextshrink-engine-windows-x86_64.zip`).
- `desktop/publish-installer.ps1 -Engine <dir>` uploads both files to R2 at
  `engine/<ver>/`.
- `dashboard/functions/releases/[[path]].js` gets an `ENGINE` path regex like `TOOLS`.
