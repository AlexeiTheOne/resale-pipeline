"""The weekly price check, after the 29 Oct 2026 business review.

1. Impressions are eBay's TOTAL_IMPRESSION_TOTAL (promoted included), not the
   organic-only LISTING_IMPRESSION_TOTAL that made 53 of 115 listings look
   INVISIBLE while each was shown ~500 times a week.
2. A suggested cut re-prices through config.PRICING_ENGINE (v2), never lands
   under the sold-comp median or the cost floor, never cuts more than 12%, and
   the expensive call only happens for a listing that would actually get a cut.
3. Watched listings get a private offer plan, sent on ONE digest tap.

No network: eBay, Claude and Apify are all stubbed, and send_offer is replaced
with a recorder in every test that could reach it. The DB is a temp file.
Run with `python tests/test_reprice_offers.py` or `pytest tests/`.
"""
import asyncio
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import db  # noqa: E402

db.DB_PATH = str(Path(tempfile.mkdtemp(prefix="reprice_offers_")) / "test.db")
db.create_tables()

import config  # noqa: E402
import ebay.analytics as an  # noqa: E402
import pipeline.reprice as rp  # noqa: E402


class _Patch:
    def __init__(self, module, **attrs):
        self.module, self.attrs, self.saved = module, attrs, {}

    def __enter__(self):
        for k, v in self.attrs.items():
            self.saved[k] = getattr(self.module, k)
            setattr(self.module, k, v)
        return self

    def __exit__(self, *exc):
        for k, v in self.saved.items():
            setattr(self.module, k, v)


def _no_send(*_a, **_k):
    raise AssertionError("send_offer must not be called here")


# Belt and braces: nothing in this file may reach the real send_offer unpatched.
rp.send_offer = _no_send


# --- 1. the impressions metric -------------------------------------------------

class _Resp:
    def __init__(self, body, status=200):
        self._body, self.status_code, self.text = body, status, str(body)

    def json(self):
        return self._body


def _traffic_body(keys, rows):
    return {"header": {"metrics": [{"key": k} for k in keys]},
            "records": [{"dimensionValues": [{"value": lid}],
                         "metricValues": [{"value": v} for v in vals]}
                        for lid, vals in rows]}


def test_traffic_report_reads_total_impressions_by_key():
    """eBay returns metrics in its own order, not the order asked for."""
    keys = ["TOTAL_IMPRESSION_TOTAL", "LISTING_VIEWS_SOURCE_SEARCH_RESULTS_PAGE",
            "LISTING_IMPRESSION_TOTAL", "LISTING_VIEWS_TOTAL", "CLICK_THROUGH_RATE"]
    body = _traffic_body(keys, [("117", [11549, 4, 226, 8, 0.02])])
    asked = {}

    def fake_get(url, headers=None, params=None, timeout=None):
        asked.update(params)
        return _Resp(body)

    with _Patch(an.httpx, get=fake_get), _Patch(an, get_access_token=lambda: "t"):
        out = an.get_traffic_report(["117"])
    assert "TOTAL_IMPRESSION_TOTAL" in asked["metric"]
    assert "LISTING_IMPRESSION_TOTAL" in asked["metric"]
    assert out["117"] == {"impressions": 11549, "organic_impressions": 226, "views": 8,
                          "search_views": 4, "ctr": 0.02}


def test_traffic_report_without_total_raises_so_the_batch_is_no_data():
    """A silent 0 there would read as INVISIBLE; raising lands the batch on
    NO_TRAFFIC_DATA via _traffic_for_all instead."""
    body = _traffic_body(["LISTING_IMPRESSION_TOTAL", "LISTING_VIEWS_TOTAL"],
                         [("117", [226, 8])])
    with _Patch(an.httpx, get=lambda *a, **k: _Resp(body)), \
            _Patch(an, get_access_token=lambda: "t"):
        try:
            an.get_traffic_report(["117"])
        except RuntimeError as e:
            assert "TOTAL_IMPRESSION_TOTAL" in str(e)
        else:
            raise AssertionError("expected a RuntimeError")
    with _Patch(rp, get_traffic_report=lambda ids: an.get_traffic_report(ids)), \
            _Patch(an.httpx, get=lambda *a, **k: _Resp(body)), \
            _Patch(an, get_access_token=lambda: "t"):
        traffic, health = rp._traffic_for_all([{"ebay": {"listing_id": "117"}}])
    assert traffic == {"117": None} and health["failed"] == 1


