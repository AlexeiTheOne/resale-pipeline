"""Competitor search: who else sells this, at what price, and what sold
(docs/listing-performance-plan.md 7.1). Shared by /check, new listings and
/diagnose.

Match ladder, best first, and every row records which one found it:
  1. UPC     Browse GTIN search: eBay matches the identifier sellers filled in.
  2. MPN     brand + style code, kept only when the title carries the code.
  3. keywords brand + the item's words (its search query or title).

Active listings come from Browse (free). The best COMPETITOR_DETAIL_MAX also
get one getItem call each for what search results don't carry: item
specifics, photo count, returns, Best Offer. Sold listings come from Apify
(pricing_v2.fetch_sold), not Marketplace Insights.

Everything found is saved in check_evidence, which is also the cache: a model
searched in the last COMPETITOR_CACHE_HOURS is answered from the table. Our
own listings are left out (pricing_v2.is_own).
"""
import re
from datetime import datetime, timedelta, timezone

import config
import db
from ebay import browse
from pipeline import pricing_v2, sold_reuse
from pipeline.gtin import product_gtin
from pipeline.specifics import style_code

_NOISE = re.compile(r"\b(nwt|nwot|new with tags|new|women'?s|men'?s|unisex|one size)\b", re.I)


def _spec(specifics: dict | None, name: str):
    value = next((v for k, v in (specifics or {}).items() if k.strip().lower() == name), None)
    if isinstance(value, list):
        value = value[0] if value else None
    text = str(value).strip() if value is not None else ""
    return None if text.lower() in ("", "null", "none", "does not apply", "n/a") else text


def keys(ident: dict | None, specifics: dict | None = None, title: str | None = None) -> dict:
    """What to search by: brand, UPC, MPN (the style code) and keywords."""
    ident = ident or {}
    brand = (ident.get("brand") or _spec(specifics, "brand") or "").strip()
    upc = product_gtin(ident.get("upc") or _spec(specifics, "upc"))
    mpn = _spec(specifics, "mpn") or style_code(specifics, ident)
    words = ident.get("search_query") or title or " ".join(
        str(x) for x in (brand, ident.get("product_name") or ident.get("item_type"), ident.get("color")) if x)
    words = " ".join(_NOISE.sub(" ", words or "").split())
    if brand and brand.lower() not in words.lower():
        words = f"{brand} {words}"
    return {"brand": brand or None, "upc": upc, "mpn": mpn, "keywords": words or None}


def model_key(ident: dict | None, k: dict) -> str | None:
    """The cache key: sold_reuse's model key when there is one, else the UPC,
    else brand + keywords."""
    found = sold_reuse.model_keys(ident)
    if found:
        return found[0]
    if k.get("upc"):
        return f"upc|{k['upc']}"
    if k.get("keywords"):
        brand = re.sub(r"[^a-z0-9]", "", (k.get("brand") or "").lower())
        return f"{brand}|words|{' '.join(sorted(set(k['keywords'].lower().split())))}"
    return None


def _norm(s) -> str:
    return re.sub(r"[^a-z0-9]", "", str(s or "").lower())


def _has_code(title: str, code: str) -> bool:
    return bool(code) and _norm(code) in _norm(title)


def _search(query=None, gtin=None) -> list[dict]:
    return browse.search_active(query, gtin=gtin, limit=config.COMPETITOR_ACTIVE_LIMIT)


def find_active(k: dict) -> tuple[list[dict], str | None, str | None]:
    """(Browse rows, found_by, query) from the first rung that finds anything."""
    if k.get("upc"):
        rows = _search(gtin=k["upc"])
        if rows:
            return rows, "upc", f"gtin:{k['upc']}"
    if k.get("mpn") and k.get("brand"):
        q = f"{k['brand']} {k['mpn']}"
        rows = [r for r in _search(q) if _has_code(r.get("title"), k["mpn"])]
        if rows:
            return rows, "mpn", q
    if k.get("keywords"):
        rows = _search(k["keywords"])
        if rows:
            return rows, "keywords", k["keywords"]
    return [], None, None


def find_sold(k: dict) -> tuple[list[dict], str | None, str | None]:
    """(Apify sold rows, found_by, query). A UPC isn't searchable in sold
    titles, so sold starts at the MPN rung."""
    if k.get("mpn") and k.get("brand"):
        q = f"{k['brand']} {k['mpn']}"
        rows = [r for r in (pricing_v2.fetch_sold(q, count=config.COMPETITOR_SOLD_COUNT) or [])
                if _has_code(r.get("title"), k["mpn"])]
        if rows:
            return rows, "mpn", q
    if k.get("keywords"):
        rows = pricing_v2.fetch_sold(k["keywords"], count=config.COMPETITOR_SOLD_COUNT) or []
        if rows:
            return rows, "keywords", k["keywords"]
    return [], None, None


