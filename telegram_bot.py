import os, sys, re, uuid, asyncio, shutil, tempfile, time, traceback
from collections import Counter, deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from dotenv import load_dotenv
import httpx
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest, NetworkError, TimedOut
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    TypeHandler,
    filters,
    ContextTypes,
)
from telegram.request import HTTPXRequest

load_dotenv()
sys.path.insert(0, str(Path(__file__).parent))

from config import (
    AUTO_CONFIRM, AUTO_CONFIRM_MIN_IDENT_CONFIDENCE,
    AUTO_CONFIRM_MIN_IDENT_CONFIDENCE_WITH_UPC,
    GEMINI_PHOTO_LIMIT, MAX_CONCURRENT_LISTINGS, EBAY_DEFAULT_AD_RATE_PCT,
    REPRICE_WEEKDAY, REPRICE_HOUR_UTC,
)
from db import (
    create_item, delete_item, get_item, latest_stats, list_items,
    update_field, update_status, VALID_STATUSES,
)
from identify import identify_item
from receipt import decode_barcode, extract_receipt
from pipeline.price import get_pricing
from pipeline.draft import generate_draft, revise_draft, revise_identification
from pipeline.reprice import run_weekly_check
from ebay.auth import get_access_token
from ebay.analytics import traffic_status
from ebay.orders import finances_status, orders_status, sales_by_item
from ebay.inventory import (
    create_draft_offer,
    delete_offer,
    get_offer,
    get_policy_id,
    publish_offer,
    update_offer_price,
    update_offer_quantity,
    withdraw_offer,
    MissingRequiredAspectsError,
)
from ebay.marketing import promote_listing, marketing_status

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
INBOX = Path("data/inbox")
INBOX.mkdir(parents=True, exist_ok=True)


def _parse_allowed_ids(raw: str | None) -> set[int]:
    ids = set()
    for tok in (raw or "").replace(";", ",").split(","):
        tok = tok.strip()
        if tok.isdigit():
            ids.add(int(tok))
    return ids


# Whitelist of Telegram user IDs allowed to drive the bot. This gates a LIVE eBay
# seller account (publish / delete / ad-rate), so an open bot means anyone who
# finds the username can act on the account. When unset the bot stays open (so an
# existing install isn't bricked on upgrade) but prints a loud startup warning;
# use /whoami to get your id, then set TELEGRAM_ALLOWED_USER_IDS in .env.
ALLOWED_USER_IDS = _parse_allowed_ids(os.getenv("TELEGRAM_ALLOWED_USER_IDS"))

DEFAULT_CAPTURE_WINDOW = 5    # seconds to wait for more photos before processing
EXTENDED_CAPTURE_WINDOW = 30  # after the user types "wait" — for forwarding big batches
WAIT_EXTENSION = 15           # seconds the inline "Wait" button waits for more photos

pending = {}        # user_id -> {"files": [bytearray,...], "task": asyncio.Task}
review = {}         # user_id -> item_id  (item currently awaiting this user's reply)
locks = {}          # user_id -> asyncio.Lock guarding pending[user_id]
last_item = {}      # user_id -> item_id  (last-touched item, survives past the review window)
capture_window = {} # user_id -> seconds to batch photos (default DEFAULT_CAPTURE_WINDOW)
staging = {}        # user_id -> {item_id, folder, paths, chat_id, task} awaiting Start/Wait/Cancel
awaiting_photos = {}# user_id -> item_id  (photos should attach to this existing item, set by /addphotos)
appending = {}      # user_id -> {item_id, folder, base, new, chat_id, task} while an append batch is collected
gate = {}           # user_id -> {item_id, stage}  paused at "identify"/"price" awaiting confirm/correction
gate_queue = {}     # user_id -> [{item_id, stage, reason}]  gates waiting their turn (a /haul runs many
                    # items at once, and only one can hold the single gate slot; the rest queue here
                    # instead of overwriting each other and getting lost)
haul_mode = set()   # user_ids whose next photo batch is a multi-item haul to be split on Ross tags


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


TELEGRAM_MAX_CHARS = 3900  # under the 4096 hard limit, leaving headroom


async def _send_chunked(message, lines: list[str]) -> None:
    """Send a list of lines as one or more messages, each under Telegram's 4096-char
    cap. /status and /comps can outgrow a single message as the item count grows."""
    buf, size = [], 0
    for line in lines:
        if buf and size + len(line) + 1 > TELEGRAM_MAX_CHARS:
            await _safe_reply(message, "\n".join(buf))
            buf, size = [], 0
        buf.append(line)
        size += len(line) + 1
    if buf:
        await _safe_reply(message, "\n".join(buf))


# Ring buffer of recent errors, dumped by /errors. The console isn't visible from
# the phone, so failures that only print a traceback there are otherwise invisible.
_recent_errors: deque = deque(maxlen=10)


def _record_error(where: str, exc: BaseException) -> None:
    ts = datetime.now(timezone.utc).strftime("%m-%d %H:%M:%SZ")
    tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    _recent_errors.append((ts, where, tb.strip()))


async def _auth_guard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Global gate, registered in handler group -1 so it runs before everything
    else. If TELEGRAM_ALLOWED_USER_IDS is set, only those users get through;
    anyone else is refused and ApplicationHandlerStop halts the rest of the
    handler chain. An unset allowlist leaves the bot open (legacy behavior)."""
    if not ALLOWED_USER_IDS:
        return
    user = update.effective_user
    if user is None or user.id not in ALLOWED_USER_IDS:
        uid = user.id if user else "unknown"
        print(f"⛔ Blocked update from unauthorized user {uid}")
        if update.callback_query is not None:
            await update.callback_query.answer("Not authorized.", show_alert=True)
        elif update.effective_message is not None:
            await _safe_reply(update.effective_message, "⛔ Not authorized to use this bot.")
        raise ApplicationHandlerStop


async def auth_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Re-consent the eBay seller account from the phone. /auth with no argument
    replies with the consent URL to open; /auth <pasted redirect URL or code>
    completes the exchange. Handy when the token loses a scope (e.g. sell.marketing
    was added) and needs re-granting without a laptop."""
    from ebay.auth import exchange_code, get_consent_url

    args = context.args
    if not args:
        await _safe_reply(update.message,
            "🔑 eBay re-consent:\n"
            "1) Open this URL and approve (sign in as the seller):\n\n"
            f"{get_consent_url()}\n\n"
            "2) On the 'Authorization successfully completed' page, copy the WHOLE "
            "address-bar URL (it contains code=) and send it back as:\n"
            "/auth <paste the URL>")
        return

    await _safe_reply(update.message, "🔑 Exchanging with eBay...")
    try:
        token = await asyncio.to_thread(exchange_code, " ".join(args))
    except Exception as e:
        traceback.print_exc()
        _record_error("auth exchange", e)
        await _safe_reply(update.message, f"⚠️ Exchange failed: {type(e).__name__}: {str(e)[:250]}")
        return
    await _safe_reply(update.message,
        f"✅ eBay authorized — token stored (expires in {token.get('expires_in')}s). "
        "Run /health to confirm the scopes.")


