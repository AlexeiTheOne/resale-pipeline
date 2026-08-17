import os, sys, re, uuid, asyncio, shutil, tempfile, time, traceback
from collections import Counter, OrderedDict, deque
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
    AUTO_CONFIRM, AUTO_CONFIRM_MIN_IDENT_CONFIDENCE,
    AUTO_CONFIRM_MIN_IDENT_CONFIDENCE_WITH_UPC,
    GEMINI_PHOTO_LIMIT, MAX_CONCURRENT_LISTINGS, EBAY_DEFAULT_AD_RATE_PCT,
    REPRICE_WEEKDAY, REPRICE_HOUR_UTC,
    OFFER_MIN_WATCHERS, OFFER_MIN_NET, OFFER_MIN_DAYS_LIVE,
    OFFER_DISCOUNT_SMALL, OFFER_DISCOUNT_LARGE,
    OFFER_LARGE_THRESHOLD, OFFER_MESSAGE,
)
from db import (
    create_item, delete_item, get_item, latest_stats, list_items,
    update_field, update_status, VALID_STATUSES,
)
from identify import identify_item
from receipt import decode_barcode, detect_ross_tags, extract_receipt, is_dark_frame
from pipeline.price import get_pricing
from pipeline.draft import generate_draft, revise_draft, revise_identification
from pipeline.reprice import run_weekly_check
from profit import breakeven_price, format_projection, project
from ebay.auth import get_access_token
from ebay.analytics import get_watch_count, traffic_status
from ebay.orders import finances_status, orders_status, sales_by_item
from ebay.listings import audit_listing_photos, get_active_listings, listings_status
from ebay.inventory import (
    create_draft_offer,
    delete_offer,
    get_offer,
    get_policy_id,
    listing_photo_order,
    publish_offer,
    refresh_offer_description,
    update_offer_price,
    update_offer_quantity,
    withdraw_offer,
    MissingRequiredAspectsError,
)
from ebay.marketing import promote_listing, marketing_status
from ebay.negotiation import (
    MIN_DISCOUNT_PCT, eligible_listing_ids, negotiation_status, send_offer,
)

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
# message_id of a gate/draft the bot sent -> the item it was about. A haul puts
# several drafts on screen at once, so "which item did you mean?" can't be
# answered by a single most-recent pointer: replying to a specific message is how
# the user says which one. Bounded so a long session can't grow it forever.
msg_target: "OrderedDict[int, str]" = OrderedDict()
MSG_TARGET_MAX = 400


def _remember_target(sent, item_id: str) -> None:
    """Tie a message the bot just sent to the item it's about, so a reply to it
    is unambiguous even with a dozen drafts on screen."""
    if sent is None or not getattr(sent, "message_id", None):
        return
    msg_target[sent.message_id] = item_id
    while len(msg_target) > MSG_TARGET_MAX:
        msg_target.popitem(last=False)


def _replied_item(update) -> str | None:
    """The item the user is replying to, if they replied to one of our messages."""
    replied = getattr(update.message, "reply_to_message", None)
    if replied is None:
        return None
    item_id = msg_target.get(replied.message_id)
    return item_id if item_id and get_item(item_id) else None


def _lock_for(user_id):
    if user_id not in locks:
        locks[user_id] = asyncio.Lock()
    return locks[user_id]


# Every secret this process holds, longest first so an overlapping value can't
# leave a fragment behind. Read once at import: these come from the environment
# and don't change while the bot runs.
_SECRETS = sorted(
    (v for v in (os.getenv(k) for k in (
        "APIFY_TOKEN", "TELEGRAM_TOKEN", "GEMINI_API_KEY", "GOOGLE_API_KEY",
        "EBAY_CLIENT_SECRET", "EBAY_CLIENT_ID", "EBAY_REFRESH_TOKEN",
        "CLOUDINARY_API_SECRET", "CLOUDINARY_API_KEY", "CLOUDINARY_URL",
    )) if v and len(v) >= 8),
    key=len, reverse=True)


def _scrub(text: str) -> str:
    """Replace any credential that made it into an outbound string.

    Belt and braces, not the primary defence — the real fix is not putting
    secrets somewhere they can be formatted into a message (see pipeline/price.py's
    _call). But error text reaches Telegram through several paths, str(e) on a
    third-party exception can embed anything, and a chat log is forever. Cheap
    insurance against the next library that decides to be helpful."""
    if not text:
        return text
    for secret in _SECRETS:
        if secret in text:
            text = text.replace(secret, "***REDACTED***")
    return text


async def _safe_reply(message, text: str, **kwargs):
    """Best-effort status ping. A transient network failure sending this
    message must never abort the actual work that follows it. Returns the sent
    Message (or None if it failed) so callers can tie it to an item — see
    _remember_target."""
    try:
        return await message.reply_text(_scrub(text), **kwargs)
    except Exception as e:
        print(f"WARNING: reply_text failed (continuing anyway): {type(e).__name__}: {e}")
        return None


TELEGRAM_MAX_CHARS = 3900  # under the 4096 hard limit, leaving headroom

# Upper bound on a hand-typed /setqty. Ross stock is near-always single units and
# the largest real listing so far held 5, so anything past this is a typo or a
# mis-parsed item id rather than an inventory decision.
_MAX_SANE_QTY = 500
# Same idea for a price. The dearest item handled so far listed around $200, so
# five figures is not a decision anyone makes by typing into a chat window — it's
# an item id, a barcode, or a slipped decimal point.
_MAX_SANE_PRICE = 10_000.0


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
    # Scrubbed at capture, not at display: /errors is one reader of this buffer
    # and a traceback holds request URLs, headers and locals.
    _recent_errors.append((ts, where, _scrub(tb.strip())))


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


# Every id shown back to the user — /status, /photos, error messages, all of
# it — is item_id[:8]. That's the shortest prefix anyone actually types or
# pastes, so it's also the shortest prefix _names_an_item should ever credit as
# "the user meant an id". See its docstring for why a shorter floor breaks it.
_ITEM_ID_PREFIX_LEN = 8


