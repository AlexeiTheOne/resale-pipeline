"""Do I already have this? The first line of every /check reply.

Looks for the checked product in everything we have or had: items (every
status), pieces (home inventory), Ross receipt lines and earlier checks.
Keys, strongest first. The same item:
  1. brand UPC: a real GTIN, never a Ross store code
  2. Ross code: the 12-digit SKU, from the price tag (REDUCED sticker or main
     tag) or a brand tag that carries it (Ross reuses SKUs now and then)
  3. the FULL style code, print/colour suffix included (EHH108-26-WIP); a
     code with no suffix (LB102100) also needs the colour to agree
The same model in another print or colour (one line of its own):
  4. the base style code only (EHH108 vs EHH108-26-TLP)
And when nothing at all matched, an AI title match over our things of the
same brand: the cheap Claude model compares names (config.CHECK_OWNED_AI).
The brand's concentration ("Ed Hardy: 44 listed, 12 sold") is shown too.

Each hit: status, price, days listed or sold date, what we paid, profit if
sold, box code if at home, eBay link. Read only; lookup() never raises: a
failure comes back as {"error": ...} and the reply says it couldn't check.
"""
import re
import traceback
from datetime import datetime, timezone

import config
from pipeline import check, gtin

# match labels, strongest first (also the sort order)
UPC, ROSS, STYLE_FULL, STYLE, SAME_MODEL, AI_SAME, AI_MODEL = (
    "UPC", "Ross code", "full style code", "style code + colour", "same model, other print/colour",
    "AI: same item", "AI: same model")
STYLE_OTHER_COLOUR = SAME_MODEL          # the old name
STRENGTH = {m: i for i, m in enumerate((UPC, ROSS, STYLE_FULL, STYLE, SAME_MODEL, AI_SAME, AI_MODEL))}
MODEL_ONLY = (SAME_MODEL, AI_MODEL)      # not this item: the same model in another print/colour
STRONG = (UPC, ROSS, STYLE_FULL, STYLE)  # this very item (and may lend its identification)
AI_CANDIDATES = 8
AI_STAGE = "check_owned"
_HOME = ("home",)                 # piece statuses that mean "in a box at home"
_SOURCE_ORDER = {"item": 0, "piece": 0, "receipt": 1, "check": 2}


# --- keys --------------------------------------------------------------------------------

def ross_code(value) -> str | None:
    """The 12-digit Ross SKU in `value` (a tag SKU, or the 18-digit tag barcode
    that starts with it); None for anything else, including generated
    placeholder codes (000000000006)."""
    d = gtin.digits_of(value)
    if len(d) == 18 and d.startswith("400"):
        d = d[:12]
    return d if len(d) == 12 and d.startswith("4") else None


def upc_key(value) -> str | None:
    """A manufacturer UPC/EAN as 14 digits (012345678905 == 0012345678905)."""
    d = gtin.product_gtin(value)
    return d.zfill(14) if d else None


def style_codes(ident: dict) -> tuple[set, set, set]:
    """(full codes, base codes, full codes that carry a suffix), normalised:
    'EHH108-26-WIP' -> full {'ehh10826wip'}, base {'ehh108'}, suffixed
    {'ehh10826wip'}; 'LB102100' -> full = base = {'lb102100'}, no suffix."""
    full, base, suffixed = set(), set(), set()
    for raw in (ident.get("style_code"), ident.get("model")):
        code = check._norm_code(raw)
        if len(code) < 5 or not check._looks_like_code(code):
            continue
        full.add(code)
        b = check._norm_code(re.split(r"[-/ ]", str(raw).strip())[0])
        if len(b) >= 5 and check._looks_like_code(b) and b != code:
            base.add(b)
            suffixed.add(code)
        else:
            base.add(code)
    return full, base, suffixed


