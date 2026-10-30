"""Smoke tests for tests/tg_fakes.py: real handlers, fake Telegram."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import telegram_bot as tb  # noqa: E402
from bot import intake  # noqa: E402
from telegram.ext import ApplicationHandlerStop  # noqa: E402
from tg_fakes import (  # noqa: E402
    DEFAULT_USER_ID, bot_state, last_markup_buttons, make_callback_update,
    make_photo_update, make_text_update, run, run_steps, sent_texts,
)


@pytest.fixture
def bot():
    with bot_state(tb):
        yield tb


def test_whoami_replies_with_id_and_allowlist_state(bot):
    update, ctx = make_text_update("/whoami")
    run(tb.whoami_command, update, ctx)
    assert sent_texts(ctx) == [
        f"Your Telegram user id: {DEFAULT_USER_ID}\n"
        "Bot access: restricted; you are on the allowlist."
    ]


def test_help_lists_commands(bot):
    update, ctx = make_text_update("/help")
    run(tb.help_command, update, ctx)
    text = "\n".join(sent_texts(ctx))
    assert "Ross Resale Bot" in text and "/whoami" in text


def test_auth_guard_lets_allowed_user_through_and_blocks_others(bot):
    update, ctx = make_text_update("hello")
    run(tb._auth_guard, update, ctx)
    assert sent_texts(ctx) == []

    update, ctx = make_text_update("hello", user_id=999)
    with pytest.raises(ApplicationHandlerStop):
        run(tb._auth_guard, update, ctx)
    assert sent_texts(ctx) == ["⛔ Not authorized to use this bot."]


def test_command_args_are_split_like_ptb():
    _, ctx = make_text_update("/setqty abc 3")
    assert ctx.args == ["abc", "3"]


def test_photo_batch_then_cancel_button(bot, monkeypatch):
    """Two photos debounce into one staged item with Start/Wait/Cancel buttons;
    Cancel deletes it. Exercises get_file, create_task and drain()."""
    monkeypatch.setattr(intake, "DEFAULT_CAPTURE_WINDOW", 0)
    p1, ctx = make_photo_update()
    p2, _ = make_photo_update(context=ctx)
    # Both photos on one loop: the second cancels the first's finalize timer.
    run_steps((tb.photo_handler, p1), (tb.photo_handler, p2), ctx=ctx)

    assert sent_texts(ctx) == [
        "2 photo(s) buffered. Start the listing, wait for more photos, or cancel?"]
    item_id = tb.staging[DEFAULT_USER_ID]["item_id"]
    assert len(tb.get_item(item_id)["photos"]) == 2
    buttons = dict(last_markup_buttons(ctx))
    assert buttons["Cancel"] == f"confirm:cancel:{item_id}"

    tap, _ = make_callback_update(buttons["Cancel"], context=ctx)
    run(tb.confirm_callback, tap, ctx)
    assert sent_texts(ctx)[-1] == "Cancelled. Photos discarded."
    assert tb.get_item(item_id) is None
    assert DEFAULT_USER_ID not in tb.staging
