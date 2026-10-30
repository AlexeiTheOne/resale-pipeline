"""Photo intake: the capture debounce window, the Start/Wait/Cancel staging
step, /addphotos appends and /haul splitting.

Moved verbatim out of telegram_bot.py (docs/cleanup-plan.md Phase 2, steps 3
and 4a). photo_handler dispatches to find / Quick add / inventory mode
(bot.inventory_cmds) before reaching the batching here.
"""
import asyncio
import uuid
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

import config
import db
import ross_tag
from bot import flow, inventory_cmds, photos_cmds, state, util
from pipeline import stages

INBOX = config.DATA_DIR / "inbox"
INBOX.mkdir(parents=True, exist_ok=True)

DEFAULT_CAPTURE_WINDOW = 5    # seconds to wait for more photos before processing
EXTENDED_CAPTURE_WINDOW = 30  # after the user types "wait" — for forwarding big batches
WAIT_EXTENSION = 15           # seconds the inline "Wait" button waits for more photos


async def photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    # message_id increases in the order you send messages. The downloads below are
    # awaited before we take the lock, and with concurrent update processing on,
    # these handlers run in parallel — so lock/append order reflects which download
    # finished first, NOT your send order. We tag each photo with its message_id
    # and sort by it so photo position (cover / tag / receipt) is deterministic.
    mid = update.message.message_id
    photo = update.message.photo[-1]
    f = await context.bot.get_file(photo.file_id)
    data = await f.download_as_bytearray()

    if user_id in state.inv_box:
        await inventory_cmds._inv_photo(user_id, update.message, bytes(data), mid)
        return
    if user_id in state.quick_add:
        await inventory_cmds._quick_add_photo(user_id, update.message, bytes(data), mid)
        return
    if user_id in state.find_mode:
        await inventory_cmds._find_one(user_id, update.message, bytes(data), mid)
        return

    async with util._lock_for(user_id):
        target = state.awaiting_photos.get(user_id)
        if target is not None:
            # /addphotos mode — this photo attaches to an existing item rather than
            # starting a new one. Buffer it and (re)arm the finalize timer that
            # writes the new photos onto the item and re-syncs the eBay offer.
            ap = state.appending.get(user_id)
            if ap is None or ap["item_id"] != target:
                existing = db.get_item(target)
                if existing is None:
                    state.awaiting_photos.pop(user_id, None)
                    state.appending.pop(user_id, None)
                    await util._safe_reply(update.message, "That item no longer exists — nothing to add photos to.")
                    return
                photos = existing.get("photos") or []
                folder = Path(photos[0]).parent if photos else (INBOX / target)
                folder.mkdir(parents=True, exist_ok=True)
                ap = state.appending[user_id] = {
                    "item_id": target, "folder": folder, "base": list(photos),
                    "new": [], "chat_id": update.effective_chat.id, "task": None,
                }
            p = ap["folder"] / f"{ap['folder'].name}_{mid}.jpg"
            p.write_bytes(data)
            ap["new"].append((mid, str(p)))
            if ap["task"] is not None:
                ap["task"].cancel()
            ap["task"] = context.application.create_task(
                _finalize_append(user_id, target, update, context)
            )
            count = len(ap["new"])
            print(f"📷 Append: {count} new photo(s) buffered for item {target[:8]}")
            return
        st = state.staging.get(user_id)
        if st is not None:
            # Item is already awaiting confirmation — this photo belongs to it.
            # Append it (kept sorted by message_id), then re-arm the confirmation
            # once photos stop arriving.
            p = st["folder"] / f"{st['folder'].name}_{mid}.jpg"
            p.write_bytes(data)
            st["photos"].append((mid, str(p)))
            st["photos"].sort(key=lambda mp: mp[0])
            st["paths"] = [path for _, path in st["photos"]]
            db.update_field(st["item_id"], "photos", st["paths"])
            if st["task"] is not None:
                st["task"].cancel()
            st["task"] = context.application.create_task(
                _await_more_photos(user_id, st["item_id"], context, WAIT_EXTENSION)
            )
            count = len(st["paths"])
        else:
            if user_id not in state.pending:
                state.pending[user_id] = {"files": [], "task": None}
            state.pending[user_id]["files"].append((mid, data))
            count = len(state.pending[user_id]["files"])

            if state.pending[user_id]["task"] is not None:
                state.pending[user_id]["task"].cancel()

            state.pending[user_id]["task"] = context.application.create_task(
                finalize_capture(user_id, update, context)
            )
    print(f"📷 Capture: photo appended, {count} buffered for user {user_id}")


