"""pipeline/pricing_v2.py: normalisation, own-listing filter, dedupe and the
code price from judged comps.

No network: Apify, Browse and Claude are all stubbed; the cache points at a
temp dir and the own-listings lookup is replaced, so data/ross.db is not read.

Run with `python tests/test_pricing_v2.py` or `pytest tests/`.
"""
import json
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

IDENT = {"brand": "Kate Spade", "product_name": "Scallop Band", "model": "KSS0089",
         "item_type": "Apple Watch Band", "size": "38mm",
         "search_query": "Kate Spade Scallop Band KSS0089 NWT"}
SOLD_ROWS = [_sold_row("100000001", "50", "10"), _sold_row("100000002", "52", "8"),
             _sold_row("100000003", "48", None, "free"),
             _sold_row("100000004", "45", "10", seller="premium_designer_outlet")]


def _run(claude_price, confidence="high", same=("S1", "S2", "S3", "A1"),
         evidence=("S1", "S2", "A1"), sold_rows=SOLD_ROWS, sold_calls=None,
         other="variant", history=()):
    """get_pricing_v2 with every source stubbed. Totals of the three foreign
    sold rows are 60, 60, 48 (median 60), so the formula, when it runs, says
    60 * 0.95 = 57 total, minus our shipping."""
    from pipeline import msrp
    saved = (config.PRICING_V2_CACHE_DIR, v2.fetch_sold, v2.fetch_active, v2.own_listings,
             v2.calibration_examples, msrp.load_history)
    seen = {}
    msrp.load_history = lambda: list(history)   # never read data/ross.db from a test
    try:
        config.PRICING_V2_CACHE_DIR = tempfile.mkdtemp()

        def fake_sold(q, count=None):
            if sold_calls is not None:
                sold_calls.append(q)
            return sold_rows
        v2.fetch_sold = fake_sold
        v2.fetch_active = lambda q=None, gtin=None: [_browse_row("200000001", 49, 10)]
        v2.own_listings = lambda: (set(), set())
        v2.calibration_examples = lambda exclude, n=15: []

        def fake_ask(stage, messages, **kw):
            seen.update(kw, stage=stage)
            ids = ("S1", "S2", "S3", "A1")
            return {"judgments": [{"id": i, "verdict": "same_item" if i in same else other}
                                  for i in ids],
                    "claude_price": claude_price, "confidence": confidence,
                    "evidence": list(evidence), "reasoning": "one line", "disagreements": []}

        r = v2.get_pricing_v2(IDENT, item_id="x", ask=fake_ask)
    finally:
        (config.PRICING_V2_CACHE_DIR, v2.fetch_sold, v2.fetch_active, v2.own_listings,
         v2.calibration_examples, msrp.load_history) = saved
    return r, seen


FORMULA = v2.charm(57 - config.EBAY_SHIP_CHARGED)


def test_claude_price_is_primary_and_v1_shape_kept():
    r, seen = _run(45.2)
    assert seen["stage"] == v2.JUDGE_STAGE and seen["output_schema"] is v2.JUDGE_SCHEMA
    assert r["dropped"]["own"] == 1 and r["candidate_count"] == 4
    assert r["suggested_price"] == 44.99 == r["machine_price"] == r["claude_price"]
    assert r["price_basis"] == "claude" and r["confidence"] == "high"
    assert r["formula_price"] == FORMULA and "formula_disagrees" not in r["review_flags"]
    assert r["needs_review"] is False
    for key in ("sold_comps", "active_listings", "review_flags", "comp_warning",
                "stock_image_url", "judged_comps", "claude_evidence"):
        assert key in r


def test_formula_disagreement_is_flagged_with_both_numbers():
    r, _ = _run(30)
    assert r["suggested_price"] == 29.99
    assert "formula_disagrees" in r["review_flags"] and r["needs_review"]
    assert "$29.99" in r["comp_warning"] and f"${FORMULA}" in r["comp_warning"]


def test_no_formula_below_three_same_item_sold():
    r, _ = _run(30, same=("S1", "A1"))
    assert r["formula_price"] is None and r["suggested_price"] == 29.99
    assert "formula_disagrees" not in r["review_flags"]


def test_no_sold_same_or_variant_is_flagged_priced_from_asks():
    r, _ = _run(45.2, same=("A1",), other="wrong")
    assert "priced_from_asks" in r["review_flags"] and r["needs_review"]
    r, _ = _run(45.2, same=("A1",), other="variant")
    assert "priced_from_asks" not in r["review_flags"]


def test_outside_store_range_needs_four_sales_and_a_clean_msrp():
    from pipeline import msrp
    hist = msrp.sold_history([
        (f"k{i}", json.dumps({"brand": "Kate Spade", "item_type": "Band", "retail_price": 100}),
         json.dumps({"sale_price": 40 + i}), "null") for i in range(4)])   # 40-43% of MSRP
    ident_msrp = dict(IDENT, retail_price=100)
    saved = dict(IDENT)
    try:
        IDENT.update(ident_msrp)
        r, _ = _run(80.0, history=hist)           # range ~$40-42; 79.99 is far above
        assert "outside_store_range" in r["review_flags"]
        r, _ = _run(48.0, history=hist)           # within 25% of the range
        assert "outside_store_range" not in r["review_flags"]
        r, _ = _run(80.0, history=hist[:3])       # only 3 sales: brand ratio not trusted
        assert "outside_store_range" not in r["review_flags"]
    finally:
        IDENT.clear()
        IDENT.update(saved)


def test_low_confidence_needs_review():
    r, _ = _run(45.2, confidence="low", same=("S1",))
    assert r["needs_review"] and "judge_low_confidence" in r["review_flags"]


