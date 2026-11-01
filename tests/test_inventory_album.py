"""/inv: photos sent as one Telegram album are ONE piece (1 Nov 2026).

Before, every photo was its own piece, so an item that needed two photos (item
+ tag close-up) got two codes. Now the album's first photo is the piece's
photo, the rest sit beside it (O1-03_2.jpg), and all are read in one go.
"""
import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import db  # noqa: E402
import telegram_bot as tb  # noqa: E402
from bot import inventory_cmds, state  # noqa: E402
from pipeline import inventory  # noqa: E402
from tg_fakes import (  # noqa: E402
    DEFAULT_USER_ID, bot_state, make_photo_update, run_steps, sent_texts,
)


@pytest.fixture
def inv(monkeypatch, tmp_path):
    """Inventory mode in box O1-WATCHES; reading is stubbed to record the photos."""
    monkeypatch.setattr(inventory_cmds, "_PIECES_DIR", tmp_path)
    monkeypatch.setattr(inventory_cmds, "INV_ALBUM_WAIT", 0.01)
    read = []

    def fake_process(code, ask=None):
        piece = db.get_piece(code)
        read.append((code, inventory.piece_photos(piece)))
        return {"code": code}
    monkeypatch.setattr(inventory, "process_piece", fake_process)
    monkeypatch.setattr(inventory, "describe", lambda res: f"{res['code']} read")
    with bot_state(tb):
        state.inv_box[DEFAULT_USER_ID] = "O1-WATCHES"
        yield read


async def _settle(_update, _ctx):
    """inventory_cmds starts plain asyncio tasks (drain() only sees the
    application's), so give the album countdown and the read time to finish."""
    await asyncio.sleep(0.2)


def _send(ups, ctx):
    run_steps(*[(tb.photo_handler, u) for u in ups], (_settle, None), ctx=ctx)


def _photos(n, group=None):
    first, ctx = make_photo_update(media_group_id=group)
    ups = [first] + [make_photo_update(context=ctx, media_group_id=group)[0] for _ in range(n - 1)]
    return ups, ctx


def test_album_is_one_piece_with_every_photo_read(inv):
    ups, ctx = _photos(3, group="album-1")
    _send(ups, ctx)

    codes = state.inv_session[DEFAULT_USER_ID]
    assert len(codes) == 1
    code = codes[0]
    assert len(inv) == 1 and inv[0][0] == code
    photos = inv[0][1]
    assert [Path(p).name for p in photos] == [f"{code}.jpg", f"{code}_2.jpg", f"{code}_3.jpg"]
    assert all(Path(p).exists() for p in photos)
    # One reply for the whole album, edited to the result once read.
    assert sent_texts(ctx) == [f"{code} ✓ (album: reading all photos)", f"{code} read"]
    assert state.inv_album == {}


def test_separate_photos_are_still_separate_pieces(inv):
    ups, ctx = _photos(2)
    _send(ups, ctx)
    assert len(state.inv_session[DEFAULT_USER_ID]) == 2
    assert [len(p) for _, p in inv] == [1, 1]


def test_two_albums_make_two_pieces(inv):
    a, ctx = make_photo_update(media_group_id="a")
    a2, _ = make_photo_update(context=ctx, media_group_id="a")
    b, _ = make_photo_update(context=ctx, media_group_id="b")
    b2, _ = make_photo_update(context=ctx, media_group_id="b")
    _send([a, a2, b, b2], ctx)
    assert len(state.inv_session[DEFAULT_USER_ID]) == 2
    assert sorted(len(p) for _, p in inv) == [2, 2]


def test_piece_photos_orders_extras_numerically(tmp_path):
    main = tmp_path / "O1-03.jpg"
    for name in ("O1-03.jpg", "O1-03_10.jpg", "O1-03_2.jpg", "O1-030.jpg", "O1-03_x.jpg"):
        (tmp_path / name).write_bytes(b"x")
    names = [Path(p).name for p in inventory.piece_photos({"photo": str(main)})]
    assert names == ["O1-03.jpg", "O1-03_2.jpg", "O1-03_10.jpg"]
    assert inventory.piece_photos({"photo": None}) == []


# --- 📤 Post it straight from /inv ------------------------------------------

READY = {"brand": "Oakley", "item_type": "sunglasses", "upc": "012345678905",
         "ross_tag_visible": False, "ross_price": None, "ross_original_price": None}


