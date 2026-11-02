"""pipeline/check.py: the /check pre-buy check (docs/check-command-design.md)."""
from datetime import datetime, timezone

import pytest

import config
from pipeline import check


@pytest.fixture(autouse=True)
def rates(monkeypatch):
    """The design's numbers, pinned so a config change doesn't move the tests."""
    for name, value in (("EBAY_FVF_PCT", 0.155), ("EBAY_AD_FEE_PCT", 0.046), ("EBAY_FIXED_FEE", 0.40),
                        ("EBAY_SHIP_CHARGED", 9.88), ("EBAY_SHIP_COST", 7.20), ("SALES_TAX_RATE", 0.07),
                        ("UNDERCUT_V2_PCT", 0.05), ("CHECK_BUY_MIN_PROFIT", 15.0),
                        ("CHECK_BUY_MIN_ROI", 0.75), ("CHECK_MIN_SALES", 3), ("CHECK_MIN_PROFIT", 5.0),
                        ("CHECK_MIN_PROFIT_AT_CHEAPEST", 5.0), ("CHECK_SOLD_CACHE_DAYS", 7.0),
                        ("CHECK_LABEL_COSTS", "bags=11.00,jewelry=4.50,shoes=12.00,clothing=7.20,other=7.20")):
        monkeypatch.setattr(config, name, value)


# --- parsing -----------------------------------------------------------------------

@pytest.mark.parametrize("text,desc,price,exact,full", [
    ("/check Coach Tabby 26 CH857 49.99", "Coach Tabby 26 CH857", 49.99, True, False),
    ("/check any full Burberry London 1.7 oz $14.99", "Burberry London 1.7 oz", 14.99, False, True),
    ("/check Full Zip Hoodie 12", "Full Zip Hoodie", 12.0, True, False),    # option words only at the start
    ("/check $8.99 nike socks 6 pack", "nike socks 6 pack", 8.99, True, False),
    ("/check coach tabby", "coach tabby", None, True, False),
    ("/check@my_bot exact quick dkny tote 19,99", "dkny tote", 19.99, True, False),
])
def test_parse_request(text, desc, price, exact, full):
    r = check.parse_request(text)
    assert (r["description"], r["ross_price"], r["exact"], r["full"]) == (desc, price, exact, full)


def test_build_identification_pulls_code_and_upc():
    ident = check.build_identification("Nike Academy Team Backpack DV0761-458 (new) 196153123456")
    assert ident["model"] == "DV0761-458" and ident["upc"] == "196153123456"
    assert ident["search_query"] == "Nike Academy Team Backpack DV0761-458 196153123456"
    assert "model" not in check.build_identification("samsung 128gb card 1080p")


@pytest.mark.parametrize("text,kind,label", [
    ("Coach Tabby shoulder bag", "bags", 11.0), ("MK necklace gold", "jewelry", 4.5),
    ("Nike sneakers size 9", "shoes", 12.0), ("polo shirt", "clothing", 7.2), ("lego set", "other", 7.2),
])
def test_label_by_item_type(text, kind, label):
    assert check.label_for(check.build_identification(text), text) == (kind, label)


# --- money --------------------------------------------------------------------------

def test_worked_example_from_the_design():
    m = check.economics(39.99, 14.99, 7.20)
    assert (m["total"], m["fees"], m["keep"], m["cost"], m["profit"], m["roi"]) == \
        (49.87, 10.42, 32.25, 16.04, 16.21, 1.01)
    assert (m["break_even_total"], m["break_even_item"]) == (29.59, 19.71)
    assert check.at_total(44.50, 14.99, 7.20)["profit"] == 11.92
    # at break-even the profit is zero
    assert abs(check.economics(m["break_even_item"], 14.99, 7.20)["profit"]) <= 0.01


# --- verdict ------------------------------------------------------------------------

def v(**kw):
    base = dict(profit=20.0, roi=1.0, sales=3, profit_at_cheapest=8.0)
    return check.verdict(**{**base, **kw})[0]


def test_verdict_edges():
    assert v() == "BUY"
    assert v(profit=14.99) == "BORDERLINE" and v(roi=0.74) == "BORDERLINE"
    assert v(profit_at_cheapest=4.99) == "BORDERLINE" and v(profit_at_cheapest=None) == "BUY"
    assert v(sales=2) == "MAYBE" and v(from_asks=True) == "MAYBE"
    assert v(low_confidence=True) == "MAYBE" and v(disagrees=True) == "MAYBE"
    assert v(profit=5.0, roi=0.3) == "BORDERLINE" and v(profit=4.99) == "SKIP"
    assert v(profit=10.0, profit_at_cheapest=-1.0, sales=2) == "SKIP"
    assert v(profit=None) == "SKIP"


# --- choosing the price ---------------------------------------------------------------

JUDGED = [
    {"source": "sold", "verdict": "same_item", "total": 50.0},
    {"source": "sold", "verdict": "same_item", "total": 54.0},
    {"source": "sold", "verdict": "variant", "total": 40.0},
    {"source": "active", "verdict": "same_item", "total": 44.5},
]


def pricing(**kw):
    base = {"suggested_price": 39.99, "formula_price": 37.99, "raw_active_floor": 44.5,
            "sold_median": 52.0, "judged_comps": JUDGED, "review_flags": [],
            "claude_confidence": "high", "same_item_active_count": 1,
            "timings": {"comps_s": 1.5, "judge_s": 9.0, "total_s": 11.0}, "sold_cache_misses": []}
    return {**base, **kw}


def test_exact_uses_the_same_item_formula_and_any_uses_claude():
    e = check.choose_price(pricing(), exact=True)
    assert (e["price"], e["sales"], e["from_asks"]) == (37.99, 2, False)
    a = check.choose_price(pricing(), exact=False)
    assert (a["price"], a["sales"], a["sold_median"]) == (39.99, 3, 50.0)


