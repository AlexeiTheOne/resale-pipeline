"""/check: the pre-buy check (docs/check-command-design.md).

Pure functions (parsing, text identification, label cost, money, verdict),
one orchestrator (run_check) and the reply text. The Telegram side is
bot/check_cmds.py. Nothing is stored: no item, no pricing row. The only
writes are what pricing already does (llm_usage cost rows, the comps
cache), plus one line per check in logs/check_timings.jsonl (timings for the
pilot, and the daily-cap count).

Modes (Person B, 2026-11-02):
  quick (default)  live active comps + sold comps ONLY from a cache under
                   CHECK_SOLD_CACHE_DAYS old (no Apify call), judge without
                   escalation. Aim: ~20 s. None of its sold searches
                   cached -> the bot goes on to the full check (check #10).
  full             the full pricing run: live sold queries, escalation on;
                   cached sold rows still only if under CHECK_SOLD_CACHE_DAYS.
  exact (default)  price, sales and "cheapest identical" from the judge's
                   same_item comps; the price is the formula over same-item
                   sales, never Claude's estimate.
  any              Claude's suggested price, sales = same_item + variant.
"""
import json
import math
import re
import statistics
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import config

EASTERN = ZoneInfo("America/New_York")
TIMINGS_FILE = Path(config.LOG_DIR) / "check_timings.jsonl"
VERDICTS = ("BUY", "MAYBE", "BORDERLINE", "SKIP")
STEP_DOWN = dict(zip(VERDICTS, VERDICTS[1:]))     # high supply: one step lower
# No price at all (no sales, no identical listing): not a judgement on the
# item. After a quick check the bot goes straight on to a full check; a full
# check with no price stays NO_DATA (Person B, 2026-11-02). SKIP only when
# real prices back it.
NO_DATA = "NO_DATA"
ICON = {"BUY": "✅", "MAYBE": "🟡", "BORDERLINE": "🟠", "SKIP": "❌", NO_DATA: "❓"}
LABEL = {NO_DATA: "NO DATA"}

# --- parsing ------------------------------------------------------------------

_FLAGS = {"exact": ("exact", True), "any": ("exact", False),
          "full": ("full", True), "quick": ("full", False)}
_PRICE = re.compile(r"^\$?(\d{1,4}(?:[.,]\d{1,2})?)$")


def parse_request(text: str | None) -> dict:
    """'/check [exact|any] [full|quick] <description> <Ross price>'.
    Options are read only at the start and only in lowercase, so "Full Zip
    Hoodie" stays text (the buttons are the usual way to switch anyway). The
    Ross price is a $-number anywhere, else a number as the LAST word."""
    words = (text or "").split()
    if words and words[0].lower().split("@")[0] == "/check":
        words = words[1:]
    opts = {"exact": True, "full": False}
    while words and words[0] in _FLAGS:
        key, value = _FLAGS[words.pop(0)]
        opts[key] = value
    idx = next((i for i in range(len(words) - 1, -1, -1)
                if words[i].startswith("$") and _PRICE.match(words[i])), None)
    if idx is None and words and _PRICE.match(words[-1]):
        idx = len(words) - 1
    price = None
    if idx is not None:
        price = float(_PRICE.match(words.pop(idx)).group(1).replace(",", "."))
    return {"description": " ".join(words).strip(), "ross_price": price, **opts}


# --- text -> identification (no LLM, design §3) ---------------------------------

_FILLER = {"in", "the", "a", "an", "of", "and", "with", "for", "new", "nwt", "nwot", "nib",
           "tags", "tag", "brand"}
_UNIT_TOKEN = re.compile(r"^\d+[a-z]{1,4}$", re.I)       # 128gb, 1080p, 500ml


def _looks_like_code(tok: str) -> bool:
    """TR105025, A14722C, DV0761-458: at least 3 digits and a letter (legacy/comps.py)."""
    digits = sum(c.isdigit() for c in tok)
    letters = sum(c.isalpha() for c in tok)
    return len(tok) >= 5 and digits >= 3 and letters >= 1 and not _UNIT_TOKEN.match(tok)


def clean_query(text: str) -> str:
    text = re.sub(r"[()\[\]{}\"“”*!?;:,|]", " ", text or "")
    return re.sub(r"\s+", " ", " ".join(w for w in text.split() if w.lower() not in _FILLER)).strip()


def build_identification(text: str) -> dict:
    """Plain text -> the identification dict pricing_v2 reads. The style code
    and UPC are pulled out; everything else goes to search as typed."""
    q = clean_query(text)
    ident = {"product_name": q, "search_query": q}
    codes = [t for t in re.findall(r"[A-Za-z0-9][A-Za-z0-9\-]{3,}[A-Za-z0-9]", text or "")
             if _looks_like_code(t)]
    if codes:
        ident["model"] = codes[0]
    upc = re.search(r"(?<!\d)(\d{12,13})(?!\d)", text or "")
    if upc:
        ident["upc"] = upc.group(1)
    return ident


# A Ross tag code (12 digits, 4...) or its 18-digit barcode: Ross's own number,
# which no eBay listing carries. Never searched for.
_ROSS_IN_TEXT = re.compile(r"(?<!\d)4\d{11}(?:\d{6})?(?!\d)")


def without_ross_codes(ident: dict) -> dict:
    """The identification with Ross codes taken out of everything eBay is
    searched with (search query, product name, UPC)."""
    from pipeline import gtin
    out = dict(ident)
    for k in ("search_query", "product_name"):
        if isinstance(out.get(k), str):
            v = re.sub(r"\s+", " ", _ROSS_IN_TEXT.sub(" ", out[k])).strip()
            if v:
                out[k] = v
            else:
                out.pop(k)
    if out.get("upc") and gtin.is_store_code(out["upc"]):
        out.pop("upc")
    return out


def searchable(ident: dict) -> bool:
    """Is there anything to search eBay with (after without_ross_codes)?"""
    from pipeline import gtin
    return bool(ident.get("brand") or ident.get("product_name") or ident.get("search_query")
                or ident.get("model") or ident.get("style_code") or gtin.product_gtin(ident.get("upc")))


# --- label cost by item type (design §4) ----------------------------------------

LABEL_WORDS = {
    "bags": ("bag", "tote", "backpack", "purse", "handbag", "crossbody", "wallet", "luggage",
             "satchel", "clutch", "duffel"),
    "jewelry": ("necklace", "bracelet", "earring", "ring", "watch", "jewelry", "pendant", "anklet"),
    "shoes": ("shoe", "sneaker", "boot", "sandal", "heel", "loafer", "slipper", "pump", "clog",
              "trainer"),
    "clothing": ("shirt", "dress", "jacket", "hoodie", "pants", "jeans", "sweater", "top", "shorts",
                 "skirt", "coat", "blouse", "legging", "sweatshirt", "polo", "tee", "vest"),
}


def label_costs() -> dict:
    out = {}
    for part in (config.CHECK_LABEL_COSTS or "").split(","):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip().lower()] = float(v)
    out.setdefault("other", config.EBAY_SHIP_COST)
    return out


def item_type_of(ident: dict, text: str = "") -> str:
    hay = " ".join(str(ident.get(k) or "") for k in ("item_type", "product_name", "search_query"))
    words = set(re.findall(r"[a-z]+", f"{hay} {text}".lower()))
    words |= {w[:-1] for w in words if w.endswith("s")}          # plurals: bags -> bag
    for kind, keys in LABEL_WORDS.items():
        if words & set(keys):
            return kind
    return "other"


SMALL_BAG_WORDS = ("sling", "crossbody", "mini", "wallet", "wristlet", "pouch", "belt", "fanny", "waist",
                   "cardholder", "card", "coin", "clutch", "cosmetic", "makeup", "phone", "lanyard")
LARGE_BAG_WORDS = ("tote", "weekender", "duffel", "duffle", "luggage", "travel", "backpack", "diaper",
                   "laptop", "shopper", "carryall", "overnight")
_DIMENSION_KEYS = ("bag width", "bag height", "bag depth", "width", "height", "depth", "length")


def _dimensions(ident: dict) -> list[float]:
    """The bag's sides in inches, from the identification's specifications."""
    out = []
    for k, v in (ident.get("specifications") or {}).items():
        if str(k).strip().lower() in _DIMENSION_KEYS:
            m = re.search(r"(\d+(?:\.\d+)?)\s*(in|inch|inches|\"|cm)?", str(v).lower())
            if m:
                x = float(m.group(1))
                out.append(x / 2.54 if m.group(2) == "cm" else x)
    return out


def bag_size(ident: dict, text: str = "") -> str:
    """"small" or "large" for the shipping label. The name first: a sling,
    crossbody, mini or wallet is small unless it is also a tote/weekender/
    backpack ("mini" wins: a mini backpack is small); a tote, weekender or
    duffel is large. A soft sling folds into a poly mailer whatever its
    length (check #5: a 21" Vans sling). Only a name that says neither
    ("handbag", "shoulder bag") goes by the measured sides: none over
    CHECK_SMALL_BAG_MAX_IN = small. Unknown: large, the safe side for profit."""
    hay = " ".join(str(ident.get(k) or "") for k in ("item_type", "product_name", "search_query"))
    words = set(re.findall(r"[a-z]+", f"{hay} {text}".lower()))
    words |= {w[:-1] for w in words if w.endswith("s")}
    small, large = words & set(SMALL_BAG_WORDS), words & set(LARGE_BAG_WORDS)
    if small and (not large or "mini" in small):
        return "small"
    if large:
        return "large"
    dims = _dimensions(ident)
    return "small" if dims and max(dims) <= config.CHECK_SMALL_BAG_MAX_IN else "large"


def label_for(ident: dict, text: str = "") -> tuple[str, float]:
    kind = item_type_of(ident, text)
    costs = label_costs()
    if kind == "bags" and bag_size(ident, text) == "small" and "small_bags" in costs:
        return kind, costs["small_bags"]
    return kind, costs.get(kind, costs["other"])


