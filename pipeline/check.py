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
ICON = {"BUY": "✅", "MAYBE": "🟡", "BORDERLINE": "🟠", "SKIP": "❌"}

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
        evidence.append(f"only {sales} matching sale(s) (need {config.CHECK_MIN_SALES})")
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
               "sold_median": pricing.get("sold_median"), "disagrees": False}
        if price is None and pricing.get("raw_active_floor"):
            item = pricing["raw_active_floor"] * (1 - config.UNDERCUT_V2_PCT) - ship
            if item > 0:
                out.update(price=_charm(item), basis="cheapest identical listing (asking price)",
                           from_asks=True)
        return out
    sold = sorted(c["total"] for c in judged if c.get("source") == "sold"
                  and c.get("verdict") in ("same_item", "variant") and c.get("total"))
    flags = pricing.get("review_flags") or []
    return {"price": pricing.get("suggested_price"), "basis": "Claude's estimate (same item + variants)",
            "sales": len(sold), "from_asks": "priced_from_asks" in flags,
            "sold_median": round(statistics.median(sold), 2) if sold else None,
            "disagrees": "formula_disagrees" in flags}


def already_have(ident: dict) -> list[str]:
    """Our items with the same UPC or brand + model (read only)."""
    from pipeline import stages
    import db
    mine = stages.same_item_keys({"identification": ident})
    if not mine:
        return []
    out = []
    for other in db.list_items():
        if other.get("status") in ("rejected", "failed"):
            continue
        if mine & stages.same_item_keys(other):
            out.append(f"{other['item_id'][:8]} ({other.get('status')})")
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
    if photo_path and identify_fn is not None:
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
    pick = choose_price(pricing, bool(req.get("exact", True)))
    kind, label = label_for(ident, text)
    ross = req.get("ross_price")
    money = economics(pick["price"], ross, label) if pick["price"] and ross else None
    cheapest = pricing.get("raw_active_floor")
    at_cheapest = at_total(cheapest, ross, label) if cheapest and ross else None
    if ross is None:
        v, why = "SKIP", ["no Ross price given"]
    else:
        v, why = verdict(profit=money["profit"] if money else None, roi=money["roi"] if money else None,
                         sales=pick["sales"],
                         profit_at_cheapest=at_cheapest["profit"] if at_cheapest else None,
                         low_confidence=pricing.get("claude_confidence") == "low",
                         from_asks=pick["from_asks"], disagrees=pick["disagrees"])
    try:
        have = have_fn(ident)
    except Exception:            # advisory only
        have = []
    timings["total_s"] = round(time.perf_counter() - t0, 2)
    return {"request": dict(req), "photo": bool(photo_path), "identification": ident,
            "item_type": kind, "label": label, "verdict": v, "reasons": why, "pick": pick,
            "money": money, "cheapest_total": cheapest, "at_cheapest": at_cheapest,
            "already_have": have, "timings": timings,
            "sold_cache_misses": pricing.get("sold_cache_misses") or [],
            "same_item_active": pricing.get("same_item_active_count"),
            "reference": pricing.get("reference"), "warnings": pricing.get("comp_warning"),
            "judge_model": pricing.get("judge_model"), "escalated": pricing.get("judge_escalated")}


# --- reply -------------------------------------------------------------------------------

def _usd(x) -> str:
    return "-" if x is None else f"${x:,.2f}"


def format_reply(res: dict) -> str:
    req, money, pick = res["request"], res["money"], res["pick"]
    ident = res["identification"]
    lines = []
    head = f"{ICON[res['verdict']]} {res['verdict']}"
    if money:
        roi = f"{money['roi']:.0%}" if money["roi"] is not None else "-"
        head += f": profit {_usd(money['profit'])} (ROI {roi})"
    lines.append(head)
    name = " ".join(str(ident.get(k)) for k in ("brand", "product_name") if ident.get(k)) \
        or req.get("description") or "item"
    lines.append(name[:120] + (f" ({ident['model']})" if ident.get("model") else ""))
    mode = ("full" if req.get("full") else "quick") + " · " + ("exact matches" if req.get("exact", True) else "any matches")
    lines.append(f"Mode: {mode}")
    if req.get("ross_price") is None:
        lines.append("Ross price missing: add it as the last number, e.g. /check coach tabby 26 49.99")
    else:
        lines.append(f"Ross {_usd(req['ross_price'])} → cost {_usd(round(req['ross_price'] * (1 + config.SALES_TAX_RATE), 2))} "
                     f"with {config.SALES_TAX_RATE:.0%} tax")
    if money:
        lines.append(f"Sell at {_usd(money['item_price'])} + {_usd(money['ship_charged'])} shipping "
                     f"(buyer pays {_usd(money['total'])}); price from {pick['basis']}")
        lines.append(f"  fees + ads {_usd(money['fees'])} · label {_usd(money['label'])} ({res['item_type']}) "
                     f"· you keep {_usd(money['keep'])}")
        lines.append(f"Break-even: {_usd(money['break_even_item'])} + shipping")
    elif req.get("ross_price") is not None:
        lines.append("No price: no matching sales or identical listings found.")
    if res["cheapest_total"]:
        line = f"Cheapest identical listing now: {_usd(res['cheapest_total'])} total"
        if res["at_cheapest"]:
            line += f" → profit there {_usd(res['at_cheapest']['profit'])}"
        lines.append(line)
    else:
        lines.append("No identical listing for sale right now.")
    sales_kind = "same item" if req.get("exact", True) else "same item + variants"
    sold_line = f"Sold ({sales_kind}): {pick['sales']}"
    if pick.get("sold_median"):
        sold_line += f" · median {_usd(pick['sold_median'])} total"
    lines.append(sold_line)
    if not req.get("full") and res["sold_cache_misses"]:
        lines.append("Quick check: no recent sold data cached. Tap Full check for sales.")
    for r in res["reasons"]:
        lines.append(f"• {r}")
    if res["already_have"]:
        lines.append("⚠️ Already in our items: " + ", ".join(res["already_have"][:3]))
    t = res["timings"]
    lines.append(f"⏱ {t.get('total_s', 0):.0f}s")
    return "\n".join(lines)


# --- timings log and daily cap ----------------------------------------------------------

def log_timings(res: dict, user_id: int | None = None, path: Path | None = None) -> None:
    path = Path(path or TIMINGS_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "user": user_id,
           "mode": "full" if res["request"].get("full") else "quick",
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
            ts = datetime.fromisoformat(json.loads(line)["ts"])
        except (ValueError, KeyError, TypeError):
            continue
        if ts.astimezone(EASTERN) >= start:
            n += 1
    return n
