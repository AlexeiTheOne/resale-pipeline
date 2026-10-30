"""Money and sales: /sold, /profit, /report, /receipt, /ross, /scoreboard and
/sync.

Moved verbatim out of telegram_bot.py (docs/cleanup-plan.md Phase 2, step 4b).
`profit` is imported as profit_lib because sold_command and profit_command use
`profit` as a local name.
"""
import asyncio
import re
import tempfile
import time
import traceback
from pathlib import Path

from telegram import Update
from telegram.ext import ContextTypes

import config
import db
import profit as profit_lib
from bot import state, util
from pipeline import sync


def _ross_title(item: dict) -> str:
    """Best short label for an item in an audit list: its listing title, else the
    identified brand+model, else a placeholder."""
    title = (item.get("listing") or {}).get("title")
    if title:
        return title
    ident = item.get("identification") or {}
    guess = " ".join(x for x in (ident.get("brand"), ident.get("model") or ident.get("product_name")) if x)
    return guess or "(no listing yet)"


def _ross_fields(item: dict):
    """(code_digits, code_kind, price) for an item's Ross tag. code_kind is
    'real' (a genuine 12-digit barcode), 'auto' (a placeholder we minted, see
    _next_receipt_code), or 'missing'. price is the reduced/paid Ross price."""
    receipt = item.get("receipt") or {}
    raw = receipt.get("code")
    digits = re.sub(r"\D", "", str(raw)) if raw else ""
    if not digits:
        kind = "missing"
    elif int(digits) < db.REAL_CODE_FLOOR:
        kind = "auto"
    else:
        kind = "real"
    return (digits or None), kind, receipt.get("reduced_price")


async def ross_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Audit Ross tag data — the code and the paid price — across items, so you can
    see at a glance what's filled in and what still needs a /receipt. Groups items
    into those missing the Ross price and those that have it; the code column shows
    a real barcode, an 'auto:' placeholder, or — for none. Rejected items are
    skipped. Usage: /ross (everything) · /ross missing (only unpriced items)."""
    items = [i for i in db.list_items() if i["status"] != "rejected"]
    if not items:
        await util._safe_reply(update.message, "No items yet.")
        return

    missing_only = bool(context.args) and context.args[0].lower() in ("missing", "todo", "unpriced")

    def line(item: dict) -> str:
        digits, kind, price = _ross_fields(item)
        code_str = digits if kind == "real" else f"auto:{digits}" if kind == "auto" else "—"
        price_str = f"${price}" if price is not None else "❌ none"
        return (f"{item['item_id'][:8]} | {price_str:>8} | {code_str:16} | "
                f"{item['status']:10} | {_ross_title(item)[:32]}")

    priced, unpriced, no_real_code = [], [], 0
    for item in sorted(items, key=lambda i: i["created_at"]):
        _, kind, price = _ross_fields(item)
        if kind != "real":
            no_real_code += 1
        (priced if price is not None else unpriced).append(item)

    header = (f"📋 Ross audit — {len(items)} item(s) · {len(unpriced)} missing price · "
              f"{no_real_code} without a real code")
    lines = [header, ""]

    if unpriced:
        lines.append(f"❌ Missing Ross price ({len(unpriced)}) — set with /receipt <id> <price>:")
        lines += [line(i) for i in unpriced]
    if not missing_only and priced:
        if unpriced:
            lines.append("")
        lines.append(f"✅ Priced ({len(priced)}):")
        lines += [line(i) for i in priced]
    if missing_only and not unpriced:
        lines.append("🎉 Every item has a Ross price recorded.")

    await util._send_chunked(update.message, lines)


async def sold_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Mark an item sold and record the sale price, replying with the profit vs.
    what you paid Ross. Usage: /sold [id] [price]. The price is optional (defaults
    to the listed price); a trailing number is taken as the sale price."""
    user_id = update.effective_user.id
    args = list(context.args)

    sale_price = None
    id_args = args
    # Item id first, number second — see _names_an_item. An all-digit id prefix
    # otherwise parses as a price and retargets the command at last_item.
    if args and not util._names_an_item(args[-1]):
        maybe = args[-1].replace("$", "").replace(",", "")
        try:
            sale_price = round(float(maybe), 2)
            id_args = args[:-1]
        except ValueError:
            sale_price = None  # trailing arg wasn't a number — treat it all as the id
        if sale_price is not None and sale_price <= 0:
            await util._safe_reply(update.message, f"'{args[-1]}' isn't a valid sale price.")
            return

    item_id, err = util._resolve_item_id(user_id, id_args)
    if err:
        await util._safe_reply(update.message, f"⚠️ {err}")
        return
    item = db.get_item(item_id)
    if item is None:
        await util._safe_reply(update.message, f"No item found for {item_id[:8]}.")
        return

    ebay = dict(item.get("ebay") or {})
    if sale_price is not None:
        ebay["sale_price"] = sale_price
        # A hand-typed price contradicts every figure /sync derived from the
        # order it replaces. Leaving them behind mixes one order's fees and unit
        # count with another order's revenue, and `fees_known` would keep
        # presenting the result as measured. Drop them and fall back to the
        # modelled rates until the next /sync supplies real ones.
        for derived in ("net_proceeds", "ebay_fees", "shipping_collected",
                        "shipping_label_cost", "units_sold", "order_id",
                        "order_ids", "fees_known", "ad_fees", "refunded",
                        "gross_sale_price", "first_sold_at", "sales"):
            ebay.pop(derived, None)
        db.update_field(item_id, "ebay", ebay)
    db.update_status(item_id, "sold")
    state.last_item[user_id] = item_id

    # Re-read so _sale_profit sees the just-saved sale price.
    paid, sale, profit = profit_lib.sale_profit(db.get_item(item_id))
    msg = f"✅ Marked {item_id[:8]} sold"
    if sale is not None:
        msg += f" for ${sale}"
    if paid is not None:
        msg += f" · paid ${paid}"
    if profit is not None:
        msg += f" · profit ${profit} (before fees)"
    await util._safe_reply(update.message, msg)


