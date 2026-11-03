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
            from_asks: bool = False, disagrees: bool = False) -> tuple[str, list[str]]:
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


def choose_price(pricing: dict, exact: bool) -> dict:
    """Which price, which sales count, from the pricing result (design §3)."""
    judged = pricing.get("judged_comps") or []
    ship = config.EBAY_SHIP_CHARGED
    if exact:
        price = pricing.get("formula_price")
        sold = sorted(c["total"] for c in judged if c.get("source") == "sold"
                      and c.get("verdict") == "same_item" and c.get("total"))
        out = {"price": price, "basis": "same-item sales", "sales": len(sold), "from_asks": False,
               "sold_median": pricing.get("sold_median"), "disagrees": False, "ask_discount": None}
        if price is None and pricing.get("raw_active_floor"):
            # Asking prices only: the cheapest identical ask, less the measured
            # ask-to-sold gap (CHECK_ASK_DISCOUNT) and our usual undercut.
            item = (pricing["raw_active_floor"] * config.CHECK_ASK_DISCOUNT
                    * (1 - config.UNDERCUT_V2_PCT) - ship)
            if item > 0:
                out.update(price=_charm(item), basis="cheapest identical listing (asking price)",
                           from_asks=True, ask_discount=config.CHECK_ASK_DISCOUNT)
        return out
    sold = sorted(c["total"] for c in judged if c.get("source") == "sold"
                  and c.get("verdict") in ("same_item", "variant") and c.get("total"))
    flags = pricing.get("review_flags") or []
    from_asks = "priced_from_asks" in flags
    price = pricing.get("suggested_price")
    if from_asks and price:
        price = _charm(price * config.CHECK_ASK_DISCOUNT)
    return {"price": price, "basis": "Claude's estimate (same item + variants)"
                                     + (" from asking prices" if from_asks else ""),
            "sales": len(sold), "from_asks": from_asks,
            "sold_median": round(statistics.median(sold), 2) if sold else None,
            "disagrees": "formula_disagrees" in flags,
            "ask_discount": config.CHECK_ASK_DISCOUNT if from_asks else None}


def evaluate(pricing: dict, ident: dict, ross_price: float | None, exact: bool,
             text: str = "", quick: bool = False) -> dict:
    """Price choice, money and verdict from a pricing result. Shared by
    run_check and the backtest (scripts/backtest_check.py), so the backtest
    scores exactly what /check would say. quick: a quick check that found no
    data at all is NO_DATA instead of SKIP."""
    pick = choose_price(pricing, exact)
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
                         from_asks=pick["from_asks"], disagrees=pick["disagrees"])
        if money is None:
            # No price at all is not evidence against the item (Person B,
            # 2026-11-02: in the backtest 14 of 20 such SKIPs were winners).
            # SKIP is only given when real prices back it.
            v, why = NO_DATA, (["the quick check found no recent sales and no identical listing"] if quick
                               else ["no sales and no identical listing found: not enough data for a verdict"])
    return {"item_type": kind, "label": label, "verdict": v, "reasons": why, "pick": pick,
            "money": money, "cheapest_total": cheapest, "at_cheapest": at_cheapest}


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
        active_max_age_s=0)
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
            "judge_model": pricing.get("judge_model"), "escalated": pricing.get("judge_escalated")}


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


def photo_roles(paths: list[str], decode=None) -> dict:
    """Which photo is the Ross tag: the first whose barcode reads as a Ross
    code (12-digit SKU + price in cents). No AI. Returns the SKU, the price,
    the tag photo and the photos for identification (the tag too when it is
    the only photo)."""
    if decode is None:
        import ross_tag

        def decode(p):
            return ross_tag.parse_barcode(ross_tag.decode_barcode(p))
    tag, sku, price = None, None, None
    for p in paths:
        try:
            parsed = decode(p) or {}
        except Exception:
            parsed = {}
        code = parsed.get("code")
        if code and len(str(code)) == 12:
            tag, sku, price = p, str(code), parsed.get("reduced_price")
            break
    others = [p for p in paths if p != tag]
    return {"tag": tag, "sku": sku, "price": price, "identify": others or ([tag] if tag else []),
            "scan": list(paths)}


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
    q = ident.get("search_query") or " ".join(
        str(ident.get(k)) for k in ("brand", "product_name") if ident.get(k)) or res["request"].get("description")
    if not q:
        return {}
    base = {"_nkw": q, "LH_ItemCondition": "1000|1500", "LH_PrefLoc": 1}
    return {"sold": "https://www.ebay.com/sch/i.html?" + urlencode({**base, "LH_Sold": 1, "LH_Complete": 1}),
            "for sale": "https://www.ebay.com/sch/i.html?" + urlencode({**base, "_sop": 15})}


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
        asks = pick.get("from_asks")
        approx, tag = ("~", " (asking)") if asks else ("", "")
        roi = f", ROI {money['roi']:.0%}" if money.get("roi") is not None and not asks else ""
        lines.append(f"Ross {_usd(ross)} · sell {approx}{_usd(money['item_price'])}{tag} · "
                     f"profit {approx}{_usd(money['profit'])}{tag}{roi}")
    elif ross is not None:
        lines.append(f"Ross {_usd(ross)} · no price found")
    else:
        lines.append("Add the Ross price as the last number, e.g. /check coach tabby 26 49.99")
    cheapest = f" (cheapest {_usd(res['cheapest_total'])})" if res.get("cheapest_total") else ""
    match = CERTAINTY.get(res.get("confidence"), "-")
    lines.append(f"Evidence: sold {sales} · for sale {res.get('same_item_active') or 0} identical"
                 f"{cheapest} · match {match}")
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
           "verdict": res["verdict"], "escalated": bool(res.get("escalated")), **res["timings"]}
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
