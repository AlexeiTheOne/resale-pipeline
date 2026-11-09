"""Receipt line decoding (pipeline/receipt_decode.py, docs/receipt-decoding.md)."""
import pytest

import config
import db
from pipeline import receipt_decode as rd


# --- rules on real receipt lines ------------------------------------------------------------------

@pytest.mark.parametrize("desc, cat, typ", [
    ("#ASST RUN/COURT", "shoes", "sneakers"),
    ("TR ROCCO SN FLP", "clothing", "jeans"),
    ("NOEX Drunk Elephan", "beauty", None),
    ("n! TR 38 blk", "accessories", "belt"),
    ("EH DUFFEL TATTOO", "bags", "duffel"),
    ("VB QLT BKPK", "bags", "backpack"),
    ("MK 40MM GLD", "jewelry_watches", "watch"),
    ("TOMMY-LUANNA II-UT", "bags", None),
])
def test_rules_read_real_lines(desc, cat, typ):
    r = rd.decode_rules(desc)
    assert (r["category"], r["item_type"]) == (cat, typ)


def test_rules_details():
    r = rd.decode_rules("n! TR 38 B1K")
    assert r["brand"] == "True Religion" and r["gender"] == "men" and r["color"] == "black"   # B1K is OCR for BLK
    assert rd.decode_rules("#LUV LBNOELITA BEA")["category"] == "bags"     # '#' + a bag brand is a bag
    assert rd.decode_rules("01/15/26 20CT GMY")["category"] == "health"     # dated + count = supplement
    assert rd.decode_rules("CHOC TRUFFLE 7OZ")["color"] is None            # colours only on wearables
    assert "too_short" in rd.decode_rules("EH")["notes"]
    assert rd.decode_rules("X")["tier"] is None


def test_noise_is_stripped_before_reading():
    u, markers, notes = rd.normalise("EH BLK SKULL $14.99B")
    assert u == "EH BLK SKULL" and "price_text" in notes
    u, markers, _ = rd.normalise("08/09/26 3.5202 SP")
    assert "dated" in markers and "OZ" in u


def test_item_identity_maps_to_the_vocabulary():
    ident = {"brand": "Ed Hardy", "item_type": "Weekender Duffle Bag", "color": "Pink Multicolor",
             "model": "EHH230-26-PNA"}
    f = rd.item_fields(ident, "i1", rd.decode_rules("EH PINK WEEKENDER (GS)"))
    assert (f["category"], f["item_type"], f["color"], f["brand_guess"], f["product_model"], f["source"],
            f["item_id"], f["confidence"]) == ("bags", "duffel", "pink/multi", "Ed Hardy", "EHH230-26-PNA",
                                               "matched_item", "i1", 1.0)
    assert f["gender"] == "kids"        # from the description's (GS): items carry no gender
    assert rd.item_fields({"item_type": "Polarized Running Sunglasses"}, None)["item_type"] == "sunglasses"
    assert rd.item_fields({"item_type": "4-Piece Sheet Set"}, None)["category"] == "home"
    assert rd.item_fields({"item_type": "Gadget"}, None)["confidence"] == 0.8


# --- which description gets which decode ---------------------------------------------------------------

def _line(key, sku, price=9.99, desc=None, line_id=1):
    return {"line_id": line_id, "desc_key": key, "description": desc or key, "sku": sku, "price": price}


def _hits(**by_sku):
    """sku -> list of item ids."""
    return {sku: {"items": [({"brand": "Ed Hardy", "item_type": "Tote Bag"},
                             {"source": "item", "id": i}) for i in ids], "checks": []}
            for sku, ids in by_sku.items()}


def test_a_description_is_our_item_only_when_every_line_is_the_same_item():
    lines = [_line("EH LEOPARD LION 4", "400100000001"), _line("EH LEOPARD LION 4", "400100000001", line_id=2),
             _line("EH BLK MIXED", "400100000002"), _line("EH BLK MIXED", "400100000099", line_id=4),
             _line("EH TWO ITEMS", "400100000003")]
    p = rd.plan(lines, {}, _hits(**{"400100000001": ["a"], "400100000002": ["b"], "400100000003": ["c", "d"]}))
    assert p["tiers"]["EH LEOPARD LION 4"] == "matched_item"
    assert p["write"]["EH LEOPARD LION 4"]["item_id"] == "a"
    assert p["tiers"]["EH BLK MIXED"] != "matched_item"          # one line's SKU isn't ours
    assert p["tiers"]["EH TWO ITEMS"] != "matched_item"          # Ross reused the code for 2 items
    assert set(p["validate"]) == {"EH LEOPARD LION 4", "EH BLK MIXED"}