OLD = config.REPRICE_STALE_WEEKS + 1


def test_a_typical_listing_is_not_invisible_any_more():
    """The 2026-10-29 median: 566 total, 56 organic, 2 views. Old rule
    (organic < 50) said INVISIBLE about half the catalogue."""
    typical = {"impressions": 566, "organic_impressions": 56, "views": 2}
    assert rp._diagnose(typical, 0, None, 2, 10) == "HEALTHY"
    assert rp._diagnose({"impressions": 100, "views": 0}, 0, None, 2, 10) == "INVISIBLE"


def test_low_ctr_needs_a_big_enough_week():
    # 0 views on 500 impressions is chance at a 0.28% median CTR.
    assert rp._diagnose({"impressions": 500, "views": 0}, 0, None, 2, 0) != "LOW_CTR"
    big = config.REPRICE_MIN_IMPRESSIONS_FOR_CTR
    assert rp._diagnose({"impressions": big, "views": 0}, 0, None, 2, 0) == "LOW_CTR"
    # Median CTR (0.28%) is fine.
    assert rp._diagnose({"impressions": 2000, "views": 6}, 0, None, 2, 0) != "LOW_CTR"


def test_listing_stats_keeps_organic_and_stores_total():
    item = {"item_id": "stats-1", "status": "published", "created_at": "2025-01-01T00:00:00+00:00",
            "ebay": {"listing_id": "901"}, "listing": {"price": 30.0}}
    traffic = {"901": {"impressions": 566, "organic_impressions": 56, "views": 2,
                       "search_views": 1, "ctr": 0.0}}
    with _Patch(rp, get_watch_count=lambda _id: 3):
        result = rp.evaluate_item(item, traffic)
    row = db.latest_stats("stats-1")
    assert row["impressions"] == 56 and row["total_impressions"] == 566
    assert row["search_views"] == 1 and row["views"] == 2
    assert result["impressions"] == 566 and result["organic_impressions"] == 56


def test_migration_adds_columns_to_an_old_listing_stats_table():
    import sqlite3
    path = str(Path(tempfile.mkdtemp(prefix="mig_")) / "old.db")
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE listing_stats (item_id TEXT NOT NULL, week TEXT NOT NULL, "
                "impressions INTEGER, views INTEGER, watchers INTEGER, price REAL, "
                "verdict TEXT, suggested_price REAL, created_at TEXT NOT NULL, "
                "PRIMARY KEY (item_id, week))")
    con.execute("INSERT INTO listing_stats VALUES ('a','2026-10-19',40,2,1,30,'INVISIBLE',NULL,'x')")
    con.commit()
    con.close()
    with _Patch(db, DB_PATH=path):
        db.create_tables()
        db.create_tables()                       # idempotent
        row = db.latest_stats("a")
    assert row["impressions"] == 40 and row["total_impressions"] is None


# --- 2. the cut suggestion ------------------------------------------------------

def _priced_item(cost=10.0, price=59.99):
    return {"item_id": "cut-" + "x" * 32, "status": "published",
            "created_at": "2025-01-01T00:00:00+00:00",
            "identification": {"search_query": "coach wallet", "brand": "Coach"},
            "receipt": {"reduced_price": cost}, "photos": [],
            "ebay": {"listing_id": "777"}, "listing": {"price": price}}


