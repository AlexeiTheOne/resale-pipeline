"""The pipeline flow: advance() through identify -> price -> draft, the
confirm gates and their queue, review approve/reject, and publish.

Moved verbatim out of telegram_bot.py (docs/cleanup-plan.md Phase 2, step 3).
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
import profit
from bot import formatting, intake, state, util
from ebay import inventory, marketing
from pipeline import stages


_pipeline_sem = None


def _pipeline_semaphore() -> asyncio.Semaphore:
    """Cap how many item pipelines run concurrently (MAX_CONCURRENT_LISTINGS).
    Created lazily so it binds to the running event loop; extra Starts queue on
    it rather than firing a swarm of parallel Gemini/Apify calls."""
    global _pipeline_sem
    if _pipeline_sem is None:
        _pipeline_sem = asyncio.Semaphore(config.MAX_CONCURRENT_LISTINGS)
    return _pipeline_sem


def _review_markup(item_id: str) -> InlineKeyboardMarkup:
    """Approve/Reject buttons carrying the item id, so a finished draft can be
    acted on unambiguously even when several are pending at once."""
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Approve", callback_data=f"review:approve:{item_id}"),
        InlineKeyboardButton("❌ Reject", callback_data=f"review:reject:{item_id}"),
    ]])


def _notifier(message):
    """A thread-safe progress pinger: worker-thread stages call it to post status
    back onto the bot's event loop (identify/pricing each take a minute or more)."""
    loop = asyncio.get_running_loop()

    def notify(text: str) -> None:
        asyncio.run_coroutine_threadsafe(util._safe_reply(message, text), loop)
    return notify


async def _run_stage(item_id, message, stage_fn, *args):
    """Run one blocking pipeline stage in a worker thread, holding the concurrency
    semaphore only for the duration of the actual work (so an item waiting at a
    confirm gate never ties up a slot). Returns (result, None) or (None, error)."""
    notify = _notifier(message)
    try:
        async with _pipeline_semaphore():
            result = await asyncio.to_thread(stage_fn, item_id, *args, notify)
        return result, None
    except Exception as e:
        print("PIPELINE ERROR:", traceback.format_exc())
        util._record_error(f"{stage_fn.__name__} ({item_id[:8]})", e)
        # The item stays at the status it failed from, so the Retry button just
        # re-runs advance() for it (same path as /retry).
        await util._safe_reply(message, f"⚠️ {stage_fn.__name__} failed: {type(e).__name__}: {str(e)[:200]}",
                          reply_markup=_retry_markup(item_id))
        return None, e


def _retry_markup(item_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🔁 Retry", callback_data=f"confirm:retry:{item_id}"),
    ]])


async def _show_review(user_id, item_id, message) -> None:
    """Show a finished draft with Approve/Reject and mark it this user's active
    review target, so a typed correction / approve applies to it. The per-draft
    buttons carry the item id explicitly, so approving is unambiguous regardless
    of how many drafts are pending."""
    item = db.get_item(item_id)
    state.review[user_id] = item_id
    state.last_item[user_id] = item_id
    result = {"identification": item["identification"], "pricing": item["pricing"],
              "listing": item["listing"], "listing_check": item.get("listing_check")}
    # Lead with the id: during a haul several drafts sit on screen together, and
    # a typed correction has to be aimed at one of them.
    body = f"📄 Draft {item_id[:8]}\n" + formatting.format_draft(result)
    sent = await util._safe_reply(message, body, parse_mode=None,
                             reply_markup=_review_markup(item_id))
    util._remember_target(sent, item_id)


async def _publish_and_report(item_id, message) -> None:
    """Publish an ebay_draft offer, report the live URL and what it stands to
    make, then auto-promote at the default ad rate if one is configured. Shared
    by /activate and advance()."""
    if not util._claim("publish", item_id):
        await util._safe_reply(message, f"⏳ {item_id[:8]} is already publishing.")
        return
    try:
        await _publish_and_report_claimed(item_id, message)
    finally:
        util._release("publish", item_id)