def keys(ident: dict | None, sku=None) -> dict:
    """What a product is known by: UPC, full and base style codes, colour, Ross
    codes. `sku`: one code or several (the REDUCED sticker's and the main
    tag's). A Ross code in the "UPC" (a brand tag's barcode that is Ross's
    code) or in identification["ross_code"] counts as a Ross code."""
    ident = ident or {}
    skus = sku if isinstance(sku, (list, tuple, set)) else [sku]
    ross = {c for c in map(ross_code, [*skus, ident.get("upc"), ident.get("ross_code")]) if c}
    full, base, suffixed = style_codes(ident)
    return {"upc": upc_key(ident.get("upc")), "full": full, "base": base, "suffixed": suffixed,
            "codes": full | base, "colour": str(ident.get("color") or "").strip(), "ross": ross}


_COLOUR_FILLER = {"and", "with", "color", "colour", "print", "pattern", "background", "trim", "accents", "the"}


def _colour_words(colour: str) -> set:
    """Named colours, else the words ("Wilma Print" -> {"wilma"}). Multi /
    multicolor / multicolour are one word, "multi"."""
    text = re.sub(r"multi[- ]?colou?r(ed)?", "multi", colour.lower())
    words = set(re.findall(r"[a-z]+", text))
    return (words & (set(check.COLOURS) | {"multi"})) or (words - _COLOUR_FILLER)


# "... Print" words that name no particular print
_GENERIC_PRINT = {"tattoo", "all", "over", "allover", "logo", "graphic", "animal", "floral", "the", "a", "with",
                  "and", "multi", "multicolor", "multicolour", "allover"}
_PRINT_RE = re.compile(r"([a-z]+)\s+print\b")


def print_names(colour: str) -> set:
    """Named prints in a colour: "Black Multi / Wilma Print" -> {"wilma"};
    "... tattoo print" names none."""
    return {w for w in _PRINT_RE.findall((colour or "").lower()) if w not in _GENERIC_PRINT}


def same_colour(a: str, b: str) -> bool:
    """True when either colour is unknown, or they share a colour word
    ("Black Multi (leopard)" and "Black"). A named print must be named on the
    other side too: "Multi" doesn't match "Wilma Print", nor does "Tillie
    Print"."""
    pa, pb = print_names(a), print_names(b)
    if pa or pb:
        la, lb = (a or "").lower(), (b or "").lower()
        return (not pa or any(p in lb for p in pa)) and (not pb or any(p in la for p in pb))
    wa, wb = _colour_words(a or ""), _colour_words(b or "")
    return not wa or not wb or bool(wa & wb)


def match(mine: dict, theirs: dict, suffixed_bases: set | frozenset = frozenset()) -> str | None:
    """The strongest key two products share, or None. `suffixed_bases`: base
    codes we also hold WITH a suffix (ehh108, from EHH108-26-TLP): such a
    code with no suffix is a base only, never the full style."""
    if mine["upc"] and mine["upc"] == theirs["upc"]:
        return UPC
    if mine["ross"] & theirs["ross"]:
        return ROSS
    full = mine["full"] & theirs["full"]
    if full & (mine["suffixed"] | theirs["suffixed"]):
        return STYLE_FULL                 # the print/colour is in the code
    if full - set(suffixed_bases) and same_colour(mine["colour"], theirs["colour"]):
        return STYLE
    if full or mine["base"] & theirs["base"]:
        return SAME_MODEL
    return None


# --- what we have ---------------------------------------------------------------------------

def _num(v):
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _when(v):
    if not v:
        return None
    try:
        t = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def _days_since(v, now: datetime) -> int | None:
    t = _when(v)
    return max(0, (now - t).days) if t else None


def _name(ident: dict, title=None) -> str:
    return (title or " ".join(str(ident.get(k)) for k in ("brand", "product_name", "model") if ident.get(k))
            or "?")[:60]


