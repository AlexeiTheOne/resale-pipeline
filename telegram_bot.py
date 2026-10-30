import os, sys, re, asyncio, shutil, tempfile, time, traceback
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from dotenv import load_dotenv
import httpx
from telegram import (
    BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto, Update,
)
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
    DATA_DIR, LOG_DIR, GEMINI_PHOTO_LIMIT,
    REPRICE_WEEKDAY, REPRICE_HOUR_UTC,
    SYNC_ENABLED, SYNC_HOUR_UTC, SYNC_DAYS, JOB_CATCHUP_GRACE_HOURS,
    SOLD_ALERTS_ENABLED, SOLD_ALERT_MINUTES,
    FEEDBACK_ENABLED, FEEDBACK_HOUR_UTC, FEEDBACK_COMMENT,
    OFFER_MESSAGE,
    WATCHER_OFFER_PCT,
)
import db
from db import (
    clear_feedback_failure, create_item, delete_item, get_item, job_run_status,
    known_feedback_failures, last_job_run, list_items, mark_not_duplicate,
    record_feedback_failure, record_job_run, update_field, update_status, VALID_STATUSES,
    record_offer_sent,
)
import identify
import llm_usage
import ross_tag
from pipeline.reprice import run_weekly_check, send_planned_offers
from profit import format_projection, project
from ebay.auth import get_access_token
from ebay.analytics import traffic_status
from ebay.orders import (
    finances_status, orders_status,
)
from ebay.feedback import leave_all_pending
from ebay.listings import audit_listing_photos, listings_status
from ebay.inventory import (
    create_draft_offer,
    delete_offer,
    get_policy_id,
    _order_is_fixed,
    listing_photo_order,
    publish_offer,
    refresh_offer_description,
    save_stock_photo,
    update_offer_price,
    update_offer_quantity,
    withdraw_offer,
    TitleTooLongError,
)
from ebay.marketing import marketing_status
from ebay.negotiation import (
    negotiation_status, send_offer,
)

# Split-out modules (docs/cleanup-plan.md Phase 2). The state names are imported
# directly for now; they are the same dict/set objects as bot.state's, mutated in
# place, never rebound. Names unused here are re-exported for the tests (tb.<name>).
from bot.state import (  # noqa: F401  (re-export for tests)
    pending, review, locks, last_item, capture_window, staging, awaiting_photos,
    appending, gate, gate_queue, find_mode, find_loc, last_find_photo, quick_add,
    inv_box, inv_session, list_after_photos, haul_mode, msg_target, MSG_TARGET_MAX,
    _in_flight,
)
from bot.util import (  # noqa: F401  (re-export for tests)
    _parse_allowed_ids, _remember_target, _replied_item, _lock_for, _secrets, _scrub,
    _safe_reply, TELEGRAM_MAX_CHARS, _send_chunked, _chunk_lines, _send_chunked_to,
    _recent_errors, _record_error, _resolve_item_id, _ITEM_ID_PREFIX_LEN,
    _names_an_item, _claim, _release,
)
from bot.formatting import (  # noqa: F401  (re-export for tests)
    _title_too_long_text, _fmt, format_identification, _EVIDENCE_LABEL, _short,
    _fmt_money, _format_pricing_v2, format_pricing, _price_evidence_line, format_draft,
)

from pipeline.stages import (  # noqa: F401  (re-export)
    stage_identify as _stage_identify, stage_price as _stage_price,
    stage_draft as _stage_draft, brand_warnings as _brand_warnings,
    duplicate_warnings as _duplicate_warnings, find_tag_photo as _find_tag_photo,
    split_receipt as _split_receipt,
)
from pipeline.sync import run_sync, sync_one as _sync_one  # noqa: F401  (re-export)
from pipeline.offers import offer_plan as _offer_plan  # noqa: F401  (re-export)
from profit import (  # noqa: F401  (re-export)
    sale_economics as _sale_economics, sale_profit as _sale_profit,
)
from db import (  # noqa: F401  (re-export)
    REAL_CODE_FLOOR as _REAL_CODE_FLOOR, match_quantity_to_pieces as _match_quantity_to_pieces,
    next_receipt_code as _next_receipt_code, set_location as _set_location,
)

from bot import flow, intake, text_router
from bot.intake import (  # noqa: F401  (re-export)
    INBOX, DEFAULT_CAPTURE_WINDOW, EXTENDED_CAPTURE_WINDOW, WAIT_EXTENSION,
    finalize_capture, _is_ross_tag, _split_haul, _run_haul, _haul_digest, haul_command,
    _confirmation_markup, _send_confirmation, _await_more_photos, _finalize_append,
    addphotos_command,
)
from bot.flow import (  # noqa: F401  (re-export)
    _pipeline_semaphore, _review_markup, _notifier, _run_stage, _retry_markup,
    _show_review, _publish_and_report, _publish_and_report_claimed,
    _identify_gate_reason, _price_gate_reason, advance, _advance_one, _gate_is_busy,
    _enqueue_gate, _activate_next_gate, _show_identify_gate, _show_price_gate,
    confirm_callback, _approve_item, review_callback, _activate_item, _promote_item,
    _retry_item,
)
from bot.text_router import (  # noqa: F401  (re-export)
    _PRICE_CORRECTION, _price_from_correction, _apply_manual_price, _charm_price,
    _parse_price_override, _CONFIRM_WORDS, _explicit_target, _handle_targeted,
    _handle_gate, text_handler, _floor_refusal,
)

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")


# Whitelist of Telegram user IDs allowed to drive the bot. This gates a LIVE eBay
# seller account (publish / delete / ad-rate), so an open bot means anyone who
# finds the username can act on the account. When unset the bot stays open (so an
# existing install isn't bricked on upgrade) but prints a loud startup warning;
# use /whoami to get your id, then set TELEGRAM_ALLOWED_USER_IDS in .env.
ALLOWED_USER_IDS = _parse_allowed_ids(os.getenv("TELEGRAM_ALLOWED_USER_IDS"))

# Upper bound on a hand-typed /setqty. Ross stock is near-always single units and
# the largest real listing so far held 5, so anything past this is a typo or a
# mis-parsed item id rather than an inventory decision.
_MAX_SANE_QTY = 500
# Same idea for a price. The dearest item handled so far listed around $200, so
# five figures is not a decision anyone makes by typing into a chat window — it's
# an item id, a barcode, or a slipped decimal point.
_MAX_SANE_PRICE = 10_000.0


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

    if user_id in inv_box:
        await _inv_photo(user_id, update.message, bytes(data), mid)
        return
    if user_id in quick_add:
        await _quick_add_photo(user_id, update.message, bytes(data), mid)
        return
    if user_id in find_mode:
        await _find_one(user_id, update.message, bytes(data), mid)
        return

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
                intake._finalize_append(user_id, target, update, context, _resync_photos)
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
                intake._await_more_photos(user_id, st["item_id"], context, intake.WAIT_EXTENSION)
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
                intake.finalize_capture(user_id, update, context)
            )
    print(f"📷 Capture: photo appended, {count} buffered for user {user_id}")


_FIND_DIR = DATA_DIR / "cache" / "find"


