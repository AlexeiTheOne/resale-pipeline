"""
comps.py - pre-purchase comps check for EbRossLister (sourcing stage).

Put this file in the project root (next to config.py and telegram_bot.py).

Give it a text description of an item; it returns every EXACT-match eBay listing:
  SOLD   - last 90 days (eBay's cap), via the sold-listings Apify actor that
           pipeline/price.py already uses (same token failover, retries, mapping).
  ACTIVE - via the official eBay Browse API (free; no Apify cost).

Plus stats (sold count, median/p10/p90, active count, lowest active, sell-through),
break-even sale price, profit and a verdict when you pass --cost. Fees and
shipping come from the numbers already measured in config.py.

Search text is cleaned first (brackets, "in", "with" etc. removed). The style code (TR105025,
A14722C ...) is taken from your description automatically. Listings that contain it are the
exact model; listings without it are kept only if their title has every word of your
description (or every "Must contain" word you gave).

How it finds enough sold rows without wasting credits:
  1. Search sold with your text (25 rows).
  2. If the rows come back full but few match -> ONE wider refetch (75 rows).
  3. If almost nothing came back at all -> ONE broader search (style code dropped)
     and matching relaxed to brand + size + your --require words. Marked "broad".
Every Apify call and row count is printed in Debug so you can see the cost.

Usage (from the project root):
  python comps.py "Converse Chuck Taylor Puff A14722C" --size 9 --cost 24.99 --ship 12
  python comps.py "Burberry London for Men eau de toilette" --brand Burberry --require london --size 1.6 --cost 14.99
  python comps.py "Nike Academy Team Backpack DV0761-458" --cost 12.99 --ship 10
  python comps.py "..." --links         (print a clickable link under every listing)
  python comps.py "..." --html          (write comps_report.html and open it in your browser)
  python comps.py "..." --exclude weekend fabric   (drop other products that share the name)
  python comps.py "..." --no-details    (skip the per-listing units-sold lookup; faster)
  python comps.py "..." --json          (full output as JSON)

Units sold: every matched ACTIVE listing gets the number of units it has already sold (S:
column), plus totals, best sellers and a units-sold-weighted price. Free eBay Browse API,
one extra call per listing (up to 120). Watch counts are requested too, but eBay has not
returned any for these listings, so that column is hidden unless some come back.
"-" means "not reported", never zero.

Links: active listings always have one (eBay Browse API). Sold rows get one if the Apify
actor returns a URL or item id; otherwise use the sold-search link printed at the end,
which always opens the same sold list on eBay.

Shipping: every active price is item + shipping, for a buyer in SOURCING_BUYER_ZIP (default: your
own zip from config.py). Listings with calculated shipping are priced with eBay's real cost for
that zip; a listing that reports no cost is flagged, never counted as free.

Sizes: --size 9   |   --size 1.6   (also matches 1.7 oz and 50ml automatically)
       --size 100ml (also matches 3.3/3.4 oz)   |   several: --size 9 9.5
Shoe size "women's 9" is NOT counted as men's 9; add --womens to search women's sizes.

Needs in .env: APIFY_TOKEN (optionally APIFY_TOKEN_BACKUP), EBAY_CLIENT_ID, EBAY_CLIENT_SECRET
"""

import sys
import os
import re
import json
import time
import base64
import html
import argparse
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlencode, quote

sys.path.insert(0, str(Path(__file__).parent))

import httpx
from dotenv import load_dotenv

from pipeline.price import (
    SOLD_URL, _call, _to_float, _is_used, _iqr_trim, _dispersion,
    _quantile, _median, _brand_match, _evidence_tier,
)
from config import (
    EBAY_FVF_PCT, EBAY_FIXED_FEE, EBAY_AD_FEE_PCT, EBAY_SHIP_COST,
    EBAY_MARKETPLACE_ID, EBAY_SHIP_FROM_ADDRESS,
)

load_dotenv()
EBAY_CLIENT_ID = os.getenv("EBAY_CLIENT_ID")
EBAY_CLIENT_SECRET = os.getenv("EBAY_CLIENT_SECRET")

# First sold fetch. These are raw keyword-search rows, not exact matches (other
# sizes, colors, lots get filtered out afterwards), so it must be wider than the
# number of comps you want to keep.
SOURCING_SOLD_FETCH = int(os.getenv("SOURCING_SOLD_FETCH", "25"))
# One-time wider refetch if the first came back full but too few matched.
SOURCING_SOLD_FETCH_WIDE = int(os.getenv("SOURCING_SOLD_FETCH_WIDE", "75"))
# Exact matches wanted before we stop widening / trust the sold numbers.
SOURCING_MIN_SOLD = int(os.getenv("SOURCING_MIN_SOLD", "5"))
SOURCING_ACTIVE_MAX = int(os.getenv("SOURCING_ACTIVE_MAX", "600"))
# Where the BUYER is, for listings with calculated shipping (USPS Ground Advantage etc.). eBay
# only returns their shipping cost when told a zip code. Default = your own zip, so the numbers
# match what you see on eBay. Buyers elsewhere pay more or less; for a rough national average set
# SOURCING_BUYER_ZIP=64105 (Kansas City) in .env.
SOURCING_BUYER_ZIP = (os.getenv("SOURCING_BUYER_ZIP")
                      or (EBAY_SHIP_FROM_ADDRESS or {}).get("postalCode") or "33172")