def test_exact_without_sales_prices_from_the_cheapest_identical_listing():
    e = check.choose_price(pricing(formula_price=None), exact=True)
    assert e["from_asks"] and e["price"] == check._charm(44.5 * 0.95 - 9.88)


# --- the orchestrator -------------------------------------------------------------------

def test_quick_text_check_passes_quick_options_and_stores_nothing():
    seen = {}

    def fake_pricing(ident, **kw):
        seen.update(kw, ident=ident)
        return pricing(sold_cache_misses=["coach tabby 26"], formula_price=None)

    res = check.run_check(check.parse_request("/check Coach Tabby 26 CH857 bag 14.99"),
                          pricing_fn=fake_pricing, have_fn=lambda ident: [])
    assert seen["sold_cache_only"] is True and seen["escalate"] is False
    assert seen["sold_max_age_s"] == 7 * 86400 and seen["active_max_age_s"] == 0
    assert seen["stage"] == "check_quick_judge" and seen["item_id"] is None
    assert seen["ident"]["model"] == "CH857"
    assert res["item_type"] == "bags" and res["label"] == 11.0
    # no same-item sales: priced from the cheapest identical ask ($31.99), which
    # with a bag's $11 label leaves $6.01
    assert res["pick"]["from_asks"] and res["money"]["profit"] == 6.01
    assert res["verdict"] == "BORDERLINE"
    assert "priced from asking prices, not sales" in res["reasons"]
    text = check.format_reply(res)
    assert "Tap Full check" in text and "exact matches" in text and "Break-even" in text


def test_full_any_check_can_say_buy():
    seen = {}

    def fake_pricing(ident, **kw):
        seen.update(kw)
        return pricing(suggested_price=44.99)

    res = check.run_check(check.parse_request("/check any full Coach Tabby 26 CH857 14.99"),
                          pricing_fn=fake_pricing, have_fn=lambda ident: ["3f2a1b2c (published)"])
    assert seen["sold_cache_only"] is False and seen["escalate"] is True and seen["stage"] == "check_judge"
    assert res["verdict"] == "BUY", res["reasons"]
    assert "Already in our items: 3f2a1b2c" in check.format_reply(res)


def test_missing_ross_price_is_a_skip_with_a_hint():
    res = check.run_check(check.parse_request("/check coach tabby"),
                          pricing_fn=lambda ident, **kw: pricing(), have_fn=lambda i: [])
    assert res["verdict"] == "SKIP" and "Ross price missing" in check.format_reply(res)


def test_photo_check_identifies_first(tmp_path):
    photo = tmp_path / "p.jpg"
    photo.write_bytes(b"x")
    res = check.run_check(check.parse_request("/check 24.99"), photo_path=str(photo),
                          identify_fn=lambda p: {"brand": "Coach", "product_name": "Tabby 26",
                                                 "item_type": "Shoulder Bag"},
                          pricing_fn=lambda ident, **kw: pricing(), have_fn=lambda i: [])
    assert res["identification"]["brand"] == "Coach" and res["item_type"] == "bags"
    assert "identify_s" in res["timings"] and res["photo"] is True


def test_quick_with_nothing_at_all_is_no_data_but_full_is_skip():
    empty = pricing(suggested_price=None, formula_price=None, raw_active_floor=None,
                    judged_comps=[], sold_median=None)
    q = check.evaluate(empty, {}, 14.99, exact=True, quick=True)
    assert q["verdict"] == check.NO_DATA and q["reasons"]
    assert check.evaluate(empty, {}, 14.99, exact=True, quick=False)["verdict"] == "SKIP"
    # any data at all (an identical listing) is a real verdict even in quick mode
    some = pricing(formula_price=None, judged_comps=[])
    assert check.evaluate(some, {}, 14.99, exact=True, quick=True)["verdict"] != check.NO_DATA
    res = check.run_check(check.parse_request("/check lego set 9.99"),
                          pricing_fn=lambda ident, **kw: empty, have_fn=lambda i: [])
    assert check.format_reply(res).startswith("❓ NO DATA")


def test_a_follow_up_reuses_the_identification():
    seen = []
    req = {**check.parse_request("/check full 9.99"), "identification": {"brand": "Coach", "model": "CH857"}}
    check.run_check(req, photo_path="x.jpg", identify_fn=lambda p: seen.append(p) or {},
                    pricing_fn=lambda ident, **kw: seen.append(ident) or pricing(), have_fn=lambda i: [])
    assert seen == [{"brand": "Coach", "model": "CH857"}]      # identify_fn not called


# --- timings log and daily cap -------------------------------------------------------------

def test_timings_log_and_daily_count(tmp_path):
    path = tmp_path / "check_timings.jsonl"
    res = check.run_check(check.parse_request("/check coach tabby 14.99"),
                          pricing_fn=lambda ident, **kw: pricing(), have_fn=lambda i: [])
    check.log_timings(res, user_id=1, path=path)
    check.log_timings(res, user_id=1, path=path)
    row = path.read_text(encoding="utf-8").splitlines()[0]
    assert '"mode": "quick"' in row and '"judge_s": 9.0' in row and '"total_s"' in row
    assert check.checks_today(path) == 2
    check.log_timings(res, user_id=1, path=path, counted=False)   # a NO_DATA quick run before a full one
    assert check.checks_today(path) == 2
    # 03:59 UTC on Nov 3 is still Nov 2 in New York: yesterday's rows don't count
    tomorrow = datetime(2099, 1, 1, tzinfo=timezone.utc)
    assert check.checks_today(path, now=tomorrow) == 0
