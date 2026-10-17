"""eBay Browse API: active listings by text query or by photo.

Read-only, public data, so it runs on the application token (client
credentials, base scope) from ebay.auth.get_app_access_token(), which is cached
in memory only and never touches the seller's stored user token.

Two calls:

- search_active(query): GET /buy/browse/v1/item_summary/search. Replaces the
  Apify active-listings actor for comps; returns real conditions and shipping.
- search_by_image(path): POST /buy/browse/v1/item_summary/search_by_image with
  the photo as base64. Active listings only, no text query alongside.

Both return the same normalised dicts (see _normalise). Prices are the item
price only; shipping is kept separate in shipping_cost, which is None when eBay
didn't give a number (calculated shipping without a buyer location, or no
shipping options at all), so callers never mistake "unknown" for "free".
Measured 17 Oct 2026: without ship_to_zip ~38% of results had calculated
shipping and no cost; with a zip, none did. Pass ship_to_zip for comps.

Both searches return the account's own live listings too (image search ranks
them first, since they carry the same photos): filter by seller_username or
legacy_item_id before treating results as comps.
"""
import base64
import io
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import httpx

from config import EBAY_MARKETPLACE_ID

EBAY_API_BASE = "https://api.ebay.com"
SEARCH_PATH = "/buy/browse/v1/item_summary/search"
IMAGE_PATH = "/buy/browse/v1/item_summary/search_by_image"

MAX_LIMIT = 200          # eBay's per-page ceiling for both endpoints
MAX_IMAGE_BYTES = 1_500_000  # photos above this are downscaled before upload
IMAGE_MAX_SIDE = 1600
RETRIES = 2              # extra attempts on 429 / 5xx / network errors