# --- money (design §4; all rates from config) -------------------------------------

def _ad_rate() -> float:
    """The ad rate a new listing gets (EBAY_DEFAULT_AD_RATE_PCT, 4%), as a fraction."""
    return max(config.EBAY_DEFAULT_AD_RATE_PCT or 0, 0) / 100


def _fee_rate() -> float:
    """eBay's percentage of what the buyer pays (item + shipping): the final
    value fee (EBAY_FVF_PCT, 15.5% effective) plus the listing's ad rate.
    The fixed fee per order (EBAY_FIXED_FEE) comes on top (Person B,
    2026-11-07; fitted on our 85 orders: fees 15.5% + $0.40, ads ~4%,
    together 19.6%)."""
    return config.EBAY_FVF_PCT + _ad_rate()


def ebay_fees(total: float) -> float:
    return total * _fee_rate() + config.EBAY_FIXED_FEE


def at_total(total: float, ross_price: float, label: float) -> dict:
    """What one unit nets when the buyer pays `total` (item + shipping)."""
    fees = ebay_fees(total)
    keep = total - fees - label
    cost = ross_price * (1 + config.SALES_TAX_RATE)
    profit = keep - cost
    return {"total": round(total, 2), "fees": round(fees, 2), "label": round(label, 2),
            "keep": round(keep, 2), "cost": round(cost, 2), "profit": round(profit, 2),
            "roi": round(profit / cost, 2) if cost > 0 else None}


def economics(item_price: float, ross_price: float, label: float,
              ship_charged: float | None = None) -> dict:
    """Profit at an ITEM price we'd list at (plus the shipping we charge),
    and the break-even item price."""
    ship = config.EBAY_SHIP_CHARGED if ship_charged is None else ship_charged
    out = at_total(item_price + ship, ross_price, label)
    cost = ross_price * (1 + config.SALES_TAX_RATE)
    be_total = (cost + label + config.EBAY_FIXED_FEE) / (1 - _fee_rate())
    out.update(item_price=round(item_price, 2), ship_charged=round(ship, 2),
               break_even_total=round(be_total, 2), break_even_item=round(be_total - ship, 2))
    return out


def max_buy(item_price: float | None, label: float, ship_charged: float | None = None,
            profit: float | None = None) -> float | None:
    """The highest Ross tag price (before tax) that still leaves `profit`
    (CHECK_BUY_MIN_PROFIT, $15) when it sells at item_price. Negative: even a
    free item doesn't make it. None without a price."""
    if not item_price:
        return None
    profit = config.CHECK_BUY_MIN_PROFIT if profit is None else profit
    keep = at_total(item_price + (config.EBAY_SHIP_CHARGED if ship_charged is None else ship_charged),
                    0.0, label)["keep"]
    return math.floor((keep - profit) / (1 + config.SALES_TAX_RATE) * 100) / 100


# --- verdict (design §5) ------------------------------------------------------------

def verdict(*, profit: float | None, roi: float | None, sales: int,
            profit_at_cheapest: float | None, low_confidence: bool = False,
            from_asks: bool = False, disagrees: bool = False,
            estimated: bool = False, own_sales: bool = False) -> tuple[str, list[str]]:
    """BUY / MAYBE / BORDERLINE / SKIP and the reasons, from config thresholds."""
    if profit is None:
        return "SKIP", ["no price found"]
    reasons = []
    money_buy = (profit >= config.CHECK_BUY_MIN_PROFIT and (roi or 0) >= config.CHECK_BUY_MIN_ROI
                 and (profit_at_cheapest is None
                      or profit_at_cheapest >= config.CHECK_MIN_PROFIT_AT_CHEAPEST))
    evidence = []
    if sales < config.CHECK_MIN_SALES:
        evidence.append("no sales found" if sales == 0 else
                        f"only {sales} sale{'s' if sales > 1 else ''} (need {config.CHECK_MIN_SALES})")
    if from_asks:
        evidence.append("priced from asking prices, not sales")
    if estimated:
        evidence.append("estimated from similar items and our own data, not same-item sales")
    if low_confidence:
        evidence.append("the judge isn't sure these are the same item")
    if disagrees:
        evidence.append("Claude and the sales formula disagree")
    if money_buy and not evidence:
        return "BUY", reasons
    if money_buy:
        return "MAYBE", evidence
    # SKIP only when real sold prices back it (Person B, 2026-11-03): 2+ eBay
    # sales of the same item, or our own sales (level 3). An estimate from
    # MSRP x ratio or a same-model sale alone with failing money is BORDERLINE.
    backed = (sales >= L1_MIN_SALES and not estimated and not from_asks) or own_sales
    if profit_at_cheapest is not None and profit_at_cheapest < 0 and sales < config.CHECK_MIN_SALES:
        why = [f"a loss at the cheapest identical listing ({_usd(profit_at_cheapest)})"] + evidence
        return ("SKIP", why) if backed else ("BORDERLINE", why)
    if profit >= config.CHECK_MIN_PROFIT:
        why = []
        if profit < config.CHECK_BUY_MIN_PROFIT:
            why.append(f"profit under ${config.CHECK_BUY_MIN_PROFIT:.0f}")
        if (roi or 0) < config.CHECK_BUY_MIN_ROI:
            why.append(f"ROI under {config.CHECK_BUY_MIN_ROI:.0%}")
        if profit_at_cheapest is not None and profit_at_cheapest < config.CHECK_MIN_PROFIT_AT_CHEAPEST:
            why.append(f"only {_usd(profit_at_cheapest)} at the cheapest identical listing")
        return "BORDERLINE", why + evidence
    why = [f"profit {_usd(profit)} is under ${config.CHECK_MIN_PROFIT:.0f}"] + evidence
    return ("SKIP", why) if backed else ("BORDERLINE", why)


# --- the check ------------------------------------------------------------------------

def _charm(x: float) -> float:
    return round(max(round(x) - 0.01, 0.99), 2)


# --- "same model" needs proof (Person B, 2026-11-02) ------------------------------------------

# Words that don't tell one model from another: types, sizes, materials, fillers.
_GENERIC = _FILLER | {w for ws in LABEL_WORDS.values() for w in ws} | {
    "to", "by", "w", "womens", "women", "mens", "men", "ladies", "girls", "boys", "kids", "unisex",
    "large", "extra", "small", "medium", "mini", "xl", "oversized", "big", "canvas", "leather", "faux",
    "nylon", "denim", "cotton", "suede", "vegan", "bags", "shoes", "set", "style", "collection"}
# Materials and finishes: the same model comes in them too ("Vegan Pony Hair").
_MATERIALS = {"pony", "hair", "fur", "sherpa", "plush", "fuzzy", "velvet", "satin", "patent", "pu",
              "polyester", "straw", "woven", "quilted", "metallic", "glitter", "sequin", "genuine",
              "pebbled", "saffiano", "vinyl", "mesh", "corduroy", "teddy", "shearling", "fleece"}
# Colour words that name a pattern: a different pattern is a different model.
_PATTERNS = {"denim", "leopard", "paisley", "floral", "plaid"}
OTHER_STYLE = "other_style"     # a judge "variant" with no style code or product name match


def _model_words(ident: dict) -> list[str]:
    """The product name's distinctive words, in order: not the brand, type,
    size, colour or material words. The same model comes in other colours and
    materials (check #10: "Purple & White Cow Print Vegan Pony Hair Bowling
    Bag" -> cow, print, bowling, so the brown one is the same model)."""
    brand = set(re.findall(r"[a-z0-9]+", str(ident.get("brand") or "").lower()))
    words = re.findall(r"[a-z0-9]+", str(ident.get("product_name") or "").lower())
    skip = _GENERIC | _MATERIALS | (set(COLOURS) - _PATTERNS)
    out = []
    for w in words:
        if w not in brand and w not in skip and w.rstrip("s") not in skip and w not in out:
            out.append(w)
    return out


def model_tokens(ident: dict) -> set:
    """The product name's distinctive words (not the brand, type, size,
    colour or material words): "Juicy To Go Weekender Tote" -> {"go",
    "weekender"}; "LBAVERY Extra Large Canvas Tote" -> {"lbavery"}."""
    return set(_model_words(ident))


FEWER_WORDS_MAX = 4


def fewer_words_query(ident: dict) -> str | None:
    """eBay's "results matching fewer words" (check #10: the long queries found
    no sales, "Von Dutch cow print bowling bag" found the same bag in four
    colours): the brand, up to FEWER_WORDS_MAX distinctive words of the
    product name (no colour, material or size) and the item type's last word."""
    brand = str(ident.get("brand") or "").strip()
    words = _model_words(ident)[:FEWER_WORDS_MAX]
    if not brand or not words:
        return None
    kind = re.findall(r"[a-z]+", str(ident.get("item_type") or "").lower())
    name = re.findall(r"[a-z]+", str(ident.get("product_name") or "").lower())
    noun = kind[-1] if kind else (name[-1] if name else "")
    if noun and noun not in words:
        words.append(noun)
    return f"{brand} {' '.join(words)}"


def _codes(ident: dict) -> list[str]:
    """The style code, and its base without a colour suffix (11JCDA0JC-JTL -> 11JCDA0JC)."""
    out = []
    for raw in (ident.get("model"), ident.get("style_code")):
        code = _norm_code(raw)
        if len(code) >= 5 and _looks_like_code(code):
            out.append(code)
            base = _norm_code(re.split(r"[-/ ]", str(raw).strip())[0])
            if len(base) >= 5 and _looks_like_code(base) and base not in out:
                out.append(base)
    return out


