"""Argument parsing for the commands that mutate live listings.

/setprice, /setqty and /sold all take an optional trailing number after an
optional item id, which makes "is this token an id or a value?" the single most
dangerous question in the command surface. Every case below is a real defect.

Run with `python tests/test_parsing.py` or `pytest tests/`.
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import telegram_bot as tb


def test_an_item_id_is_never_read_as_a_price():
    """`/setprice a1b2c3d4` — the price forgotten — used to match the "1" inside
    the id, charm-price it to $0.99, and push 99 cents to a live listing."""
    for id_like in ("a1b2c3d4", "6e4f4a44", "d1c78c5c", "682b6ab8"):
        assert tb._parse_price_override(id_like) is None, id_like


def test_real_prices_still_parse():
    assert tb._parse_price_override("35") == 34.99          # charm-priced
    assert tb._parse_price_override("$35") == 34.99
    assert tb._parse_price_override("  42.50 ") == 42.50    # exact, not charmed
    assert tb._parse_price_override("make it 42.50") == 42.50
    assert tb._parse_price_override("price 60") == 59.99


def test_non_prices_are_rejected():
    """A free listing is never what someone meant to type, and a trailing
    fragment isn't a price at all."""
    assert tb._parse_price_override("0") is None
    assert tb._parse_price_override("-5") is None
    assert tb._parse_price_override("abc") is None
    assert tb._parse_price_override("") is None
    assert tb._parse_price_override("12.999") is None       # not a money amount


def test_sanity_caps_are_ordered_sensibly():
    """The caps exist to catch a mis-parsed id or a slipped decimal, so they have
    to sit well above anything real and well below a uuid prefix read as a
    number (which runs to eight digits)."""
    assert tb._MAX_SANE_PRICE > 1000        # above any plausible Ross item
    assert tb._MAX_SANE_PRICE < 40_286_146  # below an 8-digit id prefix
    assert tb._MAX_SANE_QTY < 40_286_146


# ---------------------------------------------------------------------------
# _names_an_item and the /setqty, /sold, /setprice regression that shipped with
# it: a plain digit like "2" is a startswith-prefix of *some* uuid4 far more
# often than not (item ids are hex, and hex includes every decimal digit), so
# without a floor on how much of the token has to match, a hand-typed quantity
# or price gets misread as an item id. That broke `/setqty <id> <n>` in BOTH
# argument orders — the id-first order because the trailing quantity looked
# like an id, and the reversed order because the id, correctly spotted, then
# had nowhere real to go.
# ---------------------------------------------------------------------------

# A real Michael Kors bag, id truncated the way /status always shows it — and a
# second item whose 8-char prefix happens to be all-digit, matching the exact
# case _names_an_item's own docstring cites (`/sold 40286146`).
BAG_ID = "c2cb6480-0000-0000-0000-000000000000"
NUMERIC_PREFIX_ID = "40286146-0000-0000-0000-000000000000"
FAKE_USER_ID = 999001

# Decoy items whose ids start with the EXACT digits used below as a quantity or
# a price ("2", "35"). Real inventory is ~90 uuid4 ids, so some item coincidentally
# starting with any given short digit string is closer to the common case than
# the exception — these decoys stand in for that coincidence on purpose. Without
# them, a test using "2" or "35" would pass whether or not the length floor in
# _names_an_item actually works, because nothing in the fake db would collide.
DECOY_2_ID = "2eeeeeee-0000-0000-0000-000000000000"
DECOY_35_ID = "35eeeeee-0000-0000-0000-000000000000"
DECOY_500_ID = "500eeeee-0000-0000-0000-000000000000"


class _FakeDB:
    """A tiny in-memory stand-in for db.py, keyed like the real table.

    telegram_bot.py calls get_item/list_items/update_field/update_status as
    plain module-level names (imported with `from db import ...`), so patching
    those names on the tb module is enough to keep every command under test
    from touching the real data/ross.db — no sqlite file, no network, nothing
    left behind."""

    def __init__(self, items):
        self.items = {i["item_id"]: dict(i) for i in items}

    def get_item(self, item_id):
        return self.items.get(item_id)

    def list_items(self, status=None):
        return [i for i in self.items.values() if status is None or i["status"] == status]

    def update_field(self, item_id, field, value):
        self.items[item_id][field] = value

    def update_status(self, item_id, status):
        self.items[item_id]["status"] = status


