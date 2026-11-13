"""/revise: owner-only, preview before any write, eBay first, then the DB only
if every eBay write succeeded, one listing at a time.

The real ebay.inventory functions run against a fake eBay (inventory._request
replaced) that records every write and can be told to refuse one. No network,
no Telegram, no real DB."""
import copy

import config
import db
from bot import revise_cmds, state, util
from ebay import inventory, taxonomy
from pipeline import revise
from tests import tg_fakes

ITEM_ID = "d7432192-95d6-42bb-b111-6656046ae27e"
LIVE_LID = "117491667075"
OWNER = tg_fakes.DEFAULT_USER_ID
IMAGES = [f"https://res.cloudinary.com/x/image/upload/v1/img{n}.jpg" for n in range(4)]
OLD_TITLE = "Ed Hardy Skulls Roses Tattoo Studded Charms Shoulder Bag Black NWT EHH462-42-003"
NEW_TITLE = "Ed Hardy Skulls & Roses Shoulder Bag Purse Black Tattoo Studded Charms NWT"
OLD_DESC = ("Ed Hardy Tattoo Print Satchel Bag in black with all-over multicolor tattoo graphics.\n\n"
            "SPECIFICATIONS:\n- Brand: Ed Hardy\n- Bag Length: 11 inches\n\n"
            "SHIPPING:\nShips within 1 business day via USPS with tracking number.\n\n"
            "RETURNS:\n30-day return policy. Buyer pays return shipping.")
NEW_BODY = ("New with tags: an Ed Hardy women's shoulder bag purse in black faux leather. "
            "It measures about 11 x 9 x 5 inches.")
OLD_ASPECTS = {"Brand": ["Ed Hardy"], "Style": ["Satchel"], "Type": ["Satchel"], "Color": ["Black"],
               "Exterior Color": ["Black"], "Theme": ["Art"], "UPC": ["199347439892"],
               "MPN": ["EHH462-42-003"]}

REVISE_MSG = (f"/revise {ITEM_ID[:8]}\n"
              f"title: {NEW_TITLE}\n"
              "Style: Shoulder Bag\n"
              "Type: Handbag\n"
              "Color: Black / Multicolor\n"
              "Theme: Skull, Tattoo\n"
              "description:\n"
              f"{NEW_BODY}")


class _Resp:
    def __init__(self, payload):
        self._payload = payload
        self.status_code = 200
        self.text = ""

    def json(self):
        return self._payload


class FakeWorld:
    """Fake eBay (one inventory item + its offer) and a fake items table,
    sharing one event log so the order of writes can be checked."""

    def __init__(self, monkeypatch, **ebay_extra):
        self.events = []
        self.fail = set()          # "inventory", "offer", "db"
        self.inv_item = {"product": {"title": OLD_TITLE, "imageUrls": list(IMAGES),
                                     "aspects": copy.deepcopy(OLD_ASPECTS), "brand": "Ed Hardy",
                                     "mpn": "EHH462-42-003", "upc": ["199347439892"]},
                         "condition": "NEW_WITH_TAGS",
                         "availability": {"shipToLocationAvailability": {"quantity": 2}}}
        self.offer = {"listingDescription": inventory.description_html(OLD_DESC),
                      "pricingSummary": {"price": {"value": "44.99", "currency": "USD"}},
                      "availableQuantity": 2, "categoryId": "169291",
                      "listingPolicies": {"fulfillmentPolicyId": "F1", "returnPolicyId": "R1"},
                      "listing": {"listingId": LIVE_LID, "listingStatus": "ACTIVE"}}
        self.row = {"item_id": ITEM_ID, "status": "published",
                    "listing": {"title": OLD_TITLE, "description": OLD_DESC, "price": 44.99,
                                "category_id": "169291",
                                "item_specifics": {"Brand": "Ed Hardy", "Style": "Satchel",
                                                   "Type": "Satchel", "Color": "Black",
                                                   "Exterior Color": "Black", "Theme": "Art",
                                                   "UPC": "199347439892", "MPN": "EHH462-42-003"}},
                    "ebay": {"offer_id": "O1", "sku": ITEM_ID, "listing_id": LIVE_LID,
                             "quantity": 2, **ebay_extra}}
        monkeypatch.setattr(inventory, "_request", self.request)
        monkeypatch.setattr(inventory, "_stored_image_urls", lambda sku: list(IMAGES))
        monkeypatch.setattr(taxonomy, "category_aspects", lambda cid: [
            {"localizedAspectName": "Theme", "aspectConstraint": {"itemToAspectCardinality": "MULTI"}},
            {"localizedAspectName": "Type", "aspectConstraint": {"itemToAspectCardinality": "SINGLE"}}])
        monkeypatch.setattr(db, "get_item", lambda iid: copy.deepcopy(self.row) if iid == ITEM_ID else None)
        monkeypatch.setattr(db, "list_items", lambda: [copy.deepcopy(self.row)])
        monkeypatch.setattr(db, "update_field", self.update_field)
        monkeypatch.setattr(config, "OWNER_USER_IDS", {OWNER})
        state.revise.clear()

    def request(self, method, path, **kwargs):
        kind = "inventory" if "inventory_item" in path else "offer"
        if method == "GET":
            return _Resp(copy.deepcopy(self.inv_item if kind == "inventory" else self.offer))
        body = copy.deepcopy(kwargs.get("json"))
        self.events.append((kind, body))
        if kind in self.fail:
            raise RuntimeError(f"eBay API {method} {path} failed [400]: refused")
        if kind == "inventory":
            self.inv_item.update(body)
        else:
            self.offer.update(body)
        return _Resp({})

    def update_field(self, iid, field, value):
        self.events.append(("db", field))
        if "db" in self.fail:
            raise RuntimeError("database is locked")
        self.row[field] = copy.deepcopy(value)

    def writes(self):
        return [kind for kind, _ in self.events]