def _names_an_item(token: str) -> bool:
    """Whether this token identifies an existing item (full id or prefix).

    Commands that take an optional trailing number — /sold, /setqty, /setprice —
    must ask this BEFORE trying to parse one. Item ids are uuid4 hex, so roughly
    2.3% of the 8-char prefixes /status prints are all digits: `/sold 40286146`
    parsed as a $40,286,146 sale price, then fell through to `last_item` and
    booked it against whatever item was touched last.

    Getting it wrong in this direction is cheap (the arg resolves to an item and
    no value is set — visible immediately); getting it wrong the other way
    writes nonsense to an item the user never named.

    The prefix check below only fires past _ITEM_ID_PREFIX_LEN characters. Below
    that, hex digits and decimal digits are the same alphabet and there simply
    isn't enough token left to tell "item id" from "quantity" apart: with ~90
    items in the store, a plain "2" is a startswith-prefix of *some* uuid4 far
    more often than not, so every single-digit /setqty and every two-digit
    /sold price was being misread as the id — in both cases the number the user
    typed, not any id, and both directions of `/setqty <id> <n>` broke because
    of it. No real quantity or sane price (see _MAX_SANE_QTY/_MAX_SANE_PRICE)
    is long enough to reach the floor, so genuine short values never get
    swept up here — only the 8+-char strings this bot actually calls an id."""
    if not token:
        return False
    if get_item(token) is not None:
        return True
    if len(token) < _ITEM_ID_PREFIX_LEN:
        return False
    return any(i["item_id"].startswith(token) for i in list_items())


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
        # Haul mode stays armed until /cancel, as the /haul message promises.
        # Consuming it on the first batch meant a pause longer than the capture
        # window silently dropped back to single-item mode, and the next armful
        # of photos became ONE item containing several products and their tags.
        # Keep the long window armed for the next armful too; finalize_capture
        # resets it to the default after every batch.
        capture_window[user_id] = EXTENDED_CAPTURE_WINDOW
        groups, separators, tags = await asyncio.to_thread(_split_haul, paths)
        if not groups:
            await _safe_reply(
                update.message,
                "📦 Haul: those were all blackout frames — no item photos among them. "
                "Send the items, or /cancel to leave haul mode.")
            return
        if len(groups) == 1 and separators == 0 and tags == 0:
            # Nothing divided anything: no blackout, no barcode, no recognised
            # tag. Listing several products as a single item is much worse than
            # asking, so refuse rather than guess.
            await _safe_reply(
                update.message,
                f"📦 Haul: {len(paths)} photo(s), but I couldn't find a single Ross tag "
                f"or blackout frame, so I can't tell where one item ends and the next "
                f"begins.\n\nEither end each item with a photo of its Ross tag, or take "
                f"one dark photo (cover the lens) between items. If this really is a "
                f"single item, /cancel and send it normally.")
            return
        if tags:
            await _safe_reply(
                update.message,
                f"🏷️ Spotted {tags} Ross tag(s) — using them to split the haul.")
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
    """Does this photo carry a Ross price tag's CODE128 barcode?

    Measured over the real photo library this fires on only 41% of tag photos —
    glare, angle and focus defeat the rest — so it is a bonus boundary, never the
    one a haul depends on. See _split_haul."""
    try:
        digits = decode_barcode(path)
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
    tag_indices = detect_ross_tags(paths)

    groups, current, separators = [], [], 0
    for idx, path in enumerate(paths):
        if is_dark_frame(path):
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
        # Re-read rather than trusting ap["base"], the snapshot taken when the
        # first append photo arrived. Minutes can pass between that snapshot and
        # this commit, and the pipeline may well have run in between — writing
        # the stale list back resurrects the peeled Ross price tag and drops
        # whatever else was written meanwhile. Fall back to the snapshot only if
        # the item has vanished.
        current = (get_item(item_id) or {}).get("photos")
        base = current if current is not None else ap["base"]
        photos = list(base) + [p for p in new_sorted if p not in base]

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
    if not (item.get("photo_layout") or {}).get("manual"):
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
    # Lead with the id: during a haul several drafts sit on screen together, and
    # a typed correction has to be aimed at one of them.
    body = f"📄 Draft {item_id[:8]}\n" + format_draft(result)
    sent = await _safe_reply(message, body, parse_mode=None,
                             reply_markup=_review_markup(item_id))
    _remember_target(sent, item_id)


