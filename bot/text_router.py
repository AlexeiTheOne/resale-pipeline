"""Typed messages: the gate / review text router and price parsing.

Moved verbatim out of telegram_bot.py (docs/cleanup-plan.md Phase 2, step 3).
"""
import asyncio
import re
from datetime import datetime, timezone

from telegram import Update
from telegram.ext import ContextTypes

import config
import db
import profit
from bot import flow, intake, state, util
from pipeline import draft, listing_check


# A typed correction that is really a price instruction: "159.99", "price 159.99",
# "make it $159.99", "sell for 160". Deliberately narrow — it must not fire on
# ordinary copy edits that happen to contain a number ("size 10 not 8", "2 front
# pockets"), so a price word or a currency symbol is required unless the whole
# message is just the number.
_PRICE_CORRECTION = re.compile(
    r"^(?:(?:the\s+)?price\s*(?:should\s*be|is|to|=)?|set\s+(?:the\s+)?price\s*(?:to)?|"
    r"make\s+it|change\s+(?:it|the\s+price)\s*(?:to)?|sell\s+(?:it\s+)?for|list\s+(?:it\s+)?at)?"
    r"\s*\$?\s*(\d{1,5}(?:\.\d{1,2})?)\s*(?:dollars|usd|bucks)?\s*$",
    re.I)


def _price_from_correction(text: str) -> float | None:
    """The price a typed correction is asking for, or None if it isn't one.

    Charm-prices a whole number the same way the price gate does (35 -> 34.99),
    so the two entry points can't disagree."""
    match = _PRICE_CORRECTION.match(text.strip())
    if not match:
        return None
    raw = text.strip()
    # A bare number with no price word and no "$" is ambiguous in a sentence, but
    # unambiguous when it's the entire message.
    if not re.search(r"price|\$|sell|list|make it|change", raw, re.I) and not re.fullmatch(
            r"\$?\s*\d{1,5}(?:\.\d{1,2})?", raw):
        return None
    try:
        return _charm_price(float(match.group(1)))
    except (TypeError, ValueError):
        return None


def _apply_manual_price(pricing: dict, price: float) -> dict:
    """Record a human price override without erasing what the machine proposed.

    `machine_price` is written once by pipeline/price.py and never touched here;
    the override goes to suggested_price (what the rest of the pipeline reads)
    and is also kept as manual_price alongside it. Previously this assignment
    overwrote suggested_price outright, so every correction destroyed the number
    it was correcting — 34 of 57 items ended up with price_basis='manual' and no
    record of how far off the suggestion had been. Keeping both makes that gap
    measurable instead of invisible."""
    pricing = dict(pricing)
    pricing["manual_price"] = price
    pricing["manual_price_at"] = datetime.now(timezone.utc).isoformat()
    if pricing.get("machine_price") is None:
        # Pre-existing items priced before machine_price existed: the current
        # suggestion is the machine's, so capture it before it's replaced.
        pricing["machine_price"] = pricing.get("suggested_price")
    pricing["suggested_price"] = price
    pricing["confidence"] = "manual"
    pricing["price_basis"] = "manual"
    return pricing


def _charm_price(value: float) -> float:
    """Drop a penny off a whole-dollar amount so it ends in .99 (35 -> 34.99), the
    way the auto-pricing already rounds. Explicit cents are honored as typed."""
    if value == int(value):
        value -= 0.01
    return round(max(value, 0.0), 2)


def _parse_price_override(text: str) -> float | None:
    """Extract a price the user typed (e.g. '35', '$35', 'make it 42.50') and
    charm-price it. Returns None if the text doesn't name one.

    Deliberately anchored. This used to `re.search` for a digit run ANYWHERE in
    the text, which made every item id a valid price: `/setprice a1b2c3d4` (the
    price forgotten) matched the "1", charm-priced it to $0.99, and — because
    the id had been consumed as the price — pushed 99 cents to whatever listing
    `last_item` happened to hold. A price is now either the whole token or
    follows a word that means one."""
    raw = text.replace(",", "").strip()
    m = re.fullmatch(r"\$?\s*(\d+(?:\.\d{1,2})?)", raw)
    if m is None:
        # Allow a price embedded in a phrase, but only after an explicit cue —
        # "make it 42.50", "price 35", "$35 please".
        m = re.search(r"(?:price|sell|list|make it|change to|\$)\s*(\d+(?:\.\d{1,2})?)",
                      raw, re.I)
    if m is None:
        return None
    try:
        value = float(m.group(1))
    except (TypeError, ValueError):
        return None
    # A free item is never what someone meant to type.
    return _charm_price(value) if value > 0 else None


_CONFIRM_WORDS = ("confirm", "yes", "ok", "okay", "y", "✅", "👍")


def _explicit_target(update, text: str) -> tuple[str | None, str]:
    """Which item this message is aimed at, and the message with any id stripped.

    Two ways to aim, both needed once a haul puts several items in play at once:
      * reply to the bot's message for that item (the natural one), or
      * lead with its id: "a1b2c3d4 color is navy".
    Returns (None, text) when neither applies, so single-item use is unchanged."""
    replied = util._replied_item(update)
    if replied:
        return replied, text
    parts = text.split(None, 1)
    if len(parts) == 2 and re.fullmatch(r"[0-9a-f-]{4,36}", parts[0].lower()):
        item_id, _ = util._resolve_item_id(0, [parts[0].lower()])
        if item_id:
            return item_id, parts[1]
    return None, text


