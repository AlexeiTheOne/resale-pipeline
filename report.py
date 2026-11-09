"""Generate an Excel profit report from the item database.

One row per listed item: cover photo, title, our price, shipping, Ross cost, and
a Net profit column that nets out eBay's final value fee, the Promoted Listings
ad rate, and shipping. The fee/shipping assumptions are editable cells at the top
and the per-row math is written as Excel FORMULAS, so changing an assumption
recalculates every row live in Excel.

Run standalone (`python report.py`) to write report.xlsx, or use the bot's
/report command, which builds this and sends it to you on Telegram.
"""
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from openpyxl import Workbook
from openpyxl.drawing.image import Image as XLImage
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

import profit
from config import (
    ASSUMED_COST_RATIO, EBAY_AD_FEE_PCT, EBAY_FIXED_FEE, EBAY_FVF_PCT,
    EBAY_SHIP_CHARGED, EBAY_SHIP_COST,
)
from db import list_items

try:
    from PIL import Image, ImageOps
except ImportError:
    Image = None
    ImageOps = None

# Items with a real listing price — everything from drafting onward.
_REPORTABLE = {"drafted", "review", "approved", "ebay_draft", "published", "sold"}

MONEY = "$#,##0.00"
PCT = "0.00%"
THUMB_PX = 92

# Assumption cell addresses (column C, rows 4-8) — referenced by the row formulas.
A_FVF, A_FIXED, A_AD, A_SHIP_CHG, A_SHIP_COST = "$C$4", "$C$5", "$C$6", "$C$7", "$C$8"
A_COST_RATIO = "$C$9"   # assumed Ross cost as a share of list price, when unknown


def _price_for(item):
    """Sale price if the item sold, else its listed price.

    NOTE this is a LINE TOTAL on a sold row — ebay/orders.py accumulates
    `sale_price` across every unit and every order for the sku. Use
    `_unit_price_for` for anything that then multiplies by quantity."""
    ebay = item.get("ebay") or {}
    if ebay.get("sale_price") is not None:
        return ebay["sale_price"]
    return (item.get("listing") or {}).get("price")


def _unit_price_for(item, qty: int) -> float:
    """What ONE unit of this row fetched — the figure column F claims to hold.

    A still-listed row already stores a per-unit price, so it passes through.
    A sold row stores the realized total for `units_sold` units, so it has to be
    divided back down: every downstream formula multiplies column F by column E
    (`=F*E` for revenue, the ad fee off that, the receipt-less cost cell off
    both), and feeding a line total into that reports a two-unit sale at twice
    the money that came in."""
    price = round(float(_price_for(item)), 2)
    if item["status"] == "sold" and (item.get("ebay") or {}).get("sale_price") is not None:
        return round(price / max(qty, 1), 2)
    return price


def _report_qty(item) -> int:
    """How many units this row represents.

    For a still-listed item it's the offer's available quantity (what /setqty
    pushed to eBay) — a listing of 5 should project 5× the per-unit profit.

    For a SOLD row it's the number of units that sale actually covered, because
    `sale_price` is the realized total for all of them: one order here took 2
    units at $72.96, and charging a single Ross cost against it overstated the
    profit by a whole unit's cost. Defaults to 1 (the norm — Ross items are
    near-always unique single units) when nothing recorded the count."""
    if item["status"] == "sold":
        try:
            return max(int((item.get("ebay") or {}).get("units_sold") or 1), 1)
        except (TypeError, ValueError):
            return 1
    try:
        q = int((item.get("ebay") or {}).get("quantity", 1))
    except (TypeError, ValueError):
        q = 1
    # A recorded 0 is a fact, not a missing value: /sync zeroes the quantity on a
    # listing eBay says has ENDED. Coercing it back to 1 kept ended listings
    # projecting a unit of revenue and profit they can no longer make, and the
    # 'LIVE (projected)' band is the number inventory decisions get made on. An
    # absent or unreadable quantity still defaults to 1 above, so this only ever
    # sees a number something actually wrote.
    return max(q, 0)


