"""eBay -> local DB reconciliation (/sync and the nightly job).

Moved verbatim out of telegram_bot.py (docs/cleanup-plan.md Phase 2). Progress
goes out through `on_progress`, caught errors through `on_error`; nothing here
imports telegram_bot or bot.*.
"""
import asyncio
import traceback
from datetime import datetime, timezone

import db
from ebay import inventory, listings, orders


def differs(a, b) -> bool:
    """Whether two stored sale values differ, to the cent for numbers."""
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return abs(a - b) > 0.005
    return a != b


def sync_one(item: dict, sold_map: dict | None = None) -> dict | None:
    """Pull one item's live eBay state (price, quantity, listing status) and write
    any changes into the local DB. Runs in a worker thread (blocking eBay call).
    Returns a summary dict for the report, or None if the offer couldn't be read.

    `sold_map` is ebay.orders.sales_by_item()'s result (keyed by sku==item_id and
    by listing_id): when this item appears in it, the real sale price and eBay
    fees are recorded and the item is marked sold automatically — no manual
    /sold needed."""
    item_id = item["item_id"]
    ebay = dict(item.get("ebay") or {})
    offer_id = ebay.get("offer_id")

    offer = inventory.get_offer(offer_id)

    changes = []

    # Price: eBay is the source of truth for a manual Seller-Hub edit.
    ebay_price = (offer.get("pricingSummary") or {}).get("price") or {}
    ebay_price_val = ebay_price.get("value")
    if ebay_price_val is not None:
        new_price = round(float(ebay_price_val), 2)
        old_price = (item.get("listing") or {}).get("price")
        if old_price is None or round(float(old_price), 2) != new_price:
            listing = dict(item.get("listing") or {})
            if listing:
                listing["price"] = new_price
                db.update_field(item_id, "listing", listing)
            pricing = dict(item.get("pricing") or {})
            pricing["suggested_price"] = new_price
            pricing["price_basis"] = "ebay_sync"
            db.update_field(item_id, "pricing", pricing)
            changes.append(f"price ${old_price}→${new_price}")

    listing_info = offer.get("listing") or {}
    listing_status = listing_info.get("listingStatus")

    # Quantity: default assumption is 1 (what every listing is created with).
    # Not from an ENDED listing: its offer still carries the old stock count, so
    # copying it in and then zeroing it below flipped the same items 0→1→0 and
    # reported them on every daily sync.
    ebay_qty = offer.get("availableQuantity")
    if ebay_qty is not None and listing_status != "ENDED":
        old_qty = ebay.get("quantity", 1)
        if int(old_qty) != int(ebay_qty):
            ebay["quantity"] = int(ebay_qty)
            db.update_field(item_id, "ebay", ebay)
            changes.append(f"qty {old_qty}→{ebay_qty}")

    # Real sale from the Fulfillment/Finances APIs: if this item shows up in the
    # sold_map, record the actual sale price + eBay fees + net and mark it sold
    # outright — no manual /sold guess. Matched by sku (== item_id) first, then by
    # the stored listing_id.
    #
    # NOT skipped when the item is already sold, which is how later sales used to
    # go missing: a multi-quantity listing sells a unit at a time and a relist
    # sells again under the same sku, so "already sold once" is the START of the
    # sales history, not the end of it. The sold_map figures are aggregates across
    # every order for this item, and order_ids is what makes re-runs idempotent —
    # nothing is written unless an order we haven't seen shows up.
    auto_sold = False
    sale = None
    if sold_map:
        sale = sold_map.get(item_id)
        if sale is None and ebay.get("listing_id"):
            sale = sold_map.get(str(ebay["listing_id"]))

    new_orders = []
    known: set = set()
    if sale is not None:
        known = set(ebay.get("order_ids") or [])
        if not known and ebay.get("order_id"):
            known = {ebay["order_id"]}   # recorded before order_ids existed
        new_orders = [oid for oid in (sale.get("order_ids") or []) if oid not in known]

    # `orders.sales_by_item(days)` only sees orders INSIDE the look-back window. A row
    # that stores its per-order records (`sales`) can be folded additively: the
    # window's records replace those orders, older ones keep their stored record.
    # A row from before per-order records existed can't be, so a window that
    # doesn't cover all of its orders would erase money already recorded; refuse
    # that write, as before.
    incoming = set((sale or {}).get("order_ids") or [])
    stored_sales = ebay.get("sales")
    totals = sale
    if sale is not None and sale.get("sales") is not None and (
            stored_sales is not None or not (known - incoming)):
        totals = orders.totals_from_sales(orders.fold_sales(stored_sales, sale["sales"]))
        missing_from_window = set()
    else:
        missing_from_window = (known - incoming) if sale is not None else set()
    # Only worth saying when there's a NEW order the window can't fold in. With
    # nothing new, the stored figures are already complete and the warning just
    # repeated itself every day.
    if missing_from_window and new_orders:
        changes.append(
            f"⚠️ sale figures NOT updated — this look-back sees {len(incoming)} of "
            f"its {len(known | incoming)} orders, so writing them would erase "
            f"${ebay.get('sale_price', 0)} already recorded. Re-run /sync with a "
            f"wider window (e.g. /sync 365) to fold in the newer ones.")

    # Only on an order we haven't recorded. It used to also fire whenever the
    # status wasn't 'sold' — but a multi-quantity or relisted item with stock
    # left is put back to 'published' by the live-listing pass below, so every
    # daily sync re-marked it sold and the next pass un-marked it again.
    new_sale = None
    if totals is not None and not missing_from_window and new_orders:
        # How many units the money covers (units_sold): the report charges the
        # Ross cost per unit sold.
        ebay.update(orders.sale_db_fields(totals))
        db.update_field(item_id, "ebay", ebay)
        net = totals["net_proceeds"] if totals["fees_known"] else None
        new_sale = {"orders": len(new_orders), "total": totals["sale_price"], "net": net}
        if item["status"] != "sold":
            db.update_status(item_id, "sold")
            auto_sold = True
            changes.append(f"SOLD ${totals['sale_price']}" + (f" · net ${net}" if net is not None else ""))
        else:
            changes.append(f"+{len(new_orders)} later sale(s) → {ebay['units_sold']} unit(s) "
                           f"total ${totals['sale_price']}" + (f" · net ${net}" if net is not None else ""))
    elif (totals is not None and not missing_from_window and totals.get("fees_known")
          and ebay.get("sale_price") is not None):
        # No new order, but the money on orders already recorded can still move:
        # a refund, or a Promoted Listings fee, posts days after the sale. Update
        # the figures quietly. This is not a new sale: no status change, nothing
        # for the scheduled brief, just a line in the full /sync report.
        fresh = orders.sale_db_fields(totals)
        if any(differs(ebay.get(k), fresh.get(k)) for k in orders.SALE_MONEY_FIELDS):
            old_net, old_refund = ebay.get("net_proceeds"), ebay.get("refunded") or 0
            ebay.update(fresh)
            db.update_field(item_id, "ebay", ebay)
            note = (f"refund ${round(fresh['refunded'] - old_refund, 2)} · "
                    if (fresh.get("refunded") or 0) > old_refund + 0.005 else "")
            changes.append(f"{note}sale figures updated, net ${old_net}→${fresh['net_proceeds']}")

    try:
        sold_qty = int(listing_info.get("soldQuantity") or 0)
    except (TypeError, ValueError):
        sold_qty = 0

    # An ended listing holds no live stock, whatever quantity the offer still
    # records. Zero it, or the report keeps counting merchandise that isn't for
    # sale — a listing ended after selling one unit was still projecting its
    # remaining units as inventory.
    if listing_status == "ENDED" and (ebay.get("quantity") or 0) != 0:
        ebay["quantity"] = 0
        db.update_field(item_id, "ebay", ebay)
        changes.append("ended on eBay → qty 0")

    ended = (not auto_sold and item["status"] == "published"
             and (listing_status in ("ENDED", "OUT_OF_STOCK") or ebay_qty == 0))
    # "Ended" alone says nothing about whether it SOLD. eBay reports
    # soldQuantity, so use it: a listing that ended with soldQuantity 0 and stock
    # still on it expired or was ended by hand, and wants relisting — telling the
    # user to record a sale sends them to invent one that never happened.
    likely_sold = ended and sold_qty > 0
    ended_unsold = ended and sold_qty == 0
    return {"item_id": item_id, "changes": changes, "auto_sold": auto_sold,
            "new_sale": new_sale,
            "likely_sold": likely_sold, "ended_unsold": ended_unsold,
            "listing_status": listing_status}