async def _handle_targeted(user_id, item_id, text, update, context) -> bool:
    """Apply a message to a specific item the user aimed at, whatever else is on
    screen. Returns True if it was handled here.

    Without this, a correction typed during a haul lands on whichever draft
    happened to be shown last, which is rarely the one being looked at."""
    item = db.get_item(item_id)
    if item is None:
        await util._safe_reply(update.message, "That item no longer exists.")
        return True
    status = item["status"]

    if status in ("drafted", "review"):
        state.review[user_id] = item_id
        state.last_item[user_id] = item_id
        return False  # fall through to the review handler, now pointed correctly

    if status in ("identified", "priced"):
        stage = "identify" if status == "identified" else "price"
        active = state.gate.get(user_id)
        if active and active["item_id"] != item_id:
            # Put the item we're displacing back at the front of the queue rather
            # than dropping it.
            state.gate_queue.setdefault(user_id, []).insert(
                0, {"item_id": active["item_id"], "stage": active["stage"],
                    "reason": "returning to this one"})
        # This item is about to be handled here and now, so it must not also
        # remain queued — during a haul it usually IS queued, and leaving the
        # entry meant it came back around after being dealt with and asked for a
        # decision on a gate it had already cleared.
        queued = state.gate_queue.get(user_id)
        if queued:
            state.gate_queue[user_id] = [q for q in queued if q["item_id"] != item_id]
        state.gate[user_id] = {"item_id": item_id, "stage": stage}
        await _handle_gate(user_id, text, update, context)
        return True

    await util._safe_reply(
        update.message,
        f"{item_id[:8]} is '{status}' — nothing to change here. "
        f"/listing {item_id[:8]} to see it, or /setprice to change the price.")
    return True


async def _handle_gate(user_id, text, update, context) -> None:
    """Handle a typed message while an item is paused at the identify/price gate:
    'confirm' advances to the next stage; anything else is applied as a change and
    the same gate is shown again."""
    g = state.gate[user_id]
    item_id, stage = g["item_id"], g["stage"]
    low = text.lower().strip()

    if db.get_item(item_id) is None:
        state.gate.pop(user_id, None)
        await util._safe_reply(update.message, "That item no longer exists.")
        # Hand the slot on — otherwise a deleted item takes the rest of a haul's
        # queued gates down with it.
        await flow._activate_next_gate(user_id, update.message)
        return

    if low in _CONFIRM_WORDS:
        if stage == "price" and (db.get_item(item_id)["pricing"] or {}).get("suggested_price") is None:
            await util._safe_reply(update.message, "No price set yet — type a price first (e.g. 35 -> $34.99).")
            return
        state.gate.pop(user_id, None)
        # Confirming identify leaves the item 'identified' (advance -> price gate);
        # confirming price leaves it 'priced' (advance -> draft + review).
        note = "✅ Confirmed. Pricing..." if stage == "identify" else "✅ Confirmed. Writing the listing..."
        await util._safe_reply(update.message, note)
        context.application.create_task(flow.advance(user_id, item_id, update.message, context))
        return

    # Not a confirm → treat as a change to this stage.
    if stage == "identify":
        await util._safe_reply(update.message, "✏️ Updating the identification...")
        try:
            item = db.get_item(item_id)
            revised = await asyncio.to_thread(draft.revise_identification, item["identification"], text)
        except Exception as e:
            await util._safe_reply(update.message, f"⚠️ Could not update: {type(e).__name__}: {str(e)[:200]}")
            return
        db.update_field(item_id, "identification", revised)
        await flow._show_identify_gate(user_id, item_id, update.message)
    else:  # price
        forced = low.endswith(" force")
        price = _parse_price_override(text[:-6] if forced else text)
        if price is None:
            await util._safe_reply(update.message, "Type a price to set it (e.g. 35 -> $34.99), or 'confirm'.")
            return
        item = db.get_item(item_id)
        # Same floor the repricer and /setprice honour — the gate is a price
        # entry point like any other, and this one leads straight to review.
        if not forced:
            refusal = _floor_refusal(item, price)
            if refusal:
                await util._safe_reply(update.message,
                                  f"⚠️ {refusal.splitlines()[0]}\nType '{price:.2f} force' to set it anyway.")
                return
        pricing = _apply_manual_price(item.get("pricing") or {}, price)
        db.update_field(item_id, "pricing", pricing)
        await flow._show_price_gate(user_id, item_id, update.message)