# --- where a cost came from, and when things happened -----------------------------
# receipt: a Ross receipt line has this item's code at this price (the receipt
#          pipeline's ReceiptItems, SQL Server only);
# tag:     read off the price tag (barcode, OCR, photo, home-inventory read);
# typed:   entered by hand (/receipt, the owner);
# assumed: no Ross price at all, so the cost is ASSUMED_COST_RATIO x list price.
COST_SOURCES = ("receipt", "tag", "typed", "assumed")
_TYPED = ("manual", "owner")


def receipt_prices(items) -> set:
    """{(sku, price)} of every receipt line carrying one of these items' codes."""
    import db
    codes = set()
    for i in items:
        r = i.get("receipt") or {}
        codes |= {c for c in (r.get("code"), r.get("main_code")) if c}
    try:
        lines = db.receipt_lines_for_skus(codes)
    except Exception as e:      # the report must not fail over receipts
        print(f"WARNING: receipt lines unavailable for the report: {type(e).__name__}: {e}")
        return set()
    return {(ln["sku"], round(float(ln["price"]), 2)) for ln in lines if ln.get("price") is not None}


def cost_source(item, receipts: set = frozenset()) -> str:
    r = item.get("receipt") or {}
    price = r.get("reduced_price")
    if price is None:
        return "assumed"
    price = round(float(price), 2)
    for code in (r.get("code"), r.get("main_code")):
        # a "$21.99B" line is sometimes stored a cent high (22.00)
        if code and any((code, round(price + d, 2)) in receipts for d in (0, 0.01, -0.01)):
            return "receipt"
    return "typed" if r.get("source") in _TYPED or not r.get("source") else "tag"


def _when(value):
    if not value:
        return None
    try:
        t = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def sold_on(item):
    """When the first unit sold (UTC datetime), or None."""
    e = item.get("ebay") or {}
    return _when(e.get("first_sold_at") or e.get("sold_at"))


def days_listed(item, now=None):
    """Days from publishing to the first sale; still listed: to now."""
    pub = _when((item.get("ebay") or {}).get("published_at"))
    if pub is None:
        return None
    end = sold_on(item) if item.get("status") == "sold" else None
    return max(0, ((end or now or datetime.now(timezone.utc)) - pub).days)


def recent_sales(items, days: int = 7, now=None, receipts: set = frozenset()) -> dict:
    """Every sale (one per eBay order) in the last `days` days, with the real
    per-order figures /sync stored, and totals. Profit = the order's net
    (after eBay fees, ad fee, label and refunds) minus the Ross cost with tax
    for the units sold."""
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(days=days)
    rows = []
    for item in items:
        e = item.get("ebay") or {}
        pub = _when(e.get("published_at"))
        cost, assumed = profit.unit_cost(item)
        for s in e.get("sales") or []:
            when = _when(s.get("sold_at"))
            if not when or when < since or when > now:
                continue
            units = int(s.get("units") or 1)
            net = float(s.get("net") or 0)
            rows.append({
                "item_id": item["item_id"], "title": (item.get("listing") or {}).get("title") or "(no title)",
                "sold_at": when, "units": units, "sale": float(s.get("sale") or 0),
                "shipping": float(s.get("shipping") or 0), "fees": float(s.get("fees") or 0),
                "ad_fee": float(s.get("ad_fee") or 0), "label": float(s.get("label") or 0),
                "refund": float(s.get("refund") or 0), "net": net,
                "ross_price": (item.get("receipt") or {}).get("reduced_price"),
                "ross_code": None if (item.get("receipt") or {}).get("code_generated")
                else (item.get("receipt") or {}).get("code"),
                "cost": round(cost * units, 2) if cost is not None else None,
                "cost_source": cost_source(item, receipts), "assumed": assumed,
                "profit": round(net - (cost or 0) * units, 2),
                "days": max(0, (when - pub).days) if pub else None})
    rows.sort(key=lambda r: r["sold_at"])
    n = len(rows)
    days_known = [r["days"] for r in rows if r["days"] is not None]
    totals = {"orders": n, "units": sum(r["units"] for r in rows),
              "revenue": round(sum(r["sale"] for r in rows), 2),
              "shipping": round(sum(r["shipping"] for r in rows), 2),
              "fees": round(sum(r["fees"] + r["ad_fee"] for r in rows), 2),
              "labels": round(sum(r["label"] for r in rows), 2),
              "refunds": round(sum(r["refund"] for r in rows), 2),
              "cost": round(sum(r["cost"] or 0 for r in rows), 2),
              "profit": round(sum(r["profit"] for r in rows), 2),
              "avg_profit": round(sum(r["profit"] for r in rows) / n, 2) if n else None,
              "avg_days": round(sum(days_known) / len(days_known), 1) if days_known else None,
              "assumed": sum(1 for r in rows if r["assumed"])}
    return {"since": since, "until": now, "rows": rows, "totals": totals}


