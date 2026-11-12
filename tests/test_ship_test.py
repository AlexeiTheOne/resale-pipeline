"""The free-shipping test (pipeline/ship_test.py, bot/ship_test_cmds.py).

No eBay or Telegram calls: eBay and the DB are faked."""
import copy
from datetime import date

import pytest

import config
import db
import profit
from bot import listing_cmds, ship_test_cmds, state
from ebay import analytics, inventory
from pipeline import offers, reprice, ship_test
from tests import tg_fakes

T1, T2 = ship_test.TEST[0], ship_test.TEST[1]
C1 = ship_test.CONTROL[0]
FREE_POLICY = "FREE-1"


def _item(prefix, price=44.99, cost=9.99, **ebay):
    iid = f"{prefix}-0000-4000-8000-000000000000"
    return {"item_id": iid, "status": "published",
            "listing": {"title": f"Bag {prefix}", "price": price},
            "pricing": {"suggested_price": price},
            "receipt": {"reduced_price": cost},
            "ebay": {"offer_id": f"o-{prefix}", "sku": iid, "listing_id": f"L{prefix}",
                     "quantity": 1, **ebay}}


class _DB:
    def __init__(self, monkeypatch, *items):
        self.rows = {i["item_id"]: copy.deepcopy(i) for i in items}
        self.writes = []
        monkeypatch.setattr(db, "list_items", lambda status=None: [
            copy.deepcopy(i) for i in self.rows.values() if status in (None, i["status"])])
        monkeypatch.setattr(db, "get_item", lambda iid: copy.deepcopy(self.rows.get(iid)))
        monkeypatch.setattr(db, "update_field", self.update_field)

    def update_field(self, iid, field, value):
        self.writes.append((iid, field))
        self.rows[iid][field] = copy.deepcopy(value)


class _Ebay:
    """Offers, policies and price PUTs, in memory."""
    def __init__(self, monkeypatch, fail=(), policies=()):
        self.puts, self.created, self.fail = [], [], set(fail)
        self.policies = list(policies)
        monkeypatch.setattr(inventory, "get_offer", lambda oid: {
            "listingPolicies": {"fulfillmentPolicyId": "STD-1",
                                "bestOfferTerms": {"bestOfferEnabled": True}}})
        monkeypatch.setattr(inventory, "ensure_free_shipping_policy", self.ensure)
        monkeypatch.setattr(inventory, "update_offer_price", self.put)

    def ensure(self, template, name):
        self.created.append((template, name))
        return FREE_POLICY

    def put(self, offer_id, price, item=None, fulfillment_policy_id=None):
        if offer_id in self.fail:
            raise RuntimeError("eBay API PUT failed [400]")
        self.puts.append((offer_id, price, (item or {}).get("ebay", {}).get("free_shipping"),
                          fulfillment_policy_id))
        return {}


# --- the profit floor on free shipping ---------------------------------------------

def test_free_shipping_charges_nothing_and_pays_the_bag_label():
    item = _item(T1)
    assert profit.shipping(item) == (config.EBAY_SHIP_CHARGED, config.EBAY_SHIP_COST)
    free = ship_test.on_free_shipping(item)
    assert profit.shipping(free) == (0.0, config.FREE_SHIPPING_LABEL_COST)
    rise = profit.breakeven_price(free) - profit.breakeven_price(item)
    # The plan's ~$8.44 for the charge, plus the $0.97 higher label, over the fee rate.
    want = (config.EBAY_SHIP_CHARGED * (1 - config.EBAY_FVF_PCT)
            + config.FREE_SHIPPING_LABEL_COST - config.EBAY_SHIP_COST) / (
        1 - config.EBAY_FVF_PCT - config.EBAY_AD_FEE_PCT)
    assert rise == pytest.approx(want)
    proj = profit.project(free, price=53.99)
    assert proj["breakeven"] == pytest.approx(profit.breakeven_price(free))


def test_new_price_keeps_the_99_ending():
    assert ship_test.new_price(44.99) == 53.99
    assert ship_test.new_price(100.00) == 109.00


# --- the policy --------------------------------------------------------------------