# Max active listings to look up individually for units sold / watchers (free API calls).
SOURCING_DETAIL_MAX = int(os.getenv("SOURCING_DETAIL_MAX", "120"))
# One listing counts for at most this many units in the units-sold-weighted price, so a
# single 48-unit listing cannot decide the price on its own.
SOURCING_UNITS_CAP = int(os.getenv("SOURCING_UNITS_CAP", "10"))
# Florida 6% + Miami-Dade 1% on the Ross receipt.
SOURCING_SALES_TAX = float(os.getenv("SOURCING_SALES_TAX", "0.07"))

# Titles that aren't one new retail unit. Whole-word matching, so "lotion" is fine.
EXCLUDE_RE = re.compile(
    r"\b(lots?|bundles?|empty|testers?|samples?|decants?|replica|inspired|broken|"
    r"damaged|defect(ive)?|vintage)\b|lot of|box only|no box|for parts|read description|"
    r"old formula|gift set",
    re.I,
)

# oz <-> ml for fragrance sizes, so "1.6" also finds "50ml" and "1.7oz" titles.
_OZ_TO_ML = {"1.6": 50, "1.7": 50, "2.5": 75, "3.3": 100, "3.4": 100, "4.2": 125}
_ML_TO_OZ = {"50": ["1.6", "1.7"], "75": ["2.5"], "100": ["3.3", "3.4"], "125": ["4.2"]}


# ---------------------------------------------------------------- matching
def _norm(s: str) -> str:
    """'DV0761-458' == 'dv0761 458' == 'DV0761458'."""
    return re.sub(r"[\s\-_/\.]+", "", (s or "").lower())


_UNIT_TOKEN = re.compile(r"^\d+[a-z]{1,4}$", re.I)      # 128gb, 1080p, 500ml


def _looks_like_code(tok: str) -> bool:
    """TR105025, A14722C, DV0761-458 yes; 'tallx11' (from 17"tallx11.5"), '128gb' no.
    A style code has at least 3 digits and a letter."""
    digits = sum(c.isdigit() for c in tok)
    letters = sum(c.isalpha() for c in tok)
    return len(tok) >= 5 and digits >= 3 and letters >= 1 and not _UNIT_TOKEN.match(tok)


def auto_required_tokens(description: str) -> list[str]:
    """Style codes / model numbers found in a text."""
    tokens = re.findall(r"[A-Za-z0-9][A-Za-z0-9\-]{3,}[A-Za-z0-9]", description)
    return [t for t in tokens if _looks_like_code(t)]


_FILLER = {"in", "the", "a", "an", "of", "and", "with", "for", "new", "nwt", "nwot", "nib",
           "tags", "tag", "brand"}
_UNITS = {"oz", "ml", "fl", "inch", "inches", "cm", "mm", "lb", "lbs", "pc", "pcs", "pack"}
_NOISE = _FILLER | _UNITS      # words that never have to appear in a title


def clean_query(text: str) -> str:
    """Search text without punctuation and filler words. Brackets like '(TR105025)' made
    the sold scraper return nothing and confused eBay's search."""
    text = re.sub(r"[()\[\]{}\"\u201c\u201d*!?;:,|]", " ", text or "")
    words = [w for w in text.split() if w.lower() not in _FILLER]
    return re.sub(r"\s+", " ", " ".join(words)).strip()


def distinct_words(text: str) -> list[str]:
    """The words a title must contain to be 'the same thing' in the broad pass."""
    out, seen = [], set()
    for w in re.findall(r"[A-Za-z0-9']+", text or ""):
        lw = w.lower()
        if len(lw) < 2 or lw.isdigit() or lw in _NOISE or lw in seen:
            continue
        seen.add(lw)
        out.append(w)
    return out


def strip_codes(text: str, codes: list[str]) -> str:
    for c in codes:
        text = re.sub(re.escape(c), " ", text, flags=re.I)
    return re.sub(r"\s+", " ", text).strip()


def _oz_pat(s: str) -> str:
    return rf"(?<![\d.]){re.escape(s)}(?!\d)(?!\.\d)"


def size_patterns(sizes: list[str]) -> list[str]:
    pats = []
    for s in sizes:
        s = s.lower().strip()
        m = re.fullmatch(r"(\d+)\s?ml", s)
        if m:
            ml = m.group(1)
            pats.append(rf"(?<!\d){ml}\s?ml")
            pats += [_oz_pat(oz) for oz in _ML_TO_OZ.get(ml, [])]
            continue
        pats.append(_oz_pat(s))
        if s in _OZ_TO_ML:
            ml = _OZ_TO_ML[s]
            pats.append(rf"(?<!\d){ml}\s?ml")
            pats += [_oz_pat(oz) for oz in _ML_TO_OZ.get(str(ml), []) if oz != s]
    return pats


_WOMENS_BEFORE = re.compile(r"(wom[ae]n|wmns?|ladies|girls)'?s?\s*$")
_OTHER_REGION_BEFORE = re.compile(r"\b(uk|eu|eur|jp|cm)\s*$")   # "UK 9" is a US 10, not a US 9


