"""pipeline/competitors.py: the match ladder (UPC, then MPN, then keywords),
our own listings left out, getItem details for the best few, and the 24-hour
cache in check_evidence. Browse and Apify are faked; the DB is the test one."""
import uuid
from datetime import datetime, timedelta, timezone

import pytest

import config
from ebay import browse
from pipeline import competitors, pricing_v2, sold_reuse

IDENT = {"brand": "Michael Kors", "upc": "796483538993", "model": "MKS8006",
         "search_query": "Michael Kors MKS8006 Apple Watch band"}
SPECS = {"Brand": "Michael Kors", "Model": "MKS8006", "MPN": "MKS8006", "UPC": "796483538993"}


def _b(n, title, seller="other", price=79.99, ship=6.0, best_offer=False):
    return {"item_id": f"v1|{n}|0", "legacy_item_id": str(n), "title": title, "price": price,
            "shipping_cost": ship, "condition": "New", "item_web_url": f"https://www.ebay.com/itm/{n}",
            "buying_options": ["FIXED_PRICE"] + (["BEST_OFFER"] if best_offer else []),
            "seller_username": seller}


def _sold(n, title, price=77.99, date="2026-10-21T10:00:00Z"):
    return {"itemId": str(n), "title": title, "soldPrice": price, "shippingPrice": 7.0,
            "url": f"https://www.ebay.com/itm/{n}", "endedAt": date, "condition": "Brand New",
            "sellerUsername": "someone"}


class Fakes:
    def __init__(self, monkeypatch, *, by_gtin=(), by_query=None, sold=()):
        self.calls, self.details, self.sold_calls = [], [], []
        self.by_gtin, self.by_query, self.sold = list(by_gtin), by_query or {}, list(sold)
        monkeypatch.setattr(browse, "search_active", self.search)
        monkeypatch.setattr(browse, "item_detail", self.detail)
        monkeypatch.setattr(pricing_v2, "fetch_sold", self.fetch_sold)
        monkeypatch.setattr(pricing_v2, "own_listings", lambda: ({"999"}, set()))
        # The test DB lives for the session, so each test gets its own model key:
        # otherwise the 24-hour cache answers from an earlier test's rows.
        key = f"test|code|{uuid.uuid4().hex[:10]}"
        monkeypatch.setattr(sold_reuse, "model_keys", lambda ident: [key] if ident else [])

    def search(self, query=None, *, gtin=None, limit=50):
        self.calls.append(("gtin", gtin) if gtin else ("q", query))
        return list(self.by_gtin) if gtin else list(self.by_query.get(query, []))

    def detail(self, item_id):
        self.details.append(item_id)
        if item_id == "v1|3|0":
            raise RuntimeError("404")
        return {"specifics": {"Color": "Silver"}, "photo_count": 6, "returns_accepted": True,
                "best_offer": True, "category_id": "98624"}

    def fetch_sold(self, query, count=None, **kw):
        self.sold_calls.append(query)
        return list(self.sold)


def test_keys_take_the_style_code_as_mpn():
    k = competitors.keys({"brand": "DKNY", "search_query": "DKNY Astrid Satchel NWT"}, {"Model": "R34D1B53"})
    assert k == {"brand": "DKNY", "upc": None, "mpn": "R34D1B53", "keywords": "DKNY Astrid Satchel"}


def test_upc_rung_first_with_details_and_own_listings_dropped(monkeypatch):
    f = Fakes(monkeypatch, by_gtin=[_b(1, "Michael Kors MKS8006 Band", best_offer=True),
                                    _b(2, "MK Silver Band for Apple Watch"),
                                    _b(3, "Michael Kors Pave Band"),
                                    _b(999, "Our own listing"),
                                    _b(4, "Mine too", seller="premium_designer_outlet")])
    monkeypatch.setattr(config, "COMPETITOR_DETAIL_MAX", 3)
    out = competitors.search(IDENT, specifics=SPECS, item_id="00c8d805", sold=False)
    assert f.calls == [("gtin", "796483538993")]
    assert out["found_by"]["active"] == "upc" and out["cached"]["active"] is False
    assert [r["ebay_id"] for r in out["active"]] == ["1", "2", "3"]
    first = out["active"][0]
    assert first["specifics"] == {"Color": "Silver"} and first["photo_count"] == 6
    assert first["returns_accepted"] is True and first["best_offer"] is True
    assert first["total"] == 85.99 and first["store"] == "other" and first["item_id"] == "00c8d805"
    assert "photo_count" not in out["active"][2], "a failed getItem leaves the row without details"
    assert f.details == ["v1|1|0", "v1|2|0", "v1|3|0"]
    assert not any(k.startswith("_") for r in out["active"] for k in r)


