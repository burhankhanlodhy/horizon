---
name: report
description: Collect ContextShrink diagnostics into a file for support. Contains status counters and forwarder health only, never prompt content.
disable-model-invocation: true
---

Collect ContextShrink diagnostics. Run this PowerShell command and give the user
the path it prints. Do not read the file's contents into this conversation.

```powershell
$dir = Join-Path $env:LOCALAPPDATA 'ContextShrink\claude-code'
$port = 18788
$codetab = Join-Path $dir 'codetab.json'
if (Test-Path -LiteralPath $codetab) {
  try { $port = [int]((Get-Content -Raw -LiteralPath $codetab | ConvertFrom-Json).health_port) } catch {}
}
$report = [ordered]@{
  generated_at = (Get-Date).ToUniversalTime().ToString('o')
  paused_flag = (Test-Path -LiteralPath (Join-Path $dir 'paused'))
  health_port = $port
  status_json = $null
  status_txt = $null
  forwarder_health = $null
  errors = @()
}
try { $report.status_json = Get-Content -Raw -LiteralPath (Join-Path $dir 'status.json') | ConvertFrom-Json } catch { $report.errors += "status.json: $($_.Exception.Message)" }
try { $report.status_txt = (Get-Content -LiteralPath (Join-Path $dir 'status.txt') -TotalCount 1) } catch { $report.errors += "status.txt: $($_.Exception.Message)" }
try { $report.forwarder_health = Invoke-RestMethod -Uri "http://127.0.0.1:$port/__cs/health" -TimeoutSec 2 } catch { $report.errors += "health: $($_.Exception.Message)" }
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$out = Join-Path $dir "diagnostics-$stamp.json"
$report | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $out -Encoding utf8
Write-Output $out
```

Tell the user the file holds status counters and forwarder health only, with no
prompt or response content, and that they can attach it to a support request.
