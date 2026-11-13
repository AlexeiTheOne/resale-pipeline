"""/revise: change a live listing's title, item specifics and description in
place (the same eBay listing). Owner only, one listing at a time.

    /revise d7432192
    title: New title
    Style: Shoulder Bag
    Theme: Skull, Tattoo     <- a comma gives several values where eBay allows several
    Pattern: -               <- a dash removes the specific
    description:
    The new description, to the end of the message.

A new description with no SHIPPING/RETURNS section keeps the listing's current
one underneath.

Nothing is written until the owner taps Apply under the preview. The "before"
in the preview is what eBay shows now, read live. Apply re-checks eBay and the
DB, then writes eBay first and the DB last (pipeline/revise.apply_plan).
This module only parses the message, shows the preview and handles the
buttons; the plan and the writes are in pipeline/revise.py.
Failures are logged (/errors and bot.log).
"""
import asyncio
import re
import secrets
import time

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

import config
from bot import state, util
from pipeline import revise

PREVIEW_TTL = 30 * 60   # seconds an Apply button stays valid
REMOVE = "-"
USAGE = ("Usage, all in one message:\n"
         "/revise <id>\n"
         "title: New title\n"
         "Style: Shoulder Bag\n"
         "Theme: Skull, Tattoo   (comma = several values, where eBay allows it)\n"
         "Pattern: -   (removes it)\n"
         "description:\n"
         "New description text, to the end of the message.\n\n"
         "Every line is optional except the first.\n\n"
         "Several listings: separate them with a line of ---, each block starting with its id:\n"
         "/revise c646d6f0\ntitle: ...\n---\n5fdc486f\ntitle: ...\nExterior Color: Multicolor\n"
         "One preview for all, one Apply.")


# The plan and apply steps live in pipeline/revise.py; parse() raises the same
# error so the handler catches one type.
ReviseError = revise.ReviseError


def is_owner(user) -> bool:
    return user is not None and user.id in config.OWNER_USER_IDS


# --- parsing -------------------------------------------------------------------

_FIELD = re.compile(r"^\s*([^:\n]{1,60}?)\s*:\s*(.*)$")


def parse(text: str) -> tuple[list[str], dict]:
    """Split a /revise message into the id argument(s) and the requested change:
    {"title", "set" (name -> raw value), "remove" (names), "description"}."""
    lines = (text or "").splitlines()
    args = lines[0].split()[1:] if lines else []
    change = {"title": None, "set": {}, "remove": [], "description": None}
    seen = set()
    rest = lines[1:]
    for i, line in enumerate(rest):
        if not line.strip():
            continue
        m = _FIELD.match(line)
        if not m:
            raise ReviseError(f"Can't read this line (expected 'Name: value'):\n{line.strip()}")
        name, value = m.group(1).strip(), m.group(2).strip()
        key = name.lower()
        if key in seen:
            raise ReviseError(f"'{name}' is given twice.")
        seen.add(key)
        if key == "description":
            change["description"] = "\n".join([value] + rest[i + 1:]).strip()
            if not change["description"]:
                raise ReviseError("'description:' has no text after it.")
            break
        if not value:
            raise ReviseError(f"'{name}:' has no value. Use '{name}: -' to remove it.")
        if key == "title":
            change["title"] = value
        elif value == REMOVE:
            change["remove"].append(name)
        else:
            change["set"][name] = value
    return args, change


_SEPARATOR = re.compile(r"^\s*-{3,}\s*$")


