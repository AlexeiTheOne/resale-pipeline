"""Characterisation tests for the draft-review round trip.

Covers _show_review, review_callback (review:approve / review:reject),
text_handler's review branch (typed approve/reject/price/correction ->
revise_draft) and the identify-gate correction (-> revise_identification).
Everything that would call an LLM or eBay is stubbed; httpx is booby-trapped.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import telegram_bot as tb  # noqa: E402
from tg_fakes import (  # noqa: E402
    DEFAULT_USER_ID as UID, FakeMessage, bot_state, last_markup_buttons,
    make_callback_update, make_text_update, run, run_steps, sent_texts, tiny_jpeg,
)

IDENT = {"brand": "Nike", "model": "Air Max", "confidence": 0.9}
PRICING = {"suggested_price": 40.0, "machine_price": 40.0, "confidence": "high"}
LISTING = {"title": "Nike Air Max Shoes Size 10", "price": 39.99,
           "description": "Nice shoes."}


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    import httpx

    def boom(*a, **k):
        raise AssertionError("test tried to use the network via httpx")
    monkeypatch.setattr(httpx.Client, "send", boom)
    monkeypatch.setattr(httpx.AsyncClient, "send", boom)


@pytest.fixture
def bot(monkeypatch):
    with bot_state(tb):
        # Any unstubbed LLM / eBay entry point fails loudly.
        def forbidden(name):
            def f(*a, **k):
                raise AssertionError(f"unstubbed call: {name}")
            return f
        for name in ("revise_draft", "revise_identification", "create_draft_offer"):
            monkeypatch.setattr(tb, name, forbidden(name))
        monkeypatch.setattr(tb, "check_listing_and_store", lambda item_id: None)
        yield tb


def seed(status="drafted", *, title=None, price=None, nphotos=2):
    folder = tb.INBOX / "flow_review_test"
    folder.mkdir(parents=True, exist_ok=True)
    paths = []
    for i in range(nphotos):
        p = folder / f"p{i}.jpg"
        p.write_bytes(tiny_jpeg())
        paths.append(str(p))
    item_id = tb.create_item(paths)
    tb.update_field(item_id, "identification", dict(IDENT))
    tb.update_field(item_id, "pricing", dict(PRICING))
    listing = dict(LISTING)
    if title:
        listing["title"] = title
    if price:
        listing["price"] = price
    tb.update_field(item_id, "listing", listing)
    tb.update_status(item_id, status)
    return item_id


def fake_revise(calls):
    """Deterministic revise_draft: appends the correction to the title."""
    def revise(current, text, identification):
        calls.append((dict(current), text, identification))
        out = dict(current)
        out["title"] = f"{current['title']} [{text}]"
        return out
    return revise


def show(item_id):
    update, ctx = make_text_update("x")
    run(lambda u, c: tb._show_review(UID, item_id, u.message), update, ctx)
    return ctx


def stub_ebay_ok(monkeypatch, seen):
    def create(item_id):
        seen.append(item_id)
        return {"sku": "SKU1", "offer_id": "OFF1"}
    monkeypatch.setattr(tb, "create_draft_offer", create)


# --- the review message ----------------------------------------------------

def test_review_message_shows_title_price_and_buttons(bot):
    item_id = seed()
    ctx = show(item_id)
    text = sent_texts(ctx)[0]
    assert text.startswith(f"📄 Draft {item_id[:8]}")
    assert LISTING["title"] in text and "PRICE: $39.99" in text
    assert "Nike Air Max" in text
    assert last_markup_buttons(ctx) == [
        ("✅ Approve", f"review:approve:{item_id}"),
        ("❌ Reject", f"review:reject:{item_id}"),
    ]
    assert tb.review[UID] == item_id and tb.last_item[UID] == item_id
    # The sent message is registered as a reply target for this item.
    assert item_id in tb.msg_target.values()


# --- typed corrections ------------------------------------------------------

def test_typed_correction_revises_and_reshows(bot, monkeypatch):
    calls = []
    monkeypatch.setattr(tb, "revise_draft", fake_revise(calls))
    item_id = seed("review")
    tb.review[UID] = item_id

    update, ctx = make_text_update("color is navy")
    run(tb.text_handler, update, ctx)

    assert len(calls) == 1
    current, text, ident = calls[0]
    assert current == LISTING and text == "color is navy" and ident == IDENT
    new_title = "Nike Air Max Shoes Size 10 [color is navy]"
    assert tb.get_item(item_id)["listing"]["title"] == new_title
    texts = sent_texts(ctx)
    assert texts[0] == "✏️ Applying your correction..."
    assert new_title in texts[-1] and texts[-1].startswith("📄 Draft")
    assert last_markup_buttons(ctx)[0][1] == f"review:approve:{item_id}"
    assert tb.review[UID] == item_id
    assert tb.get_item(item_id)["status"] == "review"  # status untouched


def test_two_corrections_build_on_each_other(bot, monkeypatch):
    calls = []
    monkeypatch.setattr(tb, "revise_draft", fake_revise(calls))
    item_id = seed()
    tb.review[UID] = item_id

    u1, ctx = make_text_update("one")
    u2, _ = make_text_update("two", context=ctx)
    run_steps((tb.text_handler, u1), (tb.text_handler, u2), ctx=ctx)

    assert calls[1][0]["title"] == "Nike Air Max Shoes Size 10 [one]"
    assert tb.get_item(item_id)["listing"]["title"] == \
        "Nike Air Max Shoes Size 10 [one] [two]"
    assert "[one] [two]" in sent_texts(ctx)[-1]


def test_correction_reruns_listing_check_when_enabled(bot, monkeypatch):
    checked = []
    monkeypatch.setattr(tb, "revise_draft", fake_revise([]))
    monkeypatch.setattr(tb, "LISTING_CHECK_ENABLED", True)
    monkeypatch.setattr(tb, "check_listing_and_store", checked.append)
    item_id = seed()
    tb.review[UID] = item_id
    update, ctx = make_text_update("fix it")
    run(tb.text_handler, update, ctx)
    assert checked == [item_id]


def test_price_correction_skips_revise_draft_and_records_manual_price(bot):
    item_id = seed()
    tb.review[UID] = item_id
    update, ctx = make_text_update("35")  # revise_draft is forbidden by fixture
    run(tb.text_handler, update, ctx)

    item = tb.get_item(item_id)
    assert item["listing"]["price"] == 34.99  # charm-priced
    assert item["pricing"]["manual_price"] == 34.99
    assert item["pricing"]["machine_price"] == 40.0
    assert item["pricing"]["price_basis"] == "manual"
    texts = sent_texts(ctx)
    assert texts[0] == f"💲 Price set to $34.99 for {item_id[:8]}."
    assert "PRICE: $34.99" in texts[-1]


def test_revise_failure_reports_error_and_leaves_draft_unchanged(bot, monkeypatch):
    def broken(*a, **k):
        raise RuntimeError("model exploded")
    monkeypatch.setattr(tb, "revise_draft", broken)
    item_id = seed()
    tb.review[UID] = item_id
    before = tb.get_item(item_id)["listing"]

    update, ctx = make_text_update("make it blue")
    run(tb.text_handler, update, ctx)

    assert tb.get_item(item_id)["listing"] == before
    assert sent_texts(ctx)[-1] == "⚠️ Could not revise: model exploded"
    assert not any(t.startswith("📄 Draft") for t in sent_texts(ctx))
    assert tb.review[UID] == item_id  # still in review, can retry


def test_text_outside_review_does_not_revise(bot):
    update, ctx = make_text_update("make it blue")  # revise_draft forbidden
    run(tb.text_handler, update, ctx)
    assert sent_texts(ctx) == ["Send me photos of an item to start."]


def test_reply_to_draft_message_targets_that_item(bot, monkeypatch):
    """With draft B active, replying to draft A's message revises A."""
    calls = []
    monkeypatch.setattr(tb, "revise_draft", fake_revise(calls))
    a = seed(title="Item A title")
    b = seed(title="Item B title")
    ctx = show(a)
    msg_id = next(k for k, v in tb.msg_target.items() if v == a)
    show(b)
    assert tb.review[UID] == b

    target = FakeMessage(ctx.transcript, message_id=msg_id)
    update, ctx = make_text_update("tweak", reply_to=target, context=ctx)
    run(tb.text_handler, update, ctx)

    assert calls[0][0]["title"] == "Item A title"
    assert tb.get_item(a)["listing"]["title"].endswith("[tweak]")
    assert tb.get_item(b)["listing"]["title"] == "Item B title"
    assert tb.review[UID] == a


