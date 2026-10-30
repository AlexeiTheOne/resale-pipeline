"""Operations: /status, /errors, /comps, /usage, /health, /cancel, /help,
/feedback, /auth and /whoami.

Moved verbatim out of telegram_bot.py (docs/cleanup-plan.md Phase 2, step 4b).
"""
import asyncio
import os
import shutil
import traceback
from collections import Counter
from pathlib import Path

from telegram import Update
from telegram.ext import ContextTypes

import config
import db
import llm_usage
from bot import inventory_cmds, jobs, state, util
from ebay import analytics, auth, feedback, inventory, listings, marketing, negotiation, orders


async def auth_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Re-consent the eBay seller account from the phone. /auth with no argument
    replies with the consent URL to open; /auth <pasted redirect URL or code>
    completes the exchange. Handy when the token loses a scope (e.g. sell.marketing
    was added) and needs re-granting without a laptop."""
    from ebay.auth import exchange_code, get_consent_url

    args = context.args
    if not args:
        await util._safe_reply(update.message,
            "🔑 eBay re-consent:\n"
            "1) Open this URL and approve (sign in as the seller):\n\n"
            f"{get_consent_url()}\n\n"
            "2) On the 'Authorization successfully completed' page, copy the WHOLE "
            "address-bar URL (it contains code=) and send it back as:\n"
            "/auth <paste the URL>")
        return

    await util._safe_reply(update.message, "🔑 Exchanging with eBay...")
    try:
        token = await asyncio.to_thread(exchange_code, " ".join(args))
    except Exception as e:
        traceback.print_exc()
        util._record_error("auth exchange", e)
        await util._safe_reply(update.message, f"⚠️ Exchange failed: {type(e).__name__}: {str(e)[:250]}")
        return
    await util._safe_reply(update.message,
        f"✅ eBay authorized — token stored (expires in {token.get('expires_in')}s). "
        "Run /health to confirm the scopes.")


async def whoami_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Report the caller's numeric Telegram id — the value to put in
    TELEGRAM_ALLOWED_USER_IDS. Works from the phone before the allowlist is set."""
    user = update.effective_user
    if user is None:
        return
    if not util.ALLOWED_USER_IDS:
        state = ("⚠️ open to everyone — add "
                 f"TELEGRAM_ALLOWED_USER_IDS={user.id} to .env and restart to lock it to you.")
    elif user.id in util.ALLOWED_USER_IDS:
        state = "restricted; you are on the allowlist."
    else:
        state = "restricted; you are NOT on the allowlist."
    await util._safe_reply(update.message, f"Your Telegram user id: {user.id}\nBot access: {state}")


# How many items /status lists by default. The full inventory is ~90 items and
# growing, which is several screens of scrolling to reach the ones you're actually
# working on. The per-status counts in the header still cover everything, so
# nothing is hidden — only the row list is trimmed.
STATUS_DEFAULT_LIMIT = 20


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """List items and their pipeline status, most recent last (nearest your
    keyboard). Shows the newest STATUS_DEFAULT_LIMIT by default; the header counts
    always cover everything.

      /status              the 20 most recent
      /status published    ...with that status
      /status 50           a different number
      /status all          every item, as before

    Paginated so it never exceeds Telegram's message-size limit."""
    all_items = db.list_items()
    if not all_items:
        await util._safe_reply(update.message, "No items yet.")
        return

    # Header: a count per status across everything, so you see the funnel at a glance.
    counts = Counter(i["status"] for i in all_items)
    ordered = db.VALID_STATUSES + sorted(s for s in counts if s not in db.VALID_STATUSES)
    header = "📊 " + " · ".join(f"{s}:{counts[s]}" for s in ordered if counts[s])

    # Args in any order: a status filter, a row count, or "all" for no limit.
    limit = STATUS_DEFAULT_LIMIT
    status_filter = None
    for arg in (a.lower() for a in (context.args or [])):
        if arg in ("all", "full"):
            limit = None
        elif arg.isdigit():
            limit = max(int(arg), 1)
        else:
            status_filter = arg

    if status_filter:
        items = [i for i in all_items if i["status"] == status_filter]
        if not items:
            await util._safe_reply(update.message, f"{header}\n\nNo items with status '{status_filter}'.")
            return
    else:
        items = all_items

    items.sort(key=lambda i: i["created_at"])
    lines = [header]
    if limit is not None and len(items) > limit:
        hidden = len(items) - limit
        items = items[-limit:]   # the tail: newest, and last on screen
        lines.append(f"Showing the {limit} most recent · {hidden} older hidden "
                     f"(/status all, or /status {min(len(all_items), 50)})")
    lines.append("")
    for item in items:
        listing = item.get("listing") or {}
        title = listing.get("title") or "(no listing yet)"
        lines.append(f"{item['item_id'][:8]} | {item['status']:10} | {title[:48]}")
    await util._send_chunked(update.message, lines)


