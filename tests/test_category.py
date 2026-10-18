"""Choosing the eBay category in code, and refusing ones unrelated to the item.

The cases are the live mistakes: a8ad7eba (duffel -> Underwater Cameras),
b50bd66b (Apple Watch band -> Canes & Walking Sticks), 8a66a4b0 (sunglasses ->
Eyeglass Frames), 7583e1e0 / 94c8a02f (men's items in women's categories).
No network: Taxonomy suggestions are stubbed.

Run with `python tests/test_category.py` or `pytest tests/`.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from pipeline.category import (choose_category, department_of, known_category,
                               path_fits, suggestion_path)


def _sugg(cid, *path):
    *ancestors, leaf = path
    return {"category": {"categoryId": cid, "categoryName": leaf},
            "categoryTreeNodeAncestors": [
                {"categoryName": n, "categoryTreeNodeLevel": i + 1}
                for i, n in enumerate(reversed(ancestors))][::-1]}


FISHING = _sugg("180000", "Sporting Goods", "Fishing", "Underwater Cameras")
CANES = _sugg("182060", "Collectibles", "Vintage", "Canes & Walking Sticks")
MENS_WALLETS = _sugg("2682", "Clothing, Shoes & Accessories", "Men",
                     "Men's Accessories", "Wallets")
WOMENS_WALLETS = _sugg("45258", "Clothing, Shoes & Accessories", "Women",
                       "Women's Accessories", "Wallets")


@pytest.mark.parametrize("ident, expected", [
    ({"item_type": "Duffel Bag", "brand": "Ed Hardy"}, "169291"),            # a8ad7eba
    ({"item_type": "Apple Watch Band", "brand": "Michael Kors"}, "182068"),  # b50bd66b
    ({"item_type": "Women's Sunglasses", "brand": "PUMA"}, "45246"),         # 8a66a4b0
    ({"item_type": "Sunglasses"}, "79720"),
    ({"item_type": "Crossbody Bag"}, "169291"),
    ({"item_type": "Tote Bag", "department": "Women"}, "169291"),
    ({"item_type": "Backpack", "department": "Men"}, "52357"),
    ({"item_type": "Crossbody Wallet"}, "45258"),
    ({"item_type": "Wallet Crossbody Bag"}, "169291"),
    ({"item_type": "European Pillow Sham"}, "43397"),
    ({"item_type": "King Duvet Cover Set"}, "37644"),
    ({"item_type": "Queen Flat Sheet"}, "20460"),
    ({"item_type": "Flannel Pillowcases"}, "54257"),
    ({"item_type": "Fragrance Gift Set for Men"}, "29585"),
    ({"item_type": "Women's Wristwatch"}, "31387"),
    ({"item_type": "Smartwatch Band"}, "182068"),
])
def test_common_types_map_to_the_stores_known_good_category(ident, expected):
    chosen = choose_category(ident, suggest=lambda q: pytest.fail("no lookup needed"))
    assert chosen["category_id"] == expected
    assert chosen["source"] == "known"


def test_department_from_the_draft_moves_a_mens_bag_out_of_womens():
    # 94c8a02f: identification had no department; the draft said Men's.
    ident = {"item_type": "Flap Messenger Bag", "brand": "Michael Kors"}
    assert choose_category(ident)["category_id"] == "169291"
    chosen = choose_category(ident, {"Department": "Men"},
                             "Michael Kors Everett Flap Messenger Bag Black Men's")
    assert chosen["category_id"] == "52357"


def test_mens_wallet_never_lands_in_womens_wallets():
    # 7583e1e0: no known-good men's wallet id, so a suggestion decides, and the
    # women's one is refused even when eBay ranks it first.
    ident = {"item_type": "Men's Bifold Wallet", "brand": "Coach"}
    assert known_category(ident["item_type"], "men") is None
    chosen = choose_category(ident, suggest=lambda q: [WOMENS_WALLETS, MENS_WALLETS])
    assert chosen["category_id"] == "2682"
    assert chosen["source"] == "suggested"


def test_unrelated_suggestions_are_refused():
    ident = {"item_type": "Body Hair Trimmer"}
    assert choose_category(ident, suggest=lambda q: [FISHING, CANES]) is None
    trimmer = _sugg("67408", "Health & Beauty", "Shaving & Hair Removal", "Hair Trimmers")
    assert choose_category(ident, suggest=lambda q: [FISHING, trimmer])["category_id"] == "67408"


def test_suggestion_failure_does_not_raise():
    def boom(q):
        raise RuntimeError("taxonomy down")
    assert choose_category({"item_type": "Body Hair Trimmer"}, suggest=boom) is None


def test_path_fits():
    assert not path_fits(suggestion_path(FISHING), "Duffel Bag")
    assert not path_fits(suggestion_path(CANES), "Apple Watch Band")
    assert not path_fits("Eyeglass Frames", "Women's Sunglasses")
    assert path_fits("Women's Bags & Handbags", "Tote")
    assert not path_fits("Women > Women's Accessories > Wallets", "Bifold Wallet", "men")
    assert path_fits("Men > Men's Accessories > Wallets", "Bifold Wallet", "men")


def test_department_prefers_identification():
    assert department_of({"item_type": "Men's Bifold Wallet"}, {"Department": "Women"}) == "men"
    assert department_of({"item_type": "Tote"}, {"Department": "Unisex Adults"}) is None
    assert department_of({"item_type": "Tote"}, {"Department": "Women"}) == "women"


# --- the draft step -----------------------------------------------------------

def test_draft_overrides_the_models_freehand_category(monkeypatch):
    import pipeline.draft as draft
    monkeypatch.setattr(draft, "category_suggestions", lambda q: [])
    d = {"category_id": "182060", "title": "Michael Kors Band for Apple Watch",
         "item_specifics": {}}
    draft._enforce_category(d, {"item_type": "Apple Watch Band"})
    assert d["category_id"] == "182068"


def test_draft_keeps_model_category_when_nothing_better(monkeypatch):
    import pipeline.draft as draft
    monkeypatch.setattr(draft, "category_suggestions", lambda q: [FISHING])
    d = {"category_id": "67408", "title": "Trimmer", "item_specifics": {}}
    draft._enforce_category(d, {"item_type": "Body Hair Trimmer"})
    assert d["category_id"] == "67408"


def test_invalid_category_reselection_skips_unrelated_suggestions(monkeypatch):
    import ebay.taxonomy as tax

    def aspects(cid):
        if cid == "1":
            raise tax.InvalidCategoryError(cid)
        return []
    monkeypatch.setattr(tax, "category_aspects", aspects)
    bags = _sugg("169291", "Clothing, Shoes & Accessories", "Women",
                 "Women's Bags & Handbags")
    monkeypatch.setattr(tax, "category_suggestions", lambda q: [FISHING, bags])
    assert tax.resolve_valid_category("1", "Ed Hardy Duffel Bag Cheetah")[0] == "169291"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
