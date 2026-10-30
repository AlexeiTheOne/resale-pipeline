"""logs/busy.json: exists only while work is in flight (the deploy script waits on it)."""
import asyncio
import json
import os
from pathlib import Path

import pytest
from telegram import Bot
from telegram.ext import Application

import config
from bot import app as bot_app
from bot import state, util


@pytest.fixture(autouse=True)
def logs(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "LOG_DIR", tmp_path / "logs")
    state._in_flight.clear()
    yield tmp_path / "logs" / "busy.json"
    state._in_flight.clear()


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_file_appears_on_first_claim(logs):
    assert not logs.exists()
    assert util._claim("publish", "abc")
    data = _read(logs)
    assert data["pid"] == os.getpid()
    assert data["work"] == ["publish:abc"]
    assert data["since"].endswith("+00:00")
    assert not logs.with_suffix(".tmp").exists()


def test_two_claims_listed_and_since_kept(logs, monkeypatch):
    times = iter(["2026-10-30T10:00:00+00:00", "2026-10-30T11:00:00+00:00"])

    class FakeNow:
        def isoformat(self, timespec):
            return next(times)

    class FakeDatetime:
        @staticmethod
        def now(tz):
            return FakeNow()
    monkeypatch.setattr(util, "datetime", FakeDatetime)

    util._claim("review", "zzz")
    util._claim("publish", "abc")
    data = _read(logs)
    assert data["work"] == ["publish:abc", "review:zzz"]
    assert data["since"] == "2026-10-30T10:00:00+00:00"
    util._release("review", "zzz")
    assert _read(logs) == {**data, "work": ["publish:abc"]}

    # Idle, then busy again: a new "since".
    util._release("publish", "abc")
    util._claim("retry", "x")
    assert _read(logs)["since"] == "2026-10-30T11:00:00+00:00"


def test_file_removed_after_last_release(logs):
    util._claim("publish", "a")
    util._claim("retry", "b")
    util._release("publish", "a")
    assert logs.exists()
    util._release("retry", "b")
    assert not logs.exists()


def test_failed_write_does_not_break_claim(logs, monkeypatch, capsys):
    def boom(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(os, "replace", boom)
    assert util._claim("publish", "abc") is True
    assert ("publish", "abc") in state._in_flight
    util._release("publish", "abc")
    assert state._in_flight == set()
    assert "could not update busy.json" in capsys.readouterr().out


def test_startup_clears_stale_file(logs, monkeypatch):
    logs.parent.mkdir(parents=True)
    logs.write_text('{"pid": 1, "since": "x", "work": ["publish:old"]}', encoding="utf-8")

    async def set_my_commands(self, commands, *a, **k):
        return True
    monkeypatch.setattr(Bot, "set_my_commands", set_my_commands)
    monkeypatch.setattr(bot_app, "_git_commit", lambda: "abc123")
    monkeypatch.setattr(util, "ALLOWED_USER_IDS", set())
    for flag in ("SOLD_ALERTS_ENABLED", "SYNC_ENABLED", "FEEDBACK_ENABLED"):
        monkeypatch.setattr(config, flag, False)
    app = Application.builder().token("1:fake").build()
    asyncio.run(bot_app._post_init(app))
    assert not logs.exists()
