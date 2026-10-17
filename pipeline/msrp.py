"""What this store's sales say an item is worth, from its MSRP.

The owner prices "by MSRP and feel": under MSRP, adjusted for brand. This
turns that habit into a number the pricing judge can weigh: for every SOLD
item with a researched MSRP (identification.retail_price), sold price per unit
divided by MSRP, grouped by brand. An item's store-history estimate is its
MSRP times its brand's median ratio. A brand with fewer than MIN_N sales falls
back to its item-type bucket, then to every sale.

The MSRP is the brand's retail price as found by research. It is NOT the Ross
tag's original_price: that is Ross's own pre-markdown price, not retail, so an
MSRP equal to it is flagged as suspicious (research may have read the tag).

Read-only on data/ross.db.
"""
import json
import re
import statistics

from pipeline import price as v1

MIN_N = 3
# Sold/MSRP ratios outside this band are data errors (a wrong MSRP, a lot
# counted as one unit), not pricing evidence, and are left out of the table.
RATIO_BOUNDS = (0.05, 3.0)

_BRAND_ALIASES = {
    "lauren ralph lauren": "ralph lauren",
    "ralph lauren home": "ralph lauren",
    "lauren by ralph lauren": "ralph lauren",
    "kate spade new york": "kate spade",
    "michael kors access": "michael kors",
    "luv betsey by betsey johnson": "betsey johnson",
}

# Order matters: "Apple Watch Band" is a band, not a watch.
_TYPE_BUCKETS = [
    ("watch band", r"\bband|strap"),
    ("watch", r"watch"),
    ("sunglasses", r"sunglass"),
    ("bedding", r"duvet|sham|sheet|pillowcase|comforter|quilt|bedding|blanket"),
    ("bag", r"bag|tote|backpack|satchel|crossbody|wallet|purse|handbag|duffel|duffle|clutch|"
            r"hobo|weekender|messenger"),
    ("fragrance", r"fragrance|cologne|perfume|eau de"),
]


def brand_key(brand) -> str | None:
    b = re.sub(r"[^a-z0-9 &]", " ", str(brand or "").lower())
    b = re.sub(r"\s+", " ", b).strip()
    if not b:
        return None
    return _BRAND_ALIASES.get(b, b)


def type_bucket(item_type) -> str:
    t = str(item_type or "").lower()
    for name, pattern in _TYPE_BUCKETS:
        if re.search(pattern, t):
            return name
    return "other"


def msrp_of(ident: dict) -> float | None:
    m = v1._to_float((ident or {}).get("retail_price"))
    return m if m and m > 0 else None


def msrp_flags(ident: dict, receipt: dict | None = None) -> list[str]:
    """Reasons to doubt the item's MSRP. Empty list = nothing suspicious."""
    m = msrp_of(ident)
    if m is None:
        return ["missing"]
    flags = []
    receipt = receipt or {}
    orig = v1._to_float(receipt.get("original_price"))
    cost = v1._to_float(receipt.get("reduced_price"))
    if orig and abs(m - orig) <= 0.02 * orig:
        flags.append("equals_ross_tag_price")
    if cost and m < 1.2 * cost:
        flags.append("below_our_cost")
    return flags


def sold_history(rows) -> list[dict]:
    """rows: (item_id, identification json, ebay json, receipt json) of SOLD items.
    Returns the usable sales with their sold/MSRP ratio."""
    out = []
    for item_id, ident_json, ebay_json, receipt_json in rows:
        ident = json.loads(ident_json or "{}")
        e = json.loads(ebay_json or "{}")
        if not e.get("sale_price"):
            continue
        unit = e["sale_price"] / max(1, e.get("units_sold") or 1)
        m = msrp_of(ident)
        rec = {"item_id": item_id, "brand": brand_key(ident.get("brand")),
               "bucket": type_bucket(ident.get("item_type")), "msrp": m,
               "sold": round(unit, 2), "ratio": None}
        if m and not msrp_flags(ident, json.loads(receipt_json or "null")):
            r = unit / m
            if RATIO_BOUNDS[0] <= r <= RATIO_BOUNDS[1]:
                rec["ratio"] = r
        out.append(rec)
    return out


def load_history() -> list[dict]:
    from pipeline.pricing_v2 import _db_rows
    return sold_history(_db_rows(
        "SELECT item_id, identification, ebay, receipt FROM items WHERE status = 'sold'"))


def _stats(ratios: list[float]) -> dict:
    s = sorted(ratios)
    return {"n": len(s), "ratio": round(statistics.median(s), 3) if s else None,
            "q1": round(v1._quantile(s, 0.25), 3) if s else None,
            "q3": round(v1._quantile(s, 0.75), 3) if s else None}


def ratio_for(brand, item_type, history, exclude_item_id=None) -> dict:
    """Brand ratio, else item-type bucket, else global; leave-one-out."""
    usable = [h for h in history if h["ratio"] is not None
              and not (exclude_item_id and h["item_id"].startswith(exclude_item_id))]
    bk, tb = brand_key(brand), type_bucket(item_type)
    for level, key, subset in (("brand", bk, [h for h in usable if bk and h["brand"] == bk]),
                               ("type", tb, [h for h in usable if h["bucket"] == tb]),
                               ("global", "all", usable)):
        if len(subset) >= MIN_N or level == "global":
            return {"level": level, "key": key, **_stats([h["ratio"] for h in subset])}
    raise AssertionError("unreachable")


def store_estimate(ident: dict, receipt: dict | None = None, exclude_item_id: str | None = None,
                   history: list[dict] | None = None) -> dict:
    history = load_history() if history is None else history
    m = msrp_of(ident)
    r = ratio_for(ident.get("brand"), ident.get("item_type"), history, exclude_item_id)
    est = low = high = None
    if m and r["ratio"]:
        raw = m * r["ratio"]
        est = round(max(round(raw) - 0.01, 0.99), 2)
        low, high = round(m * r["q1"], 2), round(m * r["q3"], 2)
    # est is kept for the backtest's heuristic baseline. The judge only ever
    # sees the range: as a point estimate it anchored Claude low in round 3
    # (20/39 outcome hits vs 21/39 without it).
    return {"msrp": m, "msrp_flags": msrp_flags(ident, receipt), **r, "estimate": est,
            "range_low": low, "range_high": high}


def ratio_table(history: list[dict]) -> list[dict]:
    """Per brand, every brand with a usable ratio, most sales first."""
    brands = {}
    for h in history:
        if h["ratio"] is not None:
            brands.setdefault(h["brand"], []).append(h["ratio"])
    table = [{"brand": b, **_stats(rs)} for b, rs in brands.items()]
    return sorted(table, key=lambda t: (-t["n"], t["brand"] or ""))


def coverage(history: list[dict]) -> dict:
    return {"sold": len(history), "with_msrp": sum(1 for h in history if h["msrp"]),
            "usable_ratio": sum(1 for h in history if h["ratio"] is not None)}