def size_ok(low_title: str, pats: list[str], womens: bool) -> bool:
    for pat in pats:
        for m in re.finditer(pat, low_title):
            before = low_title[max(0, m.start() - 12):m.start()]
            if _OTHER_REGION_BEFORE.search(before):
                continue
            if not womens and _WOMENS_BEFORE.search(before):
                continue  # "WOMENS 9" is a men's 7.5, not a men's 9
            return True
    return False


def matches(title: str, required: list[str], size_pats: list[str],
            brand: str | None, womens: bool = False,
            own_codes: list[str] | None = None,
            exclude_re: re.Pattern | None = None) -> bool:
    if EXCLUDE_RE.search(title or ""):
        return False
    if exclude_re and exclude_re.search(title or ""):
        return False
    if own_codes is not None:
        # broad pass: a title naming some OTHER style code is another colorway/model
        mine = {_norm(c) for c in own_codes}
        if any(_norm(tok) not in mine for tok in auto_required_tokens(title or "")):
            return False
    t = _norm(title)
    if any(_norm(r) not in t for r in required):
        return False
    if brand and not _brand_match(title, brand):
        return False
    if size_pats and not size_ok(f" {(title or '').lower()} ", size_pats, womens):
        return False
    return True


def _item_key(r: dict):
    """Identity of a listing. eBay puts the search words into the item link (?_skw=...&hash=...),
    so the SAME listing found by two different searches has two different URLs - never
    compare URLs. Use the eBay item id (from the API, or the number after /itm/)."""
    if r.get("item_id"):
        return r["item_id"]
    m = re.search(r"/itm/(?:[^/?]+/)?(\d{9,})", r.get("url") or "")
    if m:
        return m.group(1)
    return (r["title"].lower(), r["price"])


def _merge(base: list[dict], extra: list[dict]) -> list[dict]:
    """base plus the rows of `extra` that are not already in base (same eBay item)."""
    have = {_item_key(r) for r in base}
    return list(base) + [r for r in extra if _item_key(r) not in have]


# ---------------------------------------------------------------- SOLD (Apify, via price.py)
_sold_fields_seen: set[str] = set()   # which fields the actor really returns (see Debug)


def _listing_url(item: dict) -> str:
    """A sold row's link: a URL field if the actor gives one, else built from an item id."""
    for k in ("url", "itemUrl", "itemWebUrl", "link", "productUrl", "viewItemURL", "listingUrl"):
        v = item.get(k)
        if isinstance(v, str) and v.startswith("http"):
            return v
    for k in ("itemId", "listingId", "itemNumber", "id"):
        v = str(item.get(k) or "")
        if re.fullmatch(r"\d{9,15}", v):
            return f"https://www.ebay.com/itm/{v}"
    return ""


def fetch_sold(query: str, count: int) -> list[dict]:
    rows = _call(SOLD_URL, {"keyword": query, "count": count})
    out = []
    for item in rows:
        _sold_fields_seen.update(item.keys())
        price = _to_float(item.get("totalPrice")) or _to_float(item.get("soldPrice"))
        out.append({
            "price": round(price, 2) if price is not None else None,
            "title": item.get("title", ""),
            "condition": item.get("condition", ""),
            "url": _listing_url(item),
        })
    return out


def search_urls(q: str) -> dict:
    """The same searches, as eBay pages you can open in a browser."""
    base = {"_nkw": q, "LH_ItemCondition": "1000|1500", "LH_PrefLoc": 1}
    return {
        "sold_search": "https://www.ebay.com/sch/i.html?" + urlencode(
            {**base, "LH_Sold": 1, "LH_Complete": 1, "_sop": 13}),
        "active_search": "https://www.ebay.com/sch/i.html?" + urlencode(
            {**base, "_sop": 15}),   # lowest price + shipping first
    }


# ---------------------------------------------------------------- ACTIVE (eBay Browse API, free)
_token_cache = {"value": None, "ts": 0.0}


def _ebay_app_token() -> str:
    if _token_cache["value"] and time.time() - _token_cache["ts"] < 6000:
        return _token_cache["value"]          # tokens last 2h; reuse across calls
    if not (EBAY_CLIENT_ID and EBAY_CLIENT_SECRET):
        raise SystemExit("Missing EBAY_CLIENT_ID / EBAY_CLIENT_SECRET in .env")
    creds = base64.b64encode(f"{EBAY_CLIENT_ID}:{EBAY_CLIENT_SECRET}".encode()).decode()
    r = httpx.post(
        "https://api.ebay.com/identity/v1/oauth2/token",
        headers={"Authorization": f"Basic {creds}",
                 "Content-Type": "application/x-www-form-urlencoded"},
        data={"grant_type": "client_credentials",
              "scope": "https://api.ebay.com/oauth/api_scope"},
        timeout=30,
    )
    r.raise_for_status()
    _token_cache.update(value=r.json()["access_token"], ts=time.time())
    return _token_cache["value"]


