import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import json
import os
from dotenv import load_dotenv
from google.genai import types

from ebay.taxonomy import category_aspects, category_suggestions
from llm import generate_with_retry, make_client, response_text
from pipeline.category import choose_category, department_of
from pipeline.description_guard import sanitize_description
from pipeline.gtin import product_gtin
from pipeline.specifics import clean_specifics
from pipeline.title import (TITLE_MAX_LEN, TITLE_RULES, enforce_band_for_device,
                            ensure_title_fits, fit_title)

load_dotenv()
client = make_client(api_key=os.getenv("GEMINI_API_KEY"))

from config import GEMINI_FAST_MODEL

MODEL = GEMINI_FAST_MODEL
ASPECT_HINT_VALUE_CAP = 20

SYSTEM_PROMPT = (
    "You are an expert eBay listing copywriter for a reseller store. "
    "You write concise, accurate, SEO-optimized eBay listings. "
    "You always respond with valid JSON only, no markdown, no explanation. "
    "You never invent details not present in the source data. "
    "You never claim an item is new if condition flags suggest otherwise."
)

# The word "authentic" is deliberately absent, and must stay absent.
#
# It used to appear twice here ("we specialize in authentic brand-name items…
# All items are 100% authentic"), which put it on 109 of 114 live listings.
# Claiming authenticity as a selling point is a documented VeRO trigger: it is
# exactly the phrasing rights owners' automated sweeps look for, and a VeRO
# takedown is a listing removal with no appeal path to eBay (only to the rights
# owner), escalating to account restriction on repeats. It also appears in NO
# traffic report, so a listing that vanished this way looks like a mystery.
# Michael Kors, Coach, Kate Spade and Ralph Lauren — most of this catalogue —
# are active VeRO participants. See docs/ebay-playbook.md §1.
#
# The same paragraph is also worth nothing: eBay's default search does not index
# description body text (it's the opt-in "Title and description" checkbox), so
# store boilerplate buys zero discoverability and only adds page weight above
# the fold on mobile, where item specifics render before the description.
STORE_BOILERPLATE = """
SHIPPING:
Ships within 1 business day via USPS with tracking number.
Carefully packaged to ensure safe delivery.

RETURNS:
30-day return policy. Buyer pays return shipping.
Item must be returned in the same condition it was sent.{tags}

Message us with any questions — we typically reply within 24 hours.
"""

# Conditions where the item genuinely ships with the manufacturer's tags on it.
# Everything else — new without tags, open box, used — must not demand tags back,
# because the listing itself already says there weren't any. Left unbranched, a
# NWOT listing told the buyer to return an item "with tags attached" that it had
# just finished explaining has no tags.
_TAGGED_CONDITIONS = {"1000"}


def store_boilerplate(condition_id) -> str:
    tags = (" Tags must still be attached."
            if str(condition_id) in _TAGGED_CONDITIONS else "")
    return STORE_BOILERPLATE.format(tags=tags).strip()

