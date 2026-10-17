"""Pricing v2: pooled comps, one Claude judgement, price computed in code.

docs/pricing-design.md is the agreed design; this is stages 2-3 of it. The
caller hands over the identification (stage 1, Gemini) and gets back the same
dict shape pipeline/price.py's get_pricing returns, plus the judge's working.
It is switched on with config.PRICING_ENGINE = "v2"; v1 stays the default.

Flow
  1. Candidates, pooled and de-duplicated by eBay item id:
       sold    Apify sold actor (the v1 helper), up to three queries: the style
               code, the identification's search_query, brand + product name.
               All are pooled; v1 stopped at the first query that returned.
       active  Browse search_active on brand + product name (and on the UPC as
               a GTIN when there is one), new conditions only.
       image   Browse search_by_image on the item's first photo.
     This account's own listings are dropped everywhere: by seller name (the
     sold actor's rows carry sellerUsername; Browse rows carry seller.username),
     by our own eBay listing ids, and, only for a row with no seller at all, by
     an exact match against one of our listing titles.
  2. One Claude call (PRICING_JUDGE_MODEL) labels each candidate same_item /
     variant / wrong / bundle from its title and thumbnail, with one of our
     photos and ~15 of this store's past SALES (sold price) for calibration,
     and gives its price, confidence and the 3-5 candidates it leaned on.
     Low confidence re-asks PRICING_ESCALATE_MODEL.
  3. Claude's price (.99) is the suggested price. The formula is a
     cross-check, computed only with >= 3 same_item SOLD comps: median, less
     UNDERCUT_V2_PCT, capped at the cheapest same_item active listing, .99.
     If the two differ by more than 20% the item is flagged formula_disagrees.
     (The 17 Oct pilot found 0-1 same_item sold comps on all 10 items, so a
     formula-first price was almost never available.)

Price basis: item + shipping, what the buyer pays
  Our listings charge the buyer shipping (EBAY_SHIP_CHARGED, ~$9.99 flat on
  every sold order in the DB), and eBay shows every tile as price + shipping
  and sorts "lowest price" by that total. So a $40 + free-shipping comp and a
  $30 + $10 one are the same price to a buyer, and comparing item prices alone
  (what v1 did for actives, while its sold figures included shipping) mixes
  two different numbers. Every candidate is put on the TOTAL basis:
     sold    soldPrice + shippingPrice (0 when shippingType is "free")
     active  price + Browse's cheapest quoted shippingCost (ship_to_zip set)
  Unknown shipping is imputed at our own charge and marked shipping_imputed;
  imputed rows count as sold evidence but never set the active cap. The target
  total is converted back to an ITEM price by subtracting EBAY_SHIP_CHARGED,
  because that is the number we list at. If the account moves to free
  shipping, EBAY_SHIP_CHARGED = 0 and this stays correct.

Not price signals: the Ross tag's original_price (Ross's own pre-markdown
price, not retail) and the tag code/barcode (Ross's internal number, not a
UPC). The UPC comes from the identification. retail_price from research is a
ceiling check only (flagged, never used as the price).

Every external fetch is cached under PRICING_V2_CACHE_DIR keyed by its query,
so a rerun (the backtest especially) costs nothing.
"""
import concurrent.futures
import hashlib
import io
import json
import math
import re
import sqlite3
import threading
import time
from pathlib import Path

import config
from pipeline import price as v1

JUDGE_STAGE = "price_judge"
ROOT = Path(__file__).resolve().parent.parent
NEW_CONDITION_IDS = (1000, 1500)   # New / New with tags, New other (no tags, open box)
MIN_PRICE, MAX_PRICE = 3.0, 2000.0
THUMB_SIZE = "s-l225"
VERDICTS = ("same_item", "variant", "wrong", "bundle")
# Claude's price vs the formula's: past this relative gap the item is flagged.
FORMULA_DISAGREE_PCT = 0.20


class ApifyBudgetExceeded(RuntimeError):
    """The next uncached Apify call would push estimated spend past the cap."""


# --- spend accounting (process-wide; the backtest reads and caps it) ---------

_lock = threading.Lock()
apify_spend = {"calls": 0, "rows": 0, "usd": 0.0, "cache_hits": 0}
apify_cap_usd: float | None = None
judge_stats = {"calls": 0, "escalations": 0, "thumbnail_fallbacks": 0, "zero_price_retries": 0}
# Which sold queries (by name: "code", "search_query", "product") may go to
# Apify when their result is not cached. None = all of them. The backtest
# narrows it to keep a large run cheap; cached results are always used.
uncached_sold_queries: set | None = None


def _apify_estimate(rows: int) -> float:
    return rows * config.APIFY_SOLD_COST_PER_RESULT + config.APIFY_RUN_START_COST


# --- cache ------------------------------------------------------------------