def _browse_headers() -> dict:
    return {"Authorization": f"Bearer {_ebay_app_token()}",
            "X-EBAY-C-MARKETPLACE-ID": EBAY_MARKETPLACE_ID,
            # Without this, listings with CALCULATED shipping come back with no shipping cost at
            # all - which used to be counted as $0.00 shipping.
            "X-EBAY-C-ENDUSERCTX": f"contextualLocation=country%3DUS%2Czip%3D{SOURCING_BUYER_ZIP}"}


def _cheapest_shipping(options) -> float | None:
    """Cheapest shipping cost among a listing's options; None if eBay gave no cost
    (which is NOT the same as free: free shipping is an explicit 0.00)."""
    costs = [c for o in (options or [])
             if (c := _to_float(((o or {}).get("shippingCost") or {}).get("value"))) is not None]
    return min(costs) if costs else None


def fetch_active(query: str) -> tuple[list[dict], int]:
    """All NEW-condition US listings for the query (price + shipping).
    Returns (rows, eBay's own total for the query)."""
    headers = _browse_headers()
    rows, offset, total = [], 0, 0
    while offset < SOURCING_ACTIVE_MAX:
        r = httpx.get(
            "https://api.ebay.com/buy/browse/v1/item_summary/search",
            headers=headers, timeout=30,
            params={"q": query, "filter": "conditions:{NEW},itemLocationCountry:US",
                    "limit": 200, "offset": offset},
        )
        r.raise_for_status()
        data = r.json()
        total = data.get("total", 0)
        batch = data.get("itemSummaries", []) or []
        for it in batch:
            price = _to_float((it.get("price") or {}).get("value"))
            ship = _cheapest_shipping(it.get("shippingOptions"))
            rows.append({
                "price": round(price + (ship or 0.0), 2) if price is not None else None,
                "base_price": price,                # item price without shipping
                "shipping": ship,                   # None = eBay gave no cost
                "ship_known": ship is not None,
                "title": it.get("title", ""),
                "condition": it.get("condition", ""),
                "url": it.get("itemWebUrl", ""),
                "item_id": it.get("itemId"),
                "watchers": it.get("watchCount"),   # None when eBay doesn't report it
                "sold_qty": None,                   # filled by enrich_active
            })
        if len(batch) < 200:
            break
        offset += 200
    return rows, total


_VOL_RE = re.compile(r"(\d+(?:\.\d+)?)\s?(fl\.?\s?oz|oz|ml)\b", re.I)
_STD_ML = [5, 7, 10, 15, 20, 25, 30, 50, 75, 100, 125, 150, 200, 250]


def is_multi_size(title: str) -> bool:
    """True when a title names two different bottle sizes (e.g. '1 oz, 1.7 oz, 3.3 oz').
    Those are variation listings: their sold count mixes sizes, so it can't price one size.
    1.6 oz / 1.7 oz / 50 ml all count as the same size."""
    vols = set()
    for num, unit in _VOL_RE.findall(title or ""):
        ml = float(num) if unit.lower() == "ml" else float(num) * 29.57
        near = min(_STD_ML, key=lambda x: abs(x - ml))
        vols.add(near if abs(near - ml) / near <= 0.08 else round(ml))
    return len(vols) >= 2


def _weighted_median(pairs: list[tuple[float, int]]) -> float | None:
    """Median price where each price counts once per unit sold at it."""
    pairs = sorted(p for p in pairs if p[1] > 0)
    total = sum(q for _, q in pairs)
    if not total:
        return None
    run = 0
    for price, q in pairs:
        run += q
        if run * 2 >= total:
            return price


def fetch_item_details(item_id: str) -> dict:
    """One listing's watcher count and units sold so far (Browse API getItem, free)."""
    r = httpx.get(f"https://api.ebay.com/buy/browse/v1/item/{quote(item_id, safe='')}",
                  headers=_browse_headers(), timeout=30)
    r.raise_for_status()
    d = r.json()
    sold = None
    for a in d.get("estimatedAvailabilities") or []:
        if a.get("estimatedSoldQuantity") is not None:
            sold = a["estimatedSoldQuantity"]
            break
    return {"watchers": d.get("watchCount"), "sold_qty": sold,
            "shipping": _cheapest_shipping(d.get("shippingOptions"))}


def enrich_active(rows: list[dict], max_n: int) -> tuple[int, int]:
    """Fill watchers / sold_qty on the cheapest `max_n` rows, in place. Returns (ok, failed)."""
    todo = [r for r in sorted(rows, key=lambda x: x["price"]) if r.get("item_id")][:max_n]

    def work(r):
        try:
            return r, fetch_item_details(r["item_id"])
        except Exception:
            return r, None

    ok = fail = 0
    with ThreadPoolExecutor(max_workers=6) as pool:
        for r, d in pool.map(work, todo):
            if d is None:
                fail += 1
                continue
            ok += 1
            if not r.get("ship_known") and d.get("shipping") is not None and r.get("base_price") is not None:
                r["shipping"], r["ship_known"] = d["shipping"], True
                r["price"] = round(r["base_price"] + d["shipping"], 2)
            if d["watchers"] is not None:
                r["watchers"] = d["watchers"]
            r["sold_qty"] = d["sold_qty"]
    return ok, fail


