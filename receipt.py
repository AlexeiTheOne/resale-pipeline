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
    from pyzbar.pyzbar import decode as _zbar_decode
except ImportError:
    _zbar_decode = None

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
            for res in _zbar_decode(im):
                digits = re.sub(r"\D", "", res.data.decode("utf-8", "ignore"))
                if len(digits) >= 12:
                    return digits
        except Exception:
            continue
    return None


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

def ocr_text(image_path: str) -> str:
    """Raw OCR text from the tag image. Raises if the toolchain is missing."""
    if pytesseract is None or Image is None:
        raise RuntimeError(
            "pytesseract/Pillow not installed. Run `pip install pytesseract Pillow` "
            "and install the Tesseract binary (set TESSERACT_CMD in .env if it's not on PATH)."
        )
    img = Image.open(image_path)
    # The printed text is small; upscaling 3x + autocontrast makes Tesseract read
    # the price lines reliably. psm 6 (uniform block) beat the default on Ross tags.
    img = ImageOps.exif_transpose(img)
    img = ImageOps.grayscale(img)
    img = img.resize((img.size[0] * 3, img.size[1] * 3))
    img = ImageOps.autocontrast(img)
    return pytesseract.image_to_string(img, config="--psm 6")


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
    try:
        raw_text = ocr_text(image_path)
        ocr = parse_receipt(raw_text)
    except Exception as e:
        ocr_error = f"{type(e).__name__}: {e}"

    # Barcode wins for code + paid price (exact); OCR supplies the original price
    # and backfills anything the barcode couldn't provide.
    code = bc["code"] or ocr["code"]
    reduced_price = bc["reduced_price"] if bc["reduced_price"] is not None else ocr["reduced_price"]
    original_price = ocr["original_price"]

    if barcode and bc["reduced_price"] is not None:
        source = "barcode"
    elif barcode:
        source = "barcode+ocr"
    else:
        source = "ocr"

    result = {
        "reduced_price": reduced_price,
        "original_price": original_price,
        "code": code,
        "barcode": barcode,
        "source": source,
        "raw_text": raw_text.strip(),
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
