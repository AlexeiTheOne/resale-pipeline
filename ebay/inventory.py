import hashlib
import html
import json
import os
import re
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


def _reorder_for_listing(paths: list[str]) -> list[str]:
    """Move the tag close-up (always the 2nd photo the user sends) to the end so
    it shows as the last image in the eBay listing — buyers want to lead with the
    product, not the tag. Leaves the first (overview) photo as the gallery cover."""
    if len(paths) >= 2:
        return paths[:1] + paths[2:] + [paths[1]]
    return paths


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


def _is_heading(line: str) -> bool:
    """An all-caps line (e.g. 'SPECIFICATIONS:', 'ABOUT US:') is a section header."""
    stripped = line.rstrip(":").strip()
    return len(stripped) >= 2 and any(c.isalpha() for c in stripped) and stripped == stripped.upper()


def _html_description(text: str) -> str:
    """eBay renders descriptions as HTML and collapses plain-text line breaks, so a
    nicely laid-out plain description arrives as one unformatted blob. Convert the
    generated text — all-caps headings and '-'/'✓' bullet lines — into simple, valid
    HTML so the published listing keeps its structure."""
    blocks: list[str] = []
    bullets: list[str] = []

    def flush_bullets() -> None:
        if bullets:
            items = "".join(f"<li>{html.escape(b)}</li>" for b in bullets)
            blocks.append(f"<ul>{items}</ul>")
            bullets.clear()

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            flush_bullets()
            continue
        marker = next((p for p in _BULLET_PREFIXES if line.startswith(p)), None)
        if marker:
            bullets.append(line[len(marker):].strip())
        elif _is_heading(line):
            flush_bullets()
            blocks.append(f"<h3>{html.escape(line.rstrip(':').strip())}</h3>")
        else:
            flush_bullets()
            blocks.append(f"<p>{html.escape(line)}</p>")
    flush_bullets()

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
    image_paths = _reorder_for_listing(list(photos))
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

    inventory_item = {
        "product": product,
        "condition": condition,
        "availability": {"shipToLocationAvailability": {"quantity": 1}},
    }
    _request("PUT", f"/sell/inventory/v1/inventory_item/{sku}", json=inventory_item)

    offer = {
        "sku": sku,
        "marketplaceId": EBAY_MARKETPLACE_ID,
        "format": "FIXED_PRICE",
        "availableQuantity": 1,
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
        "reselected_from": original_category_id if reselected else None,
        "reselected_from_name": category_name(original_category_id) if reselected else None,
    }
