"""Item specifics: filter-shaped values, only what the identification supports,
and never a value cut mid-phrase. Cases are from the live audit.

Run with `python tests/test_specifics.py` or `pytest tests/`.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from pipeline.specifics import clean_specifics, identification_department


def _aspect(name, single=True, selection=False, values=()):
    return {"localizedAspectName": name,
            "aspectConstraint": {"itemToAspectCardinality": "SINGLE" if single else "MULTI",
                                 "aspectMode": "SELECTION_ONLY" if selection else "FREE_TEXT"},
            "aspectValues": [{"localizedValue": v} for v in values]}


WOMENS_BAGS = [
    _aspect("Department", selection=True, values=["Women", "Unisex Adults"]),
    _aspect("Pattern", values=["Animal Print", "Floral", "Graphic", "Monogram", "Solid"]),
    _aspect("Exterior Color", values=["Black", "Beige", "Multicolor"]),
    _aspect("Style", values=["Backpack", "Crossbody", "Duffle", "Tote"]),
    _aspect("Features", single=False),
]

TEDDY = {"item_type": "Backpack", "color": "Tan, Beige, Brown, Pink",
         "product_name": "Ndelita Teddy Bear Backpack",
         "specifications": {"Pattern": "Animal Print"}}            # a77c130a
CHEETAH = {"item_type": "Duffel Bag", "color": "Cheetah",
           "product_name": "Love Kills Slowly Cheetah Duffel Bag"}  # a8ad7eba
TATTOO = {"item_type": "Hobo Shoulder Bag",
          "color": "Black with multicolor tattoo flash print (Tiger, Skull)"}  # 5fdc486f


def test_animal_print_needs_an_animal_skin_print():
    out, _ = clean_specifics({"Pattern": "Animal Print"}, TEDDY, WOMENS_BAGS)
    assert "Pattern" not in out
    out, _ = clean_specifics({"Pattern": "Animal Print, Graphic Print"}, TATTOO, WOMENS_BAGS)
    assert out["Pattern"] == "Graphic Print"
    out, _ = clean_specifics({"Pattern": "Animal Print"}, CHEETAH, WOMENS_BAGS)
    assert out["Pattern"] == "Animal Print"


def test_unisex_from_the_hint_list_becomes_the_categorys_gender():
    out, notes = clean_specifics({"Department": "Unisex Adults"}, CHEETAH, WOMENS_BAGS)
    assert out["Department"] == "Women"
    assert any("Department" in n for n in notes)


def test_identification_department_wins():
    ident = {"item_type": "Men's Bifold Wallet"}
    out, _ = clean_specifics({"Department": "Women"}, ident, None)
    assert out["Department"] == "Men"
    ident = {"item_type": "Sunglasses", "specifications": {"Department": "Unisex"}}
    out, _ = clean_specifics({"Department": "Unisex Adults"}, ident, None)
    assert out["Department"] == "Unisex Adults"


def test_department_is_never_invented():
    out, _ = clean_specifics({"Brand": "Coach"}, CHEETAH, WOMENS_BAGS)
    assert "Department" not in out


def test_identification_department_reading():
    assert identification_department({"item_type": "Women's Sunglasses"}) == "Women"
    assert identification_department({"department": "Men"}) == "Men"
    assert identification_department({"item_type": "Tote"}) is None


def test_comma_joined_single_value_aspects():
    out, _ = clean_specifics(
        {"Exterior Color": "Pink, Red, Green, Blue, Yellow, White, Black",  # da8b1b25
         "Color": "Olive Green, Navy Blue",                                  # fc33b77f
         "Style": "Duffle, Tote",
         "Features": "Adjustable Strap, Detachable Strap, Inner Pockets"},
        CHEETAH, WOMENS_BAGS)
    assert out["Exterior Color"] == "Multicolor"
    assert out["Color"] == "Olive Green"
    assert out["Style"] == "Duffle"
    assert out["Features"] == ["Adjustable Strap", "Detachable Strap", "Inner Pockets"]


def test_sentences_become_an_allowed_value_or_are_dropped():
    out, _ = clean_specifics(
        {"Pattern": "Graphic print of a city skyline at twilight",            # 460e5424
         "Strap Color": "Navy blue with white and red Tommy Hilfiger branding",  # fc33b77f
         "Exterior Color": "Natural (Beige/Cream), Hazelnut (Brown)"},          # a06a637c
        {"item_type": "Tote Bag"}, WOMENS_BAGS)
    assert out["Pattern"] == "Graphic"
    assert out["Strap Color"] == "Navy"
    assert out["Exterior Color"] == "Natural"
    out, _ = clean_specifics({"Pattern": "Repeating skull wearing a top hat and crossbones motif"},
                             {"item_type": "Tote"}, WOMENS_BAGS)
    assert "Pattern" not in out


def test_long_values_are_never_cut():
    models = ", ".join(f"Apple Watch Series {n}" for n in range(1, 9)) + ", Apple Watch SE"
    out, _ = clean_specifics({"Compatible Model": models}, {"item_type": "Apple Watch Band"}, None)
    vals = out["Compatible Model"]
    assert vals[0] == "Apple Watch Series 1" and vals[-1] == "Apple Watch SE"
    for v in vals:
        assert len(v) <= 65
    out, _ = clean_specifics({"Material": "x" * 70}, {}, None)
    assert "Material" not in out


def test_identifiers_and_free_text_pass_through():
    spec = {"Brand": "Michael Kors", "Model": "MKS8042", "UPC": "796483577589",
            "Size": "Compatible with 38mm, 40mm, and 41mm Apple Watch cases", "MPN": None}
    out, notes = clean_specifics(spec, {"item_type": "Apple Watch Band"}, None)
    assert out == spec
    assert notes == []


def test_publish_side_drops_instead_of_cutting():
    import ebay.inventory as inv
    models = ", ".join(f"Apple Watch Series {n}" for n in range(1, 9))
    vals = inv._normalize_aspect_values(models)
    assert vals == ["Apple Watch Series 1, Apple Watch Series 2, Apple Watch Series 3"]
    assert inv._normalize_aspect_values("word " * 20) == []


def test_draft_cleans_specifics_against_the_chosen_category(monkeypatch):
    import pipeline.draft as draft
    monkeypatch.setattr(draft, "category_aspects", lambda cid: WOMENS_BAGS)
    d = {"category_id": "169291",
         "item_specifics": {"Pattern": "Animal Print", "Department": "Unisex Adults"}}
    draft._clean_draft_specifics(d, TEDDY)
    assert d["item_specifics"] == {"Department": "Women"}


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
