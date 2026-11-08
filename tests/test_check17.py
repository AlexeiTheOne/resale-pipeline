"""Check #17 (Nike Air Max Muse FV1920-004, women's 9, Ross $19.99; Person B,
2026-11-07): a failed Apify search is not "no sales", the same shoe is the
same style code AND size, "no same-item sales, N model sales", and with box
vs no box counted apart."""
from datetime import datetime

import httpx
import pytest

import config
from pipeline import check, pricing_v2


@pytest.fixture(autouse=True)
def rates(monkeypatch):
    for name, value in (("EBAY_FVF_PCT", 0.155), ("EBAY_FIXED_FEE", 0.40), ("EBAY_DEFAULT_AD_RATE_PCT", 4.0),
                        ("EBAY_SHIP_CHARGED", 9.88), ("EBAY_SHIP_COST", 7.20), ("SALES_TAX_RATE", 0.07),
                        ("CHECK_BUY_MIN_PROFIT", 15.0), ("CHECK_BUY_MIN_ROI", 0.75), ("CHECK_MIN_SALES", 3),
                        ("CHECK_MIN_PROFIT", 5.0), ("CHECK_MIN_PROFIT_AT_CHEAPEST", 5.0),
                        ("CHECK_LABEL_COSTS", "bags=11.00,shoes=12.00,clothing=7.20,other=7.20"),
                        ("CHECK_SUPPLY_DAYS", 30), ("CHECK_SUPPLY_HIGH_MONTHS", 3.0),
                        ("CHECK_SUPPLY_MIN_ACTIVE", 3), ("CHECK_ASK_DISCOUNT", 0.98)):
        monkeypatch.setattr(config, name, value)
    monkeypatch.setattr(check, "load_our_sales", lambda: [])


MUSE = {"brand": "Nike", "item_type": "sneakers", "model": "FV1920-004", "style_code": "FV1920-004",
        "product_name": "Air Max Muse", "color": "Black / Metallic Oil Green / University Red",
        "size": "Women's US 9 / UK 6.5 / EUR 40.5 / 26 cm / BR 39 / CN 260"}

_ids = iter(range(500000, 999999))


def comp(source, verdict, title, total, condition):
    return {"source": source, "verdict": verdict, "title": title, "total": total, "condition": condition,
            "ebay_id": str(next(_ids)), "url": f"https://www.ebay.com/itm/{next(_ids)}", "date": "2026-11-06"}


SAME_9 = comp("sold", "variant", "Nike Air Max Muse Metallic Oil Green FV1920-004 Women's Size 9", 79.0,
              "New (Other)")
SOLD = [comp("sold", "same_item", "Nike Air Max Muse Oil Green FV1920-004 Size 8", 82.0, "Brand New"),
        comp("sold", "variant", "Nike Air Max Muse Pearl Pink Women’s Size 8.5 Lifestyle Sneakers", 104.95,
             "Brand New"),
        comp("sold", "variant", "Nike Air Max Muse Women's Black/Silver Sneakers FV1920-001 Size 8 New With Box",
             77.17, "Brand New"),
        comp("sold", "variant", "Nike Women's Air Max Muse Sneakers Black/Metallic Silver Women 7.5 Medium New",
             65.0, "New (Other)")]
ACTIVE = [comp("active", "same_item", "Nike Air Max Muse FV1920-004 Womens 9 Oil Green", 70.0, "New without box"),
          comp("active", "same_item", "Nike Air Max Muse Metallic Oil Green Size 7", 55.0, "New with box"),
          comp("active", "same_item", "Nike Air Max Muse Oil Green FV1920-004", 63.0, "New with box")]


def pricing(judged, **kw):
    return {"judged_comps": judged, "raw_active_floor": 55.0, "same_item_active_count": 3,
            "claude_confidence": "medium", "rungs_tried": ["a", "b", "c"], "sold_cache_misses": [],
            "timings": {"comps_s": 1.0, "judge_s": 2.0}, **kw}


# --- sizes -------------------------------------------------------------------------------------

