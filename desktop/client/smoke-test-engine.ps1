<#
.SYNOPSIS
  Smoke-test a built ContextShrink Code engine (build-client-embed.ps1 -Engine).

.DESCRIPTION
  The engine is packaged only when this passes (.github/workflows/desktop.yml).
  Checks, in order:

  1. Path lengths: every file path relative to the engine root is at most 150
     characters, the Windows MAX_PATH headroom for
     C:\Users\<name>\AppData\Local\ContextShrink\engine\<version>\.
  2. Cold start: horizon.exe code-engine run --port <Port>, with CONTEXTSHRINK_HOME set
     to an empty folder (no device, no plan note: unlicensed, pass-through). GET
     /__cs/health must answer 200 within 180 s; the seconds are printed. The body must
     say ok=false and engine=<engine-version.txt>.
  3. Forwarding: the proxy takes its Anthropic upstream from ANTHROPIC_TARGET_API_URL
     (horizon/providers/registry.py). A POST /v1/messages goes through the engine to a
     local fake upstream, which must receive it unchanged and answer.
  4. Imports: the engine's own python imports the modules the proxy needs.
  5. Stop: horizon.exe code-engine stop --port <Port> ends the engine. Only if it does
     not stop in time is the process started here ended, by its PID.

  Every profile folder (USERPROFILE, LOCALAPPDATA, APPDATA, TEMP, HOME) points into a
  throwaway folder, so nothing reaches the real profile or real ContextShrink home.
  Works on Windows PowerShell 5.1.

.PARAMETER EngineDir
  The built engine folder (horizon.exe at its root). Default: dist\engine next to this script.

