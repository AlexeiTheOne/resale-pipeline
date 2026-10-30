"""Photo intake, end to end through photo_handler (cleanup-plan Phase 1, item 1).

Characterisation tests of TODAY's behaviour, written as a safety net for the
telegram_bot.py -> bot/ split. They drive the real handlers with the fakes in
tests/tg_fakes.py and stub only the leaf calls that would reach a model or the
network, so the stage code (_stage_identify / _stage_price / _stage_draft,
_split_receipt, the gates, _show_review) really runs:

    identify_item, decode_barcode, detect_ross_tags, extract_receipt,
    generate_draft                       -> patched on telegram_bot
    pipeline.pricing_v2.get_pricing_v2   -> patched on its module (imported
                                            inside _stage_price at call time)
    pipeline.listing_check.check_listing -> patched on its module
    pipeline.stock_lookup.lookup         -> patched on its module (find mode)

A socket guard refuses any non-loopback connection, so a missed stub fails the
test instead of calling out.
"""
import socket
import sqlite3
import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import config  # noqa: E402
import db  # noqa: E402
import telegram_bot as tb  # noqa: E402
import pipeline.listing_check as listing_check  # noqa: E402
import pipeline.pricing_v2 as pricing_v2  # noqa: E402
import pipeline.stock_lookup as stock_lookup  # noqa: E402
from tg_fakes import (  # noqa: E402
    DEFAULT_USER_ID, bot_state, last_markup_buttons, make_callback_update,
    make_photo_update, make_text_update, run, run_steps, sent_texts, tiny_jpeg,
)

UID = DEFAULT_USER_ID
CONFIRM_TEXT = "{n} photo(s) buffered. Start the listing, wait for more photos, or cancel?"


# --------------------------------------------------------------------------- #
# Fixtures and helpers (local to this file)
# --------------------------------------------------------------------------- #

