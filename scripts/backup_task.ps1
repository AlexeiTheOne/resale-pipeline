<#
backup_task.ps1 - the nightly backup (scheduled task EbayBotBackup).

Replaces C:\Users\boris\Projects\backup_ebay_bot.ps1, which backed up
data\ross.db with inline SQLite code (frozen after the SQL Server cutover).
Install: copy this file over that one (see docs/cutover-checklist.md), or
point the task at it.

  1. DB: backup.py --db-only --require-copy. It follows DB_BACKEND in the live
     .env: on SQL Server a verified BACKUP DATABASE into SQL_BACKUP_DIR
     (D:\SQLBackups, keep 14), copied to OneDrive as ross-<UTC time>.bak
     (keep 30). OneDrive is the only off-machine copy (C: and D: are one
     disk), so a copy that fails, fails the run.
  2. Photos: data\inbox and data\pieces mirrored to OneDrive (robocopy).
  3. Any failure: a line in logs\backup.log, a Telegram alert
     (scripts\notify.py) and exit code 1 for Task Scheduler.

Must run as boris with the OneDrive client signed in: the local OneDrive
folder is off-machine only once the client has uploaded it.
ASCII only (Windows PowerShell 5.1 reads BOM-less files as ANSI).
#>
param(
    [string]$Repo = 'C:\Users\boris\Projects\ebay-list',
    [string]$Dest = 'C:\Users\boris\OneDrive\EbayBot-backups'
)
$ErrorActionPreference = 'Stop'

$repo   = $Repo
$dest   = $Dest
$python = "$repo\venv\Scripts\python.exe"
$log    = "$repo\logs\backup.log"

$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'

function Write-Log([string]$Message) {
    $line = '{0} {1}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Message
    Write-Host $line
    New-Item -ItemType Directory -Force (Split-Path $log) | Out-Null
    Add-Content -Path $log -Value $line -Encoding UTF8
}

function Send-Alert([string]$Text) {
    try {
        Push-Location $repo
        try { & $python 'scripts\notify.py' $Text.Replace('"', "'") 2>&1 | Out-Null } finally { Pop-Location }
    } catch {
        Write-Log "could not send the Telegram alert: $_"
    }
}

function Invoke-Robocopy([string]$From, [string]$To) {
    if (-not (Test-Path $From)) { Write-Log "skip $From (missing)"; return }
    robocopy $From $To /MIR /R:2 /W:5 /NP /NFL /NDL | Out-Null
    # 0-7 = success (files copied, extra files removed, ...); 8 and up = failure.
    if ($LASTEXITCODE -ge 8) { throw "robocopy $From -> $To failed (exit $LASTEXITCODE)" }
}

try {
    New-Item -ItemType Directory -Force "$dest\db" | Out-Null

    # 1. Database (SQL Server or SQLite, whichever the live .env uses).
    $env:BACKUP_DIR = "$dest\db"     # OneDrive: the only off-machine copy
    $env:BACKUP_KEEP = '30'          # .bak copies kept in OneDrive
    $env:SQL_BACKUP_KEEP = '14'      # .bak files kept in D:\SQLBackups (same disk)
    Push-Location $repo
    try {
        $ErrorActionPreference = 'Continue'   # python's stderr is not an error by itself
        $out = & $python 'backup.py' '--db-only' '--require-copy' 2>&1 | ForEach-Object { "$_" }
        $code = $LASTEXITCODE
        $ErrorActionPreference = 'Stop'
    } finally { Pop-Location }
    Write-Log ("backup.py exit {0}: {1}" -f $code, ((@($out) | Select-Object -Last 3) -join ' | '))
    if ($code -ne 0) { throw "DB backup failed or did not reach OneDrive (exit $code)" }

    # 2. Photos.
    Invoke-Robocopy "$repo\data\inbox"  "$dest\photos\inbox"
    Invoke-Robocopy "$repo\data\pieces" "$dest\photos\pieces"

    Write-Log 'nightly backup ok'
    exit 0
} catch {
    Write-Log "nightly backup FAILED: $_"
    Send-Alert ([char]::ConvertFromUtf32(0x274C) + " Nightly backup failed: $_")
    exit 1
}