def same_model(comp: dict, ident: dict) -> bool:
    """Is a sold comp the same model in another colour or size? Only when its
    title carries our style code, or every distinctive word of our product
    name (a title cut short may end in the start of the last one: "Weeken").
    A different style of the same brand (the cat-face faux-fur tote vs the
    LBAVERY canvas tote) never counts, whatever the judge said."""
    title = str(comp.get("title") or "").lower()
    flat = _norm_code(title)
    if any(c in flat for c in _codes(ident)):
        return True
    need = model_tokens(ident)
    if not need:
        return False
    words = re.findall(r"[a-z0-9]+", title)
    cut = title.rstrip().endswith(("...", "…")) or len(title) >= 75      # eBay titles stop at 80
    tail = words[-1] if words and cut else None

    def has(w):
        return w in words or (tail is not None and len(tail) >= 4 and w.startswith(tail))
    return all(has(w) for w in need)


def tighten_same_model(pricing: dict, ident: dict) -> dict:
    """The judge's "variant" verdicts that fail same_model become OTHER_STYLE:
    out of level 2 (and of level 1 in any mode), still listed in Details.
    Idempotent; the pricing dict itself is not changed."""
    judged = pricing.get("judged_comps") or []
    if not any(c.get("verdict") == "variant" for c in judged):
        return pricing
    out = [{**c, "verdict": OTHER_STYLE, "judge_verdict": "variant"}
           if c.get("verdict") == "variant" and not same_model(c, ident) else c for c in judged]
    return {**pricing, "judged_comps": out}


def best_offer(price: float | None) -> dict | None:
    """Best Offer from the fixed-price estimate (all .99): list x CHECK_BO_LIST,
    auto-accept x CHECK_BO_ACCEPT, auto-decline under x CHECK_BO_DECLINE."""
    if not price:
        return None
    return {"fixed": price, "list": _charm(price * config.CHECK_BO_LIST),
            "accept": _charm(price * config.CHECK_BO_ACCEPT), "decline": _charm(price * config.CHECK_BO_DECLINE)}


def similar_queries(ident: dict) -> list[tuple[str, str]]:
    """Extra sold searches for a full check: the brand's UPC, the name in
    fewer words (fewer_words_query), then similar items (brand + item type,
    e.g. "Juicy Couture Weekender Tote Bag")."""
    out = []
    upc = re.sub(r"\D", "", str(ident.get("upc") or ""))
    if len(upc) in (12, 13, 14):
        out.append(("upc", upc))
    fewer = fewer_words_query(ident)
    if fewer:
        out.append(("fewer", fewer))
    brand, kind = (str(ident.get(k) or "").strip() for k in ("brand", "item_type"))
    if brand and kind:
        out.append(("similar", f"{brand} {kind}"))
    return out


def similar_sold(pricing: dict) -> dict:
    """Sold comps the judge called a variant (same line, other colour/size):
    the similar-items figures, shown next to exact matches."""
    t = sorted(c["total"] for c in pricing.get("judged_comps") or []
               if c.get("source") == "sold" and c.get("verdict") == "variant" and c.get("total"))
    return {"n": len(t), "median": round(statistics.median(t), 2) if t else None,
            "low": t[0] if t else None, "high": t[-1] if t else None}


# --- dropship-style listings and demand (Person B, 2026-11-02) ---------------------------

# Phrases of titles copied from Amazon (two of them make one signal).
_AMAZON_PHRASES = ("for women", "for men", "for girls", "for boys", "large capacity", "gift for",
                   "gifts for", "perfect for", "suitable for", "ideal for", "stylish & versatile",
                   "stylish and versatile", "multi-pocket", "with multiple", "with compartments")
# Where an item of each label kind belongs on eBay (the first part of the category path).
_ROOTS = {"bags": "clothing, shoes & accessories", "shoes": "clothing, shoes & accessories",
          "clothing": "clothing, shoes & accessories", "jewelry": "jewelry & watches"}


def dropship_signals(comp: dict, kind: str) -> list[str]:
    """Why a listing looks dropshipped (2+ signals = flagged): a copied title
    (cut off with "..." or Amazon phrases), a very high-volume seller, a
    category that does not fit the item (a bag under Baby > Diaper Bags)."""
    out = []
    title = (comp.get("title") or "").strip()
    low = title.lower()
    if title.endswith(("...", "…")) or sum(p in low for p in _AMAZON_PHRASES) >= 2:
        out.append("copied title")
    fb = comp.get("seller_feedback")
    if fb is not None and fb >= config.CHECK_DROPSHIP_FEEDBACK:
        out.append(f"seller {fb:,} feedback")
    path = (comp.get("category_path") or "").strip()
    root = _ROOTS.get(kind)
    if path and root and path.split("|")[0].strip().lower() != root:
        out.append("category " + " > ".join(x.strip() for x in path.split("|")[-2:]))
    return out


def is_dropship(comp: dict, kind: str) -> bool:
    return len(dropship_signals(comp, kind)) >= 2


def with_details(c: dict, details: dict | None) -> dict:
    """A comp plus what the item lookup found (category, seller feedback)."""
    d = (details or {}).get(c.get("ebay_id")) or {}
    if not d:
        return c
    fb = d.get("seller_feedback")
    return {**c, "category_path": d.get("category_path"), "listed": d.get("created"),
            "units_sold": d.get("sold_quantity"), "title": d.get("title") or c.get("title"),
            "seller_feedback": fb if fb is not None else c.get("seller_feedback")}


