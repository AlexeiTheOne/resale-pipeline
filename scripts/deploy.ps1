<#
deploy.ps1 - pull-based auto-deploy for the eBay Telegram bot.

Run every few minutes by a scheduled task on the production PC. When origin/main
has a new commit it: tests it in a separate worktree, backs up data, waits until
the bot is idle, fast-forwards the live repo, restarts the bot and checks the
heartbeat. If the new code doesn't come up, it rolls the code back and restarts.

Usage:
  powershell -NoProfile -ExecutionPolicy Bypass -File scripts\deploy.ps1
  powershell -NoProfile -ExecutionPolicy Bypass -File scripts\deploy.ps1 -DryRun
  ... -Force        retry a commit that already failed tests or a deploy

  -DryRun  fetch, run the checks and the tests in staging, say what would happen.
           No backup, merge, restart, notification or state change.

Works on Windows PowerShell 5.1 and pwsh 7. Exit code 0 = nothing to do or
deployed; 1 = blocked, failed or rolled back.

Files it keeps (all under <repo>\logs, which git ignores):
  deploy.log         one timestamped line per step
  deploy.lock        pid of the running deploy (stale if that pid is dead)
  deploy-state.json  last alert sent (so a blocked state alerts once) and the
                     last failed sha (skipped until a newer commit, or -Force)

Never touches data\ross.db (backup.py only reads it), .env, other projects,
or any python process except the bot via restart_ebay_bot.ps1.
#>
[CmdletBinding()]
param(
    [string]$RepoPath = 'C:\Users\boris\Projects\ebay-list',
    [string]$StagingPath = 'C:\Users\boris\Projects\ebay-list-staging',
    [string]$RestartScript = 'C:\Users\boris\Projects\restart_ebay_bot.ps1',
    [string]$Branch = 'main',
    [int]$BusyWaitSeconds = 600,
    [int]$HealthWaitSeconds = 120,
    [int]$TestTimeoutSeconds = 900,
    [switch]$DryRun,
    [switch]$Force
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2

$Python   = Join-Path $RepoPath 'venv\Scripts\python.exe'
$LogDir   = Join-Path $RepoPath 'logs'
$LogFile  = Join-Path $LogDir 'deploy.log'
$LockFile = Join-Path $LogDir 'deploy.lock'
$StateFile = Join-Path $LogDir 'deploy-state.json'
$Heartbeat = Join-Path $LogDir 'heartbeat.json'
$BusyFile  = Join-Path $LogDir 'busy.json'
$ServiceLog = Join-Path $LogDir 'service.out.log'

# Emoji built from code points so this file stays ASCII (PS 5.1 reads BOM-less files as ANSI).
$IconOk   = [char]::ConvertFromUtf32(0x2705)
$IconFail = [char]::ConvertFromUtf32(0x274C)
$IconWarn = [char]::ConvertFromUtf32(0x26A0)

# Python children print UTF-8, and we decode it as UTF-8.
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }

# ---------------------------------------------------------------- helpers

function Write-Log([string]$Message) {
    $prefix = ''
    if ($DryRun) { $prefix = '[dry-run] ' }
    $line = '{0} {1}{2}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $prefix, $Message
    Write-Host $line
    if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir | Out-Null }
    Add-Content -Path $LogFile -Value $line -Encoding UTF8
}

# Run a native command; return @{Code; Out}. Stderr is merged into Out.
# EAP is relaxed here because PS 5.1 turns native stderr into terminating errors.
function Invoke-Native([string]$Exe, [string[]]$Arguments) {
    $ErrorActionPreference = 'Continue'
    $out = & $Exe @Arguments 2>&1 | ForEach-Object { "$_" }
    return @{ Code = $LASTEXITCODE; Out = (@($out) -join "`n") }
}

function Invoke-Git([string[]]$Arguments, [string]$Dir = $RepoPath) {
    return Invoke-Native 'git' (@('-C', $Dir) + $Arguments)
}

# Git command that must succeed; returns trimmed output.
function Git-Must([string[]]$Arguments, [string]$Dir = $RepoPath) {
    $r = Invoke-Git $Arguments $Dir
    if ($r.Code -ne 0) { throw "git $($Arguments -join ' ') failed ($($r.Code)): $($r.Out)" }
    return $r.Out.Trim()
}

function Get-Tail([string]$Text, [int]$Lines) {
    $all = @($Text -split "`r?`n")
    if ($all.Count -le $Lines) { return ($all -join "`n") }
    return ($all[($all.Count - $Lines)..($all.Count - 1)] -join "`n")
}

function Test-PidAlive($ProcessId) {
    if (-not $ProcessId) { return $false }
    return [bool](Get-Process -Id ([int]$ProcessId) -ErrorAction SilentlyContinue)
}

