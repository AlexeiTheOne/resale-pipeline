"""The old eBay store's sales: the workbook parser (scripts/import_old_store.py),
the CSV import with its backup (pipeline/old_store.py, both backends), the
bot handler (bot/old_store_cmds.py) and /check level 3 "old store"."""
import csv
import sys
from datetime import date
from pathlib import Path

import openpyxl
import pytest

import config
import db
from bot import old_store_cmds
from pipeline import check, old_store
from tests import tg_fakes

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
import import_old_store as script  # noqa: E402

OWNER = tg_fakes.DEFAULT_USER_ID


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    """Each test its own SQLite file (the SQL Server run empties its test DB
    before every test already)."""
    if config.DB_BACKEND == "sqlite":
        monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "ross.db"))
        db.create_tables()
    old_store._cache.clear()


def row(n=1, **kw):
    base = {"source": "old_store", "source_tab": "May 1st - Aug 31 st 2026", "source_row": n,
            "period_start": "2026-05-01", "period_end": "2026-08-31", "year_inferred": 0,
            "title": f"NWT Juicy Couture Weekender {n}", "ebay_item_id": None, "quantity": 1,
            "item_sales": 44.99, "unit_price": 44.99, "shipping_per_unit": 9.99, "label_per_unit": 8.93,
            "fees_per_unit": 7.5, "net_per_unit": 38.55, "price_paid": 15.0, "cost_per_unit": 16.05,
            "best_offer_units": 0, "seller_offer_units": None, "status": "ok", "note": None,
            "brand": "Juicy Couture", "parent_brand": None, "item_type": "bag", "bag_size": "large",
            "msrp": 118.0, "msrp_from": "title_msrp"}
    return {**base, **kw}


def write(path, rows, columns=None):
    columns = columns or list(db.OLD_STORE_COLUMNS)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)
    return path


# --- the script and the CSV agree ---------------------------------------------------

def test_script_columns_and_types_match_the_table():
    assert tuple(script.COLUMNS) == db.OLD_STORE_COLUMNS
    assert script.ITEM_TYPES == old_store.ITEM_TYPES


# --- parsing the workbook by the sums ------------------------------------------------

def test_a_normal_row():
    r = ["NWT Vera Bradley Tote", 3, 155.61, 134.97, 0, 9.67, 10.97, 35.93, 0, 0, 23.74, 6.22, 0, 5.97,
         110.01, 51.87, 0, 10, 35.1, 74.91]
    s = script.solve_row(r)
    assert (s["quantity"], s["item_sales"], s["shipping"], s["costs"], s["net"]) == (3, 134.97, 10.97, 35.93, 110.01)


def test_a_row_with_a_repeated_quantity_and_one_tax_column():
    """November 10 - December 15: the data sits one column right of the header."""
    r = ["Michel Germain Deauville Bleu", 5, 5, 215.2, 174.95, 15.3, 24.95, 65.05, 30, 6.46, 28.59, 134.85,
         43.04, 5, 0, 0, 12, 70.2]
    s = script.solve_row(r)
    assert (s["quantity"], s["item_sales"], s["shipping"], s["net"], s["n_idx"]) == (5, 174.95, 24.95, 134.85, 11)


def test_a_row_with_empty_columns_before_the_tax_and_an_ebay_id():
    assert script.solve_row(["Heelys", 6, 220.4, 154.82, None, None, None, 10.64, 54.94, 68.46, 29.94,
                             37.9, 141.3])["shipping"] == 54.94
    s = script.solve_row(["Herschel Wallet", 383945351194, 2, 51.96, 47.98, 0, 3.98, 0, 13.83, 0, 0, 7.12,
                          0, 0, 0, 6.71, 34.15])
    assert s["ebay_item_id"] == "383945351194" and s["costs"] == 13.83 and s["net"] == 34.15


def test_rows_that_dont_add_up_or_arent_sales():
    assert script.solve_row(["shipping fedex", 25.5]) is None
    assert script.solve_row(["Hurley", 1, 40, 30, 0, 5, 9, 99]) is None       # net doesn't match
    assert script.solve_row(["4-Apr 24-Apr"]) is None