async def _publish_and_report_claimed(item_id, message) -> None:
    try:
        ebay_data = await asyncio.to_thread(_activate_item, item_id)
    except Exception as e:
        traceback.print_exc()
        await util._safe_reply(message, f"⚠️ Activate failed: {type(e).__name__}: {str(e)[:300]}")
        return

    # The profit picture rides along with the "it's live" message rather than
    # waiting for the next /report: the moment to find out an item nets $3 is
    # while you still remember the item, not at the end of the week. Best-effort
    # — a listing that IS live must never be reported as a failed publish because
    # the arithmetic tripped over a half-filled item.
    body = f"🎉 Published! {ebay_data['view_item_url']}"
    try:
        projection = profit.project(db.get_item(item_id))
        if projection is not None:
            body += "\n\n" + profit.format_projection(projection)
    except Exception as e:
        print("PROFIT PROJECTION FAILED:", traceback.format_exc())
        util._record_error(f"profit projection ({item_id[:8]})", e)
    await util._safe_reply(message, body)

    if config.EBAY_DEFAULT_AD_RATE_PCT and config.EBAY_DEFAULT_AD_RATE_PCT > 0:
        await _promote_item(item_id, config.EBAY_DEFAULT_AD_RATE_PCT, message)


def _identify_gate_reason(ident: dict) -> str | None:
    """Why this identification needs your eyes, or None if it can clear its gate
    unattended. A decoded UPC lowers the bar: it's hard evidence of exactly which
    product this is, worth more than the model's self-reported confidence."""
    if not config.AUTO_CONFIRM:
        return "auto-confirm is off"
    if not ident.get("brand") and not ident.get("product_name"):
        return "item is unconfirmed"
    if ident.get("condition_flags"):
        return f"condition flags: {', '.join(ident['condition_flags'])}"
    try:
        confidence = float(ident.get("confidence") or 0)
    except (TypeError, ValueError):
        return "confidence is unreadable"
    # Only a DECODED barcode lowers the bar. config.py promises "decoded off the
    # photos", but identify.py only overwrites `upc` when pyzbar actually read
    # one — otherwise the field holds whatever digits the vision model thought it
    # saw, which is the least reliable thing it produces. Letting that lower the
    # bar meant the weakest evidence on the item bought the loosest gate. Items
    # identified before upc_decoded existed have no flag, and absence correctly
    # reads as "not decoded" and leaves them at the higher bar.
    bar = (config.AUTO_CONFIRM_MIN_IDENT_CONFIDENCE_WITH_UPC
           if ident.get("upc") and ident.get("upc_decoded")
           else config.AUTO_CONFIRM_MIN_IDENT_CONFIDENCE)
    if confidence < bar:
        return f"confidence {confidence:.2f} is below {bar:.2f}"
    return None


def _price_gate_reason(pricing: dict) -> str | None:
    """Why this price needs your eyes, or None if it can clear its gate
    unattended. pipeline/price.py already decided this (needs_review); this only
    turns it into something worth reading."""
    if not config.AUTO_CONFIRM:
        return "auto-confirm is off"
    if pricing.get("suggested_price") is None:
        return "no comp-backed price"
    # Items priced before evidence tiers existed have no needs_review field, and
    # absence must not read as "safe to skip".
    if "needs_review" not in pricing:
        return "priced before evidence grading"
    if pricing.get("needs_review"):
        flags = pricing.get("review_flags") or []
        return ", ".join(flags) if flags else f"{pricing.get('evidence')} comp evidence"
    return None


