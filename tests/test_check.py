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
                        ("CHECK_LABEL_COSTS",
                         "bags=11.00,small_bags=5.50,jewelry=4.50,shoes=12.00,clothing=7.20,other=7.20"),
                        ("CHECK_SMALL_BAG_MAX_IN", 11.0)):
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


@pytest.fixture(autouse=True)
def no_history(monkeypatch):
    """Our own sales (levels 3-4) come from the DB; tests pass them in explicitly."""
    monkeypatch.setattr(check, "load_our_sales", lambda: [])
    from ebay import browse
    monkeypatch.setattr(browse, "listing_details", lambda i: (_ for _ in ()).throw(AssertionError("Browse")))


def test_ladder_level1_same_item_sales_median_and_the_ceiling():
    p = pricing(raw_active_floor=None)
    e = check.choose_price(p, exact=True)
    assert e["used"] == [1] and e["sales"] == 2 and e["levels"][1]["value"] == 42.12 and e["price"] == 41.99
    a = check.choose_price(p, exact=False)           # any: variants count as level 1
    assert a["sales"] == 3 and a["levels"][1]["value"] == 40.12 and a["price"] == 39.99
    capped = check.choose_price(pricing(), exact=True)  # cheapest identical 44.50 x 0.98 - 9.88 = 33.73
    assert capped["capped"] and capped["price"] == 33.99


def test_asking_prices_are_never_the_price():
    p = pricing(judged_comps=[{"source": "active", "verdict": "same_item", "total": 44.5}])
    e = check.choose_price(p, exact=True)
    assert e["price"] is None and e["used"] == [] and e["levels"][5]["value"] == 33.73
    assert check.evaluate(p, {}, 14.99, exact=True)["verdict"] == check.NO_DATA


JUICY = {"brand": "Juicy Couture", "item_type": "Weekender Tote Bag", "retail_price": 109.0,
         "product_name": "Juicy To Go Weekender Tote", "model": "11JCDA0JC-JTL"}
BAGS = [{"item_id": f"b{i}", "brand": "coach", "bucket": "bag", "msrp": 100.0, "sold": 47.0, "ratio": 0.47}
        for i in range(3)]


def test_check2_ladder_levels_2_and_4_median_with_range():
    """Check #2 as it really came out: one pink variant sale ($30.49 total),
    no Juicy sales of our own, MSRP $109 x our bag ratio 0.47."""
    p = pricing(raw_active_floor=104.05,
                judged_comps=[{"source": "sold", "verdict": "variant", "total": 30.49},
                              {"source": "active", "verdict": "same_item", "total": 104.05}])
    pick = check.choose_price(p, exact=True, ident=JUICY, history=BAGS)    # choose_price takes the verdicts as given
    assert pick["levels"][2]["value"] == 20.61 and pick["levels"][4]["value"] == 50.99
    assert 3 not in pick["levels"]                    # we never sold a Juicy Couture bag
    assert pick["used"] == [2, 4] and pick["price"] == 35.99 and pick["range"] == (20.61, 50.99)
    assert pick["levels"][5]["value"] == 92.09 and not pick["capped"] and pick["estimated"]


def test_level3_our_own_sales_same_brand_and_type_with_leave_one_out():
    hist = BAGS + [{"item_id": "j1", "brand": "juicy couture", "bucket": "bag", "msrp": None, "sold": 59.99,
                    "ratio": None},
                   {"item_id": "j2", "brand": "juicy couture", "bucket": "bag", "msrp": None, "sold": 69.99,
                    "ratio": None}]
    rival = [{"source": "active", "verdict": "same_item", "total": 120.0, "title": "Juicy Couture tote"}]
    p = pricing(raw_active_floor=120.0, judged_comps=rival)
    pick = check.choose_price(p, exact=True, ident=JUICY, history=hist)
    assert pick["levels"][3]["value"] == 64.99 and pick["used"] == [3, 4] and pick["price"] == 57.99
    assert pick["competition_note"] is None and pick["competitors"] == 1
    assert check.choose_price(p, exact=True, ident=JUICY, history=hist,
                              exclude_item_id="j1")["levels"][3]["value"] == 69.99


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
    assert res["pick"]["used"] == [1]                 # 2 same-item sales in the fake comps
    text = check.format_reply(res)
    assert len(text.splitlines()) <= 8 and "quick · exact" in text and "Evidence: 2 sold same $42" in text
    details = check.format_details(res)
    assert "Evidence ladder" in details and "break-even" in details
    assert "Quick check: no recent sold data" in details


