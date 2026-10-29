"""Inventory mode: piece codes, linking, search, sold alerts, ghosts. No network."""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import db  # noqa: E402

db.DB_PATH = str(Path(tempfile.mkdtemp(prefix="inv_")) / "test.db")
db.create_tables()

from pipeline import inventory as inv  # noqa: E402


def _item(status, brand, upc=None, price=None, listing_id=None, msrp=None, qty=None):
    iid = db.create_item([])
    db.update_field(iid, "identification", {"brand": brand, "upc": upc, "item_type": "bag",
                                            "retail_price": msrp})
    if price:
        db.update_field(iid, "listing", {"title": f"{brand} thing", "price": price})
    e = {}
    if listing_id:
        e["listing_id"] = listing_id
    if qty is not None:
        e["quantity"] = qty
    if e:
        db.update_field(iid, "ebay", e)
    db.update_status(iid, status)
    return iid


def _piece(box, info):
    code = db.add_piece(box, None)
    inv.read_tag = lambda _p: info
    return code


def test_codes_count_up_per_box_and_survive_moves():
    a, b = db.add_piece("Q1"), db.add_piece("Q1")
    assert (a, b) == ("Q1-01", "Q1-02")
    db.update_piece(a, box="R2")
    assert db.add_piece("Q1") == "Q1-03"
    assert db.get_piece("q1-01")["box"] == "R2"


def test_upc_links_to_the_live_listing():
    live = _item("published", "Oakley", upc="012345678905", price=89.99, listing_id="L1")
    code = db.add_piece("Q2")
    db.update_piece(code, photo="x.jpg")
    inv.read_tag = lambda _p: {"brand": "Oakley", "upc": "012345678905", "ross_price": 24.99}
    res = inv.process_piece(code, ask=lambda *a, **k: {"match_id": None, "confidence": "low"})
    assert res["item"]["item_id"] == live and not res["created"] and res["value"] == 89.99
    assert "listed" in inv.describe(res)
    assert db.get_piece(code)["cost"] == 24.99


def test_unmatched_piece_becomes_stock_and_its_twin_links_to_it():
    for _ in range(3):  # brand history: listed at 50% of MSRP
        _item("published", "Zorro", price=50, msrp=100)
    info = {"brand": "Zorro", "item_type": "bag", "upc": "036000291452", "msrp": 80,
            "ross_price": 12.99}
    c1 = db.add_piece("Q3", "x.jpg")
    inv.read_tag = lambda _p: dict(info)
    no_match = lambda *a, **k: {"match_id": None, "confidence": "low"}
    first = inv.process_piece(c1, ask=no_match)
    assert first["created"] and first["item"]["status"] == "stocked"
    assert first["value"] == 40.0 and "MSRP" in first["basis"]
    stock_item = db.get_item(first["item"]["item_id"])
    assert stock_item["stock"]["location"] == "Q3" and stock_item["receipt"]["peeled"] is False
    c2 = db.add_piece("Q3", "y.jpg")
    second = inv.process_piece(c2, ask=no_match)
    assert not second["created"] and second["item"]["item_id"] == first["item"]["item_id"]


def test_search_by_code_box_text_and_upc():
    iid = _item("published", "Searchy", upc="042100005264", price=30)
    code = db.add_piece("Q4")
    db.update_piece(code, item_id=iid, info={"brand": "Searchy", "product_name": "Tote",
                                             "upc": "042100005264"})
    assert [p["code"] for p in inv.search(code.lower())] == [code]
    assert [p["code"] for p in inv.search("Q4")] == [code]
    assert [p["code"] for p in inv.search("searchy tote")] == [code]
    assert [p["code"] for p in inv.search("042100005264")] == [code]
    assert "listed" in inv.piece_line(db.get_piece(code))


def test_sold_alert_names_the_box_and_marks_the_piece_sold():
    iid = _item("published", "Selly", price=40, listing_id="999")
    code = db.add_piece("Q5")
    db.update_piece(code, item_id=iid)
    orders = [{"orderId": "O-1", "lineItems": [{"sku": iid, "legacyItemId": "999", "quantity": 1,
                                                "title": "Selly bag", "total": {"value": "40.00"}}]},
              {"orderId": "O-2", "lineItems": [{"sku": "unknown", "quantity": 1, "title": "Old"}]}]
    lines, handled = inv.order_alerts(orders, seen={"O-2"})
    assert handled == ["O-1"] and len(lines) == 1
    assert f"Pull from {code}" in lines[0] and "$40.00" in lines[0]
    assert db.get_piece(code)["status"] == "sold"
    again, _ = inv.order_alerts(orders, seen={"O-1", "O-2"})
    assert again == []


def test_sale_with_no_scanned_piece_still_alerts():
    iid = _item("published", "Unscanned", price=20, listing_id="777")
    lines, _ = inv.order_alerts([{"orderId": "O-3", "lineItems": [
        {"sku": iid, "quantity": 1, "title": "Unscanned thing"}]}], seen=set())
    assert "No box on file" in lines[0]


def test_ghost_report_compares_ebay_with_home():
    a = _item("published", "Ghosty", price=10, listing_id="G1")
    b = _item("published", "Shorty", price=10, listing_id="G2")
    code = db.add_piece("Q6")
    db.update_piece(code, item_id=b)
    live = {a: 1, b: 3}
    lines = inv.ghost_report(get_live=lambda it: {"status": "Active",
                                                  "available": live.get(it["item_id"], 0)})
    text = "\n".join(lines)
    assert a[:8] in text and "never found" in text
    assert f"/setqty {b[:8]} 1" in text