async def advance(user_id, item_id, message, context) -> None:
    """Run automatic steps for an item, pausing at the first gate that genuinely
    needs you, then hand the gate slot to whatever else is waiting.

    This is the one place that knows 'what comes next' in the pipeline, shared by
    the capture Start button, the confirm gates, /retry and /haul.

    Gates it can clear on its own — a confident identification, a solid-evidence
    price — are cleared with a one-line note rather than a prompt, so a clean item
    runs photos -> draft without interruption and only the genuinely ambiguous
    ones ask. The review gate before anything reaches eBay is never skipped.
    The compute stages report their own progress/errors via _run_stage."""
    try:
        await _advance_one(user_id, item_id, message, context)
    finally:
        # Whether this item finished, gated, or blew up, the next queued item
        # gets its turn — a failure must not strand the rest of a haul.
        await _activate_next_gate(user_id, message)


async def _advance_one(user_id, item_id, message, context) -> None:
    while True:
        item = db.get_item(item_id)
        if item is None:
            await util._safe_reply(message, "That item no longer exists.")
            return
        status = item["status"]

        if status == "captured":
            ident, err = await _run_stage(
                item_id, message, stages.stage_identify, item.get("photos") or [])
            if err is not None:
                return
            reason = _identify_gate_reason(ident)
            if reason:
                await _show_identify_gate(user_id, item_id, message, reason)
                return
            name = ident.get("product_name") or ident.get("item_type") or "item"
            await util._safe_reply(
                message, f"✅ {ident.get('brand') or ''} {name} — identified, pricing...".strip())

        elif status == "identified":
            pricing, err = await _run_stage(item_id, message, stages.stage_price)
            if err is not None:
                return
            reason = _price_gate_reason(pricing)
            if reason:
                await _show_price_gate(user_id, item_id, message, reason)
                return
            await util._safe_reply(
                message,
                f"✅ ${pricing.get('suggested_price')} — {formatting._price_evidence_line(pricing)} "
                f"— writing the listing...")

        elif status == "priced":
            # An item can sit at 'priced' with no price: pricing found no usable
            # comps and refused to guess. Drafting anyway writes "$None" into the
            # listing. The gate's own confirm path guards this, but /retry comes
            # straight here and would sail past it.
            if (item.get("pricing") or {}).get("suggested_price") is None:
                await _show_price_gate(user_id, item_id, message, "no comp-backed price")
                return
            _, err = await _run_stage(item_id, message, stages.stage_draft)
            if err is None:
                await _show_review(user_id, item_id, message)
            return

        elif status in ("drafted", "review"):
            # Waiting on your Approve/Reject — re-show the draft to act on.
            await _show_review(user_id, item_id, message)
            return

        elif status == "approved":
            await _approve_item(item_id, message, context)
            return

        elif status == "ebay_draft":
            await _publish_and_report(item_id, message)
            return

        else:  # published, rejected
            await util._safe_reply(message, f"Nothing to advance — {item_id[:8]} is '{status}'.")
            return


def _gate_is_busy(user_id, item_id) -> bool:
    """Is another item already holding this user's gate slot? Re-showing the SAME
    item (after a correction) is never 'busy' — that's the active conversation."""
    active = state.gate.get(user_id)
    return active is not None and active["item_id"] != item_id


def _enqueue_gate(user_id, item_id, stage, reason) -> None:
    """Park a gate behind the active one, newest last. Re-queuing an item that's
    already waiting updates it in place rather than asking about it twice."""
    queue = state.gate_queue.setdefault(user_id, [])
    for entry in queue:
        if entry["item_id"] == item_id:
            entry.update(stage=stage, reason=reason)
            return
    queue.append({"item_id": item_id, "stage": stage, "reason": reason})