def _v2(price, sold_median_total, flags=()):
    return {"suggested_price": price, "sold_median": sold_median_total,
            "comp_basis": "item+shipping total", "review_flags": list(flags),
            "needs_review": bool(flags), "engine": "v2"}


class _Engine:
    """Stands in for get_pricing_v2 / get_pricing and counts the calls."""
    def __init__(self, answer):
        self.answer, self.calls = answer, 0

    def __call__(self, *a, **k):
        self.calls += 1
        return self.answer


def _suggest(item, verdict, answer, engine="v2", watchers=0, budget=None, weeks=OLD):
    import pipeline.pricing_v2 as v2
    fake_v2, fake_v1 = _Engine(answer), _Engine(answer)
    with _Patch(config, PRICING_ENGINE=engine), _Patch(v2, get_pricing_v2=fake_v2), \
            _Patch(rp, get_pricing=fake_v1):
        out = rp._suggest_price(item, verdict, item["listing"]["price"], weeks,
                                watchers=watchers, budget=budget)
    return out, fake_v2.calls, fake_v1.calls


def test_overpriced_uses_v2_not_v1():
    (price, note), v2_calls, v1_calls = _suggest(_priced_item(), "OVERPRICED",
                                                 _v2(54.99, 50.0 + config.EBAY_SHIP_CHARGED))
    assert (v2_calls, v1_calls) == (1, 0)
    assert price == 54.99 and note is None


def test_engine_setting_v1_still_honoured():
    v1_answer = {"suggested_price": 54.99, "sold_median": 50.0, "review_flags": []}
    (price, _), v2_calls, v1_calls = _suggest(_priced_item(), "OVERPRICED", v1_answer,
                                              engine="v1")
    assert (v2_calls, v1_calls) == (0, 1) and price == 54.99


def test_never_below_the_sold_median():
    """v2 wants $39.99 but same-item sales median $55 (item basis): hold at $55."""
    (price, _), _, _ = _suggest(_priced_item(), "OVERPRICED",
                                _v2(39.99, 55.0 + config.EBAY_SHIP_CHARGED))
    assert price == 55.0


def test_no_cut_when_comps_sit_above_the_price():
    (price, note), _, _ = _suggest(_priced_item(), "OVERPRICED",
                                   _v2(39.99, 70.0 + config.EBAY_SHIP_CHARGED))
    assert price is None and "sold comps" in note


def test_one_cut_is_capped_at_12_percent():
    (price, _), _, _ = _suggest(_priced_item(price=100.0), "OVERPRICED",
                                _v2(49.99, 20.0 + config.EBAY_SHIP_CHARGED))
    assert price is not None and price >= 88.0 and price < 100.0
    # STALE's old 15% step is capped too.
    (stale, _), _, _ = _suggest(_priced_item(price=100.0), "STALE",
                                _v2(99.99, 20.0 + config.EBAY_SHIP_CHARGED),
                                weeks=config.REPRICE_STALE_WEEKS * 3)
    assert stale is not None and stale >= 88.0


def test_cost_floor_still_holds():
    # Cost $40 -> floor (with the $5 margin) ~ $63.7; sold median is lower.
    item = _priced_item(cost=40.0, price=69.99)
    (price, _), _, _ = _suggest(item, "OVERPRICED", _v2(49.99, 40.0 + config.EBAY_SHIP_CHARGED))
    assert price is not None and price >= rp._cost_floor(item)


def test_review_flags_mean_no_suggestion():
    for flags in (["few_sales"], ["priced_from_asks"], ["judge_low_confidence"]):
        (price, note), _, _ = _suggest(_priced_item(), "OVERPRICED",
                                       _v2(49.99, 60.0 + config.EBAY_SHIP_CHARGED, flags))
        assert price is None and flags[0] in note


