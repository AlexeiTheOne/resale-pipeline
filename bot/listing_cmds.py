"""Listing management: /listing, /delete, /refreshdesc, /end, /setprice,
/setqty, /activate, /promote, /offers, /pricecheck (+ the reprice digest and its
buttons), /notdup and /retry.

Moved verbatim out of telegram_bot.py (docs/cleanup-plan.md Phase 2, step 4b).
"""
import asyncio
import shutil
import traceback
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

import config
import db
from bot import flow, formatting, state, text_router, util
from ebay import inventory, negotiation
from pipeline import offers, reprice


# Upper bound on a hand-typed /setqty. Ross stock is near-always single units and
# the largest real listing so far held 5, so anything past this is a typo or a
# mis-parsed item id rather than an inventory decision.
_MAX_SANE_QTY = 500
# Same idea for a price. The dearest item handled so far listed around $200, so
# five figures is not a decision anyone makes by typing into a chat window — it's
# an item id, a barcode, or a slipped decimal point.
_MAX_SANE_PRICE = 10_000.0


async def listing_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    item_id, err = util._resolve_item_id(user_id, context.args)
    if err:
        await util._safe_reply(update.message, f"⚠️ {err}")
        return
    state.last_item[user_id] = item_id

    item = db.get_item(item_id)
    if not item or not item.get("listing"):
        await util._safe_reply(update.message, f"No listing draft yet for {item_id[:8]}.")
        return

    result = {
        "identification": item.get("identification") or {},
        "pricing": item.get("pricing") or {},
        "listing": item["listing"],
        "listing_check": item.get("listing_check"),
    }
    # Offer Approve/Reject only while the item is still awaiting review.
    markup = flow._review_markup(item_id) if item["status"] in ("review", "drafted") else None
    if markup is not None:
        state.review[user_id] = item_id
    await util._safe_reply(update.message, formatting.format_draft(result), reply_markup=markup)