def test_plan_tiers_and_what_is_left_alone():
    lines = [_line("#ASST RUN/COURT", "400200000001"), _line("ZXQ WIDGET", "400200000002"),
             _line("EH", "400200000003"), _line("NEW THING", "400200000004")]
    details = {"ZXQ WIDGET": {"source": "ai", "category": "other"}}
    p = rd.plan(lines, details, {})
    assert p["tiers"] == {"#ASST RUN/COURT": "rules", "ZXQ WIDGET": "ai", "EH": "skip", "NEW THING": "ai"}
    assert [a["key"] for a in p["ai"]] == ["NEW THING"]          # an AI row is never paid for twice
    assert [a["key"] for a in rd.plan(lines, details, {}, redo_ai=True)["ai"]] == ["ZXQ WIDGET", "NEW THING"]
    assert p["write"]["#ASST RUN/COURT"]["model"] == rd.RULES_VERSION
    same = {"#ASST RUN/COURT": {**p["write"]["#ASST RUN/COURT"]}}
    assert "#ASST RUN/COURT" not in rd.plan(lines, same, {})["write"]     # unchanged: not rewritten


def test_an_ai_row_that_becomes_our_item_is_replaced_and_kept_as_prev():
    lines = [_line("EH PINK THING", "400300000001")]
    old = {"EH PINK THING": {"source": "ai", "category": "clothing", "item_type": "tee", "model": "m"}}
    row = rd.plan(lines, old, _hits(**{"400300000001": ["a"]}))["write"]["EH PINK THING"]
    assert row["source"] == "matched_item" and row["decode_raw"]["prev"]["category"] == "clothing"


def test_an_item_row_that_stops_qualifying_goes_back_to_the_ai():
    lines = [_line("EH PINK THING", "400300000001"), _line("EH PINK THING", "400300000002", line_id=2)]
    old = {"EH PINK THING": {"source": "matched_item", "item_id": "a"}}
    p = rd.plan(lines, old, _hits(**{"400300000001": ["a"]}))
    assert [a["key"] for a in p["ai"]] == ["EH PINK THING"]


# --- the AI path, with a fake Claude ---------------------------------------------------------------------

def _fake_ask(answers, calls):
    def ask(stage, prompt, **kw):
        calls.append({"stage": stage, "prompt": prompt, **kw})
        kw["meta"].update(model="claude-haiku-5-5", cost_usd=0.01, input_tokens=100, output_tokens=50)
        return {"lines": answers.pop(0)}
    return ask


def _a(n, cat="bags", typ="tote", **kw):
    return {"n": n, "cat": cat, "type": typ, "gender": kw.get("gender", "none"), "color": kw.get("color", ""),
            "brand": kw.get("brand", ""), "model": kw.get("model", ""), "conf": kw.get("conf", 0.9)}


def test_ai_answers_are_normalised():
    batch = [{"key": "A", "desc": "a", "prices": [4.99, 9.99, 4998]}, {"key": "B", "desc": "b", "prices": []}]
    calls = []
    got = rd.ai_decode(batch, ask=_fake_ask([[
        _a(1, "food", "tote", color="Black", conf=1.4),            # type not of this category; colour on food
        _a(2, "bags", "tote", gender="women", color="Pink/Black", brand=" Ed Hardy ", model="X1")]], calls),
        meta={})
    assert got["A"]["item_type"] is None and got["A"]["color"] is None and got["A"]["confidence"] == 1.0
    assert (got["B"]["color"], got["B"]["brand_guess"], got["B"]["product_model"], got["B"]["gender"]) == \
        ("pink/black", "Ed Hardy", "X1", "women")
    assert got["B"]["model"] == "claude-haiku-5-5" and got["B"]["source"] == "ai"
    assert calls[0]["stage"] == rd.AI_STAGE and calls[0]["effort"] == "low"
    assert '| $4.99–$9.99 |' in calls[0]["prompt"]                 # the misread $4998 is left out of the hint


def test_ai_batches_retry_missing_lines_once_and_stop_at_the_cap(monkeypatch):
    monkeypatch.setattr(config, "RECEIPT_DECODE_BATCH", 2)
    pending = [{"key": k, "desc": k, "prices": []} for k in "ABCDE"]
    calls = []
    answers = [[_a(1)], [_a(1), _a(2)], [_a(1)], [_a(1)]]       # B is missed in the first batch
    got, spent = rd.ai_decode_all(pending, max_usd=1, ask=_fake_ask(answers, calls), log=lambda *a: None)
    assert set(got) == {"A", "C", "D", "E", "B"} and spent["calls"] == 4 and spent["left"] == 0
    calls.clear()
    got, spent = rd.ai_decode_all(pending, max_usd=0.015, ask=_fake_ask([[_a(1), _a(2)]] * 3, calls),
                                  log=lambda *a: None)
    assert spent["calls"] == 1 and spent["stopped"] and spent["left"] == 3


