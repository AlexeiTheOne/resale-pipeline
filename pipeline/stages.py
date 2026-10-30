"""Pipeline stages: identify -> price -> draft, plus the Ross-tag peel and the
advisory duplicate / brand-saturation warnings shown after identification.

Moved verbatim out of telegram_bot.py (docs/cleanup-plan.md Phase 2). These run
in a worker thread and reach the chat only through the `notify` callback the bot
passes in; nothing here imports telegram_bot or bot.*.
"""
import re
import time

import config
import db
import identify
import ross_tag
from config import GEMINI_PHOTO_LIMIT
# `as draft_mod`: stage_draft has a local named `draft`.
from pipeline import draft as draft_mod
from pipeline import listing_check, price
from pipeline.gtin import product_gtin


def find_tag_photo(paths: list[str]) -> tuple[int, str] | None:
    """(index, how) of the Ross price-tag photo, or None to fall back on position.

    Two tiers of evidence, strongest first:

      1. barcode — the photo whose 18-digit Ross CODE128 decodes, wherever it
         sits. Exact, but only about 41% of real tag photos decode (glare,
         angle, a crease through the bars, Telegram's compression).
      2. visual — ross_tag.detect_ross_tags, which recognises the tag's
         appearance. Measured 21/21 with no false positives on the real library,
         and it returns an empty set on any failure, so the positional default
         below stays as the last tier.

    OCR text was tried as evidence and dropped: the patterns that survive on a
    compressed tag ("a 12-digit number", "MSRP", "compare at") are equally
    present on manufacturer labels, and matching them picked a Ralph Lauren
    package label over the real tag on 8 items.

    Tier 2 matters because the caller's fallback is "peel the LAST photo", which
    is only the tag under the shooting convention. Append photos after the tag
    with /addphotos and the last photo is an innocent product shot — so that one
    gets deleted and OCR'd as a receipt while the tag, showing what you paid,
    stays in `photos` and goes up on eBay.

    Both tiers scan from the END, since the tag is normally shot last."""
    for idx in range(len(paths) - 1, -1, -1):
        try:
            digits = ross_tag.decode_barcode(paths[idx])
        except Exception:
            continue
        if digits and len(digits) == 18:
            return idx, "barcode"

    try:
        visual = ross_tag.detect_ross_tags(paths)
    except Exception:
        visual = set()
    if visual:
        # Highest index — keeps the shooting convention when several match.
        return max(visual), "visual"
    return None


