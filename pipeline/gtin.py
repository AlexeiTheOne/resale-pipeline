"""Which barcode numbers may go in a listing's UPC/EAN field.

Only a manufacturer's GTIN — read off the product's own barcode or label — is a
UPC. The Ross price tag carries the store's own item code (400xxxxxxxxx), and on
8 live listings (5fdc486f, 8c4260b4, 179def75, fe5be314, f68133f6, c57cb4cc,
db2380ba, e5756e70) that code went out as the UPC. A check-digit test could not
stop it: Ross codes ARE structurally valid UPC-A numbers (all 7 pass), printed
in GS1 number system 4, which GS1 reserves for in-store use. So the rule here is
check digit AND not a store-internal number:

  * 12-digit UPC-A starting 2 (random-weight) or 4 (in-store)        -> reject
  * the same numbers zero-padded to EAN-13 / GTIN-14 (02…, 04…)        -> reject
  * EAN-13 prefixes 20-29 (restricted circulation, store-internal)    -> reject
  * the 18-digit Ross tag barcode (<12-digit code><6-digit price>)    -> reject

A 13-digit EAN starting 40-44 is a German manufacturer prefix, not a store code,
and is kept. Pure functions, no I/O.
"""
from __future__ import annotations

import re

GTIN_LENGTHS = (8, 12, 13, 14)


def digits_of(value) -> str:
    return re.sub(r"\D", "", str(value if value is not None else ""))


def check_digit_ok(digits: str) -> bool:
    """GS1 mod-10 check digit for an 8/12/13/14-digit string."""
    if len(digits) not in GTIN_LENGTHS or not digits.isdigit():
        return False
    nums = [int(d) for d in digits]
    body, check = nums[:-1], nums[-1]
    total = sum(d * (3 if i % 2 == 0 else 1) for i, d in enumerate(reversed(body)))
    return (10 - total % 10) % 10 == check


def is_store_code(value) -> bool:
    """True for a retailer's in-store number (the Ross tag code above all) that
    must never be presented as a product UPC."""
    d = digits_of(value)
    if not d:
        return False
    if len(d) == 18 and d.startswith("400"):  # the full Ross tag barcode
        return True
    if len(d) == 12:
        return d[0] in "24"
    if len(d) == 13:
        return d.startswith(("02", "04")) or d[:2] in {str(n) for n in range(20, 30)}
    if len(d) == 14:
        return is_store_code(d[1:])
    return False


def product_gtin(value) -> str | None:
    """The digits of `value` if it is a manufacturer UPC/EAN that may go in a
    listing (right length, valid check digit, not a store code), else None."""
    d = digits_of(value)
    if not check_digit_ok(d) or is_store_code(d):
        return None
    return d