def test_full_any_check_can_say_buy():
    seen = {}

    def fake_pricing(ident, **kw):
        seen.update(kw)
        return pricing(raw_active_floor=80.0, judged_comps=[
            *({"source": "sold", "verdict": "same_item", "total": t} for t in (52.0, 54.0, 56.0)),
            {"source": "active", "verdict": "same_item", "total": 80.0}])

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
    # a full check with no price is NO DATA too: SKIP needs real prices behind it
    assert check.evaluate(empty, {}, 14.99, exact=True, quick=False)["verdict"] == check.NO_DATA
    # an identical listing alone is only a ceiling (level 5), never a price: still NO DATA
    some = pricing(judged_comps=[{"source": "active", "verdict": "same_item", "total": 44.5}])
    assert check.evaluate(some, {}, 14.99, exact=True, quick=True)["verdict"] == check.NO_DATA
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


MK_IDENT = {"brand": "Michael Kors", "product_name": "pave necklace", "model": "MKJ3969710",
            "item_type": "necklace"}
MK_SALES = [{"item_id": f"m{i}", "brand": "michael kors", "bucket": "other", "msrp": None, "sold": s,
             "ratio": None} for i, s in enumerate((40.0, 46.0))]


def test_headline_is_verdict_and_evidence_and_the_estimate_is_marked(monkeypatch):
    monkeypatch.setattr(check, "load_our_sales", lambda: MK_SALES)
    req = {**check.parse_request("/check full 14.99"), "identification": MK_IDENT}
    res = check.run_check(req, pricing_fn=lambda ident, **kw: mkj_pricing(), have_fn=lambda *a: [])
    lines = check.format_reply(res, check_id=12).splitlines()
    assert len(lines) == 8
    assert lines[0] == "🟡 MAYBE (estimated): no sales found, 1 uncertain listing"
    assert lines[1] == "Michael Kors pave necklace (MKJ3969710)"
    # level 3: our two Michael Kors sales ($40, $46); the $160.19 listing is only a ceiling
    assert res["pick"]["used"] == [3] and res["pick"]["price"] == 42.99
    assert lines[2] == "Ross $14.99 · sell ~$42.99 (est.) · profit ~$21.30"
    assert lines[3] == "List $53.99 BO (auto-accept $44.99, decline < $38.99) or $42.99 fixed"
    assert lines[4] == "Evidence: 0 sold same · our Michael Kors items ~$43 · ask ≤$147 · match uncertain"
    assert lines[5] == "🔗 For sale $160.19 (same item) https://ebay.com/itm/1"
    assert lines[6].startswith("🔎 Sold on eBay https://www.ebay.com/sch/") and "LH_Sold=1" in lines[6]
    assert " · 🔎 For sale on eBay https://" in lines[6]
    assert lines[7].startswith("#12 · ⏱") and "full · exact" in lines[7]
    html = check.format_reply(res, check_id=12, html=True).splitlines()
    assert html[5] == '🔗 <a href="https://ebay.com/itm/1">For sale $160.19 (same item)</a>'
    assert html[6].startswith('🔎 <a href="https://www.ebay.com/sch/i.html?_nkw=Michael') and "&amp;LH_Sold=1" in html[6]
    assert "no sales found" in res["reasons"] and not any("only 0" in r for r in res["reasons"])
    details = check.format_details(res, check_id=12)
    assert "L3 our own sales, same brand + type: $43.00" in details and "L1 eBay sold, same item: none" in details
    assert "Fees + ads" in details and "label $4.50 (jewelry)" in details
    assert "https://ebay.com/itm/1" in details and "LH_Sold=1" in details


def test_model_number_not_repeated_when_typed():
    res = check.run_check(check.parse_request("/check full Michael Kors pave necklace MKJ3969710 14.99"),
                          pricing_fn=lambda ident, **kw: mkj_pricing(), have_fn=lambda *a: [])
    assert check.format_reply(res).splitlines()[1] == "Michael Kors pave necklace MKJ3969710"


def test_item_links_lose_their_tracking():
    long = ("https://www.ebay.com/itm/276922449344?_skw=Michael+Kors&hash=item4079dd3dc0:g:C6MAAOSw"
            "&amdata=enc%3AAQALAAAB")
    assert check.short_url(long) == "https://www.ebay.com/itm/276922449344"
    assert check.short_url("https://www.ebay.com/itm/some-title/123456789012") == \
        "https://www.ebay.com/itm/123456789012"
    assert check.short_url(None) == ""


