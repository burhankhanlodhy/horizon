<#
.SYNOPSIS
  Build the Horizon client for the Windows desktop app on the official embeddable Python.

.DESCRIPTION
  Produces desktop\client\dist\horizon\, the folder the desktop app bundles
  (tauri.conf.json: "../../client/dist/horizon/" -> "horizon/"):

    horizon.exe            launcher (desktop\claude-code-tools\client-stub). It runs
                           python\python.exe -I -s -X utf8 app\horizon_main.py <args>
                           and passes the arguments, exit code and stdio through.
    python\                python.org embeddable Python 3.12 (the version pinned below).
                           Its SHA-256 is pinned and its signature must be the Python
                           Software Foundation's.
    app\horizon_main.py    entry point (desktop\client\horizon_main.py)
    app\horizon\           the repo's horizon package, with the version and the LiteLLM
                           price rows baked in (both removed from the repo afterwards)
    app\site-packages\     the pinned runtime dependencies (embed-requirements.txt)

  Replaces the PyInstaller one-folder build, which Trend Micro flags as malware
  (HEU_AEGISCS985): the long-running process is the PSF-signed python.exe, and the
  only native code is the small launcher. build-client.ps1 (PyInstaller) stays
  available for a rollback through build-installer.ps1 -PyInstaller.
#>
$ErrorActionPreference = "Stop"
# Windows PowerShell 5.1 defaults to TLS 1.0/1.1; python.org needs TLS 1.2.
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
$ProgressPreference = "SilentlyContinue"

# The newest 3.12 patch that python.org publishes an embeddable amd64 zip for
# (3.12.11 and later return 404 for it). The venv runs 3.12.14; the ABI is the same.
$PythonVersion = "3.12.10"
$EmbedSha256 = "4acbed6dd1c744b0376e3b1cf57ce906f9dc9e95e68824584c8099a63025a3c3"

$root = Resolve-Path (Join-Path $PSScriptRoot "..\..")
$venvPython = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPython)) { throw "Repo .venv not found at $venvPython" }

$clientDir = $PSScriptRoot
$cacheDir = Join-Path $clientDir ".cache"
$stageRoot = Join-Path $clientDir "build\embed-stage"
$stage = Join-Path $stageRoot "horizon"
$dist = Join-Path $clientDir "dist\horizon"
$tools = Join-Path $root "desktop\claude-code-tools"
$buildInfo = Join-Path $clientDir "build_info.py"
$pricing = Join-Path $clientDir "build_pricing.py"