async def _activate_next_gate(user_id, message) -> None:
    """Show the next queued gate, if the slot is free and anything is waiting.
    Called after every advance() so a finished item hands the floor to the next
    one instead of leaving a haul silently stalled."""
    if state.gate.get(user_id) is not None:
        return
    queue = state.gate_queue.get(user_id) or []
    while queue:
        entry = queue.pop(0)
        if db.get_item(entry["item_id"]) is None:
            continue  # deleted while it waited
        # Claim the slot BEFORE the first await. Several items of a haul can
        # finish in the same tick, and without claiming synchronously they'd each
        # see a free slot, pop a different entry, and all but the last would be
        # dropped on the floor.
        state.gate[user_id] = {"item_id": entry["item_id"], "stage": entry["stage"]}
        remaining = len(queue)
        suffix = f" ({remaining} more waiting)" if remaining else ""
        reason = (entry["reason"] or "") + suffix
        if entry["stage"] == "identify":
            await _show_identify_gate(user_id, entry["item_id"], message, reason)
        else:
            await _show_price_gate(user_id, entry["item_id"], message, reason)
        return


async def _show_identify_gate(user_id, item_id, message, reason=None) -> None:
    """Pause after identification: show what was identified and wait for the user
    to confirm or type a correction (handled in text_handler / _handle_gate).
    `reason` says why this one couldn't clear itself — with auto-confirm on, a
    gate that appears at all is a gate worth reading."""
    if _gate_is_busy(user_id, item_id):
        _enqueue_gate(user_id, item_id, "identify", reason)
        return
    item = db.get_item(item_id)
    state.gate[user_id] = {"item_id": item_id, "stage": "identify"}
    state.review.pop(user_id, None)
    state.last_item[user_id] = item_id
    body = f"🔎 {item_id[:8]}\n" + formatting.format_identification(item["identification"])
    if reason:
        body += f"\n\n🛑 Needs you: {reason}"
    sent = await util._safe_reply(
        message,
        body + "\n\nType 'confirm' to price it, or tell me what to fix "
               "(e.g. 'brand is Tommy Jeans, color navy').",
    )
    util._remember_target(sent, item_id)


async def _show_price_gate(user_id, item_id, message, reason=None) -> None:
    """Pause after pricing: show the suggested price and wait for confirm or a
    manual price (a whole number is charm-priced, e.g. 35 -> $34.99). `reason`
    says why this one couldn't clear itself."""
    if _gate_is_busy(user_id, item_id):
        _enqueue_gate(user_id, item_id, "price", reason)
        return
    item = db.get_item(item_id)
    state.gate[user_id] = {"item_id": item_id, "stage": "price"}
    state.review.pop(user_id, None)
    state.last_item[user_id] = item_id
    title = ((item.get("identification") or {}).get("product_name")
             or (item.get("identification") or {}).get("item_type") or "")
    body = f"💲 {item_id[:8]} {title}".rstrip() + "\n" + formatting.format_pricing(item["pricing"])
    if reason:
        body += f"\n\n🛑 Needs you: {reason}"
    sent = await util._safe_reply(
        message,
        body + "\n\nType 'confirm' to write the listing, or type a price to set it "
               "(e.g. 35 -> $34.99).",
    )
    util._remember_target(sent, item_id)


