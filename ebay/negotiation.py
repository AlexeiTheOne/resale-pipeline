"""Send "Seller Offer" discounts to buyers watching a listing.

eBay's Negotiation API is the seller-initiated half of Best Offer: it privately
offers a lower price to the people who watched or carted an item, without cutting
the public price. That distinction is the whole point — a watcher is someone who
already wants the thing and is waiting for a reason, so a private nudge converts
them at a smaller discount than a public cut costs you across every buyer.

Two calls:
  findEligibleItems           — which listings eBay will let you offer on right
                                now. Eligibility is eBay's own judgement (it
                                needs interested buyers, no active offer already
                                out, a Best-Offer-capable format), so this is the
                                authority, not our watcher counts.
  sendOfferToInterestedBuyers — actually sends it.

Needs the `sell.negotiation` scope (ebay/auth.py).

NOTE: like ebay/analytics.py and ebay/orders.py, the field names below follow
eBay's published schema and haven't been confirmed against a live response from
this account. The first real run is the test — /offers dry-runs by default for
exactly that reason.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import httpx

from config import EBAY_CURRENCY, EBAY_MARKETPLACE_ID
from ebay.auth import get_access_token

BASE = "https://api.ebay.com/sell/negotiation/v1"
_PAGE = 200

# eBay rejects a token gesture: an offer must undercut the listing by at least
# this much to be worth a buyer's attention (and to pass validation).
MIN_DISCOUNT_PCT = 5.0
# How long the buyer has to accept. eBay accepts 1-2 days on seller offers.
OFFER_DURATION_DAYS = 2


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {get_access_token()}",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "X-EBAY-C-MARKETPLACE-ID": EBAY_MARKETPLACE_ID,
    }


def eligible_listing_ids() -> set[str]:
    """Listing ids eBay will currently accept an offer on, following pagination.

    An id missing from this set is not a failure to explain away — it usually
    means nobody is watching, or an offer is already outstanding."""
    ids: set[str] = set()
    offset = 0
    while True:
        r = httpx.get(f"{BASE}/find_eligible_items", headers=_headers(),
                      params={"limit": _PAGE, "offset": offset}, timeout=30)
        if r.status_code >= 400:
            raise RuntimeError(f"eBay findEligibleItems failed [{r.status_code}]: {r.text[:300]}")
        body = r.json()
        batch = body.get("eligibleItems") or []
        for row in batch:
            listing_id = row.get("listingId")
            if listing_id:
                ids.add(str(listing_id))
        total = body.get("total") or 0
        offset += _PAGE
        if len(batch) < _PAGE or offset >= total:
            break
    return ids


def send_offer(listing_id: str, price, message: str | None = None) -> dict:
    """Offer one listing to its interested buyers at `price`.

    An absolute price rather than a percentage, because the discount here is
    decided in dollars against a known margin — letting eBay round a percentage
    would put the final number outside what was actually approved.

    Returns the parsed response; raises on a non-2xx so the caller can report the
    listing that failed rather than silently skipping it."""
    # allowCounterOffer is sent as false because eBay rejects true outright on
    # this endpoint ("Field allowCounterOffer cannot be true", errorId 150009):
    # an offer to interested buyers is take-it-or-leave-it by design, unlike a
    # one-to-one Best Offer negotiation.
    body = {
        "offeredItems": [{
            "listingId": str(listing_id),
            "quantity": 1,
            "price": {"value": f"{float(price):.2f}", "currency": EBAY_CURRENCY},
        }],
        "allowCounterOffer": False,
        "offerDuration": {"unit": "DAY", "value": str(OFFER_DURATION_DAYS)},
    }
    if message:
        # eBay caps the buyer-facing note at 250 characters.
        body["message"] = message[:250]

    r = httpx.post(f"{BASE}/send_offer_to_interested_buyers", headers=_headers(),
                   json=body, timeout=30)
    if r.status_code >= 400:
        raise RuntimeError(f"eBay sendOfferToInterestedBuyers failed "
                           f"[{r.status_code}]: {r.text[:300]}")
    return r.json() if r.content else {}


def negotiation_status() -> str:
    """One-line /health check: is sell.negotiation granted?"""
    try:
        r = httpx.get(f"{BASE}/find_eligible_items", headers=_headers(),
                      params={"limit": 1}, timeout=30)
    except Exception as e:
        return f"❌ offers to watchers: {type(e).__name__}: {str(e)[:100]}"
    if r.status_code == 403:
        return ("❌ offers to watchers: 403 — the sell.negotiation scope isn't granted. "
                "Re-run `python -m ebay.auth`.")
    if r.status_code >= 400:
        return f"❌ offers to watchers: [{r.status_code}] {r.text[:100]}"
    total = (r.json() or {}).get("total")
    return f"✅ offers to watchers: OK ({total} listing(s) eligible right now)"


if __name__ == "__main__":
    print(negotiation_status())
