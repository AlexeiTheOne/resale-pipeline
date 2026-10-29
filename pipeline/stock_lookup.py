"""Is this thing already posted? Photo in, answer out.

/find mode in the bot sends each photo here. The match goes:
  1. barcodes (free): a UPC/EAN on the hang tag matches an item exactly;
     a Ross tag code only narrows things down, since Ross reuses codes.
  2. otherwise one Claude call: the photo plus a short line per item in
     the shop, and Claude picks one or says none.
The quantity comes from eBay itself (GetItem on the live listing id), not
the DB, so Seller Hub relists and hand edits are counted right.
"""
import re

from config import FIND_MODEL
from db import list_items
from pipeline.gtin import product_gtin

# Statuses that mean "this went to eBay at some point".
_LISTED = ("published", "sold", "ebay_draft")

try:
    from PIL import Image, ImageOps
    from pyzbar.pyzbar import ZBarSymbol, decode as _zbar
    _SYMBOLS = [ZBarSymbol.CODE128, ZBarSymbol.UPCA, ZBarSymbol.EAN13,
                ZBarSymbol.UPCE, ZBarSymbol.EAN8]
except Exception:  # pragma: no cover - pyzbar missing
    _zbar = None


def read_barcodes(path: str) -> list[str]:
    """Every barcode's digits in the photo (UPC/EAN and Ross CODE128)."""
    if _zbar is None:
        return []
    try:
        img = ImageOps.exif_transpose(Image.open(path))
    except Exception:
        return []
    found = []
    for im in (img, ImageOps.grayscale(img), img.resize((img.size[0] * 2, img.size[1] * 2))):
        try:
            for res in _zbar(im, symbols=_SYMBOLS):
                digits = re.sub(r"\D", "", res.data.decode("utf-8", "ignore"))
                if digits and digits not in found:
                    found.append(digits)
        except Exception:
            continue
        if found:
            break
    return found


def _candidates() -> list[dict]:
    return [i for i in list_items() if i.get("status") in _LISTED]


def match_by_barcode(codes: list[str], items: list[dict]) -> tuple[list[dict], list[dict]]:
    """(exact UPC matches, Ross-code matches)."""
    upcs = {product_gtin(c) for c in codes} - {None}
    ross = {c[:12] for c in codes if len(c) >= 12 and c.startswith("400")}
    by_upc, by_ross = [], []
    for it in items:
        if product_gtin((it.get("identification") or {}).get("upc")) in upcs:
            by_upc.append(it)
        code = re.sub(r"\D", "", str((it.get("receipt") or {}).get("code") or ""))
        if code and code[:12] in ross:
            by_ross.append(it)
    return by_upc, by_ross


def _line(it: dict) -> str:
    ident = it.get("identification") or {}
    title = (it.get("listing") or {}).get("title") or ident.get("product_name") or ""
    bits = [it["item_id"][:8], title]
    for key in ("color", "model", "upc"):
        if ident.get(key):
            bits.append(f"{key} {ident[key]}")
    return " | ".join(str(b) for b in bits)


_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "match_id": {"type": ["string", "null"]},
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        "reason": {"type": "string"},
        "photo_shows_enough": {"type": "boolean"},
    },
    "required": ["match_id", "confidence", "reason", "photo_shows_enough"],
}


def match_by_claude(photo: str, items: list[dict], hint: list[dict] | None = None,
                    ask=None) -> dict:
    """Ask Claude which listed item is in the photo. Returns the schema dict."""
    if ask is None:
        from claude_llm import ask
    catalogue = "\n".join(_line(i) for i in items)
    hint_text = ""
    if hint:
        hint_text = ("\nThe Ross tag barcode matched these ids (Ross reuses codes, so it "
                     "may still be another one): " + ", ".join(i["item_id"][:8] for i in hint))
    prompt = (
        "This photo is one item from a reseller's stock box. Below is every item they "
        "have listed on eBay, one per line: id | title | details. Which one is in the "
        "photo? Match on brand, product, print/colour and any tag text (style number, "
        "UPC). Look-alikes in a different colour or print are NOT a match. If none "
        "fits, return match_id null.\n"
        "Set photo_shows_enough=false when the photo can't identify a specific product "
        "on its own (an inside lining, a bottom, a strap, a blurry corner). Only give a "
        "match_id then if something in the photo still pins it down.\n"
        f"{hint_text}\n\nItems:\n{catalogue}\n\n"
        "Answer with the 8-character id.")
    out = ask("find", prompt, model=FIND_MODEL, images=[photo], max_tokens=1500, effort="low",
              output_schema=_SCHEMA)
    mid = (out.get("match_id") or "").strip()[:8] or None
    out["match_id"] = mid if mid and any(i["item_id"].startswith(mid) for i in items) else None
    return out


