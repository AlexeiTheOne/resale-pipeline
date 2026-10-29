"""The scheduled jobs must not repeat themselves.

On 2026-10-26 the daily sync re-reported the same 9 items as both "sold" and
"back to published" (each run re-marked an already-recorded sale, the
live-listing pass undid it), ended listings flipped qty 0→1→0, the feedback job
posted the same 13 eBay errors it posts every day, and the weekly price check
sent one message per flagged listing. These pin the fixes.

No eBay or Telegram calls; the DB is pointed at a temp file before import.
Run with `python tests/test_quiet_notifications.py` or `pytest tests/`.
"""
import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import db  # noqa: E402

db.DB_PATH = str(Path(tempfile.mkdtemp(prefix="quiet_")) / "test.db")
db.create_tables()

import telegram_bot as tb  # noqa: E402


def _sync_with(item, offer, sold_map):
    writes = []
    saved = (tb.get_offer, tb.update_field, tb.update_status)
    tb.get_offer = lambda _id: offer
    tb.update_field = lambda *a: writes.append(("field",) + a)
    tb.update_status = lambda *a: writes.append(("status",) + a)
    try:
        return tb._sync_one(item, sold_map), writes
    finally:
        tb.get_offer, tb.update_field, tb.update_status = saved


_SALE = {"sale_price": 49.99, "shipping_collected": 0, "ebay_fees": 8.38,
         "shipping_label_cost": 0, "net_proceeds": 41.61, "order_id": "A",
         "order_ids": ["A"], "units": 1, "sold_at": "2026-10-01", "fees_known": True}


def test_recorded_sale_on_a_live_listing_is_not_re_marked_sold():
    item = {"item_id": "ac7b7ee7" + "x" * 28, "status": "published",
            "listing": {"price": 49.99},
            "ebay": {"offer_id": "o", "quantity": 1, "order_ids": ["A"]}}
    offer = {"pricingSummary": {"price": {"value": "49.99"}}, "availableQuantity": 1,
             "listing": {"listingStatus": "ACTIVE"}}
    result, writes = _sync_with(item, offer, {item["item_id"]: _SALE})
    assert not result["auto_sold"] and not result["new_sale"]
    assert result["changes"] == [] and writes == []


def test_a_new_order_is_still_recorded():
    item = {"item_id": "n" * 36, "status": "published", "listing": {"price": 49.99},
            "ebay": {"offer_id": "o", "quantity": 1}}
    offer = {"pricingSummary": {"price": {"value": "49.99"}}, "availableQuantity": 0,
             "listing": {"listingStatus": "ACTIVE"}}
    result, writes = _sync_with(item, offer, {item["item_id"]: _SALE})
    assert result["auto_sold"] and result["new_sale"]["net"] == 41.61
    assert ("status", item["item_id"], "sold") in writes


def test_ended_listing_does_not_flip_quantity():
    item = {"item_id": "570edca4" + "x" * 28, "status": "sold", "listing": {"price": 20.0},
            "ebay": {"offer_id": "o", "quantity": 0, "order_ids": ["A"]}}
    offer = {"pricingSummary": {"price": {"value": "20.0"}}, "availableQuantity": 1,
             "listing": {"listingStatus": "ENDED"}}
    result, writes = _sync_with(item, offer, {item["item_id"]: _SALE})
    assert result["changes"] == [] and writes == []


def test_short_window_warning_only_when_something_new_is_lost():
    item = {"item_id": "8b5ddc05" + "x" * 28, "status": "sold", "listing": {"price": 50.0},
            "ebay": {"offer_id": "o", "quantity": 0, "order_ids": ["A", "B"]}}
    offer = {"pricingSummary": {"price": {"value": "50.0"}}, "availableQuantity": 0,
             "listing": {"listingStatus": "ENDED"}}
    result, _ = _sync_with(item, offer, {item["item_id"]: _SALE})   # sees only A
    assert result["changes"] == []


def test_feedback_failures_are_reported_once():
    entry = {"item_id": "1", "transaction_id": "2", "order_line": "1-2",
             "buyer": "b", "title": "t"}
    first = tb._only_new_feedback_problems(
        {"left": [], "skipped": [], "errors": [(entry, "eBay said no")]})
    second = tb._only_new_feedback_problems(
        {"left": [], "skipped": [], "errors": [(entry, "eBay said no")]})
    assert len(first["errors"]) == 1 and second["errors"] == []
    assert tb._feedback_report(second) == []
    # Once it finally goes through, it's forgotten.
    tb._only_new_feedback_problems({"left": [entry], "skipped": [], "errors": []})
    assert "1-2" not in db.known_feedback_failures()


class _Bot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, reply_markup=None):
        self.sent.append((text, reply_markup))


def _r(verdict, suggested=None):
    return {"item_id": verdict.lower().ljust(36, "x"), "verdict": verdict,
            "impressions": 40, "views": 1, "cumulative_views": 3, "ctr": None,
            "watchers": 1, "current_price": 34.99, "suggested_price": suggested}


def test_price_digest_groups_instead_of_one_message_per_listing():
    results = ([_r("INVISIBLE")] * 20 + [_r("SEND_OFFERS")] * 10
               + [_r("OVERPRICED", suggested=29.99)] * 2 + [_r("HEALTHY")] * 5)
    bot = _Bot()
    asyncio.run(tb._send_reprice_digest(bot, 1, results, None))
    # header + one grouped list + one per Apply button
    assert len(bot.sent) == 4
    grouped = bot.sent[1][0]
    assert "(20)" in grouped and "(10)" in grouped and grouped.count("•") == 30
    assert all(markup is not None for _, markup in bot.sent[2:])


# --- found listing the Luv Betsey backpack, 2026-10-27 ----------------------

def test_ross_tag_sent_second_keeps_the_sent_order():
    from ebay.inventory import listing_photo_order
    photos = ["cover", "inside", "logo", "zip", "back"]
    assert listing_photo_order({"photos": photos})[-1] == "inside"      # old default
    assert listing_photo_order({"photos": photos, "photo_layout": {"as_sent": True}}) == photos


def test_v2_price_line_does_not_call_one_sale_solid():
    line = tb._price_evidence_line({"engine": "v2", "confidence": "medium",
                                    "sold_count": 1, "active_count": 3})
    assert "solid" not in line and "None" not in line and "1 same-item sale" in line
    manual = tb._price_evidence_line({"engine": "v2", "confidence": "manual",
                                      "manual_price": 49.99, "machine_price": 54.99})
    assert "set by hand" in manual and "54.99" in manual


if __name__ == "__main__":
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  ok   {name}")
            except AssertionError as e:
                fails += 1
                print(f"  FAIL {name}: {e}")
    sys.exit(1 if fails else 0)
