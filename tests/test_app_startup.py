"""Startup (bot/app.py): the allowlist is parsed by configure() at startup, and
the background loops start once without PTB's create_task warning.

No network: the Application is real but never initialized or run, the Telegram
call in _post_init is faked, and the loops are replaced by recording fakes.
"""
import asyncio
import sys
import warnings
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import config  # noqa: E402
from bot import app as bot_app  # noqa: E402
from bot import jobs, util  # noqa: E402
from telegram import Bot  # noqa: E402
from telegram.ext import Application, ApplicationHandlerStop  # noqa: E402
from telegram.warnings import PTBUserWarning  # noqa: E402
from tg_fakes import bot_state, make_text_update, run, sent_texts  # noqa: E402

import telegram_bot as tb  # noqa: E402

LOOPS = ("_weekly_reprice_loop", "_sold_alert_loop", "_daily_sync_loop",
         "_daily_feedback_loop")


@pytest.fixture
def startup_env(monkeypatch):
    """configure() reads only the test's environment, not the real .env."""
    monkeypatch.setattr(bot_app, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setattr(util, "ALLOWED_USER_IDS", set())  # restored afterwards
    return monkeypatch


def test_configure_parses_allowlist_and_guard_uses_it(startup_env):
    startup_env.setenv("TELEGRAM_ALLOWED_USER_IDS", "111, 222;333")
    with bot_state(tb):
        util.ALLOWED_USER_IDS = set()  # as at import: nothing parsed yet
        bot_app.configure()
        assert util.ALLOWED_USER_IDS == {111, 222, 333}

        update, ctx = make_text_update("hello", user_id=999)
        with pytest.raises(ApplicationHandlerStop):
            run(tb._auth_guard, update, ctx)
        assert sent_texts(ctx) == ["⛔ Not authorized to use this bot."]

        update, ctx = make_text_update("hello", user_id=222)
        run(tb._auth_guard, update, ctx)
        assert sent_texts(ctx) == []


def test_configure_unset_leaves_bot_open_with_warning(startup_env, capsys):
    startup_env.delenv("TELEGRAM_ALLOWED_USER_IDS", raising=False)
    bot_app.configure()
    assert util.ALLOWED_USER_IDS == set()
    assert "SECURITY: TELEGRAM_ALLOWED_USER_IDS is unset" in capsys.readouterr().out
    update, ctx = make_text_update("hello", user_id=999)
    run(tb._auth_guard, update, ctx)  # open: no stop, no reply
    assert sent_texts(ctx) == []


def test_main_configures_before_building(startup_env):
    """main(): logging first, then configure(), then the Application."""
    order = []
    import logging_setup
    startup_env.setattr(logging_setup, "setup_logging", lambda *a: order.append("logging"))
    startup_env.setattr(bot_app, "configure", lambda: order.append("configure"))

    class _App:
        def run_polling(self):
            order.append("run_polling")

    startup_env.setattr(bot_app, "build_app",
                        lambda token: order.append("build") or _App())
    bot_app.main()
    assert order == ["logging", "configure", "build", "run_polling"]


def test_post_init_starts_each_loop_once_without_ptb_warning(monkeypatch, capsys):
    started = {name: 0 for name in LOOPS}
    cancelled = set()

    def fake(name):
        async def loop(app):
            started[name] += 1
            try:
                await asyncio.Event().wait()  # like a real loop: never returns
            except asyncio.CancelledError:
                cancelled.add(name)
                raise
        loop.__name__ = name
        return loop

    for name in LOOPS:
        monkeypatch.setattr(jobs, name, fake(name))

    async def set_my_commands(self, commands, *a, **k):
        return True

    monkeypatch.setattr(Bot, "set_my_commands", set_my_commands)
    monkeypatch.setattr(util, "ALLOWED_USER_IDS", {4242})
    for flag in ("JOBS_ENABLED", "SOLD_ALERTS_ENABLED", "SYNC_ENABLED", "FEEDBACK_ENABLED"):
        monkeypatch.setattr(config, flag, True)

    # A real (never initialized) Application: not running, exactly as in
    # run_polling's post_init, which is when create_task used to warn.
    app = Application.builder().token("123:fake").build()
    assert not app.running

    async def go():
        await bot_app._post_init(app)
        await bot_app._post_init(app)  # a second call must not start duplicates
        for _ in range(3):
            await asyncio.sleep(0)
        await bot_app._post_shutdown(app)

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        asyncio.run(go())
    ptb = [w for w in caught if issubclass(w.category, PTBUserWarning)]
    assert not ptb, [str(w.message) for w in ptb]
    assert started == {name: 1 for name in LOOPS}
    assert cancelled == set(LOOPS), "shutdown didn't cancel the loops"
    assert bot_app._loop_tasks == {}
    out = capsys.readouterr().out
    assert "Weekly price check scheduled" in out
    assert "Sold alerts every" in out
    assert "Daily eBay sync scheduled" in out
    assert "Daily buyer feedback scheduled" in out


def test_loop_exception_goes_to_error_handlers(monkeypatch):
    """An exception escaping a loop reaches process_error, as it did under
    Application.create_task."""
    seen = []

    class _App:
        async def process_error(self, update, error):
            seen.append((update, error))

    async def boom(app):
        raise RuntimeError("loop died")

    async def go():
        bot_app._start_loop(_App(), boom)
        await bot_app._loop_tasks["boom"]
        bot_app._loop_tasks.clear()

    asyncio.run(go())
    assert len(seen) == 1 and seen[0][0] is None
    assert str(seen[0][1]) == "loop died"


def test_write_heartbeat_records_commit_time_and_pid(monkeypatch, tmp_path):
    import json
    import os
    monkeypatch.setattr(config, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(bot_app, "_git_commit", lambda: "abc123")
    path = bot_app.write_heartbeat()
    assert path == tmp_path / "logs" / "heartbeat.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["commit"] == "abc123"
    assert data["pid"] == os.getpid()
    assert data["started_at"].endswith("+00:00")
    assert not path.with_suffix(".tmp").exists()


def test_git_commit_reads_this_repo():
    commit = bot_app._git_commit()
    assert commit == "unknown" or len(commit) == 40


def test_post_init_survives_heartbeat_failure(monkeypatch, capsys):
    def boom():
        raise OSError("disk full")
    monkeypatch.setattr(bot_app, "write_heartbeat", boom)

    async def set_my_commands(self, commands, *a, **k):
        return True
    monkeypatch.setattr(Bot, "set_my_commands", set_my_commands)
    monkeypatch.setattr(util, "ALLOWED_USER_IDS", set())
    for flag in ("SOLD_ALERTS_ENABLED", "SYNC_ENABLED", "FEEDBACK_ENABLED"):
        monkeypatch.setattr(config, flag, False)
    app = Application.builder().token("1:fake").build()
    asyncio.run(bot_app._post_init(app))
    assert "could not write heartbeat" in capsys.readouterr().out


def test_jobs_disabled_starts_no_loops(monkeypatch, capsys):
    started = []
    for name in LOOPS:
        async def loop(app, name=name):
            started.append(name)
        loop.__name__ = name
        monkeypatch.setattr(jobs, name, loop)

    async def set_my_commands(self, commands, *a, **k):
        return True
    monkeypatch.setattr(Bot, "set_my_commands", set_my_commands)
    monkeypatch.setattr(util, "ALLOWED_USER_IDS", {4242})
    monkeypatch.setattr(config, "JOBS_ENABLED", False)
    for flag in ("SOLD_ALERTS_ENABLED", "SYNC_ENABLED", "FEEDBACK_ENABLED"):
        monkeypatch.setattr(config, flag, True)
    app = Application.builder().token("1:fake").build()

    async def go():
        await bot_app._post_init(app)
        await asyncio.sleep(0)
    asyncio.run(go())
    assert started == []
    assert "Background jobs OFF" in capsys.readouterr().out