async def whoami_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Report the caller's numeric Telegram id — the value to put in
    TELEGRAM_ALLOWED_USER_IDS. Works from the phone before the allowlist is set."""
    user = update.effective_user
    if user is None:
        return
    if not ALLOWED_USER_IDS:
        state = ("⚠️ open to everyone — add "
                 f"TELEGRAM_ALLOWED_USER_IDS={user.id} to .env and restart to lock it to you.")
    elif user.id in ALLOWED_USER_IDS:
        state = "restricted; you are on the allowlist."
    else:
        state = "restricted; you are NOT on the allowlist."
    await _safe_reply(update.message, f"Your Telegram user id: {user.id}\nBot access: {state}")


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
    # message_id increases in the order you send messages. The downloads below are
    # awaited before we take the lock, and with concurrent update processing on,
    # these handlers run in parallel — so lock/append order reflects which download
    # finished first, NOT your send order. We tag each photo with its message_id
    # and sort by it so photo position (cover / tag / receipt) is deterministic.
    mid = update.message.message_id
    photo = update.message.photo[-1]
    f = await context.bot.get_file(photo.file_id)
    data = await f.download_as_bytearray()

    async with _lock_for(user_id):
        target = awaiting_photos.get(user_id)
        if target is not None:
            # /addphotos mode — this photo attaches to an existing item rather than
            # starting a new one. Buffer it and (re)arm the finalize timer that
            # writes the new photos onto the item and re-syncs the eBay offer.
            ap = appending.get(user_id)
            if ap is None or ap["item_id"] != target:
                existing = get_item(target)
                if existing is None:
                    awaiting_photos.pop(user_id, None)
                    appending.pop(user_id, None)
                    await _safe_reply(update.message, "That item no longer exists — nothing to add photos to.")
                    return
                photos = existing.get("photos") or []
                folder = Path(photos[0]).parent if photos else (INBOX / target)
                folder.mkdir(parents=True, exist_ok=True)
                ap = appending[user_id] = {
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
        st = staging.get(user_id)
        if st is not None:
            # Item is already awaiting confirmation — this photo belongs to it.
            # Append it (kept sorted by message_id), then re-arm the confirmation
            # once photos stop arriving.
            p = st["folder"] / f"{st['folder'].name}_{mid}.jpg"
            p.write_bytes(data)
            st["photos"].append((mid, str(p)))
            st["photos"].sort(key=lambda mp: mp[0])
            st["paths"] = [path for _, path in st["photos"]]
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
            pending[user_id]["files"].append((mid, data))
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

    if user_id in haul_mode:
        haul_mode.discard(user_id)
        groups, trailing = await asyncio.to_thread(_split_haul, paths)
        if not groups:
            await _safe_reply(
                update.message,
                "📦 Haul: couldn't find a Ross tag barcode in any of those photos, so "
                "there's nothing to split on. Send them again as a single item, or "
                "re-shoot the tags.")
            return
        if trailing:
            await _safe_reply(
                update.message,
                f"⚠️ Haul: {len(trailing)} photo(s) came after the last Ross tag, so that "
                f"item has no tag and I've left it out. Re-send it on its own.")
        await _run_haul(user_id, groups, update, context)
        return

    item_id = create_item(paths)
    print(f"📷 Capture: item {item_id} created with {len(paths)} photo path(s) in DB")

    # Don't auto-run. Hold the item and ask the user what to do with it.
    chat_id = update.effective_chat.id
    async with _lock_for(user_id):
        staging[user_id] = {
            "item_id": item_id, "folder": folder, "paths": paths, "photos": photos,
            "chat_id": chat_id, "task": None,
        }
    await _send_confirmation(context, chat_id, item_id, len(paths))


def _is_ross_tag(path: str) -> bool:
    """Does this photo carry a Ross price tag's CODE128 barcode? That barcode is
    the store's own 18-digit price code, and since every item's photo set ends
    with its tag, it's the natural boundary between items in a haul. Decoding is
    local and free (no API call), so scanning a whole haul is cheap."""
    try:
        digits = decode_barcode(path)
    except Exception:
        return False
    return bool(digits) and len(digits) == 18


def _split_haul(paths: list[str]) -> tuple[list[list[str]], list[str]]:
    """Split one photo dump into per-item groups on Ross-tag boundaries.

    Every item's photo set ends with its Ross tag, so a tag closes the current
    group and the next photo starts a new one. Returns (groups, trailing) —
    trailing is any photos after the last tag, which means the last item's tag
    photo is missing and the user needs to be told rather than have those photos
    silently folded into the previous item or dropped."""
    groups, current = [], []
    for path in paths:
        current.append(path)
        if _is_ross_tag(path):
            groups.append(current)
            current = []
    return groups, current


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
        item_ids.append(create_item(paths))

    await _safe_reply(
        message,
        f"📦 Haul: {len(item_ids)} item(s) split off the Ross tags. Running them now — "
        f"I'll only stop for the ones that need you.")

    results = await asyncio.gather(
        *(advance(user_id, iid, message, context) for iid in item_ids),
        return_exceptions=True)
    for iid, outcome in zip(item_ids, results):
        if isinstance(outcome, Exception):
            print(f"HAUL ERROR {iid[:8]}:", outcome)
            _record_error(f"haul ({iid[:8]})", outcome)

    await _safe_reply(message, _haul_digest(item_ids, user_id))


def _haul_digest(item_ids, user_id) -> str:
    """One summary of where a haul landed, so the user sees the whole batch at a
    glance instead of reconstructing it from a stream of per-item messages."""
    buckets = {}
    for iid in item_ids:
        item = get_item(iid)
        status = item["status"] if item else "deleted"
        buckets.setdefault(status, []).append(iid)

    waiting = len(gate_queue.get(user_id) or []) + (1 if gate.get(user_id) else 0)
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
    haul_mode.add(user_id)
    capture_window[user_id] = EXTENDED_CAPTURE_WINDOW
    await _safe_reply(
        update.message,
        f"📦 Haul mode on. Send every item's photos back to back, each item ending "
        f"with its Ross tag — that tag is what splits them.\n\n"
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
    async with _lock_for(user_id):
        st = staging.get(user_id)
        if st is None or st["item_id"] != item_id:
            return
        st["task"] = None
        count, chat_id = len(st["paths"]), st["chat_id"]
    await _send_confirmation(context, chat_id, item_id, count)


async def _finalize_append(user_id, item_id, update, context) -> None:
    """After photos stop arriving in /addphotos mode, commit the new photos onto
    the item (existing photos first, new ones appended in send order) and, if the
    item already has an eBay offer, push the updated photo set to eBay."""
    window = capture_window.get(user_id, DEFAULT_CAPTURE_WINDOW)
    await asyncio.sleep(window)
    async with _lock_for(user_id):
        ap = appending.get(user_id)
        if ap is None or ap["item_id"] != item_id:
            return  # a newer batch or a cancel superseded this one
        appending.pop(user_id, None)
        awaiting_photos.pop(user_id, None)
        capture_window.pop(user_id, None)
        # Order the newly-added photos by message_id (send order), then append them
        # after the item's original photos so the gallery cover/tag order is kept.
        new_sorted = [path for _, path in sorted(ap["new"], key=lambda mp: mp[0])]
        photos = ap["base"] + new_sorted

    update_field(item_id, "photos", photos)
    last_item[user_id] = item_id
    added = len(new_sorted)
    print(f"📷 Append: committed {added} photo(s) to item {item_id}, {len(photos)} total")
    await _safe_reply(update.message, f"➕ Added {added} photo(s) to {item_id[:8]} ({len(photos)} total).")
    await _resync_photos(item_id, update.message)


async def _resync_photos(item_id: str, message) -> None:
    """Push the item's current photo set to eBay. No-op for items that haven't
    been drafted on eBay yet — the new photos are simply picked up when the draft
    is first created. For drafted/published items, rebuild the inventory item and
    re-publish so the change reaches the live listing."""
    item = get_item(item_id)
    if item is None:
        return
    status = item["status"]
    if status not in ("ebay_draft", "published"):
        await _safe_reply(message, "They'll be included when you create the eBay draft (/retry or approve).")
        return

    await _safe_reply(message, "🔄 Updating the eBay listing with the new photos...")
    try:
        result = await asyncio.to_thread(create_draft_offer, item_id)
    except Exception as e:
        traceback.print_exc()
        await _safe_reply(message,
            f"⚠️ Photos saved, but updating the eBay offer failed: {type(e).__name__}: {str(e)[:250]}\n"
            "Use /retry to try again."
        )
        return

    # create_draft_offer returns fresh sku/offer_id/image_urls; merge so we keep
    # any listing_id / view_item_url already stored for a published item.
    merged = {**(item.get("ebay") or {}), **result}
    update_field(item_id, "ebay", merged)

    if status == "published":
        offer_id = merged.get("offer_id")
        try:
            await asyncio.to_thread(publish_offer, offer_id)
        except Exception as e:
            traceback.print_exc()
            await _safe_reply(message,
                f"⚠️ Offer updated, but re-publishing the live listing failed: {type(e).__name__}: {str(e)[:250]}\n"
                "Use /activate to push it live."
            )
            return
        await _safe_reply(message, f"✅ Live listing updated. {merged.get('view_item_url', '')}".strip())
    else:
        await _safe_reply(message, "✅ eBay draft updated with the new photos. /activate when ready.")


async def addphotos_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Attach more photos to an existing item. Usage: /addphotos <id> then send the
    photos. Applies to the most recent item when no id is given."""
    user_id = update.effective_user.id
    item_id, err = _resolve_item_id(user_id, context.args)
    if err:
        await _safe_reply(update.message, f"⚠️ {err}")
        return

    async with _lock_for(user_id):
        awaiting_photos[user_id] = item_id
        appending.pop(user_id, None)  # drop any half-collected batch for a different item
    last_item[user_id] = item_id
    window = capture_window.get(user_id, DEFAULT_CAPTURE_WINDOW)
    await _safe_reply(update.message,
        f"📎 Send the photos to add to {item_id[:8]} now. "
        f"I'll attach them once they stop arriving (~{window}s). /cancel to abort."
    )


_pipeline_sem = None


def _pipeline_semaphore() -> asyncio.Semaphore:
    """Cap how many item pipelines run concurrently (MAX_CONCURRENT_LISTINGS).
    Created lazily so it binds to the running event loop; extra Starts queue on
    it rather than firing a swarm of parallel Gemini/Apify calls."""
    global _pipeline_sem
    if _pipeline_sem is None:
        _pipeline_sem = asyncio.Semaphore(MAX_CONCURRENT_LISTINGS)
    return _pipeline_sem


def _review_markup(item_id: str) -> InlineKeyboardMarkup:
    """Approve/Reject buttons carrying the item id, so a finished draft can be
    acted on unambiguously even when several are pending at once."""
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Approve", callback_data=f"review:approve:{item_id}"),
        InlineKeyboardButton("❌ Reject", callback_data=f"review:reject:{item_id}"),
    ]])


def _notifier(message):
    """A thread-safe progress pinger: worker-thread stages call it to post status
    back onto the bot's event loop (identify/pricing each take a minute or more)."""
    loop = asyncio.get_running_loop()

    def notify(text: str) -> None:
        asyncio.run_coroutine_threadsafe(_safe_reply(message, text), loop)
    return notify


async def _run_stage(item_id, message, stage_fn, *args):
    """Run one blocking pipeline stage in a worker thread, holding the concurrency
    semaphore only for the duration of the actual work (so an item waiting at a
    confirm gate never ties up a slot). Returns (result, None) or (None, error)."""
    notify = _notifier(message)
    try:
        async with _pipeline_semaphore():
            result = await asyncio.to_thread(stage_fn, item_id, *args, notify)
        return result, None
    except Exception as e:
        print("PIPELINE ERROR:", traceback.format_exc())
        _record_error(f"{stage_fn.__name__} ({item_id[:8]})", e)
        await _safe_reply(message, f"⚠️ {stage_fn.__name__} failed: {type(e).__name__}: {str(e)[:200]}")
        return None, e


async def _show_review(user_id, item_id, message) -> None:
    """Show a finished draft with Approve/Reject and mark it this user's active
    review target, so a typed correction / approve applies to it. The per-draft
    buttons carry the item id explicitly, so approving is unambiguous regardless
    of how many drafts are pending."""
    item = get_item(item_id)
    review[user_id] = item_id
    last_item[user_id] = item_id
    result = {"identification": item["identification"], "pricing": item["pricing"],
              "listing": item["listing"]}
    await _safe_reply(message, format_draft(result), parse_mode=None,
                      reply_markup=_review_markup(item_id))


async def _publish_and_report(item_id, message) -> None:
    """Publish an ebay_draft offer, report the live URL, then auto-promote at the
    default ad rate if one is configured. Shared by /activate and advance()."""
    try:
        ebay_data = await asyncio.to_thread(_activate_item, item_id)
    except Exception as e:
        traceback.print_exc()
        await _safe_reply(message, f"⚠️ Activate failed: {type(e).__name__}: {str(e)[:300]}")
        return
    await _safe_reply(message, f"🎉 Published! {ebay_data['view_item_url']}")
    if EBAY_DEFAULT_AD_RATE_PCT and EBAY_DEFAULT_AD_RATE_PCT > 0:
        await _promote_item(item_id, EBAY_DEFAULT_AD_RATE_PCT, message)


def _identify_gate_reason(ident: dict) -> str | None:
    """Why this identification needs your eyes, or None if it can clear its gate
    unattended. A decoded UPC lowers the bar: it's hard evidence of exactly which
    product this is, worth more than the model's self-reported confidence."""
    if not AUTO_CONFIRM:
        return "auto-confirm is off"
    if not ident.get("brand") and not ident.get("product_name"):
        return "item is unconfirmed"
    if ident.get("condition_flags"):
        return f"condition flags: {', '.join(ident['condition_flags'])}"
    try:
        confidence = float(ident.get("confidence") or 0)
    except (TypeError, ValueError):
        return "confidence is unreadable"
    bar = (AUTO_CONFIRM_MIN_IDENT_CONFIDENCE_WITH_UPC if ident.get("upc")
           else AUTO_CONFIRM_MIN_IDENT_CONFIDENCE)
    if confidence < bar:
        return f"confidence {confidence:.2f} is below {bar:.2f}"
    return None


def _price_gate_reason(pricing: dict) -> str | None:
    """Why this price needs your eyes, or None if it can clear its gate
    unattended. pipeline/price.py already decided this (needs_review); this only
    turns it into something worth reading."""
    if not AUTO_CONFIRM:
        return "auto-confirm is off"
    if pricing.get("suggested_price") is None:
        return "no comp-backed price"
    # Items priced before evidence tiers existed have no needs_review field, and
    # absence must not read as "safe to skip".
    if "needs_review" not in pricing:
        return "priced before evidence grading"
    if pricing.get("needs_review"):
        flags = pricing.get("review_flags") or []
        return ", ".join(flags) if flags else f"{pricing.get('evidence')} comp evidence"
    return None