class BrowseError(RuntimeError):
    """The Browse API refused or failed a request."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class BrowseRateLimited(BrowseError):
    """429 that persisted through the retries."""


def _token() -> str:
    # Imported here, not at module top: ebay.auth creates its token table on
    # import, and nothing in this module needs the DB.
    from ebay.auth import get_app_access_token
    return get_app_access_token()


def _headers(ship_to_zip: str | None = None) -> dict:
    h = {
        "Authorization": f"Bearer {_token()}",
        "X-EBAY-C-MARKETPLACE-ID": EBAY_MARKETPLACE_ID,
        "Accept": "application/json",
    }
    if ship_to_zip:
        # Without a buyer location eBay can't price calculated shipping.
        h["X-EBAY-C-ENDUSERCTX"] = f"contextualLocation=country%3DUS%2Czip%3D{ship_to_zip}"
    return h


def _send(method: str, path: str, *, params: dict, headers: dict, json_body=None):
    """The network seam (tests stub this). Returns an httpx.Response."""
    return httpx.request(method, f"{EBAY_API_BASE}{path}", params=params,
                         headers=headers, json=json_body, timeout=30)


def _error_text(r) -> str:
    try:
        body = r.json()
        msgs = [e.get("longMessage") or e.get("message") for e in body.get("errors", [])]
        msgs = [m for m in msgs if m]
        if msgs:
            return "; ".join(msgs)
    except (ValueError, AttributeError):
        pass
    return (r.text or "")[:300]


def _retry_wait(r, attempt: int) -> float:
    try:
        return min(float(r.headers.get("Retry-After", "")), 30.0)
    except (TypeError, ValueError):
        return 2.0 * (attempt + 1)


def _call(method: str, path: str, *, params: dict, json_body=None,
          ship_to_zip: str | None = None) -> dict:
    last = None
    for attempt in range(RETRIES + 1):
        try:
            r = _send(method, path, params=params, headers=_headers(ship_to_zip),
                      json_body=json_body)
        except httpx.HTTPError as e:
            last = BrowseError(f"eBay Browse {path} network error: {type(e).__name__}")
            if attempt < RETRIES:
                time.sleep(2.0 * (attempt + 1))
                continue
            raise last from None
        if r.status_code == 204:
            return {}
        if r.status_code < 400:
            return r.json()
        msg = f"eBay Browse {path} failed [{r.status_code}]: {_error_text(r)}"
        if r.status_code == 429:
            last = BrowseRateLimited(msg, r.status_code)
        elif r.status_code >= 500:
            last = BrowseError(msg, r.status_code)
        else:
            raise BrowseError(msg, r.status_code)  # 4xx: retrying won't help
        if attempt < RETRIES:
            time.sleep(_retry_wait(r, attempt))
    raise last


def _money(m) -> float | None:
    if not m or m.get("value") in (None, ""):
        return None
    try:
        return float(m["value"])
    except (TypeError, ValueError):
        return None


def _shipping(summary: dict) -> float | None:
    """Cheapest shipping eBay quoted a number for, else None (unknown)."""
    costs = [_money(o.get("shippingCost")) for o in summary.get("shippingOptions") or []]
    costs = [c for c in costs if c is not None]
    return min(costs) if costs else None


def _normalise(s: dict) -> dict:
    price = s.get("price") or s.get("currentBidPrice") or {}
    seller = s.get("seller") or {}
    fb_pct = seller.get("feedbackPercentage")
    try:
        fb_pct = float(fb_pct) if fb_pct is not None else None
    except (TypeError, ValueError):
        fb_pct = None
    return {
        "item_id": s.get("itemId"),
        "legacy_item_id": s.get("legacyItemId"),
        "title": s.get("title") or "",
        "price": _money(price),
        "currency": price.get("currency"),
        "shipping_cost": _shipping(s),
        "condition": s.get("condition"),
        "condition_id": s.get("conditionId"),
        "image_url": (s.get("image") or {}).get("imageUrl"),
        "item_web_url": s.get("itemWebUrl"),
        "buying_options": s.get("buyingOptions") or [],
        "seller_username": seller.get("username"),
        "seller_feedback_score": seller.get("feedbackScore"),
        "seller_feedback_pct": fb_pct,
    }


def _common_params(limit: int, category_ids) -> dict:
    params = {"limit": max(1, min(int(limit), MAX_LIMIT))}
    if category_ids:
        params["category_ids"] = ",".join(str(c) for c in category_ids)
    return params


def search_active(query: str | None, *, limit: int = 50, condition_ids=None,
                  category_ids=None, ship_to_zip: str | None = None,
                  gtin: str | None = None) -> list[dict]:
    """Active listings matching a text query and/or a GTIN (UPC/EAN), best
    match first. A GTIN alone is enough: eBay matches it against the product
    identifier sellers filled in, which is exact where a title search is not."""
    query = (query or "").strip()
    gtin = (gtin or "").strip()
    if not query and not gtin:
        raise ValueError("search_active needs a non-empty query or a gtin")
    params = _common_params(limit, category_ids)
    if query:
        params["q"] = query
    if gtin:
        params["gtin"] = gtin
    if condition_ids:
        params["filter"] = "conditionIds:{" + "|".join(str(c) for c in condition_ids) + "}"
    body = _call("GET", SEARCH_PATH, params=params, ship_to_zip=ship_to_zip)
    return [_normalise(s) for s in body.get("itemSummaries") or []]


def _encode_image(image_path) -> str:
    data = Path(image_path).read_bytes()
    if len(data) > MAX_IMAGE_BYTES:
        from PIL import Image, ImageOps
        img = ImageOps.exif_transpose(Image.open(io.BytesIO(data))).convert("RGB")
        img.thumbnail((IMAGE_MAX_SIDE, IMAGE_MAX_SIDE))
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=85)
        data = buf.getvalue()
    return base64.b64encode(data).decode()


def search_by_image(image_path, *, limit: int = 50, category_ids=None,
                    ship_to_zip: str | None = None) -> list[dict]:
    """Active listings that look like the photo. Similar, not necessarily the
    same product: callers must still check the titles."""
    params = _common_params(limit, category_ids)
    body = _call("POST", IMAGE_PATH, params=params,
                 json_body={"image": _encode_image(image_path)}, ship_to_zip=ship_to_zip)
    return [_normalise(s) for s in body.get("itemSummaries") or []]
