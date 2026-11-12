"""The old eBay store's sales: the owner sends old_store_sales.csv (made by
scripts/import_old_store.py) to the bot, which backs up the DB and then
replaces every old store row (pipeline/old_store.import_csv). /check uses
them as "old store" in level 3. Owner only. Nothing on eBay changes.
"""
import asyncio
from datetime import datetime, timezone

from telegram import Update
from telegram.ext import ContextTypes

import config
from bot import revise_cmds, util
from pipeline import old_store

IMPORT_DIR = config.DATA_DIR / "import"


def summary_lines(saved: dict) -> list[str]:
    years = config.CHECK_OLD_STORE_YEARS
    return [
        f"🏪 Old store sales imported: {saved['rows']} rows ({saved['units']} units)"
        + (f", replacing the {saved['replaced']} imported before" if saved["replaced"] else "") + ".",
        f"/check uses {saved['recent']} of them (periods in the last {years:g} years) as \"old store\" "
        "evidence when we have fewer than "
        f"{config.CHECK_OLD_STORE_MIN_SALES} sales of the brand and type.",
        f"DB backed up first: {saved['backup']}",
    ]


async def old_store_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if not revise_cmds.is_owner(update.effective_user):
        await util._safe_reply(message, "⛔ Importing the old store's sales is owner-only.")
        return
    doc = message.document
    if doc.file_size and doc.file_size > config.OLD_STORE_MAX_BYTES:
        await util._safe_reply(message, f"⚠️ That file is {doc.file_size / 1e6:.1f} MB; the old store CSV is "
                                        "under 1 MB. Nothing imported.")
        return
    IMPORT_DIR.mkdir(parents=True, exist_ok=True)
    # Our own name, never the sender's: the file name is not trusted.
    path = IMPORT_DIR / f"old-store-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}.csv"
    try:
        f = await context.bot.get_file(doc.file_id)
        await f.download_to_drive(custom_path=str(path))
    except Exception as e:
        util._record_error("old store download", e)
        await util._safe_reply(message, f"❌ Couldn't download the file: {type(e).__name__}. Nothing imported.")
        return
    await util._safe_reply(message, "⏳ Backing up the DB, then importing the old store's sales…")
    try:
        saved = await asyncio.to_thread(old_store.import_csv, path)
    except old_store.CsvError as e:
        await util._safe_reply(message, f"⚠️ That CSV isn't the old store's sales ({e}). Nothing imported.")
        return
    except Exception as e:
        util._record_error("old store import", e)
        await util._safe_reply(message, f"❌ Import failed: {type(e).__name__}: {str(e)[:200]}. "
                                        "Nothing was changed (the import is one transaction).")
        return
    await util._send_chunked(message, summary_lines(saved))
