"""What to buy: a per-brand and per-category scoreboard built from the item DB.

The 29 Oct 2026 business review (docs/business-review-2026-10-29.md) found the
buying mix is the biggest lever. Sunglasses, Ralph Lauren duvets and watches
sell within days. Ed Hardy, Michael Kors and most handbags sit, and the owner
kept buying Ed Hardy because nothing showed the backlog. This module puts those
numbers in front of the next Ross trip. It is local data only and makes no
eBay calls.

Per group (normalized brand, or category) it reports:
  live          listings still on eBay with stock, and the units in them
  bought/sold   units that reached eBay, and units that sold
  sell-through  sold / bought
  per100        units sold per 100 listing-days (a listing-day is one listing
                live for one day, so stock that sits drags this down)
  median days   listing → first sale, over the listings that sold
  tied up       Ross cost of the unsold live units (known receipts only)
  profit        net proceeds − Ross cost, over sold items with a known cost

Only items that reached eBay (status published or sold) count. Items still in
the pipeline haven't had a chance to sell, and rejected ones were never stock.

Verdict rules (see `verdict()`), checked in this order:
  < MIN_UNITS (4) units bought, or the Unknown brand / Other category → "ok" (too few to tell).
  "buy more" — sell-through ≥ 50%, or ≥ 30% with ≥ 3 sales per 100 listing-
               days; never when the sold items lost money overall.
  < MIN_LISTING_DAYS (60) listing-days → "ok": nothing negative is said about
               stock that hasn't had time to sell.
  "stop"     — sell-through < 10% on ≥ 8 units and ≥ 150 listing-days
               (Michael Kors: 0 of 25 units).
  "pause"    — sell-through < 30% (Ed Hardy: ~20% with 40 live).
  "ok"       — everything else.
The new-item warning (`saturation_warning`) fires separately when a brand has
more than SATURATED (8) live listings, whatever its verdict.

The sales money is read from the item's `ebay` JSON. ebay/orders.py records
the totals there (sale_price, net_proceeds, units_sold, sold_at). A per-order
list (`orders` / `sales`) is also accepted when present, and its entries win
over the totals.
"""
from __future__ import annotations

import re
import statistics
from datetime import datetime, timezone

# --- Verdict thresholds (tuned on the 29 Oct 2026 data; see module docstring) ---
MIN_UNITS = 4              # units bought before any verdict but "ok"
MIN_LISTING_DAYS = 60      # exposure before a pause/stop ("doesn't sell" needs time)
BUY_MORE_SELL_THROUGH = 0.50
BUY_MORE_PER100 = 3.0      # fast sellers at 30%+ sell-through also get "buy more"
PAUSE_SELL_THROUGH = 0.30
STOP_SELL_THROUGH = 0.10
STOP_MIN_UNITS = 8         # "stop" is the strongest call: a bigger sample...
STOP_MIN_LISTING_DAYS = 150  # ...and real exposure
SATURATED = 8              # live listings in one brand that trigger the buy warning

VERDICTS = ("buy more", "ok", "pause", "stop")

_LISTED = ("published", "sold")

# Lower-cased, punctuation-stripped brand → display name.
_BRAND_ALIASES = {
    "lauren ralph lauren": "Ralph Lauren",
    "ralph lauren": "Ralph Lauren",
    "ralph lauren home": "Ralph Lauren",
    "polo ralph lauren": "Ralph Lauren",
    "lauren by ralph lauren": "Ralph Lauren",
    "lrl": "Ralph Lauren",
    "michael kors": "Michael Kors",
    "michael kors access": "Michael Kors",
    "mk access": "Michael Kors",
    "michael michael kors": "Michael Kors",
    "mk": "Michael Kors",
    "kate spade": "Kate Spade",
    "kate spade new york": "Kate Spade",
    "luv betsey": "Betsey Johnson",
    "luv betsey by betsey johnson": "Betsey Johnson",
    "betsey johnson": "Betsey Johnson",
    "guess": "GUESS",
    "dkny": "DKNY",
    "donna karan new york": "DKNY",
    "ed hardy": "Ed Hardy",
    "tommy hilfiger": "Tommy Hilfiger",
    "juicy couture": "Juicy Couture",
    "quay": "Quay",
    "quay australia": "Quay",
    "oakley": "Oakley",
    "versus versace": "Versace",
    "versace": "Versace",
}