async def _item_profit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """One item's money, line by line — /profit <id>."""
    user_id = update.effective_user.id
    item_id, err = util._resolve_item_id(user_id, context.args)
    if err:
        await util._safe_reply(update.message, f"⚠️ {err}")
        return
    item = db.get_item(item_id)
    if item is None:
        await util._safe_reply(update.message, f"No item found for {item_id[:8]}.")
        return
    state.last_item[user_id] = item_id

    projection = profit_lib.project(item)
    if projection is None:
        await util._safe_reply(update.message,
            f"{item_id[:8]} has no price yet (status '{item['status']}') — nothing to compute.")
        return
    title = ((item.get("listing") or {}).get("title") or "(untitled)")[:48]
    verb = "Realized" if projection["actual"] else "Projected"
    margin = (f" · {projection['margin'] * 100:.0f}% margin"
              if projection["margin"] is not None else "")
    header = (f"💰 {item_id[:8]} · {title}\n"
              f"{verb}: ${projection['net']:.2f} net{margin}")
    sent = await util._safe_reply(update.message, profit_lib.format_projection(projection, header=header))
    util._remember_target(sent, item_id)


async def profit_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/profit summarizes every sold item: per-item paid → sold → fees → net →
    margin, plus the totals. Uses REAL eBay fees for items /sync has pulled an
    order for; items without one fall back to a gross number (fees unknown).
    Run /sync first to fill in real fees.

    /profit <id> instead breaks ONE item down line by line — the same figures
    posted when it went live, so you can pull them back up after a price change
    without waiting for a /report."""
    if context.args:
        await _item_profit(update, context)
        return

    # Anything that has actually taken money, not just anything flagged 'sold'. A
    # partially-sold listing goes back to 'published' once /sync confirms it still
    # has stock live, and its realized sale must not vanish from the totals with it.
    sold = [i for i in db.list_items()
            if i["status"] == "sold" or (i.get("ebay") or {}).get("sale_price") is not None]
    if not sold:
        await util._safe_reply(update.message, "No sold items yet. Run /sync to pull sold orders, "
                          "or mark one by hand with /sold <id> <price>.")
        return

    total_cost = total_sale = total_net = 0.0
    total_fees = 0.0
    any_gross = False  # at least one item still on a fees-unknown estimate
    lines = ["💵 Profit — sold items:", ""]
    for item in sorted(sold, key=lambda x: x["created_at"]):
        e = profit_lib.sale_economics(item)
        paid, sale, profit = e["paid"], e["sale"], e["profit"]
        title = ((item.get("listing") or {}).get("title") or "(untitled)")[:32]
        total_cost += paid or 0
        total_sale += sale or 0
        total_net += e["proceeds"] or 0

        margin = f"{e['margin'] * 100:.0f}%" if e["margin"] is not None else "—"
        if e["fees_known"]:
            total_fees += e["fees"] or 0
            lines.append(f"{item['item_id'][:8]} · paid ${paid} · sold ${sale} · "
                         f"fees ${e['fees']} · net ${e['net']} · +${profit} ({margin}) · {title}")
        else:
            any_gross = True
            lines.append(f"{item['item_id'][:8]} · paid ${paid} · sold ${sale} · "
                         f"+${profit} ({margin}, gross — fees unknown) · {title}")

    total_profit = round(total_net - total_cost, 2)
    lines += ["", f"TOTAL: sold ${round(total_sale, 2)} − eBay fees ${round(total_fees, 2)} "
                  f"− cost ${round(total_cost, 2)} = ${total_profit} across {len(sold)} item(s)"]
    if any_gross:
        lines.append("Some items show a gross estimate (no eBay order pulled yet) — run /sync "
                     "to fill in real fees and net.")
    await util._send_chunked(update.message, lines)


async def sync_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Reconcile the local DB with eBay: walk every item that has an eBay offer,
    pull its current price and quantity from eBay, and write back anything that
    changed (e.g. a price or quantity you edited by hand in Seller Hub). Also pulls
    real sold orders — an item eBay has an order for is marked sold automatically
    with the actual sale price and eBay fees (so /profit shows real net/margin).
    Listings that ended without a matching order are flagged for a manual /sold.
    Usage: /sync [days] (order look-back window, default 90)."""
    days = _sync_days(context.args)

    async def _ping(text: str) -> None:
        await util._safe_reply(update.message, text)

    lines, _ = await _run_sync(days, on_progress=_ping)
    await util._send_chunked(update.message, lines)