TEMPLATE = {
    "fulfillmentPolicyId": "STD-1", "name": "Flat $9.99", "marketplaceId": "EBAY_US",
    "categoryTypes": [{"name": "ALL_EXCLUDING_MOTORS_VEHICLES", "default": True}],
    "handlingTime": {"value": 1, "unit": "DAY"},
    "shippingOptions": [{"optionType": "DOMESTIC", "costType": "FLAT_RATE", "shippingServices": [
        {"shippingServiceCode": "USPSGroundAdvantage", "sortOrder": 1, "freeShipping": False,
         "shippingCost": {"value": "9.99", "currency": "USD"},
         "additionalShippingCost": {"value": "2.00", "currency": "USD"}},
        {"shippingServiceCode": "USPSPriority", "sortOrder": 2,
         "shippingCost": {"value": "14.99", "currency": "USD"}}]}],
    "warnings": []}


def test_policy_copy_makes_only_the_first_domestic_service_free():
    body = inventory.free_shipping_policy_body(TEMPLATE, "Free shipping")
    assert body["name"] == "Free shipping"
    assert "fulfillmentPolicyId" not in body and "warnings" not in body
    assert body["categoryTypes"] == [{"name": "ALL_EXCLUDING_MOTORS_VEHICLES"}]   # never the default
    first, second = body["shippingOptions"][0]["shippingServices"]
    assert first["freeShipping"] is True
    assert first["shippingCost"] == {"value": "0.00", "currency": "USD"}
    assert first["additionalShippingCost"]["value"] == "0.00"
    assert second["shippingCost"]["value"] == "14.99"
    assert body["handlingTime"] == TEMPLATE["handlingTime"]
    assert TEMPLATE["shippingOptions"][0]["shippingServices"][0]["freeShipping"] is False  # not mutated


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


def test_policy_is_created_once_then_found_by_name(monkeypatch):
    calls, existing = [], []

    def fake(method, path, **kw):
        calls.append((method, path))
        if method == "GET" and path.endswith("/fulfillment_policy"):
            return _Resp({"fulfillmentPolicies": list(existing)})
        if method == "GET":
            return _Resp(TEMPLATE)
        existing.append({"name": kw["json"]["name"], "fulfillmentPolicyId": "NEW-9"})
        return _Resp({"fulfillmentPolicyId": "NEW-9"})

    monkeypatch.setattr(inventory, "_request", fake)
    assert inventory.ensure_free_shipping_policy("STD-1", "Free shipping") == "NEW-9"
    assert ("POST", "/sell/account/v1/fulfillment_policy") in calls
    calls.clear()
    assert inventory.ensure_free_shipping_policy("STD-1", "Free shipping") == "NEW-9"
    assert [m for m, _ in calls] == ["GET"]


def test_price_update_can_switch_the_shipping_policy(monkeypatch):
    offer = {"availableQuantity": 1, "pricingSummary": {"price": {"value": "44.99"}},
             "listingPolicies": {"fulfillmentPolicyId": "STD-1", "returnPolicyId": "r",
                                 "bestOfferTerms": {"bestOfferEnabled": True}}}
    sent = {}

    def fake(method, path, **kw):
        if method == "GET":
            return _Resp(copy.deepcopy(offer))
        sent["body"] = kw["json"]
        return _Resp({})

    monkeypatch.setattr(inventory, "_request", fake)
    free = ship_test.on_free_shipping(_item(T1))
    inventory.update_offer_price("o", 53.99, free, fulfillment_policy_id=FREE_POLICY)
    pol = sent["body"]["listingPolicies"]
    assert pol["fulfillmentPolicyId"] == FREE_POLICY and pol["returnPolicyId"] == "r"
    assert pol["bestOfferTerms"] == inventory.best_offer_terms(free, 53.99)
    assert sent["body"]["pricingSummary"]["price"]["value"] == "53.99"


# --- preview and apply ---------------------------------------------------------------

def test_preview_lists_both_groups_with_new_prices_and_terms(monkeypatch):
    _DB(monkeypatch, _item(T1), _item(T2, price=59.99), _item(C1))
    p = ship_test.preview()
    assert not p["started"]
    assert [r["new_price"] for r in p["test"]] == [53.99, 68.99]
    row = p["test"][0]
    free = ship_test.on_free_shipping(_item(T1))
    assert row["terms"] == inventory.best_offer_terms(free, 53.99)
    assert row["terms"]["autoAcceptPrice"]["value"] == "51.99"     # 95% of 53.99 = 51.29
    assert row["terms"]["autoDeclinePrice"]["value"] == "45.99"    # 85% = 45.89
    assert [r["item_id"][:8] for r in p["control"]][:1] == [C1]
    missing = {pid for pid, _, why in p["problems"] if why == "not in the DB"}
    assert ship_test.TEST[2] in missing
    lines = ship_test_cmds.preview_lines(p)
    assert any("$44.99 → $53.99" in line for line in lines)


