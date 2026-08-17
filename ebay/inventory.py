import hashlib
import html
import json
import os
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import httpx
from dotenv import load_dotenv

from config import (
    EBAY_CURRENCY,
    EBAY_FULFILLMENT_POLICY_ID,
    EBAY_MARKETPLACE_ID,
    EBAY_MERCHANT_LOCATION_KEY,
    EBAY_RETURN_POLICY_ID,
    EBAY_SHIP_FROM_ADDRESS,
    WYSIWYG_NOTE,
)
from db import get_item, update_field
from ebay.auth import get_access_token
from ebay.taxonomy import InvalidCategoryError, category_aspects, category_name, resolve_valid_category

load_dotenv()

EBAY_API_BASE = "https://api.ebay.com"

ASPECT_VALUE_MAX_LEN = 65
ASPECT_MAX_VALUES = 10
TITLE_MAX_LEN = 80

GTIN_ASPECT_NAMES = {"upc", "ean", "isbn", "gtin"}


class MissingRequiredAspectsError(Exception):
    """Raised when a category requires an item specific we have no safe,
    confident value for — surfaced to the seller instead of guessed at."""

    def __init__(self, category_id: str, missing: list[dict]):
        self.category_id = category_id
        self.missing = missing
        names = ", ".join(m["name"] for m in missing)
        super().__init__(f"Category {category_id} requires aspects with no safe value: {names}")


CLOUDINARY_CLOUD_NAME = os.getenv("CLOUDINARY_CLOUD_NAME")
CLOUDINARY_API_KEY = os.getenv("CLOUDINARY_API_KEY")
CLOUDINARY_API_SECRET = os.getenv("CLOUDINARY_API_SECRET")

# Legacy Trading-API numeric condition IDs (used elsewhere in this app, see
# pipeline/draft.py's SCHEMA_INSTRUCTIONS) mapped to Inventory API condition enums.
CONDITION_MAP = {
    "1000": "NEW",
    "1500": "NEW_OTHER",
    "2750": "USED_EXCELLENT",
    "3000": "USED_GOOD",
    "4000": "USED_ACCEPTABLE",
}


def _upload_image(path: str) -> str:
    if not (CLOUDINARY_CLOUD_NAME and CLOUDINARY_API_KEY and CLOUDINARY_API_SECRET):
        raise RuntimeError(
            "Cloudinary credentials are not set. Add CLOUDINARY_CLOUD_NAME, "
            "CLOUDINARY_API_KEY, and CLOUDINARY_API_SECRET to .env."
        )

    timestamp = int(time.time())
    to_sign = f"timestamp={timestamp}{CLOUDINARY_API_SECRET}"
    signature = hashlib.sha1(to_sign.encode()).hexdigest()

    url = f"https://api.cloudinary.com/v1_1/{CLOUDINARY_CLOUD_NAME}/image/upload"
    with open(path, "rb") as f:
        r = httpx.post(
            url,
            files={"file": f},
            data={"api_key": CLOUDINARY_API_KEY, "timestamp": timestamp, "signature": signature},
            timeout=60,
        )
    if r.status_code >= 400:
        raise RuntimeError(f"Cloudinary upload failed [{r.status_code}]: {r.text}")
    return r.json()["secure_url"]


def upload_photos(paths: list[str]) -> list[str]:
    return [_upload_image(p) for p in paths]


def _reorder_for_listing(paths: list[str], manual: bool = False) -> list[str]:
    """Move the tag close-up (always the 2nd photo the user sends) to the end so
    it shows as the last image in the eBay listing — buyers want to lead with the
    product, not the tag. Leaves the first (overview) photo as the gallery cover.

    `manual` turns this off: once /cover or /arrange has set the order by hand,
    the stored list IS the intended order, and the "2nd photo is the tag"
    assumption no longer holds — applying it anyway would move whichever photo
    the user deliberately put second to the back of the listing."""
    if manual or len(paths) < 2:
        return paths
    return paths[:1] + paths[2:] + [paths[1]]


def listing_photo_order(item: dict) -> list[str]:
    """An item's photos in the order the eBay listing will actually show them —
    first is the gallery cover. The one place that answers "which photo is #3",
    so the bot's /photos numbering and the published listing can't disagree."""
    return _reorder_for_listing(
        list(item.get("photos") or []),
        manual=bool((item.get("photo_layout") or {}).get("manual")))


def _download_brand_image(url, item_id: str) -> str | None:
    """Best-effort fetch of an official product photo to a temp file so it can be
    re-hosted on Cloudinary (avoids brand-site hotlink blocking). Returns the path,
    or None if anything is off — a stock image is a nice-to-have and must NEVER
    block or break a listing."""
    if not _is_confirmed(url):
        return None
    url = str(url).strip()
    if not url.lower().startswith(("http://", "https://")):
        return None
    try:
        r = httpx.get(
            url, timeout=20, follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
        )
        if r.status_code != 200:
            return None
        ctype = r.headers.get("content-type", "").lower()
        if not ctype.startswith("image/"):
            return None
        data = r.content
        if len(data) < 5000:  # skip 1x1 trackers / placeholder thumbnails
            return None
        ext = ".png" if "png" in ctype else ".webp" if "webp" in ctype else ".jpg"
        fd, path = tempfile.mkstemp(prefix=f"stock_{item_id}_", suffix=ext)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        return path
    except Exception:
        return None


