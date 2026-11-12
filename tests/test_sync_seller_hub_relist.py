"""Items relisted in Seller Hub (155333d1, ca9ec984, ac7b7ee7).

Their offer still points at the ended original listing. /sync used to copy that
dead offer's "ended / qty 0" in, then the live-listing pass put the real quantity
back, so the same items showed as "Updated" on every run. And the bot's price and
quantity pushes went to the dead offer, never reaching the live listing.

No eBay or Telegram calls: eBay reads and the DB are faked."""
import asyncio

import db
from bot import listing_cmds
from ebay import inventory, listings, orders
from pipeline import sync
from tests import tg_fakes

ITEM_ID = "155333d1-c999-40b8-939d-e360670f7f26"
OLD_LID, LIVE_LID = "117302852938", "117347886751"


def _item(**ebay_extra):
    return {"item_id": ITEM_ID, "status": "published",
            "listing": {"title": "Lauren Ralph Lauren Pillowcases", "price": 24.99},
            "pricing": {"suggested_price": 24.99},
            "ebay": {"offer_id": "o1", "sku": ITEM_ID, "listing_id": LIVE_LID,
                     "quantity": 2, **ebay_extra}}


def _dead_offer(price="30.00"):
    """The offer as eBay returns it after a Seller Hub relist: the ended original."""
    return {"pricingSummary": {"price": {"value": price}}, "availableQuantity": 0,
            "listing": {"listingId": OLD_LID, "listingStatus": "ENDED", "soldQuantity": 1}}


class _DB:
    """An in-memory items table behind the db functions sync and the commands use."""
    def __init__(self, monkeypatch, *items):
        self.rows = {i["item_id"]: i for i in items}
        self.writes = []
        monkeypatch.setattr(db, "list_items", lambda: [self._copy(i) for i in self.rows])
        monkeypatch.setattr(db, "get_item", lambda iid: self._copy(iid) if iid in self.rows else None)
        monkeypatch.setattr(db, "update_field", self.update_field)
        monkeypatch.setattr(db, "update_status", lambda iid, s: self.rows[iid].update(status=s))

    def _copy(self, iid):
        import copy
        return copy.deepcopy(self.rows[iid])

    def update_field(self, iid, field, value):
        self.writes.append((iid, field))
        self.rows[iid][field] = value


def _sync(monkeypatch, store, offer, live_price=24.99, live_qty=2):
    monkeypatch.setattr(inventory, "get_offer", lambda oid: offer)
    monkeypatch.setattr(orders, "sales_by_item", lambda days: {})
    monkeypatch.setattr(listings, "get_active_listings", lambda: [
        {"sku": ITEM_ID, "listing_id": LIVE_LID, "price": live_price, "quantity": live_qty,
         "sold_quantity": 1, "view_item_url": "u"}])
    return asyncio.run(sync.run_sync(30))


# --- sync_one ---------------------------------------------------------------

def test_sync_one_ignores_a_dead_offers_qty_price_and_ended(monkeypatch):
    store = _DB(monkeypatch, _item())
    monkeypatch.setattr(inventory, "get_offer", lambda oid: _dead_offer())
    result = sync.sync_one(store._copy(ITEM_ID), {})
    assert result["changes"] == []
    assert store.writes == []
    assert store.rows[ITEM_ID]["ebay"]["quantity"] == 2
    assert store.rows[ITEM_ID]["listing"]["price"] == 24.99
    assert result["offer_listing_id"] == OLD_LID


def test_sync_one_still_reads_a_live_offer(monkeypatch):
    """Not a relist: offer and stored listing id agree, so eBay is still the truth."""
    store = _DB(monkeypatch, _item())
    offer = {"pricingSummary": {"price": {"value": "22.99"}}, "availableQuantity": 1,
             "listing": {"listingId": LIVE_LID, "listingStatus": "ACTIVE"}}
    monkeypatch.setattr(inventory, "get_offer", lambda oid: offer)
    result = sync.sync_one(store._copy(ITEM_ID), {})
    assert "qty 2→1" in result["changes"]
    assert any("price" in c for c in result["changes"])
    assert store.rows[ITEM_ID]["ebay"]["quantity"] == 1