async def finalize_capture(user_id, update, context):
    window = state.capture_window.get(user_id, DEFAULT_CAPTURE_WINDOW)
    await asyncio.sleep(window)
    async with util._lock_for(user_id):
        files = state.pending.pop(user_id)["files"]
        state.capture_window.pop(user_id, None)  # reset to default for the next batch
    # Order by message_id (your send order), not the order downloads finished.
    files.sort(key=lambda md: md[0])
    print(f"📷 Capture: {len(files)} photo(s) collected from buffer (window {window}s)")

    staging_id = str(uuid.uuid4())
    folder = INBOX / staging_id
    folder.mkdir(parents=True, exist_ok=True)

    paths = []
    photos = []  # (message_id, path) kept so later-added photos stay ordered
    for mid, b in files:
        p = folder / f"{staging_id}_{mid}.jpg"
        p.write_bytes(b)
        paths.append(str(p))
        photos.append((mid, str(p)))
    print(f"📷 Capture: {len(paths)} photo(s) written to disk in {folder}")

    if user_id in state.haul_mode:
        # Haul mode stays armed until /cancel, as the /haul message promises.
        # Consuming it on the first batch meant a pause longer than the capture
        # window silently dropped back to single-item mode, and the next armful
        # of photos became ONE item containing several products and their tags.
        # Keep the long window armed for the next armful too; finalize_capture
        # resets it to the default after every batch.
        state.capture_window[user_id] = EXTENDED_CAPTURE_WINDOW
        groups, separators, tags = await asyncio.to_thread(_split_haul, paths)
        if not groups:
            await util._safe_reply(
                update.message,
                "📦 Haul: those were all blackout frames — no item photos among them. "
                "Send the items, or /cancel to leave haul mode.")
            return
        if len(groups) == 1 and separators == 0 and tags == 0:
            # Nothing divided anything: no blackout, no barcode, no recognised
            # tag. Listing several products as a single item is much worse than
            # asking, so refuse rather than guess.
            await util._safe_reply(
                update.message,
                f"📦 Haul: {len(paths)} photo(s), but I couldn't find a single Ross tag "
                f"or blackout frame, so I can't tell where one item ends and the next "
                f"begins.\n\nEither end each item with a photo of its Ross tag, or take "
                f"one dark photo (cover the lens) between items. If this really is a "
                f"single item, /cancel and send it normally.")
            return
        if tags:
            await util._safe_reply(
                update.message,
                f"🏷️ Spotted {tags} Ross tag(s) — using them to split the haul.")
        await _run_haul(user_id, groups, update, context)
        return

    item_id = db.create_item(paths)
    print(f"📷 Capture: item {item_id} created with {len(paths)} photo path(s) in DB")

    # Don't auto-run. Hold the item and ask the user what to do with it.
    chat_id = update.effective_chat.id
    async with util._lock_for(user_id):
        state.staging[user_id] = {
            "item_id": item_id, "folder": folder, "paths": paths, "photos": photos,
            "chat_id": chat_id, "task": None,
        }
    await _send_confirmation(context, chat_id, item_id, len(paths))


def _is_ross_tag(path: str) -> bool:
    """Does this photo carry a Ross price tag's CODE128 barcode?

    Measured over the real photo library this fires on only 41% of tag photos —
    glare, angle and focus defeat the rest — so it is a bonus boundary, never the
    one a haul depends on. See _split_haul."""
    try:
        digits = ross_tag.decode_barcode(path)
    except Exception:
        return False
    return bool(digits) and len(digits) == 18


