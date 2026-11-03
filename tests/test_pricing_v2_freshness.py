"""pricing_v2 options added for /check: cache max age, cache-only sold rows,
no escalation, a separate llm_usage stage, timings. Defaults are unchanged
(intake and repricing): any cached copy, live fetches, escalation on."""
import json
import time

import pytest

import config
from pipeline import pricing_v2 as v2


@pytest.fixture
def cache(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PRICING_V2_CACHE_DIR", str(tmp_path))
    return tmp_path


def _age(kind, key, seconds):
    path = v2._cache_path(kind, key)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["fetched_at"] = time.time() - seconds
    path.write_text(json.dumps(data), encoding="utf-8")


def test_default_uses_any_cached_copy(cache):
    calls = []
    v2._cached("sold", "k", lambda: calls.append(1) or [{"a": 1}])
    _age("sold", "k", 400 * 86400)
    assert v2._cached("sold", "k", lambda: calls.append(1) or [{"a": 2}]) == [{"a": 1}]
    assert len(calls) == 1


def test_max_age_refetches_a_stale_copy(cache):
    v2._cached("sold", "k", lambda: [{"a": 1}])
    _age("sold", "k", 8 * 86400)
    assert v2._cached("sold", "k", lambda: [{"a": 2}], max_age_s=7 * 86400) == [{"a": 2}]
    # and the fresh copy is used afterwards
    assert v2._cached("sold", "k", lambda: [{"a": 3}], max_age_s=7 * 86400) == [{"a": 2}]


def test_cache_only_never_fetches(cache):
    def boom():
        raise AssertionError("fetched")
    assert v2._cached("sold", "k", boom, cache_only=True) is None
    v2._cached("sold", "k", lambda: [{"a": 1}])
    assert v2._cached("sold", "k", boom, cache_only=True, max_age_s=60) == [{"a": 1}]
    _age("sold", "k", 120)
    assert v2._cached("sold", "k", boom, cache_only=True, max_age_s=60) is None


def _judge_answer(conf):
    return {"judgments": [], "claude_price": 20, "confidence": conf, "evidence": [],
            "reasoning": "", "disagreements": []}


def test_judge_stage_and_no_escalation(monkeypatch):
    monkeypatch.setattr(config, "PRICING_ESCALATE_ON_LOW", True)
    seen = []

    def ask(stage, messages, **kw):
        seen.append((stage, kw["model"]))
        return _judge_answer("low")

    out = v2.judge({}, [], None, [], ask=ask, stage="check_judge", escalate=False)
    assert seen == [("check_judge", config.PRICING_JUDGE_MODEL)] and out["escalated"] is False
    seen.clear()
    out = v2.judge({}, [], None, [], ask=ask)
    assert [s for s, _ in seen] == [v2.JUDGE_STAGE] * 2 and out["escalated"] is True


def test_quick_mode_uses_only_cached_sold_rows(cache, monkeypatch):
    from pipeline import comps
    monkeypatch.setattr(comps, "call", lambda *a, **k: (_ for _ in ()).throw(AssertionError("Apify")))
    monkeypatch.setattr(v2, "fetch_active", lambda q=None, gtin=None, **kw: [])
    monkeypatch.setattr(v2, "own_listings", lambda: (set(), set()))
    ident = {"brand": "Coach", "product_name": "Tabby 26", "search_query": "coach tabby 26"}
    g = v2.gather_candidates(ident, sold_cache_only=True, sold_max_age_s=7 * 86400)
    assert g["candidates"] == [] and g["sold_cache_misses"] and not g["errors"]


def test_extra_sold_queries_are_added_once(cache, monkeypatch):
    asked = []
    monkeypatch.setattr(v2, "fetch_sold", lambda q, count=None, **kw: asked.append(q) or [])
    monkeypatch.setattr(v2, "fetch_active", lambda q=None, gtin=None, **kw: [])
    monkeypatch.setattr(v2, "own_listings", lambda: (set(), set()))
    ident = {"brand": "Juicy Couture", "product_name": "Juicy To Go Weekender Tote",
             "search_query": "juicy to go weekender tote"}
    g = v2.gather_candidates(ident, extra_sold_queries=[("upc", "885919782199"),
                                                        ("similar", "Juicy Couture Weekender Tote Bag"),
                                                        ("dup", "JUICY TO GO WEEKENDER TOTE")])
    assert "885919782199" in asked and "Juicy Couture Weekender Tote Bag" in asked
    assert len(asked) == len(set(q.lower() for q in asked)) and len(g["queries"]) == len(asked)
    asked.clear()
    v2.gather_candidates(ident)                                   # intake/repricing: unchanged
    assert "885919782199" not in asked


def test_stored_item_pricing_has_no_check_diagnostics(monkeypatch):
    monkeypatch.setattr(v2, "get_pricing_v2", lambda *a, **k: {
        "suggested_price": 10, "timings": {"total_s": 1}, "sold_cache_misses": []})
    assert v2.price_item({"identification": {}, "item_id": "x"}) == {"suggested_price": 10}
