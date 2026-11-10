"""eBay's listing quality report: the owner sends the xlsx (Seller Hub >
Performance > Listing quality report) to the bot, which parses and stores it
(pipeline/quality_report.py, db.save_quality_report). /diagnose uses the
newest one for its benchmarks and CATEGORY FIX. Owner only. Nothing on eBay
changes.
"""
import asyncio
from datetime import datetime, timezone

from telegram import Update
from telegram.ext import ContextTypes

import config
import db
from bot import revise_cmds, util
from pipeline import quality_report

REPORTS_DIR = config.DATA_DIR / "reports"


def summary_lines(report: dict, saved: dict, live_ids: set[str]) -> list[str]:
    date = report["report_date"]
    ours = sum(1 for x in report["listings"] if x.get("item_id") in live_ids)
    lines = [f"📊 eBay listing quality report of {date:%m/%d/%Y} saved"
             + (" (replaced the copy sent before)" if saved["replaced"] else "") + ": "
             f"{saved['sections']} categories, {saved['listings']} listings, {ours} of them live in our DB.", ""]
    for s in report["sections"]:
        n = sum(1 for x in report["listings"] if x["section"] == s["section"])
        line = f"• {s['section']}: {n} listing{'s' if n != 1 else ''}."
        if s.get("impressions_per_day") is not None and s.get("ctr") is not None:
            line += f" Us {s['impressions_per_day']:.0f} impressions/day, CTR {s['ctr']:.2%}"
            if s.get("top_ctr") is not None:
                line += f" (top 10% {s['top_ctr']:.2%})"
            line += "."
        for rec in s.get("recommendations") or []:
            line += f" eBay: {rec['text']}."
        lines.append(line)
    lines += ["", "/diagnose uses it now (for "
              f"{config.QUALITY_REPORT_MAX_AGE_DAYS} days)."]
    return lines


async def report_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if not revise_cmds.is_owner(update.effective_user):
        await util._safe_reply(message, "⛔ Sending eBay's listing quality report is owner-only.")
        return
    doc = message.document
    if doc.file_size and doc.file_size > config.QUALITY_REPORT_MAX_BYTES:
        await util._safe_reply(message, f"⚠️ That file is {doc.file_size / 1e6:.1f} MB; the report is "
                                        "well under 1 MB. Nothing saved.")
        return
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    # Our own name, never the sender's: the file name is not trusted.
    path = REPORTS_DIR / f"quality-report-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}.xlsx"
    try:
        f = await context.bot.get_file(doc.file_id)
        await f.download_to_drive(custom_path=str(path))
    except Exception as e:
        util._record_error("quality report download", e)
        await util._safe_reply(message, f"❌ Couldn't download the file: {type(e).__name__}. Nothing saved.")
        return
    try:
        report = await asyncio.to_thread(quality_report.parse, path)
    except quality_report.ReportError as e:
        await util._safe_reply(message, f"⚠️ That xlsx isn't eBay's listing quality report ({e}). "
                                        "Nothing saved.")
        return
    except Exception as e:
        util._record_error("quality report parse", e)
        await util._safe_reply(message, f"❌ Couldn't read the report: {type(e).__name__}: {str(e)[:200]}. "
                                        "Nothing saved.")
        return
    try:
        saved = await asyncio.to_thread(db.save_quality_report, report)
        live = await asyncio.to_thread(db.list_items)
    except Exception as e:
        util._record_error("quality report save", e)
        await util._safe_reply(message, f"❌ Couldn't save the report: {type(e).__name__}: {str(e)[:200]}.")
        return
    live_ids = {i["item_id"] for i in live if i.get("status") == "published"}
    await util._send_chunked(message, summary_lines(report, saved, live_ids))