async def notdup_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/notdup <id> <id> — these two are different physical items (same UPC or
    model, but e.g. the print sits differently), so stop warning about them."""
    user_id = update.effective_user.id
    args = context.args or []
    if len(args) != 2:
        await util._safe_reply(update.message, "Usage: /notdup <id> <id>  (8-char ids are fine)")
        return
    ids = []
    for arg in args:
        item_id, err = util._resolve_item_id(user_id, [arg])
        if err:
            await util._safe_reply(update.message, f"⚠️ {err}")
            return
        ids.append(item_id)
    if ids[0] == ids[1]:
        await util._safe_reply(update.message, "⚠️ That's the same item twice.")
        return
    db.mark_not_duplicate(*ids)
    await util._safe_reply(update.message, f"👍 Noted: {ids[0][:8]} and {ids[1][:8]} are different "
                                      "items. No more duplicate warnings for that pair.")


async def delete_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    item_id, err = util._resolve_item_id(user_id, context.args)
    if err:
        await util._safe_reply(update.message, f"⚠️ {err}")
        return

    item = db.get_item(item_id)
    if item is None:
        await util._safe_reply(update.message, f"No item found for {item_id[:8]}.")
        return

    offer_id = (item.get("ebay") or {}).get("offer_id")
    if offer_id:
        try:
            # A published offer can't be deleted while live — end it first.
            if item["status"] == "published":
                await asyncio.to_thread(inventory.withdraw_offer, offer_id)
            await asyncio.to_thread(inventory.delete_offer, offer_id)
        except Exception as e:
            await util._safe_reply(update.message, f"⚠️ Could not delete eBay offer {offer_id}: {e}")
            return

    photos = item.get("photos") or []
    if photos:
        folder = Path(photos[0]).parent
        shutil.rmtree(folder, ignore_errors=True)

    db.delete_item(item_id)
    state.last_item.pop(user_id, None)
    state.review.pop(user_id, None)
    # Drop the deleted item from the gate slot and the queue, then let whatever
    # was waiting behind it through. Without this, deleting the item currently
    # holding the gate leaves the rest of a haul queued behind a ghost.
    released = state.gate.get(user_id) is not None and state.gate[user_id]["item_id"] == item_id
    if released:
        state.gate.pop(user_id, None)
    state.gate_queue[user_id] = [e for e in state.gate_queue.get(user_id, []) if e["item_id"] != item_id]
    await util._safe_reply(update.message, f"🗑️ Deleted {item_id[:8]} (offer {offer_id or 'none'} removed).")
    if released:
        await flow._activate_next_gate(user_id, update.message)


def _refresh_descriptions(apply: bool, force: bool) -> dict:
    """Re-render every eBay-side item's description and (when apply) push it. Runs
    in a worker thread — one eBay GET per item, plus a PUT for each one changed.
    Per-item failures are collected, never fatal: one bad offer must not strand
    the other ninety."""
    results = {"updated": [], "current": [], "diverged": [], "no_offer": [], "error": []}
    for item in db.list_items():
        if item["status"] not in ("ebay_draft", "published"):
            continue
        try:
            outcome = inventory.refresh_offer_description(item, apply=apply, force=force)
        except Exception as e:
            util._record_error(f"refreshdesc ({item['item_id'][:8]})", e)
            results["error"].append(f"{item['item_id'][:8]}: {type(e).__name__}")
            continue
        results[outcome].append(item["item_id"][:8])
    return results


async def refreshdesc_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Push the current description copy to listings that are ALREADY on eBay —
    the WYSIWYG line only reaches a listing when its offer is built, so anything
    live from before is untouched until this runs.

    /refreshdesc            dry run: what would change, touching nothing
    /refreshdesc apply      push it (live listings update immediately)
    /refreshdesc apply force  also overwrite descriptions edited in Seller Hub
    """
    args = [a.lower() for a in (context.args or [])]
    apply = "apply" in args
    force = "force" in args

    await util._safe_reply(update.message,
        ("✍️ Updating descriptions on eBay..." if apply
         else "🔍 Checking descriptions (dry run — nothing will be changed)..."))
    results = await asyncio.to_thread(_refresh_descriptions, apply, force)

    verb = "Updated" if apply else "Would update"
    lines = [f"📝 Description refresh — {verb.lower()} {len(results['updated'])} listing(s).", ""]
    if results["updated"]:
        lines.append(f"{verb}: {', '.join(results['updated'])}")
    if results["current"]:
        lines.append(f"Already current ({len(results['current'])}) — no change needed.")
    if results["diverged"]:
        lines += ["",
                  f"✋ Skipped {len(results['diverged'])} whose live description isn't the one we "
                  f"generated — edited in Seller Hub, or listed before a change to the renderer. "
                  f"Pushing ours would throw that away: {', '.join(results['diverged'])}",
                  "Add 'force' to overwrite them anyway."]
    if results["no_offer"]:
        lines.append(f"No eBay offer / no description ({len(results['no_offer'])}) — skipped.")
    if results["error"]:
        lines += ["", f"⚠️ Failed ({len(results['error'])}): {', '.join(results['error'])}"]
    if not apply and results["updated"]:
        lines += ["", "Nothing was changed. Run /refreshdesc apply to push it."]
    await util._send_chunked(update.message, lines)


