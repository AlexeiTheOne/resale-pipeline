"""Read the Ross price tag that the user sends as the LAST photo of an item.

The tag carries the two things we care about: the price we actually paid (the
reduced Ross price) and a unique 12-digit item code. Both are encoded in the
tag's CODE128 barcode, which decodes far more reliably than OCR of tiny printed
digits — a single misread digit would make the code useless. So:

  • pyzbar decodes the barcode -> 12-digit code + reduced (paid) price.
    Ross barcodes are 18 digits: <12-digit code><6-digit price in cents>,
    e.g. 400286461425000999 -> code 400286461425, price $9.99.
  • Tesseract OCR reads the printed "Original $XX.XX" line (the retail/compare-at
    price, which is NOT in the barcode) and serves as a fallback for the code and
    paid price if the barcode can't be decoded.

The tag image itself never goes to eBay — only this extracted cost data is kept.
Nothing here may raise into the pipeline: `extract_receipt` always returns a
dict (with an "error" note if the toolchain is missing or both reads fail).
"""
import os
import re
from pathlib import Path

from dotenv import load_dotenv

try:
    import pytesseract
    from PIL import Image, ImageOps
except ImportError:  # deps not installed yet — extract_receipt degrades gracefully
    pytesseract = None
    Image = None
    ImageOps = None

try:
    from pyzbar.pyzbar import ZBarSymbol as _ZBarSymbol
    from pyzbar.pyzbar import decode as _zbar_decode
except ImportError:
    _zbar_decode = None
    _ZBarSymbol = None

# Restrict zbar to only the symbologies we actually read. Left unrestricted it
# runs every decoder — including the 2D PDF417/QR ones, whose C library spews
# "Assertion failed" warnings to stderr on ordinary product/tag photos and floods
# the console. These allowlists keep those decoders from ever running.
#   • product photos: globally-unique product barcodes (EAN/UPC)
#   • Ross tag: the store's CODE128 price/code barcode
_PRODUCT_ZBAR_SYMBOLS = (
    [_ZBarSymbol.EAN13, _ZBarSymbol.EAN8, _ZBarSymbol.UPCA, _ZBarSymbol.UPCE]
    if _ZBarSymbol else None
)
_TAG_ZBAR_SYMBOLS = [_ZBarSymbol.CODE128] if _ZBarSymbol else None

load_dotenv()

# On Windows the tesseract binary usually isn't on PATH; let .env point at it.
_TESSERACT_CMD = os.getenv("TESSERACT_CMD")
if _TESSERACT_CMD and pytesseract is not None:
    pytesseract.pytesseract.tesseract_cmd = _TESSERACT_CMD

# A money amount like 22.99, $ 9.99, or 1,299.00. OCR sometimes reads the decimal
# point as a comma, so accept either as the cents separator; require exactly two
# cents digits (and no third) so 12-digit codes never register as prices.
_PRICE_RE = re.compile(r"\$?\s*(\d[\d,]*)[.,](\d{2})(?!\d)")
# The 12-digit code, standalone (not part of a longer digit run).
_CODE_RE = re.compile(r"(?<!\d)(\d{12})(?!\d)")
# Lines that quote the retail/compare-at price rather than what we paid.
_ORIGINAL_HINT = re.compile(r"\b(original|compare|comp\s*at|retail|msrp|reg(?:ular)?)\b", re.I)
# Evidence that a photo is the ROSS tag rather than the manufacturer's label
# (see looks_like_tag). Both of these are deliberately narrow:
#
#   _ROSS_CODE_RE — every Ross item code observed starts 400 (26/26 across the
#     decoded-barcode history: 4002xxxxxxx and 4003xxxxxxx). Requiring the prefix
#     is what separates a Ross code from a manufacturer style number or a UPC —
#     a bare 12-digit match hit labels like "650753752001  M.S.R.P 185.00".
#   _TAG_TEXT — "REDUCED" is Ross's own wording. MSRP / retail / compare-at were
#     tried and are printed on manufacturer labels too, so they identify nothing.
_ROSS_CODE_RE = re.compile(r"(?<!\d)(400\d{9})(?!\d)")
_TAG_TEXT = re.compile(r"\breduc(?:ed)?\b", re.I)


