"""Step 2 of the pipeline: identify a product from its photos.

identify_item runs a two-stage Gemini call (grounded research, then structuring
into our JSON schema) and is the function the rest of the app imports. Run this
module directly to identify a set of photos from the command line.
"""
import json
import sys
from pathlib import Path

from dotenv import load_dotenv
from google.genai import types

from llm import generate_with_retry, make_client, response_text
from config import GEMINI_MODEL, GEMINI_FAST_MODEL

load_dotenv()

client = make_client()


def _repair_json(raw: str) -> str:
    """Best-effort repair for a truncated JSON object: close any string left
    open at the point of truncation, then close any unmatched braces/brackets
    in the order they were opened."""
    stack = []
    in_string = False
    escape = False
    for ch in raw:
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch in "{[":
            stack.append(ch)
        elif ch in "}]":
            if stack and stack[-1] == ("{" if ch == "}" else "["):
                stack.pop()

    repaired = raw
    if in_string:
        repaired += '"'
    else:
        # truncation can land right after a trailing comma (next key/item
        # never started) — strip it so the closers below produce valid JSON
        repaired = repaired.rstrip()
        if repaired.endswith(","):
            repaired = repaired[:-1]
    closers = {"{": "}", "[": "]"}
    repaired += "".join(closers[ch] for ch in reversed(stack))
    return repaired


# --- Two-stage identification -------------------------------------------------
# Stage 1 lets Gemini do what it does in the chat app: think, search the web/eBay
# for the SKU, and read results before concluding. The old single-call approach
# muzzled this by demanding "JSON only, no explanation", which short-circuits the
# search/reasoning loop and yields generic guesses ("Canvas Tote Crossbody Bag")
# plus useless price data. Stage 2 just structures the findings into our schema.

RESEARCH_SYSTEM = (
    "You are a product researcher for an eBay reseller. You are given photos of "
    "an item bought at Ross Dress for Less, usually including a tag with a brand, "
    "a style/SKU number, a UPC barcode number, and a color code. Your job is to "
    "figure out exactly what the product is and what it sells for.\n"
    "The single most reliable key is the UPC barcode number: it is globally unique, "
    "so cross-referencing it pulls up the exact product name even when a bare style "
    "number does not. Always read and search the UPC first. Style numbers for "
    "discount-retail collections (items sent to Ross) often don't surface in a plain "
    "Google search, so combine the tag codes with the visual details of the item to "
    "pin the exact product.\n"
    "Use Google Search aggressively: search the UPC, the style/SKU number, the "
    "brand, and the visible features on eBay (both sold and active listings), on the "
    "brand's own website, and on resale sites. Run several searches, read the "
    "results, and reason step by step before you conclude. Do NOT answer in JSON — "
    "write a short plain-text findings report."
)

