<#
Read-only status of the live bot. Runs on the production PC; from a laptop:
  ssh boris@100.73.116.76 "powershell -NoProfile -ExecutionPolicy Bypass -File C:\Users\boris\Projects\ebay-list\scripts\prod_status.ps1 [-Logs 40] [-Errors]"

Default: deploy status (live commit vs GitHub, heartbeat, busy, bot process,
deploy task, last deploy log lines, remembered failures).
-Logs N  : also the last N lines of logs\bot.log.
-Errors  : also the last 30 error/warning lines from logs\bot.log and the
           tail of logs\service.out.log.

Changes nothing: no fetch into the working tree, no restart, no writes.
#>
param(
    [string]$RepoPath = 'C:\Users\boris\Projects\ebay-list',
    [int]$Logs = 0,
    [switch]$Errors
)
$ErrorActionPreference = 'Continue'
$log = Join-Path $RepoPath 'logs'

function Show($title) { Write-Output ''; Write-Output "== $title" }
function Read-Json($path) {
    if (Test-Path $path) { try { return Get-Content $path -Raw | ConvertFrom-Json } catch { } }
    return $null
}

# Finding the bot's processes. The bot runs as an S4U task, so from a normal
# login its command line is hidden: searching command lines for telegram_bot.py
# found 0 processes while the bot was running. Same rules as
# C:\Users\boris\Projects\restart_ebay_bot.ps1:
#   1. visible command line running telegram_bot.py from <RepoPath>\venv
#      (the deploy task sees command lines), plus its interpreter child;
#   2. the PID in heartbeat.json, if that process started before the
#      heartbeat was written (a reused PID always starts after), plus its
#      python parent (the venv launcher).
# Dev bots (resale-dev) are never counted.

function ConvertTo-UtcTime($Value) {
    if ($Value -is [datetime]) { return $Value.ToUniversalTime() }
    return ([DateTimeOffset]::Parse([string]$Value)).UtcDateTime
}

function Get-Utc($p) { return ([datetime]$p.CreationDate).ToUniversalTime() }

function Test-Dev($p) {
    foreach ($s in @($p.CommandLine, $p.ExecutablePath)) {
        if ($s -and $s -match '[\\/]resale-dev[\\/]') { return $true }
    }
    return $false
}

# 'running', 'gone' (no such PID) or 'reused' (the PID belongs to a process
# that started after the heartbeat was written).
function Get-HeartbeatState($Procs, $Hb) {
    if (-not $Hb -or -not $Hb.pid) { return 'gone' }
    $p = @($Procs | Where-Object { [int]$_.ProcessId -eq [int]$Hb.pid })[0]
    if (-not $p) { return 'gone' }
    if ($Hb.started_at) {
        # started_at is cut to whole seconds, hence the 2 s slack.
        $written = ConvertTo-UtcTime $Hb.started_at
        if ((Get-Utc $p) -gt $written.AddSeconds(2)) { return 'reused' }
    }
    return 'running'
}

