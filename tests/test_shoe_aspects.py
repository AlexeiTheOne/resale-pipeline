"""Shoe categories: US Shoe Size and Type, and asking for what's missing.

A pair of shoes sat at 'approved' because category 15709 (Men's Athletic Shoes)
requires US Shoe Size and Type, both SELECTION_ONLY. The draft had the size
under 'Size' (the name the prompt asks for), a 'Men's 10' was dropped as no
allowed value, and 'Running Shoes' matched none of Athletic / Cleats / Sneakers.
The bot then said "fix the listing and retry manually" with no way to do it.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import telegram_bot as tb  # noqa: E402
from bot import flow  # noqa: E402
from ebay import inventory  # noqa: E402
from pipeline import draft, specifics  # noqa: E402
from tg_fakes import (  # noqa: E402
    bot_state, last_markup_buttons, make_callback_update, run, sent_texts, tiny_jpeg,
)

SIZES = [f"{n / 2:g}" for n in range(4, 37)]  # 2, 2.5, ... 18
TYPES = ["Athletic", "Cleats", "Sneakers"]


def _spec(name, values=(), mode="SELECTION_ONLY", required=True):
    return {"localizedAspectName": name,
            "aspectConstraint": {"aspectRequired": required, "aspectMode": mode,
                                 "itemToAspectCardinality": "SINGLE"},
            "aspectValues": [{"localizedValue": v} for v in values]}


MENS_ATHLETIC = [_spec("Brand", mode="FREE_TEXT"), _spec("US Shoe Size", SIZES),
                 _spec("Type", TYPES), _spec("Department", ["Men"])]


@pytest.fixture
def shoes(monkeypatch):
    monkeypatch.setattr(inventory, "category_aspects", lambda cid: MENS_ATHLETIC)


# --- the size and type readers ---------------------------------------------

@pytest.mark.parametrize("text,want", [
    ("10", "10"), ("Men's 10", "10"), ("US 10.5", "10.5"), ("10 M", "10"),
    ("10W", "10"), ("10 1/2", "10.5"), ("10½", "10.5"), ("US M 9", "9"),
    ("UK 9 / US 10", "10"), ("10 US", "10"), ("Size 11 (US)", "11"),
])
def test_shoe_size_reads_one_us_number(text, want):
    assert specifics.shoe_size_value(text, SIZES) == want


@pytest.mark.parametrize("text", [
    "UK 9", "EU 43", "27 cm", "5Y", "11C", "Men's 10 / Women's 11.5", "M", None, "", "44",
])
def test_shoe_size_never_guesses(text):
    assert specifics.shoe_size_value(text, SIZES) is None


def test_shoe_size_only_returns_a_catalog_value():
    assert specifics.shoe_size_value("10", ["7", "8", "9"]) is None
    assert specifics.shoe_size_value("10.0", ["10"]) == "10"


@pytest.mark.parametrize("hint,want", [
    ("Running Shoes", "Athletic"), ("Basketball Shoes", "Athletic"),
    ("Athletic Shoes", "Athletic"), ("Sneakers", "Sneakers"), ("Trainers", "Sneakers"),
    ("Casual Shoes", "Sneakers"), ("Soccer Cleats", "Cleats"), ("Shoes", None),
    ("Gym Bag", None), ("Dress Shoes", None),
])
def test_shoe_type(hint, want):
    assert specifics.shoe_type_value(hint, TYPES) == want


def test_clean_specifics_keeps_a_wordy_size_and_type():
    cleaned, _ = specifics.clean_specifics(
        {"US Shoe Size": "Men's 10", "Type": "Running Shoes"}, {}, MENS_ATHLETIC)
    assert cleaned["US Shoe Size"] == "10"
    assert cleaned["Type"] == "Athletic"


# --- filling the required ones at draft time --------------------------------

def test_replay_15709_size_under_size_and_running_shoes(shoes):
    r = inventory._resolve_required_aspects("15709", {"Brand": ["Nike"], "Size": ["Men's 10"]},
                                            "Running Shoes", {})
    assert r["US Shoe Size"] == ["10"]
    assert r["Type"] == ["Athletic"]
    assert r["Department"] == ["Men"]


def test_size_from_the_identification(shoes):
    ident = {"specifications": {"Size": "US 9.5 / UK 8.5"}}
    r = inventory._resolve_required_aspects("15709", {"Brand": ["Nike"]}, "Sneakers", ident)
    assert r["US Shoe Size"] == ["9.5"]
    assert r["Type"] == ["Sneakers"]


def test_unreadable_size_is_missing_with_every_value(shoes):
    with pytest.raises(inventory.MissingRequiredAspectsError) as e:
        inventory._resolve_required_aspects("15709", {"Brand": ["Nike"], "Size": ["UK 9"]},
                                            "Shoes", {})
    missing = {m["name"]: m for m in e.value.missing}
    assert set(missing) == {"US Shoe Size", "Type"}
    assert missing["US Shoe Size"]["allowed_values"] == SIZES  # not cut to 15
    assert missing["US Shoe Size"]["index"] == 1
    assert missing["Type"]["index"] == 2


def test_hint_block_names_the_shoe_size(monkeypatch):
    monkeypatch.setattr(draft, "category_aspects", lambda cid: MENS_ATHLETIC)
    block = draft._aspect_hint_block({"category_id": "15709"})
    line = next(l for l in block.splitlines() if l.startswith("- US Shoe Size"))
    assert 'under "US Shoe Size", not "Size"' in line
    assert "sewn-in label" in line


# --- asking in Telegram ------------------------------------------------------

@pytest.fixture
def bot(monkeypatch, shoes):
    with bot_state(tb):
        yield tb


def _seed():
    folder = tb.INBOX / "shoe_aspects_test"
    folder.mkdir(parents=True, exist_ok=True)
    p = folder / "p0.jpg"
    p.write_bytes(tiny_jpeg())
    item_id = tb.create_item([str(p)])
    tb.update_field(item_id, "identification", {"brand": "Nike", "item_type": "Shoes"})
    tb.update_field(item_id, "listing", {"title": "Nike Shoes", "price": 40, "description": "x",
                                         "category_id": "15709",
                                         "item_specifics": {"Brand": "Nike"}})
    tb.update_status(item_id, "review")
    return item_id


def test_missing_aspects_back_to_review_with_buttons_then_taps_create_the_draft(bot, monkeypatch):
    created = []

    def create(item_id):
        # The real check, so the stub refuses until both values are set.
        missing = inventory.missing_required_aspects(tb.get_item(item_id))
        if missing:
            raise inventory.MissingRequiredAspectsError("15709", missing)
        created.append(item_id)
        return {"sku": "SKU1", "offer_id": "OFF1"}
    monkeypatch.setattr(inventory, "create_draft_offer", create)

    item_id = _seed()
    tap, ctx = make_callback_update(f"review:approve:{item_id}")
    run(tb.review_callback, tap, ctx)

    assert tb.get_item(item_id)["status"] == "review"
    ask = sent_texts(ctx)[-1]
    assert "US Shoe Size" in ask and "Type (Athletic, Cleats, Sneakers)" in ask
    assert "retry manually" not in ask
    buttons = dict(last_markup_buttons(ctx))
    assert len(buttons) == len(SIZES) + len(TYPES)
    assert all(len(data.encode()) <= 64 for data in buttons.values())

    tap, _ = make_callback_update(buttons["10.5"], context=ctx)
    run(flow.aspect_callback, tap, ctx)
    assert tb.get_item(item_id)["listing"]["item_specifics"]["US Shoe Size"] == "10.5"
    assert "Still needed: Type" in sent_texts(ctx)[-1]
    assert created == []

    tap, _ = make_callback_update(buttons["Athletic"], context=ctx)
    run(flow.aspect_callback, tap, ctx)
    item = tb.get_item(item_id)
    assert created == [item_id]
    assert item["status"] == "ebay_draft"
    assert item["listing"]["item_specifics"]["Type"] == "Athletic"


def test_aspect_button_ignored_once_the_item_moved_on(bot, monkeypatch):
    monkeypatch.setattr(inventory, "create_draft_offer",
                        lambda i: (_ for _ in ()).throw(AssertionError("must not run")))
    item_id = _seed()
    tb.update_status(item_id, "ebay_draft")
    tap, ctx = make_callback_update(f"asp:{item_id}:15709:1:16")
    run(flow.aspect_callback, tap, ctx)
    assert "nothing to fill" in sent_texts(ctx)[-1]
    assert "US Shoe Size" not in tb.get_item(item_id)["listing"]["item_specifics"]


def test_aspect_button_for_another_category_is_out_of_date(bot):
    item_id = _seed()
    tap, ctx = make_callback_update(f"asp:{item_id}:99999:1:16")
    run(flow.aspect_callback, tap, ctx)
    assert "out of date" in sent_texts(ctx)[-1]
    assert "US Shoe Size" not in tb.get_item(item_id)["listing"]["item_specifics"]