def split_receipt(item_id, paths, notify=lambda _t: None) -> list[str]:
    """Peel the Ross tag off the listing photos, read the paid price + 12-digit
    code off it, and keep it out of `photos` so it never reaches eBay. Idempotent:
    if the receipt was already processed for this item, returns the stored photos.

    Which photo is the tag:
      1. Whichever one's 18-digit CODE128 decodes, wherever it sits. This catches
         the case where the tag isn't last.
      2. Otherwise the LAST photo — the shooting convention, and the right default.

    The fallback in (2) is deliberate and must stay. Only about 41% of real tag
    photos decode (glare, angle, Telegram's compression), and OCR of the rest is
    mostly noise, so detection alone cannot decide this. Refusing to peel when
    nothing is recognised was tried and is far worse: it leaves the Ross tag —
    showing what we paid — sitting in the eBay listing. A tag that can't be read
    still gets peeled; the user is simply asked for the price."""
    item = db.get_item(item_id)
    receipt = (item or {}).get("receipt") or {}
    # Guard on a marker only THIS function writes, not on the mere presence of a
    # receipt. /receipt writes a receipt by hand without touching `photos`, so
    # "has a receipt" made a manually-priced item skip the peel entirely and
    # publish its tag.
    #
    # A receipt with no marker predates the marker, and was therefore written by
    # this function back when it always peeled — treat it as peeled. Re-peeling
    # those would silently delete a real product photo, which is much worse than
    # the leak this guard exists to catch.
    if receipt and receipt.get("peeled", True):
        return item.get("photos") or []
    if len(paths) < 2:
        return list(paths)  # nothing to peel — no receipt to separate

    found = find_tag_photo(paths)
    tag_idx = found[0] if found else len(paths) - 1
    if found and tag_idx != len(paths) - 1:
        how = "barcode" if found[1] == "barcode" else "the tag's appearance"
        notify(f"🧾 Ross tag found at photo {tag_idx + 1} of {len(paths)} by {how}, "
               f"not the last one — peeling that one instead.")
    listing_photos = [p for idx, p in enumerate(paths) if idx != tag_idx]
    data = ross_tag.extract_receipt(paths[tag_idx])
    # Keep whatever a manual /receipt already established — the operator read the
    # tag with their own eyes, which beats OCR of a photo that defeated it.
    for field in ("reduced_price", "code", "original_price"):
        if receipt.get(field) is not None and data.get(field) is None:
            data[field] = receipt[field]
    data["peeled"] = True
    db.update_field(item_id, "photos", listing_photos)
    db.update_field(item_id, "receipt", data)
    if tag_idx == 1 and not (item.get("photo_layout") or {}).get("manual"):
        # The 2nd photo was the Ross tag, not a brand-tag close-up. Without this
        # the listing builder still moves "the 2nd photo" to the back — which is
        # now whatever product shot slid into that slot.
        db.update_field(item_id, "photo_layout", {"as_sent": True})
    if data.get("code") and data.get("reduced_price") is not None:
        orig = f" (orig ${data['original_price']})" if data.get("original_price") else ""
        notify(f"🧾 Receipt: paid ${data['reduced_price']}{orig} · code {data['code']}")
    else:
        # No barcode and OCR couldn't recover both fields — tell the user exactly
        # what's missing and how to fill it in manually for this item.
        got = []
        if data.get("reduced_price") is not None:
            got.append(f"paid ${data['reduced_price']}")
        if data.get("code"):
            got.append(f"code {data['code']}")
        detail = ": got " + ", ".join(got) if got else (f" ({data['error']})" if data.get("error") else "")
        notify(f"🧾 Couldn't fully read the tag{detail}. "
               f"Add it manually: /receipt {item_id[:8]} <price> [code]  "
               "(code optional — leave it off to auto-fill)")
    return listing_photos


# --- Pipeline stages (each pauses at a confirm gate; see the _run_*_and_gate
# orchestrators in telegram_bot.py). The trailing `notify` arg is supplied by
# _run_stage. ---

def stage_identify(item_id, paths, notify=lambda _t: None) -> dict:
    # The last photo is always the Ross receipt: OCR it for cost + code and
    # remove it from the listing photos (it must not be posted to eBay).
    listing_photos = split_receipt(item_id, paths, notify)
    # Only the first N photos (overview + tag) go to the paid API; all listing
    # photos remain on the item for the eBay listing.
    id_photos = listing_photos[:GEMINI_PHOTO_LIMIT]
    notify("🔬 Identifying the item...")
    print(f"🔬 Identifying from {len(id_photos)} of {len(listing_photos)} listing photo(s)")
    t = time.perf_counter()
    # Send the first few photos to the model, but scan ALL of them for the tag
    # barcode (decoding is free and the tag isn't always in the first few).
    ident = identify.identify_item(id_photos, scan_paths=listing_photos)
    print(f"✓ Identified in {time.perf_counter() - t:.0f}s:", ident.get("brand"),
          ident.get("product_name") or ident.get("item_type"))
    db.update_field(item_id, "identification", ident)
    db.update_status(item_id, "identified")
    for line in duplicate_warnings(item_id) + brand_warnings(item_id):
        notify(line)
    return ident


def brand_warnings(item_id: str) -> list[str]:
    """At most one line: this brand is already backed up (more than 8 live) or
    scores pause/stop on the scoreboard. Ed Hardy kept getting bought in
    October with 40 listings already sitting. Advisory only; never raises."""
    try:
        from pipeline.scoreboard import saturation_warning
        line = saturation_warning(db.get_item(item_id) or {})
    except Exception as e:
        print(f"WARNING: brand scoreboard check failed: {type(e).__name__}: {e}")
        return []
    return [line] if line else []