# ---------------------------------------------------------------- profit
def profit_at(sale_total: float, ross_price: float, ship_cost: float) -> dict:
    """What one unit nets at `sale_total` (item + shipping the buyer paid),
    using the fee rates measured in config.py."""
    fees = sale_total * (EBAY_FVF_PCT + EBAY_AD_FEE_PCT) + EBAY_FIXED_FEE
    net = sale_total - fees - ship_cost
    cost = ross_price * (1 + SOURCING_SALES_TAX)
    profit = net - cost
    return {
        "sale_total": round(sale_total, 2),
        "fees": round(fees, 2),
        "ship_cost": round(ship_cost, 2),
        "net": round(net, 2),
        "cost_with_tax": round(cost, 2),
        "profit": round(profit, 2),
        "roi": round(profit / cost, 2) if cost else None,
    }


def break_even_price(ross_price: float, ship_cost: float) -> float:
    """Sale total (item + shipping paid by buyer) at which profit is exactly $0."""
    cost = ross_price * (1 + SOURCING_SALES_TAX)
    return round((cost + ship_cost + EBAY_FIXED_FEE) / (1 - EBAY_FVF_PCT - EBAY_AD_FEE_PCT), 2)


def verdict_for(profit: dict | None, tier: str) -> str | None:
    if not profit:
        return None
    if profit["profit"] >= 10 and (profit["roi"] or 0) >= 0.5:
        return "BUY" if tier == "solid" else "MAYBE (no solid sold data - check by hand)"
    if profit["profit"] >= 0:
        note = "" if tier == "solid" else ", and no solid sold data"
        return f"BORDERLINE (bundle or skip{note})"
    return "SKIP"