def test_sale_wording():
    assert check.verdict(profit=20, roi=1, sales=1, profit_at_cheapest=None)[1] == ["only 1 sale (need 3)"]
    assert check.verdict(profit=20, roi=1, sales=2, profit_at_cheapest=None)[1] == ["only 2 sales (need 3)"]


# --- reading the Ross tag (check #2's real photo is the fixture) ---------------------------------

FIXTURE = config.BASE_DIR / "test_images" / "check_ross_tag_rotated_juicy.jpg"


def test_rotated_reduced_sticker_barcode_is_read_without_ai():
    """Check #2: the main tag is turned 90 degrees and the REDUCED sticker is at
    an angle; the plain decoder read nothing. The sticker's barcode reads once
    rotated: SKU 400303734907, $9.99 (not the 'Original $44.99')."""
    pytest.importorskip("pyzbar")
    import ross_tag
    assert ross_tag.decode_barcode(str(FIXTURE)) is None           # what failed before
    assert check.read_ross_barcode(str(FIXTURE)) == {"code": "400303734907", "reduced_price": 9.99}


def test_vision_reads_the_price_only_when_no_barcode_reads(tmp_path):
    a, b = str(tmp_path / "item.jpg"), str(tmp_path / "tag.jpg")
    seen = []

    def vision(p):
        seen.append(p)
        return {"reduced_price": 9.99, "original_price": 44.99} if p == b else {}
    roles = check.photo_roles([a, b], decode=lambda p: {}, vision=vision)
    assert (roles["price"], roles["price_source"], roles["tag"], roles["identify"]) == (9.99, "vision", b, [a])
    seen.clear()
    roles = check.photo_roles([a, b], decode=lambda p: {"code": "400303734907", "reduced_price": 9.99}
                              if p == b else {}, vision=vision)
    assert roles["price_source"] == "barcode" and seen == []          # no AI when the barcode reads


# --- full check: UPC and similar-items searches, the similar line -------------------------------

def test_full_check_searches_the_upc_and_similar_items():
    ident = {"brand": "Juicy Couture", "item_type": "Weekender Tote Bag", "upc": "885919782199",
             "model": "11JCDA0JC-JTL", "product_name": "Juicy To Go Weekender Tote"}
    assert check.similar_queries(ident) == [("upc", "885919782199"),
                                            ("similar", "Juicy Couture Weekender Tote Bag")]
    seen = []
    req = {**check.parse_request("/check full 9.99"), "identification": ident}
    check.run_check(req, pricing_fn=lambda i, **kw: seen.append(kw) or pricing(), have_fn=lambda *a: [])
    check.run_check({**req, "full": False}, pricing_fn=lambda i, **kw: seen.append(kw) or pricing(),
                    have_fn=lambda *a: [])
    assert seen[0]["extra_sold_queries"] == check.similar_queries(ident)
    assert seen[1]["extra_sold_queries"] is None                       # quick: cache only, no new searches


def test_similar_sales_are_level2_in_exact_and_level1_in_any_mode():
    judged = [{"source": "sold", "verdict": "variant", "total": t, "title": "Juicy To Go Weekender Tote pink"}
              for t in (30.49, 35.0, 42.0)] + \
             [{"source": "active", "verdict": "same_item", "total": 104.05}]
    res = check.run_check(check.parse_request("/check full juicy to go weekender tote 9.99"),
                          pricing_fn=lambda i, **kw: pricing(raw_active_floor=104.05, judged_comps=judged),
                          have_fn=lambda *a: [])
    evidence = check.format_reply(res).splitlines()[4]
    assert evidence.startswith("Evidence: 0 sold same · 3 sold same model $25 · ask ≤$92")
    res_any = check.run_check(check.parse_request("/check any full juicy to go weekender tote 9.99"),
                              pricing_fn=lambda i, **kw: pricing(raw_active_floor=104.05, judged_comps=judged),
                              have_fn=lambda *a: [])
    assert "Evidence: 3 sold same $25" in check.format_reply(res_any)


def test_level3_reports_median_days_to_sell_and_the_example_wording():
    hist = BAGS + [{"item_id": f"j{i}", "brand": "juicy couture", "bucket": "bag", "msrp": None, "sold": s,
                    "ratio": None, "days": d} for i, (s, d) in enumerate(((55.0, 9.0), (60.0, 15.0), (58.0, 12.0)))]
    p = pricing(raw_active_floor=104.05, judged_comps=[{"source": "sold", "verdict": "variant", "total": 30.49},
                                                       {"source": "active", "verdict": "same_item",
                                                        "total": 104.05}])
    pick = check.choose_price(p, exact=True, ident=JUICY, history=hist)
    assert pick["levels"][3]["value"] == 58.0 and pick["levels"][3]["days"] == 12.0
    line = check.ladder_line(pick, {})
    assert line == ("0 sold same · 1 sold same model $21 · our Juicy Couture bags ~$58 (12d) · "
                    "MSRP×47% $51 (other brands) · ask ≤$92")