async def _run_sync(days: int, on_progress=None) -> tuple[list[str], list[str]]:
    """pipeline.sync.run_sync, with caught errors going to /errors."""
    return await sync.run_sync(days, on_progress, on_error=util._record_error)


def _sync_days(args) -> int:
    """Order look-back for a /sync run: first positional arg, else the default."""
    if args:
        try:
            return max(1, int(args[0]))
        except (TypeError, ValueError):
            pass
    return config.SYNC_DAYS


async def report_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Build an Excel profit report — cover photo, title, price, shipping, Ross
    cost, and profit net of eBay fees + the ad rate — and send it as a file. It
    reflects the current database each time, and the fee/shipping assumptions are
    editable cells in the sheet that recalc every row."""
    # Optional look-back for the account-level charge reconciliation under the
    # table (Promoted Listings). Everything else in the report is local data.
    ad_days = 90
    if context.args:
        try:
            ad_days = max(1, int(context.args[0]))
        except ValueError:
            pass
    await util._safe_reply(update.message, "📊 Building the profit report...")
    import report as report_mod

    out = Path(tempfile.gettempdir()) / f"ross_report_{int(time.time())}.xlsx"
    try:
        await asyncio.to_thread(report_mod.build_report, str(out), ad_days)
    except Exception as e:
        traceback.print_exc()
        util._record_error("report build", e)
        await util._safe_reply(update.message, f"⚠️ Report failed: {type(e).__name__}: {str(e)[:200]}")
        return

    try:
        with open(out, "rb") as f:
            await update.message.reply_document(
                document=f, filename="ross_profit_report.xlsx",
                caption="Profit report — edit the yellow fee/shipping cells to recalc.")
    except Exception as e:
        traceback.print_exc()
        util._record_error("report send", e)
        await util._safe_reply(update.message, f"⚠️ Built the report but couldn't send it: {type(e).__name__}: {str(e)[:200]}")
    finally:
        try:
            out.unlink()
        except OSError:
            pass


_NO_CODE_WORDS = {"none", "n", "-", "x", "skip", "na", "n/a"}


async def receipt_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Manually set the Ross cost (paid price) — and optionally the 12-digit tag
    code — on an item, for when the barcode couldn't be decoded. Does not affect
    the eBay price. The code is optional: leave it off (or type 'none') and the
    bot fills in the next sequential code so the row lines up with the others.

    Usage: /receipt <price>                     (auto-generate the code)
           /receipt <price> none                (same — explicit)
           /receipt <price> <code>              (real 12-digit tag code)
           /receipt <item_id> <price> [code]    (explicit item)"""
    user_id = update.effective_user.id
    args = list(context.args)
    if not args:
        await util._safe_reply(update.message,
            "Usage: /receipt [item_id] <price> [code]\n"
            "e.g. /receipt 9.99 400286461425   ·   /receipt 9.99   (auto code)")
        return

    # Peel an optional trailing code off the end. A bare 'none' means "generate
    # one"; a run of ≥8 digits is a real tag-code attempt (prices are far shorter,
    # so this can't swallow the price); anything else is the price → auto-generate.
    code = None
    auto = True
    tail = args[-1]
    tail_digits = re.sub(r"\D", "", tail)
    if tail.lower() in _NO_CODE_WORDS:
        args = args[:-1]
    elif tail_digits and len(tail_digits) >= 8 and "." not in tail:
        if len(tail_digits) != 12:
            await util._safe_reply(update.message,
                f"⚠️ A tag code must be 12 digits (got {len(tail_digits)} from '{tail}'). "
                "Fix it, or type 'none' to auto-generate one.")
            return
        code, auto = tail_digits, False
        args = args[:-1]

    if not args:
        await util._safe_reply(update.message, "Usage: /receipt [item_id] <price> [code]  (e.g. /receipt 9.99)")
        return

    price_str = args[-1]
    item_id, err = util._resolve_item_id(user_id, args[:-1])
    if err:
        await util._safe_reply(update.message, f"⚠️ {err}")
        return

    try:
        price = float(price_str.replace("$", "").replace(",", ""))
    except ValueError:
        await util._safe_reply(update.message, f"⚠️ '{price_str}' isn't a valid price.")
        return

    item = db.get_item(item_id)
    if item is None:
        await util._safe_reply(update.message, f"No item found for {item_id[:8]}.")
        return

    if auto:
        code = db.next_receipt_code()

    receipt = dict(item.get("receipt") or {})
    receipt.update({"reduced_price": price, "code": code,
                    "source": "manual", "code_generated": auto})
    # Writing a receipt is not the same as removing the tag photo. Say so
    # explicitly, or _split_receipt reads this as "already handled" and the tag
    # showing what you paid stays in `photos` all the way to eBay. Preserve an
    # existing True — re-pricing an item whose tag was already peeled must not
    # queue up a second peel.
    receipt.setdefault("peeled", False)
    db.update_field(item_id, "receipt", receipt)
    state.last_item[user_id] = item_id
    tag = " (auto)" if auto else ""
    await util._safe_reply(update.message, f"🧾 Saved for {item_id[:8]}: paid ${price:.2f} · code {code}{tag}")


async def scoreboard_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """What to buy: per brand and category sell-through, speed, capital tied up
    and a buy more / ok / pause / stop verdict. Local data only."""
    from pipeline import scoreboard
    try:
        lines = scoreboard.format_scoreboard(await asyncio.to_thread(scoreboard.load))
    except Exception as e:
        traceback.print_exc()
        util._record_error("scoreboard", e)
        await util._safe_reply(update.message, f"⚠️ Scoreboard failed: {type(e).__name__}: {str(e)[:200]}")
        return
    await util._send_chunked(update.message, lines)