def test_price_paid_and_offer_units():
    assert script._price_paid([0, 0, 12, 70.2], 2, 5, None) == (12, 12.84, True)       # 12 x 5 x 1.17
    assert script._price_paid([0, 0, 6, 7.02], 2, 1, None) == (6, 6.42, True)          # x 1.07 (early 2021)
    assert script._price_paid([0, 0, 10, 99.0], 2, 1, 2) == (10, 10.7, False)          # typed Cost is off
    assert script._units([0, 5, 0], 1, 0, 6) == 5
    assert script._units([0, 300, 0], 1, 0, 11) is None      # the purchase price, not a unit count


def test_msrp_from_the_title():
    assert script.msrp_from_title("NWT Juicy Weekender MSRP $118") == (118.0, "title_msrp")
    assert script.msrp_from_title("Vera Bradley Wristlet in Winter Forest $85") == (85.0, "title_price")
    assert script.msrp_from_title("Herschel Pop Quiz Backpack") == (None, None)


def _sheet(wb, name, header, rows):
    ws = wb.create_sheet(name)
    if header:
        ws.append(header)
    for r in rows:
        ws.append(r)


HEADER = ["Listing title", "Quantity sold", "Total sales (Includes taxes)", "Item sales",
          "Taxes and government fees paid by buyer to eBay", "Shipping and handling paid by buyer to you",
          "Total selling costs", "Final value fees", "Shipping labels cost (Amount you paid)",
          "Net sales (Net of taxes and selling costs)", "Average Selling price", "Price 1", "Cost"]


def test_parse_skips_tabs_totals_zero_sales_and_summary_repeats(tmp_path):
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    _sheet(wb, "Calc", None, [["NWT Something", 1, 2, 3]])
    _sheet(wb, "Sheet2", None, [["loan", 1000]])
    _sheet(wb, "May 1st - Aug 31 st 2026", HEADER, [
        ["NWT Juicy Couture Weekender MSRP $118", 2, 109.98, 89.98, 5, 15, 38.0, 20.14, 17.86, 66.98, 54.99,
         15, 35.1],
        ["Refunded thing", 1, 0, 0, 0, 0, 3, 0, 3, -3, 0, 5, 5.85],
        [None, None, None, None, None, None, None, None, None, None, None, 910.26, 602.62],
    ])
    _sheet(wb, "23 july - 27 aug", HEADER, [["Vans Backpack", 1, 46.2, 34.99, 3.22, 7.99, 13.28, 6.1, 7.18,
                                             29.7, 46.2, 15, 17.55]])
    _sheet(wb, "Sales statistics", HEADER, [["Vans Backpack", 1, 46.2, 34.99, 3.22, 7.99, 13.28, 6.1, 7.18,
                                             29.7, 46.2, 15, 17.55]])
    path = tmp_path / "wb.xlsx"
    wb.save(path)
    rows, rejected = script.parse(path)
    assert [r["title"] for r in rows] == ["Vans Backpack", "NWT Juicy Couture Weekender MSRP $118"]
    juicy = rows[1]
    assert (juicy["quantity"], juicy["unit_price"], juicy["shipping_per_unit"], juicy["label_per_unit"],
            juicy["price_paid"], juicy["period_end"]) == (2, 44.99, 7.5, 8.93, 15, "2026-08-31")
    assert {x["why"][:6] for x in rejected} == {"$0 sal", "repeat"}


def test_a_tab_without_a_period_is_an_error(tmp_path):
    wb = openpyxl.Workbook()
    wb.active.title = "Some new tab"
    path = tmp_path / "wb.xlsx"
    wb.save(path)
    with pytest.raises(ValueError, match="no period"):
        script.parse(path)


# --- the CSV ----------------------------------------------------------------------------

def test_read_csv_types_the_values(tmp_path):
    got = old_store.read_csv(write(tmp_path / "a.csv", [row(1), row(2, bag_size=None, msrp=None)]))
    assert got[0]["unit_price"] == 44.99 and got[0]["quantity"] == 1 and got[1]["bag_size"] is None


@pytest.mark.parametrize("bad, why", [
    ({"source": "ebay"}, "source must be old_store"),
    ({"bag_size": "huge"}, "bag_size"),
    ({"item_type": "spaceship"}, "unknown item_type"),
    ({"period_end": "Aug 31"}, "not a date"),
    ({"quantity": 0}, "quantity"),
    ({"status": "maybe"}, "status"),
])
def test_read_csv_refuses_bad_rows(tmp_path, bad, why):
    with pytest.raises(old_store.CsvError, match=why):
        old_store.read_csv(write(tmp_path / "a.csv", [row(1), row(2, **bad)]))


