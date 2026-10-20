<#
.SYNOPSIS
Live test of the contextshrink npx installer on this PC, in a throwaway Claude Code config folder.

.DESCRIPTION
Runs the real connect, one headless Claude Code request, status, and then disconnect
or logout. Claude Code reads and writes only a new folder under %TEMP%
(CLAUDE_CONFIG_DIR). The child processes also get HOME and USERPROFILE pointing at a
folder inside it, so ~/.contextshrink is isolated too. The real ~/.claude is never
written: its settings.json hash is recorded before the run and compared at the end.
LOCALAPPDATA, APPDATA and the ContextShrink data folder point into the test folder as
well, so connect's Code tab part finds no real Claude Desktop folder and patches nothing.

You type your email and password at the connect prompt yourself. This script never
reads, logs or echoes them, and it never prints a key, a header or a settings value.

.PARAMETER DryRun
Runs the environment isolation and prerequisite checks only. It skips connect, sign-in,
the headless request and the cleanup, so it sends nothing and changes no settings.

.EXAMPLE
powershell -NoProfile -ExecutionPolicy Bypass -File .\run-live-test.ps1
#>
[CmdletBinding()]
param(
    [switch]$DryRun
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

$ProxyHost = 'proxy.contextshrink.com'
$HeadlessPrompt = 'Reply with the single word: pong'
$TestNamePrefix = 'cs-npx-e2e-'

function Show-Step([string]$Text) {
    Write-Host ''
    Write-Host ('== ' + $Text) -ForegroundColor Cyan
}

function Show-Ok([string]$Text) {
    Write-Host ('   ok: ' + $Text) -ForegroundColor Green
}

function Show-Note([string]$Text) {
    Write-Host ('   note: ' + $Text) -ForegroundColor Yellow
}

function Show-Fail([string]$Text) {
    Write-Host ('   FAIL: ' + $Text) -ForegroundColor Red
}

# Reads a property without failing under strict mode when it is missing.
function Get-Prop($Object, [string]$Name) {
    if ($null -eq $Object) { return $null }
    $prop = $Object.PSObject.Properties[$Name]
    if ($null -eq $prop) { return $null }
    return $prop.Value
}

# SHA-256 of a file, or the word "absent". The file is hashed, never printed.
function Get-FileState([string]$Path) {
    if (Test-Path -LiteralPath $Path -PathType Leaf) {
        return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash
    }
    return 'absent'
}

# Environment changes are made on this process only (Process scope). They are
# inherited by the child processes this script starts, and never reach the
# user's global or parent-shell environment.
$script:Original = @{}

function Set-ProcessEnv([string]$Name, [string]$Value) {
    if (-not $script:Original.ContainsKey($Name)) {
        $script:Original[$Name] = [Environment]::GetEnvironmentVariable($Name, 'Process')
    }
    [Environment]::SetEnvironmentVariable($Name, $Value, 'Process')
}

function Restore-ProcessEnv {
    foreach ($name in @($script:Original.Keys)) {
        [Environment]::SetEnvironmentVariable($name, $script:Original[$name], 'Process')
    }
    $script:Original = @{}
}

# Removes Claude Code variables inherited from the parent shell, so that
# the child processes do not pick up the real proxy, keys or settings. Returns
# the names removed (names only, never values).
function Clear-InheritedClaudeEnv {
    $names = @(
        'ANTHROPIC_BASE_URL',
        'ANTHROPIC_CUSTOM_HEADERS',
        'ENABLE_TOOL_SEARCH',
        'ANTHROPIC_API_KEY',
        'ANTHROPIC_AUTH_TOKEN',
        'CLAUDECODE'
    )
    $all = [Environment]::GetEnvironmentVariables('Process')
    $removed = @()
    foreach ($key in @($all.Keys)) {
        $name = [string]$key
        if (($names -contains $name) -or ($name -like 'CLAUDE_CODE_*')) {
            $removed += $name
        }
    }
    foreach ($name in $removed) {
        [Environment]::SetEnvironmentVariable($name, $null, 'Process')
    }
    return ,$removed
}

# Deletes a folder only when it is one of this script's test folders under %TEMP%.
function Remove-TestFolder([string]$Folder) {
    $tempRoot = [System.IO.Path]::GetFullPath($env:TEMP).TrimEnd('\')
    $full = [System.IO.Path]::GetFullPath($Folder)
    $leaf = Split-Path -Leaf $full
    if (($full.StartsWith($tempRoot + '\')) -and ($leaf.StartsWith($TestNamePrefix))) {
        Remove-Item -LiteralPath $full -Recurse -Force
        return $true
    }
    return $false
}

function Read-YesNo([string]$Question) {
    $answer = Read-Host $Question
    return ($answer.Trim() -match '^(y|yes)$')
}

# Returns the env block of the isolated settings.json, or $null when there is no file.
function Get-IsolatedEnv([string]$SettingsFile) {
    if (-not (Test-Path -LiteralPath $SettingsFile -PathType Leaf)) { return $null }
    $payload = Get-Content -Raw -LiteralPath $SettingsFile | ConvertFrom-Json
    return (Get-Prop $payload 'env')
}

# 'yes', 'no' or 'unknown', from claude auth status --json. Only the flag is read.
function Get-ClaudeLoginState {
    $raw = & claude auth status --json 2>$null
    $text = ($raw -join "`n")
    try {
        $status = $text | ConvertFrom-Json
    } catch {
        return 'unknown'
    }
    $flag = Get-Prop $status 'loggedIn'
    if ($flag -eq $true) { return 'yes' }
    if ($flag -eq $false) { return 'no' }
    return 'unknown'
}

$exitCode = 0
$connectStarted = $false
$cleanedUp = $false
$folderKept = $true

# The real Claude Code folder is only read: a hash before and after, and whether
# the desktop app owns the connection. Its contents are never printed.
$realClaudeDir = Join-Path $env:USERPROFILE '.claude'
$realSettings = Join-Path $realClaudeDir 'settings.json'
$realHashBefore = Get-FileState $realSettings
$realDesktopApp = Test-Path -LiteralPath (Join-Path $realClaudeDir '.horizon-vscode-claude.json') -PathType Leaf

$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$root = Join-Path $env:TEMP ($TestNamePrefix + $stamp)
$tempHome = Join-Path $root 'home'
$isolatedSettings = Join-Path $root 'settings.json'
$cli = [System.IO.Path]::GetFullPath((Join-Path (Join-Path $PSScriptRoot '..') (Join-Path 'bin' 'contextshrink.js')))

try {
    Show-Step 'Step 1: isolated folder and environment'
    New-Item -ItemType Directory -Path $tempHome -Force | Out-Null
    Show-Ok ('test folder: ' + $root)

    Set-ProcessEnv 'CLAUDE_CONFIG_DIR' $root
    Set-ProcessEnv 'HOME' $tempHome
    Set-ProcessEnv 'USERPROFILE' $tempHome
    # The Code tab part of connect reads these folders. Pointing them into the test
    # folder means no real Claude Desktop folder can be found or patched by this run.
    Set-ProcessEnv 'LOCALAPPDATA' (Join-Path $root 'LocalAppData')
    Set-ProcessEnv 'APPDATA' (Join-Path $root 'AppData')
    Set-ProcessEnv 'CONTEXTSHRINK_DATA_DIR' (Join-Path $root 'ContextShrinkData')
    $removed = Clear-InheritedClaudeEnv
    if ($removed.Count -gt 0) {
        Show-Note ('removed for the test processes only: ' + ($removed -join ', '))
    } else {
        Show-Ok 'no Claude Code variables were inherited'
    }

    $probe = "var e=process.env;var r={config_dir_is_test:e.CLAUDE_CONFIG_DIR===process.argv[1],home_is_test:e.USERPROFILE===process.argv[2],local_appdata_is_test:String(e.LOCALAPPDATA).startsWith(process.argv[1]),appdata_is_test:String(e.APPDATA).startsWith(process.argv[1]),base_url_inherited:('ANTHROPIC_BASE_URL' in e),headers_inherited:('ANTHROPIC_CUSTOM_HEADERS' in e),tool_search_inherited:('ENABLE_TOOL_SEARCH' in e)};console.log(JSON.stringify(r));"
    $probeRaw = & node -e $probe $root $tempHome
    $seen = ($probeRaw -join '') | ConvertFrom-Json
    if ((Get-Prop $seen 'config_dir_is_test') -ne $true) { throw 'A child process does not see the test CLAUDE_CONFIG_DIR.' }
    if ((Get-Prop $seen 'home_is_test') -ne $true) { throw 'A child process does not see the test HOME folder.' }
    if (((Get-Prop $seen 'local_appdata_is_test') -ne $true) -or ((Get-Prop $seen 'appdata_is_test') -ne $true)) {
        throw 'A child process sees the real LOCALAPPDATA or APPDATA, so the Code tab could reach Claude Desktop.'
    }
    if ((Get-Prop $seen 'base_url_inherited') -or (Get-Prop $seen 'headers_inherited') -or (Get-Prop $seen 'tool_search_inherited')) {
        throw 'A ContextShrink or tool search variable still reaches the child processes.'
    }
    Show-Ok 'child processes use the test folder, and no proxy or tool search variable is inherited'

    Show-Step 'Step 2: prerequisites'
    $nodeVersion = ([string](& node --version)).Trim()
    $nodeMajor = [int]($nodeVersion.TrimStart('v').Split('.')[0])
    if ($nodeMajor -lt 18) { throw ('Node ' + $nodeVersion + ' is too old. contextshrink needs Node 18 or newer.') }
    Show-Ok ('Node ' + $nodeVersion)

    $claudeCommand = Get-Command claude -ErrorAction SilentlyContinue
    if ($null -eq $claudeCommand) { throw 'The claude command is not on PATH. Install Claude Code, then run this again.' }
    $claudeVersion = [string](& claude --version 2>$null | Select-Object -First 1)
    Show-Ok ('claude on PATH: ' + $claudeVersion.Trim())

    if (-not (Test-Path -LiteralPath $cli -PathType Leaf)) { throw ('contextshrink CLI not found at ' + $cli) }
    Show-Ok 'contextshrink CLI found in the repository'

    if ($realDesktopApp) {
        Show-Note 'The ContextShrink desktop app manages the real ~/.claude connection. This test does not touch it.'
    }
    if ($realHashBefore -eq 'absent') {
        Show-Note 'The real ~/.claude has no settings.json. It must still be absent at the end.'
    } else {
        Show-Ok 'real ~/.claude settings.json recorded (not read or printed)'
    }

    if ($DryRun) {
        Show-Step 'Dry run: steps 3 to 7 skipped (no connect, no sign-in, no request, no cleanup)'
        Show-Ok 'dry run checks passed'
    } else {
        Show-Step 'Step 3: connect (you type your email and password at the prompt; this script does not read them)'
        $connectStarted = $true
        & node $cli connect
        if ($LASTEXITCODE -ne 0) { throw ('connect exited with code ' + $LASTEXITCODE + '. Nothing else was run.') }
        Show-Ok 'connect finished'

        Show-Step 'Step 4: Claude Code sign-in in the test folder'
        $login = Get-ClaudeLoginState
        if ($login -ne 'yes') {
            Show-Note 'This test folder has no Claude Code login yet (the real login is in another folder).'
            if (Read-YesNo 'Run claude auth login now? It opens your browser, where you sign in yourself [y/N]') {
                & claude auth login
            }
            $login = Get-ClaudeLoginState
        }
        if ($login -eq 'yes') {
            Show-Ok 'Claude Code is signed in in the test folder'
        } else {
            Show-Note 'Could not confirm the login. If the next step reports an authentication error, run "claude auth login" and try again.'
        }

        Show-Step 'Step 5: one headless request (output is reduced to a few facts)'
        Push-Location $root
        try {
            $rawOutput = & claude -p $HeadlessPrompt --output-format json 2>$null
            $headlessCode = $LASTEXITCODE
        } finally {
            Pop-Location
        }
        $text = ($rawOutput -join "`n")
        $result = $null
        try {
            $result = $text | ConvertFrom-Json
        } catch {
            $result = $null
        }
        if ($result -is [array]) {
            $result = $result | Where-Object { (Get-Prop $_ 'type') -eq 'result' } | Select-Object -Last 1
        }

        if ($null -eq $result) {
            Show-Fail ('the output was not JSON (exit code ' + $headlessCode + '). The output is not shown.')
        } else {
            $isError = Get-Prop $result 'is_error'
            $replyHasPong = ([string](Get-Prop $result 'result')) -match '(?i)pong'
            $succeeded = ($headlessCode -eq 0) -and ($isError -ne $true) -and $replyHasPong
            if ($succeeded) {
                Show-Ok 'request succeeded'
            } else {
                Show-Fail ('request failed (exit code ' + $headlessCode + ', subtype ' + [string](Get-Prop $result 'subtype') + ')')
            }
            Write-Host ('   reply check, the word pong is in the reply: ' + $(if ($replyHasPong) { 'yes' } else { 'no' }))

            $modelUsage = Get-Prop $result 'modelUsage'
            $models = @()
            if ($null -ne $modelUsage) {
                $models = @($modelUsage.PSObject.Properties | ForEach-Object { $_.Name })
            }
            $modelText = 'unknown'
            if ($models.Count -gt 0) { $modelText = $models -join ', ' }
            Write-Host ('   model: ' + $modelText)

            $usage = Get-Prop $result 'usage'
            Write-Host ('   input tokens: ' + [string](Get-Prop $usage 'input_tokens'))
            Write-Host ('   output tokens: ' + [string](Get-Prop $usage 'output_tokens'))
            $cost = Get-Prop $result 'total_cost_usd'
            if ($null -ne $cost) { Write-Host ('   total cost (USD): ' + [string]$cost) }
        }

        Show-Step 'Step 6: the test folder settings (values are not printed)'
        $envBlock = Get-IsolatedEnv $isolatedSettings
        if ($null -eq $envBlock) {
            Show-Fail 'the test folder has no settings.json'
        } else {
            $baseUrl = [string](Get-Prop $envBlock 'ANTHROPIC_BASE_URL')
            $baseHost = ''
            try { $baseHost = ([System.Uri]$baseUrl).Host } catch { $baseHost = '' }
            if ($baseHost -eq $ProxyHost) {
                Show-Ok ('ANTHROPIC_BASE_URL points at ' + $ProxyHost)
            } else {
                Show-Fail ('ANTHROPIC_BASE_URL does not point at ' + $ProxyHost + ' (host: ' + $baseHost + ')')
            }

            $headerText = [string](Get-Prop $envBlock 'ANTHROPIC_CUSTOM_HEADERS')
            if ($headerText.StartsWith('X-Horizon-Proxy-Token: ')) {
                Show-Ok 'ANTHROPIC_CUSTOM_HEADERS holds the account key header'
            } else {
                Show-Fail 'ANTHROPIC_CUSTOM_HEADERS is missing or has the wrong shape'
            }

            if ([string](Get-Prop $envBlock 'ENABLE_TOOL_SEARCH') -eq 'true') {
                Show-Ok 'ENABLE_TOOL_SEARCH is true'
            } else {
                Show-Fail 'ENABLE_TOOL_SEARCH is not true'
            }
        }
        & node $cli status

        Show-Step 'Step 7: clean up'
        $doLogout = Read-YesNo 'Run contextshrink logout? It revokes this computer key and asks for your password again. Answer N to only disconnect [y/N]'
        if ($doLogout) {
            & node $cli logout
        } else {
            & node $cli disconnect
        }
        $cleanedUp = $true
        if ($LASTEXITCODE -ne 0) { throw ('cleanup exited with code ' + $LASTEXITCODE) }

        $leftover = @()
        $afterEnv = Get-IsolatedEnv $isolatedSettings
        if ($null -ne $afterEnv) {
            foreach ($name in @('ANTHROPIC_BASE_URL', 'ANTHROPIC_CUSTOM_HEADERS', 'ENABLE_TOOL_SEARCH')) {
                if ($null -ne (Get-Prop $afterEnv $name)) { $leftover += $name }
            }
        }
        if ($leftover.Count -eq 0) {
            Show-Ok 'the test folder settings no longer hold the three keys'
        } else {
            Show-Fail ('still present in the test folder: ' + ($leftover -join ', '))
            $exitCode = 1
        }

        if (Read-YesNo ('Delete the test folder now? It may still hold your Claude Code login. [y/N]')) {
            if (Remove-TestFolder $root) {
                $folderKept = $false
                Show-Ok 'test folder deleted'
            } else {
                Show-Fail 'the test folder path did not match the expected test folder, so nothing was deleted'
            }
        }

        Show-Step 'Step 8: check the dashboard'
        Write-Host '   Open the ContextShrink dashboard and find the newest request, from the last few minutes.'
        Write-Host '   Its agent should be claude-code.'
        Write-Host '   Its Transforms should NOT contain router:tool_search_deferral.'
    }
} catch {
    $exitCode = 1
    Show-Fail $_.Exception.Message
} finally {
    if ($connectStarted -and (-not $cleanedUp) -and (Test-Path -LiteralPath $cli -PathType Leaf)) {
        Show-Note 'Restoring the test folder settings after the failure (disconnect needs no password).'
        try {
            & node $cli disconnect
        } catch {
            Show-Fail 'could not restore the test folder. Run the disconnect command again, or delete the folder.'
        }
    }

    $realHashAfter = Get-FileState $realSettings
    Show-Step 'Real Claude Code folder check'
    if ($realHashAfter -eq $realHashBefore) {
        Show-Ok 'the real ~/.claude settings.json is unchanged'
    } else {
        Show-Fail 'the real ~/.claude settings.json CHANGED during this run. Tell the developer before doing anything else.'
        $exitCode = 1
    }

    if ($DryRun) {
        if (Remove-TestFolder $root) { $folderKept = $false }
    }

    Restore-ProcessEnv

    if ($folderKept -and (Test-Path -LiteralPath $root)) {
        Show-Note ('test folder kept: ' + $root + '. Delete it when you are done with it.')
    }
}

exit $exitCode