.PARAMETER Port
  Loopback port for the test engine. Default 18799 (the engine's own default is 18793).

.PARAMETER ColdStartSeconds
  How long to wait for /__cs/health. Default 180.
#>
param(
    [string]$EngineDir = (Join-Path $PSScriptRoot "dist\engine"),
    [int]$Port = 18799,
    [int]$ColdStartSeconds = 180
)
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

$engine = (Resolve-Path -LiteralPath $EngineDir).Path
$horizon = Join-Path $engine "horizon.exe"
$python = Join-Path $engine "python\python.exe"
if (-not (Test-Path -LiteralPath $horizon)) { throw "No horizon.exe at the root of $engine" }
if (-not (Test-Path -LiteralPath $python)) { throw "No python\python.exe in $engine" }

$expectedVersion = @(Get-Content -LiteralPath (Join-Path $PSScriptRoot "engine-version.txt") |
    Where-Object { $_.Trim() -and $_ -notmatch '^\s*#' })[0].Trim()
if ($expectedVersion -notmatch '^\d+\.\d+\.\d+$') { throw "No usable version in engine-version.txt" }

function Write-Step([string]$text) { Write-Output "== $text" }

# Quotes one command-line argument for Start-Process (PowerShell 5.1 joins arguments
# with spaces and does not quote them).
function Quote-Arg([string]$value) { '"' + $value + '"' }

function Get-FreePort {
    $listener = New-Object System.Net.Sockets.TcpListener -ArgumentList @([Net.IPAddress]::Loopback, 0)
    $listener.Start()
    $found = $listener.LocalEndpoint.Port
    $listener.Stop()
    return $found
}

# A request to a loopback address that never goes through a system proxy.
# Returns $null when nothing answers.
function Invoke-Local {
    param(
        [string]$Method,
        [string]$Url,
        [string]$Body = $null,
        [hashtable]$Headers = @{},
        [int]$TimeoutMs = 5000
    )
    $request = [System.Net.HttpWebRequest]::Create($Url)
    $request.Proxy = $null
    $request.Method = $Method
    $request.Timeout = $TimeoutMs
    $request.ReadWriteTimeout = $TimeoutMs
    $request.KeepAlive = $false
    $request.ServicePoint.Expect100Continue = $false
    foreach ($key in $Headers.Keys) {
        if ($key -eq "Content-Type") { $request.ContentType = $Headers[$key] }
        else { $request.Headers[$key] = $Headers[$key] }
    }
    # A [string] parameter given no value arrives as "", so test for empty, not for $null.
    if (-not [string]::IsNullOrEmpty($Body)) {
        if (-not $request.ContentType) { $request.ContentType = "application/json" }
        $bytes = [Text.Encoding]::UTF8.GetBytes($Body)
        $request.ContentLength = $bytes.Length
        $stream = $request.GetRequestStream()
        $stream.Write($bytes, 0, $bytes.Length)
        $stream.Close()
    }
    $response = $null
    try {
        $response = [System.Net.HttpWebResponse]$request.GetResponse()
    } catch [System.Net.WebException] {
        if ($_.Exception.Response) { $response = [System.Net.HttpWebResponse]$_.Exception.Response }
        else { return $null }
    }
    try {
        $reader = New-Object IO.StreamReader($response.GetResponseStream(), [Text.Encoding]::UTF8)
        $text = $reader.ReadToEnd()
        $reader.Close()
        return [pscustomobject]@{ Status = [int]$response.StatusCode; Body = $text }
    } finally {
        $response.Close()
    }
}

# 1. Path lengths.
Write-Step "Path lengths in $engine"
$prefixLength = $engine.TrimEnd('\').Length + 1
$allFiles = @(Get-ChildItem -LiteralPath $engine -Recurse -File -Force)
$relative = @($allFiles | ForEach-Object { [pscustomobject]@{ Path = $_.FullName.Substring($prefixLength); Length = $_.FullName.Length - $prefixLength } })
$longest = $relative | Sort-Object Length -Descending | Select-Object -First 1
$tooLong = @($relative | Where-Object { $_.Length -gt 150 })
$unpackedBytes = ($allFiles | Measure-Object -Property Length -Sum).Sum
Write-Output ("{0:N1} MB unpacked in {1:N0} files; longest relative path {2} characters" -f ($unpackedBytes / 1MB), $allFiles.Count, $longest.Length)
if ($tooLong.Count) {
    $tooLong | ForEach-Object { Write-Output ("  {0} ({1} characters)" -f $_.Path, $_.Length) }
    throw "$($tooLong.Count) file path(s) are longer than 150 characters relative to the engine root"
}
Write-Output "ok    every path is at most 150 characters"

# Refuse to run if something already answers on the port: it is not ours to stop.
if (Invoke-Local -Method GET -Url "http://127.0.0.1:$Port/__cs/health" -TimeoutMs 1000) {
    throw "Something already answers on 127.0.0.1:$Port; stop it first (this test does not kill it)"
}

# Throwaway profile and home, restored in finally.
$sandbox = Join-Path ([IO.Path]::GetTempPath()) ("cs-engine-smoke-" + [guid]::NewGuid().ToString("N"))
$contextHome = Join-Path $sandbox "contextshrink-home"
New-Item -ItemType Directory -Force -Path $contextHome | Out-Null
$profileVars = @("USERPROFILE", "HOME", "LOCALAPPDATA", "APPDATA", "TEMP", "TMP")
$saved = @{}
foreach ($name in $profileVars) { $saved[$name] = [Environment]::GetEnvironmentVariable($name, "Process") }
$savedOther = @{}
foreach ($name in @("CONTEXTSHRINK_HOME", "ANTHROPIC_TARGET_API_URL", "NO_PROXY", "no_proxy", "HORIZON_SAVINGS", "HORIZON_MODEL_MODERNIZE")) {
    $savedOther[$name] = [Environment]::GetEnvironmentVariable($name, "Process")
}

$engineProc = $null
$upstreamProc = $null
try {
    foreach ($name in $profileVars) {
        $folder = Join-Path $sandbox ($name.ToLowerInvariant())
        New-Item -ItemType Directory -Force -Path $folder | Out-Null
        [Environment]::SetEnvironmentVariable($name, $folder, "Process")
    }

    # The fake upstream: a stdlib HTTP server that records the request and answers
    # with a fixed Anthropic-style message. It runs on the engine's own python.
    $upstreamPort = Get-FreePort
    $upstreamScript = Join-Path $sandbox "fake_upstream.py"
    $upstreamRecord = Join-Path $sandbox "upstream-request.txt"
    $upstreamCode = @'
import http.server
import json
import sys

port = int(sys.argv[1])
record = sys.argv[2]


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length)
        with open(record, "wb") as handle:
            handle.write(self.path.encode("utf-8") + b"\n" + body)
        answer = json.dumps({
            "id": "msg_smoke", "type": "message", "role": "assistant", "model": "smoke",
            "content": [{"type": "text", "text": "smoke-ok"}], "stop_reason": "end_turn",
            "stop_sequence": None, "usage": {"input_tokens": 1, "output_tokens": 1},
        }).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(answer)))
        self.end_headers()
        self.wfile.write(answer)

    def log_message(self, *args):
        pass


