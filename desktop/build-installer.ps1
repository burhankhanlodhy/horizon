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

# Code signing (Azure Artifact Signing, see desktop\sign.ps1 and SIGNING.md).
# Active only when all three CS_SIGN_* variables are set; otherwise nothing here runs.
# Signs the helper exes and the client's files that are not already validly
# signed (PSF/Microsoft files are left alone) before tauri packs them. Tauri
# signs the app exe and installer itself via src-tauri\tauri.signing.conf.json.
$signingConfig = @()
if ($env:CS_SIGN_ENDPOINT -and $env:CS_SIGN_ACCOUNT -and $env:CS_SIGN_PROFILE) {
    $helperDir = Join-Path $PSScriptRoot "claude-code-tools\target\release"
    $toSign = @(
        @("claude-code-launcher.exe", "contextshrink-status.exe", "horizon.exe") |
            ForEach-Object { Join-Path $helperDir $_ } |
            Where-Object { (Test-Path $_) -and (Get-AuthenticodeSignature -LiteralPath $_).Status -ne "Valid" }
        Get-ChildItem (Join-Path $PSScriptRoot "client\dist\horizon") -Recurse -File -Include *.exe, *.dll, *.pyd |
            Where-Object { (Get-AuthenticodeSignature -LiteralPath $_.FullName).Status -ne "Valid" } |
            ForEach-Object { $_.FullName }
    )
    if ($toSign.Count) { & (Join-Path $PSScriptRoot "sign.ps1") @toSign }
    $signingConfig = @("--config", "src-tauri/tauri.signing.conf.json")
}

Push-Location (Join-Path $PSScriptRoot "app")
try {
    $ErrorActionPreference = "Continue"
    npm ci
    if ($LASTEXITCODE -ne 0) { throw "npm ci failed" }
    npx tauri build @updaterConfig @signingConfig
    if ($LASTEXITCODE -ne 0) { throw "tauri build failed" }
}
finally {
    Pop-Location
}

Get-ChildItem (Join-Path $PSScriptRoot "app\src-tauri\target\release\bundle\nsis\*.exe") |
    ForEach-Object { "Installer: {0} ({1:N0} MB)" -f $_.FullName, ($_.Length / 1MB) }
