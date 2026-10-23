# Web tier on Cloudflare Pages + R2

Replaces the Pi 4 (`deploy/pi4-webtier`), which hangs every week or so.

| Host | Served by |
|---|---|
| `contextshrink.com` | Pages project `contextshrink-landing` (`landingpage/`) |
| `app.contextshrink.com` | Pages project `contextshrink-dashboard` (`dashboard/`) |
| `code.contextshrink.com` | Pages project `contextshrink-code` (`codesite/`, deploy with `-Site code`) |
| `app.contextshrink.com/releases/*` | Pages Function `dashboard/functions/releases/[[path]].js` reading the **private** R2 bucket `contextshrink-releases` |

The API and proxy stay on the Pi 5 tunnel; nothing here talks to them except
the browser. Installer URLs are unchanged (`/releases/<file>`), so old links
and the Downloads page keep working. The bucket has no public access: the
function serves single flat file names only.

Pages behaviour matches the old Caddy rules: unknown paths fall back to
`index.html` (no `404.html` in either build), `/privacy` serves
`privacy.html`, and `dashboard/public/_headers` keeps Advanced Analytics
uncached.

## Cloudflare token (one time)

1. Enable R2: dashboard → **R2 Object Storage** → subscribe to the free plan
   (asks for a payment method; 10 GB storage and all downloads are free).
2. **My Profile → API Tokens → Create Token → Custom token** with
   - Account → **Cloudflare Pages → Edit**
   - Account → **Workers R2 Storage → Edit**
   - Account resources: your account only.
3. Save it in your profile, not in the repo or chat:

   ```powershell
   Set-Content -NoNewline -Path "$env:USERPROFILE\.cloudflare-token" -Value "PASTE-TOKEN-HERE"
   ```

`cloudflare.ps1` reads that file (or `CLOUDFLARE_API_TOKEN`) and looks up the
account ID with it.

## First deploy (one time)

```powershell
. .\deploy\cloudflare-pages\cloudflare.ps1; Use-CloudflareToken
Invoke-Wrangler r2 bucket create contextshrink-releases
.\deploy\cloudflare-pages\deploy.ps1 -Create
```

Then copy the current installers and `latest.json` from the Pi 4 into the
bucket (`wrangler r2 object put contextshrink-releases/<file> --file <file>
--remote`, `latest.json` last) and check
`https://contextshrink-dashboard.pages.dev/releases/latest.json`.

## Later deploys

```powershell
.\deploy\cloudflare-pages\deploy.ps1                 # both sites
.\deploy\cloudflare-pages\deploy.ps1 -Site dashboard # one site
.\desktop\publish-installer.ps1                      # desktop release -> R2
```

No sudo and no Pi involved.

## Cutover from the Pi 4 (about a minute of downtime per host)

For `contextshrink.com`, then `app.contextshrink.com`:

1. **DNS** tab: delete the record for the host (a CNAME to
   `…cfargotunnel.com`, the Pi 4 tunnel).
2. **Workers & Pages** → the project → **Custom domains → Set up a custom
   domain** → enter the host. Cloudflare adds the new record itself.
3. Once both hosts load from Pages: **Zero Trust → Networks → Tunnels →
   pi4-web → Public hostnames**, delete the two routes. The Pi 4 can then be
   switched off.

Rollback: remove the custom domain and re-add the tunnel public hostname.

## Storage

Every release adds roughly 0.5 GB (Windows, macOS, Linux packages). Delete
old versions from the bucket before it passes the 10 GB free tier:
`wrangler r2 object delete contextshrink-releases/<file> --remote`.
