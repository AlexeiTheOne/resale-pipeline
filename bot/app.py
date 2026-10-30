"""Startup: allowlist configuration, the Application, handler registration, the
background loops and main().

Moved out of telegram_bot.py (docs/cleanup-plan.md Phase 2, step 5).
telegram_bot.py is the entry point (`python telegram_bot.py`) and calls main().
"""
import asyncio
import json
import os
import subprocess
import traceback
from datetime import datetime, timezone
from pathlib import Path

import httpx
from dotenv import load_dotenv
from telegram import BotCommand, Update
from telegram.error import BadRequest, NetworkError, TimedOut
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    TypeHandler,
    filters,
)
from telegram.request import HTTPXRequest

import config
import db
import logging_setup
from bot import (
    flow, intake, inventory_cmds, jobs, listing_cmds, ops_cmds, photos_cmds,
    sales_cmds, text_router, util,
)

_REPO_ENV = Path(__file__).resolve().parent.parent / ".env"


def configure() -> None:
    """Startup configuration, run by main() before the Application is built.

    Loads the repo's .env (a no-op for variables already set, and telegram_bot
    has normally loaded it already) and then parses the allowlist, so the
    allowlist can never be read before .env is.

    Whitelist of Telegram user IDs allowed to drive the bot. This gates a LIVE
    eBay seller account (publish / delete / ad-rate), so an open bot means anyone
    who finds the username can act on the account. When unset the bot stays open
    (so an existing install isn't bricked on upgrade) but prints a loud startup
    warning; use /whoami to get your id, then set TELEGRAM_ALLOWED_USER_IDS in
    .env. Lives on bot.util (read as util.ALLOWED_USER_IDS) because bot/jobs.py
    and /whoami read it too."""
    load_dotenv(_REPO_ENV)
    util.ALLOWED_USER_IDS = util._parse_allowed_ids(os.getenv("TELEGRAM_ALLOWED_USER_IDS"))
    if not util.ALLOWED_USER_IDS:
        print("⚠️ SECURITY: TELEGRAM_ALLOWED_USER_IDS is unset — the bot accepts "
              "commands from ANY Telegram user, on a live eBay account. Send /whoami "
              "to get your id, then set TELEGRAM_ALLOWED_USER_IDS=<id> in .env and restart.")


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
            await util._safe_reply(update.effective_message, "⛔ Not authorized to use this bot.")
        raise ApplicationHandlerStop


# The background loops (bot/jobs.py), by name. They are plain asyncio tasks, not
# Application.create_task: post_init runs before the Application is running, so
# create_task there warns that the task won't be awaited; and once it IS running,
# Application.stop() awaits every create_task task, which never finishes for a
# `while True` loop and would hang shutdown. _post_shutdown cancels them instead.
_loop_tasks: dict[str, asyncio.Task] = {}


async def _run_loop(app: Application, loop_fn) -> None:
    """Run one loop; an exception escaping it goes to the error handlers, as it
    did when these were started with Application.create_task."""
    try:
        await loop_fn(app)
    except Exception as e:
        await app.process_error(None, e)


def _start_loop(app: Application, loop_fn) -> None:
    """Start loop_fn(app) as a background task, once: a second call while it is
    still running does nothing."""
    name = loop_fn.__name__
    task = _loop_tasks.get(name)
    if task is not None and not task.done():
        return
    _loop_tasks[name] = asyncio.create_task(_run_loop(app, loop_fn), name=f"bot:{name}")