# --- Frame brightness (haul separators) ---------------------------------------

# A deliberately dark frame — lens covered, or pointed at something black — used
# to mark the end of an item in a /haul. Measured across 401 real item photos the
# darkest was mean luminance 56, so 25 leaves better than a 2x margin: no real
# photo of merchandise comes close, and covering the lens lands near zero.
DARK_FRAME_MAX_LUMA = float(os.getenv("DARK_FRAME_MAX_LUMA", "25"))

# Ask the vision model to read a tag whose barcode won't decode and whose printed
# price OCR couldn't find. One cheap flash call, only on the hard tags. Set
# TAG_VISION_FALLBACK=false to go back to asking the user for /receipt instead.
TAG_VISION_FALLBACK = os.getenv("TAG_VISION_FALLBACK", "true").strip().lower() in (
    "1", "true", "yes", "on")


def mean_luma(image_path: str) -> float | None:
    """Average brightness of an image, 0-255, or None if it can't be read.
    Downsampled first — a 64px thumbnail settles this and avoids decoding a
    12-megapixel photo just to average it."""
    if Image is None:
        return None
    try:
        img = ImageOps.exif_transpose(Image.open(image_path)).convert("L")
        img.thumbnail((64, 64))
        pixels = list(img.getdata())
        return sum(pixels) / len(pixels) if pixels else None
    except Exception:
        return None


def is_dark_frame(image_path: str) -> bool:
    """Is this photo a deliberate blackout separator rather than merchandise?"""
    luma = mean_luma(image_path)
    return luma is not None and luma < DARK_FRAME_MAX_LUMA


# --- Is this photo the Ross tag? ----------------------------------------------

def looks_like_tag(image_path: str) -> str | None:
    """How this photo was recognised as the Ross tag, or None if it isn't one.

    Returns "barcode" / "code" / "text" so callers can report which evidence
    fired. Cheapest first, and each is checked against what is ACTUALLY printed
    on a Ross tag:

      barcode — the 18-digit CODE128. Definitive, but it decodes on well under
                half of real tag photos (glare, angle, Telegram's compression).
      code    — OCR finds the Ross item code (12 digits, 400-prefixed). The
                workhorse: printed large, it survives OCR long after the barcode
                stops decoding.
      text    — OCR finds Ross's "REDUCED" wording next to a money amount.

    Two things are deliberately NOT used, both because they misfire on real
    merchandise: the word "Ross" (these tags don't print it at all), and a bare
    12-digit number or MSRP/retail wording (that is what a manufacturer's label
    looks like — matching it peeled genuine product photos off listings)."""
    digits = decode_barcode(image_path)
    if digits and len(digits) == 18:
        return "barcode"
    try:
        text = ocr_text(image_path) or ""
    except Exception:
        return None
    if _ROSS_CODE_RE.search(text):
        return "code"
    if _TAG_TEXT.search(text) and _PRICE_RE.search(text):
        return "text"
    return None


# --- Barcode (primary) --------------------------------------------------------

def decode_barcode(image_path: str) -> str | None:
    """Digits of the tag's CODE128 barcode, or None. Tries a couple of renderings
    since a glare/angle that defeats one often decodes on another."""
    if _zbar_decode is None or Image is None:
        return None
    try:
        img = ImageOps.exif_transpose(Image.open(image_path))
    except Exception:
        return None
    renderings = [img, ImageOps.grayscale(img),
                  img.resize((img.size[0] * 2, img.size[1] * 2))]
    for im in renderings:
        try:
            for res in _zbar_decode(im, symbols=_TAG_ZBAR_SYMBOLS):
                digits = re.sub(r"\D", "", res.data.decode("utf-8", "ignore"))
                if len(digits) >= 12:
                    return digits
        except Exception:
            continue
    return None


