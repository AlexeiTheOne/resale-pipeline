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
    assert k["upc"] is None and k["ross"] == {ROSS}


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


def test_receipt_lines_all_shown_even_with_an_item():
    lines = [{"line_id": 7, "sku": ROSS, "description": "JUICY TOTE", "price": 12.99,
              "receipt_date": "2026-10-30", "store_city": "HOLLYWOOD"},
             {"line_id": 3, "sku": ROSS, "description": "JUICY TOTE", "price": 12.99,
              "receipt_date": "2026-10-01", "store_city": "HOLLYWOOD"}]
    asked = []
    res = owned.lookup({}, ROSS, receipts_fn=lambda skus: asked.append(skus) or lines)
    assert asked == [[ROSS]] and [h["id"] for h in res["hits"]] == [7, 3]
    assert owned.hit_text(res["hits"][0]).startswith("Bought Oct 30 (Hollywood) · paid $12.99")
    # receipts misread lines too often to count units by them: every line stays
    _item("published", {"brand": "Juicy Couture"}, receipt={"code": ROSS})
    res = owned.lookup({}, ROSS, receipts_fn=lambda skus: lines)
    assert [h["source"] for h in res["hits"]] == ["item", "receipt", "receipt"]


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
    assert text[1] == ("• Listed $59.99, 12 days · paid $9.99 · UPC · 🔗 Juicy <Weekender> "
                       "https://www.ebay.com/itm/117000000002")
    assert len(text) == 5 and text[-1] == "• … 1 more in Details"
    html = owned.reply_lines({"hits": [_hit(match=owned.SAME_MODEL, model_code="JC1234")]}, html=True)
    assert html[0] == "✅ New"
    assert html[1].startswith("• Same model JC1234, other prints/colours: 1 listed") and html[1].endswith(
        '🔗 <a href="https://www.ebay.com/itm/117000000002">Juicy &lt;Weekender&gt;</a>')


def _line(n, receipt, date, paid, desc="EH Pink aop fish f"):
    return _hit(source="receipt", id=n, status="bought", receipt=receipt, date=date, city="HIALEAH", paid=paid,
                description=desc, name=desc, url=None, match=owned.ROSS)


def test_receipt_lines_are_one_reply_line_per_product():
    # check #4's Ross code: a receipt listing it at two prices is a misread
    lines = [_line(5, "R3", "2026-09-19", 7.99), _line(4, "R2", "2026-09-14", 7.99),
             _line(3, "R2", "2026-09-14", 4.99), _line(2, "R1", "2026-08-11", 15.99),
             _line(1, "R1", "2026-08-11", 0.99)]
    s = owned.receipts_summary(lines)
    assert (s["receipts"], s["lines"], s["misread"], s["low"], s["high"]) == (3, 5, 2, 7.99, 7.99)
    text = owned.reply_lines({"hits": [_hit()] + lines})
    assert text[0] == '🔁 Already have: "EH Pink aop fish f" (receipt) — 1 listed, 3 receipts' and len(text) == 3
    assert text[2] == ('• Bought on 3 receipts (Aug 11 – Sep 19) · "EH Pink aop fish f" · paid $7.99 · '
                       "2 receipts misread (one code, several prices) · Ross code")
    assert len(owned.details_lines({"hits": [_hit()] + lines})) == 7
    # two at one price on one receipt: bought 2, not a misread
    assert owned.receipts_summary([_line(2, "R1", "2026-08-11", 9.99), _line(1, "R1", "2026-08-11", 9.99)])[
        "misread"] == 0


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


# --- what it is, and lending it to the search ---------------------------------------------------

ED_HARDY = {"brand": "Ed Hardy", "product_name": '"Love Kills Slowly" Tattoo Print Weekender Duffel Bag',
            "item_type": "Weekender Duffel Bag", "color": "Pink Multicolor", "model": "EHH230-26-PNA",
            "upc": "796483413276"}


def test_what_it_is():
    assert owned.what_it_is(ED_HARDY) == ('Ed Hardy "Love Kills Slowly" Tattoo Print Weekender Duffel Bag · '
                                          "bag: weekender duffel · pink multicolor")
    assert owned.what_it_is({"brand": "Vans", "product_name": "Old Skool", "item_type": "Sneakers"}) == \
        "Vans Old Skool · shoes: sneakers"
    assert owned.what_it_is({"brand": "Michael Kors", "item_type": "necklace"}) == "Michael Kors · accessory: necklace"