def test_days_to_sell_from_listing_to_first_sale():
    assert check._days({"published_at": "2026-08-17T02:31:53+00:00", "first_sold_at": "2026-10-03T05:07:39.000Z"}) \
        == 47.1
    assert check._days({"published_at": "2026-10-03T00:00:00+00:00", "sold_at": "2026-10-01T00:00:00Z"}) is None
    assert check._days({}) is None


# --- Person B's approved fixes from the check #2 trace -----------------------------------------

def test_image_search_is_skipped_when_a_upc_or_style_code_is_known():
    seen = []
    for ident in ({"brand": "Juicy Couture", "upc": "885919782199"}, {"brand": "Juicy", "model": "11JCDA0JC-JTL"},
                  {"brand": "Juicy", "product_name": "weekender tote"}):
        req = {**check.parse_request("/check full 9.99"), "identification": ident}
        check.run_check(req, photo_path="x.jpg", pricing_fn=lambda i, **kw: seen.append(kw) or pricing(),
                        have_fn=lambda *a: [])
    assert [kw["image_search"] for kw in seen] == [False, False, True]


def test_sold_link_uses_the_product_name_not_the_long_query():
    res = {"identification": {"brand": "Juicy Couture", "product_name": "Juicy To Go Weekender Tote",
                              "search_query": "Juicy Couture Juicy To Go Weekender Tote Bag Liquorice Black NWT"},
           "request": {}}
    sold = check.search_links(res)["sold"]
    assert "_nkw=Juicy+Couture+Juicy+To+Go+Weekender+Tote&" in sold and "Liquorice" not in sold


def test_judge_verdict_counts_are_logged(tmp_path):
    judged = [{"source": "sold", "verdict": v, "total": 30.0} for v in ("same_item", "variant", "wrong", "wrong")]
    res = check.run_check(check.parse_request("/check coach tabby 14.99"),
                          pricing_fn=lambda i, **kw: pricing(judged_comps=judged), have_fn=lambda *a: [])
    assert res["verdicts"] == {"same_item": 1, "variant": 1, "wrong": 2}
    path = tmp_path / "t.jsonl"
    check.log_timings(res, user_id=1, path=path)
    assert '"judge_verdicts": {"same_item": 1, "variant": 1, "wrong": 2}' in path.read_text(encoding="utf-8")


# --- comps with dates, demand, dropship flag (Person B's part C) ---------------------------------

DROPSHIP_JUICY = {"ebay_id": "117471801433", "source": "active", "verdict": "same_item", "total": 104.05,
                  "title": "Juicy Couture Men's Juicy To Go Weekender Bag, Stylish & Versatile Tote for E...",
                  "seller": "buytheworld999", "seller_feedback": 16206,
                  "url": "https://www.ebay.com/itm/117471801433?hash=x"}
DROPSHIP_DETAILS = {"117471801433": {"created": "2026-06-08T10:00:00.000Z", "sold_quantity": 0,
                                     "seller_feedback": 16208, "category_path": "Baby|Diapering|Diaper Bags",
                                     "title": DROPSHIP_JUICY["title"]}}
NOW = check.datetime(2026, 11, 2, tzinfo=check.timezone.utc)


def test_dropship_signals_on_the_two_real_listings():
    juicy = {**DROPSHIP_JUICY, "category_path": "Baby|Diapering|Diaper Bags"}
    assert check.dropship_signals(juicy, "bags") == ["copied title", "seller 16,206 feedback",
                                                     "category Diapering > Diaper Bags"]
    assert check.is_dropship(juicy, "bags")
    hey_dude = {"title": "Hey Dude Wendy Sequin Slip On Shoes Women's Size 8 Silver", "seller_feedback": 4267,
                "category_path": "Clothing, Shoes & Accessories|Women|Women's Shoes|Flats"}
    assert check.dropship_signals(hey_dude, "shoes") == [] and not check.is_dropship(hey_dude, "shoes")
    # one signal alone is not a flag: big sellers are not all dropshippers
    assert not check.is_dropship({"title": "Coach Tabby 26", "seller_feedback": 50000}, "bags")


