"""Step 2 of the pipeline: identify a product from its photos.

identify_item runs a two-stage Gemini call (grounded research, then structuring
into our JSON schema) and is the function the rest of the app imports. Run this
module directly to identify a set of photos from the command line.
"""
import json
import re
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
from google.genai import types

from llm import generate_with_retry, make_client, response_text
from config import GEMINI_MODEL, GEMINI_FAST_MODEL
from receipt import read_product_upcs, ocr_text

load_dotenv()

client = make_client()

# The grounded research call occasionally returns an empty response; retry a few
# times (with backoff between attempts) before giving up rather than falling
# through to a hallucinated guess.
RESEARCH_ATTEMPTS = 4


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
    "write a short plain-text findings report.\n"
    "CRITICAL: if the UPC and style-number searches do not actually confirm a "
    "specific product, say so plainly and report the item as UNCONFIRMED. Do NOT "
    "fall back to naming a generic popular product (e.g. a common Coach or Michael "
    "Kors bag) just because it's plausible — an unconfirmed honest answer is far "
    "more useful than a confident wrong one."
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
    "- If the report says the item is UNCONFIRMED / could not be verified, do NOT "
    "invent a specific brand or product_name: set product_name (and brand, if the "
    "tag brand wasn't legible) to null, keep item_type generic, and set confidence "
    "below 0.3. Never output a confident specific product the report didn't confirm.\n"
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


# A style/SKU-looking token: 5–14 chars, letters+digits mixed (e.g. 'XW08963',
# '87F2LEVB9U'). Pure-digit runs are excluded — they're covered by the UPC and a
# bare number is too noise-prone to trust from OCR.
_STYLE_CODE_RE = re.compile(r"\b(?=[A-Z0-9-]{5,14}\b)(?=[A-Z0-9-]*[A-Z])(?=[A-Z0-9-]*\d)[A-Z0-9-]+\b")


def _extract_style_codes(text: str) -> list[str]:
    """Pull style/SKU-looking tokens out of noisy tag OCR. Raw OCR of a tag is
    mostly garbage, so we forward only these high-signal codes, not the prose."""
    seen = []
    for m in _STYLE_CODE_RE.finditer(text.upper()):
        tok = m.group(0)
        if tok not in seen:
            seen.append(tok)
    return seen[:5]


def _tag_signals(scan_paths: list[str], fallback_ocr_path: str | None) -> tuple[list[str], list[str]]:
    """Read hard identifiers off the tag WITHOUT the vision model: decode any
    product UPC/EAN barcodes across all photos, and OCR the tag for printed
    style/SKU codes the barcode doesn't carry. The vision model reads long barcode
    digits unreliably and, when it can't, confidently hallucinates a generic
    popular product — feeding it the decoded UPC removes that failure mode."""
    upcs: list[str] = []
    upc_photo: str | None = None
    for p in scan_paths:
        for u in read_product_upcs(p):
            if u not in upcs:
                upcs.append(u)
                if upc_photo is None:
                    upc_photo = p

    # OCR the photo the UPC came from (almost always the tag); otherwise the
    # caller's best guess at the tag photo. One OCR call keeps latency down, and
    # we keep only style-code tokens — never the noisy raw text.
    style_codes: list[str] = []
    ocr_path = upc_photo or fallback_ocr_path
    if ocr_path:
        try:
            style_codes = _extract_style_codes(ocr_text(ocr_path) or "")
        except Exception as e:
            print(f"Tag OCR skipped: {type(e).__name__}: {e}")
    return upcs, style_codes


