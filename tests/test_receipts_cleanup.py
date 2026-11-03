"""scripts/receipts_cleanup.py and migration step 2 (unique receipt indexes)."""
import importlib.util
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pyodbc
import pytest

import migrations
import sqlserver

_spec = importlib.util.spec_from_file_location(
    "receipts_cleanup", Path(__file__).parent.parent / "scripts" / "receipts_cleanup.py")
rc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rc)


def _fresh_name(*taken) -> str:
    for s in range(1, 120):
        name = sqlserver.new_test_database_name(datetime.now(timezone.utc) + timedelta(seconds=s))
        if name not in taken and not sqlserver.database_exists(name):
            return name
    raise RuntimeError("no free test database name")


@pytest.fixture
def v1(mssql_db):
    """A database at the baseline (before the unique indexes), with receipts
    like the real ones."""
    name = _fresh_name(mssql_db)
    sqlserver.assert_test_database(name)
    sqlserver.create_database(name)
    con = sqlserver.connect(name)
    try:
        migrations.migrate_mssql(con, upto=1)
        cur = con.cursor()
        cur.execute("SET IDENTITY_INSERT dbo.Receipts ON")
        cur.execute("""INSERT INTO dbo.Receipts (ReceiptID, ReceiptNumber, Subtotal, RawFileID, SourceFile) VALUES
            (178, '2211-10-2851-5337-1', 23.50, NULL, NULL),
            (183, '2211-10-2851-5337-1', 23.50, NULL, NULL),
            (195, '0433-01-6531-6002-2', 8.39, NULL, NULL),
            (196, '0433-01-0000-0000-1', NULL, NULL, NULL),
            (200, '0001-01-0000-0000-1', 20.00, NULL, NULL),
            (201, '0001-01-0000-0000-2', NULL, NULL, 'f201'),
            (202, '0001-01-0000-0000-3', 50.00, 'f202', NULL),
            (203, '0001-01-0000-0000-4', NULL, NULL, NULL)""")
        cur.execute("SET IDENTITY_INSERT dbo.Receipts OFF")
        cur.execute("SET IDENTITY_INSERT dbo.ReceiptItems ON")
        cur.execute("""INSERT INTO dbo.ReceiptItems (ItemID, ReceiptID, LineIndex, SKU, Description, Price) VALUES
            (1, 178, 1, '400292625088', 'FNCRYPTED POLY', 8.50),
            (2, 178, 2, '400295583057', 'PIK CAMO', 9.00),
            (3, 178, 3, '400292295946', 'express', 6.00),
            (10, 183, 1, '400292625088', 'ENCRYPTED POLY 00298468245', 242.34),
            (11, 183, 2, '400299999999', 'only on 183', 5.00),
            (20, 195, 1, '400295360368', 'puzz', 3.99),
            (21, 195, 2, '400295299040', 'lactic', 4.99),
            (22, 195, 3, '400297219350', 'foam', 0.99),
            (23, 195, 1, '400295360368', 'puzz', 3.99),
            (24, 195, 2, '400295299040', 'lactic', 4.99),
            (25, 195, 3, '400295299040', 'lactic', 4.99),
            (26, 195, 4, '400297219350', 'foam', 0.99),
            (27, 195, 5, '806002004330', 'Auth. No.', 128.24),
            (30, 196, 1, '400000000010', 'first', 1.00),
            (31, 196, 1, '400000000011', 'different', 2.00),
            (40, 200, 1, '400305783026', 'CARS 3PC', 1499.00),
            (41, 200, 2, '400305783027', 'socks', 5.01),
            (50, 201, 1, '400299700351', 'EH red/orange', 749.00),
            (60, 202, 1, '400285742662', 'x nike', 998.00),
            (70, 203, 1, '400285742663', 'huge', 60000.00)""")
        cur.execute("SET IDENTITY_INSERT dbo.ReceiptItems OFF")
        con.commit()
    finally:
        con.close()
    try:
        yield name
    finally:
        sqlserver.drop_test_database(name)


@pytest.fixture
def raw_dir(tmp_path):
    d = tmp_path / "raw"
    d.mkdir()
    (d / "f201.txt").write_text("400299700351 EH red/orange prin\n$7,49B\n\n400299700351 EH red/orange prin\n"
                                "$7.49B\nSubtotal\n", encoding="utf-8")
    (d / "f202.txt").write_text("400285742662 x nike 38 blue bla\n$0,99B\n\n", encoding="utf-8")
    return d


def _main(v1, raw_dir, tmp_path, *extra):
    return rc.main(["--target", v1, "--raw-dir", str(raw_dir), "--report-dir", str(tmp_path / "logs"), *extra])


def _state(name):
    con = sqlserver.connect(name)
    try:
        lines = {r[0]: (r[1], r[2], r[3], r[4]) for r in con.execute(
            "SELECT ItemID, ReceiptID, LineIndex, Price, PriceOriginal FROM dbo.ReceiptItems")}
        receipts = sorted(r[0] for r in con.execute("SELECT ReceiptID FROM dbo.Receipts"))
        fixes = [tuple(r) for r in con.execute(
            "SELECT table_name, row_id, column_name, old_value, new_value, reason FROM dbo.receipt_fixes "
            "ORDER BY fix_id")]
    finally:
        con.close()
    return lines, receipts, fixes


