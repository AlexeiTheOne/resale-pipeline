"""Sale money per order: ad fees, refunds, shipping discounts, per-order dates.

The 2026-10-29 review found the recorded net overstated by ~$390 over 86 orders:
Promoted Listings fees were never attributed (each one names its order under
references[ORDER_ID]), a refund that posted after the sale was never picked up,
deliveryDiscount was ignored, and merging orders kept only the newest sold_at.
The transaction shapes below are copied from this account's real Finances data.

No eBay or Telegram calls; the DB is pointed at a temp file before import.
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import db  # noqa: E402

db.DB_PATH = str(Path(tempfile.mkdtemp(prefix="ledger_")) / "test.db")
db.create_tables()

import profit  # noqa: E402
import telegram_bot as tb  # noqa: E402
from ebay import orders  # noqa: E402

CENT = 0.005


def _amt(v):
    return {"value": str(v), "currency": "USD"}


def _order(oid, created, sale, ship, sku="S1", listing="L1", qty=1, discount=None):
    summary = {"priceSubtotal": _amt(sale), "deliveryCost": _amt(ship)}
    if discount is not None:
        summary["deliveryDiscount"] = _amt(discount)
    return {"orderId": oid, "creationDate": created, "pricingSummary": summary,
            "cancelStatus": {"cancelState": "NONE_REQUESTED"},
            "lineItems": [{"sku": sku, "legacyItemId": listing, "quantity": qty,
                           "lineItemCost": _amt(sale)}]}


def _sale_txn(oid, fvf):
    return {"orderId": oid, "transactionType": "SALE", "bookingEntry": "CREDIT",
            "amount": _amt(0), "totalFeeAmount": _amt(fvf)}


def _label(oid, cost, memo="Shipping label purchased"):
    return {"orderId": oid, "transactionType": "SHIPPING_LABEL", "bookingEntry": "DEBIT",
            "amount": _amt(cost), "transactionMemo": memo}


def _ad(oid, fee, entry="DEBIT"):
    # No top-level orderId: the order is only in references.
    return {"transactionType": "NON_SALE_CHARGE", "bookingEntry": entry,
            "amount": _amt(fee), "transactionMemo": "Promoted Listings - General fee",
            "feeType": "AD_FEE",
            "references": [{"referenceId": "L1", "referenceType": "ITEM_ID"},
                           {"referenceId": oid, "referenceType": "ORDER_ID"}]}


def _refund_set(oid, paid, fee_back):
    """A return as eBay books it: dispute hold, its release, and the REFUND net
    of the fee eBay returns."""
    ref = [{"referenceId": "R1", "referenceType": "RETURN_ID"}]
    return [
        {"orderId": oid, "transactionType": "DISPUTE", "bookingEntry": "DEBIT",
         "amount": _amt(paid), "totalFeeAmount": None, "references": ref},
        {"orderId": oid, "transactionType": "CREDIT", "bookingEntry": "CREDIT",
         "amount": _amt(paid), "totalFeeAmount": None, "references": ref},
        {"orderId": oid, "transactionType": "REFUND", "bookingEntry": "DEBIT",
         "amount": _amt(round(paid - fee_back, 2)), "totalFeeAmount": _amt(fee_back),
         "references": ref},
    ]


def test_ad_fees_are_attributed_to_their_order():
    led = orders._fees_by_order([_sale_txn("O1", 8.0), _ad("O1", 2.77),
                                 _ad("O1", 0.50, entry="CREDIT")])
    assert abs(led["O1"]["ad_fees"] - 2.27) < CENT, led
    m = orders.sales_by_item(orders=[_order("O1", "2026-10-01T00:00:00.000Z", 50, 9.99)],
                             transactions=[_sale_txn("O1", 8.0), _label("O1", 6.0),
                                           _ad("O1", 2.77)])
    rec = m["S1"]
    assert rec["ad_fees"] == 2.77
    assert abs(rec["net_proceeds"] - (50 + 9.99 - 8.0 - 6.0 - 2.77)) < CENT, rec


def test_full_refund_with_return_label_nets_negative():
    """The Versus Versace watch: $89.99 + $9.99, refunded in full, outbound and
    return labels paid."""
    txns = ([_sale_txn("V", 16.23), _label("V", 7.33), _label("V", 5.97, "Return shipping label"),
             _ad("V", 3.0)] + _refund_set("V", 99.98, 16.23))
    m = orders.sales_by_item(orders=[_order("V", "2026-08-20T00:00:00.000Z", 89.99, 9.99)],
                             transactions=txns)
    rec = m["S1"]
    assert rec["refunded"] == 99.98 and rec["gross_sale_price"] == 89.99
    assert rec["sale_price"] == 0 and rec["shipping_collected"] == 0
    assert abs(rec["ebay_fees"]) < CENT              # eBay returned the whole FVF
    assert abs(rec["net_proceeds"] - (-7.33 - 5.97 - 3.0)) < CENT, rec


def test_delivery_discount_comes_off_shipping():
    m = orders.sales_by_item(
        orders=[_order("D", "2026-08-01T00:00:00.000Z", 99.98, 19.98, qty=2, discount=-7.0)],
        transactions=[_sale_txn("D", 17.5)])
    assert m["S1"]["shipping_collected"] == 12.98
    assert m["S1"]["units"] == 2


def test_each_order_keeps_its_own_date_and_money():
    m = orders.sales_by_item(
        orders=[_order("B", "2026-10-05T00:00:00.000Z", 40, 9.99),
                _order("A", "2026-08-05T00:00:00.000Z", 50, 9.99)],
        transactions=[_sale_txn("A", 9.0), _sale_txn("B", 7.5), _ad("B", 2.0)])
    rec = m["S1"]
    assert [s["order_id"] for s in rec["sales"]] == ["A", "B"]
    assert [s["sold_at"][:7] for s in rec["sales"]] == ["2026-08", "2026-10"]
    assert rec["sold_at"].startswith("2026-10") and rec["first_sold_at"].startswith("2026-08")
    assert rec["sales"][1]["ad_fee"] == 2.0 and rec["sales"][0]["ad_fee"] == 0
    assert abs(sum(s["net"] for s in rec["sales"]) - rec["net_proceeds"]) < CENT
    assert rec["order_ids"] == ["A", "B"] and rec["units"] == 2
    # The listing-id alias carries the same figures, in its own copy.
    assert m["L1"]["net_proceeds"] == rec["net_proceeds"]
    assert m["L1"]["sales"] is not rec["sales"]


def _sync_with(item, offer, sold_map):
    writes = []
    saved = (tb.get_offer, tb.update_field, tb.update_status)
    tb.get_offer = lambda _id: offer
    tb.update_field = lambda *a: writes.append(("field",) + a)
    tb.update_status = lambda *a: writes.append(("status",) + a)
    try:
        return tb._sync_one(item, sold_map), writes
    finally:
        tb.get_offer, tb.update_field, tb.update_status = saved


_OFFER_ENDED = {"pricingSummary": {"price": {"value": "89.99"}}, "availableQuantity": 0,
                "listing": {"listingStatus": "ENDED"}}


def _recorded(sold_map_entry):
    """An item row as /sync would have stored it from `sold_map_entry`."""
    return {"item_id": "S1", "status": "sold", "listing": {"price": 89.99},
            "ebay": {"offer_id": "o", "quantity": 0, **orders.sale_db_fields(sold_map_entry)}}


def test_a_late_refund_updates_the_recorded_sale_quietly():
    sold = [_order("V", "2026-08-20T00:00:00.000Z", 89.99, 9.99)]
    before = orders.sales_by_item(orders=sold, transactions=[_sale_txn("V", 16.23), _label("V", 7.33)])
    item = _recorded(before["S1"])
    assert item["ebay"]["net_proceeds"] > 70

    after = orders.sales_by_item(orders=sold, transactions=[_sale_txn("V", 16.23), _label("V", 7.33)]
                                 + _refund_set("V", 99.98, 16.23))
    result, writes = _sync_with(item, _OFFER_ENDED, after)
    assert not result["auto_sold"] and result["new_sale"] is None
    assert not [w for w in writes if w[0] == "status"]          # never re-marked
    (_, _, field, ebay), = [w for w in writes if w[0] == "field"]
    assert field == "ebay" and abs(ebay["net_proceeds"] - (-7.33)) < CENT
    assert ebay["refunded"] == 99.98
    assert any("refund" in c for c in result["changes"])

    # Same data again: nothing to write.
    item["ebay"] = ebay
    result, writes = _sync_with(item, _OFFER_ENDED, after)
    assert writes == [] and result["changes"] == []


def test_no_refresh_when_fees_are_unknown():
    sold = [_order("V", "2026-08-20T00:00:00.000Z", 89.99, 9.99)]
    item = _recorded(orders.sales_by_item(orders=sold, transactions=[_sale_txn("V", 16.23)])["S1"])
    gross = orders.sales_by_item(orders=sold, transactions=[])["S1"]
    gross = orders.totals_from_sales([dict(r, fees_known=False, fees=0.0) for r in gross["sales"]])
    _, writes = _sync_with(item, _OFFER_ENDED, {"S1": gross})
    assert writes == []


def test_short_window_folds_new_order_onto_stored_history():
    """An order that has aged out of the window keeps its stored record; a new
    one in the window is added on top, not instead."""
    old = orders.sales_by_item(orders=[_order("A", "2026-05-01T00:00:00.000Z", 50, 9.99)],
                               transactions=[_sale_txn("A", 9.0)])
    item = _recorded(old["S1"])
    item["status"] = "published"
    window = orders.sales_by_item(orders=[_order("B", "2026-10-20T00:00:00.000Z", 40, 9.99)],
                                  transactions=[_sale_txn("B", 7.5)])
    offer = {"pricingSummary": {"price": {"value": "89.99"}}, "availableQuantity": 0,
             "listing": {"listingStatus": "ACTIVE"}}
    result, writes = _sync_with(item, offer, window)
    assert result["new_sale"]["orders"] == 1
    ebay = [w for w in writes if w[0] == "field"][-1][3]
    assert ebay["order_ids"] == ["A", "B"] and ebay["units_sold"] == 2
    assert abs(ebay["net_proceeds"] - (50 + 9.99 - 9.0 + 40 + 9.99 - 7.5)) < CENT
    assert ebay["first_sold_at"].startswith("2026-05")


def test_projection_uses_the_real_ad_fee():
    rec = orders.sales_by_item(orders=[_order("O1", "2026-10-01T00:00:00.000Z", 50, 9.99)],
                               transactions=[_sale_txn("O1", 8.0), _label("O1", 6.0),
                                             _ad("O1", 2.77)])["S1"]
    item = {"item_id": "S1", "status": "sold", "listing": {"price": 50},
            "receipt": {"reduced_price": 10.0},
            "ebay": {"quantity": 0, **orders.sale_db_fields(rec)}}
    p = profit.project(item)
    assert p["actual"] and p["ad_fee"] == 2.77
    assert abs(p["net"] - (rec["net_proceeds"] - 10.0)) < CENT, p