# ---------------------------------------------------------------- main entry
def get_comps(description: str, required: list[str] | None = None,
              size: list[str] | str | None = None, brand: str | None = None,
              query: str | None = None, cost: float | None = None,
              ship: float | None = None, womens: bool = False,
              details: bool = True, exclude: list[str] | None = None) -> dict:
    query = clean_query(query or description)
    codes = auto_required_tokens(description)
    strict_req = required if required is not None else codes
    sizes = [size] if isinstance(size, str) else list(size or [])
    size_pats = size_patterns(sizes)
    broad_query = clean_query(strip_codes(query, codes))
    can_broaden = bool(broad_query) and broad_query.lower() != query.lower()
    # Broad pass (style code dropped): a title must contain YOUR words - "Must contain" if you
    # gave some, otherwise every word of the description. That keeps other models of the same
    # brand out (a "Hemi" search no longer returns every backpack the brand makes).
    loose_req = list(required) if required else distinct_words(broad_query)

    exclude_re = (re.compile(r"\b(" + "|".join(re.escape(w) for w in exclude) + r")\b", re.I)
                  if exclude else None)

    def keep(rows, req, own_codes=None):
        return [r for r in rows
                if r["price"] and 5 <= r["price"] <= 2000 and not _is_used(r["condition"])
                and matches(r["title"], req, size_pats, brand, womens, own_codes, exclude_re)]

    # ---- SOLD
    calls = []

    def run(q, n):
        rows = fetch_sold(q, n)
        calls.append({"query": q, "count": n, "rows": len(rows)})
        return rows

    sold_raw = run(query, SOURCING_SOLD_FETCH)
    sold = keep(sold_raw, strict_req)
    sold_level = "exact"
    if len(sold) < SOURCING_MIN_SOLD and len(sold_raw) >= SOURCING_SOLD_FETCH:
        sold_raw = run(query, SOURCING_SOLD_FETCH_WIDE)
        sold = keep(sold_raw, strict_req)
    if len(sold) < SOURCING_MIN_SOLD and can_broaden:
        more_raw = run(broad_query, SOURCING_SOLD_FETCH)
        merged = _merge(sold, keep(more_raw, loose_req, codes))
        sold_raw = sold_raw + more_raw
        if len(merged) > len(sold):
            sold, sold_level = merged, "broad"

    # ---- ACTIVE
    active_raw, active_total = fetch_active(query)
    active = keep(active_raw, strict_req)
    active_level = "exact"
    if can_broaden:
        more_raw, _ = fetch_active(broad_query)
        merged = _merge(active, keep(more_raw, loose_req, codes))
        if len(merged) > len(active):
            active, active_level = merged, "broad"

    detail_ok = detail_fail = 0
    if details and active:
        detail_ok, detail_fail = enrich_active(active, SOURCING_DETAIL_MAX)

    sold_prices = sorted(s["price"] for s in _iqr_trim(sold))
    active_prices = sorted(a["price"] for a in active)
    n_sold, n_active = len(sold), len(active)
    tier = _evidence_tier(sold_prices)   # solid / thin / none, same bars as pricing
    reliable = n_sold >= SOURCING_MIN_SOLD

    sold_median = _median(sold_prices)
    active_median = _median(active_prices)
    w_known = [a["watchers"] for a in active if a.get("watchers") is not None]
    q_known = [a["sold_qty"] for a in active if a.get("sold_qty") is not None]
    def code_hit(title):
        return bool(codes) and all(_norm(c) in _norm(title) for c in codes)

    for a in active:
        a["multi_size"] = is_multi_size(a["title"])
        a["code_match"] = code_hit(a["title"])
    for x in sold:
        x["code_match"] = code_hit(x["title"])
    sales_pairs = [(a["price"], a["sold_qty"]) for a in active if a.get("sold_qty")]
    # Weighted price: single-size listings only, each capped at SOURCING_UNITS_CAP units.
    weighted_pairs = [(a["price"], min(a["sold_qty"], SOURCING_UNITS_CAP))
                      for a in active if a.get("sold_qty") and not a["multi_size"]]
    n_ship_unknown = sum(1 for a in active if a.get("ship_known") is False)
    stats = {
        "ship_unknown": n_ship_unknown,
        "has_codes": bool(codes),
        "active_code_matches": sum(1 for a in active if a["code_match"]),
        "sold_code_matches": sum(1 for x in sold if x["code_match"]),
        "listings_with_sales": len(sales_pairs),
        "multi_size_listings": sum(1 for a in active if a["multi_size"]),
        # Median of the asking prices, weighted by units each listing has sold (lifetime of
        # the listing, not 90 days). Trusted from 5+ counted units.
        "units_sold_weighted_price": (_weighted_median(weighted_pairs)
                                      if sum(q for _, q in weighted_pairs) >= 5 else None),
        "watchers_total": sum(w_known) if w_known else None,
        "watchers_reported_by": len(w_known),
        "listings_with_watchers": sum(1 for w in w_known if w > 0),
        "active_units_sold_total": sum(q_known) if q_known else None,
        "active_units_sold_reported_by": len(q_known),
        "sold_count_90d": n_sold,
        "sold_used_for_price": len(sold_prices),
        "sold_median": round(sold_median, 2) if sold_median else None,
        "sold_p10": _quantile(sold_prices, 0.10),
        "sold_p90": _quantile(sold_prices, 0.90),
        "dispersion": _dispersion(sold_prices),
        "evidence": tier,
        "match_level_sold": sold_level,
        "active_count": n_active,
        "active_lowest": active_prices[0] if active_prices else None,
        "active_median": round(active_median, 2) if active_median else None,
        "match_level_active": active_level,
        # Only meaningful when enough sold rows matched; otherwise it's noise.
        "sell_through": round(n_sold / (n_sold + n_active), 2) if reliable and (n_sold + n_active) else None,
        "saturated": (n_active > 3 * n_sold) if reliable else None,
    }

    warnings = []
    if len(sold_raw) >= 3 and not any(s["price"] for s in sold_raw):
        warnings.append("sold scraper returned rows with no parseable price - "
                        "the Apify actor's output schema may have changed")
    if n_sold == 0:
        warnings.append("no matching sales found - sold numbers unavailable")
    elif not reliable:
        warnings.append(f"only {n_sold} matching sale(s) - sold price is low confidence")
    if sold_level == "broad" or active_level == "broad":
        warnings.append("some matches do not contain the style code; they matched every word of "
                        "your description instead - check the titles below")
    if n_ship_unknown:
        warnings.append(f"{n_ship_unknown} active listing(s) gave no shipping cost - their totals "
                        "leave shipping out, so they look cheaper than they are")
    if detail_fail:
        warnings.append(f"{detail_fail} listing lookup(s) failed - watcher/units-sold data is partial")
    if stats["dispersion"] and stats["dispersion"] > 2.5:
        warnings.append(f"sold prices spread {stats['dispersion']}x - matches may be "
                        "different products; tighten --require / --size")
    if reliable and len(sold_raw) >= SOURCING_SOLD_FETCH_WIDE and n_sold >= 10:
        warnings.append(f"{n_sold}+ matching sales found - demand is strong")

    # ---- reference price: the best available estimate of what it sells for
    if tier != "none" and sold_median:
        ref, ref_label = sold_median, "sold median"
    elif stats["units_sold_weighted_price"]:
        ref = stats["units_sold_weighted_price"]
        ref_label = "units-sold-weighted price of active listings (NOT 90-day sold)"
    elif active_prices:
        ref = _quantile(active_prices, 0.25)
        ref_label = "active 25th percentile (asking price, NOT sold)"
    else:
        ref, ref_label = None, None

    # ---- profit
    profit = profit_lowest = be = verdict = None
    if cost is not None:
        ship_cost = ship if ship is not None else EBAY_SHIP_COST
        be = break_even_price(cost, ship_cost)
        if ref:
            profit = profit_at(ref, cost, ship_cost)
            verdict = verdict_for(profit, tier)
        if stats["active_lowest"]:
            profit_lowest = profit_at(stats["active_lowest"], cost, ship_cost)

    return {
        "description": description,
        "query": query,
        "required_tokens": strict_req,
        "size": sizes,
        "brand": brand,
        "stats": stats,
        "cost": cost,
        "reference": {"price": ref, "label": ref_label},
        "assumptions": {
            "fvf_pct": EBAY_FVF_PCT, "ad_pct": EBAY_AD_FEE_PCT, "fixed_fee": EBAY_FIXED_FEE,
            "sales_tax": SOURCING_SALES_TAX, "ship_cost": EBAY_SHIP_COST,
            "active_p25": _quantile(active_prices, 0.25) if active_prices else None,
        },
        "break_even_sale_price": be,
        "profit_reference": ref_label,
        "profit": profit,
        "profit_at_lowest_active": profit_lowest,
        "verdict": verdict,
        "warnings": warnings,
        "links": {**search_urls(query),
                  **({"sold_search_broad": search_urls(broad_query)["sold_search"]}
                     if sold_level == "broad" else {})},
        "sold": sorted(sold, key=lambda x: x["price"]),
        "active": sorted(active, key=lambda x: x["price"]),
        "debug": {
            "apify_calls": calls,
            "apify_rows_billed": sum(c["rows"] for c in calls),
            "sold_fields_seen": sorted(_sold_fields_seen),
            "sold_rows_with_link": sum(1 for x in sold if x.get("url")),
            "sold_scraped": len(sold_raw),
            "sold_matched": n_sold,
            "active_ebay_total": active_total,
            "active_fetched": len(active_raw),
            "active_matched": n_active,
            "buyer_zip": SOURCING_BUYER_ZIP,
            "detail_lookups_ok": detail_ok,
            "detail_lookups_failed": detail_fail,
        },
    }