def test_dry_run_changes_nothing_and_lists_the_plan(v1, raw_dir, tmp_path, capsys):
    before = _state(v1)
    assert _main(v1, raw_dir, tmp_path) == 1  # receipt 196 still needs a person
    assert _state(v1) == before
    out = capsys.readouterr().out
    assert out.startswith("DRY RUN")
    report = next((tmp_path / "logs").glob("receipts-cleanup-*-dry-run.md")).read_text(encoding="utf-8")
    assert "delete_receipt | Receipts 183" in report
    assert "| 196 |" in report  # flagged


def test_apply(v1, raw_dir, tmp_path):
    assert _main(v1, raw_dir, tmp_path, "--apply") == 1  # 196 is left for a person
    lines, receipts, fixes = _state(v1)

    # duplicate receipt: 183's covered line deleted, the other moved onto 178, 183 deleted
    assert 183 not in receipts and 10 not in lines
    assert lines[11][:2] == (178, 4)
    # receipt 195: the earlier batch (20-22) is inside the later one, so it goes;
    # the card line (27) is not touched
    assert not {20, 21, 22} & set(lines) and {23, 24, 25, 26, 27} <= set(lines)
    # decimal loss: (b) subtotal agrees, (a) raw text shows 7.49
    assert lines[40][2:] == (Decimal("14.99"), Decimal("1499.00"))
    assert lines[50][2:] == (Decimal("7.49"), Decimal("749.00"))
    # no evidence (raw shows 0.99, not 9.98), and not a candidate: unchanged
    assert lines[60][2:] == (Decimal("998.00"), None) and lines[70][2:] == (Decimal("60000.00"), None)
    # 196 is flagged, not changed
    assert {30, 31} <= set(lines)

    reasons = sorted({f[5] for f in fixes})
    assert reasons == ["decimal_loss", "duplicate_lines", "duplicate_receipt"]
    assert ("ReceiptItems", 40, "Price", "1499.00", "14.99", "decimal_loss") in fixes
    assert ("ReceiptItems", 11, "LineIndex", "2", "4", "duplicate_receipt") in fixes
    assert any(f[:3] == ("Receipts", 183, "*") for f in fixes)

    # nothing left to do but 196: a second run plans no changes
    assert _main(v1, raw_dir, tmp_path, "--apply") == 1
    assert _state(v1)[2] == fixes


def test_unique_indexes_wait_for_the_cleanup(v1, raw_dir, tmp_path):
    con = sqlserver.connect(v1)
    try:
        with pytest.raises(RuntimeError, match="receipts_cleanup.py"):
            migrations.migrate_mssql(con)
        assert con.execute("SELECT MAX(version) FROM dbo.schema_version").fetchone()[0] == 1
    finally:
        con.close()

    _main(v1, raw_dir, tmp_path, "--apply")
    con = sqlserver.connect(v1)
    try:
        con.execute("DELETE FROM dbo.ReceiptItems WHERE ItemID = 31")  # the person's decision on 196
        con.commit()
        assert migrations.migrate_mssql(con)[0] == 2      # the unique indexes, then any later steps
        # the receipt bot's double submit now hits the index (it replies "already saved")
        with pytest.raises(pyodbc.IntegrityError) as e:
            con.execute("INSERT INTO dbo.Receipts (ReceiptNumber) VALUES ('0433-01-6531-6002-2')")
        assert "2601" in str(e.value)
        con.rollback()
        with pytest.raises(pyodbc.IntegrityError):
            con.execute("INSERT INTO dbo.ReceiptItems (ReceiptID, LineIndex) VALUES (178, 1)")
        con.rollback()
        con.execute("INSERT INTO dbo.Receipts (ReceiptNumber) VALUES (NULL), (NULL)")  # NULLs still allowed
        con.rollback()
    finally:
        con.close()


def test_approved_card_line_is_deleted_only_as_reviewed(v1, raw_dir, tmp_path, monkeypatch):
    # In the fixture the card line of receipt 195 is ItemID 27.
    monkeypatch.setattr(rc, "APPROVED_CARD_LINES", {
        27: {"ReceiptID": 195, "SKU": "806002004330", "Price": Decimal("128.24")},
        31: {"ReceiptID": 196, "SKU": "400000000011", "Price": Decimal("9.99")},  # price differs
    })
    _main(v1, raw_dir, tmp_path, "--apply")
    lines, _, fixes = _state(v1)
    assert 27 not in lines
    assert any(f[1] == 27 and f[5] == "card_line" for f in fixes)
    assert 31 in lines  # not as reviewed: flagged, kept
    report = sorted((tmp_path / "logs").glob("receipts-cleanup-*.md"))[-1].read_text(encoding="utf-8")
    assert "| 196 | 31 | card_line |" in report


def test_the_approved_production_card_line():
    assert rc.APPROVED_CARD_LINES == {
        789: {"ReceiptID": 195, "SKU": "806002004330", "Price": Decimal("128.24")}}


def test_apply_stops_if_a_row_changed_after_planning(v1, raw_dir):
    con = sqlserver.connect(v1)
    try:
        actions, _, _ = rc.plan(con, raw_dir)
        con.rollback()
        con.execute("UPDATE dbo.ReceiptItems SET Price = 1498.00 WHERE ItemID = 40")
        con.commit()
        before = _state(v1)
        with pytest.raises(RuntimeError, match="changed since it was planned"):
            rc.apply(con, actions, "test")
        assert _state(v1) == before
    finally:
        con.close()


def test_refuses_other_databases():
    for name in ("ReceiptsDB", "ResaleDB_Dev", "ResaleDB"):
        with pytest.raises(SystemExit):
            rc.main(["--target", name])
