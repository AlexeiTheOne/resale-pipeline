"""Keeping titles within eBay's 80 characters without losing size and colour.

The old safety net cut over-long titles off the end at a word boundary, and the
end is where the draft prompt puts size and colour. Seven live titles lost
"38/40/41mm Blush Pink", a model code, or a colour that way. The cases below are
those real titles, with the identification/specifics they were written from.

Run with `python tests/test_title.py` or `pytest tests/`.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from pipeline.title import TITLE_MAX_LEN, ensure_title_fits, fit_title

# (title, identification, specifics, words that must survive)
REAL = {
    "bff24df0": (
        "Michael Kors Access Pavé Rose Gold-Tone Logo Strap Apple Watch Band Set 38/40/41mm Blush Pink",
        {"brand": "Michael Kors Access", "item_type": "Apple Watch Band Gift Set",
         "model": "MKS8028SET", "color": "Blush Pink / Rose Gold-Tone",
         "size": "Compatible with 38mm, 40mm, and 41mm Apple Watch cases"},
        {"Brand": "Michael Kors Access", "Color": "Blush Pink, Rose Gold-Tone",
         "Size": "Compatible with 38mm, 40mm, and 41mm Apple Watch cases",
         "Material": "Stainless Steel, Rubber, Silicone", "Department": "Unisex"},
        ["Michael", "Kors", "Access", "Band", "38/40/41mm", "Blush", "Pink", "Apple", "Watch"],
    ),
    "a129b1b5": (
        "Ed Hardy Love Kills Slowly Rhinestone Heart Drawstring Backpack Black Multi One Size",
        {"brand": "Ed Hardy", "item_type": "Drawstring Backpack", "model": "EHH383-42-BMT",
         "color": "Black Multi (Black background with grey monogram, red script)",
         "size": "One Size (Backpack Purse)"},
        {"Brand": "Ed Hardy", "Color": "Black, Grey, Red, White, Yellow", "Size": "One Size"},
        ["Ed", "Hardy", "Backpack", "Black", "Multi"],
    ),
    "2f57c0f4": (
        "Juicy Couture Prep School Weekender Large Leopard Travel Tote Bag NWT 11JCEAJJC-PLB",
        {"brand": "Juicy Couture", "item_type": "Weekender Travel Tote Bag",
         "model": "11JCEAJJC-PLB", "color": "Leopard (Brown / Tan / Black)", "size": "Large"},
        {"Brand": "Juicy Couture", "Model": "11JCEAJJC-PLB", "Size": "Large",
         "Color": "Brown, Tan, Black, Cream", "MPN": "11JCEAJJC-PLB"},
        ["Juicy", "Couture", "Tote", "Large", "Leopard", "11JCEAJJC-PLB"],
    ),
    "ed54b249": (
        "Kate Spade New York Apple Watch Floral Print Silicone Band 38/40/41mm Black Floral",
        {"brand": "Kate Spade New York", "item_type": "Apple Watch Band", "model": "KSS0011",
         "color": "Black Floral / Silver", "size": "38mm / 40mm / 41mm"},
        {"Brand": "Kate Spade New York", "Color": "Black, Pink, White, Green, Silver",
         "Size": "38mm, 40mm, 41mm", "Material": "Silicone"},
        ["Kate", "Spade", "New", "York", "Band", "38/40/41mm", "Black", "Floral"],
    ),
    "182a5ab3": (
        "Ed Hardy Large Faux Leather Rhinestone 3 Hearts LOVE Skull Print Backpack Pink Multi",
        {"brand": "Ed Hardy", "item_type": "Backpack", "model": "EHH436-42-PLT",
         "color": "Pink Multi (Pink, Red, Green, Yellow, Black, White)",
         "size": "Large Backpack (approx. 16\" H x 12\" W x 5\" D)"},
        {"Brand": "Ed Hardy", "Size": "Large", "Material": "Faux Leather / Vegan Leather",
         "Color": "Pink, Red, Green, Yellow, Black, White"},
        ["Ed", "Hardy", "Large", "Backpack", "Pink", "Multi"],
    ),
    "0e48e50b": (
        "Ed Hardy Skull & Rose Graphic Tote Bag Medium Large Yellow Orange Red Black White",
        {"brand": "Ed Hardy", "item_type": "Tote Bag", "model": "EHH357-26-0ML",
         "color": "Multicolor (Yellow, Orange, Red, White, Black)", "size": "Medium to Large"},
        {"Brand": "Ed Hardy", "Color": "Yellow, Orange, Red, White, Black", "Size": "One Size"},
        ["Ed", "Hardy", "Tote", "Medium", "Large", "Yellow", "Orange", "Red", "Black", "White"],
    ),
    "db2380ba": (
        "Ed Hardy Box Small Tote Crossbody Denim Blue Multicolor Skull Roses Hummingbird Rhinestone",
        {"brand": "Ed Hardy", "item_type": "Small Box Tote / Crossbody Bag",
         "model": "EHH253-26-DMB", "color": "Denim Blue / Multicolor",
         "size": "Small / Medium Box Tote"},
        {"Brand": "Ed Hardy", "Size": "Small", "Material": "Denim, Canvas, Faux Leather",
         "Color": "Blue, Pink, Red, Green, Black, Yellow", "Department": "Women"},
        ["Ed", "Hardy", "Crossbody", "Small", "Denim", "Blue", "Multicolor"],
    ),
}


@pytest.mark.parametrize("key", sorted(REAL))
def test_real_titles_fit_and_keep_what_buyers_filter_on(key):
    title, ident, spec, must_keep = REAL[key]
    fitted, dropped = fit_title(title, ident, spec)
    assert len(fitted) <= TITLE_MAX_LEN, fitted
    words = fitted.split()
    for w in must_keep:
        assert w in words, f"{key}: lost {w!r} -> {fitted!r}"
    # Every dropped word was really in the title, and only those are gone.
    assert sorted(words + dropped) == sorted(title.split())
    # Brand still leads.
    assert fitted.startswith(" ".join(title.split()[:2]))


def test_the_old_one_size_stump_cannot_happen():
    """Truncation left '...Black Multi One'. 'One Size' goes as a pair."""
    title, ident, spec, _ = REAL["a129b1b5"]
    fitted, dropped = fit_title(title, ident, spec)
    assert not fitted.endswith(" One")
    assert dropped == ["One", "Size"]


def test_duplicate_keeps_the_colour_copy():
    """'Floral' twice: the one in the colour 'Black Floral' stays."""
    title, ident, spec, _ = REAL["ed54b249"]
    fitted, dropped = fit_title(title, ident, spec)
    assert dropped == ["Floral"]
    assert fitted.endswith("Black Floral")


def test_generic_noun_goes_before_the_condition_or_model():
    title, ident, spec, _ = REAL["2f57c0f4"]
    fitted, dropped = fit_title(title, ident, spec)
    assert dropped == ["Bag"]
    assert "11JCEAJJC-PLB" in fitted


def test_brand_new_inside_a_brand_name_is_not_a_condition_word():
    title = ("Kate Spade New York Spencer Slim Bifold Wallet Saffiano Leather "
             "Black New With Tags Gift Box")
    fitted, dropped = fit_title(title, {"brand": "Kate Spade New York",
                                        "item_type": "Wallet", "color": "Black"}, {})
    assert fitted.startswith("Kate Spade New York ")
    assert len(fitted) <= TITLE_MAX_LEN


def test_watch_band_keeps_for():
    """'for Apple Watch' is required on a band (playbook §1); 'for' is not filler."""
    title = ("Michael Kors Rose Gold-Tone Stainless Steel Link Band for Apple Watch "
             "38mm 40mm 41mm Rose Gold")
    fitted, _ = fit_title(title, {"brand": "Michael Kors", "item_type": "Watch Band",
                                  "color": "Rose Gold"}, {"Size": "38mm 40mm 41mm"})
    assert " for Apple Watch" in fitted


def test_short_title_untouched():
    assert fit_title("Coach Tote Black", {}, {}) == ("Coach Tote Black", [])


def test_works_without_any_attributes():
    title = " ".join(["Word"] * 10 + ["Alpha", "Bravo", "Charlie", "Delta", "Echo",
                                       "Foxtrot", "Golf", "Hotel", "India", "Juliet"])
    fitted, _ = fit_title(title + " Kilo Lima Mike November Oscar", None, None)
    assert len(fitted) <= TITLE_MAX_LEN


# --- the seam -------------------------------------------------------------

LONG = REAL["bff24df0"]


def test_seam_uses_refit_when_it_fits():
    t, d, how = ensure_title_fits(LONG[0], LONG[1], LONG[2],
                                  refit=lambda _t: "Michael Kors Access Apple Watch Band 38/40/41mm Blush Pink")
    assert how == "refit"
    assert t == "Michael Kors Access Apple Watch Band 38/40/41mm Blush Pink"
    assert "Logo" in d


@pytest.mark.parametrize("refit", [
    lambda t: t + " Extra",          # model made it longer
    lambda t: None,                  # model returned nothing
    lambda t: 1 / 0,                 # model call blew up
])
def test_seam_falls_back_to_fit_title(refit):
    t, _, how = ensure_title_fits(LONG[0], LONG[1], LONG[2], refit=refit)
    assert how == "fallback"
    assert t == fit_title(*LONG[:3])[0]


def test_seam_leaves_a_fitting_title_alone():
    called = []
    t, d, how = ensure_title_fits("Coach  Tote", {}, {}, refit=lambda x: called.append(x))
    assert (t, d, how, called) == ("Coach Tote", [], "ok", [])


# --- the draft step re-asks once, then falls back -----------------------------

def test_draft_reasks_model_once_then_falls_back(monkeypatch):
    import pipeline.draft as draft
    calls = []

    def still_long(title, ident, spec):
        calls.append(title)
        return title  # a model that ignores the instruction

    monkeypatch.setattr(draft, "_refit_title", still_long)
    d = {"title": LONG[0], "item_specifics": LONG[2]}
    draft._fit_draft_title(d, LONG[1])
    assert len(calls) == 1
    assert len(d["title"]) <= TITLE_MAX_LEN
    assert "Blush Pink" in d["title"]


# --- eBay writes refuse instead of cutting --------------------------------

def test_publish_payload_refuses_long_title():
    import ebay.inventory as inv
    with pytest.raises(inv.TitleTooLongError) as e:
        inv._build_product({"title": LONG[0], "item_specifics": {}}, [], {}, "")
    assert len(e.value.suggestion) <= TITLE_MAX_LEN
    assert "80" in str(e.value)


def test_title_edit_refuses_before_touching_ebay(monkeypatch):
    import ebay.inventory as inv
    called = []
    monkeypatch.setattr(inv, "_request", lambda *a, **k: called.append(a))
    with pytest.raises(inv.TitleTooLongError):
        inv.update_inventory_title("sku", LONG[0])
    assert called == [], "no eBay call may happen for a refused title"


def test_create_draft_offer_refuses_before_uploading(monkeypatch):
    import ebay.inventory as inv
    item = {"listing": {"title": LONG[0], "category_id": "1", "item_specifics": LONG[2]},
            "photos": ["a.jpg"], "identification": LONG[1]}
    monkeypatch.setattr(inv, "get_item", lambda _id: item)
    monkeypatch.setattr(inv, "resolve_valid_category",
                        lambda *a: pytest.fail("got past the title check"))
    with pytest.raises(inv.TitleTooLongError):
        inv.create_draft_offer("x")


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
