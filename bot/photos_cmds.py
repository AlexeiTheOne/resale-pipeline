"""Photo commands: /photos, /cover, /arrange, /stockphoto, /photocheck, and
_resync_photos, which pushes an item's photo set to eBay.

Moved verbatim out of telegram_bot.py (docs/cleanup-plan.md Phase 2, step 4a).
"""
import asyncio
import re
import traceback
from pathlib import Path

from telegram import InputMediaPhoto, Update
from telegram.ext import ContextTypes

import db
from bot import formatting, state, util
from ebay import inventory, listings


async def _resync_photos(item_id: str, message) -> None:
    """Push the item's current photo set to eBay. No-op for items that haven't
    been drafted on eBay yet — the new photos are simply picked up when the draft
    is first created. For drafted/published items, rebuild the inventory item and
    re-publish so the change reaches the live listing."""
    item = db.get_item(item_id)
    if item is None:
        return
    status = item["status"]
    if status not in ("ebay_draft", "published"):
        await util._safe_reply(message, "They'll be included when you create the eBay draft (/retry or approve).")
        return

    await util._safe_reply(message, "🔄 Updating the eBay listing with the new photos...")
    try:
        result = await asyncio.to_thread(inventory.create_draft_offer, item_id)
    except inventory.TitleTooLongError as e:
        await util._safe_reply(message, "Photos saved, but the eBay listing was not updated.\n"
                          + formatting._title_too_long_text(e, item_id))
        return
    except Exception as e:
        traceback.print_exc()
        await util._safe_reply(message,
            f"⚠️ Photos saved, but updating the eBay offer failed: {type(e).__name__}: {str(e)[:250]}\n"
            "Use /retry to try again."
        )
        return

    # create_draft_offer returns fresh sku/offer_id/image_urls/quantity; merge so
    # we keep any listing_id / view_item_url already stored for a published item.
    #
    # Re-read first: `item` was loaded before create_draft_offer, which spends
    # many seconds uploading a photo per call and making several eBay round
    # trips. update_field replaces the whole column, so merging onto the stale
    # snapshot silently reverts anything written meanwhile — a sale recorded by
    # a concurrent /sync, most expensively.
    fresh = (db.get_item(item_id) or {}).get("ebay") or {}
    merged = {**fresh, **result}
    db.update_field(item_id, "ebay", merged)

    if status == "published":
        offer_id = merged.get("offer_id")
        try:
            await asyncio.to_thread(inventory.publish_offer, offer_id)
        except Exception as e:
            traceback.print_exc()
            await util._safe_reply(message,
                f"⚠️ Offer updated, but re-publishing the live listing failed: {type(e).__name__}: {str(e)[:250]}\n"
                "Use /activate to push it live."
            )
            return
        await util._safe_reply(message, f"✅ Live listing updated. {merged.get('view_item_url', '')}".strip())
    else:
        await util._safe_reply(message, "✅ eBay draft updated with the new photos. /activate when ready.")


async def _send_photo_index(item: dict, message) -> None:
    """Send the item's photos as albums, in listing order, with a numbered key.

    Telegram shows an album in order but puts no visible number on each frame, so
    the text index is what makes 'photo 3' unambiguous. Albums cap at 10, so a big
    set goes out in batches."""
    ordered = inventory.listing_photo_order(item)
    existing = [p for p in ordered if Path(p).exists()]
    missing = len(ordered) - len(existing)

    for start in range(0, len(existing), 10):
        batch = existing[start:start + 10]
        media = []
        for offset, path in enumerate(batch, start=start + 1):
            with open(path, "rb") as fh:
                media.append(InputMediaPhoto(fh.read(), caption=f"#{offset}"))
        try:
            await message.reply_media_group(media)
        except Exception as e:
            print(f"WARNING: media group failed: {type(e).__name__}: {e}")
            await util._safe_reply(message, f"⚠️ Couldn't send photos {start + 1}-{start + len(batch)}: "
                                       f"{type(e).__name__}")

    lines = [f"🖼️ {item['item_id'][:8]} — {len(existing)} photo(s), in listing order:",
             "  #1 is the gallery cover (the thumbnail buyers see in search)."]
    if missing:
        lines.append(f"  ⚠️ {missing} file(s) missing from disk and skipped.")
    if not inventory._order_is_fixed(item):
        lines.append("  Order is automatic: your 2nd photo (the tag) is moved to the end.")
    lines += ["", "  /cover <n> — make photo n the cover",
              "  /arrange <order> — reorder, e.g. /arrange 3,1,2"]
    await util._safe_reply(message, "\n".join(lines))