def adopt_listing(row: dict) -> str:
    """Create a local item for a live eBay listing the bot didn't make.

    It gets status 'published' and the listing's real title/price/quantity, so it
    counts in /status, /report, /profit and the weekly price check immediately.
    What it can't have is a Ross cost (no receipt was ever scanned) or photos —
    both are marked, and the report already flags a missing cost in orange rather
    than quietly reporting the full sale price as profit."""
    item_id = db.create_item([])
    db.update_field(item_id, "listing", {
        "title": row["title"],
        "price": row["price"],
        "description": None,
    })
    db.update_field(item_id, "ebay", {
        "listing_id": row["listing_id"],
        "view_item_url": row["view_item_url"],
        "sku": row["sku"],
        "quantity": row["quantity"],
        # Marks this as adopted rather than built here: there are no photos, no
        # identification and no offer_id, so the pipeline must not try to redraft
        # or re-push it.
        "source": "external",
        "published_at": datetime.now(timezone.utc).isoformat(),
    })
    db.update_status(item_id, "published")
    return f"{item_id[:8]} ${row['price']} {row['title'][:40]}"


# One reconciliation at a time. The nightly job and a hand-typed /sync can now
# land together — and concurrent_updates(True) means two /syncs can overlap
# anyway — which would let both passes see the same un-adopted listing and adopt
# it twice, or interleave writes to the same item.
_sync_lock = asyncio.Lock()