async def _publish_and_report(item_id, message) -> None:
    """Publish an ebay_draft offer, report the live URL and what it stands to
    make, then auto-promote at the default ad rate if one is configured. Shared
    by /activate and advance()."""
    try:
        ebay_data = await asyncio.to_thread(_activate_item, item_id)
    except Exception as e:
        traceback.print_exc()
        await _safe_reply(message, f"⚠️ Activate failed: {type(e).__name__}: {str(e)[:300]}")
        return

    # The profit picture rides along with the "it's live" message rather than
    # waiting for the next /report: the moment to find out an item nets $3 is
    # while you still remember the item, not at the end of the week. Best-effort
    # — a listing that IS live must never be reported as a failed publish because
    # the arithmetic tripped over a half-filled item.
    body = f"🎉 Published! {ebay_data['view_item_url']}"
    try:
        projection = project(get_item(item_id))
        if projection is not None:
            body += "\n\n" + format_projection(projection)
    except Exception as e:
        print("PROFIT PROJECTION FAILED:", traceback.format_exc())
        _record_error(f"profit projection ({item_id[:8]})", e)
    await _safe_reply(message, body)

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
    # Only a DECODED barcode lowers the bar. config.py promises "decoded off the
    # photos", but identify.py only overwrites `upc` when pyzbar actually read
    # one — otherwise the field holds whatever digits the vision model thought it
    # saw, which is the least reliable thing it produces. Letting that lower the
    # bar meant the weakest evidence on the item bought the loosest gate. Items
    # identified before upc_decoded existed have no flag, and absence correctly
    # reads as "not decoded" and leaves them at the higher bar.
    bar = (AUTO_CONFIRM_MIN_IDENT_CONFIDENCE_WITH_UPC
           if ident.get("upc") and ident.get("upc_decoded")
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
            # An item can sit at 'priced' with no price: pricing found no usable
            # comps and refused to guess. Drafting anyway writes "$None" into the
            # listing. The gate's own confirm path guards this, but /retry comes
            # straight here and would sail past it.
            if (item.get("pricing") or {}).get("suggested_price") is None:
                await _show_price_gate(user_id, item_id, message, "no comp-backed price")
                return
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
    body = f"🔎 {item_id[:8]}\n" + format_identification(item["identification"])
    if reason:
        body += f"\n\n🛑 Needs you: {reason}"
    sent = await _safe_reply(
        message,
        body + "\n\nType 'confirm' to price it, or tell me what to fix "
               "(e.g. 'brand is Tommy Jeans, color navy').",
    )
    _remember_target(sent, item_id)


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
    title = ((item.get("identification") or {}).get("product_name")
             or (item.get("identification") or {}).get("item_type") or "")
    body = f"💲 {item_id[:8]} {title}".rstrip() + "\n" + format_pricing(item["pricing"])
    if reason:
        body += f"\n\n🛑 Needs you: {reason}"
    sent = await _safe_reply(
        message,
        body + "\n\nType 'confirm' to write the listing, or type a price to set it "
               "(e.g. 35 -> $34.99).",
    )
    _remember_target(sent, item_id)


async def confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    try:
        _, action, item_id = query.data.split(":", 2)
    except ValueError:
        return

    if action == "start":
        # Identity is checked BEFORE the pop, and inside the lock. Popping first
        # destroyed whatever was staged and only then noticed the button belonged
        # to a different item — so tapping Start on a scrolled-back message
        # cancelled the timer on the item currently waiting and left it stranded
        # with no button and no countdown. Same reasoning in the "cancel" branch.
        async with _lock_for(user_id):
            st = staging.get(user_id)
            if st is None or st["item_id"] != item_id:
                await query.edit_message_text("This item is no longer waiting.")
                return
            staging.pop(user_id, None)
            if st["task"] is not None:
                st["task"].cancel()
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
        # Only clear the staging slot if it's the item this button names. The
        # unconditional pop discarded the item currently waiting while deleting a
        # different one — losing both.
        async with _lock_for(user_id):
            st = staging.get(user_id)
            if st is not None and st["item_id"] == item_id:
                staging.pop(user_id, None)
                if st["task"] is not None:
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


def _find_tag_photo(paths: list[str]) -> tuple[int, str] | None:
    """(index, how) of the Ross price-tag photo, or None to fall back on position.

    Two tiers of evidence, strongest first:

      1. barcode — the photo whose 18-digit Ross CODE128 decodes, wherever it
         sits. Exact, but only about 41% of real tag photos decode (glare,
         angle, a crease through the bars, Telegram's compression).
      2. visual — receipt.detect_ross_tags, which recognises the tag's
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
            digits = decode_barcode(paths[idx])
        except Exception:
            continue
        if digits and len(digits) == 18:
            return idx, "barcode"

    try:
        visual = detect_ross_tags(paths)
    except Exception:
        visual = set()
    if visual:
        # Highest index — keeps the shooting convention when several match.
        return max(visual), "visual"
    return None


def _split_receipt(item_id, paths, notify=lambda _t: None) -> list[str]:
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
    item = get_item(item_id)
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

    found = _find_tag_photo(paths)
    tag_idx = found[0] if found else len(paths) - 1
    if found and tag_idx != len(paths) - 1:
        how = "barcode" if found[1] == "barcode" else "the tag's appearance"
        notify(f"🧾 Ross tag found at photo {tag_idx + 1} of {len(paths)} by {how}, "
               f"not the last one — peeling that one instead.")
    listing_photos = [p for idx, p in enumerate(paths) if idx != tag_idx]
    data = extract_receipt(paths[tag_idx])
    # Keep whatever a manual /receipt already established — the operator read the
    # tag with their own eyes, which beats OCR of a photo that defeated it.
    for field in ("reduced_price", "code", "original_price"):
        if receipt.get(field) is not None and data.get(field) is None:
            data[field] = receipt[field]
    data["peeled"] = True
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


# A typed correction that is really a price instruction: "159.99", "price 159.99",
# "make it $159.99", "sell for 160". Deliberately narrow — it must not fire on
# ordinary copy edits that happen to contain a number ("size 10 not 8", "2 front
# pockets"), so a price word or a currency symbol is required unless the whole
# message is just the number.
_PRICE_CORRECTION = re.compile(
    r"^(?:(?:the\s+)?price\s*(?:should\s*be|is|to|=)?|set\s+(?:the\s+)?price\s*(?:to)?|"
    r"make\s+it|change\s+(?:it|the\s+price)\s*(?:to)?|sell\s+(?:it\s+)?for|list\s+(?:it\s+)?at)?"
    r"\s*\$?\s*(\d{1,5}(?:\.\d{1,2})?)\s*(?:dollars|usd|bucks)?\s*$",
    re.I)


def _price_from_correction(text: str) -> float | None:
    """The price a typed correction is asking for, or None if it isn't one.

    Charm-prices a whole number the same way the price gate does (35 -> 34.99),
    so the two entry points can't disagree."""
    match = _PRICE_CORRECTION.match(text.strip())
    if not match:
        return None
    raw = text.strip()
    # A bare number with no price word and no "$" is ambiguous in a sentence, but
    # unambiguous when it's the entire message.
    if not re.search(r"price|\$|sell|list|make it|change", raw, re.I) and not re.fullmatch(
            r"\$?\s*\d{1,5}(?:\.\d{1,2})?", raw):
        return None
    try:
        return _charm_price(float(match.group(1)))
    except (TypeError, ValueError):
        return None


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
    charm-price it. Returns None if the text doesn't name one.

    Deliberately anchored. This used to `re.search` for a digit run ANYWHERE in
    the text, which made every item id a valid price: `/setprice a1b2c3d4` (the
    price forgotten) matched the "1", charm-priced it to $0.99, and — because
    the id had been consumed as the price — pushed 99 cents to whatever listing
    `last_item` happened to hold. A price is now either the whole token or
    follows a word that means one."""
    raw = text.replace(",", "").strip()
    m = re.fullmatch(r"\$?\s*(\d+(?:\.\d{1,2})?)", raw)
    if m is None:
        # Allow a price embedded in a phrase, but only after an explicit cue —
        # "make it 42.50", "price 35", "$35 please".
        m = re.search(r"(?:price|sell|list|make it|change to|\$)\s*(\d+(?:\.\d{1,2})?)",
                      raw, re.I)
    if m is None:
        return None
    try:
        value = float(m.group(1))
    except (TypeError, ValueError):
        return None
    # A free item is never what someone meant to type.
    return _charm_price(value) if value > 0 else None


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


def _explicit_target(update, text: str) -> tuple[str | None, str]:
    """Which item this message is aimed at, and the message with any id stripped.

    Two ways to aim, both needed once a haul puts several items in play at once:
      * reply to the bot's message for that item (the natural one), or
      * lead with its id: "a1b2c3d4 color is navy".
    Returns (None, text) when neither applies, so single-item use is unchanged."""
    replied = _replied_item(update)
    if replied:
        return replied, text
    parts = text.split(None, 1)
    if len(parts) == 2 and re.fullmatch(r"[0-9a-f-]{4,36}", parts[0].lower()):
        item_id, _ = _resolve_item_id(0, [parts[0].lower()])
        if item_id:
            return item_id, parts[1]
    return None, text


async def _handle_targeted(user_id, item_id, text, update, context) -> bool:
    """Apply a message to a specific item the user aimed at, whatever else is on
    screen. Returns True if it was handled here.

    Without this, a correction typed during a haul lands on whichever draft
    happened to be shown last, which is rarely the one being looked at."""
    item = get_item(item_id)
    if item is None:
        await _safe_reply(update.message, "That item no longer exists.")
        return True
    status = item["status"]

    if status in ("drafted", "review"):
        review[user_id] = item_id
        last_item[user_id] = item_id
        return False  # fall through to the review handler, now pointed correctly

    if status in ("identified", "priced"):
        stage = "identify" if status == "identified" else "price"
        active = gate.get(user_id)
        if active and active["item_id"] != item_id:
            # Put the item we're displacing back at the front of the queue rather
            # than dropping it.
            gate_queue.setdefault(user_id, []).insert(
                0, {"item_id": active["item_id"], "stage": active["stage"],
                    "reason": "returning to this one"})
        # This item is about to be handled here and now, so it must not also
        # remain queued — during a haul it usually IS queued, and leaving the
        # entry meant it came back around after being dealt with and asked for a
        # decision on a gate it had already cleared.
        queued = gate_queue.get(user_id)
        if queued:
            gate_queue[user_id] = [q for q in queued if q["item_id"] != item_id]
        gate[user_id] = {"item_id": item_id, "stage": stage}
        await _handle_gate(user_id, text, update, context)
        return True

    await _safe_reply(
        update.message,
        f"{item_id[:8]} is '{status}' — nothing to change here. "
        f"/listing {item_id[:8]} to see it, or /setprice to change the price.")
    return True


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
        # Hand the slot on — otherwise a deleted item takes the rest of a haul's
        # queued gates down with it.
        await _activate_next_gate(user_id, update.message)
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
        forced = low.endswith(" force")
        price = _parse_price_override(text[:-6] if forced else text)
        if price is None:
            await _safe_reply(update.message, "Type a price to set it (e.g. 35 -> $34.99), or 'confirm'.")
            return
        item = get_item(item_id)
        # Same floor the repricer and /setprice honour — the gate is a price
        # entry point like any other, and this one leads straight to review.
        if not forced:
            refusal = _floor_refusal(item, price)
            if refusal:
                await _safe_reply(update.message,
                                  f"⚠️ {refusal.splitlines()[0]}\nType '{price:.2f} force' to set it anyway.")
                return
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

    # An explicitly aimed message wins over everything: replying to an item's
    # message, or leading with its id, is the user saying which one they mean.
    target, text = _explicit_target(update, text)
    if target:
        low = text.lower().strip()
        if await _handle_targeted(user_id, target, text, update, context):
            return
        # Not handled means it's a review item and `review` now points at it.

    # Confirm gates (after identify / after price) take precedence over the draft
    # review — an item pauses here for a typed 'confirm' or a correction.
    #
    # Exception: 'approve'/'reject' are review words, never gate words. During a
    # haul one item can be gated while another waits at review, and routing an
    # "approve" into the gate fed it to revise_identification as a correction on
    # a different item. Send those to the review handler instead.
    if user_id in gate and not target and low not in ("approve", "reject"):
        await _handle_gate(user_id, text, update, context)
        return
    if user_id in gate and user_id not in review:
        await _safe_reply(
            update.message,
            "Nothing is waiting for approval. This item is at a gate — "
            "'confirm' to accept it, or type a correction / a price.")
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

    # A correction that names a price is a price change, and it goes through the
    # proper path: recorded as manual_price with machine_price preserved, exactly
    # as typing a price at the price gate does. Without this the copywriter would
    # be the one setting it — and the guard that stops the model drifting the
    # price would silently revert the user's own number too.
    wanted = _price_from_correction(text)
    if wanted is not None:
        pricing = _apply_manual_price(item.get("pricing") or {}, wanted)
        update_field(item_id, "pricing", pricing)
        listing = dict(current)
        listing["price"] = wanted
        update_field(item_id, "listing", listing)
        await _safe_reply(update.message, f"💲 Price set to ${wanted} for {item_id[:8]}.")
        await _show_review(user_id, item_id, update.message)
        return

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
    margin (profit / sale), and fees_known.

    `paid` is charged per unit SOLD: sale_price is the realized total across the
    order(s), so a two-unit sale has to carry two units of Ross cost."""
    ebay = item.get("ebay") or {}
    paid = (item.get("receipt") or {}).get("reduced_price")
    try:
        units = max(int(ebay.get("units_sold") or 1), 1)
    except (TypeError, ValueError):
        units = 1
    if paid is not None:
        paid = round(float(paid) * units, 2)
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
                        "order_ids", "fees_known"):
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
    # the stored listing_id.
    #
    # NOT skipped when the item is already sold, which is how later sales used to
    # go missing: a multi-quantity listing sells a unit at a time and a relist
    # sells again under the same sku, so "already sold once" is the START of the
    # sales history, not the end of it. The sold_map figures are aggregates across
    # every order for this item, and order_ids is what makes re-runs idempotent —
    # nothing is written unless an order we haven't seen shows up.
    auto_sold = False
    sale = None
    if sold_map:
        sale = sold_map.get(item_id)
        if sale is None and ebay.get("listing_id"):
            sale = sold_map.get(str(ebay["listing_id"]))

    new_orders = []
    if sale is not None:
        known = set(ebay.get("order_ids") or [])
        if not known and ebay.get("order_id"):
            known = {ebay["order_id"]}   # recorded before order_ids existed
        new_orders = [oid for oid in (sale.get("order_ids") or []) if oid not in known]

    # `sales_by_item(days)` only aggregates orders INSIDE the look-back window,
    # and the block below replaces the stored figures wholesale. So an order that
    # has aged out of the window is erased along with the money it brought in —
    # a `/sync 7` on an item that sold months ago rewrites its lifetime revenue
    # down to whatever the last week happens to contain.
    #
    # Until sales_by_item can return a per-order breakdown to fold in additively,
    # refuse any write that would shrink the record: the stored order ids must
    # all still be present in the incoming set. `known - incoming` being
    # non-empty means the window can't see the whole history, so the aggregate
    # in hand is a subset, not an update.
    incoming = set((sale or {}).get("order_ids") or [])
    missing_from_window = (known - incoming) if sale is not None else set()
    if missing_from_window:
        changes.append(
            f"⚠️ sale figures NOT updated — this look-back sees {len(incoming)} of "
            f"its {len(known | incoming)} orders, so writing them would erase "
            f"${ebay.get('sale_price', 0)} already recorded. Re-run /sync with a "
            f"wider window (e.g. /sync 365) to fold in the newer ones.")

    if sale is not None and not missing_from_window and (item["status"] != "sold" or new_orders):
        ebay["sale_price"] = sale["sale_price"]
        ebay["shipping_collected"] = sale["shipping_collected"]
        ebay["ebay_fees"] = sale["ebay_fees"]
        ebay["shipping_label_cost"] = sale["shipping_label_cost"]
        ebay["net_proceeds"] = sale["net_proceeds"]
        ebay["order_id"] = sale["order_id"]
        ebay["order_ids"] = list(sale.get("order_ids") or [])
        # How many units the money above covers. The report needs it to charge the
        # Ross cost per unit sold: one order took 2 units of the same item, and
        # costing that sale once understated what it took to make.
        ebay["units_sold"] = sale.get("units") or 1
        ebay["sold_at"] = sale["sold_at"]
        ebay["fees_known"] = sale["fees_known"]
        update_field(item_id, "ebay", ebay)
        net = sale["net_proceeds"] if sale["fees_known"] else None
        if item["status"] != "sold":
            update_status(item_id, "sold")
            auto_sold = True
            changes.append(f"SOLD ${sale['sale_price']}" + (f" · net ${net}" if net is not None else ""))
        else:
            changes.append(f"+{len(new_orders)} later sale(s) → {ebay['units_sold']} unit(s) "
                           f"total ${sale['sale_price']}" + (f" · net ${net}" if net is not None else ""))

    listing_info = offer.get("listing") or {}
    listing_status = listing_info.get("listingStatus")
    try:
        sold_qty = int(listing_info.get("soldQuantity") or 0)
    except (TypeError, ValueError):
        sold_qty = 0

    # An ended listing holds no live stock, whatever quantity the offer still
    # records. Zero it, or the report keeps counting merchandise that isn't for
    # sale — a listing ended after selling one unit was still projecting its
    # remaining units as inventory.
    if listing_status == "ENDED" and (ebay.get("quantity") or 0) != 0:
        ebay["quantity"] = 0
        update_field(item_id, "ebay", ebay)
        changes.append("ended on eBay → qty 0")

    ended = (not auto_sold and item["status"] == "published"
             and (listing_status in ("ENDED", "OUT_OF_STOCK") or ebay_qty == 0))
    # "Ended" alone says nothing about whether it SOLD. eBay reports
    # soldQuantity, so use it: a listing that ended with soldQuantity 0 and stock
    # still on it expired or was ended by hand, and wants relisting — telling the
    # user to record a sale sends them to invent one that never happened.
    likely_sold = ended and sold_qty > 0
    ended_unsold = ended and sold_qty == 0
    return {"item_id": item_id, "changes": changes, "auto_sold": auto_sold,
            "likely_sold": likely_sold, "ended_unsold": ended_unsold,
            "listing_status": listing_status}


