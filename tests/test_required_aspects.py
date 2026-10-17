"""Filling a category's required item specifics from what the draft already has.

Two Ed Hardy bags sat at 'approved' because category 169291 (Women's Bags)
requires Exterior Material, Style and Department. The draft carried every one of
them under another name: 'Material', item_type 'Tote Bag', and the category
itself.

Run with `python tests/test_required_aspects.py` or `pytest tests/`.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import ebay.inventory as inv

STYLES = ["Backpack", "Clutch", "Crossbody", "Hobo Bag", "Satchel/Top Handle Bag",
          "Shoulder Bag", "Top Handle Bag", "Tote"]


def _spec(name, mode="FREE_TEXT", values=()):
    return {
        "localizedAspectName": name,
        "aspectConstraint": {"aspectRequired": True, "aspectMode": mode},
        "aspectValues": [{"localizedValue": v} for v in values],
    }


WOMENS_BAGS = [
    _spec("Exterior Material", values=["Canvas", "Leather"]),
    _spec("Style", values=STYLES),
    _spec("Department", "SELECTION_ONLY", ["Women", "Unisex Adults"]),
]


def _resolve(specs, aspects, hint):
    original = inv.category_aspects
    inv.category_aspects = lambda _cid: specs
    try:
        return inv._resolve_required_aspects("169291", aspects, hint)
    finally:
        inv.category_aspects = original


def test_womens_tote_resolves_from_what_the_draft_already_has():
    r = _resolve(WOMENS_BAGS, {"Material": ["Vegan Leather"]}, "Tote Bag")
    assert r["Exterior Material"] == ["Vegan Leather"]
    assert r["Style"] == ["Tote"]
    assert r["Department"] == ["Women"]


def test_style_matches_bag_forms():
    assert inv._match_type_hint("Crossbody Bag", STYLES) == "Crossbody"
    assert inv._match_type_hint("Hobo", STYLES) == "Hobo Bag"
    assert inv._match_type_hint("Satchel", STYLES) == "Satchel/Top Handle Bag"
    assert inv._match_type_hint("Top Handle Bag", STYLES) == "Top Handle Bag"  # exact wins


def test_style_is_left_missing_rather_than_guessed():
    try:
        _resolve(WOMENS_BAGS, {"Material": ["Canvas"]}, "Wallet")
    except inv.MissingRequiredAspectsError as e:
        assert [m["name"] for m in e.missing] == ["Style"]
    else:
        raise AssertionError("an unmatched Style must not be filled")


def test_department_only_from_a_single_gender_category():
    assert inv._category_department(["Men", "Unisex Adults", "Teens"]) == "Men"
    assert inv._category_department(["Women", "Men", "Unisex Adults"]) is None
    assert inv._category_department(["Unisex Adults"]) is None


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
