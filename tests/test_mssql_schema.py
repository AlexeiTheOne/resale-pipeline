"""The SQL Server baseline (migrations.MSSQL_MIGRATIONS) builds cleanly and
matches docs/schema-proposal.md.

The doc is the spec: its table and column lists are parsed here and compared
with the database catalog, so a change to either one without the other fails.
Runs against this run's own ResaleDB_Test_* database (see conftest.py).
"""
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pyodbc
import pytest

import migrations
from ebay import orders

ROOT = Path(__file__).parent.parent
DOC = (ROOT / "docs" / "schema-proposal.md").read_text(encoding="utf-8")


# --- the doc, parsed ---------------------------------------------------------

def _cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _names(cell: str) -> set[str]:
    """`created_at / updated_at` or `sold_at, created_at` -> column names."""
    out = set()
    for part in re.split(r"\s*/\s*|,\s+", cell.replace("`", "")):
        m = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_]*)", part)
        if m:
            out.add(m.group(1))
    return out


def _inline_columns(cell: str) -> set[str]:
    """`id INT PK, item_id FK CASCADE, a, b DECIMAL(10,2)` -> names (first word
    of each comma-space part)."""
    out = set()
    for part in re.split(r",\s+", cell.replace("`", "")):
        m = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_]*)", part)
        if m:
            out.add(m.group(1))
    return out


def parse_doc_tables(text: str = DOC):
    """{table: set(columns) or None}, {view: set(columns) or None} from section 1."""
    sec = text[text.index("## 1. Tables"):text.index("## 2. ER diagram")]
    tables, views = {}, {}
    current, section = None, ""
    lines = sec.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("### "):
            section, current = line[4:6], None
        bold = re.findall(r"\*\*`(\w+)`\*\*", line)
        if bold and line.startswith("**`"):
            for n in bold:
                (views if n.startswith("v_") else tables).setdefault(n, None)
            current = bold[0] if len(bold) == 1 else None
        m = re.match(r"^- `(v_\w+)`", line)
        if m:
            views.setdefault(m.group(1), None)
            current = m.group(1)
        if current and re.match(r"^\s*\| Column \|", line):
            cols = set()
            i += 2  # header and separator
            while i < len(lines) and lines[i].lstrip().startswith("|"):
                cols |= _names(_cells(lines[i])[0])
                i += 1
            target = views if current.startswith("v_") else tables
            target[current] = (target.get(current) or set()) | cols
            current = None
            continue
        if section in ("1B", "1E") and line.startswith("| "):
            cells = _cells(line)
            m = re.match(r"^\*\*(\w+)\*\*$", cells[0]) if section == "1B" else \
                re.match(r"^([a-z_]+)$", cells[0])
            if m and cells[0] != "Table" and not cells[0].startswith("-"):
                tables[m.group(1)] = _inline_columns(cells[1])
        i += 1
    return tables, views


def er_entities(text: str = DOC) -> set[str]:
    block = text[text.index("```mermaid"):]
    block = block[:block.index("```", 3)]
    names = set()
    for a, b in re.findall(r"^\s+(\w+)\s+[|o}{]+(?:--|\.\.)[|o}{]+\s+(\w+)\s*:", block, re.M):
        names |= {a, b}
    return names


# --- the catalog -------------------------------------------------------------

def _objects(cur, kind: str) -> set[str]:
    cur.execute(f"SELECT name FROM sys.{kind} WHERE is_ms_shipped = 0")
    return {r[0] for r in cur.fetchall()}


def _columns(cur, obj: str) -> dict:
    cur.execute("""
        SELECT c.name, t.name, c.max_length, c.precision, c.scale, c.is_nullable,
               c.is_identity, c.is_computed
        FROM sys.columns c JOIN sys.types t ON t.user_type_id = c.user_type_id
        WHERE c.object_id = OBJECT_ID(?)""", f"dbo.{obj}")
    return {r[0]: tuple(r[1:]) for r in cur.fetchall()}