http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
'@
    [IO.File]::WriteAllText($upstreamScript, $upstreamCode, (New-Object Text.UTF8Encoding $false))
    $upstreamProc = Start-Process -FilePath $python -PassThru -WindowStyle Hidden `
        -ArgumentList @("-I", "-s", "-X", "utf8", (Quote-Arg $upstreamScript), $upstreamPort, (Quote-Arg $upstreamRecord)) `
        -RedirectStandardOutput (Join-Path $sandbox "upstream.out.log") -RedirectStandardError (Join-Path $sandbox "upstream.err.log")

    # The engine's environment: an empty home (unlicensed) and the fake upstream.
    [Environment]::SetEnvironmentVariable("CONTEXTSHRINK_HOME", $contextHome, "Process")
    [Environment]::SetEnvironmentVariable("ANTHROPIC_TARGET_API_URL", "http://127.0.0.1:$upstreamPort", "Process")
    [Environment]::SetEnvironmentVariable("NO_PROXY", "127.0.0.1,localhost", "Process")
    [Environment]::SetEnvironmentVariable("no_proxy", "127.0.0.1,localhost", "Process")

    # 2. Cold start.
    Write-Step "Starting the engine on 127.0.0.1:$Port (CONTEXTSHRINK_HOME is empty)"
    $engineProc = Start-Process -FilePath $horizon -PassThru -WindowStyle Hidden `
        -ArgumentList @("code-engine", "run", "--port", "$Port") `
        -RedirectStandardOutput (Join-Path $sandbox "engine.out.log") -RedirectStandardError (Join-Path $sandbox "engine.err.log")
    $clock = [Diagnostics.Stopwatch]::StartNew()
    $health = $null
    while ($clock.Elapsed.TotalSeconds -lt $ColdStartSeconds) {
        if ($engineProc.HasExited) { throw "The engine exited with code $($engineProc.ExitCode) before it answered; see $sandbox" }
        $probe = Invoke-Local -Method GET -Url "http://127.0.0.1:$Port/__cs/health" -TimeoutMs 2000
        if ($probe -and $probe.Status -eq 200) { $health = $probe; break }
        Start-Sleep -Milliseconds 500
    }
    if (-not $health) { throw "/__cs/health did not answer 200 within $ColdStartSeconds seconds" }
    $coldStart = $clock.Elapsed.TotalSeconds
    Write-Output ("ok    cold start: /__cs/health answered 200 after {0:N1} seconds" -f $coldStart)

    $body = $health.Body | ConvertFrom-Json
    if ($body.ok -ne $false) { throw "An unlicensed engine reports ok=$($body.ok): the license gate is open without a device" }
    if ($body.engine -ne $expectedVersion) { throw "The engine reports '$($body.engine)', expected $expectedVersion" }
    Write-Output "ok    health: ok=false (pass-through), engine=$($body.engine)"

    # 3. Forwarding through the engine to the fake upstream.
    Write-Step "Forwarding POST /v1/messages through the engine"
    $payload = '{"model":"claude-smoke-test","max_tokens":16,"messages":[{"role":"user","content":"smoke-request-7f3a"}]}'
    $answer = Invoke-Local -Method POST -Url "http://127.0.0.1:$Port/v1/messages" -Body $payload `
        -Headers @{ "x-api-key" = "smoke-test-key"; "anthropic-version" = "2023-06-01" } -TimeoutMs 30000
    if (-not $answer -or $answer.Status -ne 200) { throw "POST /v1/messages answered $(if ($answer) { $answer.Status } else { 'nothing' })" }
    if ($answer.Body -notmatch "smoke-ok") { throw "The answer did not come from the fake upstream: $($answer.Body)" }
    if (-not (Test-Path -LiteralPath $upstreamRecord)) { throw "The fake upstream received no request" }
    $recorded = [IO.File]::ReadAllText($upstreamRecord)
    if (($recorded -split "`n")[0].Trim() -ne "/v1/messages") { throw "The upstream got path '$(($recorded -split "`n")[0])', expected /v1/messages" }
    if ($recorded -notmatch "smoke-request-7f3a") { throw "The upstream did not receive the request body" }
    Write-Output "ok    forwarded: the fake upstream received /v1/messages with the body, and answered"

    # 4. Imports with the engine's own python, as the desktop client's entry point sets them up.
    Write-Step "Imports with the engine's python"
    $importScript = Join-Path $sandbox "check_imports.py"
    $importCode = @'
import importlib
import os
import site
import sys

app = sys.argv[1]
site.addsitedir(os.path.join(app, "site-packages"))
sys.path.insert(0, app)
modules = [
    "horizon.proxy.server",
    "magika",
    "onnxruntime",
    "transformers",
    "litellm",
    "tiktoken",
    "orjson",
    "cryptography.hazmat.primitives.asymmetric.ed25519",
]
failed = []
for name in modules:
    try:
        importlib.import_module(name)
        print("ok    import " + name)
    except Exception as exc:
        failed.append(name)
        print("FAIL  import %s: %r" % (name, exc))
