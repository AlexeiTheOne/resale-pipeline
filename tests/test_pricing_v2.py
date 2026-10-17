"""pipeline/pricing_v2.py: normalisation, own-listing filter, dedupe and the
code price from judged comps.

No network: Apify, Browse and Claude are all stubbed; the cache points at a
temp dir and the own-listings lookup is replaced, so data/ross.db is not read.

Run with `python tests/test_pricing_v2.py` or `pytest tests/`.
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import config
from pipeline import pricing_v2 as v2

SHIP = 9.99


def _sold_row(item_id, sold, ship=None, ship_type="paid", seller="someone", title="Bag",
              condition="Brand New"):
    return {"itemId": item_id, "soldPrice": sold, "shippingPrice": ship,
            "shippingType": ship_type, "totalPrice": None, "title": title,
            "condition": condition, "sellerUsername": seller,
            "url": f"https://www.ebay.com/itm/{item_id}?nordt=true",
            "thumbnailUrl": "https://i.ebayimg.com/images/g/AbC/s-l500.webp"}


def _browse_row(legacy, price, ship, seller="rival", title="Bag"):
    return {"item_id": f"v1|{legacy}|0", "legacy_item_id": legacy, "title": title,
            "price": price, "shipping_cost": ship, "condition": "New with tags",
            "image_url": "https://i.ebayimg.com/images/g/X/s-l225.jpg",
            "item_web_url": f"https://www.ebay.com/itm/{legacy}", "seller_username": seller}


# --- normalisation ---------------------------------------------------------

def test_sold_free_shipping_total_is_item_price():
    c = v2.sold_candidate(_sold_row("111111111", "40.00", None, "free"), SHIP)
    assert c["total"] == 40.0 and c["shipping"] == 0.0 and not c["shipping_imputed"]


def test_sold_paid_shipping_adds_to_total():
    c = v2.sold_candidate(_sold_row("111111111", "30.00", "8.50"), SHIP)
    assert c["total"] == 38.5 and c["item_price"] == 30.0


def test_unknown_shipping_is_imputed_at_ours_and_flagged():
    c = v2.sold_candidate(_sold_row("111111111", "30.00", None, "paid"), SHIP)
    assert c["total"] == 39.99 and c["shipping_imputed"]
    a = v2.active_candidate(_browse_row("222222222", 25.0, None), SHIP)
    assert a["total"] == 34.99 and a["shipping_imputed"]


def test_active_known_shipping_and_zero_is_not_unknown():
    a = v2.active_candidate(_browse_row("222222222", 25.0, 0.0), SHIP)
    assert a["total"] == 25.0 and not a["shipping_imputed"]
    assert a["ebay_id"] == "222222222"


def test_ebay_id_from_url_and_browse_id():
    assert v2.ebay_id(None, "https://www.ebay.com/itm/Some-Title/117340138995") == "117340138995"
    assert v2.ebay_id("v1|123456789|0") == "123456789"


def test_thumbnail_resized():
    assert v2.thumb("https://i.ebayimg.com/images/g/AbC/s-l500.webp").endswith("/s-l225.webp")
    assert v2.thumb("https://example.com/a.jpg") is None


# --- own listings ------------------------------------------------------------

def test_own_seller_dropped_everywhere():
    own = ("premium_designer_outlet",)
    cands = [v2.sold_candidate(_sold_row("1" * 9, "40", seller="Premium_Designer_Outlet"), SHIP),
             v2.active_candidate(_browse_row("2" * 9, 40, 5, seller="premium_designer_outlet"), SHIP),
             v2.active_candidate(_browse_row("3" * 9, 40, 5), SHIP)]
    kept, dropped = v2.filter_candidates(cands, own, set(), set())
    assert [c["ebay_id"] for c in kept] == ["3" * 9] and dropped["own"] == 2


def test_own_listing_id_dropped_even_with_other_seller_name():
    c = v2.active_candidate(_browse_row("117466756697", 40, 5, seller="x"), SHIP)
    kept, dropped = v2.filter_candidates([c], (), {"117466756697"}, set())
    assert not kept and dropped["own"] == 1


def test_title_match_only_when_seller_unknown():
    title = "Juicy Couture Prep School Weekender Large Leopard Travel Tote NWT"
    no_seller = v2.sold_candidate(_sold_row("1" * 9, "80", seller=None, title=title), SHIP)
    other = v2.sold_candidate(_sold_row("2" * 9, "80", seller="rival", title=title), SHIP)
    kept, _ = v2.filter_candidates([no_seller, other], (), set(), {title.lower()})
    assert [c["seller"] for c in kept] == ["rival"]


def test_used_and_absurd_prices_dropped():
    used = v2.sold_candidate(_sold_row("1" * 9, "40", condition="Pre-Owned"), SHIP)
    cheap = v2.sold_candidate(_sold_row("2" * 9, "1.00"), SHIP)
    kept, dropped = v2.filter_candidates([used, cheap], (), set(), set())
    assert not kept and dropped["used"] == 1 and dropped["price"] == 1


# --- dedupe ------------------------------------------------------------------

def test_dedupe_keeps_first_by_ebay_id():
    s = v2.sold_candidate(_sold_row("123456789", "40"), SHIP)
    a = v2.active_candidate(_browse_row("123456789", 45, 0), SHIP)
    b = v2.active_candidate(_browse_row("987654321", 45, 0), SHIP)
    out = v2.dedupe([s, a, b, dict(b)])
    assert [(c["source"], c["ebay_id"]) for c in out] == [("sold", "123456789"),
                                                          ("active", "987654321")]


# --- price from judged comps -------------------------------------------------

def _c(cid, source, total, imputed=False):
    return {"id": cid, "source": source, "total": total, "shipping_imputed": imputed}


def test_price_uses_same_item_sold_median_only():
    cands = [_c("S1", "sold", 50), _c("S2", "sold", 60), _c("S3", "sold", 70),
             _c("S4", "sold", 500), _c("A1", "active", 200)]
    verdicts = {"S1": "same_item", "S2": "same_item", "S3": "same_item", "S4": "wrong",
                "A1": "same_item"}
    r = v2.price_from_judged(cands, verdicts, undercut=0.05, our_shipping=SHIP)
    # median total 60, -5% = 57.00 total, minus 9.99 shipping = 47.01 -> 46.99
    assert r["evidence"] == "thin" and r["same_item_sold"] == 3
    assert r["sold_median"] == 60 and r["price"] == 46.99


def test_cheapest_same_item_active_caps_total():
    cands = [_c(f"S{i}", "sold", 60) for i in range(5)] + [_c("A1", "active", 50.5)]
    verdicts = {c["id"]: "same_item" for c in cands}
    r = v2.price_from_judged(cands, verdicts, undercut=0.05, our_shipping=SHIP)
    assert r["evidence"] == "solid" and r["active_floor"] == 50.5
    assert r["price"] + SHIP <= 50.5 and r["price"] == 39.99


def test_imputed_shipping_active_never_caps_and_tiny_active_ignored():
    cands = [_c(f"S{i}", "sold", 60) for i in range(3)] + [
        _c("A1", "active", 40, imputed=True), _c("A2", "active", 20)]
    verdicts = {c["id"]: "same_item" for c in cands}
    r = v2.price_from_judged(cands, verdicts, undercut=0.05, our_shipping=SHIP)
    assert r["active_floor"] is None and r["warnings"]          # 20 < 50% of 60
    assert r["price"] == 46.99                                   # 57.00 - 9.99, uncapped


def test_too_few_same_item_sold_gives_no_price():
    cands = [_c("S1", "sold", 50), _c("S2", "sold", 55), _c("S3", "sold", 60)]
    r = v2.price_from_judged(cands, {"S1": "same_item", "S2": "variant", "S3": "bundle"})
    assert r["evidence"] == "none" and r["price"] is None


# --- end to end with stubs -----------------------------------------------------

def test_get_pricing_v2_shape_with_stubs():
    tmp = tempfile.mkdtemp()
    saved = (config.PRICING_V2_CACHE_DIR, v2.fetch_sold, v2.fetch_active, v2.own_listings,
             v2.calibration_examples)
    try:
        config.PRICING_V2_CACHE_DIR = tmp
        v2.fetch_sold = lambda q, count=None: [
            _sold_row("100000001", "50", "10"), _sold_row("100000002", "52", "8"),
            _sold_row("100000003", "48", None, "free"),
            _sold_row("100000004", "45", "10", seller="premium_designer_outlet")]
        v2.fetch_active = lambda q=None, gtin=None: [_browse_row("200000001", 49, 10)]
        v2.own_listings = lambda: (set(), set())
        v2.calibration_examples = lambda exclude, n=15: []
        seen = {}

        def fake_ask(stage, messages, **kw):
            seen.update(kw, stage=stage, n_blocks=len(messages[0]["content"]))
            return {"judgments": [{"id": i, "verdict": "same_item"} for i in ("S1", "S2", "S3", "A1")],
                    "claude_price": 44.0, "confidence": "high", "reasoning": "r",
                    "disagreements": []}

        ident = {"brand": "Kate Spade", "product_name": "Scallop Band", "model": "KSS0089",
                 "search_query": "Kate Spade Scallop Band KSS0089 NWT"}
        r = v2.get_pricing_v2(ident, item_id="x", ask=fake_ask)
        assert seen["stage"] == v2.JUDGE_STAGE and seen["output_schema"] is v2.JUDGE_SCHEMA
        assert r["dropped"]["own"] == 1 and r["candidate_count"] == 4
        # totals 60, 60, 48 -> median 60 -> 57 total (active 59 is above it) - shipping
        assert r["suggested_price"] == v2.charm(57 - config.EBAY_SHIP_CHARGED)
        assert r["machine_price"] == r["suggested_price"] and r["claude_price"] == 44.0
        for key in ("confidence", "price_basis", "sold_comps", "active_listings",
                    "review_flags", "needs_review", "comp_warning", "stock_image_url"):
            assert key in r
    finally:
        (config.PRICING_V2_CACHE_DIR, v2.fetch_sold, v2.fetch_active, v2.own_listings,
         v2.calibration_examples) = saved


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
