"""OCR the Ross receipt/tag that the user sends as the LAST photo of an item.

The receipt carries the two things we care about: the price we actually paid
(the reduced Ross price, not the crossed-out "Original price") and a unique
12-digit code. We read them with Tesseract (cheapest option — no API cost) and
store them on the item as cost data; the receipt image itself never goes to eBay.

Design: `ocr_text` is the only engine-specific piece. `parse_receipt` works on
plain text, so swapping OCR engines later (e.g. Google Cloud Vision) only means
replacing `ocr_text`. Nothing here may raise into the pipeline — a receipt we
can't read must not block a listing, so `extract_receipt` always returns a dict
(with an "error" note when OCR is unavailable or fails).
"""
import os
import re
from pathlib import Path

try:
    import pytesseract
    from PIL import Image, ImageOps
except ImportError:  # deps not installed yet — extract_receipt degrades gracefully
    pytesseract = None
    Image = None
    ImageOps = None

# On Windows the tesseract binary usually isn't on PATH; let .env point at it.
_TESSERACT_CMD = os.getenv("TESSERACT_CMD")
if _TESSERACT_CMD and pytesseract is not None:
    pytesseract.pytesseract.tesseract_cmd = _TESSERACT_CMD

# A money amount like 12.99 or $1,299.00 (require the cents to avoid matching
# stray digits / dates). Captured group excludes the $ and thousands commas.
_PRICE_RE = re.compile(r"\$?\s*(\d{1,3}(?:,\d{3})*|\d+)\.(\d{2})\b")
# The 12-digit code, standalone (not part of a longer digit run).
_CODE_RE = re.compile(r"(?<!\d)(\d{12})(?!\d)")
# Lines that quote the retail/compare-at price rather than what we paid.
_ORIGINAL_HINT = re.compile(r"\b(original|compare|comp\s*at|retail|msrp|reg(?:ular)?)\b", re.I)


def ocr_text(image_path: str) -> str:
    """Raw OCR text from the receipt image. Raises if the toolchain is missing."""
    if pytesseract is None or Image is None:
        raise RuntimeError(
            "pytesseract/Pillow not installed. Run `pip install pytesseract Pillow` "
            "and install the Tesseract binary (set TESSERACT_CMD in .env if it's not on PATH)."
        )
    img = Image.open(image_path)
    # Light preprocessing helps on thermal-printed receipts: fix EXIF rotation,
    # go grayscale, and autocontrast so faint print reads cleanly.
    img = ImageOps.exif_transpose(img)
    img = ImageOps.grayscale(img)
    img = ImageOps.autocontrast(img)
    return pytesseract.image_to_string(img)


def _price_on_line(line: str) -> float | None:
    m = _PRICE_RE.search(line)
    if not m:
        return None
    return float(m.group(1).replace(",", "") + "." + m.group(2))


def _all_prices(text: str) -> list[float]:
    return [
        float(m.group(1).replace(",", "") + "." + m.group(2))
        for m in _PRICE_RE.finditer(text)
    ]


def parse_receipt(text: str) -> dict:
    """Pull the reduced (paid) price, the original/retail price, and the 12-digit
    code out of the OCR text. Engine-independent.

    Ross receipts print the crossed-out retail on an "Original price"/"Compare At"
    line and the price actually charged elsewhere; the reduced price is our cost.
    """
    original_price = None
    for line in text.splitlines():
        if _ORIGINAL_HINT.search(line):
            p = _price_on_line(line)
            if p is not None:
                original_price = p
                break

    prices = _all_prices(text)
    # The reduced price is what we paid: the best candidate that isn't the
    # original. Prefer the highest price strictly below the original (the item's
    # discounted price, above tax/change lines); fall back to the highest price
    # that isn't the original, then to the single price we found.
    reduced_price = None
    if original_price is not None:
        below = [p for p in prices if p < original_price]
        others = [p for p in prices if p != original_price]
        if below:
            reduced_price = max(below)
        elif others:
            reduced_price = max(others)
    elif prices:
        reduced_price = max(prices)

    # Code: the 12-digit barcode number. Try the text as-is first; if OCR split
    # the digits with spaces (e.g. "1234 5678 9012") fall back to a per-line
    # whitespace-stripped scan — per line so we never merge a price on one line
    # with digits on the next into a false 12-digit run.
    m = _CODE_RE.search(text)
    code = m.group(1) if m else None
    if code is None:
        for line in text.splitlines():
            m = _CODE_RE.search(re.sub(r"\s+", "", line))
            if m:
                code = m.group(1)
                break

    return {
        "reduced_price": reduced_price,
        "original_price": original_price,
        "code": code,
        "raw_text": text.strip(),
    }


def extract_receipt(image_path: str) -> dict:
    """OCR + parse a receipt image into cost data. Never raises: returns a dict
    with an "error" note if the toolchain is missing or OCR fails, so a bad
    receipt read can't abort the listing pipeline."""
    try:
        text = ocr_text(image_path)
    except Exception as e:
        return {
            "reduced_price": None, "original_price": None, "code": None,
            "raw_text": "", "error": f"{type(e).__name__}: {e}",
        }
    result = parse_receipt(text)
    result["photo"] = str(image_path)
    return result


if __name__ == "__main__":
    import json
    import sys

    if len(sys.argv) < 2:
        print("Usage: python receipt.py <receipt_image.jpg>")
        sys.exit(1)
    path = sys.argv[1]
    if not Path(path).exists():
        print(f"Error: file not found: {path}")
        sys.exit(1)
    print(json.dumps(extract_receipt(path), indent=2))
