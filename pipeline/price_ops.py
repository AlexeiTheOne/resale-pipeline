"""Setting an item's price: the checks and the write shared by /setprice, the
weekly digest's reprice button and the price-gate correction
(bot/listing_cmds.py, bot/text_router.py).

Moved out of the Telegram handlers with no change in behaviour, so other
callers (the planned dashboard, docs/dashboard-design.md) use the same code.

The order is always eBay first, then the DB: when eBay refuses, the stored
price stays the old one.
"""
from datetime import datetime, timezone

import db
import profit
from ebay import inventory
from pipeline import ship_test, sync

ON_EBAY_STATUSES = ("ebay_draft", "published")


class EbayRefused(Exception):
    """eBay refused the price; nothing was saved. The eBay error is __cause__."""


def manual_pricing(pricing: dict, price: float) -> dict:
    """Record a human price override without erasing what the machine proposed.

    `machine_price` is written once by pipeline/pricing_v2.py and never touched here;
    the override goes to suggested_price (what the rest of the pipeline reads)
    and is also kept as manual_price alongside it. Previously this assignment
    overwrote suggested_price outright, so every correction destroyed the number
    it was correcting — 34 of 57 items ended up with price_basis='manual' and no
    record of how far off the suggestion had been. Keeping both makes that gap
    measurable instead of invisible."""
    pricing = dict(pricing)
    pricing["manual_price"] = price
    pricing["manual_price_at"] = datetime.now(timezone.utc).isoformat()
    if pricing.get("machine_price") is None:
        # Pre-existing items priced before machine_price existed: the current
        # suggestion is the machine's, so capture it before it's replaced.
        pricing["machine_price"] = pricing.get("suggested_price")
    pricing["suggested_price"] = price
    pricing["confidence"] = "manual"
    pricing["price_basis"] = "manual"
    return pricing


def reprice_pricing(pricing: dict, price: float) -> dict:
    """The pricing record after a repricer suggestion is applied."""
    pricing = dict(pricing)
    pricing["suggested_price"] = price
    pricing["confidence"] = "reprice"
    pricing["price_basis"] = "reprice"
    return pricing


def floor_breach(item: dict, price: float) -> float | None:
    """The break-even when `price` is at or below it, else None.

    None too when the cost is unknown: breakeven_price refuses to guess from an
    estimated cost, and a floor nobody can compute must not become a wall."""
    floor = profit.breakeven_price(item)
    if floor is None or price > floor:
        return None
    return floor


def needs_force(item: dict, price: float) -> dict | None:
    """Why setting this price by hand needs `force`, or None if it's fine:
    {"why": "ship_test", "end": "YYYY-MM-DD"} while the item is in the
    free-shipping test, else {"why": "floor", "floor": x} at or below
    break-even."""
    if ship_test.frozen(item):
        return {"why": "ship_test", "end": ship_test.record(item)["end"]}
    floor = floor_breach(item, price)
    if floor is not None:
        return {"why": "floor", "floor": floor}
    return None


def save(item: dict, price: float, pricing: dict) -> None:
    """Store a new price on both the listing and the pricing record."""
    listing = dict(item.get("listing") or {})
    if listing:
        listing["price"] = price
        db.update_field(item["item_id"], "listing", listing)
    db.update_field(item["item_id"], "pricing", pricing)


def set_price(item: dict, price: float, pricing: dict, any_status: bool = False) -> dict:
    """Set the price: on eBay first (with the Best Offer thresholds for the new
    price in the same update), then in the DB. Blocking; run in a thread.

    The eBay push happens when the item has an offer and, unless `any_status`,
    is a draft or live. An item relisted in Seller Hub is saved locally only,
    because its offer can't reach the live listing.

    Returns {"pushed": bool, "detached": bool, "where": "live listing" |
    "eBay draft" | None, "terms": the bestOfferTerms sent, or None}.
    Raises EbayRefused (from the eBay error) with the DB unchanged."""
    offer_id = (item.get("ebay") or {}).get("offer_id")
    on_ebay = bool(offer_id) and (any_status or item["status"] in ON_EBAY_STATUSES)
    if on_ebay and sync.detached(item):
        save(item, price, pricing)
        return {"pushed": False, "detached": True, "where": None, "terms": None}
    terms = None
    if on_ebay:
        try:
            body = inventory.update_offer_price(offer_id, price, item)
        except Exception as e:
            raise EbayRefused(str(e)) from e
        terms = ((body or {}).get("listingPolicies") or {}).get("bestOfferTerms")
    save(item, price, pricing)
    where = None
    if on_ebay:
        where = "live listing" if item["status"] == "published" else "eBay draft"
    return {"pushed": on_ebay, "detached": False, "where": where, "terms": terms}
