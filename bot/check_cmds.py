"""/check: the pre-buy check in Telegram (docs/check-command-design.md).

The work is pipeline/check.py; this file is the Telegram side: the command,
a photo with the caption /check, and the Full check / Any matches buttons.
Nothing is stored: no item is created. A photo is kept in data/check_tmp
only while its buttons can still be tapped (30 minutes), then deleted.
"""
import asyncio
import secrets
import time
import traceback
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

import config
from bot import state, util
from pipeline import check

_DIR = Path(config.DATA_DIR) / "check_tmp"
KEEP_S = 30 * 60
USAGE = ("Usage: /check <item description> <Ross price>\n"
         "e.g. /check Coach Tabby 26 CH857 49.99\n"
         "Or send a photo of the item (or its tag) with the caption /check 49.99.\n"
         "Options at the start: full (full pricing run), any (not only exact matches).")


def _expire() -> None:
    """Drop button tokens and photos older than KEEP_S."""
    now = time.time()
    for token in [t for t, c in state.checks.items() if now - c["ts"] > KEEP_S]:
        state.checks.pop(token, None)
    live = {c.get("photo") for c in state.checks.values()}
    if _DIR.exists():
        for p in _DIR.iterdir():
            if str(p) not in live and now - p.stat().st_mtime > KEEP_S:
                p.unlink(missing_ok=True)


def _markup(token: str, req: dict) -> InlineKeyboardMarkup:
    row = []
    if not req.get("full"):
        row.append(InlineKeyboardButton("🔬 Full check", callback_data=f"chk:full:{token}"))
    if req.get("exact", True):
        row.append(InlineKeyboardButton("Any matches", callback_data=f"chk:any:{token}"))
    else:
        row.append(InlineKeyboardButton("Exact only", callback_data=f"chk:exact:{token}"))
    return InlineKeyboardMarkup([row])


def _identify(path: str) -> dict:
    import identify
    return identify.identify_item([path], scan_paths=[path])


def _tag_price(path: str) -> float | None:
    """The Ross price from a Ross tag's barcode in the photo (no LLM)."""
    import ross_tag
    try:
        return ross_tag.parse_barcode(ross_tag.decode_barcode(path)).get("reduced_price")
    except Exception:
        return None


async def _run(user_id: int, message, req: dict, photo: str | None) -> None:
    """Check limits, say "Checking…", run the check in a thread, edit the
    message with the result."""
    _expire()
    if user_id in state.check_running:
        await util._safe_reply(message, "⏳ Still working on your last /check, one moment.")
        return
    used = check.checks_today()
    if used >= config.CHECK_DAILY_MAX:
        await util._safe_reply(message, f"🛑 Daily limit reached: {used} of {config.CHECK_DAILY_MAX} "
                                        "checks today (CHECK_DAILY_MAX). It resets at midnight.")
        return
    state.check_running.add(user_id)
    try:
        how = "full check, about a minute" if req.get("full") else "quick check, about 20 s"
        sent = await util._safe_reply(message, f"🔎 Checking… ({how})")
        try:
            res = await asyncio.to_thread(check.run_check, req, photo_path=photo,
                                          identify_fn=_identify if photo else None)
        except Exception as e:
            traceback.print_exc()
            text = f"⚠️ Check failed: {type(e).__name__}: {str(e)[:200]}"
            await (sent.edit_text(util._scrub(text)) if sent else util._safe_reply(message, text))
            return
        try:
            check.log_timings(res, user_id)
        except Exception:
            traceback.print_exc()     # the pilot's timings are nice to have, not essential
        text = check.format_reply(res)
        used += 1
        if used >= config.CHECK_DAILY_WARN:
            text += f"\n({used} of {config.CHECK_DAILY_MAX} checks used today)"
        token = secrets.token_hex(4)
        state.checks[token] = {"req": dict(req), "photo": photo, "user": user_id, "ts": time.time()}
        markup = _markup(token, req)
        if sent:
            try:
                await sent.edit_text(util._scrub(text), reply_markup=markup)
                return
            except Exception:
                traceback.print_exc()
        await util._safe_reply(message, text, reply_markup=markup)
    finally:
        state.check_running.discard(user_id)


async def check_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    req = check.parse_request(message.text)
    if not req["description"]:
        await util._safe_reply(message, USAGE)
        return
    await _run(update.effective_user.id, message, req, None)


async def photo_check(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """A photo whose caption starts with /check (registered before the
    general photo handler, so it never starts an item)."""
    message = update.message
    req = check.parse_request(message.caption)
    _DIR.mkdir(parents=True, exist_ok=True)
    f = await context.bot.get_file(message.photo[-1].file_id)
    path = _DIR / f"check_{message.chat_id}_{message.message_id}.jpg"
    path.write_bytes(bytes(await f.download_as_bytearray()))
    if req["ross_price"] is None:
        req["ross_price"] = await asyncio.to_thread(_tag_price, str(path))
    await _run(update.effective_user.id, message, req, str(path))


async def check_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """chk:full|any|exact:<token>: run the same check again, changed."""
    query = update.callback_query
    await query.answer()
    _expire()
    try:
        _, action, token = query.data.split(":", 2)
    except ValueError:
        return
    saved = state.checks.get(token)
    if not saved:
        await util._safe_reply(query.message, "That check has expired. Send /check again.")
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
    if not photo and not req.get("description"):
        await util._safe_reply(query.message, "That photo is gone. Send it again with /check.")
        return
    await _run(update.effective_user.id, query.message, req, photo)