def _item_hit(it: dict, pieces: list, now: datetime) -> dict:
    ident, ebay = it.get("identification") or {}, it.get("ebay") or {}
    listing, receipt = it.get("listing") or {}, it.get("receipt") or {}
    paid = _num(receipt.get("reduced_price")) or _num(receipt.get("original_price"))
    if paid is None:
        paid = next((_num(p.get("cost")) for p in pieces if _num(p.get("cost"))), None)
    sold = it.get("status") == "sold"
    hit = {"source": "item", "id": it["item_id"], "status": it.get("status"), "name": _name(ident, listing.get("title")),
           "paid": paid, "url": None, "box": None, "price": None, "listed_days": None, "sold_at": None,
           "profit": None}
    if ebay.get("view_item_url") or ebay.get("listing_id"):
        hit["url"] = check.short_url(ebay.get("view_item_url")) or f"https://www.ebay.com/itm/{ebay['listing_id']}"
    if sold:
        hit["sold_at"] = (ebay.get("sold_at") or ebay.get("first_sold_at") or "")[:10] or None
        hit["price"] = _num(ebay.get("sale_price")) or _num(listing.get("price"))
        net, units = _num(ebay.get("net_proceeds")), _num(ebay.get("units_sold")) or 1
        if net is not None and paid is not None:
            hit["profit"] = round(net / units - paid * (1 + config.SALES_TAX_RATE), 2)
    else:
        hit["price"] = _num(listing.get("price")) or _num((it.get("stock") or {}).get("quick_price"))
        if ebay.get("published_at") and it.get("status") == "published":
            hit["listed_days"] = _days_since(ebay["published_at"], now)
    home = [p["code"] for p in pieces if p.get("status") in _HOME]
    if home:
        hit["box"] = ", ".join(home)
    elif it.get("status") == "stocked" and (it.get("stock") or {}).get("location"):
        hit["box"] = it["stock"]["location"]
    return hit


def _piece_hit(p: dict) -> dict:
    info = p.get("info") or {}
    return {"source": "piece", "id": p["code"], "status": p.get("status"), "name": _name(info),
            "paid": _num(p.get("cost")) or _num(info.get("ross_price")), "price": _num(p.get("value")),
            "box": p["code"] if p.get("status") in _HOME else None, "url": None, "listed_days": None,
            "sold_at": (p.get("sold_at") or "")[:10] or None, "profit": None}


def _check_hit(c: dict) -> dict:
    return {"source": "check", "id": c["check_id"], "status": c.get("status"), "verdict": c.get("verdict"),
            "name": _name(c.get("identification") or {}), "date": str(c.get("created_at") or "")[:10] or None,
            "price": _num(c.get("suggested_price")), "paid": _num(c.get("ross_price")), "url": None, "box": None,
            "listed_days": None, "sold_at": None, "profit": None}


def _receipt_hit(r: dict) -> dict:
    return {"source": "receipt", "id": r.get("line_id"), "status": "bought", "name": (r.get("description") or "?")[:60],
            "description": (r.get("description") or "").strip(), "receipt": r.get("receipt_number"),
            "date": r.get("receipt_date"), "city": r.get("store_city"), "paid": _num(r.get("price")),
            "price": None, "url": None, "box": None, "listed_days": None, "sold_at": None, "profit": None}


def _things(exclude_check_id=None) -> list[tuple[dict, dict, dict]]:
    """(keys, ident-like dict for the AI, hit) for every item, unlinked piece
    and earlier check. A piece that belongs to an item is that item (its box)."""
    import db
    now = datetime.now(timezone.utc)
    pieces = db.list_pieces()
    by_item = {}
    for p in pieces:
        if p.get("item_id"):
            by_item.setdefault(p["item_id"], []).append(p)
    out = []
    for it in db.list_items():
        ident = it.get("identification") or {}
        mine = by_item.get(it["item_id"], [])
        receipt = it.get("receipt") or {}
        k = keys(ident, [receipt.get("code"), receipt.get("main_code")])
        for p in mine:      # the piece may know the UPC/style code/Ross codes the item lacks
            info = p.get("info") or {}
            pk = keys({**info, "model": info.get("style_code")}, [info.get("ross_code"), info.get("ross_code_main")])
            k["upc"] = k["upc"] or pk["upc"]
            k["codes"] |= pk["codes"]
            k["ross"] |= pk["ross"]
        out.append((k, {**ident, "title": (it.get("listing") or {}).get("title")}, _item_hit(it, mine, now)))
    for p in pieces:
        if p.get("item_id"):
            continue
        info = p.get("info") or {}
        ident = {**info, "model": info.get("style_code") or info.get("model")}
        out.append((keys(ident, [info.get("ross_code"), info.get("ross_code_main")]), ident, _piece_hit(p)))
    for c in db.list_checks(limit=1000):
        if exclude_check_id is not None and c["check_id"] == exclude_check_id:
            continue
        ident = c.get("identification") or {}
        out.append((keys(ident, [c.get("ross_sku"), c.get("ross_sku_main")]), ident, _check_hit(c)))
    return out


