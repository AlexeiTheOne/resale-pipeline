"""bot/check_cmds.py: /check in Telegram, with pricing faked (no API calls)."""
import pytest

import config
import db
from bot import check_cmds, state
from pipeline import check, pricing_v2
from tests import tg_fakes as tg

JUDGED = [{"source": "sold", "verdict": "same_item", "total": t} for t in (52.0, 54.0, 56.0)] + \
         [{"source": "active", "verdict": "same_item", "total": 50.0}]


def fake_pricing(calls):
    def get(ident, **kw):
        calls.append({"ident": ident, **kw})
        return {"suggested_price": 44.99, "formula_price": 44.99, "raw_active_floor": 50.0,
                "sold_median": 54.0, "judged_comps": JUDGED, "review_flags": [],
                "claude_confidence": "high", "same_item_active_count": 1,
                "sold_cache_misses": [] if kw.get("sold_cache_only") is False else ["q"],
                "timings": {"comps_s": 1.0, "judge_s": 2.0, "total_s": 3.0}}
    return get


@pytest.fixture
def env(tmp_path, monkeypatch):
    tg.reset_bot_state(None)
    calls = []
    monkeypatch.setattr(pricing_v2, "get_pricing_v2", fake_pricing(calls))
    monkeypatch.setattr(check, "already_have", lambda ident: [])
    monkeypatch.setattr(check, "TIMINGS_FILE", tmp_path / "check_timings.jsonl")
    monkeypatch.setattr(check_cmds, "_DIR", tmp_path / "check_tmp")
    monkeypatch.setattr(config, "CHECK_DAILY_MAX", 50)
    monkeypatch.setattr(config, "CHECK_DAILY_WARN", 40)
    yield calls
    tg.reset_bot_state(None)


def test_text_check_edits_the_reply_with_a_verdict_and_buttons(env):
    before = len(db.list_items())
    upd, ctx = tg.make_text_update("/check Coach Tabby 26 CH857 shoulder bag 14.99")
    tg.run(check_cmds.check_command, upd, ctx)
    texts = tg.sent_texts(ctx)
    assert texts[0].startswith("🔎 Checking… (quick check")
    assert "Mode: quick · exact matches" in texts[-1] and "Break-even" in texts[-1]
    assert [d.split(":")[1] for _, d in tg.last_markup_buttons(ctx)] == ["full", "any"]
    assert env[0]["sold_cache_only"] is True and env[0]["escalate"] is False
    assert len(db.list_items()) == before                     # nothing stored
    assert check.checks_today(check.TIMINGS_FILE) == 1        # timings logged
    assert not state.check_running


def test_full_check_button_reruns_the_same_check_in_full(env):
    upd, ctx = tg.make_text_update("/check Coach Tabby 26 CH857 shoulder bag 14.99")
    tg.run(check_cmds.check_command, upd, ctx)
    full = next(d for _, d in tg.last_markup_buttons(ctx) if d.startswith("chk:full:"))
    upd2, ctx = tg.make_callback_update(full, context=ctx)
    tg.run(check_cmds.check_callback, upd2, ctx)
    assert env[-1]["sold_cache_only"] is False and env[-1]["escalate"] is True
    assert "Mode: full · exact matches" in tg.sent_texts(ctx)[-1]
    # BUY: 3 same-item sales; $44.99 + shipping on a bag ($11 label) = $16.40 profit
    assert tg.sent_texts(ctx)[-1].startswith("✅ BUY: profit $16.40")


def test_any_matches_button(env):
    upd, ctx = tg.make_text_update("/check Coach Tabby 26 CH857 14.99")
    tg.run(check_cmds.check_command, upd, ctx)
    anyb = next(d for _, d in tg.last_markup_buttons(ctx) if d.startswith("chk:any:"))
    upd2, ctx = tg.make_callback_update(anyb, context=ctx)
    tg.run(check_cmds.check_callback, upd2, ctx)
    assert "any matches" in tg.sent_texts(ctx)[-1]
    assert [d.split(":")[1] for _, d in tg.last_markup_buttons(ctx)] == ["full", "exact"]


