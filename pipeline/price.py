import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import concurrent.futures
import re
import httpx
import json
import os
from dotenv import load_dotenv
from config import COMPS_COUNT, ACTIVE_COUNT, DEBUG_MODE, UNDERCUT_PCT

load_dotenv()
APIFY_TOKEN = os.getenv("APIFY_TOKEN")

# Mapped by ACTUAL behavior (confirmed via debug):
#   SOLD   -> oTtB3VgfuE9GtxQt2 , input {"keyword": q, "maxProductsPerSearch": N},
#             fields: soldPrice/totalPrice (strings), title, condition, thumbnailUrl
#   ACTIVE -> Y7h6Aodb7ZXkv6Ieb , input {"searchQueries":[q], "count": N},
#             fields: price (num)/priceString, title, condition, thumbnail, images[]
SOLD_URL = "https://api.apify.com/v2/acts/oTtB3VgfuE9GtxQt2/run-sync-get-dataset-items"
ACTIVE_URL = "https://api.apify.com/v2/acts/Y7h6Aodb7ZXkv6Ieb/run-sync-get-dataset-items"


def _call(url, payload):
    r = httpx.post(url, params={"token": APIFY_TOKEN}, json=payload, timeout=180)
    r.raise_for_status()
    data = r.json()
    return data if isinstance(data, list) else []


def _to_float(value):
    if value is None:
        return None
    try:
        if isinstance(value, (int, float)):
            return float(value)
        return float(str(value).replace("$", "").replace(",", "").strip())
    except (ValueError, AttributeError):
        return None


def _median(vals):
    n = len(vals)
    mid = n // 2
    return vals[mid] if n % 2 else (vals[mid - 1] + vals[mid]) / 2


def _upgrade_image(url):
    """Bump eBay thumbnails to a large size (s-l1600). Reject empty/non-ebay urls."""
    if not url or "ebayimg.com" not in url:
        return None
    return re.sub(r"s-l\d+", "s-l1600", url)


def _similarity(a, b):
    wa, wb = set(a.lower().split()), set(b.lower().split())
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / len(wa | wb)


def _research_resale(research: dict):
    """Best resale figure the identify step found. Prefer its considered
    resale_estimate (it already weighed condition and sold-vs-asking); fall back
    to the median of the eBay prices it cited. Returns None if it has neither."""
    estimate = _to_float(research.get("resale_estimate"))
    if estimate is not None and estimate > 0:
        return estimate
    ebay_prices = sorted(
        p for e in (research.get("price_evidence") or [])
        if "ebay" in (e.get("source") or "").lower()
        and (p := _to_float(e.get("price"))) is not None and 5 <= p <= 1000
    )
    return _median(ebay_prices) if ebay_prices else None


def _finalize_price(base, active_floor):
    """Apply the standard undercut + active-floor cap and round to a .99 price."""
    suggested = base * (1 - UNDERCUT_PCT)
    if active_floor:
        suggested = min(suggested, active_floor * 0.95)
    return round(max(round(suggested) - 0.01, 0.99), 2)