async def advance(user_id, item_id, message, context) -> None:
    """Run automatic steps for an item, pausing at the first gate that genuinely
    needs you, then hand the gate slot to whatever else is waiting.

    This is the one place that knows 'what comes next' in the pipeline, shared by
    the capture Start button, the confirm gates, /retry and /haul.

    Gates it can clear on its own — a confident identification, a solid-evidence
    price — are cleared with a one-line note rather than a prompt, so a clean item
    runs photos -> draft without interruption and only the genuinely ambiguous
    ones ask. The review gate before anything reaches eBay is never skipped.
    The compute stages report their own progress/errors via _run_stage."""
    try:
        await _advance_one(user_id, item_id, message, context)
    finally:
        # Whether this item finished, gated, or blew up, the next queued item
        # gets its turn — a failure must not strand the rest of a haul.
        await _activate_next_gate(user_id, message)


async def _advance_one(user_id, item_id, message, context) -> None:
    while True:
        item = get_item(item_id)
        if item is None:
            await _safe_reply(message, "That item no longer exists.")
            return
        status = item["status"]

        if status == "captured":
            ident, err = await _run_stage(
                item_id, message, _stage_identify, item.get("photos") or [])
            if err is not None:
                return
            reason = _identify_gate_reason(ident)
            if reason:
                await _show_identify_gate(user_id, item_id, message, reason)
                return
            name = ident.get("product_name") or ident.get("item_type") or "item"
            await _safe_reply(
                message, f"✅ {ident.get('brand') or ''} {name} — identified, pricing...".strip())

        elif status == "identified":
            pricing, err = await _run_stage(item_id, message, _stage_price)
            if err is not None:
                return
            reason = _price_gate_reason(pricing)
            if reason:
                await _show_price_gate(user_id, item_id, message, reason)
                return
            await _safe_reply(
                message,
                f"✅ ${pricing.get('suggested_price')} from {pricing.get('sold_count')} solid "
                f"comps — writing the listing...")

        elif status == "priced":
            _, err = await _run_stage(item_id, message, _stage_draft)
            if err is None:
                await _show_review(user_id, item_id, message)
            return

        elif status in ("drafted", "review"):
            # Waiting on your Approve/Reject — re-show the draft to act on.
            await _show_review(user_id, item_id, message)
            return

        elif status == "approved":
            await _approve_item(item_id, message, context)
            return

        elif status == "ebay_draft":
            await _publish_and_report(item_id, message)
            return

        else:  # published, rejected
            await _safe_reply(message, f"Nothing to advance — {item_id[:8]} is '{status}'.")
            return


def _gate_is_busy(user_id, item_id) -> bool:
    """Is another item already holding this user's gate slot? Re-showing the SAME
    item (after a correction) is never 'busy' — that's the active conversation."""
    active = gate.get(user_id)
    return active is not None and active["item_id"] != item_id


def _enqueue_gate(user_id, item_id, stage, reason) -> None:
    """Park a gate behind the active one, newest last. Re-queuing an item that's
    already waiting updates it in place rather than asking about it twice."""
    queue = gate_queue.setdefault(user_id, [])
    for entry in queue:
        if entry["item_id"] == item_id:
            entry.update(stage=stage, reason=reason)
            return
    queue.append({"item_id": item_id, "stage": stage, "reason": reason})


async def _activate_next_gate(user_id, message) -> None:
    """Show the next queued gate, if the slot is free and anything is waiting.
    Called after every advance() so a finished item hands the floor to the next
    one instead of leaving a haul silently stalled."""
    if gate.get(user_id) is not None:
        return
    queue = gate_queue.get(user_id) or []
    while queue:
        entry = queue.pop(0)
        if get_item(entry["item_id"]) is None:
            continue  # deleted while it waited
        # Claim the slot BEFORE the first await. Several items of a haul can
        # finish in the same tick, and without claiming synchronously they'd each
        # see a free slot, pop a different entry, and all but the last would be
        # dropped on the floor.
        gate[user_id] = {"item_id": entry["item_id"], "stage": entry["stage"]}
        remaining = len(queue)
        suffix = f" ({remaining} more waiting)" if remaining else ""
        reason = (entry["reason"] or "") + suffix
        if entry["stage"] == "identify":
            await _show_identify_gate(user_id, entry["item_id"], message, reason)
        else:
            await _show_price_gate(user_id, entry["item_id"], message, reason)
        return


async def _show_identify_gate(user_id, item_id, message, reason=None) -> None:
    """Pause after identification: show what was identified and wait for the user
    to confirm or type a correction (handled in text_handler / _handle_gate).
    `reason` says why this one couldn't clear itself — with auto-confirm on, a
    gate that appears at all is a gate worth reading."""
    if _gate_is_busy(user_id, item_id):
        _enqueue_gate(user_id, item_id, "identify", reason)
        return
    item = get_item(item_id)
    gate[user_id] = {"item_id": item_id, "stage": "identify"}
    review.pop(user_id, None)
    last_item[user_id] = item_id
    body = format_identification(item["identification"])
    if reason:
        body += f"\n\n🛑 Needs you: {reason}"
    await _safe_reply(
        message,
        body + "\n\nType 'confirm' to price it, or tell me what to fix "
               "(e.g. 'brand is Tommy Jeans, color navy').",
    )


async def _show_price_gate(user_id, item_id, message, reason=None) -> None:
    """Pause after pricing: show the suggested price and wait for confirm or a
    manual price (a whole number is charm-priced, e.g. 35 -> $34.99). `reason`
    says why this one couldn't clear itself."""
    if _gate_is_busy(user_id, item_id):
        _enqueue_gate(user_id, item_id, "price", reason)
        return
    item = get_item(item_id)
    gate[user_id] = {"item_id": item_id, "stage": "price"}
    review.pop(user_id, None)
    last_item[user_id] = item_id
    body = format_pricing(item["pricing"])
    if reason:
        body += f"\n\n🛑 Needs you: {reason}"
    await _safe_reply(
        message,
        body + "\n\nType 'confirm' to write the listing, or type a price to set it "
               "(e.g. 35 -> $34.99).",
    )


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
        # Launch as a background task so this handler returns immediately and
        # another item can be started while this one is still processing. The item
        # is 'captured', so advance() runs identify and pauses at its confirm gate.
        context.application.create_task(
            advance(user_id, item_id, query.message, context)
        )

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