# --- AI fallback ------------------------------------------------------------------------------

_AI_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["matches"],
              "properties": {"matches": {"type": "array", "items": {
                  "type": "object", "additionalProperties": False, "required": ["n", "same"],
                  "properties": {"n": {"type": "integer"},
                                 "same": {"type": "string", "enum": ["same_item", "same_model", "no"]}}}}}}


def _describe(ident: dict) -> str:
    parts = [str(ident.get(k)) for k in ("brand", "product_name", "model", "style_code", "item_type", "color", "size")
             if ident.get(k)]
    if ident.get("title"):
        parts.append(f"title: {ident['title']}")
    return " | ".join(parts)[:300]


def _words(ident: dict) -> set:
    text = " ".join(str(ident.get(k) or "") for k in ("product_name", "model", "style_code", "title", "item_type"))
    return {w for w in re.findall(r"[a-z0-9]+", text.lower()) if len(w) > 2 and w not in check._GENERIC}


def ai_candidates(ident: dict, things: list) -> list:
    """Things of the same brand, the most shared name words first (at most
    AI_CANDIDATES); none when the brand is unknown."""
    from pipeline import scoreboard
    brand = scoreboard.normalize_brand(ident.get("brand"))
    if brand == "Unknown":
        return []
    mine = _words(ident)
    same = [t for t in things if scoreboard.normalize_brand(t[1].get("brand")) == brand]
    same.sort(key=lambda t: -len(mine & _words(t[1])))
    return same[:AI_CANDIDATES]


def ai_match(ident: dict, candidates: list, ask=None) -> list[tuple[int, str]]:
    """[(index into candidates, AI_SAME | AI_MODEL)] from one cheap Claude call."""
    import claude_llm
    ask = ask or claude_llm.ask
    lines = "\n".join(f"{n}. {_describe(t[1])}" for n, t in enumerate(candidates, 1))
    prompt = ("A reseller is about to buy this product:\n"
              f"{_describe(ident)}\n\n"
              "Which of these things we already have or had are the same product? "
              "same_item = same product and colour; same_model = same model or style in another "
              "colour or size; no = a different product (another model of the same brand is 'no').\n"
              f"{lines}\n\nAnswer for every number.")
    out = ask(AI_STAGE, prompt, model=config.CHECK_OWNED_AI_MODEL or config.CLAUDE_CHEAP_MODEL,
              max_tokens=1500, output_schema=_AI_SCHEMA)
    found = []
    for m in (out or {}).get("matches") or []:
        n, same = m.get("n"), m.get("same")
        if isinstance(n, int) and 1 <= n <= len(candidates) and same in ("same_item", "same_model"):
            found.append((n - 1, AI_SAME if same == "same_item" else AI_MODEL))
    return found


# --- lookup -----------------------------------------------------------------------------------

def brand_counts(ident: dict, things: list) -> dict | None:
    """How much of this brand we hold: {"brand", "listed", "home", "sold"} over
    items (scoreboard's brand names, so "Luv Betsey" counts as Betsey Johnson
    only if that's how it normalises). None when the brand is unknown."""
    from pipeline import scoreboard
    name = scoreboard.normalize_brand(ident.get("brand"))
    if name == "Unknown":
        return None
    n = {"brand": name, "listed": 0, "home": 0, "sold": 0}
    for _, theirs, h in things:
        if h["source"] != "item" or scoreboard.normalize_brand(theirs.get("brand")) != name:
            continue
        if h.get("status") == "published":
            n["listed"] += 1
        elif h.get("status") == "sold":
            n["sold"] += 1
        elif h.get("box"):
            n["home"] += 1
    return n


