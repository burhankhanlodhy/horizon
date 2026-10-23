<#
.SYNOPSIS
  Publish the built desktop installers to the dashboard's Downloads page.

.DESCRIPTION
  Uploads the Windows NSIS installer, the Linux packages for the same version
  (desktop\dist-linux, from desktop/linux/build-in-container.sh) and a
  latest.json listing every file with its size and SHA-256. Earlier installers
  are kept; latest.json points at the new ones and is uploaded last.

  Files with an updater signature (<file>.sig, written by builds that had the
  signing key) also go into updater.json, which installed apps check to update
  themselves.

  -Target r2 (default) uploads to the private R2 bucket that
  app.contextshrink.com/releases/* serves from (dashboard\functions\releases),
  using the token described in deploy\cloudflare-pages\README.md.
  -Target pi4 is the old path: stage on the Pi 4 and print the one sudo
  command that installs into /var/www/contextshrink/releases.

  Release flow (the Windows installer is built and signed in CI):
    1. Tag desktop-v<version> (or run "Desktop app" on Actions). The workflow
       builds macOS, Linux and Windows and uploads one artifact per platform.
    2. Download the artifacts into the folders this script reads:
         gh run download <run-id> -n contextshrink-windows-x86_64 -D desktop\dist-windows
         gh run download <run-id> -n contextshrink-macos-arm64 -D desktop\dist-macos
         gh run download <run-id> -n contextshrink-linux-x86_64 -D desktop\dist-linux
    3. .\desktop\publish-installer.ps1

  The Windows installer comes from desktop\dist-windows when it is there for
  this version, otherwise from the local build (desktop\build-installer.ps1,
  app\src-tauri\target\release\bundle\nsis). Windows installers from CI are
  signed with Azure Artifact Signing when the repo is configured (desktop\SIGNING.md);
  this script warns if the installer it publishes has no valid Authenticode signature.

  Run from PowerShell. -Platforms limits what is published.

  -ClaudeCodeTools <dir> also publishes the npx Code-tab tools (for
  npx contextshrink-claude): claude-code-launcher.exe, contextshrink-codetab.exe
  and manifest.json from <dir>, for example the CI artifact
  contextshrink-claude-code-tools-windows, downloaded into desktop\dist-claude-code-tools:
    gh run download <run-id> -n contextshrink-claude-code-tools-windows -D desktop\dist-claude-code-tools
    .\desktop\publish-installer.ps1 -ClaudeCodeTools desktop\dist-claude-code-tools
  Both exes must have a Valid Authenticode signature and match manifest.json, or
  nothing is uploaded. They go to <Bucket>/claude-code-tools/<version>/ (the version is
  the one in manifest.json) and are served at
  https://app.contextshrink.com/releases/claude-code-tools/<version>/<file>. -Target r2 only.
  The installer for the current version is still required in the same run, unless
  -ToolsOnly is given: then only the tools are uploaded and latest.json, updater.json
  and the installers are left exactly as they are.

  -Engine <dir> publishes the ContextShrink Code engine (the signed embedded client
  that npx contextshrink-claude and contextshrink-codex install): the zip
  contextshrink-engine-windows-x86_64.zip (horizon.exe at its root) and
  engine-manifest.json from <dir>, for example the CI artifact contextshrink-engine-windows:
    gh run download <run-id> -n contextshrink-engine-windows -D desktop\dist-engine
    .\desktop\publish-installer.ps1 -Engine desktop\dist-engine -EngineOnly
  The zip's SHA-256 and size must match engine-manifest.json, the manifest's url must be
  the engine URL for its version, and horizon.exe inside the zip must have a Valid
  Authenticode signature, or nothing is uploaded. They go to <Bucket>/engine/<version>/
  (the version is the one in engine-manifest.json) and are served at
  https://app.contextshrink.com/releases/engine/<version>/<file>. -Target r2 only.
  -EngineOnly uploads only the engine. Without it, -Engine is published with the
  installers, after the Code-tab tools and before latest.json.
#>
param(
    [ValidateSet("r2", "pi4")]
    [string]$Target = "r2",
    [string]$Bucket = "contextshrink-releases",
    [string]$Pi4 = "raspberrypi4@192.168.0.64",
    [ValidateSet("windows", "macos", "linux")]
    [string[]]$Platforms = @("windows", "macos", "linux"),
    # Folder with claude-code-launcher.exe, contextshrink-codetab.exe and manifest.json.
    [string]$ClaudeCodeTools,
    # Upload only the -ClaudeCodeTools files; leave the installers and manifests alone.
    [switch]$ToolsOnly,
    # Folder with contextshrink-engine-windows-x86_64.zip and engine-manifest.json.
    [string]$Engine,
    # Upload only the -Engine files; leave the installers and manifests alone.
    [switch]$EngineOnly
)
$ErrorActionPreference = "Stop"

if ($EngineOnly -and -not $Engine) { throw "-EngineOnly needs -Engine <dir>." }
if ($EngineOnly -and $ToolsOnly) { throw "-EngineOnly and -ToolsOnly cannot be combined." }
if ($ToolsOnly -and $Engine) { throw "-ToolsOnly and -Engine cannot be combined: use -EngineOnly to upload the engine alone." }

# Code-tab tools: checked before anything is uploaded, so a bad signature or a
# mismatch refuses the whole run.
$toolsFiles = @()
$toolsVersion = ""
if ($ClaudeCodeTools) {
    if ($Target -ne "r2") { throw "-ClaudeCodeTools publishes to R2 only: use -Target r2." }
    $toolsDir = (Resolve-Path -LiteralPath $ClaudeCodeTools -ErrorAction Stop).Path
    $manifestFile = Join-Path $toolsDir "manifest.json"
    if (-not (Test-Path -LiteralPath $manifestFile)) { throw "No manifest.json in $toolsDir." }
    $toolsManifest = Get-Content -LiteralPath $manifestFile -Raw | ConvertFrom-Json
    $toolsVersion = [string]$toolsManifest.version
    if ($toolsVersion -notmatch '^\d+\.\d+\.\d+$') { throw "manifest.json has no usable version: '$toolsVersion'." }
    foreach ($name in "claude-code-launcher.exe", "contextshrink-codetab.exe") {
        $path = Join-Path $toolsDir $name
        if (-not (Test-Path -LiteralPath $path)) { throw "$name is missing from $toolsDir." }
        $signature = Get-AuthenticodeSignature -LiteralPath $path
        if ($signature.Status -ne "Valid") {
            throw "$name has no valid Authenticode signature ($($signature.Status)); nothing was uploaded."
        }
        $entry = $toolsManifest.files.$name
        if (-not $entry) { throw "manifest.json does not list $name; nothing was uploaded." }
        $item = Get-Item -LiteralPath $path
        $sha = (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLower()
        if ($sha -ne ([string]$entry.sha256).ToLower() -or $item.Length -ne [long]$entry.size) {
            throw "$name does not match manifest.json (sha256 or size); nothing was uploaded."
        }
        $toolsFiles += $item
    }
    # The manifest goes last, so it never points at a file that is not there yet.
    $toolsFiles += Get-Item -LiteralPath $manifestFile
    "Code-tab tools $toolsVersion from ${toolsDir}: $(($toolsFiles | ForEach-Object Name) -join ', ')"
}

# Engine: the same rule as the tools. Everything is checked before anything is
# uploaded, so a mismatch or an unsigned horizon.exe refuses the whole run.
$engineFiles = @()
$engineVersion = ""
if ($Engine) {
    if ($Target -ne "r2") { throw "-Engine publishes to R2 only: use -Target r2." }
    $engineDir = (Resolve-Path -LiteralPath $Engine -ErrorAction Stop).Path
    $engineManifestFile = Join-Path $engineDir "engine-manifest.json"
    if (-not (Test-Path -LiteralPath $engineManifestFile)) { throw "No engine-manifest.json in $engineDir." }
    $engineManifest = Get-Content -LiteralPath $engineManifestFile -Raw | ConvertFrom-Json
    $engineVersion = [string]$engineManifest.version
    if ($engineVersion -notmatch '^\d+\.\d+\.\d+$') { throw "engine-manifest.json has no usable version: '$engineVersion'." }
    $engineName = "contextshrink-engine-windows-x86_64.zip"
    $engineZip = Join-Path $engineDir $engineName
    if (-not (Test-Path -LiteralPath $engineZip)) { throw "$engineName is missing from $engineDir." }
    $expectedUrl = "https://app.contextshrink.com/releases/engine/$engineVersion/$engineName"
    if ([string]$engineManifest.url -ne $expectedUrl) { throw "engine-manifest.json url is not $expectedUrl; nothing was uploaded." }
    $engineItem = Get-Item -LiteralPath $engineZip
    $engineSha = (Get-FileHash -LiteralPath $engineZip -Algorithm SHA256).Hash.ToLower()
    if ($engineSha -ne ([string]$engineManifest.sha256).ToLower() -or $engineItem.Length -ne [long]$engineManifest.size) {
        throw "$engineName does not match engine-manifest.json (sha256 or size); nothing was uploaded."
    }
    # horizon.exe must sit at the zip root and be validly signed as shipped.
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $archive = [IO.Compression.ZipFile]::OpenRead($engineZip)
    try {
        $entry = $archive.GetEntry("horizon.exe")
        if (-not $entry) { throw "$engineName has no horizon.exe at its root; nothing was uploaded." }
        $probe = Join-Path ([IO.Path]::GetTempPath()) ("cs-engine-check-" + [guid]::NewGuid().ToString("N") + ".exe")
        try {
            [IO.Compression.ZipFileExtensions]::ExtractToFile($entry, $probe, $true)
            $signature = Get-AuthenticodeSignature -LiteralPath $probe
        }
        finally {
            Remove-Item -LiteralPath $probe -Force -ErrorAction SilentlyContinue
        }
    }
    finally {
        $archive.Dispose()
    }
    if ($signature.Status -ne "Valid") {
        throw "horizon.exe inside $engineName has no valid Authenticode signature ($($signature.Status)); nothing was uploaded."
    }
    $engineFiles += $engineItem
    # The manifest goes last, so it never points at a zip that is not there yet.
    $engineFiles += Get-Item -LiteralPath $engineManifestFile
    "Engine $engineVersion from ${engineDir}: $(($engineFiles | ForEach-Object Name) -join ', ')"
}

# Uploads $engineFiles to <Bucket>/engine/<version>/ (zip first, manifest last).
function Send-EngineToR2 {
    foreach ($f in $engineFiles) {
        $key = "engine/$engineVersion/$($f.Name)"
        if ($f.Extension -eq ".zip") { $type = "application/zip" } else { $type = "application/json" }
        "Uploading $key"
        Invoke-Wrangler r2 object put "$Bucket/$key" --file $f.FullName --content-type $type --remote
    }
    "Published engine ${engineVersion}: https://app.contextshrink.com/releases/engine/$engineVersion/engine-manifest.json"
}

if ($ToolsOnly) {
    if (-not $ClaudeCodeTools) { throw "-ToolsOnly needs -ClaudeCodeTools <dir>." }
    . (Join-Path $PSScriptRoot "..\deploy\cloudflare-pages\cloudflare.ps1")
    Use-CloudflareToken
    foreach ($f in $toolsFiles) {
        $key = "claude-code-tools/$toolsVersion/$($f.Name)"
        $type = if ($f.Extension -eq ".json") { "application/json" } else { "application/vnd.microsoft.portable-executable" }
        "Uploading $key"
        Invoke-Wrangler r2 object put "$Bucket/$key" --file $f.FullName --content-type $type --remote
    }
    "Published Code-tab tools ${toolsVersion}: https://app.contextshrink.com/releases/claude-code-tools/$toolsVersion/manifest.json"
    return
}

if ($EngineOnly) {
    . (Join-Path $PSScriptRoot "..\deploy\cloudflare-pages\cloudflare.ps1")
    Use-CloudflareToken
    Send-EngineToR2
    return
}

$app = Join-Path $PSScriptRoot "app"
$version = (Get-Content (Join-Path $app "src-tauri\tauri.conf.json") -Raw | ConvertFrom-Json).version
# Prefer the CI-built installer (desktop\dist-windows, see the release flow
# above); fall back to the local build.
$installer = Get-ChildItem (Join-Path $PSScriptRoot "dist-windows\*_${version}_x64-setup.exe") -ErrorAction SilentlyContinue |
    Sort-Object LastWriteTime -Descending | Select-Object -First 1
if (-not $installer) {
    $installer = Get-ChildItem (Join-Path $app "src-tauri\target\release\bundle\nsis\*_${version}_x64-setup.exe") -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending | Select-Object -First 1
}
if (-not $installer) {
    throw "No Windows installer for version $version in desktop\dist-windows or the local build. Download the CI artifact or run desktop\build-installer.ps1."
}
"Windows installer: $($installer.FullName)"
if ((Get-AuthenticodeSignature -LiteralPath $installer.FullName).Status -ne "Valid") {
    Write-Warning "$($installer.Name) has no valid Authenticode signature."
}

function Get-Asset([IO.FileInfo]$File, [string]$Os, [string]$Kind) {
    [ordered]@{
        os         = $Os
        arch       = "x86_64"
        kind       = $Kind
        file       = $File.Name
        size_bytes = $File.Length
        sha256     = (Get-FileHash $File.FullName -Algorithm SHA256).Hash.ToLower()
    }
}

$files = @($installer)
$assets = @(Get-Asset $installer "windows" "installer")
$wanted = @(
    @{ os = "macos"; arch = "aarch64"; kind = "dmg"; dir = "dist-macos"; pattern = "*_${version}_aarch64.dmg" },
    @{ os = "linux"; arch = "x86_64"; kind = "deb"; dir = "dist-linux"; pattern = "*_${version}_amd64.deb" },
    @{ os = "linux"; arch = "x86_64"; kind = "rpm"; dir = "dist-linux"; pattern = "*-${version}-*.x86_64.rpm" },
    @{ os = "linux"; arch = "x86_64"; kind = "appimage"; dir = "dist-linux"; pattern = "*_${version}_amd64.AppImage" }
) | Where-Object { $Platforms -contains $_.os }
foreach ($k in $wanted) {
    $dir = Join-Path $PSScriptRoot $k.dir
    $f = Get-ChildItem (Join-Path $dir $k.pattern) -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $f) { throw "No $($k.os) $($k.kind) for version $version in $dir. Build it, or leave $($k.os) out of -Platforms." }
    $files += $f
    $asset = Get-Asset $f $k.os $k.kind
    $asset.arch = $k.arch
    $assets += $asset
}

# The top-level file fields describe the Windows installer, for Downloads
# pages built before Linux support.
$release = [ordered]@{
    version    = $version
    released   = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
    file       = $assets[0].file
    size_bytes = $assets[0].size_bytes
    sha256     = $assets[0].sha256
    assets     = $assets
}
$staging = Join-Path ([IO.Path]::GetTempPath()) "cs-release-$version"
New-Item -ItemType Directory -Force $staging | Out-Null
$latest = Join-Path $staging "latest.json"
[IO.File]::WriteAllText($latest, ($release | ConvertTo-Json -Depth 4))

# In-app updates (tauri-plugin-updater): one entry per platform whose package
# carries a signature. Windows updates through the NSIS installer, Linux
# through the AppImage.
$updaterKeys = @{ "windows/installer" = "windows-x86_64"; "linux/appimage" = "linux-x86_64" }
$updatePlatforms = [ordered]@{}
for ($i = 0; $i -lt $files.Count; $i++) {
    $key = $updaterKeys["$($assets[$i].os)/$($assets[$i].kind)"]
    $sig = "$($files[$i].FullName).sig"
    if ($key -and (Test-Path $sig)) {
        $updatePlatforms[$key] = [ordered]@{
            signature = (Get-Content $sig -Raw).Trim()
            url       = "https://app.contextshrink.com/releases/$($files[$i].Name)"
        }
    }
}
$updater = $null
if ($updatePlatforms.Count) {
    $updater = Join-Path $staging "updater.json"
    $manifest = [ordered]@{
        version   = $version
        notes     = "ContextShrink $version"
        pub_date  = $release.released
        platforms = $updatePlatforms
    }
    [IO.File]::WriteAllText($updater, ($manifest | ConvertTo-Json -Depth 4))
    "In-app update for: $($updatePlatforms.Keys -join ', ')"
} else {
    Write-Warning "No updater signatures found: installed apps will not be offered $version."
}

if ($Target -eq "r2") {
    . (Join-Path $PSScriptRoot "..\deploy\cloudflare-pages\cloudflare.ps1")
    Use-CloudflareToken
    $types = @{
        ".exe" = "application/vnd.microsoft.portable-executable"
        ".dmg" = "application/x-apple-diskimage"
        ".deb" = "application/vnd.debian.binary-package"
        ".rpm" = "application/x-rpm"
    }
    foreach ($f in $toolsFiles) {
        $key = "claude-code-tools/$toolsVersion/$($f.Name)"
        $type = if ($f.Extension -eq ".json") { "application/json" } else { "application/vnd.microsoft.portable-executable" }
        "Uploading $key"
        Invoke-Wrangler r2 object put "$Bucket/$key" --file $f.FullName --content-type $type --remote
    }
    if ($toolsFiles.Count) {
        "Published Code-tab tools ${toolsVersion}: https://app.contextshrink.com/releases/claude-code-tools/$toolsVersion/manifest.json"
    }
    if ($engineFiles.Count) { Send-EngineToR2 }
    foreach ($f in $files) {
        $type = $types[$f.Extension.ToLower()]
        if (-not $type) { $type = "application/octet-stream" }
        "Uploading $($f.Name) ($([math]::Round($f.Length / 1MB)) MB)"
        Invoke-Wrangler r2 object put "$Bucket/$($f.Name)" --file $f.FullName --content-type $type --remote
    }
    # Last, so the Downloads page and installed apps never point at a file
    # that is not there yet.
    Invoke-Wrangler r2 object put "$Bucket/latest.json" --file $latest --content-type application/json --remote
    if ($updater) {
        Invoke-Wrangler r2 object put "$Bucket/updater.json" --file $updater --content-type application/json --remote
    }
    "Published version ${version}: https://app.contextshrink.com/releases/latest.json"
    return
}

# Windows PowerShell 5.1 turns any stderr line from a native command (e.g.
# OpenSSH's harmless "IO is still pending on closed socket") into a terminating
# error under "Stop"; judge ssh/scp by their exit codes instead.
$ErrorActionPreference = "Continue"
function Assert-Ok([string]$what) {
    if ($LASTEXITCODE -ne 0) { throw "$what failed (exit $LASTEXITCODE)" }
}

$remote = ssh -o BatchMode=yes $Pi4 "mktemp -d ~/cs-release.XXXXXX" 2>$null
Assert-Ok "Creating the staging folder on the Pi 4"
$remote = "$remote".Trim()
foreach ($f in $files) {
    scp -o BatchMode=yes -q $f.FullName "${Pi4}:$remote/" 2>$null
    Assert-Ok "Uploading $($f.Name)"
}
scp -o BatchMode=yes -q $latest "${Pi4}:$remote/" 2>$null
Assert-Ok "Uploading latest.json"

$script = @'
#!/bin/bash
# Install a staged ContextShrink release into the Downloads folder.
set -euo pipefail
SRC=__SRC__
DEST=/var/www/contextshrink/releases
sudo mkdir -p "$DEST"
sudo find "$SRC" -maxdepth 1 -type f ! -name latest.json -exec cp {} "$DEST/" \;
sudo cp "$SRC/latest.json" "$DEST/latest.json"
sudo chown -R root:root "$DEST"
sudo chmod -R u=rwX,go=rX "$DEST"
rm -rf "$SRC"
echo "Published version $(grep -o '"version": *"[^"]*"' "$DEST/latest.json")"
rm -f -- "$0"
'@ -replace "__SRC__", $remote
# Windows PowerShell 5.1 prefixes piped text with a UTF-8 BOM, which breaks the
# shebang: send BOM-less UTF-8, and strip CRs and any BOM on arrival as well.
$OutputEncoding = New-Object System.Text.UTF8Encoding $false
$script | ssh -o BatchMode=yes $Pi4 "tr -d '\r' | sed '1s/^\xEF\xBB\xBF//' > /tmp/cs-publish-release.sh && chmod 755 /tmp/cs-publish-release.sh && bash -n /tmp/cs-publish-release.sh" 2>$null
Assert-Ok "Staging the publish script"

$files | ForEach-Object { "Staged $($_.Name) ($([math]::Round($_.Length / 1MB)) MB)" }
"Run this to publish (asks for the Pi 4 sudo password):"
"  ssh -t $Pi4 /tmp/cs-publish-release.sh"
