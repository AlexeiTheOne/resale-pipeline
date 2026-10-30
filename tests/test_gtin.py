"""Only a manufacturer UPC/EAN may reach a listing's UPC field.

The Ross tag's own item code (400xxxxxxxxx) went out as the UPC on 8 live
listings. Those codes pass the GS1 check digit, so the check digit alone could
not catch them; pipeline/gtin.py also refuses GS1's in-store number ranges.

Run with `python tests/test_gtin.py` or `pytest tests/`.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from pipeline.gtin import check_digit_ok, is_store_code, product_gtin

# Real values from the live listings (item_specifics.UPC on 5fdc486f, 8c4260b4,
# 179def75, fe5be314, f68133f6, c57cb4cc, db2380ba).
ROSS_CODES = ["400299603058", "400297374769", "400299113298", "400294791385",
              "400305072205", "400298771819", "400298209190"]
# Real manufacturer UPCs from good listings (Michael Kors, Coach, Ed Hardy, ...).
REAL_UPCS = ["196163464596", "886087451047", "199347123210", "796483577589",
             "042429537477", "9343963022841"]


@pytest.mark.parametrize("code", ROSS_CODES)
def test_ross_codes_pass_the_check_digit_but_are_refused(code):
    assert check_digit_ok(code), "the reason a check digit alone was not enough"
    assert is_store_code(code)
    assert product_gtin(code) is None


@pytest.mark.parametrize("code", REAL_UPCS)
def test_manufacturer_upcs_are_kept(code):
    assert product_gtin(code) == code


def test_formatting_is_normalised():
    assert product_gtin("1 96163 46459 6") == "196163464596"


@pytest.mark.parametrize("value", [
    "400299603058000999",   # the full 18-digit Ross tag barcode
    "0400299603058",        # a Ross code zero-padded to EAN-13
    "2012345678906",        # EAN-13 restricted circulation (20-29)
    "19347478327",          # 11 digits (a misread on 3347c963)
    "196163464597",         # bad check digit
    None, "", "null", "Does not apply",
])
def test_not_a_product_gtin(value):
    assert product_gtin(value) is None


def test_german_ean_is_not_mistaken_for_a_store_code():
    assert product_gtin("4006381333931") == "4006381333931"  # 40-44 = Germany


def test_publish_side_refuses_a_ross_code():
    import ebay.inventory as inv
    assert not inv._valid_gtin("400299113298")
    aspects = inv._build_aspects({"UPC": "400299113298", "Brand": "Coach"})
    assert "UPC" not in aspects
    product = inv._build_product({"title": "Coach Tote",
                                  "item_specifics": {"UPC": "400299113298"}}, [], {}, "")
    assert "upc" not in product


def test_draft_takes_upc_only_from_the_label():
    import pipeline.draft as draft
    spec = {"UPC": "400299113298", "Brand": "Coach"}
    draft._clean_gtins(spec, label_upc="400299113298")
    assert "UPC" not in spec
    spec = {"UPC": "886087451016"}  # a search guess, not what the label says
    draft._clean_gtins(spec, label_upc="886087451047")
    assert spec["UPC"] == "886087451047"
    spec = {"EAN": "886087451047"}
    draft._clean_gtins(spec, label_upc=None)
    assert spec == {}


def test_revision_keeps_a_valid_upc_and_drops_a_store_code():
    import pipeline.draft as draft
    spec = {"UPC": "886087451047"}
    draft._clean_gtins(spec, keep_valid=True)
    assert spec == {"UPC": "886087451047"}
    spec = {"UPC": "400299113298"}
    draft._clean_gtins(spec, keep_valid=True)
    assert spec == {}


def test_barcode_reader_drops_the_ross_tag_symbol(monkeypatch):
    import ross_tag

    class _Sym:
        def __init__(self, kind, data):
            self.type, self.data = kind, data.encode()

    class _Img:
        size = (10, 10)

        def resize(self, size):
            return self

    monkeypatch.setattr(ross_tag, "Image", type("I", (), {"open": staticmethod(lambda p: _Img())}))
    monkeypatch.setattr(ross_tag, "ImageOps", type("O", (), {
        "exif_transpose": staticmethod(lambda im: im),
        "grayscale": staticmethod(lambda im: im)}))
    monkeypatch.setattr(ross_tag, "_zbar_decode", lambda im, symbols=None: [
        _Sym("UPCA", "400299113298"), _Sym("UPCA", "196163464596")])
    assert ross_tag.read_product_upcs("tag.jpg") == ["196163464596"]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
