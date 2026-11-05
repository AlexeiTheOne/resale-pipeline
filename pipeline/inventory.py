"""Physical stock: one row per piece at home (G3-07), linked to listings.

Inventory mode in the bot reserves a code the moment a photo arrives, then
`process_piece` fills in the rest in the background:
  1. read the tag: one cheap Gemini call (brand, type, Ross price, MSRP)
     plus free barcode decoding (UPC on the hang tag, Ross store code);
  2. link it to what's already in the shop: same UPC, same brand+model, or
     (only among the same brand) a Claude photo match;
  3. if nothing matches, it becomes a 'stocked' item ready for /list;
  4. a rough value with no extra AI cost: the live price when it's listed,
     else MSRP x the owner's usual % for the brand, else the brand's typical
     listed price.
"""
import json
import re
import statistics
from datetime import datetime, timezone
from pathlib import Path

import db
import ross_tag
from pipeline.gtin import product_gtin

# A box: room letter(s) + number (G3), optionally with a label stuck on or after
# a dash (O1W, O1-WATCHES), or a named spot (R-SHELF1). A label always starts
# with a letter, so a box never looks like a piece code (O1-01 is a code).
_BOX = r"(?:[A-Z]{1,3}\d{1,3}(?:-?[A-Z][A-Z0-9]{0,14})?|[A-Z]{1,3}-[A-Z]+\d{0,3})"
BOX_RE = re.compile(rf"^{_BOX}$")
CODE_RE = re.compile(rf"^{_BOX}-\d{{2,4}}$")
LINKABLE = ("published", "stocked", "ebay_draft")

_TAG_PROMPT = """These are quick inventory photos (one or more) of ONE retail item, usually
with its tags; several photos are different views of the same item, so combine
what they show. Read what you can see. Return JSON with these keys (null when not visible):
brand, item_type (e.g. "crossbody bag", "sunglasses", "duvet cover set"),
product_name (model/collection name if printed), color, style_code (style/model
number on the hang tag), upc (12-13 digit product barcode number if printed),
msrp (retail price on the brand hang tag, a number), ross_price (the price on
the Ross tag, the reduced one if marked down, a number), ross_original_price
(Ross's crossed-out/original price, a number), notes (anything odd: damage,
missing tag, a set of several pieces), brand_visible (true only if the brand
name or logo is actually readable in the photo; false if you are guessing from
the style or print), ross_tag_visible (true if a Ross store price tag or sticker
is in ANY photo, even partly; false only if you are sure there is none)."""


def _num(v):
    try:
        return round(float(str(v).replace("$", "").replace(",", "")), 2)
    except (TypeError, ValueError):
        return None


def extra_photo_path(main: str | Path, n: int) -> Path:
    """Where photo n (2, 3, ...) of a piece sent as an album is kept: next to the
    main photo, O1-03.jpg -> O1-03_2.jpg. The pieces table holds one photo, so
    the extras live beside it rather than in the database."""
    main = Path(main)
    return main.with_name(f"{main.stem}_{n}{main.suffix}")


def piece_photos(piece: dict) -> list[str]:
    """The piece's main photo, then any album extras (O1-03_2.jpg, ...) in order."""
    main = piece.get("photo")
    if not main:
        return []
    main = Path(main)
    extras = []
    for f in main.parent.glob(f"{main.stem}_*{main.suffix}"):
        n = f.stem[len(main.stem) + 1:]
        if n.isdigit():
            extras.append((int(n), str(f)))
    return [str(main)] + [f for _, f in sorted(extras)]