async def photos_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show an item's photos in listing order, numbered. Usage: /photos [id]."""
    user_id = update.effective_user.id
    item_id, err = util._resolve_item_id(user_id, context.args)
    if err:
        await util._safe_reply(update.message, f"⚠️ {err}")
        return
    item = db.get_item(item_id)
    if item is None:
        await util._safe_reply(update.message, f"No item found for {item_id[:8]}.")
        return
    state.last_item[user_id] = item_id
    if not (item.get("photos") or []):
        await util._safe_reply(update.message, f"{item_id[:8]} has no photos. /addphotos to add some.")
        return
    await _send_photo_index(item, update.message)


async def _apply_photo_order(item_id: str, new_order: list[str], message, note: str) -> None:
    """Persist a hand-set photo order and push it to eBay.

    Marks the layout manual so the builder stops applying its own reorder — the
    stored list is now exactly what the listing should show."""
    db.update_field(item_id, "photos", new_order)
    db.update_field(item_id, "photo_layout", {"manual": True})
    await util._safe_reply(message, note)
    await _resync_photos(item_id, message)


def _photo_args(user_id, args: list[str]) -> tuple[str | None, str | None, str | None]:
    """(item_id, spec, error) from '<id> <spec>' or just '<spec>' — the trailing
    argument is the instruction, anything before it names the item (same shape as
    /promote, so the id stays optional)."""
    if not args:
        return None, None, "missing argument"
    item_id, err = util._resolve_item_id(user_id, args[:-1])
    if err:
        return None, None, err
    return item_id, args[-1], None


async def cover_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Make one photo the gallery cover. Usage: /cover <n> or /cover <id> <n>,
    where n is the number shown by /photos."""
    user_id = update.effective_user.id
    item_id, spec, err = _photo_args(user_id, list(context.args or []))
    if err:
        await util._safe_reply(update.message,
            f"⚠️ {err}\nUsage: /cover <n>  (see /photos for the numbers)")
        return

    item = db.get_item(item_id)
    if item is None:
        await util._safe_reply(update.message, f"No item found for {item_id[:8]}.")
        return
    ordered = inventory.listing_photo_order(item)
    try:
        n = int(spec)
    except ValueError:
        await util._safe_reply(update.message, f"'{spec}' isn't a photo number. Usage: /cover 3")
        return
    if not 1 <= n <= len(ordered):
        await util._safe_reply(update.message,
            f"{item_id[:8]} has {len(ordered)} photo(s), so pick 1-{len(ordered)}.")
        return

    state.last_item[user_id] = item_id
    if n == 1:
        await util._safe_reply(update.message, f"Photo #1 is already the cover for {item_id[:8]}.")
        return
    chosen = ordered[n - 1]
    await _apply_photo_order(
        item_id, [chosen] + [p for p in ordered if p != chosen], update.message,
        f"🖼️ Photo #{n} is now the cover for {item_id[:8]}.")


async def arrange_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Reorder an item's photos. Usage: /arrange 3,1,2 or /arrange <id> 3,1,2.

    Numbers are the ones /photos shows. Any photo you leave out keeps its
    relative position at the end, so /arrange 4 just promotes photo 4 to the
    front and leaves the rest alone."""
    user_id = update.effective_user.id
    item_id, spec, err = _photo_args(user_id, list(context.args or []))
    if err:
        await util._safe_reply(update.message,
            f"⚠️ {err}\nUsage: /arrange 3,1,2  (see /photos for the numbers)")
        return

    item = db.get_item(item_id)
    if item is None:
        await util._safe_reply(update.message, f"No item found for {item_id[:8]}.")
        return
    ordered = inventory.listing_photo_order(item)

    picks, seen = [], set()
    for token in re.split(r"[,\s]+", spec.strip()):
        if not token:
            continue
        try:
            n = int(token)
        except ValueError:
            await util._safe_reply(update.message,
                f"'{token}' isn't a photo number. Usage: /arrange 3,1,2")
            return
        if not 1 <= n <= len(ordered):
            await util._safe_reply(update.message,
                f"{item_id[:8]} has {len(ordered)} photo(s), so pick 1-{len(ordered)} (got {n}).")
            return
        if n in seen:
            await util._safe_reply(update.message, f"Photo {n} is listed twice — each one once, please.")
            return
        seen.add(n)
        picks.append(ordered[n - 1])

    if not picks:
        await util._safe_reply(update.message, "Usage: /arrange 3,1,2  (see /photos for the numbers)")
        return

    state.last_item[user_id] = item_id
    # Anything not named keeps its current relative order, appended after the
    # photos that were — so a partial spec is a promotion, not a truncation.
    rest = [p for p in ordered if p not in picks]
    await _apply_photo_order(
        item_id, picks + rest, update.message,
        f"🖼️ Reordered {item_id[:8]} — photo #{picks and ordered.index(picks[0]) + 1} is now the cover"
        + (f", {len(rest)} unlisted photo(s) kept at the end." if rest else "."))