async def offers_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send private discounts to the people watching your listings.

    Targets intent, not margin: an offer only converts someone who already wants
    the item, so the gate is live watchers, floored on the money left afterwards.
    A private offer beats a public price cut — it converts the interested buyer
    without giving the same money away to everyone else.

      /offers              dry run — who'd get what, and why the rest won't
      /offers apply        send them
      /offers apply 8      send with a flat $8 off instead of the $5/$10 default
    """
    args = [a.lower() for a in (context.args or [])]
    apply = "apply" in args
    override = None
    for arg in args:
        try:
            override = float(arg.replace("$", ""))
        except ValueError:
            continue

    await util._safe_reply(update.message,
        "💌 Sending offers to watchers..." if apply
        else "🔍 Checking which listings have watchers and margin (dry run)...")
    try:
        rows = await asyncio.to_thread(offers.offer_plan, override)
    except Exception as e:
        traceback.print_exc()
        util._record_error("offers", e)
        await util._safe_reply(update.message,
            f"⚠️ Couldn't read eligible listings: {type(e).__name__}: {str(e)[:250]}\n"
            "If that's a 403, the sell.negotiation scope is missing — re-run "
            "`python -m ebay.auth`.")
        return

    send = [r for r in rows if r.get("send")]
    skip = [r for r in rows if not r.get("send")]
    sent, failed = [], []
    if apply:
        for row in send:
            try:
                await asyncio.to_thread(negotiation.send_offer, row["listing_id"], row["offer_price"],
                                        config.OFFER_MESSAGE)
                sent.append(row)
            except Exception as e:
                util._record_error(f"offer ({row['item_id'][:8]})", e)
                failed.append((row, f"{type(e).__name__}: {str(e)[:90]}"))
                continue
            try:
                # So the weekly digest's offer plan doesn't re-offer it next week.
                db.record_offer_sent(row["listing_id"], row["offer_price"], item_id=row["item_id"],
                                  list_price=row["price"], source="/offers")
            except Exception as e:
                print(f"WARNING: couldn't record offer sent: {type(e).__name__}: {e}")

    verb = "Sent" if apply else "Would send"
    lines = [f"💌 Offers to watchers — {verb.lower()} {len(sent) if apply else len(send)} offer(s).", ""]
    for row in (sent if apply else send):
        est = " ⚠️est cost" if row["cost_estimated"] else ""
        units = f" ×{row['qty']}" if row.get("qty", 1) > 1 else ""
        lines.append(
            f"{row['item_id'][:8]} {row['watchers']}w · ${row['price']:.2f} → ${row['offer_price']:.2f} "
            f"(−${row['discount']:.0f}, {row['pct']:.0f}%){units} · keeps ${row['net_after']:.2f}/unit "
            f"({row['margin_after'] * 100:.0f}%){est} · {row['title']}")
    if failed:
        lines += ["", f"⚠️ Failed ({len(failed)}):"]
        lines += [f"  {r['item_id'][:8]}: {why}" for r, why in failed]
    if skip:
        lines += ["", f"Skipped {len(skip)} eligible listing(s):"]
        lines += [f"  {r['item_id'][:8]} ${r['price']:.2f} — {r['verdict']} · {r['title']}"
                  for r in skip[:15]]
        if len(skip) > 15:
            lines.append(f"  ...and {len(skip) - 15} more")
    if not apply and send:
        lines += ["", f"Nothing sent. /offers apply to send these {len(send)} offer(s) — "
                      f"they go straight to buyers and can't be recalled."]
    if not rows:
        lines = ["💌 No listings are eligible for offers right now — eBay needs watchers "
                 "(or carted items) and no offer already outstanding."]
    await util._send_chunked(update.message, lines)


async def end_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """End a live listing (withdraw it from eBay) without deleting the item. The
    offer drops back to a draft, so you can /activate it again later. Use this when
    an item sold elsewhere or a live listing needs pulling."""
    user_id = update.effective_user.id
    item_id, err = util._resolve_item_id(user_id, context.args)
    if err:
        await util._safe_reply(update.message, f"⚠️ {err}")
        return
    state.last_item[user_id] = item_id

    item = db.get_item(item_id)
    if item is None:
        await util._safe_reply(update.message, f"No item found for {item_id[:8]}.")
        return
    if item["status"] != "published":
        await util._safe_reply(update.message,
            f"{item_id[:8]} is '{item['status']}', not a live listing — nothing to end.")
        return
    offer_id = (item.get("ebay") or {}).get("offer_id")
    if not offer_id:
        await util._safe_reply(update.message, "No eBay offer id stored for this item.")
        return

    try:
        await asyncio.to_thread(inventory.withdraw_offer, offer_id)
    except Exception as e:
        traceback.print_exc()
        util._record_error(f"end ({item_id[:8]})", e)
        await util._safe_reply(update.message, f"⚠️ Could not end the listing: {type(e).__name__}: {str(e)[:250]}")
        return

    db.update_status(item_id, "ebay_draft")
    await util._safe_reply(update.message,
        f"🛑 Ended the live listing for {item_id[:8]} — it's back to a draft. /activate to relist.")


async def setprice_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Set an item's price. Usage: /setprice [id] <price> [force] (a whole number
    is charm-priced, 35 -> $34.99). Updates the stored price, and if the item
    already has an eBay offer (draft or live), pushes the new price to eBay too.

    Refuses a price below break-even unless `force` is given."""
    user_id = update.effective_user.id
    args = list(context.args)
    forced = bool(args) and args[-1].lower() in ("force", "-f", "!")
    if forced:
        args = args[:-1]
    if not args:
        await util._safe_reply(update.message, "Usage: /setprice [id] <price>  (e.g. /setprice 35 -> $34.99)")
        return
    # Item id first — an all-digit id prefix is a valid-looking price, and
    # consuming it as one leaves no id behind, so the command silently retargets
    # at last_item and pushes that number to a LIVE listing.
    if util._names_an_item(args[-1]):
        await util._safe_reply(update.message,
            f"'{args[-1]}' names an item, not a price. Usage: /setprice {args[-1]} <price>")
        return
    price = text_router._parse_price_override(args[-1])
    if price is None:
        await util._safe_reply(update.message, f"'{args[-1]}' isn't a valid price.")
        return
    if price > _MAX_SANE_PRICE:
        await util._safe_reply(update.message,
            f"${price:,.2f} is past the ${_MAX_SANE_PRICE:,.0f} sanity cap — "
            f"pass a smaller number, or edit it in Seller Hub if you really mean it.")
        return
    item_id, err = util._resolve_item_id(user_id, args[:-1])
    if err:
        await util._safe_reply(update.message, f"⚠️ {err}")
        return
    item = db.get_item(item_id)
    if item is None:
        await util._safe_reply(update.message, f"No item found for {item_id[:8]}.")
        return
    state.last_item[user_id] = item_id

    if not forced:
        refusal = text_router._floor_refusal(item, price)
        if refusal:
            await util._safe_reply(update.message, f"⚠️ {refusal}")
            return

    # Update the stored price on both the listing and the pricing record.
    listing = dict(item.get("listing") or {})
    if listing:
        listing["price"] = price
        db.update_field(item_id, "listing", listing)
    pricing = text_router._apply_manual_price(item.get("pricing") or {}, price)
    db.update_field(item_id, "pricing", pricing)

    offer_id = (item.get("ebay") or {}).get("offer_id")
    if offer_id and item["status"] in ("ebay_draft", "published"):
        try:
            await asyncio.to_thread(inventory.update_offer_price, offer_id, price)
        except Exception as e:
            traceback.print_exc()
            util._record_error(f"setprice ({item_id[:8]})", e)
            await util._safe_reply(update.message,
                f"💲 Saved ${price} locally, but updating eBay failed: {type(e).__name__}: {str(e)[:200]}\n"
                "The stored price is updated; use /retry or /addphotos to rebuild the offer.")
            return
        where = "live listing" if item["status"] == "published" else "eBay draft"
        await util._safe_reply(update.message, f"💲 Price set to ${price} for {item_id[:8]} (updated the {where}).")
    else:
        await util._safe_reply(update.message, f"💲 Price set to ${price} for {item_id[:8]}.")


