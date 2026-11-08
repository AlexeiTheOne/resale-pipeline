"""Sold searches reused across items of the same model (pipeline/sold_reuse.py):
another colour, a /check before the intake, a reprice after a /check."""
import json
import time

import pytest

import config
from pipeline import check, comps, pricing_v2 as v2, sold_reuse

WEEK = 7 * 86400
PURPLE = {"brand": "Von Dutch", "model": "VDBB-1036PRP", "item_type": "Bowling Bag",
          "product_name": "Purple & White Cow Print Vegan Pony Hair Bowling Bag",
          "search_query": "Von Dutch purple cow print bowling bag", "color": "Purple"}
BROWN = {"brand": "Von Dutch", "model": "VDBB-1036BRN", "item_type": "Bowling Bag",
         "product_name": "Brown & White Cow Print Bowling Bag",
         "search_query": "Von Dutch brown cow print bowling bag", "color": "Brown"}
OTHER = {"brand": "Von Dutch", "model": "VDBB-1054", "item_type": "Bowling Bag",
         "product_name": "Velour Rhinestone Bowling Bag",
         "search_query": "Von Dutch velour rhinestone bowling bag", "color": "Black"}


@pytest.fixture
def apify(tmp_path, monkeypatch):
    """Every Apify call is logged; each query sells 2 bags with ids of its own."""
    monkeypatch.setattr(config, "PRICING_V2_CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(v2, "fetch_active", lambda q=None, gtin=None, **kw: [])
    monkeypatch.setattr(v2, "own_listings", lambda: (set(), set()))
    calls = []

    def call(url, payload, **kw):
        q = payload["keyword"]
        calls.append(q)
        n = len(calls)
        return [{"itemId": f"{n}{i:010d}", "soldPrice": "45", "shippingPrice": "0",
                 "title": f"Von Dutch Cow Print Bowling Bag {q} {i}", "endedAt": "2026-11-01T00:00:00Z"}
                for i in range(2)]
    monkeypatch.setattr(comps, "call", call)
    return calls


def gather(ident, **kw):
    kw.setdefault("model_reuse_s", WEEK)
    return v2.gather_candidates(ident, extra_sold_queries=check.similar_queries(ident),
                                sold_max_age_s=WEEK, **kw)


def _age_all(seconds):
    for p in (v2._cache_dir() / "sold").glob("*.json"):
        d = json.loads(p.read_text(encoding="utf-8"))
        d["fetched_at"] = time.time() - seconds
        p.write_text(json.dumps(d), encoding="utf-8")


def test_model_keys_base_code_and_distinctive_words():
    # a suffix glued on (1036PRP) isn't split off (owned.style_codes): the two
    # colours meet on the words key
    assert sold_reuse.model_keys(PURPLE) == ["vondutch|code|vdbb1036prp", "vondutch|words|bowling cow print"]
    assert set(sold_reuse.model_keys(BROWN)) & set(sold_reuse.model_keys(PURPLE)) == {
        "vondutch|words|bowling cow print"}
    assert sold_reuse.model_keys({"brand": "Ed Hardy", "model": "EHH108-26-WIP"}) == ["edhardy|code|ehh108"]
    assert "vondutch|code|vdbb1054" in sold_reuse.model_keys(OTHER)
    assert sold_reuse.model_keys({"product_name": "Cow Print Bowling Bag"}) == []      # no brand
    assert sold_reuse.model_keys({"brand": "Coach", "product_name": "Tote"}) == []      # nothing distinctive


def test_another_colour_reuses_the_models_searches_and_runs_only_its_own_code(apify):
    first = gather(PURPLE)
    assert first["apify"]["searches"] == len(apify) == 5 and first["apify"]["rows"] == 10
    assert first["apify"]["usd"] == pytest.approx(10 * 0.004 + 5 * 0.0001)
    apify.clear()
    g = gather(BROWN)
    # the colour-free searches are cached by their text; the colour-worded ones
    # are the purple bag's; only the exact style code goes to Apify
    assert apify == ["Von Dutch VDBB-1036BRN"]
    assert set(g["sold_reused"]) == {PURPLE["search_query"], "Von Dutch " + PURPLE["product_name"]}
    assert set(g["sold_reused_for"]) == {BROWN["search_query"], "Von Dutch " + BROWN["product_name"]}
    assert g["apify"] == {"searches": 1, "rows": 2, "usd": pytest.approx(2 * 0.004 + 0.0001)}
    assert len([c for c in g["candidates"] if c["source"] == "sold"]) == 10
    assert set(g["sold_reused"]) <= set(g["queries"])            # supply re-reads them


def test_no_reuse_past_the_window_for_another_model_or_when_off(apify):
    gather(PURPLE)
    apify.clear()
    gather(OTHER)                                   # VDBB-1054: another model
    assert OTHER["search_query"] in apify
    apify.clear()
    gather(BROWN, model_reuse_s=None)               # off (backtests)
    assert BROWN["search_query"] in apify
    apify.clear()
    _age_all(8 * 86400)
    gather({**BROWN, "model": "VDBB-1036BLK", "search_query": "Von Dutch black cow print bowling bag",
            "product_name": "Black & White Cow Print Bowling Bag"})
    assert "Von Dutch black cow print bowling bag" in apify     # 8 days old: searched again


def test_a_quick_check_counts_reused_searches_as_searched(apify):
    gather(PURPLE)
    apify.clear()
    g = v2.gather_candidates(BROWN, sold_cache_only=True, sold_max_age_s=WEEK, model_reuse_s=WEEK)
    assert apify == [] and g["apify"]["searches"] == 0
    assert g["sold_cache_misses"] == ["Von Dutch VDBB-1036BRN"]    # only the exact code
    assert [c for c in g["candidates"] if c["source"] == "sold"]


def test_repricing_reuses_a_checks_searches(apify, monkeypatch):
    """price_item (intake and repricing) reuses the same model's searches of the
    last SOLD_MODEL_REUSE_DAYS; its own cached rows of any age still come first."""
    gather(PURPLE)
    apify.clear()
    seen = {}

    def fake(ident, **kw):
        seen.update(kw)
        return {"timings": {}}
    monkeypatch.setattr(v2, "get_pricing_v2", fake)
    v2.price_item({"identification": BROWN, "item_id": "x"})
    assert seen["model_reuse_s"] == config.SOLD_MODEL_REUSE_DAYS * 86400
    g = v2.gather_candidates(BROWN, model_reuse_s=WEEK)            # intake/repricing searches
    assert apify == ["Von Dutch VDBB-1036BRN"] and g["sold_reused"]


def test_a_check_passes_the_reuse_window(monkeypatch):
    seen = {}
    check.run_check({"description": "Von Dutch cow print bowling bag $20", "full": True},
                    pricing_fn=lambda i, **kw: seen.update(kw) or {"judged_comps": [], "timings": {}},
                    have_fn=lambda *a: [], supply_fn=lambda *a: None)
    assert seen["model_reuse_s"] == config.SOLD_MODEL_REUSE_DAYS * 86400