def _dash(v) -> str:
    return "-" if v is None else str(v)


def _print_report(res: dict, top: int = 15, show_links: bool = False):
    s = res["stats"]
    print(f"\nQuery: {res['query']}")
    print(f"Must match -> codes/words: {res['required_tokens'] or '-'}  "
          f"size: {res['size'] or '-'}  brand: {res['brand'] or '-'}")
    print(f"\nSOLD (90d):  {s['sold_count_90d']} matched   median ${s['sold_median']}   "
          f"p10 ${s['sold_p10']}   p90 ${s['sold_p90']}   "
          f"evidence: {s['evidence']}   ({s['match_level_sold']} match)")
    print(f"ACTIVE now:  {s['active_count']} matched   lowest ${s['active_lowest']}   "
          f"median ${s['active_median']}   ({s['match_level_active']} match)")
    show_w = s["watchers_reported_by"] > 0
    if show_w:
        print(f"Watchers: {s['watchers_total']} total | {s['listings_with_watchers']} of "
              f"{s['active_count']} listings have watchers | {s['watchers_reported_by']} of "
              f"{s['active_count']} report a count")
    else:
        print("Watchers: eBay does not report watch counts for these listings (column hidden)")
    if s["active_units_sold_total"] is not None:
        print(f"Units already sold by active listings: {s['active_units_sold_total']} across "
              f"{s['listings_with_sales']} listings ({s['active_units_sold_reported_by']} of "
              f"{s['active_count']} checked; lifetime of each listing, not 90 days)")
        if s["units_sold_weighted_price"]:
            print(f"Units-sold-weighted price: ${s['units_sold_weighted_price']} "
                  f"(multi-size listings excluded; each listing capped at {SOURCING_UNITS_CAP} units)")
        best = sorted((a for a in res["active"] if a.get("sold_qty")),
                      key=lambda x: -x["sold_qty"])[:5]
        if best:
            print("Best sellers among active listings:")
            for a in best:
                flag = " (multi-size)" if a.get("multi_size") else ""
                print(f"  {a['sold_qty']:>3} sold | ${a['price']:>7.2f} | {a['title'][:62]}{flag}")
    if s["sell_through"] is not None:
        print(f"Sell-through: {s['sell_through']}   Saturated: {s['saturated']}")
    else:
        print("Sell-through: n/a (not enough matching sales to judge)")
    if res["cost"] is not None:
        print(f"\nBreak-even sale price: ${res['break_even_sale_price']} "
              f"(for a ${res['cost']} Ross price)")
        p = res["profit"]
        if p:
            print(f"At {res['profit_reference']} ${p['sale_total']}: fees ${p['fees']} + "
                  f"ship ${p['ship_cost']} -> net ${p['net']} | cost ${p['cost_with_tax']} "
                  f"-> PROFIT ${p['profit']} (ROI {p['roi']})")
        pl = res["profit_at_lowest_active"]
        if pl:
            print(f"If you match the lowest active (${pl['sale_total']}): "
                  f"PROFIT ${pl['profit']} (ROI {pl['roi']})")
        if res["verdict"]:
            print(f"VERDICT: {res['verdict']}")
    for w in res["warnings"]:
        print(f"! {w}")
    d = res["debug"]
    print(f"\nApify: {len(d['apify_calls'])} call(s), {d['apify_rows_billed']} rows -> {d['apify_calls']}")
    print(f"eBay Browse: {d['active_ebay_total']} total, {d['active_fetched']} fetched, "
          f"{d['active_matched']} matched")
    for label, rows in (("SOLD", res["sold"]), ("ACTIVE (cheapest first)", res["active"])):
        print(f"\n--- {label}: {len(rows)} ---")
        for it in rows[:top]:
            tag = ""
            if label.startswith("ACTIVE"):
                tag = (f"W:{_dash(it.get('watchers'))} " if show_w else "") + f"S:{_dash(it.get('sold_qty'))}  "
            print(f"${it['price']:>8.2f}  {tag}{it['title'][:80]}")
            if show_links:
                print(f"          {it['url'] or '(no link from source)'}")
        if len(rows) > top:
            print(f"  ... {len(rows) - top} more (use --top 100 or --json)")
    print("\nOpen in browser:")
    for name, url in res["links"].items():
        print(f"  {name}: {url}")
    print()


