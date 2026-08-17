"""What one item nets, in Python.

report.py already knows this maths, but it lives there as Excel FORMULAS so the
spreadsheet can recalculate when you edit an assumption — which means the only
way to see what a listing makes used to be to build a report. This module is the
same arithmetic evaluated now, so the bot can quote a number the instant a
listing goes live (telegram_bot.py's _publish_and_report) instead of at the end
of the week.

The two must not drift, so both read their rates from config.py, and the
break-even price here is report.py's own profit formula solved for the price at
which net hits a target — the same solve pipeline/reprice.py uses for its
cost floor, which now calls in here rather than keeping a second copy.

Everything is a projection built on measured averages (see the rate comments in
config.py) EXCEPT a sold item that /sync has pulled real figures for: there the
shipping collected, the eBay fees and the postage actually paid are used, and
`actual` comes back True. Ad spend is always the assumption — eBay bills
Promoted Listings as account-level charges with no order id, so no single item
can ever be told what its share was.
"""
from config import (
    ASSUMED_COST_RATIO, EBAY_AD_FEE_PCT, EBAY_FIXED_FEE, EBAY_FVF_PCT,
    EBAY_SHIP_CHARGED, EBAY_SHIP_COST,
)


def _listed_qty(item: dict) -> int:
    """Units this listing holds — what /setqty pushed to eBay, defaulting to 1."""
    try:
        q = int((item.get("ebay") or {}).get("quantity", 1))
    except (TypeError, ValueError):
        return 1
    return q if q > 0 else 1


def unit_cost(item: dict) -> tuple[float | None, bool]:
    """(what one unit cost you, whether that's an estimate).

    The receipt's reduced_price when it was captured. Without one the cost is
    NOT zero — costing a receipt-less item at zero reports its whole sale price
    as profit — so it falls back to a share of the list price (see
    ASSUMED_COST_RATIO), flagged as an estimate so no caller can present a guess
    as a measurement."""
    paid = (item.get("receipt") or {}).get("reduced_price")
    if paid is not None:
        return round(float(paid), 2), False
    price = (item.get("listing") or {}).get("price")
    if price is None:
        return None, True
    return round(float(price) * ASSUMED_COST_RATIO, 2), True


def _units_sold(item: dict) -> int:
    """Units the stored sale_price actually covers. ebay/orders.py accumulates
    money and units together across every order for the sku, so sale_price is a
    line total for this many units — never a per-unit price."""
    try:
        n = int((item.get("ebay") or {}).get("units_sold") or 1)
    except (TypeError, ValueError):
        return 1
    return max(n, 1)


def _remaining_qty(item: dict) -> int:
    """Units still for sale on the listing. Distinguishes a genuine 0 (an ended
    or sold-out listing) from an absent key, which means 'never set' -> 1."""
    ebay = item.get("ebay") or {}
    if ebay.get("quantity") is None:
        return 1
    try:
        return max(int(ebay["quantity"]), 0)
    except (TypeError, ValueError):
        return 1


def is_realized(item: dict) -> bool:
    """Whether this item's money is DONE — a sale exists and no stock is left.

    A listing that sold one of five units is not realized: it carries a
    sale_price AND three live units, and treating it as either one alone is how
    a past order's economics end up priced against current stock."""
    return ((item.get("ebay") or {}).get("sale_price") is not None
            and _remaining_qty(item) <= 0)


def price_for(item: dict) -> float | None:
    """What ONE unit of this item fetched, or is listed at.

    The realized branch divides sale_price by the units it covers, so callers
    can multiply by a quantity without double-counting. It only applies to a
    fully-realized row — while stock remains, the listing's own price is the
    honest per-unit figure, not the price some earlier unit happened to sell
    for."""
    ebay = item.get("ebay") or {}
    if is_realized(item):
        return round(float(ebay["sale_price"]) / _units_sold(item), 2)
    price = (item.get("listing") or {}).get("price")
    return round(float(price), 2) if price is not None else None


def breakeven_price(item: dict, min_margin: float = 0.0) -> float | None:
    """Lowest unit price that still nets `min_margin` dollars, or None when the
    Ross cost is unknown (a floor computed off an ESTIMATED cost would be a guess
    dressed up as a limit, and callers use this to refuse loss-making cuts).

    Solves the net formula for the price P:

        net(P) = P + S_chg - [FVF*(P + S_chg) + fixed] - ad*P - S_cost - paid

    Note FVF applies to the shipping you charge as well as the item, which is why
    shipping doesn't cancel out even when charged == cost."""
    paid = (item.get("receipt") or {}).get("reduced_price")
    if paid is None:
        return None
    # The EFFECTIVE ad cost, not the rate set on the listing: eBay billed 5.0% of
    # sold revenue against a 4% configured rate, and a floor built on the lower
    # number would approve prices that don't actually clear.
    denom = 1 - EBAY_FVF_PCT - (EBAY_AD_FEE_PCT or 0)
    if denom <= 0:
        return None
    numerator = (min_margin + EBAY_FIXED_FEE + float(paid) + EBAY_SHIP_COST
                 - EBAY_SHIP_CHARGED * (1 - EBAY_FVF_PCT))
    return numerator / denom