async def confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    try:
        _, action, item_id = query.data.split(":", 2)
    except ValueError:
        return

    if action == "start":
        # Identity is checked BEFORE the pop, and inside the lock. Popping first
        # destroyed whatever was staged and only then noticed the button belonged
        # to a different item — so tapping Start on a scrolled-back message
        # cancelled the timer on the item currently waiting and left it stranded
        # with no button and no countdown. Same reasoning in the "cancel" branch.
        async with util._lock_for(user_id):
            st = state.staging.get(user_id)
            if st is None or st["item_id"] != item_id:
                await query.edit_message_text("This item is no longer waiting.")
                return
            state.staging.pop(user_id, None)
            if st["task"] is not None:
                st["task"].cancel()
        paths = st["paths"]
        await query.edit_message_text(f"Starting. {len(paths)} photo(s). Processing...")
        # Launch as a background task so this handler returns immediately and
        # another item can be started while this one is still processing. The item
        # is 'captured', so advance() runs identify and pauses at its confirm gate.
        context.application.create_task(
            advance(user_id, item_id, query.message, context)
        )

    elif action == "wait":
        async with util._lock_for(user_id):
            st = state.staging.get(user_id)
            if st is None or st["item_id"] != item_id:
                await query.edit_message_text("This item is no longer waiting.")
                return
            if st["task"] is not None:
                st["task"].cancel()
            st["task"] = context.application.create_task(
                intake._await_more_photos(user_id, item_id, context, intake.WAIT_EXTENSION)
            )
        await query.edit_message_text(f"Waiting {intake.WAIT_EXTENSION}s for more photos. Send them now.")

    elif action == "cancel":
        # Only clear the staging slot if it's the item this button names. The
        # unconditional pop discarded the item currently waiting while deleting a
        # different one — losing both.
        async with util._lock_for(user_id):
            st = state.staging.get(user_id)
            if st is not None and st["item_id"] == item_id:
                state.staging.pop(user_id, None)
                if st["task"] is not None:
                    st["task"].cancel()
        item = db.get_item(item_id)
        if item is not None:
            photos = item.get("photos") or []
            if photos:
                shutil.rmtree(Path(photos[0]).parent, ignore_errors=True)
            db.delete_item(item_id)
        await query.edit_message_text("Cancelled. Photos discarded.")

    elif action == "retry":
        # The 🔁 Retry button on a failed stage (_run_stage). Claimed before the
        # first await so a double tap doesn't run the stage twice.
        if not util._claim("retry", item_id):
            return
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass

        async def _retry_then_release():
            try:
                await _retry_item(user_id, item_id, query.message, context)
            finally:
                util._release("retry", item_id)
        # Background, like Start: a stage takes a minute or more.
        context.application.create_task(_retry_then_release())


async def _approve_item(item_id: str, message, context) -> None:
    """Mark an item approved and push it to eBay as a draft offer. Shared by the
    typed 'approve' and the per-draft Approve button. Reports progress/errors to
    `message`; leaves the item at 'approved' (retryable) on any eBay failure."""
    db.update_status(item_id, "approved")
    await util._safe_reply(message, "✅ Approved. Creating eBay draft...")

    try:
        ebay_result = await asyncio.to_thread(inventory.create_draft_offer, item_id)
    except inventory.MissingRequiredAspectsError as e:
        lines = [
            f"⚠️ Can't create the draft yet — eBay category {e.category_id} requires these "
            "item specifics and I couldn't safely infer a value:"
        ]
        for m in e.missing:
            opts = f" (allowed: {', '.join(m['allowed_values'])})" if m["allowed_values"] else ""
            lines.append(f"  • {m['name']}{opts}")
        lines.append("The item is saved as 'approved' — fix the listing and retry manually.")
        await util._safe_reply(message, "\n".join(lines))
        return
    except inventory.TitleTooLongError as e:
        # Back to review, not left at 'approved': a title is fixed with a typed
        # correction, and corrections only apply to a draft under review.
        db.update_status(item_id, "review")
        await util._safe_reply(message, formatting._title_too_long_text(e, item_id))
        return
    except Exception as e:
        traceback.print_exc()
        await util._safe_reply(message,
            f"⚠️ Approved, but eBay draft creation failed: {type(e).__name__}: {str(e)[:300]}\n"
            "The item is saved as 'approved' — fix the issue and retry manually."
        )
        return

    db.update_field(item_id, "ebay", ebay_result)
    db.update_status(item_id, "ebay_draft")

    msg = f"📝 Draft created on eBay (SKU {ebay_result['sku']}, offer {ebay_result['offer_id']})."
    msg += "\nUse /listing to review it and /activate to publish it when ready — it may not show up in Seller Hub's Drafts UI (API-created offers often don't)."
    if ebay_result.get("reselected_from"):
        def _label(name, cid):
            return f"{name} ({cid})" if name else str(cid)
        new_label = _label(ebay_result.get("category_name"), ebay_result["category_id"])
        old_label = _label(ebay_result.get("reselected_from_name"), ebay_result["reselected_from"])
        msg += f"\n⚠️ Listed under category {new_label} (auto-corrected from {old_label})"
    await util._safe_reply(message, msg)