def test_preview_leaves_out_sold_and_relisted(monkeypatch):
    sold = _item(T1)
    sold["status"] = "sold"
    _DB(monkeypatch, sold, _item(T2, relisted_in_seller_hub=True))
    p = ship_test.preview()
    why = {pid: w for pid, _, w in p["problems"]}
    assert why[T1] == "status is sold" and "Seller Hub" in why[T2]
    assert p["test"] == []


def test_apply_sends_ebay_first_then_saves(monkeypatch):
    store = _DB(monkeypatch, _item(T1), _item(T2, price=59.99), _item(C1))
    ebay = _Ebay(monkeypatch)
    out = ship_test.apply(ship_test.preview(), today=date(2026, 11, 13))
    assert ebay.created == [("STD-1", config.FREE_SHIPPING_POLICY_NAME)]
    assert ebay.puts == [(f"o-{T1}", 53.99, True, FREE_POLICY), (f"o-{T2}", 68.99, True, FREE_POLICY)]
    t1 = store.rows[_item(T1)["item_id"]]
    assert t1["listing"]["price"] == 53.99 and t1["ebay"]["free_shipping"] is True
    assert t1["ebay"]["ship_test"] == {"group": "test", "start": "2026-11-13", "end": "2026-12-04",
                                       "price_before": 44.99, "policy_before": "STD-1",
                                       "policy": FREE_POLICY}
    c1 = store.rows[_item(C1)["item_id"]]
    assert c1["listing"]["price"] == 44.99 and "free_shipping" not in c1["ebay"]
    assert c1["ebay"]["ship_test"]["group"] == "control"
    assert len(out["applied"]) == 2 and len(out["marked"]) == 1


def test_a_refused_listing_keeps_its_db_price(monkeypatch):
    store = _DB(monkeypatch, _item(T1), _item(T2))
    _Ebay(monkeypatch, fail={f"o-{T1}"})
    out = ship_test.apply(ship_test.preview())
    t1 = store.rows[_item(T1)["item_id"]]
    assert t1["listing"]["price"] == 44.99 and "ship_test" not in t1["ebay"]
    assert out["failed"][0][0] == T1 and len(out["applied"]) == 1


def test_a_price_moved_since_the_preview_is_skipped(monkeypatch):
    store = _DB(monkeypatch, _item(T1))
    ebay = _Ebay(monkeypatch)
    p = ship_test.preview()
    store.rows[_item(T1)["item_id"]]["listing"]["price"] = 39.99
    out = ship_test.apply(p)
    assert ebay.puts == [] and "price changed" in out["failed"][0][1]


def test_policy_failure_changes_nothing(monkeypatch):
    store = _DB(monkeypatch, _item(T1), _item(C1))
    ebay = _Ebay(monkeypatch)

    def boom(*a):
        raise RuntimeError("eBay API POST failed [400]")

    monkeypatch.setattr(inventory, "ensure_free_shipping_policy", boom)
    with pytest.raises(RuntimeError):
        ship_test.apply(ship_test.preview())
    assert ebay.puts == [] and store.writes == []


# --- the freeze ----------------------------------------------------------------------

def _rec(group="test", end="2026-12-04"):
    return {"group": group, "start": "2026-11-13", "end": end, "price_before": 44.99}


def test_frozen_until_the_end_day_inclusive():
    item = _item(T1, ship_test=_rec())
    assert ship_test.frozen(item, date(2026, 12, 4))
    assert not ship_test.frozen(item, date(2026, 12, 5))
    assert not ship_test.frozen(_item(T1))


def test_offers_skip_frozen_listings(monkeypatch):
    _DB(monkeypatch, _item(T1, ship_test=_rec()), _item(T2))
    ids = [i["item_id"][:8] for i in offers.items_with_live_stock()]
    assert ids == [T2]


def test_weekly_check_drops_frozen_results_but_counts_them(monkeypatch):
    _DB(monkeypatch, _item(T1, ship_test=_rec(end="2999-01-01")), _item(T2))
    monkeypatch.setattr(reprice, "list_items", db.list_items)
    monkeypatch.setattr(reprice, "_traffic_for_all", lambda items: ({}, {"batches": 1, "failed": 0}))
    monkeypatch.setattr(reprice, "evaluate_item", lambda item, t, b: {
        "item_id": item["item_id"], "verdict": "CUT_PRICE", "suggested_price": 30.0, "watchers": 3})
    seen = {}
    monkeypatch.setattr(reprice, "build_offer_plan", lambda items, results: seen.setdefault(
        "ids", [r["item_id"][:8] for r in results]) and {"rows": []})
    results, health = reprice.run_weekly_check()
    assert [r["item_id"][:8] for r in results] == [T2]
    assert seen["ids"] == [T2] and health["frozen"] == 1


