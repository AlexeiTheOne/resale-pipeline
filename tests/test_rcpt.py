"""/rcpt <query> (bot/receipt_cmds.py): read-only receipt line search."""
from datetime import date

import pytest

import config
import db
from bot import receipt_cmds as rc
from pipeline import owned
from tests import tg_fakes

TODAY = date(2026, 11, 8)


# --- parsing ----------------------------------------------------------------------------------

@pytest.mark.parametrize("text, kind, value", [
    ("#9001", "receipt_id", 9001),
    ("# 23", "receipt_id", 23),
    ("9901-03-1234-5678-3", "receipt_number", "9901-03-1234-5678-3"),
    ("990103123456783", "receipt_number", "9901-03-1234-5678-3"),
    ("9901-03-1234", "receipt_number_part", "9901031234"),
    ("5678-3", "receipt_number_part", "56783"),
    ("56785", "sku_suffix", "56785"),
    ("…456785", "sku_suffix", "456785"),
    ("SKU 400123456785", "sku", "400123456785"),
    ("400123456785", "sku", "400123456785"),
    ("400123456785001999", "sku", "400123456785"),       # the 18-digit tag barcode
])
def test_parse_codes(text, kind, value):
    assert rc.parse_query(text) == (kind, value)


@pytest.mark.parametrize("text", ["", "#", "12", "4001234567850", "x", "a b", "%", "12-3",
                                  "500123456785001999"])
def test_parse_refuses(text):
    kind, msg = rc.parse_query(text)
    assert kind == "error" and msg


def test_words_become_terms():
    kind, (terms, dropped) = rc.parse_query("Ed Hardy men's duffel")
    assert kind == "words" and dropped == 0
    assert terms[0] == {"text": "ed hardy", "codes": ["EH"], "gender": None}     # a brand finds its code
    assert terms[1]["gender"] == "men" and terms[2]["text"] == "duffel"
    _, (terms, dropped) = rc.parse_query("one two three four five six seven")
    assert len(terms) == 5 and dropped == 2


# --- the reply --------------------------------------------------------------------------------

def _line(**kw):
    base = {"line_id": 1, "receipt_id": 9001, "line_index": 1, "sku": "400123456785",
            "description": "SAMPLE HOODIE NAVY", "desc_key": "SAMPLE HOODIE NAVY", "price": 18.49,
            "receipt_number": "9901-03-1234-5678-3", "receipt_date": "2026-10-19", "store_number": "9901",
            "store_city": None, "subtotal": None, "total": None, "tax": None, "same_code_prices": 1,
            "line_count": 2, "lines_sum": 28.48, "fixed": False, "d_source": None}
    base.update(kw)
    return base


ITEM = ({"brand": "Acme", "product_name": "Sample Hoodie", "item_type": "Hoodie", "color": "Navy"},
        {"source": "item", "id": "f00dcafe-aaaa", "status": "published", "name": "Sample Hoodie",
         "price": 49.99, "listed_days": 3, "paid": 18.49, "url": "https://www.ebay.com/itm/110000000001",
         "box": None, "sold_at": None, "profit": None, "match": owned.ROSS})
STORES = [("9901", "Dress For Less Springfield", 4)]


def test_a_line_with_our_item_and_a_misread():
    res = {"total": 1, "lines": [_line(same_code_prices=2)], "receipt": None, "store_rows": STORES}
    text = "\n".join(rc.format_reply("sku_suffix", "56785", res, {"400123456785": {"items": [ITEM], "checks": []}},
                                     TODAY))
    assert "1 receipt line for SKU …56785" in text
    assert "Oct 19 (20 days ago) · Springfield (store 9901) · $18.49" in text      # city from the store's receipts
    assert '"SAMPLE HOODIE NAVY" · SKU 400123456785 · receipt #9001' in text
    assert "(our item)" in text and "Acme" in text
    assert "⚠️ misread? this receipt lists this code at 2 prices" in text
    assert "📦 Listed $49.99, 3 days · paid $18.49 · https://www.ebay.com/itm/110000000001" in text
    assert "Ross code" not in text


