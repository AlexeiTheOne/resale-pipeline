import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import concurrent.futures
import re
import time
import httpx
import json
import os
from dotenv import load_dotenv
from config import (
    ACTIVE_COUNT, ACTIVE_FETCH_COUNT, ACTIVE_FLOOR_MIN_RATIO, COMPS_COUNT,
    COMPS_FETCH_COUNT, DEBUG_MODE, PRICE_SOLID_MAX_DISPERSION,
    PRICE_SOLID_MIN_COMPS, PRICE_THIN_MAX_DISPERSION, PRICE_THIN_MIN_COMPS,
    RESEARCH_SANITY_RATIO, UNDERCUT_PCT,
)

load_dotenv()
APIFY_TOKEN = os.getenv("APIFY_TOKEN")

# Mapped by ACTUAL behavior (re-confirmed against the live actors):
#   SOLD   -> oTtB3VgfuE9GtxQt2 , input {"keyword": q, "count": N},
#             fields: soldPrice/totalPrice (strings), title, condition, thumbnailUrl
#             NOTE: this actor ignores "maxProductsPerSearch" and falls back to a
#             default of 100 rows — the size knob here is "count".
#   ACTIVE -> Y7h6Aodb7ZXkv6Ieb , input {"searchQueries":[q], "maxProductsPerSearch": N},
#             fields: price (num)/priceString, title, condition, thumbnail, images[]
# Neither takes a condition filter here: eBay's maps to "New" (1000) only, which
# throws away new-without-tags / open-box comps. Used rows are dropped locally
# instead (_is_used), which keeps the pool wide and the filtering visible.
SOLD_URL = "https://api.apify.com/v2/acts/oTtB3VgfuE9GtxQt2/run-sync-get-dataset-items"
ACTIVE_URL = "https://api.apify.com/v2/acts/Y7h6Aodb7ZXkv6Ieb/run-sync-get-dataset-items"


def _call(url, payload, attempts=3):
    """POST one Apify run-sync scrape, retrying transient failures.

    These are long-held connections (a scrape can run for minutes) and Apify
    drops one now and then — mid-run connection resets and 5xx/429s are routine.
    The query ladder makes up to one call per rung, so a single un-retried blip
    would fail the whole pricing run; retrying here keeps a network hiccup from
    costing the item its comps.

    The token goes in an Authorization header, NOT the query string. httpx builds
    HTTPStatusError's message as "... for url '<full url>'", query string
    included, and three separate paths put str(e) into a Telegram message —
    _run_stage's error reply, /pricecheck's, and the unprompted weekly digest.
    A 402 (quota exhausted) or 404 (actor retired) leaves the token perfectly
    valid and pastes it into a chat log that keeps it forever."""
    last = None
    headers = {"Authorization": f"Bearer {APIFY_TOKEN}"} if APIFY_TOKEN else {}
    for attempt in range(attempts):
        try:
            r = httpx.post(url, headers=headers, json=payload, timeout=180)
            if r.status_code == 429 or r.status_code >= 500:
                raise httpx.HTTPStatusError(
                    f"Apify returned {r.status_code}", request=r.request, response=r)
            r.raise_for_status()
            data = r.json()
            return data if isinstance(data, list) else []
        except (httpx.TransportError, httpx.HTTPStatusError) as e:
            last = e
            if attempt < attempts - 1:
                time.sleep(2 ** attempt)
    raise last


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
    if not n:
        return None
    mid = n // 2
    return vals[mid] if n % 2 else (vals[mid - 1] + vals[mid]) / 2


def _quantile(sorted_vals, q):
    if not sorted_vals:
        return None
    idx = int(round(q * (len(sorted_vals) - 1)))
    return sorted_vals[min(len(sorted_vals) - 1, max(0, idx))]