async def _approve_item(item_id: str, message, context) -> None:
    """Mark an item approved and push it to eBay as a draft offer. Shared by the
    typed 'approve' and the per-draft Approve button. Reports progress/errors to
    `message`; leaves the item at 'approved' (retryable) on any eBay failure."""
    update_status(item_id, "approved")
    await _safe_reply(message, "✅ Approved. Creating eBay draft...")

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
        await _safe_reply(message, "\n".join(lines))
        return
    except Exception as e:
        traceback.print_exc()
        await _safe_reply(message,
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
    await _safe_reply(message, msg)


async def review_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle the per-draft Approve/Reject buttons. The item id rides in the
    callback data, so this works no matter how many drafts are pending."""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    try:
        _, action, item_id = query.data.split(":", 2)
    except ValueError:
        return

    item = get_item(item_id)
    if item is None:
        await query.edit_message_text("This item no longer exists.")
        return
    if item["status"] not in ("review", "drafted"):
        await query.edit_message_text(f"Item {item_id[:8]} is '{item['status']}' — nothing to review.")
        return

    # Drop the buttons so the draft can't be double-actioned.
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass

    if review.get(user_id) == item_id:
        review.pop(user_id, None)
    last_item[user_id] = item_id

    if action == "approve":
        await _approve_item(item_id, query.message, context)
    elif action == "reject":
        update_status(item_id, "rejected")
        await _safe_reply(query.message, f"❌ Rejected {item_id[:8]}.")


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
# orchestrators above). The trailing `notify` arg is supplied by _run_stage. ---

def _stage_identify(item_id, paths, notify=lambda _t: None) -> dict:
    # The last photo is always the Ross receipt: OCR it for cost + code and
    # remove it from the listing photos (it must not be posted to eBay).
    listing_photos = _split_receipt(item_id, paths, notify)
    # Only the first N photos (overview + tag) go to the paid API; all listing
    # photos remain on the item for the eBay listing.
    id_photos = listing_photos[:GEMINI_PHOTO_LIMIT]
    notify("🔬 Identifying the item...")
    print(f"🔬 Identifying from {len(id_photos)} of {len(listing_photos)} listing photo(s)")
    t = time.perf_counter()
    # Send the first few photos to the model, but scan ALL of them for the tag
    # barcode (decoding is free and the tag isn't always in the first few).
    ident = identify_item(id_photos, scan_paths=listing_photos)
    print(f"✓ Identified in {time.perf_counter() - t:.0f}s:", ident.get("brand"),
          ident.get("product_name") or ident.get("item_type"))
    update_field(item_id, "identification", ident)
    update_status(item_id, "identified")
    return ident


def _stage_price(item_id, notify=lambda _t: None) -> dict:
    ident = get_item(item_id)["identification"]
    notify("💰 Pricing...")
    t = time.perf_counter()
    pricing = get_pricing(ident["search_query"], research=ident)
    print(f"✓ Priced in {time.perf_counter() - t:.0f}s:", pricing.get("suggested_price"),
          f"({pricing.get('confidence')})")
    update_field(item_id, "pricing", pricing)
    if pricing.get("price_source_url"):
        update_field(item_id, "price_source_url", pricing["price_source_url"])
    update_status(item_id, "priced")
    return pricing


def _stage_draft(item_id, notify=lambda _t: None) -> dict:
    item = get_item(item_id)
    notify("✍️ Writing the listing...")
    t = time.perf_counter()
    draft = generate_draft(item["identification"], item["pricing"])
    print(f"✓ Draft generated in {time.perf_counter() - t:.0f}s")
    update_field(item_id, "listing", draft)
    update_status(item_id, "drafted")
    update_status(item_id, "review")
    return draft


def _fmt(value) -> str:
    return "—" if value in (None, "", []) else str(value)


def format_identification(ident: dict) -> str:
    lines = [
        "🔍 Identified:",
        f"Brand: {_fmt(ident.get('brand'))}",
        f"Type: {_fmt(ident.get('item_type'))}",
        f"Product: {_fmt(ident.get('product_name'))}",
        f"Model/SKU: {_fmt(ident.get('model'))}",
        f"Color: {_fmt(ident.get('color'))}",
        f"Size: {_fmt(ident.get('size'))}",
        f"Condition: {_fmt(ident.get('condition'))}",
        f"Confidence: {_fmt(ident.get('confidence'))}",
    ]
    if ident.get("upc"):
        lines.append(f"UPC: {ident['upc']}")
    if ident.get("condition_flags"):
        lines.append(f"⚠️ Flags: {', '.join(ident['condition_flags'])}")
    if not ident.get("brand") and not ident.get("product_name"):
        lines.append("⚠️ Item is unconfirmed — please correct it before pricing.")
    return "\n".join(lines)


# How much the comps behind a price can be trusted (pipeline/price.py's tiers).
_EVIDENCE_LABEL = {
    "solid": "✅ solid comps",
    "thin": "🟡 thin comps — worth a look",
    "none": "❌ no usable comps",
}


def format_pricing(pricing: dict) -> str:
    sp = pricing.get("suggested_price")
    head = f"💰 Suggested price: ${sp}" if sp is not None else "💰 Price: none — type one"
    lines = [head]

    evidence = pricing.get("evidence")
    if evidence:
        n, disp = pricing.get("sold_count"), pricing.get("dispersion")
        detail = f"{n} sold comp(s)"
        if disp:
            detail += f", {disp}x spread"
        if pricing.get("query_rung") and pricing["query_rung"] != "exact":
            detail += f", matched on '{pricing.get('query_used')}'"
        lines.append(f"  {_EVIDENCE_LABEL.get(evidence, evidence)} — {detail}")

    lines.append(
        f"  sold median: ${_fmt(pricing.get('sold_median'))} | "
        f"active floor: ${_fmt(pricing.get('active_floor'))} | {_fmt(pricing.get('confidence'))}")

    # Only useful when there's no comp-backed price to compare it against —
    # otherwise it invites pricing off an LLM estimate, which is exactly what
    # the evidence tiers exist to stop.
    if sp is None and pricing.get("research_resale"):
        lines.append(f"  research estimate (not a comp): ${pricing['research_resale']}")
    if pricing.get("price_source_url"):
        lines.append(f"  source: {pricing['price_source_url']}")
    if pricing.get("comp_warning"):
        lines.append(f"  ⚠️ {pricing['comp_warning']}")
    return "\n".join(lines)


def _apply_manual_price(pricing: dict, price: float) -> dict:
    """Record a human price override without erasing what the machine proposed.

    `machine_price` is written once by pipeline/price.py and never touched here;
    the override goes to suggested_price (what the rest of the pipeline reads)
    and is also kept as manual_price alongside it. Previously this assignment
    overwrote suggested_price outright, so every correction destroyed the number
    it was correcting — 34 of 57 items ended up with price_basis='manual' and no
    record of how far off the suggestion had been. Keeping both makes that gap
    measurable instead of invisible."""
    pricing = dict(pricing)
    pricing["manual_price"] = price
    pricing["manual_price_at"] = datetime.now(timezone.utc).isoformat()
    if pricing.get("machine_price") is None:
        # Pre-existing items priced before machine_price existed: the current
        # suggestion is the machine's, so capture it before it's replaced.
        pricing["machine_price"] = pricing.get("suggested_price")
    pricing["suggested_price"] = price
    pricing["confidence"] = "manual"
    pricing["price_basis"] = "manual"
    return pricing


def _charm_price(value: float) -> float:
    """Drop a penny off a whole-dollar amount so it ends in .99 (35 -> 34.99), the
    way the auto-pricing already rounds. Explicit cents are honored as typed."""
    if value == int(value):
        value -= 0.01
    return round(max(value, 0.0), 2)


def _parse_price_override(text: str) -> float | None:
    """Extract a price the user typed (e.g. '35', '$35', 'make it 42.50') and
    charm-price it. Returns None if no number is present."""
    m = re.search(r"\d+(?:\.\d{1,2})?", text.replace(",", ""))
    return _charm_price(float(m.group(0))) if m else None


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
        "Tap Approve / Reject below, or type a correction (e.g. 'color is yellow not orange').",
    ]

    return "\n".join(lines)


_CONFIRM_WORDS = ("confirm", "yes", "ok", "okay", "y", "✅", "👍")


async def _handle_gate(user_id, text, update, context) -> None:
    """Handle a typed message while an item is paused at the identify/price gate:
    'confirm' advances to the next stage; anything else is applied as a change and
    the same gate is shown again."""
    g = gate[user_id]
    item_id, stage = g["item_id"], g["stage"]
    low = text.lower().strip()

    if get_item(item_id) is None:
        gate.pop(user_id, None)
        await _safe_reply(update.message, "That item no longer exists.")
        return

    if low in _CONFIRM_WORDS:
        if stage == "price" and (get_item(item_id)["pricing"] or {}).get("suggested_price") is None:
            await _safe_reply(update.message, "No price set yet — type a price first (e.g. 35 -> $34.99).")
            return
        gate.pop(user_id, None)
        # Confirming identify leaves the item 'identified' (advance -> price gate);
        # confirming price leaves it 'priced' (advance -> draft + review).
        note = "✅ Confirmed. Pricing..." if stage == "identify" else "✅ Confirmed. Writing the listing..."
        await _safe_reply(update.message, note)
        context.application.create_task(advance(user_id, item_id, update.message, context))
        return

    # Not a confirm → treat as a change to this stage.
    if stage == "identify":
        await _safe_reply(update.message, "✏️ Updating the identification...")
        try:
            item = get_item(item_id)
            revised = await asyncio.to_thread(revise_identification, item["identification"], text)
        except Exception as e:
            await _safe_reply(update.message, f"⚠️ Could not update: {type(e).__name__}: {str(e)[:200]}")
            return
        update_field(item_id, "identification", revised)
        await _show_identify_gate(user_id, item_id, update.message)
    else:  # price
        price = _parse_price_override(text)
        if price is None:
            await _safe_reply(update.message, "Type a price to set it (e.g. 35 -> $34.99), or 'confirm'.")
            return
        item = get_item(item_id)
        pricing = _apply_manual_price(item.get("pricing") or {}, price)
        update_field(item_id, "pricing", pricing)
        await _show_price_gate(user_id, item_id, update.message)


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

    # Confirm gates (after identify / after price) take precedence over the draft
    # review — an item pauses here for a typed 'confirm' or a correction.
    if user_id in gate:
        await _handle_gate(user_id, text, update, context)
        return

    if user_id not in review:
        await _safe_reply(update.message, "Send me photos of an item to start.")
        return

    # Typed approve/reject/correction target the most-recent draft (the buttons
    # on each draft handle any-order approval unambiguously).
    item_id = review[user_id]

    if low in ("approve", "✅", "yes", "ok"):
        review.pop(user_id, None)
        await _approve_item(item_id, update.message, context)
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
    await _safe_reply(update.message, format_draft(result), reply_markup=_review_markup(item_id))


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """List items and their pipeline status. Optional status filter: /status
    published. Paginated so it never exceeds Telegram's message-size limit."""
    all_items = list_items()
    if not all_items:
        await _safe_reply(update.message, "No items yet.")
        return

    # Header: a count per status across everything, so you see the funnel at a glance.
    counts = Counter(i["status"] for i in all_items)
    ordered = VALID_STATUSES + sorted(s for s in counts if s not in VALID_STATUSES)
    header = "📊 " + " · ".join(f"{s}:{counts[s]}" for s in ordered if counts[s])

    status_filter = context.args[0].lower() if context.args else None
    if status_filter:
        items = [i for i in all_items if i["status"] == status_filter]
        if not items:
            await _safe_reply(update.message, f"{header}\n\nNo items with status '{status_filter}'.")
            return
    else:
        items = all_items

    lines = [header, ""]
    for item in sorted(items, key=lambda i: i["created_at"]):
        listing = item.get("listing") or {}
        title = listing.get("title") or "(no listing yet)"
        lines.append(f"{item['item_id'][:8]} | {item['status']:10} | {title[:48]}")
    await _send_chunked(update.message, lines)


def _ross_title(item: dict) -> str:
    """Best short label for an item in an audit list: its listing title, else the
    identified brand+model, else a placeholder."""
    title = (item.get("listing") or {}).get("title")
    if title:
        return title
    ident = item.get("identification") or {}
    guess = " ".join(x for x in (ident.get("brand"), ident.get("model") or ident.get("product_name")) if x)
    return guess or "(no listing yet)"


def _ross_fields(item: dict):
    """(code_digits, code_kind, price) for an item's Ross tag. code_kind is
    'real' (a genuine 12-digit barcode), 'auto' (a placeholder we minted, see
    _next_receipt_code), or 'missing'. price is the reduced/paid Ross price."""
    receipt = item.get("receipt") or {}
    raw = receipt.get("code")
    digits = re.sub(r"\D", "", str(raw)) if raw else ""
    if not digits:
        kind = "missing"
    elif int(digits) < _REAL_CODE_FLOOR:
        kind = "auto"
    else:
        kind = "real"
    return (digits or None), kind, receipt.get("reduced_price")


async def ross_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Audit Ross tag data — the code and the paid price — across items, so you can
    see at a glance what's filled in and what still needs a /receipt. Groups items
    into those missing the Ross price and those that have it; the code column shows
    a real barcode, an 'auto:' placeholder, or — for none. Rejected items are
    skipped. Usage: /ross (everything) · /ross missing (only unpriced items)."""
    items = [i for i in list_items() if i["status"] != "rejected"]
    if not items:
        await _safe_reply(update.message, "No items yet.")
        return

    missing_only = bool(context.args) and context.args[0].lower() in ("missing", "todo", "unpriced")

    def line(item: dict) -> str:
        digits, kind, price = _ross_fields(item)
        code_str = digits if kind == "real" else f"auto:{digits}" if kind == "auto" else "—"
        price_str = f"${price}" if price is not None else "❌ none"
        return (f"{item['item_id'][:8]} | {price_str:>8} | {code_str:16} | "
                f"{item['status']:10} | {_ross_title(item)[:32]}")

    priced, unpriced, no_real_code = [], [], 0
    for item in sorted(items, key=lambda i: i["created_at"]):
        _, kind, price = _ross_fields(item)
        if kind != "real":
            no_real_code += 1
        (priced if price is not None else unpriced).append(item)

    header = (f"📋 Ross audit — {len(items)} item(s) · {len(unpriced)} missing price · "
              f"{no_real_code} without a real code")
    lines = [header, ""]

    if unpriced:
        lines.append(f"❌ Missing Ross price ({len(unpriced)}) — set with /receipt <id> <price>:")
        lines += [line(i) for i in unpriced]
    if not missing_only and priced:
        if unpriced:
            lines.append("")
        lines.append(f"✅ Priced ({len(priced)}):")
        lines += [line(i) for i in priced]
    if missing_only and not unpriced:
        lines.append("🎉 Every item has a Ross price recorded.")

    await _send_chunked(update.message, lines)


async def errors_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show recent errors (the console isn't visible from the phone). Each entry is
    the time, where it happened, and the exception's final line; the full traceback
    still prints to the server console."""
    if not _recent_errors:
        await _safe_reply(update.message, "✅ No errors recorded since startup.")
        return
    lines = ["🐞 Recent errors (newest first):", ""]
    for ts, where, tb in reversed(_recent_errors):
        last = tb.splitlines()[-1] if tb else ""
        lines.append(f"🕐 {ts} · {where}")
        lines.append(f"   {last[:250]}")
    await _send_chunked(update.message, lines)


async def comps_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show the sold/active comps the price was built from — the evidence behind a
    suggested price, so you can sanity-check it before overriding."""
    user_id = update.effective_user.id
    item_id, err = _resolve_item_id(user_id, context.args)
    if err:
        await _safe_reply(update.message, f"⚠️ {err}")
        return
    last_item[user_id] = item_id

    item = get_item(item_id)
    pricing = (item or {}).get("pricing") or {}
    if not pricing:
        await _safe_reply(update.message, f"No pricing data for {item_id[:8]} yet.")
        return

    lines = [f"💰 Comps for {item_id[:8]} — suggested ${pricing.get('suggested_price')} "
             f"({pricing.get('confidence')})"]
    if pricing.get("comp_warning"):
        lines.append(f"⚠️ {pricing['comp_warning']}")

    sold = pricing.get("sold_comps") or []
    lines += ["", f"SOLD ({len(sold)}, median ${pricing.get('sold_median')}):"]
    lines += [f"  ${c.get('price')} · {(c.get('title') or '')[:48]}" for c in sold] or ["  (none)"]

    active = pricing.get("active_listings") or []
    lines += ["", f"ACTIVE ({len(active)}, floor ${pricing.get('active_floor')}):"]
    lines += [f"  ${a.get('price')} · {(a.get('title') or '')[:48]}" for a in active] or ["  (none)"]

    if pricing.get("research_resale"):
        lines += ["", f"Research resale estimate: ${pricing['research_resale']}"]
    if pricing.get("price_source_url"):
        lines.append(f"Anchor: {pricing['price_source_url']}")
    await _send_chunked(update.message, lines)


def _sale_profit(item: dict):
    """(paid, sale, profit) for an item, or Nones where unknown. Cost is what you
    paid Ross (the receipt); sale is the recorded sale price, falling back to the
    listed price. Profit is gross — before eBay fees, shipping, and ad rate. Kept
    for /sold's immediate reply (fees usually aren't known yet at sale time);
    /profit uses the richer _sale_economics below once /sync has pulled fees."""
    paid = (item.get("receipt") or {}).get("reduced_price")
    ebay = item.get("ebay") or {}
    sale = ebay.get("sale_price")
    if sale is None:
        sale = (item.get("listing") or {}).get("price")
    profit = round(sale - paid, 2) if (paid is not None and sale is not None) else None
    return paid, sale, profit


def _sale_economics(item: dict) -> dict:
    """Full profit picture for a sold item. When /sync has pulled the real eBay
    order (net_proceeds set), profit is exact — net of actual fees; otherwise it
    falls back to a gross estimate (recorded/listed sale price, fees unknown).

    Returns paid, sale, fees, net (proceeds after fees), profit (net − paid),
    margin (profit / sale), and fees_known."""
    paid = (item.get("receipt") or {}).get("reduced_price")
    ebay = item.get("ebay") or {}
    sale = ebay.get("sale_price")
    if sale is None:
        sale = (item.get("listing") or {}).get("price")

    net = ebay.get("net_proceeds")
    fees_known = net is not None and ebay.get("fees_known", True)
    # Real net when we have it; otherwise the gross sale stands in (fees unknown).
    proceeds = net if fees_known else sale

    profit = round(proceeds - paid, 2) if (paid is not None and proceeds is not None) else None
    margin = round(profit / sale, 4) if (profit is not None and sale) else None
    return {
        "paid": paid, "sale": sale, "fees": ebay.get("ebay_fees"),
        "net": net, "proceeds": proceeds, "profit": profit,
        "margin": margin, "fees_known": fees_known,
    }


async def sold_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Mark an item sold and record the sale price, replying with the profit vs.
    what you paid Ross. Usage: /sold [id] [price]. The price is optional (defaults
    to the listed price); a trailing number is taken as the sale price."""
    user_id = update.effective_user.id
    args = list(context.args)

    sale_price = None
    id_args = args
    if args:
        maybe = args[-1].replace("$", "").replace(",", "")
        try:
            sale_price = round(float(maybe), 2)
            id_args = args[:-1]
        except ValueError:
            sale_price = None  # trailing arg wasn't a number — treat it all as the id

    item_id, err = _resolve_item_id(user_id, id_args)
    if err:
        await _safe_reply(update.message, f"⚠️ {err}")
        return
    item = get_item(item_id)
    if item is None:
        await _safe_reply(update.message, f"No item found for {item_id[:8]}.")
        return

    ebay = dict(item.get("ebay") or {})
    if sale_price is not None:
        ebay["sale_price"] = sale_price
        update_field(item_id, "ebay", ebay)
    update_status(item_id, "sold")
    last_item[user_id] = item_id

    # Re-read so _sale_profit sees the just-saved sale price.
    paid, sale, profit = _sale_profit(get_item(item_id))
    msg = f"✅ Marked {item_id[:8]} sold"
    if sale is not None:
        msg += f" for ${sale}"
    if paid is not None:
        msg += f" · paid ${paid}"
    if profit is not None:
        msg += f" · profit ${profit} (before fees)"
    await _safe_reply(update.message, msg)