def test_no_engine_call_for_visibility_verdicts_or_watched_listings():
    answer = _v2(49.99, 60.0)
    for verdict in ("INVISIBLE", "LOW_CTR", "STALE_UNSEEN", "SEND_OFFERS", "HEALTHY"):
        (price, _), v2_calls, _ = _suggest(_priced_item(), verdict, answer)
        assert price is None and v2_calls == 0
    (price, note), v2_calls, _ = _suggest(_priced_item(), "STALE", answer, watchers=2)
    assert price is None and v2_calls == 0 and "offer" in note


def test_no_engine_call_without_a_cost_floor():
    item = _priced_item()
    item["receipt"] = None
    (price, note), v2_calls, _ = _suggest(item, "OVERPRICED", _v2(49.99, 60.0))
    assert price is None and v2_calls == 0 and "Ross cost" in note


def test_pricing_calls_are_capped_per_run():
    budget = rp.PricingBudget(2)
    calls = 0
    for _ in range(4):
        (_, note), v2_calls, _ = _suggest(_priced_item(), "OVERPRICED",
                                          _v2(54.99, 50.0 + config.EBAY_SHIP_CHARGED),
                                          budget=budget)
        calls += v2_calls
    assert calls == 2 and budget.used == 2 and "budget" in note


def test_engine_failure_is_a_note_not_a_crash():
    import pipeline.pricing_v2 as v2

    def boom(*a, **k):
        raise TimeoutError("apify")
    with _Patch(config, PRICING_ENGINE="v2"), _Patch(v2, get_pricing_v2=boom):
        price, note = rp._suggest_price(_priced_item(), "OVERPRICED", 59.99, OLD, watchers=0)
    assert price is None and "failed" in note


# --- 3. offers to watchers ------------------------------------------------------

def _offer_item(n, price=59.99, cost=10.0, days=30, listing=None):
    when = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    return {"item_id": f"offer{n}".ljust(36, "x"), "status": "published",
            "created_at": when, "receipt": {"reduced_price": cost},
            "ebay": {"listing_id": listing or f"L{n}", "published_at": when},
            "listing": {"price": price, "title": f"Bag {n}"}}


def _result(item, watchers, verdict="SEND_OFFERS"):
    return {"item_id": item["item_id"], "listing_id": item["ebay"]["listing_id"],
            "verdict": verdict, "watchers": watchers,
            "current_price": item["listing"]["price"]}


def test_offer_plan_is_10_percent_most_watched_first():
    a, b = _offer_item(1), _offer_item(2, price=99.99)
    plan = rp.plan_watcher_offers([a, b], [_result(a, 2), _result(b, 9)], {"L1", "L2"}, set())
    assert [r["listing_id"] for r in plan] == ["L2", "L1"]
    assert plan[1]["offer_price"] == 53.99 and plan[0]["offer_price"] == 89.99


def test_offer_plan_gates():
    young, unwatched, unknown, offered, ineligible, ok = (
        _offer_item(1, days=5), _offer_item(2), _offer_item(3), _offer_item(4),
        _offer_item(5), _offer_item(6))
    nocost = _offer_item(7)
    nocost["receipt"] = None
    items = [young, unwatched, unknown, offered, ineligible, ok, nocost]
    results = [_result(young, 3), _result(unwatched, 0), _result(unknown, None),
               _result(offered, 3), _result(ineligible, 3), _result(ok, 1),
               _result(nocost, 3), {"item_id": ok["item_id"], "verdict": "ERROR"}]
    eligible = {"L1", "L2", "L3", "L4", "L6", "L7"}
    plan = rp.plan_watcher_offers(items, results, eligible, {"L4"})
    assert [r["listing_id"] for r in plan] == ["L6"]


def test_offer_never_below_the_auto_decline_floor():
    from ebay.inventory import auto_decline_price
    item = _offer_item(1, price=59.99)
    with _Patch(rp, WATCHER_OFFER_PCT=30.0):
        plan = rp.plan_watcher_offers([item], [_result(item, 4)], {"L1"}, set())
    floor = auto_decline_price(item, 59.99)
    assert plan and plan[0]["offer_price"] == floor           # 75% of $59.99 -> $45
    # A floor that leaves under eBay's 5% minimum discount means no offer.
    tight = _offer_item(2, price=20.0, cost=12.0)
    assert rp.plan_watcher_offers([tight], [_result(tight, 4)], {"L2"}, set()) == []


