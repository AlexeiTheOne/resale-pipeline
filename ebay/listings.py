"""Enumerate the seller's live eBay listings — including ones this bot didn't create.

The Sell Inventory API only knows about offers created THROUGH it, so a listing
you made by hand in Seller Hub is invisible there (and therefore invisible to
/status, /report, /profit and the weekly price check). The legacy Trading API's
GetMyeBaySelling is the only call that returns every active listing regardless of
how it was created, so that's what this uses.

It authenticates with the same OAuth user token as the REST calls, passed as
X-EBAY-API-IAF-TOKEN rather than a Bearer header — Trading's way of accepting an
OAuth token. No separate credential is needed.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import re
import xml.etree.ElementTree as ET

import httpx

from ebay.auth import get_access_token

TRADING_URL = "https://api.ebay.com/ws/api.dll"
_PAGE = 100          # GetMyeBaySelling max entries per page
_MAX_PAGES = 50      # hard stop so a pagination bug can't loop forever
_COMPAT_LEVEL = "967"


def _headers(call_name: str) -> dict:
    return {
        "X-EBAY-API-SITEID": "0",                      # 0 = eBay US
        "X-EBAY-API-COMPATIBILITY-LEVEL": _COMPAT_LEVEL,
        "X-EBAY-API-CALL-NAME": call_name,
        "X-EBAY-API-IAF-TOKEN": get_access_token(),
        "Content-Type": "text/xml",
    }


def _strip_ns(tag: str) -> str:
    return tag.split("}", 1)[-1]


def _text(node, path):
    """Namespace-agnostic child lookup — Trading responses are namespaced and the
    prefix isn't worth threading through every call site."""
    for child in node.iter():
        if _strip_ns(child.tag) == path:
            return (child.text or "").strip()
    return None


def _price(node):
    for child in node.iter():
        if _strip_ns(child.tag) in ("CurrentPrice", "StartPrice", "BuyItNowPrice"):
            try:
                return round(float(child.text), 2)
            except (TypeError, ValueError):
                continue
    return None


def _int(node, tag: str, default: int = 0) -> int:
    try:
        return int(_text(node, tag) or default)
    except (TypeError, ValueError):
        return default


def get_active_listings() -> list[dict]:
    """Every currently-active listing on the account.

    Returns dicts of {listing_id, title, sku, price, quantity, quantity_total,
    sold_quantity, view_item_url}. `sku` is None for listings created outside the
    Inventory API — that's the signal that this bot has never seen the listing
    before.

    `quantity` is what's STILL AVAILABLE, which is not what Trading reports:
    Item.Quantity is the quantity the listing was created with and never moves as
    units sell, so a listing of 3 that sold 2 still says 3. Available is
    Quantity − QuantitySold. Reading it wrong overstates live stock — one listing
    here was recorded as 3 units in hand when it had 1, which the profit report
    then projected three units of profit on."""
    listings, page = [], 1
    while page <= _MAX_PAGES:
        body = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<GetMyeBaySellingRequest xmlns="urn:ebay:apis:eBLBaseComponents">'
            "<ActiveList><Include>true</Include><Pagination>"
            f"<EntriesPerPage>{_PAGE}</EntriesPerPage><PageNumber>{page}</PageNumber>"
            "</Pagination></ActiveList>"
            "</GetMyeBaySellingRequest>"
        )
        r = httpx.post(TRADING_URL, headers=_headers("GetMyeBaySelling"),
                       content=body.encode(), timeout=90)
        if r.status_code != 200:
            raise RuntimeError(f"eBay Trading GetMyeBaySelling failed [{r.status_code}]: {r.text[:300]}")

        root = ET.fromstring(r.text)
        ack = _text(root, "Ack")
        if ack in ("Failure", "PartialFailure"):
            msg = _text(root, "LongMessage") or _text(root, "ShortMessage") or r.text[:200]
            raise RuntimeError(f"eBay Trading GetMyeBaySelling error: {msg}")

        batch = [n for n in root.iter() if _strip_ns(n.tag) == "Item"]
        for node in batch:
            listing_id = _text(node, "ItemID")
            if not listing_id:
                continue
            total = _int(node, "Quantity", 1)
            sold = _int(node, "QuantitySold", 0)
            listings.append({
                "listing_id": listing_id,
                "title": _text(node, "Title") or "",
                "sku": _text(node, "SKU") or None,
                "price": _price(node),
                "quantity": max(total - sold, 0),
                "quantity_total": total,
                "sold_quantity": sold,
                "view_item_url": _text(node, "ViewItemURL")
                                 or f"https://www.ebay.com/itm/{listing_id}",
            })
        if len(batch) < _PAGE:
            break
        page += 1
    return listings