def _preview(text=REVISE_MSG, user_id=OWNER):
    update, ctx = tg_fakes.make_text_update(text, user_id=user_id)
    tg_fakes.run(revise_cmds.revise_command, update, ctx)
    return ctx


def _tap(ctx, which="apply", user_id=OWNER):
    buttons = dict((label, data) for label, data in tg_fakes.last_markup_buttons(ctx))
    data = next(d for d in buttons.values() if d.startswith(f"rev:{which}:"))
    update, ctx2 = tg_fakes.make_callback_update(data, user_id=user_id)
    tg_fakes.run(revise_cmds.revise_callback, update, ctx2)
    return "\n".join(tg_fakes.sent_texts(ctx2))


# --- parsing --------------------------------------------------------------------

def test_parse_reads_title_specifics_removals_and_a_multiline_description():
    args, change = revise_cmds.parse("/revise abc\ntitle: T\nStyle: Shoulder Bag\nPattern: -\n"
                                     "description:\nLine one.\n\nLine: two.")
    assert args == ["abc"]
    assert change == {"title": "T", "set": {"Style": "Shoulder Bag"}, "remove": ["Pattern"],
                      "description": "Line one.\n\nLine: two."}


def test_parse_refuses_a_line_it_cant_read_and_duplicates():
    for text in ("/revise a\njust words", "/revise a\nStyle: X\nstyle: Y"):
        try:
            revise_cmds.parse(text)
        except revise_cmds.ReviseError:
            continue
        raise AssertionError(f"accepted: {text!r}")


def test_a_new_description_keeps_the_current_shipping_and_returns_section():
    out = revise.with_footer(NEW_BODY, OLD_DESC)
    assert out.startswith(NEW_BODY)
    assert "SHIPPING:\nShips within 1 business day" in out and "RETURNS:" in out
    assert "SPECIFICATIONS" not in out
    own = NEW_BODY + "\n\nSHIPPING:\nFree."
    assert revise.with_footer(own, OLD_DESC) == own


# --- owner only, single listing -------------------------------------------------------

def test_only_the_owner_can_revise(monkeypatch):
    world = FakeWorld(monkeypatch)
    ctx = _preview(user_id=999)
    assert any("owner-only" in t for t in tg_fakes.sent_texts(ctx))
    monkeypatch.setattr(config, "OWNER_USER_IDS", set())
    ctx = _preview()
    assert any("OWNER_USER_IDS" in t for t in tg_fakes.sent_texts(ctx))
    assert world.events == []


def test_one_listing_at_a_time(monkeypatch):
    world = FakeWorld(monkeypatch)
    ctx = _preview(f"/revise {ITEM_ID[:8]} other1\ntitle: X")
    assert any("One listing per block" in t for t in tg_fakes.sent_texts(ctx))
    assert world.events == []


def test_a_seller_hub_relist_is_refused(monkeypatch):
    world = FakeWorld(monkeypatch, relisted_in_seller_hub=True)
    ctx = _preview()
    assert any("relisted in Seller Hub" in t for t in tg_fakes.sent_texts(ctx))
    assert world.events == [] and not state.revise


def test_a_title_over_80_is_refused_before_anything(monkeypatch):
    world = FakeWorld(monkeypatch)
    ctx = _preview(f"/revise {ITEM_ID[:8]}\ntitle: {'x' * 81}")
    assert any("eBay allows 80" in t for t in tg_fakes.sent_texts(ctx))
    assert world.events == []


# --- preview ---------------------------------------------------------------------------

def test_preview_shows_before_and_after_and_writes_nothing(monkeypatch):
    world = FakeWorld(monkeypatch)
    ctx = _preview()
    text = "\n".join(tg_fakes.sent_texts(ctx))
    assert world.events == [], "a preview must not write anything"
    assert f"before: {OLD_TITLE}" in text and f"after:  {NEW_TITLE} (74/80)" in text
    assert "Style: Satchel → Shoulder Bag" in text
    assert "Type: Satchel → Handbag" in text
    assert "Color: Black → Black / Multicolor" in text
    assert "Theme: Art → Skull, Tattoo" in text
    assert "UPC 199347439892" in text            # unchanged, and said so
    assert "DESCRIPTION before:" in text and "DESCRIPTION after:" in text
    assert NEW_BODY in text and "Nothing has been sent yet" in text
    labels = [label for label, _ in tg_fakes.last_markup_buttons(ctx)]
    assert labels == ["✅ Apply to eBay", "✖ Cancel"]


