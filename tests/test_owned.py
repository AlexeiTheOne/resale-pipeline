"""pipeline/owned.py: "do I already have this?" at the top of every /check reply."""
from datetime import datetime, timedelta, timezone

import pytest

import config
import db
from pipeline import owned

ROSS = "400111222333"


@pytest.fixture(autouse=True)
def pinned(monkeypatch, tmp_path):
    if config.DB_BACKEND != "sqlserver":      # SQL Server: conftest empties the test database
        monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "owned.db"))
        db.create_tables()
    monkeypatch.setattr(config, "SALES_TAX_RATE", 0.07)
    monkeypatch.setattr(config, "CHECK_OWNED_AI", True)
    monkeypatch.setattr(config, "CHECK_OWNED_SHOW", 3)
    import claude_llm
    monkeypatch.setattr(claude_llm, "ask", lambda *a, **kw: (_ for _ in ()).throw(AssertionError("no AI here")))


def _iso(days_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


def _item(status, ident, *, listing=None, ebay=None, receipt=None):
    item = db.create_item([])
    db.update_field(item, "identification", ident)
    for field, value in (("listing", listing), ("ebay", ebay), ("receipt", receipt)):
        if value:
            db.update_field(item, field, value)
    db.update_status(item, status)
    return item


JUICY = {"brand": "Juicy Couture", "product_name": "Weekender", "model": "JC1234AB", "color": "Black",
         "upc": "796483413276"}


# --- keys ---------------------------------------------------------------------------------

def test_keys_ross_code_and_upc():
    assert owned.ross_code("400111222333001499") == ROSS          # the 18-digit tag barcode
    assert owned.ross_code(ROSS) == ROSS
    assert owned.ross_code("000000000006") is None                # a generated placeholder
    assert owned.upc_key("796483413276") == owned.upc_key("0796483413276")
    assert owned.upc_key(ROSS) is None                            # a Ross code is never a UPC
    k = owned.keys({"upc": ROSS})                                 # stored in the UPC field by mistake
    assert k["upc"] is None and k["ross"] == ROSS


@pytest.mark.parametrize("a,b,same", [("Black", "black multi (leopard)", True), ("Black", "Pink", False),
                                      ("", "Pink", True), ("Cherry Gingham", "cherry", True)])
def test_same_colour(a, b, same):
    assert owned.same_colour(a, b) is same


def test_match_order():
    mine = owned.keys(JUICY, ROSS)
    assert owned.match(mine, owned.keys(JUICY)) == owned.UPC
    assert owned.match(mine, owned.keys({**JUICY, "upc": None})) == owned.STYLE
    assert owned.match(mine, owned.keys({**JUICY, "upc": None, "color": "Pink"})) == owned.STYLE_OTHER_COLOUR
    assert owned.match(mine, owned.keys({"color": "Pink"}, ROSS)) == owned.ROSS
    assert owned.match(mine, owned.keys({"model": "XX9999ZZ"})) is None


# --- lookup ---------------------------------------------------------------------------------

def test_sold_item_shows_sold_date_price_paid_and_profit():
    _item("sold", JUICY, listing={"title": "Juicy Couture Weekender", "price": 59.99},
          ebay={"listing_id": "117000000001", "view_item_url": "https://www.ebay.com/itm/Juicy-/117000000001?x=1",
                "published_at": _iso(40), "sold_at": "2026-09-29T01:39:44.000Z", "sale_price": 54.99,
                "net_proceeds": 40.0, "units_sold": 1},
          receipt={"code": ROSS, "reduced_price": 10.0})
    res = owned.lookup(dict(JUICY))
    [h] = res["hits"]
    assert (h["source"], h["match"], h["status"], h["price"], h["paid"], h["profit"]) == \
        ("item", owned.UPC, "sold", 54.99, 10.0, 29.3)
    assert h["sold_at"] == "2026-09-29" and h["url"] == "https://www.ebay.com/itm/117000000001"
    text = owned.hit_text(h)
    assert text.startswith("Sold Sep 29") and "for $54.99" in text and "paid $10.00" in text \
        and "profit $29.30" in text and text.endswith("UPC")


def test_listed_item_days_and_piece_box():
    item = _item("published", {**JUICY, "upc": None}, listing={"price": 69.99},
                 ebay={"listing_id": "117000000002", "published_at": _iso(136.2)},
                 receipt={"code": ROSS, "reduced_price": 14.99})
    code = db.add_piece("O1W")
    db.update_piece(code, item_id=item, status="home")
    [h] = owned.lookup(dict(JUICY))["hits"]
    assert (h["match"], h["listed_days"], h["box"], h["price"]) == (owned.STYLE, 136, code, 69.99)
    assert owned.hit_text(h).startswith("Listed $69.99, 136 days · box O1W-")


def test_unlinked_piece_at_home():
    code = db.add_piece("O1W")
    db.update_piece(code, info={"brand": "Michael Kors", "style_code": "MKJ3969710", "ross_code": ROSS,
                                "ross_price": 14.99}, value=43.0, cost=14.99)
    [h] = owned.lookup({}, ROSS)["hits"]
    assert (h["source"], h["box"], h["paid"], h["match"]) == ("piece", code, 14.99, owned.ROSS)
    assert owned.hit_text(h) == f"At home, box {code} (~$43.00) · paid $14.99 · Ross code"


def test_earlier_checks_but_not_this_one():
    first = db.add_check(user_id=1, ross_sku=ROSS, ross_price=9.99, identification={"brand": "Vans"},
                         verdict="MAYBE", suggested_price=24.99)
    this = db.add_check(user_id=1, ross_sku=ROSS, ross_price=9.99)
    [h] = owned.lookup({}, ROSS, this)["hits"]
    assert (h["source"], h["id"]) == ("check", first)
    assert owned.hit_text(h).startswith(f"Checked #{first} ") and ": MAYBE, sell $24.99 · Ross $9.99" in \
        owned.hit_text(h)


def test_receipt_lines_not_yet_entered():
    lines = [{"line_id": 7, "sku": ROSS, "description": "JUICY TOTE", "price": 12.99,
              "receipt_date": "2026-10-30", "store_city": "HOLLYWOOD"},
             {"line_id": 3, "sku": ROSS, "description": "JUICY TOTE", "price": 12.99,
              "receipt_date": "2026-10-01", "store_city": "HOLLYWOOD"}]
    asked = []
    res = owned.lookup({}, ROSS, receipts_fn=lambda skus: asked.append(skus) or lines)
    assert asked == [[ROSS]] and [h["id"] for h in res["hits"]] == [7, 3]
    assert owned.hit_text(res["hits"][0]).startswith("Bought Oct 30 (Hollywood) · paid $12.99")
    # one of the two purchases is already an item: only the other line shows
    _item("published", {"brand": "Juicy Couture"}, receipt={"code": ROSS})
    res = owned.lookup({}, ROSS, receipts_fn=lambda skus: lines)
    assert [h["source"] for h in res["hits"]] == ["item", "receipt"] and res["hits"][1]["id"] == 3


def test_ai_fallback_only_without_a_key_match():
    item = _item("published", {"brand": "Juicy Couture", "product_name": "Weekender Duffle"},
                 listing={"title": "Juicy Couture Weekender Duffle Bag"})
    _item("published", {"brand": "Coach", "product_name": "Tabby 26"})
    seen = []

    def ai(ident, cands):
        seen.append([c[2]["id"] for c in cands])
        return [(0, owned.AI_SAME)]

    res = owned.lookup({"brand": "Juicy Couture", "product_name": "Weekender"}, ai_fn=ai)
    assert seen == [[item]] and res["ai"] == {"candidates": 1}       # same brand only
    assert res["hits"][0]["match"] == owned.AI_SAME
    assert "AI: same item: Juicy Couture Weekender" in owned.hit_text(res["hits"][0])
    seen.clear()
    owned.lookup({"brand": "Unknown brand thing"}, ai_fn=ai)
    assert seen == []                                                 # nothing of that brand
    _item("published", JUICY)                                         # a key match: no AI call
    seen.clear()
    assert owned.lookup(dict(JUICY), ai_fn=ai)["hits"][0]["match"] == owned.UPC and seen == []


def test_ai_match_reads_the_answer():
    cands = [({}, {"brand": "Juicy Couture", "product_name": "A"}, {"id": 1}),
             ({}, {"brand": "Juicy Couture", "product_name": "B"}, {"id": 2})]
    asked = []

    def ask(stage, prompt, **kw):
        asked.append((stage, kw["model"], prompt))
        return {"matches": [{"n": 1, "same": "no"}, {"n": 2, "same": "same_model"}, {"n": 9, "same": "same_item"}]}

    assert owned.ai_match({"brand": "Juicy Couture"}, cands, ask=ask) == [(1, owned.AI_MODEL)]
    assert asked[0][0] == "check_owned" and asked[0][1] == config.CLAUDE_CHEAP_MODEL
    assert "1. Juicy Couture | A" in asked[0][2]


def test_lookup_never_raises():
    res = owned.lookup({}, ROSS, things_fn=lambda _id: 1 / 0)
    assert res["hits"] == [] and res["error"].startswith("ZeroDivisionError")
    assert owned.reply_lines(res) == ["⚠️ Couldn't check what we already have"]


# --- reply lines ----------------------------------------------------------------------------

def _hit(**kw):
    base = {"source": "item", "id": "abc", "status": "published", "name": "Juicy <Weekender>", "paid": 9.99,
            "price": 59.99, "listed_days": 12, "sold_at": None, "profit": None, "box": None,
            "url": "https://www.ebay.com/itm/117000000002", "match": owned.UPC}
    return {**base, **kw}


def test_reply_lines_new_and_hits():
    assert owned.reply_lines({"hits": [], "ai": None, "error": None}) == ["✅ New"]
    hits = [_hit(), _hit(status="sold", sold_at="2026-09-29", profit=12.4, url=None),
            _hit(source="check", id=4, status="checked", verdict="NO_DATA", date="2026-11-03", price=None,
                 url=None), _hit(source="piece", id="O1W-01", status="home", box="O1W-01", url=None)]
    text = owned.reply_lines({"hits": hits})
    assert text[0] == "🔁 Already have: 1 listed, 1 at home, 1 sold, 1 checked before"
    assert text[1] == "• Listed $59.99, 12 days · paid $9.99 · UPC · https://www.ebay.com/itm/117000000002"
    assert len(text) == 5 and text[-1] == "• … 1 more in Details"
    html = owned.reply_lines({"hits": [_hit(match=owned.STYLE_OTHER_COLOUR)]}, html=True)
    assert html[0].startswith("🔁 Similar: 1 listed")
    assert "Juicy &lt;Weekender&gt;" in html[1] and html[1].endswith(
        '<a href="https://www.ebay.com/itm/117000000002">eBay</a>')


def test_receipt_lines_are_one_reply_line():
    lines = [_hit(source="receipt", id=n, status="bought", date=d, city=c, paid=p, url=None, match=owned.ROSS)
             for n, d, c, p in ((3, "2026-09-19", "HIALEAH", 7.99), (2, "2026-09-14", "CORAL GABLES", 4.99),
                                (1, "2026-08-11", "MIAMI", 15.99))]
    text = owned.reply_lines({"hits": [_hit()] + lines})
    assert text[0] == "🔁 Already have: 1 listed, 3 on receipts" and len(text) == 3
    assert text[2].startswith("• Bought 3× (Aug 11") and "paid $4.99–$15.99 · last Hialeah · Ross code" in text[2]
    assert len(owned.details_lines({"hits": [_hit()] + lines})) == 5


def test_details_lists_every_hit():
    lines = owned.details_lines({"hits": [_hit()] * 5, "ai": {"candidates": 4}, "error": None})
    assert lines[0] == "Already have (5) (AI title match over 4 same-brand thing(s)):" and len(lines) == 6


# --- receipt lines from the database ------------------------------------------------------------

def test_receipt_lines_on_sqlite_are_empty(monkeypatch):
    monkeypatch.setattr(config, "DB_BACKEND", "sqlite")
    assert db.receipt_lines_for_skus([ROSS]) == [] and db.receipt_lines_for_skus([]) == []


def test_receipt_lines_on_sqlserver(mssql, monkeypatch):
    monkeypatch.setattr(config, "DB_BACKEND", "sqlserver")
    cur = mssql.cursor()
    cur.execute("INSERT INTO dbo.Receipts (ReceiptNumber, StoreCity, ReceiptDate) OUTPUT INSERTED.ReceiptID "
                "VALUES ('1234-5', 'HOLLYWOOD', '2026-10-30')")
    rid = cur.fetchone()[0]
    cur.execute("INSERT INTO dbo.ReceiptItems (ReceiptID, LineIndex, SKU, Description, Price) "
                "VALUES (?, 1, ?, 'JUICY TOTE', 12.99), (?, 2, '400999999999', 'OTHER', 5.00)", rid, ROSS, rid)
    mssql.commit()
    [line] = db.receipt_lines_for_skus([ROSS])
    assert (line["sku"], line["description"], line["price"], line["receipt_date"], line["store_city"],
            line["receipt_number"]) == (ROSS, "JUICY TOTE", 12.99, "2026-10-30", "HOLLYWOOD", "1234-5")