def _cache_dir() -> Path:
    d = Path(config.PRICING_V2_CACHE_DIR)
    return d if d.is_absolute() else ROOT / d


def _cache_path(kind: str, key: str) -> Path:
    return _cache_dir() / kind / f"{hashlib.sha1(key.encode()).hexdigest()[:20]}.json"


def _cached(kind: str, key: str, fetch):
    """Rows for `key` from disk, else fetch() and store them. Failures raise
    and are NOT cached, so a transient error is retried on the next run."""
    path = _cache_path(kind, key)
    if path.exists():
        with _lock:
            apify_spend["cache_hits"] += kind == "sold"
        return json.loads(path.read_text(encoding="utf-8"))["rows"]
    rows = fetch()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"key": key, "fetched_at": time.time(), "rows": rows}),
                    encoding="utf-8")
    return rows


# --- fetchers ---------------------------------------------------------------

def _sold_key(query: str, count: int) -> str:
    return json.dumps({"keyword": query, "count": count}, sort_keys=True)


def sold_is_cached(query: str, count: int | None = None) -> bool:
    return _cache_path("sold", _sold_key(query, count or config.PRICING_V2_SOLD_COUNT)).exists()


def fetch_sold(query: str, count: int | None = None) -> list[dict]:
    count = count or config.PRICING_V2_SOLD_COUNT
    payload = {"keyword": query, "count": count}

    def go():
        with _lock:
            if apify_cap_usd is not None and \
                    apify_spend["usd"] + _apify_estimate(count) > apify_cap_usd:
                raise ApifyBudgetExceeded(
                    f"Apify estimate ${apify_spend['usd']:.3f} + up to "
                    f"${_apify_estimate(count):.3f} would pass the ${apify_cap_usd:.2f} cap")
        rows = v1._call(v1.SOLD_URL, payload)
        with _lock:
            apify_spend["calls"] += 1
            apify_spend["rows"] += len(rows)
            apify_spend["usd"] += _apify_estimate(len(rows))
        return rows

    return _cached("sold", _sold_key(query, count), go)


def fetch_active(query: str | None = None, gtin: str | None = None) -> list[dict]:
    from ebay import browse
    params = {"q": query or "", "gtin": gtin or "", "limit": config.PRICING_V2_ACTIVE_COUNT,
              "zip": config.PRICING_V2_SHIP_TO_ZIP, "cond": list(NEW_CONDITION_IDS)}
    return _cached("active", json.dumps(params, sort_keys=True), lambda: browse.search_active(
        query, gtin=gtin, limit=params["limit"], condition_ids=NEW_CONDITION_IDS,
        ship_to_zip=params["zip"]))


def fetch_by_image(photo_path) -> list[dict]:
    from ebay import browse
    digest = hashlib.sha1(Path(photo_path).read_bytes()).hexdigest()
    params = {"image_sha1": digest, "limit": config.PRICING_V2_IMAGE_COUNT,
              "zip": config.PRICING_V2_SHIP_TO_ZIP}
    return _cached("image", json.dumps(params, sort_keys=True), lambda: browse.search_by_image(
        photo_path, limit=params["limit"], ship_to_zip=params["zip"]))


# --- identity and queries ---------------------------------------------------

def _clean(v) -> str:
    return re.sub(r"\s+", " ", str(v or "")).strip()


def style_code(ident: dict) -> str | None:
    """The identification's model field when it looks like a style/model code
    (has a digit, 4-30 chars), not a marketing name."""
    m = _clean(ident.get("model"))
    if m and re.search(r"\d", m) and 4 <= len(m.replace(" ", "")) <= 30:
        return m
    return None


def upc_of(ident: dict) -> str | None:
    u = re.sub(r"\D", "", str(ident.get("upc") or ""))
    return u if len(u) in (12, 13, 14) else None


def identity_record(ident: dict, receipt: dict | None = None) -> dict:
    rec = {k: _clean(ident.get(k)) or None for k in (
        "brand", "product_name", "item_type", "model", "size", "color", "material",
        "condition", "search_query")}
    rec["style_code"] = style_code(ident)
    rec["upc"] = upc_of(ident)
    # receipt: only reduced_price (our cost) is meaningful and it is not a market
    # signal; original_price is Ross's pre-markdown price and code/barcode are
    # Ross's own numbers. Nothing from it is sent to the judge or searched.
    return rec


def broad_query(ident: dict) -> str | None:
    """brand + item type, plus the size for watch bands (band sizes do not
    fit each other, so a 38mm sale says little about a 45mm band)."""
    brand, item_type = _clean(ident.get("brand")), _clean(ident.get("item_type"))
    if not brand or not item_type:
        return None
    q = f"{brand} {item_type}"
    size = _clean(ident.get("size"))
    if size and re.search(r"\bband|strap", item_type.lower()) and len(size) <= 20:
        q += f" {size}"
    return q