def listing_picture_count(listing_id: str) -> int | None:
    """How many photos a live listing actually shows a buyer, or None if the
    listing couldn't be read.

    This is the only trustworthy answer to "did a listing lose its photos".
    The Sell Inventory API's getInventoryItem is NOT: it returns a lossy
    projection that drops product.description outright and, on a sizeable
    minority of items, reports imageUrls collapsed to a single URL while the
    live listing is still showing every photo. Auditing against that field
    reports damage that isn't there — and, worse, invites a "repair" that
    writes the bad read back through a full-replace PUT.

    OutputSelector keeps the response to the picture list rather than the
    whole item, which matters when this runs across the catalogue."""
    body = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<GetItemRequest xmlns="urn:ebay:apis:eBLBaseComponents">'
        f"<ItemID>{listing_id}</ItemID>"
        "<OutputSelector>Item.PictureDetails.PictureURL</OutputSelector>"
        "</GetItemRequest>"
    )
    try:
        r = httpx.post(TRADING_URL, headers=_headers("GetItem"),
                       content=body.encode(), timeout=30)
    except httpx.HTTPError:
        return None
    if r.status_code != 200:
        return None
    try:
        root = ET.fromstring(r.text)
    except ET.ParseError:
        return None
    if _text(root, "Ack") not in ("Success", "Warning"):
        return None
    return sum(1 for n in root.iter() if _strip_ns(n.tag) == "PictureURL")


def audit_listing_photos(items: list[dict]) -> list[dict]:
    """Compare every live listing's real photo count against what we uploaded.

    Takes item rows (as db.list_items returns them) and reports only the ones
    where the listing shows FEWER photos than the URLs we hold for it, which is
    the shape a photo loss takes. Returns dicts of
    {item_id, listing_id, live, expected, title}, worst shortfall first.

    Run it after anything that rewrites listings in bulk. A listing quietly
    dropping to one photo is invisible in every traffic report — impressions
    keep accruing and the click-through just decays — so nothing else in the
    system would surface it."""
    findings = []
    for item in items:
        ebay_data = item.get("ebay") or {}
        listing_id = ebay_data.get("listing_id")
        expected = len(ebay_data.get("image_urls") or [])
        if not listing_id or not expected:
            continue
        live = listing_picture_count(listing_id)
        if live is None or live >= expected:
            continue
        findings.append({
            "item_id": item["item_id"],
            "listing_id": listing_id,
            "live": live,
            "expected": expected,
            "title": (item.get("listing") or {}).get("title") or "",
        })
    return sorted(findings, key=lambda f: f["live"] - f["expected"])


def listings_status() -> str:
    """One-line health check for /health: can we read the account's listings?"""
    try:
        rows = get_active_listings()
    except Exception as e:
        return f"❌ active listings: {type(e).__name__}: {str(e)[:120]}"
    external = sum(1 for r in rows if not r["sku"])
    return f"✅ active listings: {len(rows)} on eBay ({external} without a bot SKU)"


if __name__ == "__main__":
    for row in get_active_listings():
        print(f"{row['listing_id']}  sku={row['sku'] or '-':38}  "
              f"${row['price']}  {row['title'][:60]}")
