import os, sys, uuid, asyncio, shutil, time, traceback
from pathlib import Path
from dotenv import load_dotenv
import httpx
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)
from telegram.request import HTTPXRequest

load_dotenv()
sys.path.insert(0, str(Path(__file__).parent))

from config import GEMINI_PHOTO_LIMIT
from db import create_item, delete_item, get_item, list_items, update_field, update_status
from identify import identify_item
from receipt import extract_receipt
from pipeline.price import get_pricing
from pipeline.draft import generate_draft, revise_draft
from ebay.auth import get_access_token
from ebay.inventory import (
    create_draft_offer,
    delete_offer,
    get_policy_id,
    publish_offer,
    MissingRequiredAspectsError,
)

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
INBOX = Path("data/inbox")
INBOX.mkdir(parents=True, exist_ok=True)

DEFAULT_CAPTURE_WINDOW = 5    # seconds to wait for more photos before processing
EXTENDED_CAPTURE_WINDOW = 30  # after the user types "wait" — for forwarding big batches
WAIT_EXTENSION = 15           # seconds the inline "Wait" button waits for more photos

pending = {}        # user_id -> {"files": [bytearray,...], "task": asyncio.Task}
review = {}         # user_id -> item_id  (item currently awaiting this user's reply)
locks = {}          # user_id -> asyncio.Lock guarding pending[user_id]
last_item = {}      # user_id -> item_id  (last-touched item, survives past the review window)
capture_window = {} # user_id -> seconds to batch photos (default DEFAULT_CAPTURE_WINDOW)
staging = {}        # user_id -> {item_id, folder, paths, chat_id, task} awaiting Start/Wait/Cancel


def _lock_for(user_id):
    if user_id not in locks:
        locks[user_id] = asyncio.Lock()
    return locks[user_id]


async def _safe_reply(message, text: str, **kwargs) -> None:
    """Best-effort status ping. A transient network failure sending this
    message must never abort the actual work that follows it."""
    try:
        await message.reply_text(text, **kwargs)
    except Exception as e:
        print(f"WARNING: reply_text failed (continuing anyway): {type(e).__name__}: {e}")


def _resolve_item_id(user_id: int, args: list[str]) -> tuple[str | None, str | None]:
    """Resolve an item_id from a command's args (full id or unique prefix),
    falling back to last_item[user_id] when no arg is given. Returns
    (item_id, error_message) — exactly one of which is None."""
    if args:
        arg = args[0]
        if get_item(arg) is not None:
            return arg, None
        matches = [i["item_id"] for i in list_items() if i["item_id"].startswith(arg)]
        if len(matches) == 1:
            return matches[0], None
        if len(matches) > 1:
            return None, f"'{arg}' matches {len(matches)} items — use a longer prefix."
        return None, f"No item found matching '{arg}'."

    item_id = last_item.get(user_id)
    if item_id is None:
        return None, "No item specified and no recent item to fall back to. Provide an item_id."
    return item_id, None


async def photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    photo = update.message.photo[-1]
    f = await context.bot.get_file(photo.file_id)
    data = await f.download_as_bytearray()

    async with _lock_for(user_id):
        st = staging.get(user_id)
        if st is not None:
            # Item is already awaiting confirmation — this photo belongs to it.
            # Append it, then re-arm the confirmation once photos stop arriving.
            idx = len(st["paths"])
            p = st["folder"] / f"{st['folder'].name}_{idx}.jpg"
            p.write_bytes(data)
            st["paths"].append(str(p))
            update_field(st["item_id"], "photos", st["paths"])
            if st["task"] is not None:
                st["task"].cancel()
            st["task"] = context.application.create_task(
                _await_more_photos(user_id, st["item_id"], context, WAIT_EXTENSION)
            )
            count = len(st["paths"])
        else:
            if user_id not in pending:
                pending[user_id] = {"files": [], "task": None}
            pending[user_id]["files"].append(data)
            count = len(pending[user_id]["files"])

            if pending[user_id]["task"] is not None:
                pending[user_id]["task"].cancel()

            pending[user_id]["task"] = context.application.create_task(
                finalize_capture(user_id, update, context)
            )
    print(f"📷 Capture: photo appended, {count} buffered for user {user_id}")