def _own():
    ids, titles = pricing_v2.own_listings()
    return config.OWN_SELLER_USERNAMES, ids, titles


def _active_rows(browse_rows, found_by, query, base) -> list[dict]:
    own = _own()
    out = []
    for s in browse_rows:
        c = pricing_v2.active_candidate(s, config.EBAY_SHIP_CHARGED)
        if c is None or pricing_v2.is_own(c, *own):
            continue
        ship = s.get("shipping_cost")   # the listing's own figure; None = unknown
        out.append({**base, "source": "ebay_active", "found_by": found_by, "query": query,
                    "store": c["seller"], "title": c["title"], "condition": c["condition"] or None,
                    "price": c["item_price"], "shipping": ship,
                    "total": round(c["item_price"] + ship, 2) if ship is not None else None,
                    "url": (c["url"] or "").split("?")[0] or None, "ebay_id": c["ebay_id"],
                    "best_offer": "BEST_OFFER" in (s.get("buying_options") or []),
                    "_browse_id": s.get("item_id")})
    return out


def _add_details(rows: list[dict]) -> None:
    """getItem for the first COMPETITOR_DETAIL_MAX rows, in place. A failed
    call leaves that row without details rather than failing the search."""
    for r in rows[:config.COMPETITOR_DETAIL_MAX]:
        if not r.get("_browse_id"):
            continue
        try:
            d = browse.item_detail(r["_browse_id"])
        except Exception as e:
            print(f"competitors: getItem {r['_browse_id']} failed: {type(e).__name__}")
            continue
        r.update({k: d[k] for k in ("specifics", "photo_count", "returns_accepted", "category_id")
                  if d.get(k) is not None})
        r["best_offer"] = d.get("best_offer", r.get("best_offer"))


def _sold_rows(apify_rows, found_by, query, base) -> list[dict]:
    own = _own()
    out = []
    for r in apify_rows:
        c = pricing_v2.sold_candidate(r, config.EBAY_SHIP_CHARGED)
        if c is None or pricing_v2.is_own(c, *own):
            continue
        out.append({**base, "source": "ebay_sold", "found_by": found_by, "query": query,
                    "store": c["seller"], "title": c["title"], "condition": c["condition"] or None,
                    "price": c["item_price"],
                    "shipping": None if c["shipping_imputed"] else c["shipping"],
                    "total": None if c["shipping_imputed"] else c["total"],
                    "url": (c["url"] or "").split("?")[0] or None, "ebay_id": c["ebay_id"],
                    "event_date": c["date"]})
    return out


def _save(rows: list[dict]) -> None:
    try:
        db.add_evidence([{k: v for k, v in r.items() if not k.startswith("_")} for r in rows])
    except Exception as e:   # the search result is still good without its record
        print(f"competitors: evidence not saved: {type(e).__name__}: {e}")


def search(ident: dict | None, *, specifics: dict | None = None, title: str | None = None,
           item_id: str | None = None, check_id: int | None = None, sold: bool = True,
           now: datetime | None = None) -> dict:
    """Competitors for one item. Returns {"model_key", "keys", "active",
    "sold", "found_by": {"active", "sold"}, "cached": {"active", "sold"}}.
    Rows are check_evidence dicts. `sold=False` skips Apify (it costs money)."""
    now = now or datetime.now(timezone.utc)
    k = keys(ident, specifics, title)
    mk = model_key(ident, k)
    out = {"model_key": mk, "keys": k, "active": [], "sold": [],
           "found_by": {"active": None, "sold": None}, "cached": {"active": False, "sold": False}}
    if not mk:
        return out
    since = now - timedelta(hours=config.COMPETITOR_CACHE_HOURS)
    base = {"model_key": mk, "item_id": item_id, "check_id": check_id}

    cached = db.recent_evidence(mk, "ebay_active", since)
    if cached:
        out["active"], out["cached"]["active"] = cached, True
    else:
        rows, by, q = find_active(k)
        out["active"] = _active_rows(rows, by, q, base)
        _add_details(out["active"])
        _save(out["active"])
    out["found_by"]["active"] = next((r.get("found_by") for r in out["active"]), None)

    if sold:
        cached = db.recent_evidence(mk, "ebay_sold", since)
        if cached:
            out["sold"], out["cached"]["sold"] = cached, True
        else:
            rows, by, q = find_sold(k)
            out["sold"] = _sold_rows(rows, by, q, base)
            _save(out["sold"])
        out["found_by"]["sold"] = next((r.get("found_by") for r in out["sold"]), None)
    out["active"] = [{k2: v for k2, v in r.items() if not k2.startswith("_")} for r in out["active"]]
    return out