def _split_haul(paths: list[str]) -> tuple[list[list[str]], int, int]:
    """Split one photo dump into per-item groups.
    Returns (groups, separators_seen, tags_detected).

    The boundary is a **blackout frame** — a deliberately dark photo (cover the
    lens) between items. Automatic tag detection was tried first and measured on
    the real library: an 18-digit barcode decodes on 41% of tag photos, any
    barcode on 39%, OCR finding Ross wording on 23% — 44% for all three combined.
    A boundary detector that misses over half the time is worse than none,
    because every miss silently welds two items into one.

    A blackout frame is unambiguous: across 401 real photos the darkest averaged
    56/255, while a covered lens lands near zero, so the threshold has room to
    spare and merchandise can never trip it.

    Two cheaper boundaries are tried first, so in practice you rarely need to
    shoot a blackout at all:

      * a Ross tag whose barcode decodes (free, local, never wrong when it fires)
      * a Ross tag RECOGNISED by the vision model. Recognising a tag is far
        easier than reading one — 21 of 21 real tags detected, 0 false positives
        across 6 merchandise sets — so it works on the tags whose barcode won't
        decode, which is most of them. One call per haul, not per photo.

    The blackout frame stays the guaranteed override: it needs no network, no
    model, and can't be argued with. Separator frames are dropped (they're not
    photos of anything); trailing photos after the last separator are the final
    item, since a separator delimits items and the last needs no terminator."""
    tag_indices = ross_tag.detect_ross_tags(paths)

    groups, current, separators = [], [], 0
    for idx, path in enumerate(paths):
        if ross_tag.is_dark_frame(path):
            separators += 1
            if current:
                groups.append(current)
                current = []
            continue
        is_tag = idx in tag_indices or _is_ross_tag(path)
        # Two tags in a row — a double-stickered item photographed twice, or a
        # re-shoot of an unreadable tag — would otherwise close a group whose
        # only member is the second tag. _split_receipt returns early on a
        # single-photo group without peeling, so that phantom item's ONE photo
        # is the Ross tag itself, and it goes to eBay showing what you paid.
        # A tag closes the group it belongs to; it never opens one.
        if is_tag and not current:
            if groups:
                groups[-1].append(path)   # belongs to the item just closed
            else:
                current.append(path)      # haul opens on a tag; it'll be peeled
            continue
        current.append(path)
        if is_tag:
            groups.append(current)
            current = []
    if current:
        groups.append(current)
    return groups, separators, len(tag_indices)


async def _run_haul(user_id, groups, update, context) -> None:
    """Create an item per photo group and run them all through the pipeline.

    The pipeline semaphore (MAX_CONCURRENT_LISTINGS) caps how many actually
    compute at once; gates queue via gate_queue, so the user is asked about one
    item at a time even though many are running."""
    message = update.message
    item_ids = []
    for group in groups:
        staging_id = str(uuid.uuid4())
        folder = INBOX / staging_id
        folder.mkdir(parents=True, exist_ok=True)
        paths = []
        for src in group:
            dest = folder / Path(src).name
            dest.write_bytes(Path(src).read_bytes())
            paths.append(str(dest))
        item_ids.append(db.create_item(paths))

    await util._safe_reply(
        message,
        f"📦 Haul: {len(item_ids)} item(s) split off the Ross tags. Running them now — "
        f"I'll only stop for the ones that need you.")

    results = await asyncio.gather(
        *(flow.advance(user_id, iid, message, context) for iid in item_ids),
        return_exceptions=True)
    for iid, outcome in zip(item_ids, results):
        if isinstance(outcome, Exception):
            print(f"HAUL ERROR {iid[:8]}:", outcome)
            util._record_error(f"haul ({iid[:8]})", outcome)

    await util._safe_reply(message, _haul_digest(item_ids, user_id))


def _haul_digest(item_ids, user_id) -> str:
    """One summary of where a haul landed, so the user sees the whole batch at a
    glance instead of reconstructing it from a stream of per-item messages."""
    buckets = {}
    for iid in item_ids:
        item = db.get_item(iid)
        status = item["status"] if item else "deleted"
        buckets.setdefault(status, []).append(iid)

    waiting = len(state.gate_queue.get(user_id) or []) + (1 if state.gate.get(user_id) else 0)
    labels = {
        "review": "✅ drafted, waiting on your approve",
        "identified": "💬 needs you at the identify gate",
        "priced": "✅ priced, drafting",
        "captured": "⚠️ stalled before identification",
        "ebay_draft": "✅ pushed to eBay as a draft",
        "published": "🎉 live on eBay",
        "rejected": "❌ rejected",
    }
    lines = [f"📦 Haul done — {len(item_ids)} item(s):"]
    for status, ids in sorted(buckets.items(), key=lambda kv: -len(kv[1])):
        lines.append(f"  {labels.get(status, status)}: {len(ids)}")
        lines.append(f"    {', '.join(i[:8] for i in ids)}")
    if waiting:
        lines.append(f"\n{waiting} item(s) still need a decision from you — "
                     f"answer the prompt above and the next one follows.")
    return "\n".join(lines)