async def finalize_capture(user_id, update, context):
    window = capture_window.get(user_id, DEFAULT_CAPTURE_WINDOW)
    await asyncio.sleep(window)
    async with _lock_for(user_id):
        files = pending.pop(user_id)["files"]
        capture_window.pop(user_id, None)  # reset to default for the next batch
    print(f"📷 Capture: {len(files)} photo(s) collected from buffer (window {window}s)")

    staging_id = str(uuid.uuid4())
    folder = INBOX / staging_id
    folder.mkdir(parents=True, exist_ok=True)

    paths = []
    for i, b in enumerate(files):
        p = folder / f"{staging_id}_{i}.jpg"
        p.write_bytes(b)
        paths.append(str(p))
    print(f"📷 Capture: {len(paths)} photo(s) written to disk in {folder}")

    item_id = create_item(paths)
    print(f"📷 Capture: item {item_id} created with {len(paths)} photo path(s) in DB")

    # Don't auto-run. Hold the item and ask the user what to do with it.
    chat_id = update.effective_chat.id
    async with _lock_for(user_id):
        staging[user_id] = {
            "item_id": item_id, "folder": folder, "paths": paths,
            "chat_id": chat_id, "task": None,
        }
    await _send_confirmation(context, chat_id, item_id, len(paths))


def _confirmation_markup(item_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("Start", callback_data=f"confirm:start:{item_id}"),
        InlineKeyboardButton("Wait", callback_data=f"confirm:wait:{item_id}"),
        InlineKeyboardButton("Cancel", callback_data=f"confirm:cancel:{item_id}"),
    ]])


async def _send_confirmation(context, chat_id, item_id, count) -> None:
    text = f"{count} photo(s) buffered. Start the listing, wait for more photos, or cancel?"
    try:
        await context.bot.send_message(
            chat_id=chat_id, text=text, reply_markup=_confirmation_markup(item_id)
        )
    except Exception as e:
        print(f"WARNING: send confirmation failed: {type(e).__name__}: {e}")


async def _await_more_photos(user_id, item_id, context, delay) -> None:
    """After the Wait window, re-show the confirmation if the item is still staged
    and no newer photo restarted the timer."""
    await asyncio.sleep(delay)
    async with _lock_for(user_id):
        st = staging.get(user_id)
        if st is None or st["item_id"] != item_id:
            return
        st["task"] = None
        count, chat_id = len(st["paths"]), st["chat_id"]
    await _send_confirmation(context, chat_id, item_id, count)


async def _run_and_review(user_id, item_id, paths, message, context) -> None:
    # run_pipeline runs in a worker thread; this schedules each progress ping back
    # onto the bot's event loop so the user sees which step is running (some steps
    # — identification, Apify pricing — take a minute or more).
    loop = asyncio.get_running_loop()

    def notify(text: str) -> None:
        asyncio.run_coroutine_threadsafe(_safe_reply(message, text), loop)

    try:
        result = await asyncio.to_thread(run_pipeline, item_id, paths, notify)
    except Exception as e:
        error_details = traceback.format_exc()
        print("PIPELINE ERROR:", error_details)
        await _safe_reply(message, f"⚠️ Pipeline error at step: {type(e).__name__}: {str(e)[:200]}")
        return

    review[user_id] = item_id
    last_item[user_id] = item_id
    await _safe_reply(message, format_draft(result), parse_mode=None)


async def confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    try:
        _, action, item_id = query.data.split(":", 2)
    except ValueError:
        return

    if action == "start":
        async with _lock_for(user_id):
            st = staging.pop(user_id, None)
            if st and st["task"] is not None:
                st["task"].cancel()
        if st is None or st["item_id"] != item_id:
            await query.edit_message_text("This item is no longer waiting.")
            return
        paths = st["paths"]
        await query.edit_message_text(f"Starting. {len(paths)} photo(s). Processing...")
        await _run_and_review(user_id, item_id, paths, query.message, context)

    elif action == "wait":
        async with _lock_for(user_id):
            st = staging.get(user_id)
            if st is None or st["item_id"] != item_id:
                await query.edit_message_text("This item is no longer waiting.")
                return
            if st["task"] is not None:
                st["task"].cancel()
            st["task"] = context.application.create_task(
                _await_more_photos(user_id, item_id, context, WAIT_EXTENSION)
            )
        await query.edit_message_text(f"Waiting {WAIT_EXTENSION}s for more photos. Send them now.")

    elif action == "cancel":
        async with _lock_for(user_id):
            st = staging.pop(user_id, None)
            if st and st["task"] is not None:
                st["task"].cancel()
        item = get_item(item_id)
        if item is not None:
            photos = item.get("photos") or []
            if photos:
                shutil.rmtree(Path(photos[0]).parent, ignore_errors=True)
            delete_item(item_id)
        await query.edit_message_text("Cancelled. Photos discarded.")