def same_item_keys(item: dict) -> set[tuple[str, str]]:
    """What makes two items the same product: the UPC, or brand + model.
    Not the Ross tag code — Ross reuses one code across different products
    (two different Michael Kors watch bands share one), so it false-alarms."""
    keys = set()
    ident = item.get("identification") or {}
    gtin = product_gtin(ident.get("upc"))
    if gtin:
        keys.add(("UPC", gtin))
    brand = re.sub(r"\W", "", str(ident.get("brand") or "")).lower()
    model = re.sub(r"\W", "", str(ident.get("model") or "")).lower()
    if brand and len(model) >= 4:
        keys.add(("model", f"{brand}:{model}"))
    return keys


def duplicate_warnings(item_id: str) -> list[str]:
    """Warn — never block — when this item matches one already in the shop.
    Buying two of the same thing is fine; listing one bag twice is not, and on
    2026-10-27 a haul nearly did exactly that with a Luv Betsey backpack."""
    item = db.get_item(item_id) or {}
    mine = same_item_keys(item)
    if not mine:
        return []
    lines = []
    confirmed_different = db.not_duplicate_pairs()
    for other in db.list_items():
        if other["item_id"] == item_id or other.get("status") in ("rejected", "failed"):
            continue
        if tuple(sorted((item_id, other["item_id"]))) in confirmed_different:
            continue
        shared = mine & same_item_keys(other)
        if not shared:
            continue
        how = ", ".join(sorted(k for k, _ in shared))
        title = ((other.get("listing") or {}).get("title")
                 or (other.get("identification") or {}).get("product_name") or "")[:60]
        url = (other.get("ebay") or {}).get("view_item_url") or ""
        lines.append(f"⚠️ Possible duplicate — same {how} as {other['item_id'][:8]} "
                     f"({other.get('status')}): {title} {url}".rstrip())
    if lines:
        lines.append("If it's a second unit, carry on (or bump that listing's quantity "
                     f"instead); if it's a different item, /notdup {item_id[:8]} <other id>. "
                     f"Otherwise /delete {item_id[:8]}.")
    return lines


def stage_price(item_id, notify=lambda _t: None) -> dict:
    ident = db.get_item(item_id)["identification"]
    notify("💰 Pricing...")
    t = time.perf_counter()
    if config.PRICING_ENGINE == "v2":
        # The default since 17 Oct 2026 (config.PRICING_ENGINE); same return
        # shape as v1, which stays selectable with config.PRICING_ENGINE=v1.
        from pipeline.pricing_v2 import get_pricing_v2
        item = db.get_item(item_id)
        pricing = get_pricing_v2(ident, photo_paths=item.get("photos") or [],
                                 receipt=item.get("receipt"), item_id=item_id)
    else:
        pricing = price.get_pricing(ident["search_query"], research=ident)
    print(f"✓ Priced in {time.perf_counter() - t:.0f}s:", pricing.get("suggested_price"),
          f"({pricing.get('confidence')})")
    db.update_field(item_id, "pricing", pricing)
    if pricing.get("price_source_url"):
        db.update_field(item_id, "price_source_url", pricing["price_source_url"])
    db.update_status(item_id, "priced")
    return pricing


def stage_draft(item_id, notify=lambda _t: None) -> dict:
    item = db.get_item(item_id)
    notify("✍️ Writing the listing...")
    t = time.perf_counter()
    draft = draft_mod.generate_draft(item["identification"], item["pricing"])
    print(f"✓ Draft generated in {time.perf_counter() - t:.0f}s")
    db.update_field(item_id, "listing", draft)
    db.update_status(item_id, "drafted")
    if config.LISTING_CHECK_ENABLED:
        notify("🔎 Checking the listing...")
        listing_check.check_and_store(item_id)  # advisory; never raises
    db.update_status(item_id, "review")
    return draft