# Returns @{Id; Why} for each production bot process.
function Find-BotProcesses($Procs, $Hb, [string]$RepoPath) {
    $byId = @{}
    foreach ($p in $Procs) { $byId[[int]$p.ProcessId] = $p }
    $found = [ordered]@{}
    $venv = $RepoPath.TrimEnd('\') + '\venv\'

    # Rule 1: visible launcher from the live venv, plus its children.
    foreach ($p in $Procs) {
        if (-not $p.CommandLine -or $p.CommandLine -notmatch 'telegram_bot\.py') { continue }
        if (-not $p.ExecutablePath -or
            -not $p.ExecutablePath.StartsWith($venv, [StringComparison]::OrdinalIgnoreCase)) { continue }
        foreach ($c in $Procs) {
            if ($c.ParentProcessId -eq $p.ProcessId -and (Get-Utc $c) -ge (Get-Utc $p)) {
                $found[[string]$c.ProcessId] = 'interpreter'
            }
        }
        if (-not $found.Contains([string]$p.ProcessId)) { $found[[string]$p.ProcessId] = 'launcher' }
    }

    # Rule 2: the heartbeat PID, if it is the process that wrote the heartbeat.
    if ((Get-HeartbeatState $Procs $Hb) -eq 'running') {
        $p = $byId[[int]$Hb.pid]
        if (-not $found.Contains([string]$p.ProcessId)) { $found[[string]$p.ProcessId] = 'heartbeat pid' }
        $par = $byId[[int]$p.ParentProcessId]
        if ($par -and $par.Name -like 'python*' -and (Get-Utc $par) -le (Get-Utc $p) -and
            -not $found.Contains([string]$par.ProcessId)) {
            $found[[string]$par.ProcessId] = 'launcher'
        }
    }

    $out = @()
    foreach ($k in $found.Keys) {
        $p = $byId[[int]$k]
        $par = $byId[[int]$p.ParentProcessId]
        if ((Test-Dev $p) -or ($par -and (Test-Dev $par))) { continue }
        $out += @{ Id = [int]$k; Why = $found[$k] }
    }
    return ,$out
}

function Format-Age([timespan]$t) {
    if ($t.TotalDays -ge 1) { return '{0}d {1}h' -f [int][math]::Floor($t.TotalDays), $t.Hours }
    if ($t.TotalHours -ge 1) { return '{0}h {1}m' -f [int][math]::Floor($t.TotalHours), $t.Minutes }
    return '{0}m' -f [int][math]::Floor($t.TotalMinutes)
}

if ($MyInvocation.InvocationName -eq '.') { return }   # dot-sourced by a test

Show 'Code'
$live = (git -C $RepoPath rev-parse --short HEAD 2>$null)
# Through the live copy's own remote: it uses the read-only deploy key, so this
# works from any session once the repo is private (no stored HTTPS login).
$remote = (git -C $RepoPath ls-remote origin refs/heads/main 2>$null)
$remoteShort = if ($remote) { $remote.Substring(0, 7) } else { 'unknown (no network?)' }
Write-Output "live copy : $live  $(git -C $RepoPath log -1 --format=%s 2>$null)"
Write-Output "GitHub    : $remoteShort"
if ($remote -and -not $remote.StartsWith((git -C $RepoPath rev-parse HEAD 2>$null))) {
    Write-Output 'status    : GitHub is ahead; the next deploy run should pick it up'
} elseif ($remote) {
    Write-Output 'status    : up to date'
}
$dirty = git -C $RepoPath status --porcelain --untracked-files=no 2>$null
if ($dirty) { Write-Output "WARNING   : live copy has local changes (blocks deploys):`n$dirty" }

Show 'Bot'
$hbPath = Join-Path $log 'heartbeat.json'
$hb = Read-Json $hbPath
$all = @(Get-CimInstance Win32_Process -Filter "Name like 'python%'" -ErrorAction SilentlyContinue)
if ($hb) {
    $state = Get-HeartbeatState $all $hb
    $stateText = switch ($state) {
        'running' { 'running' }
        'reused'  { 'NOT running: that PID now belongs to a newer process' }
        default   { 'NOT running' }
    }
    # The heartbeat is written once, at startup, so its age is the uptime.
    $age = Format-Age ((Get-Date) - (Get-Item $hbPath).LastWriteTime)
    Write-Output "heartbeat : commit $($hb.commit.Substring(0,7)), started $($hb.started_at) UTC ($age ago), pid $($hb.pid) ($stateText)"
} else { Write-Output 'heartbeat : none' }
$bot = Find-BotProcesses $all $hb $RepoPath
$detail = ($bot | ForEach-Object { "$($_.Id) $($_.Why)" }) -join ', '
Write-Output "processes : $($bot.Count) bot process(es)$(if ($detail) { ": $detail" })"
$busy = Read-Json (Join-Path $log 'busy.json')
if ($busy) { Write-Output "busy      : since $($busy.since): $($busy.work -join ', ')" } else { Write-Output 'busy      : idle' }

Show 'Deploy'
$task = Get-ScheduledTask -TaskName EbayBotDeploy -ErrorAction SilentlyContinue
if ($task) {
    $info = Get-ScheduledTaskInfo -TaskName EbayBotDeploy
    Write-Output "task      : $($task.State), last run $($info.LastRunTime) result $($info.LastTaskResult), next $($info.NextRunTime)"
} else { Write-Output 'task      : EbayBotDeploy not found' }
$st = Read-Json (Join-Path $log 'deploy-state.json')
if ($st) {
    if ($st.failed) { Write-Output "failed    : $($st.failed) (skipped until a newer commit, or deploy.ps1 -Force)" }
    if ($st.alert)  { Write-Output "alert     : $($st.alert)" }
}
$dl = Join-Path $log 'deploy.log'
if (Test-Path $dl) { Write-Output 'last log lines:'; Get-Content $dl -Tail 8 | ForEach-Object { "  $_" } }

if ($Logs -gt 0) {
    Show "bot.log (last $Logs)"
    Get-Content (Join-Path $log 'bot.log') -Tail $Logs -ErrorAction SilentlyContinue
}
if ($Errors) {
    Show 'bot.log errors/warnings (last 30)'
    Select-String -Path (Join-Path $log 'bot.log') -Pattern 'ERROR|Traceback|WARNING|UNHANDLED|FAIL' -ErrorAction SilentlyContinue |
        Select-Object -Last 30 | ForEach-Object { $_.Line }
    Show 'service.out.log (last 20)'
    Get-Content (Join-Path $log 'service.out.log') -Tail 20 -ErrorAction SilentlyContinue
}