async def setqty_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Set an item's available quantity on eBay. Usage: /setqty [id] <n>.
    Quantity isn't asked during posting (every item defaults to 1); use this after
    the item is on eBay to stock more than one. Requires an eBay offer (a draft or
    a live listing), and pushes the new quantity to eBay."""
    user_id = update.effective_user.id
    args = list(context.args)
    if not args:
        await util._safe_reply(update.message, "Usage: /setqty [id] <quantity>  (e.g. /setqty 3)")
        return
    # Item id first — an all-digit id prefix would otherwise parse as a quantity
    # and push a 40-million stock count to eBay against the wrong listing.
    if util._names_an_item(args[-1]):
        await util._safe_reply(update.message,
            f"'{args[-1]}' names an item, not a quantity. Usage: /setqty {args[-1]} <n>")
        return
    try:
        qty = int(args[-1])
    except ValueError:
        await util._safe_reply(update.message, f"'{args[-1]}' isn't a whole number.")
        return
    if qty < 0:
        await util._safe_reply(update.message, "Quantity can't be negative.")
        return
    if qty > _MAX_SANE_QTY:
        await util._safe_reply(update.message,
            f"{qty} units? That's past the {_MAX_SANE_QTY} sanity cap — pass a smaller number.")
        return
    item_id, err = util._resolve_item_id(user_id, args[:-1])
    if err:
        await util._safe_reply(update.message, f"⚠️ {err}")
        return
    item = db.get_item(item_id)
    if item is None:
        await util._safe_reply(update.message, f"No item found for {item_id[:8]}.")
        return
    state.last_item[user_id] = item_id

    ebay = dict(item.get("ebay") or {})
    offer_id, sku = ebay.get("offer_id"), ebay.get("sku")
    if not offer_id or not sku or item["status"] not in ("ebay_draft", "published"):
        await util._safe_reply(update.message,
            f"{item_id[:8]} isn't on eBay yet (status '{item['status']}') — it needs an eBay "
            "offer first. Approve/activate it, then set the quantity.")
        return

    try:
        await asyncio.to_thread(inventory.update_offer_quantity, sku, offer_id, qty)
    except Exception as e:
        traceback.print_exc()
        util._record_error(f"setqty ({item_id[:8]})", e)
        await util._safe_reply(update.message,
            f"⚠️ Could not set the quantity: {type(e).__name__}: {str(e)[:250]}")
        return

    ebay["quantity"] = qty
    db.update_field(item_id, "ebay", ebay)
    where = "live listing" if item["status"] == "published" else "eBay draft"
    await util._safe_reply(update.message, f"📦 Quantity set to {qty} for {item_id[:8]} (updated the {where}).")


async def activate_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    item_id, err = util._resolve_item_id(user_id, context.args)
    if err:
        await util._safe_reply(update.message, f"⚠️ {err}")
        return
    state.last_item[user_id] = item_id

    item = db.get_item(item_id)
    if item is None:
        await util._safe_reply(update.message, f"No item found for {item_id[:8]}.")
        return
    if item["status"] != "ebay_draft":
        await util._safe_reply(update.message,
            f"Item {item_id[:8]} is at status '{item['status']}', not 'ebay_draft'. Nothing to activate."
        )
        return

    # Publish + auto-promote, shared with advance() so both paths behave identically.
    await flow._publish_and_report(item_id, update.message)


async def promote_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Promote a listing (or change its ad rate). Usage: /promote <id> <pct>
    or /promote <pct> to apply to the most recent item. eBay accepts 2–100%."""
    user_id = update.effective_user.id
    args = list(context.args)

    # Trailing arg is the percentage; the rest (if any) identifies the item.
    if not args:
        await util._safe_reply(update.message, "Usage: /promote <id> <pct>  (e.g. /promote 3f2a 10)")
        return
    pct = args[-1]
    id_args = args[:-1]
    item_id, err = util._resolve_item_id(user_id, id_args)
    if err:
        await util._safe_reply(update.message, f"⚠️ {err}")
        return
    state.last_item[user_id] = item_id
    await flow._promote_item(item_id, pct, update.message)