def _tag_data_block(upcs: list[str], style_codes: list[str]) -> str:
    """Prepended to the research prompt as authoritative, decoded tag facts."""
    if not upcs and not style_codes:
        return ""
    lines = []
    if upcs:
        lines.append("- Product UPC/EAN (decoded from the barcode): " + ", ".join(upcs))
    if style_codes:
        lines.append("- Possible style/SKU codes (OCR — may contain errors): "
                     + ", ".join(style_codes))
    return (
        "AUTHORITATIVE TAG DATA — decoded directly from the item's barcode/tag, so "
        "the UPC below is ground truth and beats anything you think you see in the photo:\n"
        + "\n".join(lines) + "\n"
        "Search the UPC digits FIRST (try them as-is, without a leading zero, and "
        "as a 12-digit UPC-A) to pull up the exact product. Only conclude the item "
        "is unconfirmed if these searches genuinely fail.\n\n"
    )


def identify_item(image_paths: list[str], scan_paths: list[str] | None = None) -> dict:
    image_parts = [
        types.Part.from_bytes(data=Path(p).read_bytes(), mime_type="image/jpeg")
        for p in image_paths
    ]

    # Decode the tag barcode / OCR the tag across ALL photos (barcode reading is
    # free and local), even though only these first few images go to the model.
    scan_paths = scan_paths or image_paths
    fallback_ocr = image_paths[1] if len(image_paths) >= 2 else (image_paths[0] if image_paths else None)
    upcs, style_codes = _tag_signals(scan_paths, fallback_ocr)
    if upcs:
        print(f"Tag barcode decoded UPC(s): {', '.join(upcs)}")
    research_prompt = _tag_data_block(upcs, style_codes) + RESEARCH_PROMPT

    # Stage 1 — free-form grounded research (think + search the web/eBay).
    # The grounded call intermittently returns an empty response (finish_reason
    # STOP, zero output tokens, zero searches) — a transient flake, not a token
    # limit. Left unhandled it silently yields no findings, and the format stage
    # then hallucinates a generic popular product from the photos (the recurring
    # "Coach Signature PVC Zip Tote" every time). So retry on an empty report.
    report = ""
    research = None
    for attempt in range(1, RESEARCH_ATTEMPTS + 1):
        research = generate_with_retry(
            client,
            model=GEMINI_MODEL,
            contents=[*image_parts, types.Part.from_text(text=research_prompt)],
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
        if report:
            break
        fr = research.candidates[0].finish_reason if research.candidates else None
        print(f"Research returned empty text (finish_reason={fr}); "
              f"retry {attempt}/{RESEARCH_ATTEMPTS}")
        # Back off between attempts so retries land in DIFFERENT time windows —
        # firing them back-to-back just re-hits the same bad moment on the
        # grounding service, so all of them come back empty together.
        if attempt < RESEARCH_ATTEMPTS:
            time.sleep(min(3 * attempt, 12))

    queries = []
    if research and research.candidates:
        gm = getattr(research.candidates[0], "grounding_metadata", None)
        queries = list(getattr(gm, "web_search_queries", None) or [])
    if queries:
        print(f"Research ran {len(queries)} search(es): {', '.join(queries[:6])}"
              + (" ..." if len(queries) > 6 else ""))
    if not report:
        # Still empty after retries. Do NOT invite the model to guess from the
        # photos — hand it the decoded tag data (if any) and an explicit
        # "unconfirmed" so it records the UPC and returns nulls, not a fake match.
        known = []
        if upcs:
            known.append("UPC " + ", ".join(upcs))
        if style_codes:
            known.append("style code(s) " + ", ".join(style_codes))
        detail = ("Decoded tag data: " + "; ".join(known) + ". ") if known else ""
        report = (f"(No research findings could be produced. {detail}"
                  "The item is UNCONFIRMED — do not guess a specific product.)")

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

    # The decoded barcode is exact — trust it over whatever digits the model read.
    if upcs:
        result["upc"] = upcs[0]

    # Safety net: downstream pricing requires a search_query.
    if not result.get("search_query"):
        bits = [result.get("brand"), result.get("product_name") or result.get("item_type"),
                result.get("color")]
        result["search_query"] = " ".join(b for b in bits if b).strip()
    # Last resort (unconfirmed item): search the exact UPC so pricing still has
    # something concrete to look up rather than an empty query.
    if not result.get("search_query") and result.get("upc"):
        result["search_query"] = result["upc"]
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