def sold_queries(ident: dict) -> list[tuple[str, str]]:
    """(a) the identification's search_query, (b) brand + product name, (c) a
    broad brand + item type query. Pooled, not a ladder: (c) mostly brings
    variants, which are the fallback evidence when no same-item sale exists."""
    brand = _clean(ident.get("brand"))
    product = _clean(ident.get("product_name"))
    out = [("search_query", _clean(ident.get("search_query")))]
    if brand and product:
        out.append(("product", f"{brand} {product}"))
    out.append(("broad", broad_query(ident) or ""))
    seen, ladder = set(), []
    for name, q in out:
        if q and q.lower() not in seen:
            seen.add(q.lower())
            ladder.append((name, q))
    return ladder


def active_query(ident: dict) -> str | None:
    brand, product = _clean(ident.get("brand")), _clean(ident.get("product_name"))
    if brand and product:
        return f"{brand} {product}"
    return _clean(ident.get("search_query")) or None


# --- normalisation ----------------------------------------------------------

_ITM_RE = re.compile(r"/itm/(?:[^/?#]+/)?(\d{9,})")


def ebay_id(item_id=None, url=None) -> str | None:
    if item_id not in (None, ""):
        s = str(item_id)
        m = re.match(r"v1\|(\d+)\|", s)
        return m.group(1) if m else s
    m = _ITM_RE.search(url or "")
    return m.group(1) if m else None


def thumb(url: str | None) -> str | None:
    if not url or "ebayimg.com" not in url:
        return None
    return re.sub(r"s-l\d+(\.\w+)", THUMB_SIZE + r"\1", url) if re.search(r"s-l\d+\.\w+", url) else url


def to_total(item_price, shipping, our_shipping: float) -> tuple[float, float, bool]:
    """(total, shipping used, imputed). Unknown shipping is imputed at our own
    charge so the row still lands on the total basis, and is flagged."""
    if shipping is None:
        return round(item_price + our_shipping, 2), our_shipping, True
    return round(item_price + shipping, 2), shipping, False


def sold_candidate(row: dict, our_shipping: float) -> dict | None:
    item = v1._to_float(row.get("soldPrice"))
    ship = v1._to_float(row.get("shippingPrice"))
    if (row.get("shippingType") or "").lower() == "free":
        ship = 0.0
    if item is None:
        total = v1._to_float(row.get("totalPrice"))
        if total is None:
            return None
        item = total - (ship or 0.0)
    total, ship_used, imputed = to_total(item, ship, our_shipping)
    return {
        "source": "sold", "ebay_id": ebay_id(row.get("itemId"), row.get("url")),
        "title": _clean(row.get("title")), "item_price": round(item, 2),
        "shipping": ship_used, "shipping_imputed": imputed, "total": total,
        "condition": row.get("condition") or "", "image": row.get("thumbnailUrl") or "",
        "url": row.get("url") or "", "seller": (row.get("sellerUsername") or "").lower() or None,
        "date": row.get("endedAt"),
    }


def active_candidate(s: dict, our_shipping: float, source: str = "active") -> dict | None:
    item = s.get("price")
    if item is None:
        return None
    total, ship_used, imputed = to_total(float(item), s.get("shipping_cost"), our_shipping)
    return {
        "source": source, "ebay_id": ebay_id(s.get("legacy_item_id") or s.get("item_id"),
                                             s.get("item_web_url")),
        "title": _clean(s.get("title")), "item_price": round(float(item), 2),
        "shipping": ship_used, "shipping_imputed": imputed, "total": total,
        "condition": s.get("condition") or "", "image": s.get("image_url") or "",
        "url": s.get("item_web_url") or "",
        "seller": (s.get("seller_username") or "").lower() or None, "date": None,
    }


def _norm_title(t: str) -> str:
    return re.sub(r"\s+", " ", (t or "").lower()).strip()


def is_own(c: dict, own_sellers, own_ids, own_titles) -> bool:
    if c.get("seller") and c["seller"] in own_sellers:
        return True
    if c.get("ebay_id") and c["ebay_id"] in own_ids:
        return True
    # Title match only when the row can't tell us who sold it: an exact title
    # copy by another seller is rare, and with no seller there is no other check.
    return not c.get("seller") and _norm_title(c.get("title")) in own_titles


def dedupe(cands: list[dict]) -> list[dict]:
    """First occurrence wins, so pass sold before active before image."""
    seen, out = set(), []
    for c in cands:
        key = c.get("ebay_id") or (c.get("url") or "").split("?")[0] or _norm_title(c["title"])
        if key in seen:
            continue
        seen.add(key)
        out.append(c)
    return out