def _split_receipt(item_id, paths, notify=lambda _t: None) -> list[str]:
    """Peel the last photo — always the Ross receipt — off the listing photos,
    OCR it for the paid price + 12-digit code, and persist both. The receipt is
    dropped from `photos` so it never reaches eBay. Idempotent: if the receipt
    was already processed for this item, returns the stored listing photos."""
    item = get_item(item_id)
    if item and item.get("receipt"):
        return item.get("photos") or []
    if len(paths) < 2:
        return list(paths)  # nothing to peel — no receipt to separate

    listing_photos = list(paths[:-1])
    data = extract_receipt(paths[-1])
    update_field(item_id, "photos", listing_photos)
    update_field(item_id, "receipt", data)
    if data.get("error"):
        notify(f"🧾 Receipt saved, but OCR failed: {data['error']}")
    else:
        notify(f"🧾 Receipt: paid ${data.get('reduced_price')} · code {data.get('code')}")
    return listing_photos


def run_pipeline(item_id, paths, notify=lambda _t: None) -> dict:
    try:
        # The last photo is always the Ross receipt: OCR it for cost + code and
        # remove it from the listing photos (it must not be posted to eBay).
        listing_photos = _split_receipt(item_id, paths, notify)
        # Only the first N photos (overview + tag) go to the paid API; all listing
        # photos remain on the item for the eBay listing.
        id_photos = listing_photos[:GEMINI_PHOTO_LIMIT]
        notify("🔬 Identifying the item...")
        print(f"🔬 Identifying from {len(id_photos)} of {len(listing_photos)} listing photo(s)")
        t = time.perf_counter()
        ident = identify_item(id_photos)
        id_secs = time.perf_counter() - t
        update_field(item_id, "identification", ident)
        update_status(item_id, "identified")
        name = ident.get("product_name") or ident.get("item_type")
        print(f"✓ Identified in {id_secs:.0f}s:", ident.get("brand"), name)

        notify(f"✓ Identified ({id_secs:.0f}s): {ident.get('brand')} {name}\n💰 Pricing...")
        t = time.perf_counter()
        pricing = get_pricing(ident["search_query"], research=ident)
        price_secs = time.perf_counter() - t
        update_field(item_id, "pricing", pricing)
        if pricing.get("price_source_url"):
            update_field(item_id, "price_source_url", pricing["price_source_url"])
        update_status(item_id, "priced")
        print(f"✓ Priced in {price_secs:.0f}s:", pricing.get("suggested_price"), f"({pricing.get('confidence')})")

        notify(f"✓ Priced ({price_secs:.0f}s): ${pricing.get('suggested_price')}\n✍️ Writing the listing...")
        t = time.perf_counter()
        draft = generate_draft(ident, pricing)
        draft_secs = time.perf_counter() - t
        update_field(item_id, "listing", draft)
        update_status(item_id, "drafted")
        update_status(item_id, "review")
        print(f"✓ Draft generated in {draft_secs:.0f}s")

        return {"identification": ident, "pricing": pricing, "listing": draft}
    except Exception as e:
        print("PIPELINE TRACEBACK:")
        traceback.print_exc()
        raise


def format_draft(result) -> str:
    ident = result["identification"]
    pricing = result["pricing"]
    listing = result["listing"]

    desc = listing.get("description", "")
    short_desc = desc[:300] + ("..." if len(desc) > 300 else "")

    lines = [
        f"✓ {ident['brand']} {ident['model']}",
        f"Confidence: {ident['confidence']}",
    ]

    if ident.get("condition_flags"):
        lines.append(f"⚠️ FLAGS: {ident['condition_flags']}")

    lines += [
        "",
        f"TITLE ({len(listing['title'])} chars):",
        listing["title"],
        "",
        f"PRICE: ${listing['price']}",
        f"  sold median: ${pricing.get('sold_median')} | active floor: ${pricing.get('active_floor')} | {pricing.get('confidence')}",
    ]

    if pricing.get("price_source_url"):
        lines.append(f"  price source: {pricing['price_source_url']}")

    lines += [
        "",
        "DESCRIPTION:",
        short_desc,
        "",
        "Reply: 'approve', 'reject', or type a correction (e.g. 'color is yellow not orange')",
    ]

    return "\n".join(lines)


