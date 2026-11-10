"""eBay's listing quality report (the xlsx from Seller Hub), parsed for
/diagnose (docs/listing-performance-plan.md 7.4).

The workbook has a Summary, a Guide, a Google Shopping sheet and one sheet per
category ("section"). Each section sheet has:
  - a header: the category and filters, our GMV rank, the report date;
  - the buyer funnel: our impressions/day, CTR and conversion with ranks, and
    eBay's recommendation for each stage (with ours / top 10% / bottom 10%);
  - "More benchmarks": our row, the top 10% and the bottom 10% for each stage;
  - the listings table (one row per live listing).
Column order differs between sheets, so everything is found by its label,
never by position.
"""
import re
from datetime import datetime, timedelta, timezone

import openpyxl

# The time zones eBay prints in "Report generated: US - 11/06/2026 06-38 PST".
_TZ = {"PST": -8, "PDT": -7, "MST": -7, "MDT": -6, "CST": -6, "CDT": -5, "EST": -5, "EDT": -4,
       "UTC": 0, "GMT": 0}
_GENERATED = re.compile(r"Report generated:.*?(\d{1,2})/(\d{1,2})/(\d{4})\s+(\d{1,2})-(\d{2})\s*([A-Z]{3})?")
_RANK = re.compile(r"([\d,]+)\s+out of\s+([\d,]+)")
_SELLERS = re.compile(r"Sellers selling in this section:\s*([\d,]+)")
_STAGES = {"Average per day": "impressions", "Rate": "ctr", "Conversion": "conversion"}
# "More benchmarks" blocks: label in column B, the metric's column header.
_BENCH_BLOCKS = {"Impressions in eBay search": "impressions", "Click-through": "ctr",
                 "Sales conversion": "conversion"}
_MEETS = "meets the top benchmark"

# Listing table header -> stored column.
LISTING_COLUMNS = {
    "Item title": "title", "Item Id": "ebay_item_id", "Custom label": "item_id", "Price": "price",
    "Shipping fee": "shipping_fee", "Free shipping": "free_shipping",
    "Daily impressions per listing": "impressions_per_day", "Click-through rate": "ctr",
    "Sales conversion rate": "conversion", "Promoted listings ad rate": "ad_rate",
    "Recommended item specifics filled": "specifics_filled",
    "Recommended item specifics to add": "specifics_to_add", "Number of watchers": "watchers",
    "Item age in days": "age_days", "Sales count in last 31 days": "sales_31d",
}
_INT = {"specifics_filled", "watchers", "age_days", "sales_31d"}
# eBay's words for a real zero (impressions but no clicks; clicks but no sale).
_ZERO = {"No clicks", "No sales"}
_FLOAT = {"price", "shipping_fee", "impressions_per_day", "ctr", "conversion", "ad_rate"}


class ReportError(ValueError):
    """Not an eBay listing quality report, or one this parser can't read."""


def _text(v) -> str:
    return "" if v is None else str(v).strip()


def _num(v) -> float | None:
    """A number from a cell: numbers as they are, "1,397" or "4%" as text,
    and None for "No sales", "Not provided", "N/A", blanks."""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    t = _text(v).replace(",", "").replace("$", "")
    pct = t.endswith("%")
    try:
        x = float(t.rstrip("%"))
    except ValueError:
        return None
    return x / 100 if pct else x


def _int(v) -> int | None:
    x = _num(v)
    return None if x is None else int(round(x))


def _yes(v) -> bool | None:
    t = _text(v)
    if t in ("✔", "✓", "Yes", "yes", "1"):
        return True
    if t in ("Not provided", "No", "no", "0", "Not enabled"):
        return False
    return None


def report_date(text: str) -> datetime | None:
    """The UTC time in "Report generated: US - 11/06/2026 06-38 PST"."""
    m = _GENERATED.search(text or "")
    if not m:
        return None
    month, day, year, hour, minute, tz = m.groups()
    local = datetime(int(year), int(month), int(day), int(hour), int(minute))
    offset = _TZ.get(tz or "UTC", 0)
    return (local - timedelta(hours=offset)).replace(tzinfo=timezone.utc)