# --- apply -------------------------------------------------------------------------------

def test_apply_updates_ebay_then_the_db(monkeypatch):
    world = FakeWorld(monkeypatch)
    reply = _tap(_preview())
    assert world.writes() == ["inventory", "offer", "db"], "eBay first, then the DB"
    product = world.inv_item["product"]
    assert product["title"] == NEW_TITLE
    assert product["aspects"]["Style"] == ["Shoulder Bag"]
    assert product["aspects"]["Type"] == ["Handbag"]
    assert product["aspects"]["Color"] == ["Black / Multicolor"]
    assert product["aspects"]["Theme"] == ["Skull", "Tattoo"]
    assert product["aspects"]["UPC"] == ["199347439892"] and product["upc"] == ["199347439892"]
    assert product["aspects"]["Exterior Color"] == ["Black"]
    assert product["imageUrls"] == IMAGES
    assert "It measures about 11 x 9 x 5 inches." in world.offer["listingDescription"]
    assert world.offer["pricingSummary"]["price"]["value"] == "44.99"   # price untouched
    listing = world.row["listing"]
    assert listing["title"] == NEW_TITLE
    assert listing["description"].startswith(NEW_BODY) and "RETURNS:" in listing["description"]
    assert listing["item_specifics"]["Type"] == "Handbag"
    assert listing["item_specifics"]["Theme"] == ["Skull", "Tattoo"]
    assert listing["item_specifics"]["UPC"] == "199347439892"
    assert listing["price"] == 44.99
    assert "✅ Revised" in reply and "same listing" in reply


def test_refused_title_or_specifics_leave_the_db_alone(monkeypatch):
    world = FakeWorld(monkeypatch)
    ctx = _preview()
    world.fail.add("inventory")
    monkeypatch.setattr(util, "_recent_errors", type(util._recent_errors)(maxlen=10))
    reply = _tap(ctx)
    assert "db" not in world.writes() and "offer" not in world.writes()
    assert world.row["listing"]["title"] == OLD_TITLE
    assert "refused the title/specifics" in reply and "DB was not changed" in reply
    # pipeline/revise reports through the bot's recorder, so /errors has it.
    assert util._recent_errors[0][1] == f"revise title/specifics ({ITEM_ID[:8]})"


def test_refused_description_puts_title_and_specifics_back(monkeypatch):
    world = FakeWorld(monkeypatch)
    ctx = _preview()
    world.fail.add("offer")
    reply = _tap(ctx)
    assert world.writes() == ["inventory", "offer", "inventory"], "second inventory write = put back"
    assert world.inv_item["product"]["title"] == OLD_TITLE
    assert world.inv_item["product"]["aspects"] == OLD_ASPECTS
    assert world.row["listing"]["title"] == OLD_TITLE
    assert world.row["listing"]["description"] == OLD_DESC
    assert "put back" in reply and "DB was not changed" in reply


def test_a_failed_db_write_is_reported(monkeypatch):
    world = FakeWorld(monkeypatch)
    ctx = _preview()
    world.fail.add("db")
    reply = _tap(ctx)
    assert world.writes() == ["inventory", "offer", "db"]
    assert "eBay was updated, but saving it to the DB failed" in reply


def test_nothing_is_written_if_ebay_changed_since_the_preview(monkeypatch):
    world = FakeWorld(monkeypatch)
    ctx = _preview()
    world.inv_item["product"]["title"] = "Edited in Seller Hub meanwhile"
    reply = _tap(ctx)
    assert world.events == []
    assert "changed on eBay or in the bot since the preview" in reply


def test_a_price_change_from_sync_does_not_block_apply(monkeypatch):
    world = FakeWorld(monkeypatch)
    ctx = _preview()
    world.row["listing"]["price"] = 39.99
    _tap(ctx)
    assert world.writes() == ["inventory", "offer", "db"]
    assert world.row["listing"]["price"] == 39.99


def test_cancel_writes_nothing_and_a_button_works_once(monkeypatch):
    world = FakeWorld(monkeypatch)
    ctx = _preview()
    assert "Cancelled" in _tap(ctx, "cancel")
    assert world.events == [] and not state.revise


def test_another_user_cannot_apply_the_owners_preview(monkeypatch):
    world = FakeWorld(monkeypatch)
    ctx = _preview()
    monkeypatch.setattr(config, "OWNER_USER_IDS", {OWNER, 5151})
    reply = _tap(ctx, user_id=5151)
    assert "isn't yours" in reply and world.events == []