# --- buttons ---------------------------------------------------------------

def test_approve_button_creates_ebay_draft(bot, monkeypatch):
    seen = []
    stub_ebay_ok(monkeypatch, seen)
    item_id = seed()
    ctx = show(item_id)
    tap, _ = make_callback_update(dict(last_markup_buttons(ctx))["✅ Approve"],
                                  context=ctx)
    run(tb.review_callback, tap, ctx)

    assert seen == [item_id]
    item = tb.get_item(item_id)
    assert item["status"] == "ebay_draft"
    assert item["ebay"] == {"sku": "SKU1", "offer_id": "OFF1"}
    assert UID not in tb.review
    assert tb.last_item[UID] == item_id
    assert any("Draft created on eBay (SKU SKU1, offer OFF1)" in t
               for t in sent_texts(ctx))
    # buttons were stripped from the draft message
    stripped = ctx.transcript.of("edit_message_reply_markup")
    assert stripped[0].kwargs["reply_markup"] is None


def test_approve_button_ebay_failure_leaves_item_approved(bot, monkeypatch):
    def fail(item_id):
        raise RuntimeError("ebay down")
    monkeypatch.setattr(tb, "create_draft_offer", fail)
    item_id = seed()
    tap, ctx = make_callback_update(f"review:approve:{item_id}")
    run(tb.review_callback, tap, ctx)
    assert tb.get_item(item_id)["status"] == "approved"
    assert "eBay draft creation failed: RuntimeError: ebay down" in sent_texts(ctx)[-1]