async def find_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Turn on lookup mode: every photo is checked against what's listed.
    /find G3 also files everything scanned under that box."""
    user_id = update.effective_user.id
    find_mode.add(user_id)
    loc = " ".join(context.args or []).strip().upper() or None
    if loc:
        find_loc[user_id] = loc
    else:
        find_loc.pop(user_id, None)
    where = f" Box: {loc}." if loc else ""
    await _safe_reply(update.message,
        f"🔎 Find mode on.{where} Send a photo of an item (hang tag or front works best) "
        "and I'll say if it's posted and how many eBay shows.\n"
        "/find G3 to set the box · /done when finished.")


async def done_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if user_id in inv_box:
        await _inv_done(user_id, update.message)
        return
    if user_id in find_mode:
        _end_find(user_id)
        await _safe_reply(update.message, "👍 Find mode off. Photos start new listings again.")
    else:
        await _safe_reply(update.message, "Nothing to finish.")


def _end_find(user_id: int) -> None:
    find_mode.discard(user_id)
    find_loc.pop(user_id, None)
    old = last_find_photo.pop(user_id, None)
    if old:
        Path(old).unlink(missing_ok=True)
    qa = quick_add.pop(user_id, None)
    if qa and qa.get("task"):
        qa["task"].cancel()


async def _find_one(user_id: int, message, data: bytes, mid: int) -> None:
    from pipeline.stock_lookup import format_answer, lookup
    _FIND_DIR.mkdir(parents=True, exist_ok=True)
    path = _FIND_DIR / f"find_{mid}.jpg"
    path.write_bytes(data)
    old = last_find_photo.get(user_id)
    if old and old != str(path):
        Path(old).unlink(missing_ok=True)
    last_find_photo[user_id] = str(path)
    loc = find_loc.get(user_id)
    try:
        res = await asyncio.to_thread(lookup, str(path))
    except Exception as e:
        traceback.print_exc()
        await _safe_reply(message, f"⚠️ Lookup failed: {type(e).__name__}: {str(e)[:200]}")
        return
    text = format_answer(res)
    if res["item"] is not None:
        _set_location(res["item"]["item_id"], loc)
        if loc:
            text += f"\n📦 Filed under {loc}"
        await _safe_reply(message, text)
        return
    markup = InlineKeyboardMarkup([[InlineKeyboardButton(
        "⚡ Quick add", callback_data="qa:start")]])
    await _safe_reply(message, text, reply_markup=markup)


_QUICK_ADD_WAIT_FIRST = 45   # seconds to wait for extra photos after the tap
_QUICK_ADD_WAIT_NEXT = 6     # seconds after the last photo


async def quick_add_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass
    seed = last_find_photo.pop(user_id, None)
    old = quick_add.pop(user_id, None)
    if old and old.get("task"):
        old["task"].cancel()
    qa = quick_add[user_id] = {"photos": [seed] if seed and Path(seed).exists() else [],
                               "task": None, "loc": find_loc.get(user_id)}
    await _safe_reply(query.message,
        "📸 Send 1–2 more photos: front, brand or hang tag, and the Ross tag if it's still on. "
        f"I'll start {_QUICK_ADD_WAIT_NEXT}s after the last one (or in "
        f"{_QUICK_ADD_WAIT_FIRST}s with just the photo you sent).")
    qa["task"] = asyncio.create_task(
        _quick_add_timer(user_id, query.message, _QUICK_ADD_WAIT_FIRST))


async def _quick_add_photo(user_id: int, message, data: bytes, mid: int) -> None:
    qa = quick_add[user_id]
    _FIND_DIR.mkdir(parents=True, exist_ok=True)
    path = _FIND_DIR / f"qa_{mid}.jpg"
    path.write_bytes(data)
    qa["photos"].append(str(path))
    if qa.get("task"):
        qa["task"].cancel()
    qa["task"] = asyncio.create_task(_quick_add_timer(user_id, message, _QUICK_ADD_WAIT_NEXT))


async def _quick_add_timer(user_id: int, message, wait: int) -> None:
    try:
        await asyncio.sleep(wait)
    except asyncio.CancelledError:
        return
    qa = quick_add.pop(user_id, None)
    if not qa or not qa["photos"]:
        await _safe_reply(message, "No photos for Quick add, so nothing saved.")
        return
    await _safe_reply(message, f"⚡ Identifying and pricing ({len(qa['photos'])} photo(s))...")
    try:
        item_id = await asyncio.to_thread(_quick_add_item, qa["photos"], qa["loc"])
    except Exception as e:
        traceback.print_exc()
        _record_error("quick add", e)
        await _safe_reply(message, f"⚠️ Quick add failed: {type(e).__name__}: {str(e)[:200]}")
        return
    lines = [_stock_line(get_item(item_id), detail=True)]
    lines += _duplicate_warnings(item_id) + _brand_warnings(item_id)
    lines.append(f"/list {item_id[:8]} when you're ready to post it.")
    await _safe_reply(message, "\n".join(lines))


def _quick_add_item(paths: list[str], loc: str | None) -> str:
    """Photos -> a 'stocked' item: identified and quick-priced, not listed.
    The Ross tag is only peeled when it's actually recognised; Quick add has
    no 'tag is the last photo' convention to fall back on."""
    from pipeline.quick_price import quick_price
    item_id = create_item([])
    folder = INBOX / item_id
    folder.mkdir(parents=True, exist_ok=True)
    photos = []
    for n, p in enumerate(paths):
        dst = folder / f"{item_id}_{n}.jpg"
        shutil.move(p, dst)
        photos.append(str(dst))
    receipt = None
    found = _find_tag_photo(photos) if len(photos) >= 2 else None
    if found:
        tag = photos.pop(found[0])
        receipt = ross_tag.extract_receipt(tag)
        receipt["peeled"] = True
        receipt["photo"] = tag
        update_field(item_id, "receipt", receipt)
    update_field(item_id, "photos", photos)
    ident = identify.identify_item(photos[:GEMINI_PHOTO_LIMIT], scan_paths=photos)
    update_field(item_id, "identification", ident)
    priced = quick_price(ident, photos[0], receipt, item_id=item_id)
    update_field(item_id, "stock", {"location": loc, "quick_price": priced,
                                    "added_at": datetime.now(timezone.utc).isoformat()})
    update_status(item_id, "stocked")
    return item_id


def _stock_line(item: dict, detail: bool = False) -> str:
    ident = item.get("identification") or {}
    stock = item.get("stock") or {}
    qp = stock.get("quick_price") or {}
    name = " ".join(str(x) for x in (ident.get("brand"), ident.get("product_name")
                                    or ident.get("item_type")) if x)[:55]
    cost = (item.get("receipt") or {}).get("reduced_price")
    bits = [f"${qp.get('price')}" if qp.get("price") else "no price"]
    if cost:
        bits.append(f"cost ${cost}")
    if stock.get("location"):
        bits.append(stock["location"])
    line = f"📦 {item['item_id'][:8]} {name} · " + " · ".join(bits)
    if detail and qp.get("reasoning"):
        line = "✅ Saved, not posted.\n" + line + f"\n💡 {qp['reasoning']}"
    return line


async def stock_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Everything quick-added but not posted yet, best to list first."""
    items = list_items("stocked")
    if not items:
        await _safe_reply(update.message, "Nothing waiting. Use /find, then ⚡ Quick add on "
                                          "anything that isn't posted.")
        return
    verdicts = {}
    norm = lambda b: b  # noqa: E731
    try:
        from pipeline.scoreboard import load, normalize_brand
        verdicts = {g["name"]: g["verdict"] for g in load()["brands"]}
        norm = normalize_brand
    except Exception as e:
        print(f"WARNING: scoreboard unavailable for /stock: {e}")
    rank = {"buy more": 0, "ok": 1, "pause": 2, "stop": 3}

    def verdict_of(it):
        return verdicts.get(norm((it.get("identification") or {}).get("brand")))

    def price_of(it):
        return ((it.get("stock") or {}).get("quick_price") or {}).get("price") or 0

    items.sort(key=lambda it: (rank.get(verdict_of(it) or "ok", 1), -price_of(it)))
    total = sum(price_of(i) for i in items)
    cost = sum((i.get("receipt") or {}).get("reduced_price") or 0 for i in items)
    lines = [f"📦 Not posted: {len(items)} item(s), worth ~${total:,.0f} (cost ${cost:,.0f})",
             "List from the top: best sellers first.", ""]
    for it in items:
        v = verdict_of(it)
        flag = f" ⚠️{v}" if v in ("pause", "stop") else ""
        lines.append(_stock_line(it) + flag)
    lines += ["", "/list <id> to post one."]
    await _send_chunked(update.message, lines)


_PIECES_DIR = DATA_DIR / "pieces"
_inv_sem = None


def _inv_semaphore() -> asyncio.Semaphore:
    global _inv_sem
    if _inv_sem is None:
        _inv_sem = asyncio.Semaphore(3)
    return _inv_sem


async def inv_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Inventory mode: /inv G3, then one photo per item with the tag showing."""
    from pipeline.inventory import BOX_RE
    user_id = update.effective_user.id
    box = " ".join(context.args or []).strip().upper()
    if not box or not BOX_RE.match(box):
        await _safe_reply(update.message,
            "Usage: /inv G3 — then one photo per item, tag showing.\n"
            "Boxes: G garage, O office, S small bedroom, R full room (G1, O2, R-SHELF1).")
        return
    _end_find(user_id)
    inv_box[user_id] = box
    inv_session.setdefault(user_id, [])
    await _safe_reply(update.message,
        f"📦 Inventory mode: {box}. Send one photo per item with the tag showing; "
        f"each gets the next code ({db.next_piece_code(box)} first). Type another box "
        "code (e.g. G4) to switch. /done for the summary.")


async def _inv_photo(user_id: int, message, data: bytes, mid: int) -> None:
    from db import add_piece, update_piece
    box = inv_box[user_id]
    _PIECES_DIR.mkdir(parents=True, exist_ok=True)
    code = add_piece(box)
    path = _PIECES_DIR / f"{code}.jpg"
    path.write_bytes(data)
    update_piece(code, photo=str(path))
    inv_session.setdefault(user_id, []).append(code)
    sent = await _safe_reply(message, f"{code} ✓")
    asyncio.create_task(_inv_process(code, sent))


async def _inv_process(code: str, sent) -> None:
    from pipeline.inventory import describe, process_piece
    async with _inv_semaphore():
        try:
            res = await asyncio.to_thread(process_piece, code)
            text = describe(res)
        except Exception as e:
            traceback.print_exc()
            _record_error(f"inventory {code}", e)
            text = f"{code} ✓ saved, but reading it failed ({type(e).__name__}). /where {code}"
    if sent is not None:
        try:
            await sent.edit_text(_scrub(text))
            return
        except Exception as e:
            print(f"WARNING: couldn't edit {code} reply: {e}")


async def _inv_done(user_id: int, message) -> None:
    from pipeline.inventory import box_summary
    codes = inv_session.pop(user_id, [])
    inv_box.pop(user_id, None)
    # Give pieces still being read a moment to finish.
    from db import get_piece
    for _ in range(30):
        if all((get_piece(c) or {}).get("item_id") for c in codes):
            break
        await asyncio.sleep(2)
    await _send_chunked(message, box_summary(codes) + ["", "/stock for what to list · /ghosts after the whole house"])


async def where_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/where G3-07 · /where G3 · /where oakley · /where 198190095323"""
    from pipeline.inventory import piece_line, search
    q = " ".join(context.args or []).strip()
    if not q:
        await _safe_reply(update.message, "Usage: /where G3-07 · /where G3 · /where oakley · /where <UPC>")
        return
    found = await asyncio.to_thread(search, q)
    if not found:
        await _safe_reply(update.message, f"Nothing found for '{q}'.")
        return
    items = {i["item_id"]: i for i in list_items()}
    lines = [f"🔎 {len(found)} piece(s) for '{q}':"] + [piece_line(p, items) for p in found[:40]]
    if len(found) > 40:
        lines.append(f"... and {len(found) - 40} more")
    await _send_chunked(update.message, lines)


async def move_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/move G3-07 R2 — the code stays, the location changes."""
    from db import get_piece, update_piece
    from pipeline.inventory import BOX_RE
    args = [a.upper() for a in (context.args or [])]
    if len(args) != 2 or not BOX_RE.match(args[1]):
        await _safe_reply(update.message, "Usage: /move G3-07 R2")
        return
    p = get_piece(args[0])
    if not p:
        await _safe_reply(update.message, f"No piece {args[0]}.")
        return
    update_piece(p["code"], box=args[1])
    await _safe_reply(update.message, f"👍 {p['code']} is now in {args[1]} (its code stays {p['code']}).")


async def mark_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/mark G3-07 lot|donated|home|sold"""
    from db import PIECE_STATUSES, get_piece, update_piece
    args = context.args or []
    if len(args) != 2 or args[1].lower() not in PIECE_STATUSES:
        await _safe_reply(update.message, "Usage: /mark G3-07 lot  (lot, donated, home, sold)")
        return
    p = get_piece(args[0])
    if not p:
        await _safe_reply(update.message, f"No piece {args[0].upper()}.")
        return
    update_piece(p["code"], status=args[1].lower())
    await _safe_reply(update.message, f"👍 {p['code']} marked {args[1].lower()}.")