def test_dropship_sales_are_left_out_of_the_price():
    fake = {"source": "sold", "verdict": "same_item", "total": 30.0, "seller_feedback": 20000,
            "title": "Juicy Couture Weekender Bag for Women, Large Capacity Travel Tote"}
    real = [{"source": "sold", "verdict": "same_item", "total": t} for t in (60.0, 70.0)]
    pick = check.choose_price(pricing(raw_active_floor=None, judged_comps=real + [fake]), exact=True, ident=JUICY)
    assert pick["sales"] == 2 and pick["levels"][1]["value"] == 55.12 and pick["dropship_excluded"] == 1


def test_demand_lookup_reads_the_cheapest_identical_listings_only():
    judged = [{"ebay_id": str(i), "source": "active", "verdict": "same_item", "total": t}
              for i, t in ((1, 90.0), (2, 70.0), (3, 80.0), (4, 60.0))] +              [{"ebay_id": "9", "source": "active", "verdict": "variant", "total": 10.0},
              {"ebay_id": "8", "source": "sold", "verdict": "same_item", "total": 5.0}]
    asked = []

    def details(i):
        asked.append(i)
        if i == "3":
            raise RuntimeError("404")
        return {"created": "2026-10-01T00:00:00Z", "sold_quantity": 1}
    got = check.demand_lookup({"judged_comps": judged}, details_fn=details)
    assert sorted(asked) == ["2", "3", "4"] and sorted(got) == ["2", "4"]


def test_comp_list_sold_newest_first_then_listings_cheapest_with_flags():
    judged = [{"ebay_id": "a", "source": "sold", "verdict": "variant", "total": 30.49, "date": "2026-09-01T00:00:00Z"},
              {"ebay_id": "b", "source": "sold", "verdict": "same_item", "total": 61.0, "date": "2026-10-12T00:00:00Z"},
              {"ebay_id": "c", "source": "sold", "verdict": "wrong", "total": 5.0},
              DROPSHIP_JUICY,
              {"ebay_id": "d", "source": "image", "verdict": "same_item", "total": 99.0, "title": "Juicy tote"}]
    rows = check.comp_list({"judged_comps": judged}, "bags", True, DROPSHIP_DETAILS)
    assert [r["ebay_id"] for r in rows] == ["b", "a", "d", "117471801433"]
    assert rows[3]["dropship"] and not rows[2]["dropship"] and rows[3]["units_sold"] == 0
    assert check.demand_summary(rows, now=NOW) == "1 for sale 146d, 0 sold"
    line = check.comp_line(rows[3], now=NOW)
    assert line.startswith("for sale $104.05 · listed 146d, 0 sold · same item · ⚠️ dropship? copied title, "
                           "seller 16,208 feedback, category Diapering > Diaper Bags (ceiling only)")
    assert line.endswith("https://www.ebay.com/itm/117471801433")
    assert check.comp_line(rows[0]).startswith("sold $61.00 · 2026-10-12 · same item")


def test_full_check_shows_demand_and_every_comp_quick_looks_nothing_up():
    judged = [{"ebay_id": "a", "source": "sold", "verdict": "variant", "total": 30.49,
               "date": "2026-09-01T00:00:00Z", "url": "https://www.ebay.com/itm/111111111111",
               "title": "Juicy Couture Pink Juicy To Go Weekender Tote Bag"},
              DROPSHIP_JUICY]
    asked = []
    run = lambda text: check.run_check(  # noqa: E731
        check.parse_request(text), pricing_fn=lambda i, **kw: pricing(raw_active_floor=104.05, judged_comps=judged),
        have_fn=lambda *a: [], details_fn=lambda i: asked.append(i) or DROPSHIP_DETAILS[i])
    quick = run("/check juicy to go weekender tote bag 9.99")
    assert asked == [] and quick["demand"] is None
    res = run("/check full juicy to go weekender tote bag 9.99")
    assert asked == ["117471801433"] and res["demand"].startswith("1 for sale ")
    assert res["demand"] in check.format_reply(res).splitlines()[4]
    details = check.format_details(res)
    assert "Comps used" in details and "sold $30.49 · 2026-09-01 · same model" in details
    assert "⚠️ dropship?" in details and "https://www.ebay.com/itm/111111111111" in details
    assert res["comps"][1]["dropship"] is True and res["comps"][0]["date"] == "2026-09-01T00:00:00Z"


# --- competition, Best Offer, reply links (Person B's additions after part C) ----------------------

LUV = {"brand": "Luv Betsey by Betsey Johnson", "item_type": "Tote Bag", "model": "LB104065",
       "product_name": "LBAVERY Extra Large Canvas Tote", "retail_price": 98.0, "color": "Light Blue"}