def _reprice_markup(item_id: str, price: float) -> InlineKeyboardMarkup:
    """Apply/Skip for one digest row. The price the button will apply is carried
    IN the callback data, because the button is the record of what was authorised:
    tapping Apply means "yes, that price", and a digest message sits in the chat
    indefinitely. Re-reading the suggestion at tap time — which is what this used
    to do — hands you whatever the most recent /pricecheck stored instead, so a
    second run between the digest and the tap silently substitutes a different
    number for the one on screen.

    Telegram caps callback_data at 64 bytes; this is 58 at worst (a 36-char uuid
    plus a 7-char price)."""
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Apply", callback_data=f"reprice:apply:{price:.2f}:{item_id}"),
        InlineKeyboardButton("Skip", callback_data=f"reprice:skip:-:{item_id}"),
    ]])


def _offers_markup(plan_id: str, n: int) -> InlineKeyboardMarkup:
    """The digest's ONE button for the whole watcher-offer plan. The plan's rows
    live in db.offer_plans (64 bytes can't carry a dozen prices); the tap sends
    exactly those, via pipeline.reprice.send_planned_offers. 53 bytes."""
    return InlineKeyboardMarkup([[InlineKeyboardButton(
        f"📨 Send offers to {n} watched listing{'s' if n != 1 else ''}",
        callback_data=f"reprice:offers:-:{plan_id}")]])


def _offer_plan_lines(offers: dict | None) -> list[str]:
    """The digest's offer section: one line per planned listing, said once."""
    rows = (offers or {}).get("rows") or []
    if not rows:
        return []
    lines = [f"📨 Offers to watchers ready ({len(rows)}) — ~{config.WATCHER_OFFER_PCT:.0f}% off, never "
             f"under the Best Offer auto-decline or cost floor. A private offer beats a "
             f"public cut. Nothing is sent until you tap the button."]
    for row in rows:
        lines.append(f"• {row['item_id'][:8]} {row['title'][:30]} ${row['price']:.2f} → "
                     f"${row['offer_price']:.2f} (−{row['pct']:.0f}%) · {row['watchers']} watch")
    return lines


_VERDICT_LABEL = {
    "INVISIBLE": "🙈 Barely shown on eBay (promoted included)",
    "LOW_CTR": "😐 Seen but not clicked",
    "OVERPRICED": "💸 Views but no watchers — likely overpriced",
    "SEND_OFFERS": "👀 Watchers building — consider a private offer before cutting",
    "STALE": "🕸️ Stale — real views, still unsold",
    "STALE_UNSEEN": "🔍 Unsold but barely seen — a findability problem, not a price one",
    "TOO_NEW": "🌱 Too new to judge yet",
    "HEALTHY": "✅ Healthy",
    "NO_WATCH_DATA": "❔ Couldn't read the watch count — not judged",
    "NO_TRAFFIC_DATA": "❔ Couldn't load eBay traffic — not judged",
    "ERROR": "⚠️ Couldn't check",
}


