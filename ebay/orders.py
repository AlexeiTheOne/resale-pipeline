"""Real sold-order economics from eBay: what each item actually sold for, what
eBay took in fees, and the net — the exact numbers behind /profit's real margins
and /sync's auto-sold detection (replacing the old "flag it, go run /sold by
hand" guess and the fall-back-to-listed-price estimate).

Two APIs, because neither alone has the whole picture:
  - Sell Fulfillment API (`getOrders`) — the orders themselves: sale price,
    buyer-paid shipping, tax, and the item mapping. Each line item carries the
    `sku` we set to our item_id (ebay/inventory.py: `sku = item_id`) and
    `legacyItemId` = the listing id we store on publish, so an order maps straight
    back to a DB row. Needs the `sell.fulfillment.readonly` scope.
  - Sell Finances API (`getTransactions`) — the money ledger: the exact eBay fee
    total per order (`totalFeeAmount` on the SALE transaction), eBay-bought
    shipping labels, Promoted Listings ad fees (NON_SALE_CHARGE, tied to the
    order through references[ORDER_ID]) and refunds. Needs the `sell.finances`
    scope. NOTE:
    the Finances API lives on a DIFFERENT host — apiz.ebay.com, not api.ebay.com.

Degrades gracefully: if the finances scope isn't granted, sales_by_item still
returns the gross sale price and shipping from getOrders, just with fees unknown
(net == gross) — /profit then shows a gross number and hints to re-consent.

Checked 2026-10-29 against this account's live data (86 orders, 266
transactions): the per-order net from sales_by_item matches an independent cash
ledger built from the same two APIs to the cent ($5,303.76).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from datetime import datetime, timedelta, timezone

import httpx

from ebay.auth import get_access_token

# getOrders is on the standard host; getTransactions is on eBay's separate
# financials host. Getting this wrong 404s, so keep them distinct.
FULFILLMENT_BASE = "https://api.ebay.com"
FINANCES_BASE = "https://apiz.ebay.com"

_PAGE = 200  # getOrders max page size (Finances allows more, but this is safe for both)


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {get_access_token()}",
        "Accept": "application/json",
    }


def _iso(dt: datetime) -> str:
    """eBay's date-range filters want UTC as yyyy-MM-ddTHH:mm:ss.SSSZ."""
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _money(obj) -> float:
    """Pull a float out of eBay's {"value": "12.34", "currency": "USD"} amount
    shape. Missing/None/unparseable → 0.0, so callers can sum without guarding."""
    if not isinstance(obj, dict):
        return 0.0
    try:
        return float(obj.get("value") or 0)
    except (TypeError, ValueError):
        return 0.0


def get_orders(days: int = 90) -> list[dict]:
    """Every order created in the trailing `days`, following pagination. Raises on
    a non-2xx (a missing sell.fulfillment.readonly scope shows up as 403 here)."""
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    filter_str = f"creationdate:[{_iso(start)}..{_iso(end)}]"

    orders, offset = [], 0
    while True:
        params = {"filter": filter_str, "limit": _PAGE, "offset": offset}
        r = httpx.get(f"{FULFILLMENT_BASE}/sell/fulfillment/v1/order",
                      headers=_headers(), params=params, timeout=30)
        if r.status_code >= 400:
            raise RuntimeError(f"eBay Fulfillment getOrders failed [{r.status_code}]: {r.text}")
        body = r.json()
        batch = body.get("orders") or []
        orders.extend(batch)
        total = body.get("total") or 0
        offset += _PAGE
        if len(batch) < _PAGE or offset >= total:
            break
    return orders


def get_transactions(days: int = 90) -> list[dict]:
    """Every finance transaction in the trailing `days` (SALE, REFUND,
    SHIPPING_LABEL, …), following pagination. Raises on a non-2xx (a missing
    sell.finances scope shows up as 403 here). Note the apiz.ebay.com host."""
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    filter_str = f"transactionDate:[{_iso(start)}..{_iso(end)}]"

    txns, offset = [], 0
    while True:
        params = {"filter": filter_str, "limit": _PAGE, "offset": offset}
        r = httpx.get(f"{FINANCES_BASE}/sell/finances/v1/transaction",
                      headers=_headers(), params=params, timeout=30)
        if r.status_code >= 400:
            raise RuntimeError(f"eBay Finances getTransactions failed [{r.status_code}]: {r.text}")
        body = r.json()
        batch = body.get("transactions") or []
        txns.extend(batch)
        total = body.get("total") or 0
        offset += _PAGE
        if len(batch) < _PAGE or offset >= total:
            break
    return txns


