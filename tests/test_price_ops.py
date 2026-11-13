"""pipeline/price_ops: the price checks and the eBay-first write shared by
/setprice and the reprice button. Made-up items; no eBay, no real DB."""
from datetime import date, timedelta

import pytest

import db
import profit
from ebay import inventory
from pipeline import price_ops


def _item(status="published", **ebay):
    return {"item_id": "abcd1234-0000-4000-8000-000000000001", "status": status,
            "listing": {"title": "Sample Tote", "price": 49.99},
            "pricing": {"suggested_price": 49.99, "machine_price": 52.0},
            "ebay": {"offer_id": "OFFER1", **ebay}}


@pytest.fixture
def world(monkeypatch):
    events = []
    monkeypatch.setattr(db, "update_field", lambda iid, field, value: events.append(("db", field, value)))

    def push(offer_id, price, item):
        events.append(("ebay", offer_id, price))
        return {"listingPolicies": {"bestOfferTerms": {"autoAcceptPrice": {"value": "37.99"}}}}

    monkeypatch.setattr(inventory, "update_offer_price", push)
    return events


def test_ebay_first_then_db(world):
    res = price_ops.set_price(_item(), 39.99, {"suggested_price": 39.99})
    assert [e[0] for e in world] == ["ebay", "db", "db"]
    assert world[1][2]["price"] == 39.99 and world[2] == ("db", "pricing", {"suggested_price": 39.99})
    assert res == {"pushed": True, "detached": False, "where": "live listing",
                   "terms": {"autoAcceptPrice": {"value": "37.99"}}}


def test_ebay_refusal_leaves_db_alone(world, monkeypatch):
    boom = RuntimeError("25016")
    monkeypatch.setattr(inventory, "update_offer_price", lambda *a: (_ for _ in ()).throw(boom))
    with pytest.raises(price_ops.EbayRefused) as err:
        price_ops.set_price(_item(), 39.99, {})
    assert err.value.__cause__ is boom
    assert world == []


def test_seller_hub_relist_saves_locally_only(world):
    res = price_ops.set_price(_item(relisted_in_seller_hub=True), 39.99, {})
    assert res["detached"] and not res["pushed"]
    assert [e[0] for e in world] == ["db", "db"]


def test_draft_says_ebay_draft(world):
    assert price_ops.set_price(_item("ebay_draft"), 39.99, {})["where"] == "eBay draft"


def test_status_gate_unless_any_status(world):
    """/setprice pushes only drafts and live items; the reprice button pushes
    any item with an offer (as before the move)."""
    res = price_ops.set_price(_item("sold"), 39.99, {})
    assert not res["pushed"] and [e[0] for e in world] == ["db", "db"]
    world.clear()
    assert price_ops.set_price(_item("sold"), 39.99, {}, any_status=True)["pushed"]
    assert world[0][0] == "ebay"


def test_no_offer_is_local(world):
    item = _item()
    item["ebay"] = {}
    res = price_ops.set_price(item, 39.99, {})
    assert res == {"pushed": False, "detached": False, "where": None, "terms": None}


def test_needs_force(monkeypatch):
    monkeypatch.setattr(profit, "breakeven_price", lambda item: 20.0)
    assert price_ops.needs_force(_item(), 25.0) is None
    assert price_ops.needs_force(_item(), 20.0) == {"why": "floor", "floor": 20.0}
    end = (date.today() + timedelta(days=5)).isoformat()
    frozen = _item(ship_test={"group": "control", "start": "2026-01-01", "end": end})
    assert price_ops.needs_force(frozen, 25.0) == {"why": "ship_test", "end": end}
    monkeypatch.setattr(profit, "breakeven_price", lambda item: None)
    assert price_ops.floor_breach(_item(), 1.0) is None


def test_pricing_records_keep_the_input():
    before = {"suggested_price": 49.99, "machine_price": 52.0}
    manual = price_ops.manual_pricing(before, 44.99)
    assert manual["suggested_price"] == 44.99 and manual["machine_price"] == 52.0
    assert manual["price_basis"] == "manual" and manual["manual_price"] == 44.99
    rep = price_ops.reprice_pricing(before, 46.99)
    assert (rep["suggested_price"], rep["price_basis"], rep["confidence"]) == (46.99, "reprice", "reprice")
    assert before == {"suggested_price": 49.99, "machine_price": 52.0}
