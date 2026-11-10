"""/diagnose (pipeline/diagnose.py, bot/diagnose_cmds.py): one primary
verdict per live listing with a reason and one fix. eBay, Apify and the DB
are faked. The MK MKS8006 band (15 watchers, an offer sent Nov 2, no sale,
sold comps around $78) is the main case."""
from datetime import datetime, timedelta, timezone

import config
import db
import profit
from bot import diagnose_cmds
from ebay import analytics
from pipeline import diagnose
from tests import tg_fakes

NOW = datetime(2026, 11, 9, 20, tzinfo=timezone.utc)
OWNER = tg_fakes.DEFAULT_USER_ID


def _m(**kw):
    base = {"item_id": "00c8d805-x", "listing_id": "117361213532", "title": "Michael Kors Band MKS8006",
            "price": 100.0, "category_id": "98624", "days": 30.0, "watchers": 0, "offered": False,
            "sales": 0, "traffic_known": True, "impressions": 3000, "views": 20,
            "impressions_per_day": 100.0, "ctr": 20 / 3000}
    base.update(kw)
    return base


BENCH = {"impressions_per_day": 100.0, "ctr": 0.006, "n": 10, "source": "store average"}


# --- verdict order -------------------------------------------------------------

def test_unknown_traffic_and_too_few_impressions_are_not_enough_data():
    assert diagnose.verdict(_m(traffic_known=False), BENCH) == "NOT_ENOUGH_DATA"
    assert diagnose.verdict(_m(impressions=400, views=1), BENCH) == "NOT_ENOUGH_DATA"   # 400 x 0.6% = 2.4


def test_watchers_come_before_price_and_split_on_offers():
    assert diagnose.verdict(_m(watchers=15, offered=True), BENCH) == "WATCHERS_OFFER_SENT"
    assert diagnose.verdict(_m(watchers=15), BENCH) == "WATCHERS"
    assert diagnose.verdict(_m(watchers=15, sales=1), BENCH) != "WATCHERS"


def test_unknown_watchers_fall_through_to_the_other_checks():
    assert diagnose.verdict(_m(watchers=None, views=50), BENCH) == "PRICE"
    assert diagnose.verdict(_m(watchers=None, views=5, ctr=5 / 3000), BENCH) == "SEARCH_CARD"


def test_search_card_visibility_and_ok():
    assert diagnose.verdict(_m(views=5, ctr=5 / 3000), BENCH) == "SEARCH_CARD"       # 0.17% < 0.3%
    assert diagnose.verdict(_m(impressions_per_day=30.0, impressions=900, views=6, ctr=6 / 900),
                            BENCH) == "VISIBILITY"
    assert diagnose.verdict(_m(), BENCH) == "OK"


# --- reasons and fixes ----------------------------------------------------------------

ITEM = {"item_id": "00c8d805-x", "listing": {"title": "MK band", "price": 100.0, "item_specifics": {"A": 1}},
        "photos": ["a", "b", "c"]}


def _comps(prices, how="mpn", active=()):
    return {"sold": [{"price": p} for p in prices], "active": list(active),
            "found_by": {"sold": how, "active": "upc"}}


def test_offer_already_sent_suggests_a_market_price_above_the_floor(monkeypatch):
    monkeypatch.setattr(profit, "breakeven_price", lambda item, margin=0: 46.0)
    code, reason, fix = diagnose.explain("WATCHERS_OFFER_SENT", _m(watchers=15, offered=True), BENCH, ITEM,
                                         _comps([77.99, 78.0, 80.0, 67.5, 85.0, 94.99]))
    assert code == "WATCHERS_OFFER_SENT"
    assert "an offer already went out" in reason and "median $79.00" in reason and "n=6" in reason
    assert fix == "Price is above the market: /setprice $78.99 (floor $46.00)."


def test_the_floor_wins_over_cheap_comps(monkeypatch):
    monkeypatch.setattr(profit, "breakeven_price", lambda item, margin=0: 95.0)
    _, _, fix = diagnose.explain("PRICE", _m(views=50), BENCH, ITEM, _comps([60.0, 62.0, 65.0]))
    assert "/setprice $95.99" in fix