def filter_candidates(cands, own_sellers, own_ids, own_titles):
    """Drop own listings, used rows and absurd prices. Returns (kept, dropped
    counts by reason)."""
    kept, dropped = [], {"own": 0, "used": 0, "price": 0}
    for c in cands:
        if c is None:
            continue
        if is_own(c, own_sellers, own_ids, own_titles):
            dropped["own"] += 1
        elif v1._is_used(c.get("condition")):
            dropped["used"] += 1
        elif not (MIN_PRICE <= c["item_price"] <= MAX_PRICE):
            dropped["price"] += 1
        else:
            kept.append(c)
    return kept, dropped


# --- our own catalogue (read-only) -------------------------------------------

def _db_rows(sql: str):
    path = ROOT / "data" / "ross.db"
    if not path.exists():
        return []
    con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        return con.execute(sql).fetchall()
    finally:
        con.close()


_own_cache: tuple | None = None


def own_listings() -> tuple[set, set]:
    """(listing ids, normalised titles) of everything this account has listed."""
    global _own_cache
    if _own_cache is None:
        ids, titles = set(), set()
        for ebay_json, listing_json in _db_rows("SELECT ebay, listing FROM items"):
            e = json.loads(ebay_json or "{}")
            lst = json.loads(listing_json or "{}")
            if e.get("listing_id"):
                ids.add(str(e["listing_id"]))
            if lst.get("title"):
                titles.add(_norm_title(lst["title"]))
        _own_cache = (ids, titles)
    return _own_cache


def calibration_examples(exclude_item_id: str | None, n: int = 15) -> list[dict]:
    """~n past SALES: what this store's stock actually sold for, per unit.
    Unsold listed prices are left out: a price nobody has paid yet says what
    the owner hoped for, not what the market pays, and the judge copied them.
    Chosen deterministically (evenly spaced by item id) so the judge's prompt
    prefix is the same from call to call; one spare is drawn so excluding the
    item being priced still leaves n."""
    rows = []
    for item_id, ident_json, ebay_json in _db_rows(
            "SELECT item_id, identification, ebay FROM items "
            "WHERE status = 'sold' ORDER BY item_id"):
        e = json.loads(ebay_json or "{}")
        if not e.get("sale_price"):
            continue
        ident = json.loads(ident_json or "{}")
        rows.append({"item_id": item_id, "brand": ident.get("brand"),
                     "item": _clean(ident.get("product_name") or ident.get("item_type"))[:60],
                     "type": _clean(ident.get("item_type"))[:40],
                     "msrp": v1._to_float(ident.get("retail_price")),
                     "sold_price": round(e["sale_price"] / max(1, e.get("units_sold") or 1), 2)})
    if not rows:
        return []
    step = max(1, len(rows) // (n + 1))
    picked = rows[::step][:n + 1]
    out = [r for r in picked if not (exclude_item_id and r["item_id"].startswith(exclude_item_id))]
    return out[:n]


# --- the judge ----------------------------------------------------------------

SYSTEM_PROMPT = """You price new-with-tags stock bought at Ross (off-price retail) for resale on eBay US.

You are given the item (identity record and one photo) and candidate eBay listings, SOLD (completed sales) and ACTIVE (for sale now). Each candidate shows its price on the TOTAL basis: item price + shipping, which is what a buyer pays and compares. Our own listings charge the buyer about ${ship:.2f} shipping on top of the item price.

Label every candidate:
- same_item: the same product a buyer would accept as identical: same brand, same model/style/collection, same size class, same colour or pattern. A different colourway of the same style is a variant, not same_item, unless colour plainly does not change value.
- variant: same brand and product line but a different size, colour, material, model year or style.
- wrong: a different product (different brand, different type, a lookalike, an accessory for it, or a used/damaged one).
- bundle: several items sold together, or a lot.

Judge from the title AND the thumbnail when one is shown; the thumbnail decides colour and pattern. Do not guess same_item from a vague title.

Then give claude_price: the ITEM price (excluding shipping) you would list this item at as a Buy It Now, to sell within about two weeks. Order of evidence:
1. same_item SOLD comps.
2. If there are none: near-identical variant SOLD comps (same line, different colour or size) together with the STORE HISTORY ESTIMATE (the item's MSRP times what this store's sales of the brand have fetched as a share of MSRP). Prefer these over active asks.
3. ACTIVE listings are asking prices, not sales: treat the cheapest comparable active as a CEILING a buyer can undercut us with, never as a target. Many actives sit unsold for months.
4. The RESEARCH block (a web search) is weak evidence: it mixes asks, sales, used items and other sites.
Use this store's past sales below to calibrate what its stock actually sells for. Your price is the one we list at, so make it the price you believe in. confidence: high when several same_item sold comps agree, medium when the evidence is thin or only variants, low when you could not find the item. evidence: the 3-5 candidate ids your price leans on most, strongest first. reasoning: one line. disagreements: short notes where sources conflict (e.g. sold vs active, identity record vs photo, research retail vs comps); empty list if none.

This store's past sales (brand | item | MSRP | sold item price, shipping excluded):
{calibration}"""

JUDGE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "judgments": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "properties": {"id": {"type": "string"}, "verdict": {"type": "string", "enum": list(VERDICTS)}},
            "required": ["id", "verdict"]}},
        "claude_price": {"type": "number",
                         "description": "item price in USD excluding shipping; always > 0"},
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        "evidence": {"type": "array", "items": {"type": "string"}},
        "reasoning": {"type": "string"},
        "disagreements": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["judgments", "claude_price", "confidence", "evidence", "reasoning",
                 "disagreements"],
}