def _adopt_listing(row: dict) -> str:
    """Create a local item for a live eBay listing the bot didn't make.

    It gets status 'published' and the listing's real title/price/quantity, so it
    counts in /status, /report, /profit and the weekly price check immediately.
    What it can't have is a Ross cost (no receipt was ever scanned) or photos —
    both are marked, and the report already flags a missing cost in orange rather
    than quietly reporting the full sale price as profit."""
    item_id = create_item([])
    update_field(item_id, "listing", {
        "title": row["title"],
        "price": row["price"],
        "description": None,
    })
    update_field(item_id, "ebay", {
        "listing_id": row["listing_id"],
        "view_item_url": row["view_item_url"],
        "sku": row["sku"],
        "quantity": row["quantity"],
        # Marks this as adopted rather than built here: there are no photos, no
        # identification and no offer_id, so the pipeline must not try to redraft
        # or re-push it.
        "source": "external",
        "published_at": datetime.now(timezone.utc).isoformat(),
    })
    update_status(item_id, "published")
    return f"{item_id[:8]} ${row['price']} {row['title'][:40]}"


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

    updated, auto_sold, sold_flags, errors, ended_flags = [], [], [], [], []
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
        if result.get("ended_unsold"):
            ended_flags.append(result["item_id"])

    # Reconcile against every listing actually live on eBay, not just the ones we
    # already know about. Listings made by hand in Seller Hub never touch the
    # Inventory API, so without this pass they're invisible to /status, /report,
    # /profit and the weekly price check. A relist also mints a NEW listing id,
    # which leaves the stored one pointing at a dead listing (and the price check
    # reading traffic for the wrong thing).
    adopted, relinked, live_mismatch, discover_err = [], [], [], None
    relisted_live = []   # were 'sold' locally but still have stock live on eBay
    live_item_ids: set[str] = set()
    try:
        live = await asyncio.to_thread(get_active_listings)
        live_item_ids = {row["sku"] for row in live if row["sku"]}
        all_items = list_items()
        by_sku = {i["item_id"]: i for i in all_items}
        # Second index, on the eBay listing id, because the sku index can't reach
        # an adopted listing: one made by hand in Seller Hub has no SKU at all, so
        # `by_sku.get(row["sku"])` misses, `known_ids` then skipped the row as
        # already-adopted, and _sync_one never touched it either (that loop
        # requires an offer_id, which a hand-made listing doesn't have). Every
        # adopted listing was therefore frozen at whatever price and quantity it
        # had on the day it was adopted, permanently — and _adopt_listing exists
        # precisely so those listings COUNT in /status, /report and /profit.
        by_listing_id = {}
        for i in all_items:
            lid = (i.get("ebay") or {}).get("listing_id")
            if lid:
                by_listing_id[str(lid)] = i
        known_ids = set(by_listing_id)
        for row in live:
            item = by_sku.get(row["sku"]) if row["sku"] else None
            if item is None:
                item = by_listing_id.get(str(row["listing_id"]))
            if item is not None:
                ebay_data = dict(item.get("ebay") or {})
                # Price, for the listings _sync_one can't reach. It owns this for
                # anything with an offer_id and has already run by now; this fills
                # the gap for adopted listings, whose only link to eBay is the row
                # in hand.
                if not ebay_data.get("offer_id") and row["price"] is not None:
                    new_price = round(float(row["price"]), 2)
                    listing = dict(item.get("listing") or {})
                    old_price = listing.get("price")
                    if old_price is None or round(float(old_price), 2) != new_price:
                        listing["price"] = new_price
                        update_field(item["item_id"], "listing", listing)
                        updated.append(f"{item['item_id'][:8]}: price ${old_price}→${new_price} (eBay)")
                changed = False
                if str(ebay_data.get("listing_id") or "") != str(row["listing_id"]):
                    ebay_data["listing_id"] = row["listing_id"]
                    ebay_data["view_item_url"] = row["view_item_url"]
                    changed = True
                    relinked.append(f"{item['item_id'][:8]} → {row['listing_id']}")
                # Take the LIVE quantity from here. _sync_one reads it from the
                # stored offer_id, which after a relist points at the dead offer
                # and reports 0 — so an item that sold one unit and still has
                # stock looked like it had none, and its remaining inventory
                # stopped being counted anywhere. row["quantity"] is AVAILABLE
                # stock (ebay/listings.py nets off QuantitySold); 0 is a real
                # value and must be written, not treated as "no data".
                if row["quantity"] is not None and ebay_data.get("quantity") != row["quantity"]:
                    ebay_data["quantity"] = row["quantity"]
                    changed = True
                if changed:
                    update_field(item["item_id"], "ebay", ebay_data)
                # eBay says this listing is live and the local status says sold.
                # Both can be true at once — a multi-quantity listing that sold one
                # unit, or a relist — and the status is the half that's wrong,
                # because it describes the LISTING while the sale lives in its own
                # ebay fields. So put the status back to what eBay demonstrably
                # says, which loses no sales history: sale_price, order_ids and
                # units_sold are untouched, and the report still counts the money.
                #
                # Only when stock is actually available. Sold out but still active
                # (eBay leaves a listing up briefly) stays 'sold'.
                if item["status"] != "published":
                    if item["status"] == "sold" and row["quantity"] > 0:
                        update_status(item["item_id"], "published")
                        relisted_live.append(
                            f"{item['item_id'][:8]} — {row['sold_quantity']} sold, "
                            f"{row['quantity']} still live at ${row['price']} → back to 'published'")
                    else:
                        live_mismatch.append(
                            f"{item['item_id'][:8]} is '{item['status']}' locally but LIVE "
                            f"at ${row['price']} with {row['quantity']} available")
                continue
            if row["listing_id"] in known_ids:
                continue
            adopted.append(_adopt_listing(row))
    except Exception as e:
        traceback.print_exc()
        _record_error("sync discover", e)
        discover_err = f"{type(e).__name__}: {str(e)[:150]}"

    lines = [f"✅ Sync done — checked {len(items)} item(s) (orders: last {days}d)."]
    if adopted:
        lines += ["", f"🆕 Adopted {len(adopted)} listing(s) made outside the bot:"]
        lines += [f"  {a}" for a in adopted]
        lines.append("  (no photos or Ross cost locally — /receipt <id> <price> <code> "
                     "to make their profit real)")
    if relinked:
        lines += ["", f"🔗 Re-linked {len(relinked)} relisted item(s):"] + [f"  {x}" for x in relinked]
    if relisted_live:
        lines += ["", f"♻️ Sold but still selling ({len(relisted_live)}) — stock left on the "
                  "listing, so these are live inventory again (the recorded sale is kept):"]
        lines += [f"  {x}" for x in relisted_live]
    if live_mismatch:
        lines += ["", f"❓ Live on eBay but not 'published' here ({len(live_mismatch)}) — "
                  "no stock available, so the status is left alone. /end it if it's gone:"]
        lines += [f"  {x}" for x in live_mismatch]
    if discover_err:
        lines += ["", f"⚠️ Couldn't enumerate live listings ({discover_err})"]
    if auto_sold:
        lines += ["", f"🟢 Recorded sold from eBay orders ({len(auto_sold)}):"] + [f"  {a}" for a in auto_sold]
    if updated:
        lines += ["", f"📝 Updated ({len(updated)}):"] + [f"  {u}" for u in updated]
    elif not auto_sold:
        lines.append("No price/quantity changes found.")
    # A relisted item's OLD offer reads as ended/out-of-stock, which is exactly
    # the "likely sold" signal — but the item is demonstrably still live under a
    # new listing id. Drop those: reporting a live listing as probably-sold sends
    # you off to record a sale that never happened.
    sold_flags = [sid for sid in sold_flags if sid not in live_item_ids]
    ended_flags = [sid for sid in ended_flags if sid not in live_item_ids]
    if ended_flags:
        lines += ["", f"🔁 Ended WITHOUT selling ({len(ended_flags)}) — eBay reports "
                  "soldQuantity 0 with stock left. Relist with /activate <id>:"]
        lines += [f"  {sid[:8]}" for sid in ended_flags]
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
        await _activate_next_gate(user_id, update.message)


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