# Telegram message to the owner via scripts\notify.py. Never fatal.
function Send-Notify([string]$Text) {
    if ($DryRun) { Write-Log "would notify: $Text"; return }
    $notify = Join-Path $RepoPath 'scripts\notify.py'
    if (-not (Test-Path $notify)) { Write-Log "notify.py missing; message not sent: $Text"; return }
    if ($Text.Length -gt 3500) { $Text = $Text.Substring(0, 3500) + "`n..." }
    # PS 5.1 mangles embedded double quotes in native arguments.
    $Text = $Text.Replace('"', "'")
    Push-Location $RepoPath
    try { $r = Invoke-Native $Python @($notify, $Text) } finally { Pop-Location }
    if ($r.Code -ne 0) { Write-Log "notify failed ($($r.Code)): $(Get-Tail $r.Out 3)" }
}

# ---------------------------------------------------------------- state

function Get-State {
    # alert = last alert key sent; failed = sha not to retry; passed = sha whose tests passed
    $s = @{ alert = ''; failed = ''; passed = '' }
    if (Test-Path $StateFile) {
        try {
            $j = Get-Content $StateFile -Raw | ConvertFrom-Json
            foreach ($p in $j.PSObject.Properties) {
                if ($s.ContainsKey($p.Name) -and $p.Value) { $s[$p.Name] = [string]$p.Value }
            }
        } catch { Write-Log "state file unreadable, starting fresh: $_" }
    }
    return $s
}

function Save-State($State) {
    if ($DryRun) { return }
    (New-Object psobject -Property $State) | ConvertTo-Json | Set-Content -Path $StateFile -Encoding UTF8
}

# Alert once per distinct blocked state (key), then stay quiet until it changes.
function Send-AlertOnce($State, [string]$Key, [string]$Text) {
    if ($State.alert -eq $Key) { return }
    Write-Log $Text
    Send-Notify $Text
    $State.alert = $Key
    Save-State $State
}

# ---------------------------------------------------------------- lock

function Enter-Lock {
    if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir | Out-Null }
    for ($i = 0; $i -lt 2; $i++) {
        try {
            # CreateNew fails if the file exists, so two runs can't both get it.
            $fs = [System.IO.File]::Open($LockFile, 'CreateNew', 'Write', 'None')
            $bytes = [System.Text.Encoding]::ASCII.GetBytes("$PID")
            $fs.Write($bytes, 0, $bytes.Length)
            $fs.Close()
            return $true
        } catch {
            if (-not (Test-Path $LockFile)) { throw }   # some other I/O problem
            $other = (Get-Content $LockFile -Raw -ErrorAction SilentlyContinue)
            if ($other) { $other = $other.Trim() }
            if ($other -match '^\d+$' -and (Test-PidAlive $other)) { return $false }
            Write-Log "removing stale lock (pid $other)"
            Remove-Item $LockFile -Force -ErrorAction SilentlyContinue
        }
    }
    return $false
}

function Exit-Lock {
    $owner = (Get-Content $LockFile -Raw -ErrorAction SilentlyContinue)
    if ($owner -and $owner.Trim() -eq "$PID") { Remove-Item $LockFile -Force -ErrorAction SilentlyContinue }
}

# ---------------------------------------------------------------- steps

# Tests the commit in the staging worktree. Returns @{Ok; Out}.
function Test-InStaging([string]$Sha) {
    if (Test-Path $StagingPath) {
        if (-not (Test-Path (Join-Path $StagingPath '.git'))) {
            throw "$StagingPath exists but is not a git worktree; remove it by hand."
        }
        Git-Must @('checkout', '--detach', '--force', $Sha) $StagingPath | Out-Null
        Git-Must @('clean', '-ffdx') $StagingPath | Out-Null
    } else {
        Git-Must @('worktree', 'prune') | Out-Null
        Git-Must @('worktree', 'add', '--detach', $StagingPath, $Sha) | Out-Null
    }

    $probe = Invoke-Native $Python @('-c', 'import pytest')
    if ($probe.Code -ne 0) {
        throw "pytest is not installed in $Python. Install it once by hand: venv\Scripts\python.exe -m pip install pytest"
    }

    # Dummy keys for the test process only; restored afterwards.
    $keys = 'GEMINI_API_KEY', 'GOOGLE_API_KEY', 'ANTHROPIC_API_KEY'
    $saved = @{}
    foreach ($k in $keys) {
        $saved[$k] = [Environment]::GetEnvironmentVariable($k, 'Process')
        [Environment]::SetEnvironmentVariable($k, 'dummy', 'Process')
    }
    $outFile = Join-Path $LogDir 'deploy-pytest.out'
    $errFile = Join-Path $LogDir 'deploy-pytest.err'
    try {
        $p = Start-Process -FilePath $Python -ArgumentList '-m', 'pytest', '-q' `
            -WorkingDirectory $StagingPath -NoNewWindow -PassThru `
            -RedirectStandardOutput $outFile -RedirectStandardError $errFile
        $null = $p.Handle   # keeps ExitCode readable after exit (PS 5.1 quirk)
        if (-not $p.WaitForExit($TestTimeoutSeconds * 1000)) {
            $p.Kill()   # only this pytest process
            $p.WaitForExit()
            return @{ Ok = $false; Out = "pytest timed out after $TestTimeoutSeconds s" }
        }
        $p.WaitForExit()
        $out = (Get-Content $outFile -Raw -ErrorAction SilentlyContinue) + "`n" +
               (Get-Content $errFile -Raw -ErrorAction SilentlyContinue)
        return @{ Ok = ($p.ExitCode -eq 0); Out = $out.Trim() }
    } finally {
        foreach ($k in $keys) { [Environment]::SetEnvironmentVariable($k, $saved[$k], 'Process') }
    }
}

