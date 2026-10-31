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

Show 'Code'
$live = (git -C $RepoPath rev-parse --short HEAD 2>$null)
$remote = (git ls-remote https://github.com/AlexeiTheOne/resale-pipeline.git refs/heads/main 2>$null)
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
$hb = Read-Json (Join-Path $log 'heartbeat.json')
if ($hb) {
    $alive = [bool](Get-Process -Id $hb.pid -ErrorAction SilentlyContinue)
    Write-Output "heartbeat : commit $($hb.commit.Substring(0,7)), started $($hb.started_at) UTC, pid $($hb.pid) ($(if ($alive) {'running'} else {'NOT running'}))"
} else { Write-Output 'heartbeat : none' }
$procs = @(Get-CimInstance Win32_Process -Filter "CommandLine like '%telegram_bot.py%'" -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -like 'python*' })
Write-Output "processes : $($procs.Count) telegram_bot.py process(es)"
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