def test_watchers_get_an_offer_only_when_comps_support_it(monkeypatch):
    monkeypatch.setattr(profit, "breakeven_price", lambda item, margin=0: None)
    code, _, fix = diagnose.explain("WATCHERS", _m(watchers=15), BENCH, ITEM, _comps([95.0, 99.0, 105.0]))
    assert code == "WATCHERS" and "offer around $90.00" in fix
    # comps below the $90 offer: the fix is a price, not another offer (owner, 2026-11-09)
    code, _, fix = diagnose.explain("WATCHERS", _m(watchers=15), BENCH, ITEM, _comps([77.99, 78.0, 80.0]))
    assert code == "PRICE" and "/setprice" in fix


def test_thin_or_skipped_comps_give_no_price():
    code, reason, fix = diagnose.explain("PRICE", _m(views=50), BENCH, ITEM, _comps([80.0]))
    assert "1 sold comp in 90 days, too few" in reason and "by hand" in fix
    _, reason, _ = diagnose.explain("PRICE", _m(views=50), BENCH, ITEM, {**_comps([]), "sold_skipped": True})
    assert "Apify budget" in reason


def test_search_card_compares_totals_and_never_cuts_price():
    active = [{"total": 85.99, "found_by": "upc"}, {"total": 90.0, "found_by": "mpn"},
              {"total": 20.0, "found_by": "keywords"}]
    _, reason, fix = diagnose.explain("SEARCH_CARD", _m(ctr=0.0017), BENCH, ITEM, _comps([], active=active))
    assert "Our total $109.99 vs same-item competitors' median $88.00" in reason
    assert "first photo" in fix and "don't cut the price" in fix


def test_visibility_names_what_competitors_have():
    active = [{"photo_count": 8, "specifics": {str(n): n for n in range(12)}, "shipping": 0.0},
              {"photo_count": 7, "specifics": {str(n): n for n in range(10)}, "shipping": 0.0},
              {"photo_count": 9, "specifics": {}, "shipping": 5.0}]
    _, _, fix = diagnose.explain("VISIBILITY", _m(impressions_per_day=20.0), BENCH, ITEM,
                                 _comps([], active=active))
    assert "10 item specifics vs our 1" in fix and "8 photos vs our 3" in fix and "2 of 3 offer free shipping" in fix


def test_benchmarks_are_per_category_and_skip_unknown_traffic():
    ms = [_m(category_id="a", impressions=1000, views=10, impressions_per_day=50.0),
          _m(category_id="a", impressions=3000, views=10, impressions_per_day=150.0),
          _m(category_id="b", traffic_known=False)]
    b = diagnose.benchmarks(ms)
    assert set(b) == {"a"} and b["a"]["impressions_per_day"] == 100.0 and b["a"]["ctr"] == 20 / 4000
    assert b["a"]["source"] == "store average"


# --- the whole run ----------------------------------------------------------------------

def _item(n, cat="98624", **ebay):
    return {"item_id": f"item{n}-x", "status": "published",
            "listing": {"title": f"Listing {n}", "price": 100.0, "category_id": cat, "item_specifics": {}},
            "ebay": {"listing_id": f"L{n}", "published_at": (NOW - timedelta(days=60)).isoformat(), **ebay}}


def _run(monkeypatch, items, traffic, watchers=None, offered=(), search=None, **cfg):
    monkeypatch.setattr(db, "list_items", lambda: items)
    monkeypatch.setattr(db, "offers_sent_since", lambda since: set(offered))
    monkeypatch.setattr(analytics, "get_traffic_report", lambda ids, days: {k: v for k, v in traffic.items() if k in ids})
    monkeypatch.setattr(analytics, "get_watch_count", lambda lid: (watchers or {}).get(lid))
    monkeypatch.setattr(profit, "breakeven_price", lambda item, margin=0: 40.0)
    for k, v in cfg.items():
        monkeypatch.setattr(config, k, v)
    calls = []

    def fake_search(ident, **kw):
        calls.append(kw)
        return search(kw) if search else {"active": [], "sold": [], "found_by": {"active": None, "sold": None}}
    return diagnose.diagnose(now=NOW, search=fake_search), calls