def save_stock_photo(item_id: str, url: str, position: int = 2) -> str:
    """Download a product photo from a URL and add it to an item's photo set.

    For anything sold sealed in retail packaging — bedding above all — every
    photo we can take is a plastic bag with a branded band, and the buyer never
    sees the pattern they are buying. The retailer's styled shot is the only way
    to show it.

    This exists because the retailers who own those photos (Macy's, Belk, Ralph
    Lauren, Wayfair, Amazon, Dillard's) all block automated fetching — measured,
    every one of them, via both a browser-UA client and a separate fetch
    service. A person's browser is not blocked, so the URL comes from the
    seller, who is also the only one who can confirm the pattern actually
    matches the item in hand. A wrong colourway is worse than no photo.

    The file is stored beside the item's own photos, not in a temp dir, so it
    survives the next rebuild. Returns the saved path.

    position is 1-based in listing order; the default of 2 puts it directly
    after the real cover, where it is the first thing the carousel shows.
    """
    item = get_item(item_id)
    if item is None:
        raise ValueError(f"No item {item_id[:8]}")

    path = _download_brand_image(url, item_id)
    if not path:
        raise ValueError(
            "That URL didn't return a usable image. It needs to be the direct "
            "image address (right-click the photo → Copy image address), not "
            "the product page.")

    existing = list(item.get("photos") or [])
    if not existing:
        raise ValueError(f"{item_id[:8]} has no photos of its own yet")
    folder = Path(existing[0]).parent
    folder.mkdir(parents=True, exist_ok=True)
    dest = folder / f"{item_id}_stock{Path(path).suffix or '.jpg'}"
    shutil.move(path, dest)

    ordered = listing_photo_order(item)
    ordered = [p for p in ordered if Path(p) != dest]
    index = max(0, min(len(ordered), position - 1))
    ordered.insert(index, str(dest))

    update_field(item_id, "photos", ordered)
    # The stored list is now exactly the intended order, so stop the builder
    # applying its own tag-to-the-back reorder on top of it.
    update_field(item_id, "photo_layout", {"manual": True})
    return str(dest)


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {get_access_token()}",
        "Content-Type": "application/json",
        "Content-Language": "en-US",
        "Accept-Language": "en-US",
    }


def _request(method: str, path: str, **kwargs):
    r = httpx.request(method, f"{EBAY_API_BASE}{path}", headers=_headers(), timeout=30, **kwargs)
    if r.status_code >= 400:
        raise RuntimeError(f"eBay API {method} {path} failed [{r.status_code}]: {r.text}")
    return r


def _ensure_location() -> None:
    url = f"{EBAY_API_BASE}/sell/inventory/v1/location/{EBAY_MERCHANT_LOCATION_KEY}"
    r = httpx.get(url, headers=_headers(), timeout=30)
    if r.status_code == 200:
        return
    if r.status_code != 404:
        raise RuntimeError(f"eBay API GET location failed [{r.status_code}]: {r.text}")

    body = {
        "location": {"address": EBAY_SHIP_FROM_ADDRESS},
        "locationTypes": ["WAREHOUSE"],
        "name": "Resale warehouse",
    }
    create = httpx.post(url, headers=_headers(), json=body, timeout=30)
    if create.status_code >= 400:
        raise RuntimeError(f"eBay API create location failed [{create.status_code}]: {create.text}")


def _existing_offer_id(body_text: str) -> str | None:
    """eBay's createOffer returns the existing offerId as an error parameter
    when an offer already exists for this sku/marketplace/format."""
    try:
        body = json.loads(body_text)
    except (ValueError, TypeError):
        return None
    for error in body.get("errors", []):
        for param in error.get("parameters", []):
            if param.get("name") == "offerId":
                return param.get("value")
    return None


def _create_offer(offer: dict) -> str:
    r = httpx.post(f"{EBAY_API_BASE}/sell/inventory/v1/offer", headers=_headers(), json=offer, timeout=30)
    if r.status_code >= 400:
        existing_id = _existing_offer_id(r.text)
        if existing_id:
            # An offer already exists for this sku. Returning its id alone threw
            # away everything we just built — price, description, category — and
            # every caller then reported success, so a /setprice that failed
            # transiently and was "recovered" by rebuilding the offer left eBay
            # selling at the old price forever while the DB, the report and the
            # repricer all agreed on the new one.
            #
            # PUT the body we built onto the offer that exists. Same full-replace
            # shape update_offer_price and _update_offer_description already use.
            p = httpx.put(f"{EBAY_API_BASE}/sell/inventory/v1/offer/{existing_id}",
                          headers=_headers(), json=offer, timeout=30)
            if p.status_code >= 400:
                raise RuntimeError(
                    f"eBay offer {existing_id} already existed for this SKU and "
                    f"updating it failed [{p.status_code}]: {p.text}")
            return existing_id
        raise RuntimeError(f"eBay API POST /sell/inventory/v1/offer failed [{r.status_code}]: {r.text}")
    return r.json()["offerId"]


