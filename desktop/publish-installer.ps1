<#
.SYNOPSIS
  Publish the built desktop installers to the dashboard's Downloads page.

.DESCRIPTION
  Uploads the Windows NSIS installer, the Linux packages for the same version
  (desktop\dist-linux, from desktop/linux/build-in-container.sh) and a
  latest.json listing every file with its size and SHA-256 to the Pi 4, and
  stages an install script there. Installing into
  /var/www/contextshrink/releases needs sudo, so the script prints the one
  command to run. Earlier installers are kept; latest.json points at the new ones.

  Run after desktop\build-installer.ps1 and the Linux build, from PowerShell.
  Pass -WindowsOnly to publish without Linux packages.
#>
param(
    [string]$Pi4 = "raspberrypi4@192.168.0.64",
    [switch]$WindowsOnly
)
$ErrorActionPreference = "Stop"

$app = Join-Path $PSScriptRoot "app"
$version = (Get-Content (Join-Path $app "src-tauri\tauri.conf.json") -Raw | ConvertFrom-Json).version
$installer = Get-ChildItem (Join-Path $app "src-tauri\target\release\bundle\nsis\*_${version}_x64-setup.exe") |
    Sort-Object LastWriteTime -Descending | Select-Object -First 1
if (-not $installer) { throw "No installer for version $version. Run desktop\build-installer.ps1 first." }

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
if (-not $WindowsOnly) {
    $linuxDir = Join-Path $PSScriptRoot "dist-linux"
    foreach ($k in @(
            @{ kind = "deb"; pattern = "*_${version}_amd64.deb" },
            @{ kind = "rpm"; pattern = "*-${version}-*.x86_64.rpm" },
            @{ kind = "appimage"; pattern = "*_${version}_amd64.AppImage" })) {
        $f = Get-ChildItem (Join-Path $linuxDir $k.pattern) -ErrorAction SilentlyContinue | Select-Object -First 1
        if (-not $f) { throw "No Linux $($k.kind) for version $version in $linuxDir. Build it, or pass -WindowsOnly." }
        $files += $f
        $assets += Get-Asset $f "linux" $k.kind
    }
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

# Windows PowerShell 5.1 turns any stderr line from a native command (e.g.
# OpenSSH's harmless "IO is still pending on closed socket") into a terminating
# error under "Stop"; judge ssh/scp by their exit codes instead.
$ErrorActionPreference = "Continue"
function Assert-Ok([string]$what) {
    if ($LASTEXITCODE -ne 0) { throw "$what failed (exit $LASTEXITCODE)" }
}

$remote = ssh -o BatchMode=yes $Pi4 "mktemp -d /tmp/cs-release.XXXXXX" 2>$null
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
