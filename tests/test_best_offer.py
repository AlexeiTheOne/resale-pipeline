"""Best Offer on new listings, and on live offers via update_offer_fields.

The auto-decline floor is the break-even rounded UP to a whole dollar; there is
never an auto-accept; and no floor is set when the cost is unknown or the floor
would reach the price (it would decline every offer).

Run with `python tests/test_best_offer.py` or `pytest tests/`.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import ebay.inventory as inv
from profit import breakeven_price

COSTED = {"receipt": {"reduced_price": 24.99}, "listing": {"price": 129.99}}
NO_RECEIPT = {"listing": {"price": 59.99}}


# --- the floor ----------------------------------------------------------------

def test_floor_is_breakeven_rounded_up_to_a_dollar():
    be = breakeven_price(COSTED)
    floor = inv.auto_decline_price(COSTED, 129.99)
    assert isinstance(floor, int)
    assert be <= floor < be + 1


def test_no_floor_without_a_known_cost():
    assert inv.auto_decline_price(NO_RECEIPT, 59.99) is None


def test_no_floor_when_it_would_decline_every_offer():
    floor = inv.auto_decline_price(COSTED, 129.99)
    assert inv.auto_decline_price(COSTED, floor) is None
    assert inv.auto_decline_price(COSTED, floor - 0.01) is None
    assert inv.auto_decline_price(COSTED, floor + 0.01) == floor


def test_no_floor_when_breakeven_is_under_a_dollar():
    # 905ea463, live: a $0.49 receipt puts break-even below zero, and eBay
    # rejected the resulting $0 floor (errorId 25016).
    cheap = {"receipt": {"reduced_price": 0.49}, "listing": {"price": 39.99}}
    assert breakeven_price(cheap) < 1
    assert inv.auto_decline_price(cheap, 39.99) is None
    assert inv.best_offer_terms(cheap, 39.99) == {"bestOfferEnabled": True}


def test_terms_never_auto_accept():
    terms = inv.best_offer_terms(COSTED, 129.99)
    floor = inv.auto_decline_price(COSTED, 129.99)
    assert terms == {"bestOfferEnabled": True,
                     "autoDeclinePrice": {"value": f"{floor}.00", "currency": "USD"}}
    assert inv.best_offer_terms(NO_RECEIPT, 59.99) == {"bestOfferEnabled": True}


# --- live offers --------------------------------------------------------------

OFFER = {"offerId": "1", "sku": "s", "categoryId": "180000", "availableQuantity": 2,
         "listingDescription": "<p>d</p>", "merchantLocationKey": "loc",
         "listingPolicies": {"fulfillmentPolicyId": "f", "returnPolicyId": "r",
                             "paymentPolicyId": "p"},
         "pricingSummary": {"price": {"value": "59.99", "currency": "USD"}},
         "listing": {"listingId": "117", "listingStatus": "ACTIVE"}, "status": "PUBLISHED"}


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


def test_update_offer_fields_sets_only_best_offer(monkeypatch):
    sent = {}

    def fake_request(method, path, **kw):
        if method == "GET":
            return _Resp(dict(OFFER))
        sent.update(method=method, path=path, body=kw["json"])
        return _Resp({})

    monkeypatch.setattr(inv, "_request", fake_request)
    terms = {"bestOfferEnabled": True, "autoDeclinePrice": {"value": "31.00", "currency": "USD"}}
    inv.update_offer_fields("1", best_offer_terms=terms)
    body = sent["body"]
    assert sent["method"] == "PUT" and sent["path"].endswith("/offer/1")
    assert body["listingPolicies"] == {**OFFER["listingPolicies"], "bestOfferTerms": terms}
    assert body["pricingSummary"]["price"]["value"] == "59.99"
    assert body["availableQuantity"] == 2
    assert body["categoryId"] == "180000"
    assert body["listingDescription"] == "<p>d</p>"


# --- publish path -------------------------------------------------------------

def _stub_draft(monkeypatch, item):
    created = {}
    monkeypatch.setattr(inv, "get_item", lambda _id: item)
    monkeypatch.setattr(inv, "resolve_valid_category", lambda cid, title: (cid, "Cat"))
    monkeypatch.setattr(inv, "_resolve_required_aspects", lambda c, a, h: a)
    monkeypatch.setattr(inv, "_enforce_single_cardinality", lambda c, a: a)
    monkeypatch.setattr(inv, "listing_photo_order", lambda it: ["a.jpg"])
    monkeypatch.setattr(inv, "_download_brand_image", lambda *a: None)
    monkeypatch.setattr(inv, "upload_photos", lambda paths: ["https://x/a.jpg"])
    monkeypatch.setattr(inv, "_ensure_location", lambda: None)
    monkeypatch.setattr(inv, "EBAY_FULFILLMENT_POLICY_ID", "f")
    monkeypatch.setattr(inv, "EBAY_RETURN_POLICY_ID", "r")
    monkeypatch.setattr(inv, "_request", lambda *a, **k: _Resp({}))

    def fake_create(offer):
        created["offer"] = offer
        return "99"

    monkeypatch.setattr(inv, "_create_offer", fake_create)
    return created


def _item(price, receipt=True):
    item = {"photos": ["a.jpg"], "identification": {"item_type": "Tote Bag"},
            "listing": {"title": "Michael Kors Jet Set Tote Black", "category_id": "169291",
                        "condition_id": "1000", "price": price, "description": "A bag.",
                        "item_specifics": {"Brand": "Michael Kors"}}}
    if receipt:
        item["receipt"] = {"reduced_price": 24.99}
    return item


def test_new_listing_publishes_with_best_offer_and_floor(monkeypatch):
    item = _item(129.99)
    created = _stub_draft(monkeypatch, item)
    inv.create_draft_offer("x")
    terms = created["offer"]["listingPolicies"]["bestOfferTerms"]
    assert terms == inv.best_offer_terms(item, 129.99)
    assert "autoDeclinePrice" in terms and "autoAcceptPrice" not in terms
    assert created["offer"]["listingPolicies"]["fulfillmentPolicyId"] == "f"


def test_new_listing_without_cost_gets_best_offer_but_no_floor(monkeypatch):
    created = _stub_draft(monkeypatch, _item(59.99, receipt=False))
    inv.create_draft_offer("x")
    assert created["offer"]["listingPolicies"]["bestOfferTerms"] == {"bestOfferEnabled": True}


def test_best_offer_can_be_switched_off(monkeypatch):
    created = _stub_draft(monkeypatch, _item(129.99))
    monkeypatch.setattr(inv, "BEST_OFFER_ENABLED", False)
    inv.create_draft_offer("x")
    assert "bestOfferTerms" not in created["offer"]["listingPolicies"]


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