def _saved_plan(items, watchers=3):
    rows = rp.plan_watcher_offers(items, [_result(i, watchers) for i in items],
                                  {i["ebay"]["listing_id"] for i in items}, set())
    for i in items:
        iid = db.create_item([])
        # Mirror the test item into the DB under the generated id.
        i["item_id"] = iid
        db.update_field(iid, "listing", i["listing"])
        db.update_field(iid, "ebay", i["ebay"])
        db.update_field(iid, "receipt", i["receipt"])
    by_listing = {i["ebay"]["listing_id"]: i["item_id"] for i in items}
    for row in rows:
        row["item_id"] = by_listing[row["listing_id"]]
    return db.save_offer_plan(rows), rows


def test_tap_sends_the_plan_once_and_records_it():
    a, b, c = _offer_item(11, listing="S1"), _offer_item(12, listing="S2"), \
        _offer_item(13, listing="S3")
    plan_id, rows = _saved_plan([a, b, c])
    # b's price moved since the digest; c's eBay eligibility lapsed.
    db.update_field(b["item_id"], "listing", dict(b["listing"], price=49.99))
    sent = []

    def fake_send(listing_id, price, message=None):
        sent.append((listing_id, price))
        return {}

    with _Patch(rp, send_offer=fake_send, eligible_listing_ids=lambda: {"S1", "S2"}):
        res = rp.send_planned_offers(plan_id)
        again = rp.send_planned_offers(plan_id)
    assert sent == [("S1", 53.99)]
    assert [r["listing_id"] for r in res["sent"]] == ["S1"]
    reasons = dict((r["listing_id"], why) for r, why in res["skipped"])
    assert "price changed" in reasons["S2"] and "eligible" in reasons["S3"]
    assert "S1" in db.offers_sent_since("2000-01-01")
    assert again["error"] and "already" in again["error"]
    # Next week's plan skips what was just sent.
    recent = db.offers_sent_since(rp._cooldown_cutoff())
    assert rp.plan_watcher_offers([a], [_result(a, 3)], {"S1"}, recent) == []


def test_a_failed_send_is_reported_and_not_recorded():
    a = _offer_item(21, listing="F1")
    plan_id, _ = _saved_plan([a])

    def failing(*_a, **_k):
        raise RuntimeError("eBay sendOfferToInterestedBuyers failed [400]")

    with _Patch(rp, send_offer=failing, eligible_listing_ids=lambda: {"F1"}):
        res = rp.send_planned_offers(plan_id)
    assert res["sent"] == [] and len(res["failed"]) == 1
    assert "F1" not in db.offers_sent_since("2000-01-01")


def test_an_old_plan_or_failed_eligibility_sends_nothing():
    a = _offer_item(31, listing="O1")
    plan_id, _ = _saved_plan([a])

    def no_eligibility():
        raise RuntimeError("403")

    with _Patch(rp, eligible_listing_ids=no_eligibility):
        res = rp.send_planned_offers(plan_id)
    assert res["error"] and res["sent"] == []
    # The failed read didn't use the plan up...
    assert db.get_offer_plan(plan_id)["used_at"] is None
    # ...but age does.
    later = datetime.now(timezone.utc) + timedelta(days=config.WATCHER_OFFER_PLAN_MAX_AGE_DAYS + 1)

    class _Later(datetime):
        @classmethod
        def now(cls, tz=None):
            return later
    with _Patch(rp, datetime=_Later, eligible_listing_ids=lambda: {"O1"}):
        res = rp.send_planned_offers(plan_id)
    assert "days old" in res["error"]