def _calibration_text(examples: list[dict]) -> str:
    lines = []
    for e in examples:
        msrp = f"${e['msrp']:g}" if e.get("msrp") else "?"
        lines.append(f"- {e['brand'] or '?'} | {e['item'] or e['type'] or '?'} | "
                     f"MSRP {msrp} | sold ${e['sold_price']}")
    return "\n".join(lines) or "- (none)"


def _photo_bytes(path, side: int = 768) -> bytes | None:
    try:
        from PIL import Image, ImageOps
        img = ImageOps.exif_transpose(Image.open(path)).convert("RGB")
        img.thumbnail((side, side))
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=80)
        return buf.getvalue()
    except Exception:
        return None


def _candidate_line(c: dict) -> str:
    ship = "free ship" if c["shipping"] == 0 else (
        f"+${c['shipping']:.2f} ship" + (" (est.)" if c["shipping_imputed"] else ""))
    kind = "SOLD" if c["source"] == "sold" else "ACTIVE"
    when = f" | sold {c['date']}" if c.get("date") else ""
    return (f"[{c['id']}] {kind} | total ${c['total']:.2f} (${c['item_price']:.2f} {ship}) | "
            f"{c['condition'] or '?'}{when} | {c['title']}")


def build_judge_request(identity: dict, cands: list[dict], photo: bytes | None,
                        calibration: list[dict], thumbnails: bool = True,
                        context: str | None = None):
    """(system blocks, user content blocks). The system text depends only on the
    calibration set and the shipping constant, so it is identical from item to
    item and is marked for prompt caching."""
    from claude_llm import _image_block
    system = [{"type": "text",
               "text": SYSTEM_PROMPT.format(ship=config.EBAY_SHIP_CHARGED,
                                            calibration=_calibration_text(calibration)),
               "cache_control": {"type": "ephemeral"}}]
    content = [{"type": "text", "text": "ITEM identity record:\n" +
                json.dumps({k: v for k, v in identity.items() if v}, indent=1)}]
    if context:
        content.append({"type": "text", "text": context})
    if photo:
        content.append({"type": "text", "text": "Our photo of the item:"})
        content.append(_image_block((photo, "image/jpeg")))
    content.append({"type": "text", "text": f"CANDIDATES ({len(cands)}):"})
    for c in cands:
        content.append({"type": "text", "text": _candidate_line(c)})
        t = thumb(c.get("image")) if thumbnails else None
        if t:
            content.append({"type": "image", "source": {"type": "url", "url": t}})
    content.append({"type": "text", "text": "Label every candidate id, then price the item."})
    return system, content


def judge(identity, cands, photo, calibration, *, item_id=None, ask=None,
          context: str | None = None) -> dict:
    """One Claude call, escalated once on low confidence. Returns the parsed
    answer plus model / escalated / thumbnails_used."""
    import anthropic
    import claude_llm
    ask = ask or claude_llm.ask

    def call(model, thumbnails, note=None):
        system, content = build_judge_request(identity, cands, photo, calibration, thumbnails,
                                              context)
        if note:
            content.append({"type": "text", "text": note})
        judge_stats["calls"] += 1
        return ask(JUDGE_STAGE, [{"role": "user", "content": content}], model=model,
                   system=system, max_tokens=8000, effort=config.PRICING_JUDGE_EFFORT,
                   output_schema=JUDGE_SCHEMA, item_id=item_id)

    thumbnails = True
    model = config.PRICING_JUDGE_MODEL
    try:
        out = call(model, True)
    except anthropic.BadRequestError as e:
        # A thumbnail URL eBay won't serve fails the whole request; titles only.
        if "image" not in str(e).lower() and "url" not in str(e).lower():
            raise
        judge_stats["thumbnail_fallbacks"] += 1
        thumbnails = False
        out = call(model, False)
    escalated = False
    if out.get("confidence") == "low" and config.PRICING_ESCALATE_ON_LOW:
        judge_stats["escalations"] += 1
        escalated = True
        model = config.PRICING_ESCALATE_MODEL
        out = call(model, thumbnails)
    if not (v1._to_float(out.get("claude_price")) or 0) > 0:
        # Seen twice in 40 backtest items: a sensible price in the reasoning
        # and 0 in claude_price. Asked once more, saying so; still 0 -> no price.
        judge_stats["zero_price_retries"] += 1
        out = call(model, thumbnails, note="claude_price must be the positive item price you "
                                           "would list at; a previous answer returned 0.")
    out.update(model=model, escalated=escalated, thumbnails_used=thumbnails)
    return out