@pytest.fixture
def cur(mssql):
    c = mssql.cursor()
    yield c
    c.close()


# --- builds cleanly ----------------------------------------------------------

def test_baseline_is_recorded_and_rerun_does_nothing(mssql, cur):
    cur.execute("SELECT version, name FROM dbo.schema_version")
    assert [tuple(r) for r in cur.fetchall()] == [(1, "resaledb_baseline")]
    assert migrations.migrate_mssql(mssql) == []


def test_read_committed_snapshot_is_on(cur):
    cur.execute("SELECT is_read_committed_snapshot_on FROM sys.databases WHERE name = DB_NAME()")
    assert cur.fetchone()[0] is True


def test_every_constraint_is_trusted_and_enabled(cur):
    cur.execute("SELECT name FROM sys.foreign_keys WHERE is_not_trusted = 1 OR is_disabled = 1 "
                "UNION ALL SELECT name FROM sys.check_constraints WHERE is_not_trusted = 1 OR is_disabled = 1")
    assert cur.fetchall() == []


# --- matches the doc ---------------------------------------------------------

def test_doc_parser_sees_the_whole_design():
    tables, views = parse_doc_tables()
    assert len(tables) == 35, sorted(tables)  # 34 + schema_version
    assert set(views) == {"v_item_sales", "v_item_cost", "v_items", "v_receipt_checks"}
    # Every table's columns are parsed, except the receipt tables, which the doc
    # defines as "same as ReceiptsDB" (checked against RECEIPTSDB below).
    assert {t for t, cols in tables.items() if not cols} == {"Receipts", "ReceiptItems"}


def test_tables_and_views_match_the_doc(cur):
    tables, views = parse_doc_tables()
    assert _objects(cur, "tables") == set(tables)
    assert _objects(cur, "views") == set(views)


def test_er_diagram_names_real_objects(cur):
    assert er_entities() <= _objects(cur, "tables")


@pytest.mark.parametrize("table", sorted(t for t, cols in parse_doc_tables()[0].items() if cols))
def test_columns_match_the_doc(cur, table):
    doc_cols = parse_doc_tables()[0][table]
    assert set(_columns(cur, table)) == doc_cols


def test_doc_view_columns_exist(cur):
    _, views = parse_doc_tables()
    assert views["v_item_cost"] <= set(_columns(cur, "v_item_cost"))
    # Listed in prose in the doc (1B and 1D); each must be named there too.
    prose = {
        "v_item_sales": ["sale_price", "gross_sale_price", "shipping_collected", "ebay_fees",
                         "shipping_label_cost", "ad_fees", "refunded", "net_proceeds",
                         "units_sold", "order_id", "sold_at", "first_sold_at", "fees_known"],
        "v_receipt_checks": ["items_sum", "subtotal_gap", "total_gap", "has_returns",
                             "missing_fields", "suspect_prices", "review_status"],
    }
    for view, cols in prose.items():
        for col in cols:
            assert f"`{col}`" in DOC, f"{col} not in the doc"
        assert set(cols) <= set(_columns(cur, view)), view


# --- receipts stay compatible with receipt_pipeline -------------------------