async def stockphoto_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Add a retailer's product photo from a URL. Usage: /stockphoto <url> or
    /stockphoto <id> <url>.

    For anything sealed in retail packaging — bedding above all — every photo we
    can take is a plastic bag with a branded band, and the buyer never sees the
    pattern they are buying. The retailer's styled shot is the only way to show
    it, and it lands at #2, right after the real cover.

    The URL comes from you because the retailers who own these photos block
    automated fetching, and because you are the one holding the item: a wrong
    colourway is worse than no photo at all."""
    user_id = update.effective_user.id
    item_id, url, err = _photo_args(user_id, list(context.args or []))
    if err:
        await util._safe_reply(update.message,
            f"⚠️ {err}\nUsage: /stockphoto <url>  (right-click the photo → Copy image address)")
        return
    if not url.lower().startswith(("http://", "https://")):
        await util._safe_reply(update.message,
            f"'{url[:60]}' isn't a URL. It needs to be the direct image address "
            "(right-click the photo → Copy image address), not the product page.")
        return

    state.last_item[user_id] = item_id
    await util._safe_reply(update.message, "⬇️ Fetching that image...")
    try:
        await asyncio.to_thread(inventory.save_stock_photo, item_id, url)
    except ValueError as e:
        await util._safe_reply(update.message, f"⚠️ {e}")
        return
    except Exception as e:
        traceback.print_exc()
        await util._safe_reply(update.message, f"⚠️ Couldn't save it: {type(e).__name__}: {str(e)[:200]}")
        return

    n = len(inventory.listing_photo_order(db.get_item(item_id) or {}))
    await util._safe_reply(update.message,
        f"🖼️ Added as photo #2 of {n} for {item_id[:8]}. /photos to see them, "
        "/arrange to move it.")
    await _resync_photos(item_id, update.message)


async def photocheck_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Check that every live listing still shows every photo we uploaded for it.

    A listing that quietly drops to one photo is invisible everywhere else: it
    keeps accruing impressions, so the traffic report shows a slow CTR decay
    rather than a fault, and nothing in /status or /report looks at images at
    all. This is the only thing that would catch it.

    Deliberately reads the LIVE listing (Trading GetItem), not the Inventory
    API's inventory_item — see ebay/listings.audit_listing_photos for why that
    field cannot be used for this.

    Usage: /photocheck
    """
    items = [i for i in db.list_items() if (i.get("ebay") or {}).get("listing_id")]
    if not items:
        await util._safe_reply(update.message, "No live listings to check.")
        return

    await util._safe_reply(update.message,
                      f"🖼️ Checking photos on {len(items)} live listing(s)... "
                      "one eBay call each, so give it a minute.")
    try:
        short = await asyncio.to_thread(listings.audit_listing_photos, items)
    except Exception as e:
        util._record_error("photocheck", e)
        await util._safe_reply(update.message, f"⚠️ Photo check failed: {type(e).__name__}: {str(e)[:200]}")
        return

    if not short:
        await util._safe_reply(update.message,
                          f"✅ All {len(items)} live listing(s) show every photo we hold. "
                          "Nothing missing.")
        return

    lines = [f"⚠️ {len(short)} listing(s) are showing fewer photos than we uploaded:", ""]
    for f in short:
        lines.append(f"{f['item_id'][:8]} — {f['live']}/{f['expected']} photos "
                     f"({f['expected'] - f['live']} missing)\n   {f['title'][:60]}")
    lines += ["", "The originals are still stored, so this is repairable: "
              "/arrange or /cover on an item rebuilds its offer and re-sends every photo."]
    await util._send_chunked(update.message, lines)