def brand_text(b: dict | None) -> str:
    """'Ed Hardy: 44 listed, 1 at home, 12 sold' ('Ed Hardy: none yet')."""
    if not b:
        return ""
    parts = [f"{b[k]} {w}" for k, w in (("listed", "listed"), ("home", "at home"), ("sold", "sold")) if b[k]]
    return f"{b['brand']}: {', '.join(parts) or 'none yet'}"


def lookup(ident: dict, sku=None, exclude_check_id=None, *, things_fn=None, receipts_fn=None,
           ai_fn=None) -> dict:
    """{"hits": [...strongest and most useful first], "ai": None | {"candidates": n},
    "error": None | text}. Never raises."""
    import db
    try:
        mine = keys(ident, sku)
        things = (things_fn or _things)(exclude_check_id)
        hits = []
        # base codes some item carries with a suffix: without one, they are a base only
        suffixed_bases = {b for k, _, _ in things if k["suffixed"] for b in k["base"]}
        if mine["suffixed"]:
            suffixed_bases |= mine["base"]
        for k, theirs, hit in things:
            m = match(mine, k, suffixed_bases)
            if m:
                shared = sorted(mine["base"] & k["base"]) or sorted(mine["full"] & k["full"])
                hits.append({**hit, "match": m, "ident": theirs,
                             **({"model_code": shared[0].upper()} if m == SAME_MODEL and shared else {})})
        # receipt lines (every one: receipts misread lines too often to count
        # units by them; the reply shows them per receipt, see receipts_summary)
        if mine["ross"]:
            for r in (receipts_fn or db.receipt_lines_for_skus)(sorted(mine["ross"])):
                hits.append({**_receipt_hit(r), "match": ROSS})
        ai = None
        if not hits and config.CHECK_OWNED_AI:
            cands = ai_candidates(ident, things)
            if cands:
                ai = {"candidates": len(cands)}
                for i, m in (ai_fn or ai_match)(ident, cands):
                    hits.append({**cands[i][2], "match": m, "ident": cands[i][1]})
        hits.sort(key=lambda h: str(h.get("date") or ""), reverse=True)      # newest first, then
        hits.sort(key=lambda h: (_SOURCE_ORDER[h["source"]], STRENGTH[h["match"]], h.get("status") == "sold"))
        # the brand: the check's own, else the matched item's (a typed code has none)
        best = donor({"hits": hits})
        who = ident if ident.get("brand") or not best else best["ident"]
        return {"hits": hits, "ai": ai, "error": None, "brand": brand_counts(who, things)}
    except Exception as e:  # advisory: never stops a check
        traceback.print_exc()
        return {"hits": [], "ai": None, "error": f"{type(e).__name__}: {str(e)[:120]}"}


# --- reply lines ------------------------------------------------------------------------------

def _date(v) -> str:
    t = _when(v)
    return f"{t:%b} {t.day}" + (f" {t.year}" if t.year != datetime.now(timezone.utc).year else "") if t else str(v)