async def haul_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Arm multi-item capture: the next photo dump is split into one item per
    Ross tag instead of becoming a single item."""
    user_id = update.effective_user.id
    state.haul_mode.add(user_id)
    state.capture_window[user_id] = EXTENDED_CAPTURE_WINDOW
    await util._safe_reply(
        update.message,
        f"📦 Haul mode on.\n\n"
        f"Shoot each item as usual (ending with its Ross tag), then **take one dark "
        f"photo with the lens covered** before starting the next item. That blackout "
        f"frame is the divider — it's the only thing I need to tell items apart, and "
        f"it never fails the way reading the tag barcode does (that works on about 4 "
        f"tags in 10).\n\n"
        f"No blackout needed after the last item. If a tag barcode does decode, it "
        f"ends that item too, so a clean shot needs no divider.\n\n"
        f"I'll wait {EXTENDED_CAPTURE_WINDOW}s after the last photo, then run them all. "
        f"Items with solid comps go straight to a draft; I'll only ask about the rest.\n\n"
        f"/cancel to drop out.")


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
    async with util._lock_for(user_id):
        st = state.staging.get(user_id)
        if st is None or st["item_id"] != item_id:
            return
        st["task"] = None
        count, chat_id = len(st["paths"]), st["chat_id"]
    await _send_confirmation(context, chat_id, item_id, count)


async def _finalize_append(user_id, item_id, update, context) -> None:
    """After photos stop arriving in /addphotos mode, commit the new photos onto
    the item (existing photos first, new ones appended in send order) and, if the
    item already has an eBay offer, push the updated photo set to eBay."""
    window = state.capture_window.get(user_id, DEFAULT_CAPTURE_WINDOW)
    await asyncio.sleep(window)
    async with util._lock_for(user_id):
        ap = state.appending.get(user_id)
        if ap is None or ap["item_id"] != item_id:
            return  # a newer batch or a cancel superseded this one
        state.appending.pop(user_id, None)
        state.awaiting_photos.pop(user_id, None)
        state.capture_window.pop(user_id, None)
        # Order the newly-added photos by message_id (send order), then append them
        # after the item's original photos so the gallery cover/tag order is kept.
        new_sorted = [path for _, path in sorted(ap["new"], key=lambda mp: mp[0])]
        # Re-read rather than trusting ap["base"], the snapshot taken when the
        # first append photo arrived. Minutes can pass between that snapshot and
        # this commit, and the pipeline may well have run in between — writing
        # the stale list back resurrects the peeled Ross price tag and drops
        # whatever else was written meanwhile. Fall back to the snapshot only if
        # the item has vanished.
        current = (db.get_item(item_id) or {}).get("photos")
        base = current if current is not None else ap["base"]
        photos = list(base) + [p for p in new_sorted if p not in base]

    db.update_field(item_id, "photos", photos)
    state.last_item[user_id] = item_id
    added = len(new_sorted)
    print(f"📷 Append: committed {added} photo(s) to item {item_id}, {len(photos)} total")
    if state.list_after_photos.get(user_id) == item_id:
        state.list_after_photos.pop(user_id, None)
        await asyncio.to_thread(stages.split_receipt, item_id, photos,
                                lambda t: None)
        db.update_status(item_id, "identified")
        await util._safe_reply(update.message, f"🚀 Got {added} photo(s). Pricing {item_id[:8]}...")
        await flow.advance(user_id, item_id, update.message, context)
        return
    await util._safe_reply(update.message, f"➕ Added {added} photo(s) to {item_id[:8]} ({len(photos)} total).")
    await photos_cmds._resync_photos(item_id, update.message)


async def addphotos_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Attach more photos to an existing item. Usage: /addphotos <id> then send the
    photos. Applies to the most recent item when no id is given."""
    user_id = update.effective_user.id
    item_id, err = util._resolve_item_id(user_id, context.args)
    if err:
        await util._safe_reply(update.message, f"⚠️ {err}")
        return

    async with util._lock_for(user_id):
        state.awaiting_photos[user_id] = item_id
        state.appending.pop(user_id, None)  # drop any half-collected batch for a different item
    state.last_item[user_id] = item_id
    window = state.capture_window.get(user_id, DEFAULT_CAPTURE_WINDOW)
    await util._safe_reply(update.message,
        f"📎 Send the photos to add to {item_id[:8]} now. "
        f"I'll attach them once they stop arriving (~{window}s). /cancel to abort."
    )