def test_ranked_worst_first_with_comps_only_where_needed(monkeypatch):
    items = [_item(1), _item(2), _item(3), _item(4)]
    traffic = {"L1": {"impressions": 3000, "views": 20}, "L2": {"impressions": 3000, "views": 60},
               "L3": {"impressions": 100, "views": 0}}            # L4 missing from a good batch: zero
    sold = lambda kw: {"active": [], "sold": [{"price": 78.0}, {"price": 79.0}, {"price": 80.0}],
                       "found_by": {"active": None, "sold": "mpn"}}
    results, calls = _run(monkeypatch, items, traffic, watchers={"L1": 15}, offered={"L1"}, search=sold)
    assert [(r["item_id"][:5], r["verdict"]) for r in results] == [
        ("item3", "NOT_ENOUGH_DATA"), ("item4", "NOT_ENOUGH_DATA"),
        ("item1", "WATCHERS_OFFER_SENT"), ("item2", "PRICE")]
    assert [c["sold"] for c in calls] == [True, True]
    assert "/setprice $78.99" in results[2]["fix"]


def test_a_failed_traffic_batch_is_unknown_not_zero(monkeypatch):
    def broken(ids, days):
        raise RuntimeError("429")
    monkeypatch.setattr(db, "list_items", lambda: [_item(1)])
    monkeypatch.setattr(db, "offers_sent_since", lambda since: set())
    monkeypatch.setattr(analytics, "get_traffic_report", broken)
    monkeypatch.setattr(analytics, "get_watch_count", lambda lid: None)
    [r] = diagnose.diagnose(now=NOW, search=lambda *a, **k: None)
    assert r["verdict"] == "NOT_ENOUGH_DATA" and "couldn't be read" in r["reason"]


def test_sold_searches_stop_at_the_budget(monkeypatch):
    items = [_item(n) for n in range(1, 5)]
    traffic = {f"L{n}": {"impressions": 3000, "views": 60} for n in range(1, 5)}
    results, calls = _run(monkeypatch, items, traffic, DIAGNOSE_SOLD_MAX=2)
    assert [c["sold"] for c in calls] == [True, True, False, False]
    assert sum("Apify budget" in r["reason"] for r in results) == 2


def test_one_listing_still_uses_every_listing_for_benchmarks(monkeypatch):
    items = [_item(1), _item(2)]
    traffic = {"L1": {"impressions": 3000, "views": 4}, "L2": {"impressions": 3000, "views": 40}}
    monkeypatch.setattr(db, "list_items", lambda: items)
    monkeypatch.setattr(db, "offers_sent_since", lambda since: set())
    monkeypatch.setattr(analytics, "get_traffic_report", lambda ids, days: traffic)
    monkeypatch.setattr(analytics, "get_watch_count", lambda lid: 0)
    [r] = diagnose.diagnose(["item1-x"], now=NOW, search=lambda *a, **k: None)
    assert r["verdict"] == "SEARCH_CARD" and r["benchmark"]["n"] == 2


# --- the command --------------------------------------------------------------------------

def test_owner_only(monkeypatch):
    monkeypatch.setattr(config, "OWNER_USER_IDS", set())
    update, ctx = tg_fakes.make_text_update("/diagnose all")
    tg_fakes.run(diagnose_cmds.diagnose_command, update, ctx)
    assert any("owner-only" in t for t in tg_fakes.sent_texts(ctx))


def test_all_lists_problems_worst_first(monkeypatch):
    monkeypatch.setattr(config, "OWNER_USER_IDS", {OWNER})
    fake = [{**_m(item_id="aaaa1111-x", watchers=15, offered=True), "verdict": "WATCHERS_OFFER_SENT",
             "reason": "r1", "fix": "f1", "benchmark": BENCH},
            {**_m(item_id="bbbb2222-x"), "verdict": "OK", "reason": "-", "fix": "-", "benchmark": BENCH}]
    monkeypatch.setattr(diagnose, "diagnose", lambda ids: fake)
    update, ctx = tg_fakes.make_text_update("/diagnose all", user_id=OWNER)
    tg_fakes.run(diagnose_cmds.diagnose_command, update, ctx)
    text = "\n".join(tg_fakes.sent_texts(ctx))
    assert "2 live listings" in text and "WATCHERS, NO SALES, OFFER SENT 1" in text and "OK 1" in text
    assert "aaaa1111" in text and "bbbb2222" not in text and "→ f1" in text