async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    text = update.message.text.strip()
    low = text.lower()

    if low in ("wait", "hold", "hold on", "⏳"):
        capture_window[user_id] = EXTENDED_CAPTURE_WINDOW
        # If a batch is already counting down on the short window, restart its
        # timer so the longer window takes effect right away.
        async with _lock_for(user_id):
            pend = pending.get(user_id)
            if pend is not None:
                if pend["task"] is not None:
                    pend["task"].cancel()
                pend["task"] = context.application.create_task(
                    finalize_capture(user_id, update, context)
                )
        await _safe_reply(
            update.message,
            f"⏳ OK — holding for {EXTENDED_CAPTURE_WINDOW}s. Send all your photos now.",
        )
        return

    if user_id not in review:
        await _safe_reply(update.message, "Send me photos of an item to start.")
        return

    item_id = review[user_id]

    if low in ("approve", "✅", "yes", "ok"):
        update_status(item_id, "approved")
        review.pop(user_id, None)
        await _safe_reply(update.message, "✅ Approved. Creating eBay draft...")

        try:
            ebay_result = await asyncio.to_thread(create_draft_offer, item_id)
        except MissingRequiredAspectsError as e:
            lines = [
                f"⚠️ Can't create the draft yet — eBay category {e.category_id} requires these "
                "item specifics and I couldn't safely infer a value:"
            ]
            for m in e.missing:
                opts = f" (allowed: {', '.join(m['allowed_values'])})" if m["allowed_values"] else ""
                lines.append(f"  • {m['name']}{opts}")
            lines.append("The item is saved as 'approved' — fix the listing and retry manually.")
            await _safe_reply(update.message, "\n".join(lines))
            return
        except Exception as e:
            traceback.print_exc()
            await _safe_reply(update.message, 
                f"⚠️ Approved, but eBay draft creation failed: {type(e).__name__}: {str(e)[:300]}\n"
                "The item is saved as 'approved' — fix the issue and retry manually."
            )
            return

        update_field(item_id, "ebay", ebay_result)
        update_status(item_id, "ebay_draft")

        msg = f"📝 Draft created on eBay (SKU {ebay_result['sku']}, offer {ebay_result['offer_id']})."
        msg += "\nUse /listing to review it and /activate to publish it when ready — it may not show up in Seller Hub's Drafts UI (API-created offers often don't)."
        if ebay_result.get("reselected_from"):
            def _label(name, cid):
                return f"{name} ({cid})" if name else str(cid)
            new_label = _label(ebay_result.get("category_name"), ebay_result["category_id"])
            old_label = _label(ebay_result.get("reselected_from_name"), ebay_result["reselected_from"])
            msg += f"\n⚠️ Listed under category {new_label} (auto-corrected from {old_label})"
        await _safe_reply(update.message, msg)
        return

    if low in ("reject", "❌", "no"):
        update_status(item_id, "rejected")
        review.pop(user_id, None)
        await _safe_reply(update.message, "❌ Rejected.")
        return

    item = get_item(item_id)
    current = item["listing"]
    await _safe_reply(update.message, "✏️ Applying your correction...")
    try:
        revised = await asyncio.to_thread(revise_draft, current, text)
    except Exception as e:
        await _safe_reply(update.message, f"⚠️ Could not revise: {e}")
        return

    update_field(item_id, "listing", revised)
    result = {
        "identification": item["identification"],
        "pricing": item["pricing"],
        "listing": revised,
    }
    await _safe_reply(update.message, format_draft(result))


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    items = list_items()
    if not items:
        await _safe_reply(update.message, "No items yet.")
        return

    lines = []
    for item in sorted(items, key=lambda i: i["created_at"]):
        listing = item.get("listing") or {}
        title = listing.get("title") or "(no listing yet)"
        lines.append(f"{item['item_id'][:8]} | {item['status']:10} | {title}")
    await _safe_reply(update.message, "\n".join(lines))