# --- price from judged comps ------------------------------------------------

def charm(x: float) -> float:
    return round(max(round(x) - 0.01, 0.99), 2)


def price_from_judged(cands: list[dict], verdicts: dict, *, undercut: float | None = None,
                      our_shipping: float | None = None) -> dict:
    """Code price from same_item SOLD comps (total basis), returned as an ITEM
    price. Pure: no I/O, so it is unit-tested directly."""
    undercut = config.UNDERCUT_V2_PCT if undercut is None else undercut
    our_shipping = config.EBAY_SHIP_CHARGED if our_shipping is None else our_shipping
    same = [c for c in cands if verdicts.get(c["id"]) == "same_item"]
    sold = sorted(c["total"] for c in same if c["source"] == "sold")
    active = sorted(c["total"] for c in same if c["source"] != "sold" and not c["shipping_imputed"])
    n = len(sold)
    grade = ("solid" if n >= config.PRICE_SOLID_MIN_COMPS else
             "thin" if n >= config.PRICE_THIN_MIN_COMPS else "none")
    res = {"evidence": grade, "same_item_sold": n,
           "same_item_active": sum(1 for c in same if c["source"] != "sold"),
           "sold_median": None, "sold_p10": None, "sold_p90": None,
           "dispersion": v1._dispersion(sold), "active_floor": None,
           "raw_active_floor": active[0] if active else None,
           "price": None, "target_total": None, "warnings": []}
    if grade == "none":
        return res
    median = v1._median(sold)
    res.update(sold_median=round(median, 2), sold_p10=v1._quantile(sold, 0.10),
               sold_p90=v1._quantile(sold, 0.90))
    target = median * (1 - undercut)
    cap = active[0] if active else None
    if cap is not None and cap < median * config.ACTIVE_FLOOR_MIN_RATIO:
        res["warnings"].append(f"cheapest same-item active (${cap} total) is under "
                               f"{config.ACTIVE_FLOOR_MIN_RATIO:.0%} of the sold median "
                               f"(${median:.2f}): not used as a cap")
        cap = None
    res["active_floor"] = cap
    if cap is not None and cap < target:
        target = cap
    item = target - our_shipping
    p = charm(item)
    if cap is not None and p + our_shipping > cap:
        p = charm(p - 1)    # rounding up must not lift us over the cheapest rival
    res.update(target_total=round(target, 2), price=p if item > 0 else None)
    if item <= 0:
        res["warnings"].append("target total is below our shipping charge")
    return res


# --- entry point --------------------------------------------------------------

def _resolve(p) -> str | None:
    """Stored photo paths are relative to the repo root (data/inbox/...)."""
    if not p:
        return None
    path = Path(p)
    if not path.is_absolute():
        path = ROOT / path
    return str(path) if path.exists() else None


def gather_candidates(ident: dict, photo_paths=None) -> dict:
    """Fetch (cached) and normalise every candidate. Returns candidates plus
    per-source counts, the queries used and any fetch errors."""
    our_ship = config.EBAY_SHIP_CHARGED
    queries = sold_queries(ident)
    aq, gtin = active_query(ident), upc_of(ident)
    photo = next((p for p in map(_resolve, photo_paths or []) if p), None)
    errors, skipped = [], []
    if uncached_sold_queries is not None and queries:
        allowed = set(uncached_sold_queries)
        if not any(n in allowed for n, _ in queries):
            allowed.add(queries[0][0])      # always at least one live query
        kept_q = [(n, q) for n, q in queries if n in allowed or sold_is_cached(q)]
        skipped = [q for n, q in queries if (n, q) not in kept_q]
        queries = kept_q
    jobs = {f"sold:{name}": (fetch_sold, (q, config.PRICING_V2_BROAD_SOLD_COUNT
                                         if name == "broad" else None))
            for name, q in queries}
    if aq:
        jobs["active:text"] = (fetch_active, (aq, None))
    if gtin:
        jobs["active:gtin"] = (fetch_active, (None, gtin))
    if photo:
        jobs["image"] = (fetch_by_image, (photo,))
    raw = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        futs = {pool.submit(fn, *args): name for name, (fn, args) in jobs.items()}
        for f in concurrent.futures.as_completed(futs):
            name = futs[f]
            try:
                raw[name] = f.result()
            except ApifyBudgetExceeded as e:
                errors.append(f"{name}: skipped, {e}")
                skipped.append(dict(queries).get(name.split(":", 1)[-1], name))
                raw[name] = []
            except Exception as e:      # one dead source must not lose the others
                errors.append(f"{name}: {type(e).__name__}: {str(e)[:160]}")
                raw[name] = []
    sold = [sold_candidate(r, our_ship) for name, _ in queries for r in raw.get(f"sold:{name}", [])]
    active = [active_candidate(r, our_ship) for k in ("active:gtin", "active:text")
              for r in raw.get(k, [])]
    image = [active_candidate(r, our_ship, "image") for r in raw.get("image", [])]
    own_ids, own_titles = own_listings()
    # Dedupe first: the three sold queries overlap heavily, and the drop counts
    # should count listings, not search hits.
    pooled, dropped = filter_candidates(dedupe([c for c in sold + active + image if c]),
                                        config.OWN_SELLER_USERNAMES, own_ids, own_titles)
    sold_c = [c for c in pooled if c["source"] == "sold"]
    other = [c for c in pooled if c["source"] != "sold"]
    pooled = (sold_c + other)[:config.PRICING_V2_MAX_CANDIDATES]
    counters = {"S": 0, "A": 0, "I": 0}
    for c in pooled:
        p = {"sold": "S", "active": "A", "image": "I"}[c["source"]]
        counters[p] += 1
        c["id"] = f"{p}{counters[p]}"
    return {"candidates": pooled, "dropped": dropped, "errors": errors,
            "queries": [q for _, q in queries], "skipped_queries": skipped,
            "active_query": aq, "gtin": gtin,
            "photo": photo, "raw_counts": {k: len(v) for k, v in raw.items()}}