def hit_text(h: dict) -> str:
    """One hit in words, without its link: "Listed $69.99, 136 days · paid $14.99 · UPC"."""
    usd = check._usd
    src, status = h["source"], h.get("status")
    if src == "check":
        what = f"Checked #{h['id']} {_date(h['date'])}: {check.LABEL.get(h.get('verdict'), h.get('verdict') or '?')}"
        if h.get("price"):
            what += f", sell {usd(h['price'])}"
        if status in ("bought", "skipped"):
            what += f" ({status})"
        bits = [what]
    elif src == "receipt":
        bits = [f"Bought {_date(h['date'])}" + (f" ({h['city'].title()})" if h.get("city") else "")]
    elif status == "sold":
        bits = ["Sold" + (f" {_date(h['sold_at'])}" if h.get("sold_at") else "")
                + (f" for {usd(h['price'])}" if h.get("price") else "")]
    elif status == "published":
        bits = ["Listed" + (f" {usd(h['price'])}" if h.get("price") else "")
                + (f", {h['listed_days']} days" if h.get("listed_days") is not None else "")]
    elif h.get("box"):
        bits = [f"At home, box {h['box']}" + (f" (~{usd(h['price'])})" if h.get("price") else "")]
    else:
        bits = [f"{'Piece' if src == 'piece' else 'Item'} ({status})"
                + (f" {usd(h['price'])}" if h.get("price") else "")]
    if h.get("box") and not bits[0].startswith("At home"):
        bits.append(f"box {h['box']}")
    if h.get("paid") is not None and src != "check":
        bits.append(f"paid {usd(h['paid'])}")
    elif h.get("paid") is not None:
        bits.append(f"Ross {usd(h['paid'])}")
    if h.get("profit") is not None:
        bits.append(f"profit {usd(h['profit'])}")
    label = h["match"]
    if label not in STRONG:                     # weaker matches name what matched
        label += f": {h['name'][:40]}"
    bits.append(label)
    return " · ".join(bits)


def _summary(hits: list) -> str:
    n = {"listed": 0, "home": 0, "sold": 0, "other": 0, "receipt": 0, "check": 0}
    n["receipt"] = len({_receipt_key(h) for h in hits if h["source"] == "receipt"})
    for h in hits:
        if h["source"] == "receipt":
            continue
        if h["source"] == "check":
            n["check"] += 1
        elif h.get("status") == "sold":
            n["sold"] += 1
        elif h.get("status") == "published":
            n["listed"] += 1
        elif h.get("box"):
            n["home"] += 1
        else:
            n["other"] += 1
    words = [f"{n['listed']} listed", f"{n['home']} at home", f"{n['sold']} sold", f"{n['other']} in progress",
             f"{n['receipt']} receipt{'' if n['receipt'] == 1 else 's'}", f"{n['check']} checked before"]
    return ", ".join(w for w in words if not w.startswith("0 "))


# --- what it is, and lending it to the search ---------------------------------------------------

_CATEGORY = {"bags": "bag", "shoes": "shoes", "clothing": "clothing", "jewelry": "accessory"}
_LEND = ("brand", "product_name", "model", "style_code", "upc", "color", "item_type", "size", "material",
         "department", "gender", "retail_price")


def donor(owned: dict | None) -> dict | None:
    """The best hit whose identification the check can use: an item, piece or
    earlier check matched by UPC, style code + colour or Ross code, with a
    brand or a product name."""
    for h in (owned or {}).get("hits") or []:
        ident = h.get("ident") or {}
        if h["match"] in STRONG and h["source"] in ("item", "piece", "check") and \
                (ident.get("brand") or ident.get("product_name")):
            return h
    return None


def what_it_is(ident: dict) -> str:
    """'Ed Hardy "Love Kills Slowly" Weekender Duffel Bag · bag: weekender duffel · pink multicolor'."""
    brand = str(ident.get("brand") or "").strip()
    name = str(ident.get("product_name") or ident.get("title") or "").strip()
    full = name if brand and brand.lower() in name.lower() else " ".join(x for x in (brand, name) if x)
    sub = str(ident.get("item_type") or "").strip().lower()
    cat = _CATEGORY.get(check.item_type_of(ident))
    if cat:
        rest = " ".join(w for w in sub.split() if w.rstrip("s") not in (cat, "bag", "shoe", "handbag"))
        kind = f"{cat}: {rest}" if rest else cat
    else:
        kind = sub
    colour = str(ident.get("color") or "").strip().lower()
    return " · ".join(x for x in (full[:80], kind, colour) if x)


