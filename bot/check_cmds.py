"""/check and /shop: the pre-buy check in Telegram (docs/check-command-design.md).

The work is pipeline/check.py; this file is the Telegram side.

  /shop        shopping mode for SHOP_HOURS (or until /shop off): every photo
               or album is a check, never a new item. Intake, /find, /inv and
               /haul are untouched and resume afterwards.
  album        1-3 photos within ALBUM_WAIT_S = one check. The Ross tag is
               found by its barcode (price + SKU, no AI); the rest go to
               identification. No tag -> the price is asked (buttons or one
               typed number) while identification runs.
  /check ...   typed fallback; a photo captioned /check works too.

Every check is saved (db.add_check, table checks; not an item), photos under
data/checks/<check_id>/. Buttons: Full check, Any/Exact matches, Bought.
"""
import asyncio
import re
import secrets
import time
import traceback
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes, filters

import config
import db
from bot import state, util
from pipeline import check

CHECKS_DIR = Path(config.DATA_DIR) / "checks"
SHOP_HOURS = 3
ALBUM_WAIT_S = 3.0
MAX_PHOTOS = 3
PRICE_WAIT_S = 10 * 60
KEEP_S = 30 * 60          # how long a reply's buttons work
PRICE_BUTTONS = (4.99, 7.99, 9.99, 12.99, 14.99, 19.99, 24.99, 29.99)
USAGE = ("Usage: /check <item description> <Ross price>\n"
         "e.g. /check Coach Tabby 26 CH857 49.99\n"
         "Easier in a shop: /shop, then just send photos (item + Ross tag).")
_NUMBER = re.compile(r"^\s*\$?(\d{1,4}(?:[.,]\d{1,2})?)\s*$")


# --- shopping mode -------------------------------------------------------------------

def shopping(user_id: int) -> bool:
    """On and not expired (an expired entry is dropped here, in case the
    timer was lost, e.g. a restart)."""
    s = state.shop_mode.get(user_id)
    if s and time.time() < s["until"]:
        return True
    if s:
        state.shop_mode.pop(user_id, None)
    return False


class _ShoppingFilter(filters.MessageFilter):
    def filter(self, message) -> bool:
        return bool(message.from_user) and shopping(message.from_user.id)


class _AwaitingPriceFilter(filters.MessageFilter):
    """A bare number while a check waits for its Ross price."""
    def filter(self, message) -> bool:
        w = state.shop_price_wait.get(message.from_user.id) if message.from_user else None
        return bool(w and time.time() - w["ts"] < PRICE_WAIT_S and _NUMBER.match(message.text or ""))


SHOPPING = _ShoppingFilter()
AWAITING_PRICE = _AwaitingPriceFilter()


async def _shop_timer(user_id: int, bot, chat_id: int, until: float) -> None:
    await asyncio.sleep(max(0.0, until - time.time()))
    s = state.shop_mode.get(user_id)
    if s and s["until"] == until:
        state.shop_mode.pop(user_id, None)
        try:
            await bot.send_message(chat_id, "🛒 Shopping mode ended. Photos are new items again; "
                                            "/shop to turn it back on.")
        except Exception:
            traceback.print_exc()


async def shop_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id, message = update.effective_user.id, update.message
    args = [a.lower() for a in (context.args or [])]
    if args and args[0] in ("off", "stop", "end"):
        s = state.shop_mode.pop(user_id, None)
        if s and s.get("task"):
            s["task"].cancel()
        await util._safe_reply(message, "🛒 Shopping mode OFF. Photos are new items again."
                               if s else "Shopping mode wasn't on.")
        return
    old = state.shop_mode.get(user_id)
    if old and old.get("task"):
        old["task"].cancel()
    until = time.time() + SHOP_HOURS * 3600
    task = asyncio.create_task(_shop_timer(user_id, context.bot, message.chat_id, until))
    state.shop_mode[user_id] = {"until": until, "task": task}
    ends = time.strftime("%H:%M", time.localtime(until))
    await util._safe_reply(message, f"🛒 Shopping mode ON until {ends}. Every photo or album is a "
                                    "check, not a new item: send the item and its Ross tag together "
                                    "(up to 3 photos). /shop off to end.")


# --- photos -> one check ----------------------------------------------------------------