SCHEMA_INSTRUCTIONS = """Generate an eBay listing as a valid JSON object matching this exact schema:

{
  "title": "string, AT MOST 80 characters including spaces, eBay SEO optimized. Lead with brand + product_name (the marketing/retail name from identification), then size/color. Use product_name rather than item_type whenever it is present; fall back to item_type only if product_name is null.",
  "description": "string, concise and scannable: short bullet sections, honest about any flaws (see structure below)",
  "category_id": "string: the category_id given under EBAY CATEGORY above; only if none is given, the best-fit eBay category ID (it is checked against the item type in code)",
  "item_specifics": {
    "Brand": "string",
    "Model": "string or null",
    "Color": "string",
    "Size": "One Size if universal fit, otherwise the specific size from tag",
    "Material": "string or null",
    "Condition": "string",
    "MPN": "manufacturer part number from Google Search or null",
    "UPC": "identification.upc copied exactly, or null (never a number from search or the Ross price tag)"
  },
  "price": 0.0,
  "condition_id": "string"
}

Rules:
- title: """ + TITLE_RULES + """
- NEVER describe the item as "authentic", "100% authentic", "genuine article" or
  "guaranteed genuine", in the title or the description — not even as the opening
  adjective ("Authentic Michael Kors ..."). Claiming authenticity is what rights
  owners' automated brand-protection sweeps look for, and it gets listings pulled
  under VeRO with no appeal path to eBay. State what the item IS; never argue that
  it is real. ("Genuine Leather" as a MATERIAL is fine — that is a different word
  doing a different job.)
- price: use the provided suggested_price exactly.
- condition_id mapping: 1000=New with tags, 1500=New without tags, 2750=Like new, 3000=Good, 4000=Fair
- description must mention any condition_flags honestly.
- item_specifics Brand and Color must be non-null strings.
- Return raw JSON only, no markdown fences.
- Size field: write 'One Size' if the item has one universal size.
  Put dimensions in the description SPECIFICATIONS section only, not in Size field.

Build the item_specifics this way:

PRIMARY SOURCE - identification.specifications:
The ITEM IDENTIFICATION above contains a "specifications" object built from the
EXACT product matched online (via its UPC/style number) and already verified
against photos of this specific unit. This is your authoritative source. Copy
every relevant entry into item_specifics, mapping the keys onto the eBay category
aspect names listed above (e.g. Hardware Color, Lining Color, Strap Color,
Exterior Color, Pattern, Finish, Closure, Features, Country of Origin, Bag Height/
Width/Depth). These values describe THIS unit — prefer them over any generic
assumption. The identification's color/condition fields are likewise photo-verified.

SECONDARY SOURCE - YOUR OWN KNOWLEDGE:
Use your general knowledge only to FILL GAPS the specifications don't cover (MPN,
a missing category aspect, care instructions). Never UPC: it comes only from
identification.upc. Do not overwrite a value from
identification.specifications with a generic catalog guess.

Rules for item_specifics:
- Fill every aspect you can confidently determine from the specifications;
  do not leave a field blank when the data clearly answers it.
- Each value is a short filter value (a word or two), never a sentence or a
  comma-joined list. Color/Exterior Color: the one main colour (e.g. "Black");
  "Multicolor" only when no single colour dominates. Pattern and Style: one
  value.
- Leave an aspect out when the identification does not answer it — a guessed
  value is worse than none. Department only when the identification states it.
  Pattern "Animal Print" only for an animal-skin print (leopard, cheetah, zebra,
  snake, croc), never for a print that merely shows an animal.
- Never invent condition details — only what the identification/photos report.
- Never list the same value twice in one aspect.

description: Keep it lightweight and scannable — bullets over paragraphs, no
marketing fluff. Use this structure:
- One short opening line summarizing the item (a single sentence, not a paragraph).
- Do NOT write a "what you see is what you get" line yourself. It is added in bold
  above the SHIPPING section of every listing automatically, and writing your own
  only pushes the bold one out (see WYSIWYG_NOTE in config.py).
- SPECIFICATIONS: short bullet list — brand, color, material, size/dimensions.
- FEATURES: 3-5 one-line bullets with ✓ (concise, no filler).
- CONDITION: one short bullet on condition and any flags.
- INCLUDED: bullet list of exactly what ships.
- Write section headings as bare ALL-CAPS text ending in a colon, e.g.
  SPECIFICATIONS: — plain, on their own line.
- NO MARKDOWN. Never wrap anything in ** or * for emphasis. The renderer treats
  a leading * as a bullet marker, so a heading written as **SPECIFICATIONS:**
  is read as a bullet and the heading disappears from the published listing,
  asterisks and all. Bullets start with -, • or ✓ and nothing else.
- INCLUDED must describe what is actually in the photos, not just the item
  itself: if a pouch, retail box, dust bag, tags or adapters are visible in the
  images, list them. Do not invent packaging that isn't shown."""


def _choose_category(identification: dict | None, specifics: dict | None = None,
                     title: str | None = None) -> dict | None:
    """pipeline/category.py's choice, with the live Taxonomy suggestions as its
    fallback source. Never raises."""
    try:
        return choose_category(identification, specifics, title, suggest=category_suggestions)
    except Exception as e:
        print(f"⚠️ category choice failed ({type(e).__name__}: {e})")
        return None