# ReceiptsDB as surveyed read-only on 2026-11-02: (type, max_length, precision,
# scale, nullable, identity). nvarchar max_length is in bytes.
RECEIPTSDB = {
    "Receipts": {
        "ReceiptID": ("int", 4, 10, 0, False, True), "ReceiptNumber": ("varchar", 30, 0, 0, True, False),
        "Store": ("varchar", 100, 0, 0, True, False), "StoreCity": ("varchar", 100, 0, 0, True, False),
        "StoreZip": ("varchar", 10, 0, 0, True, False), "TransactionType": ("varchar", 10, 0, 0, True, False),
        "ReceiptDate": ("date", 3, 10, 0, True, False), "ReceiptTime": ("time", 5, 16, 7, True, False),
        "Subtotal": ("decimal", 9, 10, 2, True, False), "Tax": ("decimal", 9, 10, 2, True, False),
        "Total": ("decimal", 9, 10, 2, True, False), "Confidence": ("decimal", 5, 4, 2, True, False),
        "Flags": ("varchar", 200, 0, 0, True, False), "RawFileID": ("varchar", 50, 0, 0, True, False),
        "CreatedAt": ("datetime", 8, 23, 3, True, False), "ItemsCount": ("int", 4, 10, 0, True, False),
        "SourceFile": ("nvarchar", 510, 0, 0, True, False), "Status": ("nvarchar", 200, 0, 0, True, False),
    },
    "ReceiptItems": {
        "ItemID": ("int", 4, 10, 0, False, True), "ReceiptID": ("int", 4, 10, 0, False, False),
        "LineIndex": ("int", 4, 10, 0, False, False), "SKU": ("varchar", 20, 0, 0, True, False),
        "Description": ("nvarchar", 1000, 0, 0, True, False), "Price": ("decimal", 9, 10, 2, True, False),
    },
}


@pytest.mark.parametrize("table", sorted(RECEIPTSDB))
def test_receipt_tables_keep_every_receiptsdb_column(cur, table):
    actual = _columns(cur, table)
    for col, (typ, length, prec, scale, nullable, identity) in RECEIPTSDB[table].items():
        assert col in actual, col
        a = actual[col]
        if col == "CreatedAt":  # datetime -> UTC datetime2(3), same nullability
            assert a[0] == "datetime2" and a[4] is True
            continue
        assert (a[0], a[1], a[2], a[3], a[4], a[5]) == (typ, length, prec, scale, nullable, identity), col
    # Everything added is invisible to the receipt bot's INSERTs.
    for col in set(actual) - set(RECEIPTSDB[table]):
        assert actual[col][4] is True or actual[col][6] is True, f"{col} must be nullable or computed"