@pytest.mark.parametrize("title,sizes", [
    ("Nike Air Max Muse Pearl Pink Women’s Size 8.5", {("us_w", 8.5)}),
    ("Nike WMNS Air Max Muse Size 43 White", {("eu", 43.0)}),
    ("Nike Women's Air Max Muse Black Women 7.5 Medium New", {("us_w", 7.5)}),
    ("Nike Air Max 90 Mens US10", {("us_m", 10.0)}),
    ("Nike Air Max 270 Black", set()),                       # a model number is no size
    ("Women's US 9 / UK 6.5 / EUR 40.5 / 26 cm", {("us_w", 9.0), ("uk", 6.5), ("eu", 40.5)}),
    ("Air Max Muse W9 FV1920-004", {("us_w", 9.0)}),
])
def test_shoe_sizes_from_titles(title, sizes):
    assert check.shoe_sizes(title) == sizes


def test_our_size_and_matching():
    ours = check.size_of(MUSE)
    assert ("us_w", 9.0) in ours and ("eu", 40.5) in ours and check.size_label(MUSE) == "9"
    assert check.size_match(check.shoe_sizes("Muse Size 9"), ours) is True
    assert check.size_match(check.shoe_sizes("Muse Size 8"), ours) is False
    assert check.size_match(check.shoe_sizes("Muse EU 40.5"), ours) is True
    assert check.size_match(check.shoe_sizes("Muse Mens 7.5"), ours) is True       # men's 7.5 = women's 9
    assert check.size_match(set(), ours) is None
    assert check.size_of({"size": "9", "item_type": "sneakers", "product_name": "Women's Air Max"}) \
        == {("us_w", 9.0)}
    assert check.size_of({**MUSE, "item_type": "Tote Bag"}) == set()                # bags have no size
    tee = {"item_type": "T-Shirt", "size": "M"}
    assert check.size_of(tee) == {("letter", "M")}
    assert check.size_match(check.clothing_sizes("Polo Small Pony Tee Mens Medium"), check.size_of(tee)) is True
    assert check.clothing_sizes("Polo Ralph Lauren Small Pony Tee") == set()        # a logo, not a size


def test_same_item_is_the_style_code_and_size():
    p = check.tighten_size(pricing([SAME_9] + SOLD + ACTIVE), MUSE)
    by_title = {c["title"]: c for c in p["judged_comps"]}
    # the judge's "same item" in size 8 is our colour in another size: same model
    size8 = by_title[SOLD[0]["title"]]
    assert size8["verdict"] == "variant" and size8["other_size"]
    # the judge's "variant" with our full code and size 9 is the same item
    assert by_title[SAME_9["title"]]["verdict"] == "same_item"
    # identical listings: size 9, and the one whose title states no size; not size 7
    assert p["same_item_active_count"] == 2 and p["raw_active_floor"] == 63.0
    assert check.tighten_size(p, MUSE) is p                     # idempotent
    # our colour in other sizes for sale: not "no competition, unique" (no upper-end price)
    sized_out = [c for c in ACTIVE if "Size 7" in c["title"]] + [SAME_9] + SOLD
    pick = check.choose_price(check.tighten_size(pricing(sized_out), MUSE), True, MUSE, history=[])
    assert pick["competitors"] == 1 and pick["competition_note"] is None and not pick["upper_end"]
    # another colour (FV1920-001) in size 9 stays a variant
    other = comp("sold", "variant", "Nike Air Max Muse Black FV1920-001 Size 9", 70.0, "Brand New")
    assert check.tighten_size(pricing([other]), MUSE)["judged_comps"][0]["verdict"] == "variant"


# --- wording: model sales, not "no sales" -----------------------------------------------------------

def test_no_same_item_sales_but_model_sales_is_said_so():
    res = check.run_check({**check.parse_request("/check full 19.99"), "identification": MUSE},
                          pricing_fn=lambda i, **kw: pricing(SOLD + ACTIVE), have_fn=lambda *a: [],
                          supply_fn=lambda *a: None)
    assert res["pick"]["sales"] == 0 and res["pick"]["model_sales"] == 4
    assert "no same-item sales, 4 model sales" in res["reasons"] and "no sales found" not in res["reasons"]
    first = check.format_reply(res).splitlines()[0]
    assert "no same-item sales, 4 model sales, 2 listings" in first and "no sales found" not in first
    assert "0 sold same size 9 · 4 sold same model" in check.format_reply(res)
    assert check.sales_phrase(1, 4) == "1 same-item sale, 4 model sales"
    assert check.sales_phrase(3) == "3 sales" and check.sales_phrase(0) == "no sales found"
    assert "same colour, other size" in check.format_details(res)


