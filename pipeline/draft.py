import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import json
import os
from dotenv import load_dotenv
from google.genai import types

from ebay.taxonomy import category_aspects, category_suggestions
from llm import generate_with_retry, make_client, response_text

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

STORE_BOILERPLATE = """
SHIPPING:
Ships within 1 business day via USPS with tracking number.
Carefully packaged to ensure safe delivery.

RETURNS:
30-day return policy. Buyer pays return shipping.
Item must be in original condition with tags attached.

ABOUT US:
Thank you for visiting our store! We specialize in authentic brand-name items at competitive prices. All items are 100% authentic and exactly as described.
Questions? Feel free to message us - we typically respond within 24 hours!
"""

SCHEMA_INSTRUCTIONS = """Generate an eBay listing as a valid JSON object matching this exact schema:

{
  "title": "string, MAX 80 characters, eBay SEO optimized. Lead with brand + product_name (the marketing/retail name from identification), then size/color. Use product_name rather than item_type whenever it is present; fall back to item_type only if product_name is null.",
  "description": "string, concise and scannable: short bullet sections, honest about any flaws (see structure below)",
  "category_id": "string, best-fit eBay category ID for this item",
  "item_specifics": {
    "Brand": "string",
    "Model": "string or null",
    "Color": "string",
    "Size": "One Size if universal fit, otherwise the specific size from tag",
    "Material": "string or null",
    "Condition": "string",
    "MPN": "manufacturer part number from Google Search or null",
    "UPC": "UPC or EAN barcode number from Google Search or null"
  },
  "price": 0.0,
  "condition_id": "string"
}

Rules:
- title MUST be under 80 characters. Count carefully. Never use ALL CAPS.
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
UPC, a missing category aspect, care instructions). Do not overwrite a value from
identification.specifications with a generic catalog guess.

Rules for item_specifics:
- Fill every aspect you can confidently determine from the specifications;
  do not leave a field blank when the data clearly answers it.
- For colors, list the actual colors broken out (e.g. "Black, Red, White"), not a
  vague "Multicolor".
- Never invent condition details — only what the identification/photos report.
- Never list the same value twice in one aspect.

description: Keep it lightweight and scannable — bullets over paragraphs, no
marketing fluff. Use this structure:
- One short opening line summarizing the item (a single sentence, not a paragraph).
- SPECIFICATIONS: short bullet list — brand, color, material, size/dimensions.
- FEATURES: 3-5 one-line bullets with ✓ (concise, no filler).
- CONDITION: one short bullet on condition and any flags.
- INCLUDED: bullet list of exactly what ships."""


def _aspect_hint_block(identification: dict) -> str | None:
    """Best-effort guidance block listing every item specific eBay's catalog
    defines for a best-guess category, not just the required ones, so Gemini
    can fill in the ones buyers actually filter/search by. Never blocks draft
    generation — publish-time validation in ebay/inventory.py is the real
    safety net if this lookup fails or guesses the wrong category."""
    query = identification.get("search_query") or identification.get("item_type")
    if not query:
        return None

    try:
        suggestions = category_suggestions(query)
        if not suggestions:
            return None
        category_id = (suggestions[0].get("category") or {}).get("categoryId")
        if not category_id:
            return None
        aspects = category_aspects(category_id)
    except Exception:
        return None

    if not aspects:
        return None

    lines = [
        f"EBAY ITEM SPECIFICS FOR THIS CATEGORY (best-guess category {category_id} — "
        "use a different category_id yourself if you find a better fit; this list is "
        "guidance, not a constraint on that choice):",
        "Buyers filter and search by these. Fill in every REQUIRED and RECOMMENDED one "
        "with a confident, accurate value from the photos or your search. Fill OPTIONAL "
        "ones only if you're confident — never guess. Add everything you can to "
        "item_specifics, beyond the fields already listed in the schema below.",
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
    aspect_hints = _aspect_hint_block(identification)
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
        client,
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

    draft["description"] = draft["description"] + "\n\n" + STORE_BOILERPLATE.strip()
    draft["stock_image_url"] = stock_url
    return draft


def revise_draft(current_draft: dict, correction: str) -> dict:
    system = (
        "You revise eBay listings based on a user's correction. "
        "Return the FULL updated listing as valid JSON only, no markdown. "
        "Apply the correction and keep everything else the same. "
        "Preserve the SHIPPING, RETURNS, and ABOUT US sections of the "
        "description exactly as they are."
    )
    user_message = (
        "CURRENT LISTING:\n" + json.dumps(current_draft, indent=2) +
        "\n\nCORRECTION REQUESTED:\n" + correction +
        "\n\nReturn the full updated listing as JSON matching the same schema."
    )
    response = generate_with_retry(
        client,
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
        return json.loads(raw)
    except json.JSONDecodeError:
        raise ValueError(raw)


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
        client,
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