# Symbologies that carry a globally-unique product UPC/EAN (so they identify the
# product). A Ross tag's CODE128 is the store's own 18-digit price code — not the
# product — so it's deliberately excluded here.
_PRODUCT_SYMBOLS = {"EAN13", "EAN8", "UPCA", "UPCE"}


def read_product_upcs(image_path: str) -> list[str]:
    """Decode product UPC/EAN barcodes from a photo, returning the digit strings
    (deduped, in decode order). Best-effort: [] if none decode or pyzbar/Pillow
    aren't installed. Excludes the Ross CODE128 store code and empty QR codes so
    only real product identifiers come back."""
    if _zbar_decode is None or Image is None:
        return []
    try:
        img = ImageOps.exif_transpose(Image.open(image_path))
    except Exception:
        return []
    found = []
    for im in (img, ImageOps.grayscale(img),
               img.resize((img.size[0] * 2, img.size[1] * 2))):
        try:
            results = _zbar_decode(im, symbols=_PRODUCT_ZBAR_SYMBOLS)
        except Exception:
            continue
        for res in results:
            if res.type not in _PRODUCT_SYMBOLS:
                continue
            digits = re.sub(r"\D", "", res.data.decode("utf-8", "ignore"))
            if len(digits) >= 8 and digits not in found:
                found.append(digits)
    return found


def parse_barcode(digits: str | None) -> dict:
    """Split a Ross barcode into code + paid price. The 18-digit form is
    <12-digit code><6-digit price in cents>; a bare 12-digit form is code only."""
    if not digits:
        return {"code": None, "reduced_price": None}
    if len(digits) == 18:
        return {"code": digits[:12], "reduced_price": int(digits[12:]) / 100}
    if len(digits) == 12:
        return {"code": digits, "reduced_price": None}
    # Unexpected length: keep the leading 12 as the code, don't guess a price.
    return {"code": digits[:12], "reduced_price": None}


# --- OCR (original price + fallback) ------------------------------------------

def _require_ocr():
    if pytesseract is None or Image is None:
        raise RuntimeError(
            "pytesseract/Pillow not installed. Run `pip install pytesseract Pillow` "
            "and install the Tesseract binary (set TESSERACT_CMD in .env if it's not on PATH)."
        )


def ocr_text(image_path: str) -> str:
    """Raw OCR text from the tag image. Raises if the toolchain is missing."""
    _require_ocr()
    img = ImageOps.exif_transpose(Image.open(image_path))
    img = ImageOps.grayscale(img)
    img = img.resize((img.size[0] * 3, img.size[1] * 3))
    img = ImageOps.autocontrast(img)
    return pytesseract.image_to_string(img, config="--psm 6")


def ocr_variants(image_path: str):
    """Yield (label, text) for several renderings of the tag, best-guess first.

    One rendering is not enough. Tags get photographed at any angle — several in
    the real library are a full 180 degrees round, which is why OCR of them came
    back as "3zISSNO" ("ONE SIZE" reversed) — and the aggressive upscale + psm 6
    that helps a flat, upright tag turns an angled one into noise. Measured over
    the 33 tags whose barcode won't decode, that single rendering recovered a
    price or code on ZERO of them, while trying these variants recovers 7.

    Ordered cheapest and most-likely-correct first so callers can stop early;
    only reached when the barcode has already failed, so the cost is paid on the
    hard cases only."""
    _require_ocr()
    img = ImageOps.exif_transpose(Image.open(image_path))
    gray = ImageOps.autocontrast(ImageOps.grayscale(img).resize(
        (img.size[0] * 2, img.size[1] * 2)))

    yield "plain", pytesseract.image_to_string(img)
    yield "gray2x", pytesseract.image_to_string(gray)
    for deg in (180, 90, 270):
        yield f"rot{deg}", pytesseract.image_to_string(img.rotate(deg, expand=True))
        yield f"gray2x_rot{deg}", pytesseract.image_to_string(gray.rotate(deg, expand=True))
    # The original single-shot rendering, kept last: it still wins on a clean,
    # flat, upright tag where the 3x upscale sharpens small print.
    big = ImageOps.autocontrast(ImageOps.grayscale(img).resize(
        (img.size[0] * 3, img.size[1] * 3)))
    yield "gray3x_psm6", pytesseract.image_to_string(big, config="--psm 6")