def project(item: dict, price: float | None = None, qty: int | None = None) -> dict | None:
    """The full money picture for one item, or None if it has no price yet.

    Money keys are LINE TOTALS (per-unit x qty) so a multi-quantity listing shows
    what the whole listing makes; `net_per_unit` is what one of them makes, which
    is the number to judge the item on. `margin` is net over everything the buyer
    pays (item + shipping), and is the same either way since every term scales
    with quantity.

    A row is one of three things, and conflating them is the bug this signature
    is shaped to prevent:

      realized     a sale exists and no stock is left -> real billed figures,
                   priced over the units the money covered (`actual` True)
      partly sold  a sale exists and stock remains    -> projected at the LIVE
                   list price over the REMAINING units; the past order's fees
                   describe units that are already gone
      live         no sale yet                        -> projected

    Passing `price` or `qty` always means "what would this make", so it forces
    the projected path whatever the row's history."""
    unit_price = price if price is not None else price_for(item)
    if unit_price is None:
        return None
    unit_price = round(float(unit_price), 2)

    ebay = item.get("ebay") or {}
    # A caller that supplies a price or a quantity is asking "what WOULD this
    # make", so the realized figures below must not apply — they are absolute
    # amounts eBay billed on one past order, and spreading them over a
    # hypothetical price or a different unit count understates the cost of the
    # thing being asked about. /offers relies on this: it prices a discount it
    # cannot recall once sent.
    hypothetical = price is not None or qty is not None
    if qty is None:
        # Realized rows are measured in the units the money covered; live rows
        # in the units still for sale.
        qty = _units_sold(item) if is_realized(item) else _listed_qty(item)
    qty = max(int(qty), 1)

    revenue = unit_price * qty
    actual = (not hypothetical
              and is_realized(item)
              and bool(ebay.get("fees_known")))
    if actual:
        ship_charged = round(float(ebay.get("shipping_collected") or 0), 2)
        ebay_fee = round(float(ebay.get("ebay_fees") or 0), 2)
        ship_cost = round(float(ebay.get("shipping_label_cost") or 0), 2)
    else:
        ship_charged = EBAY_SHIP_CHARGED * qty
        # FVF applies to item + shipping charged; the fixed fee is per unit sold.
        ebay_fee = EBAY_FVF_PCT * (revenue + ship_charged) + EBAY_FIXED_FEE * qty
        ship_cost = EBAY_SHIP_COST * qty
    ad_fee = EBAY_AD_FEE_PCT * revenue

    cost_per_unit, cost_estimated = unit_cost(item)
    # unit_cost returns None when there is neither a receipt nor a list price to
    # estimate from. Costing that at zero reports the entire sale as profit, so
    # carry the ignorance forward: cost_known=False lets callers refuse to quote
    # a margin they can't stand behind, and format_projection says so out loud.
    cost_known = cost_per_unit is not None
    cost = (cost_per_unit or 0) * qty

    net = revenue + ship_charged - ebay_fee - ad_fee - ship_cost - cost
    buyer_pays = revenue + ship_charged
    return {
        "price": unit_price,
        "qty": qty,
        "revenue": round(revenue, 2),
        "ship_charged": round(ship_charged, 2),
        "ebay_fee": round(ebay_fee, 2),
        "ad_fee": round(ad_fee, 2),
        "ship_cost": round(ship_cost, 2),
        "cost": round(cost, 2),
        "cost_per_unit": cost_per_unit,
        "cost_estimated": cost_estimated,
        "cost_known": cost_known,
        "net": round(net, 2),
        "net_per_unit": round(net / qty, 2),
        "margin": round(net / buyer_pays, 4) if buyer_pays else None,
        # Return on what you spent at Ross — the number that says whether the
        # rack pick was worth making, where margin says whether the price is.
        "roi": round(net / cost, 2) if cost else None,
        "breakeven": breakeven_price(item),
        "actual": actual,
    }


def format_projection(p: dict, *, header: str | None = None) -> str:
    """The projection as a Telegram message: headline first (net and margin are
    what you actually want to know), then the line items that produced it, so a
    number that looks wrong can be traced to the assumption behind it."""
    verb = "Realized" if p["actual"] else "Projected"
    lines = []
    if header is None:
        margin = f" · {p['margin'] * 100:.0f}% margin" if p["margin"] is not None else ""
        header = f"💰 {verb}: ${p['net']:.2f} net{margin}"
    if header:
        lines.append(header)

    qty_note = f" × {p['qty']}" if p["qty"] > 1 else ""
    lines += [
        "",
        f"  Price        ${p['price']:.2f}{qty_note}",
        f"+ Shipping     ${p['ship_charged']:.2f} collected",
        f"− eBay fees    ${p['ebay_fee']:.2f}",
        f"− Ads          ${p['ad_fee']:.2f}",
        f"− Postage      ${p['ship_cost']:.2f}",
        f"− Ross cost    ${p['cost']:.2f}" + ("  ⚠️ estimated" if p["cost_estimated"] else ""),
        f"= NET          ${p['net']:.2f}",
    ]

    tail = []
    if p["margin"] is not None:
        tail.append(f"{p['margin'] * 100:.0f}% margin")
    if p["roi"] is not None:
        tail.append(f"{p['roi']:.1f}× on cost")
    if p["qty"] > 1:
        tail.append(f"${p['net_per_unit']:.2f} per unit")
    if tail:
        lines += ["", "  " + " · ".join(tail)]

    if p["breakeven"] is not None:
        lines.append(f"  Break-even at ${p['breakeven']:.2f} — below that it sells at a loss.")
    if not p.get("cost_known", True):
        lines.append("  ⚠️ No receipt AND no list price, so this counts the Ross "
                     "cost as $0 — the net above is an upper bound, not a figure. "
                     "/receipt to fix it.")
    elif p["cost_estimated"]:
        lines.append(f"  ⚠️ No receipt for this one, so the cost is a guess "
                     f"({ASSUMED_COST_RATIO * 100:.0f}% of list). /receipt to fix it.")
    if not p["actual"]:
        lines.append("  Fees/shipping are measured averages, not this item's actual bill.")
    return "\n".join(lines)