RESEARCH_PROMPT = (
    "Identify this exact item and its market price.\n"
    "\n"
    "Do this:\n"
    "1. Read EVERY code printed on the tag/photos, exactly as written:\n"
    "   - UPC / barcode number (the long digit string, e.g. '1 98928 67569 8')\n"
    "   - style / SKU number (e.g. 'XW08963')\n"
    "   - color code (often a 3-digit suffix after the style number, e.g. '971' = a "
    "specific color combo)\n"
    "   - size, material, and any printed retail/original price.\n"
    "2. Look it up, UPC first. Search the UPC barcode number against product "
    "databases to get the exact product name. Then search the style number + color "
    "code, then the brand + visual features on eBay. Determine the real product "
    "line / marketing name buyers actually use (e.g. 'Large Summer Spring Tote "
    "Crossbody', 'Shopper Tote') — never settle for a generic description like "
    "'Canvas Tote Bag' if a real product name exists. Combine the tag codes with "
    "the visible design (shape, trim color, straps) to confirm the match.\n"
    "3. Find price evidence: the original retail/MSRP, and what the item actually "
    "sells for on eBay (sold and active) in this condition. Cite the prices you find.\n"
    "4. Copy the FULL specification / item-specifics list from the matched product "
    "page (the brand or retailer page for this EXACT product, ideally found via the "
    "UPC). Capture everything a buyer would filter by: dimensions (height/width/depth), "
    "weight, materials, ALL features (pockets, luggage back-band, crossbody strap, "
    "etc.), pattern/motifs (e.g. gingham, cherries, bows), hardware color, lining/"
    "interior color, strap color(s), the exterior colors broken out (e.g. Black, Red, "
    "White — not a vague 'Multicolor'), finish (e.g. quilted), closure, and Country of "
    "Origin. This matched spec sheet — not guesswork — is the backbone of the listing.\n"
    "5. Judge condition from the photos (New with tags / New without tags / used) "
    "and note any visible defects, stains, or missing tags. Then VERIFY the matched "
    "spec sheet against the photos: confirm the actual colorway, hardware color, and "
    "pattern of THIS unit, and correct any spec that doesn't match what you see. The "
    "photos win on visible attributes; the matched page wins on specs you can't see "
    "(dimensions, country of origin, materials).\n"
    "6. Find a direct link to an OFFICIAL product photo — from the brand's own "
    "website or a major retailer (Macy's, Nordstrom, etc.), matching this exact "
    "color/variant. Give the direct image-file URL (ending in .jpg/.png/.webp) if "
    "you can. Do NOT use another eBay/Poshmark/Mercari seller's photo.\n"
    "\n"
    "Then write a findings report with these labelled lines:\n"
    "- Brand / Item type / SKU-Style number / UPC / Color code / Product line name / "
    "Color / Size / Material\n"
    "- Specifications (every item-specific from the matched product: dimensions, "
    "materials, features, pattern, hardware color, lining color, strap color, exterior "
    "colors broken out, finish, closure, Country of Origin — list as Name: value pairs)\n"
    "- Photo-verified corrections (anything about THIS unit's colorway/hardware/"
    "condition that differs from the generic matched listing)\n"
    "- Condition and any defects\n"
    "- Retail / MSRP price (with source)\n"
    "- eBay resale prices found (list each: price + sold or active)\n"
    "- Best estimate of what it sells for on eBay in this condition\n"
    "- Official product image URL (brand/retailer only, direct image link) or none\n"
    "- A short eBay search string a buyer would use to find this exact item"
)

FORMAT_SYSTEM = (
    "You convert a product research report into a single strict JSON object. "
    "Respond with valid JSON only — no markdown, no explanation. Use only facts "
    "stated in the report and visible in the photos; use null when unknown."
)

FORMAT_INSTRUCTIONS = (
    "Convert the research report below into JSON matching this EXACT schema:\n"
    "\n"
    "{\n"
    '  "brand": "brand name or null",\n'
    '  "item_type": "e.g. handbag, sneakers, blanket, blouse",\n'
    '  "model": "style/SKU number if known or null",\n'
    '  "upc": "UPC/barcode number read from the tag (digits only) or null",\n'
    '  "product_name": "the real product-line / marketing name buyers search for, '
    "WITHOUT the brand prefix, e.g. 'Shopper Tote', 'Wawona Blanket', 'Wayfarer'. "
    'null only if truly unknown",\n'
    '  "color": "primary color(s)",\n'
    '  "size": "from tag or null",\n'
    '  "material": "or null",\n'
    '  "condition": "New with tags|New without tags|Like new|Good|Fair",\n'
    '  "condition_flags": ["visible defects, missing tags, stains - empty array if none"],\n'
    '  "search_query": "optimized eBay search string: brand + product_name + key attributes",\n'
    '  "retail_price": original retail/MSRP as a plain number, or null,\n'
    '  "resale_estimate": realistic eBay selling price in this condition as a plain number, or null,\n'
    '  "price_evidence": [{"source": "ebay sold|ebay active|tag|msrp", "price": number, "note": "short note"}],\n'
    '  "specifications": {"<item-specific name>": "<value>", "...": "... every spec from the matched product + photo-verified visual details, e.g. Hardware Color, Lining Color, Strap Color, Exterior Color, Pattern, Finish, Closure, Features, Country of Origin, Bag Height, Bag Width, Bag Depth ..."},\n'
    '  "product_image_url": "direct URL to an OFFICIAL brand/retailer product photo (https, ideally ending .jpg/.png/.webp), or null. Never an eBay/Poshmark/Mercari seller photo",\n'
    '  "confidence": 0.0\n'
    "}\n"
    "\n"
    "Rules:\n"
    "- product_name must be the real product-line name from the report, never a "
    "generic 'Canvas Tote Bag', and never prefixed with the brand name.\n"
    "- search_query DOES include the brand.\n"
    "- All prices are plain numbers (no $ or commas). Use null, not 0, when unknown.\n"
    "- price_evidence: include every concrete price the report cites; empty array if none.\n"
    "- specifications: copy every Name: value pair from the report's Specifications "
    "line, plus the photo-verified corrections (the corrected value wins). Use real "
    "eBay item-specific names as keys (Hardware Color, Lining Color, Strap Color, "
    "Exterior Color, Pattern, Finish, Closure, Features, Country of Origin, Bag "
    "Height, etc.). Empty object if the report has none.\n"
    "- product_image_url: only an official brand/retailer image URL from the report; "
    "null if the report has none or only cites marketplace-seller photos.\n"
    "- confidence 0.0-1.0; lower it if the report could not pin the product or price.\n"
    "- Return raw JSON only, no markdown fences.\n"
    "\n"
    "RESEARCH REPORT:\n"
)


