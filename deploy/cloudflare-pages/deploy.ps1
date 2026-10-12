<#
.SYNOPSIS
  Build and deploy the landing page and/or dashboard to Cloudflare Pages.

.DESCRIPTION
  Builds each site with the production URLs and Turnstile site key baked in,
  then uploads dist\ (and, for the dashboard, functions\ with the R2-backed
  /releases/* route) with wrangler. -Create makes the Pages projects first
  (one time). Run from PowerShell, not Git Bash: Git Bash rewrites URL-like
  values. See README.md in this folder.
#>
param(
    [ValidateSet("landing", "dashboard")]
    [string[]]$Site = @("landing", "dashboard"),
    [switch]$Create
)
$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "cloudflare.ps1")
Use-CloudflareToken

$root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$env:VITE_LANDING_URL = "https://contextshrink.com"
$env:VITE_DASHBOARD_URL = "https://app.contextshrink.com"
$env:VITE_API_URL = "https://api.contextshrink.com"
$env:VITE_PROXY_URL = "https://proxy.contextshrink.com"
# Public by design (it is in every page); the secret half lives on the Pi 5.
$env:VITE_TURNSTILE_SITE_KEY = "0x4AAAAAAFUGeYKmz2vHEYhU"

$projects = @{ landing = "contextshrink-landing"; dashboard = "contextshrink-dashboard" }
$dirs = @{ landing = "landingpage"; dashboard = "dashboard" }

foreach ($s in $Site) {
    $dir = Join-Path $root $dirs[$s]
    Push-Location $dir
    try {
        $ErrorActionPreference = "Continue"
        if (-not (Test-Path "node_modules")) {
            npm ci
            if ($LASTEXITCODE -ne 0) { throw "npm ci failed in $dir" }
        }
        npm run build
        if ($LASTEXITCODE -ne 0) { throw "Build failed in $dir" }
        $ErrorActionPreference = "Stop"

        if ($Create) {
            Invoke-Wrangler pages project create $projects[$s] --production-branch main
        }
        # Run from the site folder so wrangler finds wrangler.toml and functions\.
        Invoke-Wrangler pages deploy --project-name $projects[$s] --branch main --commit-dirty=true
    } finally {
        Pop-Location
    }
}