def test_lines_without_an_item_and_more_than_shown():
    lines = [_line(sku="400100000001", description="ZXQ WIDGET", d_source="ai", d_category="toys",
                   d_item_type="game", d_confidence=0.4),
             _line(line_id=2, sku="400100000002", description="#ASST RUN/COURT", receipt_date=None),
             _line(line_id=3, sku="400100000003", description="QQQ ZZZ")]
    two = (ITEM[0], {**ITEM[1], "id": "b"})
    hits = {"400100000002": {"items": [ITEM, two], "checks": []},
            "400100000003": {"items": [], "checks": [({}, {"source": "check", "id": 12, "status": "checked",
                                                           "verdict": "BUY", "date": "2026-10-01", "price": 30.0,
                                                           "paid": 9.99, "name": "x", "match": owned.ROSS})]}}
    text = "\n".join(rc.format_reply("words", ([{"text": "widget", "codes": [], "gender": None}], 0),
                                     {"total": 14, "lines": lines, "receipt": None, "store_rows": []}, hits, TODAY))
    assert "toys: game?" in text                                # saved AI decode, unsure
    assert "no date" in text and "📦 2 items share this code" in text
    assert "🔎 checked before buying: Checked #12" in text
    assert "no item linked" in text
    assert "…and 11 more. Add a word or more digits." in text


def test_live_rules_when_nothing_is_saved():
    line = _line(description="#ASST RUN/COURT")
    assert rc.what_it_is(line, []).endswith("(rules, not saved)")
    assert rc.what_it_is(_line(description="QQQ ZZZ"), []) == "not decoded yet"


def test_a_whole_receipt():
    head = {"receipt_id": 9001, "receipt_number": "9901-03-1234-5678-3", "receipt_date": "2026-10-19",
            "store_number": "9901", "store_city": "Dress For Less Springfield", "total": None, "subtotal_gap": -17.5,
            "has_returns": True}
    text = "\n".join(rc.format_reply("receipt_id", 9001, {"total": 1, "lines": [_line()], "receipt": head,
                                                          "store_rows": []}, {}, TODAY))
    assert text.startswith("🧾 Receipt #9001 · 9901-03-1234-5678-3 · Oct 19 (20 days ago) · Springfield (store 9901)")
    assert "no total" in text and "differ from the subtotal by $17.50" in text and "has returns" in text
    assert '1. $18.49 · "SAMPLE HOODIE NAVY"' in text
    empty = rc.format_reply("receipt_id", 9001, {"total": 0, "lines": [], "receipt": head, "store_rows": []}, {},
                            TODAY)
    assert "No lines were read from this receipt." in empty
    assert rc.format_reply("receipt_id", 9, {"total": 0, "lines": [], "receipt": None}, {}, TODAY) == \
        ["🧾 No receipt #9."]


# --- the handler --------------------------------------------------------------------------------

def _run(text):
    update, ctx = tg_fakes.make_text_update(text)
    tg_fakes.run(rc.rcpt_command, update, ctx)
    return "\n".join(tg_fakes.sent_texts(ctx)), ctx


def test_handler_usage_and_sqlite(monkeypatch):
    assert "Usage: /rcpt" in _run("/rcpt")[0]
    monkeypatch.setattr(config, "DB_BACKEND", "sqlite")
    assert _run("/rcpt 56785")[0] == "Receipts are on SQL Server only."


def test_handler_reads_and_replies_without_previews(monkeypatch):
    monkeypatch.setattr(config, "DB_BACKEND", "sqlserver")
    seen = {}

    def search(kind, value, limit):
        seen.update(kind=kind, value=value, limit=limit)
        return {"total": 1, "lines": [_line()], "receipt": None, "store_rows": STORES}
    monkeypatch.setattr(db, "receipt_lines_search", search)
    monkeypatch.setattr(owned, "ross_hits", lambda skus: {"400123456785": {"items": [ITEM], "checks": []}})
    text, ctx = _run("/rcpt ed hardy")
    assert seen["kind"] == "words" and seen["value"][0]["codes"] == ["EH"] and seen["limit"] == config.RCPT_MAX_LINES
    assert "SAMPLE HOODIE NAVY" in text
    assert all(c.kwargs.get("disable_web_page_preview") for c in ctx.transcript.of("reply_text"))


def test_handler_reports_a_failure(monkeypatch):
    monkeypatch.setattr(config, "DB_BACKEND", "sqlserver")
    monkeypatch.setattr(db, "receipt_lines_search", lambda *a: (_ for _ in ()).throw(RuntimeError("boom")))
    from bot import util
    errors = []
    monkeypatch.setattr(util, "_record_error", lambda where, e: errors.append(where))
    assert "⚠️ Couldn't read receipts: RuntimeError: boom" in _run("/rcpt 56785")[0]
    assert errors == ["rcpt"]


# --- the search on SQL Server ------------------------------------------------------------------------