async def profit_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Summarize profit across all items marked sold: per-item paid → sold → fees
    → net → margin, and the totals. Uses REAL eBay fees for items /sync has pulled
    an order for; items without one fall back to a gross number (fees unknown).
    Run /sync first to fill in real fees."""
    sold = [i for i in list_items() if i["status"] == "sold"]
    if not sold:
        await _safe_reply(update.message, "No sold items yet. Run /sync to pull sold orders, "
                          "or mark one by hand with /sold <id> <price>.")
        return

    total_cost = total_sale = total_net = 0.0
    total_fees = 0.0
    any_gross = False  # at least one item still on a fees-unknown estimate
    lines = ["💵 Profit — sold items:", ""]
    for item in sorted(sold, key=lambda x: x["created_at"]):
        e = _sale_economics(item)
        paid, sale, profit = e["paid"], e["sale"], e["profit"]
        title = ((item.get("listing") or {}).get("title") or "(untitled)")[:32]
        total_cost += paid or 0
        total_sale += sale or 0
        total_net += e["proceeds"] or 0

        margin = f"{e['margin'] * 100:.0f}%" if e["margin"] is not None else "—"
        if e["fees_known"]:
            total_fees += e["fees"] or 0
            lines.append(f"{item['item_id'][:8]} · paid ${paid} · sold ${sale} · "
                         f"fees ${e['fees']} · net ${e['net']} · +${profit} ({margin}) · {title}")
        else:
            any_gross = True
            lines.append(f"{item['item_id'][:8]} · paid ${paid} · sold ${sale} · "
                         f"+${profit} ({margin}, gross — fees unknown) · {title}")

    total_profit = round(total_net - total_cost, 2)
    lines += ["", f"TOTAL: sold ${round(total_sale, 2)} − eBay fees ${round(total_fees, 2)} "
                  f"− cost ${round(total_cost, 2)} = ${total_profit} across {len(sold)} item(s)"]
    if any_gross:
        lines.append("Some items show a gross estimate (no eBay order pulled yet) — run /sync "
                     "to fill in real fees and net.")
    await _send_chunked(update.message, lines)


def _sync_one(item: dict, sold_map: dict | None = None) -> dict | None:
    """Pull one item's live eBay state (price, quantity, listing status) and write
    any changes into the local DB. Runs in a worker thread (blocking eBay call).
    Returns a summary dict for the report, or None if the offer couldn't be read.

    `sold_map` is ebay.orders.sales_by_item()'s result (keyed by sku==item_id and
    by listing_id): when this item appears in it, the real sale price and eBay
    fees are recorded and the item is marked sold automatically — no manual
    /sold needed."""
    item_id = item["item_id"]
    ebay = dict(item.get("ebay") or {})
    offer_id = ebay.get("offer_id")

    offer = get_offer(offer_id)

    changes = []

    # Price: eBay is the source of truth for a manual Seller-Hub edit.
    ebay_price = (offer.get("pricingSummary") or {}).get("price") or {}
    ebay_price_val = ebay_price.get("value")
    if ebay_price_val is not None:
        new_price = round(float(ebay_price_val), 2)
        old_price = (item.get("listing") or {}).get("price")
        if old_price is None or round(float(old_price), 2) != new_price:
            listing = dict(item.get("listing") or {})
            if listing:
                listing["price"] = new_price
                update_field(item_id, "listing", listing)
            pricing = dict(item.get("pricing") or {})
            pricing["suggested_price"] = new_price
            pricing["price_basis"] = "ebay_sync"
            update_field(item_id, "pricing", pricing)
            changes.append(f"price ${old_price}→${new_price}")

    # Quantity: default assumption is 1 (what every listing is created with).
    ebay_qty = offer.get("availableQuantity")
    if ebay_qty is not None:
        old_qty = ebay.get("quantity", 1)
        if int(old_qty) != int(ebay_qty):
            ebay["quantity"] = int(ebay_qty)
            update_field(item_id, "ebay", ebay)
            changes.append(f"qty {old_qty}→{ebay_qty}")

    # Real sale from the Fulfillment/Finances APIs: if this item shows up in the
    # sold_map, record the actual sale price + eBay fees + net and mark it sold
    # outright — no manual /sold guess. Matched by sku (== item_id) first, then by
    # the stored listing_id. Skipped if already sold so re-runs are idempotent.
    auto_sold = False
    sale = None
    if sold_map and item["status"] != "sold":
        sale = sold_map.get(item_id)
        if sale is None and ebay.get("listing_id"):
            sale = sold_map.get(str(ebay["listing_id"]))
    if sale is not None:
        ebay["sale_price"] = sale["sale_price"]
        ebay["shipping_collected"] = sale["shipping_collected"]
        ebay["ebay_fees"] = sale["ebay_fees"]
        ebay["shipping_label_cost"] = sale["shipping_label_cost"]
        ebay["net_proceeds"] = sale["net_proceeds"]
        ebay["order_id"] = sale["order_id"]
        ebay["sold_at"] = sale["sold_at"]
        ebay["fees_known"] = sale["fees_known"]
        update_field(item_id, "ebay", ebay)
        update_status(item_id, "sold")
        auto_sold = True
        net = sale["net_proceeds"] if sale["fees_known"] else None
        changes.append(f"SOLD ${sale['sale_price']}" + (f" · net ${net}" if net is not None else ""))

    listing_status = (offer.get("listing") or {}).get("listingStatus")
    # A published listing eBay reports as ended or out of stock, that we DIDN'T
    # just match to a real order above, has probably sold (or was ended in Seller
    # Hub) — flag it so /sold can record a price the orders feed didn't carry
    # (e.g. an order older than the sold_map window). availableQuantity dropping
    # to 0 is the same signal.
    likely_sold = (
        not auto_sold
        and item["status"] == "published"
        and (listing_status in ("ENDED", "OUT_OF_STOCK") or ebay_qty == 0)
    )
    return {"item_id": item_id, "changes": changes, "auto_sold": auto_sold,
            "likely_sold": likely_sold, "listing_status": listing_status}


async def sync_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Reconcile the local DB with eBay: walk every item that has an eBay offer,
    pull its current price and quantity from eBay, and write back anything that
    changed (e.g. a price or quantity you edited by hand in Seller Hub). Also pulls
    real sold orders — an item eBay has an order for is marked sold automatically
    with the actual sale price and eBay fees (so /profit shows real net/margin).
    Listings that ended without a matching order are flagged for a manual /sold.
    Usage: /sync [days] (order look-back window, default 90)."""
    items = [
        i for i in list_items()
        if (i.get("ebay") or {}).get("offer_id")
        and i["status"] in ("ebay_draft", "published", "sold")
    ]
    if not items:
        await _safe_reply(update.message, "Nothing to sync — no items have an eBay offer yet.")
        return

    days = 90
    if context.args:
        try:
            days = max(1, int(context.args[0]))
        except ValueError:
            pass

    await _safe_reply(update.message, f"🔄 Syncing {len(items)} item(s) from eBay...")

    # Pull real sold orders once (keyed by sku==item_id and listing_id) and hand
    # the map to every _sync_one. A missing fulfillment/finances scope shouldn't
    # break price/qty sync, so degrade to an empty map and note it in the report.
    sold_map, sold_map_err = {}, None
    try:
        sold_map = await asyncio.to_thread(sales_by_item, days)
    except Exception as e:
        traceback.print_exc()
        _record_error("sync (orders)", e)
        sold_map_err = f"{type(e).__name__}: {str(e)[:150]}"

    updated, auto_sold, sold_flags, errors = [], [], [], []
    for item in items:
        try:
            result = await asyncio.to_thread(_sync_one, item, sold_map)
        except Exception as e:
            traceback.print_exc()
            _record_error(f"sync ({item['item_id'][:8]})", e)
            errors.append(f"{item['item_id'][:8]}: {type(e).__name__}: {str(e)[:120]}")
            continue
        if result["auto_sold"]:
            auto_sold.append(f"{result['item_id'][:8]}: {', '.join(result['changes'])}")
        elif result["changes"]:
            updated.append(f"{result['item_id'][:8]}: {', '.join(result['changes'])}")
        if result["likely_sold"]:
            sold_flags.append(result["item_id"])

    lines = [f"✅ Sync done — checked {len(items)} item(s) (orders: last {days}d)."]
    if auto_sold:
        lines += ["", f"🟢 Recorded sold from eBay orders ({len(auto_sold)}):"] + [f"  {a}" for a in auto_sold]
    if updated:
        lines += ["", f"📝 Updated ({len(updated)}):"] + [f"  {u}" for u in updated]
    elif not auto_sold:
        lines.append("No price/quantity changes found.")
    if sold_flags:
        lines += ["", "🛒 Likely sold (ended/out of stock, no order matched) — record with "
                  "/sold <id> <price>:"]
        lines += [f"  {sid[:8]}" for sid in sold_flags]
    if sold_map_err:
        lines += ["", f"⚠️ Couldn't read eBay orders ({sold_map_err})",
                  "   → if this is a scope/403 error, re-run `python -m ebay.auth` to grant "
                  "sell.fulfillment.readonly + sell.finances."]
    if errors:
        lines += ["", f"⚠️ Couldn't read ({len(errors)}):"] + [f"  {e}" for e in errors]
    await _send_chunked(update.message, lines)