class _FakeMessage:
    def __init__(self):
        self.sent: list[str] = []

    async def reply_text(self, text, **kwargs):
        self.sent.append(text)
        return None


class _FakeUpdate:
    def __init__(self, user_id):
        self.effective_user = type("U", (), {"id": user_id})()
        self.message = _FakeMessage()


class _FakeContext:
    def __init__(self, args):
        self.args = args


class _patched_db:
    """Swap tb's db functions for a _FakeDB for the duration of a `with` block,
    and always put the real ones back — these are shared module globals, so a
    test that raised partway through must not leave the next test talking to a
    stub."""

    def __init__(self, db: _FakeDB):
        self.db = db
        self._saved = {}

    def __enter__(self):
        for name in ("get_item", "list_items", "update_field", "update_status"):
            self._saved[name] = getattr(tb, name)
            setattr(tb, name, getattr(self.db, name))
        return self.db

    def __exit__(self, *exc):
        for name, fn in self._saved.items():
            setattr(tb, name, fn)
        tb.last_item.pop(FAKE_USER_ID, None)


def _run_command(coro_fn, args, user_id=FAKE_USER_ID):
    """Invoke a command handler with fake Update/context and return the text of
    every reply it sent (usually just one)."""
    update = _FakeUpdate(user_id)
    context = _FakeContext(args)
    asyncio.run(coro_fn(update, context))
    return update.message.sent


def test_names_an_item_ignores_short_numeric_tokens():
    """The exact false positive behind the bug: a bare quantity like "2" or a
    two-digit price like "35" must never be read as an item id just because
    some item's uuid happens to start with those digits. DECOY_*_ID exists
    specifically so these ids genuinely DO start with "2" / "35" / "500" —
    without a real collision in the fixture, this assertion would pass
    whether or not the fix works."""
    db = _patched_db(_FakeDB([{"item_id": BAG_ID, "status": "published"},
                              {"item_id": NUMERIC_PREFIX_ID, "status": "published"},
                              {"item_id": DECOY_2_ID, "status": "published"},
                              {"item_id": DECOY_35_ID, "status": "published"},
                              {"item_id": DECOY_500_ID, "status": "published"}]))
    with db:
        assert tb._names_an_item("2") is False
        assert tb._names_an_item("35") is False
        assert tb._names_an_item("500") is False


def test_names_an_item_still_catches_a_real_id_prefix():
    """The protection _names_an_item exists for must survive: an 8-char prefix
    that genuinely names an item — digits-only or not — still counts as one."""
    db = _patched_db(_FakeDB([{"item_id": BAG_ID, "status": "published"},
                              {"item_id": NUMERIC_PREFIX_ID, "status": "published"}]))
    with db:
        assert tb._names_an_item("c2cb6480") is True
        assert tb._names_an_item(BAG_ID) is True
        # The scenario in _names_an_item's own docstring: an all-digit 8-char
        # id prefix must still be read as an id, not a $40,286,146 price/qty.
        assert tb._names_an_item("40286146") is True


def test_setqty_id_then_quantity_is_accepted():
    """The first reported failure: `/setqty c2cb6480 2` — id first, quantity
    last, exactly as /setqty's own usage string says — used to bounce off
    "'2' names an item, not a quantity" because "2" prefix-matched some item
    (DECOY_2_ID, standing in for that coincidence). It must now get PAST
    parsing (proven by reaching the "needs an eBay offer" business-logic
    reply, not a parsing complaint) and resolve the actual id, not the decoy."""
    db = _patched_db(_FakeDB([{"item_id": BAG_ID, "status": "captured",
                              "listing": {}, "ebay": {}},
                             {"item_id": DECOY_2_ID, "status": "captured",
                              "listing": {}, "ebay": {}}]))
    with db:
        sent = _run_command(tb.setqty_command, ["c2cb6480", "2"])
    assert len(sent) == 1
    assert "names an item" not in sent[0]
    assert "isn't on eBay yet" in sent[0]
    assert "c2cb6480" in sent[0]