def context_text(store: dict | None, ident: dict) -> str:
    """STORE HISTORY and RESEARCH blocks for the judge."""
    lines = []
    if store:
        m = store.get("msrp")
        flags = ", ".join(store.get("msrp_flags") or [])
        lines.append("STORE HISTORY ESTIMATE (this store's own sales):")
        lines.append(f"- MSRP: {'$%g' % m if m else 'unknown'}" + (f" (doubtful: {flags})" if flags else ""))
        if store.get("ratio"):
            lines.append(f"- {store['level']} '{store['key']}': {store['n']} past sale(s) fetched a median "
                         f"{store['ratio']:.0%} of MSRP (middle half {store['q1']:.0%}-{store['q3']:.0%})")
        if store.get("estimate"):
            lines.append(f"- MSRP x ratio = ${store['estimate']} item price")
    ev = ident.get("price_evidence") or []
    if ev or ident.get("resale_estimate"):
        lines.append("RESEARCH (web search by another model; weak evidence):")
        if ident.get("resale_estimate"):
            lines.append(f"- its resale estimate: ${ident['resale_estimate']}")
        for e in ev[:8]:
            lines.append(f"- {e.get('source') or '?'}: ${e.get('price')} {(e.get('note') or '')[:60]}".rstrip())
    return "\n".join(lines)


def _pct_apart(a: float, b: float) -> float:
    return abs(a - b) / min(a, b) if a > 0 and b > 0 else float("inf")


