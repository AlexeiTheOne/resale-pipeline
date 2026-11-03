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
                   escalation. Aim: ~20 s.
  full             the full pricing run: live sold queries, escalation on;
                   cached sold rows still only if under CHECK_SOLD_CACHE_DAYS.
  exact (default)  price, sales and "cheapest identical" from the judge's
                   same_item comps; the price is the formula over same-item
                   sales, never Claude's estimate.
  any              Claude's suggested price, sales = same_item + variant.
"""
import json
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


def label_for(ident: dict, text: str = "") -> tuple[str, float]:
    kind = item_type_of(ident, text)
    costs = label_costs()
    return kind, costs.get(kind, costs["other"])


# --- money (design §4; all rates from config) -------------------------------------

def _fee_rate() -> float:
    return config.EBAY_FVF_PCT + config.EBAY_AD_FEE_PCT


def at_total(total: float, ross_price: float, label: float) -> dict:
    """What one unit nets when the buyer pays `total` (item + shipping)."""
    fees = total * _fee_rate() + config.EBAY_FIXED_FEE
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


# --- verdict (design §5) ------------------------------------------------------------

def verdict(*, profit: float | None, roi: float | None, sales: int,
            profit_at_cheapest: float | None, low_confidence: bool = False,
            from_asks: bool = False, disagrees: bool = False,
            estimated: bool = False) -> tuple[str, list[str]]:
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
    if profit_at_cheapest is not None and profit_at_cheapest < 0 and sales < config.CHECK_MIN_SALES:
        return "SKIP", [f"a loss at the cheapest identical listing (${profit_at_cheapest})"] + evidence
    if profit >= config.CHECK_MIN_PROFIT:
        why = []
        if profit < config.CHECK_BUY_MIN_PROFIT:
            why.append(f"profit under ${config.CHECK_BUY_MIN_PROFIT:.0f}")
        if (roi or 0) < config.CHECK_BUY_MIN_ROI:
            why.append(f"ROI under {config.CHECK_BUY_MIN_ROI:.0%}")
        if profit_at_cheapest is not None and profit_at_cheapest < config.CHECK_MIN_PROFIT_AT_CHEAPEST:
            why.append(f"only ${profit_at_cheapest} at the cheapest identical listing")
        return "BORDERLINE", why + evidence
    return "SKIP", [f"profit ${profit} is under ${config.CHECK_MIN_PROFIT:.0f}"] + evidence


# --- the check ------------------------------------------------------------------------

def _charm(x: float) -> float:
    return round(max(round(x) - 0.01, 0.99), 2)


def similar_queries(ident: dict) -> list[tuple[str, str]]:
    """Extra sold searches for a full check: the brand's UPC, then similar
    items (brand + item type, e.g. "Juicy Couture Weekender Tote Bag")."""
    out = []
    upc = re.sub(r"\D", "", str(ident.get("upc") or ""))
    if len(upc) in (12, 13, 14):
        out.append(("upc", upc))
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
                 receipt: dict | None = None, exclude_item_id: str | None = None) -> dict:
    """The evidence ladder (Person B, 2026-11-02). Every level is an ITEM price
    (eBay totals minus the shipping we charge):
      1 eBay sold, same item: 2+ sales -> their median, done.
      2 eBay sold, same model other colour/size (the judge's variants), 1+.
      3 our own sales, same brand + type (median sold price).
      4 MSRP x brand ratio (our sold/MSRP; type bucket when the brand has
        too few sales; never the all-sales fallback).
      5 eBay asking prices x CHECK_ASK_DISCOUNT: a ceiling, never the price.
    No level 1: the median of levels 2-4. Capped at level 5 (so never above
    the cheapest identical listing). Any mode counts variants as level 1."""
    from pipeline import msrp
    ident = ident or {}
    judged = pricing.get("judged_comps") or []
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
    if variant:
        levels[2] = {"value": round(_median(variant) - ship, 2), "n": len(variant),
                     "detail": f"median of {len(variant)} sale(s) ({_median(variant):.2f} total)"}
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
                     "detail": f"MSRP ${est['msrp']:.2f} × {est['ratio']} ({est['level']} {est['key']}, "
                               f"{est.get('n')} sales)"}
    cheapest = pricing.get("raw_active_floor")
    if cheapest:
        levels[5] = {"value": round(cheapest * config.CHECK_ASK_DISCOUNT - ship, 2), "n": None,
                     "detail": f"cheapest identical ${cheapest:.2f} total × {config.CHECK_ASK_DISCOUNT}"}
    used = [1] if 1 in levels else [k for k in (2, 3, 4) if k in levels]
    raw = levels[1]["value"] if 1 in levels else _median([levels[k]["value"] for k in used])
    capped = False
    if raw is not None and 5 in levels and levels[5]["value"] < raw:
        raw, capped = levels[5]["value"], True
    price = _charm(raw) if raw and raw > 0 else None
    vals = [levels[k]["value"] for k in used]
    spread = (min(vals), max(vals)) if len(vals) > 1 and min(vals) > 0 \
        and max(vals) / min(vals) > LADDER_SPREAD else None
    if 1 in used:
        basis = "eBay sales of the same item (level 1)"
    elif used:
        basis = "median of levels " + ", ".join(str(k) for k in used) + " (no same-item sales)"
    else:
        basis = "no level 1-4 evidence"
    return {"price": price, "basis": basis + (", capped at the asking-price ceiling" if capped else ""),
            "levels": levels, "used": used, "capped": capped, "range": spread,
            "sales": len(same), "from_asks": False, "estimated": bool(used) and 1 not in used,
            "sold_median": _median(same), "disagrees": False, "ask_discount": None}


def evaluate(pricing: dict, ident: dict, ross_price: float | None, exact: bool,
             text: str = "", quick: bool = False, history: list | None = None,
             exclude_item_id: str | None = None) -> dict:
    """Price choice, money and verdict from a pricing result. Shared by
    run_check and the backtest (scripts/backtest_check.py), so the backtest
    scores exactly what /check would say. quick: a quick check that found no
    data at all is NO_DATA instead of SKIP."""
    pick = choose_price(pricing, exact, ident, history=history, exclude_item_id=exclude_item_id)
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
                         estimated=pick.get("estimated", False))
        if money is None:
            # No price at all is not evidence against the item (Person B,
            # 2026-11-02: in the backtest 14 of 20 such SKIPs were winners).
            # SKIP is only given when real prices back it.
            v, why = NO_DATA, (["the quick check found no recent sales and no identical listing"] if quick
                               else ["no sales and no identical listing found: not enough data for a verdict"])
    return {"item_type": kind, "label": label, "verdict": v, "reasons": why, "pick": pick,
            "money": money, "cheapest_total": cheapest, "at_cheapest": at_cheapest,
            "similar": similar_sold(pricing)}


def _norm_code(v) -> str:
    return re.sub(r"\W", "", str(v or "")).lower()


def stock_keys(*, upc=None, model=None, brand=None, sku=None) -> set:
    """What makes two things the same product for the in-stock warning: the
    UPC, a style/model code (it looks like one: digits and letters), brand +
    model, or the Ross SKU (Ross reuses SKUs now and then, so that one says
    "similar", see stages.same_item_keys)."""
    from pipeline import gtin as gtin_mod
    keys = set()
    gtin = gtin_mod.product_gtin(upc)
    if gtin:
        keys.add(("UPC", gtin))
    code = _norm_code(model)
    if len(code) >= 5 and _looks_like_code(code):
        keys.add(("code", code))
    b = _norm_code(brand)
    if b and len(code) >= 4:
        keys.add(("model", f"{b}:{code}"))
    if sku and len(str(sku)) == 12:
        keys.add(("Ross SKU", str(sku)))
    return keys


def already_have(ident: dict, sku: str | None = None) -> list[str]:
    """Items and pieces (home inventory) that look like the same product: an
    in-stock warning before buying another (read only)."""
    import db
    mine = stock_keys(upc=ident.get("upc"), model=ident.get("model") or ident.get("style_code"),
                      brand=ident.get("brand"), sku=sku)
    if not mine:
        return []
    out = []
    for p in db.list_pieces():
        if p.get("status") in ("sold", "donated"):
            continue
        info = p.get("info") or {}
        theirs = stock_keys(upc=info.get("upc"), model=info.get("style_code") or info.get("model"),
                            brand=info.get("brand"), sku=info.get("ross_code"))
        if mine & theirs:
            what = " ".join(str(info[k]) for k in ("brand", "item_type") if info.get(k))
            out.append(f"piece {p['code']}" + (f" ({what}, {p.get('status')})" if what else ""))
    for it in db.list_items():
        if it.get("status") in ("rejected", "failed"):
            continue
        i = it.get("identification") or {}
        theirs = stock_keys(upc=i.get("upc"), model=i.get("model") or i.get("style_code"),
                            brand=i.get("brand"), sku=(it.get("receipt") or {}).get("code"))
        if mine & theirs:
            out.append(f"item {it['item_id'][:8]} ({it.get('status')})")
    return out


def run_check(req: dict, *, photo_path: str | None = None, pricing_fn=None, identify_fn=None,
              have_fn=None) -> dict:
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
    ev = evaluate(pricing, ident, req.get("ross_price"), bool(req.get("exact", True)), text,
                  quick=not full)
    try:
        have = have_fn(ident, req.get("ross_sku"))
    except Exception:            # advisory only
        have = []
    timings["total_s"] = round(time.perf_counter() - t0, 2)
    return {"request": dict(req), "photo": bool(photo_path), "identification": ident,
            **ev, "already_have": have, "timings": timings,
            "confidence": pricing.get("claude_confidence"),
            "sold_cache_misses": pricing.get("sold_cache_misses") or [],
            # up to 5 identical comps, saved with the check (comps_summary)
            "comps": [{k: c.get(k) for k in ("source", "verdict", "title", "total", "url")}
                      for c in (pricing.get("judged_comps") or [])
                      if c.get("verdict") in ("same_item", "variant")][:5],
            "same_item_active": pricing.get("same_item_active_count"),
            "reference": pricing.get("reference"), "warnings": pricing.get("comp_warning"),
            "judge_model": pricing.get("judge_model"), "escalated": pricing.get("judge_escalated"),
            # how the judge labelled every candidate (logged: quick vs full agreement)
            "verdicts": dict(Counter(c.get("verdict") or "unjudged" for c in pricing.get("judged_comps") or []))}


def check_fields(res: dict) -> dict:
    """A result as checks-table columns (db.update_check)."""
    money, pick, req = res.get("money") or {}, res["pick"], res["request"]
    return {"identification": res["identification"], "mode": "full" if req.get("full") else "quick",
            "exact": bool(req.get("exact", True)), "verdict": res["verdict"],
            "suggested_price": pick.get("price"), "profit": money.get("profit"), "roi": money.get("roi"),
            "break_even": money.get("break_even_item"), "cheapest_identical": res.get("cheapest_total"),
            "sold_count": pick.get("sales"),
            "comps_summary": {"basis": pick.get("basis"), "sales": pick.get("sales"),
                              "sold_median": pick.get("sold_median"), "item_type": res.get("item_type"),
                              "label": res.get("label"), "reasons": res.get("reasons"),
                              "comps": res.get("comps") or [],
                              "warnings": (res.get("warnings") or "")[:500] or None}}


# Barcode angles to try, most likely first: a tag photographed sideways (90/270),
# upside down, or a markdown sticker stuck on at a slant. Check #2's REDUCED
# sticker read only at 30-45 degrees.
BARCODE_ANGLES = (0, 90, 270, 180, 30, -30, 45, -45, 15, -15, 60, -60)
BARCODE_BUDGET_S = 6.0


def read_ross_barcode(path: str, budget_s: float = BARCODE_BUDGET_S) -> dict:
    """{"code", "reduced_price"} from a Ross barcode anywhere in the photo, or
    {}. Plain decode first (ross_tag.decode_barcode), then rotations of the
    whole photo, then at double size, until the time budget runs out. Local,
    no AI."""
    import ross_tag
    digits = ross_tag.decode_barcode(path)
    if not digits:
        try:
            from PIL import Image, ImageOps
            img = ImageOps.grayscale(ImageOps.exif_transpose(Image.open(path)))
        except Exception:
            return {}
        deadline = time.perf_counter() + budget_s
        for scale in (1, 2):
            base = img if scale == 1 else img.resize((img.size[0] * 2, img.size[1] * 2))
            for angle in BARCODE_ANGLES:
                if time.perf_counter() > deadline:
                    break
                try:
                    found = ross_tag._zbar_decode(base.rotate(angle, expand=True, fillcolor=255),
                                                  symbols=ross_tag._TAG_ZBAR_SYMBOLS)
                except Exception:
                    continue
                for res in found:
                    d = re.sub(r"\D", "", res.data.decode("utf-8", "ignore"))
                    if len(d) >= 12:
                        digits = d
                        break
                if digits:
                    break
            if digits:
                break
    parsed = ross_tag.parse_barcode(digits) if digits else {}
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
    tag, sku, price, source = None, None, None, None
    for p in paths:
        try:
            parsed = decode(p) or {}
        except Exception:
            parsed = {}
        code = parsed.get("code")
        if code and len(str(code)) == 12:
            tag, sku, price, source = p, str(code), parsed.get("reduced_price"), "barcode"
            break
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
    return {"tag": tag, "sku": sku, "price": price, "price_source": source,
            "identify": others or ([tag] if tag else []), "scan": list(paths)}


# --- reply -------------------------------------------------------------------------------

def _usd(x) -> str:
    return "-" if x is None else f"${x:,.2f}"


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
    name = " ".join(str(ident.get(k)) for k in ("brand", "product_name") if ident.get(k)) \
        or req.get("description") or "item"
    model = ident.get("model")
    if model and _norm_code(model) not in _norm_code(name):    # no duplicated model number
        name += f" ({model})"
    return name[:90]


def short_url(url: str | None) -> str:
    """An eBay item link without its tracking: https://www.ebay.com/itm/<id>."""
    m = re.search(r"/itm/(?:[^/?#]+/)?(\d{9,})", url or "")
    return f"https://www.ebay.com/itm/{m.group(1)}" if m else (url or "")


def search_links(res: dict) -> dict:
    """eBay sold / active searches a person can open (as legacy/comps.py)."""
    from urllib.parse import urlencode
    ident = res["identification"]
    # The product name, not the long search query (check #2's long query found nothing).
    q = " ".join(str(ident.get(k)) for k in ("brand", "product_name") if ident.get(k)) \
        or ident.get("search_query") or res["request"].get("description")
    if not q:
        return {}
    base = {"_nkw": q, "LH_ItemCondition": "1000|1500", "LH_PrefLoc": 1}
    return {"sold": "https://www.ebay.com/sch/i.html?" + urlencode({**base, "LH_Sold": 1, "LH_Complete": 1}),
            "for sale": "https://www.ebay.com/sch/i.html?" + urlencode({**base, "_sop": 15})}


LEVEL_SHORT = {1: "sold", 2: "similar", 3: "ours", 4: "MSRP×ratio", 5: "ask ≤"}


def _whole(x) -> str:
    return "-" if x is None else f"${x:,.0f}"


def ladder_line(pick: dict, res: dict) -> str:
    """Each level found, in plain words and whole dollars (item prices), e.g.
    "0 sold same · 1 sold same model $21 · our Juicy Couture bags ~$58 (12d)
    · MSRP×47% $51 · ask ≤$92"."""
    levels = pick.get("levels") or {}
    n1 = pick.get("sales") or 0
    parts = [f"{n1} sold same" + (f" {_whole(levels[1]['value'])}" if 1 in levels else "")]
    if 2 in levels:
        parts.append(f"{levels[2]['n']} sold same model {_whole(levels[2]['value'])}")
    if 3 in levels:
        l3 = levels[3]
        kind = {"bag": "bags", "watch": "watches", "other": "items"}.get(l3.get("bucket"), f"{l3.get('bucket')}s")
        days = f" ({l3['days']:.0f}d)" if l3.get("days") is not None else ""
        parts.append(f"our {l3.get('brand') or ''} {kind} ~{_whole(l3['value'])}{days}".replace("  ", " "))
    if 4 in levels:
        r = levels[4].get("ratio")
        parts.append(f"MSRP×{r:.0%} {_whole(levels[4]['value'])}" if r else f"MSRP×ratio {_whole(levels[4]['value'])}")
    if 5 in levels:
        parts.append(f"ask ≤{_whole(levels[5]['value'])}" + (" (cap)" if pick.get("capped") else ""))
    return " · ".join(parts)


def format_reply(res: dict, *, check_id: int | None = None, prefix: str = "", footer: str = "") -> str:
    """At most 5 lines: verdict + why / item / cost · sell · profit /
    evidence / check number, time, mode. The rest: format_details."""
    req, money, pick = res["request"], res["money"], res["pick"]
    v = res["verdict"]
    sales = pick.get("sales") or 0
    if req.get("ross_price") is None:
        why = "Ross price missing"
    else:
        why = f"{_sales_phrase(sales)}, {_listings_phrase(res)}"
    lines = [f"{prefix}{ICON[v]} {LABEL.get(v, v)}: {why}"]
    line2 = _item_name(res)
    if res.get("already_have"):
        line2 += " · ⚠️ in stock: " + ", ".join(res["already_have"][:2])
    lines.append(line2)
    ross = req.get("ross_price")
    if money:
        asks, est = pick.get("from_asks"), pick.get("estimated")
        approx, tag = ("~", " (asking)") if asks else ("~", " (est.)") if est else ("", "")
        rng = pick.get("range")
        if rng:
            tag += f", range {_usd(rng[0])}–{_usd(rng[1])}"
        roi = f", ROI {money['roi']:.0%}" if money.get("roi") is not None and not (asks or est) else ""
        lines.append(f"Ross {_usd(ross)} · sell {approx}{_usd(money['item_price'])}{tag} · "
                     f"profit {approx}{_usd(money['profit'])}{roi}")
    elif ross is not None:
        lines.append(f"Ross {_usd(ross)} · no price found")
    else:
        lines.append("Add the Ross price as the last number, e.g. /check coach tabby 26 49.99")
    cheapest = f" (cheapest {_usd(res['cheapest_total'])})" if res.get("cheapest_total") else ""
    match = CERTAINTY.get(res.get("confidence"), "-")
    lines.append("Evidence: " + ladder_line(pick, res) + f" · match {match}")
    t = res["timings"].get("total_s", 0)
    mode = ("full" if req.get("full") else "quick") + " · " + ("exact" if req.get("exact", True) else "any")
    lines.append(" · ".join(x for x in (f"#{check_id}" if check_id else "", f"⏱ {t:.0f}s", mode, footer) if x))
    return "\n".join(lines)


def format_details(res: dict, *, check_id: int | None = None, sku: str | None = None) -> str:
    """Everything the short reply leaves out (the Details button)."""
    req, money, pick = res["request"], res["money"], res["pick"]
    ross = req.get("ross_price")
    out = [f"Details{f' for check #{check_id}' if check_id else ''}: {_item_name(res)}"]
    if ross is not None:
        out.append(f"Ross {_usd(ross)}" + (f" · SKU {sku}" if sku else "")
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
        out.append(f"Fees + ads {_usd(money['fees'])} ({_fee_rate():.1%} + ${config.EBAY_FIXED_FEE:.2f}) · "
                   f"label {_usd(money['label'])} ({res.get('item_type')}) · you keep {_usd(money['keep'])}")
        roi = f"{money['roi']:.0%}" if money.get("roi") is not None else "-"
        out.append(f"Profit {_usd(money['profit'])} · ROI {roi} · break-even {_usd(money['break_even_item'])} "
                   f"+ shipping")
    if res.get("cheapest_total"):
        at = res.get("at_cheapest") or {}
        out.append(f"Cheapest identical for sale: {_usd(res['cheapest_total'])} total"
                   + (f" → profit there {_usd(at.get('profit'))}" if at else ""))
    if pick.get("sold_median"):
        out.append(f"Sold median: {_usd(pick['sold_median'])} total")
    for r in res.get("reasons") or []:
        out.append(f"• {r}")
    for c in (res.get("comps") or [])[:3]:
        out.append(f"• {c.get('source')} {_usd(c.get('total'))} ({c.get('verdict')}): "
                   f"{(c.get('title') or '')[:60]} {short_url(c.get('url'))}".rstrip())
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