def _git_commit() -> str:
    """The commit the repo is checked out at, or "unknown" without git."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=config.BASE_DIR,
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return out.stdout.strip() if out.returncode == 0 and out.stdout.strip() else "unknown"


def write_heartbeat() -> Path:
    """Write logs/heartbeat.json: which commit started, when, and the process id.
    The deploy script reads it to confirm the new version came up. Written via
    a temp file + replace so a reader never sees a half-written file."""
    path = Path(config.LOG_DIR) / "heartbeat.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "commit": _git_commit(),
        "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "pid": os.getpid(),
    }
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    return path


async def _post_init(app: Application) -> None:
    """Register the command list with Telegram so typing '/' on the phone offers
    autocomplete. Built from HELP_SECTIONS so it stays in sync with /help.

    Also writes the startup heartbeat. post_init runs after the bot has logged
    in to Telegram, so the heartbeat means "started and connected"."""
    try:
        write_heartbeat()
    except OSError as e:
        print(f"WARNING: could not write heartbeat (non-fatal): {e}")
    # Nothing is in flight yet, so this removes a busy.json left by a crash.
    util.write_busy()
    commands = [
        BotCommand(cmd.lstrip("/").split()[0], desc[:256])
        for _, entries in ops_cmds.HELP_SECTIONS
        for cmd, desc in entries
        if cmd.startswith("/")
    ]
    try:
        await app.bot.set_my_commands(commands)
        print(f"Registered {len(commands)} commands with Telegram's autocomplete menu.")
    except Exception as e:
        print(f"WARNING: set_my_commands failed (non-fatal): {type(e).__name__}: {e}")

    if not config.JOBS_ENABLED:
        print("Background jobs OFF (JOBS_ENABLED=0): no price check, sold alerts, "
              "sync or feedback. Dev mode.")
        return

    if util.ALLOWED_USER_IDS:
        _start_loop(app, jobs._weekly_reprice_loop)
        print(f"Weekly price check scheduled: weekday={config.REPRICE_WEEKDAY} hour={config.REPRICE_HOUR_UTC} UTC.")
    else:
        print("⚠️ Weekly price check NOT scheduled — no TELEGRAM_ALLOWED_USER_IDS to notify. "
              "Use /pricecheck manually, or set the allowlist to enable it.")

    # The sync job is worth running even with no one to notify: its real product
    # is a DB that matches eBay, which every money figure reads. The digest is
    # just the by-product, so this is deliberately NOT inside the allowlist check
    # above.
    if config.SOLD_ALERTS_ENABLED:
        _start_loop(app, jobs._sold_alert_loop)
        print(f"Sold alerts every {config.SOLD_ALERT_MINUTES} min.")
    if config.SYNC_ENABLED:
        _start_loop(app, jobs._daily_sync_loop)
        print(f"Daily eBay sync scheduled: hour={config.SYNC_HOUR_UTC} UTC "
              f"(orders look-back {config.SYNC_DAYS}d, catch-up grace "
              f"{config.JOB_CATCHUP_GRACE_HOURS}h).")
        last = db.last_job_run(jobs._JOB_SYNC)
        print(f"   last completed sync: {last.isoformat() if last else 'never recorded'}")
    else:
        print("⚠️ Daily eBay sync DISABLED (SYNC_ENABLED=0) — sold orders will only "
              "reach the DB when you run /sync by hand, and every money figure "
              "understates itself until you do.")

    if config.FEEDBACK_ENABLED:
        _start_loop(app, jobs._daily_feedback_loop)
        print(f"Daily buyer feedback scheduled: hour={config.FEEDBACK_HOUR_UTC} UTC.")
    else:
        print("Buyer feedback automation disabled (FEEDBACK_ENABLED=0); use /feedback.")


async def _post_shutdown(app: Application) -> None:
    """Cancel the background loops started by _post_init."""
    tasks = [t for t in _loop_tasks.values() if not t.done()]
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    _loop_tasks.clear()


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
        util._record_error("unhandled", err)


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


def register_handlers(app: Application) -> None:
    """Every handler the bot answers to, in priority order."""
    app.add_error_handler(error_handler)
    # Auth gate first (group -1): blocks unauthorized users before any handler below.
    app.add_handler(TypeHandler(Update, _auth_guard), group=-1)
    app.add_handler(MessageHandler(filters.PHOTO, intake.photo_handler))
    app.add_handler(CallbackQueryHandler(flow.confirm_callback, pattern=r"^confirm:"))
    app.add_handler(CallbackQueryHandler(flow.review_callback, pattern=r"^review:"))
    app.add_handler(CallbackQueryHandler(listing_cmds.reprice_callback, pattern=r"^reprice:"))
    app.add_handler(CallbackQueryHandler(inventory_cmds.quick_add_callback, pattern=r"^qa:"))
    app.add_handler(CommandHandler("haul", intake.haul_command))
    app.add_handler(CommandHandler("find", inventory_cmds.find_command))
    app.add_handler(CommandHandler("done", inventory_cmds.done_command))
    app.add_handler(CommandHandler("stock", inventory_cmds.stock_command))
    app.add_handler(CommandHandler("list", inventory_cmds.list_command))
    app.add_handler(CommandHandler("inv", inventory_cmds.inv_command))
    app.add_handler(CommandHandler("where", inventory_cmds.where_command))
    app.add_handler(CommandHandler("move", inventory_cmds.move_command))
    app.add_handler(CommandHandler("mark", inventory_cmds.mark_command))
    app.add_handler(CommandHandler("ghosts", inventory_cmds.ghosts_command))
    app.add_handler(CommandHandler("status", ops_cmds.status_command))
    app.add_handler(CommandHandler("ross", sales_cmds.ross_command))
    app.add_handler(CommandHandler("errors", ops_cmds.errors_command))
    app.add_handler(CommandHandler("comps", ops_cmds.comps_command))
    app.add_handler(CommandHandler("sold", sales_cmds.sold_command))
    app.add_handler(CommandHandler("profit", sales_cmds.profit_command))
    app.add_handler(CommandHandler("report", sales_cmds.report_command))
    app.add_handler(CommandHandler("scoreboard", sales_cmds.scoreboard_command))
    app.add_handler(CommandHandler("listing", listing_cmds.listing_command))
    app.add_handler(CommandHandler("receipt", sales_cmds.receipt_command))
    app.add_handler(CommandHandler("addphotos", intake.addphotos_command))
    app.add_handler(CommandHandler("delete", listing_cmds.delete_command))
    app.add_handler(CommandHandler("notdup", listing_cmds.notdup_command))
    app.add_handler(CommandHandler("end", listing_cmds.end_command))
    app.add_handler(CommandHandler("refreshdesc", listing_cmds.refreshdesc_command))
    app.add_handler(CommandHandler("photos", photos_cmds.photos_command))
    app.add_handler(CommandHandler("cover", photos_cmds.cover_command))
    app.add_handler(CommandHandler("arrange", photos_cmds.arrange_command))
    app.add_handler(CommandHandler("stockphoto", photos_cmds.stockphoto_command))
    app.add_handler(CommandHandler("offers", listing_cmds.offers_command))
    app.add_handler(CommandHandler("setprice", listing_cmds.setprice_command))
    app.add_handler(CommandHandler("setqty", listing_cmds.setqty_command))
    app.add_handler(CommandHandler("sync", sales_cmds.sync_command))
    app.add_handler(CommandHandler("feedback", ops_cmds.feedback_command))
    app.add_handler(CommandHandler("activate", listing_cmds.activate_command))
    app.add_handler(CommandHandler("promote", listing_cmds.promote_command))
    app.add_handler(CommandHandler("pricecheck", listing_cmds.pricecheck_command))
    app.add_handler(CommandHandler("retry", listing_cmds.retry_command))
    app.add_handler(CommandHandler("photocheck", photos_cmds.photocheck_command))
    app.add_handler(CommandHandler("health", ops_cmds.health_command))
    app.add_handler(CommandHandler("usage", ops_cmds.usage_command))
    app.add_handler(CommandHandler("auth", ops_cmds.auth_command))
    app.add_handler(CommandHandler("whoami", ops_cmds.whoami_command))
    app.add_handler(CommandHandler("help", ops_cmds.help_command))
    app.add_handler(CommandHandler("cancel", ops_cmds.cancel_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_router.text_handler))


def build_app(token: str | None) -> Application:
    """The configured Application with every handler registered. Call
    configure() first."""
    app = (
        Application.builder()
        .token(token)
        .request(_ipv4_request(256))
        .get_updates_request(_ipv4_request(1))
        # Process updates concurrently so a running pipeline / eBay call doesn't
        # stall other actions (e.g. reviewing item B while item A is pricing).
        .concurrent_updates(True)
        # Publishes the command list to Telegram so "/" offers autocomplete, and
        # starts the background loops.
        .post_init(_post_init)
        .post_shutdown(_post_shutdown)
        .build()
    )
    register_handlers(app)
    return app


def main() -> None:
    logging_setup.setup_logging(config.LOG_DIR)
    configure()
    app = build_app(os.getenv("TELEGRAM_BOT_TOKEN"))
    app.run_polling()