_LOOPBACK = {"127.0.0.1", "::1", "localhost"}


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Refuse any outbound connection. Loopback stays open: asyncio on Windows
    builds its self-pipe with a localhost socketpair."""
    real_connect = socket.socket.connect

    def guarded_connect(self, address, *a, **k):
        host = address[0] if isinstance(address, tuple) else address
        if host not in _LOOPBACK:
            raise AssertionError(f"test tried the network: connect({address!r})")
        return real_connect(self, address, *a, **k)

    real_gai = socket.getaddrinfo

    def guarded_gai(host, *a, **k):
        if host not in _LOOPBACK and host is not None:
            raise AssertionError(f"test tried the network: getaddrinfo({host!r})")
        return real_gai(host, *a, **k)

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket, "getaddrinfo", guarded_gai)


@pytest.fixture
def bot(monkeypatch):
    with bot_state(tb):
        monkeypatch.setattr(tb, "DEFAULT_CAPTURE_WINDOW", 0)
        monkeypatch.setattr(tb, "WAIT_EXTENSION", 0)
        yield tb


class Pipeline:
    """Leaf stubs for identify -> price -> draft. Each stub records the item's
    DB status at the moment it ran, so the tests can assert the transitions."""

    def __init__(self, monkeypatch, *, confidence=0.95, identify_error=None):
        # Unique brand/model per test: the temp DB is shared across the session,
        # and _duplicate_warnings / the brand scoreboard look at every item in it.
        tag = uuid.uuid4().hex[:6]
        self.brand = f"Brand{tag}"
        self.model = f"MDL{tag}"
        self.confidence = confidence
        self.identify_error = identify_error
        self.seen = []            # (stage, status-at-call)
        self.identify_photos = None
        self.receipt_photo = None
        self.current_item = None

        monkeypatch.setattr(tb, "AUTO_CONFIRM", True)
        monkeypatch.setattr(tb, "AUTO_CONFIRM_MIN_IDENT_CONFIDENCE", 0.8)
        monkeypatch.setattr(tb, "AUTO_CONFIRM_MIN_IDENT_CONFIDENCE_WITH_UPC", 0.6)
        monkeypatch.setattr(tb, "PRICING_ENGINE", "v2")
        monkeypatch.setattr(tb, "LISTING_CHECK_ENABLED", True)
        monkeypatch.setattr(tb, "EBAY_DEFAULT_AD_RATE_PCT", 0)
        monkeypatch.setattr(tb, "decode_barcode", lambda path: None)
        monkeypatch.setattr(tb, "detect_ross_tags", lambda paths: set())
        monkeypatch.setattr(tb, "extract_receipt", self._extract_receipt)
        monkeypatch.setattr(tb, "identify_item", self._identify)
        monkeypatch.setattr(pricing_v2, "get_pricing_v2", self._price)
        monkeypatch.setattr(tb, "generate_draft", self._draft)
        monkeypatch.setattr(listing_check, "check_listing", self._check)

    def _status(self):
        return tb.get_item(self.current_item)["status"]

    def _extract_receipt(self, path):
        self.receipt_photo = path
        return {"reduced_price": 9.99, "code": "400123456789", "original_price": 25.0}

    def _identify(self, photos, scan_paths=None):
        self.identify_photos = list(photos)
        self.seen.append(("identify", self._status()))
        if self.identify_error is not None:
            raise self.identify_error
        return {"brand": self.brand, "model": self.model, "product_name": "Canvas Tote",
                "item_type": "tote bag", "color": "navy", "size": None,
                "condition": "New with tags", "condition_flags": [],
                "confidence": self.confidence, "upc": None,
                "search_query": f"{self.brand} canvas tote"}

    def _price(self, ident, *, photo_paths=None, receipt=None, item_id=None, ask=None):
        self.seen.append(("price", self._status()))
        self.price_receipt = receipt
        return {"engine": "v2", "suggested_price": 24.99, "machine_price": 24.99,
                "confidence": "high", "claude_confidence": "high",
                "needs_review": False, "review_flags": [], "evidence": "solid",
                "sold_count": 3, "active_count": 2, "candidate_count": 5}

    def _draft(self, ident, pricing):
        self.seen.append(("draft", self._status()))
        return {"title": f"{self.brand} Canvas Tote Bag Navy NWT", "price": 24.99,
                "description": "A navy canvas tote."}

    def _check(self, item):
        self.seen.append(("check", item["status"]))
        return {"issues": []}


def send_batch(n, ctx=None):
    """Send n photos as one batch (one loop, no drain between) and return
    (ctx, item_id, file_ids) once the confirmation is out."""
    updates = []
    for _ in range(n):
        u, ctx = make_photo_update(context=ctx)
        updates.append(u)
    run_steps(*[(tb.photo_handler, u) for u in updates], ctx=ctx)
    item_id = tb.staging[UID]["item_id"]
    return ctx, item_id, updates


def run_again(handler, update, ctx):
    """tg_fakes.run() on a context that already ran on an earlier event loop.

    Every run()/run_steps() is its own asyncio.run, but drain() awaits ALL of
    ctx.application.tasks, including finished tasks from the previous loop;
    mixing those with new ones makes asyncio.gather raise "The future belongs to
    a different loop". They're all done by the time run() returned, so forget
    them first."""
    ctx.application.tasks.clear()
    return run(handler, update, ctx)


def tap(ctx, data, handler=None):
    upd, _ = make_callback_update(data, context=ctx)
    run_again(handler or tb.confirm_callback, upd, ctx)


def raw_photos(item_id):
    """The photos column as stored, bypassing db's absolute-path mapping."""
    import json
    with sqlite3.connect(db.DB_PATH) as con:
        row = con.execute("SELECT photos FROM items WHERE item_id = ?", (item_id,)).fetchone()
    return json.loads(row[0])


def buttons_of(call):
    markup = call.kwargs.get("reply_markup")
    if markup is None:
        return []
    return [(b.text, b.callback_data) for row in markup.inline_keyboard for b in row]


# --------------------------------------------------------------------------- #
# 1-3: capture, batching, Wait, Cancel
# --------------------------------------------------------------------------- #