def _order_ref(t: dict) -> str | None:
    """The order a finance transaction belongs to. SALE / REFUND / SHIPPING_LABEL
    carry a top-level `orderId`; a Promoted Listings fee (NON_SALE_CHARGE) does
    NOT, but it lists the order under references[referenceType == ORDER_ID]."""
    if t.get("orderId"):
        return t["orderId"]
    for ref in t.get("references") or []:
        if (ref or {}).get("referenceType") == "ORDER_ID" and ref.get("referenceId"):
            return ref["referenceId"]
    return None


def _signed_cost(t: dict) -> float:
    """A cost-type amount, positive when it's money out (DEBIT) and negative when
    eBay gives it back (CREDIT, e.g. a voided label or a reversed ad fee). eBay
    sends the magnitude in `amount` and the direction in `bookingEntry`; an
    absent bookingEntry is treated as the usual debit."""
    amount = abs(_money(t.get("amount")))
    return -amount if (t.get("bookingEntry") or "").upper() == "CREDIT" else amount


def _is_ad_fee(t: dict) -> bool:
    return ((t.get("feeType") or "").upper() == "AD_FEE"
            or "promoted" in (t.get("transactionMemo") or "").lower())


def _fees_by_order(transactions: list[dict]) -> dict[str, dict]:
    """Collapse the finance ledger to per-order money: {orderId: {
        "fees":       final value fee kept by eBay (SALE totalFeeAmount minus any
                      fee eBay returned on a refund, plus other order-level charges),
        "label_cost": eBay-bought labels, incl. return labels and underpaid-postage
                      surcharges (net of voids),
        "ad_fees":    Promoted Listings fees charged on this order (net of credits),
        "refunded":   GROSS money returned to the buyer,
    }}.

    How eBay books a refund (checked against this account's three returns): a
    REFUND debit whose `amount` is the refund NET of the fee eBay hands back, with
    that fee credit in `totalFeeAmount` ($83.75 + $16.23 = the $99.98 the buyer
    paid). So gross refund = amount + totalFeeAmount, and the fee credit comes off
    `fees`. The DISPUTE debit and the matching CREDIT that accompany a return are
    a hold and its release; they cancel and are ignored here. TRANSFER is the
    owner's own top-up and has no order."""
    by_order: dict[str, dict] = {}
    for t in transactions:
        order_id = _order_ref(t)
        if not order_id:
            continue
        ttype = t.get("transactionType")
        bucket = by_order.setdefault(
            order_id, {"fees": 0.0, "label_cost": 0.0, "ad_fees": 0.0, "refunded": 0.0})
        if ttype == "SALE":
            bucket["fees"] += _money(t.get("totalFeeAmount"))
        elif ttype == "SHIPPING_LABEL":
            # Labels bought elsewhere never appear here, so postage is 0 for those.
            bucket["label_cost"] += _signed_cost(t)
        elif ttype == "REFUND":
            fee_back = abs(_money(t.get("totalFeeAmount")))
            bucket["refunded"] += abs(_money(t.get("amount"))) + fee_back
            bucket["fees"] -= fee_back
        elif ttype == "NON_SALE_CHARGE":
            if _is_ad_fee(t):
                bucket["ad_fees"] += _signed_cost(t)
            else:
                bucket["fees"] += _signed_cost(t)
        elif ttype == "CREDIT":
            # A dispute-hold release carries no fee; a standalone fee credit does.
            bucket["fees"] -= abs(_money(t.get("totalFeeAmount")))
    return {oid: {k: round(v, 2) for k, v in b.items()}
            for oid, b in by_order.items() if any(b.values())}


def totals_from_sales(sales: list[dict]) -> dict:
    """Fold per-order sale records (see sales_by_item) into the item-level totals
    every reader uses. Pure, so /sync can fold a stored history with a fresh
    window's records and get the same numbers sales_by_item would.

        sale_price          item money KEPT: gross item price minus refunds
                            (floored at 0 per order)
        gross_sale_price    item price before refunds
        shipping_collected  shipping kept (a refund larger than the item price
                            comes off shipping next)
        ebay_fees           FVF + other order charges, net of fee credits
        shipping_label_cost eBay-bought postage
        ad_fees             Promoted Listings fees attributed to these orders
        refunded            gross refunds
        net_proceeds        sum of each order's net (sale + ship - refund - fees
                            - labels - ads)
        units               units ordered (refunded ones included: they left the
                            shelf, and whether they came back is not in eBay data)
        order_ids           oldest first; order_id / sold_at = the newest,
                            first_sold_at = the oldest
        sales               the per-order records, oldest first. Use these, not
                            sold_at, to put money in the month it was earned."""
    recs = sorted((dict(r) for r in sales),
                  key=lambda r: (r.get("sold_at") or "", r.get("order_id") or ""))
    tot = {k: 0.0 for k in ("sale_price", "gross_sale_price", "shipping_collected", "ebay_fees",
                            "shipping_label_cost", "ad_fees", "refunded", "net_proceeds")}
    units = 0
    for r in recs:
        refund = r.get("refund") or 0.0
        tot["sale_price"] += max(r["sale"] - refund, 0.0)
        tot["gross_sale_price"] += r["sale"]
        tot["shipping_collected"] += max(r["shipping"] - max(refund - r["sale"], 0.0), 0.0)
        tot["ebay_fees"] += r["fees"]
        tot["shipping_label_cost"] += r["label"]
        tot["ad_fees"] += r.get("ad_fee") or 0.0
        tot["refunded"] += refund
        tot["net_proceeds"] += r["net"]
        units += r.get("units") or 1
    out = {k: round(v, 2) for k, v in tot.items()}
    newest = recs[-1] if recs else {}
    out.update({
        "units": units,
        "order_ids": [r["order_id"] for r in recs if r.get("order_id")],
        "order_id": newest.get("order_id"),
        "sold_at": newest.get("sold_at"),
        "first_sold_at": recs[0].get("sold_at") if recs else None,
        "listing_id": newest.get("listing_id"),
        "fees_known": all(r.get("fees_known", True) for r in recs),
        "sales": recs,
    })
    return out


