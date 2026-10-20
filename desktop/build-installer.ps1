<#
.SYNOPSIS
  Build the ContextShrink Windows installer (NSIS .exe).

.DESCRIPTION
  1. Freezes the Horizon client (client\build-client.ps1) so users need no Python.
  2. Builds the Tauri app, bundling the client as a resource.
  Output: app\src-tauri\target\release\bundle\nsis\ContextShrink_<version>_x64-setup.exe

  With the updater signing key (TAURI_SIGNING_PRIVATE_KEY, or the file
  %USERPROFILE%\.tauri\contextshrink-updater.key) the build also writes
  <installer>.exe.sig, which publish-installer.ps1 puts in updater.json so
  installed apps update themselves. Without it the installer still builds,
  but installed apps are not offered that version.
#>
[CmdletBinding()]
param(
    # Freeze the client with PyInstaller (client\build-client.ps1) instead of the
    # embeddable Python build. Kept for a rollback only.
    [switch]$PyInstaller
)
$ErrorActionPreference = "Stop"

$updaterConfig = @()
$keyFile = Join-Path $env:USERPROFILE ".tauri\contextshrink-updater.key"
if (-not $env:TAURI_SIGNING_PRIVATE_KEY -and (Test-Path $keyFile)) {
    $env:TAURI_SIGNING_PRIVATE_KEY = (Get-Content $keyFile -Raw).Trim()
}
if ($env:TAURI_SIGNING_PRIVATE_KEY) {
    if ($null -eq $env:TAURI_SIGNING_PRIVATE_KEY_PASSWORD) { $env:TAURI_SIGNING_PRIVATE_KEY_PASSWORD = "" }
    $updaterConfig = @("--config", "src-tauri/tauri.updater.conf.json")
} else {
    Write-Warning "No updater signing key: installed apps will not be offered this build."
}

if ($PyInstaller) {
    & (Join-Path $PSScriptRoot "client\build-client.ps1")
} else {
    & (Join-Path $PSScriptRoot "client\build-client-embed.ps1")
}

Push-Location (Join-Path $PSScriptRoot "app")
try {
    $ErrorActionPreference = "Continue"
    npm ci
    if ($LASTEXITCODE -ne 0) { throw "npm ci failed" }
    npx tauri build @updaterConfig
    if ($LASTEXITCODE -ne 0) { throw "tauri build failed" }
}
finally {
    Pop-Location
}

Get-ChildItem (Join-Path $PSScriptRoot "app\src-tauri\target\release\bundle\nsis\*.exe") |
    ForEach-Object { "Installer: {0} ({1:N0} MB)" -f $_.FullName, ($_.Length / 1MB) }