def get_pricing(search_query: str, research: dict | None = None) -> dict:
    research = research or {}
    brand = (research.get("brand") or "").strip()
    comps_count = 3 if DEBUG_MODE else COMPS_COUNT
    active_count = 3 if DEBUG_MODE else ACTIVE_COUNT

    def _brand_relevant(comps):
        # A real comp for a branded item should mention the brand; without this,
        # a "Tommy Hilfiger" handbag gets priced off "garden tool totes". Only
        # narrow when the brand actually matches something, so we never end up
        # with zero comps purely because of a wording mismatch.
        if not brand:
            return comps
        hit = [c for c in comps if brand.lower() in c["title"].lower()]
        return hit if hit else comps

    # Fetch sold + active comps concurrently. Each is an independent Apify run-sync
    # scrape that can take a minute or more; running them in parallel rather than
    # back-to-back roughly halves the pricing wait.
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        sold_future = pool.submit(_call, SOLD_URL, {"keyword": search_query, "count": comps_count})
        active_future = pool.submit(
            _call, ACTIVE_URL,
            {"searchQueries": [search_query], "maxProductsPerSearch": active_count},
        )
        raw_sold = sold_future.result()
        raw_active = active_future.result()

    # --- SOLD comps ---
    sold_comps = []
    for item in raw_sold:
        price = _to_float(item.get("totalPrice")) or _to_float(item.get("soldPrice"))
        if price is not None and 5 <= price <= 500:
            sold_comps.append({
                "price": round(price, 2),
                "title": item.get("title", ""),
                "condition": item.get("condition", ""),
                "image": item.get("thumbnailUrl", ""),
            })
    sold_comps = _brand_relevant(sold_comps)[:comps_count]
    sold_prices = sorted(c["price"] for c in sold_comps)

    # --- ACTIVE listings ---
    active_listings = []
    for item in raw_active:
        price = _to_float(item.get("price")) or _to_float(item.get("priceString"))
        if price is not None and 5 <= price <= 500:
            imgs = item.get("images") or []
            active_listings.append({
                "price": round(price, 2),
                "title": item.get("title", ""),
                "condition": item.get("condition", ""),
                "image": (imgs[0] if imgs else item.get("thumbnail", "")),
            })
    active_listings = _brand_relevant(active_listings)[:active_count]
    active_prices = [a["price"] for a in active_listings]
    active_floor = min(active_prices) if active_prices else None

    # Price evidence the identify step gathered, surfaced for the draft step.
    research_resale = _research_resale(research)
    research_fields = {
        "retail_price": research.get("retail_price"),
        "resale_estimate": research.get("resale_estimate"),
        "research_resale": round(research_resale, 2) if research_resale else None,
        "price_evidence": research.get("price_evidence"),
    }

    # --- Best-match reference listing (prefer SOLD = proven sales) ---
    reference = None
    stock_image_url = None
    pool = [{**c, "source": "sold"} for c in sold_comps] + \
           [{**a, "source": "active"} for a in active_listings]
    if pool:
        best = max(pool, key=lambda x: _similarity(x["title"], search_query))
        if _similarity(best["title"], search_query) >= 0.25:
            reference = {
                "title": best["title"],
                "price": best["price"],
                "condition": best["condition"],
                "source": best["source"],
            }
            stock_image_url = _upgrade_image(best.get("image"))

    # The researched resale figure is a firm signal about THIS exact item, so it
    # acts as a price floor (undercut applied, but NOT subject to the active-floor
    # cap, which is often set by a loosely-related cheaper listing). This stops a
    # $138-retail bag being priced at $14 off generic same-brand comps.
    research_floor = _finalize_price(research_resale, None) if research_resale else None

    if len(sold_prices) < 3:
        # Too few proven sales to trust comps. Fall back to the eBay price
        # evidence the identify step found rather than returning no price.
        base = {
            "sold_count": len(sold_prices),
            "active_count": len(active_prices),
            "active_floor": active_floor,
            "active_listings": active_listings,
            "sold_comps": sold_comps,
            "reference": reference,
            "stock_image_url": stock_image_url,
            **research_fields,
        }
        if research_floor:
            return {
                "suggested_price": research_floor,
                "confidence": "research",
                "price_basis": "research",
                **base,
            }
        return {"suggested_price": None, "confidence": "insufficient", **base}

    n = len(sold_prices)
    median = _median(sold_prices)
    p10 = sold_prices[max(0, int(n * 0.1))]
    p90 = sold_prices[min(n - 1, int(n * 0.9))]

    comp_suggested = _finalize_price(median, active_floor)
    # Combine: trust comps when they're at/above the researched value, but never
    # let loose comps drag the price below what research says the item is worth.
    suggested = comp_suggested
    price_basis = "comps"
    if research_floor and research_floor > comp_suggested:
        suggested = research_floor
        price_basis = "research_floor"

    confidence = "high" if n >= 10 else "medium" if n >= 5 else "low"

    return {
        "suggested_price": suggested,
        "comp_suggested": comp_suggested,
        "price_basis": price_basis,
        "sold_median": round(median, 2),
        "sold_p10": round(p10, 2),
        "sold_p90": round(p90, 2),
        "sold_count": n,
        "active_floor": active_floor,
        "active_count": len(active_prices),
        "active_listings": active_listings,
        "sold_comps": sold_comps,
        "reference": reference,
        "stock_image_url": stock_image_url,
        "confidence": confidence,
        **research_fields,
    }


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python pipeline/price.py 'search query'")
        sys.exit(1)
    result = get_pricing(sys.argv[1])
    print(json.dumps(result, indent=2))
    if result.get("suggested_price"):
        print(f"\nSuggested listing price: ${result['suggested_price']}")
    else:
        print("\nWarning: insufficient data to suggest a price.")