async def review_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle the per-draft Approve/Reject buttons. The item id rides in the
    callback data, so this works no matter how many drafts are pending."""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    try:
        _, action, item_id = query.data.split(":", 2)
    except ValueError:
        return
    if action not in ("approve", "reject"):
        return  # unknown button: already answered, change nothing

    item = db.get_item(item_id)
    if item is None:
        await query.edit_message_text("This item no longer exists.")
        return
    if item["status"] not in ("review", "drafted"):
        await query.edit_message_text(f"Item {item_id[:8]} is '{item['status']}' — nothing to review.")
        return
    # Claimed right after the status check, before any await, so a concurrent
    # second tap can't also pass the check while this one is still working.
    if not util._claim("review", item_id):
        await util._safe_reply(query.message, f"⏳ Already handling {item_id[:8]}.")
        return
    try:
        # Drop the buttons so the draft can't be double-actioned.
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass

        if state.review.get(user_id) == item_id:
            state.review.pop(user_id, None)
        state.last_item[user_id] = item_id

        if action == "approve":
            await _approve_item(item_id, query.message, context)
        else:
            db.update_status(item_id, "rejected")
            await util._safe_reply(query.message, f"❌ Rejected {item_id[:8]}.")
    finally:
        util._release("review", item_id)


def _activate_item(item_id: str) -> dict:
    item = db.get_item(item_id)
    offer_id = (item.get("ebay") or {}).get("offer_id")
    if not offer_id:
        raise ValueError("No offer_id stored for this item yet — create the eBay draft first.")

    listing_id = inventory.publish_offer(offer_id)
    ebay_data = dict(item.get("ebay") or {})
    ebay_data["listing_id"] = listing_id
    ebay_data["view_item_url"] = f"https://www.ebay.com/itm/{listing_id}"
    # A relist (end -> activate again) gets a new eBay listingId and starts
    # accumulating fresh traffic, so always stamp "now" rather than keeping an
    # old value — pipeline/reprice.py uses this to compute weeks-live.
    ebay_data["published_at"] = datetime.now(timezone.utc).isoformat()
    db.update_field(item_id, "ebay", ebay_data)
    db.update_status(item_id, "published")
    return ebay_data


async def _promote_item(item_id: str, pct, message) -> None:
    """Add/adjust the Promoted Listings ad rate for an item's SKU."""
    item = db.get_item(item_id)
    sku = (item.get("ebay") or {}).get("sku") if item else None
    if not sku:
        await util._safe_reply(message, "No eBay SKU for this item yet — create the draft first.")
        return
    try:
        result = await asyncio.to_thread(marketing.promote_listing, sku, pct)
    except Exception as e:
        traceback.print_exc()
        await util._safe_reply(message, f"⚠️ Could not set the ad rate: {type(e).__name__}: {str(e)[:250]}")
        return
    verb = "Updated" if result["action"] == "updated" else "Set"
    await util._safe_reply(message, f"📣 {verb} ad rate to {result['bid_percentage']}% for {item_id[:8]} (Promoted Listings).")


async def _retry_item(user_id, item_id, message, context) -> None:
    """/retry and the 🔁 Retry button on a failed stage."""
    state.last_item[user_id] = item_id

    item = db.get_item(item_id)
    if item is None:
        await util._safe_reply(message, f"No item found for {item_id[:8]}.")
        return

    await util._safe_reply(message, f"🔄 Retrying {item_id[:8]} (status: {item['status']})...")
    # Route through the same dispatcher the happy path uses, so a retried item
    # stops at the identify/price confirm gates instead of blowing past them.
    await advance(user_id, item_id, message, context)