CAT_TOTE = {"source": "sold", "verdict": "variant", "total": 39.66, "date": "2026-10-20T00:00:00Z",
            "title": "Luv Betsey by Betsey Johnson Tote Bag, Large Cat Face Fluffy Faux Fur",
            "url": "https://www.ebay.com/itm/377563627161?nordt=true"}


def test_luv_betsey_reduced_sticker_is_read_without_ai():
    got = check.read_ross_barcode(str(FIXTURE.with_name("check_ross_tag_luv_betsey.jpg")))
    assert got == {"code": "400308434185", "reduced_price": 8.99}


def test_no_competition_with_demand_prices_at_the_upper_end():
    """Check #3: no identical listing, one similar sale, MSRP $98 x our bag ratio."""
    p = pricing(raw_active_floor=None, judged_comps=[CAT_TOTE])
    pick = check.choose_price(p, exact=True, ident=LUV, history=BAGS)
    assert pick["levels"][2]["value"] == 29.78 and pick["levels"][4]["value"] == 45.99
    assert pick["competition_note"] == "no competition, unique" and pick["upper_end"]
    assert pick["price"] == 45.99 and pick["basis"].startswith("upper end of levels 2, 4")


def test_no_competition_and_no_demand_keeps_the_median():
    pick = check.choose_price(pricing(raw_active_floor=None, judged_comps=[]), exact=True, ident=LUV, history=BAGS)
    assert pick["used"] == [4] and pick["price"] == 45.99
    assert pick["competition_note"] == "no competition, no demand data" and not pick["upper_end"]


def test_a_real_identical_listing_keeps_the_median_and_the_ceiling():
    rival = {"source": "active", "verdict": "same_item", "total": 52.0, "title": "Luv Betsey LBAVERY tote"}
    pick = check.choose_price(pricing(raw_active_floor=52.0, judged_comps=[CAT_TOTE, rival]), exact=True,
                              ident=LUV, history=BAGS)
    assert pick["competitors"] == 1 and pick["competition_note"] is None
    assert pick["price"] == 37.99 and not pick["capped"]          # median of 29.78 and 45.99, under 52x0.98-9.88


def test_dropship_listings_are_not_competition_but_stay_the_ceiling():
    """Check #2: the only identical listings are dropship-style (copied title,
    16k seller, Diaper Bags): no real competition, the pink sale shows demand."""
    pink = {"source": "sold", "verdict": "variant", "total": 30.49, "title": "Juicy Couture Powder Blush Pink Juicy"}
    p = pricing(raw_active_floor=104.05, judged_comps=[pink, DROPSHIP_JUICY])
    pick = check.choose_price(p, exact=True, ident=JUICY, history=BAGS, details=DROPSHIP_DETAILS)
    assert pick["competitors"] == 0 and pick["competition_note"] == "no competition, unique"
    assert pick["price"] == 50.99 and pick["levels"][5]["value"] == 92.09 and not pick["capped"]
    low_cap = pricing(raw_active_floor=50.0, judged_comps=[pink, {**DROPSHIP_JUICY, "total": 50.0}])
    assert check.choose_price(low_cap, exact=True, ident=JUICY, history=BAGS, details=DROPSHIP_DETAILS)["capped"]


def test_best_offer_prices_from_the_estimate():
    assert check.best_offer(39.99) == {"fixed": 39.99, "list": 49.99, "accept": 41.99, "decline": 35.99}
    assert check.offer_line(check.best_offer(39.99)) == \
        "List $49.99 BO (auto-accept $41.99, decline < $35.99) or $39.99 fixed"
    assert check.best_offer(None) is None