def read_tag(photo: str | list[str]) -> dict:
    """One Gemini call over every photo of the piece, + barcodes from each.
    Never raises on a bad read: returns what it got."""
    from google.genai import types
    from config import GEMINI_FAST_MODEL
    from identify import client
    from llm import generate_with_retry, response_text
    from ross_tag import read_product_upcs

    photos = [photo] if isinstance(photo, (str, Path)) else list(photo)
    info: dict = {}
    try:
        resp = generate_with_retry(
            client, stage="inventory_tag", model=GEMINI_FAST_MODEL,
            contents=[types.Part.from_bytes(data=Path(p).read_bytes(), mime_type="image/jpeg")
                      for p in photos] + [_TAG_PROMPT],
            config=types.GenerateContentConfig(
                temperature=0, max_output_tokens=2000,
                response_mime_type="application/json",
                thinking_config=types.ThinkingConfig(thinking_budget=0)))
        info = json.loads(response_text(resp, "inventory tag"))
    except Exception as e:  # keep the piece; it can be fixed later
        info = {"notes": f"tag read failed: {type(e).__name__}"}
    for key in ("msrp", "ross_price", "ross_original_price"):
        info[key] = _num(info.get(key))
    if info.get("brand") and info.get("brand_visible") is False:
        # A guessed brand misfiles the piece (a Betsey backpack read as Kate
        # Spade from its polka dots). Unknown is better than wrong.
        info["brand_guess"] = info.pop("brand")
    # Free barcode reads beat OCR'd digits.
    for p in photos:
        upcs = read_product_upcs(p)
        if upcs:
            info["upc"] = upcs[0]
            break
    info["upc"] = product_gtin(info.get("upc"))
    # Every Ross barcode in every photo: the REDUCED sticker (what we paid) is
    # the code kept; a main tag with another code is kept as ross_code_main.
    codes = ross_tag.split_codes([b for p in photos for b in ross_tag.decode_barcodes(p) if len(b) >= 18])
    if codes["code"]:
        info["ross_barcode"] = codes["barcode"]   # the whole barcode, kept for the record
        info["ross_code"] = codes["code"]
        if codes["reduced_price"]:
            info["ross_price"] = codes["reduced_price"]
        if codes["main_code"]:
            info["ross_code_main"] = codes["main_code"]
        if codes["main_price"] and info.get("ross_original_price") is None:
            info["ross_original_price"] = codes["main_price"]
    return info


def _keys(ident: dict) -> set:
    keys = set()
    upc = product_gtin(ident.get("upc"))
    if upc:
        keys.add(("upc", upc))
    brand = re.sub(r"\W", "", str(ident.get("brand") or "")).lower()
    model = re.sub(r"\W", "", str(ident.get("style_code") or ident.get("model") or "")).lower()
    if brand and len(model) >= 4:
        keys.add(("model", f"{brand}:{model}"))
    return keys


def _brand(s) -> str:
    from pipeline.msrp import brand_key
    return brand_key(s) or ""


def find_match(info: dict, photo: str | None, items: list[dict] | None = None,
               ask=None) -> tuple[dict | None, str | None]:
    """(item, how) for the listing/stock row this piece belongs to, or (None, None)."""
    items = [i for i in (items if items is not None else db.list_items())
             if i.get("status") in LINKABLE]
    mine = _keys(info)
    rank = {"published": 0, "ebay_draft": 1, "stocked": 2}
    if mine:
        hits = [i for i in items if mine & _keys(i.get("identification") or {})]
        if hits:
            hit = min(hits, key=lambda i: rank[i["status"]])
            how = "UPC" if any(k[0] == "upc" for k in mine & _keys(hit.get("identification") or {})) else "style number"
            return hit, how
    brand = _brand(info.get("brand"))
    if brand:
        same = [i for i in items if _brand((i.get("identification") or {}).get("brand")) == brand]
    else:
        # No readable brand: compare against everything (costs ~3 cents).
        same = items
    if photo and same and (len(same) <= 100 or not brand):
        from pipeline.stock_lookup import match_by_claude
        try:
            got = match_by_claude(photo, same, ask=ask)
        except Exception as e:
            print(f"WARNING: inventory photo match failed: {type(e).__name__}: {e}")
            return None, None
        if got.get("match_id") and got.get("confidence") in ("high", "medium"):
            hit = next(i for i in same if i["item_id"].startswith(got["match_id"]))
            return hit, "photo"
    return None, None