def delete_offer(offer_id: str) -> None:
    """Remove an offer from eBay's side. Treats 'already gone' (404) as success
    so cleanup is safe to retry."""
    r = httpx.delete(f"{EBAY_API_BASE}/sell/inventory/v1/offer/{offer_id}", headers=_headers(), timeout=30)
    if r.status_code in (204, 404):
        return
    raise RuntimeError(
        f"eBay API DELETE /sell/inventory/v1/offer/{offer_id} failed [{r.status_code}]: {r.text}"
    )


def publish_offer(offer_id: str) -> str:
    """Publish a previously-created draft offer, making it a live listing.
    Returns the resulting eBay listing id."""
    return _request("POST", f"/sell/inventory/v1/offer/{offer_id}/publish").json()["listingId"]


def withdraw_offer(offer_id: str) -> None:
    """End a live listing (withdraw the published offer). The offer itself remains
    as a draft, so it can be re-published later with publish_offer. A 404 (already
    gone) or an 'offer not published' error is treated as success so /end is safe
    to retry."""
    r = httpx.post(
        f"{EBAY_API_BASE}/sell/inventory/v1/offer/{offer_id}/withdraw",
        headers=_headers(), timeout=30,
    )
    if r.status_code in (200, 204, 404):
        return
    # Not published (25710) means it's already not live — nothing to end.
    if "25710" in r.text or "not published" in r.text.lower():
        return
    raise RuntimeError(
        f"eBay API POST /sell/inventory/v1/offer/{offer_id}/withdraw failed [{r.status_code}]: {r.text}"
    )


# Offer fields that carry over when we re-send an offer to change its price. eBay's
# updateOffer is a full replace, so we read the current offer and resend these.
# Deliberately excludes listingStartDate — a published offer's start date is in the
# past and eBay rejects re-sending a past date.
_OFFER_UPDATABLE_FIELDS = (
    "availableQuantity", "categoryId", "listingDescription", "listingPolicies",
    "merchantLocationKey", "tax", "storeCategoryNames",
    "quantityLimitPerBuyer", "lotSize", "hideBuyerDetails",
)


def update_offer_price(offer_id: str, price) -> None:
    """Change an offer's price. eBay's updateOffer is a full PUT replace, so read
    the current offer, swap in the new price, and send the carried-over fields
    back. Works for both an unpublished draft and a live listing (a published
    offer's update takes effect on the live listing immediately)."""
    current = _request("GET", f"/sell/inventory/v1/offer/{offer_id}").json()
    body = {k: current[k] for k in _OFFER_UPDATABLE_FIELDS if current.get(k) is not None}
    body["pricingSummary"] = {"price": {"value": str(price), "currency": EBAY_CURRENCY}}
    _request("PUT", f"/sell/inventory/v1/offer/{offer_id}", json=body)


# Inventory-item fields that carry over when we re-send one to change its title.
# Like updateOffer, the inventory_item PUT is a full replace — anything omitted is
# erased, and `sku` and `locale` are read-only on the way back in.
_INVENTORY_UPDATABLE_FIELDS = (
    "product", "condition", "conditionDescription", "conditionDescriptors",
    "packageWeightAndSize", "availability",
)


class InventoryImagesMissingError(Exception):
    """About to write an inventory item with fewer images than it should have,
    and there is no stored list to repair it from.

    Raised instead of writing, because the inventory_item PUT is a FULL REPLACE:
    sending back a truncated product block makes the truncation permanent and
    strips a live listing down to whatever came back."""


def _stored_image_urls(sku: str) -> list[str]:
    """The Cloudinary URLs we uploaded for this SKU, from our own DB.

    This — not eBay's GET — is the authoritative list. SKU is the item_id
    (create_draft_offer sets `sku = item_id`), so the lookup is direct."""
    item = get_item(sku) or {}
    return list((item.get("ebay") or {}).get("image_urls") or [])


