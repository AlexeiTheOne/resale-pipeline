"""scripts/prod_status.ps1 finds the live bot's processes.

The bot runs as an S4U task, so a normal login can't see its command line, and
searching command lines for telegram_bot.py reported 0 processes while the bot
was running. The script now uses the heartbeat PID (and that the process started
before the heartbeat was written), like restart_ebay_bot.ps1.

Dot-sources the script and feeds it fake process lists: no real processes."""
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "prod_status.ps1"
LIVE = r"C:\Users\boris\Projects\ebay-list"
POWERSHELL = shutil.which("powershell") or shutil.which("pwsh")

pytestmark = pytest.mark.skipif(sys.platform != "win32" or not POWERSHELL,
                                reason="needs Windows PowerShell")

HB = {"commit": "91ddeff62bbf", "started_at": "2026-11-09T15:37:11+00:00", "pid": 30144}


def _proc(pid, parent, created, cmd=None, exe=None):
    return {"ProcessId": pid, "ParentProcessId": parent, "Name": "python.exe",
            "CommandLine": cmd, "ExecutablePath": exe, "CreationDate": created}


def _run(procs, hb=HB):
    """(heartbeat state, [(pid, why), ...]) from the script's own functions."""
    ps = f"""
$ErrorActionPreference = 'Stop'
. '{SCRIPT}'
$procs = @('{json.dumps(procs)}' | ConvertFrom-Json | ForEach-Object {{
    $_.CreationDate = [DateTimeOffset]::Parse($_.CreationDate).UtcDateTime; $_ }})
$hb = '{json.dumps(hb)}' | ConvertFrom-Json
$found = Find-BotProcesses $procs $hb '{LIVE}'
@{{ state = (Get-HeartbeatState $procs $hb);
    found = @($found | ForEach-Object {{ ,@($_.Id, $_.Why) }}) }} | ConvertTo-Json -Depth 4 -Compress
"""
    out = subprocess.run([POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                          "-Command", ps], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    data = json.loads(out.stdout)
    found = data["found"] or []
    if found and not isinstance(found[0], list):   # PowerShell unwraps a single pair
        found = [found]
    return data["state"], [(int(p), w) for p, w in found]


def test_hidden_s4u_bot_is_found_by_heartbeat_pid():
    procs = [_proc(1984, 900, "2026-11-09T15:36:58Z"),          # launcher, hidden
             _proc(30144, 1984, "2026-11-09T15:36:59Z")]        # interpreter, hidden
    state, found = _run(procs)
    assert state == "running"
    assert found == [(30144, "heartbeat pid"), (1984, "launcher")]


def test_a_reused_pid_is_not_the_bot():
    procs = [_proc(30144, 1, "2026-11-09T16:10:00Z")]           # started after the heartbeat
    state, found = _run(procs)
    assert state == "reused"
    assert found == []


def test_a_dead_bot_has_no_processes():
    state, found = _run([_proc(555, 1, "2026-11-09T15:00:00Z")])
    assert state == "gone"
    assert found == []


def test_visible_command_line_from_the_live_venv_is_found():
    """What the deploy task sees: command lines are visible there."""
    exe = LIVE + r"\venv\Scripts\python.exe"
    procs = [_proc(1984, 900, "2026-11-09T15:36:58Z", r"venv\Scripts\python.exe telegram_bot.py", exe),
             _proc(30144, 1984, "2026-11-09T15:36:59Z", r"venv\Scripts\python.exe telegram_bot.py",
                   r"C:\Python313\python.exe")]
    state, found = _run(procs)
    assert state == "running"
    assert sorted(found) == [(1984, "launcher"), (30144, "interpreter")]


def test_a_dev_bot_is_not_counted():
    dev = r"C:\Users\boris\Projects\resale-dev\venv\Scripts\python.exe"
    procs = [_proc(4000, 900, "2026-11-09T15:00:00Z", r"venv\Scripts\python.exe telegram_bot.py", dev)]
    _, found = _run(procs, hb={"commit": "x", "started_at": "2026-11-09T15:37:11+00:00", "pid": 4000})
    assert found == []