def test_setqty_quantity_then_id_is_rejected_without_self_contradiction():
    """The second reported failure: `/setqty 2 c2cb6480` — the unsupported
    reversed order. This one SHOULD still be rejected (id-first is the only
    documented form), but the message must not echo the id into the quantity
    slot: it names c2cb6480 as the item, and puts c2cb6480 — not "2" — in the
    id position of the usage line it suggests."""
    db = _patched_db(_FakeDB([{"item_id": BAG_ID, "status": "captured",
                              "listing": {}, "ebay": {}}]))
    with db:
        sent = _run_command(tb.setqty_command, ["2", "c2cb6480"])
    assert len(sent) == 1
    assert "'c2cb6480' names an item, not a quantity" in sent[0]
    assert "Usage: /setqty c2cb6480 <n>" in sent[0]
    # The bug put the mis-detected token where the id belongs in the usage
    # hint; here the token really is the id, so this is what "correct" looks
    # like — but pin it so a future edit can't slide back to echoing "2".
    assert "Usage: /setqty 2 " not in sent[0]


def test_setqty_bare_quantity_falls_back_to_last_item():
    """Sibling-command convention (see /setprice, /sold): a single trailing
    number with no id targets whatever was last touched. Must keep working
    once _names_an_item stops misfiring on the number itself — DECOY_2_ID is
    in the fixture so "2" really does collide with a real item's prefix."""
    db = _patched_db(_FakeDB([{"item_id": BAG_ID, "status": "captured",
                              "listing": {}, "ebay": {}},
                             {"item_id": DECOY_2_ID, "status": "captured",
                              "listing": {}, "ebay": {}}]))
    with db:
        tb.last_item[FAKE_USER_ID] = BAG_ID
        sent = _run_command(tb.setqty_command, ["2"])
    assert len(sent) == 1
    assert "names an item" not in sent[0]
    assert "isn't on eBay yet" in sent[0]


def test_sold_price_is_not_silently_dropped():
    """/sold shares _names_an_item but reacts to it differently than /setqty:
    instead of an explicit rejection, a false positive made it treat the WHOLE
    argument list as the id and silently discard the trailing price — the item
    got marked sold at its listed price instead of the one just typed, with no
    error at all. DECOY_35_ID makes "35" a genuine collision. Pin that the
    typed price actually lands."""
    db = _patched_db(_FakeDB([{"item_id": BAG_ID, "status": "published",
                              "listing": {"price": 59.99}, "ebay": {}, "receipt": {}},
                             {"item_id": DECOY_35_ID, "status": "published",
                              "listing": {"price": 59.99}, "ebay": {}, "receipt": {}}]))
    with db:
        sent = _run_command(tb.sold_command, ["c2cb6480", "35"])
        stored = db.db.get_item(BAG_ID)
    assert len(sent) == 1
    assert stored["ebay"]["sale_price"] == 35.0
    assert "for $35.0" in sent[0]
    assert "for $59.99" not in sent[0]  # the listing price, i.e. the dropped-price symptom


def test_setprice_id_then_price_is_accepted():
    """Same shared defect, same id-first convention as /setqty: `/setprice
    c2cb6480 35` must not bounce off "'35' names an item, not a price" —
    DECOY_35_ID makes that a genuine collision, not a vacuous check."""
    db = _patched_db(_FakeDB([{"item_id": BAG_ID, "status": "captured",
                              "listing": {}, "ebay": {}, "receipt": {}, "pricing": {}},
                             {"item_id": DECOY_35_ID, "status": "captured",
                              "listing": {}, "ebay": {}, "receipt": {}, "pricing": {}}]))
    with db:
        sent = _run_command(tb.setprice_command, ["c2cb6480", "35"])
        stored = db.db.get_item(BAG_ID)
    assert len(sent) == 1
    assert "names an item" not in sent[0]
    assert "Price set to $34.99" in sent[0]   # 35 charm-priced
    assert stored["pricing"]["suggested_price"] == 34.99


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"  FAIL  {t.__name__}\n          {e}")
        except Exception as e:
            failed += 1
            print(f" ERROR  {t.__name__}\n          {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
