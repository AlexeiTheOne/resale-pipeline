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
from datetime import datetime, timezone
from pathlib import Path

from openpyxl import Workbook
from openpyxl.drawing.image import Image as XLImage
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

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
    """Sale price if the item sold, else its listed price."""
    ebay = item.get("ebay") or {}
    if ebay.get("sale_price") is not None:
        return ebay["sale_price"]
    return (item.get("listing") or {}).get("price")


def _report_qty(item) -> int:
    """How many units this row represents.

    For a still-listed item it's the offer's available quantity (what /setqty
    pushed to eBay) — a listing of 5 should project 5× the per-unit profit. For a
    SOLD item it's 1: `sale_price` is already the realized amount for what sold,
    and the live offer quantity has usually dropped to 0, so multiplying would
    either double-count or zero the row out. (Ross items are near-always unique
    single units, so a multi-unit sold order is the rare exception, not the norm.)
    """
    if item["status"] == "sold":
        return 1
    try:
        q = int((item.get("ebay") or {}).get("quantity", 1))
    except (TypeError, ValueError):
        q = 1
    return q if q > 0 else 1


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


def build_report(path: str) -> str:
    items = [i for i in list_items()
             if (i.get("listing") or {}).get("price") is not None and i["status"] in _REPORTABLE]
    items.sort(key=lambda i: (i["status"], i.get("created_at", "")))

    wb = Workbook()
    ws = wb.active
    ws.title = "Profit"

    bold = Font(bold=True)
    header_fill = PatternFill("solid", fgColor="D9E1F2")
    edit_fill = PatternFill("solid", fgColor="FFF2CC")   # editable assumption cells
    missing_fill = PatternFill("solid", fgColor="FCE4D6")  # unknown cost — fill it in
    total_fill = PatternFill("solid", fgColor="E2EFDA")
    center = Alignment(horizontal="center", vertical="center")
    thin = Side(style="thin", color="BFBFBF")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)

    ws["A1"] = "Ross Resale — Profit Report"
    ws["A1"].font = Font(bold=True, size=14)
    ws["A2"] = ("Generated " + datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
                + "  ·  edit the yellow assumption cells to recalc every row")

    # --- Assumptions (editable, rows 4-9) ---
    assumptions = [
        ("eBay final value fee %", 0.1325, PCT),
        ("eBay fixed fee per order", 0.40, MONEY),
        ("Promoted Listings ad rate %", 0.04, PCT),
        ("Shipping charged to buyer", 10.0, MONEY),
        ("Your shipping cost (postage)", 10.0, MONEY),
        # Items with no scanned receipt used to be costed at ZERO, which reported
        # their entire sale price as profit — one showed a 68% margin purely
        # because its cost was missing. A share of the list price beats a flat
        # guess: across 63 items with a known cost it lands at a median 29% of
        # list (p25 22%, p75 34%), so it scales with the item instead of costing
        # a $110 listing the same as a $30 one.
        ("Assumed Ross cost when unknown (% of list)", 0.29, PCT),
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
    headers = ["Photo", "ID", "Title", "Status", "Qty", "Unit price", "Revenue",
               "Ship chg", "eBay fee", "Ad fee", "Ship cost", "Cost (Ross)",
               "Net profit", "Margin", "Net / unit"]
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
        unit_price = round(float(_price_for(item)), 2)
        qty = _report_qty(item)
        paid = (item.get("receipt") or {}).get("reduced_price")

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
            cost_cell.fill = missing_fill
        ws.cell(row=r, column=13,
                value=f"=G{r}+H{r}-I{r}-J{r}-K{r}-L{r}").number_format = MONEY         # net profit
        ws.cell(row=r, column=14,
                value=f'=IF((G{r}+H{r})=0,"",M{r}/(G{r}+H{r}))').number_format = "0.0%"  # margin
        # Per-unit profit: what ONE of these makes, so a multi-quantity row can
        # still be judged as an item rather than as a batch.
        ws.cell(row=r, column=15,
                value=f'=IF(E{r}=0,"",M{r}/E{r})').number_format = MONEY
        for col in range(2, 16):
            ws.cell(row=r, column=col).border = border
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
              "H": 9, "I": 10, "J": 9, "K": 10, "L": 12, "M": 12, "N": 9, "O": 11}
    for col, w in widths.items():
        ws.column_dimensions[col].width = w
    ws.freeze_panes = "A12"

    note_row = r + 2
    ws.cell(row=note_row, column=2,
            value="Sold rows use REAL eBay figures (shipping collected, fees, and the postage you "
                  "actually paid for the label); unsold rows use the assumptions above.  ·  "
                  "Orange 'Cost (Ross)' cells = no receipt was captured, so the cost is ESTIMATED "
                  "from the list price (see the assumption above) — type the real amount to replace it.  ·  "
                  "'Net / unit' is the profit ONE unit makes; the money columns are line totals (per-unit x qty).")
    ws.cell(row=note_row, column=2).font = Font(italic=True, color="808080")

    wb.save(path)
    return path


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else "report.xlsx"
    build_report(out)
    print(f"Wrote {out}")
