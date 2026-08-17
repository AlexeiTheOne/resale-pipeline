"""What an item makes, pinned down.

These are the highest-value tests in the repo because they cover the only thing
the bot does that can silently lose money: the arithmetic. Every case below is a
real defect that shipped, kept as a regression so it can't come back.

Run with `python tests/test_money.py` (no pytest needed) or `pytest tests/`.

NOTE: importing report.py opens data/ross.db as an import side effect (db.py
calls create_tables() at module scope). Nothing here reads or writes rows — the
helpers under test are pure — but that side effect is why these tests can't run
against a clean checkout without a data/ directory.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import profit
import report
from config import (
    EBAY_AD_FEE_PCT, EBAY_FIXED_FEE, EBAY_FVF_PCT, EBAY_SHIP_CHARGED,
    EBAY_SHIP_COST,
)

CENT = 0.005


def _sold_two_units() -> dict:
    """A qty-2 listing that sold both units across two orders at $36.48 each.

    ebay/orders.py's merge() sums money AND units, so sale_price is the
    two-unit total of $72.96 while units_sold is 2 — the shape the report used
    to multiply together."""
    return {
        "item_id": "aaaaaaaa-0000-0000-0000-000000000000",
        "status": "sold",
        "listing": {"price": 39.99, "title": "test"},
        "receipt": {"reduced_price": 12.00},
        "ebay": {
            "sale_price": 72.96,
            "units_sold": 2,
            "quantity": 0,           # sold out
            "fees_known": True,
            "shipping_collected": 21.72,
            "ebay_fees": 11.31,
            "shipping_label_cost": 20.18,
        },
    }


def _partially_sold() -> dict:
    """A qty-5 listing that sold 2 units and is still live with 3.

    /sync flips this back to 'published' while deliberately keeping the sale
    fields, so a row can carry realized money AND live stock at once."""
    return {
        "item_id": "bbbbbbbb-0000-0000-0000-000000000000",
        "status": "published",
        "listing": {"price": 59.99, "title": "test"},
        "receipt": {"reduced_price": 12.00},
        "ebay": {
            "sale_price": 119.98,
            "units_sold": 2,
            "quantity": 3,           # still for sale
            "fees_known": True,
            "shipping_collected": 21.72,
            "ebay_fees": 18.60,
            "shipping_label_cost": 20.18,
        },
    }


def test_report_does_not_multiply_a_realized_total_by_its_own_unit_count():
    """Column G is `=F*E`, so F has to be a UNIT price on a sold row.

    Feeding it the line total reported this two-unit sale at $145.92 against
    $72.96 actually taken, and carried that into the ad fee, the net, and the
    'SOLD (realized)' band the whole business is judged by."""
    item = _sold_two_units()
    qty = report._report_qty(item)
    unit = report._unit_price_for(item, qty)

    assert qty == 2, qty
    assert abs(unit - 36.48) < CENT, unit
    assert abs(unit * qty - 72.96) < CENT, "revenue must reconstruct the money taken"


def test_report_and_profit_agree_on_the_same_sale():
    """profit.py's docstring says the two must not drift. Pin it.

    They disagreed by a whole unit's Ross cost: report charged paid x units,
    profit charged paid x 1."""
    item = _sold_two_units()
    qty = report._report_qty(item)
    unit = report._unit_price_for(item, qty)
    e = item["ebay"]

    revenue = unit * qty
    ad_fee = EBAY_AD_FEE_PCT * revenue
    cost = 12.00 * qty
    report_net = (revenue + e["shipping_collected"] - e["ebay_fees"]
                  - ad_fee - e["shipping_label_cost"] - cost)

    p = profit.project(item)
    assert abs(report_net - p["net"]) < CENT, (report_net, p["net"])
    assert abs(p["net"] - 35.54) < CENT, p["net"]
    assert p["cost"] == 24.00, "two units sold means two units of Ross cost"
    assert p["actual"] is True


def test_partially_sold_listing_is_projected_not_realized():
    """A listing with stock left is priced at its LIST price over the REMAINING
    units. Pricing it at the realized total x remaining stock reported $270.88
    of net on a listing that had taken $119.98."""
    p = profit.project(_partially_sold())

    assert p["actual"] is False, "stock remains, so nothing here is realized"
    assert abs(p["price"] - 59.99) < CENT, p["price"]
    assert p["qty"] == 3, p["qty"]
    assert abs(p["revenue"] - 179.97) < CENT, p["revenue"]
    assert p["net"] < 120, f"net {p['net']} is larger than the sale ever was"


def test_an_explicit_price_never_borrows_a_past_orders_fees():
    """/offers asks `project(item, price=offer_price)` before sending a discount
    it cannot recall. Answering with the flat fees eBay billed on an earlier,
    smaller order overstates net and clears the OFFER_MIN_NET floor on listings
    that should be skipped."""
    item = _sold_two_units()          # carries realized fees + fees_known
    p = profit.project(item, price=30.00)
    n = p["qty"]

    assert p["actual"] is False, "a supplied price means 'what would this make'"
    # Every fee must scale with the quantity being quoted. The realized figures
    # are flat per-order amounts ($11.31 of fees, one $20.18 label), so a fee
    # that fails to scale is the tell that they leaked in.
    expected_fee = EBAY_FVF_PCT * (30.00 * n + EBAY_SHIP_CHARGED * n) + EBAY_FIXED_FEE * n
    assert abs(p["ebay_fee"] - expected_fee) < CENT, (p["ebay_fee"], expected_fee)
    assert abs(p["ship_cost"] - EBAY_SHIP_COST * n) < CENT, p["ship_cost"]
    assert abs(p["ship_charged"] - EBAY_SHIP_CHARGED * n) < CENT, p["ship_charged"]
    # The scaling checks above are the real discriminator; this is just the
    # headline number. (Don't add one for ship_cost — EBAY_SHIP_COST * 2 is
    # coincidentally equal to this fixture's realized label cost, so it would
    # pass whether or not the realized value leaked.)
    assert abs(p["ebay_fee"] - item["ebay"]["ebay_fees"]) > CENT


def test_breakeven_price_round_trips_to_zero_net():
    """The floor that stops /offers and the repricer selling at a loss. If this
    drifts from project()'s formula, every floor in the system is wrong."""
    item = {
        "item_id": "cccccccc-0000-0000-0000-000000000000",
        "status": "published",
        "listing": {"price": 49.99},
        "receipt": {"reduced_price": 22.00},
        "ebay": {"quantity": 1},
    }
    be = profit.breakeven_price(item)
    assert be is not None

    p = profit.project(item, price=be, qty=1)
    assert abs(p["net"]) < 0.05, f"net at break-even should be ~0, got {p['net']}"

    # And with a target margin, net should land on that target.
    be15 = profit.breakeven_price(item, min_margin=15.0)
    p15 = profit.project(item, price=be15, qty=1)
    assert abs(p15["net"] - 15.0) < 0.05, p15["net"]


