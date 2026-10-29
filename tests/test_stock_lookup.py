"""/find: photo in, "posted, N available" or "not posted" out. No network."""
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import db  # noqa: E402

db.DB_PATH = str(Path(tempfile.mkdtemp(prefix="find_")) / "test.db")
db.create_tables()

from pipeline import stock_lookup as sl  # noqa: E402


def _item(status, title, upc=None, listing_id=None):
    iid = db.create_item([])
    db.update_field(iid, "identification", {"brand": "Lauren", "upc": upc})
    db.update_field(iid, "listing", {"title": title})
    if listing_id:
        db.update_field(iid, "ebay", {"listing_id": listing_id})
    db.update_status(iid, status)
    return iid


def _xml(status, qty, sold, price="27.99"):
    return lambda _lid: ET.fromstring(
        f"<r><Item><Quantity>{qty}</Quantity><SellingStatus><QuantitySold>{sold}</QuantitySold>"
        f"<CurrentPrice>{price}</CurrentPrice><ListingStatus>{status}</ListingStatus>"
        f"</SellingStatus></Item></r>")


def _no_claude(*a, **k):
    raise AssertionError("Claude should not be called")


def test_upc_on_the_tag_matches_without_claude(monkeypatch=None):
    iid = _item("published", "Estella Pillowcases", upc="886087411348", listing_id="111")
    sl.read_barcodes = lambda _p: ["886087411348"]
    res = sl.lookup("x.jpg", ask=_no_claude, get_item_xml=_xml("Active", 3, 1))
    assert res["item"]["item_id"] == iid and res["live"]["available"] == 2
    assert "✅ Posted, 2 available" in sl.format_answer(res)


def test_shared_upc_reports_the_live_listing():
    ended = _item("ebay_draft", "Twin ended", upc="199347090093", listing_id="E")
    live = _item("published", "Twin live", upc="199347090093", listing_id="L")
    sl.read_barcodes = lambda _p: ["199347090093"]
    xml = lambda lid: _xml("Completed" if lid == "E" else "Active", 2, 0)(lid)
    res = sl.lookup("x.jpg", ask=_no_claude, get_item_xml=xml)
    assert res["item"]["item_id"] == live and res["item"]["item_id"] != ended


def test_no_barcode_asks_claude_and_says_not_posted():
    sl.read_barcodes = lambda _p: []
    res = sl.lookup("x.jpg", ask=lambda *a, **k: {"match_id": None, "confidence": "high",
                                                  "reason": "nothing like it"})
    assert res["item"] is None and "Not posted" in sl.format_answer(res)


def test_claude_match_and_unknown_ids_are_ignored():
    iid = _item("published", "Oakley Holbrook", listing_id="222")
    sl.read_barcodes = lambda _p: []
    res = sl.lookup("x.jpg", ask=lambda *a, **k: {"match_id": iid[:8], "confidence": "medium",
                                                  "reason": "same frame"},
                    get_item_xml=_xml("Active", 1, 0))
    assert res["item"]["item_id"] == iid
    bogus = sl.lookup("x.jpg", ask=lambda *a, **k: {"match_id": "zzzzzzzz", "confidence": "high",
                                                    "reason": "made up"})
    assert bogus["item"] is None


def test_low_confidence_is_a_maybe_not_an_answer():
    iid = _item("published", "Ed Hardy Tote", listing_id="333")
    sl.read_barcodes = lambda _p: []
    res = sl.lookup("x.jpg", ask=lambda *a, **k: {"match_id": iid[:8], "confidence": "low",
                                                  "reason": "similar print"})
    assert res["item"] is None and res["maybe"]["item_id"] == iid
    assert "Not sure" in sl.format_answer(res)


def test_sold_out_listing_says_so():
    _item("sold", "Oakley Frogskins", upc="012345678905", listing_id="444")
    sl.read_barcodes = lambda _p: ["012345678905"]
    res = sl.lookup("x.jpg", ask=_no_claude, get_item_xml=_xml("Completed", 1, 1))
    assert "not live" in sl.format_answer(res)


def test_an_inside_shot_is_not_called_not_posted():
    sl.read_barcodes = lambda _p: []
    res = sl.lookup("x.jpg", ask=lambda *a, **k: {"match_id": None, "confidence": "low",
                                                  "reason": "just lining",
                                                  "photo_shows_enough": False})
    assert "Can't tell" in sl.format_answer(res)


def test_stocked_items_keep_their_location_and_quick_price():
    iid = _item("stocked", "Betsey backpack")
    db.update_field(iid, "stock", {"location": "G3", "quick_price": {"price": 44.99}})
    item = db.get_item(iid)
    assert item["stock"]["location"] == "G3"
    import telegram_bot as tb
    line = tb._stock_line(item)
    assert "$44.99" in line and "G3" in line