def test_a_merged_line_is_never_sure():
    row = rd.ai_fields(_a(1, conf=0.95), "m", merged=True)
    assert row["confidence"] == 0.5
    assert rd.is_merged("AIWA 3PC SPKR/TWS/ 400294104635 20/CD GREY")


def test_decode_pending_writes_rules_and_ai_rows(monkeypatch):
    lines = [_line("#ASST RUN/COURT", "400200000001"), _line("ZXQ WIDGET", "400200000002")]
    monkeypatch.setattr(db, "receipt_decode_inputs", lambda: {"lines": lines, "details": {}, "has_table": True})
    from pipeline import owned
    monkeypatch.setattr(owned, "ross_hits", lambda skus: {})
    saved = []
    monkeypatch.setattr(db, "save_receipt_line_details", lambda rows: saved.extend(rows) or len(rows))
    calls = []
    out = rd.decode_pending(ask=_fake_ask([[_a(1, "toys", "game")]], calls), log=lambda *a: None)
    assert {r["desc_key"]: r["source"] for r in saved} == {"#ASST RUN/COURT": "rules", "ZXQ WIDGET": "ai"}
    assert out["saved"] == 2 and out["ai"] == 1 and out["ai_cost_usd"] == 0.01


# --- misread flags ----------------------------------------------------------------------------------

def _codes(line, receipt=None, **kw):
    return [c for c, _ in rd.misread_reasons(line, receipt, **kw)]


@pytest.mark.parametrize("line, code", [
    ({"description": "", "price": 4.99}, "blank"),
    ({"description": "REEB ? ??????", "price": 4.99}, "garbled"),
    ({"description": "8.25xб", "price": 4.99}, "garbled"),
    ({"description": "PT 25-23", "price": 4.99}, "cut_off"),
    ({"description": "AIWA 3PC SPKR/TWS/ 400294104635 20/CD GREY", "price": 9.99}, "merged"),
    ({"description": "OTC NOEX Tuesday Disc 10% $0.15 Original Price: $1.49", "price": 1.49}, "promo"),
    ({"description": "#ASST AIR MAX $63.00B", "price": 14.99}, "price_shift"),
    ({"description": "SAINTS SOCCER FASH", "price": 4998.0}, "decimal"),
    ({"description": "SAINTS SOCCER FASH", "price": None}, "no_price"),
    ({"description": "SAINTS SOCCER FASH", "price": 0.15}, "tiny"),
    ({"description": "MASTERCARD Auth. No. 0823208", "price": 1.0, "sku": "806000000000"}, "not_item"),
    ({"description": "SOUL WiRELESS HEA", "price": 0.99, "sku": "460283077681"}, "sku"),
    ({"description": "**Begin Return***", "price": 4.99}, "return"),
    ({"description": "CLUB AMERICA NAVY", "price": 9.99, "same_code_prices": 2}, "several_prices"),
    ({"description": "CLUB AMERICA NAVY", "price": 9.99, "fixed": True}, "fixed"),
])
def test_each_misread_flag(line, code):
    assert code in _codes(line)


def test_good_lines_are_not_flagged():
    assert _codes({"description": "MLS ASST WOMEN’S J", "price": 12.99}) == []     # a curly apostrophe
    assert _codes({"description": "• EH BLK SKULL", "price": 0.49}) == []          # a bullet; real $0.49
    # two prices in the text (merged lines): one of them matches
    assert "price_shift" not in _codes({"description": "Workpro 14' Rollin $4.49R $14.99R", "price": 14.99})
    assert "cut_off" not in _codes({"description": "EH", "price": 12.99}, linked=True)   # our item: fine
    assert "cut_off" not in _codes({"description": "08/09/26 3.5202 SP", "price": 2.99})
    assert "price_shift" not in _codes({"description": "#ASST AIR MAX $21.99B", "price": 22.00})


def test_decimal_fix_suggestion():
    assert rd.decimal_fix(4998) == 4.99 and rd.decimal_fix(4399) == 43.99 and rd.decimal_fix(10006) is None
    assert rd.decimal_fix(499.0) is None


def test_total_read_as_an_item():
    receipt = {"subtotal": 148.32, "total": 157.51, "tax": 9.19, "line_count": 6, "lines_sum": 296.64}
    assert "total" in _codes({"description": "CAT LITTLE MACHINE", "price": 148.32}, receipt)
    small = {"subtotal": 14.99, "total": 16.04, "tax": 1.05, "line_count": 3, "lines_sum": 40.0}
    assert "total" not in _codes({"description": "Workpro 14' Rollin", "price": 14.99}, small)   # under $20