async def ghosts_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Live listings vs pieces found at home. Meaningful after the whole house."""
    from pipeline.inventory import ghost_report
    await _safe_reply(update.message, "🔎 Checking every live listing against what's at home...")
    lines = await asyncio.to_thread(ghost_report)
    await _send_chunked(update.message, lines)


async def _sold_alert_loop(app: Application) -> None:
    """Every SOLD_ALERT_MINUTES: announce new orders with where to pull them."""
    from db import mark_orders_seen, seen_order_ids
    from ebay.orders import get_orders
    from pipeline.inventory import order_alerts
    first = True
    while True:
        try:
            orders = await asyncio.to_thread(get_orders, 3 if not first else 30)
            seen = seen_order_ids()
            if first and not seen:
                # First run ever: everything before today is history, not news.
                mark_orders_seen([o.get("orderId") for o in orders if o.get("orderId")])
            else:
                lines, handled = await asyncio.to_thread(order_alerts, orders, seen)
                mark_orders_seen(handled)
                for uid in ALLOWED_USER_IDS:
                    for line in lines:
                        try:
                            await app.bot.send_message(uid, _scrub(line))
                        except Exception as e:
                            print(f"WARNING: sold alert to {uid} failed: {e}")
        except Exception as e:
            print(f"WARNING: sold-alert poll failed: {type(e).__name__}: {e}")
        first = False
        await asyncio.sleep(SOLD_ALERT_MINUTES * 60)


async def list_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Turn a quick-added item into a real listing: full pricing, draft, review."""
    user_id = update.effective_user.id
    item_id, err = _resolve_item_id(user_id, context.args or [])
    if err:
        await _safe_reply(update.message, f"⚠️ {err}")
        return
    item = get_item(item_id)
    if item["status"] != "stocked":
        await _safe_reply(update.message,
                          f"{item_id[:8]} is '{item['status']}', not waiting stock.")
        return
    _match_quantity_to_pieces(item_id)
    if (item.get("stock") or {}).get("source") == "inventory":
        # The inventory photo shows the Ross tag (our price): never list it.
        # Take real listing photos, peel the tag off those, then carry on.
        async with _lock_for(user_id):
            awaiting_photos[user_id] = item_id
            appending.pop(user_id, None)
            list_after_photos[user_id] = item_id
        stock = dict(item.get("stock") or {})
        stock["inventory_photo"] = (item.get("photos") or [None])[0]
        update_field(item_id, "stock", stock)
        update_field(item_id, "photos", [])
        await _safe_reply(update.message,
            f"📸 Send the listing photos for {item_id[:8]} now, Ross tag last as usual. "
            "I'll price and draft it once they stop arriving.")
        return
    update_status(item_id, "identified")
    await _safe_reply(update.message,
        f"🚀 Listing {item_id[:8]}. Full pricing next, then the draft for review.\n"
        f"Want more photos on the listing? /addphotos {item_id[:8]} any time before you approve.")
    await flow.advance(user_id, item_id, update.message, context)


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
    except TitleTooLongError as e:
        await _safe_reply(message, "Photos saved, but the eBay listing was not updated.\n"
                          + _title_too_long_text(e, item_id))
        return
    except Exception as e:
        traceback.print_exc()
        await _safe_reply(message,
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
    fresh = (get_item(item_id) or {}).get("ebay") or {}
    merged = {**fresh, **result}
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


async def _send_photo_index(item: dict, message) -> None:
    """Send the item's photos as albums, in listing order, with a numbered key.

    Telegram shows an album in order but puts no visible number on each frame, so
    the text index is what makes 'photo 3' unambiguous. Albums cap at 10, so a big
    set goes out in batches."""
    ordered = listing_photo_order(item)
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
            await _safe_reply(message, f"⚠️ Couldn't send photos {start + 1}-{start + len(batch)}: "
                                       f"{type(e).__name__}")

    lines = [f"🖼️ {item['item_id'][:8]} — {len(existing)} photo(s), in listing order:",
             "  #1 is the gallery cover (the thumbnail buyers see in search)."]
    if missing:
        lines.append(f"  ⚠️ {missing} file(s) missing from disk and skipped.")
    if not _order_is_fixed(item):
        lines.append("  Order is automatic: your 2nd photo (the tag) is moved to the end.")
    lines += ["", "  /cover <n> — make photo n the cover",
              "  /arrange <order> — reorder, e.g. /arrange 3,1,2"]
    await _safe_reply(message, "\n".join(lines))


async def photos_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show an item's photos in listing order, numbered. Usage: /photos [id]."""
    user_id = update.effective_user.id
    item_id, err = _resolve_item_id(user_id, context.args)
    if err:
        await _safe_reply(update.message, f"⚠️ {err}")
        return
    item = get_item(item_id)
    if item is None:
        await _safe_reply(update.message, f"No item found for {item_id[:8]}.")
        return
    last_item[user_id] = item_id
    if not (item.get("photos") or []):
        await _safe_reply(update.message, f"{item_id[:8]} has no photos. /addphotos to add some.")
        return
    await _send_photo_index(item, update.message)


async def _apply_photo_order(item_id: str, new_order: list[str], message, note: str) -> None:
    """Persist a hand-set photo order and push it to eBay.

    Marks the layout manual so the builder stops applying its own reorder — the
    stored list is now exactly what the listing should show."""
    update_field(item_id, "photos", new_order)
    update_field(item_id, "photo_layout", {"manual": True})
    await _safe_reply(message, note)
    await _resync_photos(item_id, message)


def _photo_args(user_id, args: list[str]) -> tuple[str | None, str | None, str | None]:
    """(item_id, spec, error) from '<id> <spec>' or just '<spec>' — the trailing
    argument is the instruction, anything before it names the item (same shape as
    /promote, so the id stays optional)."""
    if not args:
        return None, None, "missing argument"
    item_id, err = _resolve_item_id(user_id, args[:-1])
    if err:
        return None, None, err
    return item_id, args[-1], None


async def cover_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Make one photo the gallery cover. Usage: /cover <n> or /cover <id> <n>,
    where n is the number shown by /photos."""
    user_id = update.effective_user.id
    item_id, spec, err = _photo_args(user_id, list(context.args or []))
    if err:
        await _safe_reply(update.message,
            f"⚠️ {err}\nUsage: /cover <n>  (see /photos for the numbers)")
        return

    item = get_item(item_id)
    if item is None:
        await _safe_reply(update.message, f"No item found for {item_id[:8]}.")
        return
    ordered = listing_photo_order(item)
    try:
        n = int(spec)
    except ValueError:
        await _safe_reply(update.message, f"'{spec}' isn't a photo number. Usage: /cover 3")
        return
    if not 1 <= n <= len(ordered):
        await _safe_reply(update.message,
            f"{item_id[:8]} has {len(ordered)} photo(s), so pick 1-{len(ordered)}.")
        return

    last_item[user_id] = item_id
    if n == 1:
        await _safe_reply(update.message, f"Photo #1 is already the cover for {item_id[:8]}.")
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
        await _safe_reply(update.message,
            f"⚠️ {err}\nUsage: /arrange 3,1,2  (see /photos for the numbers)")
        return

    item = get_item(item_id)
    if item is None:
        await _safe_reply(update.message, f"No item found for {item_id[:8]}.")
        return
    ordered = listing_photo_order(item)

    picks, seen = [], set()
    for token in re.split(r"[,\s]+", spec.strip()):
        if not token:
            continue
        try:
            n = int(token)
        except ValueError:
            await _safe_reply(update.message,
                f"'{token}' isn't a photo number. Usage: /arrange 3,1,2")
            return
        if not 1 <= n <= len(ordered):
            await _safe_reply(update.message,
                f"{item_id[:8]} has {len(ordered)} photo(s), so pick 1-{len(ordered)} (got {n}).")
            return
        if n in seen:
            await _safe_reply(update.message, f"Photo {n} is listed twice — each one once, please.")
            return
        seen.add(n)
        picks.append(ordered[n - 1])

    if not picks:
        await _safe_reply(update.message, "Usage: /arrange 3,1,2  (see /photos for the numbers)")
        return

    last_item[user_id] = item_id
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
        await _safe_reply(update.message,
            f"⚠️ {err}\nUsage: /stockphoto <url>  (right-click the photo → Copy image address)")
        return
    if not url.lower().startswith(("http://", "https://")):
        await _safe_reply(update.message,
            f"'{url[:60]}' isn't a URL. It needs to be the direct image address "
            "(right-click the photo → Copy image address), not the product page.")
        return

    last_item[user_id] = item_id
    await _safe_reply(update.message, "⬇️ Fetching that image...")
    try:
        await asyncio.to_thread(save_stock_photo, item_id, url)
    except ValueError as e:
        await _safe_reply(update.message, f"⚠️ {e}")
        return
    except Exception as e:
        traceback.print_exc()
        await _safe_reply(update.message, f"⚠️ Couldn't save it: {type(e).__name__}: {str(e)[:200]}")
        return

    n = len(listing_photo_order(get_item(item_id) or {}))
    await _safe_reply(update.message,
        f"🖼️ Added as photo #2 of {n} for {item_id[:8]}. /photos to see them, "
        "/arrange to move it.")
    await _resync_photos(item_id, update.message)


# How many items /status lists by default. The full inventory is ~90 items and
# growing, which is several screens of scrolling to reach the ones you're actually
# working on. The per-status counts in the header still cover everything, so
# nothing is hidden — only the row list is trimmed.
STATUS_DEFAULT_LIMIT = 20


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """List items and their pipeline status, most recent last (nearest your
    keyboard). Shows the newest STATUS_DEFAULT_LIMIT by default; the header counts
    always cover everything.

      /status              the 20 most recent
      /status published    ...with that status
      /status 50           a different number
      /status all          every item, as before

    Paginated so it never exceeds Telegram's message-size limit."""
    all_items = list_items()
    if not all_items:
        await _safe_reply(update.message, "No items yet.")
        return

    # Header: a count per status across everything, so you see the funnel at a glance.
    counts = Counter(i["status"] for i in all_items)
    ordered = VALID_STATUSES + sorted(s for s in counts if s not in VALID_STATUSES)
    header = "📊 " + " · ".join(f"{s}:{counts[s]}" for s in ordered if counts[s])

    # Args in any order: a status filter, a row count, or "all" for no limit.
    limit = STATUS_DEFAULT_LIMIT
    status_filter = None
    for arg in (a.lower() for a in (context.args or [])):
        if arg in ("all", "full"):
            limit = None
        elif arg.isdigit():
            limit = max(int(arg), 1)
        else:
            status_filter = arg

    if status_filter:
        items = [i for i in all_items if i["status"] == status_filter]
        if not items:
            await _safe_reply(update.message, f"{header}\n\nNo items with status '{status_filter}'.")
            return
    else:
        items = all_items

    items.sort(key=lambda i: i["created_at"])
    lines = [header]
    if limit is not None and len(items) > limit:
        hidden = len(items) - limit
        items = items[-limit:]   # the tail: newest, and last on screen
        lines.append(f"Showing the {limit} most recent · {hidden} older hidden "
                     f"(/status all, or /status {min(len(all_items), 50)})")
    lines.append("")
    for item in items:
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

    if pricing.get("engine") == "v2":
        lines.append("(v2: same-item comps only, prices include shipping)")
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


async def sold_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Mark an item sold and record the sale price, replying with the profit vs.
    what you paid Ross. Usage: /sold [id] [price]. The price is optional (defaults
    to the listed price); a trailing number is taken as the sale price."""
    user_id = update.effective_user.id
    args = list(context.args)

    sale_price = None
    id_args = args
    # Item id first, number second — see _names_an_item. An all-digit id prefix
    # otherwise parses as a price and retargets the command at last_item.
    if args and not _names_an_item(args[-1]):
        maybe = args[-1].replace("$", "").replace(",", "")
        try:
            sale_price = round(float(maybe), 2)
            id_args = args[:-1]
        except ValueError:
            sale_price = None  # trailing arg wasn't a number — treat it all as the id
        if sale_price is not None and sale_price <= 0:
            await _safe_reply(update.message, f"'{args[-1]}' isn't a valid sale price.")
            return

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
        # A hand-typed price contradicts every figure /sync derived from the
        # order it replaces. Leaving them behind mixes one order's fees and unit
        # count with another order's revenue, and `fees_known` would keep
        # presenting the result as measured. Drop them and fall back to the
        # modelled rates until the next /sync supplies real ones.
        for derived in ("net_proceeds", "ebay_fees", "shipping_collected",
                        "shipping_label_cost", "units_sold", "order_id",
                        "order_ids", "fees_known", "ad_fees", "refunded",
                        "gross_sale_price", "first_sold_at", "sales"):
            ebay.pop(derived, None)
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


async def _item_profit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """One item's money, line by line — /profit <id>."""
    user_id = update.effective_user.id
    item_id, err = _resolve_item_id(user_id, context.args)
    if err:
        await _safe_reply(update.message, f"⚠️ {err}")
        return
    item = get_item(item_id)
    if item is None:
        await _safe_reply(update.message, f"No item found for {item_id[:8]}.")
        return
    last_item[user_id] = item_id

    projection = project(item)
    if projection is None:
        await _safe_reply(update.message,
            f"{item_id[:8]} has no price yet (status '{item['status']}') — nothing to compute.")
        return
    title = ((item.get("listing") or {}).get("title") or "(untitled)")[:48]
    verb = "Realized" if projection["actual"] else "Projected"
    margin = (f" · {projection['margin'] * 100:.0f}% margin"
              if projection["margin"] is not None else "")
    header = (f"💰 {item_id[:8]} · {title}\n"
              f"{verb}: ${projection['net']:.2f} net{margin}")
    sent = await _safe_reply(update.message, format_projection(projection, header=header))
    _remember_target(sent, item_id)


async def profit_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/profit summarizes every sold item: per-item paid → sold → fees → net →
    margin, plus the totals. Uses REAL eBay fees for items /sync has pulled an
    order for; items without one fall back to a gross number (fees unknown).
    Run /sync first to fill in real fees.

    /profit <id> instead breaks ONE item down line by line — the same figures
    posted when it went live, so you can pull them back up after a price change
    without waiting for a /report."""
    if context.args:
        await _item_profit(update, context)
        return

    # Anything that has actually taken money, not just anything flagged 'sold'. A
    # partially-sold listing goes back to 'published' once /sync confirms it still
    # has stock live, and its realized sale must not vanish from the totals with it.
    sold = [i for i in list_items()
            if i["status"] == "sold" or (i.get("ebay") or {}).get("sale_price") is not None]
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


async def sync_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Reconcile the local DB with eBay: walk every item that has an eBay offer,
    pull its current price and quantity from eBay, and write back anything that
    changed (e.g. a price or quantity you edited by hand in Seller Hub). Also pulls
    real sold orders — an item eBay has an order for is marked sold automatically
    with the actual sale price and eBay fees (so /profit shows real net/margin).
    Listings that ended without a matching order are flagged for a manual /sold.
    Usage: /sync [days] (order look-back window, default 90)."""
    days = _sync_days(context.args)

    async def _ping(text: str) -> None:
        await _safe_reply(update.message, text)

    lines, _ = await _run_sync(days, on_progress=_ping)
    await _send_chunked(update.message, lines)


async def _run_sync(days: int, on_progress=None) -> tuple[list[str], list[str]]:
    """pipeline.sync.run_sync, with caught errors going to /errors."""
    return await run_sync(days, on_progress, on_error=_record_error)


def _sync_days(args) -> int:
    """Order look-back for a /sync run: first positional arg, else the default."""
    if args:
        try:
            return max(1, int(args[0]))
        except (TypeError, ValueError):
            pass
    return SYNC_DAYS


async def report_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Build an Excel profit report — cover photo, title, price, shipping, Ross
    cost, and profit net of eBay fees + the ad rate — and send it as a file. It
    reflects the current database each time, and the fee/shipping assumptions are
    editable cells in the sheet that recalc every row."""
    # Optional look-back for the account-level charge reconciliation under the
    # table (Promoted Listings). Everything else in the report is local data.
    ad_days = 90
    if context.args:
        try:
            ad_days = max(1, int(context.args[0]))
        except ValueError:
            pass
    await _safe_reply(update.message, "📊 Building the profit report...")
    import report as report_mod

    out = Path(tempfile.gettempdir()) / f"ross_report_{int(time.time())}.xlsx"
    try:
        await asyncio.to_thread(report_mod.build_report, str(out), ad_days)
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
        "listing_check": item.get("listing_check"),
    }
    # Offer Approve/Reject only while the item is still awaiting review.
    markup = flow._review_markup(item_id) if item["status"] in ("review", "drafted") else None
    if markup is not None:
        review[user_id] = item_id
    await _safe_reply(update.message, format_draft(result), reply_markup=markup)


_NO_CODE_WORDS = {"none", "n", "-", "x", "skip", "na", "n/a"}


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
    # Writing a receipt is not the same as removing the tag photo. Say so
    # explicitly, or _split_receipt reads this as "already handled" and the tag
    # showing what you paid stays in `photos` all the way to eBay. Preserve an
    # existing True — re-pricing an item whose tag was already peeled must not
    # queue up a second peel.
    receipt.setdefault("peeled", False)
    update_field(item_id, "receipt", receipt)
    last_item[user_id] = item_id
    tag = " (auto)" if auto else ""
    await _safe_reply(update.message, f"🧾 Saved for {item_id[:8]}: paid ${price:.2f} · code {code}{tag}")


async def scoreboard_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """What to buy: per brand and category sell-through, speed, capital tied up
    and a buy more / ok / pause / stop verdict. Local data only."""
    from pipeline import scoreboard
    try:
        lines = scoreboard.format_scoreboard(await asyncio.to_thread(scoreboard.load))
    except Exception as e:
        traceback.print_exc()
        _record_error("scoreboard", e)
        await _safe_reply(update.message, f"⚠️ Scoreboard failed: {type(e).__name__}: {str(e)[:200]}")
        return
    await _send_chunked(update.message, lines)


async def notdup_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/notdup <id> <id> — these two are different physical items (same UPC or
    model, but e.g. the print sits differently), so stop warning about them."""
    user_id = update.effective_user.id
    args = context.args or []
    if len(args) != 2:
        await _safe_reply(update.message, "Usage: /notdup <id> <id>  (8-char ids are fine)")
        return
    ids = []
    for arg in args:
        item_id, err = _resolve_item_id(user_id, [arg])
        if err:
            await _safe_reply(update.message, f"⚠️ {err}")
            return
        ids.append(item_id)
    if ids[0] == ids[1]:
        await _safe_reply(update.message, "⚠️ That's the same item twice.")
        return
    mark_not_duplicate(*ids)
    await _safe_reply(update.message, f"👍 Noted: {ids[0][:8]} and {ids[1][:8]} are different "
                                      "items. No more duplicate warnings for that pair.")


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
    # Drop the deleted item from the gate slot and the queue, then let whatever
    # was waiting behind it through. Without this, deleting the item currently
    # holding the gate leaves the rest of a haul queued behind a ghost.
    released = gate.get(user_id) is not None and gate[user_id]["item_id"] == item_id
    if released:
        gate.pop(user_id, None)
    gate_queue[user_id] = [e for e in gate_queue.get(user_id, []) if e["item_id"] != item_id]
    await _safe_reply(update.message, f"🗑️ Deleted {item_id[:8]} (offer {offer_id or 'none'} removed).")
    if released:
        await flow._activate_next_gate(user_id, update.message)


def _refresh_descriptions(apply: bool, force: bool) -> dict:
    """Re-render every eBay-side item's description and (when apply) push it. Runs
    in a worker thread — one eBay GET per item, plus a PUT for each one changed.
    Per-item failures are collected, never fatal: one bad offer must not strand
    the other ninety."""
    results = {"updated": [], "current": [], "diverged": [], "no_offer": [], "error": []}
    for item in list_items():
        if item["status"] not in ("ebay_draft", "published"):
            continue
        try:
            outcome = refresh_offer_description(item, apply=apply, force=force)
        except Exception as e:
            _record_error(f"refreshdesc ({item['item_id'][:8]})", e)
            results["error"].append(f"{item['item_id'][:8]}: {type(e).__name__}")
            continue
        results[outcome].append(item["item_id"][:8])
    return results


async def refreshdesc_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Push the current description copy to listings that are ALREADY on eBay —
    the WYSIWYG line only reaches a listing when its offer is built, so anything
    live from before is untouched until this runs.

    /refreshdesc            dry run: what would change, touching nothing
    /refreshdesc apply      push it (live listings update immediately)
    /refreshdesc apply force  also overwrite descriptions edited in Seller Hub
    """
    args = [a.lower() for a in (context.args or [])]
    apply = "apply" in args
    force = "force" in args

    await _safe_reply(update.message,
        ("✍️ Updating descriptions on eBay..." if apply
         else "🔍 Checking descriptions (dry run — nothing will be changed)..."))
    results = await asyncio.to_thread(_refresh_descriptions, apply, force)

    verb = "Updated" if apply else "Would update"
    lines = [f"📝 Description refresh — {verb.lower()} {len(results['updated'])} listing(s).", ""]
    if results["updated"]:
        lines.append(f"{verb}: {', '.join(results['updated'])}")
    if results["current"]:
        lines.append(f"Already current ({len(results['current'])}) — no change needed.")
    if results["diverged"]:
        lines += ["",
                  f"✋ Skipped {len(results['diverged'])} whose live description isn't the one we "
                  f"generated — edited in Seller Hub, or listed before a change to the renderer. "
                  f"Pushing ours would throw that away: {', '.join(results['diverged'])}",
                  "Add 'force' to overwrite them anyway."]
    if results["no_offer"]:
        lines.append(f"No eBay offer / no description ({len(results['no_offer'])}) — skipped.")
    if results["error"]:
        lines += ["", f"⚠️ Failed ({len(results['error'])}): {', '.join(results['error'])}"]
    if not apply and results["updated"]:
        lines += ["", "Nothing was changed. Run /refreshdesc apply to push it."]
    await _send_chunked(update.message, lines)


async def offers_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send private discounts to the people watching your listings.

    Targets intent, not margin: an offer only converts someone who already wants
    the item, so the gate is live watchers, floored on the money left afterwards.
    A private offer beats a public price cut — it converts the interested buyer
    without giving the same money away to everyone else.

      /offers              dry run — who'd get what, and why the rest won't
      /offers apply        send them
      /offers apply 8      send with a flat $8 off instead of the $5/$10 default
    """
    args = [a.lower() for a in (context.args or [])]
    apply = "apply" in args
    override = None
    for arg in args:
        try:
            override = float(arg.replace("$", ""))
        except ValueError:
            continue

    await _safe_reply(update.message,
        "💌 Sending offers to watchers..." if apply
        else "🔍 Checking which listings have watchers and margin (dry run)...")
    try:
        rows = await asyncio.to_thread(_offer_plan, override)
    except Exception as e:
        traceback.print_exc()
        _record_error("offers", e)
        await _safe_reply(update.message,
            f"⚠️ Couldn't read eligible listings: {type(e).__name__}: {str(e)[:250]}\n"
            "If that's a 403, the sell.negotiation scope is missing — re-run "
            "`python -m ebay.auth`.")
        return

    send = [r for r in rows if r.get("send")]
    skip = [r for r in rows if not r.get("send")]
    sent, failed = [], []
    if apply:
        for row in send:
            try:
                await asyncio.to_thread(send_offer, row["listing_id"], row["offer_price"],
                                        OFFER_MESSAGE)
                sent.append(row)
            except Exception as e:
                _record_error(f"offer ({row['item_id'][:8]})", e)
                failed.append((row, f"{type(e).__name__}: {str(e)[:90]}"))
                continue
            try:
                # So the weekly digest's offer plan doesn't re-offer it next week.
                record_offer_sent(row["listing_id"], row["offer_price"], item_id=row["item_id"],
                                  list_price=row["price"], source="/offers")
            except Exception as e:
                print(f"WARNING: couldn't record offer sent: {type(e).__name__}: {e}")

    verb = "Sent" if apply else "Would send"
    lines = [f"💌 Offers to watchers — {verb.lower()} {len(sent) if apply else len(send)} offer(s).", ""]
    for row in (sent if apply else send):
        est = " ⚠️est cost" if row["cost_estimated"] else ""
        units = f" ×{row['qty']}" if row.get("qty", 1) > 1 else ""
        lines.append(
            f"{row['item_id'][:8]} {row['watchers']}w · ${row['price']:.2f} → ${row['offer_price']:.2f} "
            f"(−${row['discount']:.0f}, {row['pct']:.0f}%){units} · keeps ${row['net_after']:.2f}/unit "
            f"({row['margin_after'] * 100:.0f}%){est} · {row['title']}")
    if failed:
        lines += ["", f"⚠️ Failed ({len(failed)}):"]
        lines += [f"  {r['item_id'][:8]}: {why}" for r, why in failed]
    if skip:
        lines += ["", f"Skipped {len(skip)} eligible listing(s):"]
        lines += [f"  {r['item_id'][:8]} ${r['price']:.2f} — {r['verdict']} · {r['title']}"
                  for r in skip[:15]]
        if len(skip) > 15:
            lines.append(f"  ...and {len(skip) - 15} more")
    if not apply and send:
        lines += ["", f"Nothing sent. /offers apply to send these {len(send)} offer(s) — "
                      f"they go straight to buyers and can't be recalled."]
    if not rows:
        lines = ["💌 No listings are eligible for offers right now — eBay needs watchers "
                 "(or carted items) and no offer already outstanding."]
    await _send_chunked(update.message, lines)


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
    """Set an item's price. Usage: /setprice [id] <price> [force] (a whole number
    is charm-priced, 35 -> $34.99). Updates the stored price, and if the item
    already has an eBay offer (draft or live), pushes the new price to eBay too.

    Refuses a price below break-even unless `force` is given."""
    user_id = update.effective_user.id
    args = list(context.args)
    forced = bool(args) and args[-1].lower() in ("force", "-f", "!")
    if forced:
        args = args[:-1]
    if not args:
        await _safe_reply(update.message, "Usage: /setprice [id] <price>  (e.g. /setprice 35 -> $34.99)")
        return
    # Item id first — an all-digit id prefix is a valid-looking price, and
    # consuming it as one leaves no id behind, so the command silently retargets
    # at last_item and pushes that number to a LIVE listing.
    if _names_an_item(args[-1]):
        await _safe_reply(update.message,
            f"'{args[-1]}' names an item, not a price. Usage: /setprice {args[-1]} <price>")
        return
    price = text_router._parse_price_override(args[-1])
    if price is None:
        await _safe_reply(update.message, f"'{args[-1]}' isn't a valid price.")
        return
    if price > _MAX_SANE_PRICE:
        await _safe_reply(update.message,
            f"${price:,.2f} is past the ${_MAX_SANE_PRICE:,.0f} sanity cap — "
            f"pass a smaller number, or edit it in Seller Hub if you really mean it.")
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

    if not forced:
        refusal = text_router._floor_refusal(item, price)
        if refusal:
            await _safe_reply(update.message, f"⚠️ {refusal}")
            return

    # Update the stored price on both the listing and the pricing record.
    listing = dict(item.get("listing") or {})
    if listing:
        listing["price"] = price
        update_field(item_id, "listing", listing)
    pricing = text_router._apply_manual_price(item.get("pricing") or {}, price)
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
    # Item id first — an all-digit id prefix would otherwise parse as a quantity
    # and push a 40-million stock count to eBay against the wrong listing.
    if _names_an_item(args[-1]):
        await _safe_reply(update.message,
            f"'{args[-1]}' names an item, not a quantity. Usage: /setqty {args[-1]} <n>")
        return
    try:
        qty = int(args[-1])
    except ValueError:
        await _safe_reply(update.message, f"'{args[-1]}' isn't a whole number.")
        return
    if qty < 0:
        await _safe_reply(update.message, "Quantity can't be negative.")
        return
    if qty > _MAX_SANE_QTY:
        await _safe_reply(update.message,
            f"{qty} units? That's past the {_MAX_SANE_QTY} sanity cap — pass a smaller number.")
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
    await flow._publish_and_report(item_id, update.message)


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
    await flow._promote_item(item_id, pct, update.message)


def _reprice_markup(item_id: str, price: float) -> InlineKeyboardMarkup:
    """Apply/Skip for one digest row. The price the button will apply is carried
    IN the callback data, because the button is the record of what was authorised:
    tapping Apply means "yes, that price", and a digest message sits in the chat
    indefinitely. Re-reading the suggestion at tap time — which is what this used
    to do — hands you whatever the most recent /pricecheck stored instead, so a
    second run between the digest and the tap silently substitutes a different
    number for the one on screen.

    Telegram caps callback_data at 64 bytes; this is 58 at worst (a 36-char uuid
    plus a 7-char price)."""
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Apply", callback_data=f"reprice:apply:{price:.2f}:{item_id}"),
        InlineKeyboardButton("Skip", callback_data=f"reprice:skip:-:{item_id}"),
    ]])


