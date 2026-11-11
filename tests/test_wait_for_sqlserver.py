"""Startup wait for SQL Server (sqlserver.wait_for_server, telegram_bot.py).

After a reboot the bot could start before SQL Server and crash at import
(db.py connects at import), every 15 s, until the server came up.
"""
from pathlib import Path

import pyodbc
import pytest

import sqlserver


class _Clock:
    """Fake time: sleep() moves the clock, so no test really waits."""

    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def clock(self):
        return self.now

    def sleep(self, s):
        self.sleeps.append(s)
        self.now += s


class _Con:
    closed = False

    def close(self):
        self.closed = True


def _fake_connect(monkeypatch, fail_times):
    calls = []

    def connect(database=None, *, autocommit=False, timeout=10):
        calls.append((database, timeout))
        if len(calls) <= fail_times:
            raise pyodbc.OperationalError("08001", "Could not open a connection to SQL Server")
        return _Con()

    monkeypatch.setattr(sqlserver, "connect", connect)
    return calls


def test_server_up_returns_at_once_without_logging(monkeypatch):
    calls = _fake_connect(monkeypatch, fail_times=0)
    t, logs = _Clock(), []
    waited = sqlserver.wait_for_server(120, 5, log=logs.append, sleep=t.sleep, clock=t.clock)
    assert waited == 0
    assert len(calls) == 1 and t.sleeps == [] and logs == []


def test_waits_then_logs_once_and_when_up(monkeypatch):
    calls = _fake_connect(monkeypatch, fail_times=3)
    t, logs = _Clock(), []
    waited = sqlserver.wait_for_server(120, 5, log=logs.append, sleep=t.sleep, clock=t.clock)
    assert waited == 15 and len(calls) == 4 and t.sleeps == [5, 5, 5]
    assert len(logs) == 2
    assert "Waiting for SQL Server" in logs[0] and "up to 120 s" in logs[0]
    assert "is up after 15 s (4 tries)" in logs[1]


def test_gives_up_after_max_wait_with_clear_error(monkeypatch):
    calls = _fake_connect(monkeypatch, fail_times=10_000)
    t, logs = _Clock(), []
    with pytest.raises(RuntimeError, match="not reachable after 120 s") as exc:
        sqlserver.wait_for_server(120, 5, log=logs.append, sleep=t.sleep, clock=t.clock)
    assert isinstance(exc.value.__cause__, pyodbc.Error)
    assert len(calls) == 25 and sum(t.sleeps) == 120   # 2 minutes, every 5 s
    assert "giving up" in logs[-1]


def test_default_wait_is_two_minutes_from_config(monkeypatch):
    monkeypatch.setattr(sqlserver.config, "SQL_STARTUP_WAIT_SECONDS", 120)
    _fake_connect(monkeypatch, fail_times=10_000)
    t = _Clock()
    with pytest.raises(RuntimeError):
        sqlserver.wait_for_server(log=lambda m: None, sleep=t.sleep, clock=t.clock)
    assert t.now == 120


def test_log_lines_are_ascii(monkeypatch):
    """They go to service.out.log before logging is set up (cp1252 on Windows)."""
    _fake_connect(monkeypatch, fail_times=1)
    t, logs = _Clock(), []
    sqlserver.wait_for_server(120, 5, log=logs.append, sleep=t.sleep, clock=t.clock)
    assert all(m.isascii() for m in logs)


def test_entry_point_waits_before_importing_db():
    """db connects at import, so the wait must come first, and only when run
    as the bot (the tests import telegram_bot)."""
    src = (Path(__file__).parent.parent / "telegram_bot.py").read_text(encoding="utf-8")
    wait = src.index("sqlserver.wait_for_server()")
    assert wait < src.index("from db import")
    guard = src.rindex('if __name__ == "__main__":', 0, wait)
    assert 'config.DB_BACKEND == "sqlserver"' in src[guard:wait]