def _iqr_trim(comps):
    """Drop comps whose price falls outside 1.5*IQR — the mis-matched products
    that share search words with the real item ("garden tool tote" priced against
    a handbag). Below 4 comps there's no distribution to speak of, so nothing is
    trimmed and the dispersion check downstream does the filtering instead."""
    prices = sorted(c["price"] for c in comps)
    if len(prices) < 4:
        return list(comps)
    q1, q3 = _quantile(prices, 0.25), _quantile(prices, 0.75)
    iqr = q3 - q1
    if iqr <= 0:
        return list(comps)
    lo, hi = q1 - 1.5 * iqr, q3 + 1.5 * iqr
    return [c for c in comps if lo <= c["price"] <= hi]


def _dispersion(prices):
    """p90/p10 of the (already trimmed) comp prices — how much the "comparable"
    sales actually disagree. 1.0 is identical; past ~2.5 they aren't the same
    product and their median is not a price."""
    if len(prices) < 2:
        return None
    p10, p90 = _quantile(prices, 0.10), _quantile(prices, 0.90)
    if not p10:
        return None
    return round(p90 / p10, 2)


_USED_MARKERS = ("used", "pre-owned", "preowned", "pre owned", "refurb")


def _is_used(condition):
    """Ross stock is new, so a used comp is the wrong market. Filtered here on
    the row's own condition text rather than via the scrapers' condition
    parameter: that parameter maps to eBay's "New" (condition 1000) alone and
    discards new-without-tags / open-box comps, which is most of the pool for
    apparel."""
    return any(m in (condition or "").lower() for m in _USED_MARKERS)


def _query_ladder(search_query, research):
    """Search strings to try, most specific first. The identify step's
    search_query carries color/size/style detail that is great when eBay has that
    exact item and returns nothing at all when it doesn't — which is why most
    items used to end up with no comps. Each rung drops a layer of specificity so
    a miss degrades to a broader real search instead of straight to a guess."""
    brand = (research.get("brand") or "").strip()
    product = (research.get("product_name") or "").strip()
    item_type = (research.get("item_type") or "").strip()

    candidates = [("exact", (search_query or "").strip())]
    if brand and product:
        candidates.append(("product", f"{brand} {product}"))
    if brand and item_type:
        candidates.append(("category", f"{brand} {item_type}"))

    ladder, seen = [], set()
    for name, query in candidates:
        if query and query.lower() not in seen:
            seen.add(query.lower())
            ladder.append((name, query))
    return ladder


def _evidence_tier(prices):
    """How much this comp set can be trusted:
      solid — enough agreeing sales to price from without asking anyone
      thin  — usable, but a human should look at it
      none  — no price may be derived from this; the caller must ask.
    Count alone is not enough: 10 comps spanning 9x are worse evidence than 4
    that agree, so both bars have to be cleared."""
    n = len(prices)
    disp = _dispersion(prices)
    if n >= PRICE_SOLID_MIN_COMPS and (disp is None or disp <= PRICE_SOLID_MAX_DISPERSION):
        return "solid"
    if n >= PRICE_THIN_MIN_COMPS and (disp is None or disp <= PRICE_THIN_MAX_DISPERSION):
        return "thin"
    return "none"


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


def _brand_match(title, brand):
    """Does this comp's title actually name the brand? Accepts the brand as a
    phrase or as all of its words present separately ("Calvin Klein Jeans" vs a
    title reading "Jeans by Calvin Klein").

    Matching is on whole words: a substring test lets "Old Navy" match "Womens
    Gold Navy Blue Blouse" ("old" inside "gold"), and since the unfiltered-comps
    fallback is gone, false positives like that become the entire comp set and
    can be graded solid."""
    text = (title or "").lower()
    brand = (brand or "").lower().strip()
    if not brand:
        return False
    words = brand.split()
    if not words:
        return False
    if re.search(rf"\b{re.escape(brand)}\b", text):
        return True
    return all(re.search(rf"\b{re.escape(word)}\b", text) for word in words)


