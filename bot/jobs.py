"""Scheduled work: the weekly price check, daily eBay sync, daily buyer
feedback and the sold-order alert poll, with their catch-up logic.

Moved verbatim out of telegram_bot.py (docs/cleanup-plan.md Phase 2, step 4b).
"""
import asyncio
import traceback
from datetime import datetime, timedelta, timezone

from telegram.ext import Application

import config
import db
import llm_usage
from bot import listing_cmds, sales_cmds, util
from ebay import feedback
from pipeline import reprice


async def _sold_alert_loop(app: Application) -> None:
    """Every SOLD_ALERT_MINUTES: announce new orders with where to pull them."""
    from db import mark_orders_seen, seen_order_ids
    from ebay.orders import get_orders
    from pipeline.inventory import order_alerts
    first = True
    while True:
        try:
            orders = await asyncio.to_thread(get_orders, 3 if not first else 30)
            seen = seen_order_ids()
            if first and not seen:
                # First run ever: everything before today is history, not news.
                mark_orders_seen([o.get("orderId") for o in orders if o.get("orderId")])
            else:
                lines, handled = await asyncio.to_thread(order_alerts, orders, seen)
                mark_orders_seen(handled)
                for uid in util.ALLOWED_USER_IDS:
                    for line in lines:
                        try:
                            await app.bot.send_message(uid, util._scrub(line))
                        except Exception as e:
                            print(f"WARNING: sold alert to {uid} failed: {e}")
        except Exception as e:
            print(f"WARNING: sold-alert poll failed: {type(e).__name__}: {e}")
        first = False
        await asyncio.sleep(config.SOLD_ALERT_MINUTES * 60)


# Names under which the scheduled jobs stamp db.job_runs. Changing one resets
# that job's catch-up history, so don't.
_JOB_REPRICE = "weekly_reprice"
_JOB_SYNC = "daily_sync"
_JOB_FEEDBACK = "daily_feedback"


def _previous_slot(now: datetime, hour_utc: int, weekday: int | None = None) -> datetime:
    """The most recent scheduled slot at or before `now`. `weekday` None = daily."""
    if weekday is None:
        slot = now.replace(hour=hour_utc, minute=0, second=0, microsecond=0)
        return slot if slot <= now else slot - timedelta(days=1)
    days_back = (now.weekday() - weekday) % 7
    slot = (now - timedelta(days=days_back)).replace(
        hour=hour_utc, minute=0, second=0, microsecond=0)
    return slot if slot <= now else slot - timedelta(days=7)


def _next_slot(now: datetime, hour_utc: int, weekday: int | None = None) -> datetime:
    """The next scheduled slot strictly after `now`. `weekday` None = daily."""
    period = timedelta(days=1 if weekday is None else 7)
    return _previous_slot(now, hour_utc, weekday) + period


def _missed_slot(job: str, now: datetime, hour_utc: int,
                 weekday: int | None = None) -> bool:
    """Whether `job` should run a catch-up right now, because it never ran for
    the slot that has already passed.

    This is the whole point of persisting job_runs. The loops used to compute
    their next slot from `now` alone, so a process that wasn't alive at the slot
    (laptop asleep, Windows Update reboot) skipped that run silently and waited a
    full period — which is why the 2026-08-03 weekly digest simply never
    happened and nothing recorded that it hadn't.

    Bounded by JOB_CATCHUP_GRACE_HOURS: a run that is days late is stale news,
    and firing it at startup would be noise rather than a recovery."""
    previous = _previous_slot(now, hour_utc, weekday)
    if now - previous > timedelta(hours=config.JOB_CATCHUP_GRACE_HOURS):
        return False
    last = db.last_job_run(job)
    return last is None or last < previous