def _order_records(order: dict, ledger: dict, fees_known: bool) -> list[tuple[str | None, dict]]:
    """[(sku, per-order record)] for each line item of one getOrders order."""
    order_id = order.get("orderId")
    summary = order.get("pricingSummary") or {}
    order_subtotal = _money(summary.get("priceSubtotal"))
    # deliveryDiscount is a negative amount (a combined-shipping discount, -$7 on
    # two orders here); what the buyer actually paid for shipping is cost + it.
    order_ship = _money(summary.get("deliveryCost")) - abs(_money(summary.get("deliveryDiscount")))
    costs = ledger.get(order_id) or {}
    lines = order.get("lineItems") or []
    out = []
    for li in lines:
        # lineItemCost, NOT total: `total` is item cost PLUS shipping, which used
        # to count the buyer's postage twice.
        sale = _money(li.get("lineItemCost")) or _money(li.get("total"))
        share = (sale / order_subtotal) if (order_subtotal and len(lines) > 1) else 1.0
        shipping = round(max(order_ship, 0.0) * share, 2)
        fees = round(costs.get("fees", 0.0) * share, 2)
        label = round(costs.get("label_cost", 0.0) * share, 2)
        ad_fee = round(costs.get("ad_fees", 0.0) * share, 2)
        refund = round(costs.get("refunded", 0.0) * share, 2)
        try:
            units = max(int(li.get("quantity") or 1), 1)
        except (TypeError, ValueError):
            units = 1
        listing_id = li.get("legacyItemId")
        out.append((li.get("sku"), {
            "order_id": order_id,
            "sold_at": order.get("creationDate"),
            "units": units,
            "sale": round(sale, 2),
            "shipping": shipping,
            "fees": fees,
            "label": label,
            "ad_fee": ad_fee,
            "refund": refund,
            "net": round(sale + shipping - refund - fees - label - ad_fee, 2),
            "listing_id": str(listing_id) if listing_id else None,
            "fees_known": fees_known,
        }))
    return out


def sales_by_item(days: int = 90, *, orders: list[dict] | None = None,
                  transactions: list[dict] | None = None) -> dict[str, dict]:
    """Map each sold item to its real economics, keyed by BOTH the line item's
    `sku` (== our item_id) and its `legacyItemId` (== our stored listing_id).
    Each value is totals_from_sales() over that key's orders: the item totals
    plus `sales`, one record per order:

        {order_id, sold_at, units, sale, shipping, fees, label, ad_fee, refund,
         net, listing_id, fees_known}

    A key is not one sale: a multi-quantity listing sells a unit at a time and a
    relist sells again under the same sku, so money accumulates across orders.
    Order-level costs are split across line items by subtotal share (a no-op for
    the usual single-line order).

    `orders` / `transactions` may be passed in (already fetched) instead of
    calling eBay. Degrades if the finances scope is missing: gross figures,
    fees_known False."""
    if orders is None:
        orders = get_orders(days)
    try:
        ledger = _fees_by_order(transactions if transactions is not None
                                else get_transactions(days))
        fees_known = True
    except RuntimeError:
        ledger, fees_known = {}, False

    by_key: dict[str, list[dict]] = {}
    for order in orders:
        # A cancelled order is not a sale.
        cancel_state = ((order.get("cancelStatus") or {}).get("cancelState") or "").upper()
        if cancel_state in ("CANCELED", "CANCELLED", "CANCEL_CLOSED"):
            continue
        for sku, rec in _order_records(order, ledger, fees_known):
            # Separate copies per alias, so nothing is shared between the keys.
            if sku:
                by_key.setdefault(sku, []).append(dict(rec))
            if rec["listing_id"]:
                by_key.setdefault(rec["listing_id"], []).append(dict(rec))
    return {key: totals_from_sales(recs) for key, recs in by_key.items()}