async def errors_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show recent errors (the console isn't visible from the phone). Each entry is
    the time, where it happened, and the exception's final line; the full traceback
    still prints to the server console."""
    if not util._recent_errors:
        await util._safe_reply(update.message, "✅ No errors recorded since startup.")
        return
    lines = ["🐞 Recent errors (newest first):", ""]
    for ts, where, tb in reversed(util._recent_errors):
        last = tb.splitlines()[-1] if tb else ""
        lines.append(f"🕐 {ts} · {where}")
        lines.append(f"   {last[:250]}")
    await util._send_chunked(update.message, lines)


async def comps_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show the sold/active comps the price was built from — the evidence behind a
    suggested price, so you can sanity-check it before overriding."""
    user_id = update.effective_user.id
    item_id, err = util._resolve_item_id(user_id, context.args)
    if err:
        await util._safe_reply(update.message, f"⚠️ {err}")
        return
    state.last_item[user_id] = item_id

    item = db.get_item(item_id)
    pricing = (item or {}).get("pricing") or {}
    if not pricing:
        await util._safe_reply(update.message, f"No pricing data for {item_id[:8]} yet.")
        return

    lines = [f"💰 Comps for {item_id[:8]} — suggested ${pricing.get('suggested_price')} "
             f"({pricing.get('confidence')})"]
    if pricing.get("comp_warning"):
        lines.append(f"⚠️ {pricing['comp_warning']}")

    if pricing.get("engine") == "v2":
        lines.append("(v2: same-item comps only, prices include shipping)")
    sold = pricing.get("sold_comps") or []
    lines += ["", f"SOLD ({len(sold)}, median ${pricing.get('sold_median')}):"]
    lines += [f"  ${c.get('price')} · {(c.get('title') or '')[:48]}" for c in sold] or ["  (none)"]

    active = pricing.get("active_listings") or []
    lines += ["", f"ACTIVE ({len(active)}, floor ${pricing.get('active_floor')}):"]
    lines += [f"  ${a.get('price')} · {(a.get('title') or '')[:48]}" for a in active] or ["  (none)"]

    if pricing.get("research_resale"):
        lines += ["", f"Research resale estimate: ${pricing['research_resale']}"]
    if pricing.get("price_source_url"):
        lines.append(f"Anchor: {pricing['price_source_url']}")
    await util._send_chunked(update.message, lines)


async def usage_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/usage: this month's model spend by provider, model and stage."""
    lines = await asyncio.to_thread(llm_usage.usage_report)
    await util._send_chunked(update.message, lines)


async def feedback_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/feedback: leave positive feedback for every buyer still waiting on it,
    right now, instead of at the daily slot."""
    await util._safe_reply(update.message, "⭐ Leaving feedback for buyers...")
    try:
        result = await asyncio.to_thread(feedback.leave_all_pending, config.FEEDBACK_COMMENT)
    except Exception as e:
        util._record_error("feedback", e)
        await util._safe_reply(update.message, f"❌ Feedback run failed: {e}")
        return
    db.record_job_run(jobs._JOB_FEEDBACK, detail=f"{len(result['left'])} left (manual)")
    lines = jobs._feedback_report(result) or ["No buyers are waiting on feedback."]
    await util._send_chunked(update.message, lines)


async def health_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    lines = ["🏥 Health check:"]

    try:
        await asyncio.to_thread(auth.get_access_token)
        lines.append("✅ eBay user token: OK")
    except Exception as e:
        lines.append(f"❌ eBay user token: {e}")

    for policy_type in ("fulfillment", "return"):
        try:
            policy_id = await asyncio.to_thread(inventory.get_policy_id, policy_type)
            lines.append(f"✅ {policy_type.capitalize()} policy: {policy_id}")
        except Exception as e:
            lines.append(f"❌ {policy_type.capitalize()} policy: {e}")

    try:
        status = await asyncio.to_thread(marketing.marketing_status)
        lines.append(f"✅ Promoted Listings (sell.marketing): {status}")
    except Exception as e:
        lines.append(
            f"❌ Promoted Listings (sell.marketing): {str(e)[:150]}\n"
            "   → if this is a scope/403 error, re-run `python -m ebay.auth` to re-consent."
        )

    try:
        status = await asyncio.to_thread(analytics.traffic_status)
        lines.append(f"✅ Traffic analytics (sell.analytics.readonly): {status}")
    except Exception as e:
        lines.append(
            f"❌ Traffic analytics (sell.analytics.readonly): {str(e)[:150]}\n"
            "   → if this is a scope/403 error, re-run `python -m ebay.auth` to re-consent."
        )

    try:
        lines.append(await asyncio.to_thread(listings.listings_status))
    except Exception as e:
        lines.append(f"❌ Active listings (Trading API): {str(e)[:150]}")

    try:
        status = await asyncio.to_thread(orders.orders_status)
        lines.append(f"✅ Sold orders (sell.fulfillment.readonly): {status}")
    except Exception as e:
        lines.append(
            f"❌ Sold orders (sell.fulfillment.readonly): {str(e)[:150]}\n"
            "   → if this is a scope/403 error, re-run `python -m ebay.auth` to re-consent."
        )

    try:
        status = await asyncio.to_thread(orders.finances_status)
        lines.append(f"✅ Order fees (sell.finances): {status}")
    except Exception as e:
        lines.append(
            f"❌ Order fees (sell.finances): {str(e)[:150]}\n"
            "   → if this is a scope/403 error, re-run `python -m ebay.auth` to re-consent."
        )

    # /offers needs its own scope, and it's the one nothing else exercises — a
    # missing sell.negotiation consent shows up as "no eligible listings", which
    # reads as "nobody's watching anything" rather than as a broken permission.
    # negotiation_status was imported for this check and then never called.
    try:
        status = await asyncio.to_thread(negotiation.negotiation_status)
        lines.append(f"✅ Offers to watchers (sell.negotiation): {status}")
    except Exception as e:
        lines.append(
            f"❌ Offers to watchers (sell.negotiation): {str(e)[:150]}\n"
            "   → if this is a scope/403 error, re-run `python -m ebay.auth` to re-consent."
        )

    for name in ("CLOUDINARY_CLOUD_NAME", "CLOUDINARY_API_KEY", "CLOUDINARY_API_SECRET"):
        lines.append(f"✅ {name}: set" if os.getenv(name) else f"❌ {name}: missing")

    await util._safe_reply(update.message, "\n".join(lines))


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    async with util._lock_for(user_id):
        data = state.pending.pop(user_id, None)
        if data and data.get("task") is not None:
            data["task"].cancel()
        ap = state.appending.pop(user_id, None)
        if ap and ap.get("task") is not None:
            ap["task"].cancel()
        was_appending = state.awaiting_photos.pop(user_id, None) is not None
        # Photos already captured and sitting on a Start/Wait/Cancel prompt.
        # /cancel skipped this slot entirely, so it answered "No capture in
        # progress" while an item WAS staged — and the countdown task kept
        # running, starting the pipeline on photos the user had just cancelled.
        st = state.staging.pop(user_id, None)
        if st and st.get("task") is not None:
            st["task"].cancel()
    was_gated = state.gate.pop(user_id, None) is not None
    was_hauling = user_id in state.haul_mode
    state.haul_mode.discard(user_id)
    inventory_cmds._end_find(user_id)
    state.inv_box.pop(user_id, None)
    state.inv_session.pop(user_id, None)
    state.list_after_photos.pop(user_id, None)
    # Only drop the queue when a gate was actually what got cancelled. Popping it
    # unconditionally meant cancelling an unrelated photo capture silently threw
    # away every queued haul gate and said nothing about it.
    queued = len(state.gate_queue.pop(user_id, []) or []) if was_gated else 0

    if data:
        await util._safe_reply(update.message, f"🛑 Cancelled capture ({len(data['files'])} photo(s) discarded).")
    elif st:
        # Same treatment as the Cancel button on the prompt: drop the item and its
        # photos. Leaving the row behind would strand it at 'captured' forever,
        # counted by /status and reachable by nothing.
        staged_id = st["item_id"]
        item = db.get_item(staged_id)
        if item is not None:
            photos = item.get("photos") or []
            if photos:
                shutil.rmtree(Path(photos[0]).parent, ignore_errors=True)
            db.delete_item(staged_id)
        await util._safe_reply(
            update.message,
            f"🛑 Cancelled — {len(st.get('paths') or [])} photo(s) discarded before processing.")
    elif was_appending:
        await util._safe_reply(update.message, "🛑 Cancelled — stopped waiting for photos to add.")
    elif was_gated:
        extra = f" {queued} queued item(s) dropped too." if queued else ""
        await util._safe_reply(
            update.message,
            f"🛑 Cancelled — the item is paused; use /retry to resume, or /delete it.{extra}")
    elif was_hauling:
        await util._safe_reply(update.message, "🛑 Haul mode off.")
    else:
        await util._safe_reply(update.message, "No capture in progress.")


# Single source of truth for /help and for Telegram's "/" autocomplete menu, so
# the two can't drift apart from each other or from the handlers registered below.
HELP_SECTIONS = [
    ("Getting started", [
        ("(send photos)", "Start a new item. 1st photo = cover, 2nd = tag close-up, last = the Ross price tag"),
        ("/haul", "Multi-item mode: dump a whole Ross run, split on a dark photo between items"),
        ("wait", "Hold the photo-batching window open longer for a big batch"),
        ("confirm", "At a gate: accept the identification / price and continue"),
        ("(free text)", "At a gate: correct it (e.g. 'brand is Tommy Jeans, color navy'). At review: approve, reject, or a correction"),
        ("/cancel", "Discard photos being captured, or drop out of a gate"),
    ]),
    ("Items", [
        ("/status [status|n|all]", "List items and their stage — the 20 most recent by default. "
                                   "Filter (/status published), widen (/status 50, /status all)"),
        ("/ross [missing]", "Audit Ross code + paid price per item; /ross missing shows only unpriced ones"),
        ("/listing [id]", "Show the current draft for an item"),
        ("/comps [id]", "Show the sold/active comps the price was built from"),
        ("/addphotos [id]", "Attach more photos to an existing item"),
        ("/photos [id]", "Show the photos in listing order, numbered"),
        ("/cover [id] <n>", "Make photo n the gallery cover (the search thumbnail)"),
        ("/arrange [id] <order>", "Reorder photos, e.g. /arrange 3,1,2 — unlisted ones go last"),
        ("/stockphoto [id] <url>", "Add a retailer's product photo as #2 — for items sealed in packaging"),
        ("/receipt [id] <price> [code]", "Set the Ross cost by hand; code is optional (omit or 'none' to auto-fill)"),
    ]),
    ("Selling", [
        ("/setprice [id] <price>", "Set the price (35 -> $34.99); pushes to eBay if it already has an offer"),
        ("/setqty [id] <n>", "Set the quantity on eBay (once the item has an offer); default is 1"),
        ("/sync [days]", "Pull live price/quantity + real sold orders from eBay; auto-marks sold items with actual fees"),
        ("/feedback", "Leave positive feedback now for every paid buyer still waiting on it (also runs daily)"),
        ("/activate [id]", "Publish an eBay draft — makes it live"),
        ("/end [id]", "End a live listing (drops to a draft; /activate to relist)"),
        ("/refreshdesc [apply]", "Push the current description copy to listings already on eBay "
                                 "(dry run without 'apply')"),
        ("/promote [id] <pct>", "Set the Promoted Listings ad rate (2-100%)"),
        ("/pricecheck", "Check live listings against eBay traffic/watchers; suggests price cuts (also runs weekly)"),
        ("/offers [apply] [$]", "Send private discounts to people watching your listings. "
                                "Dry run without 'apply'; add a number for a flat $ off"),
        ("/retry [id]", "Re-run the current pipeline step for an item"),
        ("/delete [id]", "Delete the item, its photos, and its eBay offer"),
        ("/find [box]", "Photo an item: posted or not? Quick add what isn't. /done to stop"),
        ("/stock", "Items quick-added but not posted, best to list first"),
        ("/inv <box>", "Inventory mode: one photo per item, each gets a code (G3-07)"),
        ("/where <q>", "Find pieces by code, box, brand, text or UPC"),
        ("/move <code> <box>", "Piece moved to another box (code stays)"),
        ("/mark <code> <status>", "Mark a piece lot, donated, home or sold"),
        ("/ghosts", "Live listings vs what's actually at home"),
        ("/list <id>", "Post a quick-added item: full pricing, draft, review"),
        ("/notdup <id> <id>", "Two look-alikes are different items: stop the duplicate warning"),
    ]),
    ("Money", [
        ("/sold [id] [price]", "Mark an item sold and record the sale price; replies with profit"),
        ("/profit [id]", "Profit per sold item: paid → sold → fees → net → margin (real fees after "
                         "/sync). With an id: that one item's full breakdown"),
        ("/report", "Excel report: photos, prices, fees, and profit"),
        ("/scoreboard", "What to buy: sell-through, speed and cash tied up per brand and "
                        "category, with buy more / pause / stop"),
    ]),
    ("Admin", [
        ("/photocheck", "Verify every live listing still shows every photo we uploaded"),
        ("/health", "Check eBay token, business policies, ad scope, Cloudinary"),
        ("/usage", "This month's Claude/Gemini spend by model and stage, against the Claude budget"),
        ("/auth [url]", "Re-consent eBay: no argument prints the consent URL; paste the redirect URL to finish"),
        ("/whoami", "Your Telegram user id (for TELEGRAM_ALLOWED_USER_IDS)"),
        ("/errors", "Recent errors (the server console isn't visible from the phone)"),
        ("/help", "This list"),
    ]),
]


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """List every command and what it does."""
    lines = ["🤖 Ross Resale Bot — commands", ""]
    for title, entries in HELP_SECTIONS:
        lines.append(f"—— {title} ——")
        for cmd, desc in entries:
            lines.append(f"{cmd}")
            lines.append(f"    {desc}")
        lines.append("")
    lines.append("[id] = a full item id or a unique prefix (as shown by /status). "
                 "Leave it out to act on the most recent item.")
    await util._send_chunked(update.message, lines)