def _enforce_category(draft: dict, identification: dict | None,
                      chosen: dict | None = None) -> None:
    """Put the code-chosen category on the draft, in place.

    The model used to pick category_id freehand, and nothing but "does eBay
    accept the number" checked it: a duffel went out under Underwater Cameras
    (a8ad7eba), an Apple Watch band under Canes & Walking Sticks (b50bd66b),
    sunglasses under Eyeglass Frames (8a66a4b0), a men's messenger under Women's
    Bags (94c8a02f). The draft's Department/title can reveal a department the
    identification did not state, so the choice is redone when they do."""
    specifics = draft.get("item_specifics") or {}
    title = draft.get("title")
    if not chosen or department_of(identification, specifics, title) != chosen.get("department"):
        chosen = _choose_category(identification, specifics, title)
    model_id = str(draft.get("category_id") or "")
    if chosen:
        if chosen["category_id"] != model_id:
            print(f"🗂️ category {model_id or '(none)'} -> {chosen['category_id']} "
                  f"({chosen['source']}{': ' + chosen['name'] if chosen.get('name') else ''})")
        draft["category_id"] = chosen["category_id"]
    else:
        print(f"⚠️ no known or consistent category for item type "
              f"{(identification or {}).get('item_type')!r}; keeping the model's {model_id!r}"
              " — check it at review")


def _guard_description(draft: dict, identification: dict | None) -> None:
    """Strip authenticity claims, hashtags, keyword blocks and other stores'
    names from the model's description, in place (pipeline/description_guard.py).
    The prompt forbids them; this is what makes it true."""
    brand = (draft.get("item_specifics") or {}).get("Brand") or (identification or {}).get("brand")
    cleaned, removed = sanitize_description(draft.get("description") or "", brand)
    for r in removed:
        print(f"🛡️ description: removed {r}")
    draft["description"] = cleaned


def _clean_draft_specifics(draft: dict, identification: dict | None) -> None:
    """Hold item_specifics to filter-shaped values the identification supports
    (pipeline/specifics.py), against the chosen category's aspect metadata."""
    try:
        meta = category_aspects(str(draft.get("category_id"))) if draft.get("category_id") else None
    except Exception:
        meta = None
    cleaned, notes = clean_specifics(draft.get("item_specifics") or {}, identification, meta)
    for note in notes:
        print(f"🧹 specifics: {note}")
    draft["item_specifics"] = cleaned


def _aspect_hint_block(category: dict | None) -> str | None:
    """Best-effort guidance block listing every item specific eBay's catalog
    defines for the chosen category, not just the required ones, so Gemini
    can fill in the ones buyers actually filter/search by. Never blocks draft
    generation — publish-time validation in ebay/inventory.py is the real
    safety net if this lookup fails.

    It used to describe the first raw Taxonomy suggestion for the search query,
    a category nothing else used and nothing checked (180000 Underwater Cameras
    for a duffel), which is how camera aspects end up in a bag prompt."""
    category_id = (category or {}).get("category_id")
    if not category_id:
        return None
    try:
        aspects = category_aspects(category_id)
    except Exception:
        return None

    if not aspects:
        return None

    lines = [
        f"EBAY CATEGORY: use category_id {category_id} (chosen from the item type). "
        "Item specifics for that category:",
        "Buyers filter and search by these. Fill one only when the identification or "
        "photos clearly answer it, and leave it out otherwise — never pick a value just "
        "because it is listed (the lists are alphabetical, not a default). Missing "
        "required ones are filled safely at publish.",
        "",
    ]
    for spec in aspects:
        name = spec.get("localizedAspectName")
        if not name:
            continue

        constraint = spec.get("aspectConstraint") or {}
        if constraint.get("aspectRequired"):
            level = "REQUIRED"
        elif constraint.get("aspectUsage") == "RECOMMENDED":
            level = "RECOMMENDED"
        else:
            level = "OPTIONAL"

        values = [v.get("localizedValue") for v in (spec.get("aspectValues") or []) if v.get("localizedValue")]
        if values:
            shown = ", ".join(values[:ASPECT_HINT_VALUE_CAP])
            if len(values) > ASPECT_HINT_VALUE_CAP:
                shown += ", ..."
            lines.append(f"- {name} [{level}] (choose one: {shown})")
        else:
            lines.append(f"- {name} [{level}] (free text)")

    return "\n".join(lines)