async def listing_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    item_id, err = _resolve_item_id(user_id, context.args)
    if err:
        await _safe_reply(update.message, f"⚠️ {err}")
        return
    last_item[user_id] = item_id

    item = get_item(item_id)
    if not item or not item.get("listing"):
        await _safe_reply(update.message, f"No listing draft yet for {item_id[:8]}.")
        return

    result = {
        "identification": item.get("identification") or {},
        "pricing": item.get("pricing") or {},
        "listing": item["listing"],
    }
    await _safe_reply(update.message, format_draft(result))


async def delete_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    item_id, err = _resolve_item_id(user_id, context.args)
    if err:
        await _safe_reply(update.message, f"⚠️ {err}")
        return

    item = get_item(item_id)
    if item is None:
        await _safe_reply(update.message, f"No item found for {item_id[:8]}.")
        return

    offer_id = (item.get("ebay") or {}).get("offer_id")
    if offer_id:
        try:
            await asyncio.to_thread(delete_offer, offer_id)
        except Exception as e:
            await _safe_reply(update.message, f"⚠️ Could not delete eBay offer {offer_id}: {e}")
            return

    photos = item.get("photos") or []
    if photos:
        folder = Path(photos[0]).parent
        shutil.rmtree(folder, ignore_errors=True)

    delete_item(item_id)
    last_item.pop(user_id, None)
    review.pop(user_id, None)
    await _safe_reply(update.message, f"🗑️ Deleted {item_id[:8]} (offer {offer_id or 'none'} removed).")


def _activate_item(item_id: str) -> dict:
    item = get_item(item_id)
    offer_id = (item.get("ebay") or {}).get("offer_id")
    if not offer_id:
        raise ValueError("No offer_id stored for this item yet — create the eBay draft first.")

    listing_id = publish_offer(offer_id)
    ebay_data = dict(item.get("ebay") or {})
    ebay_data["listing_id"] = listing_id
    ebay_data["view_item_url"] = f"https://www.ebay.com/itm/{listing_id}"
    update_field(item_id, "ebay", ebay_data)
    update_status(item_id, "published")
    return ebay_data


async def activate_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    item_id, err = _resolve_item_id(user_id, context.args)
    if err:
        await _safe_reply(update.message, f"⚠️ {err}")
        return
    last_item[user_id] = item_id

    item = get_item(item_id)
    if item is None:
        await _safe_reply(update.message, f"No item found for {item_id[:8]}.")
        return
    if item["status"] != "ebay_draft":
        await _safe_reply(update.message, 
            f"Item {item_id[:8]} is at status '{item['status']}', not 'ebay_draft'. Nothing to activate."
        )
        return

    try:
        ebay_data = await asyncio.to_thread(_activate_item, item_id)
    except Exception as e:
        traceback.print_exc()
        await _safe_reply(update.message, f"⚠️ Activate failed: {type(e).__name__}: {str(e)[:300]}")
        return

    await _safe_reply(update.message, f"🎉 Published! {ebay_data['view_item_url']}")


async def retry_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    item_id, err = _resolve_item_id(user_id, context.args)
    if err:
        await _safe_reply(update.message, f"⚠️ {err}")
        return
    last_item[user_id] = item_id

    item = get_item(item_id)
    if item is None:
        await _safe_reply(update.message, f"No item found for {item_id[:8]}.")
        return

    status = item["status"]
    await _safe_reply(update.message, f"🔄 Retrying {item_id[:8]} (status: {status})...")

    try:
        if status == "captured":
            listing_photos = await asyncio.to_thread(_split_receipt, item_id, item["photos"])
            ident = await asyncio.to_thread(identify_item, listing_photos[:GEMINI_PHOTO_LIMIT])
            update_field(item_id, "identification", ident)
            update_status(item_id, "identified")
            await _safe_reply(update.message, f"✓ Identified: {ident.get('brand')} {ident.get('product_name') or ident.get('item_type')}")

        elif status == "identified":
            pricing = await asyncio.to_thread(
                get_pricing, item["identification"]["search_query"], research=item["identification"]
            )
            update_field(item_id, "pricing", pricing)
            if pricing.get("price_source_url"):
                update_field(item_id, "price_source_url", pricing["price_source_url"])
            update_status(item_id, "priced")
            await _safe_reply(update.message, f"✓ Priced: ${pricing.get('suggested_price')} ({pricing.get('confidence')})")

        elif status == "priced":
            draft = await asyncio.to_thread(generate_draft, item["identification"], item["pricing"])
            update_field(item_id, "listing", draft)
            update_status(item_id, "drafted")
            update_status(item_id, "review")
            review[user_id] = item_id
            await _safe_reply(update.message, format_draft({
                "identification": item["identification"],
                "pricing": item["pricing"],
                "listing": draft,
            }))

        elif status == "approved":
            result = await asyncio.to_thread(create_draft_offer, item_id)
            update_field(item_id, "ebay", result)
            update_status(item_id, "ebay_draft")
            await _safe_reply(update.message, f"📝 Draft created: SKU {result['sku']}, offer {result['offer_id']}")

        elif status == "ebay_draft":
            ebay_data = await asyncio.to_thread(_activate_item, item_id)
            await _safe_reply(update.message, f"🎉 Published! {ebay_data['view_item_url']}")

        else:
            await _safe_reply(update.message, f"Nothing to retry — status is '{status}'.")

    except MissingRequiredAspectsError as e:
        lines = [f"⚠️ Category {e.category_id} requires these item specifics:"]
        for m in e.missing:
            opts = f" (allowed: {', '.join(m['allowed_values'])})" if m["allowed_values"] else ""
            lines.append(f"  • {m['name']}{opts}")
        await _safe_reply(update.message, "\n".join(lines))
    except Exception as e:
        traceback.print_exc()
        await _safe_reply(update.message, f"⚠️ Retry failed: {type(e).__name__}: {str(e)[:300]}")