# eBay category id → scoreboard category. The DB stores the id but not the name
# (ebay.category_name is null), so the names live here; see pipeline/category.py.
_CATEGORY_BY_ID = {
    "169291": "Handbags", "52357": "Handbags", "16080": "Handbags",
    "45258": "Wallets", "2996": "Wallets",
    "37644": "Duvets",
    "43397": "Shams/pillowcases", "54257": "Shams/pillowcases",
    "20460": "Sheets",
    "79720": "Sunglasses", "45246": "Sunglasses",
    "182068": "Watch bands", "98624": "Watch bands",
    "31387": "Watches",
    "29585": "Beauty", "11848": "Beauty", "67408": "Beauty",
}

# Fallback on identification.item_type when there is no category id. Order
# matters: "watch band" before "watch", "pillow sham" before anything bedding.
_CATEGORY_BY_TYPE = [
    (re.compile(r"sunglass", re.I), "Sunglasses"),
    (re.compile(r"watch\s*band|\bband\b|strap", re.I), "Watch bands"),
    (re.compile(r"watch", re.I), "Watches"),
    (re.compile(r"sham|pillow\s*case|pillowcase", re.I), "Shams/pillowcases"),
    (re.compile(r"duvet|comforter|quilt|blanket", re.I), "Duvets"),
    (re.compile(r"sheet", re.I), "Sheets"),
    (re.compile(r"wallet", re.I), "Wallets"),
    (re.compile(r"bag|tote|satchel|backpack|purse|crossbody|hobo|duffel|duffle|"
                r"weekender|clutch", re.I), "Handbags"),
    (re.compile(r"fragrance|perfume|cologne|trimmer|shaver", re.I), "Beauty"),
]


def normalize_brand(brand) -> str:
    """One display name per brand, so "Lauren Ralph Lauren" and "Ralph Lauren"
    (or "MK Access" and "Michael Kors") are scored together."""
    raw = re.sub(r"\s+", " ", str(brand or "")).strip()
    if not raw:
        return "Unknown"
    key = re.sub(r"[^\w\s]", "", raw).lower().strip()
    key = re.sub(r"\s+", " ", key)
    if key in _BRAND_ALIASES:
        return _BRAND_ALIASES[key]
    for alias, name in _BRAND_ALIASES.items():
        # "Kate Spade New York Home", "Michael Kors Collection"…
        if len(alias) > 3 and key.startswith(alias + " "):
            return name
    return raw if any(c.islower() for c in raw) or len(raw) <= 5 else raw.title()


def category_of(item: dict) -> str:
    ebay = item.get("ebay") or {}
    listing = item.get("listing") or {}
    cat_id = str(ebay.get("category_id") or listing.get("category_id") or "")
    if cat_id in _CATEGORY_BY_ID:
        return _CATEGORY_BY_ID[cat_id]
    # Adopted Seller-Hub listings have no identification, so fall back to the title.
    for text in ((item.get("identification") or {}).get("item_type"), listing.get("title")):
        for rx, name in _CATEGORY_BY_TYPE:
            if text and rx.search(str(text)):
                return name
    return "Other"