def test_post_check_needs_everything_and_no_ross_tag(monkeypatch):
    monkeypatch.setattr(inventory.ross_tag, "detect_ross_tags", lambda paths: set())
    assert inventory.post_check(dict(READY), ["a.jpg"]) is None
    assert inventory.post_check({**READY, "upc": None, "style_code": "OO9208"}, ["a.jpg"]) is None
    for change, why in (({"ross_tag_visible": True}, "Ross tag"),
                        ({"ross_tag_visible": None}, "Ross tag"),   # unsure = no
                        ({"ross_price": 19.99}, "Ross tag"),
                        ({"ross_code": "400123456789"}, "Ross tag"),
                        ({"brand": None}, "brand"),
                        ({"item_type": None}, "item"),
                        ({"upc": None}, "UPC")):
        assert why in inventory.post_check({**READY, **change}, ["a.jpg"]), change
    # The dedicated tag check has the last word.
    monkeypatch.setattr(inventory.ross_tag, "detect_ross_tags", lambda paths: {0})
    assert "Ross tag" in inventory.post_check(dict(READY), ["a.jpg"])


def _process_for_real(monkeypatch, info, tag_seen=False):
    """Run the real process_piece with the photo reading stubbed."""
    monkeypatch.setattr(inventory, "process_piece", _real_process)
    monkeypatch.setattr(inventory, "read_tag", lambda _p: dict(info))
    monkeypatch.setattr(inventory, "find_match", lambda *a, **k: (None, None))
    monkeypatch.setattr(inventory, "rough_value", lambda *a, **k: (30.0, "msrp"))
    monkeypatch.setattr(inventory, "describe", lambda res: f"{res['code']} read")
    monkeypatch.setattr(inventory.ross_tag, "detect_ross_tags",
                        lambda paths: {0} if tag_seen else set())


_real_process = inventory.process_piece


def _buttons(ctx):
    from tg_fakes import last_markup_buttons
    try:
        return dict(last_markup_buttons(ctx))
    except Exception:
        return {}


def test_post_button_only_when_ready(inv, monkeypatch):
    _process_for_real(monkeypatch, READY)
    ups, ctx = _photos(1)
    _send(ups, ctx)
    code = state.inv_session[DEFAULT_USER_ID][-1]
    assert _buttons(ctx).get("📤 Post it") == f"invpost:{code}"
    item = db.get_item(db.get_piece(code)["item_id"])
    assert item["stock"]["post_ready"] is True and item["status"] == "stocked"


def test_no_post_button_with_a_ross_tag(inv, monkeypatch):
    _process_for_real(monkeypatch, {**READY, "ross_tag_visible": True, "ross_price": 19.99})
    ups, ctx = _photos(1)
    _send(ups, ctx)
    code = state.inv_session[DEFAULT_USER_ID][-1]
    assert "📤 Post it" not in _buttons(ctx)
    item = db.get_item(db.get_piece(code)["item_id"])
    assert item["stock"]["post_ready"] is False
    assert item["stock"]["post_blocker"] == "Ross tag in the photo"
    assert item["receipt"]["reduced_price"] == 19.99      # the tag data is still saved
    assert db.get_piece(code)["info"]["ross_price"] == 19.99


def test_post_tap_starts_the_listing_once(inv, monkeypatch):
    from bot import flow
    from tg_fakes import make_callback_update, run
    _process_for_real(monkeypatch, READY)
    ups, ctx = _photos(1)
    _send(ups, ctx)
    code = state.inv_session[DEFAULT_USER_ID][-1]
    item_id = db.get_piece(code)["item_id"]
    started = []

    async def fake_advance(user_id, iid, message, context):
        started.append(iid)
    monkeypatch.setattr(flow, "advance", fake_advance)

    tap, _ = make_callback_update(f"invpost:{code}", context=ctx)
    run(inventory_cmds.inv_post_callback, tap, ctx)
    assert started == [item_id]
    assert db.get_item(item_id)["status"] == "identified"
    # A second tap doesn't post it twice.
    tap2, _ = make_callback_update(f"invpost:{code}", context=ctx)
    run(inventory_cmds.inv_post_callback, tap2, ctx)
    assert started == [item_id]
    assert "can't be posted" in sent_texts(ctx)[-1]