def rough_value(info: dict, item: dict | None = None, items: list[dict] | None = None) -> tuple[float | None, str]:
    """(value, basis). No AI call."""
    if item is not None:
        price = (item.get("listing") or {}).get("price") or \
            ((item.get("stock") or {}).get("quick_price") or {}).get("price")
        if price:
            return round(float(price), 2), "listing price" if item.get("status") != "stocked" else "stock estimate"
    from pipeline.quick_price import owner_habits
    ident = {"brand": info.get("brand"), "item_type": info.get("item_type")}
    habits = owner_habits(ident, items)
    if info.get("msrp") and habits.get("listed_pct"):
        pct = habits["listed_pct"]["median"]
        return round(info["msrp"] * pct, 2), f"MSRP x {pct:.0%} ({habits['key']})"
    listed = [float((i.get("listing") or {}).get("price")) for i in (items or db.list_items())
              if (i.get("listing") or {}).get("price")
              and _brand((i.get("identification") or {}).get("brand")) == _brand(info.get("brand"))]
    if len(listed) >= 2:
        return round(statistics.median(listed), 2), f"typical {info.get('brand')} price"
    return None, "unknown"


def _as_identification(info: dict) -> dict:
    return {"brand": info.get("brand"), "item_type": info.get("item_type"),
            "product_name": info.get("product_name"), "color": info.get("color"),
            "model": info.get("style_code"), "upc": info.get("upc"),
            "retail_price": info.get("msrp"), "condition": "New with tags",
            "source": "inventory"}


def post_check(info: dict, photos: list[str]) -> str | None:
    """None when a piece can be posted straight from its /inv photos, else why
    not. Never with a Ross tag in shot (it shows buyers what we paid): the read
    must say outright there is none, and a dedicated tag check must agree."""
    if (info.get("ross_tag_visible") is not False or info.get("ross_price") is not None
            or info.get("ross_code") or info.get("ross_original_price") is not None):
        return "Ross tag in the photo"
    if not info.get("brand"):
        return "brand not readable"
    if not info.get("item_type"):
        return "not sure what the item is"
    if not (info.get("upc") or info.get("style_code")):
        return "no UPC or style number"
    if ross_tag.detect_ross_tags(photos):
        return "Ross tag in the photo"
    return None


def process_piece(code: str, ask=None) -> dict:
    """Fill in a freshly captured piece. Returns a summary for the reply."""
    piece = db.get_piece(code)
    photo = piece.get("photo")
    photos = piece_photos(piece)
    info = read_tag(photos if len(photos) > 1 else photo) if photo else {}
    items = db.list_items()
    item, how = find_match(info, photo, items, ask=ask)
    created = False
    if item is None:
        item_id = db.create_item(photos)
        db.update_field(item_id, "identification", _as_identification(info))
        if info.get("ross_price") is not None or info.get("ross_code"):
            # peeled=False: the inventory photo shows the tag, so /list asks for
            # real photos and peels the tag off those.
            db.update_field(item_id, "receipt", {
                "reduced_price": info.get("ross_price"), "code": info.get("ross_code"),
                "original_price": info.get("ross_original_price"), "source": "inventory",
                "peeled": False, **({"main_code": info["ross_code_main"]} if info.get("ross_code_main") else {})})
        value, basis = rough_value(info, None, items)
        not_postable = post_check(info, photos)
        db.update_field(item_id, "stock", {
            "location": piece["box"], "source": "inventory",
            "quick_price": {"price": value, "basis": basis, "rough": True},
            "post_ready": not_postable is None, "post_blocker": not_postable,
            "added_at": datetime.now(timezone.utc).isoformat()})
        db.update_status(item_id, "stocked")
        item = db.get_item(item_id)
        created = True
    else:
        value, basis = rough_value(info, item, items)
    db.update_piece(code, item_id=item["item_id"], info=info, value=value,
                    cost=info.get("ross_price"))
    return {"code": code, "item": item, "how": how, "created": created,
            "value": value, "basis": basis, "info": info,
            "post_ready": bool(created and (item.get("stock") or {}).get("post_ready"))}