def test_breakeven_is_none_without_a_receipt():
    """A floor computed off an ESTIMATED cost is a guess dressed up as a limit,
    and callers use it to refuse loss-making cuts."""
    assert profit.breakeven_price({"listing": {"price": 49.99}, "ebay": {}}) is None


def test_missing_cost_is_flagged_rather_than_counted_as_zero():
    """No receipt and no list price used to mean cost=$0, which reports the
    entire sale as profit."""
    item = {
        "item_id": "dddddddd-0000-0000-0000-000000000000",
        "status": "sold",
        "listing": {},
        "ebay": {"sale_price": 80.00, "units_sold": 1, "quantity": 0},
    }
    p = profit.project(item)
    assert p["cost_known"] is False
    assert "upper bound" in profit.format_projection(p)


def test_an_ended_listing_projects_no_inventory():
    """/sync zeroes the quantity on a listing eBay says has ENDED. The report used
    to coerce that 0 back to 1, so ended listings kept projecting a unit of
    revenue and profit they can no longer make — into the same 'LIVE (projected)'
    band that inventory decisions get made on."""
    ended = {
        "item_id": "eeeeeeee-0000-0000-0000-000000000000",
        "status": "published",
        "listing": {"price": 49.99},
        "receipt": {"reduced_price": 12.00},
        "ebay": {"quantity": 0},
    }
    assert report._report_qty(ended) == 0

    # An absent or unreadable quantity is a different thing from a recorded zero
    # and still defaults to one unit.
    assert report._report_qty({**ended, "ebay": {}}) == 1
    assert report._report_qty({**ended, "ebay": {"quantity": None}}) == 1


def test_refunds_and_fee_credits_come_off_the_ledger():
    """A refunded order used to stay booked at full value forever: REFUND
    transactions were dropped by an if/elif that only knew SALE and
    SHIPPING_LABEL, and nothing anywhere else reduces a recorded sale."""
    from ebay.orders import _fees_by_order

    ledger = _fees_by_order([
        {"orderId": "O1", "transactionType": "SALE",
         "totalFeeAmount": {"value": "15.50", "currency": "USD"}},
        {"orderId": "O1", "transactionType": "SHIPPING_LABEL",
         "amount": {"value": "10.09", "currency": "USD"}},
        {"orderId": "O1", "transactionType": "REFUND",
         "amount": {"value": "-119.99", "currency": "USD"}},
        {"orderId": "O1", "transactionType": "CREDIT",
         "totalFeeAmount": {"value": "-15.50", "currency": "USD"}},
    ])

    o1 = ledger["O1"]
    assert abs(o1["refunded"] - 119.99) < CENT, o1
    assert abs(o1["label_cost"] - 10.09) < CENT, o1
    # The fee was credited back, so eBay kept nothing on this order.
    assert abs(o1["fees"]) < CENT, o1


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"  FAIL  {t.__name__}\n          {e}")
        except Exception as e:
            failed += 1
            print(f" ERROR  {t.__name__}\n          {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