async def report_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Build an Excel profit report — cover photo, title, price, shipping, Ross
    cost, and profit net of eBay fees + the ad rate — and send it as a file. It
    reflects the current database each time, and the fee/shipping assumptions are
    editable cells in the sheet that recalc every row."""
    await _safe_reply(update.message, "📊 Building the profit report...")
    import report as report_mod

    out = Path(tempfile.gettempdir()) / f"ross_report_{int(time.time())}.xlsx"
    try:
        await asyncio.to_thread(report_mod.build_report, str(out))
    except Exception as e:
        traceback.print_exc()
        _record_error("report build", e)
        await _safe_reply(update.message, f"⚠️ Report failed: {type(e).__name__}: {str(e)[:200]}")
        return

    try:
        with open(out, "rb") as f:
            await update.message.reply_document(
                document=f, filename="ross_profit_report.xlsx",
                caption="Profit report — edit the yellow fee/shipping cells to recalc.")
    except Exception as e:
        traceback.print_exc()
        _record_error("report send", e)
        await _safe_reply(update.message, f"⚠️ Built the report but couldn't send it: {type(e).__name__}: {str(e)[:200]}")
    finally:
        try:
            out.unlink()
        except OSError:
            pass


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
    # Offer Approve/Reject only while the item is still awaiting review.
    markup = _review_markup(item_id) if item["status"] in ("review", "drafted") else None
    if markup is not None:
        review[user_id] = item_id
    await _safe_reply(update.message, format_draft(result), reply_markup=markup)


_NO_CODE_WORDS = {"none", "n", "-", "x", "skip", "na", "n/a"}

# Real Ross tag codes are full 12-digit numbers (≥ 1e11). Generated placeholder
# codes count up from 0 and stay far below that ceiling, so a generated code can
# never collide with a real barcode no matter how many we mint.
_REAL_CODE_FLOOR = 100_000_000_000  # smallest true 12-digit code (1e11)


def _next_receipt_code() -> str:
    """Next synthetic 12-digit Ross code, for an item you paid for but have no tag
    code on hand. Continues the PLACEHOLDER sequence from the highest placeholder
    on file (+1) — deliberately ignoring real barcodes, so it counts up 0, 1, 2…
    in its own low range and can't ever match a real code. Zero-padded to 12
    digits; starts at 0 when no placeholder has been recorded yet."""
    highest = -1
    for it in list_items():
        c = (it.get("receipt") or {}).get("code")
        digits = re.sub(r"\D", "", str(c)) if c else ""
        if digits:
            value = int(digits)
            if value < _REAL_CODE_FLOOR:
                highest = max(highest, value)
    return str(highest + 1).zfill(12)


async def receipt_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Manually set the Ross cost (paid price) — and optionally the 12-digit tag
    code — on an item, for when the barcode couldn't be decoded. Does not affect
    the eBay price. The code is optional: leave it off (or type 'none') and the
    bot fills in the next sequential code so the row lines up with the others.

    Usage: /receipt <price>                     (auto-generate the code)
           /receipt <price> none                (same — explicit)
           /receipt <price> <code>              (real 12-digit tag code)
           /receipt <item_id> <price> [code]    (explicit item)"""
    user_id = update.effective_user.id
    args = list(context.args)
    if not args:
        await _safe_reply(update.message,
            "Usage: /receipt [item_id] <price> [code]\n"
            "e.g. /receipt 9.99 400286461425   ·   /receipt 9.99   (auto code)")
        return

    # Peel an optional trailing code off the end. A bare 'none' means "generate
    # one"; a run of ≥8 digits is a real tag-code attempt (prices are far shorter,
    # so this can't swallow the price); anything else is the price → auto-generate.
    code = None
    auto = True
    tail = args[-1]
    tail_digits = re.sub(r"\D", "", tail)
    if tail.lower() in _NO_CODE_WORDS:
        args = args[:-1]
    elif tail_digits and len(tail_digits) >= 8 and "." not in tail:
        if len(tail_digits) != 12:
            await _safe_reply(update.message,
                f"⚠️ A tag code must be 12 digits (got {len(tail_digits)} from '{tail}'). "
                "Fix it, or type 'none' to auto-generate one.")
            return
        code, auto = tail_digits, False
        args = args[:-1]

    if not args:
        await _safe_reply(update.message, "Usage: /receipt [item_id] <price> [code]  (e.g. /receipt 9.99)")
        return

    price_str = args[-1]
    item_id, err = _resolve_item_id(user_id, args[:-1])
    if err:
        await _safe_reply(update.message, f"⚠️ {err}")
        return

    try:
        price = float(price_str.replace("$", "").replace(",", ""))
    except ValueError:
        await _safe_reply(update.message, f"⚠️ '{price_str}' isn't a valid price.")
        return

    item = get_item(item_id)
    if item is None:
        await _safe_reply(update.message, f"No item found for {item_id[:8]}.")
        return

    if auto:
        code = _next_receipt_code()

    receipt = dict(item.get("receipt") or {})
    receipt.update({"reduced_price": price, "code": code,
                    "source": "manual", "code_generated": auto})
    update_field(item_id, "receipt", receipt)
    last_item[user_id] = item_id
    tag = " (auto)" if auto else ""
    await _safe_reply(update.message, f"🧾 Saved for {item_id[:8]}: paid ${price:.2f} · code {code}{tag}")


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
            # A published offer can't be deleted while live — end it first.
            if item["status"] == "published":
                await asyncio.to_thread(withdraw_offer, offer_id)
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


async def end_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """End a live listing (withdraw it from eBay) without deleting the item. The
    offer drops back to a draft, so you can /activate it again later. Use this when
    an item sold elsewhere or a live listing needs pulling."""
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
    if item["status"] != "published":
        await _safe_reply(update.message,
            f"{item_id[:8]} is '{item['status']}', not a live listing — nothing to end.")
        return
    offer_id = (item.get("ebay") or {}).get("offer_id")
    if not offer_id:
        await _safe_reply(update.message, "No eBay offer id stored for this item.")
        return

    try:
        await asyncio.to_thread(withdraw_offer, offer_id)
    except Exception as e:
        traceback.print_exc()
        _record_error(f"end ({item_id[:8]})", e)
        await _safe_reply(update.message, f"⚠️ Could not end the listing: {type(e).__name__}: {str(e)[:250]}")
        return

    update_status(item_id, "ebay_draft")
    await _safe_reply(update.message,
        f"🛑 Ended the live listing for {item_id[:8]} — it's back to a draft. /activate to relist.")