def brand_of(item: dict, known: list[str] = ()) -> str:
    """identification.brand, normalized. With no brand (adopted listings carry
    no identification), the known brand the listing title starts with."""
    brand = normalize_brand((item.get("identification") or {}).get("brand"))
    if brand != "Unknown":
        return brand
    title = re.sub(r"[^\w\s]", "", str((item.get("listing") or {}).get("title") or "")).lower()
    candidates = list(known) + list(_BRAND_ALIASES)
    for cand in sorted(candidates, key=len, reverse=True):
        key = re.sub(r"[^\w\s]", "", cand).lower()
        if key and (title == key or title.startswith(key + " ")):
            return normalize_brand(cand)
    return "Unknown"


def _ts(value):
    if not value:
        return None
    try:
        d = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _num(value):
    try:
        return None if value is None or value == "" else float(value)
    except (TypeError, ValueError):
        return None


def _int(value, default=0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _sales(ebay: dict) -> dict:
    """Units, net proceeds and first sale time, in either recorded shape.

    Per-order list (`orders` / `sales`, a list of dicts) wins when present.
    Otherwise the totals: `units_sold` (or `units`), `net_proceeds`,
    `first_sold_at` and `sold_at`. A sale recorded with no unit count is one unit."""
    for key in ("orders", "sales"):
        rows = ebay.get(key)
        if isinstance(rows, list) and rows and all(isinstance(r, dict) for r in rows):
            units, net, net_known, times = 0, 0.0, True, []
            for r in rows:
                units += max(_int(r.get("units", r.get("quantity", r.get("units_sold", 1))), 1), 0)
                n = _num(r.get("net_proceeds", r.get("net")))
                if n is None:
                    net_known = False
                else:
                    net += n
                t = _ts(r.get("sold_at") or r.get("created_at") or r.get("date"))
                if t:
                    times.append(t)
            return {"units": units, "net": net if net_known else None,
                    "first": min(times) if times else None,
                    "last": max(times) if times else None}
    sold = any(ebay.get(k) is not None for k in ("sale_price", "net_proceeds", "sold_at"))
    if not sold:
        return {"units": 0, "net": None, "first": None, "last": None}
    units = _int(ebay.get("units_sold", ebay.get("units")), 0)
    if units <= 0:
        units = max(len(ebay.get("order_ids") or []), 1)
    last = _ts(ebay.get("sold_at"))
    first = _ts(ebay.get("first_sold_at")) or last
    return {"units": units, "net": _num(ebay.get("net_proceeds")), "first": first,
            "last": max(t for t in (first, last) if t) if (first or last) else None}


def item_facts(item: dict, now: datetime | None = None, known_brands=()) -> dict | None:
    """The numbers one item contributes, or None when it never reached eBay."""
    if item.get("status") not in _LISTED:
        return None
    now = now or datetime.now(timezone.utc)
    ebay = item.get("ebay") or {}
    sale = _sales(ebay)
    if "quantity" in ebay:
        remaining = max(_int(ebay.get("quantity"), 0), 0)
    else:
        remaining = 0 if item["status"] == "sold" else 1
    if item["status"] == "sold" and sale["units"] == 0:
        sale["units"] = 1   # marked sold by hand with no record of how many
    cost = _num((item.get("receipt") or {}).get("reduced_price"))
    listed = _ts(ebay.get("published_at")) or _ts(item.get("created_at"))
    live = remaining > 0
    end = now if live else (sale["last"] or now)
    listing_days = max((end - listed).total_seconds() / 86400, 1.0) if listed else None
    days_to_sell = None
    if sale["first"] and listed:
        d = (sale["first"] - listed).total_seconds() / 86400
        if d >= -1:   # a sale before the listing date means a merged/adopted record
            days_to_sell = max(d, 0.0)
    profit = None
    if sale["units"] and sale["net"] is not None and cost is not None:
        profit = sale["net"] - cost * sale["units"]
    return {
        "brand": brand_of(item, known_brands),
        "category": category_of(item),
        "live": live, "remaining": remaining,
        "sold": sale["units"], "bought": sale["units"] + remaining,
        "listing_days": listing_days, "days_to_sell": days_to_sell,
        "tied_up": cost * remaining if cost is not None and remaining else 0.0,
        "tied_up_unknown": remaining if cost is None else 0,
        "profit": profit,
    }


def _empty() -> dict:
    return {"live": 0, "live_units": 0, "bought": 0, "sold": 0, "listing_days": 0.0,
            "days": [], "tied_up": 0.0, "tied_up_unknown": 0, "profit": 0.0,
            "profit_items": 0, "listings": 0}


def _finish(name: str, g: dict) -> dict:
    g = dict(g)
    g["name"] = name
    g["sell_through"] = g["sold"] / g["bought"] if g["bought"] else None
    g["per100"] = 100 * g["sold"] / g["listing_days"] if g["listing_days"] else None
    g["median_days"] = statistics.median(g["days"]) if g["days"] else None
    g["verdict"], g["why"] = verdict(g)
    del g["days"]
    return g


def verdict(g: dict) -> tuple[str, str]:
    """(verdict, reason). Plain rules, documented in the module docstring."""
    bought, sold = g.get("bought") or 0, g.get("sold") or 0
    days = g.get("listing_days") or 0
    if bought < MIN_UNITS:
        return "ok", "too few to tell"
    if g.get("name") in ("Unknown", "Other"):
        return "ok", "mixed bag, no verdict"
    st = sold / bought
    per100 = 100 * sold / days if days else 0
    lost_money = g.get("profit_items") and g.get("profit", 0) < 0
    if st >= BUY_MORE_SELL_THROUGH or (st >= PAUSE_SELL_THROUGH and per100 >= BUY_MORE_PER100):
        if lost_money:
            return "ok", f"{st:.0%} sold but lost money"
        return "buy more", f"{st:.0%} sold"
    if days < MIN_LISTING_DAYS:
        return "ok", "too new to tell"
    if st < STOP_SELL_THROUGH and bought >= STOP_MIN_UNITS and days >= STOP_MIN_LISTING_DAYS:
        return "stop", f"{sold} of {bought} sold"
    if st < PAUSE_SELL_THROUGH:
        return "pause", f"{st:.0%} sold"
    return "ok", f"{st:.0%} sold"


def build(items: list[dict], now: datetime | None = None) -> dict:
    """{"brands": [...], "categories": [...], "total": {...}}, groups sorted by
    units bought (largest first)."""
    now = now or datetime.now(timezone.utc)
    brands, cats, total = {}, {}, _empty()
    known = sorted({normalize_brand((i.get("identification") or {}).get("brand"))
                    for i in items} - {"Unknown"})
    for item in items:
        f = item_facts(item, now, known)
        if f is None:
            continue
        for g in (brands.setdefault(f["brand"], _empty()),
                  cats.setdefault(f["category"], _empty()), total):
            g["listings"] += 1
            g["live"] += 1 if f["live"] else 0
            g["live_units"] += f["remaining"]
            g["bought"] += f["bought"]
            g["sold"] += f["sold"]
            g["listing_days"] += f["listing_days"] or 0
            if f["days_to_sell"] is not None:
                g["days"].append(f["days_to_sell"])
            g["tied_up"] += f["tied_up"]
            g["tied_up_unknown"] += f["tied_up_unknown"]
            if f["profit"] is not None:
                g["profit"] += f["profit"]
                g["profit_items"] += 1

    def ordered(groups):
        return sorted((_finish(k, v) for k, v in groups.items()),
                      key=lambda g: (-g["bought"], g["name"]))
    return {"brands": ordered(brands), "categories": ordered(cats),
            "total": _finish("All", total)}


def load(now: datetime | None = None) -> dict:
    from db import list_items
    return build(list_items(), now)


# --- Text for Telegram (short lines; phones wrap anything wide) ---

def _pct(x) -> str:
    return "—" if x is None else f"{x:.0%}"


def _money(x) -> str:
    return f"-${-x:,.0f}" if x < 0 else f"${x:,.0f}"


def _days(x) -> str:
    return "—" if x is None else f"{x:.0f}d"


def _key_number(g: dict) -> str:
    st = _pct(g["sell_through"])
    if g["verdict"] == "buy more":
        md = f", sells in {_days(g['median_days'])}" if g["median_days"] is not None else ""
        return f"{g['sold']}/{g['bought']} sold ({st}){md}"
    if g["verdict"] == "stop":
        return f"{g['sold']}/{g['bought']} sold, {_money(g['tied_up'])} tied up"
    return f"{g['live']} live, {st} sold, {_money(g['tied_up'])} tied up"


def summary_lines(board: dict) -> list[str]:
    """The headline: buy more / pause / stop, one line per brand or category."""
    lines = []
    icons = {"buy more": "🟢 Buy more", "pause": "🟡 Pause", "stop": "🔴 Stop"}
    for v in ("buy more", "pause", "stop"):
        hits = [g for g in board["categories"] + board["brands"] if g["verdict"] == v]
        if not hits:
            continue
        lines.append(f"{icons[v]}:")
        for g in hits:
            lines.append(f"• {g['name']}: {_key_number(g)}")
    t = board["total"]
    lines.append(f"All: {t['sold']}/{t['bought']} sold ({_pct(t['sell_through'])}), "
                 f"{_money(t['tied_up'])} tied up in {t['live_units']} live units")
    return lines


def _row(g: dict) -> list[str]:
    profit = _money(g["profit"]) if g["profit_items"] else "—"
    return [f"{g['name']} — {g['verdict']}",
            f"  {g['live']} live/{g['live_units']}u · {g['sold']}/{g['bought']} sold "
            f"({_pct(g['sell_through'])})",
            f"  {g['per100']:.1f}/100d · med {_days(g['median_days'])} · "
            f"{_money(g['tied_up'])} tied · {profit} profit"
            if g["per100"] is not None else
            f"  med {_days(g['median_days'])} · {_money(g['tied_up'])} tied · {profit} profit"]


def format_scoreboard(board: dict, brands: int = 12) -> list[str]:
    """Lines for /scoreboard: the headline, then categories, then top brands."""
    lines = ["📋 What to buy — scoreboard", ""] + summary_lines(board)
    lines += ["", "—— By category ——"]
    for g in board["categories"]:
        lines += _row(g)
    top = board["brands"][:brands]
    if top:
        lines += ["", f"—— Top {len(top)} brands ——"]
        for g in top:
            lines += _row(g)
    unknown = board["total"]["tied_up_unknown"]
    lines += ["", "Units = units bought that reached eBay. /100d = units sold per 100 "
                  "listing-days. Tied = Ross cost of unsold live units"
              + (f" ({unknown} units have no receipt)." if unknown else ".")]
    return lines


def saturation_warning(item: dict, board: dict | None = None) -> str | None:
    """One line when a newly identified item's brand is already backed up
    (> SATURATED live listings) or scored pause/stop, else None. When the
    item's category sells well (e.g. Ralph Lauren is backed up with shams but
    its duvets fly), the line says so rather than steer the owner off a winner."""
    name = brand_of(item)
    if name == "Unknown":
        return None
    board = board if board is not None else load()
    g = next((b for b in board["brands"] if b["name"] == name), None)
    if g is None or (g["verdict"] not in ("pause", "stop") and g["live"] <= SATURATED):
        return None
    advice = {"stop": "consider not buying more", "pause": "consider pausing"}.get(
        g["verdict"], "plenty in stock already")
    cat_name = category_of(item)
    cat = next((c for c in board["categories"] if c["name"] == cat_name), None)
    if cat is not None and cat["verdict"] == "buy more":
        advice += f" ({cat_name} sell well)"
    return f"⚠️ {name}: {g['live']} live, {_pct(g['sell_through'])} sell-through — {advice}."
