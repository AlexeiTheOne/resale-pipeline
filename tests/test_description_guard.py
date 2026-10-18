"""The description guard: authenticity claims, hashtags, keyword blocks and
other stores' names never reach a live description; facts that merely share a
word ("Genuine Leather", "authenticity card", "Amazon Green") survive.

Run with `python tests/test_description_guard.py` or `pytest tests/`.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from pipeline.description_guard import sanitize_description


def _clean(text, brand="Michael Kors"):
    return sanitize_description(text, brand)[0]


@pytest.mark.parametrize("text, expected", [
    ("Authentic Michael Kors Jet Set tote in black.", "Michael Kors Jet Set tote in black."),
    ("100% Authentic Coach wallet.", "Coach wallet."),
    ("Michael Kors tote in black. 100% authentic, guaranteed!", "Michael Kors tote in black."),
    ("Black tote. This is a genuine article, not a replica.", "Black tote."),
])
def test_authenticity_claims_are_removed(text, expected):
    assert _clean(text) == expected


def test_a_bullet_with_a_claim_goes_whole():
    text = "FEATURES:\n- Zip closure\n- 100% authentic, straight from the store\n- Gold hardware"
    assert _clean(text) == "FEATURES:\n- Zip closure\n- Gold hardware"


@pytest.mark.parametrize("text", [
    "- Material: Genuine Leather",
    "- Genuine leather trim",
    "INCLUDED:\n- Original dust bag\n- Registration/authenticity card",   # 331dc4a9
    "Coach Men's 3-In-1 Wallet in Amazon Green with Monogram Print.",       # 7583e1e0
    "- Style #CF134, Model #MKS8042",
    "SHIPPING:\nShips within 1 business day via USPS with tracking number.",
    "Item must be returned in the same condition it was sent. Tags must still be attached.",
])
def test_facts_that_share_a_word_survive(text):
    out, removed = sanitize_description(text, "Coach")
    assert out == text
    assert removed == []


def test_hashtags_are_removed():
    text = "Black Michael Kors tote. #michaelkors #tote #nwt\n#handbag #purse"
    assert _clean(text) == "Black Michael Kors tote."


def test_keyword_blocks_are_removed():
    text = ("Black tote.\nKeywords: michael kors, mk, purse, handbag\n"
            "michael kors, mk, purse, handbag, tote, shoulder bag, jet set, black, gold, nwt\n"
            "- Zip closure")
    assert _clean(text) == "Black tote.\n- Zip closure"


@pytest.mark.parametrize("text", [
    "Black tote. Purchased at Ross Dress for Less.",
    "Black tote. Compare at Macy's for $199.",
    "Black tote. Also listed on Poshmark.",
    "Black tote. Retails at TJ Maxx and Marshalls.",
    "Black tote. Bought at Target.",
])
def test_other_store_names_are_removed(text):
    assert _clean(text) == "Black tote."


def test_brand_that_shares_a_store_name_is_kept():
    text = "Nordstrom cashmere wrap in grey."
    assert sanitize_description(text, "Nordstrom")[0] == text


def test_draft_step_guards_before_the_boilerplate(monkeypatch):
    import pipeline.draft as draft
    d = {"description": "Authentic Michael Kors tote. #mk\n- Zip closure",
         "item_specifics": {"Brand": "Michael Kors"}}
    draft._guard_description(d, {"brand": "Michael Kors"})
    assert d["description"] == "Michael Kors tote.\n- Zip closure"


def test_publish_renders_the_guarded_text(monkeypatch):
    import ebay.inventory as inv
    item = {"listing": {"title": "Michael Kors Jet Set Tote Black", "category_id": "169291",
                        "description": "Authentic Michael Kors tote.\n- 100% authentic\n- Zip",
                        "item_specifics": {"Brand": "Michael Kors"}, "price": 50,
                        "condition_id": "1000"},
            "photos": ["a.jpg"], "identification": {"item_type": "Tote"}}
    seen = {}
    monkeypatch.setattr(inv, "get_item", lambda _id: item)
    monkeypatch.setattr(inv, "resolve_valid_category", lambda cid, t: (cid, None))
    monkeypatch.setattr(inv, "_resolve_required_aspects", lambda c, a, h: a)
    monkeypatch.setattr(inv, "_enforce_single_cardinality", lambda c, a: a)
    monkeypatch.setattr(inv, "listing_photo_order", lambda it: [])
    monkeypatch.setattr(inv, "_download_brand_image", lambda u, i: None)
    monkeypatch.setattr(inv, "upload_photos", lambda p: [])
    monkeypatch.setattr(inv, "_ensure_location", lambda: None)
    monkeypatch.setattr(inv, "get_policy_id", lambda t: "p")

    class Stop(Exception):
        pass

    def capture(text):
        seen["text"] = text
        raise Stop
    monkeypatch.setattr(inv, "_html_description", capture)
    with pytest.raises(Stop):
        inv.create_draft_offer("x")
    assert "uthentic" not in seen["text"]
    assert "Michael Kors tote." in seen["text"]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