def borrow_identification(ident: dict, hit: dict | None) -> tuple[dict, dict | None]:
    """(identification for the eBay search, where it came from). A code that
    matched one of ours says what the thing is: with nothing of its own (a
    typed code, no brand) the check takes our item's identification and
    searches its listing title; with its own (a photo) it only fills gaps."""
    if not hit:
        return ident, None
    theirs = {k: v for k, v in (hit.get("ident") or {}).items() if v not in (None, "", [], {})}
    title = theirs.get("title") or theirs.get("search_query")
    if not ident.get("brand"):
        out = {k: theirs[k] for k in _LEND if k in theirs}
        out["search_query"] = title or " ".join(str(theirs[k]) for k in ("brand", "product_name") if k in theirs)
    else:
        out = dict(ident)
        for k in _LEND:
            if not out.get(k) and k in theirs:
                out[k] = theirs[k]
        if out.get("upc") and gtin.is_store_code(out["upc"]) and theirs.get("upc"):
            out["upc"] = theirs["upc"]
        if not check.without_ross_codes(out).get("search_query") and title:
            out["search_query"] = title
    if out.get("upc") and not gtin.product_gtin(out["upc"]):
        out.pop("upc")
    return out, {"source": hit["source"], "id": hit["id"], "match": hit["match"],
                 "query": out.get("search_query")}


def reply_lines(owned: dict | None, *, html: bool = False, limit: int | None = None) -> list[str]:
    """The top of the reply:
      "🔁 Already have: <what it is> — 1 listed, 1 sold", up to `limit` hits
      of this item with their links, or "✅ New";
      then the same model in other prints/colours on one line of its own;
      then the brand's concentration ("Ed Hardy: 44 listed, 12 sold")."""
    from html import escape
    if not owned:
        return []
    if owned.get("error"):
        return ["⚠️ Couldn't check what we already have"]
    hits = owned.get("hits") or []
    same = [h for h in hits if h["match"] not in MODEL_ONLY]
    models = [h for h in hits if h["match"] in MODEL_ONLY]
    brand = brand_text(owned.get("brand"))
    esc = escape if html else (lambda s: s)
    limit = config.CHECK_OWNED_SHOW if limit is None else limit
    if not same:
        lines = [esc("✅ New" + (f" · {brand}" if brand and not models else ""))]
    else:
        best = donor(owned)
        what = what_it_is(best["ident"]) if best else next(
            (f'"{h["description"]}" (receipt)' for h in same if h["source"] == "receipt" and h.get("description")),
            "")
        lines = [esc("🔁 Already have: " + (f"{what} — " if what else "") + _summary(same))]
        shown = _grouped(same)
        for h in shown[:limit]:
            text = _receipts_text(h["lines"]) if h["source"] == "receipts" else hit_text(h)
            lines.append("• " + _with_link(text, h, html))
        if len(shown) > limit:
            lines.append(f"• … {len(shown) - limit} more in Details")
    if models:
        lines.append("• " + _with_link(_models_text(models), next((h for h in models if h.get("url")), {}), html))
    if brand and (same or models):
        lines.append(esc(f"• {brand}"))
    return lines


def _with_link(text: str, h: dict, html: bool) -> str:
    """The text, then "🔗 <listing title>" linking to the hit's eBay page."""
    from html import escape
    label = _short(h.get("name") or "eBay", 45)
    if html:
        return escape(text) + (f' · 🔗 <a href="{escape(h["url"])}">{escape(label)}</a>' if h.get("url") else "")
    return text + (f" · 🔗 {label} {h['url']}" if h.get("url") else "")


def _models_text(models: list) -> str:
    """'Same model EHH108, other prints/colours: 2 listed, 1 sold'."""
    code = next((h["model_code"] for h in models if h.get("model_code")), None)
    who = f"Same model {code}" if code else "Same model"
    return f"{who}, other prints/colours: {_summary(models)}"


def _short(text: str, n: int) -> str:
    """At most n characters, cut at a word: 'Ed Hardy Love Kills Slowly Weekender…'."""
    text = str(text).strip()
    if len(text) <= n:
        return text
    return text[:n].rsplit(" ", 1)[0].rstrip(" ,-·") + "…"


def _grouped(hits: list) -> list:
    """The receipt lines as one entry (where the first one was): a SKU bought
    ten times is one line in the reply, not ten."""
    out, lines = [], [h for h in hits if h["source"] == "receipt"]
    for h in hits:
        if h["source"] != "receipt":
            out.append(h)
        elif h is lines[0]:
            out.append({"source": "receipts", "lines": lines, "url": None})
    return out