def test_read_csv_refuses_other_files_and_repeats(tmp_path):
    with pytest.raises(old_store.CsvError, match="columns"):
        old_store.read_csv(write(tmp_path / "a.csv", [row(1)], columns=["title", "price"]))
    with pytest.raises(old_store.CsvError, match="appears twice"):
        old_store.read_csv(write(tmp_path / "b.csv", [row(1), row(1)]))
    with pytest.raises(old_store.CsvError, match="no rows"):
        old_store.read_csv(write(tmp_path / "c.csv", []))


def test_import_backs_up_first_then_replaces_every_row(tmp_path):
    calls = []

    def backup():
        calls.append(len(db.old_store_sales()))      # rows present when the backup runs
        return tmp_path / "ross-1.bak"
    first = old_store.import_csv(write(tmp_path / "a.csv", [row(1), row(2, period_end="2021-08-27",
                                                                         period_start="2021-07-23")]),
                                 backup_fn=backup)
    assert first["rows"] == 2 and first["replaced"] == 0 and first["recent"] == 1 and first["units"] == 2
    second = old_store.import_csv(write(tmp_path / "b.csv", [row(7, unit_price=50.0, item_sales=50.0)]),
                                  backup_fn=backup)
    assert second["replaced"] == 2 and calls == [0, 2]
    got = db.old_store_sales()
    assert len(got) == 1 and got[0]["source_row"] == 7 and got[0]["unit_price"] == 50.0
    assert got[0]["period_end"] == "2026-08-31" and got[0]["year_inferred"] is False


def test_nothing_is_written_when_the_backup_or_the_csv_fails(tmp_path):
    old_store.import_csv(write(tmp_path / "a.csv", [row(1)]), backup_fn=lambda: "x.bak")

    def broken():
        raise RuntimeError("disk full")
    with pytest.raises(RuntimeError):
        old_store.import_csv(write(tmp_path / "b.csv", [row(2), row(3)]), backup_fn=broken)
    with pytest.raises(old_store.CsvError):
        old_store.import_csv(write(tmp_path / "c.csv", [row(4, source="x")]), backup_fn=lambda: "x.bak")
    assert [r["source_row"] for r in db.old_store_sales()] == [1]


def test_old_store_sales_since(tmp_path):
    old_store.import_csv(write(tmp_path / "a.csv", [
        row(1), row(2, period_start="2023-10-07", period_end="2023-11-10"),
        row(3, period_start="2021-07-23", period_end="2021-08-27")]), backup_fn=lambda: "x.bak")
    assert [r["source_row"] for r in db.old_store_sales("2023-11-11")] == [1]
    assert len(db.old_store_sales()) == 3


def test_the_window_is_three_years_by_default():
    assert old_store.window_start(date(2026, 11, 11)) == "2023-11-11"
    assert old_store.window_start(date(2026, 11, 11), years=1) == "2025-11-11"


# --- matching -----------------------------------------------------------------------------

def shaped(brand, price, units=1, item_type="bag", size="large", parent=None):
    return old_store._shape({"brand": brand, "parent_brand": parent, "item_type": item_type,
                             "bag_size": size, "unit_price": price, "quantity": units,
                             "period_end": "2025-01-31"})


JUICY_ROWS = ([shaped("Juicy Couture", p) for p in (40, 45, 50)]
              + [shaped("Juicy Couture", 25, size="small"), shaped("Juicy Couture", 27, units=2, size="small"),
                 shaped("Juicy Couture", 29, size="small")])


def test_brand_type_and_size_first_then_brand_and_type():
    m = old_store.match("Juicy Couture", "bag", "large", JUICY_ROWS)
    assert (m["value"], m["n"], m["size"], m["match"]) == (45, 3, "large", "brand + type + size")
    m = old_store.match("Juicy Couture", "bag", "small", JUICY_ROWS)       # 4 units over 3 listings
    assert (m["value"], m["n"], m["match"]) == (27, 4, "brand + type + size")
    m = old_store.match("Juicy Couture", "bag", None, JUICY_ROWS)          # size unknown
    assert (m["value"], m["n"], m["match"]) == (29, 7, "brand + type")     # 25 27 27 [29] 40 45 50
    assert old_store.match("Juicy Couture", "watch band", None, JUICY_ROWS) is None
    assert old_store.match("Coach", "bag", "large", JUICY_ROWS) is None
    assert old_store.match(None, "bag", "large", JUICY_ROWS) is None