# What to actually do about it. The visibility verdicts never carry a suggested
# price (pipeline/reprice.py only prices OVERPRICED and STALE), so without this
# they'd arrive as a diagnosis with no next step.
_VERDICT_ADVICE = {
    "INVISIBLE": "Under the weekly impressions floor even with promotion. Check the "
                 "category and item specifics, or /end and relist to refresh it. "
                 "Not a price cut.",
    "LOW_CTR": "They see it and scroll past: cover photo first, then title. "
               "Not a price cut.",
    "STALE_UNSEEN": "Too few views to blame the price. Rework the title/keywords "
                    "and consider /promote, or /end and relist to refresh ranking.",
    "SEND_OFFERS": "Send an offer to the watchers before cutting the public price.",
    "NO_WATCH_DATA": "eBay didn't return a watch count, and every remaining check "
                     "depends on it. Nothing is wrong with the listing as far as "
                     "this run can tell — re-run /pricecheck later.",
    "NO_TRAFFIC_DATA": "eBay's traffic report failed for this listing, so there "
                       "are no impressions to judge — this is NOT \"invisible\". "
                       "Re-run /pricecheck later.",
}


def _format_reprice_line(r: dict) -> str:
    item = db.get_item(r["item_id"])
    title = ((item or {}).get("listing") or {}).get("title") or "(untitled)"
    label = _VERDICT_LABEL.get(r["verdict"], r["verdict"])
    line = f"{r['item_id'][:8]} · {label}\n  {title[:48]}"
    if r["verdict"] == "ERROR":
        line += f"\n  {r.get('error', '')[:150]}"
        return line
    watchers = r.get("watchers")
    # Cumulative views alongside this week's, because the OVERPRICED/STALE
    # verdicts are decided on the running total — showing only the week's figure
    # made those diagnoses look like they'd been reached on 3 views.
    cum = r.get("cumulative_views")
    # None = the traffic fetch failed (NO_TRAFFIC_DATA) — show "?", not 0.
    impr = r.get("impressions")
    views = f"{r['views'] if r.get('views') is not None else '?'} views"
    if cum is not None and cum != r.get("views"):
        views += f" ({cum} total)"
    ctr = r.get("ctr")
    # impressions = total (promoted included), the number verdicts use.
    organic = r.get("organic_impressions")
    impr_text = f"{impr if impr is not None else '?'} impr"
    if impr is not None and organic is not None:
        impr_text += f" ({organic} organic)"
    line += (f"\n  {impr_text} · {views} · "
             + (f"{ctr * 100:.2f}% CTR · " if ctr else "")
             + f"{watchers if watchers is not None else '?'} watchers · ${r.get('current_price')}")
    if r.get("suggested_price"):
        line += f" → suggest ${r['suggested_price']}"
    elif _VERDICT_ADVICE.get(r["verdict"]):
        line += f"\n  💡 {_VERDICT_ADVICE[r['verdict']]}"
    elif r.get("floor_note"):
        # Why there's no suggested cut. Without this the digest shows a problem
        # item with no action and no explanation, which reads as a bug.
        line += f"\n  ⛔ {r['floor_note']}"
    return line


def _format_reprice_short(r: dict) -> str:
    """One line per listing for the grouped digest: id, title, price, and the
    numbers that matter (impressions / views / watchers)."""
    item = db.get_item(r["item_id"])
    title = ((item or {}).get("listing") or {}).get("title") or "(untitled)"
    if r["verdict"] == "ERROR":
        return f"• {r['item_id'][:8]} {title[:32]} — {str(r.get('error', ''))[:60]}"
    impr = r.get("impressions")
    views = r.get("cumulative_views")
    if views is None:
        views = r.get("views")
    watchers = r.get("watchers")
    q = lambda v: "?" if v is None else v
    return (f"• {r['item_id'][:8]} {title[:32]} ${r.get('current_price')} · "
            f"{q(impr)} impr · {q(views)} views · {q(watchers)} watch")


def _reprice_health_line(health: dict | None) -> str | None:
    """One line saying how much of eBay's data actually arrived. Without it a
    run whose traffic fetch failed looked exactly like a week where nothing was
    found in search (2026-08-17: 71 INVISIBLE; 2026-10-05: 97)."""
    if not health:
        return None
    return (f"Data: {health.get('traffic_failed', 0)}/{health.get('traffic_batches', 0)} "
            f"traffic batch(es) failed · watch count missing for "
            f"{health.get('watch_failed', 0)}/{health.get('watch_lookups', 0)}.")