async def _run_reprice_job(app: Application, *, catch_up: bool = False) -> None:
    """One weekly price check + digest. Only stamps job_runs "ok" on a complete
    run, so a run that failed is retried at the next start rather than being
    counted as done.

    A run where any traffic batch failed is stamped "partial", which
    last_job_run doesn't count as completed — so it is caught up like a missed
    slot. It used to be stamped ok: the 2026-10-05 run read 97 listings as
    INVISIBLE off a failed fetch and the scheduler considered the week served."""
    previous_status = db.job_run_status(_JOB_REPRICE)
    print(f"📈 Running {'catch-up' if catch_up else 'scheduled'} weekly price check...")
    try:
        results, health = await asyncio.to_thread(reprice.run_weekly_check)
    except Exception as e:
        traceback.print_exc()
        util._record_error("weekly reprice", e)
        return
    status = "partial" if health.get("traffic_failed") else "ok"
    db.record_job_run(_JOB_REPRICE, status=status,
                   detail=f"{len(results)} listing(s); {listing_cmds._reprice_health_line(health)}")
    if status != "ok":
        print(f"   price check incomplete: {listing_cmds._reprice_health_line(health)}")
    if not results:
        return
    incomplete_note = (f"Recorded as incomplete — it re-runs automatically at the next "
                       f"bot start within {config.JOB_CATCHUP_GRACE_HOURS}h of the slot, "
                       f"or run /pricecheck later.")
    for uid in util.ALLOWED_USER_IDS:
        if catch_up:
            notice = ("⏰ Retrying the price check: the last run couldn't load eBay "
                      "traffic for some listings." if previous_status == "partial" else
                      "⏰ Catch-up: last week's price check never ran (the bot "
                      "wasn't up at its slot). Running it now.")
            try:
                await app.bot.send_message(chat_id=uid, text=notice)
            except Exception as e:
                print(f"WARNING: catch-up notice failed: {type(e).__name__}: {e}")
        await listing_cmds._send_reprice_digest(app.bot, uid, results, health,
                                   incomplete_note=incomplete_note)


async def _weekly_reprice_loop(app: Application) -> None:
    """Runs for the lifetime of the bot: catches up on a missed slot, then sleeps
    until the next configured weekly slot (REPRICE_WEEKDAY/REPRICE_HOUR_UTC),
    runs the price check, and posts the digest to every allowed user. A manual
    /pricecheck doesn't reset this clock."""
    if _missed_slot(_JOB_REPRICE, datetime.now(timezone.utc),
                    config.REPRICE_HOUR_UTC, config.REPRICE_WEEKDAY):
        await _run_reprice_job(app, catch_up=True)
    while True:
        now = datetime.now(timezone.utc)
        target = _next_slot(now, config.REPRICE_HOUR_UTC, config.REPRICE_WEEKDAY)
        await asyncio.sleep((target - now).total_seconds())
        await _run_reprice_job(app)


async def _run_sync_job(app: Application, *, catch_up: bool = False) -> None:
    """One scheduled reconciliation with eBay, using the same _run_sync the
    manual /sync calls. Posts the report only when it actually says something."""
    print(f"🔄 Running {'catch-up' if catch_up else 'scheduled'} eBay sync...")
    try:
        lines, brief = await sales_cmds._run_sync(config.SYNC_DAYS)
    except Exception as e:
        traceback.print_exc()
        util._record_error("scheduled sync", e)
        return
    db.record_job_run(_JOB_SYNC, detail=f"{len(lines)} report line(s)")
    await _send_budget_warning(app)

    # Bookkeeping (price/qty corrections, status fixes) happens silently; the
    # phone only hears about new sales and things that need a decision.
    if config.SOLD_ALERTS_ENABLED:
        # The 15-minute sold alert already announced each sale with its box.
        brief = [l for l in brief if not l.startswith("💰 Sold")]
    if not brief:
        print("   sync: nothing new to report, staying quiet.")
        return
    for uid in util.ALLOWED_USER_IDS:
        try:
            await util._send_chunked_to(app.bot, uid, brief)
        except Exception as e:
            print(f"WARNING: sync digest to {uid} failed: {type(e).__name__}: {e}")


async def _send_budget_warning(app: Application) -> None:
    """Pass on the monthly Claude-spend warning, which llm_usage hands out at
    most once per threshold per month. Rides on the daily sync so it needs no
    timer of its own."""
    try:
        text = await asyncio.to_thread(llm_usage.budget_warning)
    except Exception as e:
        print(f"WARNING: budget check failed: {type(e).__name__}: {e}")
        return
    if not text:
        return
    print(text)
    for uid in util.ALLOWED_USER_IDS:
        try:
            await app.bot.send_message(chat_id=uid, text=text)
        except Exception as e:
            print(f"WARNING: budget warning to {uid} failed: {type(e).__name__}: {e}")