def update_inventory_title(sku: str, title: str, known_images: int | None = None) -> str:
    """Change just the title on an existing inventory item, live listing included.

    The alternative is create_draft_offer, which is the only title path the bot
    had — but that rebuilds the whole item and RE-UPLOADS every photo to
    Cloudinary on the way through. For a title fix that's several hundred
    needless uploads across a catalogue and minutes of wall-clock, to change one
    string. This reads the item, swaps the title, and sends it back.

    Returns the title eBay ended up with, which is not always the one passed:
    eBay hard-caps titles at 80 characters and _truncate_title cuts at a word
    boundary, so the caller can compare and notice if anything was lost.

    IMAGES ARE NEVER TAKEN FROM eBay's RESPONSE. getInventoryItem returns a
    lossy projection of the record: it drops `product.description` on every
    item, and on a substantial minority of them it returns `imageUrls` collapsed
    to the single first URL while the live listing still shows the full set.
    Measured here: 30 of 109 items reported one image each, at the same rate for
    items this function had never touched (3 of 13) as for those it had (27 of
    96), and one item reverted to a single URL twice within twenty minutes with
    nothing writing to it. Because the PUT is a full replace, echoing that back
    is what would turn eBay's bad read into a real, buyer-visible loss. So the
    stored list wins and a short response is repaired, not propagated.

    The trade-off: a photo deliberately deleted in Seller Hub gets restored on
    the next title edit. That is recoverable in a way a listing stripped to one
    photo is not.
    """
    current = _request("GET", f"/sell/inventory/v1/inventory_item/{sku}").json()
    body = {k: current[k] for k in _INVENTORY_UPDATABLE_FIELDS
            if current.get(k) is not None}
    product = dict(body.get("product") or {})

    returned = product.get("imageUrls") or []
    stored = _stored_image_urls(sku)
    expected = known_images if known_images is not None else len(stored)
    if len(returned) < expected:
        if not stored:
            raise InventoryImagesMissingError(
                f"{sku}: eBay returned {len(returned)} image(s), fewer than the "
                f"{expected} expected, and no stored URLs exist to repair it "
                f"from. Refusing to PUT — that would make the loss permanent.")
        print(f"🖼️ {sku[:8]}: eBay returned {len(returned)} image(s); "
              f"restoring the stored {len(stored)} instead of echoing the loss")
        product["imageUrls"] = stored

    product["title"] = _truncate_title(title)
    body["product"] = product

    # eBay hands back `{"weight": {"value": 0.0, "unit": "POUND"}}` on GET for any
    # item whose weight was never set, then rejects that exact payload on PUT with
    # errorId 25709 "Invalid value for weight.value". Round-tripping its own
    # response fails on every such item — 11 of 40 here. A zero weight carries no
    # information (it IS the unset placeholder), so dropping it loses nothing and
    # is the only way the update can succeed.
    pkg = dict(body.get("packageWeightAndSize") or {})
    if pkg:
        try:
            weight_value = float((pkg.get("weight") or {}).get("value") or 0)
        except (TypeError, ValueError):
            weight_value = 0.0
        if weight_value <= 0:
            pkg.pop("weight", None)
        # What's left may be just `{"shippingIrregular": false}`, which is not
        # worth sending on its own.
        if not pkg or set(pkg) <= {"shippingIrregular"} and not pkg.get("shippingIrregular"):
            body.pop("packageWeightAndSize", None)
        else:
            body["packageWeightAndSize"] = pkg

    _request("PUT", f"/sell/inventory/v1/inventory_item/{sku}", json=body)
    return product["title"]


def get_offer(offer_id: str) -> dict:
    """Read an offer's current state from eBay: price, availableQuantity, and the
    nested `listing` container (listingId + listingStatus). Used by /sync to pull
    manual Seller-Hub edits back into the local DB."""
    return _request("GET", f"/sell/inventory/v1/offer/{offer_id}").json()


def _update_offer_description(offer_id: str, current: dict, description_html: str) -> None:
    """Swap an existing offer's description, live listing included. Same full-PUT
    replace as update_offer_price — and note pricingSummary is carried over
    explicitly: it's excluded from _OFFER_UPDATABLE_FIELDS, so a PUT without it
    would strip the price off the listing."""
    body = {k: current[k] for k in _OFFER_UPDATABLE_FIELDS if current.get(k) is not None}
    body["listingDescription"] = description_html
    price = ((current.get("pricingSummary") or {}).get("price") or {})
    if price.get("value") is not None:
        body["pricingSummary"] = {"price": {"value": str(price["value"]),
                                            "currency": price.get("currency") or EBAY_CURRENCY}}
    _request("PUT", f"/sell/inventory/v1/offer/{offer_id}", json=body)


def refresh_offer_description(item: dict, apply: bool = False, force: bool = False) -> str:
    """Re-render one item's stored description and push it to its existing eBay
    offer, so a listing that went live before a copy change (the WYSIWYG line) can
    pick it up without being rebuilt or relisted.

    Returns what happened, or would happen when apply is False:
      no_offer  — nothing on eBay to update
      current   — eBay already has exactly this description
      diverged  — the live description isn't what we'd generate from our stored
                  text, so someone edited it in Seller Hub (or it predates a
                  change to the renderer). Skipped unless force: overwriting it
                  would silently throw that editing away.
      updated   — pushed (or, in a dry run, would be)

    Read-only when apply is False: it only ever GETs the offer."""
    offer_id = (item.get("ebay") or {}).get("offer_id")
    text = (item.get("listing") or {}).get("description")
    if not offer_id or not text:
        return "no_offer"

    new_html = _html_description(text)
    current = _request("GET", f"/sell/inventory/v1/offer/{offer_id}").json()
    live = current.get("listingDescription") or ""

    if live == new_html:
        return "current"
    # Compare with the banner taken off BOTH sides: what's left is the listing's
    # actual copy. Equal means the live description is ours and the only change is
    # the promise line (or a rewording of it) — safe to replace. Different means
    # someone edited it in Seller Hub, and overwriting would throw that away.
    if not force and _strip_banner(live) != _strip_banner(new_html):
        return "diverged"

    if apply:
        _update_offer_description(offer_id, current, new_html)
    return "updated"


