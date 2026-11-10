"""/diagnose: why a live listing isn't selling, and the one fix to try
(pipeline/diagnose.py). Owner only, read-only: fixes go through /revise,
/setprice and /offers.

    /diagnose <id>    one listing, with its numbers
    /diagnose all     every live listing, worst first
"""
import asyncio
import collections

from telegram import Update
from telegram.ext import ContextTypes

import config
from bot import revise_cmds, util
from pipeline import diagnose

USAGE = "Usage: /diagnose <id>  or  /diagnose all"


async def diagnose_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if not revise_cmds.is_owner(update.effective_user):
        await util._safe_reply(message, "⛔ /diagnose is owner-only.")
        return
    args = list(context.args or [])
    if len(args) != 1:
        await util._safe_reply(message, USAGE)
        return
    every = args[0].lower() == "all"
    item_ids = None
    if not every:
        item_id, err = util._resolve_item_id(update.effective_user.id, args)
        if err:
            await util._safe_reply(message, f"⚠️ {err}")
            return
        item_ids = [item_id]
    await util._safe_reply(message, "🩺 Reading eBay traffic, watchers and competitors"
                                    + (" for every live listing (a few minutes)..." if every else "..."))
    info: dict = {}
    try:
        results = await asyncio.to_thread(diagnose.diagnose, item_ids, info=info)
    except Exception as e:
        util._record_error("diagnose", e)
        await util._safe_reply(message, f"❌ /diagnose failed: {type(e).__name__}: {str(e)[:200]}")
        return
    if not results:
        await util._safe_reply(message, "Not a live listing." if not every else "No live listings.")
        return
    note = info.get("benchmarks")
    if not every:
        await util._send_chunked(message, diagnose.format_detail(results[0]) + (["", note] if note else []))
        return
    counts = collections.Counter(r["verdict"] for r in results)
    lines = [f"🩺 {len(results)} live listings, worst first. "
             + ", ".join(f"{diagnose.LABEL[c]} {counts[c]}" for c in diagnose.ORDER if counts[c])]
    if note:
        lines.append(note)
    lines.append("")
    # Category fixes are account-wide: one block below, not one line per listing.
    listed = [r for r in results if r["verdict"] not in ("OK", "NOT_ENOUGH_DATA", "CATEGORY_FIX")]
    shown = listed[:config.DIAGNOSE_SHOW_MAX]
    for r in shown:
        lines += [diagnose.format_line(r, also=False), ""]
    if len(listed) > len(shown):
        lines.append(f"(+{len(listed) - len(shown)} more; /diagnose <id> for any listing)")
    summary = diagnose.category_summary(results)
    if summary:
        lines += ["", f"{diagnose.ICON['CATEGORY_FIX']} eBay's category recommendations:"] + summary
    lines.append("Fixes: /revise (title, specifics, description), /setprice, /offers.")
    await util._send_chunked(message, lines)
