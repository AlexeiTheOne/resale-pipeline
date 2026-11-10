"""/revise with several listings: one combined preview, one Apply, each
listing sent to eBay first and then saved in the DB, independently.

Same fakes as test_revise.py, for two items (two SKUs, two offers)."""
import copy

import config
import db
from bot import revise_cmds, state
from ebay import inventory, taxonomy
from tests import tg_fakes

OWNER = tg_fakes.DEFAULT_USER_ID
A, B = "c646d6f0-1111-4000-8000-000000000001", "5fdc486f-2222-4000-8000-000000000002"
DESC = "A plaid sham. It is soft. It is warm. It is new.\n\nSHIPPING:\nShips fast.\n\nRETURNS:\n30 days."


class World:
    def __init__(self, monkeypatch):
        self.events, self.fail = [], set()
        self.inv, self.offer, self.rows = {}, {}, {}
        for iid, title, aspects in (
                (A, "Lauren Ralph Lauren Ulster Plaid Euro Pillow Sham Grey White Cotton Flannel",
                 {"Brand": ["Lauren Ralph Lauren"], "Color": ["Grey, White"]}),
                (B, "Ed Hardy Black Tattoo Studded Hobo Bag One Size Shoulder Purse NWT",
                 {"Brand": ["Ed Hardy"], "Color": ["Black"], "Exterior Color": ["Black"],
                  "Style": ["Hobo Bag"]})):
            self.inv[iid] = {"product": {"title": title, "imageUrls": ["u1"], "aspects": aspects},
                             "condition": "NEW_WITH_TAGS"}
            self.offer["O" + iid[:4]] = {"listingDescription": inventory.description_html(DESC),
                                         "pricingSummary": {"price": {"value": "29.99", "currency": "USD"}},
                                         "listing": {"listingId": "L" + iid[:4]}}
            self.rows[iid] = {"item_id": iid, "status": "published",
                              "listing": {"title": title, "description": DESC, "category_id": "169291",
                                          "item_specifics": {k: v[0] for k, v in aspects.items()}},
                              "ebay": {"offer_id": "O" + iid[:4], "sku": iid, "listing_id": "L" + iid[:4]}}
        monkeypatch.setattr(inventory, "_request", self.request)
        monkeypatch.setattr(inventory, "_stored_image_urls", lambda sku: ["u1"])
        monkeypatch.setattr(taxonomy, "category_aspects", lambda cid: [])
        monkeypatch.setattr(db, "get_item", lambda iid: copy.deepcopy(self.rows.get(iid)))
        monkeypatch.setattr(db, "list_items", lambda: [copy.deepcopy(r) for r in self.rows.values()])
        monkeypatch.setattr(db, "update_field", self.update_field)
        monkeypatch.setattr(config, "OWNER_USER_IDS", {OWNER})
        state.revise.clear()

    def request(self, method, path, **kwargs):
        key = path.rsplit("/", 1)[-1]
        store = self.inv if "inventory_item" in path else self.offer

        class R:
            status_code, text = 200, ""

            def __init__(self, payload):
                self._p = payload

            def json(self):
                return self._p
        if method == "GET":
            return R(copy.deepcopy(store[key]))
        self.events.append((key[:8], "inventory" if store is self.inv else "offer"))
        if (key[:8], method) in self.fail or key[:8] in self.fail:
            raise RuntimeError("eBay refused")
        store[key].update(copy.deepcopy(kwargs.get("json")))
        return R({})

    def update_field(self, iid, field, value):
        self.events.append((iid[:8], "db"))
        self.rows[iid][field] = copy.deepcopy(value)


MSG = (f"/revise {A[:8]}\n"
       "title: Lauren Ralph Lauren Ulster Grey Plaid Euro Pillow Sham White Cotton Flannel\n"
       "---\n"
       f"{B[:8]}\n"
       "title: Ed Hardy Tiger Eagle Skull Tattoo Studded Hobo Bag Black Purse One Size NWT\n"
       "Exterior Color: Multicolor\n")


def _preview(text=MSG):
    update, ctx = tg_fakes.make_text_update(text, user_id=OWNER)
    tg_fakes.run(revise_cmds.revise_command, update, ctx)
    return ctx


def _apply(ctx):
    data = next(d for _, d in tg_fakes.last_markup_buttons(ctx) if d.startswith("rev:apply:"))
    update, ctx2 = tg_fakes.make_callback_update(data, user_id=OWNER)
    tg_fakes.run(revise_cmds.revise_callback, update, ctx2)
    return "\n".join(tg_fakes.sent_texts(ctx2))


def test_split_blocks_reads_each_listing():
    blocks = revise_cmds.split_blocks(MSG)
    assert [b[0] for b in blocks] == [[A[:8]], [B[:8]]]
    assert blocks[1][1]["set"] == {"Exterior Color": "Multicolor"}
    assert revise_cmds.split_blocks(f"/revise {A[:8]}\ntitle: X") == [([A[:8]], {
        "title": "X", "set": {}, "remove": [], "description": None})]


def test_one_combined_preview_and_nothing_written(monkeypatch):
    w = World(monkeypatch)
    ctx = _preview()
    text = "\n".join(tg_fakes.sent_texts(ctx))
    assert w.events == []
    assert "Revise 2 listings" in text and "1 of 2" in text and "2 of 2" in text
    assert "Exterior Color: Black → Multicolor" in text
    assert [label for label, _ in tg_fakes.last_markup_buttons(ctx)] == ["✅ Apply all 2 to eBay", "✖ Cancel"]


def test_apply_all_goes_to_ebay_then_db_for_each(monkeypatch):
    w = World(monkeypatch)
    reply = _apply(_preview())
    assert w.events == [(A[:8], "inventory"), (A[:8], "db"), (B[:8], "inventory"), (B[:8], "db")]
    assert w.rows[A]["listing"]["title"].startswith("Lauren Ralph Lauren Ulster Grey")
    assert w.rows[B]["listing"]["item_specifics"]["Exterior Color"] == "Multicolor"
    assert w.inv[B]["product"]["aspects"]["Exterior Color"] == ["Multicolor"]
    assert "Done: 2 of 2 revised." in reply


def test_one_failure_leaves_the_other_listing_revised(monkeypatch):
    w = World(monkeypatch)
    ctx = _preview()
    w.fail.add(A[:8])
    reply = _apply(ctx)
    assert (A[:8], "db") not in w.events and (B[:8], "db") in w.events
    assert w.rows[A]["listing"]["title"].startswith("Lauren Ralph Lauren Ulster Plaid")
    assert "Done: 1 of 2 revised." in reply


def test_a_bad_block_stops_the_whole_preview(monkeypatch):
    w = World(monkeypatch)
    ctx = _preview(MSG + f"---\n{A[:8]}\ntitle: again\n")
    assert any("given twice" in t for t in tg_fakes.sent_texts(ctx))
    ctx = _preview(f"/revise {A[:8]}\ntitle: {'x' * 81}\n---\n{B[:8]}\ntitle: fine")
    assert any("eBay allows 80" in t for t in tg_fakes.sent_texts(ctx))
    assert w.events == [] and not state.revise


def test_too_many_listings(monkeypatch):
    World(monkeypatch)
    monkeypatch.setattr(config, "REVISE_MAX_LISTINGS", 1)
    ctx = _preview()
    assert any("one /revise takes up to 1" in t for t in tg_fakes.sent_texts(ctx))