def test_quick_no_data_goes_straight_on_to_a_full_check(env, monkeypatch):
    calls = []

    def pricing(ident, **kw):
        calls.append({"ident": ident, **kw})
        if kw.get("sold_cache_only"):      # quick: nothing at all
            return {"suggested_price": None, "formula_price": None, "raw_active_floor": None,
                    "judged_comps": [], "review_flags": [], "sold_cache_misses": ["q"],
                    "timings": {"comps_s": 1.0, "judge_s": 0.0}}
        return fake_pricing([])(ident, **kw)
    monkeypatch.setattr(pricing_v2, "get_pricing_v2", pricing)
    identified = []
    monkeypatch.setattr(check_cmds, "_identify", lambda p: identified.append(p) or {
        "brand": "Coach", "product_name": "Tabby 26 bag", "model": "CH857"})
    monkeypatch.setattr(check_cmds, "_tag_price", lambda p: None)
    upd, ctx = tg.make_photo_update(caption="/check 14.99")
    tg.run(check_cmds.photo_check, upd, ctx)
    texts = tg.sent_texts(ctx)
    assert any("NO DATA in the quick check" in t and "Checking more sources" in t for t in texts)
    assert [c["sold_cache_only"] for c in calls] == [True, False]       # quick, then full
    assert calls[1]["escalate"] is True and calls[1]["ident"] == calls[0]["ident"]
    assert len(identified) == 1                                       # the photo was identified once
    assert texts[-1].startswith("✅ BUY") and "Mode: full" in texts[-1]
    assert check.checks_today(check.TIMINGS_FILE) == 1                # one check toward the cap
    assert [d.split(":")[1] for _, d in tg.last_markup_buttons(ctx)] == ["any"]   # already full


def test_expired_button(env):
    upd, ctx = tg.make_callback_update("chk:full:deadbeef")
    tg.run(check_cmds.check_callback, upd, ctx)
    assert "expired" in tg.sent_texts(ctx)[-1]


def test_photo_with_caption_reads_the_tag_price_and_identifies(env, monkeypatch):
    monkeypatch.setattr(check_cmds, "_tag_price", lambda path: 24.99)
    monkeypatch.setattr(check_cmds, "_identify", lambda path: {
        "brand": "Coach", "product_name": "Tabby 26", "item_type": "Shoulder Bag", "model": "CH857"})
    upd, ctx = tg.make_photo_update(caption="/check")
    tg.run(check_cmds.photo_check, upd, ctx)
    assert env[0]["photo_paths"] and env[0]["ident"]["brand"] == "Coach"
    assert "Ross $24.99" in tg.sent_texts(ctx)[-1]


def test_usage_without_a_description(env):
    upd, ctx = tg.make_text_update("/check")
    tg.run(check_cmds.check_command, upd, ctx)
    assert tg.sent_texts(ctx)[-1].startswith("Usage: /check")


def test_daily_cap_and_warning(env, monkeypatch):
    monkeypatch.setattr(config, "CHECK_DAILY_MAX", 2)
    monkeypatch.setattr(config, "CHECK_DAILY_WARN", 2)
    for _ in range(2):
        upd, ctx = tg.make_text_update("/check dkny tote 9.99")
        tg.run(check_cmds.check_command, upd, ctx)
    assert "(2 of 2 checks used today)" in tg.sent_texts(ctx)[-1]
    upd, ctx = tg.make_text_update("/check dkny tote 9.99")
    tg.run(check_cmds.check_command, upd, ctx)
    assert "Daily limit reached" in tg.sent_texts(ctx)[-1] and len(env) == 2


def test_one_check_at_a_time_per_user(env):
    state.check_running.add(tg.DEFAULT_USER_ID)
    upd, ctx = tg.make_text_update("/check dkny tote 9.99")
    tg.run(check_cmds.check_command, upd, ctx)
    assert "Still working" in tg.sent_texts(ctx)[-1] and env == []


def test_a_captioned_photo_goes_to_check_not_intake():
    from telegram.ext import Application, MessageHandler
    from bot import app as bot_app, intake
    application = Application.builder().token("1:fake").build()
    bot_app.register_handlers(application)
    photo_handlers = [h.callback for h in application.handlers[0]
                      if isinstance(h, MessageHandler) and h.callback in (check_cmds.photo_check,
                                                                          intake.photo_handler)]
    assert photo_handlers == [check_cmds.photo_check, intake.photo_handler]
