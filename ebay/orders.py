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
    total per order (`totalFeeAmount` on the SALE transaction) plus any
    eBay-bought shipping-label charges. Needs the `sell.finances` scope. NOTE:
    the Finances API lives on a DIFFERENT host — apiz.ebay.com, not api.ebay.com.

Degrades gracefully: if the finances scope isn't granted, sales_by_item still
returns the gross sale price and shipping from getOrders, just with fees unknown
(net == gross) — /profit then shows a gross number and hints to re-consent.

NOTE: like ebay/analytics.py, the field names below follow eBay's published
schema and haven't been confirmed against a live response from this account yet
(getOrders' pricingSummary/lineItem paths, the Finances transactionType values
and totalFeeAmount). Run /health (checks both scopes) and /sync, then eyeball the
first real order — if a number looks off, log the raw JSON and fix the mapping
here, the same way pipeline/price.py's Apify mapping was pinned down.
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


def _fees_by_order(transactions: list[dict]) -> dict[str, dict]:
    """Collapse the finance ledger to per-order money eBay took: {orderId:
    {"fees": <sum of SALE totalFeeAmount>, "label_cost": <sum of eBay-bought
    shipping labels>}}. Both are seller costs, subtracted from the net."""
    by_order: dict[str, dict] = {}
    for t in transactions:
        order_id = t.get("orderId")
        if not order_id:
            continue
        ttype = t.get("transactionType")
        bucket = by_order.setdefault(
            order_id, {"fees": 0.0, "label_cost": 0.0, "refunded": 0.0})
        if ttype == "SALE":
            bucket["fees"] += _money(t.get("totalFeeAmount"))
        elif ttype == "SHIPPING_LABEL":
            # A label bought through eBay is a debit against the seller; its amount
            # is the postage cost we can net out exactly. Labels bought elsewhere
            # never appear here, so postage is simply 0 for those (handled offline).
            bucket["label_cost"] += _money(t.get("amount"))
        elif ttype == "REFUND":
            # Money going back out. getOrders still returns a refunded order, so
            # without this the sale stays booked at full value forever — there is
            # no other path in the tree that reduces it, and no command that can.
            # eBay also credits the fee back; that arrives as its own
            # CREDIT/NON_SALE_CHARGE transaction, so only the principal is
            # tracked here and the fee credit is picked up below.
            bucket["refunded"] += abs(_money(t.get("amount")))
        elif ttype in ("CREDIT", "NON_SALE_CHARGE"):
            # A fee credit reduces what eBay kept. Negative amounts are already
            # signed as credits by the API, so subtract the magnitude.
            bucket["fees"] -= abs(_money(t.get("totalFeeAmount")))
    return {oid: v for oid, v in by_order.items()
            if v["fees"] or v["label_cost"] or v["refunded"]}