def test_single_photo_is_buffered_stored_and_staged(bot, monkeypatch):
    # The real layout keeps data/ inside BASE_DIR, so paths are stored relative.
    # The test DATA_DIR is a temp dir outside the repo; point BASE_DIR at it to
    # reproduce that layout (otherwise stored_path keeps them absolute).
    monkeypatch.setattr(config, "BASE_DIR", config.DATA_DIR)
    image = tiny_jpeg((10, 200, 10))
    upd, ctx = make_photo_update(image)
    run(tb.photo_handler, upd, ctx)

    assert sent_texts(ctx) == [CONFIRM_TEXT.format(n=1)]
    item_id = tb.staging[UID]["item_id"]
    assert last_markup_buttons(ctx) == [
        ("Start", f"confirm:start:{item_id}"),
        ("Wait", f"confirm:wait:{item_id}"),
        ("Cancel", f"confirm:cancel:{item_id}"),
    ]
    # The confirmation goes to the chat via bot.send_message, not as a reply.
    assert ctx.transcript.of("send_message")[0].kwargs["chat_id"] == UID

    item = tb.get_item(item_id)
    assert item["status"] == "captured"
    assert len(item["photos"]) == 1
    photo = Path(item["photos"][0])
    assert photo.is_absolute()
    assert photo.parent.parent == tb.INBOX
    assert photo.read_bytes() == image
    stored = raw_photos(item_id)
    assert not Path(stored[0]).is_absolute()
    assert Path(stored[0]).parts[0] == "inbox"
    assert config.resolve_path(stored[0]) == photo
    # The in-memory buffer is consumed; only the staging slot remains.
    assert UID not in tb.pending


def test_multi_photo_batch_makes_one_item_in_send_order(bot):
    before = len(tb.list_items())
    ctx, item_id, updates = send_batch(3)

    assert sent_texts(ctx) == [CONFIRM_TEXT.format(n=3)]
    assert len(tb.list_items()) == before + 1
    photos = tb.get_item(item_id)["photos"]
    assert len(photos) == 3
    assert len({Path(p).parent for p in photos}) == 1        # one folder per item
    folder = Path(photos[0]).parent.name
    mids = [u.message.message_id for u in updates]
    assert [Path(p).name for p in photos] == [f"{folder}_{m}.jpg" for m in mids]
    assert all(Path(p).exists() for p in photos)


def test_inbox_folder_is_named_by_staging_id_not_item_id(bot):
    ctx, item_id, _ = send_batch(1)
    folder = Path(tb.get_item(item_id)["photos"][0]).parent
    assert folder.name != item_id


def test_cancel_deletes_item_and_photo_folder(bot):
    ctx, item_id, _ = send_batch(2)
    folder = Path(tb.get_item(item_id)["photos"][0]).parent
    assert folder.exists()

    tap(ctx, f"confirm:cancel:{item_id}")

    assert sent_texts(ctx)[-1] == "Cancelled. Photos discarded."
    assert ctx.transcript.of("edit_message_text")[-1].text == "Cancelled. Photos discarded."
    assert tb.get_item(item_id) is None
    assert not folder.exists()
    assert UID not in tb.staging
    # Nothing ran: no gate, no review, no further messages.
    assert UID not in tb.gate and UID not in tb.review
    assert len(sent_texts(ctx)) == 2


def test_wait_then_extra_photo_joins_the_staged_item(bot):
    ctx, item_id, _ = send_batch(2)

    tap(ctx, f"confirm:wait:{item_id}")
    # WAIT_EXTENSION is 0, so the timer re-shows the confirmation at once.
    assert sent_texts(ctx)[-2:] == ["Waiting 0s for more photos. Send them now.",
                                    CONFIRM_TEXT.format(n=2)]

    extra, _ = make_photo_update(context=ctx)
    run_again(tb.photo_handler, extra, ctx)

    assert tb.staging[UID]["item_id"] == item_id
    photos = tb.get_item(item_id)["photos"]
    assert len(photos) == 3
    assert Path(photos[-1]).name.endswith(f"_{extra.message.message_id}.jpg")
    assert sent_texts(ctx)[-1] == CONFIRM_TEXT.format(n=3)
    assert dict(last_markup_buttons(ctx))["Start"] == f"confirm:start:{item_id}"


def test_start_on_a_stale_button_does_nothing(bot):
    ctx, item_id, _ = send_batch(1)
    tap(ctx, "confirm:start:not-the-staged-item")
    assert sent_texts(ctx)[-1] == "This item is no longer waiting."
    assert tb.staging[UID]["item_id"] == item_id
    assert tb.get_item(item_id)["status"] == "captured"


# --------------------------------------------------------------------------- #
# 4: Start -> identify -> price -> draft -> review
# --------------------------------------------------------------------------- #