function Invoke-Backup {
    Push-Location $RepoPath
    try { $r = Invoke-Native $Python @('backup.py') } finally { Pop-Location }
    return $r
}

# Waits until busy.json is gone or stale. True when idle.
function Wait-Idle {
    $deadline = (Get-Date).AddSeconds($BusyWaitSeconds)
    while ($true) {
        if (-not (Test-Path $BusyFile)) { return $true }
        $busyPid = $null
        try { $busyPid = (Get-Content $BusyFile -Raw | ConvertFrom-Json).pid } catch { }
        if (-not (Test-PidAlive $busyPid)) {
            Write-Log "busy.json is stale (pid $busyPid not running); treating as idle"
            return $true
        }
        if ((Get-Date) -ge $deadline) { return $false }
        Start-Sleep -Seconds 15
    }
}

function Install-Requirements {
    Push-Location $RepoPath
    try { $r = Invoke-Native $Python @('-m', 'pip', 'install', '-q', '-r', 'requirements.txt') } finally { Pop-Location }
    if ($r.Code -ne 0) { Write-Log "pip install failed: $(Get-Tail $r.Out 10)" }
    return ($r.Code -eq 0)
}

# Kills only telegram_bot.py (the restart script's job); the loop restarts it.
function Restart-Bot {
    $hostExe = (Get-Process -Id $PID).Path
    $r = Invoke-Native $hostExe @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $RestartScript)
    Write-Log "restart script exit $($r.Code): $(Get-Tail $r.Out 3)"
}

function ConvertTo-UtcTime($Value) {
    # pwsh 7 ConvertFrom-Json already turns ISO strings into DateTime; 5.1 leaves a string.
    if ($Value -is [datetime]) { return $Value.ToUniversalTime() }
    return ([DateTimeOffset]::Parse([string]$Value)).UtcDateTime
}

# True when a bot started after $Since reports $Sha and stays up ~20s.
function Wait-Healthy([string]$Sha, [datetime]$Since) {
    $deadline = (Get-Date).AddSeconds($HealthWaitSeconds)
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Seconds 5
        if (-not (Test-Path $Heartbeat)) { continue }
        try { $hb = Get-Content $Heartbeat -Raw | ConvertFrom-Json } catch { continue }
        if ($hb.commit -ne $Sha) { continue }
        if ((ConvertTo-UtcTime $hb.started_at) -le $Since) { continue }
        if (-not (Test-PidAlive $hb.pid)) { continue }
        Start-Sleep -Seconds 20   # catch a crash right after startup
        if (Test-PidAlive $hb.pid) { return $true }
    }
    return $false
}

function Get-ServiceTail {
    if (-not (Test-Path $ServiceLog)) { return '(no service.out.log)' }
    return ((Get-Content $ServiceLog -Tail 20) -join "`n")
}

# ---------------------------------------------------------------- main