async def _send_reprice_digest(bot, chat_id, results: list[dict],
                               health: dict | None = None,
                               incomplete_note: str | None = None) -> None:
    """Post a weekly price-check digest: one summary header, one grouped list of
    flagged listings, one watcher-offer message with a single "Send offers"
    button (health["offers"], when there's a plan), and one message per
    suggested price with its Apply/Skip button. Healthy/too-new items are just counted in
    the header, not spelled out individually — and so are NO_TRAFFIC_DATA ones:
    a failed traffic fetch covers whole batches, there is nothing to act on per
    listing, and 97 "couldn't load" messages would bury the real flags.

    `health` (from run_weekly_check) becomes a data-health line in the header;
    `incomplete_note` is appended when traffic batches failed, to say what
    happens next (the scheduler retries; /pricecheck is re-run by hand)."""
    no_traffic = [r for r in results if r["verdict"] == "NO_TRAFFIC_DATA"]
    flagged = [r for r in results
               if r["verdict"] not in ("HEALTHY", "TOO_NEW", "NO_TRAFFIC_DATA")]
    healthy = [r for r in results if r["verdict"] == "HEALTHY"]
    too_new = [r for r in results if r["verdict"] == "TOO_NEW"]

    header = (f"📈 Weekly price check — {len(results)} live listing(s): "
              f"{len(flagged)} flagged, {len(healthy)} healthy, {len(too_new)} too new to judge"
              + (f", {len(no_traffic)} not judged (eBay traffic didn't load)" if no_traffic else "")
              + ".")
    health_line = _reprice_health_line(health)
    if health_line:
        header += f"\n{health_line}"
    if (health or {}).get("traffic_failed"):
        header += ("\n⚠️ Incomplete: listings without traffic data were NOT diagnosed "
                   "as invisible — the numbers are missing, not zero."
                   + (f" {incomplete_note}" if incomplete_note else ""))
    offers = (health or {}).get("offers") or {}
    if offers.get("error"):
        header += (f"\n⚠️ Offers to watchers not planned: couldn't read eBay's eligible "
                   f"listings ({offers['error'][:80]}). /offers to retry.")
    try:
        await bot.send_message(chat_id=chat_id, text=header)
    except Exception as e:
        print(f"WARNING: reprice digest header failed: {type(e).__name__}: {e}")
        return

    # Only a suggested price earns its own message, because its Apply button has
    # to sit under it. Everything else is grouped by verdict into one compact
    # list with the advice said once — one message per listing was 50+ phone
    # notifications a week that nobody read. The watcher-offer plan is one more
    # grouped message with one button for all of it; a listing in that plan
    # isn't listed again under SEND_OFFERS.
    planned = {row["item_id"] for row in offers.get("rows") or []}
    actionable = [r for r in flagged if r.get("suggested_price")]
    grouped: dict[str, list[dict]] = {}
    for r in flagged:
        if r.get("suggested_price"):
            continue
        if r["verdict"] == "SEND_OFFERS" and r["item_id"] in planned:
            continue
        grouped.setdefault(r["verdict"], []).append(r)

    lines = []
    for verdict, rows in grouped.items():
        if lines:
            lines.append("")
        lines.append(f"{_VERDICT_LABEL.get(verdict, verdict)} ({len(rows)})")
        advice = _VERDICT_ADVICE.get(verdict)
        if advice:
            lines.append(f"💡 {advice}")
        for r in rows:
            lines.append(_format_reprice_short(r))
    if lines:
        try:
            for chunk in util._chunk_lines(lines):
                await bot.send_message(chat_id=chat_id, text=chunk)
        except Exception as e:
            print(f"WARNING: reprice digest list failed: {type(e).__name__}: {e}")

    offer_lines = _offer_plan_lines(offers)
    if offer_lines and offers.get("plan_id"):
        chunks = util._chunk_lines(offer_lines)
        try:
            for i, chunk in enumerate(chunks):
                markup = (_offers_markup(offers["plan_id"], len(offers["rows"]))
                          if i == len(chunks) - 1 else None)
                await bot.send_message(chat_id=chat_id, text=chunk, reply_markup=markup)
        except Exception as e:
            print(f"WARNING: reprice digest offers failed: {type(e).__name__}: {e}")

    for r in actionable:
        try:
            await bot.send_message(chat_id=chat_id, text=_format_reprice_line(r),
                                   reply_markup=_reprice_markup(r["item_id"], r["suggested_price"]))
        except Exception as e:
            print(f"WARNING: reprice digest item failed: {type(e).__name__}: {e}")


