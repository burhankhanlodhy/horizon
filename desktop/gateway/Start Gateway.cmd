@echo off
setlocal
title ContextShrink gateway (pass-through)
rem Runs the horizon proxy on 127.0.0.1:8787 as a pure pass-through gateway with
rem exact per-request metering. Nothing is compressed or rewritten. Keep this
rem window open while you test; close it to stop the gateway.

set "REPO=%~dp0..\.."
for %%I in ("%REPO%") do set "REPO=%%~fI"
set "PY=%CONTEXTSHRINK_PY%"
if not defined PY if exist "%REPO%\.venv\Scripts\python.exe" set "PY=%REPO%\.venv\Scripts\python.exe"
rem A Claude Code worktree (<checkout>\.claude\worktrees\<name>) uses the checkout's venv.
if not defined PY if exist "%REPO%\..\..\..\.venv\Scripts\python.exe" set "PY=%REPO%\..\..\..\.venv\Scripts\python.exe"
if not defined PY (
  echo Python was not found. Set CONTEXTSHRINK_PY to your python.exe and try again.
  goto end
)
if not exist "%REPO%\horizon\_core.pyd" (
  echo horizon\_core.pyd is missing. Copy it from a built checkout into "%REPO%\horizon".
  goto end
)

set "PYTHONPATH=%REPO%"
if not defined HORIZON_WORKSPACE_DIR set "HORIZON_WORKSPACE_DIR=%USERPROFILE%\ContextShrinkGateway"
if not exist "%HORIZON_WORKSPACE_DIR%\logs" mkdir "%HORIZON_WORKSPACE_DIR%\logs"

rem Pass-through: every transform off. --passthrough enforces this in code; the
rem variables below keep older builds without that flag in pass-through too.
set "HORIZON_PASSTHROUGH=1"
set "HORIZON_TOOL_SEARCH=0"
set "HORIZON_CONTEXT_GUARD=0"
set "HORIZON_CACHE_CONTROL_TTL_GUARD=0"
set "HORIZON_STABLE_BOUNDARY_BREAKPOINT=0"
set "HORIZON_BETA_HEADER_STICKY=disabled"
set "HORIZON_OUTPUT_SHAPER=0"
set "HORIZON_DISABLE_FEATURES=proxy_output_shaper,read_maturation,tool_result_interceptors"
set "HORIZON_SAVINGS=off"
set "ANTHROPIC_TARGET_API_URL="
set "HORIZON_PROXY_TOKEN="
set "CONTEXTSHRINK_API_URL="
set "CONTEXTSHRINK_SERVICE_TOKEN="

echo Gateway: http://127.0.0.1:8787   usage log: %HORIZON_WORKSPACE_DIR%\gateway_usage.jsonl
"%PY%" -c "from horizon.cli import main; main()" proxy --host 127.0.0.1 --port 8787 --mode cache --passthrough --no-optimize --no-ccr --no-cache --no-rate-limit --no-subscription-tracking --no-code-aware --no-read-lifecycle --log-file "%HORIZON_WORKSPACE_DIR%\logs\requests.jsonl"

:end
echo.
pause