def _recent_sheet(wb, recent: dict, bold, header_fill, red_fill, border) -> None:
    ws = wb.create_sheet("Last 7 days")
    t = recent["totals"]
    ws["A1"] = f"Sold in the last 7 days ({recent['since']:%b %d} – {recent['until']:%b %d %Y}, UTC)"
    ws["A1"].font = Font(bold=True, size=14)
    summary = [("Orders", t["orders"], "0"), ("Units", t["units"], "0"), ("Revenue (item prices)", t["revenue"], MONEY),
               ("Shipping collected", t["shipping"], MONEY), ("Fees (eBay + ads)", t["fees"], MONEY),
               ("Labels", t["labels"], MONEY), ("Refunds", t["refunds"], MONEY), ("Ross cost (with tax)", t["cost"], MONEY),
               ("Profit", t["profit"], MONEY), ("Average profit per order", t["avg_profit"], MONEY),
               ("Average days to sell", t["avg_days"], "0.0")]
    for idx, (label, val, fmt) in enumerate(summary):
        ws.cell(row=3 + idx, column=1, value=label).font = bold
        c = ws.cell(row=3 + idx, column=2, value=val)
        c.number_format = fmt
    hr = 3 + len(summary) + 1
    headers = ["ID", "Title", "Sold", "Units", "Sale", "Ship chg", "eBay fee", "Ad fee", "Label", "Refund",
               "Net", "Ross price", "Ross code", "Cost (with tax)", "Cost source", "Profit", "Days to sell"]
    for col, h in enumerate(headers, start=1):
        c = ws.cell(row=hr, column=col, value=h)
        c.font = bold
        c.fill = header_fill
        c.border = border
    for i, row in enumerate(recent["rows"], start=1):
        r = hr + i
        values = [row["item_id"][:8], row["title"], row["sold_at"].strftime("%Y-%m-%d"), row["units"], row["sale"],
                  row["shipping"], row["fees"], row["ad_fee"], row["label"], row["refund"], row["net"],
                  row["ross_price"], row["ross_code"], row["cost"], row["cost_source"], row["profit"], row["days"]]
        for col, v in enumerate(values, start=1):
            c = ws.cell(row=r, column=col, value=v)
            c.border = border
            if col in (5, 6, 7, 8, 9, 10, 11, 12, 14, 16):
                c.number_format = MONEY
            if row["assumed"]:
                c.fill = red_fill
    if not recent["rows"]:
        ws.cell(row=hr + 1, column=1, value="No sales in the last 7 days.")
    if t["assumed"]:
        ws.cell(row=hr + len(recent["rows"]) + 2, column=1,
                value="Red rows: no Ross price, so the cost is ASSUMED from the list price.").font = \
            Font(italic=True, color="C00000")
    for col, w in zip("ABCDEFGHIJKLMNOPQ", (10, 40, 11, 6, 10, 9, 9, 8, 8, 8, 10, 10, 14, 12, 11, 10, 11)):
        ws.column_dimensions[col].width = w