def _items_with_live_stock() -> list[dict]:
    """Everything that still has units for sale on eBay.

    That is NOT the same set as status == 'published'. A multi-quantity listing
    that sells one unit is marked 'sold' while the rest stays live, and a relist
    keeps the sold status until the next /sync — so filtering on the status alone
    silently drops real, for-sale inventory. That case is the exact scenario
    config.py's OFFER_* block cites as the reason offers exist, and it was the one
    case /offers couldn't see.

    Mirrors pipeline/reprice.py's eligibility rule, which had the same bug."""
    out = []
    for item in list_items("published") + list_items("sold"):
        if item["status"] == "published":
            out.append(item)
            continue
        try:
            remaining = int((item.get("ebay") or {}).get("quantity") or 0)
        except (TypeError, ValueError):
            remaining = 0
        if remaining > 0:
            out.append(item)
    return out


def _offer_plan(discount_override: float | None = None) -> list[dict]:
    """Decide which live listings should get an offer to their watchers.

    The gates, in order:
      1. eBay says the listing is eligible (findEligibleItems) — no offer already
         out, and a format that accepts one.
      2. Somebody is actually watching (OFFER_MIN_WATCHERS), read LIVE from eBay
         rather than from the weekly snapshot, which is up to seven days stale and
         blind to everything listed since it ran.
      3. The listing has had a fair run at full price (OFFER_MIN_DAYS_LIVE).
      4. The discount clears eBay's 5% minimum.
      5. What's left still pays: above break-even, and at least OFFER_MIN_NET a
         unit.

    Returns a row per candidate with a `verdict` explaining any skip, so the dry
    run can show the reasoning rather than an unexplained short list. Read-only —
    sending is the caller's job. Ranked by watchers, because that's the ranking of
    how likely the offer is to land."""
    eligible = eligible_listing_ids()
    rows = []
    for item in _items_with_live_stock():
        listing_id = str((item.get("ebay") or {}).get("listing_id") or "")
        price = (item.get("listing") or {}).get("price")
        if not listing_id or listing_id not in eligible or price is None:
            continue

        price = round(float(price), 2)
        # Price passed explicitly. project() now treats any supplied price as a
        # question about the future and refuses to answer it with a past order's
        # figures — see profit.project's docstring. Passing it here is still the
        # right call (it names the listing's current price rather than relying on
        # the default), but it is no longer the only thing standing between a
        # partially-sold listing and a discount priced off economics that belong
        # to units already gone.
        projection = project(item, price=price)
        watchers = get_watch_count(listing_id)
        if watchers is None:   # call failed or eBay omits the field at zero
            watchers = (latest_stats(item["item_id"]) or {}).get("watchers") or 0
        row = {
            "item_id": item["item_id"], "listing_id": listing_id, "price": price,
            "margin": projection["margin"] if projection else None,
            "watchers": watchers, "qty": projection["qty"] if projection else 1,
            "title": ((item.get("listing") or {}).get("title") or "")[:38],
            "cost_estimated": projection["cost_estimated"] if projection else True,
        }

        if watchers < OFFER_MIN_WATCHERS:
            row["verdict"] = "nobody watching"
            rows.append(row)
            continue

        # A young listing hasn't been rejected on price — it hasn't finished being
        # considered. Its watchers arrived days ago and are still deciding.
        when = (item.get("ebay") or {}).get("published_at") or item.get("created_at")
        days_live = None
        if when:
            then = datetime.fromisoformat(when)
            if then.tzinfo is None:
                then = then.replace(tzinfo=timezone.utc)
            days_live = (datetime.now(timezone.utc) - then).total_seconds() / 86400
        row["days_live"] = days_live
        if days_live is not None and days_live < OFFER_MIN_DAYS_LIVE:
            row["verdict"] = f"only {days_live:.1f}d old — let it sell at full price first"
            rows.append(row)
            continue

        discount = discount_override if discount_override is not None else (
            OFFER_DISCOUNT_LARGE if price >= OFFER_LARGE_THRESHOLD else OFFER_DISCOUNT_SMALL)
        offer_price = round(price - discount, 2)
        pct = (discount / price) * 100 if price else 0

        # eBay rejects an offer that barely undercuts the listing, and raising the
        # discount to clear that bar would spend more than was authorised — so
        # this is a skip with a reason, not a silent bigger discount.
        if pct < MIN_DISCOUNT_PCT:
            need = price * MIN_DISCOUNT_PCT / 100
            row["verdict"] = f"${discount:.0f} is only {pct:.1f}% — eBay needs 5% (${need:.2f})"
            rows.append(row)
            continue

        floor = breakeven_price(item)
        if floor is not None and offer_price <= floor:
            row["verdict"] = f"${offer_price:.2f} is under the ${floor:.2f} break-even"
            rows.append(row)
            continue

        after = project(item, price=offer_price)
        net_after = after["net_per_unit"] if after else 0
        if net_after < OFFER_MIN_NET:
            row["verdict"] = f"would net only ${net_after:.2f}/unit"
            rows.append(row)
            continue

        row.update({"send": True, "discount": discount, "offer_price": offer_price,
                    "pct": pct, "margin_after": after["margin"] if after else None,
                    "net_after": net_after, "net_total": after["net"] if after else None})
        rows.append(row)
    rows.sort(key=lambda r: (not r.get("send"), -(r.get("watchers") or 0)))
    return rows


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