def recommendation_kind(text: str) -> tuple[str, float | None]:
    """("free_shipping" | "ad_rate" | "specifics" | "other", target)."""
    t = text.lower()
    if "free shipping" in t:
        return "free_shipping", None
    m = re.search(r"ad rate to\s*([\d.]+)\s*%", t)
    if m:
        return "ad_rate", float(m.group(1)) / 100
    m = re.search(r"at least\s*(\d+)\s*recommended item specifics", t)
    if m:
        return "specifics", float(m.group(1))
    return "other", None


def _parse_section(ws) -> tuple[dict, list[dict]] | None:
    rows = [list(r) for r in ws.iter_rows(values_only=True)]
    cell = lambda r, c: rows[r][c] if r < len(rows) and c < len(rows[r]) else None  # noqa: E731

    section = conditions = None
    date = None
    gmv = sellers = None
    for r in range(min(len(rows), 10)):
        for c, v in enumerate(rows[r]):
            t = _text(v)
            if t.startswith("Category:"):
                body = t[len("Category:"):].strip()
                section, _, rest = body.partition(", Listings condition")
                section = section.strip()
                conditions = ("Listings condition" + rest).strip() if rest else None
            elif t.startswith("Report generated:"):
                date = report_date(t)
            elif "sales rank (GMV)" in t:
                m = _RANK.search(t)
                if m:
                    gmv = int(m.group(1).replace(",", ""))
                    sellers = int(m.group(2).replace(",", ""))
    if not section:
        return None

    sec = {"section": section, "conditions": conditions, "report_date": date, "gmv_rank": gmv,
           "sellers": sellers, "recommendations": [], "benchmarks": {}}
    listings_header = None
    shared: list[str] = []
    for r, row in enumerate(rows):
        for c, v in enumerate(row):
            t = _text(v)
            m = _SELLERS.search(t)
            if m:
                sec["sellers"] = int(m.group(1).replace(",", ""))
            stage = _STAGES.get(t)
            if stage and c == 3:  # funnel: label in D, value below it, rank in F
                sec[stage if stage != "impressions" else "impressions_per_day"] = _num(cell(r + 1, 3))
                sec[f"{stage}_rank"] = _int(cell(r + 1, 5))
                rec = _text(cell(r, 7))
                if rec and _MEETS not in rec:
                    text = rec.lstrip("●").strip()
                    kind, target = recommendation_kind(text)
                    sec["recommendations"].append({
                        "stage": stage, "text": text, "kind": kind, "target": target,
                        "ours": _num(cell(r, 10)), "top": _num(cell(r, 11)), "bottom": _num(cell(r, 12))})
        b = _text(cell(r, 1))
        if b in _BENCH_BLOCKS and _text(cell(r, 5)):
            block = _BENCH_BLOCKS[b]
            header = [_text(x) for x in row]
            # Only the first block labels columns G onwards; the others share them.
            if block == "impressions":
                shared = header
            elif shared:
                header = [h or (shared[c] if c < len(shared) else "") for c, h in enumerate(header)]
                header += shared[len(header):]
            got = {}
            for k, label in enumerate(("ours", "top", "bottom"), 1):
                vals = rows[r + k] if r + k < len(rows) else []
                got[label] = {header[c]: vals[c] for c in range(5, len(header))
                              if header[c] and c < len(vals) and vals[c] not in (None, "")}
            sec["benchmarks"][block] = got
            metric = {"impressions": "impressions_per_day", "ctr": "ctr", "conversion": "conversion"}[block]
            first = header[5]
            for label in ("top", "bottom"):
                sec[f"{label}_{metric}"] = _num(got[label].get(first))
        if b == "Item title":
            listings_header = r
    listings = []
    if listings_header is not None:
        header = [_text(x) for x in rows[listings_header]]
        for row in rows[listings_header + 1:]:
            if not _text(row[1] if len(row) > 1 else None):
                break
            raw = {header[c]: row[c] for c in range(len(header))
                   if header[c] and c < len(row) and row[c] not in (None, "", " ")}
            listing = {"section": section, "raw": raw}
            for label, col in LISTING_COLUMNS.items():
                v = raw.get(label)
                if col in _INT:
                    listing[col] = _int(v)
                elif col in _FLOAT:
                    listing[col] = 0.0 if _text(v) in _ZERO else _num(v)
                elif col == "free_shipping":
                    listing[col] = _yes(v)
                elif col == "ebay_item_id":
                    listing[col] = (str(int(v)) if isinstance(v, (int, float)) else _text(v)) or None
                else:
                    listing[col] = _text(v) or None
            if listing["ebay_item_id"]:
                listings.append(listing)
    return sec, listings


