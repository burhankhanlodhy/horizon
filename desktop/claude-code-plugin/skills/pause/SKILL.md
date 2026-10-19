---
name: pause
description: Pause ContextShrink for new requests. Creates the pause file the forwarder reads.
disable-model-invocation: true
---

Pause ContextShrink. Run this PowerShell command, then report the path it prints:

```powershell
$dir = Join-Path $env:LOCALAPPDATA 'ContextShrink\claude-code'
New-Item -ItemType Directory -Force -Path $dir | Out-Null
$flag = Join-Path $dir 'paused'
if (-not (Test-Path -LiteralPath $flag)) { New-Item -ItemType File -Path $flag | Out-Null }
Write-Output "ContextShrink paused: $flag"
```

Tell the user that requests pass through unchanged until they run
/contextshrink:resume. Do not change any other file.