sys.exit(1 if failed else 0)
'@
    [IO.File]::WriteAllText($importScript, $importCode, (New-Object Text.UTF8Encoding $false))
    $importOutput = & $python -I -s -X utf8 $importScript (Join-Path $engine "app")
    $importOutput | ForEach-Object { Write-Output "    $_" }
    if ($LASTEXITCODE -ne 0) { throw "Some modules do not import in the engine's python" }

    # 5. Stop through the engine's own command.
    Write-Step "Stopping the engine"
    $stopOutput = & $horizon code-engine stop --port $Port
    $stopCode = $LASTEXITCODE
    $stopOutput | ForEach-Object { Write-Output "    $_" }
    $deadline = (Get-Date).AddSeconds(30)
    while ((Get-Date) -lt $deadline -and (Invoke-Local -Method GET -Url "http://127.0.0.1:$Port/__cs/health" -TimeoutMs 1000)) {
        Start-Sleep -Milliseconds 300
    }
    $stillAnswering = [bool](Invoke-Local -Method GET -Url "http://127.0.0.1:$Port/__cs/health" -TimeoutMs 1000)
    if ($stopCode -ne 0 -or $stillAnswering) {
        throw "The engine did not stop (exit $stopCode, still answering: $stillAnswering)"
    }
    Write-Output "ok    stopped"

    # 6. The background start: the bundle's pythonw.exe (GUI subsystem, so no console window)
    # runs the same CLI. It must answer on the port, and its process must have no window.
    Write-Step "Windowless start with python\pythonw.exe"
    $pythonw = Join-Path $engine "python\pythonw.exe"
    $appScript = Join-Path $engine "app\horizon_main.py"
    if (-not (Test-Path -LiteralPath $pythonw)) { throw "No python\pythonw.exe in $engine" }
    $windowlessProc = Start-Process -FilePath $pythonw -PassThru -WindowStyle Hidden `
        -ArgumentList @("-I", "-s", "-X", "utf8", $appScript, "code-engine", "run", "--port", "$Port")
    $deadline = (Get-Date).AddSeconds(120)
    while ((Get-Date) -lt $deadline -and -not (Invoke-Local -Method GET -Url "http://127.0.0.1:$Port/__cs/health" -TimeoutMs 1000)) {
        Start-Sleep -Milliseconds 300
    }
    if (-not (Invoke-Local -Method GET -Url "http://127.0.0.1:$Port/__cs/health" -TimeoutMs 1000)) {
        throw "The windowless engine did not answer on 127.0.0.1:$Port"
    }
    $windowlessProc.Refresh()
    if ($windowlessProc.MainWindowHandle -ne 0) { throw "The windowless engine has a window" }
    Write-Output "ok    windowless engine answers, no window"
    Start-Process -FilePath $pythonw -Wait -WindowStyle Hidden `
        -ArgumentList @("-I", "-s", "-X", "utf8", $appScript, "code-engine", "stop", "--port", "$Port") | Out-Null
    if (-not $windowlessProc.WaitForExit(60000)) { throw "The windowless engine did not stop" }
    Write-Output "ok    windowless engine stopped"
    Write-Output ""
    Write-Output ("SMOKE TEST PASSED for engine {0}: cold start {1:N1} s, {2:N1} MB unpacked in {3:N0} files" -f $expectedVersion, $coldStart, ($unpackedBytes / 1MB), $allFiles.Count)
}
finally {
    if ($engineProc -and -not $engineProc.HasExited) {
        Write-Output "Ending the test engine by its PID $($engineProc.Id)"
        Stop-Process -Id $engineProc.Id -Force -ErrorAction SilentlyContinue
    }
    if ($upstreamProc -and -not $upstreamProc.HasExited) {
        Stop-Process -Id $upstreamProc.Id -Force -ErrorAction SilentlyContinue
    }
    foreach ($name in $profileVars) { [Environment]::SetEnvironmentVariable($name, $saved[$name], "Process") }
    foreach ($name in $savedOther.Keys) { [Environment]::SetEnvironmentVariable($name, $savedOther[$name], "Process") }
    Start-Sleep -Milliseconds 500
    Remove-Item -LiteralPath $sandbox -Recurse -Force -ErrorAction SilentlyContinue
    # The run cached bytecode in the engine folder, as a first start does on a user's PC.
    # Remove it, so the engine that is packaged is the one that was built (sources only).
    Get-ChildItem -LiteralPath $engine -Recurse -Directory -Filter "__pycache__" -Force -ErrorAction SilentlyContinue |
        Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
}
