"""Find mode, Quick add, /stock, inventory mode (/inv, /where, /move, /mark,
/ghosts) and /list, which turns stock into a listing.

Moved verbatim out of telegram_bot.py (docs/cleanup-plan.md Phase 2, step 4a).
"""
import asyncio
import shutil
import traceback
from datetime import datetime, timezone
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

import config
import db
import identify
import ross_tag
from bot import flow, intake, state, util
from pipeline import stages


_FIND_DIR = config.DATA_DIR / "cache" / "find"


async def find_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Turn on lookup mode: every photo is checked against what's listed.
    /find G3 also files everything scanned under that box."""
    user_id = update.effective_user.id
    state.find_mode.add(user_id)
    loc = " ".join(context.args or []).strip().upper() or None
    if loc:
        state.find_loc[user_id] = loc
    else:
        state.find_loc.pop(user_id, None)
    where = f" Box: {loc}." if loc else ""
    await util._safe_reply(update.message,
        f"🔎 Find mode on.{where} Send a photo of an item (hang tag or front works best) "
        "and I'll say if it's posted and how many eBay shows.\n"
        "/find G3 to set the box · /done when finished.")


async def done_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if user_id in state.inv_box:
        await _inv_done(user_id, update.message)
        return
    if user_id in state.find_mode:
        _end_find(user_id)
        await util._safe_reply(update.message, "👍 Find mode off. Photos start new listings again.")
    else:
        await util._safe_reply(update.message, "Nothing to finish.")


def _end_find(user_id: int) -> None:
    state.find_mode.discard(user_id)
    state.find_loc.pop(user_id, None)
    old = state.last_find_photo.pop(user_id, None)
    if old:
        Path(old).unlink(missing_ok=True)
    qa = state.quick_add.pop(user_id, None)
    if qa and qa.get("task"):
        qa["task"].cancel()


async def _find_one(user_id: int, message, data: bytes, mid: int) -> None:
    from pipeline.stock_lookup import format_answer, lookup
    _FIND_DIR.mkdir(parents=True, exist_ok=True)
    path = _FIND_DIR / f"find_{mid}.jpg"
    path.write_bytes(data)
    old = state.last_find_photo.get(user_id)
    if old and old != str(path):
        Path(old).unlink(missing_ok=True)
    state.last_find_photo[user_id] = str(path)
    loc = state.find_loc.get(user_id)
    try:
        res = await asyncio.to_thread(lookup, str(path))
    except Exception as e:
        traceback.print_exc()
        await util._safe_reply(message, f"⚠️ Lookup failed: {type(e).__name__}: {str(e)[:200]}")
        return
    text = format_answer(res)
    if res["item"] is not None:
        db.set_location(res["item"]["item_id"], loc)
        if loc:
            text += f"\n📦 Filed under {loc}"
        await util._safe_reply(message, text)
        return
    markup = InlineKeyboardMarkup([[InlineKeyboardButton(
        "⚡ Quick add", callback_data="qa:start")]])
    await util._safe_reply(message, text, reply_markup=markup)


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
    seed = state.last_find_photo.pop(user_id, None)
    old = state.quick_add.pop(user_id, None)
    if old and old.get("task"):
        old["task"].cancel()
    qa = state.quick_add[user_id] = {"photos": [seed] if seed and Path(seed).exists() else [],
                               "task": None, "loc": state.find_loc.get(user_id)}
    await util._safe_reply(query.message,
        "📸 Send 1–2 more photos: front, brand or hang tag, and the Ross tag if it's still on. "
        f"I'll start {_QUICK_ADD_WAIT_NEXT}s after the last one (or in "
        f"{_QUICK_ADD_WAIT_FIRST}s with just the photo you sent).")
    qa["task"] = asyncio.create_task(
        _quick_add_timer(user_id, query.message, _QUICK_ADD_WAIT_FIRST))


async def _quick_add_photo(user_id: int, message, data: bytes, mid: int) -> None:
    qa = state.quick_add[user_id]
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
    qa = state.quick_add.pop(user_id, None)
    if not qa or not qa["photos"]:
        await util._safe_reply(message, "No photos for Quick add, so nothing saved.")
        return
    await util._safe_reply(message, f"⚡ Identifying and pricing ({len(qa['photos'])} photo(s))...")
    try:
        item_id = await asyncio.to_thread(_quick_add_item, qa["photos"], qa["loc"])
    except Exception as e:
        traceback.print_exc()
        util._record_error("quick add", e)
        await util._safe_reply(message, f"⚠️ Quick add failed: {type(e).__name__}: {str(e)[:200]}")
        return
    lines = [_stock_line(db.get_item(item_id), detail=True)]
    lines += stages.duplicate_warnings(item_id) + stages.brand_warnings(item_id)
    lines.append(f"/list {item_id[:8]} when you're ready to post it.")
    await util._safe_reply(message, "\n".join(lines))


def _quick_add_item(paths: list[str], loc: str | None) -> str:
    """Photos -> a 'stocked' item: identified and quick-priced, not listed.
    The Ross tag is only peeled when it's actually recognised; Quick add has
    no 'tag is the last photo' convention to fall back on."""
    from pipeline.quick_price import quick_price
    item_id = db.create_item([])
    folder = intake.INBOX / item_id
    folder.mkdir(parents=True, exist_ok=True)
    photos = []
    for n, p in enumerate(paths):
        dst = folder / f"{item_id}_{n}.jpg"
        shutil.move(p, dst)
        photos.append(str(dst))
    receipt = None
    found = stages.find_tag_photo(photos) if len(photos) >= 2 else None
    if found:
        tag = photos.pop(found[0])
        receipt = ross_tag.extract_receipt(tag)
        receipt["peeled"] = True
        receipt["photo"] = tag
        db.update_field(item_id, "receipt", receipt)
    db.update_field(item_id, "photos", photos)
    ident = identify.identify_item(photos[:config.GEMINI_PHOTO_LIMIT], scan_paths=photos)
    db.update_field(item_id, "identification", ident)
    priced = quick_price(ident, photos[0], receipt, item_id=item_id)
    db.update_field(item_id, "stock", {"location": loc, "quick_price": priced,
                                    "added_at": datetime.now(timezone.utc).isoformat()})
    db.update_status(item_id, "stocked")
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
    items = db.list_items("stocked")
    if not items:
        await util._safe_reply(update.message, "Nothing waiting. Use /find, then ⚡ Quick add on "
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
    await util._send_chunked(update.message, lines)


_PIECES_DIR = config.DATA_DIR / "pieces"


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
        await util._safe_reply(update.message,
            "Usage: /inv G3 — then one photo per item, tag showing.\n"
            "Boxes: G garage, O office, S small bedroom, R full room (G1, O2, R-SHELF1).")
        return
    _end_find(user_id)
    state.inv_box[user_id] = box
    state.inv_session.setdefault(user_id, [])
    await util._safe_reply(update.message,
        f"📦 Inventory mode: {box}. Send one photo per item with the tag showing; "
        f"each gets the next code ({db.next_piece_code(box)} first). Type another box "
        "code (e.g. G4) to switch. /done for the summary.")


async def _inv_photo(user_id: int, message, data: bytes, mid: int) -> None:
    from db import add_piece, update_piece
    box = state.inv_box[user_id]
    _PIECES_DIR.mkdir(parents=True, exist_ok=True)
    code = add_piece(box)
    path = _PIECES_DIR / f"{code}.jpg"
    path.write_bytes(data)
    update_piece(code, photo=str(path))
    state.inv_session.setdefault(user_id, []).append(code)
    sent = await util._safe_reply(message, f"{code} ✓")
    asyncio.create_task(_inv_process(code, sent))


async def _inv_process(code: str, sent) -> None:
    from pipeline.inventory import describe, process_piece
    async with _inv_semaphore():
        try:
            res = await asyncio.to_thread(process_piece, code)
            text = describe(res)
        except Exception as e:
            traceback.print_exc()
            util._record_error(f"inventory {code}", e)
            text = f"{code} ✓ saved, but reading it failed ({type(e).__name__}). /where {code}"
    if sent is not None:
        try:
            await sent.edit_text(util._scrub(text))
            return
        except Exception as e:
            print(f"WARNING: couldn't edit {code} reply: {e}")


async def _inv_done(user_id: int, message) -> None:
    from pipeline.inventory import box_summary
    codes = state.inv_session.pop(user_id, [])
    state.inv_box.pop(user_id, None)
    # Give pieces still being read a moment to finish.
    from db import get_piece
    for _ in range(30):
        if all((get_piece(c) or {}).get("item_id") for c in codes):
            break
        await asyncio.sleep(2)
    await util._send_chunked(message, box_summary(codes) + ["", "/stock for what to list · /ghosts after the whole house"])


async def where_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/where G3-07 · /where G3 · /where oakley · /where 198190095323"""
    from pipeline.inventory import piece_line, search
    q = " ".join(context.args or []).strip()
    if not q:
        await util._safe_reply(update.message, "Usage: /where G3-07 · /where G3 · /where oakley · /where <UPC>")
        return
    found = await asyncio.to_thread(search, q)
    if not found:
        await util._safe_reply(update.message, f"Nothing found for '{q}'.")
        return
    items = {i["item_id"]: i for i in db.list_items()}
    lines = [f"🔎 {len(found)} piece(s) for '{q}':"] + [piece_line(p, items) for p in found[:40]]
    if len(found) > 40:
        lines.append(f"... and {len(found) - 40} more")
    await util._send_chunked(update.message, lines)