def split_blocks(text: str) -> list[tuple[list[str], dict]]:
    """One (args, change) per listing. A message without --- lines is one
    listing, exactly as parse() reads it. With them, each block after the
    first starts with its id on its own line; the first block's id is on the
    /revise line (or, if not, on the block's first line)."""
    lines = (text or "").splitlines()
    if not any(_SEPARATOR.match(ln) for ln in lines[1:]):
        return [parse(text)]
    head = lines[0].split()[1:] if lines else []
    blocks, current = [], []
    for ln in lines[1:]:
        if _SEPARATOR.match(ln):
            blocks.append(current)
            current = []
        else:
            current.append(ln)
    blocks.append(current)
    out = []
    for n, block in enumerate(blocks):
        body = list(block)
        if n == 0 and head:
            ids = head
        else:
            while body and not body[0].strip():
                body.pop(0)
            if not body:
                raise ReviseError(f"Listing {n + 1} is empty.")
            ids = body.pop(0).split()
        if len(ids) != 1 or ":" in ids[0]:
            raise ReviseError(f"Listing {n + 1} must start with one id on its own line.")
        out.append(parse("/revise " + ids[0] + "\n" + "\n".join(body)))
    return out


def _fmt(values) -> str:
    if values is None:
        return "(none)"
    return ", ".join(values) if isinstance(values, list) else str(values)


def preview_lines(plan: dict) -> list[str]:
    lines = [f"✏️ Revise {plan['item_id'][:8]} (eBay {plan['listing_id']}). Nothing sent yet.", ""]
    if plan["title"] is not None:
        lines += ["TITLE", f"  before: {plan['title_before']}",
                  f"  after:  {plan['title']} ({len(plan['title'])}/80)", ""]
    else:
        lines += [f"TITLE (unchanged): {plan['title_before']}", ""]
    if plan["aspect_diff"]:
        lines.append("SPECIFICS")
        for name, before, after in plan["aspect_diff"]:
            lines.append(f"  {name}: {_fmt(before)} → {'(removed)' if after is None else _fmt(after)}")
        kept = plan["unchanged_aspects"]
        ids = [f"{k} {_fmt(v)}" for k, v in kept.items() if k.lower() in ("upc", "ean", "isbn", "mpn")]
        lines.append(f"  unchanged: {len(kept)} others" + (f", incl. {'; '.join(ids)}" if ids else ""))
        lines.append("")
    else:
        lines += ["SPECIFICS: unchanged", ""]
    if plan["desc_text"] is not None:
        note = "" if plan["desc_before_is_ours"] else " (as eBay shows it; edited outside the bot)"
        lines += [f"DESCRIPTION before{note}:", plan["desc_before"], "",
                  "DESCRIPTION after:", plan["desc_text"]]
    else:
        lines.append("DESCRIPTION: unchanged")
    return lines


# --- Telegram handlers ----------------------------------------------------------------

def _expire_old() -> None:
    now = time.time()
    for token in [t for t, p in state.revise.items() if now - p["ts"] > PREVIEW_TTL]:
        state.revise.pop(token, None)


def _no_change(change: dict) -> bool:
    return (change["title"] is None and not change["set"] and not change["remove"]
            and change["description"] is None)


