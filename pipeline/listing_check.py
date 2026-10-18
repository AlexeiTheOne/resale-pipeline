"""Final listing check: one Claude call that reads a finished draft the way a
careful buyer would and names concrete contradictions before the owner approves.

Each earlier stage checks only its own output: identification reads the photos,
the copywriter writes from the identification, pricing judges comps. Nothing
reads the title, the item specifics, the description, the price evidence and
the photos side by side, which is where the defects that reached live listings
lived: a colour in the specifics but not the title, "With Original Box: No" on a
bag photographed in its box, "**" left in a description, a watch band without
"for Apple Watch". This is that read.

Advisory only. It never edits the draft and never blocks the pipeline: any
error, refusal or timeout comes back as no issues plus an `unchecked` note.
The prompt is deliberately narrow (docs/ebay-playbook.md §1-§3): a check that
cries wolf on style preferences is one the owner learns to scroll past.
"""
import html
import io
import re
from datetime import datetime, timezone
from pathlib import Path

import config

STAGE = "listing_check"
MAX_ISSUES = 5
MESSAGE_MAX = 120
DESCRIPTION_CHARS = 800
PHOTO_SIDE = 768

SEVERITIES = ("fix", "consider")
FIELDS = ("title", "specifics", "description", "price", "condition", "photos")

CONDITION_NAMES = {"1000": "New with tags", "1500": "New without tags",
                   "1750": "New with defects", "2750": "Like new",
                   "3000": "Pre-owned / Good", "4000": "Fair"}

_ROOT = Path(__file__).resolve().parent.parent

# Kept byte-stable (no dates, no per-item text) so it caches across a haul.
SYSTEM_PROMPT = """You are the final check on an eBay listing that a reseller (new Ross/TJX clearance stock: handbags, watches and watch bands, bedding, apparel, accessories) is about to publish. You see the title, item specifics, condition, the start of the description, the price with the comps it was based on, the product identification, and a few of the listing photos.

Report ONLY concrete defects from this list, each one something you can point to in the data or a photo:

1. Title vs specifics. A Color, Size, or Model value in the specifics that is missing from the title. Colour counts as present if the title has the same colour word or an obvious synonym (Navy for Navy Blue); for multi-colour values the main colour is enough. Ignore "One Size", "Multicolor" and empty values. Size matters for apparel, shoes and bedding; a bag or wallet without size in the title is fine. For Apple Watch bands the size is the case size (38/40/41mm, 42/44/45/49mm), not the lug or band width (16mm, 20mm), and a band marked 38/40mm also fits 41mm cases (42/44mm fits 45/49mm): never report either. A named model or product line (e.g. "Jet Set", "Series 9", "Wawona") missing from the title is "fix"; a style or SKU code (e.g. "EHH357-26-0ML") missing is at most "consider", and only when the title is under 70 characters.
2. Brand mismatch: the brand in the title differs from specifics Brand, or the photos clearly show a different brand.
3. Watch bands and straps made to fit an Apple Watch (or another smartwatch) whose title does not say "for Apple Watch" (or "for <device>"). The band's own brand comes first; the device name must follow "for". Not for watches themselves.
4. Specifics contradicted by the photos: colour clearly different, department (Women's/Men's/Kids) clearly wrong, material clearly wrong (e.g. specifics say Leather on an obviously fabric item), "With Original Box: No" / "Original Packaging: No" while a branded box or packaging is visible, or the reverse.
5. Condition contradicted by the photos: visible wear, stains or damage on an item listed as new; or "New with tags" on clothing, a bag, a wallet or a fashion accessory normally sold with a hang tag, when none of the photos you were given shows any hang tag or retail tag (use "consider" and say "no tag visible in the photos checked"). Never report missing tags on sealed or boxed goods (bedding, fragrance, grooming, electronics, watches or bands in a retail box): there the retail box or its price sticker is the tag.
6. Description (already shown as the buyer sees it: **bold** markers are removed when it is rendered, and lines starting with "*", "-", "•" or "✓" are bullets, which is fine): markdown that is still there (a line starting with #, or ** / __ markers), text about a different item (another brand, colour, model or item type than this listing), or claims of "authentic", "100% authentic" or "genuine" as an authenticity claim ("Genuine Leather" as a material is fine). Quote the offending text.
7. Price: an evidence comp that looks like a different model or item than this one (different product line, size, set count, or a bundle), or a list price far from the evidence (under half or over double the evidence comps' typical price) without a stated reason. Comp prices include shipping; the list price excludes it.

Severity: "fix" when it is clearly wrong and the owner should change it before publishing; "consider" when it is likely but you are not sure from what you can see.

Do NOT report: style or wording preferences; keyword "optimization", SEO ideas or extra words that could be added; title length; missing optional specifics; anything about photo quality, lighting, backgrounds or photo count; shipping or returns text; the boilerplate after SHIPPING:; anything you cannot quote or point to; anything you conclude is fine after all (leave it out instead). If the specifics and title agree and the photos do not contradict them, return an empty list. False alarms make the owner ignore this check, so an empty list is the right answer for most listings.

Each message: at most 120 characters, concrete, quoting the offending value, e.g. Title missing colour "Navy" (in specifics) or Specifics say "With Original Box: No" but a box is in photo 2. At most 5 issues, most important first."""