# --- in words -------------------------------------------------------------------------------------

def test_describe_and_cities():
    assert rd.describe({"category": "bags", "item_type": "belt_bag", "brand_guess": "Ed Hardy", "color": "pink",
                        "gender": "women", "confidence": 0.5}) == "Ed Hardy · bags: belt bag · women's · pink?"
    assert rd.describe(None) == ""
    assert rd.clean_city("Press For Less Doral") == "Doral" and rd.clean_city("Dress For Less ") is None
    assert rd.store_cities([("2710", "Dress For Less Miami", 5), ("2710", "Ress For Less Mimi", 1),
                            ("0443", None, 2)]) == {"2710": "Miami"}


# --- SQL Server ------------------------------------------------------------------------------------

def _receipt(cur, number, date, lines, store="2710", city="Dress For Less Miami", subtotal=None):
    cur.execute("INSERT INTO dbo.Receipts (ReceiptNumber, ReceiptDate, StoreCity, Subtotal) "
                "OUTPUT INSERTED.ReceiptID VALUES (?, ?, ?, ?)", number, date, city, subtotal)
    rid = cur.fetchone()[0]
    for i, (sku, desc, price) in enumerate(lines, 1):
        cur.execute("INSERT INTO dbo.ReceiptItems (ReceiptID, LineIndex, SKU, Description, Price) "
                    "VALUES (?, ?, ?, ?, ?)", rid, i, sku, desc, price)
    return rid


def test_inputs_save_and_reread_on_sqlserver(mssql, monkeypatch):
    monkeypatch.setattr(config, "DB_BACKEND", "sqlserver")
    cur = mssql.cursor()
    rid = _receipt(cur, "2710-03-5447-6292-3", "2026-10-19",
                   [("400300371436", " club america navy ", 16.99), ("400300371436", "CLUB AMERICA NAVY", 9.99),
                    ("400300000001", None, 4.99)])
    mssql.commit()
    data = db.receipt_decode_inputs()
    assert data["has_table"] and data["details"] == {}
    keys = [ln["desc_key"] for ln in data["lines"]]
    assert keys == ["CLUB AMERICA NAVY", "CLUB AMERICA NAVY", None]       # made by SQL: trimmed, upper
    first = data["lines"][0]
    assert first["same_code_prices"] == 2 and first["line_count"] == 3 and first["receipt_id"] == rid
    assert first["receipt_date"] == "2026-10-19" and first["price"] == 16.99
    row = {"desc_key": "CLUB AMERICA NAVY", "category": "clothing", "item_type": "jersey", "gender": None,
           "color": "navy", "brand_guess": None, "product_model": None, "confidence": 0.85, "source": "ai",
           "model": "claude-haiku-5-5", "item_id": None, "decode_raw": {"cat": "clothing"}}
    assert db.save_receipt_line_details([row]) == 1
    assert db.save_receipt_line_details([{**row, "desc_key": "club america navy", "item_type": "hoodie"}]) == 1
    saved = db.receipt_decode_inputs()["details"]
    assert list(saved) == ["CLUB AMERICA NAVY"]                            # updated, not a second row
    assert saved["CLUB AMERICA NAVY"]["item_type"] == "hoodie" and saved["CLUB AMERICA NAVY"]["confidence"] == 0.85
    assert saved["CLUB AMERICA NAVY"]["decode_raw"] == {"cat": "clothing"}


def test_the_save_writes_only_receipt_line_details(monkeypatch):
    import db_mssql
    seen = []

    class Cur:
        def execute(self, sql, *a):
            seen.append(sql)
    monkeypatch.setattr(db_mssql, "_write", lambda fn, *a, **k: fn(Cur(), *a, **k))
    db_mssql.save_receipt_line_details([{"desc_key": "X", "source": "rules", "decode_raw": {}}])
    import re
    tables = {t.lower() for s in seen for t in re.findall(r"(?:UPDATE|INSERT INTO)\s+dbo\.(\w+)", s)}
    assert tables == {"receipt_line_details"}


def test_save_refuses_bad_rows_and_sqlite(monkeypatch):
    with pytest.raises(ValueError):
        db.save_receipt_line_details([{"desc_key": "", "source": "ai"}])
    with pytest.raises(ValueError):
        db.save_receipt_line_details([{"desc_key": "X", "source": "guess"}])
    monkeypatch.setattr(config, "DB_BACKEND", "sqlite")
    with pytest.raises(RuntimeError):
        db.save_receipt_line_details([{"desc_key": "X", "source": "ai"}])
    assert db.receipt_decode_inputs()["lines"] == [] and db.receipt_lines_search("sku", "x")["lines"] == []