def _refit_title(title: str, identification: dict | None, specifics: dict | None) -> str | None:
    """Ask the copywriter model once, with nothing but the title-fit job, to
    bring an over-long title within eBay's limit. Returns its answer unchecked —
    ensure_title_fits measures it and falls back to fit_title if it's still long.

    This is the existing Gemini model on purpose: a dedicated (Claude-based)
    fitter can replace it by passing a different `refit` to ensure_title_fits."""
    ident = identification or {}
    spec = specifics or {}
    facts = {k: v for k, v in {
        "brand": spec.get("Brand") or ident.get("brand"),
        "item_type": ident.get("item_type"),
        "model": spec.get("Model") or ident.get("model"),
        "size": spec.get("Size") or ident.get("size"),
        "color": spec.get("Color") or ident.get("color"),
        "material": spec.get("Material"),
        "department": spec.get("Department"),
    }.items() if v}
    user_message = (
        f"This eBay title is {len(title)} characters; the limit is {TITLE_MAX_LEN}.\n"
        f"TITLE: {title}\n"
        f"ITEM FACTS: {json.dumps(facts, ensure_ascii=False)}\n\n"
        f"Rewrite it to at most {TITLE_MAX_LEN} characters. {TITLE_RULES} "
        "Do not add any word that is not already true of the item. "
        "Reply with the new title only — no quotes, no JSON, no explanation."
    )
    response = generate_with_retry(
        client,
        model=MODEL,
        contents=user_message,
        config=types.GenerateContentConfig(
            temperature=0,
            max_output_tokens=2000,
            thinking_config=types.ThinkingConfig(thinking_budget=512),
        ),
    )
    text = response_text(response, "title refit").strip()
    lines = [ln for ln in text.splitlines() if ln.strip()]
    return lines[0].strip().strip("\"'`") if lines else None


def _fit_draft_title(draft: dict, identification: dict | None) -> None:
    """Validate the parsed title and bring it within 80 characters in place:
    one re-ask of the model, then the deterministic fit_title. Never truncates
    off the end, which is where size and colour live."""
    specifics = draft.get("item_specifics") or {}
    original = draft.get("title") or ""
    # "<Brand> Apple Watch Band" -> "<Brand> Band for Apple Watch", before the
    # fit (it adds a word) and again after it, since a model refit may undo it.
    enforced = enforce_band_for_device(original, identification, specifics)
    if enforced != " ".join(original.split()):
        print(f"⌚ band title: {original!r} -> {enforced!r}")
    fitted, dropped, how = ensure_title_fits(
        enforced, identification, specifics,
        refit=lambda t: _refit_title(t, identification, specifics))
    again = enforce_band_for_device(fitted, identification, specifics)
    if again != fitted:
        fitted, more = fit_title(again, identification, specifics)
        dropped, how = dropped + more, how + "+band"
    if how != "ok":
        print(f"✂️ title was {len(original)} chars; {how} -> {len(fitted)}: {fitted!r}"
              f" (dropped: {', '.join(dropped) or 'nothing'})")
    draft["title"] = fitted


_GTIN_KEYS = {"upc", "ean", "gtin", "isbn"}


def _clean_gtins(specifics: dict, label_upc=None, keep_valid: bool = False) -> None:
    """Hold the UPC/EAN item specifics to a manufacturer barcode, in place.

    The Ross tag's 400xxxxxxxxx item code reached the UPC field of 8 live
    listings (5fdc486f, 8c4260b4, 179def75, fe5be314, f68133f6, c57cb4cc,
    db2380ba, e5756e70): identification carried it as `upc`, and the copywriter,
    told to fill UPC "from Google Search", copied it. A UPC is only worth
    sending when it was read off the product's own barcode/label, so on a fresh
    draft it comes from identification (`label_upc`) and nowhere else; on a
    revision (`keep_valid`) a value already there survives only if it is a real
    manufacturer GTIN (pipeline/gtin.py)."""
    for key in [k for k in specifics if k.strip().lower() in _GTIN_KEYS]:
        value = specifics.pop(key)
        if keep_valid and product_gtin(value):
            specifics[key] = product_gtin(value)
        elif value not in (None, "", "null"):
            print(f"⚠️ dropping {key} {value!r}: not a manufacturer UPC from the label")
    upc = product_gtin(label_upc)
    if upc and not any(k.strip().lower() in _GTIN_KEYS for k in specifics):
        specifics["UPC"] = upc