# --- Apify out of credit: sold data incomplete ------------------------------------------------------

def test_failure_reasons():
    both = RuntimeError("Apify rejected both APIFY_TOKEN and APIFY_TOKEN_BACKUP (HTTP 402). Both keys are bad")
    assert pricing_v2.sold_failure_reason(both) == "out_of_credit"
    req = httpx.Request("POST", "https://api.apify.com/x")
    one = httpx.HTTPStatusError("x", request=req, response=httpx.Response(402, request=req))
    assert pricing_v2.sold_failure_reason(one) == "out_of_credit"
    assert pricing_v2.sold_failure_reason(pricing_v2.ApifyBudgetExceeded("cap")) == "budget"
    assert pricing_v2.sold_failure_reason(httpx.ConnectError("reset")) == "error"
    assert check.sold_incomplete([{"query": "q", "reason": "error"}, {"query": "r", "reason": "out_of_credit"}]) \
        == "Apify out of credit"
    assert check.sold_incomplete([{"query": "q", "reason": "error"}], 7) == "1 of 7 sold searches failed"
    assert check.sold_incomplete([]) is None


def test_gather_records_the_failed_sold_search(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "PRICING_V2_CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(pricing_v2, "own_listings", lambda: (set(), set()))

    def fetch_sold(q, **kw):
        if q == "Nike FV1920-004":
            raise RuntimeError("Apify rejected both APIFY_TOKEN and APIFY_TOKEN_BACKUP (HTTP 402).")
        return [{"itemId": "1", "title": "Nike Air Max Muse", "soldPrice": 70, "shippingPrice": 0,
                 "condition": "Brand New", "endedAt": "2026-11-06"}]
    monkeypatch.setattr(pricing_v2, "fetch_sold", fetch_sold)
    monkeypatch.setattr(pricing_v2, "fetch_active", lambda *a, **k: [])
    g = pricing_v2.gather_candidates(MUSE)
    assert g["sold_failed"] == [{"query": "Nike FV1920-004", "reason": "out_of_credit"}]
    assert any(c["source"] == "sold" for c in g["candidates"])       # the other searches still count


def test_a_402_says_sold_data_incomplete_at_the_top_never_no_sales():
    failed = [{"query": "FV1920-004", "reason": "out_of_credit"}]
    p = pricing(SOLD[1:] + ACTIVE, sold_failed=failed)
    sup = {"days": 30, "item": check._supply_part(4, 41, False, False),
           "model": check._supply_part(18, 201, True, True), "high": True, "high_part": "item",
           "item_label": "same colour"}
    res = check.run_check({**check.parse_request("/check full 19.99"), "identification": MUSE},
                          pricing_fn=lambda i, **kw: p, have_fn=lambda *a: [], supply_fn=lambda *a: sup)
    lines = check.format_reply(res).splitlines()
    assert lines[0] == "⚠️ Sold data incomplete: Apify out of credit"
    assert "no sales found" not in "\n".join(lines)
    assert "(searches incomplete)" in "\n".join(lines)
    # the high-supply step stays at colour level (Person B): the asks are real
    assert res["reasons"][0].startswith("high supply: same colour 4 sold / 41 active")
    assert res["supply"]["sold_incomplete"] and "sold counts incomplete" in check.supply_line(res)
    details = check.format_details(res)
    assert "Sold searches failed (Apify out of credit): FV1920-004" in details
    assert check.check_fields(res)["comps_summary"]["sold_incomplete"] == "Apify out of credit"
    # no sales at all and a failed search: never "no sales found"
    empty = check.run_check({**check.parse_request("/check full 19.99"), "identification": MUSE},
                            pricing_fn=lambda i, **kw: pricing([], raw_active_floor=None, sold_failed=failed),
                            have_fn=lambda *a: [], supply_fn=lambda *a: None)
    assert empty["verdict"] == check.NO_DATA and "Apify out of credit" in empty["reasons"][0]


# --- supply: colour level plus size --------------------------------------------------------------

NOW = datetime(2026, 11, 7, 12, tzinfo=check.EASTERN)


def row(source, title, total=70.0):
    return {"source": source, "ebay_id": str(next(_ids)), "title": title, "total": total, "item_price": total,
            "date": "2026-11-05" if source == "sold" else None, "condition": "Brand New"}


def test_supply_counts_colour_and_size_and_keeps_the_colour_step():
    sold = [row("sold", "Nike Air Max Muse Oil Green FV1920-004 Size 9"),
            row("sold", "Nike Air Max Muse Oil Green FV1920-004 Size 8"),
            row("sold", "Nike Air Max Muse Pearl Pink Women's Size 9")]
    active = ([row("active", "Nike Air Max Muse FV1920-004 Womens 9") for _ in range(2)]
              + [row("active", f"Nike Air Max Muse FV1920-004 Size {s}") for s in (7, 8, 10, 11, 6)]
              + [row("active", "Nike Air Max Muse FV1920-004 Oil Green")])
    sup = check.count_supply(sold, active, MUSE, now=NOW)
    assert (sup["item"]["sold"], sup["item"]["active"], sup["item"]["months"]) == (2, 8, 4.0)
    assert sup["item"]["high"] and sup["high_part"] == "item" and sup["item_label"] == "same colour"
    assert (sup["size"]["sold"], sup["size"]["active"]) == (1, 2) and not sup["size"]["high"]
    assert check.supply_line({"supply": sup}) == (
        "⚠️ Supply: same model 3 sold / 8 active in 30d (~3 months of supply) · same colour 2 sold / 8 active "
        "in 30d (~4 months of supply, high) · size 9: 1 sold / 2 active")


# --- with box or not -------------------------------------------------------------------------------

def test_box_from_condition():
    assert check.box_of("Brand New") == check.box_of("New with box") == "box"
    assert check.box_of("New (Other)") == check.box_of("New without box") == "no_box"
    assert check.box_of("New with defects") is None and check.box_of(None) is None


def test_box_counted_apart_and_asked():
    res = check.run_check({**check.parse_request("/check full 19.99"), "identification": MUSE},
                          pricing_fn=lambda i, **kw: pricing([SAME_9] + SOLD + ACTIVE), have_fn=lambda *a: [],
                          supply_fn=lambda *a: None)
    split = res["box_split"]
    assert len(split["sold"]["box"]) == 3 and len(split["sold"]["no_box"]) == 2
    line = check.box_line(res)
    assert line == ("📦 Sold: 3 with box ~$72 · 2 no box (New other) ~$62 · identical for sale 1 with box / "
                    "1 no box · has yours the box? Tap below")
    assert line in check.format_reply(res).splitlines()
    # no box: only the no-box sales (79.00 size 9, 65.00): median 72.00 - 9.88 shipping -> $61.99
    nb = check.with_box(res, False)
    assert nb["pick"]["box_left_out"] == 3 and nb["pick"]["levels"][2]["n"] == 2
    assert nb["pick"]["price"] == 61.99 and nb["cheapest_total"] == 70.0
    assert check.box_line(nb).endswith("yours: no box")
    assert "3 sale(s) with box left out (yours has no box)" in check.format_details(nb)
    # with box: 77.17, 82.00, 104.95 -> 82.00 - 9.88 -> $71.99; the boxed identical listing is the ceiling
    wb = check.with_box(res, True)
    assert wb["pick"]["price"] == 71.99 and wb["cheapest_total"] == 63.0 and wb["request"]["box"] is True
    assert check.check_fields(wb)["comps_summary"]["box"] is True
    # none in our condition: all sales, and said so
    only_boxed = check.run_check({**check.parse_request("/check full 19.99"), "identification": MUSE,
                                  "box": False},
                                 pricing_fn=lambda i, **kw: pricing(SOLD[:3] + ACTIVE), have_fn=lambda *a: [],
                                 supply_fn=lambda *a: None)
    assert "no sales without box: priced from the others" in only_boxed["reasons"]
    # bags: no box line
    bag = check.run_check({**check.parse_request("/check full 19.99"),
                           "identification": {**MUSE, "item_type": "Tote Bag", "product_name": "Muse Tote"}},
                          pricing_fn=lambda i, **kw: pricing(SOLD + ACTIVE), have_fn=lambda *a: [],
                          supply_fn=lambda *a: None)
    assert bag["box_split"] is None and check.box_line(bag) is None
