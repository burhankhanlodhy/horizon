@echo off
setlocal
title ContextShrink gateway report
rem Prints cost per session from the gateway's exact usage log and saves it as
rem gateway-report.json next to this file.

set "REPO=%~dp0..\.."
for %%I in ("%REPO%") do set "REPO=%%~fI"
set "PY=%CONTEXTSHRINK_PY%"
if not defined PY if exist "%REPO%\.venv\Scripts\python.exe" set "PY=%REPO%\.venv\Scripts\python.exe"
if not defined PY if exist "%REPO%\..\..\..\.venv\Scripts\python.exe" set "PY=%REPO%\..\..\..\.venv\Scripts\python.exe"
if not defined PY (
  echo Python was not found. Set CONTEXTSHRINK_PY to your python.exe and try again.
  goto end
)
set "PYTHONPATH=%REPO%"
if not defined HORIZON_WORKSPACE_DIR set "HORIZON_WORKSPACE_DIR=%USERPROFILE%\ContextShrinkGateway"

"%PY%" -c "from horizon.cli import main; main()" gateway report --workspace "%HORIZON_WORKSPACE_DIR%"
"%PY%" -c "from horizon.cli import main; main()" gateway report --workspace "%HORIZON_WORKSPACE_DIR%" --json > "%~dp0gateway-report.json"
echo.
echo Saved gateway-report.json in %~dp0

:end
echo.
pause