async def move_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/move G3-07 R2 — the code stays, the location changes."""
    from db import get_piece, update_piece
    from pipeline.inventory import BOX_RE
    args = [a.upper() for a in (context.args or [])]
    if len(args) != 2 or not BOX_RE.match(args[1]):
        await util._safe_reply(update.message, "Usage: /move G3-07 R2")
        return
    p = get_piece(args[0])
    if not p:
        await util._safe_reply(update.message, f"No piece {args[0]}.")
        return
    update_piece(p["code"], box=args[1])
    await util._safe_reply(update.message, f"👍 {p['code']} is now in {args[1]} (its code stays {p['code']}).")


async def mark_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/mark G3-07 lot|donated|home|sold"""
    from db import PIECE_STATUSES, get_piece, update_piece
    args = context.args or []
    if len(args) != 2 or args[1].lower() not in PIECE_STATUSES:
        await util._safe_reply(update.message, "Usage: /mark G3-07 lot  (lot, donated, home, sold)")
        return
    p = get_piece(args[0])
    if not p:
        await util._safe_reply(update.message, f"No piece {args[0].upper()}.")
        return
    update_piece(p["code"], status=args[1].lower())
    await util._safe_reply(update.message, f"👍 {p['code']} marked {args[1].lower()}.")


async def ghosts_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Live listings vs pieces found at home. Meaningful after the whole house."""
    from pipeline.inventory import ghost_report
    await util._safe_reply(update.message, "🔎 Checking every live listing against what's at home...")
    lines = await asyncio.to_thread(ghost_report)
    await util._send_chunked(update.message, lines)


async def list_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Turn a quick-added item into a real listing: full pricing, draft, review."""
    user_id = update.effective_user.id
    item_id, err = util._resolve_item_id(user_id, context.args or [])
    if err:
        await util._safe_reply(update.message, f"⚠️ {err}")
        return
    item = db.get_item(item_id)
    if item["status"] != "stocked":
        await util._safe_reply(update.message,
                          f"{item_id[:8]} is '{item['status']}', not waiting stock.")
        return
    db.match_quantity_to_pieces(item_id)
    if (item.get("stock") or {}).get("source") == "inventory":
        # The inventory photo shows the Ross tag (our price): never list it.
        # Take real listing photos, peel the tag off those, then carry on.
        async with util._lock_for(user_id):
            state.awaiting_photos[user_id] = item_id
            state.appending.pop(user_id, None)
            state.list_after_photos[user_id] = item_id
        stock = dict(item.get("stock") or {})
        stock["inventory_photo"] = (item.get("photos") or [None])[0]
        db.update_field(item_id, "stock", stock)
        db.update_field(item_id, "photos", [])
        await util._safe_reply(update.message,
            f"📸 Send the listing photos for {item_id[:8]} now, Ross tag last as usual. "
            "I'll price and draft it once they stop arriving.")
        return
    db.update_status(item_id, "identified")
    await util._safe_reply(update.message,
        f"🚀 Listing {item_id[:8]}. Full pricing next, then the draft for review.\n"
        f"Want more photos on the listing? /addphotos {item_id[:8]} any time before you approve.")
    await flow.advance(user_id, item_id, update.message, context)