def parse(path) -> dict:
    """{"report_date": UTC datetime, "sections": [...], "listings": [...]}.
    Raises ReportError for anything that isn't a listing quality report."""
    try:
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    except Exception as e:
        raise ReportError(f"not a readable xlsx ({type(e).__name__})") from e
    try:
        sections, listings = [], []
        for ws in wb.worksheets:
            got = _parse_section(ws)
            if got:
                sections.append(got[0])
                listings += got[1]
    finally:
        wb.close()
    if not sections:
        raise ReportError("no category sheets found: is this eBay's listing quality report?")
    dates = {s["report_date"] for s in sections if s["report_date"]}
    if len(dates) != 1:
        raise ReportError("couldn't read one report date from the category sheets")
    date = dates.pop()
    for s in sections:
        s["report_date"] = date
    seen, unique = set(), []
    for listing in listings:  # a listing in two sections: keep the first
        if listing["ebay_item_id"] not in seen:
            seen.add(listing["ebay_item_id"])
            unique.append(listing)
    return {"report_date": date, "sections": sections, "listings": unique}


# --- applying it to a listing ----------------------------------------------------

def applies(rec: dict, listing: dict | None, item: dict | None = None) -> bool:
    """Does a section recommendation apply to this listing? Uses the report's
    row for it; for a listing newer than the report, what we know from the DB
    (unknown counts as "applies": the owner reads the fix and decides)."""
    kind, target = rec.get("kind"), rec.get("target")
    if kind == "free_shipping":
        if listing and listing.get("free_shipping") is not None:
            return not listing["free_shipping"]
        return True
    if kind == "ad_rate":
        rate = (listing or {}).get("ad_rate")
        return rate is None or target is None or rate < target - 1e-9
    if kind == "specifics":
        filled = (listing or {}).get("specifics_filled")
        if filled is None and item is not None:
            filled = len((item.get("listing") or {}).get("item_specifics") or {})
        return filled is None or target is None or filled < target
    return True


def fix_text(rec: dict, listing: dict | None, item_id: str) -> str:
    """One line: eBay's recommendation, and how we'd carry it out."""
    kind, target, top = rec.get("kind"), rec.get("target"), rec.get("top")
    if kind == "free_shipping":
        share = f" ({top:.0%} of the top listings offer it)" if top else ""
        return (f"eBay: free shipping for this category{share}. Decide it through the free-shipping "
                "test (plan §5), not on one listing.")
    if kind == "ad_rate":
        now = (listing or {}).get("ad_rate")
        return (f"eBay: ad rate {target:.0%} for this category"
                + (f" (this listing: {now:.0%})" if now is not None else "")
                + ". Owner decision (plan §6).")
    if kind == "specifics":
        missing = _text((listing or {}).get("specifics_to_add"))
        filled = (listing or {}).get("specifics_filled")
        return (f"eBay: fill at least {target:.0f} recommended item specifics"
                + (f" (this listing: {filled})" if filled is not None else "")
                + (f"; missing: {missing}" if missing else "")
                + f". /revise {item_id[:8]} with them.")
    return f"eBay: {rec.get('text')}."