def generate_draft(identification: dict, pricing: dict, examples: list | None = None) -> dict:
    suggested = pricing.get("suggested_price")
    reference = pricing.get("reference")
    stock_url = pricing.get("stock_image_url")

    parts = [
        "ITEM IDENTIFICATION:",
        json.dumps(identification, indent=2),
        "",
        "PRICING DATA:",
        f"Suggested listing price: ${suggested}",
        f"Sold median: ${pricing.get('sold_median')}",
        f"Active floor (cheapest competitor): ${pricing.get('active_floor')}",
        f"Pricing confidence: {pricing.get('confidence')}",
        "",
    ]

    if examples:
        parts.append("EXAMPLES OF GOOD LISTINGS FROM OUR STORE:")
        parts.append(json.dumps(examples, indent=2))
        parts.append("")

    if reference:
        parts.append(
            "MOST SIMILAR SUCCESSFUL EBAY LISTING (reference for structure, "
            "item specifics, and keywords ONLY — DO NOT copy any text verbatim, "
            "rewrite everything in your own words):"
        )
        parts.append(json.dumps(reference, indent=2))
        parts.append("")

    print("  draft: fetching eBay aspect hints...", flush=True)
    category = _choose_category(identification)
    aspect_hints = _aspect_hint_block(category)
    if aspect_hints:
        parts.append(aspect_hints)
        parts.append("")

    parts.append(SCHEMA_INSTRUCTIONS)
    user_message = "\n".join(parts)
    print(f"  draft: calling {MODEL}...", flush=True)

    # No google_search here: identification already did the grounded research and
    # passed its findings (specifications, aspect hints) into the prompt above. A
    # second grounded call burns the small free-tier grounding quota and stalls the
    # draft step on 429 retries. thinking_budget caps reasoning tokens so the schema
    # JSON always has room within max_output_tokens.
    response = generate_with_retry(
        client, stage="draft",
        model=MODEL,
        contents=user_message,
        config=types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            temperature=0,
            max_output_tokens=8000,
            thinking_config=types.ThinkingConfig(thinking_budget=1024),
        ),
    )

    print("  draft: got response, parsing...", flush=True)
    raw = response_text(response, "draft generation")
    # Remove markdown code fences
    if "```" in raw:
        parts = raw.split("```")
        for part in parts:
            cleaned = part.lstrip("json").strip()
            if cleaned.startswith("{"):
                raw = cleaned
                break
    # Find the first { and last } and extract just the JSON
    start = raw.find("{")
    end = raw.rfind("}") + 1
    if start != -1 and end > start:
        raw = raw[start:end]

    try:
        draft = json.loads(raw)
    except json.JSONDecodeError:
        raise ValueError(raw)

    draft["item_specifics"] = draft.get("item_specifics") or {}
    _clean_gtins(draft["item_specifics"], identification.get("upc"))

    _fit_draft_title(draft, identification)
    _enforce_category(draft, identification, category)
    _clean_draft_specifics(draft, identification)

    _guard_description(draft, identification)
    draft["description"] =(draft["description"] + "\n\n"
                            + store_boilerplate(draft.get("condition_id")))
    draft["stock_image_url"] = stock_url

    # The price is decided by pipeline/price.py (or by you at the gate), never by
    # the copywriter. The prompt asks it to echo suggested_price exactly and it
    # mostly does — but on 6 of 64 real listings it wrote its own number instead,
    # from -22% to +71% off, and that number is what went live. An LLM's aside is
    # not a pricing decision, so the value is overwritten here rather than
    # trusted.
    if suggested is not None:
        model_price = draft.get("price")
        try:
            drifted = model_price is None or round(float(model_price), 2) != round(float(suggested), 2)
        except (TypeError, ValueError):
            drifted = True
        if drifted:
            print(f"⚠️ draft model priced at {model_price}, overriding with {suggested}")
        draft["price"] = suggested
    return draft