# --- run_sync ---------------------------------------------------------------

def test_a_relisted_item_is_not_updated_on_every_sync(monkeypatch):
    store = _DB(monkeypatch, _item())
    for _ in range(3):
        lines, _brief = _sync(monkeypatch, store, _dead_offer())
        assert not any(line.startswith("📝 Updated") for line in lines), lines
        assert store.rows[ITEM_ID]["ebay"]["quantity"] == 2


def test_a_relisted_item_is_flagged_and_takes_the_live_price(monkeypatch):
    store = _DB(monkeypatch, _item())
    lines, brief = _sync(monkeypatch, store, _dead_offer(), live_price=21.99)
    assert store.rows[ITEM_ID]["ebay"]["relisted_in_seller_hub"] is True
    assert store.rows[ITEM_ID]["listing"]["price"] == 21.99   # live price, not the dead $30
    flag = [line for line in lines if "Relisted in Seller Hub" in line]
    assert flag and "price changes from the bot won't reach eBay" in flag[0]
    assert any(ITEM_ID[:8] in line and LIVE_LID in line for line in lines)
    assert any("relisted in Seller Hub" in b for b in brief)

    # The full report keeps listing it; the brief only tells you once.
    lines, brief = _sync(monkeypatch, store, _dead_offer(), live_price=21.99)
    assert any("Relisted in Seller Hub (1)" in line for line in lines)
    assert not any("relisted in Seller Hub" in b for b in brief)


def test_a_seller_hub_relist_is_relinked_and_flagged(monkeypatch):
    """First sync after the relist: stored id is still the old one."""
    store = _DB(monkeypatch, _item(listing_id=OLD_LID))
    lines, _ = _sync(monkeypatch, store, _dead_offer())
    ebay = store.rows[ITEM_ID]["ebay"]
    assert ebay["listing_id"] == LIVE_LID
    assert ebay["relisted_in_seller_hub"] is True
    assert ebay["quantity"] == 2
    # From the second sync on, nothing to report.
    lines, _ = _sync(monkeypatch, store, _dead_offer())
    assert not any(line.startswith("📝 Updated") for line in lines), lines


def test_the_flag_clears_when_the_offer_is_live_again(monkeypatch):
    store = _DB(monkeypatch, _item(relisted_in_seller_hub=True))
    offer = {"pricingSummary": {"price": {"value": "24.99"}}, "availableQuantity": 2,
             "listing": {"listingId": LIVE_LID, "listingStatus": "ACTIVE"}}
    lines, _ = _sync(monkeypatch, store, offer)
    assert "relisted_in_seller_hub" not in store.rows[ITEM_ID]["ebay"]
    assert not any("Relisted in Seller Hub" in line for line in lines)


# --- price and quantity commands ---------------------------------------------

def _no_push(monkeypatch):
    pushed = []
    monkeypatch.setattr(inventory, "update_offer_price", lambda *a: pushed.append(a))
    monkeypatch.setattr(inventory, "update_offer_quantity", lambda *a: pushed.append(a))
    return pushed


def test_setprice_on_a_relisted_item_says_it_did_not_reach_ebay(monkeypatch):
    store = _DB(monkeypatch, _item(relisted_in_seller_hub=True))
    pushed = _no_push(monkeypatch)
    update, ctx = tg_fakes.make_text_update(f"/setprice {ITEM_ID[:8]} 21.99")
    tg_fakes.run(listing_cmds.setprice_command, update, ctx)
    sent = tg_fakes.sent_texts(ctx)
    assert pushed == []
    assert any("NOT on eBay" in t and "Seller Hub" in t for t in sent), sent
    assert store.rows[ITEM_ID]["listing"]["price"] == 21.99   # saved locally


