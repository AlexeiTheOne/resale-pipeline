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
                          pricing_fn=fake_pricing, have_fn=lambda *a: [])
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
    assert len(text.splitlines()) <= 5 and "quick · exact" in text
    assert "sell ~$31.99 (asking) · profit ~$6.01 (asking)" in text and "ROI" not in text
    details = check.format_details(res)
    assert "break-even" in details and "Quick check: no recent sold data" in details


def test_full_any_check_can_say_buy():
    seen = {}

    def fake_pricing(ident, **kw):
        seen.update(kw)
        return pricing(suggested_price=44.99)

    res = check.run_check(check.parse_request("/check any full Coach Tabby 26 CH857 14.99"),
                          pricing_fn=fake_pricing, have_fn=lambda *a: ["item 3f2a1b2c (published)"])
    assert seen["sold_cache_only"] is False and seen["escalate"] is True and seen["stage"] == "check_judge"
    assert res["verdict"] == "BUY", res["reasons"]
    assert "⚠️ in stock: item 3f2a1b2c" in check.format_reply(res)


def test_missing_ross_price_is_a_skip_with_a_hint():
    res = check.run_check(check.parse_request("/check coach tabby"),
                          pricing_fn=lambda ident, **kw: pricing(), have_fn=lambda *a: [])
    assert res["verdict"] == "SKIP" and "Ross price missing" in check.format_reply(res)


def test_photo_check_identifies_first(tmp_path):
    photo = tmp_path / "p.jpg"
    photo.write_bytes(b"x")
    res = check.run_check(check.parse_request("/check 24.99"), photo_path=str(photo),
                          identify_fn=lambda p: {"brand": "Coach", "product_name": "Tabby 26",
                                                 "item_type": "Shoulder Bag"},
                          pricing_fn=lambda ident, **kw: pricing(), have_fn=lambda *a: [])
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
                          pricing_fn=lambda ident, **kw: empty, have_fn=lambda *a: [])
    assert check.format_reply(res).startswith("❓ NO DATA")


def test_a_follow_up_reuses_the_identification():
    seen = []
    req = {**check.parse_request("/check full 9.99"), "identification": {"brand": "Coach", "model": "CH857"}}
    check.run_check(req, photo_path="x.jpg", identify_fn=lambda p: seen.append(p) or {},
                    pricing_fn=lambda ident, **kw: seen.append(ident) or pricing(), have_fn=lambda *a: [])
    assert seen == [{"brand": "Coach", "model": "CH857"}]      # identify_fn not called


# --- reply format (Person B's feedback on the MKJ3969710 test) --------------------------------

def mkj_pricing(**kw):
    """The real full check of 'Michael Kors pave necklace MKJ3969710': no sales,
    one $160.19 listing the judge wasn't sure about."""
    return pricing(suggested_price=139.99, formula_price=None, raw_active_floor=160.19, sold_median=None,
                   judged_comps=[{"source": "active", "verdict": "same_item", "total": 160.19,
                                  "title": "MK pave necklace", "url": "https://ebay.com/itm/1"}],
                   review_flags=["priced_from_asks", "judge_low_confidence"], claude_confidence="low",
                   same_item_active_count=1, **kw)


def test_headline_is_verdict_and_evidence_and_profit_is_marked_asking():
    res = check.run_check(check.parse_request("/check full Michael Kors pave necklace MKJ3969710 14.99"),
                          pricing_fn=lambda ident, **kw: mkj_pricing(), have_fn=lambda *a: [])
    text = check.format_reply(res, check_id=12)
    lines = text.splitlines()
    assert len(lines) <= 5
    assert lines[0] == "🟡 MAYBE: no sales found, 1 uncertain listing"
    assert lines[1] == "Michael Kors pave necklace MKJ3969710"            # model not repeated
    # asking only: the cheapest ask x 0.98 (measured) x 0.95 (undercut) - our shipping
    assert res["pick"]["price"] == check._charm(160.19 * 0.98 * 0.95 - 9.88) == 138.99
    assert lines[2].startswith("Ross $14.99 · sell ~$138.99 (asking) · profit ~$") and "(asking)" in lines[2]
    assert lines[3] == "Evidence: sold 0 · for sale 1 identical (cheapest $160.19) · match uncertain"
    assert lines[4].startswith("#12 · ⏱") and "full · exact" in lines[4]
    assert "no sales found" in res["reasons"] and not any("only 0" in r for r in res["reasons"])
    details = check.format_details(res, check_id=12)
    assert "×0.98" in details and "Fees + ads" in details and "label $4.50 (jewelry)" in details
    assert "https://ebay.com/itm/1" in details and "LH_Sold=1" in details


def test_item_links_lose_their_tracking():
    long = ("https://www.ebay.com/itm/276922449344?_skw=Michael+Kors&hash=item4079dd3dc0:g:C6MAAOSw"
            "&amdata=enc%3AAQALAAAB")
    assert check.short_url(long) == "https://www.ebay.com/itm/276922449344"
    assert check.short_url("https://www.ebay.com/itm/some-title/123456789012") == \
        "https://www.ebay.com/itm/123456789012"
    assert check.short_url(None) == ""


def test_any_mode_from_asks_is_discounted_too():
    pick = check.choose_price(mkj_pricing(), exact=False)
    assert pick["from_asks"] and pick["price"] == check._charm(139.99 * 0.98) and pick["ask_discount"] == 0.98


def test_sale_wording():
    assert check.verdict(profit=20, roi=1, sales=1, profit_at_cheapest=None)[1] == ["only 1 sale (need 3)"]
    assert check.verdict(profit=20, roi=1, sales=2, profit_at_cheapest=None)[1] == ["only 2 sales (need 3)"]


# --- in-stock warning: items AND pieces (production has piece O1W-01) ---------------------------

def _mk_piece():
    import db
    code = db.add_piece("O1W")
    db.update_piece(code, info={"brand": "Michael Kors", "item_type": "necklace", "style_code": "MKJ3969710",
                                "upc": "796483413276", "ross_code": "400289769542"})
    return code


def test_in_stock_warning_finds_a_piece_by_style_code_upc_or_ross_sku():
    code = _mk_piece()
    by_code = check.already_have(check.build_identification("Michael Kors pave necklace MKJ3969710"))
    assert by_code and by_code[0].startswith(f"piece {code}") and "Michael Kors necklace" in by_code[0]
    assert check.already_have({"upc": "796483413276"})
    assert check.already_have({}, "400289769542")                      # the Ross tag's SKU
    assert check.already_have({"brand": "Coach", "model": "CH857"}) == []


def test_in_stock_warning_finds_an_item_and_shows_in_the_reply():
    import db
    item = db.create_item([])
    db.update_field(item, "identification", {"brand": "Michael Kors", "model": "MKJ3969710"})
    res = check.run_check(check.parse_request("/check Michael Kors pave necklace MKJ3969710 14.99"),
                          pricing_fn=lambda ident, **kw: mkj_pricing())
    assert f"item {item[:8]}" in check.format_reply(res).splitlines()[1]


# --- timings log and daily cap -------------------------------------------------------------

def test_timings_log_and_daily_count(tmp_path):
    path = tmp_path / "check_timings.jsonl"
    res = check.run_check(check.parse_request("/check coach tabby 14.99"),
                          pricing_fn=lambda ident, **kw: pricing(), have_fn=lambda *a: [])
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