def test_build_offer_plan_reports_an_eligibility_failure():
    def boom():
        raise RuntimeError("403 scope")
    with _Patch(rp, eligible_listing_ids=boom):
        out = rp.build_offer_plan([], [])
    assert out["rows"] == [] and out["plan_id"] is None and "403" in out["error"]


# --- the digest: one grouped message + one button -----------------------------

class _Bot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, reply_markup=None):
        self.sent.append((text, reply_markup))


def _digest_row(verdict, n, suggested=None):
    return {"item_id": f"{verdict.lower()}{n}".ljust(36, "x"), "verdict": verdict,
            "impressions": 600, "organic_impressions": 50, "views": 2,
            "cumulative_views": 9, "ctr": None, "watchers": 2,
            "current_price": 39.99, "suggested_price": suggested}


def test_digest_shows_the_offer_plan_with_one_button():
    import telegram_bot as tb
    watched = [_digest_row("SEND_OFFERS", n) for n in range(6)]
    plan_rows = [{"item_id": r["item_id"], "listing_id": f"L{n}", "price": 39.99,
                  "offer_price": 35.99, "pct": 10.0, "watchers": 2, "title": "Bag"}
                 for n, r in enumerate(watched[:4])]
    results = ([_digest_row("INVISIBLE", n) for n in range(3)] + watched
               + [_digest_row("OVERPRICED", 1, suggested=35.99)])
    health = {"traffic_batches": 6, "traffic_failed": 0, "watch_lookups": 10,
              "watch_failed": 0, "offers": {"plan_id": "p" * 36, "rows": plan_rows,
                                            "error": None}}
    bot = _Bot()
    asyncio.run(tb._send_reprice_digest(bot, 1, results, health))
    # header + grouped list + offer plan + one Apply
    assert len(bot.sent) == 4
    grouped = bot.sent[1][0]
    assert "(3)" in grouped and "(2)" in grouped      # 2 SEND_OFFERS left unplanned
    offer_text, markup = bot.sent[2]
    assert offer_text.count("•") == 4 and "$35.99" in offer_text
    buttons = [b for row in markup.inline_keyboard for b in row]
    assert len(buttons) == 1 and "4 watched listings" in buttons[0].text
    assert buttons[0].callback_data == "reprice:offers:-:" + "p" * 36
    assert len(buttons[0].callback_data.encode()) <= 64


def test_digest_header_says_when_offers_could_not_be_planned():
    import telegram_bot as tb
    bot = _Bot()
    health = {"offers": {"plan_id": None, "rows": [], "error": "RuntimeError: 403"}}
    asyncio.run(tb._send_reprice_digest(bot, 1, [_digest_row("HEALTHY", 1)], health))
    assert len(bot.sent) == 1 and "Offers to watchers not planned" in bot.sent[0][0]


def test_offers_button_sends_through_the_plan():
    import telegram_bot as tb
    calls, replies = [], []

    class _Msg:
        pass

    class _Query:
        data = "reprice:offers:-:" + "q" * 36
        message = _Msg()

        async def answer(self):
            pass

        async def edit_message_reply_markup(self, reply_markup=None):
            pass

    class _Update:
        callback_query = _Query()

    def fake_plan_send(plan_id):
        calls.append(plan_id)
        return {"sent": [{"item_id": "a" * 36, "price": 39.99, "offer_price": 35.99,
                          "watchers": 3, "title": "Bag"}],
                "skipped": [], "failed": [], "error": None}

    async def fake_reply(_msg, text, **_k):
        replies.append(text)

    async def fake_chunked(_msg, lines):
        replies.append("\n".join(lines))

    with _Patch(tb, send_planned_offers=fake_plan_send, _safe_reply=fake_reply,
                _send_chunked=fake_chunked):
        asyncio.run(tb.reprice_callback(_Update(), None))
    assert calls == ["q" * 36]
    assert any("Sent 1 offer" in r for r in replies)


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
