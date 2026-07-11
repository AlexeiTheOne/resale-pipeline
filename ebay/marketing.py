"""eBay Promoted Listings Standard (COST_PER_SALE) support.

Promoted Listings Standard works like this on eBay: you attach an *ad rate*
(a percentage of the final sale price) to a listing inside an ad campaign. When
the item sells through a promoted placement, eBay charges that percentage. There
is no up-front cost — it's purely a cut of the sale, so a higher rate buys more
visibility.

This module keeps one long-running COST_PER_SALE campaign for the account and
adds each listing to it as an ad carrying its own bid percentage. Our listings
are created through the Inventory API (an offer against a SKU), so ads are
created "by inventory reference" (the SKU), which works whether or not the offer
is published yet.

NOTE: the Marketing API needs the `sell.marketing` OAuth scope. It was added to
ebay/auth.py's SCOPES, but a token minted before that change won't carry it — the
seller must re-run `python -m ebay.auth` to re-consent, or calls here will 403.
"""
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import httpx

from config import EBAY_MARKETPLACE_ID, EBAY_PROMOTED_CAMPAIGN_NAME
from ebay.auth import get_access_token

EBAY_API_BASE = "https://api.ebay.com"

# eBay's accepted ad-rate range for Promoted Listings Standard.
MIN_BID_PCT = 2.0
MAX_BID_PCT = 100.0

_campaign_id_cache: str | None = None


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {get_access_token()}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _request(method: str, path: str, **kwargs) -> httpx.Response:
    r = httpx.request(method, f"{EBAY_API_BASE}{path}", headers=_headers(), timeout=30, **kwargs)
    if r.status_code >= 400:
        raise RuntimeError(f"eBay Marketing API {method} {path} failed [{r.status_code}]: {r.text}")
    return r


def normalize_bid(pct) -> str:
    """Validate an ad-rate percentage and return it as the string eBay expects
    (one decimal place). Raises ValueError if it's out of range."""
    try:
        value = float(str(pct).strip().rstrip("%"))
    except (TypeError, ValueError):
        raise ValueError(f"'{pct}' is not a valid percentage.")
    if not (MIN_BID_PCT <= value <= MAX_BID_PCT):
        raise ValueError(f"Ad rate must be between {MIN_BID_PCT:g}% and {MAX_BID_PCT:g}% (got {value:g}%).")
    return f"{value:.1f}"


def _find_campaign_id() -> str | None:
    """Return the id of our standing COST_PER_SALE campaign if it already exists."""
    r = _request(
        "GET",
        "/sell/marketing/v1/ad_campaign",
        params={"marketplace_id": EBAY_MARKETPLACE_ID},
    )
    for campaign in r.json().get("campaigns", []):
        if campaign.get("campaignName") == EBAY_PROMOTED_CAMPAIGN_NAME:
            return campaign.get("campaignId")
    return None


def _create_campaign() -> str:
    """Create the standing COST_PER_SALE campaign and return its id. Runs open-
    ended (no end date) so every listing can be added to the same campaign."""
    body = {
        "campaignName": EBAY_PROMOTED_CAMPAIGN_NAME,
        "marketplaceId": EBAY_MARKETPLACE_ID,
        "fundingStrategy": {"fundingModel": "COST_PER_SALE"},
        "startDate": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
    }
    r = _request("POST", "/sell/marketing/v1/ad_campaign", json=body)
    # createCampaign returns 201 with the new id in the Location header.
    location = r.headers.get("Location", "")
    campaign_id = location.rstrip("/").split("/")[-1] if location else None
    if not campaign_id:
        # Fall back to a lookup if the header wasn't present.
        campaign_id = _find_campaign_id()
    if not campaign_id:
        raise RuntimeError(f"Campaign created but no campaignId could be resolved (Location: {location!r}).")
    return campaign_id


def ensure_campaign() -> str:
    """Return the account's standing Promoted Listings campaign id, creating it on
    first use. Cached for the process lifetime."""
    global _campaign_id_cache
    if _campaign_id_cache:
        return _campaign_id_cache
    _campaign_id_cache = _find_campaign_id() or _create_campaign()
    return _campaign_id_cache


def marketing_status() -> str:
    """Non-destructive check for /health: a successful GET of the campaigns list
    proves the token carries the sell.marketing scope (a missing scope 403s here).
    Only reads — never creates a campaign. Returns a short human-readable status."""
    campaign_id = _find_campaign_id()
    if campaign_id:
        return f"OK (campaign {campaign_id})"
    return "OK (no campaign yet — created on first /promote)"


def promote_listing(sku: str, bid_percentage) -> dict:
    """Promote the listing behind `sku` at the given ad rate (a percentage).

    Creating an ad twice for the same inventory reference errors on eBay's side;
    in that case we update the existing ad's bid instead, so /promote doubles as
    "change the ad rate". Returns {'campaign_id', 'bid_percentage', 'action'}.
    """
    bid = normalize_bid(bid_percentage)
    campaign_id = ensure_campaign()

    body = {
        "bidPercentage": bid,
        "inventoryReferenceId": sku,
        "inventoryReferenceType": "INVENTORY_ITEM",
    }
    r = httpx.post(
        f"{EBAY_API_BASE}/sell/marketing/v1/ad_campaign/{campaign_id}/create_ads_by_inventory_reference",
        headers=_headers(),
        json=body,
        timeout=30,
    )
    if r.status_code < 400:
        return {"campaign_id": campaign_id, "bid_percentage": bid, "action": "created"}

    # Already promoted → adjust the existing ad's rate instead of failing.
    if "already" in r.text.lower():
        update = {"bidPercentage": bid, "inventoryReferenceId": sku, "inventoryReferenceType": "INVENTORY_ITEM"}
        _request(
            "POST",
            f"/sell/marketing/v1/ad_campaign/{campaign_id}/bulk_update_ads_bid_by_inventory_reference",
            json={"requests": [update]},
        )
        return {"campaign_id": campaign_id, "bid_percentage": bid, "action": "updated"}

    raise RuntimeError(
        f"eBay Marketing API create ad failed [{r.status_code}]: {r.text}"
    )