def account_charges(days: int = 90) -> dict:
    """Charges eBay billed as NON_SALE_CHARGE (Promoted Listings, mostly).

    Each Promoted Listings fee DOES name its order (references[ORDER_ID]), and
    sales_by_item books those per order as `ad_fees`. This total is kept for the
    report's reconciliation: `attributed` is the part tied to an order,
    `unattributed` the rest (true account-level charges). Credits net off.

    Returns {"total", "count", "attributed", "unattributed",
             "by_memo": {memo: {"total", "count"}}}. Raises like the other calls
    if the finances scope is missing."""
    out = {"total": 0.0, "count": 0, "attributed": 0.0, "unattributed": 0.0, "by_memo": {}}
    for txn in get_transactions(days):
        if txn.get("transactionType") != "NON_SALE_CHARGE":
            continue
        amount = _signed_cost(txn)
        memo = (txn.get("transactionMemo") or "other charge").strip()
        out["total"] += amount
        out["count"] += 1
        out["attributed" if _order_ref(txn) else "unattributed"] += amount
        bucket = out["by_memo"].setdefault(memo, {"total": 0.0, "count": 0})
        bucket["total"] += amount
        bucket["count"] += 1
    for k in ("total", "attributed", "unattributed"):
        out[k] = round(out[k], 2)
    for bucket in out["by_memo"].values():
        bucket["total"] = round(bucket["total"], 2)
    return out


def orders_status() -> str:
    """Non-destructive /health check: a limit=1 read returns 2xx when
    sell.fulfillment.readonly is granted and 403 when it isn't."""
    r = httpx.get(f"{FULFILLMENT_BASE}/sell/fulfillment/v1/order",
                  headers=_headers(), params={"limit": 1}, timeout=30)
    if r.status_code == 403:
        raise RuntimeError(f"403 (scope missing?): {r.text[:200]}")
    if r.status_code >= 400:
        raise RuntimeError(f"eBay error [{r.status_code}]: {r.text[:200]}")
    return "OK"


def finances_status() -> str:
    """Non-destructive /health check for the sell.finances scope (apiz host)."""
    r = httpx.get(f"{FINANCES_BASE}/sell/finances/v1/transaction",
                  headers=_headers(), params={"limit": 1}, timeout=30)
    if r.status_code == 403:
        raise RuntimeError(f"403 (scope missing?): {r.text[:200]}")
    if r.status_code >= 400:
        raise RuntimeError(f"eBay error [{r.status_code}]: {r.text[:200]}")
    return "OK"


# The item-row keys a recorded sale owns, as /sync writes them into items.ebay.
# `units` is stored as `units_sold`; everything else keeps its name.
SALE_DB_FIELDS = ("sale_price", "gross_sale_price", "shipping_collected", "ebay_fees",
                  "shipping_label_cost", "ad_fees", "refunded", "net_proceeds",
                  "units_sold", "order_id", "order_ids", "sold_at", "first_sold_at",
                  "fees_known", "sales")
# The money-bearing subset, for "did the figures change" comparisons.
SALE_MONEY_FIELDS = ("sale_price", "shipping_collected", "ebay_fees", "shipping_label_cost",
                     "ad_fees", "refunded", "net_proceeds", "units_sold")


def sale_db_fields(totals: dict) -> dict:
    """totals_from_sales() output as the items.ebay keys to write. Keys the totals
    don't carry are left out rather than written as None, and `sales` is only
    written when there are per-order records: an empty list would claim the
    row's history is known to be empty."""
    out = {k: totals[k] for k in SALE_DB_FIELDS
           if k not in ("units_sold", "sales") and totals.get(k) is not None}
    out["units_sold"] = totals.get("units") or 1
    out["order_ids"] = list(totals.get("order_ids") or [])
    if totals.get("sales") is not None:
        out["sales"] = [dict(r) for r in totals["sales"]]
    return out


def fold_sales(stored: list[dict] | None, incoming: list[dict]) -> list[dict]:
    """Stored per-order records with a fresh window's records laid over them, by
    order id. The window wins for any order it can see (a refund or a late ad fee
    changes that order's figures); orders that have aged out of the window keep
    their stored record instead of being erased."""
    merged = {r["order_id"]: dict(r) for r in stored or [] if r.get("order_id")}
    for r in incoming:
        if r.get("order_id"):
            merged[r["order_id"]] = dict(r)
    return list(merged.values())