def _parse_json_object(raw: str) -> dict:
    """Strip any markdown fences, isolate the outermost JSON object, and parse it,
    repairing a truncated tail if needed."""
    raw = (raw or "").strip()
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
        return json.loads(_repair_json(raw))


def identify_item(image_paths: list[str]) -> dict:
    image_parts = [
        types.Part.from_bytes(data=Path(p).read_bytes(), mime_type="image/jpeg")
        for p in image_paths
    ]

    # Stage 1 — free-form grounded research (think + search the web/eBay).
    research = generate_with_retry(
        client,
        model=GEMINI_MODEL,
        contents=[*image_parts, types.Part.from_text(text=RESEARCH_PROMPT)],
        config=types.GenerateContentConfig(
            system_instruction=RESEARCH_SYSTEM,
            temperature=0,
            # flash is a thinking model with dynamic thinking on by default, and
            # those tokens count against this budget — give the findings report
            # enough headroom that thinking can't truncate it.
            max_output_tokens=8000,
            tools=[types.Tool(google_search=types.GoogleSearch())],
        ),
    )
    report = (research.text or "").strip()
    queries = []
    if research.candidates:
        gm = getattr(research.candidates[0], "grounding_metadata", None)
        queries = list(getattr(gm, "web_search_queries", None) or [])
    if queries:
        print(f"Research ran {len(queries)} search(es): {', '.join(queries[:6])}"
              + (" ..." if len(queries) > 6 else ""))
    if not report:
        report = "(No research text was returned; identify from the photos alone.)"

    # Stage 2 — structure the findings into our schema on the fast model (no tools,
    # no photos: everything visual was already captured into the report in stage 1,
    # so we don't re-send the images and pay for them again). thinking_budget caps
    # the reasoning tokens so the schema JSON always has room within max_output_tokens.
    formatted = generate_with_retry(
        client,
        model=GEMINI_FAST_MODEL,
        contents=types.Part.from_text(text=FORMAT_INSTRUCTIONS + report),
        config=types.GenerateContentConfig(
            system_instruction=FORMAT_SYSTEM,
            temperature=0,
            max_output_tokens=8000,
            thinking_config=types.ThinkingConfig(thinking_budget=1024),
        ),
    )

    raw_text = response_text(formatted, "format stage")
    try:
        result = _parse_json_object(raw_text)
    except (json.JSONDecodeError, ValueError):
        raise ValueError(raw_text)

    # Safety net: downstream pricing requires a search_query.
    if not result.get("search_query"):
        bits = [result.get("brand"), result.get("product_name") or result.get("item_type"),
                result.get("color")]
        result["search_query"] = " ".join(b for b in bits if b).strip()
    return result


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python identify.py photo1.jpg photo2.jpg photo3.jpg")
        sys.exit(1)

    image_paths = sys.argv[1:]
    missing = [p for p in image_paths if not Path(p).exists()]
    if missing:
        for p in missing:
            print(f"Error: file not found: {p}")
        sys.exit(1)

    print(json.dumps(identify_item(image_paths), indent=2))
