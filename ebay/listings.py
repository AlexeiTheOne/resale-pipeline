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


def get_active_listings() -> list[dict]:
    """Every currently-active listing on the account.

    Returns dicts of {listing_id, title, sku, price, quantity, view_item_url}.
    `sku` is None for listings created outside the Inventory API — that's the
    signal that this bot has never seen the listing before."""
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
            try:
                qty = int(_text(node, "Quantity") or 1)
            except ValueError:
                qty = 1
            listings.append({
                "listing_id": listing_id,
                "title": _text(node, "Title") or "",
                "sku": _text(node, "SKU") or None,
                "price": _price(node),
                "quantity": qty,
                "view_item_url": _text(node, "ViewItemURL")
                                 or f"https://www.ebay.com/itm/{listing_id}",
            })
        if len(batch) < _PAGE:
            break
        page += 1
    return listings


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