def get_pricing_v2(identification: dict, *, photo_paths=None, receipt=None,
                   item_id: str | None = None, ask=None) -> dict:
    """Claude's price is the suggested price. The formula over same_item sold
    comps is a cross-check: only computed with >= PRICE_THIN_MIN_COMPS of them,
    and a gap over FORMULA_DISAGREE_PCT is flagged for review with both numbers.
    (Pilot, 17 Oct 2026: same_item sold comps were 0-1 on all 10 items, so a
    formula-first price existed for none of them, while Claude's was within 20%
    of the owner's on 7.)"""
    from pipeline import msrp as msrp_mod
    ident = identification or {}
    identity = identity_record(ident, receipt)
    try:
        store = msrp_mod.store_estimate(ident, receipt, exclude_item_id=item_id)
    except Exception as e:      # history is advisory; never lose the item over it
        store = {"error": f"{type(e).__name__}: {e}"}
    g = gather_candidates(ident, photo_paths)
    cands = g["candidates"]
    warnings, flags = list(g["errors"]), []
    if g["dropped"]["own"]:
        warnings.append(f"dropped {g['dropped']['own']} of our own listings from the comps")

    verdict_map, j = {}, None
    if cands:
        photo = _photo_bytes(g["photo"]) if g["photo"] else None
        j = judge(identity, cands, photo, calibration_examples(item_id), item_id=item_id, ask=ask,
                  context=context_text(store, ident))
        verdict_map = {x["id"]: x["verdict"] for x in j.get("judgments", [])}
        missing = [c["id"] for c in cands if c["id"] not in verdict_map]
        if missing:
            warnings.append(f"judge skipped {len(missing)} candidate(s); treated as unjudged")
    else:
        flags.append("no_comps")
        warnings.append("no comps found at all: type a price")

    priced = price_from_judged(cands, verdict_map)
    warnings += priced["warnings"]
    for c in cands:
        c["verdict"] = verdict_map.get(c["id"])

    claude_raw = v1._to_float(j.get("claude_price")) if j else None
    claude_conf = j.get("confidence") if j else None
    p = charm(claude_raw) if claude_raw and claude_raw > 0 else None
    formula = priced["price"]
    if p and formula and _pct_apart(p, formula) > FORMULA_DISAGREE_PCT:
        flags.append("formula_disagrees")
        warnings.append(f"Claude says ${p}, the formula over {priced['same_item_sold']} "
                        f"same-item sales says ${formula}")
    if claude_conf == "low":
        flags.append("judge_low_confidence")
    if j and not p:
        flags.append("no_claude_price")
    retail = v1._to_float(ident.get("retail_price"))
    if p and retail and p > retail:
        flags.append("above_retail")
        warnings.append(f"price ${p} is above the researched retail ${retail}")
    if formula and priced["dispersion"] and priced["dispersion"] > config.PRICE_THIN_MAX_DISPERSION:
        flags.append("dispersed")

    same_sold = [c for c in cands if c["verdict"] == "same_item" and c["source"] == "sold"]
    same_active = [c for c in cands if c["verdict"] == "same_item" and c["source"] != "sold"]

    def comp(c):
        return {"price": c["total"], "item_price": c["item_price"], "shipping": c["shipping"],
                "title": c["title"], "condition": c["condition"], "image": c["image"],
                "url": c["url"]}

    by_id = {c["id"]: c for c in cands}
    ev_ids = [i for i in ((j.get("evidence") or []) if j else []) if i in by_id][:5]
    ev_comps = [{"id": i, "source": "sold" if by_id[i]["source"] == "sold" else "active",
                 "verdict": by_id[i]["verdict"], **comp(by_id[i])} for i in ev_ids]

    reference = stock_image_url = price_source_url = None
    pool = [by_id[i] for i in ev_ids if by_id[i]["verdict"] == "same_item"] or same_sold \
        or same_active
    if pool:
        top = pool[0]
        reference = {"title": top["title"], "price": top["total"], "condition": top["condition"],
                     "source": "sold" if top["source"] == "sold" else "active",
                     "url": top["url"] or None}
        stock_image_url = v1._upgrade_image(top["image"])
        price_source_url = top["url"] or None

    research_resale = v1._research_resale(ident)
    return {
        # --- v1 shape ---
        "suggested_price": p, "machine_price": p,
        # comp_suggested keeps its v1 meaning: the comp-formula price.
        "comp_suggested": formula,
        "price_basis": "claude" if p else "none",
        "confidence": claude_conf or "none",
        "sold_median": priced["sold_median"], "sold_p10": priced["sold_p10"],
        "sold_p90": priced["sold_p90"], "active_floor": priced["active_floor"],
        "raw_active_floor": priced["raw_active_floor"],
        "comp_warning": "; ".join(warnings) or None,
        "review_flags": flags,
        "needs_review": bool(flags) or p is None,
        "evidence": priced["evidence"],
        "query_rung": "pooled", "query_used": " || ".join(g["queries"]),
        "rungs_tried": g["queries"], "dispersion": priced["dispersion"],
        "sold_count": len(same_sold), "sold_comps": [comp(c) for c in same_sold],
        "active_count": len(same_active), "active_listings": [comp(c) for c in same_active],
        "reference": reference, "stock_image_url": stock_image_url,
        "price_source_url": price_source_url,
        "retail_price": ident.get("retail_price"), "resale_estimate": ident.get("resale_estimate"),
        "research_resale": round(research_resale, 2) if research_resale else None,
        "price_evidence": ident.get("price_evidence"),
        # --- v2 extras ---
        "engine": "v2", "comp_basis": "item+shipping total",
        "our_shipping": config.EBAY_SHIP_CHARGED, "target_total": priced["target_total"],
        "formula_price": formula, "store_estimate": store,
        "judged_comps": [{k: c.get(k) for k in ("id", "source", "verdict", "title", "total",
                                                 "item_price", "shipping", "shipping_imputed",
                                                 "condition", "url", "seller")} for c in cands],
        "candidate_count": len(cands), "dropped": g["dropped"],
        "skipped_queries": g.get("skipped_queries") or [],
        "same_item_sold_count": priced["same_item_sold"],
        "same_item_active_count": priced["same_item_active"],
        "claude_price": p, "claude_price_raw": claude_raw, "claude_confidence": claude_conf,
        "claude_reasoning": j.get("reasoning") if j else None,
        "claude_evidence": {"ids": ev_ids, "comps": ev_comps,
                            "reason": j.get("reasoning") if j else None},
        "disagreements": j.get("disagreements") if j else [],
        "judge_model": j.get("model") if j else None,
        "judge_escalated": bool(j and j.get("escalated")),
        "thumbnails_used": bool(j and j.get("thumbnails_used")),
        "raw_counts": g["raw_counts"],
    }
