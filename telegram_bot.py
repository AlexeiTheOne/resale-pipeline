import os, sys, traceback
from pathlib import Path
from dotenv import load_dotenv
import httpx
from telegram import BotCommand, Update
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
    LOG_DIR,
    REPRICE_WEEKDAY, REPRICE_HOUR_UTC,
    SYNC_ENABLED, SYNC_HOUR_UTC, SYNC_DAYS, JOB_CATCHUP_GRACE_HOURS,
    SOLD_ALERTS_ENABLED, SOLD_ALERT_MINUTES,
    FEEDBACK_ENABLED, FEEDBACK_HOUR_UTC,
)
from db import last_job_run
from db import get_item, list_items, update_field, update_status  # noqa: F401  (re-export for tests)
from ebay.inventory import TitleTooLongError  # noqa: F401  (re-export for tests)

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
    create_item, REAL_CODE_FLOOR as _REAL_CODE_FLOOR,
    match_quantity_to_pieces as _match_quantity_to_pieces,
    next_receipt_code as _next_receipt_code, set_location as _set_location,
)

from bot import util
from bot.intake import (  # noqa: F401  (re-export)
    INBOX, DEFAULT_CAPTURE_WINDOW, EXTENDED_CAPTURE_WINDOW, WAIT_EXTENSION,
    finalize_capture, _is_ross_tag, _split_haul, _run_haul, _haul_digest, haul_command,
    _confirmation_markup, _send_confirmation, _await_more_photos, _finalize_append,
    addphotos_command, photo_handler,
)
from bot.inventory_cmds import (  # noqa: F401  (re-export)
    _FIND_DIR, find_command, done_command, _end_find, _find_one, _QUICK_ADD_WAIT_FIRST,
    _QUICK_ADD_WAIT_NEXT, quick_add_callback, _quick_add_photo, _quick_add_timer,
    _quick_add_item, _stock_line, stock_command, _PIECES_DIR, _inv_semaphore,
    inv_command, _inv_photo, _inv_process, _inv_done, where_command, move_command,
    mark_command, ghosts_command, list_command,
)
from bot.photos_cmds import (  # noqa: F401  (re-export)
    _resync_photos, _send_photo_index, photos_command, _apply_photo_order, _photo_args,
    cover_command, arrange_command, stockphoto_command, photocheck_command,
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
from bot.listing_cmds import (  # noqa: F401  (re-export)
    _MAX_SANE_QTY, _MAX_SANE_PRICE, listing_command, notdup_command, delete_command,
    _refresh_descriptions, refreshdesc_command, offers_command, end_command,
    setprice_command, setqty_command, activate_command, promote_command, _reprice_markup,
    _offers_markup, _offer_plan_lines, _VERDICT_LABEL, _VERDICT_ADVICE,
    _format_reprice_line, _format_reprice_short, _reprice_health_line,
    _send_reprice_digest, pricecheck_command, _send_offer_plan, reprice_callback,
    retry_command,
)
from bot.sales_cmds import (  # noqa: F401  (re-export)
    _ross_title, _ross_fields, ross_command, sold_command, _item_profit, profit_command,
    sync_command, _run_sync, _sync_days, report_command, _NO_CODE_WORDS, receipt_command,
    scoreboard_command,
)
from bot.ops_cmds import (  # noqa: F401  (re-export)
    auth_command, whoami_command, STATUS_DEFAULT_LIMIT, status_command, errors_command,
    comps_command, usage_command, feedback_command, health_command, cancel_command,
    HELP_SECTIONS, help_command,
)
from bot.jobs import (  # noqa: F401  (re-export)
    _sold_alert_loop, _JOB_REPRICE, _JOB_SYNC, _JOB_FEEDBACK, _previous_slot, _next_slot,
    _missed_slot, _run_reprice_job, _weekly_reprice_loop, _run_sync_job,
    _send_budget_warning, _daily_sync_loop, _feedback_report, _only_new_feedback_problems,
    _run_feedback_job, _daily_feedback_loop,
)

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")


# Whitelist of Telegram user IDs allowed to drive the bot. This gates a LIVE eBay
# seller account (publish / delete / ad-rate), so an open bot means anyone who
# finds the username can act on the account. When unset the bot stays open (so an
# existing install isn't bricked on upgrade) but prints a loud startup warning;
# use /whoami to get your id, then set TELEGRAM_ALLOWED_USER_IDS in .env.
# Lives on bot.util (read as util.ALLOWED_USER_IDS) because bot/jobs.py and
# /whoami read it too; parsed here, after load_dotenv().
util.ALLOWED_USER_IDS = _parse_allowed_ids(os.getenv("TELEGRAM_ALLOWED_USER_IDS"))


async def _auth_guard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Global gate, registered in handler group -1 so it runs before everything
    else. If TELEGRAM_ALLOWED_USER_IDS is set, only those users get through;
    anyone else is refused and ApplicationHandlerStop halts the rest of the
    handler chain. An unset allowlist leaves the bot open (legacy behavior)."""
    if not util.ALLOWED_USER_IDS:
        return
    user = update.effective_user
    if user is None or user.id not in util.ALLOWED_USER_IDS:
        uid = user.id if user else "unknown"
        print(f"⛔ Blocked update from unauthorized user {uid}")
        if update.callback_query is not None:
            await update.callback_query.answer("Not authorized.", show_alert=True)
        elif update.effective_message is not None:
            await _safe_reply(update.effective_message, "⛔ Not authorized to use this bot.")
        raise ApplicationHandlerStop


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

    if util.ALLOWED_USER_IDS:
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
    if not util.ALLOWED_USER_IDS:
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