def update_offer_quantity(sku: str, offer_id: str, quantity: int) -> None:
    """Change how many units are available for a SKU's offer. eBay keeps two
    quantities that must agree — the inventory item's shipToLocationAvailability
    and the offer's availableQuantity — so this updates both atomically via
    bulkUpdatePriceQuantity (setting only one leaves them inconsistent and the
    offer rejects a higher availableQuantity than the item stocks). Works for a
    draft or a live offer. The bulk call returns HTTP 200 even when an individual
    SKU fails, so the per-SKU status in the body is checked."""
    body = {
        "requests": [{
            "sku": sku,
            "shipToLocationAvailability": {"quantity": quantity},
            "offers": [{"offerId": offer_id, "availableQuantity": quantity}],
        }]
    }
    resp = _request("POST", "/sell/inventory/v1/bulk_update_price_quantity", json=body).json()
    for res in resp.get("responses", []):
        status = res.get("statusCode", 200)
        if status >= 400:
            raise RuntimeError(
                f"eBay bulk_update_price_quantity failed for SKU {res.get('sku')} "
                f"[{status}]: {res.get('errors')}"
            )


def get_policy_id(policy_type: str) -> str:
    resp = _request(
        "GET",
        f"/sell/account/v1/{policy_type}_policy",
        params={"marketplace_id": EBAY_MARKETPLACE_ID},
    ).json()
    policies = resp.get(f"{policy_type}Policies") or []
    if not policies:
        raise RuntimeError(
            f"No {policy_type} policy found for {EBAY_MARKETPLACE_ID}. "
            "Set up Business Policies in eBay Seller Hub (Account > Business Policies) first."
        )
    return policies[0][f"{policy_type}PolicyId"]


_PLACEHOLDER_VALUES = {"", "null", "none", "n/a", "na", "unknown", "tbd"}


def _is_confirmed(value) -> bool:
    return value is not None and str(value).strip().lower() not in _PLACEHOLDER_VALUES


def _valid_gtin(value) -> bool:
    """True if value is a structurally valid GTIN (UPC-A/EAN-8/13/GTIN-14):
    right length and a correct check digit. The model frequently misreads a
    barcode off a photo, and eBay rejects the whole publish on a bad UPC — so we
    validate and drop it (eBay lets the field be blank) rather than block listing."""
    digits = re.sub(r"\D", "", str(value))
    if len(digits) not in (8, 12, 13, 14):
        return False
    nums = [int(d) for d in digits]
    body, check = nums[:-1], nums[-1]
    total = sum(d * (3 if i % 2 == 0 else 1) for i, d in enumerate(reversed(body)))
    return (10 - total % 10) % 10 == check


def _truncate(text: str, limit: int) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    if " " in cut:
        cut = cut.rsplit(" ", 1)[0]  # don't cut mid-word
    return cut.rstrip(" ,;:-/").strip()  # drop a dangling trailing fragment


def _truncate_value(value) -> str:
    return _truncate(str(value), ASPECT_VALUE_MAX_LEN)


def _truncate_title(title: str) -> str:
    return _truncate(title, TITLE_MAX_LEN)


def _dedup_preserve(items) -> list[str]:
    """De-duplicate case-insensitively while keeping first-seen order."""
    seen, out = set(), []
    for it in items:
        s = str(it).strip()
        key = s.lower()
        if s and key not in seen:
            seen.add(key)
            out.append(s)
    return out


def _normalize_aspect_values(value) -> list[str]:
    values = value if isinstance(value, list) else [value]
    cleaned = []
    for v in values:
        if not _is_confirmed(v):
            continue
        tv = _truncate_value(v)
        # collapse a value that repeats comma-separated tokens, e.g.
        # "Travel, Casual, Travel, Casual" -> "Travel, Casual"
        if "," in tv:
            tv = ", ".join(_dedup_preserve(tv.split(",")))
        cleaned.append(tv)
    return _dedup_preserve(cleaned)[:ASPECT_MAX_VALUES]


def _build_aspects(item_specifics: dict) -> dict:
    item_specifics = item_specifics or {}
    aspects = {}
    for key, value in item_specifics.items():
        if key == "BrandMPN":
            continue
        if key.lower() in GTIN_ASPECT_NAMES and _is_confirmed(value) and not _valid_gtin(value):
            print(f"⚠️ Dropping invalid {key} '{value}' from aspects (bad GTIN check digit)")
            continue
        values = _normalize_aspect_values(value)
        if values:
            aspects[key] = values
    return aspects