def _price(int_part: str, cents: str) -> float:
    return int(int_part.replace(",", "")) + int(cents) / 100


def _price_on_line(line: str) -> float | None:
    m = _PRICE_RE.search(line)
    return _price(m.group(1), m.group(2)) if m else None


def _all_prices(text: str) -> list[float]:
    return [_price(m.group(1), m.group(2)) for m in _PRICE_RE.finditer(text)]


def parse_receipt(text: str) -> dict:
    """Pull the original (retail) price, and — as a barcode fallback — the reduced
    price and 12-digit code out of the OCR text. Engine-independent."""
    original_price = None
    for line in text.splitlines():
        if _ORIGINAL_HINT.search(line):
            p = _price_on_line(line)
            if p is not None:
                original_price = p
                break

    prices = _all_prices(text)
    # Reduced (paid) price: the best candidate that isn't the original — the
    # highest price strictly below it (above tax/change lines); fall back to the
    # highest non-original price, then the single price found.
    reduced_price = None
    if original_price is not None:
        below = [p for p in prices if p < original_price]
        others = [p for p in prices if p != original_price]
        reduced_price = max(below) if below else (max(others) if others else None)
    elif prices:
        reduced_price = max(prices)

    m = _CODE_RE.search(text)
    code = m.group(1) if m else None
    if code is None:  # OCR often splits the digits with spaces — retry per line
        for line in text.splitlines():
            m = _CODE_RE.search(re.sub(r"\s+", "", line))
            if m:
                code = m.group(1)
                break

    return {"original_price": original_price, "reduced_price": reduced_price, "code": code}


# --- Public entry point -------------------------------------------------------

# --- Vision fallback ----------------------------------------------------------

_VISION_PROMPT = (
    "This is a photo of a Ross Dress for Less store price tag. There may be two "
    "tags overlapping at different rotations, and the photo may be upside down.\n"
    "Read it and return ONLY JSON:\n"
    '{"reduced_price": <the price the customer pays - the large price, or the one '
    'printed under REDUCED, or null>,\n'
    ' "original_price": <the number after "Original $" or "Comparable Value", or null>,\n'
    ' "code": "<the 12-digit number starting 400, or null>"}\n'
    "Use null for anything not clearly legible. Do not guess or compute values."
)


def vision_read_tag(image_path: str) -> dict:
    """Read the tag with the vision model as a last resort.

    Reached only when the barcode won't decode and OCR couldn't find the paid
    price. Tesseract fails on the common real-world tag: two stickers overlapping
    at 180 degrees to each other, the barcode tilted and creased, the whole thing
    JPEG-compressed by Telegram. That is ordinary reading for a vision model.

    Returns {} on any failure — this is a best-effort improvement on "ask the user
    to type it", and must never break the pipeline. Imports are local so receipt.py
    still runs standalone without the Gemini SDK installed."""
    try:
        import json as _json

        from google.genai import types

        from config import GEMINI_FAST_MODEL
        from llm import make_client, generate_with_retry, response_text

        part = types.Part.from_bytes(data=Path(image_path).read_bytes(),
                                     mime_type="image/jpeg")
        response = generate_with_retry(
            make_client(), model=GEMINI_FAST_MODEL,
            contents=[part, types.Part.from_text(text=_VISION_PROMPT)])
        text = re.sub(r"^```(?:json)?|```$", "", response_text(response, "tag").strip(),
                      flags=re.M).strip()
        data = _json.loads(text)
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}

    out = {}
    for field in ("reduced_price", "original_price"):
        value = data.get(field)
        # The model returns these as a number on some calls and a quoted string
        # ("15.99") on others, so accept both rather than silently dropping half
        # the results.
        if isinstance(value, str):
            value = value.replace("$", "").replace(",", "").strip()
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if 0 < number <= 10000:
            out[field] = round(number, 2)
    code = str(data.get("code") or "").strip()
    # Hold it to the same shape a real Ross code has, so a hallucinated or
    # misread value can't enter the cost record.
    if _CODE_RE.fullmatch(code) and code.startswith("400"):
        out["code"] = code
    return out


