---
name: resume
description: Resume ContextShrink after a pause. Removes the pause file the forwarder reads.
disable-model-invocation: true
---

Resume ContextShrink. Run this PowerShell command, then report the result it prints:

```powershell
$flag = Join-Path $env:LOCALAPPDATA 'ContextShrink\claude-code\paused'
if (Test-Path -LiteralPath $flag) {
  Remove-Item -LiteralPath $flag
  Write-Output 'ContextShrink resumed'
} else {
  Write-Output 'ContextShrink was not paused'
}
```

Do not delete anything else.