def revise_draft(current_draft: dict, correction: str,
                 identification: dict | None = None) -> dict:
    system = (
        "You revise eBay listings based on a user's correction. "
        "Return the FULL updated listing as valid JSON only, no markdown. "
        "Apply the correction and keep everything else the same. "
        "Preserve the SHIPPING, RETURNS, and ABOUT US sections of the "
        "description exactly as they are. "
        "If the title changes: " + TITLE_RULES
    )
    user_message = (
        "CURRENT LISTING:\n" + json.dumps(current_draft, indent=2) +
        "\n\nCORRECTION REQUESTED:\n" + correction +
        "\n\nReturn the full updated listing as JSON matching the same schema."
    )
    response = generate_with_retry(
        client, stage="draft_revise",
        model=MODEL,
        contents=user_message,
        config=types.GenerateContentConfig(
            system_instruction=system,
            temperature=0,
            max_output_tokens=8000,
            thinking_config=types.ThinkingConfig(thinking_budget=1024),
        ),
    )
    raw = response_text(response, "draft revision")
    if "```" in raw:
        for part in raw.split("```"):
            cleaned = part.lstrip("json").strip()
            if cleaned.startswith("{"):
                raw = cleaned
                break
    start = raw.find("{")
    end = raw.rfind("}") + 1
    if start != -1 and end > start:
        raw = raw[start:end]
    try:
        revised = json.loads(raw)
    except json.JSONDecodeError:
        raise ValueError(raw)

    # A correction that lengthens the title ("add the colour") is exactly how a
    # title goes over 80, so the revision is held to the same limit.
    _fit_draft_title(revised, identification)
    _guard_description(revised, identification)

    # A copy edit keeps the category unless it asks about the category: the
    # model otherwise re-picks the number freehand on every revision.
    if "categor" not in correction.lower() and current_draft.get("category_id"):
        revised["category_id"] = current_draft["category_id"]
    if isinstance(revised.get("item_specifics"), dict):
        _clean_gtins(revised["item_specifics"], keep_valid=True)

    # Same rule as generate_draft: a copy edit must not move the price. Rewording
    # a title should never quietly reprice a live listing — price changes go
    # through the price gate or /setprice, which record what changed and check
    # the margin floor.
    current_price = current_draft.get("price")
    if current_price is not None and revised.get("price") != current_price:
        # Drift, not intent: a message that actually asks for a price is handled
        # before this is ever called (telegram_bot._price_from_correction), so
        # anything reaching here is the model repricing a copy edit on its own.
        print(f"⚠️ copy edit tried to reprice {current_price} -> {revised.get('price')}; "
              f"keeping {current_price} (say 'price 159.99' to change it deliberately)")
        revised["price"] = current_price
    return revised


def revise_identification(current: dict, correction: str) -> dict:
    """Apply a user's free-text correction to an identification dict (e.g. 'brand
    is Tommy Jeans, color navy'). Returns the FULL updated identification. Keeps
    search_query consistent with any brand/product/color change so the follow-on
    pricing step searches the corrected item."""
    system = (
        "You correct a product identification JSON based on a user's note. "
        "Return the FULL updated object as valid JSON only, no markdown. Apply the "
        "correction, keep every other field the same, and use null for unknowns. "
        "If the correction changes the brand, product_name, color, or item_type, "
        "update search_query to match (search_query includes the brand)."
    )
    user_message = (
        "CURRENT IDENTIFICATION:\n" + json.dumps(current, indent=2) +
        "\n\nCORRECTION FROM THE USER:\n" + correction +
        "\n\nReturn the full updated identification as JSON with the same fields."
    )
    response = generate_with_retry(
        client, stage="identification_revise",
        model=MODEL,
        contents=user_message,
        config=types.GenerateContentConfig(
            system_instruction=system,
            temperature=0,
            max_output_tokens=8000,
            thinking_config=types.ThinkingConfig(thinking_budget=1024),
        ),
    )
    raw = response_text(response, "identification revision")
    if "```" in raw:
        for part in raw.split("```"):
            cleaned = part.lstrip("json").strip()
            if cleaned.startswith("{"):
                raw = cleaned
                break
    start = raw.find("{")
    end = raw.rfind("}") + 1
    if start != -1 and end > start:
        raw = raw[start:end]
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        raise ValueError(raw)


if __name__ == "__main__":
    test_identification = {
        "brand": "The North Face",
        "item_type": "blanket",
        "model": "Wawona Blanket",
        "color": "Orange/Black",
        "size": "72 x 56 in",
        "material": "Polyester",
        "condition": "New with tags",
        "condition_flags": [],
        "search_query": "The North Face Wawona Blanket Orange Black",
        "confidence": 0.95,
    }
    test_pricing = {
        "suggested_price": 51.99,
        "sold_median": 70.00,
        "active_floor": 55.00,
        "confidence": "low",
    }
    draft = generate_draft(test_identification, test_pricing)
    print(json.dumps(draft, indent=2))
    title = draft.get("title", "")
    print(f"\nTitle length: {len(title)} chars")