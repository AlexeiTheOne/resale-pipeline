"""bot/check_cmds.py: /shop, /check and saved checks in Telegram, with pricing
and identification faked (no API calls)."""
import asyncio
import time

import pytest

import config
import db
from bot import check_cmds, state
from pipeline import check, pricing_v2
from tests import tg_fakes as tg

JUDGED = [{"source": "sold", "verdict": "same_item", "total": t, "title": f"sold {t}", "url": "u"}
          for t in (52.0, 54.0, 56.0)] + \
         [{"source": "active", "verdict": "same_item", "total": 80.0, "title": "active", "url": "u"}]
IDENT = {"brand": "Coach", "product_name": "Tabby 26 shoulder bag", "item_type": "Shoulder Bag", "model": "CH857"}


def fake_pricing(calls, empty_quick=False):
    def get(ident, **kw):
        calls.append({"ident": ident, **kw})
        if empty_quick and kw.get("sold_cache_only"):
            return {"suggested_price": None, "formula_price": None, "raw_active_floor": None,
                    "judged_comps": [], "review_flags": [], "sold_cache_misses": ["q"],
                    "timings": {"comps_s": 1.0, "judge_s": 0.0}}
        return {"suggested_price": 44.99, "formula_price": 44.99, "raw_active_floor": 80.0,
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
    monkeypatch.setattr(check, "already_have", lambda *a: [])
    from pipeline import msrp
    monkeypatch.setattr(msrp, "load_history", lambda: [])
    monkeypatch.setattr(check, "TIMINGS_FILE", tmp_path / "check_timings.jsonl")
    monkeypatch.setattr(check_cmds, "CHECKS_DIR", tmp_path / "checks")
    monkeypatch.setattr(check_cmds, "ALBUM_WAIT_S", 0.0)
    monkeypatch.setattr(check_cmds, "_identify_roles", lambda roles: dict(IDENT))
    monkeypatch.setattr(check_cmds, "_identify_one", lambda path: dict(IDENT))
    monkeypatch.setattr(config, "CHECK_DAILY_MAX", 50)
    monkeypatch.setattr(config, "CHECK_DAILY_WARN", 40)
    yield calls
    for s in state.shop_mode.values():
        if s.get("task"):
            s["task"].cancel()
    tg.reset_bot_state(None)


def tag_on(monkeypatch, tag_index=None, sku="400289769542", price=14.99):
    """Fake barcode reading: photo number `tag_index` (1-based file name) is the tag."""
    real = check.photo_roles

    def decode(path):
        if tag_index is not None and path.endswith(f"{tag_index}.jpg"):
            return {"code": sku, "reduced_price": price}
        return {"code": None, "reduced_price": None}
    monkeypatch.setattr(check, "photo_roles",
                        lambda paths, decode_=None: real(paths, decode=decode, vision=lambda p: {}))


def shop_on(ctx=None):
    upd, ctx = tg.make_text_update("/shop", context=ctx)
    tg.run(check_cmds.shop_command, upd, ctx, wait=False)
    return ctx


# --- shopping mode ----------------------------------------------------------------------

def test_shop_on_off_and_expiry(env):
    ctx = shop_on()
    assert "Shopping mode ON until" in tg.sent_texts(ctx)[-1] and check_cmds.shopping(tg.DEFAULT_USER_ID)
    upd, ctx = tg.make_text_update("/shop off", context=ctx)
    tg.run(check_cmds.shop_command, upd, ctx)
    assert "Shopping mode OFF" in tg.sent_texts(ctx)[-1] and not check_cmds.shopping(tg.DEFAULT_USER_ID)
    state.shop_mode[tg.DEFAULT_USER_ID] = {"until": time.time() - 1, "task": None}
    assert not check_cmds.shopping(tg.DEFAULT_USER_ID) and tg.DEFAULT_USER_ID not in state.shop_mode


def test_the_shopping_filter_only_takes_photos_while_on(env):
    upd, _ = tg.make_photo_update()
    assert not check_cmds.SHOPPING.filter(upd.message)
    shop_on()
    assert check_cmds.SHOPPING.filter(upd.message)


def test_album_with_a_tag_is_one_saved_check_and_no_item(env, monkeypatch):
    tag_on(monkeypatch, tag_index=2)
    ctx = shop_on()
    before = len(db.list_items())
    a, ctx = tg.make_photo_update(context=ctx, media_group_id="g1")
    b, ctx = tg.make_photo_update(context=ctx, media_group_id="g1")
    tg.run_steps((check_cmds.shop_photo, a), (check_cmds.shop_photo, b), ctx=ctx)
    texts = tg.sent_texts(ctx)
    assert texts[-1].startswith("🛒 ✅ BUY: 3 sales, 1 listing") and len(texts[-1].splitlines()) <= 5
    details = next(d for _, d in tg.last_markup_buttons(ctx) if d.startswith("chk:details:"))
    upd, ctx = tg.make_callback_update(details, context=ctx)
    tg.run(check_cmds.check_callback, upd, ctx)
    assert "SKU 400289769542" in tg.sent_texts(ctx)[-1] and "Fees + ads" in tg.sent_texts(ctx)[-1]
    assert len(env) == 1 and env[0]["sold_cache_only"] is True        # one quick pricing run
    c = db.list_checks(limit=1)[0]
    assert (c["ross_sku"], c["ross_price"], c["verdict"], c["status"]) == ("400289769542", 14.99, "BUY", "checked")
    assert len(c["photos"]) == 2 and c["identification"]["brand"] == "Coach"
    assert c["comps_summary"]["comps"] and c["suggested_price"] == 43.99 and c["sold_count"] == 3
    assert (check_cmds.CHECKS_DIR / str(c["check_id"]) / "2.jpg").exists()
    assert len(db.list_items()) == before                          # not an item
    labels = [d.split(":")[1] for _, d in tg.last_markup_buttons(ctx)]
    assert labels == ["full", "any", "bought", "details"]


def test_no_tag_asks_the_price_then_a_typed_number_runs_the_check(env, monkeypatch):
    tag_on(monkeypatch, tag_index=None)
    ctx = shop_on()
    p, ctx = tg.make_photo_update(context=ctx)
    n, ctx = tg.make_text_update("12.99", context=ctx)
    assert True  # n is routed by AWAITING_PRICE once the question is out (checked below)
    tg.run_steps((check_cmds.shop_photo, p), ctx=ctx)
    assert "no Ross tag barcode found" in tg.sent_texts(ctx)[-1]
    prices = [d for _, d in tg.last_markup_buttons(ctx)]
    assert len(prices) == 8 and all(d.startswith("chkp:") for d in prices)
    assert check_cmds.AWAITING_PRICE.filter(n.message)

    async def answer():           # the same event loop as the waiting identification
        await check_cmds.price_text(n, ctx)
        await tg.drain(ctx)
    # the identification task was started on the previous loop: restart it here
    state.shop_price_wait[tg.DEFAULT_USER_ID]["ident_task"] = None
    asyncio.run(answer())
    c = db.list_checks(limit=1)[0]
    assert c["ross_price"] == 12.99 and c["ross_sku"] is None and c["verdict"]
    assert "Ross $12.99" in tg.sent_texts(ctx)[-1]
    assert tg.DEFAULT_USER_ID not in state.shop_price_wait


def test_price_buttons(env, monkeypatch):
    tag_on(monkeypatch, tag_index=None)
    ctx = shop_on()
    p, ctx = tg.make_photo_update(context=ctx)

    async def go():
        await check_cmds.shop_photo(p, ctx)
        await tg.drain(ctx)
        cid = state.shop_price_wait[tg.DEFAULT_USER_ID]["check_id"]
        upd, _ = tg.make_callback_update(f"chkp:{cid}:19.99", context=ctx)
        await check_cmds.price_callback(upd, ctx)
        await tg.drain(ctx)
        return cid
    cid = asyncio.run(go())
    c = db.get_check(cid)
    assert c["ross_price"] == 19.99 and c["identification"]["brand"] == "Coach"   # identified meanwhile


def test_bought_button(env, monkeypatch):
    tag_on(monkeypatch, tag_index=1)
    ctx = shop_on()
    p, ctx = tg.make_photo_update(context=ctx)
    tg.run_steps((check_cmds.shop_photo, p), ctx=ctx)
    bought = next(d for _, d in tg.last_markup_buttons(ctx) if d.startswith("chk:bought:"))
    upd, ctx = tg.make_callback_update(bought, context=ctx)
    tg.run(check_cmds.check_callback, upd, ctx)
    c = db.list_checks(limit=1)[0]
    assert c["status"] == "bought" and c["status_at"]
    assert "marked bought" in tg.sent_texts(ctx)[-1]


def test_quick_no_data_goes_straight_on_to_full_and_counts_once(env, monkeypatch):
    calls = []
    monkeypatch.setattr(pricing_v2, "get_pricing_v2", fake_pricing(calls, empty_quick=True))
    tag_on(monkeypatch, tag_index=1)
    ctx = shop_on()
    p, ctx = tg.make_photo_update(context=ctx)
    tg.run_steps((check_cmds.shop_photo, p), ctx=ctx)
    texts = tg.sent_texts(ctx)
    assert any("NO DATA in the quick check" in t and "Checking more sources" in t for t in texts)
    assert [c["sold_cache_only"] for c in calls] == [True, False]
    assert calls[1]["ident"] == calls[0]["ident"]
    assert check.checks_today(check.TIMINGS_FILE) == 1
    c = db.list_checks(limit=1)[0]
    assert c["mode"] == "full" and c["verdict"] == "BUY"


# --- typed /check and a captioned photo (fallbacks) -----------------------------------------

def test_typed_check_is_saved_too(env):
    upd, ctx = tg.make_text_update("/check Coach Tabby 26 CH857 shoulder bag 14.99")
    tg.run(check_cmds.check_command, upd, ctx)
    assert "#" in tg.sent_texts(ctx)[-1] and "quick · exact" in tg.sent_texts(ctx)[-1]
    c = db.list_checks(limit=1)[0]
    assert c["ross_price"] == 14.99 and c["verdict"] and c["photos"] is None


def test_full_and_any_buttons_update_the_same_check(env):
    upd, ctx = tg.make_text_update("/check Coach Tabby 26 CH857 shoulder bag 14.99")
    tg.run(check_cmds.check_command, upd, ctx)
    cid = db.list_checks(limit=1)[0]["check_id"]
    full = next(d for _, d in tg.last_markup_buttons(ctx) if d.startswith("chk:full:"))
    upd2, ctx = tg.make_callback_update(full, context=ctx)
    tg.run(check_cmds.check_callback, upd2, ctx)
    assert env[-1]["sold_cache_only"] is False and "full · exact" in tg.sent_texts(ctx)[-1]
    assert [c["check_id"] for c in db.list_checks(limit=5)].count(cid) == 1 and db.get_check(cid)["mode"] == "full"
    anyb = next(d for _, d in tg.last_markup_buttons(ctx) if d.startswith("chk:any:"))
    upd3, ctx = tg.make_callback_update(anyb, context=ctx)
    tg.run(check_cmds.check_callback, upd3, ctx)
    assert db.get_check(cid)["exact"] is False


def test_captioned_photo_outside_shopping_mode(env, monkeypatch):
    tag_on(monkeypatch, tag_index=1, price=24.99)
    upd, ctx = tg.make_photo_update(caption="/check")
    tg.run(check_cmds.photo_check, upd, ctx)
    assert "Ross $24.99" in tg.sent_texts(ctx)[-1]
    assert db.list_checks(limit=1)[0]["ross_sku"] == "400289769542"


def test_usage_cap_and_one_at_a_time(env, monkeypatch):
    upd, ctx = tg.make_text_update("/check")
    tg.run(check_cmds.check_command, upd, ctx)
    assert tg.sent_texts(ctx)[-1].startswith("Usage: /check")
    monkeypatch.setattr(config, "CHECK_DAILY_MAX", 1)
    for _ in range(2):
        upd, ctx = tg.make_text_update("/check dkny tote 9.99")
        tg.run(check_cmds.check_command, upd, ctx)
    assert "Daily limit reached" in tg.sent_texts(ctx)[-1]
    monkeypatch.setattr(config, "CHECK_DAILY_MAX", 50)
    state.check_running.add(tg.DEFAULT_USER_ID)
    upd, ctx = tg.make_text_update("/check dkny tote 9.99")
    tg.run(check_cmds.check_command, upd, ctx)
    assert "Still working" in tg.sent_texts(ctx)[-1]


def test_expired_button(env):
    upd, ctx = tg.make_callback_update("chk:full:deadbeef")
    tg.run(check_cmds.check_callback, upd, ctx)
    assert "expired" in tg.sent_texts(ctx)[-1]


def test_handler_order_shopping_then_caption_then_intake():
    from telegram.ext import Application, MessageHandler
    from bot import app as bot_app, intake
    application = Application.builder().token("1:fake").build()
    bot_app.register_handlers(application)
    photo = [h.callback for h in application.handlers[0] if isinstance(h, MessageHandler)
             and h.callback in (check_cmds.shop_photo, check_cmds.photo_check, intake.photo_handler)]
    assert photo == [check_cmds.shop_photo, check_cmds.photo_check, intake.photo_handler]
    from bot import text_router
    text = [h.callback for h in application.handlers[0] if isinstance(h, MessageHandler)
            and h.callback in (check_cmds.price_text, text_router.text_handler)]
    assert text == [check_cmds.price_text, text_router.text_handler]