def test_sub_lines_stay_apart_and_fall_back_to_the_parent_under_three_sales():
    rows = ([shaped("Betsey Johnson", p) for p in (48, 50, 52)]
            + [shaped("Luv Betsey", 60, parent="Betsey Johnson")])
    m = old_store.match("Luv Betsey by Betsey Johnson", "bag", "large", rows)
    assert (m["brand"], m["n"], m["value"], m["match"]) == ("Betsey Johnson", 4, 51, "parent brand + type + size")
    rows += [shaped("Luv Betsey", p, parent="Betsey Johnson") for p in (45, 46)]
    m = old_store.match("Luv Betsey", "bag", "large", rows)                # 3 of its own now
    assert (m["brand"], m["n"], m["value"]) == ("Luv Betsey", 3, 46)
    # the parent's own sales don't take in the sub-line
    assert old_store.match("Betsey Johnson", "bag", "large", rows)["n"] == 3


def test_one_bulk_listing_is_not_three_sales():
    """Luv Betsey on dev: one listing of 3 at $26 was its only large bag sale in
    3 years; the Betsey Johnson family answers instead."""
    rows = [shaped("Luv Betsey", 26, units=3, parent="Betsey Johnson")] +         [shaped("Betsey Johnson", p) for p in (49.99, 59.99, 49.99)]
    m = old_store.match("Luv Betsey", "bag", "large", rows)
    assert (m["brand"], m["n"], m["value"], m["match"]) == ("Betsey Johnson", 6, 38.0,
                                                            "parent brand + type + size")


def test_too_few_sales_anywhere_still_gives_the_brand_and_type():
    m = old_store.match("Vans", "bag", "large", [shaped("Vans", 35), shaped("Vans", 30, size="small")])
    assert (m["n"], m["value"], m["match"]) == (2, 32.5, "brand + type")


def test_brand_names_resolve_by_words():
    keys = {"luv betsey", "betsey johnson", "kate spade", "herschel supply co", "ralph lauren",
            "lauren ralph lauren"}
    assert old_store.resolve_brand("Luv Betsey by Betsey Johnson", keys) == "luv betsey"
    assert old_store.resolve_brand("Kate Spade New York", keys) == "kate spade"
    assert old_store.resolve_brand("Herschel", keys) == "herschel supply co"
    assert old_store.resolve_brand("Lauren Ralph Lauren", keys) == "lauren ralph lauren"
    assert old_store.resolve_brand("RALPH LAUREN", keys) == "ralph lauren"
    assert old_store.resolve_brand("Coach", keys) is None


# --- /check level 3 -----------------------------------------------------------------------

JUICY = {"brand": "Juicy Couture", "item_type": "Weekender Tote Bag", "retail_price": 109.0,
         "product_name": "Juicy To Go Weekender Tote"}


def pricing():
    return {"judged_comps": [], "raw_active_floor": None}


def ours(n, sold=58.0):
    return [{"item_id": f"j{i}", "brand": "juicy couture", "bucket": "bag", "msrp": None, "sold": sold,
             "ratio": None, "days": 12.0} for i in range(n)]


def test_old_store_fills_level_3_when_we_have_too_few_sales():
    pick = check.choose_price(pricing(), True, JUICY, history=ours(1), old_store_rows=JUICY_ROWS)
    l3 = pick["levels"][3]
    assert l3["source"] == "old_store" and l3["value"] == 45 and l3["n"] == 3 and l3["size"] == "large"
    assert 3 in pick["used"] and pick["price"] is not None
    line = check.ladder_line(pick, {})
    assert "our Juicy Couture bags ~$58 (1)" in line and "old store Juicy Couture large bags ~$45 (3)" in line
    assert "old store" in l3["detail"] and "brand + type + size" in l3["detail"]