OUTPUT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "issues": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "properties": {
                "severity": {"type": "string", "enum": list(SEVERITIES)},
                "field": {"type": "string", "enum": list(FIELDS)},
                "message": {"type": "string"},
            },
            "required": ["severity", "field", "message"]}},
    },
    "required": ["issues"],
}


def _plain(text: str | None) -> str:
    """Description as plain text: tags dropped, entities decoded, blank runs
    squeezed. The markdown itself is kept: finding it is one of the jobs."""
    t = re.sub(r"<br\s*/?>|</p>|</li>", "\n", text or "", flags=re.I)
    t = html.unescape(re.sub(r"<[^>]+>", "", t))
    return re.sub(r"\n{3,}", "\n\n", t).strip()


def _as_rendered(text: str) -> str:
    """The description with the emphasis the publish renderer strips already
    stripped (ebay/inventory._strip_markdown), so only markdown a buyer would
    actually see is left for the check to find. Stored drafts are full of
    '**HEADING:**' that never reaches a live listing; flagging it is noise."""
    try:
        from ebay.inventory import _strip_markdown
    except Exception:
        def _strip_markdown(line):
            return re.sub(r"\*{2,}", "", re.sub(r"\*\*+(.+?)\*\*+", r"\1", line)).strip()
    return "\n".join(_strip_markdown(ln) for ln in text.splitlines())


def _resolve(p) -> Path | None:
    path = Path(str(p))
    if not path.is_absolute():
        path = _ROOT / path
    return path if path.is_file() else None


def _photo_order(item: dict) -> list:
    """The listing's photo order (cover first). Falls back to the stored order
    if the eBay module can't be imported here."""
    try:
        from ebay.inventory import listing_photo_order
        return listing_photo_order(item)
    except Exception:
        return list(item.get("photos") or [])


def pick_photos(paths: list, n: int | None = None) -> list[Path]:
    """Cover, second shot and the last one: the cover carries colour and
    department, and the listing builder moves the tag photo to the end, which is
    the one that settles "New with tags" and box questions."""
    n = config.LISTING_CHECK_PHOTOS if n is None else n
    found = [r for r in (_resolve(p) for p in paths or []) if r]
    if len(found) <= n:
        return found
    return found[:n - 1] + [found[-1]]


def _downscale(path: Path, side: int = PHOTO_SIDE) -> bytes | None:
    try:
        from PIL import Image, ImageOps
        img = ImageOps.exif_transpose(Image.open(path)).convert("RGB")
        img.thumbnail((side, side))
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=80)
        return buf.getvalue()
    except Exception:
        return None


def _evidence_comps(pricing: dict) -> list[dict]:
    comps = ((pricing.get("claude_evidence") or {}).get("comps")) or []
    if not comps:  # v1-priced items: the sold comps the price came from
        comps = (pricing.get("sold_comps") or [])[:5]
    return comps[:5]


def build_prompt(item: dict, n_photos: int) -> str:
    listing = item.get("listing") or {}
    pricing = item.get("pricing") or {}
    ident = item.get("identification") or {}
    specifics = {k: v for k, v in (listing.get("item_specifics") or {}).items()
                 if v not in (None, "", [])}
    cond_id = str(listing.get("condition_id") or "")
    condition = CONDITION_NAMES.get(cond_id) or specifics.get("Condition") or cond_id or "unknown"

    lines = [f"TITLE ({len(listing.get('title') or '')} chars): {listing.get('title') or ''}",
             "", "ITEM SPECIFICS:"]
    lines += [f"- {k}: {', '.join(map(str, v)) if isinstance(v, list) else v}"
              for k, v in specifics.items()] or ["- (none)"]
    lines += ["", f"CONDITION: {condition} (condition_id {cond_id or '-'})", "",
              f"DESCRIPTION (first {DESCRIPTION_CHARS} characters):",
              _as_rendered(_plain(listing.get("description")))[:DESCRIPTION_CHARS], ""]

    reason = (pricing.get("claude_evidence") or {}).get("reason") or pricing.get("claude_reasoning")
    lines.append(f"PRICE: ${listing.get('price')} item price, shipping charged separately")
    if pricing.get("claude_price") is not None:
        lines.append(f"Pricing model's price ${pricing['claude_price']}, "
                     f"confidence {pricing.get('claude_confidence') or '-'}")
    if reason:
        lines.append(f"Reason: {str(reason)[:300]}")
    if pricing.get("review_flags"):
        lines.append(f"Review flags: {', '.join(pricing['review_flags'])}")
    comps = _evidence_comps(pricing)
    if comps:
        lines.append("Evidence comps (price incl. shipping | title):")
        lines += [f"- ${c.get('price')} | {c.get('source') or 'sold'} | {c.get('title')}"
                  for c in comps]
    lines += ["", "IDENTIFICATION: " + "; ".join(
        f"{k}={ident.get(k)}" for k in ("brand", "product_name", "item_type", "model",
                                        "size", "color") if ident.get(k))]
    lines += ["", f"PHOTOS: {n_photos} attached above, in listing order"
              + (" (photo 1 is the cover; the last is the final listing photo)." if n_photos else
                 " (none available: skip photo checks).")]
    return "\n".join(lines)