def _floor_refusal(item: dict, price: float) -> str | None:
    """Why this price shouldn't be set, or None if it's fine.

    The README has always promised that price changes "respect the margin
    floor", and pipeline/reprice honours it — but the automated path was the
    only one that did. A price typed by hand went straight to eBay, so the one
    route nothing reviews afterwards was also the one with no floor under it.

    Returns None when the cost is unknown: breakeven_price refuses to guess from
    an estimated cost, and a floor nobody can compute must not become a wall."""
    floor = breakeven_price(item)
    if floor is None or price > floor:
        return None
    return (f"${price:.2f} is at or below the ${floor:.2f} break-even — it would "
            f"sell at a loss after fees, ads and postage.\n"
            f"Add `force` to do it anyway (e.g. /setprice {item['item_id'][:8]} "
            f"{price:.2f} force).")


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
    price = _parse_price_override(args[-1])
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
        refusal = _floor_refusal(item, price)
        if refusal:
            await _safe_reply(update.message, f"⚠️ {refusal}")
            return

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


_VERDICT_LABEL = {
    "INVISIBLE": "🙈 Not being found in search",
    "LOW_CTR": "😐 Seen but not clicked",
    "OVERPRICED": "💸 Views but no watchers — likely overpriced",
    "SEND_OFFERS": "👀 Watchers building — consider a private offer before cutting",
    "STALE": "🕸️ Stale — real views, still unsold",
    "STALE_UNSEEN": "🔍 Unsold but barely seen — a findability problem, not a price one",
    "TOO_NEW": "🌱 Too new to judge yet",
    "HEALTHY": "✅ Healthy",
    "NO_WATCH_DATA": "❔ Couldn't read the watch count — not judged",
    "ERROR": "⚠️ Couldn't check",
}