async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    text = update.message.text.strip()
    low = text.lower()

    if user_id in state.inv_box:
        from pipeline import inventory
        if inventory.BOX_RE.match(text.upper()):
            state.inv_box[user_id] = text.upper()
            await util._safe_reply(update.message, f"📦 Now in {text.upper()}. Next code: "
                              f"{db.next_piece_code(text.upper())}")
            return

    if low in ("wait", "hold", "hold on", "⏳"):
        state.capture_window[user_id] = intake.EXTENDED_CAPTURE_WINDOW
        # If a batch is already counting down on the short window, restart its
        # timer so the longer window takes effect right away.
        async with util._lock_for(user_id):
            pend = state.pending.get(user_id)
            if pend is not None:
                if pend["task"] is not None:
                    pend["task"].cancel()
                pend["task"] = context.application.create_task(
                    intake.finalize_capture(user_id, update, context)
                )
        await util._safe_reply(
            update.message,
            f"⏳ OK — holding for {intake.EXTENDED_CAPTURE_WINDOW}s. Send all your photos now.",
        )
        return

    # An explicitly aimed message wins over everything: replying to an item's
    # message, or leading with its id, is the user saying which one they mean.
    target, text = _explicit_target(update, text)
    if target:
        low = text.lower().strip()
        if await _handle_targeted(user_id, target, text, update, context):
            return
        # Not handled means it's a review item and `review` now points at it.

    # Confirm gates (after identify / after price) take precedence over the draft
    # review — an item pauses here for a typed 'confirm' or a correction.
    #
    # Exception: 'approve'/'reject' are review words, never gate words. During a
    # haul one item can be gated while another waits at review, and routing an
    # "approve" into the gate fed it to revise_identification as a correction on
    # a different item. Send those to the review handler instead.
    if user_id in state.gate and not target and low not in ("approve", "reject"):
        await _handle_gate(user_id, text, update, context)
        return
    if user_id in state.gate and user_id not in state.review:
        await util._safe_reply(
            update.message,
            "Nothing is waiting for approval. This item is at a gate — "
            "'confirm' to accept it, or type a correction / a price.")
        return

    if user_id not in state.review:
        await util._safe_reply(update.message, "Send me photos of an item to start.")
        return

    # Typed approve/reject/correction target the most-recent draft (the buttons
    # on each draft handle any-order approval unambiguously).
    item_id = state.review[user_id]

    if low in ("approve", "✅", "yes", "ok", "reject", "❌", "no"):
        # Same claim as the Approve/Reject buttons: a typed approve racing a tap
        # on the same draft must not build the eBay draft twice.
        if not util._claim("review", item_id):
            await util._safe_reply(update.message, f"⏳ Already handling {item_id[:8]}.")
            return
        try:
            state.review.pop(user_id, None)
            if low in ("reject", "❌", "no"):
                db.update_status(item_id, "rejected")
                await util._safe_reply(update.message, "❌ Rejected.")
            else:
                await flow._approve_item(item_id, update.message, context)
        finally:
            util._release("review", item_id)
        return

    item = db.get_item(item_id)
    current = item["listing"]

    # A correction that names a price is a price change, and it goes through the
    # proper path: recorded as manual_price with machine_price preserved, exactly
    # as typing a price at the price gate does. Without this the copywriter would
    # be the one setting it — and the guard that stops the model drifting the
    # price would silently revert the user's own number too.
    wanted = _price_from_correction(text)
    if wanted is not None:
        pricing = _apply_manual_price(item.get("pricing") or {}, wanted)
        db.update_field(item_id, "pricing", pricing)
        listing = dict(current)
        listing["price"] = wanted
        db.update_field(item_id, "listing", listing)
        await util._safe_reply(update.message, f"💲 Price set to ${wanted} for {item_id[:8]}.")
        await flow._show_review(user_id, item_id, update.message)
        return

    await util._safe_reply(update.message, "✏️ Applying your correction...")
    try:
        revised = await asyncio.to_thread(draft.revise_draft, current, text,
                                            item.get("identification"))
    except Exception as e:
        await util._safe_reply(update.message, f"⚠️ Could not revise: {e}")
        return

    db.update_field(item_id, "listing", revised)
    # The correction changed the draft, so the old check no longer describes it.
    if config.LISTING_CHECK_ENABLED:
        await asyncio.to_thread(listing_check.check_and_store, item_id)
    # Through _show_review, so the revised draft carries its id header and is
    # registered as a reply target like the first one — a haul correction made
    # by replying to it used to land on nothing.
    await flow._show_review(user_id, item_id, update.message)


def _floor_refusal(item: dict, price: float) -> str | None:
    """Why this price shouldn't be set, or None if it's fine.

    The README has always promised that price changes "respect the margin
    floor", and pipeline/reprice honours it — but the automated path was the
    only one that did. A price typed by hand went straight to eBay, so the one
    route nothing reviews afterwards was also the one with no floor under it.

    Returns None when the cost is unknown: breakeven_price refuses to guess from
    an estimated cost, and a floor nobody can compute must not become a wall."""
    floor = profit.breakeven_price(item)
    if floor is None or price > floor:
        return None
    return (f"${price:.2f} is at or below the ${floor:.2f} break-even — it would "
            f"sell at a loss after fees, ads and postage.\n"
            f"Add `force` to do it anyway (e.g. /setprice {item['item_id'][:8]} "
            f"{price:.2f} force).")