def test_our_own_sales_win_from_three():
    pick = check.choose_price(pricing(), True, JUICY, history=ours(3), old_store_rows=JUICY_ROWS)
    assert pick["levels"][3].get("source") is None and pick["levels"][3]["value"] == 58
    assert "old store" not in check.ladder_line(pick, {})


def test_old_store_off_or_empty_leaves_the_ladder_as_it_was(monkeypatch):
    assert 3 not in check.choose_price(pricing(), True, JUICY, history=[], old_store_rows=[])["levels"]
    monkeypatch.setattr(config, "CHECK_OLD_STORE_YEARS", 0)
    assert 3 not in check.choose_price(pricing(), True, JUICY, history=[], old_store_rows=JUICY_ROWS)["levels"]


def test_level_3_reads_the_db_window_when_not_given(tmp_path, monkeypatch):
    old_store.import_csv(write(tmp_path / "a.csv", [
        row(1, unit_price=40.0), row(2, unit_price=45.0), row(3, unit_price=50.0),
        row(4, unit_price=99.0, period_start="2021-07-23", period_end="2021-08-27")]), backup_fn=lambda: "x")
    pick = check.choose_price(pricing(), True, JUICY, history=[])
    assert pick["levels"][3]["value"] == 45 and pick["levels"][3]["n"] == 3     # the 2021 sale is too old
    monkeypatch.setattr(config, "CHECK_OLD_STORE_YEARS", 10)
    old_store._cache.clear()
    assert check.choose_price(pricing(), True, JUICY, history=[])["levels"][3]["n"] == 4


def test_old_store_sales_back_a_skip_like_our_own():
    pick = check.choose_price(pricing(), True, JUICY, history=[], old_store_rows=JUICY_ROWS)
    assert 3 in pick["used"]
    v, _ = check.verdict(profit=1.0, roi=0.05, sales=0, profit_at_cheapest=None, estimated=True,
                         own_sales=3 in pick["used"])
    assert v == "SKIP"


def test_bag_size_can_say_unknown():
    assert check.bag_size({"item_type": "Handbag"}) == "large"
    assert check.bag_size({"item_type": "Handbag"}, default=None) is None
    assert check.bag_size({"item_type": "Crossbody Bag"}, default=None) == "small"


# --- the bot ------------------------------------------------------------------------------

def _send(monkeypatch, tmp_path, data, user=OWNER):
    monkeypatch.setattr(config, "OWNER_USER_IDS", {OWNER})
    monkeypatch.setattr(old_store_cmds, "IMPORT_DIR", tmp_path / "import")
    update, ctx = tg_fakes.make_document_update(data, "old_store_sales.csv", user_id=user)
    tg_fakes.run(old_store_cmds.old_store_document, update, ctx)
    return "\n".join(tg_fakes.sent_texts(ctx))


def test_owner_sends_the_csv_and_it_is_imported_after_a_backup(monkeypatch, tmp_path):
    backups = []
    real = old_store.import_csv
    monkeypatch.setattr(old_store, "import_csv",
                        lambda path: real(path, backup_fn=lambda: backups.append(1) or "D:\\SQLBackups\\x.bak"))
    data = write(tmp_path / "a.csv", [row(1), row(2, quantity=2, item_sales=90.0, unit_price=45.0)]).read_bytes()
    text = _send(monkeypatch, tmp_path, data)
    assert "Old store sales imported: 2 rows (3 units)" in text and "DB backed up first: D:\\SQLBackups\\x.bak" in text
    assert backups == [1] and len(db.old_store_sales()) == 2
    saved = list((tmp_path / "import").iterdir())
    assert len(saved) == 1 and saved[0].name.startswith("old-store-")      # our name, not the sender's
    assert "replacing the 2 imported before" in _send(monkeypatch, tmp_path, data)


def test_not_the_owner_or_not_the_csv(monkeypatch, tmp_path):
    data = write(tmp_path / "a.csv", [row(1)]).read_bytes()
    assert "owner-only" in _send(monkeypatch, tmp_path, data, user=5151)
    text = _send(monkeypatch, tmp_path, b"title,price\nbag,10\n")
    assert "isn't the old store's sales" in text and "Nothing imported" in text
    monkeypatch.setattr(config, "OLD_STORE_MAX_BYTES", 10)
    assert "Nothing imported" in _send(monkeypatch, tmp_path, data)
    assert db.old_store_sales() == []