def _thumbnail(cover_path, dest_dir):
    if Image is None or not cover_path:
        return None
    p = Path(cover_path)
    if not p.exists():
        return None
    try:
        img = ImageOps.exif_transpose(Image.open(p)).convert("RGB")
        img.thumbnail((THUMB_PX, THUMB_PX))
        out = Path(dest_dir) / (p.stem + ".png")
        img.save(out, "PNG")
        return str(out)
    except Exception:
        return None


def _expand_partially_sold(items: list[dict]) -> list[dict]:
    """Split an item that sold a unit but still has stock into two rows.

    A multi-quantity listing can sell one unit and stay live with the rest, but
    an item has a single status, so its two halves can't both be told: whichever
    status it carries, the other half stops being counted — $268.92 of live
    inventory in one measurement, invisible to the report and to the weekly
    price check.

    Neither row alone is right: the realized sale belongs in SOLD, the remaining
    units belong in STILL LISTED. So emit both, sharing the item id.

    Keyed off the SALE RECORD rather than the status, because either status can
    carry this state — 'sold' with stock left, or back to 'published' by /sync
    once eBay confirmed the listing is still live."""
    out = []
    for item in items:
        ebay = item.get("ebay") or {}
        remaining = 0
        try:
            remaining = int(ebay.get("quantity") or 0)
        except (TypeError, ValueError):
            remaining = 0
        if ebay.get("sale_price") is None or remaining <= 0:
            out.append(item)
            continue

        realized = dict(item)
        realized["status"] = "sold"   # the money actually taken, units_sold units
        out.append(realized)
        still_listed = dict(item)
        still_listed["status"] = "published"
        # Drop the sale so this row prices at the CURRENT list price, not the
        # price the sold unit fetched.
        still_listed["ebay"] = {k: v for k, v in ebay.items() if k != "sale_price"}
        out.append(still_listed)
    return out


def _scoreboard_sheet(wb, bold, header_fill) -> None:
    """A "What to buy" sheet: pipeline/scoreboard.py's per-category and
    per-brand numbers and verdicts, for planning the next Ross trip."""
    from pipeline import scoreboard

    board = scoreboard.build(list_items())
    ws = wb.create_sheet("What to buy")
    ws["A1"] = "What to buy — scoreboard"
    ws["A1"].font = Font(bold=True, size=14)
    r = 3
    for line in scoreboard.summary_lines(board):
        ws.cell(row=r, column=1, value=line)
        r += 1
    headers = ["Group", "Verdict", "Why", "Live listings", "Live units", "Units bought",
               "Units sold", "Sell-through", "Sales / 100 listing-days", "Median days to sell",
               "Ross cost tied up", "Profit to date"]
    for title, groups in (("By category", board["categories"]), ("By brand", board["brands"])):
        r += 1
        ws.cell(row=r, column=1, value=title).font = bold
        r += 1
        for col, h in enumerate(headers, start=1):
            c = ws.cell(row=r, column=col, value=h)
            c.font = bold
            c.fill = header_fill
        for g in groups:
            r += 1
            values = [g["name"], g["verdict"], g["why"], g["live"], g["live_units"], g["bought"],
                      g["sold"], g["sell_through"], g["per100"], g["median_days"], g["tied_up"],
                      g["profit"] if g["profit_items"] else None]
            for col, v in enumerate(values, start=1):
                c = ws.cell(row=r, column=col, value=round(v, 2) if isinstance(v, float) else v)
                if col == 8:
                    c.number_format = "0%"
                elif col in (11, 12):
                    c.number_format = MONEY
        r += 1
    ws.column_dimensions["A"].width = 22
    ws.column_dimensions["C"].width = 22
    for col in "DEFGHIJKL":
        ws.column_dimensions[col].width = 12