async def health_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    lines = ["🏥 Health check:"]

    try:
        await asyncio.to_thread(get_access_token)
        lines.append("✅ eBay user token: OK")
    except Exception as e:
        lines.append(f"❌ eBay user token: {e}")

    for policy_type in ("fulfillment", "return"):
        try:
            policy_id = await asyncio.to_thread(get_policy_id, policy_type)
            lines.append(f"✅ {policy_type.capitalize()} policy: {policy_id}")
        except Exception as e:
            lines.append(f"❌ {policy_type.capitalize()} policy: {e}")

    for name in ("CLOUDINARY_CLOUD_NAME", "CLOUDINARY_API_KEY", "CLOUDINARY_API_SECRET"):
        lines.append(f"✅ {name}: set" if os.getenv(name) else f"❌ {name}: missing")

    await _safe_reply(update.message, "\n".join(lines))


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    async with _lock_for(user_id):
        data = pending.pop(user_id, None)
        if data and data.get("task") is not None:
            data["task"].cancel()

    if data:
        await _safe_reply(update.message, f"🛑 Cancelled capture ({len(data['files'])} photo(s) discarded).")
    else:
        await _safe_reply(update.message, "No capture in progress.")


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    print(f"UNHANDLED ERROR: {type(context.error).__name__}: {context.error}")
    traceback.print_exception(type(context.error), context.error, context.error.__traceback__)


def _ipv4_request(connection_pool_size: int) -> HTTPXRequest:
    """Force python-telegram-bot's httpx client onto IPv4. This machine's IPv6
    route to api.telegram.org fails the TLS handshake intermittently (~1/3 of
    connections), which crashes bootstrap with an empty httpx.ConnectError.
    Binding the socket to 0.0.0.0 pins connections to the working IPv4 path."""
    transport = httpx.AsyncHTTPTransport(
        local_address="0.0.0.0",
        limits=httpx.Limits(
            max_connections=connection_pool_size,
            max_keepalive_connections=connection_pool_size,
        ),
    )
    return HTTPXRequest(
        connection_pool_size=connection_pool_size,
        httpx_kwargs={"transport": transport},
    )


def main() -> None:
    app = (
        Application.builder()
        .token(TOKEN)
        .request(_ipv4_request(256))
        .get_updates_request(_ipv4_request(1))
        .build()
    )
    app.add_error_handler(error_handler)
    app.add_handler(MessageHandler(filters.PHOTO, photo_handler))
    app.add_handler(CallbackQueryHandler(confirm_callback, pattern=r"^confirm:"))
    app.add_handler(CommandHandler("status", status_command))
    app.add_handler(CommandHandler("listing", listing_command))
    app.add_handler(CommandHandler("delete", delete_command))
    app.add_handler(CommandHandler("activate", activate_command))
    app.add_handler(CommandHandler("retry", retry_command))
    app.add_handler(CommandHandler("health", health_command))
    app.add_handler(CommandHandler("cancel", cancel_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    app.run_polling()


if __name__ == "__main__":
    main()