def _offers_markup(plan_id: str, n: int) -> InlineKeyboardMarkup:
    """The digest's ONE button for the whole watcher-offer plan. The plan's rows
    live in db.offer_plans (64 bytes can't carry a dozen prices); the tap sends
    exactly those, via pipeline.reprice.send_planned_offers. 53 bytes."""
    return InlineKeyboardMarkup([[InlineKeyboardButton(
        f"📨 Send offers to {n} watched listing{'s' if n != 1 else ''}",
        callback_data=f"reprice:offers:-:{plan_id}")]])


def _offer_plan_lines(offers: dict | None) -> list[str]:
    """The digest's offer section: one line per planned listing, said once."""
    rows = (offers or {}).get("rows") or []
    if not rows:
        return []
    lines = [f"📨 Offers to watchers ready ({len(rows)}) — ~{WATCHER_OFFER_PCT:.0f}% off, never "
             f"under the Best Offer auto-decline or cost floor. A private offer beats a "
             f"public cut. Nothing is sent until you tap the button."]
    for row in rows:
        lines.append(f"• {row['item_id'][:8]} {row['title'][:30]} ${row['price']:.2f} → "
                     f"${row['offer_price']:.2f} (−{row['pct']:.0f}%) · {row['watchers']} watch")
    return lines