async def run_sync(days: int, on_progress=None,
                   on_error=lambda _where, _exc: None) -> tuple[list[str], list[str]]:
    """The whole /sync reconciliation, as a function that RETURNS its report
    lines instead of replying to a chat.

    Split out of sync_command so the nightly job (_daily_sync_loop) runs exactly
    the same reconciliation the manual command does, rather than a second,
    drifting copy of it. `on_progress` is an optional async callback for the
    interactive path's status ping; the scheduled path passes none.
    `on_error(where, exc)` records a caught failure (the bot passes its /errors
    buffer); the default drops it.

    Returns (full report, brief). The brief is what the scheduled run posts:
    new sales and things that need a decision, nothing else — empty when there's
    nothing worth a notification. /sync shows the full report.

    Serialized on _sync_lock: a second caller waits rather than running a
    concurrent pass over the same items."""
    if _sync_lock.locked() and on_progress:
        await on_progress("⏳ A sync is already running — waiting for it to finish...")
    async with _sync_lock:
        return await run_sync_locked(days, on_progress, on_error)


async def run_sync_locked(days: int, on_progress=None,
                          on_error=lambda _where, _exc: None) -> tuple[list[str], list[str]]:
    items = [
        i for i in db.list_items()
        if (i.get("ebay") or {}).get("offer_id")
        and i["status"] in ("ebay_draft", "published", "sold")
    ]
    if not items:
        return ["Nothing to sync — no items have an eBay offer yet."], []

    if on_progress:
        await on_progress(f"🔄 Syncing {len(items)} item(s) from eBay...")

    # Pull real sold orders once (keyed by sku==item_id and listing_id) and hand
    # the map to every sync_one. A missing fulfillment/finances scope shouldn't
    # break price/qty sync, so degrade to an empty map and note it in the report.
    sold_map, sold_map_err = {}, None
    try:
        sold_map = await asyncio.to_thread(orders.sales_by_item, days)
    except Exception as e:
        traceback.print_exc()
        on_error("sync (orders)", e)
        sold_map_err = f"{type(e).__name__}: {str(e)[:150]}"

    updated, auto_sold, sold_flags, errors, ended_flags = [], [], [], [], []
    new_sales = []   # (item, new_sale) — for the scheduled brief
    for item in items:
        try:
            result = await asyncio.to_thread(sync_one, item, sold_map)
        except Exception as e:
            traceback.print_exc()
            on_error(f"sync ({item['item_id'][:8]})", e)
            errors.append(f"{item['item_id'][:8]}: {type(e).__name__}: {str(e)[:120]}")
            continue
        if result.get("new_sale"):
            new_sales.append((item, result["new_sale"]))
        if result["auto_sold"]:
            auto_sold.append(f"{result['item_id'][:8]}: {', '.join(result['changes'])}")
        elif result["changes"]:
            updated.append(f"{result['item_id'][:8]}: {', '.join(result['changes'])}")
        if result["likely_sold"]:
            sold_flags.append(result["item_id"])
        if result.get("ended_unsold"):
            ended_flags.append(result["item_id"])

    # Reconcile against every listing actually live on eBay, not just the ones we
    # already know about. Listings made by hand in Seller Hub never touch the
    # Inventory API, so without this pass they're invisible to /status, /report,
    # /profit and the weekly price check. A relist also mints a NEW listing id,
    # which leaves the stored one pointing at a dead listing (and the price check
    # reading traffic for the wrong thing).
    adopted, relinked, live_mismatch, discover_err = [], [], [], None
    relisted_live = []   # were 'sold' locally but still have stock live on eBay
    live_item_ids: set[str] = set()
    try:
        live = await asyncio.to_thread(listings.get_active_listings)
        live_item_ids = {row["sku"] for row in live if row["sku"]}
        all_items = db.list_items()
        by_sku = {i["item_id"]: i for i in all_items}
        # Second index, on the eBay listing id, because the sku index can't reach
        # an adopted listing: one made by hand in Seller Hub has no SKU at all, so
        # `by_sku.get(row["sku"])` misses, `known_ids` then skipped the row as
        # already-adopted, and sync_one never touched it either (that loop
        # requires an offer_id, which a hand-made listing doesn't have). Every
        # adopted listing was therefore frozen at whatever price and quantity it
        # had on the day it was adopted, permanently — and adopt_listing exists
        # precisely so those listings COUNT in /status, /report and /profit.
        by_listing_id = {}
        for i in all_items:
            lid = (i.get("ebay") or {}).get("listing_id")
            if lid:
                by_listing_id[str(lid)] = i
        known_ids = set(by_listing_id)
        for row in live:
            item = by_sku.get(row["sku"]) if row["sku"] else None
            if item is None:
                item = by_listing_id.get(str(row["listing_id"]))
            if item is not None:
                ebay_data = dict(item.get("ebay") or {})
                # Price, for the listings sync_one can't reach. It owns this for
                # anything with an offer_id and has already run by now; this fills
                # the gap for adopted listings, whose only link to eBay is the row
                # in hand.
                if not ebay_data.get("offer_id") and row["price"] is not None:
                    new_price = round(float(row["price"]), 2)
                    listing = dict(item.get("listing") or {})
                    old_price = listing.get("price")
                    if old_price is None or round(float(old_price), 2) != new_price:
                        listing["price"] = new_price
                        db.update_field(item["item_id"], "listing", listing)
                        updated.append(f"{item['item_id'][:8]}: price ${old_price}→${new_price} (eBay)")
                changed = False
                if str(ebay_data.get("listing_id") or "") != str(row["listing_id"]):
                    ebay_data["listing_id"] = row["listing_id"]
                    ebay_data["view_item_url"] = row["view_item_url"]
                    changed = True
                    relinked.append(f"{item['item_id'][:8]} → {row['listing_id']}")
                # Take the LIVE quantity from here. sync_one reads it from the
                # stored offer_id, which after a relist points at the dead offer
                # and reports 0 — so an item that sold one unit and still has
                # stock looked like it had none, and its remaining inventory
                # stopped being counted anywhere. row["quantity"] is AVAILABLE
                # stock (ebay/listings.py nets off QuantitySold); 0 is a real
                # value and must be written, not treated as "no data".
                if row["quantity"] is not None and ebay_data.get("quantity") != row["quantity"]:
                    ebay_data["quantity"] = row["quantity"]
                    changed = True
                if changed:
                    db.update_field(item["item_id"], "ebay", ebay_data)
                # eBay says this listing is live and the local status says sold.
                # Both can be true at once — a multi-quantity listing that sold one
                # unit, or a relist — and the status is the half that's wrong,
                # because it describes the LISTING while the sale lives in its own
                # ebay fields. So put the status back to what eBay demonstrably
                # says, which loses no sales history: sale_price, order_ids and
                # units_sold are untouched, and the report still counts the money.
                #
                # Only when stock is actually available. Sold out but still active
                # (eBay leaves a listing up briefly) stays 'sold'.
                if item["status"] != "published":
                    if item["status"] == "sold" and row["quantity"] > 0:
                        db.update_status(item["item_id"], "published")
                        relisted_live.append(
                            f"{item['item_id'][:8]} — {row['sold_quantity']} sold, "
                            f"{row['quantity']} still live at ${row['price']} → back to 'published'")
                    else:
                        live_mismatch.append(
                            f"{item['item_id'][:8]} is '{item['status']}' locally but LIVE "
                            f"at ${row['price']} with {row['quantity']} available")
                continue
            if row["listing_id"] in known_ids:
                continue
            adopted.append(adopt_listing(row))
    except Exception as e:
        traceback.print_exc()
        on_error("sync discover", e)
        discover_err = f"{type(e).__name__}: {str(e)[:150]}"

    lines = [f"✅ Sync done — checked {len(items)} item(s) (orders: last {days}d)."]
    if adopted:
        lines += ["", f"🆕 Adopted {len(adopted)} listing(s) made outside the bot:"]
        lines += [f"  {a}" for a in adopted]
        lines.append("  (no photos or Ross cost locally — /receipt <id> <price> <code> "
                     "to make their profit real)")
    if relinked:
        lines += ["", f"🔗 Re-linked {len(relinked)} relisted item(s):"] + [f"  {x}" for x in relinked]
    if relisted_live:
        lines += ["", f"♻️ Sold but still selling ({len(relisted_live)}) — stock left on the "
                  "listing, so these are live inventory again (the recorded sale is kept):"]
        lines += [f"  {x}" for x in relisted_live]
    if live_mismatch:
        lines += ["", f"❓ Live on eBay but not 'published' here ({len(live_mismatch)}) — "
                  "no stock available, so the status is left alone. /end it if it's gone:"]
        lines += [f"  {x}" for x in live_mismatch]
    if discover_err:
        lines += ["", f"⚠️ Couldn't enumerate live listings ({discover_err})"]
    if auto_sold:
        lines += ["", f"🟢 Recorded sold from eBay orders ({len(auto_sold)}):"] + [f"  {a}" for a in auto_sold]
    if updated:
        lines += ["", f"📝 Updated ({len(updated)}):"] + [f"  {u}" for u in updated]
    elif not auto_sold:
        lines.append("No price/quantity changes found.")
    # A relisted item's OLD offer reads as ended/out-of-stock, which is exactly
    # the "likely sold" signal — but the item is demonstrably still live under a
    # new listing id. Drop those: reporting a live listing as probably-sold sends
    # you off to record a sale that never happened.
    sold_flags = [sid for sid in sold_flags if sid not in live_item_ids]
    ended_flags = [sid for sid in ended_flags if sid not in live_item_ids]
    if ended_flags:
        lines += ["", f"🔁 Ended WITHOUT selling ({len(ended_flags)}) — eBay reports "
                  "soldQuantity 0 with stock left. Relist with /activate <id>:"]
        lines += [f"  {sid[:8]}" for sid in ended_flags]
    if sold_flags:
        lines += ["", "🛒 Likely sold (ended/out of stock, no order matched) — record with "
                  "/sold <id> <price>:"]
        lines += [f"  {sid[:8]}" for sid in sold_flags]
    if sold_map_err:
        lines += ["", f"⚠️ Couldn't read eBay orders ({sold_map_err})",
                  "   → if this is a scope/403 error, re-run `python -m ebay.auth` to grant "
                  "sell.fulfillment.readonly + sell.finances."]
    if errors:
        lines += ["", f"⚠️ Couldn't read ({len(errors)}):"] + [f"  {e}" for e in errors]

    brief = []
    for item, sale in new_sales:
        title = ((item.get("listing") or {}).get("title") or "(untitled)")[:45]
        net = f" · net ${sale['net']}" if sale.get("net") is not None else ""
        brief.append(f"💰 Sold: {title} — ${sale['total']}{net}")
    todo = []
    if ended_flags:
        todo.append(f"🔁 {len(ended_flags)} ended without selling — relist with /activate: "
                    + ", ".join(sid[:8] for sid in ended_flags))
    if sold_flags:
        todo.append(f"🛒 {len(sold_flags)} look sold but no order matched — /sold <id> <price>: "
                    + ", ".join(sid[:8] for sid in sold_flags))
    if live_mismatch:
        todo.append(f"❓ {len(live_mismatch)} live on eBay but not 'published' here — see /sync")
    if adopted:
        todo.append(f"🆕 Picked up {len(adopted)} listing(s) made in Seller Hub — see /sync")
    if errors or discover_err or sold_map_err:
        todo.append("⚠️ Some eBay data couldn't be read — run /sync for details")
    if todo:
        brief += ([""] if brief else []) + todo
    return lines, brief