def test_first_line_says_what_the_matched_item_is():
    hits = [_hit(ident={**ED_HARDY, "title": "Ed Hardy Love Kills Slowly Weekender"}, match=owned.ROSS),
            _line(1, "R1", "2026-08-11", 15.99)]
    head = owned.reply_lines({"hits": hits})[0]
    assert head == ('🔁 Already have: Ed Hardy "Love Kills Slowly" Tattoo Print Weekender Duffel Bag · '
                    "bag: weekender duffel · pink multicolor — 1 listed, 1 receipt")
    # only receipts: their description
    assert owned.reply_lines({"hits": [_line(1, "R1", "2026-08-11", 1.49, "4PK LUBA LIME MARG")]})[0] == \
        '🔁 Already have: "4PK LUBA LIME MARG" (receipt) — 1 receipt'


def test_borrow_for_a_typed_code_takes_our_item():
    hit = {"source": "item", "id": "da8b1b25", "match": owned.ROSS,
           "ident": {**ED_HARDY, "title": "Ed Hardy Love Kills Slowly Weekender Duffel Bag Pink"}}
    typed = {"product_name": ROSS, "search_query": ROSS, "upc": ROSS}
    got, info = owned.borrow_identification(typed, hit)
    assert got["search_query"] == "Ed Hardy Love Kills Slowly Weekender Duffel Bag Pink"
    assert (got["brand"], got["model"], got["upc"]) == ("Ed Hardy", "EHH230-26-PNA", "796483413276")
    assert info == {"source": "item", "id": "da8b1b25", "match": owned.ROSS, "query": got["search_query"]}
    # a photo's own identification is kept; only the gaps are filled
    photo = {"brand": "Ed Hardy", "product_name": "Tattoo duffel", "search_query": "ed hardy duffel"}
    got, _ = owned.borrow_identification(photo, hit)
    assert (got["product_name"], got["search_query"], got["model"]) == ("Tattoo duffel", "ed hardy duffel",
                                                                        "EHH230-26-PNA")
    assert owned.borrow_identification(typed, None) == (typed, None)
    assert owned.donor({"hits": [{**hit, "match": owned.AI_SAME}]}) is None      # weak matches lend nothing


def test_run_check_searches_our_item_never_the_ross_code():
    from pipeline import check
    _item("published", ED_HARDY, listing={"title": "Ed Hardy Love Kills Slowly Weekender Duffel Bag Pink"},
          receipt={"code": ROSS, "reduced_price": 15.99})
    seen = []
    res = check.run_check(check.parse_request(f"/check {ROSS} 15.99"),
                          pricing_fn=lambda ident, **kw: seen.append(ident) or {"judged_comps": [], "timings": {}})
    [ident] = seen
    assert ident["search_query"] == "Ed Hardy Love Kills Slowly Weekender Duffel Bag Pink"
    assert not any(ROSS in str(v) for v in ident.values())
    assert res["borrowed"]["match"] == owned.ROSS and res["typed_ross"] == ROSS
    assert check.check_fields(res)["ross_sku"] == ROSS
    reply = check.format_reply(res)
    assert reply.splitlines()[0].startswith('🔁 Already have: Ed Hardy "Love Kills Slowly"')
    assert ROSS not in reply.split("🔎", 1)[1]                        # the eBay search links
    assert "Searched as our item" in check.format_details(res)


def test_an_unknown_ross_code_is_not_searched():
    from pipeline import check
    res = check.run_check(check.parse_request("/check 400999999990 9.99"),
                          pricing_fn=lambda ident, **kw: (_ for _ in ()).throw(AssertionError("searched")))
    assert res["no_search"] and res["verdict"] == check.NO_DATA
    reply = check.format_reply(res)
    assert "Only a Ross code" in reply and "Ross code 400999999990" in reply and "🔎" not in reply


# --- full vs base style code, colours, brand concentration (check #5, Ed Hardy EHH108) ---------

WILMA = {"brand": "Ed Hardy", "product_name": "Wilma Print Tattoo Skull Shoulder Tote Bag", "model": "EHH108-26-WIP",
         "color": "Black Multi / Wilma Print", "item_type": "Shoulder Tote Bag"}


def test_style_codes_full_base_and_suffix():
    assert owned.style_codes({"model": "EHH108-26-WIP"}) == ({"ehh10826wip"}, {"ehh108"}, {"ehh10826wip"})
    assert owned.style_codes({"model": "LB102100"}) == ({"lb102100"}, {"lb102100"}, set())
    # a first part too short to be a base (3347c963)
    assert owned.style_codes({"model": "EH-H02-26-DHO"}) == ({"ehh0226dho"}, {"ehh02"}, {"ehh0226dho"})
    assert owned.style_codes({"model": "XW09620 970 0IC"}) == ({"xw096209700ic"}, {"xw09620"}, {"xw096209700ic"})


