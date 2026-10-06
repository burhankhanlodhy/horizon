<#
.SYNOPSIS
  End the output-shaper experiment: stop the local proxy and restart the
  desktop app on the hosted proxy. The request log is kept for analyze.py.
#>
$ErrorActionPreference = "Continue"
Get-Process contextshrink-desktop -ErrorAction SilentlyContinue | Stop-Process -Force
Get-CimInstance Win32_Process |
    Where-Object { ($_.Name -eq "horizon.exe" -and $_.CommandLine -like "*forward start*") -or
                   ($_.Name -eq "python.exe" -and $_.CommandLine -like "*horizon.cli proxy*--port*18800*") } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
Remove-Item Env:CONTEXTSHRINK_PROXY_URL -ErrorAction SilentlyContinue
Start-Process (Join-Path $env:LOCALAPPDATA "ContextShrink\contextshrink-desktop.exe")
"Experiment stopped; ContextShrink is back on the hosted proxy."
"Results: python experiments\output-shaper\analyze.py"