async def pricecheck_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Manually run the weekly price-health check now (it also runs automatically
    once a week). Pulls eBay traffic + watch count for every live listing,
    diagnoses why an unsold item isn't moving, and offers a one-tap price cut
    where the evidence supports one and the receipt cost is on file."""
    await util._safe_reply(update.message, "📈 Checking live listings against eBay traffic...")
    try:
        results, health = await asyncio.to_thread(reprice.run_weekly_check)
    except Exception as e:
        traceback.print_exc()
        util._record_error("pricecheck", e)
        await util._safe_reply(update.message, f"⚠️ Price check failed: {type(e).__name__}: {str(e)[:250]}")
        return
    if not results:
        await util._safe_reply(update.message, "No published listings to check.")
        return
    await _send_reprice_digest(context.bot, update.effective_chat.id, results, health,
                               incomplete_note="Re-run /pricecheck later.")


async def _send_offer_plan(message, plan_id: str) -> None:
    """Send a digest's saved watcher-offer plan (the owner tapped its button)
    and report what happened to each listing."""
    await util._safe_reply(message, "📨 Sending offers to watchers...")
    try:
        res = await asyncio.to_thread(reprice.send_planned_offers, plan_id)
    except Exception as e:
        traceback.print_exc()
        util._record_error("digest offers", e)
        await util._safe_reply(message, f"⚠️ Offers failed: {type(e).__name__}: {str(e)[:200]}")
        return
    if res.get("error"):
        await util._safe_reply(message, f"⚠️ No offers sent: {res['error']}")
        return
    for row, why in res["failed"]:
        util._record_error(f"offer ({row['item_id'][:8]})", RuntimeError(why))
    lines = [f"📨 Sent {len(res['sent'])} offer(s) to watchers."]
    lines += [f"• {r['item_id'][:8]} ${r['price']:.2f} → ${r['offer_price']:.2f} · "
              f"{r['watchers']} watch · {r['title'][:30]}" for r in res["sent"]]
    if res["failed"]:
        lines += ["", f"⚠️ Failed ({len(res['failed'])}):"]
        lines += [f"• {r['item_id'][:8]}: {why}" for r, why in res["failed"]]
    if res["skipped"]:
        lines += ["", f"Skipped {len(res['skipped'])}:"]
        lines += [f"• {r['item_id'][:8]}: {why}" for r, why in res["skipped"]]
    await util._send_chunked(message, lines)


async def reprice_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle the buttons on a weekly price-check digest: per-item Apply/Skip,
    and the one "Send offers" button for the watcher-offer plan."""
    query = update.callback_query
    await query.answer()
    try:
        _, action, price_field, item_id = query.data.split(":", 3)
    except ValueError:
        return
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass

    if action == "skip":
        return
    if action == "offers":
        # The digest's one "Send offers" button; the last field is the plan id.
        await _send_offer_plan(query.message, item_id)
        return

    item = db.get_item(item_id)
    if item is None:
        await util._safe_reply(query.message, "That item no longer exists.")
        return
    # The price comes from the button, not from the DB — it's the figure that was
    # displayed above this button and the one the tap authorises. See
    # _reprice_markup.
    try:
        price = round(float(price_field), 2)
    except (TypeError, ValueError):
        await util._safe_reply(query.message,
                          "That button is from an older digest and no longer carries its "
                          "price — run /pricecheck again.")
        return
    if price <= 0:
        return

    listing = dict(item.get("listing") or {})
    if listing:
        listing["price"] = price
        db.update_field(item_id, "listing", listing)
    pricing = dict(item.get("pricing") or {})
    pricing["suggested_price"] = price
    pricing["confidence"] = "reprice"
    pricing["price_basis"] = "reprice"
    db.update_field(item_id, "pricing", pricing)

    offer_id = (item.get("ebay") or {}).get("offer_id")
    if offer_id:
        try:
            await asyncio.to_thread(inventory.update_offer_price, offer_id, price)
        except Exception as e:
            traceback.print_exc()
            util._record_error(f"reprice apply ({item_id[:8]})", e)
            await util._safe_reply(query.message,
                f"⚠️ Saved ${price} locally, but updating eBay failed: {type(e).__name__}: {str(e)[:200]}\n"
                "Use /setprice to retry pushing it to eBay.")
            return
    await util._safe_reply(query.message, f"💲 Repriced {item_id[:8]} to ${price}.")


async def retry_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    item_id, err = util._resolve_item_id(user_id, context.args)
    if err:
        await util._safe_reply(update.message, f"⚠️ {err}")
        return
    await flow._retry_item(user_id, item_id, update.message, context)