@pytest.mark.parametrize("theirs,expected", [
    ({"model": "EHH108-26-WIP", "color": "Pink"}, owned.STYLE_FULL),    # the suffix is the print
    ({"model": "EHH108-26-TLP"}, owned.SAME_MODEL),                     # Tillie print
    ({"model": "EHH108", "color": "Black Multi"}, owned.SAME_MODEL),    # base only: never the same item
    ({"model": "EHH203"}, None),
])
def test_full_style_is_the_same_item_base_is_the_same_model(theirs, expected):
    assert owned.match(owned.keys(WILMA), owned.keys(theirs)) == expected


def test_a_code_with_no_suffix_needs_the_colour():
    lb = {"model": "LB102100", "color": "Cherry Gingham"}
    assert owned.match(owned.keys(lb), owned.keys({"model": "LB102100", "color": "cherry"})) == owned.STYLE
    assert owned.match(owned.keys(lb), owned.keys({"model": "LB102100", "color": "Black"})) == owned.SAME_MODEL


def test_multi_does_not_match_a_named_print():
    assert not owned.same_colour("Multi", "Wilma Print")
    assert not owned.same_colour("Multicolor", "Tillie")
    assert not owned.same_colour("Black Multi / Wilma Print", "Black background, tattoo print")
    assert not owned.same_colour("Wilma Print", "Tillie Print")
    assert owned.same_colour("Black Wilma Print", "wilma print, black")
    assert owned.same_colour("Black Multi", "Black") and owned.same_colour("Multi", "multicolour")


def test_a_base_we_also_hold_with_a_suffix_is_only_the_model():
    # both stored as plain EHH108, but EHH108-26-TLP shows the brand's codes take suffixes
    a, b = owned.keys({"model": "EHH108", "color": "Black"}), owned.keys({"model": "EHH108", "color": "Black"})
    assert owned.match(a, b) == owned.STYLE
    assert owned.match(a, b, {"ehh108"}) == owned.SAME_MODEL


def test_brand_tag_ross_code_is_a_ross_code():
    k = owned.keys({"ross_code": "400298771833"})
    assert k["ross"] == {"400298771833"} and k["upc"] is None
    assert owned.match(owned.keys({}, ["400298771833"]), k) == owned.ROSS


def test_check5_wilma_is_new_with_other_prints_on_their_own_line():
    _item("published", {"brand": "Ed Hardy", "model": "EHH108-26-TLP", "color": "Hot Pink, Black"},
          listing={"title": "Ed Hardy Zip Tote Tillie Print Large Hot Pink"},
          ebay={"listing_id": "117000000009", "published_at": _iso(30)})
    _item("published", {"brand": "Ed Hardy", "model": "EHH108", "color": "Black background with tattoo print"})
    _item("sold", {"brand": "Ed Hardy", "model": "EHH108-26-BTP", "color": "Black Multi"})
    _item("published", {"brand": "Ed Hardy", "model": "EHH230-26-PNA"})
    res = owned.lookup(dict(WILMA), ["400298771833"])
    assert {h["match"] for h in res["hits"]} == {owned.SAME_MODEL}
    assert res["brand"] == {"brand": "Ed Hardy", "listed": 3, "home": 0, "sold": 1}
    lines = owned.reply_lines(res)
    assert lines[0] == "✅ New"
    assert lines[1].startswith("• Same model EHH108, other prints/colours: 2 listed, 1 sold · 🔗 Ed Hardy Zip Tote")
    assert lines[2] == "• Ed Hardy: 3 listed, 1 sold"
    assert owned.donor(res) is None                                   # another print lends nothing to pricing


def test_new_with_brand_on_the_same_line():
    _item("published", {"brand": "Ed Hardy", "model": "EHH230-26-PNA"})
    no_ai = lambda ident, cands: []  # noqa: E731
    assert owned.reply_lines(owned.lookup({"brand": "Ed Hardy", "model": "EHH999-26-ZZZ"}, ai_fn=no_ai)) == \
        ["✅ New · Ed Hardy: 1 listed"]
    assert owned.reply_lines(owned.lookup({"brand": "Nobody"})) == ["✅ New · Nobody: none yet"]


def test_identify_keeps_the_full_style_code():
    import identify
    assert identify.full_style_code("EHH108", ["EHH108-26-WIP", "X1234"]) == "EHH108-26-WIP"
    assert identify.full_style_code("EHH108-26-WIP", []) == "EHH108-26-WIP"
    assert identify.full_style_code("LB102100", ["AB12345"]) == "LB102100"
    assert identify.full_style_code("Krush Weekender", ["EHH108"]) is None
