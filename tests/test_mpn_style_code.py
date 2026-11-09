"""The style code goes into MPN as well as Model.

eBay's listing quality report (2026-11-06) showed "MPN: Not provided" on 60 of
72 bag listings. None of the 60 had an MPN in our DB; 53 had the style code
under Model, because the draft model filed it there and left MPN null.
Cases below are real values from those listings."""
from pipeline import draft, specifics


def test_style_code_comes_from_model_then_identification():
    assert specifics.style_code({"Model": "R34D1B53"}, {}) == "R34D1B53"
    assert specifics.style_code({"model": "87F2LEVB9U"}, {}) == "87F2LEVB9U"
    assert specifics.style_code({}, {"style_code": "EHH462-42-003"}) == "EHH462-42-003"
    assert specifics.style_code({"Model": None}, {"model": "XW09620"}) == "XW09620"


def test_a_code_with_colour_and_size_suffixes_counts_by_its_first_token():
    assert specifics.style_code({}, {"style_code": "XW09620 970 0IC"}) == "XW09620"


def test_names_are_not_style_codes():
    for name in ("The J Marc", "Ava Tote", "LBPERCY", "Astrid", "", "null"):
        assert specifics.style_code({"Model": name}, {}) is None, name


def test_mpn_is_filled_and_model_stays():
    spec = {"Brand": "DKNY", "Model": "R34D1B53", "MPN": None}
    assert specifics.fill_mpn_from_style_code(spec, {}) == "R34D1B53"
    assert spec["MPN"] == "R34D1B53" and spec["Model"] == "R34D1B53"


def test_does_not_apply_is_replaced_but_a_real_mpn_is_kept():
    spec = {"Model": "R34D1B53", "MPN": "Does not apply"}
    specifics.fill_mpn_from_style_code(spec, {})
    assert spec["MPN"] == "R34D1B53"
    spec = {"Model": "R34D1B53", "MPN": "LB102100"}
    assert specifics.fill_mpn_from_style_code(spec, {}) is None
    assert spec["MPN"] == "LB102100"


def test_no_code_leaves_mpn_empty():
    spec = {"Model": "The J Marc", "MPN": None}
    assert specifics.fill_mpn_from_style_code(spec, {}) is None
    assert spec["MPN"] is None


def test_mpn_is_added_when_the_draft_had_no_mpn_key():
    spec = {"Model": "87F2LEVB9U"}
    specifics.fill_mpn_from_style_code(spec, {})
    assert spec["MPN"] == "87F2LEVB9U"


def test_draft_cleanup_fills_mpn(monkeypatch):
    """Both generate_draft and revise_draft end their specifics clean-up here."""
    monkeypatch.setattr(draft, "category_aspects", lambda cid: [])
    d = {"category_id": "169291",
         "item_specifics": {"Brand": "Tommy Hilfiger", "Model": "XW09620", "MPN": None,
                            "UPC": "199808130948"}}
    draft._clean_draft_specifics(d, {"style_code": "XW09620 970 0IC"})
    assert d["item_specifics"]["MPN"] == "XW09620"
    assert d["item_specifics"]["Model"] == "XW09620"
    assert d["item_specifics"]["UPC"] == "199808130948"
