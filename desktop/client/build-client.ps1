<#
.SYNOPSIS
  Freeze the Horizon CLI into a standalone Windows client for the desktop app.

.DESCRIPTION
  Produces desktop\client\dist\horizon\horizon.exe (one-folder build) using the
  repo's .venv. The desktop app bundles that folder, so users never need
  Python. Package data (e.g. the OpenCode transport plugin that routes custom
  providers through the proxy) is bundled. Heavy ML packages are excluded:
  the client only forwards, stores the device key and wraps tools;
  compression runs on the hosted proxy.
#>
$ErrorActionPreference = "Stop"
$root = Resolve-Path (Join-Path $PSScriptRoot "..\..")
$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) { throw "Repo .venv not found at $python" }

$excludes = @(
    "torch", "transformers", "onnxruntime", "magika", "litellm", "tokenizers",
    "tiktoken", "scipy", "sklearn", "fastembed", "hnswlib", "sentence_transformers",
    "PIL", "matplotlib", "pandas", "tkinter", "IPython", "pytest"
) | ForEach-Object { "--exclude-module=$_" }

# Bake the version into horizon\_build_info.py (removed again below); without
# it the frozen client reports whatever stale package metadata the venv holds.
$buildInfo = Join-Path $PSScriptRoot "build_info.py"
$version = & $python $buildInfo write
if ($LASTEXITCODE -ne 0) { throw "Writing the build version failed" }

# Run from the repo root so `horizon` resolves to the real package (its data
# files are collected from there); outputs still land in desktop\client.
Push-Location $root
try {
    # PyInstaller logs to stderr; judge success by the exit code instead.
    $ErrorActionPreference = "Continue"
    & $python -m PyInstaller (Join-Path $PSScriptRoot "horizon_client.py") `
        --name horizon `
        --distpath (Join-Path $PSScriptRoot "dist") `
        --workpath (Join-Path $PSScriptRoot "build") `
        --specpath $PSScriptRoot `
        --noconfirm --clean --console `
        --paths "$root" `
        --collect-submodules horizon.cli `
        --collect-data horizon `
        --collect-submodules keyring `
        --copy-metadata keyring `
        --hidden-import keyring.backends.Windows `
        --hidden-import win32timezone `
        --hidden-import horizon._build_info `
        @excludes
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }
}
finally {
    & $python $buildInfo remove
    Pop-Location
}

$reported = & (Join-Path $PSScriptRoot "dist\horizon\horizon.exe") --version
if ("$reported".Trim() -notmatch (" " + [regex]::Escape("$version".Trim()) + "$")) {
    throw "Frozen client reports '$reported', expected version $version"
}
"Client: $reported"
