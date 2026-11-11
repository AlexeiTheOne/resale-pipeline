"""eBay's suggested Promoted Listings ad rate per listing (Sell Recommendation
API, findListingRecommendations), for /diagnose when eBay's listing quality
report doesn't cover a listing.

eBay bases the TRENDING rate on the category's average ad rates and says it
is a guide, not the best rate. The ITEM basis (added in v1.1.0) is eBay's
rate for that listing. Needs the sell.marketing scope, which the token
already carries (ebay/auth.py).

findListingRecommendations is a POST that changes nothing on the account. It
goes through ebay/guard.py like every POST and is let through by its exact
path (guard.REST_READ_POSTS), so with EBAY_WRITES_ENABLED=0 it still runs
(and fails harmlessly in dev, where there is no user token).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import httpx

from config import EBAY_MARKETPLACE_ID
from ebay import guard
from ebay.auth import get_access_token

EBAY_API_BASE = "https://api.ebay.com"
FIND_PATH = "/sell/recommendation/v1/find"
MAX_IDS = 500   # eBay's limit per request
# Preferred basis first.
_BASES = ("ITEM", "TRENDING")


def suggested_ad_rates(listing_ids: list[str]) -> dict[str, dict]:
    """{listing_id: {"rate": fraction (0.07), "basis": "ITEM" | "TRENDING"}}.
    A listing eBay has no rate for is left out. Raises on an HTTP error."""
    out: dict[str, dict] = {}
    ids = [str(i) for i in listing_ids if i]
    for start in range(0, len(ids), MAX_IDS):
        chunk = ids[start:start + MAX_IDS]
        guard.require_rest_write("POST", FIND_PATH)
        r = httpx.post(f"{EBAY_API_BASE}{FIND_PATH}", params={"filter": "recommendationTypes:{AD}", "limit": len(chunk)},
                       headers={"Authorization": f"Bearer {get_access_token()}",
                                "Content-Type": "application/json", "Accept": "application/json",
                                "X-EBAY-C-MARKETPLACE-ID": EBAY_MARKETPLACE_ID},
                       json={"listingIds": chunk}, timeout=30)
        if r.status_code >= 400:
            raise RuntimeError(f"eBay Recommendation API find failed [{r.status_code}]: {r.text[:300]}")
        for rec in (r.json() or {}).get("listingRecommendations") or []:
            ad = ((rec.get("marketing") or {}).get("ad")) or {}
            rates = {b.get("basis"): b.get("value") for b in ad.get("bidPercentages") or []}
            for basis in _BASES:
                try:
                    value = float(rates.get(basis))
                except (TypeError, ValueError):
                    continue
                out[str(rec.get("listingId"))] = {"rate": value / 100, "basis": basis}
                break
    return out