async def revise_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if not is_owner(update.effective_user):
        await util._safe_reply(message, "⛔ /revise is owner-only."
                               + ("" if config.OWNER_USER_IDS else
                                  " No owner is set: add OWNER_USER_IDS to .env and restart."))
        return
    try:
        blocks = split_blocks(message.text or "")
    except ReviseError as e:
        await util._safe_reply(message, f"⚠️ {e}\n\n{USAGE}")
        return
    if len(blocks) > config.REVISE_MAX_LISTINGS:
        await util._safe_reply(message, f"⚠️ {len(blocks)} listings; one /revise takes up to "
                                        f"{config.REVISE_MAX_LISTINGS}. Nothing changed.")
        return
    if len(blocks) == 1 and len(blocks[0][0]) != 1:
        await util._safe_reply(message, "One listing per block: /revise <id> on the first line.\n\n" + USAGE)
        return
    if any(_no_change(change) for _, change in blocks):
        await util._safe_reply(message, USAGE)
        return

    item_ids, errors = [], []
    for args, _ in blocks:
        item_id, err = util._resolve_item_id(update.effective_user.id, args)
        if err:
            errors.append(f"{args[0]}: {err}")
        elif item_id in item_ids:
            errors.append(f"{item_id[:8]} is given twice.")
        item_ids.append(item_id)
    plans = []
    if not errors:
        for item_id, (_, change) in zip(item_ids, blocks):
            try:
                plans.append(await asyncio.to_thread(revise.build_plan, item_id, change))
            except ReviseError as e:
                errors.append(f"{item_id[:8]}: {e}")
            except Exception as e:
                util._record_error(f"revise preview ({item_id[:8]})", e)
                errors.append(f"{item_id[:8]}: couldn't read the listing from eBay "
                              f"({type(e).__name__}: {str(e)[:150]})")
    if errors:
        # All or nothing at preview time: a partial preview is easy to Apply by mistake.
        await util._send_chunked(message, ["⚠️ Nothing changed:"] + [f"  {e}" for e in errors])
        return

    _expire_old()
    token = secrets.token_hex(4)
    state.revise[token] = {"user": update.effective_user.id, "ts": time.time(), "plans": plans}
    if len(plans) == 1:
        lines = preview_lines(plans[0])
        ask = f"Apply these changes to eBay listing {plans[0]['listing_id']}? Nothing has been sent yet."
        apply_label = "✅ Apply to eBay"
    else:
        lines = [f"✏️ Revise {len(plans)} listings. Nothing sent yet.", ""]
        for n, plan in enumerate(plans, 1):
            lines += [f"── {n} of {len(plans)} ──"] + preview_lines(plan)[1:] + [""]
        ask = (f"Apply all {len(plans)} to eBay? Each is sent to eBay first, then saved in the DB. "
               "Nothing has been sent yet.")
        apply_label = f"✅ Apply all {len(plans)} to eBay"
    await util._send_chunked(message, lines)
    markup = InlineKeyboardMarkup([[
        InlineKeyboardButton(apply_label, callback_data=f"rev:apply:{token}"),
        InlineKeyboardButton("✖ Cancel", callback_data=f"rev:cancel:{token}")]])
    await util._safe_reply(message, ask, reply_markup=markup)


async def _apply_one(plan: dict) -> dict:
    item_id = plan["item_id"]
    if not util._claim("revise", item_id):
        return {"ok": False, "lines": [f"⚠️ {item_id[:8]} is already being revised. Not changed."]}
    try:
        return await asyncio.to_thread(revise.apply_plan, plan, util._record_error)
    except Exception as e:
        util._record_error(f"revise ({item_id[:8]})", e)
        return {"ok": False, "lines": [f"❌ {item_id[:8]}: revise failed: {type(e).__name__}: "
                                       f"{str(e)[:200]}. Check the listing before trying again; see /errors."]}
    finally:
        util._release("revise", item_id)


async def revise_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    try:
        _, action, token = query.data.split(":", 2)
    except ValueError:
        return
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass
    if not is_owner(update.effective_user):
        await util._safe_reply(query.message, "⛔ /revise is owner-only.")
        return
    pending = state.revise.get(token)
    if pending is None or pending["user"] != update.effective_user.id:
        await util._safe_reply(query.message, "That preview has expired or isn't yours. "
                                              "Run /revise again. Nothing changed.")
        return
    state.revise.pop(token, None)   # single use
    if action == "cancel":
        await util._safe_reply(query.message, "Cancelled. Nothing changed.")
        return
    if action != "apply":
        return
    if time.time() - pending["ts"] > PREVIEW_TTL:
        await util._safe_reply(query.message, "That preview is over 30 minutes old. "
                                              "Run /revise again. Nothing changed.")
        return

    plans = pending["plans"]
    await util._safe_reply(query.message, "✍️ Sending the changes for "
                                          f"{', '.join(p['item_id'][:8] for p in plans)} to eBay...")
    # One at a time, each eBay first and then the DB. A failure stops only its
    # own listing: the others are independent and were previewed.
    results = [await _apply_one(plan) for plan in plans]
    lines = [line for r in results for line in r["lines"]]
    if len(plans) > 1:
        ok = sum(r["ok"] for r in results)
        lines.append(f"Done: {ok} of {len(plans)} revised."
                     + ("" if ok == len(plans) else " The others are as described above."))
    await util._send_chunked(query.message, lines)