def _fetch_rung(query, brand, fetch_sold, fetch_active, comps_count, active_count):
    """One rung of the query ladder: pull sold + active rows for `query` in
    parallel, parse them, drop used comps, keep the brand-relevant ones, and
    outlier-trim the sold set. Returns everything the caller needs to judge
    whether this rung is good enough to stop at."""

    def _brand_relevant(comps):
        # A real comp for a branded item must name the brand: otherwise a
        # "Tommy Hilfiger" handbag gets priced off "garden tool totes". This used
        # to fall back to the UNFILTERED list when nothing matched, on the theory
        # that some comps beat none. That's false, and actively harmful now that
        # the ladder broadens the query on a miss: the broadest rung would return
        # whatever eBay had for the bare category and the dispersion check would
        # happily call it solid evidence. No brand match means no comps, which
        # lands on the "none" tier and asks a human — the correct failure.
        if not brand:
            return comps
        return [c for c in comps if _brand_match(c["title"], brand)]

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        sold_future = pool.submit(_call, SOLD_URL, {"keyword": query, "count": fetch_sold})
        active_future = pool.submit(
            _call, ACTIVE_URL, {"searchQueries": [query], "maxProductsPerSearch": fetch_active})
        raw_sold = sold_future.result()
        raw_active = active_future.result()

    sold_comps, sold_parseable = [], 0
    for item in raw_sold:
        price = _to_float(item.get("totalPrice")) or _to_float(item.get("soldPrice"))
        if price is not None:
            sold_parseable += 1
        if price is None or not (5 <= price <= 500) or _is_used(item.get("condition")):
            continue
        sold_comps.append({
            "price": round(price, 2),
            "title": item.get("title", ""),
            "condition": item.get("condition", ""),
            "image": item.get("thumbnailUrl", ""),
            "url": item.get("url", ""),
        })

    active_listings, active_parseable = [], 0
    for item in raw_active:
        price = _to_float(item.get("price")) or _to_float(item.get("priceString"))
        if price is not None:
            active_parseable += 1
        if price is None or not (5 <= price <= 500) or _is_used(item.get("condition")):
            continue
        imgs = item.get("images") or []
        active_listings.append({
            "price": round(price, 2),
            "title": item.get("title", ""),
            "condition": item.get("condition", ""),
            "image": (imgs[0] if imgs else item.get("thumbnail", "")),
            "url": item.get("url", ""),
        })

    # Trim BEFORE capping to comps_count, so the cap keeps surviving comps rather
    # than truncating the pool the trim still needs to see.
    sold_comps = _iqr_trim(_brand_relevant(sold_comps))[:comps_count]
    # Sorted by price BEFORE the cap. active_floor downstream is min() of whatever
    # survives this slice and is used as "the cheapest competitor we must undercut"
    # — but the scraper returns rows in eBay's own order, so an unsorted slice made
    # that floor a function of where a listing happened to land in the results.
    # Pull the genuinely cheapest ACTIVE_COUNT and the floor means what it says.
    active_listings = sorted(_brand_relevant(active_listings), key=lambda a: a["price"])[:active_count]
    sold_prices = sorted(c["price"] for c in sold_comps)

    return {
        "query": query,
        "sold_comps": sold_comps,
        "sold_prices": sold_prices,
        "active_listings": active_listings,
        "tier": _evidence_tier(sold_prices),
        "dispersion": _dispersion(sold_prices),
        "raw_sold": len(raw_sold),
        "raw_active": len(raw_active),
        "sold_parseable": sold_parseable,
        "active_parseable": active_parseable,
    }