def test_setprice_refuses_a_frozen_listing_without_force(monkeypatch):
    store = _DB(monkeypatch, _item(T1, ship_test=_rec(end="2999-01-01")))
    pushed = []
    monkeypatch.setattr(inventory, "update_offer_price", lambda *a, **k: pushed.append(a) or {})
    update, ctx = tg_fakes.make_text_update(f"/setprice {T1} 39.99")
    tg_fakes.run(listing_cmds.setprice_command, update, ctx)
    assert pushed == [] and store.writes == []
    assert any("free-shipping test" in t for t in tg_fakes.sent_texts(ctx))


# --- the report ------------------------------------------------------------------------

def test_report_compares_each_group_with_its_own_baseline(monkeypatch):
    t = _item(T1, ship_test=_rec(), free_shipping=True,
              sales=[{"sold_at": "2026-11-20T10:00:00Z", "units": 1}])
    c = _item(C1, ship_test=_rec("control"))
    _DB(monkeypatch, t, c)
    calls = []

    def traffic(ids, days=7, *, start=None, end=None):
        calls.append((start, end))
        during = start == date(2026, 11, 13)
        return {f"L{T1}": {"impressions": 1000, "views": 9 if during else 5},
                f"L{C1}": {"impressions": 1000, "views": 5}}

    monkeypatch.setattr(analytics, "get_traffic_report", traffic)
    r = ship_test.report(today=date(2026, 11, 22))
    assert r["days"] == 10
    assert calls == [(date(2026, 11, 13), date(2026, 11, 22)), (date(2026, 11, 3), date(2026, 11, 12))]
    tg, cg = r["groups"]["test"], r["groups"]["control"]
    assert tg["during"]["ctr"] == pytest.approx(0.009) and tg["before"]["ctr"] == pytest.approx(0.005)
    assert tg["during"]["sales"] == 1 and cg["during"]["sales"] == 0
    assert any("CTR 0.50% → 0.90%" in line for line in ship_test_cmds.report_lines(r))


# --- the command ------------------------------------------------------------------------

def test_command_is_owner_only(monkeypatch):
    monkeypatch.setattr(config, "OWNER_USER_IDS", set())
    update, ctx = tg_fakes.make_text_update("/shiptest")
    tg_fakes.run(ship_test_cmds.ship_test_command, update, ctx)
    assert any("owner-only" in t for t in tg_fakes.sent_texts(ctx))


def test_command_previews_then_applies_on_the_button(monkeypatch):
    store = _DB(monkeypatch, _item(T1), _item(C1))
    ebay = _Ebay(monkeypatch)
    update, ctx = tg_fakes.make_text_update("/shiptest")
    monkeypatch.setattr(config, "OWNER_USER_IDS", {update.effective_user.id})
    tg_fakes.run(ship_test_cmds.ship_test_command, update, ctx)
    assert ebay.puts == [] and store.writes == []          # preview sends nothing
    [token] = list(state.ship_test)
    cb, cctx = tg_fakes.make_callback_update(f"shipt:apply:{token}")
    tg_fakes.run(ship_test_cmds.ship_test_callback, cb, cctx)
    assert ebay.puts == [(f"o-{T1}", 53.99, True, FREE_POLICY)]
    assert any("Test started" in t for t in tg_fakes.sent_texts(cctx))
    assert state.ship_test == {}


def test_test_state_round_trips_through_the_db():
    """ship_test and free_shipping have no columns of their own; they must
    survive the real DB layer (ebay_raw on SQL Server)."""
    iid = db.create_item([])
    rec = {**_rec(), "policy_before": "STD-1", "policy": FREE_POLICY}
    db.update_field(iid, "ebay", {"offer_id": "o1", "sku": iid, "quantity": 1,
                                  "free_shipping": True, "ship_test": rec})
    ebay = db.get_item(iid)["ebay"]
    assert ebay["free_shipping"] is True and ebay["ship_test"] == rec
    assert ship_test.frozen(db.get_item(iid), date(2026, 11, 20))