async def shop_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """A photo in shopping mode: collect it; ALBUM_WAIT_S after the last one,
    the batch becomes one check."""
    message, user_id = update.message, update.effective_user.id
    f = await context.bot.get_file(message.photo[-1].file_id)
    data = bytes(await f.download_as_bytearray())
    batch = state.shop_batch.setdefault(user_id, {"photos": [], "message": message, "task": None,
                                                  "price": None})
    batch["photos"].append(data)
    typed = check.parse_request(message.caption)["ross_price"] if message.caption else None
    batch["price"] = typed or batch["price"]     # "/check 14.99" or "14.99" as a caption
    if batch["task"]:
        batch["task"].cancel()
    batch["task"] = context.application.create_task(_close_batch(user_id, ALBUM_WAIT_S))


def _save_photos(check_id: int, photos: list[bytes]) -> list[str]:
    folder = CHECKS_DIR / str(check_id)
    folder.mkdir(parents=True, exist_ok=True)
    paths = []
    for i, data in enumerate(photos, 1):
        p = folder / f"{i}.jpg"
        p.write_bytes(data)
        paths.append(str(p))
    return paths


async def _close_batch(user_id: int, wait: float) -> None:
    await asyncio.sleep(wait)
    batch = state.shop_batch.pop(user_id, None)
    if not batch:
        return
    message, photos = batch["message"], batch["photos"]
    extra = len(photos) - MAX_PHOTOS
    photos = photos[:MAX_PHOTOS]
    check_id = await asyncio.to_thread(db.add_check, user_id=user_id, mode="quick", exact=True)
    paths = await asyncio.to_thread(_save_photos, check_id, photos)
    roles = await asyncio.to_thread(check.photo_roles, paths)
    await asyncio.to_thread(db.update_check, check_id, photos=[config.stored_path(p) for p in paths],
                            ross_sku=roles["sku"], ross_price=roles["price"])
    note = f" (only the first {MAX_PHOTOS} photos are used; {extra} ignored)" if extra > 0 else ""
    if roles.get("price_source") == "vision":
        note += " (Ross price read from the tag by AI: check it)"
    if roles["price"] is None and batch.get("price"):
        roles["price"] = batch["price"]
        await asyncio.to_thread(db.update_check, check_id, ross_price=roles["price"])
    if roles["price"] is None:
        await _ask_price(user_id, message, check_id, roles, note)
        return
    req = {"description": "", "ross_price": roles["price"], "ross_sku": roles["sku"], "exact": True,
           "full": False}
    await _run(user_id, message, req, _pricing_photo(roles), check_id=check_id, roles=roles,
               note=note, guard=False)


def _pricing_photo(roles: dict) -> str | None:
    """The photo for eBay image search: an item photo, not the tag."""
    return (roles.get("identify") or [None])[0]


def _identify_roles(roles: dict) -> dict:
    import identify
    return identify.identify_item(roles["identify"], scan_paths=roles["scan"])


async def _ask_price(user_id, message, check_id, roles, note="") -> None:
    """No Ross barcode: ask the price; identification starts meanwhile."""
    task = asyncio.create_task(asyncio.to_thread(_identify_roles, roles)) if roles["identify"] else None
    state.shop_price_wait[user_id] = {"check_id": check_id, "roles": roles, "ident_task": task,
                                      "message": message, "ts": time.time()}
    buttons = [InlineKeyboardButton(f"${p:g}", callback_data=f"chkp:{check_id}:{p}") for p in PRICE_BUTTONS]
    markup = InlineKeyboardMarkup([buttons[:4], buttons[4:]])
    await util._safe_reply(message, f"🛒 Check #{check_id}: no Ross tag barcode found{note}.\n"
                                    "What did it cost? Tap a price or send the number.",
                           reply_markup=markup)


async def _price_given(user_id: int, message, check_id: int, price: float) -> None:
    w = state.shop_price_wait.get(user_id)
    if not w or w["check_id"] != check_id:
        await util._safe_reply(message, "That check isn't waiting for a price any more.")
        return
    state.shop_price_wait.pop(user_id, None)
    ident = None
    if w.get("ident_task"):
        try:
            ident = await w["ident_task"]
        except Exception:
            traceback.print_exc()
    await asyncio.to_thread(db.update_check, check_id, ross_price=price)
    req = {"description": "", "ross_price": price, "ross_sku": w["roles"].get("sku"), "exact": True,
           "full": False}
    if ident:
        req["identification"] = ident
    await _run(user_id, w["message"], req, _pricing_photo(w["roles"]), check_id=check_id, roles=w["roles"],
               guard=False)