def test_reply_links_best_matches_sold_first_and_always_the_searches():
    req = {**check.parse_request("/check full 8.99"), "identification": LUV}
    pink = {**CAT_TOTE, "title": "Luv Betsey LBAVERY Pink Canvas Tote", "url": "https://www.ebay.com/itm/222222222222"}
    rows = [pink, {"source": "active", "verdict": "same_item", "total": 61.0, "title": "Luv Betsey LBAVERY",
                   "url": "https://www.ebay.com/itm/333333333333?x=1"}, CAT_TOTE]
    res = check.run_check(req, have_fn=lambda *a: [],
                          pricing_fn=lambda i, **kw: pricing(raw_active_floor=61.0, judged_comps=rows))
    # the cat-face tote (another style) is left out; the identical listing comes next
    assert check.best_links(res) == [("🔗", "Sold $39.66 (pink)", "https://www.ebay.com/itm/222222222222"),
                                     ("🔗", "For sale $61.00 (same item)", "https://www.ebay.com/itm/333333333333")]
    lines = check.format_reply(res).splitlines()
    assert lines[5].startswith("🔗 Sold $39.66 (pink) https://www.ebay.com/itm/222222222222 · 🔗 For sale $61.00")
    nothing = check.run_check(req, have_fn=lambda *a: [],
                              pricing_fn=lambda i, **kw: pricing(raw_active_floor=None, judged_comps=[]))
    nothing = {**nothing, "pick": {**nothing["pick"], "levels": {}, "used": [], "price": None},
               "money": None, "verdict": check.NO_DATA, "offer": None}
    text = check.format_reply(nothing, html=True)
    assert "List " not in text and '🔎 <a href="https://www.ebay.com/sch/i.html?_nkw=Luv+Betsey' in text
    assert "For sale on eBay</a>" in text


# --- "same model" needs a style code or product name match (Person B, 2026-11-02) ----------------

def test_a_different_style_of_the_same_brand_is_never_same_model():
    assert check.model_tokens(LUV) == {"lbavery"}
    assert not check.same_model(CAT_TOTE, LUV)          # cat-face faux-fur tote vs the LBAVERY canvas tote
    assert check.same_model({"title": "Luv Betsey LBAVERY Tote Bag Pink Paisley NWT"}, LUV)
    assert check.same_model({"title": "Betsey Johnson tote lb104065 blue"}, LUV)          # style code
    pink = {"title": "Juicy Couture Powder Blush Pink Juicy To Go Weekender Tote Bag NWT"}
    assert check.model_tokens(JUICY) == {"go", "weekender"} and check.same_model(pink, JUICY)
    cut = {"title": "Juicy Couture Powder Blush Pink Juicy To Go Weeken..."}               # title cut short
    assert check.same_model(cut, JUICY)
    assert not check.same_model({"title": "Juicy Couture Velour Weekender Duffel"}, JUICY)  # no "go"
    assert check.same_model({"title": "Juicy Couture 11JCDA0JC-BLK tote"}, JUICY)          # code base, other colour
    assert not check.same_model({"title": "Juicy Couture tote"}, {"brand": "Juicy Couture"})  # nothing to match on


def test_check3_judge_variant_of_another_style_is_left_out_of_level2():
    """Check #3's second run: the judge called the cat-face tote a variant."""
    p = pricing(raw_active_floor=None, judged_comps=[CAT_TOTE])
    ev = check.evaluate(p, LUV, 8.99, exact=True, history=BAGS)
    assert 2 not in ev["pick"]["levels"] and ev["pick"]["used"] == [4]
    assert ev["pick"]["competition_note"] == "no competition, no demand data" and ev["pick"]["price"] == 45.99
    assert p["judged_comps"][0]["verdict"] == "variant"                      # the input is not changed
    res = check.run_check({**check.parse_request("/check full 8.99"), "identification": LUV},
                          pricing_fn=lambda i, **kw: p, have_fn=lambda *a: [])
    assert res["verdicts"] == {"variant": 1} and res["demoted"] == 1 and res["comp_list"] == []
    assert "1 other style(s) left out" in check.format_details(res)
    any_mode = check.evaluate(p, LUV, 8.99, exact=False, history=BAGS)       # any mode: not level 1 either
    assert any_mode["pick"]["sales"] == 0


# --- check #5 (Vans Detached Sling Bag): SKIP needs sales, label by bag size -----------------

VANS = {"brand": "Vans", "item_type": "Sling Bag", "product_name": "Detached Sling Bag", "model": "VN000Q9BFS8",
        "upc": "197804374267", "retail_price": 45.0, "size": "OS"}
VANS_ACTIVES = [{"source": "active", "verdict": "same_item", "total": 33.75, "title": "Brand New Vans Detached Sling Bag"},
                {"source": "active", "verdict": "same_item", "total": 41.14, "title": "Vans Detached Sling Bag New"}]


def test_vans_sticker_is_read_without_ai():
    assert check.read_ross_barcode(str(FIXTURE.with_name("check_ross_tag_vans_sling.jpg"))) == \
        {"code": "400309056676", "reduced_price": 12.99}


