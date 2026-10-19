---
name: savings
description: Show ContextShrink savings for this session, today, the last 7 days and all time, with the three labelled figures.
disable-model-invocation: true
---

Show ContextShrink savings. Run this PowerShell command and show its output
exactly as printed, as a table, without changing the figures:

```powershell
& "$env:LOCALAPPDATA\ContextShrink\bin\contextshrink-status.exe" --detail
```

If the command is not found, say that ContextShrink is not installed on this
machine and stop. Do not estimate or invent any figure.

Only if the user asks what the labels mean:

- "trimmed %" is the share of request tokens ContextShrink removed.
- "$ of tokens at list price" is the value of those removed tokens at the
  model's list input price. It is not money saved.
- "saved $ (verified)" is the saving the proxy verified, as recorded in its
  savings ledger. It reads "pending" until the proxy reports a verified saving.
