<#
.SYNOPSIS
  Start the three local proxies for the cache keep-alive live test.

.DESCRIPTION
  control   127.0.0.1:18851  no keep-alive, client's cache lane unchanged
  keepalive 127.0.0.1:18852  HORIZON_CACHE_KEEPALIVE=1
  ttl       127.0.0.1:18853  HORIZON_CACHE_TTL_UPGRADE=1h
  Each runs this repository's proxy with the hosted settings and logs token
  usage (no message content) to its own folder. stop.ps1 ends them.
#>
$ErrorActionPreference = "Stop"
$repo = Resolve-Path (Join-Path $PSScriptRoot "..\..")
$root = Join-Path $env:LOCALAPPDATA "ContextShrinkExperiment\cache-keeper"
$arms = @(
    @{ name = "control"; port = 18851; env = @{} },
    @{ name = "keepalive"; port = 18852; env = @{ HORIZON_CACHE_KEEPALIVE = "1" } },
    @{ name = "ttl"; port = 18853; env = @{ HORIZON_CACHE_TTL_UPGRADE = "1h" } }
)
foreach ($arm in $arms) {
    $ws = Join-Path $root $arm.name
    New-Item -ItemType Directory -Force $ws | Out-Null
    if (Get-NetTCPConnection -LocalAddress 127.0.0.1 -LocalPort $arm.port -State Listen -ErrorAction SilentlyContinue) {
        "$($arm.name) proxy already running on $($arm.port)"
        continue
    }
    $env:PYTHONPATH = "$repo"
    $env:PYTHONIOENCODING = "utf-8"
    $env:HORIZON_WORKSPACE_DIR = $ws
    $env:HORIZON_CONFIG_DIR = Join-Path $ws "config"
    $env:HORIZON_DETECT_BACKEND = "rust"
    foreach ($k in "HORIZON_CACHE_KEEPALIVE", "HORIZON_CACHE_TTL_UPGRADE") { Remove-Item "env:$k" -ErrorAction SilentlyContinue }
    foreach ($k in $arm.env.Keys) { Set-Item "env:$k" $arm.env[$k] }
    Start-Process -FilePath (Join-Path $repo ".venv\Scripts\python.exe") `
        -ArgumentList "-m", "horizon.cli", "proxy", "--host", "127.0.0.1", "--port", "$($arm.port)", "--code-aware", `
            "--log-file", (Join-Path $ws "requests.jsonl") `
        -WorkingDirectory $ws -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $ws "proxy.out.log") -RedirectStandardError (Join-Path $ws "proxy.err.log")
    $ready = $false
    foreach ($i in 1..120) {
        try { if ((Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 "http://127.0.0.1:$($arm.port)/readyz").StatusCode -eq 200) { $ready = $true; break } } catch {}
        Start-Sleep -Milliseconds 500
    }
    if (-not $ready) { throw "$($arm.name) proxy did not start; see $ws\proxy.err.log" }
    "$($arm.name) proxy running on 127.0.0.1:$($arm.port)"
}
foreach ($k in "HORIZON_CACHE_KEEPALIVE", "HORIZON_CACHE_TTL_UPGRADE") { Remove-Item "env:$k" -ErrorAction SilentlyContinue }