function Invoke-Deploy {
    $state = Get-State

    $fetch = Invoke-Git @('fetch', '--quiet', 'origin', $Branch)
    if ($fetch.Code -ne 0) { Write-Log "git fetch failed: $($fetch.Out)"; return 1 }

    $old = Git-Must @('rev-parse', 'HEAD')
    $new = Git-Must @('rev-parse', "origin/$Branch")

    # Nothing new: stay silent so the log isn't spammed every few minutes.
    if ($old -eq $new) {
        if ($state.alert) { $state.alert = ''; Save-State $state }
        return 0
    }
    if ($state.failed -eq $new -and -not $Force) { return 0 }

    $short = $new.Substring(0, 7)

    # Refuse to deploy over local edits or local commits on prod.
    $dirty = Git-Must @('status', '--porcelain', '--untracked-files=no')
    if ($dirty) {
        Send-AlertOnce $state "dirty:${old}:${new}" "$IconWarn Deploy of $short blocked: live repo has uncommitted changes:`n$(Get-Tail $dirty 15)"
        return 1
    }
    $anc = Invoke-Git @('merge-base', '--is-ancestor', $old, $new)
    if ($anc.Code -ne 0) {
        Send-AlertOnce $state "diverged:${old}:${new}" "$IconWarn Deploy of $short blocked: live HEAD $($old.Substring(0,7)) is not an ancestor of origin/$Branch (commits made on prod?)."
        return 1
    }

    # Tests run once per sha; a run that then waits on busy/backup won't retest.
    if ($state.passed -ne $new -or $DryRun) {
        Write-Log "new commit $short (live at $($old.Substring(0,7))); testing in $StagingPath"
        $test = Test-InStaging $new
        if (-not $test.Ok) {
            Write-Log "tests FAILED for $short"
            Send-Notify "$IconFail Tests failed for $short, not deploying.`n$(Get-Tail $test.Out 25)"
            $state.failed = $new; Save-State $state
            return 1
        }
        Write-Log "tests passed for $short"
        $state.passed = $new; Save-State $state
    }

    $subjects = Git-Must @('log', '--format=%h %s', '--max-count=10', "$old..$new")
    $count = [int](Git-Must @('rev-list', '--count', "$old..$new"))
    $reqChanged = (Invoke-Git @('diff', '--quiet', $old, $new, '--', 'requirements.txt')).Code -ne 0

    if ($DryRun) {
        Write-Log "would deploy $count commit(s), requirements changed: $reqChanged`n$subjects"
        return 0
    }

    $bk = Invoke-Backup
    if ($bk.Code -ne 0) {
        Write-Log "backup FAILED: $($bk.Out)"
        Send-AlertOnce $state "backup:$new" "$IconFail Deploy of $short stopped: backup failed.`n$(Get-Tail $bk.Out 10)"
        return 1
    }
    Write-Log "backup ok: $(Get-Tail $bk.Out 3)"

    if (-not (Wait-Idle)) {
        Write-Log "bot still busy after $BusyWaitSeconds s; will retry next run"
        return 0
    }

    # Deploy.
    $merge = Invoke-Git @('merge', '--ff-only', $new)
    if ($merge.Code -ne 0) {
        Write-Log "merge failed: $($merge.Out)"
        Send-Notify "$IconFail Deploy of $short failed at git merge; live code unchanged.`n$(Get-Tail $merge.Out 10)"
        $state.failed = $new; Save-State $state
        return 1
    }
    Write-Log "fast-forwarded live to $short"

    $ok = $true
    if ($reqChanged) {
        Write-Log 'requirements.txt changed; pip install'
        $ok = Install-Requirements
    }
    if ($ok) {
        $since = (Get-Date).ToUniversalTime()
        Restart-Bot
        $ok = Wait-Healthy $new $since
    }

    if ($ok) {
        Write-Log "deployed $short"
        $more = ''
        if ($count -gt 10) { $more = "`n(+$($count - 10) more)" }
        $subject = Git-Must @('log', '-1', '--format=%s', $new)
        Send-Notify "$IconOk Deployed ${short}: $subject`n`n$subjects$more"
        $state.alert = ''; $state.failed = ''; Save-State $state
        return 0
    }

    # Roll back code only: data\ and .env are gitignored, reset --hard leaves them alone.
    Write-Log "deploy of $short unhealthy; rolling back to $($old.Substring(0,7))"
    Git-Must @('reset', '--hard', $old) | Out-Null
    if ($reqChanged) { Install-Requirements | Out-Null }
    $since = (Get-Date).ToUniversalTime()
    Restart-Bot
    $back = Wait-Healthy $old $since
    $status = 'bot is back up'
    if (-not $back) { $status = 'BOT DID NOT COME BACK UP - check the PC' }
    Write-Log "rollback done; $status"
    Send-Notify "$IconFail Deploy of $short failed, rolled back to $($old.Substring(0,7)) ($status).`n`nservice.out.log:`n$(Get-ServiceTail)"
    $state.failed = $new; Save-State $state
    return 1
}

# ---------------------------------------------------------------- entry

if (-not (Test-Path $Python)) { Write-Log "python not found at $Python"; exit 1 }
if (-not (Enter-Lock)) { exit 0 }   # another deploy is running
$code = 1
try {
    $code = Invoke-Deploy
} catch {
    # Alert once per distinct error so a persistent problem doesn't page every run.
    $err = "$_"
    Write-Log "deploy error: $err"
    try { Send-AlertOnce (Get-State) "error:$err" "$IconFail Deploy script error: $err" } catch { Write-Log "could not alert: $_" }
    $code = 1
} finally {
    Exit-Lock
}
exit $code