def extract_receipt(image_path: str) -> dict:
    """Read a Ross tag into cost data: the paid price + 12-digit code (from the
    barcode, with OCR fallback) and the original price (from OCR). Never raises:
    returns a dict with an "error" note if nothing could be read, so a bad tag
    read can't abort the listing pipeline."""
    barcode = decode_barcode(image_path)
    bc = parse_barcode(barcode)

    ocr = {"original_price": None, "reduced_price": None, "code": None}
    raw_text = ""
    ocr_error = None
    ocr_variant = None
    if barcode and bc["reduced_price"] is not None:
        # The barcode already gave code + paid price exactly. OCR is then only
        # wanted for the printed "Original" line, so one cheap pass will do.
        try:
            raw_text = ocr_text(image_path)
            ocr = parse_receipt(raw_text)
            ocr_variant = "gray3x_psm6"
        except Exception as e:
            ocr_error = f"{type(e).__name__}: {e}"
    else:
        # No usable barcode: this is the hard case, so work through the
        # renderings and keep the best parse. "Best" means the most fields
        # recovered, preferring the paid price — that's the number the item's
        # whole profit calculation rests on.
        try:
            best_score = -1
            for label, text in ocr_variants(image_path):
                parsed = parse_receipt(text)
                score = ((2 if parsed.get("reduced_price") is not None else 0)
                         + (1 if parsed.get("code") else 0)
                         + (1 if parsed.get("original_price") is not None else 0))
                if score > best_score:
                    best_score, ocr, raw_text, ocr_variant = score, parsed, text, label
                if parsed.get("reduced_price") is not None and parsed.get("code"):
                    break  # nothing better to find
        except Exception as e:
            ocr_error = f"{type(e).__name__}: {e}"

    # Barcode wins for code + paid price (exact); OCR supplies the original price
    # and backfills anything the barcode couldn't provide.
    code = bc["code"] or ocr["code"]
    reduced_price = bc["reduced_price"] if bc["reduced_price"] is not None else ocr["reduced_price"]
    original_price = ocr["original_price"]

    # Last resort: the paid price is the one field the item's whole profit
    # calculation rests on, so if neither the barcode nor OCR produced it, ask the
    # vision model rather than making the user type it. Never consulted when the
    # barcode already gave an exact price.
    vision = {}
    if reduced_price is None and TAG_VISION_FALLBACK:
        vision = vision_read_tag(image_path)
        if vision.get("reduced_price") is not None:
            reduced_price = vision["reduced_price"]
        code = code or vision.get("code")
        if original_price is None:
            original_price = vision.get("original_price")

    if barcode and bc["reduced_price"] is not None:
        source = "barcode"
    elif barcode:
        source = "barcode+ocr"
    elif vision.get("reduced_price") is not None:
        source = "vision"
    else:
        source = "ocr"

    result = {
        "reduced_price": reduced_price,
        "original_price": original_price,
        "code": code,
        "barcode": barcode,
        "source": source,
        "raw_text": raw_text.strip(),
        "ocr_variant": ocr_variant,
        "photo": str(image_path),
    }
    if code is None and reduced_price is None:
        result["error"] = ocr_error or "no barcode decoded and no price/code found in OCR text"
    return result


if __name__ == "__main__":
    import json
    import sys

    if len(sys.argv) < 2:
        print("Usage: python receipt.py <tag_image.jpg>")
        sys.exit(1)
    path = sys.argv[1]
    if not Path(path).exists():
        print(f"Error: file not found: {path}")
        sys.exit(1)
    print(json.dumps(extract_receipt(path), indent=2))