def _guess_fallback(name: str, item_type_hint: str | None, allowed_values: list[str]) -> str | None:
    key = name.strip().lower()
    if key == "brand":
        return "Unbranded"
    if key == "mpn":
        return "Does not apply"
    if key in GTIN_ASPECT_NAMES:
        return "Does not apply"
    if key == "type" and item_type_hint:
        for value in allowed_values:
            if value.lower() == item_type_hint.strip().lower():
                return value  # use the catalog's exact casing for SELECTION_ONLY
        return item_type_hint
    if allowed_values and {v.lower() for v in allowed_values} == {"yes", "no"}:
        return next(v for v in allowed_values if v.lower() == "no")
    return None


# Aspect names that mean the same thing across categories. Used to satisfy a
# required aspect from a synonym we already have — e.g. the model wrote "Color"
# but the category requires "Exterior Color".
ASPECT_SYNONYMS = [
    {"color", "colour", "exterior color", "main color", "main colour"},
]


def _synonym_values(name: str, resolved: dict) -> list | None:
    """Values from an already-filled aspect that is synonymous with `name`."""
    key = name.strip().lower()
    group = next((g for g in ASPECT_SYNONYMS if key in g), None)
    if not group:
        return None
    for other, values in resolved.items():
        if other.strip().lower() != key and other.strip().lower() in group and values:
            return values if isinstance(values, list) else [values]
    return None


def _match_allowed(values: list, allowed_values: list[str]) -> str | None:
    """First of our values (or a comma-separated token within one) that matches an
    allowed SELECTION_ONLY value, returned in the catalog's exact casing."""
    canonical = {a.lower(): a for a in allowed_values}
    for v in values:
        for token in [str(v)] + [t.strip() for t in str(v).split(",")]:
            if token.lower() in canonical:
                return canonical[token.lower()]
    return None


def _resolve_required_aspects(category_id: str, aspects: dict, item_type_hint: str | None) -> dict:
    """Fill every category-required aspect with a safe fallback. Raises
    MissingRequiredAspectsError for any we can't confidently fill, instead of
    sending something that's likely to fail validation at publish time."""
    resolved = dict(aspects)
    missing = []

    for spec in category_aspects(category_id):
        name = spec.get("localizedAspectName")
        if not name:
            continue

        constraint = spec.get("aspectConstraint") or {}
        # "Type" trips this validation often enough in practice that we treat
        # it as required even when the category metadata doesn't flag it.
        required = bool(constraint.get("aspectRequired")) or name.strip().lower() == "type"
        if not required or resolved.get(name):
            continue

        mode = constraint.get("aspectMode", "FREE_TEXT")
        allowed_values = [v.get("localizedValue") for v in (spec.get("aspectValues") or []) if v.get("localizedValue")]

        # Satisfy it from a synonym we already have (e.g. Color -> Exterior Color).
        synonym = _synonym_values(name, resolved)
        if synonym:
            chosen = _match_allowed(synonym, allowed_values) if mode == "SELECTION_ONLY" else synonym[0]
            if chosen:
                resolved[name] = _normalize_aspect_values(chosen)
                continue

        fallback = _guess_fallback(name, item_type_hint, allowed_values)
        if fallback and (mode != "SELECTION_ONLY" or fallback in allowed_values):
            resolved[name] = _normalize_aspect_values(fallback)
            continue

        missing.append({"name": name, "mode": mode, "allowed_values": allowed_values[:15]})

    if missing:
        raise MissingRequiredAspectsError(category_id, missing)

    return resolved


def _enforce_single_cardinality(category_id: str, aspects: dict) -> dict:
    """eBay rejects a multi-value aspect that its category defines as single-valued
    (e.g. 'Exterior Color should contain only one value'). Trim those to the first
    value; genuinely multi-value aspects (Features, Material) are left untouched.
    Only trims aspects the category explicitly marks SINGLE, so anything not in the
    category metadata is left as-is."""
    single = {
        spec["localizedAspectName"]
        for spec in category_aspects(category_id)
        if spec.get("localizedAspectName")
        and (spec.get("aspectConstraint") or {}).get("itemToAspectCardinality") == "SINGLE"
    }
    return {
        name: (values[:1] if name in single and isinstance(values, list) else values)
        for name, values in aspects.items()
    }


_BULLET_PREFIXES = ("-", "–", "—", "•", "*", "✓", "✔")


_MD_EMPHASIS = re.compile(r"\*\*+(.+?)\*\*+")


def _strip_markdown(line: str) -> str:
    """Drop the markdown emphasis this renderer doesn't speak.

    The copywriter drifts into **bold** on a large share of generations, and '*'
    is a bullet prefix — so '**SPECIFICATIONS:**' matched as a bullet whose text
    is '*SPECIFICATIONS:**'. Every section heading silently became a list item
    and literal asterisks shipped to the live listing: 63 of 114 live listings
    were rendering that way when this was found.

    Emphasis is stripped rather than converted to <b>, because the bold tag is
    what _strip_banner uses to recognise the WYSIWYG line by shape — emitting
    <b> anywhere else would make an arbitrary bolded phrase look like the
    banner and get silently deleted from the comparison."""
    line = _MD_EMPHASIS.sub(r"\1", line)
    return re.sub(r"\*{2,}", "", line).strip()