def get_pricing(search_query: str, research: dict | None = None) -> dict:
    research = research or {}
    brand = (research.get("brand") or "").strip()
    comps_count = 3 if DEBUG_MODE else COMPS_COUNT
    active_count = 3 if DEBUG_MODE else ACTIVE_COUNT
    fetch_sold = 3 if DEBUG_MODE else COMPS_FETCH_COUNT
    fetch_active = 3 if DEBUG_MODE else ACTIVE_FETCH_COUNT

    # Walk the ladder specific -> broad, stopping at the first rung whose comps
    # are good enough to price from. Rungs run sequentially (each is a ~1min
    # scrape) but only on a miss, so a well-identified item still costs one pass.
    ladder = _query_ladder(search_query, research)
    if not ladder:
        # Nothing to search on at all (no query, no brand, no product/type). Not
        # reachable from identify.py, which backstops search_query, but a typed
        # correction through revise_identification has no such net — and an empty
        # ladder would otherwise blow up on max() of no attempts.
        return {
            "suggested_price": None, "machine_price": None,
            "confidence": "none", "price_basis": "none",
            "evidence": "none", "needs_review": True,
            "review_flags": ["no_query"],
            "comp_warning": "nothing to search comps with — the item has no search "
                            "query, brand, or product type. Fix the identification "
                            "or type a price.",
            "sold_count": 0, "sold_comps": [], "active_count": 0, "active_listings": [],
            "sold_median": None, "sold_p10": None, "sold_p90": None,
            "active_floor": None, "reference": None, "stock_image_url": None,
            "price_source_url": None, "query_rung": None, "query_used": None,
            "rungs_tried": [], "dispersion": None,
            "retail_price": research.get("retail_price"),
            "resale_estimate": research.get("resale_estimate"),
            "research_resale": None,
            "price_evidence": research.get("price_evidence"),
        }
    attempts = []
    for name, query in ladder:
        rung = _fetch_rung(query, brand, fetch_sold, fetch_active, comps_count, active_count)
        rung["rung"] = name
        attempts.append(rung)
        if rung["tier"] != "none":
            break

    # Nothing cleared the bar: keep the rung with the most comps for display, so
    # the gate can still show what little was found.
    best = max(attempts, key=lambda r: (r["tier"] != "none", len(r["sold_prices"])))

    sold_comps = best["sold_comps"]
    sold_prices = best["sold_prices"]
    active_listings = best["active_listings"]
    active_prices = [a["price"] for a in active_listings]
    tier = best["tier"]

    # Canary for scraper schema drift: the Apify actors are third-party and mapped
    # "by ACTUAL behavior" (see the module header) — if an actor changes its output
    # field names, every row parses to no price and the comp lists go empty. Rows
    # returned but none parseable is the tell, checked across every rung tried.
    # Surfaced at the price gate rather than swallowed. (Zero rows is NOT flagged —
    # that's often just a genuinely rare item with no comps.)
    # comp_warnings are shown to the human; review_flags are the subset that mean
    # "don't trust this unattended". The distinction matters once the pipeline
    # auto-confirms solid prices: holding a price up off the p10 floor is a
    # correct action worth explaining, not a reason to stop and ask.
    comp_warnings, review_flags = [], []
    if any(a["raw_sold"] >= 3 and a["sold_parseable"] == 0 for a in attempts):
        comp_warnings.append(
            "sold-comp scraper returned rows but none had a parseable price — "
            "the Apify actor's output schema may have changed")
        review_flags.append("scraper_schema")
    if any(a["raw_active"] >= 3 and a["active_parseable"] == 0 for a in attempts):
        comp_warnings.append(
            "active-listing scraper returned rows but none had a parseable price — "
            "the Apify actor's output schema may have changed")
        review_flags.append("scraper_schema")

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
    price_source_url = None
    pool = [{**c, "source": "sold"} for c in sold_comps] + \
           [{**a, "source": "active"} for a in active_listings]
    if pool:
        top = max(pool, key=lambda x: _similarity(x["title"], search_query))
        if _similarity(top["title"], search_query) >= 0.25:
            reference = {
                "title": top["title"],
                "price": top["price"],
                "condition": top["condition"],
                "source": top["source"],
                "url": top.get("url") or None,
            }
            stock_image_url = _upgrade_image(top.get("image"))
            price_source_url = top.get("url") or None

    base = {
        "evidence": tier,
        "query_rung": best["rung"],
        "query_used": best["query"],
        "rungs_tried": [a["rung"] for a in attempts],
        "dispersion": best["dispersion"],
        "sold_count": len(sold_prices),
        "sold_comps": sold_comps,
        "active_count": len(active_prices),
        "active_listings": active_listings,
        "reference": reference,
        "stock_image_url": stock_image_url,
        "price_source_url": price_source_url,
        **research_fields,
    }

    if tier == "none":
        # No comp evidence worth pricing from. Say so instead of emitting a
        # number: the research resale figure is an LLM estimate, and pricing off
        # it is what produced the prices that got overridden. It's returned as
        # context for the human, never as the price.
        comp_warnings.append(
            f"no usable sold comps after {len(attempts)} search(es) — "
            "type a price (research estimate and any comps found are shown above)")
        return {
            "suggested_price": None,
            "machine_price": None,
            "confidence": "none",
            "price_basis": "none",
            "sold_median": None,
            "sold_p10": None,
            "sold_p90": None,
            "active_floor": None,
            "comp_warning": "; ".join(comp_warnings) or None,
            "review_flags": review_flags + ["no_comps"],
            "needs_review": True,
            **base,
        }

    median = _median(sold_prices)
    p10, p90 = _quantile(sold_prices, 0.10), _quantile(sold_prices, 0.90)

    # The cheapest active listing only caps our price when it's plausibly the
    # same product. A listing far below the sold median is a different item (or a
    # damaged one), and letting it set the ceiling is how a comp-backed price
    # gets dragged to a fraction of what the thing actually sells for.
    raw_active_floor = min(active_prices) if active_prices else None
    active_floor = raw_active_floor
    if raw_active_floor is not None and raw_active_floor < median * ACTIVE_FLOOR_MIN_RATIO:
        active_floor = None
        comp_warnings.append(
            f"cheapest active listing (${raw_active_floor}) is far below the sold "
            f"median (${round(median, 2)}) — ignored as a different product")

    suggested = _finalize_price(median, active_floor)

    # Undercutting the cheapest competitor is only sane down to a point. p10 is
    # the 10th percentile of PROVEN sales — people demonstrably paid it — so a
    # price below that isn't competing, it's leaving money on the table because
    # one competitor happens to be cheap. The undercut and the active-floor cap
    # compound (15% off the median, then 5% under the floor), which is how a
    # $32 median turned into a $16 suggestion. Proven sales win.
    p10_bound = round(max(round(p10) - 0.01, 0.99), 2)
    if suggested < p10_bound:
        comp_warnings.append(
            f"undercutting the cheapest active listing would price this at "
            f"${suggested}, below the ${p10_bound} that comparable items actually "
            f"sold for — held at ${p10_bound}")
        suggested = p10_bound

    # Research is a sanity check, not a price source: a large disagreement means
    # one of the two is describing a different item, which is worth a human look.
    if research_resale and suggested:
        ratio = max(research_resale, suggested) / min(research_resale, suggested)
        if ratio > RESEARCH_SANITY_RATIO:
            comp_warnings.append(
                f"comps say ${suggested} but research estimated ${round(research_resale, 2)} "
                f"({ratio:.1f}x apart) — check the comps match the item")
            review_flags.append("research_conflict")

    return {
        "suggested_price": suggested,
        # Immutable record of what the machine actually proposed. A manual
        # override replaces suggested_price; this stays, so the gap between the
        # two can be measured over time instead of being overwritten and lost.
        "machine_price": suggested,
        "comp_suggested": suggested,
        "price_basis": "comps",
        "confidence": tier,
        "sold_median": round(median, 2),
        "sold_p10": round(p10, 2),
        "sold_p90": round(p90, 2),
        "active_floor": active_floor,
        "raw_active_floor": raw_active_floor,
        "comp_warning": "; ".join(comp_warnings) or None,
        "review_flags": review_flags,
        # Solid evidence with nothing suspicious about it is safe to act on
        # unattended; anything else wants eyes.
        "needs_review": bool(review_flags) or tier != "solid",
        **base,
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