def test_receipt_bot_sql_works_unchanged(mssql, cur):
    # Copied from receipt_pipeline/bot.py (insert_receipt_sync): text dates
    # in MM/DD/YY and a 12-hour time string, as that bot sends them.
    cur.execute("""
                    INSERT INTO Receipts (
                        ReceiptNumber, Store, StoreCity, StoreZip,
                        TransactionType, ReceiptDate, ReceiptTime,
                        Subtotal, Tax, Total, Confidence, Flags, RawFileID
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                "0443-08-1090-6304-0", "Dress For Less", "Miami", "33186", "sale",
                "10/31/26", "1:41:39 PM", "81.93", "5.73", "87.66", 1.0, "", "20261101_135147_414181")
    cur.execute("SELECT ReceiptID FROM Receipts WHERE ReceiptNumber=?", "0443-08-1090-6304-0")
    receipt_id = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM ReceiptItems WHERE ReceiptID=?", receipt_id)
    assert cur.fetchone()[0] == 0
    cur.executemany("""
                        INSERT INTO ReceiptItems
                        (ReceiptID, LineIndex, SKU, Description, Price)
                        VALUES (?, ?, ?, ?, ?)
                    """, [(receipt_id, 1, "400307072241", "AV mushroom split", "81.93")])
    mssql.commit()
    cur.execute("SELECT ReceiptDate, StoreNumber, CreatedAt FROM Receipts WHERE ReceiptID = ?", receipt_id)
    date, store, created = cur.fetchone()
    assert str(date) == "2026-10-31"  # month first, not day first
    assert store == "0443"
    assert abs(datetime.now(timezone.utc).replace(tzinfo=None) - created) < timedelta(minutes=5)  # UTC


# --- constraints behave as designed -----------------------------------------

def _item(cur, item_id="i1", status="published"):
    cur.execute("INSERT INTO dbo.items (item_id, status) VALUES (?, ?)", item_id, status)


def test_deleting_an_item_cascades_through_pricing_runs(mssql, cur):
    _item(cur)
    cur.execute("INSERT INTO dbo.pricing_runs (item_id, engine) OUTPUT INSERTED.run_id VALUES ('i1', 'v2')")
    run = cur.fetchone()[0]
    cur.execute("INSERT INTO dbo.pricing_comps (run_id, kind, position, price) VALUES (?, 'sold', 0, 10)", run)
    cur.execute("INSERT INTO dbo.item_pricing (item_id, latest_run_id, suggested_price) VALUES ('i1', ?, 20)", run)
    cur.execute("INSERT INTO dbo.item_photos (item_id, position, path) VALUES ('i1', 0, 'a.jpg')")
    cur.execute("INSERT INTO dbo.locations (location_code) VALUES ('G3')")
    cur.execute("INSERT INTO dbo.pieces (code, location_code, item_id) VALUES ('G3-01', 'G3', 'i1')")
    cur.execute("DELETE FROM dbo.items WHERE item_id = 'i1'")
    for table in ("pricing_runs", "pricing_comps", "item_pricing", "item_photos"):
        cur.execute(f"SELECT COUNT(*) FROM dbo.{table}")
        assert cur.fetchone()[0] == 0, table
    cur.execute("SELECT item_id FROM dbo.pieces WHERE code = 'G3-01'")
    assert cur.fetchone()[0] is None  # the piece stays, unlinked


def test_a_sold_item_cannot_be_deleted(cur):
    _item(cur, status="sold")
    cur.execute("INSERT INTO dbo.sales (item_id, order_id, sale, net) VALUES ('i1', 'o1', 10, 8)")
    with pytest.raises(pyodbc.IntegrityError):
        cur.execute("DELETE FROM dbo.items WHERE item_id = 'i1'")


@pytest.mark.parametrize("sql", [
    "INSERT INTO dbo.items (item_id, status) VALUES ('x', 'bogus')",
    "INSERT INTO dbo.locations (location_code) VALUES ('g3')",
    "INSERT INTO dbo.items (item_id, extra_raw) VALUES ('x', 'not json')",
    "INSERT INTO dbo.ebay_listings (item_id, quantity) VALUES ('i1', -1)",
    "INSERT INTO dbo.item_tags (item_id, ross_code) VALUES ('i1', '12345')",
    "INSERT INTO dbo.item_tags (item_id, ross_code) VALUES ('i1', '40030707224A')",
    "INSERT INTO dbo.not_duplicates (a, b) VALUES ('b', 'a')",
    "INSERT INTO dbo.ebay_tokens (id) VALUES (2)",
    "INSERT INTO dbo.item_flags (item_id, kind, flag) VALUES ('i1', 'other', 'x')",
])
def test_bad_rows_are_refused(cur, sql):
    _item(cur)
    with pytest.raises(pyodbc.IntegrityError):
        cur.execute(sql)


def test_piece_codes_must_be_upper_case(cur):
    cur.execute("INSERT INTO dbo.locations (location_code) VALUES ('O1-WATCHES')")
    cur.execute("INSERT INTO dbo.pieces (code, location_code) VALUES ('O1-WATCHES-01', 'O1-WATCHES')")
    with pytest.raises(pyodbc.IntegrityError):
        cur.execute("INSERT INTO dbo.pieces (code, location_code) VALUES ('o1-watches-02', 'O1-WATCHES')")


def test_unique_ebay_ids_ignore_blanks(cur):
    for i in ("a", "b", "c"):
        _item(cur, i)
    cur.execute("INSERT INTO dbo.ebay_listings (item_id) VALUES ('a')")
    cur.execute("INSERT INTO dbo.ebay_listings (item_id) VALUES ('b')")  # two NULL skus: fine
    cur.execute("UPDATE dbo.ebay_listings SET sku = 's1' WHERE item_id = 'a'")
    with pytest.raises(pyodbc.IntegrityError):
        cur.execute("INSERT INTO dbo.ebay_listings (item_id, sku) VALUES ('c', 's1')")


def _receipt_line(cur, sku="400307072241", price="25.00", date="2026-06-01"):
    cur.execute("INSERT INTO dbo.Receipts (ReceiptNumber, ReceiptDate) OUTPUT INSERTED.ReceiptID "
                "VALUES ('2710-01-0000-0000-1', ?)", date)
    rid = cur.fetchone()[0]
    cur.execute("INSERT INTO dbo.ReceiptItems (ReceiptID, LineIndex, SKU, Price) OUTPUT INSERTED.ItemID "
                "VALUES (?, 1, ?, ?)", rid, sku, price)
    return cur.fetchone()[0]


def test_a_receipt_line_is_confirmed_at_most_once(cur):
    _item(cur, "a")
    _item(cur, "b")
    line = _receipt_line(cur)
    ins = ("INSERT INTO dbo.receipt_matches (receipt_item_id, item_id, sku, method, confidence, status) "
           "VALUES (?, ?, '400307072241', 'sku_only', 0.4, ?)")
    cur.execute(ins, line, "a", "proposed")
    cur.execute(ins, line, "b", "proposed")      # several proposals are fine
    cur.execute(ins, line, "a", "confirmed")
    with pytest.raises(pyodbc.IntegrityError):
        cur.execute(ins, line, "b", "confirmed")


def test_a_match_needs_a_piece_or_an_item(cur):
    line = _receipt_line(cur)
    with pytest.raises(pyodbc.IntegrityError):
        cur.execute("INSERT INTO dbo.receipt_matches (receipt_item_id, sku, method, confidence) "
                    "VALUES (?, '400307072241', 'manual', 1)", line)


# --- views -------------------------------------------------------------------

def test_v_item_sales_matches_orders_totals_from_sales(cur):
    _item(cur, status="sold")
    recs = [  # the second order is refunded beyond its item price
        {"order_id": "o1", "sold_at": "2026-08-01T10:00:00", "units": 1, "sale": 40.0,
         "shipping": 8.0, "fees": 6.1, "label": 7.5, "ad_fee": 2.0, "refund": 0.0,
         "net": 32.4, "listing_id": "L1", "fees_known": True},
        {"order_id": "o2", "sold_at": "2026-09-01T10:00:00", "units": 2, "sale": 30.0,
         "shipping": 9.0, "fees": 4.0, "label": 8.0, "ad_fee": 0.0, "refund": 33.0,
         "net": -6.0, "listing_id": "L2", "fees_known": False},
    ]
    for r in recs:
        cur.execute("INSERT INTO dbo.sales (item_id, order_id, listing_id, sold_at, units, sale, shipping, "
                    "fees, label, ad_fee, refund, net, fees_known) VALUES ('i1', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    r["order_id"], r["listing_id"], r["sold_at"], r["units"], r["sale"], r["shipping"],
                    r["fees"], r["label"], r["ad_fee"], r["refund"], r["net"], r["fees_known"])
    want = orders.totals_from_sales(recs)
    cur.execute("SELECT * FROM dbo.v_item_sales WHERE item_id = 'i1'")
    got = dict(zip([d[0] for d in cur.description], cur.fetchone()))
    for k in ("sale_price", "gross_sale_price", "shipping_collected", "ebay_fees",
              "shipping_label_cost", "ad_fees", "refunded", "net_proceeds"):
        assert float(got[k]) == pytest.approx(want[k]), k
    assert got["units_sold"] == want["units"]
    assert got["order_id"] == want["order_id"] == "o2"
    assert got["listing_id"] == want["listing_id"]
    assert got["fees_known"] is want["fees_known"] is False
    assert got["first_sold_at"] == datetime(2026, 8, 1, 10, 0)


def _cost(cur, item_id="i1"):
    cur.execute("SELECT unit_cost, cost_source, tax_rate, unit_cost_with_tax "
                "FROM dbo.v_item_cost WHERE item_id = ?", item_id)
    return tuple(cur.fetchone())


def test_cost_needs_no_receipt(cur):
    _item(cur)
    assert _cost(cur) == (None, None, Decimal("0.0700"), None)
    cur.execute("INSERT INTO dbo.item_tags (item_id, ross_code, ross_price) VALUES ('i1', '400307072241', 23.99)")
    assert _cost(cur) == (Decimal("23.99"), "tag", Decimal("0.0700"), Decimal("25.67"))


def test_a_confirmed_receipt_price_wins_over_the_tag(cur):
    _item(cur)
    cur.execute("INSERT INTO dbo.item_tags (item_id, ross_code, ross_price) VALUES ('i1', '400307072241', 23.99)")
    line = _receipt_line(cur, price="19.99")
    cur.execute("INSERT INTO dbo.receipt_matches (receipt_item_id, item_id, sku, method, confidence, status) "
                "VALUES (?, 'i1', '400307072241', 'sku_unique', 0.7, 'proposed')", line)
    assert _cost(cur)[1] == "tag"  # a proposal changes nothing
    cur.execute("UPDATE dbo.receipt_matches SET status = 'confirmed'")
    assert _cost(cur) == (Decimal("19.99"), "receipt", Decimal("0.0700"), Decimal("21.39"))


def test_tax_rate_comes_from_app_settings(cur):
    _item(cur)
    cur.execute("INSERT INTO dbo.item_tags (item_id, ross_price) VALUES ('i1', 100)")
    cur.execute("UPDATE dbo.app_settings SET value = '0' WHERE name = 'SALES_TAX_RATE'")
    assert _cost(cur)[3] == Decimal("100.00")


def test_v_receipt_checks_flags_without_fixing(cur):
    cur.execute("INSERT INTO dbo.Receipts (ReceiptNumber, ReceiptDate, Subtotal, Tax, Total, Flags) "
                "OUTPUT INSERTED.ReceiptID VALUES ('2243-01-0000-0000-1', '2026-06-01', 100, 7, 110, "
                "'contains_returns')")
    rid = cur.fetchone()[0]
    cur.execute("INSERT INTO dbo.ReceiptItems (ReceiptID, LineIndex, SKU, Price) VALUES (?, 1, 'x', 16998.00)", rid)
    cur.execute("SELECT subtotal_gap, total_gap, has_returns, missing_fields, suspect_prices, items_sum "
                "FROM dbo.v_receipt_checks WHERE ReceiptID = ?", rid)
    assert tuple(cur.fetchone()) == (Decimal("-16898.00"), Decimal("3.00"), True, False, 1,
                                     Decimal("16998.00"))
    cur.execute("SELECT Price FROM dbo.ReceiptItems WHERE ReceiptID = ?", rid)
    assert cur.fetchone()[0] == Decimal("16998.00")  # flagged, not changed


def test_v_items_has_one_row_per_item(cur):
    for i in ("a", "b"):
        _item(cur, i)
    cur.execute("INSERT INTO dbo.locations (location_code) VALUES ('G3')")
    cur.execute("INSERT INTO dbo.pieces (code, location_code, item_id) VALUES ('G3-01', 'G3', 'a'), "
                "('G3-02', 'G3', 'a')")
    cur.execute("SELECT item_id, pieces_home, pieces_total FROM dbo.v_items ORDER BY item_id")
    assert [tuple(r) for r in cur.fetchall()] == [("a", 2, 2), ("b", 0, 0)]


# --- the migration runner ----------------------------------------------------

def test_a_failed_step_changes_nothing(mssql, cur):
    def bad(c):
        c.execute("CREATE TABLE dbo.half_done (x INT)")
        raise RuntimeError("boom")

    steps = migrations.MSSQL_MIGRATIONS + [(2, "bad", bad)]
    with pytest.raises(RuntimeError, match="boom"):
        migrations.migrate_mssql(mssql, steps)
    cur.execute("SELECT OBJECT_ID('dbo.half_done'), MAX(version) FROM dbo.schema_version")
    assert tuple(cur.fetchone()) == (None, 1)


def test_runner_refuses_autocommit(mssql):
    mssql.autocommit = True
    try:
        with pytest.raises(ValueError):
            migrations.migrate_mssql(mssql)
    finally:
        mssql.autocommit = False