# What to actually do about it. The visibility verdicts never carry a suggested
# price (pipeline/reprice.py only prices OVERPRICED and STALE), so without this
# they'd arrive as a diagnosis with no next step.
_VERDICT_ADVICE = {
    "INVISIBLE": "Fix findability: title keywords, category, item specifics. "
                 "/promote to buy impressions.",
    "LOW_CTR": "They see it and scroll past: cover photo first, then title. "
               "Not a price cut.",
    "STALE_UNSEEN": "Too few views to blame the price. Rework the title/keywords "
                    "and consider /promote, or /end and relist to refresh ranking.",
    "SEND_OFFERS": "Send an offer to the watchers before cutting the public price.",
    "NO_WATCH_DATA": "eBay didn't return a watch count, and every remaining check "
                     "depends on it. Nothing is wrong with the listing as far as "
                     "this run can tell — re-run /pricecheck later.",
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
    views = f"{r.get('views', 0)} views"
    if cum is not None and cum != r.get("views"):
        views += f" ({cum} total)"
    ctr = r.get("ctr")
    line += (f"\n  {r.get('impressions', 0)} impr · {views} · "
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
        markup = (_reprice_markup(r["item_id"], r["suggested_price"])
                  if r.get("suggested_price") else None)
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
        _, action, price_field, item_id = query.data.split(":", 3)
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
        ("/receipt [id] <price> [code]", "Set the Ross cost by hand; code is optional (omit or 'none' to auto-fill)"),
    ]),
    ("Selling", [
        ("/setprice [id] <price>", "Set the price (35 -> $34.99); pushes to eBay if it already has an offer"),
        ("/setqty [id] <n>", "Set the quantity on eBay (once the item has an offer); default is 1"),
        ("/sync [days]", "Pull live price/quantity + real sold orders from eBay; auto-marks sold items with actual fees"),
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
    ]),
    ("Money", [
        ("/sold [id] [price]", "Mark an item sold and record the sale price; replies with profit"),
        ("/profit [id]", "Profit per sold item: paid → sold → fees → net → margin (real fees after "
                         "/sync). With an id: that one item's full breakdown"),
        ("/report", "Excel report: photos, prices, fees, and profit"),
    ]),
    ("Admin", [
        ("/photocheck", "Verify every live listing still shows every photo we uploaded"),
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
    app.add_handler(CommandHandler("refreshdesc", refreshdesc_command))
    app.add_handler(CommandHandler("photos", photos_command))
    app.add_handler(CommandHandler("cover", cover_command))
    app.add_handler(CommandHandler("arrange", arrange_command))
    app.add_handler(CommandHandler("offers", offers_command))
    app.add_handler(CommandHandler("setprice", setprice_command))
    app.add_handler(CommandHandler("setqty", setqty_command))
    app.add_handler(CommandHandler("sync", sync_command))
    app.add_handler(CommandHandler("activate", activate_command))
    app.add_handler(CommandHandler("promote", promote_command))
    app.add_handler(CommandHandler("pricecheck", pricecheck_command))
    app.add_handler(CommandHandler("retry", retry_command))
    app.add_handler(CommandHandler("photocheck", photocheck_command))
    app.add_handler(CommandHandler("health", health_command))
    app.add_handler(CommandHandler("auth", auth_command))
    app.add_handler(CommandHandler("whoami", whoami_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("cancel", cancel_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    app.run_polling()


if __name__ == "__main__":
    main()
