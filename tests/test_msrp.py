"""pipeline/msrp.py: the store-history estimate (MSRP x what this store's sales
of the brand fetched as a share of MSRP), plus the round-3 query and prompt
additions in pricing_v2 that use it. No DB, no network.

Run with `python tests/test_msrp.py` or `pytest tests/`.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from pipeline import msrp
from pipeline import pricing_v2 as v2


def _row(item_id, brand, item_type, retail, sale, units=1, receipt=None):
    return (item_id, json.dumps({"brand": brand, "item_type": item_type, "retail_price": retail}),
            json.dumps({"sale_price": sale, "units_sold": units}), json.dumps(receipt))


HISTORY = msrp.sold_history([
    _row("rl1", "Lauren Ralph Lauren", "Duvet Cover Set", 300, 120),     # 0.40
    _row("rl2", "Ralph Lauren Home", "Pillow Sham", 100, 30),            # 0.30
    _row("rl3", "Ralph Lauren", "Flat Sheet", 200, 100),                 # 0.50
    _row("eh1", "Ed Hardy", "Tote Bag", 100, 50),                        # 0.50
    _row("dk1", "DKNY", "Crossbody Bag", 100, 40),                       # 0.40
    _row("kb1", "Kate Spade", "Shoulder Bag", 100, 60),                  # 0.60
    _row("ok1", "Oakley", "Sunglasses", 200, 160, units=2),              # 0.40 per unit
    _row("bad", "Oakley", "Sunglasses", 30, 25, receipt={"original_price": 30}),  # Ross tag
    _row("nom", "Coach", "Wallet", None, 50),                            # no MSRP
])


def test_brand_aliases_and_buckets():
    assert msrp.brand_key("Lauren Ralph Lauren") == msrp.brand_key("ralph lauren home") == "ralph lauren"
    assert msrp.brand_key("kate spade new york") == "kate spade"
    assert msrp.type_bucket("Apple Watch Band") == "watch band"
    assert msrp.type_bucket("Women's Wristwatch") == "watch"
    assert msrp.type_bucket("European Sham") == "bedding"
    assert msrp.type_bucket("Weekender Duffel Bag") == "bag"


def test_history_ratio_per_unit_and_suspicious_msrp_left_out():
    by_id = {h["item_id"]: h for h in HISTORY}
    assert by_id["ok1"]["ratio"] == 0.4                 # 160 / 2 units / 200
    assert by_id["bad"]["ratio"] is None                 # MSRP equals the Ross tag price
    assert by_id["nom"]["ratio"] is None and by_id["nom"]["msrp"] is None
    assert msrp.coverage(HISTORY) == {"sold": 9, "with_msrp": 8, "usable_ratio": 7}


def test_brand_level_when_three_sales():
    r = msrp.ratio_for("Ralph Lauren", "Duvet Cover", HISTORY)
    assert r["level"] == "brand" and r["n"] == 3 and r["ratio"] == 0.4


def test_leave_one_out_falls_back_to_type_then_global():
    r = msrp.ratio_for("Ralph Lauren", "Duvet Cover", HISTORY, exclude_item_id="rl1")
    # 2 Ralph Lauren sales left, 2 bedding sales: neither reaches 3 -> every sale
    assert r["level"] == "global" and r["n"] == 6
    r = msrp.ratio_for("Ed Hardy", "Tote Bag", HISTORY)       # 1 Ed Hardy sale; 3 bags
    assert r["level"] == "type" and r["key"] == "bag" and r["n"] == 3 and r["ratio"] == 0.5
    r = msrp.ratio_for("Nobody", "Hair Trimmer", HISTORY)
    assert r["level"] == "global" and r["n"] == 7


def test_store_estimate_and_flags():
    est = msrp.store_estimate({"brand": "Ralph Lauren", "item_type": "Sham", "retail_price": 150},
                              {"original_price": 39.99, "reduced_price": 12.99}, history=HISTORY)
    assert est["estimate"] == 59.99 and est["msrp_flags"] == []      # 150 x 0.40
    est = msrp.store_estimate({"brand": "Ed Hardy", "item_type": "Tote", "retail_price": 29.99},
                              {"original_price": 29.99, "reduced_price": 26.0}, history=HISTORY)
    assert set(est["msrp_flags"]) == {"equals_ross_tag_price", "below_our_cost"}
    est = msrp.store_estimate({"brand": "Ed Hardy", "item_type": "Tote"}, None, history=HISTORY)
    assert est["msrp_flags"] == ["missing"] and est["estimate"] is None


def test_ratio_table_sorted_by_sales():
    table = msrp.ratio_table(HISTORY)
    assert table[0]["brand"] == "ralph lauren" and table[0]["n"] == 3


# --- pricing_v2 round-3 additions ------------------------------------------------

def test_three_sold_queries_with_broad_band_size():
    ident = {"brand": "Kate Spade", "product_name": "Scallop Link", "item_type": "Apple Watch Band",
             "size": "38/40mm", "search_query": "Kate Spade Scallop Link Apple Watch Band 38mm"}
    assert v2.sold_queries(ident) == [
        ("search_query", "Kate Spade Scallop Link Apple Watch Band 38mm"),
        ("product", "Kate Spade Scallop Link"),
        ("broad", "Kate Spade Apple Watch Band 38/40mm")]
    tote = {"brand": "DKNY", "item_type": "Tote Bag", "size": "Large", "search_query": "DKNY tote"}
    assert v2.broad_query(tote) == "DKNY Tote Bag"


def test_context_gives_store_range_not_an_estimate_and_weak_research():
    store = {"msrp": 150.0, "msrp_flags": ["equals_ross_tag_price"], "level": "brand",
             "key": "ralph lauren", "n": 13, "ratio": 0.378, "q1": 0.33, "q3": 0.47,
             "estimate": 55.99, "range_low": 49.5, "range_high": 70.5}
    ident = {"resale_estimate": 60, "price_evidence": [{"source": "ebay sold", "price": 49.99,
                                                        "note": "one sale"}]}
    text = v2.context_text(store, ident)
    assert "do NOT anchor" in text and "MSRP $150 (doubtful: equals_ross_tag_price)" in text
    assert "13 past sale(s) (brand 'ralph lauren') fetched 33%-47% of MSRP" in text
    assert "$49.50-$70.50" in text
    assert "55.99" not in text                    # the point estimate is never shown
    assert "weak evidence" in text and "ebay sold: $49.99 one sale" in text


def test_store_estimate_gives_iqr_range():
    est = msrp.store_estimate({"brand": "Ralph Lauren", "item_type": "Sham", "retail_price": 100},
                              None, history=HISTORY)
    assert (est["range_low"], est["range_high"]) == (100 * est["q1"], 100 * est["q3"])


# --- backtest outcome scoring ---------------------------------------------------

def _score():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "backtest_pricing", Path(__file__).parent.parent / "scripts" / "backtest_pricing.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.score_outcome


def test_outcome_scoring_rules():
    score = _score()
    sold = dict(status="sold", owner=50.0, sold=50.0)
    assert score(59.99, days=30, **sold) == "hit" and score(64.99, days=30, **sold) == "miss"
    assert score(64.99, days=3, **sold) == "hit"        # sold fast: probably underpriced
    assert score(40.99, days=3, **sold) == "hit" and score(35.0, days=3, **sold) == "miss"
    assert score(None, days=30, **sold) == "miss"
    stale = dict(status="published", owner=50.0, sold=None)
    assert score(29.99, days=90, **stale) == "hit"      # under a stale owner price is fine
    assert score(59.99, days=90, **stale) == "miss"
    assert score(59.99, days=20, **stale) is None       # too new to judge


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