def _is_heading(line: str) -> bool:
    """An all-caps line (e.g. 'SPECIFICATIONS:', 'ABOUT US:') is a section header."""
    stripped = line.rstrip(":").strip()
    return len(stripped) >= 2 and any(c.isalpha() for c in stripped) and stripped == stripped.upper()


def _already_says_wysiwyg(text: str) -> bool:
    """Whether the description already makes the promise, in any punctuation or
    casing — so a copywriter that wrote it, or a listing being rebuilt from a
    description that already carries it, doesn't get told twice."""
    squashed = re.sub(r"[^a-z]", "", text.lower())
    return "whatyouseeiswhatyouget" in squashed


def _wysiwyg_banner(text: str) -> str:
    if not WYSIWYG_NOTE or _already_says_wysiwyg(text):
        return ""
    return f'<p style="margin:16px 0 12px"><b>{html.escape(WYSIWYG_NOTE)}</b></p>'


def _strip_banner(description_html: str) -> str:
    """A rendered description with any WYSIWYG line removed, for comparing one
    against another without the promise itself getting in the way. The renderer
    emits <b> nowhere else, so a bold-only paragraph is always the banner —
    including an older wording of it, which is why this strips by shape rather
    than by matching the current WYSIWYG_NOTE text."""
    return re.sub(r"<p[^>]*><b>.*?</b></p>", "", description_html, count=1, flags=re.DOTALL)


# Store-policy headings, i.e. where the item's own description ends and the
# boilerplate begins. The WYSIWYG line goes immediately above the first of these:
# it's the closing word on the item, read after the specs and condition, not a
# banner over them. SHIPPING is always the first in practice (see draft.py's
# STORE_BOILERPLATE); the rest are here so a revised description that dropped or
# reordered a section still puts the line in the right place.
_POLICY_HEADINGS = ("SHIPPING", "RETURNS", "ABOUT US")


def _html_description(text: str) -> str:
    """eBay renders descriptions as HTML and collapses plain-text line breaks, so a
    nicely laid-out plain description arrives as one unformatted blob. Convert the
    generated text — all-caps headings and '-'/'✓' bullet lines — into simple, valid
    HTML so the published listing keeps its structure, with the bold
    what-you-see-is-what-you-get promise (WYSIWYG_NOTE in config.py) sitting just
    above the shipping/returns boilerplate."""
    blocks: list[str] = []
    bullets: list[str] = []
    policy_at: int | None = None   # index of the first store-policy heading

    def flush_bullets() -> None:
        if bullets:
            items = "".join(f"<li>{html.escape(b)}</li>" for b in bullets)
            blocks.append(f"<ul>{items}</ul>")
            bullets.clear()

    for raw in text.splitlines():
        # Emphasis comes off before anything is classified: a '**HEADING:**' has
        # to stop looking like a '*' bullet before the marker test sees it.
        line = _strip_markdown(raw.strip())
        if not line:
            flush_bullets()
            continue
        marker = next((p for p in _BULLET_PREFIXES if line.startswith(p)), None)
        if marker:
            bullets.append(line[len(marker):].strip())
        elif _is_heading(line):
            flush_bullets()
            heading = line.rstrip(":").strip()
            if policy_at is None and heading.upper() in _POLICY_HEADINGS:
                policy_at = len(blocks)
            blocks.append(f"<h3>{html.escape(heading)}</h3>")
        else:
            flush_bullets()
            blocks.append(f"<p>{html.escape(line)}</p>")
    flush_bullets()

    # No policy section at all (a hand-written description, say) — the line still
    # has to appear, so it goes last rather than being silently dropped.
    banner = _wysiwyg_banner(text)
    if banner:
        blocks.insert(policy_at if policy_at is not None else len(blocks), banner)

    body = "".join(blocks)
    return f'<div style="font-family:Arial,Helvetica,sans-serif;font-size:14px;line-height:1.5">{body}</div>'


def _build_product(listing: dict, image_urls: list[str], aspects: dict, description: str) -> dict:
    specifics = listing.get("item_specifics") or {}
    product = {
        "title": _truncate_title(listing["title"]),
        "description": description,
        "aspects": aspects,
        "imageUrls": image_urls,
    }

    # eBay's category validation treats Brand+MPN as a required pair (surfaced
    # in errors as the legacy "BrandMPN" tag) — it wants these top-level product
    # identifier fields present, using the "Does not apply" convention when
    # there's genuinely no MPN, rather than omitting the field.
    brand = specifics.get("Brand")
    product["brand"] = str(brand) if _is_confirmed(brand) else "Unbranded"

    mpn = specifics.get("MPN")
    product["mpn"] = str(mpn) if _is_confirmed(mpn) else "Does not apply"

    upc = specifics.get("UPC")
    if _is_confirmed(upc):
        if _valid_gtin(upc):
            product["upc"] = [_truncate_value(upc)]
        else:
            print(f"⚠️ Dropping invalid UPC '{upc}' (bad GTIN check digit); listing without UPC")
    return product