def build_report(path: str, ad_days: int = 90) -> str:
    """`ad_days` is the look-back for the account-level charge reconciliation
    printed under the table (Promoted Listings, which eBay bills without an
    order id and no row can therefore carry)."""
    items = [i for i in list_items()
             if (i.get("listing") or {}).get("price") is not None and i["status"] in _REPORTABLE]
    items = _expand_partially_sold(items)
    items.sort(key=lambda i: (i["status"], i.get("created_at", "")))

    wb = Workbook()
    ws = wb.active
    ws.title = "Profit"

    bold = Font(bold=True)
    header_fill = PatternFill("solid", fgColor="D9E1F2")
    edit_fill = PatternFill("solid", fgColor="FFF2CC")   # editable assumption cells
    red_fill = PatternFill("solid", fgColor="FFC7CE")      # assumed cost: no Ross price known
    red_font = Font(bold=True, color="9C0006")
    total_fill = PatternFill("solid", fgColor="E2EFDA")
    center = Alignment(horizontal="center", vertical="center")
    thin = Side(style="thin", color="BFBFBF")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)

    ws["A1"] = "Ross Resale — Profit Report"
    ws["A1"].font = Font(bold=True, size=14)
    ws["A2"] = ("Generated " + datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
                + "  ·  edit the yellow assumption cells to recalc every row")

    # --- Assumptions (editable, rows 4-9) ---
    # Defaults come from config.py so the report and the repricing floor can't
    # disagree about what a sale costs. They're measured from settled orders, not
    # eBay's rate card — see the comments there.
    assumptions = [
        ("eBay fee % (effective, incl. fee charged on tax)", EBAY_FVF_PCT, PCT),
        ("eBay fixed fee per order", EBAY_FIXED_FEE, MONEY),
        ("Promoted Listings, effective %", EBAY_AD_FEE_PCT, PCT),
        ("Shipping charged to buyer", EBAY_SHIP_CHARGED, MONEY),
        ("Your shipping cost (postage)", EBAY_SHIP_COST, MONEY),
        # Also from config.py, so the estimate this report puts on a receipt-less
        # item matches the one the bot quotes at publish time (see profit.py).
        ("Assumed Ross cost when unknown (% of list)", ASSUMED_COST_RATIO, PCT),
    ]
    for idx, (label, val, fmt) in enumerate(assumptions):
        r = 4 + idx
        ws[f"B{r}"] = label
        ws[f"B{r}"].font = bold
        cell = ws[f"C{r}"]
        cell.value = val
        cell.number_format = fmt
        cell.fill = edit_fill
        cell.border = border

    # --- Table header (row 11) ---
    # Every money column is a LINE TOTAL (per-unit × qty), so a multi-quantity
    # listing projects its full profit. That's right for a projection but wrong
    # for judging the item: a qty-4 row shows four units' profit blended into one
    # figure, which isn't the number you decide on. "Net / unit" is that number —
    # the profit one of these actually makes, whatever the quantity.
    #
    # (Margin % is the same either way, since every term scales with qty. It's the
    # DOLLARS that multiply — and the TOTAL row's margin is revenue-weighted, so a
    # qty-5 listing counts five times toward the blended figure.)
    #
    # Qty comes from _report_qty: the offer quantity for live items, 1 for sold
    # ones whose sale_price is already the realized amount.
    # P-T come after the formula columns so no formula moves.
    headers = ["Photo", "ID", "Title", "Status", "Qty", "Unit price", "Revenue",
               "Ship chg", "eBay fee", "Ad fee", "Ship cost", "Cost (Ross)",
               "Net profit", "Margin", "Net / unit",
               "Ross price (pre-tax)", "Ross code", "Cost source", "Sold", "Days listed"]
    receipts = receipt_prices(items)
    now = datetime.now(timezone.utc)
    HR = 11
    for col, h in enumerate(headers, start=1):
        cell = ws.cell(row=HR, column=col, value=h)
        cell.font = bold
        cell.fill = header_fill
        cell.alignment = center
        cell.border = border

    tmpdir = tempfile.mkdtemp(prefix="report_thumbs_")
    first = HR + 1
    r = first
    for item in items:
        L = item.get("listing") or {}
        qty = _report_qty(item)
        unit_price = _unit_price_for(item, qty)
        paid = profit.paid_with_tax(item)

        ws.row_dimensions[r].height = 72
        photos = item.get("photos") or []
        thumb = _thumbnail(photos[0] if photos else None, tmpdir)
        if thumb:
            ws.add_image(XLImage(thumb), f"A{r}")

        # Columns: A Photo B ID C Title D Status E Qty F Unit price G Revenue
        # H Ship chg I eBay fee J Ad fee K Ship cost L Cost M Net profit N Margin
        ws.cell(row=r, column=2, value=item["item_id"][:8])
        ws.cell(row=r, column=3, value=L.get("title") or "(no title)")
        ws.cell(row=r, column=4, value=item["status"])
        ws.cell(row=r, column=5, value=qty).alignment = center
        ws.cell(row=r, column=6, value=unit_price).number_format = MONEY
        ws.cell(row=r, column=7, value=f"=F{r}*E{r}").number_format = MONEY           # revenue

        # A SOLD item doesn't need assumptions — eBay billed real amounts, and
        # /sync stored them. Use those: the shipping the buyer actually paid, the
        # fees actually charged, and the actual cost of the label bought through
        # eBay. Across the sold items the assumptions were off by +$28 on shipping
        # collected, +$25 on fees and +$5 on postage, all in the same direction:
        # they understated what really came in.
        #
        # Ad fees stay on the assumption even here. eBay bills Promoted Listings
        # as periodic "General fee" transactions with NO orderId, so there is
        # nothing to attribute to a line item; see the ad-spend note written below
        # the table for the real total.
        ebay_data = item.get("ebay") or {}
        actual = ebay_data.get("sale_price") is not None and ebay_data.get("fees_known")
        if actual:
            ws.cell(row=r, column=8,
                    value=round(float(ebay_data.get("shipping_collected") or 0), 2)).number_format = MONEY
            ws.cell(row=r, column=9,
                    value=round(float(ebay_data.get("ebay_fees") or 0), 2)).number_format = MONEY
            ws.cell(row=r, column=11,
                    value=round(float(ebay_data.get("shipping_label_cost") or 0), 2)).number_format = MONEY
        else:
            ws.cell(row=r, column=8, value=f"={A_SHIP_CHG}*E{r}").number_format = MONEY    # ship charged
            # FVF% applies to (item revenue + shipping charged); the fixed per-order
            # fee is charged per unit sold.
            ws.cell(row=r, column=9,
                    value=f"={A_FVF}*(G{r}+H{r})+{A_FIXED}*E{r}").number_format = MONEY    # eBay fee
            ws.cell(row=r, column=11, value=f"={A_SHIP_COST}*E{r}").number_format = MONEY  # ship cost
        ws.cell(row=r, column=10, value=f"={A_AD}*G{r}").number_format = MONEY         # ad fee
        cost_cell = ws.cell(row=r, column=12)
        cost_cell.number_format = MONEY
        if paid is not None:
            cost_cell.value = f"={round(float(paid), 2)}*E{r}"  # unit cost × qty
        else:
            # No receipt was captured. Estimate from the list price rather than
            # leaving it empty: an empty cell reads as zero cost, which reported
            # the whole sale price as profit. Still orange, so an estimate is
            # never mistaken for a real figure — type the amount to replace it.
            cost_cell.value = f"={A_COST_RATIO}*F{r}*E{r}"
        ws.cell(row=r, column=13,
                value=f"=G{r}+H{r}-I{r}-J{r}-K{r}-L{r}").number_format = MONEY         # net profit
        ws.cell(row=r, column=14,
                value=f'=IF((G{r}+H{r})=0,"",M{r}/(G{r}+H{r}))').number_format = "0.0%"  # margin
        # Per-unit profit: what ONE of these makes, so a multi-quantity row can
        # still be judged as an item rather than as a batch.
        ws.cell(row=r, column=15,
                value=f'=IF(E{r}=0,"",M{r}/E{r})').number_format = MONEY
        rec = item.get("receipt") or {}
        source = cost_source(item, receipts)
        if rec.get("reduced_price") is not None:
            ws.cell(row=r, column=16, value=round(float(rec["reduced_price"]), 2)).number_format = MONEY
        # a generated placeholder (000000000006) is not a Ross code
        ws.cell(row=r, column=17, value=None if rec.get("code_generated") else rec.get("code"))
        ws.cell(row=r, column=18, value=source)
        sold = sold_on(item) if item["status"] == "sold" else None
        ws.cell(row=r, column=19, value=sold.strftime("%Y-%m-%d") if sold else None)
        ws.cell(row=r, column=20, value=days_listed(item, now))
        for col in range(2, 21):
            ws.cell(row=r, column=col).border = border
            if source == "assumed":
                ws.cell(row=r, column=col).fill = red_fill
        if source == "assumed":
            ws.cell(row=r, column=18).font = red_font
        r += 1

    # --- Totals rows ---
    # Three bands, not one. A single grand total adds money you have actually
    # been paid to money you merely hope to be paid, and reads as profit — with
    # most of the inventory unsold, that number is mostly wishful. SOLD is what
    # the business has really made; STILL LISTED is the projection.
    #
    # Banded with SUMIF over the status column rather than row ranges, so the
    # split stays correct no matter how the rows are sorted.
    last = r - 1
    if last >= first:
        money_cols = ("E", "G", "H", "I", "J", "K", "L", "M")
        bands = [
            ("SOLD (realized)", f'"sold"'),
            ("STILL LISTED (projected)", f'"<>sold"'),
            ("TOTAL", None),
        ]
        for label, criterion in bands:
            ws.cell(row=r, column=2, value=label).font = bold
            for col_letter in money_cols:
                if criterion is None:
                    formula = f"=SUM({col_letter}{first}:{col_letter}{last})"
                else:
                    formula = (f"=SUMIF($D${first}:$D${last},{criterion},"
                               f"{col_letter}{first}:{col_letter}{last})")
                c = ws.cell(row=r, column=ord(col_letter) - 64, value=formula)
                c.number_format = "0" if col_letter == "E" else MONEY
                c.font = bold
            # Blended margin for the band: its net / its revenue. Note this is
            # revenue-weighted, so a multi-quantity listing counts once per unit
            # toward it — read "Net / unit" for per-item economics.
            ws.cell(row=r, column=14,
                    value=f'=IF(G{r}=0,"",M{r}/G{r})').number_format = "0.0%"
            ws.cell(row=r, column=14).font = bold
            for col in range(2, 16):
                cell = ws.cell(row=r, column=col)
                cell.fill = total_fill
                cell.border = border
            r += 1
        r -= 1  # the note below is positioned relative to the last written row

    # --- Widths + note ---
    widths = {"A": 14, "B": 10, "C": 40, "D": 11, "E": 6, "F": 10, "G": 10,
              "H": 9, "I": 10, "J": 9, "K": 10, "L": 12, "M": 12, "N": 9, "O": 11,
              "P": 11, "Q": 14, "R": 11, "S": 11, "T": 8}
    for col, w in widths.items():
        ws.column_dimensions[col].width = w
    ws.freeze_panes = "A12"

    # --- Ad-spend reconciliation ---
    # The one cost the per-row maths can't carry. eBay bills Promoted Listings as
    # periodic charges with no orderId, so each row can only ever show the ASSUMED
    # ad rate. Printing what was actually billed, against the revenue it was billed
    # on, is the only way to know whether that assumption is anywhere near right —
    # on a recent window it was 5.0% actual against 4% assumed.
    recon_row = r + 2
    try:
        from ebay.orders import account_charges
        charges = account_charges(ad_days)
        # Column G is `=F*E`, so mirror it exactly — _unit_price_for already
        # divided a realized total back to a unit price, and multiplying by the
        # same qty reconstructs it. Using _price_for here instead would restate
        # every multi-unit sale at units_sold times the money taken, and this
        # figure is the denominator the ad rate is judged against.
        sold_revenue = sum(
            _unit_price_for(i, _report_qty(i)) * _report_qty(i)
            for i in items if i["status"] == "sold")
        lines = [f"eBay billed ${charges['total']:.2f} in account-level charges over the "
                 f"last {ad_days} days ({charges['count']} charges), which no row above can "
                 f"attribute — eBay posts them without an order id."]
        for memo, bucket in sorted(charges["by_memo"].items(), key=lambda kv: -kv[1]["total"]):
            share = f" = {bucket['total'] / sold_revenue * 100:.1f}% of sold revenue" if sold_revenue else ""
            lines.append(f"    {memo}: ${bucket['total']:.2f} ({bucket['count']}){share}")
        if sold_revenue:
            lines.append(f"    Compare with the assumed ad rate in C6. Sold revenue: ${sold_revenue:.2f}.")
        ws.cell(row=recon_row, column=2, value="  ".join(lines[:1]))
        ws.cell(row=recon_row, column=2).font = Font(italic=True, color="808080")
        for offset, line in enumerate(lines[1:], start=1):
            ws.cell(row=recon_row + offset, column=2, value=line)
            ws.cell(row=recon_row + offset, column=2).font = Font(italic=True, color="808080")
        r = recon_row + len(lines)
    except Exception as e:
        # No finances scope, no network, standalone run — the report is still
        # complete without it, so say why rather than failing the build.
        ws.cell(row=recon_row, column=2,
                value=f"(Could not read eBay account-level charges — Promoted Listings spend is "
                      f"not reflected above: {type(e).__name__})")
        ws.cell(row=recon_row, column=2).font = Font(italic=True, color="808080")
        r = recon_row

    note_row = r + 2
    ws.cell(row=note_row, column=2,
            value="Sold rows use REAL eBay figures (shipping collected, fees, and the postage you "
                  "actually paid for the label); unsold rows use the assumptions above.  ·  "
                  "RED rows = cost source 'assumed': no Ross price was captured, so the cost is ESTIMATED "
                  "from the list price (see the assumption above) — type the real amount to replace it.  ·  "
                  "Cost source: receipt (a receipt line has the code at that price), tag (read off the tag), "
                  "typed (entered by hand), assumed.  ·  "
                  "'Net / unit' is the profit ONE unit makes; the money columns are line totals (per-unit x qty).")
    ws.cell(row=note_row, column=2).font = Font(italic=True, color="808080")

    try:
        everything = list_items()
        _recent_sheet(wb, recent_sales(everything, 7, now, receipt_prices(everything)), bold, header_fill,
                      red_fill, border)
    except Exception as e:
        print(f"WARNING: Last 7 days sheet skipped: {type(e).__name__}: {e}")

    try:
        _scoreboard_sheet(wb, bold, header_fill)
    except Exception as e:
        # Advisory extra; the profit sheet is the report.
        print(f"WARNING: scoreboard sheet skipped: {type(e).__name__}: {e}")

    wb.save(path)
    return path


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else "report.xlsx"
    build_report(out)
    print(f"Wrote {out}")