def test_title_too_long_sends_item_back_to_review(bot, monkeypatch):
    err = tb.TitleTooLongError.__new__(tb.TitleTooLongError)  # skip its ctor

    def raiser(item_id):
        raise err
    monkeypatch.setattr(tb, "create_draft_offer", raiser)
    monkeypatch.setattr(tb, "_title_too_long_text", lambda e, i: "TITLE TOO LONG")
    item_id = seed()
    tap, ctx = make_callback_update(f"review:approve:{item_id}")
    run(tb.review_callback, tap, ctx)
    assert tb.get_item(item_id)["status"] == "review"
    assert sent_texts(ctx)[-1] == "TITLE TOO LONG"


def test_reject_button_rejects_and_clears_review(bot):
    item_id = seed()
    ctx = show(item_id)
    tap, _ = make_callback_update(f"review:reject:{item_id}", context=ctx)
    run(tb.review_callback, tap, ctx)  # create_draft_offer is forbidden
    assert tb.get_item(item_id)["status"] == "rejected"
    assert UID not in tb.review
    assert tb.last_item[UID] == item_id
    assert sent_texts(ctx)[-1] == f"❌ Rejected {item_id[:8]}."


def test_button_on_other_draft_keeps_current_review_target(bot):
    a, b = seed(), seed()
    show(a)
    show(b)
    tap, ctx = make_callback_update(f"review:reject:{a}")
    run(tb.review_callback, tap, ctx)
    assert tb.get_item(a)["status"] == "rejected"
    assert tb.review[UID] == b      # b is still the typed-correction target
    assert tb.last_item[UID] == a   # but last_item follows the tapped one


def test_button_on_already_actioned_item_does_nothing(bot):
    item_id = seed("rejected")
    tap, ctx = make_callback_update(f"review:approve:{item_id}")
    run(tb.review_callback, tap, ctx)  # would fail loudly if it approved
    assert tb.get_item(item_id)["status"] == "rejected"
    assert sent_texts(ctx) == [f"Item {item_id[:8]} is 'rejected' — nothing to review."]


def test_button_on_missing_item_and_malformed_data(bot):
    tap, ctx = make_callback_update("review:approve:no-such-id")
    run(tb.review_callback, tap, ctx)
    assert sent_texts(ctx) == ["This item no longer exists."]

    tap, ctx = make_callback_update("review:approve")  # no item id
    run(tb.review_callback, tap, ctx)
    assert sent_texts(ctx) == []


