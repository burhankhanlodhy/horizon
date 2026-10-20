<#
.SYNOPSIS
  Sign Windows files with Azure Artifact Signing, or skip when it is not configured.

.DESCRIPTION
  sign.ps1 <file> [<file> ...]

  Tauri calls it with one path per file (bundle.windows.signCommand in
  app\src-tauri\tauri.signing.generated.json, written by build-installer.ps1);
  build-installer.ps1 also calls it directly for the
  bundled helper exes and client files.

  Signing is configured when all of these are set:
    CS_SIGN_ENDPOINT  account endpoint for the account's region, e.g. https://wus2.codesigning.azure.net
    CS_SIGN_ACCOUNT   Artifact Signing account name
    CS_SIGN_PROFILE   certificate profile name
  With none of them set the script prints "Signing skipped (not configured)" and
  exits 0. With only some set it fails, so a half-configured build cannot pass
  as signed or unsigned by accident.

  Each file gets a SHA-256 Authenticode signature and an RFC 3161 timestamp from
  http://timestamp.acs.microsoft.com (Artifact Signing certificates last three
  days, so the timestamp is what keeps signatures valid). Every file is then
  checked with Get-AuthenticodeSignature: it must be Valid and timestamped.

  Authentication is the dlib's DefaultAzureCredential: `az login` on a
  workstation, or azure/login (OIDC) in CI.

  Optional overrides:
    CS_SIGNTOOL   signtool.exe (default: newest Windows SDK x64 signtool)
    CS_SIGN_DLIB  Azure.CodeSigning.Dlib.dll, x64 (default: searched under Program Files)

  Docs: https://learn.microsoft.com/en-us/azure/artifact-signing/how-to-signing-integrations
#>
# ValueFromRemainingArguments: with `powershell -File`, a plain [string[]]
# parameter binds only the first path and drops the rest silently.
param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Files)

$ErrorActionPreference = "Stop"
# Tauri's signCommand hides this script's output; CS_SIGN_LOG keeps a copy.
if ($env:CS_SIGN_LOG) { Start-Transcript -LiteralPath $env:CS_SIGN_LOG -Append | Out-Null }
$required = @("CS_SIGN_ENDPOINT", "CS_SIGN_ACCOUNT", "CS_SIGN_PROFILE")
$present = @($required | Where-Object { [Environment]::GetEnvironmentVariable($_) })
if ($present.Count -eq 0) {
    "Signing skipped (not configured)"
    exit 0
}
if ($present.Count -ne $required.Count) {
    $missing = @($required | Where-Object { -not [Environment]::GetEnvironmentVariable($_) })
    throw "Signing is half-configured; missing: $($missing -join ', '). Set all three CS_SIGN_* variables or none."
}
if (-not $Files) {
    "Signing: no files given"
    exit 0
}

# Full paths; a missing file fails here, before anything is signed.
$paths = @($Files | ForEach-Object { (Get-Item -LiteralPath $_).FullName })

function Find-SignTool {
    if ($env:CS_SIGNTOOL) { return $env:CS_SIGNTOOL }
    $bin = Join-Path ${env:ProgramFiles(x86)} "Windows Kits\10\bin"
    $found = @(Get-ChildItem -Path (Join-Path $bin "*\x64\signtool.exe") -ErrorAction SilentlyContinue |
        Where-Object { $_.Directory.Parent.Name -match '^\d+(\.\d+){3}$' } |
        Sort-Object { [version]$_.Directory.Parent.Name } -Descending)
    if (-not $found) { throw "signtool.exe not found under $bin. Install the Windows SDK or set CS_SIGNTOOL." }
    return $found[0].FullName
}

function Find-Dlib {
    if ($env:CS_SIGN_DLIB) { return $env:CS_SIGN_DLIB }
    $hits = @(Get-ChildItem -ErrorAction SilentlyContinue -Path @(
        "$env:ProgramFiles\*\*\bin\x64\Azure.CodeSigning.Dlib.dll",
        "${env:ProgramFiles(x86)}\*\*\bin\x64\Azure.CodeSigning.Dlib.dll",
        "$env:LOCALAPPDATA\*\*\bin\x64\Azure.CodeSigning.Dlib.dll"))
    if (-not $hits) {
        throw "Azure.CodeSigning.Dlib.dll not found. Install the Artifact Signing Client Tools (winget install -e --id Microsoft.Azure.ArtifactSigningClientTools) or set CS_SIGN_DLIB."
    }
    return $hits[0].FullName
}

$signtool = Find-SignTool
$dlib = Find-Dlib

# metadata.json for the dlib. Written without a BOM; removed in finally.
$metadata = Join-Path ([IO.Path]::GetTempPath()) ("cs-sign-" + [guid]::NewGuid().ToString("N") + ".json")
$json = [ordered]@{
    Endpoint               = $env:CS_SIGN_ENDPOINT
    CodeSigningAccountName = $env:CS_SIGN_ACCOUNT
    CertificateProfileName = $env:CS_SIGN_PROFILE
} | ConvertTo-Json
[IO.File]::WriteAllText($metadata, $json, (New-Object Text.UTF8Encoding $false))

try {
    # signtool prints to stderr too; judge it by the exit code.
    $ErrorActionPreference = "Continue"
    # Batches keep the command line well under the Windows length limit.
    for ($start = 0; $start -lt $paths.Count; $start += 50) {
        $batch = @($paths | Select-Object -Skip $start -First 50)
        & $signtool sign /v /fd SHA256 /tr "http://timestamp.acs.microsoft.com" /td SHA256 `
            /d ContextShrink /dlib $dlib /dmdf $metadata @batch
        if ($LASTEXITCODE -ne 0) { throw "signtool failed (exit $LASTEXITCODE)" }
    }
    $ErrorActionPreference = "Stop"

    $bad = @()
    foreach ($p in $paths) {
        $sig = Get-AuthenticodeSignature -LiteralPath $p
        if ($sig.Status -ne "Valid" -or -not $sig.TimeStamperCertificate) {
            $bad += "$p ($($sig.Status): $($sig.StatusMessage))"
        } else {
            "Signed: $p ($($sig.SignerCertificate.Subject))"
        }
    }
    if ($bad) { throw "Signature check failed:`n  $($bad -join "`n  ")" }
}
finally {
    $ErrorActionPreference = "Stop"
    Remove-Item -LiteralPath $metadata -Force -ErrorAction SilentlyContinue
}