def write_html_report(res: dict, path: str = "comps_report.html") -> str:
    """Clickable report: every listing links to its eBay page."""
    s = res["stats"]

    show_w = s["watchers_reported_by"] > 0

    def table(items, extras=False):
        rows = []
        for it in items:
            t = html.escape(it["title"])
            cell = (f'<a href="{html.escape(it["url"])}" target="_blank">{t}</a>'
                    if it.get("url") else f"{t} <i>(no link)</i>")
            if it.get("multi_size"):
                cell += " <i>(multi-size)</i>"
            ex = ((f"<td>{_dash(it.get('watchers'))}</td>" if show_w else "")
                  + f"<td>{_dash(it.get('sold_qty'))}</td>") if extras else ""
            rows.append(f"<tr><td>${it['price']:.2f}</td>{ex}<td>{cell}</td></tr>")
        return "\n".join(rows) or "<tr><td colspan=4>none</td></tr>"

    links = " | ".join(f'<a href="{html.escape(u)}" target="_blank">{n}</a>'
                       for n, u in res["links"].items())
    profit = ""
    if res["cost"] is not None and res["profit"]:
        p = res["profit"]
        profit = (f"<p><b>Break-even ${res['break_even_sale_price']}</b> | at "
                  f"{html.escape(res['profit_reference'])} ${p['sale_total']}: "
                  f"profit <b>${p['profit']}</b> (ROI {p['roi']}) | "
                  f"<b>{html.escape(res['verdict'] or '')}</b></p>")
    warn = "".join(f"<li>{html.escape(w)}</li>" for w in res["warnings"])
    doc = f"""<!doctype html><meta charset="utf-8"><title>Comps: {html.escape(res['query'])}</title>
<style>body{{font:15px system-ui;margin:24px;max-width:1000px}}table{{border-collapse:collapse;width:100%}}
td{{border-bottom:1px solid #ddd;padding:4px 8px}}td:first-child{{white-space:nowrap;font-weight:600}}</style>
<h2>{html.escape(res['query'])}</h2>
<p>SOLD (90d): <b>{s['sold_count_90d']}</b> matched, median ${s['sold_median']}, evidence {s['evidence']}
| ACTIVE: <b>{s['active_count']}</b> matched, lowest ${s['active_lowest']}, median ${s['active_median']}
| units-sold-weighted price: <b>${s['units_sold_weighted_price']}</b></p>
{profit}<ul>{warn}</ul><p>{links}</p>
<h3>Sold ({len(res['sold'])})</h3><table>{table(res['sold'])}</table>
<h3>Active, cheapest first ({len(res['active'])}) &mdash; units sold by these listings: {_dash(s['active_units_sold_total'])}</h3>
<table><tr><th>Price</th>{'<th>Watchers</th>' if show_w else ''}<th>Units sold</th><th>Listing</th></tr>{table(res['active'], True)}</table>"""
    Path(path).write_text(doc, encoding="utf-8")
    return str(Path(path).resolve())


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="eBay sold + active comps for an exact item")
    ap.add_argument("description", help='e.g. "Converse Chuck Taylor Puff A14722C"')
    ap.add_argument("--require", nargs="*",
                    help="words/codes every title must contain (default: style codes found in description)")
    ap.add_argument("--size", nargs="+", help="e.g. 9  |  1.6  |  100ml  |  9 9.5")
    ap.add_argument("--womens", action="store_true", help="search women's sizes")
    ap.add_argument("--brand", help="brand the title must name, e.g. Burberry")
    ap.add_argument("--query", help="eBay search text, if different from the description")
    ap.add_argument("--cost", type=float, help="Ross shelf price (before tax), to calculate profit")
    ap.add_argument("--ship", type=float, help=f"your label cost (default ${EBAY_SHIP_COST} from config)")
    ap.add_argument("--top", type=int, default=15, help="listings to print per list (default 15)")
    ap.add_argument("--exclude", nargs="+",
                    help="drop titles containing these words, e.g. --exclude weekend fabric")
    ap.add_argument("--no-details", action="store_true",
                    help="skip the per-listing watcher / units-sold lookup (faster)")
    ap.add_argument("--links", action="store_true", help="print the eBay link under every listing")
    ap.add_argument("--html", action="store_true",
                    help="write comps_report.html (clickable links) and open it in your browser")
    ap.add_argument("--json", action="store_true", help="print full JSON")
    a = ap.parse_args()

    result = get_comps(a.description, a.require, a.size, a.brand, a.query,
                       a.cost, a.ship, a.womens, details=not a.no_details,
                       exclude=a.exclude)
    if a.json:
        print(json.dumps(result, indent=2, default=str))
    else:
        _print_report(result, a.top, a.links)
    if a.html:
        path = write_html_report(result)
        print(f"Report: {path}")
        webbrowser.open("file:///" + path.replace("\\", "/"))