def test_mpn_rung_keeps_only_titles_with_the_code(monkeypatch):
    f = Fakes(monkeypatch, by_query={"Michael Kors MKS8006": [_b(1, "Michael Kors MKS-8006 band"),
                                                              _b(2, "Michael Kors MKS8010 band")]})
    out = competitors.search(IDENT, specifics=SPECS, sold=False)
    assert f.calls[:2] == [("gtin", "796483538993"), ("q", "Michael Kors MKS8006")]
    assert out["found_by"]["active"] == "mpn" and [r["ebay_id"] for r in out["active"]] == ["1"]


def test_the_ladder_climbs_until_enough_and_tags_each_row(monkeypatch):
    """One UPC match is not enough: the MPN and keyword rungs add the
    identical listings that left the UPC out. Duplicates count once."""
    f = Fakes(monkeypatch, by_gtin=[_b(1, "Michael Kors MKS8006 band")],
              by_query={"Michael Kors MKS8006": [_b(1, "Michael Kors MKS8006 band"),
                                                 _b(2, "Michael Kors MKS8006 silver")],
                        "Michael Kors MKS8006 Apple Watch band": [_b(3, "MK pave band"), _b(4, "MK band"),
                                                                  _b(5, "MK band 2"), _b(6, "MK band 3")]})
    out = competitors.search(IDENT, specifics=SPECS, sold=False)
    assert [(r["ebay_id"], r["found_by"]) for r in out["active"]] == [
        ("1", "upc"), ("2", "mpn"), ("3", "keywords"), ("4", "keywords"), ("5", "keywords"), ("6", "keywords")]
    assert len(f.calls) == 3


def test_the_ladder_stops_once_there_are_enough(monkeypatch):
    f = Fakes(monkeypatch, by_gtin=[_b(n, f"Michael Kors MKS8006 band {n}") for n in range(1, 7)])
    competitors.search(IDENT, specifics=SPECS, sold=False)
    assert f.calls == [("gtin", "796483538993")]


def test_keywords_rung_last(monkeypatch):
    ident = {"brand": "Patricia Nash", "search_query": "Patricia Nash Ava Rose Tooled Black Leather Tote"}
    f = Fakes(monkeypatch, by_query={"Patricia Nash Ava Rose Tooled Black Leather Tote":
                                     [_b(5, "Patricia Nash Ava Tote")]})
    out = competitors.search(ident, specifics={"Model": "Ava Tote"}, sold=False)
    assert out["found_by"]["active"] == "keywords" and len(out["active"]) == 1
    assert f.calls == [("q", "Patricia Nash Ava Rose Tooled Black Leather Tote")]


def test_sold_from_apify_with_the_mpn_filter(monkeypatch):
    f = Fakes(monkeypatch, by_gtin=[_b(1, "Michael Kors MKS8006")],
              sold=[_sold(7, "Michael Kors MKS8006 Silver Band"), _sold(8, "Michael Kors MKS8010SET")])
    out = competitors.search(IDENT, specifics=SPECS)
    assert f.sold_calls == ["Michael Kors MKS8006"]
    assert out["found_by"]["sold"] == "mpn" and [r["ebay_id"] for r in out["sold"]] == ["7"]
    assert out["sold"][0]["price"] == 77.99 and out["sold"][0]["event_date"] == "2026-10-21T10:00:00Z"


def test_a_second_search_within_a_day_uses_the_table(monkeypatch):
    f = Fakes(monkeypatch, by_gtin=[_b(1, "Michael Kors MKS8006")], sold=[_sold(7, "Michael Kors MKS8006")])
    first = competitors.search(IDENT, specifics=SPECS)
    calls = (len(f.calls), len(f.sold_calls), len(f.details))
    again = competitors.search(IDENT, specifics=SPECS)
    assert (len(f.calls), len(f.sold_calls), len(f.details)) == calls, "no new eBay or Apify calls"
    assert again["cached"] == {"active": True, "sold": True}
    assert [r["ebay_id"] for r in again["active"]] == [r["ebay_id"] for r in first["active"]]
    assert again["active"][0]["specifics"] == {"Color": "Silver"}
    later = competitors.search(IDENT, specifics=SPECS,
                               now=datetime.now(timezone.utc) + timedelta(hours=25))
    assert later["cached"]["active"] is False and len(f.calls) > calls[0]


def test_sold_false_spends_nothing_on_apify(monkeypatch):
    f = Fakes(monkeypatch, by_gtin=[_b(1, "Michael Kors MKS8006")])
    competitors.search(IDENT, specifics=SPECS, sold=False)
    assert f.sold_calls == []


def test_nothing_to_search_by(monkeypatch):
    Fakes(monkeypatch)
    monkeypatch.setattr(sold_reuse, "model_keys", lambda ident: [])
    out = competitors.search({}, specifics={})
    assert out["model_key"] is None and out["active"] == [] and out["sold"] == []