def describe(res: dict) -> str:
    info, item = res["info"], res["item"]
    ident = item.get("identification") or {}
    name = " ".join(str(x) for x in (info.get("brand") or ident.get("brand"),
                                    info.get("product_name") or info.get("item_type")
                                    or ident.get("product_name")) if x)[:50] or "item"
    bits = [f"~${res['value']:.0f}" if res.get("value") else "no value yet"]
    if info.get("ross_price"):
        bits.append(f"cost ${info['ross_price']}")
    if item.get("status") == "published":
        link = f"listed ({res['how']} match)"
    elif not res["created"]:
        link = f"same as stock {item['item_id'][:8]} ({res['how']})"
    else:
        link = "not posted"
    return f"{res['code']} ✓ {name} · " + " · ".join(bits) + f" · {link}"


def piece_state(piece: dict, item: dict | None) -> str:
    if piece["status"] != "home":
        return piece["status"]
    return "listed" if item and item.get("status") == "published" else "home"


def box_summary(codes: list[str]) -> list[str]:
    pieces = [p for p in (db.get_piece(c) for c in codes) if p]
    if not pieces:
        return ["Nothing captured."]
    items = {i["item_id"]: i for i in db.list_items()}
    total = sum(p["value"] or 0 for p in pieces)
    listed = [p for p in pieces if piece_state(p, items.get(p["item_id"])) == "listed"]
    pending = [p for p in pieces if not p.get("item_id")]
    boxes = sorted({p["box"] for p in pieces})
    lines = [f"📦 {', '.join(boxes)}: {len(pieces)} piece(s), ~${total:,.0f}",
             f"   {len(listed)} already listed · {len(pieces) - len(listed) - len(pending)} not posted"
             + (f" · {len(pending)} still reading" if pending else "")]
    # Live listings where we found more pieces than eBay is selling.
    by_item = {}
    for p in listed:
        by_item.setdefault(p["item_id"], []).append(p)
    for item_id, ps in by_item.items():
        home = [x for x in db.list_pieces(item_id=item_id, status="home")]
        qty = (items[item_id].get("ebay") or {}).get("quantity")
        if qty is not None and len(home) > int(qty):
            lines.append(f"   ⚠️ {item_id[:8]}: found {len(home)} at home, eBay sells {qty} "
                         f"→ /setqty {item_id[:8]} {len(home)}")
    return lines


def search(q: str) -> list[dict]:
    """Pieces matching a code, a box, a UPC, or text (brand / name / title)."""
    q = q.strip()
    up = q.upper()
    if CODE_RE.match(up):
        p = db.get_piece(up)
        return [p] if p else []
    if BOX_RE.match(up):
        return db.list_pieces(box=up)
    items = {i["item_id"]: i for i in db.list_items()}
    digits = re.sub(r"\D", "", q)
    out = []
    for p in db.list_pieces():
        item = items.get(p["item_id"]) or {}
        ident = item.get("identification") or {}
        if len(digits) >= 8 and digits == q.replace(" ", ""):
            upc = product_gtin(digits)
            if upc and upc in (p["info"].get("upc"), product_gtin(ident.get("upc"))):
                out.append(p)
            continue
        hay = " ".join(str(x) for x in (
            p["info"].get("brand"), p["info"].get("product_name"), p["info"].get("item_type"),
            ident.get("brand"), ident.get("product_name"), (item.get("listing") or {}).get("title"))
            if x).lower()
        if all(w in hay for w in q.lower().split()):
            out.append(p)
    return out


def piece_line(p: dict, items: dict | None = None) -> str:
    items = items if items is not None else {i["item_id"]: i for i in db.list_items()}
    item = items.get(p["item_id"]) or {}
    ident = item.get("identification") or {}
    name = " ".join(str(x) for x in (p["info"].get("brand") or ident.get("brand"),
                                    p["info"].get("product_name") or ident.get("product_name")
                                    or p["info"].get("item_type")) if x)[:45]
    moved = f" (now in {p['box']})" if not p["code"].startswith(p["box"] + "-") else ""
    val = f" · ~${p['value']:.0f}" if p.get("value") else ""
    return f"{p['code']}{moved} · {name or '?'}{val} · {piece_state(p, item)}"