def _receipt(cur, number, date_, lines, city="Dress For Less Springfield"):
    cur.execute("INSERT INTO dbo.Receipts (ReceiptNumber, ReceiptDate, StoreCity) OUTPUT INSERTED.ReceiptID "
                "VALUES (?, ?, ?)", number, date_, city)
    rid = cur.fetchone()[0]
    for i, (sku, desc, price) in enumerate(lines, 1):
        cur.execute("INSERT INTO dbo.ReceiptItems (ReceiptID, LineIndex, SKU, Description, Price) "
                    "VALUES (?, ?, ?, ?, ?)", rid, i, sku, desc, price)
    return rid


@pytest.fixture
def receipts(mssql, monkeypatch):
    monkeypatch.setattr(config, "DB_BACKEND", "sqlserver")
    cur = mssql.cursor()
    old = _receipt(cur, "9901-03-1234-5678-3", "2026-10-19",
                   [("400123456785", "SAMPLE HOODIE NAVY", 18.49), ("400123456785", "SAMPLE HOODIE NAVY", 9.99)])
    new = _receipt(cur, "9904-08-1111-2222-0", "2026-11-01",
                   [("400200000001", "EH BLK SKULL 50%_OFF", 14.99), ("400200000002", "WMNS RUN SHOE", 24.99),
                    ("400200000003", "MENS RUN SHOE", 22.99)])
    cur.execute("INSERT INTO dbo.receipt_line_details (desc_key, source, gender, category, item_type) "
                "VALUES ('WMNS RUN SHOE', 'rules', 'women', 'shoes', 'sneakers'), "
                "('MENS RUN SHOE', 'rules', 'men', 'shoes', 'sneakers')")
    cur.execute("INSERT INTO dbo.items (item_id, status) VALUES ('i1', 'published')")
    cur.execute("INSERT INTO dbo.item_identification (item_id, brand, item_type) VALUES ('i1', 'Ed Hardy', "
                "'Weekender Duffel Bag')")
    cur.execute("INSERT INTO dbo.item_tags (item_id, ross_code, code_generated) VALUES ('i1', '400200000001', 0)")
    mssql.commit()
    return {"old": old, "new": new}


def test_search_by_code_number_and_id(receipts):
    r = db.receipt_lines_search("sku_suffix", "56785")
    assert r["total"] == 2 and [ln["price"] for ln in r["lines"]] == [18.49, 9.99]
    assert all(ln["same_code_prices"] == 2 for ln in r["lines"])
    r = db.receipt_lines_search("receipt_number", "9901-03-1234-5678-3")
    assert r["receipt"]["receipt_id"] == receipts["old"] and len(r["lines"]) == 2
    r = db.receipt_lines_search("receipt_id", receipts["new"])
    assert [ln["line_index"] for ln in r["lines"]] == [1, 2, 3] and r["receipt"]["receipt_number"].startswith("9904")
    assert db.receipt_lines_search("receipt_id", 999999)["receipt"] is None
    r = db.receipt_lines_search("receipt_number_part", "56783")
    assert r["total"] == 2
    assert ("9901", "Dress For Less Springfield", 1) in r["store_rows"]


def test_search_words(receipts):
    def words(*terms):
        return db.receipt_lines_search("words", [{"text": t, "codes": [], "gender": None} for t in terms])
    assert words("run", "shoe")["total"] == 2
    assert [ln["description"] for ln in words("sample")["lines"]] == ["SAMPLE HOODIE NAVY"] * 2
    assert words("duffel")["lines"][0]["sku"] == "400200000001"       # through the item behind the SKU
    assert words("50%")["total"] == 1                                   # % is literal, not a wildcard
    assert words("0%_")["total"] == 1 and words("x_o")["total"] == 0     # so is _
    men = db.receipt_lines_search("words", [{"text": "men", "codes": [], "gender": "men"}])
    assert [ln["description"] for ln in men["lines"]] == ["MENS RUN SHOE"]       # not WMNS
    eh = db.receipt_lines_search("words", [{"text": "ed hardy", "codes": ["EH"], "gender": None}])
    assert [ln["sku"] for ln in eh["lines"]] == ["400200000001"]
    both = db.receipt_lines_search("words", [{"text": "s", "codes": [], "gender": None}])["lines"]
    assert [ln["receipt_date"] for ln in both][:1] == ["2026-11-01"]     # newest receipt first


def test_search_before_step_5_still_works(receipts, monkeypatch):
    import db_mssql
    monkeypatch.setattr(db_mssql, "_has_line_details", lambda cur: False)
    r = db.receipt_lines_search("sku_suffix", "0002")
    assert r["total"] == 1 and r["lines"][0]["d_source"] is None
    assert db.receipt_lines_search("words", [{"text": "men", "codes": [], "gender": "men"}])["total"] == 0
