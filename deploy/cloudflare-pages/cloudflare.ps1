# Shared by deploy.ps1 and desktop\publish-installer.ps1 (dot-source it).
# The Cloudflare API token comes from a file in the user profile, never from
# the repo, the command line or chat; the account ID is looked up with it.

$WranglerVersion = "4.40.0"
$env:WRANGLER_SEND_METRICS = "false"

function Use-CloudflareToken {
    if (-not $env:CLOUDFLARE_API_TOKEN) {
        $file = Join-Path $env:USERPROFILE ".cloudflare-token"
        if (-not (Test-Path $file)) {
            throw "No Cloudflare token: save it to $file (see deploy\cloudflare-pages\README.md)."
        }
        $env:CLOUDFLARE_API_TOKEN = (Get-Content $file -Raw).Trim()
    }
    if (-not $env:CLOUDFLARE_ACCOUNT_ID) {
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        $r = Invoke-RestMethod -Uri "https://api.cloudflare.com/client/v4/accounts" `
            -Headers @{ Authorization = "Bearer $env:CLOUDFLARE_API_TOKEN" }
        $ids = @($r.result | ForEach-Object { $_.id })
        if ($ids.Count -ne 1) { throw "The token sees $($ids.Count) Cloudflare accounts; set CLOUDFLARE_ACCOUNT_ID." }
        $env:CLOUDFLARE_ACCOUNT_ID = $ids[0]
    }
}

function Invoke-Wrangler {
    # Windows PowerShell 5.1 turns native stderr lines into terminating errors
    # under "Stop"; judge wrangler by its exit code instead.
    # Copy $args first: splatting @args straight into npx.ps1 passes nothing.
    $wranglerArgs = @($args)
    $prev = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        & npx --yes "wrangler@$WranglerVersion" @wranglerArgs
        if ($LASTEXITCODE -ne 0) { throw "wrangler $($wranglerArgs[0]) $($wranglerArgs[1]) failed (exit $LASTEXITCODE)" }
    } finally {
        $ErrorActionPreference = $prev
    }
}