def test_reprice_apply_on_a_relisted_item_says_it_did_not_reach_ebay(monkeypatch):
    _DB(monkeypatch, _item(relisted_in_seller_hub=True))
    pushed = _no_push(monkeypatch)
    update, ctx = tg_fakes.make_callback_update(f"reprice:apply:21.99:{ITEM_ID}")
    tg_fakes.run(listing_cmds.reprice_callback, update, ctx)
    assert pushed == []
    assert any("NOT on eBay" in t for t in tg_fakes.sent_texts(ctx))


def test_setqty_on_a_relisted_item_changes_nothing(monkeypatch):
    store = _DB(monkeypatch, _item(relisted_in_seller_hub=True))
    pushed = _no_push(monkeypatch)
    update, ctx = tg_fakes.make_text_update(f"/setqty {ITEM_ID[:8]} 3")
    tg_fakes.run(listing_cmds.setqty_command, update, ctx)
    assert pushed == []
    assert store.rows[ITEM_ID]["ebay"]["quantity"] == 2
    assert any("NOT on eBay" in t for t in tg_fakes.sent_texts(ctx))


# --- eBay first, then the DB (bff24df0) ----------------------------------------

def _live(monkeypatch, fail=False):
    """A normal live item; update_offer_price fakes eBay's answer."""
    store = _DB(monkeypatch, _item())
    calls = []

    def push(offer_id, price, item=None):
        calls.append((offer_id, price, item and item["item_id"]))
        if fail:
            raise RuntimeError("eBay API PUT failed [400]: errorId 25016")
        return {"listingPolicies": {"bestOfferTerms": inventory.best_offer_terms(item, price)}}

    monkeypatch.setattr(inventory, "update_offer_price", push)
    return store, calls


def test_setprice_sends_the_item_so_best_offer_moves_with_the_price(monkeypatch):
    store, calls = _live(monkeypatch)
    update, ctx = tg_fakes.make_text_update(f"/setprice {ITEM_ID[:8]} 21.99 force")
    tg_fakes.run(listing_cmds.setprice_command, update, ctx)
    assert calls == [("o1", 21.99, ITEM_ID)]
    assert store.rows[ITEM_ID]["listing"]["price"] == 21.99
    sent = tg_fakes.sent_texts(ctx)
    assert any("Best Offer: auto-accept" in t and "auto-decline" in t for t in sent), sent


def test_setprice_keeps_the_db_price_when_ebay_refuses(monkeypatch):
    store, calls = _live(monkeypatch, fail=True)
    update, ctx = tg_fakes.make_text_update(f"/setprice {ITEM_ID[:8]} 21.99 force")
    tg_fakes.run(listing_cmds.setprice_command, update, ctx)
    assert calls and store.writes == []
    assert store.rows[ITEM_ID]["listing"]["price"] == 24.99
    assert store.rows[ITEM_ID]["pricing"] == {"suggested_price": 24.99}
    assert any("Nothing changed" in t for t in tg_fakes.sent_texts(ctx))


def test_reprice_apply_keeps_the_db_price_when_ebay_refuses(monkeypatch):
    store, calls = _live(monkeypatch, fail=True)
    update, ctx = tg_fakes.make_callback_update(f"reprice:apply:21.99:{ITEM_ID}")
    tg_fakes.run(listing_cmds.reprice_callback, update, ctx)
    assert calls == [("o1", 21.99, ITEM_ID)] and store.writes == []
    assert store.rows[ITEM_ID]["listing"]["price"] == 24.99


def test_reprice_apply_saves_after_ebay_takes_it(monkeypatch):
    store, calls = _live(monkeypatch)
    update, ctx = tg_fakes.make_callback_update(f"reprice:apply:21.99:{ITEM_ID}")
    tg_fakes.run(listing_cmds.reprice_callback, update, ctx)
    assert calls == [("o1", 21.99, ITEM_ID)]
    assert store.rows[ITEM_ID]["listing"]["price"] == 21.99
    assert store.rows[ITEM_ID]["pricing"]["price_basis"] == "reprice"