def test_unknown_action_strips_buttons_but_changes_nothing(bot):
    # NOTE: looks like a bug (minor): any unrecognised review:<action> still
    # removes the buttons, pops review and sets last_item, then silently does
    # nothing, so the draft is left with no buttons. Only approve/reject exist
    # today, so this is unreachable from the real keyboard.
    item_id = seed()
    tb.review[UID] = item_id
    tap, ctx = make_callback_update(f"review:edit:{item_id}")
    run(tb.review_callback, tap, ctx)
    assert tb.get_item(item_id)["status"] == "drafted"
    assert UID not in tb.review
    assert len(ctx.transcript.of("edit_message_reply_markup")) == 1
    assert sent_texts(ctx) == []


# --- typed approve / reject ------------------------------------------------

@pytest.mark.parametrize("word", ["approve", "ok", "yes", "✅"])
def test_typed_approve(bot, monkeypatch, word):
    seen = []
    stub_ebay_ok(monkeypatch, seen)
    item_id = seed()
    tb.review[UID] = item_id
    update, ctx = make_text_update(word)
    run(tb.text_handler, update, ctx)
    assert seen == [item_id]
    assert tb.get_item(item_id)["status"] == "ebay_draft"
    assert UID not in tb.review


@pytest.mark.parametrize("word", ["reject", "no", "❌"])
def test_typed_reject(bot, word):
    item_id = seed()
    tb.review[UID] = item_id
    update, ctx = make_text_update(word)
    run(tb.text_handler, update, ctx)
    assert tb.get_item(item_id)["status"] == "rejected"
    assert UID not in tb.review
    assert sent_texts(ctx) == ["❌ Rejected."]


def test_correct_then_approve_publishes_revised_listing(bot, monkeypatch):
    monkeypatch.setattr(tb, "revise_draft", fake_revise([]))
    seen = []

    def create(item_id):
        seen.append(tb.get_item(item_id)["listing"]["title"])
        return {"sku": "S", "offer_id": "O"}
    monkeypatch.setattr(tb, "create_draft_offer", create)
    item_id = seed()
    ctx = show(item_id)
    u1, _ = make_text_update("navy", context=ctx)
    run(tb.text_handler, u1, ctx)
    tap, _ = make_callback_update(f"review:approve:{item_id}", context=ctx)
    run(tb.review_callback, tap, ctx)
    assert seen == ["Nike Air Max Shoes Size 10 [navy]"]
    assert tb.get_item(item_id)["status"] == "ebay_draft"


# --- identify gate ---------------------------------------------------------

def test_identify_gate_correction_uses_revise_identification(bot, monkeypatch):
    calls = []

    def revise_ident(ident, text):
        calls.append((dict(ident), text))
        return {**ident, "brand": "Adidas"}
    monkeypatch.setattr(tb, "revise_identification", revise_ident)
    item_id = seed("identified")
    tb.gate[UID] = {"item_id": item_id, "stage": "identify"}

    update, ctx = make_text_update("brand is Adidas")  # revise_draft forbidden
    run(tb.text_handler, update, ctx)

    assert calls == [(IDENT, "brand is Adidas")]
    assert tb.get_item(item_id)["identification"]["brand"] == "Adidas"
    texts = sent_texts(ctx)
    assert texts[0] == "✏️ Updating the identification..."
    assert "Adidas" in texts[-1]
    assert tb.gate[UID] == {"item_id": item_id, "stage": "identify"}


def test_identify_gate_revise_failure_keeps_identification(bot, monkeypatch):
    def broken(ident, text):
        raise ValueError("bad json")
    monkeypatch.setattr(tb, "revise_identification", broken)
    item_id = seed("identified")
    tb.gate[UID] = {"item_id": item_id, "stage": "identify"}
    update, ctx = make_text_update("brand is Adidas")
    run(tb.text_handler, update, ctx)
    assert tb.get_item(item_id)["identification"] == IDENT
    assert sent_texts(ctx)[-1] == "⚠️ Could not update: ValueError: bad json"


def test_typed_approve_while_gated_goes_to_review_not_gate(bot, monkeypatch):
    seen = []
    stub_ebay_ok(monkeypatch, seen)
    gated = seed("identified")
    drafted = seed()
    tb.gate[UID] = {"item_id": gated, "stage": "identify"}
    tb.review[UID] = drafted
    update, ctx = make_text_update("approve")  # revise_identification forbidden
    run(tb.text_handler, update, ctx)
    assert seen == [drafted]