def live_quantity(item: dict, get_item_xml=None) -> dict:
    """{"available", "status", "price", "url"} from eBay's GetItem."""
    ebay = item.get("ebay") or {}
    listing_id = ebay.get("listing_id")
    url = ebay.get("view_item_url") or (f"https://www.ebay.com/itm/{listing_id}" if listing_id else None)
    if not listing_id:
        return {"available": None, "status": "never published", "price": None, "url": url}
    if get_item_xml is None:
        from ebay.feedback import _trading

        def get_item_xml(lid):
            return _trading("GetItem", f"<ItemID>{lid}</ItemID>")
    root = get_item_xml(listing_id)

    def text(tag):
        return next((e.text for e in root.iter() if e.tag.endswith(tag)), None)
    qty, sold = int(text("Quantity") or 0), int(text("QuantitySold") or 0)
    status = text("ListingStatus") or "?"
    return {"available": max(qty - sold, 0) if status == "Active" else 0,
            "status": status, "price": text("CurrentPrice"), "url": url}


def lookup(photo: str, ask=None, get_item_xml=None) -> dict:
    """{"item": dict|None, "how": str, "confidence": str, "live": dict|None, "maybe": dict|None}"""
    items = _candidates()
    by_upc, by_ross = match_by_barcode(read_barcodes(photo), items)
    if len(by_upc) == 1:
        item, how, conf = by_upc[0], "UPC barcode", "high"
    elif len(by_upc) > 1:
        # Same product on several rows (a relist, an ended twin): the answer
        # that matters is whichever is live, so ask eBay rather than Claude.
        lives = [(i, live_quantity(i, get_item_xml)) for i in by_upc]
        item, live = max(lives, key=lambda p: (p[1]["status"] == "Active", p[1]["available"] or 0))
        return {"item": item, "how": "UPC barcode", "confidence": "high", "live": live, "maybe": None}
    else:
        # A Ross code is only a hint (Ross reuses codes), so Claude decides.
        got = match_by_claude(photo, items, hint=by_ross or None, ask=ask)
        item = next((i for i in items if got["match_id"] and i["item_id"].startswith(got["match_id"])), None)
        how, conf = ("Ross tag + photo" if by_ross else "photo"), got["confidence"]
        if item is None and got.get("photo_shows_enough") is False:
            # An inside or bottom shot can't prove "not posted".
            return {"item": None, "how": how, "confidence": "low", "live": None,
                    "maybe": None, "unclear": True}
    if item is not None and conf == "low":
        return {"item": None, "how": how, "confidence": conf, "live": None, "maybe": item}
    live = live_quantity(item, get_item_xml) if item else None
    return {"item": item, "how": how, "confidence": conf, "live": live, "maybe": None}


def format_answer(res: dict) -> str:
    def title(it):
        return ((it.get("listing") or {}).get("title") or "")[:70]
    if res["item"] is None:
        if res.get("maybe"):
            m = res["maybe"]
            return (f"🤔 Not sure. Closest is {m['item_id'][:8]}: {title(m)}\n"
                    "Try a photo of the hang tag or the front.")
        if res.get("unclear"):
            return "🤔 Can't tell from this photo. Send the front of the item or its hang tag."
        return "❌ Not posted. Nothing in the shop matches."
    it, live = res["item"], res["live"] or {}
    head = f"{it['item_id'][:8]}: {title(it)}"
    avail = live.get("available")
    if live.get("status") == "Active" and avail:
        line = f"✅ Posted, {avail} available on eBay · ${live.get('price')}"
    elif live.get("status") == "Active":
        line = "⚠️ Posted but 0 available (out of stock on eBay)"
    elif live.get("status") == "never published":
        line = "⚠️ In the bot but never went live"
    else:
        line = f"⚠️ Listing is {live.get('status', '?').lower()}, not live"
    tail = f"\n{live['url']}" if live.get("url") else ""
    return f"{line}\n{head}\n(matched by {res['how']}){tail}"
