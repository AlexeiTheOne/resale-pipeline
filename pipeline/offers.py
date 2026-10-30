"""Offer planning: which live listings get a private offer to their watchers.

Moved verbatim out of telegram_bot.py (docs/cleanup-plan.md Phase 2). Read-only;
sending is the caller's job. Nothing here imports telegram_bot or bot.*.
"""
from datetime import datetime, timezone

import db
import profit
from config import (
    OFFER_DISCOUNT_LARGE, OFFER_DISCOUNT_SMALL, OFFER_LARGE_THRESHOLD,
    OFFER_MIN_DAYS_LIVE, OFFER_MIN_NET, OFFER_MIN_WATCHERS,
)
from ebay import analytics, negotiation
from ebay.negotiation import MIN_DISCOUNT_PCT


def items_with_live_stock() -> list[dict]:
    """Everything that still has units for sale on eBay.

    That is NOT the same set as status == 'published'. A multi-quantity listing
    that sells one unit is marked 'sold' while the rest stays live, and a relist
    keeps the sold status until the next /sync — so filtering on the status alone
    silently drops real, for-sale inventory. That case is the exact scenario
    config.py's OFFER_* block cites as the reason offers exist, and it was the one
    case /offers couldn't see.

    Mirrors pipeline/reprice.py's eligibility rule, which had the same bug."""
    out = []
    for item in db.list_items("published") + db.list_items("sold"):
        if item["status"] == "published":
            out.append(item)
            continue
        try:
            remaining = int((item.get("ebay") or {}).get("quantity") or 0)
        except (TypeError, ValueError):
            remaining = 0
        if remaining > 0:
            out.append(item)
    return out


def offer_plan(discount_override: float | None = None) -> list[dict]:
    """Decide which live listings should get an offer to their watchers.

    The gates, in order:
      1. eBay says the listing is eligible (findEligibleItems) — no offer already
         out, and a format that accepts one.
      2. Somebody is actually watching (OFFER_MIN_WATCHERS), read LIVE from eBay
         rather than from the weekly snapshot, which is up to seven days stale and
         blind to everything listed since it ran.
      3. The listing has had a fair run at full price (OFFER_MIN_DAYS_LIVE).
      4. The discount clears eBay's 5% minimum.
      5. What's left still pays: above break-even, and at least OFFER_MIN_NET a
         unit.

    Returns a row per candidate with a `verdict` explaining any skip, so the dry
    run can show the reasoning rather than an unexplained short list. Read-only —
    sending is the caller's job. Ranked by watchers, because that's the ranking of
    how likely the offer is to land."""
    eligible = negotiation.eligible_listing_ids()
    rows = []
    for item in items_with_live_stock():
        listing_id = str((item.get("ebay") or {}).get("listing_id") or "")
        price = (item.get("listing") or {}).get("price")
        if not listing_id or listing_id not in eligible or price is None:
            continue

        price = round(float(price), 2)
        # Price passed explicitly. profit.project() now treats any supplied price as a
        # question about the future and refuses to answer it with a past order's
        # figures — see profit.project's docstring. Passing it here is still the
        # right call (it names the listing's current price rather than relying on
        # the default), but it is no longer the only thing standing between a
        # partially-sold listing and a discount priced off economics that belong
        # to units already gone.
        projection = profit.project(item, price=price)
        watchers = analytics.get_watch_count(listing_id)
        if watchers is None:   # call failed or eBay omits the field at zero
            watchers = (db.latest_stats(item["item_id"]) or {}).get("watchers") or 0
        row = {
            "item_id": item["item_id"], "listing_id": listing_id, "price": price,
            "margin": projection["margin"] if projection else None,
            "watchers": watchers, "qty": projection["qty"] if projection else 1,
            "title": ((item.get("listing") or {}).get("title") or "")[:38],
            "cost_estimated": projection["cost_estimated"] if projection else True,
        }

        if watchers < OFFER_MIN_WATCHERS:
            row["verdict"] = "nobody watching"
            rows.append(row)
            continue

        # A young listing hasn't been rejected on price — it hasn't finished being
        # considered. Its watchers arrived days ago and are still deciding.
        when = (item.get("ebay") or {}).get("published_at") or item.get("created_at")
        days_live = None
        if when:
            then = datetime.fromisoformat(when)
            if then.tzinfo is None:
                then = then.replace(tzinfo=timezone.utc)
            days_live = (datetime.now(timezone.utc) - then).total_seconds() / 86400
        row["days_live"] = days_live
        if days_live is not None and days_live < OFFER_MIN_DAYS_LIVE:
            row["verdict"] = f"only {days_live:.1f}d old — let it sell at full price first"
            rows.append(row)
            continue

        discount = discount_override if discount_override is not None else (
            OFFER_DISCOUNT_LARGE if price >= OFFER_LARGE_THRESHOLD else OFFER_DISCOUNT_SMALL)
        offer_price = round(price - discount, 2)
        pct = (discount / price) * 100 if price else 0

        # eBay rejects an offer that barely undercuts the listing, and raising the
        # discount to clear that bar would spend more than was authorised — so
        # this is a skip with a reason, not a silent bigger discount.
        if pct < MIN_DISCOUNT_PCT:
            need = price * MIN_DISCOUNT_PCT / 100
            row["verdict"] = f"${discount:.0f} is only {pct:.1f}% — eBay needs 5% (${need:.2f})"
            rows.append(row)
            continue

        floor = profit.breakeven_price(item)
        if floor is not None and offer_price <= floor:
            row["verdict"] = f"${offer_price:.2f} is under the ${floor:.2f} break-even"
            rows.append(row)
            continue

        after = profit.project(item, price=offer_price)
        net_after = after["net_per_unit"] if after else 0
        if net_after < OFFER_MIN_NET:
            row["verdict"] = f"would net only ${net_after:.2f}/unit"
            rows.append(row)
            continue

        row.update({"send": True, "discount": discount, "offer_price": offer_price,
                    "pct": pct, "margin_after": after["margin"] if after else None,
                    "net_after": net_after, "net_total": after["net"] if after else None})
        rows.append(row)
    rows.sort(key=lambda r: (not r.get("send"), -(r.get("watchers") or 0)))
    return rows