async def price_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    try:
        _, cid, price = query.data.split(":", 2)
        await _price_given(update.effective_user.id, query.message, int(cid), float(price))
    except ValueError:
        return


async def price_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    w = state.shop_price_wait.get(update.effective_user.id)
    m = _NUMBER.match(update.message.text or "")
    if w and m:
        await _price_given(update.effective_user.id, update.message, w["check_id"],
                           float(m.group(1).replace(",", ".")))


# --- running a check -----------------------------------------------------------------------

def _expire() -> None:
    now = time.time()
    for token in [t for t, c in state.checks.items() if now - c["ts"] > KEEP_S]:
        state.checks.pop(token, None)


def _markup(token: str, req: dict, bought: bool = False) -> InlineKeyboardMarkup:
    row = []
    if not req.get("full"):
        row.append(InlineKeyboardButton("🔬 Full check", callback_data=f"chk:full:{token}"))
    row.append(InlineKeyboardButton("Any matches" if req.get("exact", True) else "Exact only",
                                    callback_data=f"chk:{'any' if req.get('exact', True) else 'exact'}:{token}"))
    if not bought:
        row.append(InlineKeyboardButton("🛍 Bought", callback_data=f"chk:bought:{token}"))
    return InlineKeyboardMarkup([row, [InlineKeyboardButton("Details", callback_data=f"chk:details:{token}")]])


def _identify_one(path: str) -> dict:
    import identify
    return identify.identify_item([path], scan_paths=[path])


def _log(res: dict, user_id: int, counted: bool = True) -> None:
    try:
        check.log_timings(res, user_id, counted=counted)
    except Exception:
        traceback.print_exc()     # the pilot's timings are nice to have, not essential


async def _run(user_id: int, message, req: dict, photo: str | None, *, check_id: int | None = None,
               roles: dict | None = None, note: str = "", guard: bool = True) -> None:
    """Limits, "Checking…", the check in a thread (quick; NO DATA goes straight
    on to full), save it, reply with buttons."""
    _expire()
    if guard and user_id in state.check_running:
        await util._safe_reply(message, "⏳ Still working on your last /check, one moment.")
        return
    used = check.checks_today()
    if used >= config.CHECK_DAILY_MAX:
        await util._safe_reply(message, f"🛑 Daily limit reached: {used} of {config.CHECK_DAILY_MAX} "
                                        "checks today (CHECK_DAILY_MAX). It resets at midnight.")
        return
    shop = "🛒 " if shopping(user_id) else ""
    if check_id is None:
        check_id = await asyncio.to_thread(db.add_check, user_id=user_id, ross_price=req.get("ross_price"),
                                           mode="full" if req.get("full") else "quick",
                                           exact=bool(req.get("exact", True)))
    if guard:
        state.check_running.add(user_id)
    try:
        how = "full check, about a minute" if req.get("full") else "quick check, about 20 s"
        sent = await util._safe_reply(message, f"{shop}🔎 Check #{check_id}: checking… ({how}){note}")
        identify_fn = None
        if photo and not req.get("identification"):
            identify_fn = (lambda _p: _identify_roles(roles)) if roles else _identify_one
        try:
            res = await asyncio.to_thread(check.run_check, req, photo_path=photo, identify_fn=identify_fn)
            if res["verdict"] == check.NO_DATA and not req.get("full"):
                # Nothing at all in the quick check: straight on to the full
                # check; the pair counts as one (Person B, 2026-11-02).
                _log(res, user_id, counted=False)
                if sent:
                    try:
                        await sent.edit_text(f"{shop}❓ Check #{check_id}: NO DATA in the quick check.\n"
                                             "Checking more sources… (full check, about a minute)")
                    except Exception:
                        traceback.print_exc()
                req = {**req, "full": True, "auto_full": True, "identification": res["identification"]}
                res = await asyncio.to_thread(check.run_check, req, photo_path=photo, identify_fn=None)
        except Exception as e:
            traceback.print_exc()
            text = f"⚠️ Check #{check_id} failed: {type(e).__name__}: {str(e)[:200]}"
            await (sent.edit_text(util._scrub(text)) if sent else util._safe_reply(message, text))
            return
        _log(res, user_id)
        try:
            await asyncio.to_thread(db.update_check, check_id, **check.check_fields(res))
        except Exception:
            traceback.print_exc()
        used += 1
        footer = note.strip(" ()")
        if used >= config.CHECK_DAILY_WARN:
            footer = " · ".join(x for x in (footer, f"{used}/{config.CHECK_DAILY_MAX} today") if x)
        # HTML so the links have short labels ("🔗 Sold $30.49 (pink)"); plain text if Telegram refuses it
        html = check.format_reply(res, check_id=check_id, prefix=shop, footer=footer, html=True)
        text = check.format_reply(res, check_id=check_id, prefix=shop, footer=footer)
        token = secrets.token_hex(4)
        saved = {k: v for k, v in req.items() if k != "auto_full"}
        saved["identification"] = res["identification"]     # buttons don't re-identify
        state.checks[token] = {"req": saved, "photo": photo, "user": user_id, "ts": time.time(),
                               "check_id": check_id,
                               "details": check.format_details(res, check_id=check_id,
                                                               sku=req.get("ross_sku"))}
        markup = _markup(token, req)
        if sent:
            try:
                await sent.edit_text(util._scrub(html), reply_markup=markup, parse_mode="HTML",
                                     disable_web_page_preview=True)
                return
            except Exception:
                traceback.print_exc()
        if not await util._safe_reply(message, html, reply_markup=markup, parse_mode="HTML",
                                      disable_web_page_preview=True):
            await util._safe_reply(message, text, reply_markup=markup, disable_web_page_preview=True)
    finally:
        if guard:
            state.check_running.discard(user_id)