async def setprice_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Set an item's price. Usage: /setprice [id] <price> (a whole number is
    charm-priced, 35 -> $34.99). Updates the stored price, and if the item already
    has an eBay offer (draft or live), pushes the new price to eBay too."""
    user_id = update.effective_user.id
    args = list(context.args)
    if not args:
        await _safe_reply(update.message, "Usage: /setprice [id] <price>  (e.g. /setprice 35 -> $34.99)")
        return
    price = _parse_price_override(args[-1])
    if price is None:
        await _safe_reply(update.message, f"'{args[-1]}' isn't a valid price.")
        return
    item_id, err = _resolve_item_id(user_id, args[:-1])
    if err:
        await _safe_reply(update.message, f"⚠️ {err}")
        return
    item = get_item(item_id)
    if item is None:
        await _safe_reply(update.message, f"No item found for {item_id[:8]}.")
        return
    last_item[user_id] = item_id

    # Update the stored price on both the listing and the pricing record.
    listing = dict(item.get("listing") or {})
    if listing:
        listing["price"] = price
        update_field(item_id, "listing", listing)
    pricing = _apply_manual_price(item.get("pricing") or {}, price)
    update_field(item_id, "pricing", pricing)

    offer_id = (item.get("ebay") or {}).get("offer_id")
    if offer_id and item["status"] in ("ebay_draft", "published"):
        try:
            await asyncio.to_thread(update_offer_price, offer_id, price)
        except Exception as e:
            traceback.print_exc()
            _record_error(f"setprice ({item_id[:8]})", e)
            await _safe_reply(update.message,
                f"💲 Saved ${price} locally, but updating eBay failed: {type(e).__name__}: {str(e)[:200]}\n"
                "The stored price is updated; use /retry or /addphotos to rebuild the offer.")
            return
        where = "live listing" if item["status"] == "published" else "eBay draft"
        await _safe_reply(update.message, f"💲 Price set to ${price} for {item_id[:8]} (updated the {where}).")
    else:
        await _safe_reply(update.message, f"💲 Price set to ${price} for {item_id[:8]}.")


async def setqty_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Set an item's available quantity on eBay. Usage: /setqty [id] <n>.
    Quantity isn't asked during posting (every item defaults to 1); use this after
    the item is on eBay to stock more than one. Requires an eBay offer (a draft or
    a live listing), and pushes the new quantity to eBay."""
    user_id = update.effective_user.id
    args = list(context.args)
    if not args:
        await _safe_reply(update.message, "Usage: /setqty [id] <quantity>  (e.g. /setqty 3)")
        return
    try:
        qty = int(args[-1])
    except ValueError:
        await _safe_reply(update.message, f"'{args[-1]}' isn't a whole number.")
        return
    if qty < 0:
        await _safe_reply(update.message, "Quantity can't be negative.")
        return
    item_id, err = _resolve_item_id(user_id, args[:-1])
    if err:
        await _safe_reply(update.message, f"⚠️ {err}")
        return
    item = get_item(item_id)
    if item is None:
        await _safe_reply(update.message, f"No item found for {item_id[:8]}.")
        return
    last_item[user_id] = item_id

    ebay = dict(item.get("ebay") or {})
    offer_id, sku = ebay.get("offer_id"), ebay.get("sku")
    if not offer_id or not sku or item["status"] not in ("ebay_draft", "published"):
        await _safe_reply(update.message,
            f"{item_id[:8]} isn't on eBay yet (status '{item['status']}') — it needs an eBay "
            "offer first. Approve/activate it, then set the quantity.")
        return

    try:
        await asyncio.to_thread(update_offer_quantity, sku, offer_id, qty)
    except Exception as e:
        traceback.print_exc()
        _record_error(f"setqty ({item_id[:8]})", e)
        await _safe_reply(update.message,
            f"⚠️ Could not set the quantity: {type(e).__name__}: {str(e)[:250]}")
        return

    ebay["quantity"] = qty
    update_field(item_id, "ebay", ebay)
    where = "live listing" if item["status"] == "published" else "eBay draft"
    await _safe_reply(update.message, f"📦 Quantity set to {qty} for {item_id[:8]} (updated the {where}).")


def _activate_item(item_id: str) -> dict:
    item = get_item(item_id)
    offer_id = (item.get("ebay") or {}).get("offer_id")
    if not offer_id:
        raise ValueError("No offer_id stored for this item yet — create the eBay draft first.")

    listing_id = publish_offer(offer_id)
    ebay_data = dict(item.get("ebay") or {})
    ebay_data["listing_id"] = listing_id
    ebay_data["view_item_url"] = f"https://www.ebay.com/itm/{listing_id}"
    # A relist (end -> activate again) gets a new eBay listingId and starts
    # accumulating fresh traffic, so always stamp "now" rather than keeping an
    # old value — pipeline/reprice.py uses this to compute weeks-live.
    ebay_data["published_at"] = datetime.now(timezone.utc).isoformat()
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

    # Publish + auto-promote, shared with advance() so both paths behave identically.
    await _publish_and_report(item_id, update.message)


async def _promote_item(item_id: str, pct, message) -> None:
    """Add/adjust the Promoted Listings ad rate for an item's SKU."""
    item = get_item(item_id)
    sku = (item.get("ebay") or {}).get("sku") if item else None
    if not sku:
        await _safe_reply(message, "No eBay SKU for this item yet — create the draft first.")
        return
    try:
        result = await asyncio.to_thread(promote_listing, sku, pct)
    except Exception as e:
        traceback.print_exc()
        await _safe_reply(message, f"⚠️ Could not set the ad rate: {type(e).__name__}: {str(e)[:250]}")
        return
    verb = "Updated" if result["action"] == "updated" else "Set"
    await _safe_reply(message, f"📣 {verb} ad rate to {result['bid_percentage']}% for {item_id[:8]} (Promoted Listings).")


async def promote_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Promote a listing (or change its ad rate). Usage: /promote <id> <pct>
    or /promote <pct> to apply to the most recent item. eBay accepts 2–100%."""
    user_id = update.effective_user.id
    args = list(context.args)

    # Trailing arg is the percentage; the rest (if any) identifies the item.
    if not args:
        await _safe_reply(update.message, "Usage: /promote <id> <pct>  (e.g. /promote 3f2a 10)")
        return
    pct = args[-1]
    id_args = args[:-1]
    item_id, err = _resolve_item_id(user_id, id_args)
    if err:
        await _safe_reply(update.message, f"⚠️ {err}")
        return
    last_item[user_id] = item_id
    await _promote_item(item_id, pct, update.message)


def _reprice_markup(item_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Apply", callback_data=f"reprice:apply:{item_id}"),
        InlineKeyboardButton("Skip", callback_data=f"reprice:skip:{item_id}"),
    ]])


_VERDICT_LABEL = {
    "INVISIBLE": "🙈 Not being found in search",
    "LOW_CTR": "😐 Seen but not clicked",
    "OVERPRICED": "💸 Views but no watchers — likely overpriced",
    "SEND_OFFERS": "👀 Watchers building — consider a private offer before cutting",
    "STALE": "🕸️ Stale — unsold a while",
    "TOO_NEW": "🌱 Too new to judge yet",
    "HEALTHY": "✅ Healthy",
    "ERROR": "⚠️ Couldn't check",
}


def _format_reprice_line(r: dict) -> str:
    item = get_item(r["item_id"])
    title = ((item or {}).get("listing") or {}).get("title") or "(untitled)"
    label = _VERDICT_LABEL.get(r["verdict"], r["verdict"])
    line = f"{r['item_id'][:8]} · {label}\n  {title[:48]}"
    if r["verdict"] == "ERROR":
        line += f"\n  {r.get('error', '')[:150]}"
        return line
    watchers = r.get("watchers")
    line += (f"\n  {r.get('impressions', 0)} impr · {r.get('views', 0)} views · "
             f"{watchers if watchers is not None else '?'} watchers · ${r.get('current_price')}")
    if r.get("suggested_price"):
        line += f" → suggest ${r['suggested_price']}"
    elif r.get("floor_note"):
        # Why there's no suggested cut. Without this the digest shows a problem
        # item with no action and no explanation, which reads as a bug.
        line += f"\n  ⛔ {r['floor_note']}"
    return line


async def _send_reprice_digest(bot, chat_id, results: list[dict]) -> None:
    """Post a weekly price-check digest: one summary header, then one message per
    flagged item (with an Apply/Skip button when a suggested price exists) so
    each can be actioned independently. Healthy/too-new items are just counted in
    the header, not spelled out individually."""
    flagged = [r for r in results if r["verdict"] not in ("HEALTHY", "TOO_NEW")]
    healthy = [r for r in results if r["verdict"] == "HEALTHY"]
    too_new = [r for r in results if r["verdict"] == "TOO_NEW"]

    header = (f"📈 Weekly price check — {len(results)} live listing(s): "
              f"{len(flagged)} flagged, {len(healthy)} healthy, {len(too_new)} too new to judge.")
    try:
        await bot.send_message(chat_id=chat_id, text=header)
    except Exception as e:
        print(f"WARNING: reprice digest header failed: {type(e).__name__}: {e}")
        return

    for r in flagged:
        text = _format_reprice_line(r)
        markup = _reprice_markup(r["item_id"]) if r.get("suggested_price") else None
        try:
            await bot.send_message(chat_id=chat_id, text=text, reply_markup=markup)
        except Exception as e:
            print(f"WARNING: reprice digest item failed: {type(e).__name__}: {e}")


async def pricecheck_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Manually run the weekly price-health check now (it also runs automatically
    once a week). Pulls eBay traffic + watch count for every live listing,
    diagnoses why an unsold item isn't moving, and offers a one-tap price cut
    where the evidence supports one and the receipt cost is on file."""
    await _safe_reply(update.message, "📈 Checking live listings against eBay traffic...")
    try:
        results = await asyncio.to_thread(run_weekly_check)
    except Exception as e:
        traceback.print_exc()
        _record_error("pricecheck", e)
        await _safe_reply(update.message, f"⚠️ Price check failed: {type(e).__name__}: {str(e)[:250]}")
        return
    if not results:
        await _safe_reply(update.message, "No published listings to check.")
        return
    await _send_reprice_digest(context.bot, update.effective_chat.id, results)