_VERDICT_LABEL = {
    "INVISIBLE": "🙈 Barely shown on eBay (promoted included)",
    "LOW_CTR": "😐 Seen but not clicked",
    "OVERPRICED": "💸 Views but no watchers — likely overpriced",
    "SEND_OFFERS": "👀 Watchers building — consider a private offer before cutting",
    "STALE": "🕸️ Stale — real views, still unsold",
    "STALE_UNSEEN": "🔍 Unsold but barely seen — a findability problem, not a price one",
    "TOO_NEW": "🌱 Too new to judge yet",
    "HEALTHY": "✅ Healthy",
    "NO_WATCH_DATA": "❔ Couldn't read the watch count — not judged",
    "NO_TRAFFIC_DATA": "❔ Couldn't load eBay traffic — not judged",
    "ERROR": "⚠️ Couldn't check",
}

# What to actually do about it. The visibility verdicts never carry a suggested
# price (pipeline/reprice.py only prices OVERPRICED and STALE), so without this
# they'd arrive as a diagnosis with no next step.
_VERDICT_ADVICE = {
    "INVISIBLE": "Under the weekly impressions floor even with promotion. Check the "
                 "category and item specifics, or /end and relist to refresh it. "
                 "Not a price cut.",
    "LOW_CTR": "They see it and scroll past: cover photo first, then title. "
               "Not a price cut.",
    "STALE_UNSEEN": "Too few views to blame the price. Rework the title/keywords "
                    "and consider /promote, or /end and relist to refresh ranking.",
    "SEND_OFFERS": "Send an offer to the watchers before cutting the public price.",
    "NO_WATCH_DATA": "eBay didn't return a watch count, and every remaining check "
                     "depends on it. Nothing is wrong with the listing as far as "
                     "this run can tell — re-run /pricecheck later.",
    "NO_TRAFFIC_DATA": "eBay's traffic report failed for this listing, so there "
                       "are no impressions to judge — this is NOT \"invisible\". "
                       "Re-run /pricecheck later.",
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
    # Cumulative views alongside this week's, because the OVERPRICED/STALE
    # verdicts are decided on the running total — showing only the week's figure
    # made those diagnoses look like they'd been reached on 3 views.
    cum = r.get("cumulative_views")
    # None = the traffic fetch failed (NO_TRAFFIC_DATA) — show "?", not 0.
    impr = r.get("impressions")
    views = f"{r['views'] if r.get('views') is not None else '?'} views"
    if cum is not None and cum != r.get("views"):
        views += f" ({cum} total)"
    ctr = r.get("ctr")
    # impressions = total (promoted included), the number verdicts use.
    organic = r.get("organic_impressions")
    impr_text = f"{impr if impr is not None else '?'} impr"
    if impr is not None and organic is not None:
        impr_text += f" ({organic} organic)"
    line += (f"\n  {impr_text} · {views} · "
             + (f"{ctr * 100:.2f}% CTR · " if ctr else "")
             + f"{watchers if watchers is not None else '?'} watchers · ${r.get('current_price')}")
    if r.get("suggested_price"):
        line += f" → suggest ${r['suggested_price']}"
    elif _VERDICT_ADVICE.get(r["verdict"]):
        line += f"\n  💡 {_VERDICT_ADVICE[r['verdict']]}"
    elif r.get("floor_note"):
        # Why there's no suggested cut. Without this the digest shows a problem
        # item with no action and no explanation, which reads as a bug.
        line += f"\n  ⛔ {r['floor_note']}"
    return line


def _format_reprice_short(r: dict) -> str:
    """One line per listing for the grouped digest: id, title, price, and the
    numbers that matter (impressions / views / watchers)."""
    item = get_item(r["item_id"])
    title = ((item or {}).get("listing") or {}).get("title") or "(untitled)"
    if r["verdict"] == "ERROR":
        return f"• {r['item_id'][:8]} {title[:32]} — {str(r.get('error', ''))[:60]}"
    impr = r.get("impressions")
    views = r.get("cumulative_views")
    if views is None:
        views = r.get("views")
    watchers = r.get("watchers")
    q = lambda v: "?" if v is None else v
    return (f"• {r['item_id'][:8]} {title[:32]} ${r.get('current_price')} · "
            f"{q(impr)} impr · {q(views)} views · {q(watchers)} watch")


def _reprice_health_line(health: dict | None) -> str | None:
    """One line saying how much of eBay's data actually arrived. Without it a
    run whose traffic fetch failed looked exactly like a week where nothing was
    found in search (2026-08-17: 71 INVISIBLE; 2026-10-05: 97)."""
    if not health:
        return None
    return (f"Data: {health.get('traffic_failed', 0)}/{health.get('traffic_batches', 0)} "
            f"traffic batch(es) failed · watch count missing for "
            f"{health.get('watch_failed', 0)}/{health.get('watch_lookups', 0)}.")


async def _send_reprice_digest(bot, chat_id, results: list[dict],
                               health: dict | None = None,
                               incomplete_note: str | None = None) -> None:
    """Post a weekly price-check digest: one summary header, one grouped list of
    flagged listings, one watcher-offer message with a single "Send offers"
    button (health["offers"], when there's a plan), and one message per
    suggested price with its Apply/Skip button. Healthy/too-new items are just counted in
    the header, not spelled out individually — and so are NO_TRAFFIC_DATA ones:
    a failed traffic fetch covers whole batches, there is nothing to act on per
    listing, and 97 "couldn't load" messages would bury the real flags.

    `health` (from run_weekly_check) becomes a data-health line in the header;
    `incomplete_note` is appended when traffic batches failed, to say what
    happens next (the scheduler retries; /pricecheck is re-run by hand)."""
    no_traffic = [r for r in results if r["verdict"] == "NO_TRAFFIC_DATA"]
    flagged = [r for r in results
               if r["verdict"] not in ("HEALTHY", "TOO_NEW", "NO_TRAFFIC_DATA")]
    healthy = [r for r in results if r["verdict"] == "HEALTHY"]
    too_new = [r for r in results if r["verdict"] == "TOO_NEW"]

    header = (f"📈 Weekly price check — {len(results)} live listing(s): "
              f"{len(flagged)} flagged, {len(healthy)} healthy, {len(too_new)} too new to judge"
              + (f", {len(no_traffic)} not judged (eBay traffic didn't load)" if no_traffic else "")
              + ".")
    health_line = _reprice_health_line(health)
    if health_line:
        header += f"\n{health_line}"
    if (health or {}).get("traffic_failed"):
        header += ("\n⚠️ Incomplete: listings without traffic data were NOT diagnosed "
                   "as invisible — the numbers are missing, not zero."
                   + (f" {incomplete_note}" if incomplete_note else ""))
    offers = (health or {}).get("offers") or {}
    if offers.get("error"):
        header += (f"\n⚠️ Offers to watchers not planned: couldn't read eBay's eligible "
                   f"listings ({offers['error'][:80]}). /offers to retry.")
    try:
        await bot.send_message(chat_id=chat_id, text=header)
    except Exception as e:
        print(f"WARNING: reprice digest header failed: {type(e).__name__}: {e}")
        return

    # Only a suggested price earns its own message, because its Apply button has
    # to sit under it. Everything else is grouped by verdict into one compact
    # list with the advice said once — one message per listing was 50+ phone
    # notifications a week that nobody read. The watcher-offer plan is one more
    # grouped message with one button for all of it; a listing in that plan
    # isn't listed again under SEND_OFFERS.
    planned = {row["item_id"] for row in offers.get("rows") or []}
    actionable = [r for r in flagged if r.get("suggested_price")]
    grouped: dict[str, list[dict]] = {}
    for r in flagged:
        if r.get("suggested_price"):
            continue
        if r["verdict"] == "SEND_OFFERS" and r["item_id"] in planned:
            continue
        grouped.setdefault(r["verdict"], []).append(r)

    lines = []
    for verdict, rows in grouped.items():
        if lines:
            lines.append("")
        lines.append(f"{_VERDICT_LABEL.get(verdict, verdict)} ({len(rows)})")
        advice = _VERDICT_ADVICE.get(verdict)
        if advice:
            lines.append(f"💡 {advice}")
        for r in rows:
            lines.append(_format_reprice_short(r))
    if lines:
        try:
            for chunk in _chunk_lines(lines):
                await bot.send_message(chat_id=chat_id, text=chunk)
        except Exception as e:
            print(f"WARNING: reprice digest list failed: {type(e).__name__}: {e}")

    offer_lines = _offer_plan_lines(offers)
    if offer_lines and offers.get("plan_id"):
        chunks = _chunk_lines(offer_lines)
        try:
            for i, chunk in enumerate(chunks):
                markup = (_offers_markup(offers["plan_id"], len(offers["rows"]))
                          if i == len(chunks) - 1 else None)
                await bot.send_message(chat_id=chat_id, text=chunk, reply_markup=markup)
        except Exception as e:
            print(f"WARNING: reprice digest offers failed: {type(e).__name__}: {e}")

    for r in actionable:
        try:
            await bot.send_message(chat_id=chat_id, text=_format_reprice_line(r),
                                   reply_markup=_reprice_markup(r["item_id"], r["suggested_price"]))
        except Exception as e:
            print(f"WARNING: reprice digest item failed: {type(e).__name__}: {e}")


async def pricecheck_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Manually run the weekly price-health check now (it also runs automatically
    once a week). Pulls eBay traffic + watch count for every live listing,
    diagnoses why an unsold item isn't moving, and offers a one-tap price cut
    where the evidence supports one and the receipt cost is on file."""
    await _safe_reply(update.message, "📈 Checking live listings against eBay traffic...")
    try:
        results, health = await asyncio.to_thread(run_weekly_check)
    except Exception as e:
        traceback.print_exc()
        _record_error("pricecheck", e)
        await _safe_reply(update.message, f"⚠️ Price check failed: {type(e).__name__}: {str(e)[:250]}")
        return
    if not results:
        await _safe_reply(update.message, "No published listings to check.")
        return
    await _send_reprice_digest(context.bot, update.effective_chat.id, results, health,
                               incomplete_note="Re-run /pricecheck later.")


async def _send_offer_plan(message, plan_id: str) -> None:
    """Send a digest's saved watcher-offer plan (the owner tapped its button)
    and report what happened to each listing."""
    await _safe_reply(message, "📨 Sending offers to watchers...")
    try:
        res = await asyncio.to_thread(send_planned_offers, plan_id)
    except Exception as e:
        traceback.print_exc()
        _record_error("digest offers", e)
        await _safe_reply(message, f"⚠️ Offers failed: {type(e).__name__}: {str(e)[:200]}")
        return
    if res.get("error"):
        await _safe_reply(message, f"⚠️ No offers sent: {res['error']}")
        return
    for row, why in res["failed"]:
        _record_error(f"offer ({row['item_id'][:8]})", RuntimeError(why))
    lines = [f"📨 Sent {len(res['sent'])} offer(s) to watchers."]
    lines += [f"• {r['item_id'][:8]} ${r['price']:.2f} → ${r['offer_price']:.2f} · "
              f"{r['watchers']} watch · {r['title'][:30]}" for r in res["sent"]]
    if res["failed"]:
        lines += ["", f"⚠️ Failed ({len(res['failed'])}):"]
        lines += [f"• {r['item_id'][:8]}: {why}" for r, why in res["failed"]]
    if res["skipped"]:
        lines += ["", f"Skipped {len(res['skipped'])}:"]
        lines += [f"• {r['item_id'][:8]}: {why}" for r, why in res["skipped"]]
    await _send_chunked(message, lines)


async def reprice_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle the buttons on a weekly price-check digest: per-item Apply/Skip,
    and the one "Send offers" button for the watcher-offer plan."""
    query = update.callback_query
    await query.answer()
    try:
        _, action, price_field, item_id = query.data.split(":", 3)
    except ValueError:
        return
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass

    if action == "skip":
        return
    if action == "offers":
        # The digest's one "Send offers" button; the last field is the plan id.
        await _send_offer_plan(query.message, item_id)
        return

    item = get_item(item_id)
    if item is None:
        await _safe_reply(query.message, "That item no longer exists.")
        return
    # The price comes from the button, not from the DB — it's the figure that was
    # displayed above this button and the one the tap authorises. See
    # _reprice_markup.
    try:
        price = round(float(price_field), 2)
    except (TypeError, ValueError):
        await _safe_reply(query.message,
                          "That button is from an older digest and no longer carries its "
                          "price — run /pricecheck again.")
        return
    if price <= 0:
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


# Names under which the scheduled jobs stamp db.job_runs. Changing one resets
# that job's catch-up history, so don't.
_JOB_REPRICE = "weekly_reprice"
_JOB_SYNC = "daily_sync"
_JOB_FEEDBACK = "daily_feedback"


def _previous_slot(now: datetime, hour_utc: int, weekday: int | None = None) -> datetime:
    """The most recent scheduled slot at or before `now`. `weekday` None = daily."""
    if weekday is None:
        slot = now.replace(hour=hour_utc, minute=0, second=0, microsecond=0)
        return slot if slot <= now else slot - timedelta(days=1)
    days_back = (now.weekday() - weekday) % 7
    slot = (now - timedelta(days=days_back)).replace(
        hour=hour_utc, minute=0, second=0, microsecond=0)
    return slot if slot <= now else slot - timedelta(days=7)


def _next_slot(now: datetime, hour_utc: int, weekday: int | None = None) -> datetime:
    """The next scheduled slot strictly after `now`. `weekday` None = daily."""
    period = timedelta(days=1 if weekday is None else 7)
    return _previous_slot(now, hour_utc, weekday) + period


def _missed_slot(job: str, now: datetime, hour_utc: int,
                 weekday: int | None = None) -> bool:
    """Whether `job` should run a catch-up right now, because it never ran for
    the slot that has already passed.

    This is the whole point of persisting job_runs. The loops used to compute
    their next slot from `now` alone, so a process that wasn't alive at the slot
    (laptop asleep, Windows Update reboot) skipped that run silently and waited a
    full period — which is why the 2026-08-03 weekly digest simply never
    happened and nothing recorded that it hadn't.

    Bounded by JOB_CATCHUP_GRACE_HOURS: a run that is days late is stale news,
    and firing it at startup would be noise rather than a recovery."""
    previous = _previous_slot(now, hour_utc, weekday)
    if now - previous > timedelta(hours=JOB_CATCHUP_GRACE_HOURS):
        return False
    last = last_job_run(job)
    return last is None or last < previous


async def _run_reprice_job(app: Application, *, catch_up: bool = False) -> None:
    """One weekly price check + digest. Only stamps job_runs "ok" on a complete
    run, so a run that failed is retried at the next start rather than being
    counted as done.

    A run where any traffic batch failed is stamped "partial", which
    last_job_run doesn't count as completed — so it is caught up like a missed
    slot. It used to be stamped ok: the 2026-10-05 run read 97 listings as
    INVISIBLE off a failed fetch and the scheduler considered the week served."""
    previous_status = job_run_status(_JOB_REPRICE)
    print(f"📈 Running {'catch-up' if catch_up else 'scheduled'} weekly price check...")
    try:
        results, health = await asyncio.to_thread(run_weekly_check)
    except Exception as e:
        traceback.print_exc()
        _record_error("weekly reprice", e)
        return
    status = "partial" if health.get("traffic_failed") else "ok"
    record_job_run(_JOB_REPRICE, status=status,
                   detail=f"{len(results)} listing(s); {_reprice_health_line(health)}")
    if status != "ok":
        print(f"   price check incomplete: {_reprice_health_line(health)}")
    if not results:
        return
    incomplete_note = (f"Recorded as incomplete — it re-runs automatically at the next "
                       f"bot start within {JOB_CATCHUP_GRACE_HOURS}h of the slot, "
                       f"or run /pricecheck later.")
    for uid in ALLOWED_USER_IDS:
        if catch_up:
            notice = ("⏰ Retrying the price check: the last run couldn't load eBay "
                      "traffic for some listings." if previous_status == "partial" else
                      "⏰ Catch-up: last week's price check never ran (the bot "
                      "wasn't up at its slot). Running it now.")
            try:
                await app.bot.send_message(chat_id=uid, text=notice)
            except Exception as e:
                print(f"WARNING: catch-up notice failed: {type(e).__name__}: {e}")
        await _send_reprice_digest(app.bot, uid, results, health,
                                   incomplete_note=incomplete_note)


async def _weekly_reprice_loop(app: Application) -> None:
    """Runs for the lifetime of the bot: catches up on a missed slot, then sleeps
    until the next configured weekly slot (REPRICE_WEEKDAY/REPRICE_HOUR_UTC),
    runs the price check, and posts the digest to every allowed user. A manual
    /pricecheck doesn't reset this clock."""
    if _missed_slot(_JOB_REPRICE, datetime.now(timezone.utc),
                    REPRICE_HOUR_UTC, REPRICE_WEEKDAY):
        await _run_reprice_job(app, catch_up=True)
    while True:
        now = datetime.now(timezone.utc)
        target = _next_slot(now, REPRICE_HOUR_UTC, REPRICE_WEEKDAY)
        await asyncio.sleep((target - now).total_seconds())
        await _run_reprice_job(app)


async def _run_sync_job(app: Application, *, catch_up: bool = False) -> None:
    """One scheduled reconciliation with eBay, using the same _run_sync the
    manual /sync calls. Posts the report only when it actually says something."""
    print(f"🔄 Running {'catch-up' if catch_up else 'scheduled'} eBay sync...")
    try:
        lines, brief = await _run_sync(SYNC_DAYS)
    except Exception as e:
        traceback.print_exc()
        _record_error("scheduled sync", e)
        return
    record_job_run(_JOB_SYNC, detail=f"{len(lines)} report line(s)")
    await _send_budget_warning(app)

    # Bookkeeping (price/qty corrections, status fixes) happens silently; the
    # phone only hears about new sales and things that need a decision.
    if SOLD_ALERTS_ENABLED:
        # The 15-minute sold alert already announced each sale with its box.
        brief = [l for l in brief if not l.startswith("💰 Sold")]
    if not brief:
        print("   sync: nothing new to report, staying quiet.")
        return
    for uid in ALLOWED_USER_IDS:
        try:
            await _send_chunked_to(app.bot, uid, brief)
        except Exception as e:
            print(f"WARNING: sync digest to {uid} failed: {type(e).__name__}: {e}")


async def _send_budget_warning(app: Application) -> None:
    """Pass on the monthly Claude-spend warning, which llm_usage hands out at
    most once per threshold per month. Rides on the daily sync so it needs no
    timer of its own."""
    try:
        text = await asyncio.to_thread(llm_usage.budget_warning)
    except Exception as e:
        print(f"WARNING: budget check failed: {type(e).__name__}: {e}")
        return
    if not text:
        return
    print(text)
    for uid in ALLOWED_USER_IDS:
        try:
            await app.bot.send_message(chat_id=uid, text=text)
        except Exception as e:
            print(f"WARNING: budget warning to {uid} failed: {type(e).__name__}: {e}")


async def usage_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/usage: this month's model spend by provider, model and stage."""
    lines = await asyncio.to_thread(llm_usage.usage_report)
    await _send_chunked(update.message, lines)


async def _daily_sync_loop(app: Application) -> None:
    """Runs for the lifetime of the bot: keeps the local DB reconciled with eBay
    once a day, so sold orders land in the DB without anyone remembering to type
    /sync.

    Before this existed /sync was manual-only, and every money figure built on
    the DB (/profit, /report, COGS) quietly understated itself for as long as a
    sync was overdue — the DB sat 10 days stale on 2026-09-03, missing 8 August
    orders and putting that month's COGS 35% low."""
    if _missed_slot(_JOB_SYNC, datetime.now(timezone.utc), SYNC_HOUR_UTC):
        await _run_sync_job(app, catch_up=True)
    while True:
        now = datetime.now(timezone.utc)
        target = _next_slot(now, SYNC_HOUR_UTC)
        await asyncio.sleep((target - now).total_seconds())
        await _run_sync_job(app)


def _feedback_report(result: dict) -> list[str]:
    """Report lines for a feedback run. Empty when there was nothing to do."""
    left, skipped, errors = result["left"], result["skipped"], result["errors"]
    if not (left or skipped or errors):
        return []
    lines = [f"⭐ Left positive feedback for {len(left)} buyer(s)."] if left else []
    for e in left:
        lines.append(f"  {e['buyer']}: {e['title'][:50]}")
    if skipped:
        lines += ([""] if lines else []) + [f"Skipped ({len(skipped)}):"] + [
            f"  {e['item_id']} {e['title'][:40]}: {why}" for e, why in skipped]
    if errors:
        lines += ([""] if lines else []) + [
            f"⚠️ Couldn't leave feedback ({len(errors)}) — eBay refused:"] + [
            f"  {e['item_id']} {e['title'][:40]}: {msg}" for e, msg in errors]
    return lines


def _only_new_feedback_problems(result: dict) -> dict:
    """Drop failures and skips already reported on an earlier run. eBay keeps
    listing a sale it won't take feedback on until the 60-day window closes, so
    without this the same 13 errors were posted every single day. Each one is
    still retried every run; it's just reported once. /feedback shows them all."""
    seen = known_feedback_failures()
    for e in result["left"]:
        if e.get("order_line") in seen:
            clear_feedback_failure(e["order_line"])
    fresh = {"left": result["left"], "skipped": [], "errors": []}
    for kind in ("skipped", "errors"):
        for e, why in result[kind]:
            key = e.get("order_line") or f"{e['item_id']}-{e['transaction_id']}"
            if key not in seen:
                fresh[kind].append((e, why))
            record_feedback_failure(key, str(why)[:200])
    return fresh


async def feedback_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/feedback: leave positive feedback for every buyer still waiting on it,
    right now, instead of at the daily slot."""
    await _safe_reply(update.message, "⭐ Leaving feedback for buyers...")
    try:
        result = await asyncio.to_thread(leave_all_pending, FEEDBACK_COMMENT)
    except Exception as e:
        _record_error("feedback", e)
        await _safe_reply(update.message, f"❌ Feedback run failed: {e}")
        return
    record_job_run(_JOB_FEEDBACK, detail=f"{len(result['left'])} left (manual)")
    lines = _feedback_report(result) or ["No buyers are waiting on feedback."]
    await _send_chunked(update.message, lines)


async def _run_feedback_job(app: Application) -> None:
    """One automatic feedback run. Posts only when it left (or failed to leave)
    something. Stamped only on success, so a failed run retries at next start."""
    print("⭐ Running scheduled buyer feedback...")
    try:
        result = await asyncio.to_thread(leave_all_pending, FEEDBACK_COMMENT)
    except Exception as e:
        traceback.print_exc()
        _record_error("scheduled feedback", e)
        return
    record_job_run(_JOB_FEEDBACK, detail=f"{len(result['left'])} left")
    lines = _feedback_report(_only_new_feedback_problems(result))
    if not lines:
        return
    for uid in ALLOWED_USER_IDS:
        await _send_chunked_to(app.bot, uid, lines)


async def _daily_feedback_loop(app: Application) -> None:
    """Runs for the lifetime of the bot: leaves positive feedback for new buyers
    once a day, catching up at startup if the slot was missed."""
    if _missed_slot(_JOB_FEEDBACK, datetime.now(timezone.utc), FEEDBACK_HOUR_UTC):
        await _run_feedback_job(app)
    while True:
        now = datetime.now(timezone.utc)
        target = _next_slot(now, FEEDBACK_HOUR_UTC)
        await asyncio.sleep((target - now).total_seconds())
        await _run_feedback_job(app)


async def retry_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    item_id, err = _resolve_item_id(user_id, context.args)
    if err:
        await _safe_reply(update.message, f"⚠️ {err}")
        return
    await flow._retry_item(user_id, item_id, update.message, context)


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
    items = [i for i in list_items() if (i.get("ebay") or {}).get("listing_id")]
    if not items:
        await _safe_reply(update.message, "No live listings to check.")
        return

    await _safe_reply(update.message,
                      f"🖼️ Checking photos on {len(items)} live listing(s)... "
                      "one eBay call each, so give it a minute.")
    try:
        short = await asyncio.to_thread(audit_listing_photos, items)
    except Exception as e:
        _record_error("photocheck", e)
        await _safe_reply(update.message, f"⚠️ Photo check failed: {type(e).__name__}: {str(e)[:200]}")
        return

    if not short:
        await _safe_reply(update.message,
                          f"✅ All {len(items)} live listing(s) show every photo we hold. "
                          "Nothing missing.")
        return

    lines = [f"⚠️ {len(short)} listing(s) are showing fewer photos than we uploaded:", ""]
    for f in short:
        lines.append(f"{f['item_id'][:8]} — {f['live']}/{f['expected']} photos "
                     f"({f['expected'] - f['live']} missing)\n   {f['title'][:60]}")
    lines += ["", "The originals are still stored, so this is repairable: "
              "/arrange or /cover on an item rebuilds its offer and re-sends every photo."]
    await _send_chunked(update.message, lines)


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
        lines.append(await asyncio.to_thread(listings_status))
    except Exception as e:
        lines.append(f"❌ Active listings (Trading API): {str(e)[:150]}")

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

    # /offers needs its own scope, and it's the one nothing else exercises — a
    # missing sell.negotiation consent shows up as "no eligible listings", which
    # reads as "nobody's watching anything" rather than as a broken permission.
    # negotiation_status was imported for this check and then never called.
    try:
        status = await asyncio.to_thread(negotiation_status)
        lines.append(f"✅ Offers to watchers (sell.negotiation): {status}")
    except Exception as e:
        lines.append(
            f"❌ Offers to watchers (sell.negotiation): {str(e)[:150]}\n"
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
        # Photos already captured and sitting on a Start/Wait/Cancel prompt.
        # /cancel skipped this slot entirely, so it answered "No capture in
        # progress" while an item WAS staged — and the countdown task kept
        # running, starting the pipeline on photos the user had just cancelled.
        st = staging.pop(user_id, None)
        if st and st.get("task") is not None:
            st["task"].cancel()
    was_gated = gate.pop(user_id, None) is not None
    was_hauling = user_id in haul_mode
    haul_mode.discard(user_id)
    _end_find(user_id)
    inv_box.pop(user_id, None)
    inv_session.pop(user_id, None)
    list_after_photos.pop(user_id, None)
    # Only drop the queue when a gate was actually what got cancelled. Popping it
    # unconditionally meant cancelling an unrelated photo capture silently threw
    # away every queued haul gate and said nothing about it.
    queued = len(gate_queue.pop(user_id, []) or []) if was_gated else 0

    if data:
        await _safe_reply(update.message, f"🛑 Cancelled capture ({len(data['files'])} photo(s) discarded).")
    elif st:
        # Same treatment as the Cancel button on the prompt: drop the item and its
        # photos. Leaving the row behind would strand it at 'captured' forever,
        # counted by /status and reachable by nothing.
        staged_id = st["item_id"]
        item = get_item(staged_id)
        if item is not None:
            photos = item.get("photos") or []
            if photos:
                shutil.rmtree(Path(photos[0]).parent, ignore_errors=True)
            delete_item(staged_id)
        await _safe_reply(
            update.message,
            f"🛑 Cancelled — {len(st.get('paths') or [])} photo(s) discarded before processing.")
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
        ("/haul", "Multi-item mode: dump a whole Ross run, split on a dark photo between items"),
        ("wait", "Hold the photo-batching window open longer for a big batch"),
        ("confirm", "At a gate: accept the identification / price and continue"),
        ("(free text)", "At a gate: correct it (e.g. 'brand is Tommy Jeans, color navy'). At review: approve, reject, or a correction"),
        ("/cancel", "Discard photos being captured, or drop out of a gate"),
    ]),
    ("Items", [
        ("/status [status|n|all]", "List items and their stage — the 20 most recent by default. "
                                   "Filter (/status published), widen (/status 50, /status all)"),
        ("/ross [missing]", "Audit Ross code + paid price per item; /ross missing shows only unpriced ones"),
        ("/listing [id]", "Show the current draft for an item"),
        ("/comps [id]", "Show the sold/active comps the price was built from"),
        ("/addphotos [id]", "Attach more photos to an existing item"),
        ("/photos [id]", "Show the photos in listing order, numbered"),
        ("/cover [id] <n>", "Make photo n the gallery cover (the search thumbnail)"),
        ("/arrange [id] <order>", "Reorder photos, e.g. /arrange 3,1,2 — unlisted ones go last"),
        ("/stockphoto [id] <url>", "Add a retailer's product photo as #2 — for items sealed in packaging"),
        ("/receipt [id] <price> [code]", "Set the Ross cost by hand; code is optional (omit or 'none' to auto-fill)"),
    ]),
    ("Selling", [
        ("/setprice [id] <price>", "Set the price (35 -> $34.99); pushes to eBay if it already has an offer"),
        ("/setqty [id] <n>", "Set the quantity on eBay (once the item has an offer); default is 1"),
        ("/sync [days]", "Pull live price/quantity + real sold orders from eBay; auto-marks sold items with actual fees"),
        ("/feedback", "Leave positive feedback now for every paid buyer still waiting on it (also runs daily)"),
        ("/activate [id]", "Publish an eBay draft — makes it live"),
        ("/end [id]", "End a live listing (drops to a draft; /activate to relist)"),
        ("/refreshdesc [apply]", "Push the current description copy to listings already on eBay "
                                 "(dry run without 'apply')"),
        ("/promote [id] <pct>", "Set the Promoted Listings ad rate (2-100%)"),
        ("/pricecheck", "Check live listings against eBay traffic/watchers; suggests price cuts (also runs weekly)"),
        ("/offers [apply] [$]", "Send private discounts to people watching your listings. "
                                "Dry run without 'apply'; add a number for a flat $ off"),
        ("/retry [id]", "Re-run the current pipeline step for an item"),
        ("/delete [id]", "Delete the item, its photos, and its eBay offer"),
        ("/find [box]", "Photo an item: posted or not? Quick add what isn't. /done to stop"),
        ("/stock", "Items quick-added but not posted, best to list first"),
        ("/inv <box>", "Inventory mode: one photo per item, each gets a code (G3-07)"),
        ("/where <q>", "Find pieces by code, box, brand, text or UPC"),
        ("/move <code> <box>", "Piece moved to another box (code stays)"),
        ("/mark <code> <status>", "Mark a piece lot, donated, home or sold"),
        ("/ghosts", "Live listings vs what's actually at home"),
        ("/list <id>", "Post a quick-added item: full pricing, draft, review"),
        ("/notdup <id> <id>", "Two look-alikes are different items: stop the duplicate warning"),
    ]),
    ("Money", [
        ("/sold [id] [price]", "Mark an item sold and record the sale price; replies with profit"),
        ("/profit [id]", "Profit per sold item: paid → sold → fees → net → margin (real fees after "
                         "/sync). With an id: that one item's full breakdown"),
        ("/report", "Excel report: photos, prices, fees, and profit"),
        ("/scoreboard", "What to buy: sell-through, speed and cash tied up per brand and "
                        "category, with buy more / pause / stop"),
    ]),
    ("Admin", [
        ("/photocheck", "Verify every live listing still shows every photo we uploaded"),
        ("/health", "Check eBay token, business policies, ad scope, Cloudinary"),
        ("/usage", "This month's Claude/Gemini spend by model and stage, against the Claude budget"),
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

    # The sync job is worth running even with no one to notify: its real product
    # is a DB that matches eBay, which every money figure reads. The digest is
    # just the by-product, so this is deliberately NOT inside the allowlist check
    # above.
    if SOLD_ALERTS_ENABLED:
        app.create_task(_sold_alert_loop(app))
        print(f"Sold alerts every {SOLD_ALERT_MINUTES} min.")
    if SYNC_ENABLED:
        app.create_task(_daily_sync_loop(app))
        print(f"Daily eBay sync scheduled: hour={SYNC_HOUR_UTC} UTC "
              f"(orders look-back {SYNC_DAYS}d, catch-up grace "
              f"{JOB_CATCHUP_GRACE_HOURS}h).")
        last = last_job_run(_JOB_SYNC)
        print(f"   last completed sync: {last.isoformat() if last else 'never recorded'}")
    else:
        print("⚠️ Daily eBay sync DISABLED (SYNC_ENABLED=0) — sold orders will only "
              "reach the DB when you run /sync by hand, and every money figure "
              "understates itself until you do.")

    if FEEDBACK_ENABLED:
        app.create_task(_daily_feedback_loop(app))
        print(f"Daily buyer feedback scheduled: hour={FEEDBACK_HOUR_UTC} UTC.")
    else:
        print("Buyer feedback automation disabled (FEEDBACK_ENABLED=0); use /feedback.")


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
    from logging_setup import setup_logging
    setup_logging(LOG_DIR)
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
    app.add_handler(CallbackQueryHandler(quick_add_callback, pattern=r"^qa:"))
    app.add_handler(CommandHandler("haul", haul_command))
    app.add_handler(CommandHandler("find", find_command))
    app.add_handler(CommandHandler("done", done_command))
    app.add_handler(CommandHandler("stock", stock_command))
    app.add_handler(CommandHandler("list", list_command))
    app.add_handler(CommandHandler("inv", inv_command))
    app.add_handler(CommandHandler("where", where_command))
    app.add_handler(CommandHandler("move", move_command))
    app.add_handler(CommandHandler("mark", mark_command))
    app.add_handler(CommandHandler("ghosts", ghosts_command))
    app.add_handler(CommandHandler("status", status_command))
    app.add_handler(CommandHandler("ross", ross_command))
    app.add_handler(CommandHandler("errors", errors_command))
    app.add_handler(CommandHandler("comps", comps_command))
    app.add_handler(CommandHandler("sold", sold_command))
    app.add_handler(CommandHandler("profit", profit_command))
    app.add_handler(CommandHandler("report", report_command))
    app.add_handler(CommandHandler("scoreboard", scoreboard_command))
    app.add_handler(CommandHandler("listing", listing_command))
    app.add_handler(CommandHandler("receipt", receipt_command))
    app.add_handler(CommandHandler("addphotos", addphotos_command))
    app.add_handler(CommandHandler("delete", delete_command))
    app.add_handler(CommandHandler("notdup", notdup_command))
    app.add_handler(CommandHandler("end", end_command))
    app.add_handler(CommandHandler("refreshdesc", refreshdesc_command))
    app.add_handler(CommandHandler("photos", photos_command))
    app.add_handler(CommandHandler("cover", cover_command))
    app.add_handler(CommandHandler("arrange", arrange_command))
    app.add_handler(CommandHandler("stockphoto", stockphoto_command))
    app.add_handler(CommandHandler("offers", offers_command))
    app.add_handler(CommandHandler("setprice", setprice_command))
    app.add_handler(CommandHandler("setqty", setqty_command))
    app.add_handler(CommandHandler("sync", sync_command))
    app.add_handler(CommandHandler("feedback", feedback_command))
    app.add_handler(CommandHandler("activate", activate_command))
    app.add_handler(CommandHandler("promote", promote_command))
    app.add_handler(CommandHandler("pricecheck", pricecheck_command))
    app.add_handler(CommandHandler("retry", retry_command))
    app.add_handler(CommandHandler("photocheck", photocheck_command))
    app.add_handler(CommandHandler("health", health_command))
    app.add_handler(CommandHandler("usage", usage_command))
    app.add_handler(CommandHandler("auth", auth_command))
    app.add_handler(CommandHandler("whoami", whoami_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("cancel", cancel_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    app.run_polling()


if __name__ == "__main__":
    main()
