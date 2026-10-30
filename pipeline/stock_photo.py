"""Is the brand's stock photo the same product as the item in hand?

identify.py finds an "official" product photo on a retailer site, and the
draft adds it to the listing. Nothing checked it. On 30 Oct 2026 the Polo x
Whitehouse Cox braided belt (style 405898683001) got a photo of a different
Polo belt (405807144001: roller buckle, flat braid) — a "not as described"
return waiting to happen. A wrong stock photo is worse than none, so this
fails closed: any doubt, error or missing photo means no stock photo.

Two checks, cheapest first:
  1. Style numbers. Retailer image URLs often carry the style number
     (".../PRL405807144001FW21.jpg"). A long digit run in the URL that is
     not the item's own style/model/UPC means a different product, no call
     needed; one that IS ours means it's the right product.
  2. Otherwise Claude compares the stock photo with our own photos.
"""
import re

from config import CLAUDE_MODEL

LONG_DIGITS = re.compile(r"\d{9,14}")


def _own_codes(ident: dict) -> set[str]:
    codes = set()
    for k in ("model", "style_code", "upc", "mpn"):
        codes.update(LONG_DIGITS.findall(str(ident.get(k) or "")))
    return codes


def code_verdict(url: str, ident: dict) -> bool | None:
    """True/False when the URL's style number settles it, None when it can't."""
    mine = _own_codes(ident)
    found = LONG_DIGITS.findall(str(url or ""))
    if any(m in u for u in found for m in mine):
        return True
    # Only a run as long as one of our codes counts as a rival style number;
    # URLs also carry timestamps and CDN ids, which must not veto a photo.
    lengths = {len(m) for m in mine}
    rivals = [u for u in found if len(u) in lengths]
    return False if rivals else None


SCHEMA = {
    "type": "object",
    "properties": {
        "differences": {"type": "array", "items": {"type": "string"}},
        "same_product": {"type": "boolean"},
        "reason": {"type": "string"},
    },
    "required": ["differences", "same_product", "reason"],
    "additionalProperties": False,
}


def _small(path, side: int = 768):
    from pipeline.pricing_v2 import _photo_bytes
    data = _photo_bytes(path, side)
    return (data, "image/jpeg") if data else None


def stock_photo_matches(stock_path: str, url: str, ident: dict, own_photos: list[str],
                        *, item_id: str | None = None, ask_fn=None) -> tuple[bool, str]:
    """(ok, why). ok=False means leave the stock photo out."""
    by_code = code_verdict(url, ident)
    if by_code is True:
        return True, "style number in the image URL matches"
    if by_code is False:
        return False, "image URL carries a different style number"
    own = [b for b in (_small(p) for p in own_photos[:3]) if b]
    stock = _small(stock_path)
    if not own or not stock:
        return False, "couldn't load the photos to compare"
    prompt = (
        f"The first {len(own)} photo(s) show the exact item a reseller is listing: "
        f"{ident.get('brand') or ''} {ident.get('product_name') or ident.get('item_type') or ''}. "
        "The LAST photo is a retailer stock photo found online. Decide whether it shows the "
        "SAME product, or a different product from the same brand.\n"
        "Compare detail by detail, as a picky buyer would: hardware type and shape (e.g. "
        "roller vs plain buckle), construction (e.g. separate round braided strands vs one "
        "flat braid), straps/keepers/loops, stitching, print or pattern placement, logos, "
        "colour. List every visible difference in `differences` (empty only if none). Any "
        "real difference means same_product=false. Lighting, angle and background are "
        "not differences. If the stock photo is too unclear to tell, answer false.")
    if ask_fn is None:
        from claude_llm import ask as ask_fn
    try:
        out = ask_fn("stock_photo_check", prompt, model=CLAUDE_MODEL,
                              images=own + [stock], max_tokens=1200, effort="medium",
                              output_schema=SCHEMA, item_id=item_id)
    except Exception as e:
        return False, f"check failed ({type(e).__name__})"
    diffs = [d for d in (out.get("differences") or []) if str(d).strip()]
    same = bool(out.get("same_product")) and not diffs
    why = "; ".join(diffs) if diffs else str(out.get("reason") or "")
    return same, why[:200]