function Get-Sha256([string]$path) {
    (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Assert-PythonSigned([string]$path) {
    $sig = Get-AuthenticodeSignature -LiteralPath $path
    $signer = ""
    if ($sig.SignerCertificate) { $signer = $sig.SignerCertificate.Subject }
    if ($sig.Status -ne "Valid" -or $signer -notlike "*O=Python Software Foundation*") {
        throw "$path is not validly signed by the Python Software Foundation (status $($sig.Status); signer '$signer')"
    }
}

# 1. The embeddable Python, from the cache or python.org, checked against the pin.
New-Item -ItemType Directory -Force -Path $cacheDir | Out-Null
$zipName = "python-$PythonVersion-embed-amd64.zip"
$zip = Join-Path $cacheDir $zipName
if ((Test-Path $zip) -and (Get-Sha256 $zip) -ne $EmbedSha256) {
    Write-Output "Cached $zipName does not match the pinned SHA-256; downloading it again."
    Remove-Item -LiteralPath $zip -Force
}
if (-not (Test-Path $zip)) {
    $url = "https://www.python.org/ftp/python/$PythonVersion/$zipName"
    $partial = $zip + ".partial"
    Write-Output "Downloading $url"
    Invoke-WebRequest -Uri $url -OutFile $partial -UseBasicParsing
    if ((Get-Sha256 $partial) -ne $EmbedSha256) {
        Remove-Item -LiteralPath $partial -Force
        throw "SHA-256 mismatch for $url (expected $EmbedSha256). Refusing to use it."
    }
    Move-Item -LiteralPath $partial -Destination $zip -Force
}
Write-Output "Embeddable Python ${PythonVersion}: SHA-256 $EmbedSha256 verified"

# 2. Stage the layout in a scratch folder, moved into dist only when complete.
if (Test-Path $stageRoot) { Remove-Item -LiteralPath $stageRoot -Recurse -Force }
New-Item -ItemType Directory -Force -Path $stage | Out-Null
$py = Join-Path $stage "python"
Expand-Archive -LiteralPath $zip -DestinationPath $py
foreach ($signed in "python.exe", "python312.dll", "python3.dll") {
    Assert-PythonSigned (Join-Path $py $signed)
}
Write-Output "Signatures valid: python.exe, python312.dll, python3.dll (Python Software Foundation)"

# The stdlib zip, the folder of python.exe, and the app folder; site-packages
# comes from `import site` plus site.addsitedir in horizon_main.py (for .pth files).
$pth = @("python312.zip", ".", "..\app", "..\app\site-packages", "import site", "")
[IO.File]::WriteAllText((Join-Path $py "python312._pth"), ($pth -join "`r`n"), (New-Object Text.UTF8Encoding($false)))

$app = Join-Path $stage "app"
$sitePackages = Join-Path $app "site-packages"
New-Item -ItemType Directory -Force -Path $sitePackages | Out-Null

# 3. The pinned dependencies, binary wheels for this Python only.
$requirements = Join-Path $clientDir "embed-requirements.txt"
& $venvPython -m pip install --target $sitePackages --requirement $requirements `
    --only-binary=:all: --platform win_amd64 --python-version 3.12 --implementation cp `
    --no-deps --no-compile --disable-pip-version-check
if ($LASTEXITCODE -ne 0) { throw "Installing the embedded dependencies failed" }
# Test suites of the wheels are not needed at run time.
Get-ChildItem -LiteralPath $sitePackages -Recurse -Directory -Force |
    Where-Object { $_.Name -in @("tests", "test") } |
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
# Nor is anything else the client never loads (PyInstaller left these out too):
# build tooling, the console-script launchers pip writes to bin\, pywin32's GUI
# (Pythonwin) and help file, and type stubs.
$unused = @("setuptools", "_distutils_hack", "pip", "bin", "pythonwin", "PyWin32.chm", "distutils-precedence.pth")
foreach ($name in $unused) {
    $item = Join-Path $sitePackages $name
    if (Test-Path -LiteralPath $item) { Remove-Item -LiteralPath $item -Recurse -Force }
}
Get-ChildItem -LiteralPath $sitePackages -Directory -Filter "setuptools-*.dist-info" | Remove-Item -Recurse -Force
Get-ChildItem -LiteralPath $sitePackages -Directory -Filter "pip-*.dist-info" | Remove-Item -Recurse -Force
Get-ChildItem -LiteralPath $sitePackages -Recurse -File -Filter "*.pyi" | Remove-Item -Force

# 4. The horizon package. The version and the price rows are written into the
# repo for this copy and removed afterwards, as build-client.ps1 does.
Push-Location $root
try {
    $version = & $venvPython $buildInfo write
    if ($LASTEXITCODE -ne 0) { throw "Writing the build version failed" }
    $priceRows = & $venvPython $pricing write
    if ($LASTEXITCODE -ne 0) { throw "Writing the bundled price rows failed" }
    Write-Output "Baked version $version and $priceRows LiteLLM price rows"

    $source = Join-Path $root "horizon"
    $target = Join-Path $app "horizon"
    robocopy $source $target /E /NFL /NDL /NJH /NJS /NP `
        /XD __pycache__ tests /XF "_core*.pyd" "*.pyc" | Out-Null
    if ($LASTEXITCODE -ge 8) { throw "Copying the horizon package failed (robocopy exit $LASTEXITCODE)" }
}
finally {
    & $venvPython $pricing remove
    & $venvPython $buildInfo remove
    Pop-Location
}
Copy-Item -LiteralPath (Join-Path $clientDir "horizon_main.py") -Destination (Join-Path $app "horizon_main.py")

# 5. The launcher, horizon.exe.
& cargo build --release --offline -p horizon-client-stub --manifest-path (Join-Path $tools "Cargo.toml")
if ($LASTEXITCODE -ne 0) { throw "cargo build of the launcher failed" }
Copy-Item -LiteralPath (Join-Path $tools "target\release\horizon.exe") -Destination (Join-Path $stage "horizon.exe")

# 6. Byte-compile with the embedded interpreter and ship the bytecode only, as
# PyInstaller did: -b writes each .pyc next to its source (the layout Python
# imports without a source file), then the sources and __pycache__ folders go.
# A source is removed only when its .pyc exists; the entry script stays source.
& (Join-Path $py "python.exe") -m compileall -q -b -j 0 $app
if ($LASTEXITCODE -ne 0) { throw "Byte-compiling app with the embedded Python failed" }
$entry = Join-Path $app "horizon_main.py"
$sources = Get-ChildItem -LiteralPath $app -Recurse -File -Filter "*.py" |
    Where-Object { $_.FullName -ne $entry -and (Test-Path -LiteralPath ($_.FullName + "c")) }
$sources | Remove-Item -Force
Get-ChildItem -LiteralPath $app -Recurse -Directory -Filter "__pycache__" | Remove-Item -Recurse -Force
Write-Output ("Shipping bytecode only: removed {0:N0} source files" -f @($sources).Count)

# 7. Move the finished layout into dist.
if (Test-Path $dist) { Remove-Item -LiteralPath $dist -Recurse -Force }
New-Item -ItemType Directory -Force -Path (Split-Path $dist) | Out-Null
Move-Item -LiteralPath $stage -Destination $dist
Remove-Item -LiteralPath $stageRoot -Recurse -Force

# 8. Self-check, in a throwaway profile so nothing reaches the real one.
$horizon = Join-Path $dist "horizon.exe"
$sandbox = Join-Path ([IO.Path]::GetTempPath()) ("cs-embed-check-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Force -Path $sandbox | Out-Null
$profileVars = "USERPROFILE", "HOME", "LOCALAPPDATA", "APPDATA", "TEMP", "TMP"
$saved = @{}
foreach ($name in $profileVars) { $saved[$name] = [Environment]::GetEnvironmentVariable($name, "Process") }
try {
    foreach ($name in $profileVars) {
        $folder = Join-Path $sandbox $name.ToLowerInvariant()
        New-Item -ItemType Directory -Force -Path $folder | Out-Null
        [Environment]::SetEnvironmentVariable($name, $folder, "Process")
    }
    Push-Location $sandbox
    try {
        $reported = & $horizon --version
        if ($LASTEXITCODE -ne 0) { throw "horizon.exe --version failed" }
        if ("$reported".Trim() -notmatch (" " + [regex]::Escape("$version".Trim()) + "$")) {
            throw "Embedded client reports '$reported', expected version $version"
        }
        Write-Output "Client: $reported"
        foreach ($cmd in @("vault", "forward", "codetab", "lean")) {
            & $horizon $cmd --help | Out-Null
            if ($LASTEXITCODE -ne 0) { throw "horizon.exe $cmd --help failed (exit $LASTEXITCODE)" }
            Write-Output "ok    horizon $cmd --help"
        }
    }
    finally {
        Pop-Location
    }
}
finally {
    foreach ($name in $profileVars) { [Environment]::SetEnvironmentVariable($name, $saved[$name], "Process") }
    Remove-Item -LiteralPath $sandbox -Recurse -Force -ErrorAction SilentlyContinue
}

$files = Get-ChildItem -LiteralPath $dist -Recurse -File
$bytes = ($files | Measure-Object -Property Length -Sum).Sum
"Built {0}: {1:N0} MB in {2:N0} files" -f $dist, ($bytes / 1MB), $files.Count