def test_start_runs_through_to_review_when_gates_clear(bot, monkeypatch):
    pipe = Pipeline(monkeypatch, confidence=0.95)
    ctx, item_id, _ = send_batch(2)
    pipe.current_item = item_id
    photos = tb.get_item(item_id)["photos"]

    tap(ctx, f"confirm:start:{item_id}")

    # Status at each stage, read from the DB as the stub ran.
    assert pipe.seen == [("identify", "captured"), ("price", "identified"),
                         ("draft", "priced"), ("check", "drafted")]
    item = tb.get_item(item_id)
    assert item["status"] == "review"
    assert item["identification"]["brand"] == pipe.brand
    assert item["pricing"]["suggested_price"] == 24.99
    assert item["listing"]["title"].startswith(pipe.brand)
    assert item["listing_check"] == {"issues": []}

    # The last photo is peeled off as the Ross tag: OCR'd, kept off the listing.
    assert pipe.receipt_photo == photos[-1]
    assert item["photos"] == photos[:1]
    assert pipe.identify_photos == photos[:1]
    assert item["receipt"]["reduced_price"] == 9.99 and item["receipt"]["peeled"] is True
    assert pipe.price_receipt["code"] == "400123456789"

    texts = sent_texts(ctx)
    assert texts[0] == CONFIRM_TEXT.format(n=2)
    assert texts[1] == "Starting. 2 photo(s). Processing..."
    for expected in (
        "🧾 Receipt: paid $9.99 (orig $25.0) · code 400123456789",
        "🔬 Identifying the item...",
        f"✅ {pipe.brand} Canvas Tote — identified, pricing...",
        "💰 Pricing...",
        "✅ $24.99 — Claude, high confidence · 3 same-item sale(s), 2 active "
        "— writing the listing...",
        "✍️ Writing the listing...",
        "🔎 Checking the listing...",
    ):
        assert expected in texts, expected
    # The bot's own lines come in pipeline order.
    order = [texts.index(f"✅ {pipe.brand} Canvas Tote — identified, pricing..."),
             texts.index("✍️ Writing the listing...")]
    assert order == sorted(order)

    review_call = ctx.transcript[-1]
    assert review_call.text.startswith(f"📄 Draft {item_id[:8]}\n✓ {pipe.brand} {pipe.model}")
    assert "PRICE: $24.99" in review_call.text
    assert item["listing"]["title"] in review_call.text
    assert review_call.kwargs.get("parse_mode") is None
    assert buttons_of(review_call) == [("✅ Approve", f"review:approve:{item_id}"),
                                       ("❌ Reject", f"review:reject:{item_id}")]
    assert tb.review[UID] == item_id
    assert tb.last_item[UID] == item_id
    assert UID not in tb.gate
    assert UID not in tb.staging
    # Never reaches eBay on its own: the review gate is never skipped.
    assert not any("eBay" in t for t in texts)


@pytest.mark.parametrize("auto_confirm, confidence, reason", [
    (False, 0.95, "auto-confirm is off"),
    (True, 0.40, "confidence 0.40 is below 0.80"),
])
def test_start_stops_at_identify_gate(bot, monkeypatch, auto_confirm, confidence, reason):
    pipe = Pipeline(monkeypatch, confidence=confidence)
    monkeypatch.setattr(tb, "AUTO_CONFIRM", auto_confirm)
    ctx, item_id, _ = send_batch(1)
    pipe.current_item = item_id

    tap(ctx, f"confirm:start:{item_id}")

    assert pipe.seen == [("identify", "captured")]      # price/draft never ran
    item = tb.get_item(item_id)
    assert item["status"] == "identified"
    assert item["pricing"] is None and item["listing"] is None
    # A single photo has nothing to peel: no receipt, photo kept.
    assert pipe.receipt_photo is None
    assert len(item["photos"]) == 1

    gate_call = ctx.transcript[-1]
    assert gate_call.text.startswith(f"🔎 {item_id[:8]}\n🔍 Identified:\nBrand: {pipe.brand}")
    assert f"🛑 Needs you: {reason}" in gate_call.text
    assert gate_call.text.endswith(
        "Type 'confirm' to price it, or tell me what to fix "
        "(e.g. 'brand is Tommy Jeans, color navy').")
    # NOTE: the identify gate has no buttons today; it's confirmed by typing.
    assert buttons_of(gate_call) == []
    assert tb.gate[UID] == {"item_id": item_id, "stage": "identify"}
    assert UID not in tb.review
    assert item_id in tb.msg_target.values()   # a reply to the gate targets it
    assert not any(t.startswith("✅") for t in sent_texts(ctx))