def sales_by_item(days: int = 90) -> dict[str, dict]:
    """Map each sold item to its real economics, keyed by BOTH the line item's
    `sku` (== our item_id) and its `legacyItemId` (== our stored listing_id), so a
    caller can look up by whichever it holds. Each record:

        sale_price          item price actually paid (excl. shipping & tax)
        shipping_collected  what the buyer paid for shipping
        ebay_fees           eBay's cut for this line (order fee, apportioned)
        shipping_label_cost eBay-bought postage for this line (0 if bought elsewhere)
        net_proceeds        sale_price + shipping_collected − ebay_fees − label_cost
        units               how many units these sales covered
        order_ids           every order behind the figures, oldest first
        order_id, sold_at   the most recent one, for provenance
        fees_known          False if the finances scope was missing (net == gross)

    Fees and labels are order-level; when an order bundles several of our items
    they're apportioned by each line's share of the order subtotal (a no-op for
    the usual single-item order, share == 1).

    The money figures ACCUMULATE across every order for that key. A key is not one
    sale: a multi-quantity listing sells a unit at a time, and a relist can sell
    again under the same sku. This used to keep only the last order it happened to
    see, so every earlier sale of the same item silently vanished — and since a
    partially-sold listing stays live to sell the rest, that's the normal case for
    multi-quantity stock, not an edge case."""
    orders = get_orders(days)

    # Finances is a separate scope; if it isn't granted, don't fail the whole
    # sync/report — fall back to gross numbers with fees flagged unknown.
    try:
        fees_by_order = _fees_by_order(get_transactions(days))
        fees_known = True
    except RuntimeError:
        fees_by_order, fees_known = {}, False

    def merge(existing: dict | None, record: dict) -> dict:
        """Fold one line item into whatever this key already has. Money adds up;
        provenance keeps the newest sale but remembers every order id."""
        if existing is None:
            return dict(record)
        for field in ("sale_price", "shipping_collected", "ebay_fees",
                      "shipping_label_cost", "net_proceeds"):
            existing[field] = round(existing[field] + record[field], 2)
        existing["units"] += record["units"]
        for oid in record["order_ids"]:
            if oid not in existing["order_ids"]:
                existing["order_ids"].append(oid)
        if (record["sold_at"] or "") > (existing["sold_at"] or ""):
            existing["sold_at"] = record["sold_at"]
            existing["order_id"] = record["order_id"]
        existing["fees_known"] = existing["fees_known"] and record["fees_known"]
        return existing

    out: dict[str, dict] = {}
    for order in orders:
        order_id = order.get("orderId")

        # A cancelled order is not a sale. getOrders returns it under a
        # creationdate-only filter like any other, so counting it books revenue
        # that never arrived — and nothing downstream ever un-books it.
        cancel_state = ((order.get("cancelStatus") or {}).get("cancelState") or "").upper()
        if cancel_state in ("CANCELED", "CANCELLED", "CANCEL_CLOSED"):
            continue

        summary = order.get("pricingSummary") or {}
        order_subtotal = _money(summary.get("priceSubtotal"))
        order_ship = _money(summary.get("deliveryCost"))
        order_costs = fees_by_order.get(
            order_id, {"fees": 0.0, "label_cost": 0.0, "refunded": 0.0})
        sold_at = order.get("creationDate")

        for li in order.get("lineItems") or []:
            sku = li.get("sku")
            listing_id = li.get("legacyItemId")
            # lineItemCost, NOT total: eBay's `total` is the item cost PLUS
            # shipping ($119.99 + $14.99 = $134.98 on a real order here). Reading
            # `total` as the sale price and then adding shipping_collected on top
            # counted the buyer's postage twice — inflating both the recorded sale
            # price and the net, and (because the share below is measured against a
            # subtotal that excludes shipping) over-apportioning the fees by the
            # same ratio.
            sale_price = _money(li.get("lineItemCost")) or _money(li.get("total"))

            # Apportion order-level eBay costs by this line's share of the order
            # subtotal. Both sides now exclude shipping, so share == 1 for a
            # single-line order instead of 1.125.
            share = (sale_price / order_subtotal) if order_subtotal else 1.0
            # The line carries its own shipping when eBay breaks it out; fall back
            # to apportioning the order-level figure.
            line_ship = _money((li.get("deliveryCost") or {}).get("shippingCost"))
            shipping = line_ship if line_ship else round(order_ship * share, 2)
            fees = round(order_costs["fees"] * share, 2)
            label = round(order_costs["label_cost"] * share, 2)
            # Money refunded to the buyer comes back off this line's revenue.
            # A full refund leaves sale_price at 0 rather than at the amount the
            # buyer briefly paid; the postage and any fee eBay kept stay charged,
            # which is what a return actually costs.
            refunded = round(order_costs.get("refunded", 0.0) * share, 2)
            sale_price = max(round(sale_price - refunded, 2), 0.0)
            net = round(sale_price + shipping - fees - label, 2)

            try:
                units = max(int(li.get("quantity") or 1), 1)
            except (TypeError, ValueError):
                units = 1

            record = {
                "order_id": order_id,
                "order_ids": [order_id] if order_id else [],
                "sold_at": sold_at,
                "sale_price": round(sale_price, 2),
                "shipping_collected": shipping,
                "ebay_fees": fees,
                "shipping_label_cost": label,
                "net_proceeds": net,
                "units": units,
                "listing_id": str(listing_id) if listing_id else None,
                "fees_known": fees_known,
            }
            # Keyed twice, but folded separately: the same record object must not
            # be shared between the sku and listing_id keys, or a later merge
            # would double-count it through the alias.
            if sku:
                out[sku] = merge(out.get(sku), record)
            if listing_id:
                key = str(listing_id)
                out[key] = merge(out.get(key), dict(record, order_ids=list(record["order_ids"])))
    return out


def account_charges(days: int = 90) -> dict:
    """Charges eBay bills to the account rather than to an order.

    Promoted Listings is the big one, and it is invisible to per-item accounting:
    eBay posts it as periodic NON_SALE_CHARGE transactions with NO orderId, so
    there is nothing to attribute a share of to any line item. Over a recent
    120-day window that was $53.17 across 14 charges — real money that the
    per-order maths (_fees_by_order, which only reads SALE and SHIPPING_LABEL)
    counts nowhere.

    Returns {"total": float, "count": int, "by_memo": {memo: {"total", "count"}}}
    so a report can reconcile the assumed ad rate against what was actually
    billed. Raises like the other calls if the finances scope is missing."""
    out = {"total": 0.0, "count": 0, "by_memo": {}}
    for txn in get_transactions(days):
        if txn.get("transactionType") != "NON_SALE_CHARGE":
            continue
        amount = _money(txn.get("amount"))
        memo = (txn.get("transactionMemo") or "other charge").strip()
        out["total"] += amount
        out["count"] += 1
        bucket = out["by_memo"].setdefault(memo, {"total": 0.0, "count": 0})
        bucket["total"] += amount
        bucket["count"] += 1
    out["total"] = round(out["total"], 2)
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