def _age_days(iso, now: datetime | None = None):
    try:
        t = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return max(0, int(((now or datetime.now(timezone.utc)) - t).total_seconds() // 86400))


def demand_lookup(pricing: dict, *, details_fn=None, limit: int | None = None) -> dict:
    """Full check: eBay's facts on the cheapest identical listings (listed
    since, units sold, seller, category), by legacy id. Read only, app token;
    a failed lookup is skipped. eBay gives no watcher counts for listings
    that are not ours."""
    from concurrent.futures import ThreadPoolExecutor
    from ebay import browse
    details_fn = details_fn or browse.listing_details
    limit = config.CHECK_DEMAND_LISTINGS if limit is None else limit
    ids = []
    for c in sorted((c for c in pricing.get("judged_comps") or []
                     if c.get("source") != "sold" and c.get("verdict") == "same_item" and c.get("total")),
                    key=lambda c: c["total"]):
        if c.get("ebay_id") and c["ebay_id"] not in ids:
            ids.append(c["ebay_id"])
    ids = ids[:limit]

    def one(i):
        try:
            return i, details_fn(i)
        except Exception:
            return i, None
    if not ids:
        return {}
    with ThreadPoolExecutor(max_workers=len(ids)) as pool:
        return {i: d for i, d in pool.map(one, ids) if d}


def comp_list(pricing: dict, kind: str, exact: bool, details: dict | None = None) -> list[dict]:
    """Every comp the price used: sold same item and same model (newest
    first), then identical listings for sale (cheapest first). Dropship-style
    listings are flagged: out of the sold evidence, still in the ceiling."""
    details = details or {}
    sold_v = ("same_item", "variant")
    out = []
    for c in pricing.get("judged_comps") or []:
        src = "sold" if c.get("source") == "sold" else "active"
        if not c.get("total") or (src == "sold" and c.get("verdict") not in sold_v)                 or (src == "active" and c.get("verdict") != "same_item"):
            continue
        m = with_details(c, details)
        row = {k: m.get(k) for k in ("ebay_id", "verdict", "title", "total", "url", "date", "seller",
                                     "seller_feedback", "category_path", "listed", "units_sold")}
        row["status"] = src
        row["signals"] = dropship_signals(row, kind)
        row["dropship"] = len(row["signals"]) >= 2
        out.append(row)
    sold = sorted((r for r in out if r["status"] == "sold"), key=lambda r: str(r.get("date") or ""), reverse=True)
    active = sorted((r for r in out if r["status"] == "active"), key=lambda r: r["total"])
    return sold + active


def demand_summary(comps: list[dict], now: datetime | None = None) -> str | None:
    """"3 for sale 12–147d, 0 sold" from the looked-up identical listings."""
    rows = [r for r in comps if r["status"] == "active" and r.get("listed")]
    if not rows:
        return None
    ages = sorted(a for a in (_age_days(r["listed"], now) for r in rows) if a is not None)
    sold = sum(r.get("units_sold") or 0 for r in rows)
    span = f"{ages[0]}d" if len(ages) == 1 else f"{ages[0]}–{ages[-1]}d"
    return f"{len(rows)} for sale {span}, {sold} sold"


# --- saturation: sales vs listings for sale (Person B, 2026-11-06) --------------------------

_SIZE_WORDS = {"mini"}


def _colour_words(text: str) -> list[str]:
    """Colour words in order (not pattern words: those name a model)."""
    plain = set(COLOURS) - _PATTERNS
    out = []
    for w in re.findall(r"[a-z]+", str(text or "").lower()):
        w = "grey" if w == "gray" else w
        if w in plain and w not in out:
            out.append(w)
    return out


def same_item_title(comp: dict, ident: dict) -> bool:
    """A listing of the same item in our colour: the same model (same_model)
    with our full style code, or with our main colour and no colour we don't
    have ("Purple Cow Print ... Bowling Bag" for the purple & white bag; the
    brown one is the same model, not the same item). A mini is not the same
    item as a bag that isn't one."""
    if not same_model(comp, ident):
        return False
    title = str(comp.get("title") or "").lower()
    words = set(re.findall(r"[a-z]+", title))
    name = " ".join(str(ident.get(k) or "") for k in ("product_name", "size")).lower()
    if any(w in words and w not in name for w in _SIZE_WORDS):
        return False
    codes = _codes(ident)
    if codes and codes[0] in _norm_code(title):
        return True
    ours = _colour_words(ident.get("product_name")) or _colour_words(ident.get("color"))
    if not ours:
        return False
    ours_all = set(ours) | set(_colour_words(ident.get("color")))
    theirs = _colour_words(title)
    return ours[0] in theirs and set(theirs) <= ours_all


def _supply_part(sold: int, active: int, capped: bool, active_capped: bool) -> dict:
    months = round(active / sold, 1) if sold else None
    high = not capped and active > 0 and (
        (months is not None and months > config.CHECK_SUPPLY_HIGH_MONTHS)
        or (not sold and active >= config.CHECK_SUPPLY_MIN_ACTIVE))
    return {"sold": sold, "active": active, "months": months, "high": high,
            "sold_capped": capped, "active_capped": active_capped}


def count_supply(sold_rows: list[dict], active_rows: list[dict], ident: dict, judged: list | None = None,
                 *, now: datetime | None = None, full_sold: list | None = None,
                 full_active: list | None = None) -> dict:
    """Sales in the last CHECK_SUPPLY_DAYS and listings for sale now, of the
    same item and of the same model (all colours; the same item included).
    Rows are pricing candidates (pricing_v2.sold_candidate / active_candidate),
    own listings already left out. A row counts by its title (same_model,
    same_item_title) or by the judge's verdict on it (judged, by ebay_id).
    full_sold / full_active: the rows of searches that came back as a full
    page (sold: all inside the window). When most of such a page is the
    item or model counted, there are more of them than the page shows: the
    count is a floor ("7+") and a sales floor is never called high supply.
    A full page of mostly other styles ("Von Dutch Bowling Bag") is not."""
    verdicts = {c.get("ebay_id"): c.get("verdict") for c in judged or [] if c.get("ebay_id")}
    today = (now or datetime.now(EASTERN)).date()

    def recent(c):
        try:
            d = datetime.fromisoformat(str(c.get("date"))[:10]).date()
        except (TypeError, ValueError):
            return False
        return (today - d).days <= config.CHECK_SUPPLY_DAYS

    brand = re.sub(r"[^a-z0-9]", "", str(ident.get("brand") or "").lower())

    def kind(c):
        v = verdicts.get(c.get("ebay_id"))
        # by title only when it names our brand ("Lime Green Cow Print ... Bowling Bag" may be anyone's)
        titled = not brand or brand in re.sub(r"[^a-z0-9]", "", str(c.get("title") or "").lower())
        if v == "same_item" or (titled and same_item_title(c, ident)):
            return "item"
        if v == "variant" or (titled and same_model(c, ident)):
            return "model"
        return None
    from pipeline import pricing_v2
    sold = [c for c in pricing_v2.dedupe([c for c in sold_rows if c]) if recent(c)]
    active = pricing_v2.dedupe([c for c in active_rows if c])
    s_kind, a_kind = [kind(c) for c in sold], [kind(c) for c in active]

    def floor(pages, kinds):
        for rows in pages or []:
            rows = [c for c in rows if c]
            if rows and sum(kind(c) in kinds for c in rows) * 2 > len(rows):
                return True
        return False
    item_k, model_k = ("item",), ("item", "model")
    item = _supply_part(s_kind.count("item"), a_kind.count("item"),
                        floor(full_sold, item_k), floor(full_active, item_k))
    model = _supply_part(sum(k in model_k for k in s_kind), sum(k in model_k for k in a_kind),
                         floor(full_sold, model_k), floor(full_active, model_k))
    return {"days": config.CHECK_SUPPLY_DAYS, "item": item, "model": model,
            "high": item["high"] or model["high"],
            "high_part": "model" if model["high"] else "item" if item["high"] else None}


def supply_lookup(pricing: dict, ident: dict, *, fetch_sold=None, fetch_active=None,
                  now: datetime | None = None) -> dict | None:
    """Full check: the saturation counts. Sold rows are re-read from the cache
    the full check just filled (no new Apify call); listings are the pricing
    searches plus one free Browse search on the name in fewer words with up to
    CHECK_SUPPLY_ACTIVE_COUNT rows (the long-name search finds few listings
    of the other colours). None when nothing could be counted."""
    from pipeline import pricing_v2
    fetch_sold = fetch_sold or pricing_v2.fetch_sold
    fetch_active = fetch_active or pricing_v2.fetch_active
    ship = config.EBAY_SHIP_CHARGED
    sold, full_q = [], []
    window = config.CHECK_SUPPLY_DAYS
    today = (now or datetime.now(EASTERN)).date()
    for q in pricing.get("rungs_tried") or []:
        try:
            rows = fetch_sold(q, cache_only=True) or []
        except Exception:
            rows = []
        cands = [c for c in (pricing_v2.sold_candidate(r, ship) for r in rows) if c]
        sold += cands
        dates = []
        for c in cands:
            try:
                dates.append(datetime.fromisoformat(str(c.get("date"))[:10]).date())
            except (TypeError, ValueError):
                pass
        if len(rows) >= config.PRICING_V2_SOLD_COUNT and dates and (today - min(dates)).days <= window:
            full_q.append(cands)
    active, full_a = [], []
    searches = [(pricing_v2.active_query(ident), None, None)]
    if pricing_v2.upc_of(ident):
        searches.append((None, pricing_v2.upc_of(ident), None))
    fewer = fewer_words_query(ident)
    if fewer:
        searches.append((fewer, None, config.CHECK_SUPPLY_ACTIVE_COUNT))
    for q, gtin, limit in searches:
        if not (q or gtin):
            continue
        try:
            rows = fetch_active(q, gtin, limit=limit) if limit else fetch_active(q, gtin)
        except Exception:
            continue
        cands = [pricing_v2.active_candidate(r, ship) for r in rows]
        if limit:                  # the wide search decides alone: a short page of it is all there is
            full_a = [cands] if len(rows) >= limit else []
        elif len(rows) >= config.PRICING_V2_ACTIVE_COUNT:
            full_a.append(cands)
        active += cands
    if not sold and not any(active):
        return None
    try:
        own_ids, own_titles = pricing_v2.own_listings()
    except Exception:
        own_ids, own_titles = set(), set()
    kept_s, _ = pricing_v2.filter_candidates(sold, config.OWN_SELLER_USERNAMES, own_ids, own_titles)
    kept_a, _ = pricing_v2.filter_candidates(active, config.OWN_SELLER_USERNAMES, own_ids, own_titles)
    return count_supply(kept_s, kept_a, ident, pricing.get("judged_comps"), now=now,
                        full_sold=full_q, full_active=full_a)


def _supply_count(n: int, capped: bool) -> str:
    return f"{n}+" if capped else str(n)


def supply_text(part: dict, days: int) -> str:
    """"4 sold / 20 active in 30d (~5 months of supply)"."""
    s = f"{_supply_count(part['sold'], part.get('sold_capped'))} sold / " \
        f"{_supply_count(part['active'], part.get('active_capped'))} active in {days}d"
    if part.get("sold_capped"):
        tail = "fast seller" if part["sold"] else ""
    elif not part["sold"]:
        tail = "no sales" if part["active"] else ""
    elif part["months"] < 1:
        tail = "under a month of supply"
    else:
        tail = f"~{part['months']:.0f} month{'s' if round(part['months']) != 1 else ''} of supply"
    if part.get("high"):
        tail = (tail + ", high" if tail else "high")
    return s + (f" ({tail})" if tail else "")


def supply_line(res: dict) -> str | None:
    """"Supply: same model 4 sold / 20 active in 30d (~5 months of supply,
    high) · same item 1 sold / 3 active in 30d (~3 months of supply)"."""
    sup = res.get("supply")
    if not sup:
        return None
    parts = [f"same model {supply_text(sup['model'], sup['days'])}"]
    if sup["item"]["sold"] or sup["item"]["active"]:
        parts.append(f"same item {supply_text(sup['item'], sup['days'])}")
    return ("⚠️ " if sup.get("high") else "") + "Supply: " + " · ".join(parts)


LEVEL_NAMES = {1: "eBay sold, same item", 2: "eBay sold, same model other colour/size",
               3: "our own sales, same brand + type", 4: "MSRP × brand ratio",
               5: "eBay asking prices (ceiling only)"}
LADDER_SPREAD = 1.40      # levels further apart than this: show the range
L1_MIN_SALES = 2


def _days(ebay: dict):
    """Listing to first sale, in days (None when unknown or inconsistent)."""
    from datetime import datetime as _dt
    try:
        a = _dt.fromisoformat(str(ebay.get("published_at")).replace("Z", "+00:00"))
        b = _dt.fromisoformat(str(ebay.get("first_sold_at") or ebay.get("sold_at")).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    d = (b - a).total_seconds() / 86400
    return round(d, 1) if d >= 0 else None


def load_our_sales() -> list[dict]:
    """Our sold items for levels 3-4: msrp.sold_history plus days to sell (read only)."""
    import db
    from pipeline import msrp
    rows = db.sold_items_for_calibration()
    hist = msrp.sold_history([(s["item_id"], s["identification"], s["ebay"], s["receipt"]) for s in rows])
    days = {s["item_id"]: _days(s["ebay"] if isinstance(s["ebay"], dict) else json.loads(s["ebay"] or "{}"))
            for s in rows}
    for h in hist:
        h["days"] = days.get(h["item_id"])
    return hist


def _median(xs):
    return round(statistics.median(xs), 2) if xs else None


def choose_price(pricing: dict, exact: bool, ident: dict | None = None, history: list | None = None,
                 receipt: dict | None = None, exclude_item_id: str | None = None,
                 details: dict | None = None, supply: dict | None = None) -> dict:
    """The evidence ladder (Person B, 2026-11-02). Every level is an ITEM price
    (eBay totals minus the shipping we charge):
      1 eBay sold, same item: 2+ sales -> their median, done.
      2 eBay sold, same model other colour/size (the judge's variants), 1+,
        plus a single same-item sale.
      3 our own sales, same brand + type (median sold price).
      4 MSRP x brand ratio (our sold/MSRP; type bucket when the brand has
        too few sales; never the all-sales fallback).
      5 eBay asking prices x CHECK_ASK_DISCOUNT: a ceiling, never the price.
    Real sales win (Person B, 2026-11-06): no level 1 -> the median of levels
    2 and 3; no eBay sales either -> levels 3 and 4. Only a price without
    eBay sales is capped at level 5 (never above the cheapest identical
    listing). Any mode counts variants as level 1.
    Competition (Person B, 2026-11-02): no identical listing for sale
    (dropship-style ones don't count) and demand shown by level 2 or 3 ->
    the upper end of levels 2-4 ("no competition, unique"); no demand
    either -> the median stays ("no competition, no demand data").
    High supply (supply_lookup, Person B 2026-11-06): a price from eBay
    sales goes no higher than the lower quartile of those sales."""
    from pipeline import msrp
    ident = ident or {}
    kind = item_type_of(ident)
    all_judged = pricing.get("judged_comps") or []
    # Dropship-style sold rows are not evidence of what a buyer pays us.
    judged = [c for c in all_judged if c.get("source") != "sold" or not is_dropship(c, kind)]
    dropped = len(all_judged) - len(judged)
    ship = config.EBAY_SHIP_CHARGED
    same_v = ("same_item", "variant") if not exact else ("same_item",)
    same = sorted(c["total"] for c in judged if c.get("source") == "sold"
                  and c.get("verdict") in same_v and c.get("total"))
    variant = [] if not exact else sorted(c["total"] for c in judged if c.get("source") == "sold"
                                          and c.get("verdict") == "variant" and c.get("total"))
    levels = {}
    if len(same) >= L1_MIN_SALES:
        levels[1] = {"value": round(_median(same) - ship, 2), "n": len(same),
                     "detail": f"median of {len(same)} sales ({_median(same):.2f} total)"}
    # A single same-item sale is too few for level 1, but it is a real sale:
    # it joins the same-model sales in level 2 (check #10, Person B 2026-11-06).
    lone = same if len(same) < L1_MIN_SALES else []
    l2 = sorted(variant + lone)
    if l2:
        levels[2] = {"value": round(_median(l2) - ship, 2), "n": len(l2), "same_item": len(lone),
                     "detail": f"median of {len(l2)} sale(s) ({_median(l2):.2f} total)"
                               + (f", {len(lone)} of them the same item" if lone else "")}
    if history is None:
        try:
            history = load_our_sales()
        except Exception:
            history = []
    bk, tb = msrp.brand_key(ident.get("brand")), msrp.type_bucket(ident.get("item_type"))
    mine = [h for h in history if bk and h["brand"] == bk and h["bucket"] == tb
            and not (exclude_item_id and h["item_id"].startswith(exclude_item_id))]
    if mine:
        days = [h["days"] for h in mine if h.get("days") is not None]
        levels[3] = {"value": _median([h["sold"] for h in mine]), "n": len(mine),
                     "days": _median(days), "brand": ident.get("brand"), "bucket": tb,
                     "detail": f"median of {len(mine)} of our {bk} {tb} sale(s)"
                               + (f", median {_median(days):.0f} days to sell" if days else "")}
    try:
        est = msrp.store_estimate(ident, receipt, exclude_item_id=exclude_item_id, history=history)
    except Exception:
        est = {}
    if est.get("estimate") and not est.get("msrp_flags") and est.get("level") in ("brand", "type"):
        levels[4] = {"value": est["estimate"], "n": est.get("n"), "ratio": est.get("ratio"),
                     # "type": no sales of this brand, the ratio is other brands' (Person B)
                     "level": est.get("level"), "key": est.get("key"),
                     "detail": f"MSRP ${est['msrp']:.2f} × {est['ratio']} ({est['level']} {est['key']}, "
                               f"{est.get('n')} sales)"
                               + ("; no sales of this brand yet: ratio from other brands"
                                  if est.get("level") == "type" else "")}
    cheapest = pricing.get("raw_active_floor")
    if cheapest:
        levels[5] = {"value": round(cheapest * config.CHECK_ASK_DISCOUNT - ship, 2), "n": None,
                     "detail": f"cheapest identical ${cheapest:.2f} total × {config.CHECK_ASK_DISCOUNT}"}
    # Real sales win (Person B, 2026-11-06): eBay sales of the same item
    # (level 1), else of the same model (level 2) with our own sales (level 3).
    # MSRP x ratio (level 4) is only used when there are no sales at all.
    if 1 in levels:
        used = [1]
    elif 2 in levels:
        used = [k for k in (2, 3) if k in levels]
    else:
        used = [k for k in (3, 4) if k in levels]
    sold_backed = 1 in used or 2 in used
    raw = levels[1]["value"] if 1 in levels else _median([levels[k]["value"] for k in used])
    competitors = [c for c in all_judged if c.get("source") != "sold" and c.get("verdict") == "same_item"
                   and c.get("total") and not is_dropship(with_details(c, details), kind)]
    note, upper = None, False
    if not competitors and used:
        if 1 in used:
            note = "no competition"
        elif 2 in levels or 3 in levels:
            raw, upper, note = max(levels[k]["value"] for k in used), True, "no competition, unique"
        else:
            note = "no competition, no demand data"
    low_end = None
    if supply and supply.get("high") and sold_backed and raw is not None:
        real = same if 1 in used else l2
        q1 = statistics.quantiles(real, n=4, method="inclusive")[0] if len(real) > 1 else real[0]
        low_end = round(q1 - ship, 2)
        if low_end < raw:
            raw, upper = low_end, False
        else:
            low_end = None
    capped = False
    # Asking prices are a ceiling only for estimates: when eBay sales back the
    # price, an unsold ask doesn't lower it (Person B, 2026-11-06).
    if raw is not None and 5 in levels and levels[5]["value"] < raw and not sold_backed:
        raw, capped = levels[5]["value"], True
    price = _charm(raw) if raw and raw > 0 else None
    vals = [levels[k]["value"] for k in used]
    spread = (min(vals), max(vals)) if len(vals) > 1 and min(vals) > 0 \
        and max(vals) / min(vals) > LADDER_SPREAD else None
    if 1 in used:
        basis = "eBay sales of the same item (level 1)"
    elif used:
        basis = ("upper end" if upper else "median") + " of levels " + ", ".join(str(k) for k in used)             + " (no same-item sales)"
    else:
        basis = "no level 1-4 evidence"
    if low_end is not None:
        basis += ", lowered to the lower quartile of the sales (high supply)"
    return {"price": price, "basis": basis + (", capped at the asking-price ceiling" if capped else ""),
            "low_end": low_end,
            "levels": levels, "used": used, "capped": capped, "range": spread,
            "sales": len(same), "from_asks": False, "estimated": bool(used) and 1 not in used,
            "sold_median": _median(same), "disagrees": False, "ask_discount": None,
            "dropship_excluded": dropped, "competitors": len(competitors), "competition_note": note,
            "upper_end": upper}


def evaluate(pricing: dict, ident: dict, ross_price: float | None, exact: bool,
             text: str = "", quick: bool = False, history: list | None = None,
             exclude_item_id: str | None = None, details: dict | None = None,
             supply: dict | None = None) -> dict:
    """Price choice, money and verdict from a pricing result. Shared by
    run_check and the backtest (scripts/backtest_check.py), so the backtest
    scores exactly what /check would say. quick: a quick check that found no
    data at all is NO_DATA instead of SKIP. supply (full check,
    supply_lookup): high supply lowers the verdict one step."""
    pricing = tighten_same_model(pricing, ident or {})
    pick = choose_price(pricing, exact, ident, history=history, exclude_item_id=exclude_item_id,
                        details=details, supply=supply)
    kind, label = label_for(ident, text)
    money = economics(pick["price"], ross_price, label) if pick["price"] and ross_price else None
    cheapest = pricing.get("raw_active_floor")
    at_cheapest = at_total(cheapest, ross_price, label) if cheapest and ross_price else None
    if ross_price is None:
        v, why = "SKIP", ["no Ross price given"]
    else:
        v, why = verdict(profit=money["profit"] if money else None, roi=money["roi"] if money else None,
                         sales=pick["sales"],
                         profit_at_cheapest=at_cheapest["profit"] if at_cheapest else None,
                         low_confidence=pricing.get("claude_confidence") == "low",
                         from_asks=pick["from_asks"], disagrees=pick["disagrees"],
                         estimated=pick.get("estimated", False), own_sales=3 in (pick.get("used") or []))
        if money is None:
            # No price at all is not evidence against the item (Person B,
            # 2026-11-02: in the backtest 14 of 20 such SKIPs were winners).
            # SKIP is only given when real prices back it.
            v, why = NO_DATA, (["the quick check found no recent sales and no identical listing"] if quick
                               else ["no sales and no identical listing found: not enough data for a verdict"])
    if supply and supply.get("high") and v in STEP_DOWN:
        part = supply[supply["high_part"]]
        what = "same model" if supply["high_part"] == "model" else "same item"
        v, why = STEP_DOWN[v], [f"high supply: {what} {supply_text(part, supply['days'])}, "
                                f"one step lower"] + list(why)
    size =bag_size(ident or {}, text) if kind == "bags" else None
    return {"item_type": kind, "label": label, "label_size": size, "verdict": v, "reasons": why, "pick": pick,
            "money": money, "cheapest_total": cheapest, "at_cheapest": at_cheapest,
            "similar": similar_sold(pricing), "offer": best_offer(pick["price"]),
            "max_buy": max_buy(pick["price"], label), "supply": supply}


def _norm_code(v) -> str:
    return re.sub(r"\W", "", str(v or "")).lower()


def already_have(ident: dict, sku: str | None = None, exclude_check_id: int | None = None) -> dict:
    """Do I already have this? Items, pieces, receipt lines and earlier checks
    (pipeline/owned.py). Read only; never raises."""
    from pipeline import owned
    return owned.lookup(ident, sku, exclude_check_id)


def run_check(req: dict, *, photo_path: str | None = None, pricing_fn=None, identify_fn=None,
              have_fn=None, details_fn=None, supply_fn=None) -> dict:
    """Identify (photo or text), price, money, verdict. Returns everything the
    reply and the timings log need. Raises only on programming errors; API
    failures come back as pricing warnings."""
    from pipeline import pricing_v2
    pricing_fn = pricing_fn or pricing_v2.get_pricing_v2
    have_fn = have_fn or already_have
    t0 = time.perf_counter()
    timings = {}
    text = req.get("description") or ""
    if req.get("identification"):
        # A follow-up (auto full check, a button) reuses the first identification:
        # no second photo identification, same search.
        ident = dict(req["identification"])
    elif photo_path and identify_fn is not None:
        t = time.perf_counter()
        ident = identify_fn(photo_path) or {}
        timings["identify_s"] = round(time.perf_counter() - t, 2)
        if text:     # what the user typed sharpens the search
            ident.setdefault("search_query", clean_query(text))
    else:
        ident = build_identification(text)
    full = bool(req.get("full"))
    # Do I already have this? First: a match lends its identification to the search.
    from pipeline import owned
    # a Ross code typed, or read off a brand tag (identify keeps it as ross_code, not a UPC)
    typed_ross = owned.ross_code(ident.get("upc")) or owned.ross_code(ident.get("ross_code"))
    skus = [s for s in (req.get("ross_sku"), req.get("ross_sku_main"), typed_ross) if s]
    try:
        have = have_fn(ident, skus, req.get("check_id"))
    except Exception as e:       # advisory only
        have = {"hits": [], "ai": None, "error": f"{type(e).__name__}: {str(e)[:120]}"}
    if not isinstance(have, dict):   # a fake that returns a list: not looked up, no top line
        have = None
    borrowed = None
    if not req.get("identification"):      # a follow-up's identification is already the final one
        ident, borrowed = owned.borrow_identification(ident, owned.donor(have))
    ident = without_ross_codes(ident)
    no_search = not searchable(ident)
    if no_search:
        # Only a Ross code that isn't ours: it means nothing to eBay, so no search.
        pricing = {"judged_comps": [], "timings": {},
                   "comp_warning": "only a Ross code, which eBay doesn't know: no search"}
    else:
        pricing = pricing_fn(
            ident, photo_paths=[photo_path] if photo_path else None, item_id=None,
            stage="check_judge" if full else "check_quick_judge", escalate=full,
            sold_cache_only=not full, sold_max_age_s=config.CHECK_SOLD_CACHE_DAYS * 86400,
            active_max_age_s=0, extra_sold_queries=similar_queries(ident) if full else None,
            # A UPC or style code already pins the item; image search on a dark
            # photo only adds noise (check #2: 16 Hugo Boss bags).
            image_search=not (pricing_v2.upc_of(ident) or pricing_v2.style_code(ident)))
    for k, v in (pricing.get("timings") or {}).items():
        if k != "total_s":
            timings[k] = v
    raw_verdicts = Counter(c.get("verdict") or "unjudged" for c in pricing.get("judged_comps") or [])
    pricing = tighten_same_model(pricing, ident)
    details = {}
    if full:           # listing age, units sold, category of the cheapest identical listings
        t = time.perf_counter()
        details = demand_lookup(pricing, details_fn=details_fn)
        timings["demand_s"] = round(time.perf_counter() - t, 2)
    supply = None
    if full and not no_search:     # sold in 30 days vs listings for sale (saturation)
        t = time.perf_counter()
        try:
            supply = (supply_fn or supply_lookup)(pricing, ident)
        except Exception:          # advisory: a failed count never loses the check
            supply = None
        timings["supply_s"] = round(time.perf_counter() - t, 2)
    ev = evaluate(pricing, ident, req.get("ross_price"), bool(req.get("exact", True)), text,
                  quick=not full, details=details, supply=supply)
    comps = comp_list(pricing, ev["item_type"], bool(req.get("exact", True)), details)
    timings["total_s"] = round(time.perf_counter() - t0, 2)
    return {"request": dict(req), "photo": bool(photo_path), "identification": ident,
            **ev, "already_have": have, "borrowed": borrowed, "no_search": no_search,
            "typed_ross": typed_ross if typed_ross and not req.get("ross_sku") else None,
            "timings": timings,
            "confidence": pricing.get("claude_confidence"),
            "sold_cache_misses": pricing.get("sold_cache_misses") or [],
            # False: a quick check found none of its sold searches cached, so it
            # looked at no sales at all (check #10); the bot goes on to the full check
            "sold_searched": not (not full and not no_search and pricing.get("rungs_tried")
                                  and len(pricing.get("sold_cache_misses") or [])
                                  >= len(pricing.get("rungs_tried") or [])),
            # every comp used (Details); the first 5 are saved with the check
            "comp_list": comps, "demand": demand_summary(comps),
            "comps": [{"source": c["status"], **{k: c.get(k) for k in ("verdict", "title", "total", "url",
                                                                       "date", "dropship")}}
                      for c in comps][:5],
            "same_item_active": pricing.get("same_item_active_count"),
            "reference": pricing.get("reference"), "warnings": pricing.get("comp_warning"),
            "judge_model": pricing.get("judge_model"), "escalated": pricing.get("judge_escalated"),
            # how the judge labelled every candidate (logged: quick vs full agreement)
            "verdicts": dict(raw_verdicts),
            # judge "variant"s that failed the style code / product name test
            "demoted": sum(c.get("verdict") == OTHER_STYLE for c in pricing.get("judged_comps") or [])}


def owned_saved(have) -> list:
    from pipeline import owned
    return owned.saved(have) if isinstance(have, dict) else []


def check_fields(res: dict) -> dict:
    """A result as checks-table columns (db.update_check)."""
    money, pick, req = res.get("money") or {}, res["pick"], res["request"]
    typed = {"ross_sku": res["typed_ross"]} if res.get("typed_ross") else {}   # /check 400301430764 15.99
    return {**typed, "identification": res["identification"], "mode": "full" if req.get("full") else "quick",
            "exact": bool(req.get("exact", True)), "verdict": res["verdict"],
            "suggested_price": pick.get("price"), "profit": money.get("profit"), "roi": money.get("roi"),
            "break_even": money.get("break_even_item"), "cheapest_identical": res.get("cheapest_total"),
            "sold_count": pick.get("sales"),
            "comps_summary": {"basis": pick.get("basis"), "sales": pick.get("sales"),
                              "sold_median": pick.get("sold_median"), "item_type": res.get("item_type"),
                              "label": res.get("label"), "reasons": res.get("reasons"),
                              "max_buy": res.get("max_buy"), "supply": res.get("supply"),
                              "comps": res.get("comps") or [],
                              "owned": owned_saved(res.get("already_have")),
                              "warnings": (res.get("warnings") or "")[:500] or None}}


# The rotation-aware reader lives in ross_tag (intake uses it too).
BARCODE_BUDGET_S = 6.0


def _barcode_result(got: dict) -> dict:
    """code + reduced_price, and main_code only when the tag has a second code."""
    out = {"code": got["code"], "reduced_price": got["reduced_price"]}
    if got.get("main_code"):
        out["main_code"] = got["main_code"]
    return out


def read_ross_barcode(path: str, budget_s: float = BARCODE_BUDGET_S) -> dict:
    """{"code", "reduced_price", "main_code"} from the Ross barcodes anywhere in
    the photo, or {}. The REDUCED sticker (lowest price) is the code; a main
    tag with another code is main_code (ross_tag.split_codes). Plain decode
    first (every barcode), then rotations of the whole photo (every barcode at
    the first angle that reads), then at double size, until the time budget
    runs out. Local, no AI."""
    import ross_tag
    found = ross_tag.read_barcodes(path, budget_s)      # the same reader as intake
    if not found:
        return {}
    got = ross_tag.split_codes(found)
    if got.get("code"):
        return _barcode_result(got)
    parsed = ross_tag.parse_barcode(found[0])
    return parsed if parsed.get("code") else {}


def read_ross_vision(path: str) -> dict:
    """Last resort: the vision model reads the printed tag (ross_tag's own
    fallback). Its prompt takes the REDUCED price over "Original $"."""
    import ross_tag
    return ross_tag.vision_read_tag(path) or {}


def photo_roles(paths: list[str], decode=None, vision=None) -> dict:
    """Which photo is the Ross tag, the SKU and the Ross price.
    1. Barcode (read_ross_barcode, with rotations; no AI) on every photo: the
       first that reads as a Ross code (12-digit SKU + price in cents) wins.
    2. None read: the vision model reads the printed prices (REDUCED sticker
       over "Original $") on each photo, tag-looking ones first, until one
       gives a price.
    The other photos go to identification (the tag too when it's the only
    photo); all photos are scanned for UPC/style codes."""
    decode = decode or read_ross_barcode
    vision = vision or read_ross_vision
    tag, sku, price, source, main = None, None, None, None, None
    for p in paths:
        try:
            parsed = decode(p) or {}
        except Exception:
            parsed = {}
        code = parsed.get("code")
        if code and len(str(code)) == 12:
            tag, sku, price, source = p, str(code), parsed.get("reduced_price"), "barcode"
            main = parsed.get("main_code")
            break
    if tag and not main:
        # The main tag and the REDUCED sticker photographed apart: a quick plain
        # decode of the other photos (no rotations) for a second code.
        import ross_tag
        bars = [b for p in paths if p != tag for b in ross_tag.decode_barcodes(p) if len(b) == 18]
        if bars:
            both = ross_tag.split_codes(bars + ([f"{sku}{round(price * 100):06d}"] if price else [sku]))
            if both.get("code") and both.get("main_code"):
                sku, price, main = both["code"], both["reduced_price"], both["main_code"]
    if price is None:
        def tagness(p):
            try:
                import ross_tag
                return 0 if ross_tag.looks_like_tag(p) else 1
            except Exception:
                return 1
        for p in ([tag] if tag else sorted(paths, key=tagness)):
            try:
                got = vision(p) or {}
            except Exception:
                got = {}
            if got.get("reduced_price"):
                tag, price, source = p, got["reduced_price"], "vision"
                sku = sku or got.get("code")
                break
    others = [p for p in paths if p != tag]
    return {"tag": tag, "sku": sku, "sku_main": main if main != sku else None, "price": price,
            "price_source": source, "identify": others or ([tag] if tag else []), "scan": list(paths)}


# --- reply -------------------------------------------------------------------------------

def _usd(x) -> str:
    """$12.50; a loss with a real minus sign: −$0.63."""
    if x is None:
        return "-"
    return f"−${-x:,.2f}" if x < 0 else f"${x:,.2f}"


CERTAINTY = {"high": "certain", "medium": "likely", "low": "uncertain"}


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def _sales_phrase(n: int) -> str:
    return "no sales found" if not n else _plural(n, "sale")


def _listings_phrase(res: dict) -> str:
    m = res.get("same_item_active") or 0
    if not m:
        return "no identical listing for sale"
    sure = "uncertain " if res.get("confidence") == "low" else ""
    return f"{m} {sure}listing{'' if m == 1 else 's'}"


def _item_name(res: dict) -> str:
    ident, req = res["identification"], res["request"]
    desc = req.get("description") or ""
    if desc and not _ROSS_IN_TEXT.sub("", desc).strip(" -"):
        desc = f"Ross code {desc.strip()}"       # only a code was typed: say so, it isn't a name
    name = " ".join(str(ident.get(k)) for k in ("brand", "product_name") if ident.get(k)) or desc or "item"
    model = ident.get("model")
    if model and _norm_code(model) not in _norm_code(name):    # no duplicated model number
        name += f" ({model})"
    return name[:90]


def short_url(url: str | None) -> str:
    """An eBay item link without its tracking: https://www.ebay.com/itm/<id>."""
    m = re.search(r"/itm/(?:[^/?#]+/)?(\d{9,})", url or "")
    return f"https://www.ebay.com/itm/{m.group(1)}" if m else (url or "")


def search_links(res: dict, labels: bool = False) -> dict:
    """eBay sold / active searches a person can open (as legacy/comps.py):
    the product name, else the style code, else what was typed. labels: the
    reply's wording ("Sold on eBay")."""
    from urllib.parse import urlencode
    ident = res["identification"]
    # The product name, not the long search query (check #2's long query found nothing).
    q = " ".join(str(ident.get(k)) for k in ("brand", "product_name") if ident.get(k)) \
        or ident.get("model") or ident.get("style_code") or ident.get("search_query") \
        or res["request"].get("description")
    q = re.sub(r"\s+", " ", _ROSS_IN_TEXT.sub(" ", q or "")).strip()     # never search a Ross code
    if not q:
        return {}
    base = {"_nkw": q, "LH_ItemCondition": "1000|1500", "LH_PrefLoc": 1}
    sold = "https://www.ebay.com/sch/i.html?" + urlencode({**base, "LH_Sold": 1, "LH_Complete": 1})
    active = "https://www.ebay.com/sch/i.html?" + urlencode({**base, "_sop": 15})
    if labels:
        return {"Sold on eBay": sold, "For sale on eBay": active}
    return {"sold": sold, "for sale": active}


LEVEL_SHORT = {1: "sold", 2: "similar", 3: "ours", 4: "MSRP×ratio", 5: "ask ≤"}


def _pct(x: float) -> str:
    """75%; a loss: −5%."""
    n = round(x * 100)
    return f"−{-n}%" if n < 0 else f"{n}%"


def _whole(x) -> str:
    if x is None:
        return "-"
    n = round(x)
    return f"−${-n:,}" if n < 0 else f"${n:,}"


def ladder_line(pick: dict, res: dict) -> str:
    """Each level found, in plain words and whole dollars (item prices), e.g.
    "0 sold same · 1 sold same model $21 · our Juicy Couture bags ~$58 (12d)
    · MSRP×47% $51 · ask ≤$92"."""
    levels = pick.get("levels") or {}
    n1 = pick.get("sales") or 0
    if res.get("sold_searched") is False:
        # a quick check with nothing cached looked at no sales: never "0 sold"
        parts = ["sold not searched"]
    else:
        parts = [f"{n1} sold same" + (f" {_whole(levels[1]['value'])}" if 1 in levels else "")]
    if 2 in levels:
        incl = f" (incl. {levels[2]['same_item']} same)" if levels[2].get("same_item") else ""
        parts.append(f"{levels[2]['n']} sold same model{incl} {_whole(levels[2]['value'])}")
    if 3 in levels:
        l3 = levels[3]
        kind = {"bag": "bags", "watch": "watches", "other": "items"}.get(l3.get("bucket"), f"{l3.get('bucket')}s")
        days = f" ({l3['days']:.0f}d)" if l3.get("days") is not None else ""
        parts.append(f"our {l3.get('brand') or ''} {kind} ~{_whole(l3['value'])}{days}".replace("  ", " "))
    if 4 in levels:
        r = levels[4].get("ratio")
        other = " (other brands)" if levels[4].get("level") == "type" else ""
        parts.append((f"MSRP×{r:.0%} " if r else "MSRP×ratio ") + _whole(levels[4]["value"]) + other)
    if 5 in levels:
        parts.append(f"ask ≤{_whole(levels[5]['value'])}" + (" (cap)" if pick.get("capped") else ""))
    if res.get("demand"):
        parts.append(res["demand"])
    if pick.get("competition_note"):
        parts.append(pick["competition_note"])
    return " · ".join(parts)


def format_reply(res: dict, *, check_id: int | None = None, prefix: str = "", footer: str = "",
                 html: bool = False) -> str:
    """Verdict + why / item / cost · sell · profit / Best Offer prices /
    evidence / up to 2 best matches / eBay sold and for-sale searches / check
    number, time, mode. The rest: format_details. html: Telegram HTML with
    short link labels (the bot); plain text shows the URLs."""
    req, money, pick = res["request"], res["money"], res["pick"]
    v = res["verdict"]
    sales = pick.get("sales") or 0
    if req.get("ross_price") is None:
        why = "Ross price missing"
    else:
        why = f"{_sales_phrase(sales)}, {_listings_phrase(res)}"
    est = " (estimated)" if pick.get("estimated") and v != NO_DATA else ""
    from pipeline import owned
    have = res.get("already_have") if isinstance(res.get("already_have"), dict) else None
    # "Do I already have this?" first (already HTML-escaped when html)
    top = owned.reply_lines(have, html=html)
    if top and prefix:
        top[0] = prefix + top[0]
        prefix = ""
    lines = [f"{prefix}{ICON[v]} {LABEL.get(v, v)}{est}: {why}", _item_name(res)]
    ross = req.get("ross_price")
    if money:
        asks, est = pick.get("from_asks"), pick.get("estimated")
        approx, tag = ("~", " (asking)") if asks else ("~", " (est.)") if est else ("", "")
        rng = pick.get("range")
        if rng:
            tag += f", range {_usd(rng[0])}–{_usd(rng[1])}"
        roi = f", ROI {_pct(money['roi'])}" if money.get("roi") is not None and not (asks or est) else ""
        lines.append(f"Ross {_usd(ross)} · sell {approx}{_usd(money['item_price'])}{tag} · "
                     f"profit {approx}{_usd(money['profit'])}{roi}")
    elif ross is not None:
        lines.append(f"Ross {_usd(ross)} · no price found")
    else:
        lines.append("Add the Ross price as the last number, e.g. /check coach tabby 26 49.99")
    lines.append(max_buy_line(res))
    if res.get("no_search"):
        lines.append("Only a Ross code, not one of ours: send a photo or describe the item to search eBay")
    offer = res.get("offer")
    if money and offer:
        lines.append(offer_line(offer))
    match = CERTAINTY.get(res.get("confidence"), "-")
    lines.append("Evidence: " + ladder_line(pick, res) + f" · match {match}")
    sup = supply_line(res)
    if sup:
        lines.append(sup)
    t = res["timings"].get("total_s", 0)
    mode = ("full" if req.get("full") else "quick") + " · " + ("exact" if req.get("exact", True) else "any")
    last = " · ".join(x for x in (f"#{check_id}" if check_id else "", f"⏱ {t:.0f}s", mode, footer) if x)
    links = [best_links(res), [("🔎", k, u) for k, u in search_links(res, labels=True).items()]]
    if not html:
        return "\n".join(top + lines + [" · ".join(f"{i} {k} {u}" for i, k, u in row) for row in links if row]
                         + [last])
    from html import escape
    linked = [" · ".join(f'{i} <a href="{escape(u)}">{escape(k)}</a>' for i, k, u in row) for row in links if row]
    return "\n".join(top + [escape(x) for x in lines] + linked + [escape(last)])


def max_buy_line(res: dict) -> str:
    """"Max buy: $21.40 ($15 profit at $49.99)", on every reply."""
    mb, price = res.get("max_buy"), (res.get("pick") or {}).get("price")
    need = f"${config.CHECK_BUY_MIN_PROFIT:.0f} profit"
    if mb is None or not price:
        return "Max buy: - (no sell price found)"
    if mb <= 0:
        return f"Max buy: none (no {need} even at $0 when it sells at {_usd(price)})"
    est = "~" if (res.get("pick") or {}).get("estimated") else ""
    return f"Max buy: {est}{_usd(mb)} ({need} at {est}{_usd(price)})"


def offer_line(offer: dict) -> str:
    """"List $49.99 BO (auto-accept $41.99, decline < $35.99) or $39.99 fixed"."""
    return (f"List {_usd(offer['list'])} BO (auto-accept {_usd(offer['accept'])}, "
            f"decline < {_usd(offer['decline'])}) or {_usd(offer['fixed'])} fixed")


COLOURS = ("black", "white", "pink", "blush", "red", "blue", "navy", "green", "olive", "yellow", "orange",
           "purple", "lilac", "brown", "tan", "beige", "cream", "ivory", "grey", "gray", "silver", "gold",
           "teal", "burgundy", "coral", "camel", "khaki", "denim", "leopard", "paisley", "floral", "plaid")


def _comp_tag(c: dict, ident: dict) -> str:
    """What sets a comp apart, for its link: a colour our item doesn't have
    ("pink"), else how the judge matched it; "dropship?" when flagged."""
    if c.get("dropship"):
        return "dropship?"
    ours = " ".join(str(ident.get(k) or "") for k in ("color", "product_name", "search_query")).lower()
    for w in re.findall(r"[a-z]+", (c.get("title") or "").lower()):
        if w in COLOURS and w not in ours:
            return w
    return VERDICT_WORDS.get(c.get("verdict"), c.get("verdict") or "")


def best_links(res: dict, n: int = 2) -> list[tuple[str, str, str]]:
    """Up to n best matches to tap: sold first (same item, then same model,
    newest first), then identical listings (real sellers before dropship-style
    ones, cheapest first)."""
    rows = [c for c in res.get("comp_list") or [] if c.get("url")]
    sold = sorted((c for c in rows if c["status"] == "sold"), key=lambda c: c.get("verdict") != "same_item")
    active = sorted((c for c in rows if c["status"] != "sold"), key=lambda c: bool(c.get("dropship")))
    out = []
    for c in (sold + active)[:n]:
        tag = _comp_tag(c, res.get("identification") or {})
        what = "Sold" if c["status"] == "sold" else "For sale"
        out.append(("🔗", f"{what} {_usd(c.get('total'))}" + (f" ({tag})" if tag else ""), short_url(c["url"])))
    return out


COMP_LINES = 12
VERDICT_WORDS = {"same_item": "same item", "variant": "same model"}


def comp_line(c: dict, now: datetime | None = None) -> str:
    """"sold $30.49 · 2026-10-12 · same model · <title> <link>"; listings for
    sale add their age and units sold, and a dropship flag."""
    bits = [f"{'sold' if c.get('status') == 'sold' else 'for sale'} {_usd(c.get('total'))}"]
    if c.get("status") == "sold":
        if c.get("date"):
            bits.append(str(c["date"])[:10])
    else:
        age = _age_days(c.get("listed"), now)
        if age is not None:
            bits.append(f"listed {age}d" + (f", {c['units_sold']} sold" if c.get("units_sold") is not None else ""))
    bits.append(VERDICT_WORDS.get(c.get("verdict"), c.get("verdict") or "-"))
    if c.get("dropship"):
        bits.append("⚠️ dropship? " + ", ".join(c.get("signals") or [])
                    + (" (left out)" if c.get("status") == "sold" else " (ceiling only)"))
    return " · ".join(bits) + f" · {(c.get('title') or '')[:50]} {short_url(c.get('url'))}".rstrip()


def format_details(res: dict, *, check_id: int | None = None, sku: str | None = None) -> str:
    """Everything the short reply leaves out (the Details button)."""
    req, money, pick = res["request"], res["money"], res["pick"]
    ross = req.get("ross_price")
    out = [f"Details{f' for check #{check_id}' if check_id else ''}: {_item_name(res)}"]
    if isinstance(res.get("already_have"), dict):
        from pipeline import owned
        out += owned.details_lines(res["already_have"])
    b = res.get("borrowed")
    if b:
        ref = {"item": f"item {str(b['id'])[:8]}", "piece": f"piece {b['id']}", "check": f"check #{b['id']}"}
        out.append(f"Searched as our {ref.get(b['source'], b['source'])} ({b['match']} match): "
                   f"{b.get('query') or '-'}")
    if res.get("no_search"):
        out.append("No eBay search: only a Ross code, and it isn't one of ours. Ross codes are never searched.")
    if ross is not None:
        main = req.get("ross_sku_main")
        out.append(f"Ross {_usd(ross)}" + (f" · SKU {sku}" if sku else "")
                   + (f" (main tag {main})" if main and main != sku else "")
                   + f" → cost {_usd(round(ross * (1 + config.SALES_TAX_RATE), 2))} with "
                     f"{config.SALES_TAX_RATE:.0%} tax")
    levels = pick.get("levels") or {}
    if levels or pick.get("used") is not None:
        out.append("Evidence ladder (item prices, before shipping):")
        for k in (1, 2, 3, 4, 5):
            mark = "✓ used" if k in (pick.get("used") or []) else ("cap" if k == 5 and pick.get("capped") else "")
            if k in levels:
                out.append(f"  L{k} {LEVEL_NAMES[k]}: {_usd(levels[k]['value'])} ({levels[k]['detail']}) {mark}".rstrip())
            else:
                out.append(f"  L{k} {LEVEL_NAMES[k]}: none")
        if pick.get("range"):
            out.append(f"  levels differ by more than {LADDER_SPREAD - 1:.0%}: range "
                       f"{_usd(pick['range'][0])}–{_usd(pick['range'][1])}")
    if money:
        out.append(f"Sell at {_usd(money['item_price'])} + {_usd(money['ship_charged'])} shipping "
                   f"(buyer pays {_usd(money['total'])}), price from {pick['basis']}")
        if pick.get("ask_discount"):
            out.append(f"  asking prices only: ×{pick['ask_discount']} (our sold/ask gap)"
                       + (f" and −{config.UNDERCUT_V2_PCT:.0%} undercut" if "cheapest" in pick['basis'] else ""))
        out.append(f"Fees + ads {_usd(money['fees'])} ({config.EBAY_FVF_PCT:.1%} + {_ad_rate():.1%} ad rate of what "
                   f"the buyer pays + {_usd(config.EBAY_FIXED_FEE)}) · "
                   f"label {_usd(money['label'])} ({res.get('item_type')}"
                   + (f", {res['label_size']}" if res.get("label_size") else "") + f") · you keep {_usd(money['keep'])}")
        roi = _pct(money["roi"]) if money.get("roi") is not None else "-"
        out.append(f"Profit {_usd(money['profit'])} · ROI {roi} · break-even {_usd(money['break_even_item'])} "
                   f"+ shipping")
        if res.get("offer"):
            o = res["offer"]
            out.append(offer_line(o) + f" (×{config.CHECK_BO_LIST} / ×{config.CHECK_BO_ACCEPT} / "
                                       f"×{config.CHECK_BO_DECLINE} of the estimate)")
    out.append(max_buy_line(res))
    sup = supply_line(res)
    if sup:
        out.append(sup.replace("⚠️ ", ""))
        out.append(f"  high supply = over {config.CHECK_SUPPLY_HIGH_MONTHS:g} months of listings at the"
                   f" {res['supply']['days']}-day sales rate"
                   + ("; the verdict is one step lower" if res["supply"].get("high") else ""))
        if pick.get("low_end") is not None:
            out.append(f"  high supply: priced at the lower quartile of the sales, {_usd(pick['low_end'])}")
    if pick.get("competition_note"):
        out.append(f"Competition: {pick.get('competitors', 0)} identical listing(s) besides dropship-style ones"
                   f" → {pick['competition_note']}" + (", priced at the upper end" if pick.get("upper_end") else ""))
    if res.get("cheapest_total"):
        at = res.get("at_cheapest") or {}
        out.append(f"Cheapest identical for sale: {_usd(res['cheapest_total'])} total"
                   + (f" → profit there {_usd(at.get('profit'))}" if at else ""))
    if pick.get("sold_median"):
        out.append(f"Sold median: {_usd(pick['sold_median'])} total")
    for r in res.get("reasons") or []:
        out.append(f"• {r}")
    rows = res.get("comp_list")
    if rows is None:            # an older saved result
        rows = [{"status": c.get("source"), **c} for c in res.get("comps") or []]
    if rows:
        out.append("Comps used (totals with shipping; tap to open):")
    for c in rows[:COMP_LINES]:
        out.append("  " + comp_line(c))
    if len(rows) > COMP_LINES:
        out.append(f"  … and {len(rows) - COMP_LINES} more")
    if res.get("demoted"):
        out.append(f"  {res['demoted']} other style(s) left out: the judge said same model, but the title has "
                   f"neither our style code nor our product name")
    if pick.get("dropship_excluded"):
        out.append(f"  {pick['dropship_excluded']} dropship-style sale(s) left out of the price")
    links = search_links(res)
    if links:
        out.append("eBay: " + " · ".join(f"{k} {u}" for k, u in links.items()))
    t = res["timings"]
    parts = [f"{k[:-2]} {v:.0f}s" for k, v in t.items() if k.endswith("_s") and k != "total_s"]
    model = res.get("judge_model") or "-"
    out.append(f"Time: {' · '.join(parts) or '-'} · model {model}{' (escalated)' if res.get('escalated') else ''}")
    if not req.get("full") and res.get("sold_cache_misses"):
        out.append("Quick check: no recent sold data cached; Full check searches sales.")
    if res.get("warnings"):
        out.append(f"Warnings: {str(res['warnings'])[:300]}")
    return "\n".join(out)


# --- timings log and daily cap ----------------------------------------------------------

def log_timings(res: dict, user_id: int | None = None, path: Path | None = None,
                counted: bool = True) -> None:
    """One line per run. counted=False: a quick run that found NO_DATA and went
    straight on to the full check; the pair counts once toward the cap."""
    path = Path(path or TIMINGS_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "user": user_id,
           "mode": "full" if res["request"].get("full") else "quick",
           "auto_full": bool(res["request"].get("auto_full")), "counted": counted,
           "exact": bool(res["request"].get("exact", True)), "input": "photo" if res["photo"] else "text",
           "verdict": res["verdict"], "escalated": bool(res.get("escalated")),
           "judge_verdicts": res.get("verdicts") or {}, **res["timings"]}
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row) + "\n")


def checks_today(path: Path | None = None, now: datetime | None = None) -> int:
    """Checks logged since midnight US Eastern (the daily cap)."""
    path = Path(path or TIMINGS_FILE)
    if not path.exists():
        return 0
    now = (now or datetime.now(timezone.utc)).astimezone(EASTERN)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    n = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
            ts = datetime.fromisoformat(row["ts"])
        except (ValueError, KeyError, TypeError):
            continue
        if ts.astimezone(EASTERN) >= start and row.get("counted", True):
            n += 1
    return n