async def reprice_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle the per-item Apply/Skip buttons on a weekly price-check digest."""
    query = update.callback_query
    await query.answer()
    try:
        _, action, item_id = query.data.split(":", 2)
    except ValueError:
        return
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass

    if action == "skip":
        return

    item = get_item(item_id)
    if item is None:
        await _safe_reply(query.message, "That item no longer exists.")
        return
    stats = latest_stats(item_id)
    price = stats.get("suggested_price") if stats else None
    if price is None:
        await _safe_reply(query.message, "No suggested price on file anymore — run /pricecheck again.")
        return

    listing = dict(item.get("listing") or {})
    if listing:
        listing["price"] = price
        update_field(item_id, "listing", listing)
    pricing = dict(item.get("pricing") or {})
    pricing["suggested_price"] = price
    pricing["confidence"] = "reprice"
    pricing["price_basis"] = "reprice"
    update_field(item_id, "pricing", pricing)

    offer_id = (item.get("ebay") or {}).get("offer_id")
    if offer_id:
        try:
            await asyncio.to_thread(update_offer_price, offer_id, price)
        except Exception as e:
            traceback.print_exc()
            _record_error(f"reprice apply ({item_id[:8]})", e)
            await _safe_reply(query.message,
                f"⚠️ Saved ${price} locally, but updating eBay failed: {type(e).__name__}: {str(e)[:200]}\n"
                "Use /setprice to retry pushing it to eBay.")
            return
    await _safe_reply(query.message, f"💲 Repriced {item_id[:8]} to ${price}.")


async def _weekly_reprice_loop(app: Application) -> None:
    """Runs for the lifetime of the bot: sleeps until the next configured weekly
    slot (REPRICE_WEEKDAY/REPRICE_HOUR_UTC), runs the price check, and posts the
    digest to every allowed user. A manual /pricecheck doesn't reset this clock."""
    while True:
        now = datetime.now(timezone.utc)
        days_ahead = (REPRICE_WEEKDAY - now.weekday()) % 7
        target = (now + timedelta(days=days_ahead)).replace(
            hour=REPRICE_HOUR_UTC, minute=0, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=7)
        await asyncio.sleep((target - now).total_seconds())

        print("📈 Running scheduled weekly price check...")
        try:
            results = await asyncio.to_thread(run_weekly_check)
        except Exception as e:
            traceback.print_exc()
            _record_error("weekly reprice", e)
            continue
        if not results:
            continue
        for uid in ALLOWED_USER_IDS:
            await _send_reprice_digest(app.bot, uid, results)


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

    await _safe_reply(update.message, f"🔄 Retrying {item_id[:8]} (status: {item['status']})...")
    # Route through the same dispatcher the happy path uses, so a retried item
    # stops at the identify/price confirm gates instead of blowing past them.
    await advance(user_id, item_id, update.message, context)


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

    try:
        status = await asyncio.to_thread(marketing_status)
        lines.append(f"✅ Promoted Listings (sell.marketing): {status}")
    except Exception as e:
        lines.append(
            f"❌ Promoted Listings (sell.marketing): {str(e)[:150]}\n"
            "   → if this is a scope/403 error, re-run `python -m ebay.auth` to re-consent."
        )

    try:
        status = await asyncio.to_thread(traffic_status)
        lines.append(f"✅ Traffic analytics (sell.analytics.readonly): {status}")
    except Exception as e:
        lines.append(
            f"❌ Traffic analytics (sell.analytics.readonly): {str(e)[:150]}\n"
            "   → if this is a scope/403 error, re-run `python -m ebay.auth` to re-consent."
        )

    try:
        status = await asyncio.to_thread(orders_status)
        lines.append(f"✅ Sold orders (sell.fulfillment.readonly): {status}")
    except Exception as e:
        lines.append(
            f"❌ Sold orders (sell.fulfillment.readonly): {str(e)[:150]}\n"
            "   → if this is a scope/403 error, re-run `python -m ebay.auth` to re-consent."
        )

    try:
        status = await asyncio.to_thread(finances_status)
        lines.append(f"✅ Order fees (sell.finances): {status}")
    except Exception as e:
        lines.append(
            f"❌ Order fees (sell.finances): {str(e)[:150]}\n"
            "   → if this is a scope/403 error, re-run `python -m ebay.auth` to re-consent."
        )

    for name in ("CLOUDINARY_CLOUD_NAME", "CLOUDINARY_API_KEY", "CLOUDINARY_API_SECRET"):
        lines.append(f"✅ {name}: set" if os.getenv(name) else f"❌ {name}: missing")

    await _safe_reply(update.message, "\n".join(lines))


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    async with _lock_for(user_id):
        data = pending.pop(user_id, None)
        if data and data.get("task") is not None:
            data["task"].cancel()
        ap = appending.pop(user_id, None)
        if ap and ap.get("task") is not None:
            ap["task"].cancel()
        was_appending = awaiting_photos.pop(user_id, None) is not None
    was_gated = gate.pop(user_id, None) is not None
    was_hauling = user_id in haul_mode
    haul_mode.discard(user_id)
    # Drop the whole queue too: cancelling one gate but leaving a haul's worth of
    # others armed would hand the user a prompt they thought they'd just dismissed.
    queued = len(gate_queue.pop(user_id, []) or [])

    if data:
        await _safe_reply(update.message, f"🛑 Cancelled capture ({len(data['files'])} photo(s) discarded).")
    elif was_appending:
        await _safe_reply(update.message, "🛑 Cancelled — stopped waiting for photos to add.")
    elif was_gated:
        extra = f" {queued} queued item(s) dropped too." if queued else ""
        await _safe_reply(
            update.message,
            f"🛑 Cancelled — the item is paused; use /retry to resume, or /delete it.{extra}")
    elif was_hauling:
        await _safe_reply(update.message, "🛑 Haul mode off.")
    else:
        await _safe_reply(update.message, "No capture in progress.")


# Single source of truth for /help and for Telegram's "/" autocomplete menu, so
# the two can't drift apart from each other or from the handlers registered below.
HELP_SECTIONS = [
    ("Getting started", [
        ("(send photos)", "Start a new item. 1st photo = cover, 2nd = tag close-up, last = the Ross price tag"),
        ("/haul", "Multi-item mode: dump a whole Ross run, split into items on each Ross tag"),
        ("wait", "Hold the photo-batching window open longer for a big batch"),
        ("confirm", "At a gate: accept the identification / price and continue"),
        ("(free text)", "At a gate: correct it (e.g. 'brand is Tommy Jeans, color navy'). At review: approve, reject, or a correction"),
        ("/cancel", "Discard photos being captured, or drop out of a gate"),
    ]),
    ("Items", [
        ("/status [status]", "List items and their stage. Filter, e.g. /status published"),
        ("/ross [missing]", "Audit Ross code + paid price per item; /ross missing shows only unpriced ones"),
        ("/listing [id]", "Show the current draft for an item"),
        ("/comps [id]", "Show the sold/active comps the price was built from"),
        ("/addphotos [id]", "Attach more photos to an existing item"),
        ("/receipt [id] <price> [code]", "Set the Ross cost by hand; code is optional (omit or 'none' to auto-fill)"),
    ]),
    ("Selling", [
        ("/setprice [id] <price>", "Set the price (35 -> $34.99); pushes to eBay if it already has an offer"),
        ("/setqty [id] <n>", "Set the quantity on eBay (once the item has an offer); default is 1"),
        ("/sync [days]", "Pull live price/quantity + real sold orders from eBay; auto-marks sold items with actual fees"),
        ("/activate [id]", "Publish an eBay draft — makes it live"),
        ("/end [id]", "End a live listing (drops to a draft; /activate to relist)"),
        ("/promote [id] <pct>", "Set the Promoted Listings ad rate (2-100%)"),
        ("/pricecheck", "Check live listings against eBay traffic/watchers; suggests price cuts (also runs weekly)"),
        ("/retry [id]", "Re-run the current pipeline step for an item"),
        ("/delete [id]", "Delete the item, its photos, and its eBay offer"),
    ]),
    ("Money", [
        ("/sold [id] [price]", "Mark an item sold and record the sale price; replies with profit"),
        ("/profit", "Profit per sold item: paid → sold → fees → net → margin (real fees after /sync)"),
        ("/report", "Excel report: photos, prices, fees, and profit"),
    ]),
    ("Admin", [
        ("/health", "Check eBay token, business policies, ad scope, Cloudinary"),
        ("/auth [url]", "Re-consent eBay: no argument prints the consent URL; paste the redirect URL to finish"),
        ("/whoami", "Your Telegram user id (for TELEGRAM_ALLOWED_USER_IDS)"),
        ("/errors", "Recent errors (the server console isn't visible from the phone)"),
        ("/help", "This list"),
    ]),
]


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """List every command and what it does."""
    lines = ["🤖 Ross Resale Bot — commands", ""]
    for title, entries in HELP_SECTIONS:
        lines.append(f"—— {title} ——")
        for cmd, desc in entries:
            lines.append(f"{cmd}")
            lines.append(f"    {desc}")
        lines.append("")
    lines.append("[id] = a full item id or a unique prefix (as shown by /status). "
                 "Leave it out to act on the most recent item.")
    await _send_chunked(update.message, lines)


async def _post_init(app: Application) -> None:
    """Register the command list with Telegram so typing '/' on the phone offers
    autocomplete. Built from HELP_SECTIONS so it stays in sync with /help."""
    commands = [
        BotCommand(cmd.lstrip("/").split()[0], desc[:256])
        for _, entries in HELP_SECTIONS
        for cmd, desc in entries
        if cmd.startswith("/")
    ]
    try:
        await app.bot.set_my_commands(commands)
        print(f"Registered {len(commands)} commands with Telegram's autocomplete menu.")
    except Exception as e:
        print(f"WARNING: set_my_commands failed (non-fatal): {type(e).__name__}: {e}")

    if ALLOWED_USER_IDS:
        app.create_task(_weekly_reprice_loop(app))
        print(f"Weekly price check scheduled: weekday={REPRICE_WEEKDAY} hour={REPRICE_HOUR_UTC} UTC.")
    else:
        print("⚠️ Weekly price check NOT scheduled — no TELEGRAM_ALLOWED_USER_IDS to notify. "
              "Use /pricecheck manually, or set the allowlist to enable it.")


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    err = context.error
    # Transient Telegram-side transport blips (502 Bad Gateway, request timeouts)
    # while long-polling are retried automatically by PTB's polling loop — the bot
    # keeps running, so they're not real failures. Log a one-liner instead of a
    # scary traceback and keep them OUT of /errors, so that ring buffer stays full
    # of things you can actually act on. BadRequest subclasses NetworkError but is a
    # genuine API error (e.g. message too long), so it's deliberately excluded here.
    if isinstance(err, (NetworkError, TimedOut)) and not isinstance(err, BadRequest):
        print(f"⚠️ Transient Telegram network error (auto-retried): {type(err).__name__}: {err}")
        return
    print(f"UNHANDLED ERROR: {type(err).__name__}: {err}")
    traceback.print_exception(type(err), err, err.__traceback__)
    if err is not None:
        _record_error("unhandled", err)


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
        # Process updates concurrently so a running pipeline / eBay call doesn't
        # stall other actions (e.g. reviewing item B while item A is pricing).
        .concurrent_updates(True)
        # Publishes the command list to Telegram so "/" offers autocomplete.
        .post_init(_post_init)
        .build()
    )
    if not ALLOWED_USER_IDS:
        print("⚠️ SECURITY: TELEGRAM_ALLOWED_USER_IDS is unset — the bot accepts "
              "commands from ANY Telegram user, on a live eBay account. Send /whoami "
              "to get your id, then set TELEGRAM_ALLOWED_USER_IDS=<id> in .env and restart.")

    app.add_error_handler(error_handler)
    # Auth gate first (group -1): blocks unauthorized users before any handler below.
    app.add_handler(TypeHandler(Update, _auth_guard), group=-1)
    app.add_handler(MessageHandler(filters.PHOTO, photo_handler))
    app.add_handler(CallbackQueryHandler(confirm_callback, pattern=r"^confirm:"))
    app.add_handler(CallbackQueryHandler(review_callback, pattern=r"^review:"))
    app.add_handler(CallbackQueryHandler(reprice_callback, pattern=r"^reprice:"))
    app.add_handler(CommandHandler("haul", haul_command))
    app.add_handler(CommandHandler("status", status_command))
    app.add_handler(CommandHandler("ross", ross_command))
    app.add_handler(CommandHandler("errors", errors_command))
    app.add_handler(CommandHandler("comps", comps_command))
    app.add_handler(CommandHandler("sold", sold_command))
    app.add_handler(CommandHandler("profit", profit_command))
    app.add_handler(CommandHandler("report", report_command))
    app.add_handler(CommandHandler("listing", listing_command))
    app.add_handler(CommandHandler("receipt", receipt_command))
    app.add_handler(CommandHandler("addphotos", addphotos_command))
    app.add_handler(CommandHandler("delete", delete_command))
    app.add_handler(CommandHandler("end", end_command))
    app.add_handler(CommandHandler("setprice", setprice_command))
    app.add_handler(CommandHandler("setqty", setqty_command))
    app.add_handler(CommandHandler("sync", sync_command))
    app.add_handler(CommandHandler("activate", activate_command))
    app.add_handler(CommandHandler("promote", promote_command))
    app.add_handler(CommandHandler("pricecheck", pricecheck_command))
    app.add_handler(CommandHandler("retry", retry_command))
    app.add_handler(CommandHandler("health", health_command))
    app.add_handler(CommandHandler("auth", auth_command))
    app.add_handler(CommandHandler("whoami", whoami_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("cancel", cancel_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    app.run_polling()


if __name__ == "__main__":
    main()