# --- sold alerts ---------------------------------------------------------------

def order_alerts(orders: list[dict], seen: set[str]) -> tuple[list[str], list[str]]:
    """(message lines, order ids handled) for orders not seen before. Marks the
    pulled pieces sold."""
    items = db.list_items()
    by_id = {i["item_id"]: i for i in items}
    by_listing = {str((i.get("ebay") or {}).get("listing_id")): i for i in items
                  if (i.get("ebay") or {}).get("listing_id")}
    lines, handled = [], []
    for order in orders:
        oid = order.get("orderId")
        if not oid or oid in seen:
            continue
        handled.append(oid)
        if (order.get("cancelStatus") or {}).get("cancelState") not in (None, "NONE_REQUESTED"):
            continue
        for li in order.get("lineItems") or []:
            item = by_id.get(li.get("sku")) or by_listing.get(str(li.get("legacyItemId")))
            qty = int(li.get("quantity") or 1)
            total = (li.get("total") or li.get("lineItemCost") or {}).get("value")
            title = (li.get("title") or "")[:60]
            pull = []
            if item:
                for p in db.list_pieces(item_id=item["item_id"], status="home")[:qty]:
                    db.update_piece(p["code"], status="sold", order_id=oid,
                                    sold_at=datetime.now(timezone.utc).isoformat())
                    pull.append(p["code"] + (f" (in {p['box']})" if not p["code"].startswith(p["box"] + "-") else ""))
            where = ("📦 Pull from " + ", ".join(pull)) if pull else "📦 No box on file (not scanned yet)"
            if pull and len(pull) < qty:
                where += f", plus {qty - len(pull)} more with no box on file"
            lines.append(f"💰 Sold: {title}" + (f" — ${float(total):.2f}" if total else "")
                         + (f" ×{qty}" if qty > 1 else "") + f"\n{where}")
    return lines, handled


# --- ghosts: live on eBay, not found at home ------------------------------------

def ghost_report(get_live=None) -> list[str]:
    """Compare each live listing's eBay quantity with the pieces found at home."""
    from pipeline.stock_lookup import live_quantity
    get_live = get_live or live_quantity
    lines_missing, lines_short, lines_extra = [], [], []
    for item in db.list_items("published"):
        try:
            live = get_live(item)
        except Exception as e:
            lines_missing.append(f"   {item['item_id'][:8]}: couldn't read eBay ({type(e).__name__})")
            continue
        if live.get("status") != "Active":
            continue
        avail = live.get("available") or 0
        found = db.list_pieces(item_id=item["item_id"], status="home")
        title = ((item.get("listing") or {}).get("title") or "")[:45]
        if not found:
            lines_missing.append(f"   {item['item_id'][:8]} {title} (eBay {avail})")
        elif len(found) < avail:
            lines_short.append(f"   {item['item_id'][:8]} {title}: eBay {avail}, found {len(found)} "
                               f"({', '.join(p['code'] for p in found)}) → /setqty {item['item_id'][:8]} {len(found)}")
        elif len(found) > avail:
            lines_extra.append(f"   {item['item_id'][:8]} {title}: eBay {avail}, found {len(found)} "
                               f"→ /setqty {item['item_id'][:8]} {len(found)}")
    out = []
    if lines_missing:
        out += [f"👻 Live on eBay, never found at home ({len(lines_missing)}):"] + lines_missing
    if lines_short:
        out += [f"⚠️ eBay sells more than you have ({len(lines_short)}):"] + lines_short
    if lines_extra:
        out += [f"➕ You have more than eBay sells ({len(lines_extra)}):"] + lines_extra
    return out or ["✅ Every live listing matches what's at home."]