def test_typed_confirm_at_identify_gate_continues_to_review(bot, monkeypatch):
    """The gate -> 'confirm' -> price -> draft hop, so the whole intake path is
    covered with the gate on."""
    pipe = Pipeline(monkeypatch)
    monkeypatch.setattr(tb, "AUTO_CONFIRM", False)
    ctx, item_id, _ = send_batch(1)
    pipe.current_item = item_id
    tap(ctx, f"confirm:start:{item_id}")
    assert tb.gate[UID]["stage"] == "identify"

    upd, _ = make_text_update("confirm", context=ctx)
    run_again(tb.text_handler, upd, ctx)

    # With auto-confirm off the price gate stops it next.
    assert tb.get_item(item_id)["status"] == "priced"
    assert tb.gate[UID] == {"item_id": item_id, "stage": "price"}
    assert "🛑 Needs you: auto-confirm is off" in sent_texts(ctx)[-1]
    assert sent_texts(ctx)[-1].startswith(f"💲 {item_id[:8]} Canvas Tote\n💰 Suggested price: $24.99")

    upd, _ = make_text_update("confirm", context=ctx)
    run_again(tb.text_handler, upd, ctx)

    assert tb.get_item(item_id)["status"] == "review"
    assert buttons_of(ctx.transcript[-1]) == [("✅ Approve", f"review:approve:{item_id}"),
                                              ("❌ Reject", f"review:reject:{item_id}")]
    assert UID not in tb.gate
    assert [s for s, _ in pipe.seen] == ["identify", "price", "draft", "check"]


# --------------------------------------------------------------------------- #
# 5: failure path
# --------------------------------------------------------------------------- #

def test_identify_failure_reports_error_and_leaves_item_captured(bot, monkeypatch):
    pipe = Pipeline(monkeypatch, identify_error=RuntimeError("vision model down"))
    ctx, item_id, _ = send_batch(1)
    pipe.current_item = item_id

    tap(ctx, f"confirm:start:{item_id}")     # drain() would re-raise a task error

    texts = sent_texts(ctx)
    assert texts[-1] == "⚠️ _stage_identify failed: RuntimeError: vision model down"
    item = tb.get_item(item_id)
    assert item["status"] == "captured"
    assert item["identification"] is None
    assert pipe.seen == [("identify", "captured")]
    assert UID not in tb.gate and UID not in tb.review
    # NOTE: the staging slot was consumed by Start, so the item now sits at
    # 'captured' with no Start button; the user's way back is /retry.
    assert UID not in tb.staging
    assert any("_stage_identify" in where for _, where, _ in tb._recent_errors)
    assert all(t.done() and not t.cancelled() and t.exception() is None
               for t in ctx.application.tasks)


# --------------------------------------------------------------------------- #
# 6: find mode
# --------------------------------------------------------------------------- #

def test_find_mode_photo_looks_up_and_creates_no_listing(bot, monkeypatch):
    looked_up = []

    def fake_lookup(path, ask=None, get_item_xml=None):
        looked_up.append(path)
        return {"item": None, "how": "none", "confidence": "low", "live": None, "maybe": None}

    monkeypatch.setattr(stock_lookup, "lookup", fake_lookup)
    upd, ctx = make_text_update("/find G3")
    run(tb.find_command, upd, ctx)
    assert sent_texts(ctx)[0].startswith("🔎 Find mode on. Box: G3.")

    before = len(tb.list_items())
    photo, _ = make_photo_update(context=ctx)
    run_again(tb.photo_handler, photo, ctx)

    assert len(tb.list_items()) == before
    assert UID not in tb.pending and UID not in tb.staging
    assert ctx.application.tasks == []
    assert len(looked_up) == 1 and Path(looked_up[0]).exists()
    assert Path(looked_up[0]).parent == tb._FIND_DIR
    assert tb.last_find_photo[UID] == looked_up[0]
    last = ctx.transcript[-1]
    assert last.text == "❌ Not posted. Nothing in the shop matches."
    assert buttons_of(last) == [("⚡ Quick add", "qa:start")]