def test_evidence_stored_and_unknown_ids_dropped():
    r, _ = _run(45.2, evidence=("S2", "Z9", "A1"))
    ev = r["claude_evidence"]
    assert ev["ids"] == ["S2", "A1"] and ev["reason"] == "one line"
    assert [c["source"] for c in ev["comps"]] == ["sold", "active"]
    assert ev["comps"][0]["url"].endswith("100000002?nordt=true")
    assert {c["id"] for c in r["judged_comps"]} == {"S1", "S2", "S3", "A1"}


def test_backtest_policy_sends_one_uncached_sold_query():
    calls = []
    saved = v2.uncached_sold_queries
    try:
        v2.uncached_sold_queries = {"search_query"}
        r, _ = _run(45.2, sold_calls=calls)
    finally:
        v2.uncached_sold_queries = saved
    assert calls == [IDENT["search_query"]]
    assert len(r["skipped_queries"]) == 2


def test_calibration_is_sold_items_at_sold_price_only():
    rows = [("aaa1", '{"brand": "Oakley", "item_type": "Sunglasses"}',
             '{"sale_price": 159.98, "units_sold": 2}'),
            ("bbb2", '{"brand": "DKNY"}', '{}'),                # sold row with no sale data
            ("ccc3", '{"brand": "Coach"}', '{"sale_price": 99.99}')]
    saved = v2._db_rows
    try:
        v2._db_rows = lambda sql: (rows if "status = 'sold'" in sql else [])
        ex = v2.calibration_examples(None, n=5)
        assert [(e["brand"], e["sold_price"]) for e in ex] == [("Oakley", 79.99), ("Coach", 99.99)]
        assert [e["brand"] for e in v2.calibration_examples("ccc3", n=5)] == ["Oakley"]
        assert "$79.99" in v2._calibration_text(ex)
    finally:
        v2._db_rows = saved


def test_zero_claude_price_is_asked_once_more():
    answers = [{"judgments": [], "claude_price": 0, "confidence": "medium", "evidence": [],
                "reasoning": "r", "disagreements": []},
               {"judgments": [], "claude_price": 41.5, "confidence": "medium", "evidence": [],
                "reasoning": "r", "disagreements": []}]
    notes = []

    def fake_ask(stage, messages, **kw):
        notes.append(messages[0]["content"][-1]["text"])
        return dict(answers.pop(0))

    out = v2.judge({"brand": "x"}, [], None, [], ask=fake_ask)
    assert out["claude_price"] == 41.5 and len(notes) == 2 and "previous answer" in notes[1]


def test_apify_cap_reserves_worst_case_across_parallel_queries():
    import threading
    from pipeline import price as v1
    saved = (config.PRICING_V2_CACHE_DIR, v2.apify_cap_usd, dict(v2.apify_spend), v1._call)
    gate = threading.Barrier(3, timeout=5)
    try:
        config.PRICING_V2_CACHE_DIR = tempfile.mkdtemp()
        v2.apify_spend.update(usd=0.0, calls=0, rows=0)
        v2.apify_cap_usd = 0.13          # room for two worst-case (15-row) queries, not three
        started = []

        def fake_call(url, payload):
            started.append(payload["keyword"])
            return [{}] * 15

        v1._call = fake_call
        results = []

        def run(q):
            gate.wait()
            try:
                v2.fetch_sold(q, 15)
                results.append("ok")
            except v2.ApifyBudgetExceeded:
                results.append("capped")

        threads = [threading.Thread(target=run, args=(f"q{i}",)) for i in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert sorted(results) == ["capped", "ok", "ok"] and len(started) == 2
        assert v2.apify_spend["usd"] <= 0.13
    finally:
        (config.PRICING_V2_CACHE_DIR, v2.apify_cap_usd, spend, v1._call) = saved
        v2.apify_spend.clear()
        v2.apify_spend.update(spend)


# --- review screen -------------------------------------------------------------

def test_review_screen_shows_claude_price_reason_and_evidence():
    import telegram_bot as tb
    r, _ = _run(45.2, evidence=("S1", "A1"))
    text = tb.format_pricing(r)
    assert text.startswith("💰 Suggested price: $44.99  (Claude, high confidence)")
    assert "one line" in text
    assert "• $60.00 sold — Bag" in text
    assert "\n    https://www.ebay.com/itm/100000001\n" in text   # query string dropped
    assert "active" in text and len(text.splitlines()) <= 10
    long = dict(r, claude_evidence={"reason": "x", "comps": [
        {"source": "sold", "price": 50.0, "title": "W" * 80, "url": None}]})
    assert "W" * 49 + "…" in tb.format_pricing(long)


def test_review_screen_says_priced_from_asks_once():
    import telegram_bot as tb
    r, _ = _run(45.2, same=("A1",), other="wrong", evidence=("A1",))
    text = tb.format_pricing(r)
    assert text.count("no sales found, priced from asking prices") == 1


def test_review_screen_v1_unchanged_and_manual_override_named():
    import telegram_bot as tb
    v1_text = tb.format_pricing({"suggested_price": 39.99, "evidence": "solid", "sold_count": 6,
                                 "sold_median": 45.0, "confidence": "solid"})
    assert "Claude" not in v1_text and "sold median: $45.0" in v1_text
    r, _ = _run(45.2)
    typed = dict(r, suggested_price=59.99, price_basis="manual", confidence="manual")
    assert "(typed; Claude said $44.99, high)" in tb.format_pricing(typed)


def test_a_price_on_one_sale_waits_for_review():
    r, _ = _run(45.2, confidence="medium", same=("S1", "A1"), other="wrong")
    assert "few_sales" in r["review_flags"] and r["needs_review"]
    r, _ = _run(45.2, confidence="medium", same=("S1", "S2", "A1"), other="wrong")
    assert "few_sales" not in r["review_flags"]


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
