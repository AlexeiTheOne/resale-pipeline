"""Best Offer thresholds: on new listings, on every price the bot sets, and on
live offers via update_offer_fields.

Auto-accept at BEST_OFFER_AUTO_ACCEPT_PCT (95) of the price, auto-decline at
BEST_OFFER_AUTO_DECLINE_PCT (85), each rounded UP to .99 and never below the
break-even. eBay needs decline < accept < price (errorId 25016), so a
threshold that would break that is left off.

Run with `python tests/test_best_offer.py` or `pytest tests/`.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import ebay.inventory as inv
from profit import breakeven_price

COSTED = {"receipt": {"reduced_price": 24.99}, "listing": {"price": 129.99}}
NO_RECEIPT = {"listing": {"price": 59.99}}


@pytest.fixture(autouse=True)
def _defaults(monkeypatch):
    monkeypatch.setattr(inv, "BEST_OFFER_AUTO_ACCEPT_PCT", 95.0)
    monkeypatch.setattr(inv, "BEST_OFFER_AUTO_DECLINE_PCT", 85.0)


# --- rounding -----------------------------------------------------------------

@pytest.mark.parametrize("value, want", [(47.49, 47.99), (47.99, 47.99), (48.0, 48.99),
                                         (42.4915, 42.99), (30.000000001, 30.99), (0.5, 0.99)])
def test_up_to_99(value, want):
    assert inv.up_to_99(value) == want


# --- the thresholds -----------------------------------------------------------

def test_percentages_round_up_to_99():
    # $59.99: 95% = 56.99, 85% = 50.99 (already .99); $49.99: 47.49 -> 47.99, 42.49 -> 42.99
    assert inv.offer_thresholds(NO_RECEIPT, 59.99) == (56.99, 50.99)
    assert inv.offer_thresholds(NO_RECEIPT, 49.99) == (47.99, 42.99)


def test_low_cost_item_uses_the_percentages():
    # A cheap bag: $44.99 with an $8.49 cost, break-even ~ $12
    item = {"receipt": {"reduced_price": 8.49}, "listing": {"price": 44.99}}
    assert breakeven_price(item) < 38
    assert inv.offer_thresholds(item, 44.99) == (42.99, 38.99)


def test_neither_threshold_goes_below_breakeven():
    # A $30 cost on a $44.99 bag: 85% ($38.99) is under the break-even.
    item = {"receipt": {"reduced_price": 30.0}, "listing": {"price": 44.99}}
    be = breakeven_price(item)
    accept, decline = inv.offer_thresholds(item, 44.99)
    assert decline == inv.up_to_99(be) and decline >= be
    assert accept is None or accept >= be


def test_breakeven_pinning_both_drops_the_accept_keeps_the_floor(monkeypatch):
    # Break-even above both percentages: decline == accept would be rejected,
    # so the floor stays and nothing is accepted automatically.
    item = {"receipt": {"reduced_price": 30.0}, "listing": {"price": 44.99}}
    be99 = inv.up_to_99(breakeven_price(item))
    monkeypatch.setattr(inv, "BEST_OFFER_AUTO_ACCEPT_PCT", 60.0)
    monkeypatch.setattr(inv, "BEST_OFFER_AUTO_DECLINE_PCT", 50.0)
    assert inv.offer_thresholds(item, 44.99) == (None, be99)


def test_no_accept_when_rounding_reaches_the_price():
    # $9.99: 95% = 9.49 -> 9.99, the price itself.
    assert inv.offer_thresholds(NO_RECEIPT, 9.99) == (None, 8.99)


def test_no_decline_when_breakeven_reaches_the_price():
    item = {"receipt": {"reduced_price": 40.0}, "listing": {"price": 39.99}}
    assert breakeven_price(item) >= 39.99
    assert inv.offer_thresholds(item, 39.99) == (None, None)
    assert inv.best_offer_terms(item, 39.99) == {"bestOfferEnabled": True}


def test_no_floor_when_breakeven_is_under_a_dollar(monkeypatch):
    # A tiny receipt puts break-even below zero, and eBay rejected the
    # resulting $0 floor (errorId 25016).
    cheap = {"receipt": {"reduced_price": 0.39}, "listing": {"price": 39.99}}
    assert breakeven_price(cheap) < 1
    monkeypatch.setattr(inv, "BEST_OFFER_AUTO_ACCEPT_PCT", 0.0)
    monkeypatch.setattr(inv, "BEST_OFFER_AUTO_DECLINE_PCT", 0.0)
    assert inv.offer_thresholds(cheap, 39.99) == (None, None)
    assert inv.best_offer_terms(cheap, 39.99) == {"bestOfferEnabled": True}


def test_percentages_are_settings(monkeypatch):
    monkeypatch.setattr(inv, "BEST_OFFER_AUTO_ACCEPT_PCT", 90.0)
    monkeypatch.setattr(inv, "BEST_OFFER_AUTO_DECLINE_PCT", 70.0)
    assert inv.offer_thresholds(NO_RECEIPT, 59.99) == (53.99, 41.99)
    monkeypatch.setattr(inv, "BEST_OFFER_AUTO_ACCEPT_PCT", 0.0)   # 0 = no auto-accept
    assert inv.offer_thresholds(NO_RECEIPT, 59.99) == (None, 41.99)


def test_terms_carry_both_thresholds():
    assert inv.best_offer_terms(NO_RECEIPT, 59.99) == {
        "bestOfferEnabled": True,
        "autoAcceptPrice": {"value": "56.99", "currency": "USD"},
        "autoDeclinePrice": {"value": "50.99", "currency": "USD"}}
    assert inv.auto_decline_price(NO_RECEIPT, 59.99) == 50.99


@pytest.mark.parametrize("price", [9.99, 19.99, 24.99, 44.99, 59.99, 99.99, 129.99, 549.99])
@pytest.mark.parametrize("item", [COSTED, NO_RECEIPT,
                                  {"receipt": {"reduced_price": 30.0}, "listing": {}}])
def test_ebay_ordering_always_holds(item, price):
    accept, decline = inv.offer_thresholds(item, price)
    if accept is not None:
        assert accept < price
    if decline is not None:
        assert 1 <= decline < (accept if accept is not None else price)


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


# --- price changes (update_offer_price) -------------------------------------------

def _price_put(monkeypatch, offer):
    sent = {}

    def fake_request(method, path, **kw):
        if method == "GET":
            return _Resp(dict(offer))
        sent.update(method=method, body=kw["json"])
        return _Resp({})

    monkeypatch.setattr(inv, "_request", fake_request)
    return sent


def _with_terms(**terms):
    return {**OFFER, "listingPolicies": {**OFFER["listingPolicies"],
                                         "bestOfferTerms": {"bestOfferEnabled": True, **terms}}}


def test_price_cut_below_the_old_floor_rewrites_it_in_the_same_put(monkeypatch):
    # bff24df0, live 2026-11-12: the offer kept a $99.99 auto-decline, so any
    # price at or below it failed with errorId 25016.
    sent = _price_put(monkeypatch, _with_terms(
        autoDeclinePrice={"value": "99.99", "currency": "USD"}))
    body = inv.update_offer_price("1", 89.99, NO_RECEIPT)
    assert sent["body"] == body
    assert body["pricingSummary"]["price"]["value"] == "89.99"
    terms = body["listingPolicies"]["bestOfferTerms"]
    assert terms == inv.best_offer_terms(NO_RECEIPT, 89.99)
    assert terms["autoAcceptPrice"]["value"] == "85.99"      # 95% = 85.49 -> 85.99
    assert terms["autoDeclinePrice"]["value"] == "76.99"     # 85% = 76.49 -> 76.99
    assert body["listingPolicies"]["fulfillmentPolicyId"] == "f"   # policies carried over


def test_price_change_leaves_best_offer_off_when_it_is_off(monkeypatch):
    sent = _price_put(monkeypatch, OFFER)          # no bestOfferTerms at all
    inv.update_offer_price("1", 49.99, NO_RECEIPT)
    assert "bestOfferTerms" not in sent["body"]["listingPolicies"]
    sent = _price_put(monkeypatch, {**OFFER, "listingPolicies": {
        **OFFER["listingPolicies"], "bestOfferTerms": {"bestOfferEnabled": False}}})
    inv.update_offer_price("1", 49.99, NO_RECEIPT)
    assert sent["body"]["listingPolicies"]["bestOfferTerms"] == {"bestOfferEnabled": False}


def test_price_change_without_item_keeps_terms_as_they_are(monkeypatch):
    offer = _with_terms(autoDeclinePrice={"value": "40.00", "currency": "USD"})
    sent = _price_put(monkeypatch, offer)
    inv.update_offer_price("1", 59.99)
    assert sent["body"]["listingPolicies"] == offer["listingPolicies"]


# --- publish path -------------------------------------------------------------

def _stub_draft(monkeypatch, item):
    created = {}
    monkeypatch.setattr(inv, "get_item", lambda _id: item)
    monkeypatch.setattr(inv, "resolve_valid_category", lambda cid, title: (cid, "Cat"))
    monkeypatch.setattr(inv, "_resolve_required_aspects", lambda c, a, h, i=None: a)
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
    assert "autoDeclinePrice" in terms and "autoAcceptPrice" in terms
    assert created["offer"]["listingPolicies"]["fulfillmentPolicyId"] == "f"


def test_new_listing_without_cost_gets_the_percentages(monkeypatch):
    created = _stub_draft(monkeypatch, _item(59.99, receipt=False))
    inv.create_draft_offer("x")
    assert created["offer"]["listingPolicies"]["bestOfferTerms"] == {
        "bestOfferEnabled": True, "autoAcceptPrice": {"value": "56.99", "currency": "USD"},
        "autoDeclinePrice": {"value": "50.99", "currency": "USD"}}


def test_best_offer_can_be_switched_off(monkeypatch):
    created = _stub_draft(monkeypatch, _item(129.99))
    monkeypatch.setattr(inv, "BEST_OFFER_ENABLED", False)
    inv.create_draft_offer("x")
    assert "bestOfferTerms" not in created["offer"]["listingPolicies"]


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
