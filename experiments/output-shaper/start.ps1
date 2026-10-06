<#
.SYNOPSIS
  Start the output-shaper experiment: a local proxy with the output shaper on
  for a random half of conversations, and the desktop app pointed at it.

.DESCRIPTION
  Same settings as the hosted proxy (cache mode, code-aware compression), plus
  HORIZON_OUTPUT_SHAPER=1 at the default level and HORIZON_OUTPUT_HOLDOUT=0.5:
  each whole conversation is randomly shaped (treatment) or not (control).
  Every request is logged with its token usage to requests.jsonl (no message
  content). Run again after a restart; analyze with analyze.py; stop.ps1 ends it.

  Usage during the experiment does not appear on the dashboard (the local
  proxy has no account checks).
#>
$ErrorActionPreference = "Stop"
$repo = Resolve-Path (Join-Path $PSScriptRoot "..\..")
$ws = Join-Path $env:LOCALAPPDATA "ContextShrinkExperiment"
$port = 18800
New-Item -ItemType Directory -Force $ws | Out-Null

if (-not (Get-NetTCPConnection -LocalAddress 127.0.0.1 -LocalPort $port -State Listen -ErrorAction SilentlyContinue)) {
    $env:PYTHONPATH = "$repo"
    $env:PYTHONIOENCODING = "utf-8"
    $env:HORIZON_WORKSPACE_DIR = $ws
    $env:HORIZON_CONFIG_DIR = Join-Path $ws "config"
    $env:HORIZON_DETECT_BACKEND = "rust"
    $env:HORIZON_OUTPUT_SHAPER = "1"
    $env:HORIZON_OUTPUT_HOLDOUT = "0.5"
    Start-Process -FilePath (Join-Path $repo ".venv\Scripts\python.exe") `
        -ArgumentList "-m", "horizon.cli", "proxy", "--host", "127.0.0.1", "--port", "$port", "--code-aware", `
            "--log-file", (Join-Path $ws "requests.jsonl") `
        -WorkingDirectory $ws -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $ws "proxy.out.log") -RedirectStandardError (Join-Path $ws "proxy.err.log")
    $ready = $false
    foreach ($i in 1..120) {
        try { if ((Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 "http://127.0.0.1:$port/readyz").StatusCode -eq 200) { $ready = $true; break } } catch {}
        Start-Sleep -Milliseconds 500
    }
    if (-not $ready) { throw "The experiment proxy did not start; see $ws\proxy.err.log" }
}
"Experiment proxy running on 127.0.0.1:$port"

# Restart the desktop app pointed at the experiment proxy.
Get-Process contextshrink-desktop -ErrorAction SilentlyContinue | Stop-Process -Force
Get-CimInstance Win32_Process -Filter "Name='horizon.exe'" |
    Where-Object { $_.CommandLine -like "*forward start*" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
$env:CONTEXTSHRINK_PROXY_URL = "http://127.0.0.1:$port"
Start-Process (Join-Path $env:LOCALAPPDATA "ContextShrink\contextshrink-desktop.exe")
"ContextShrink restarted on the experiment proxy. Use your tools as usual."