def parse_issues(raw) -> list[dict]:
    """Keep well-formed issues only, capped at MAX_ISSUES, messages cut to 120
    characters. The schema already constrains the enums; this is the backstop
    for maxLength/maxItems, which structured outputs don't enforce."""
    out = []
    items = raw.get("issues") if isinstance(raw, dict) else None
    for i in items if isinstance(items, list) else []:
        if not isinstance(i, dict):
            continue
        sev, field = i.get("severity"), i.get("field")
        msg = " ".join(str(i.get("message") or "").split())
        if sev not in SEVERITIES or field not in FIELDS or not msg:
            continue
        if len(msg) > MESSAGE_MAX:
            msg = msg[:MESSAGE_MAX - 1].rstrip() + "…"
        out.append({"severity": sev, "field": field, "message": msg})
    # "fix" first, otherwise in the model's order.
    out.sort(key=lambda x: SEVERITIES.index(x["severity"]))
    return out[:MAX_ISSUES]


def _result(issues, *, model=None, cost=None, unchecked=None, meta=None) -> dict:
    meta = meta or {}
    return {"issues": issues, "model": model or meta.get("model"),
            "cost_usd": round(cost if cost is not None else (meta.get("cost_usd") or 0.0), 5),
            "input_tokens": meta.get("input_tokens"), "output_tokens": meta.get("output_tokens"),
            "unchecked": unchecked,
            "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}


def check_listing(item: dict, *, ask=None) -> dict:
    """Run the check on an item dict (as db.get_item returns it). Returns
    {"issues": [...], "model", "cost_usd", "unchecked", ...}; `unchecked` is a
    short reason when the check did not run, and issues is then empty."""
    if not config.LISTING_CHECK_ENABLED:
        return _result([], unchecked="disabled")
    if not (item or {}).get("listing"):
        return _result([], unchecked="no draft")
    meta: dict = {}
    try:
        photos = [b for b in (_downscale(p) for p in pick_photos(_photo_order(item))) if b]
        prompt = build_prompt(item, len(photos))
        if ask is None:
            import claude_llm
            client = claude_llm.get_client().with_options(
                timeout=config.LISTING_CHECK_TIMEOUT_S, max_retries=1)

            def ask(*a, **kw):
                return claude_llm.ask(*a, client=client, **kw)
        raw = ask(STAGE, prompt, model=config.LISTING_CHECK_MODEL,
                  system=[{"type": "text", "text": SYSTEM_PROMPT,
                           "cache_control": {"type": "ephemeral"}}],
                  max_tokens=4000, effort=config.LISTING_CHECK_EFFORT,
                  images=[(b, "image/jpeg") for b in photos] or None,
                  output_schema=OUTPUT_SCHEMA, item_id=item.get("item_id"), meta=meta)
        return _result(parse_issues(raw), meta=meta)
    except Exception as e:  # advisory: never let it break the draft
        print(f"⚠️ listing check failed: {type(e).__name__}: {str(e)[:200]}")
        return _result([], unchecked=f"{type(e).__name__}", meta=meta)


def check_and_store(item_id: str) -> dict | None:
    """Check the item's current draft and save the result on the item. Never
    raises; returns the stored result (None only if the item is gone)."""
    try:
        from db import get_item, update_field
        item = get_item(item_id)
        if item is None:
            return None
        result = check_listing(item)
        if result.get("unchecked") != "disabled":
            update_field(item_id, "listing_check", result)
        return result
    except Exception as e:
        print(f"⚠️ listing check not stored: {type(e).__name__}: {e}")
        return None


def format_check(result: dict | None) -> str | None:
    """The review-screen block, or None when there is nothing to say."""
    if not result:
        return None
    issues = result.get("issues") or []
    if issues:
        return "\n".join(["⚠️ Check:"] + [f"  • [{i['severity']}] {i['message']}"
                                          for i in issues])
    if result.get("unchecked") and result["unchecked"] not in ("disabled", "no draft"):
        return f"(listing check didn't run: {result['unchecked']})"
    return None