async def _daily_sync_loop(app: Application) -> None:
    """Runs for the lifetime of the bot: keeps the local DB reconciled with eBay
    once a day, so sold orders land in the DB without anyone remembering to type
    /sync.

    Before this existed /sync was manual-only, and every money figure built on
    the DB (/profit, /report, COGS) quietly understated itself for as long as a
    sync was overdue — the DB sat 10 days stale on 2026-09-03, missing 8 August
    orders and putting that month's COGS 35% low."""
    if _missed_slot(_JOB_SYNC, datetime.now(timezone.utc), config.SYNC_HOUR_UTC):
        await _run_sync_job(app, catch_up=True)
    while True:
        now = datetime.now(timezone.utc)
        target = _next_slot(now, config.SYNC_HOUR_UTC)
        await asyncio.sleep((target - now).total_seconds())
        await _run_sync_job(app)


def _feedback_report(result: dict) -> list[str]:
    """Report lines for a feedback run. Empty when there was nothing to do."""
    left, skipped, errors = result["left"], result["skipped"], result["errors"]
    if not (left or skipped or errors):
        return []
    lines = [f"⭐ Left positive feedback for {len(left)} buyer(s)."] if left else []
    for e in left:
        lines.append(f"  {e['buyer']}: {e['title'][:50]}")
    if skipped:
        lines += ([""] if lines else []) + [f"Skipped ({len(skipped)}):"] + [
            f"  {e['item_id']} {e['title'][:40]}: {why}" for e, why in skipped]
    if errors:
        lines += ([""] if lines else []) + [
            f"⚠️ Couldn't leave feedback ({len(errors)}) — eBay refused:"] + [
            f"  {e['item_id']} {e['title'][:40]}: {msg}" for e, msg in errors]
    return lines


def _only_new_feedback_problems(result: dict) -> dict:
    """Drop failures and skips already reported on an earlier run. eBay keeps
    listing a sale it won't take feedback on until the 60-day window closes, so
    without this the same 13 errors were posted every single day. Each one is
    still retried every run; it's just reported once. /feedback shows them all."""
    seen = db.known_feedback_failures()
    for e in result["left"]:
        if e.get("order_line") in seen:
            db.clear_feedback_failure(e["order_line"])
    fresh = {"left": result["left"], "skipped": [], "errors": []}
    for kind in ("skipped", "errors"):
        for e, why in result[kind]:
            key = e.get("order_line") or f"{e['item_id']}-{e['transaction_id']}"
            if key not in seen:
                fresh[kind].append((e, why))
            db.record_feedback_failure(key, str(why)[:200])
    return fresh


async def _run_feedback_job(app: Application) -> None:
    """One automatic feedback run. Posts only when it left (or failed to leave)
    something. Stamped only on success, so a failed run retries at next start."""
    print("⭐ Running scheduled buyer feedback...")
    try:
        result = await asyncio.to_thread(feedback.leave_all_pending, config.FEEDBACK_COMMENT)
    except Exception as e:
        traceback.print_exc()
        util._record_error("scheduled feedback", e)
        return
    db.record_job_run(_JOB_FEEDBACK, detail=f"{len(result['left'])} left")
    lines = _feedback_report(_only_new_feedback_problems(result))
    if not lines:
        return
    for uid in util.ALLOWED_USER_IDS:
        await util._send_chunked_to(app.bot, uid, lines)


async def _daily_feedback_loop(app: Application) -> None:
    """Runs for the lifetime of the bot: leaves positive feedback for new buyers
    once a day, catching up at startup if the slot was missed."""
    if _missed_slot(_JOB_FEEDBACK, datetime.now(timezone.utc), config.FEEDBACK_HOUR_UTC):
        await _run_feedback_job(app)
    while True:
        now = datetime.now(timezone.utc)
        target = _next_slot(now, config.FEEDBACK_HOUR_UTC)
        await asyncio.sleep((target - now).total_seconds())
        await _run_feedback_job(app)