def create_draft_offer(item_id: str) -> dict:
    """Push the listing to eBay as an unpublished offer (a Draft in Seller Hub).
    Does not call publishOffer — nothing goes live until that's done separately."""
    item = get_item(item_id)
    if item is None:
        raise ValueError(f"No item found for id {item_id}")

    listing = item.get("listing")
    if not listing:
        raise ValueError(f"Item {item_id} has no listing draft yet")

    photos = item.get("photos") or []
    if not photos:
        raise ValueError(f"Item {item_id} has no photos to list")
    print(f"📤 Draft: {len(photos)} photo(s) in DB record for {item_id}")

    original_category_id = str(listing["category_id"])
    item_type_hint = (item.get("identification") or {}).get("item_type")

    # Validate the category up front; if eBay rejects it (62005), re-select via
    # the Taxonomy suggestions endpoint using the listing title, and persist the
    # correction so a retry doesn't repeat the lookup.
    category_id, resolved_category_name = resolve_valid_category(original_category_id, listing["title"])
    if category_id != original_category_id:
        listing["category_id"] = category_id
        update_field(item_id, "listing", listing)

    # Resolve category-required aspects before doing any side-effecting work
    # (image uploads, API calls) so a missing aspect fails fast and cheap.
    aspects = _build_aspects(listing.get("item_specifics"))
    aspects = _resolve_required_aspects(category_id, aspects, item_type_hint)
    aspects = _enforce_single_cardinality(category_id, aspects)

    # Reorder so the tag close-up (2nd photo) becomes the last listing image, then
    # append the official brand/retailer product photo (if the identify step found
    # one) as a secondary image after the seller's real photos, re-hosted via
    # Cloudinary. Kept secondary — never the gallery cover — so the buyer always
    # leads with photos of the actual item, regardless of condition.
    image_paths = listing_photo_order(item)
    stock_url = (item.get("identification") or {}).get("product_image_url")
    stock_path = _download_brand_image(stock_url, item_id)
    if stock_path:
        image_paths.append(stock_path)
        print("🖼️ Draft: added official brand product photo as a secondary image")

    image_urls = upload_photos(image_paths)
    print(f"📤 Draft: {len(image_urls)} Cloudinary URL(s) returned from upload_photos")

    _ensure_location()
    fulfillment_policy_id = EBAY_FULFILLMENT_POLICY_ID or get_policy_id("fulfillment")
    return_policy_id = EBAY_RETURN_POLICY_ID or get_policy_id("return")

    sku = item_id
    condition = CONDITION_MAP.get(str(listing.get("condition_id")), "USED_GOOD")

    description_html = _html_description(listing["description"])
    product = _build_product(listing, image_urls, aspects, description_html)
    print(f"📤 Draft: {len(product['imageUrls'])} imageUrl(s) in the eBay inventory_item payload")

    # Stock comes from the item, not a literal. This function is also the REBUILD
    # path — /addphotos, /cover and /arrange all route back through it for an
    # already-published listing — so a hard-coded 1 silently dropped a listing
    # stocked with /setqty back to a single unit. The inventory_item PUT is a
    # full replace, and _create_offer's already-exists branch left the offer
    # advertising the old count, so eBay ended up holding 1 unit against an offer
    # for 5 while the DB (and the report, and the repricer) still said 5.
    #
    # A missing key means "never set" -> 1; a stored 0 is a real answer (an ended
    # or sold-out listing) and is preserved, since re-stocking it here would put
    # merchandise back on sale that /sync deliberately zeroed.
    stored_qty = (item.get("ebay") or {}).get("quantity")
    try:
        quantity = 1 if stored_qty is None else max(int(stored_qty), 0)
    except (TypeError, ValueError):
        quantity = 1

    inventory_item = {
        "product": product,
        "condition": condition,
        "availability": {"shipToLocationAvailability": {"quantity": quantity}},
    }
    _request("PUT", f"/sell/inventory/v1/inventory_item/{sku}", json=inventory_item)

    offer = {
        "sku": sku,
        "marketplaceId": EBAY_MARKETPLACE_ID,
        "format": "FIXED_PRICE",
        "availableQuantity": quantity,
        "categoryId": category_id,
        "listingDescription": description_html,
        "listingPolicies": {
            "fulfillmentPolicyId": fulfillment_policy_id,
            "returnPolicyId": return_policy_id,
        },
        "pricingSummary": {"price": {"value": str(listing["price"]), "currency": EBAY_CURRENCY}},
        "merchantLocationKey": EBAY_MERCHANT_LOCATION_KEY,
    }
    offer_id = _create_offer(offer)

    reselected = category_id != original_category_id
    return {
        "sku": sku,
        "offer_id": offer_id,
        "image_urls": image_urls,
        "category_id": category_id,
        "category_name": resolved_category_name,
        # Echoed back so the caller's merge keeps the DB and eBay agreeing on
        # stock. Without it a rebuild reported nothing about quantity and the
        # stored value drifted from what eBay actually holds.
        "quantity": quantity,
        "reselected_from": original_category_id if reselected else None,
        "reselected_from_name": category_name(original_category_id) if reselected else None,
    }