def _receipt_key(h: dict):
    return h.get("receipt") or (h.get("date"), h.get("city"))


def receipts_summary(lines: list) -> dict:
    """Receipt lines of one product, per receipt. The receipt reader often
    splits one purchase over adjacent lines at different prices (57 times in
    213 receipts, 2026-11-05: $0.99 next to $15.99), so a receipt that lists
    the code at several prices is "misread" and its prices are left out of
    the range. Repeated lines at one price stay (2 bought)."""
    by = {}
    for h in lines:
        by.setdefault(_receipt_key(h), []).append(h)
    clean, odd = [], 0
    for ls in by.values():
        prices = {h["paid"] for h in ls if h.get("paid") is not None}
        if len(prices) > 1:
            odd += 1
        else:
            clean += list(prices)
    descs = [h["description"] for h in lines if len(h.get("description") or "") > 3]
    dates = sorted(str(h["date"]) for h in lines if h.get("date"))
    return {"receipts": len(by), "lines": len(lines), "misread": odd,
            "low": min(clean) if clean else None, "high": max(clean) if clean else None,
            "description": max(set(descs), key=descs.count) if descs else None,
            "first": dates[0] if dates else None, "last": dates[-1] if dates else None,
            "city": next((h["city"] for h in lines if h.get("city")), None)}


def _receipts_text(lines: list) -> str:
    """'Bought on 7 receipts (Aug 11 – Sep 19) · "EH Pink aop fish f" · paid $7.99–$14.99 ·
    3 receipts misread · Ross code'."""
    usd = check._usd
    s = receipts_summary(lines)
    if s["lines"] == 1:
        return hit_text(lines[0]).replace(" · Ross code", "") + \
            (f' · "{s["description"]}"' if s["description"] else "") + " · Ross code"
    span = f" ({_date(s['first'])} – {_date(s['last'])})" if s["first"] and s["first"] != s["last"] else \
        (f" ({_date(s['first'])})" if s["first"] else "")
    bits = [f"Bought on {s['receipts']} receipt{'' if s['receipts'] == 1 else 's'}{span}"]
    if s["description"]:
        bits.append(f'"{s["description"]}"')
    if s["low"] is not None:
        bits.append(f"paid {usd(s['low'])}" if s["low"] == s["high"] else f"paid {usd(s['low'])}–{usd(s['high'])}")
    if s["misread"]:
        bits.append(f"{s['misread']} receipt{'' if s['misread'] == 1 else 's'} misread (one code, several prices)")
    bits.append(ROSS)
    return " · ".join(bits)


def details_lines(owned: dict | None) -> list[str]:
    """Every hit, for the Details button."""
    if not owned:
        return []
    if owned.get("error"):
        return [f"Already have: couldn't check ({owned['error']})"]
    hits = owned.get("hits") or []
    ai = owned.get("ai")
    note = f" (AI title match over {ai['candidates']} same-brand thing(s))" if ai else ""
    brand = brand_text(owned.get("brand"))
    if not hits:
        return [f"Already have: nothing in items, pieces, receipts or earlier checks{note}"] + \
            ([f"  {brand}"] if brand else [])
    out = [f"Already have ({len(hits)}){note}:" + (f" · {brand}" if brand else "")]
    for h in hits:
        ref = {"item": f"item {str(h['id'])[:8]}", "piece": f"piece {h['id']}", "check": f"check #{h['id']}",
               "receipt": f"receipt line {h['id']}"}[h["source"]]
        desc = f' "{h["description"]}" (receipt {h.get("receipt") or "?"})' if h["source"] == "receipt" else ""
        out.append(f"  {ref}{desc} · {hit_text(h)}" + (f" · {h['url']}" if h.get("url") else ""))
    return out


def saved(owned: dict | None) -> list[dict]:
    """A compact copy for the checks table (comps_summary.owned)."""
    return [{"source": h["source"], "id": h["id"], "match": h["match"], "status": h.get("status")}
            for h in (owned or {}).get("hits") or []][:10]