def test_an_estimate_with_failing_money_is_borderline_never_skip():
    v, why = check.verdict(profit=-0.63, roi=-0.05, sales=0, profit_at_cheapest=2.0, estimated=True)
    assert v == "BORDERLINE" and why[0] == "profit −$0.63 is under $5"
    v, _ = check.verdict(profit=3.0, roi=0.2, sales=0, profit_at_cheapest=-1.0, estimated=True)
    assert v == "BORDERLINE"
    # real sales behind the price: SKIP stays
    assert check.verdict(profit=-0.63, roi=-0.05, sales=4, profit_at_cheapest=2.0)[0] == "SKIP"
    # our own sales (level 3) are real sold prices too (Person B, 2026-11-03)
    assert check.verdict(profit=1.0, roi=0.1, sales=0, profit_at_cheapest=None, estimated=True,
                         own_sales=True)[0] == "SKIP"
    assert check.verdict(profit=3.0, roi=0.2, sales=2, profit_at_cheapest=-1.0)[0] == "SKIP"


def test_check5_vans_sling_is_borderline_estimated_with_a_small_bag_label(monkeypatch):
    monkeypatch.setattr(check, "load_our_sales", lambda: BAGS)
    p = pricing(raw_active_floor=33.75, judged_comps=VANS_ACTIVES)
    ev = check.evaluate(p, VANS, 12.99, exact=True, history=BAGS)
    assert ev["pick"]["used"] == [4] and ev["pick"]["price"] == 20.99 and ev["pick"]["levels"][4]["level"] == "type"
    assert ev["label"] == 5.50 and ev["label_size"] == "small"
    assert ev["verdict"] == "BORDERLINE" and ev["money"]["profit"] == 4.87
    res = check.run_check({**check.parse_request("/check full 12.99"), "identification": VANS},
                          pricing_fn=lambda i, **kw: p, have_fn=lambda *a: [])
    lines = check.format_reply(res).splitlines()
    assert lines[0].startswith("🟠 BORDERLINE (estimated): no sales found")
    assert "MSRP×47% $21 (other brands)" in lines[4]
    details = check.format_details(res)
    assert "label $5.50 (bags, small)" in details and "ratio from other brands" in details


def test_skip_backed_by_our_own_sales_of_the_brand_and_type():
    """Backtest: 9 of the 12 right SKIPs (all unsold 60+ days) had level 3."""
    cheap = [{"item_id": f"e{i}", "brand": "ed hardy", "bucket": "bag", "msrp": None, "sold": s, "ratio": None}
             for i, s in enumerate((24.0, 26.0, 28.0))]
    ed = {"brand": "Ed Hardy", "item_type": "Tote Bag", "product_name": "Tiger Tote"}
    ev = check.evaluate(pricing(raw_active_floor=None, judged_comps=[]), ed, 24.99, exact=True, history=cheap)
    assert ev["pick"]["used"] == [3] and ev["verdict"] == "SKIP"
    msrp_only = check.evaluate(pricing(raw_active_floor=None, judged_comps=[]), VANS, 24.99, exact=True,
                               history=BAGS)
    assert msrp_only["pick"]["used"] == [4] and msrp_only["verdict"] == "BORDERLINE"


def test_bag_size_from_dimensions_then_name():
    assert check.bag_size(VANS) == "small"
    assert check.bag_size({"item_type": "Weekender Tote Bag"}) == "large"
    assert check.bag_size({"item_type": "Mini Backpack"}) == "small"            # mini wins
    assert check.bag_size({"item_type": "Crossbody Tote"}) == "large"
    assert check.bag_size({"item_type": "Shoulder Bag"}) == "large"             # unknown size: the safe side
    # the name decides; a 21" sling (check #5's real identification) still ships small
    assert check.bag_size({**VANS, "specifications": {"Bag Width": "21 inches", "Bag Height": "8.5 inches"}}) \
        == "small"
    # a name that says neither: the measured sides
    assert check.bag_size({"item_type": "Handbag", "specifications": {"Bag Width": "10 in", "Bag Height": "7 in"}}) \
        == "small"
    assert check.bag_size({"item_type": "Shoulder Bag", "specifications": {"Bag Width": "14 in"}}) == "large"
    assert check.bag_size({"item_type": "bag", "specifications": {"Width": "25 cm"}}) == "small"
    assert check.label_for(VANS) == ("bags", 5.50) and check.label_for(JUICY) == ("bags", 11.0)


def test_money_shows_a_real_minus_sign():
    assert check._usd(-0.63) == "−$0.63" and check._usd(4.87) == "$4.87" and check._usd(None) == "-"
    assert check._pct(-0.05) == "−5%" and check._pct(0.97) == "97%"
    assert check._whole(-12.4) == "−$12" and check._whole(-0.2) == "$0" and check._whole(51.2) == "$51"


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