# --- typed /check and a captioned photo (fallbacks) ----------------------------------------

async def check_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    req = check.parse_request(message.text)
    if not req["description"]:
        await util._safe_reply(message, USAGE)
        return
    await _run(update.effective_user.id, message, req, None)


async def photo_check(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """A photo captioned /check (outside shopping mode; registered before intake)."""
    message, user_id = update.message, update.effective_user.id
    req = check.parse_request(message.caption)
    f = await context.bot.get_file(message.photo[-1].file_id)
    data = bytes(await f.download_as_bytearray())
    check_id = await asyncio.to_thread(db.add_check, user_id=user_id, mode="quick", exact=req["exact"])
    paths = await asyncio.to_thread(_save_photos, check_id, [data])
    roles = await asyncio.to_thread(check.photo_roles, paths)
    if req["ross_price"] is None:
        req["ross_price"] = roles["price"]
    await asyncio.to_thread(db.update_check, check_id, photos=[config.stored_path(p) for p in paths],
                            ross_sku=roles["sku"], ross_price=req["ross_price"])
    req["ross_sku"] = roles["sku"]
    await _run(user_id, message, req, paths[0], check_id=check_id, roles=roles)


# --- buttons ---------------------------------------------------------------------------------

async def check_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """chk:full|any|exact|bought:<token>."""
    query = update.callback_query
    await query.answer()
    _expire()
    try:
        _, action, token = query.data.split(":", 2)
    except ValueError:
        return
    saved = state.checks.get(token)
    if not saved:
        await util._safe_reply(query.message, "That check has expired. Send it again.")
        return
    if action == "details":
        await util._safe_reply(query.message, saved.get("details") or "No details kept for this check.",
                               disable_web_page_preview=True)
        return
    if action == "bought":
        await asyncio.to_thread(db.set_check_status, saved["check_id"], "bought")
        try:
            await query.message.edit_reply_markup(reply_markup=_markup(token, saved["req"], bought=True))
        except Exception:
            traceback.print_exc()
        await util._safe_reply(query.message, f"🛍 Check #{saved['check_id']} marked bought.")
        return
    req = dict(saved["req"])
    if action == "full":
        req["full"] = True
    elif action in ("any", "exact"):
        req["exact"] = action == "exact"
    else:
        return
    photo = saved.get("photo")
    if photo and not Path(photo).exists():
        photo = None
    await _run(update.effective_user.id, query.message, req, photo, check_id=saved["check_id"])
