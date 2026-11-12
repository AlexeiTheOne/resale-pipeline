"""scripts/migrate_sqlite_to_mssql.py: the production migration, and the lock
that keeps everything else away from ResaleDB_Migrate_* rehearsal copies."""
import importlib.util
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import config
import db
import sqlserver
from ebay import orders

_spec = importlib.util.spec_from_file_location(
    "migrate_sqlite_to_mssql", Path(__file__).parent.parent / "scripts" / "migrate_sqlite_to_mssql.py")
mig = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mig)


# --- the rehearsal lock (no server needed) -------------------------------------

@pytest.fixture
def no_unlocks(monkeypatch):
    monkeypatch.setattr(sqlserver, "_MIGRATE_ALLOWED", set())


@pytest.mark.parametrize("name", ["ResaleDB_Migrate_20261102", "resaledb_migrate_20261102",
                                  "RESALEDB_MIGRATE_x"])
def test_rehearsal_databases_are_refused_by_default(no_unlocks, name):
    with pytest.raises(RuntimeError, match="rehearsal databases"):
        sqlserver.conn_str(name)


def test_the_bot_cannot_start_on_a_rehearsal_database(no_unlocks, monkeypatch):
    monkeypatch.setattr(config, "DB_BACKEND", "sqlserver")
    monkeypatch.setattr(config, "SQL_DATABASE", "ResaleDB_Migrate_20261102")
    with pytest.raises(RuntimeError, match="rehearsal databases"):
        db.create_tables()  # the bot's first DB call at startup


def test_unlocking_is_per_name(no_unlocks):
    sqlserver.allow_migrate_database("ResaleDB_Migrate_20261102")
    assert "Database=ResaleDB_Migrate_20261102" in sqlserver.conn_str("ResaleDB_Migrate_20261102")
    with pytest.raises(RuntimeError):
        sqlserver.conn_str("ResaleDB_Migrate_20261103")
    with pytest.raises(ValueError):
        sqlserver.allow_migrate_database("ResaleDB_Migrate_2026")


def test_target_rules(no_unlocks):
    for name in ("ResaleDB", "resaledb"):
        with pytest.raises(SystemExit, match="--production"):
            mig.check_target(name, production=False)
    mig.check_target("ResaleDB", production=True)
    for name in ("ResaleDB_Dev", "ReceiptsDB", "master", "ResaleDB_Migrate_x"):
        with pytest.raises((SystemExit, ValueError, RuntimeError)):
            mig.check_target(name, production=False)
    with pytest.raises(SystemExit):
        mig.check_target("ResaleDB_Migrate_20261102", production=True)
    mig.check_target("ResaleDB_Migrate_20261102", production=False)
    sqlserver.conn_str("ResaleDB_Migrate_20261102")  # now unlocked in this process


# --- snapshot (SQLite only) ----------------------------------------------------

def test_snapshot_copies_a_live_file_read_only(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_BACKEND", "sqlite")
    live = tmp_path / "live" / "ross.db"
    live.parent.mkdir()
    con = sqlite3.connect(live)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("CREATE TABLE t (x)")
    con.execute("INSERT INTO t VALUES (1)")
    con.commit()  # left open, like the running bot (data still in the WAL)
    try:
        before = sorted(p.name for p in live.parent.iterdir())
        dest = mig.take_snapshot(live, tmp_path / "rehearsal")
        assert sorted(p.name for p in live.parent.iterdir()) == before
        snap = sqlite3.connect(dest)
        assert snap.execute("SELECT x FROM t").fetchall() == [(1,)]
        snap.close()
    finally:
        con.close()


# --- the whole migration on a test database ------------------------------------

def _receipts_source(name):
    """A ReceiptsDB look-alike: same columns, identity gaps, a deleted last id."""
    sqlserver.create_database(name)
    con = sqlserver.connect(name, autocommit=True)
    try:
        con.execute("""CREATE TABLE dbo.Receipts (ReceiptID INT IDENTITY(1,1) PRIMARY KEY,
            ReceiptNumber VARCHAR(30), Store VARCHAR(100), StoreCity VARCHAR(100), StoreZip VARCHAR(10),
            TransactionType VARCHAR(10), ReceiptDate DATE, ReceiptTime TIME, Subtotal DECIMAL(10,2),
            Tax DECIMAL(10,2), Total DECIMAL(10,2), Confidence DECIMAL(4,2), Flags VARCHAR(200),
            RawFileID VARCHAR(50), CreatedAt DATETIME DEFAULT GETDATE(), ItemsCount INT,
            SourceFile NVARCHAR(255), Status NVARCHAR(100))""")
        con.execute("""CREATE TABLE dbo.ReceiptItems (ItemID INT IDENTITY(1,1) PRIMARY KEY,
            ReceiptID INT NOT NULL REFERENCES dbo.Receipts (ReceiptID), LineIndex INT NOT NULL,
            SKU VARCHAR(20), Description NVARCHAR(500), Price DECIMAL(10,2))""")
        con.execute("SET IDENTITY_INSERT dbo.Receipts ON")
        con.execute("INSERT INTO dbo.Receipts (ReceiptID, ReceiptNumber, ReceiptDate, Subtotal, Tax, Total, "
                    "CreatedAt, Flags) VALUES (7, '9902-10-1111-2222-1', '2026-01-20', 30.00, 2.10, 32.10, "
                    "'2026-01-21 00:08:45.997', 'contains_returns'), "
                    "(12, '0457-01-0001-0002-3', NULL, NULL, NULL, NULL, '2026-07-04 13:00:00', NULL), "
                    "(13, NULL, NULL, NULL, NULL, NULL, '2025-11-02 01:30:00', NULL)")
        con.execute("SET IDENTITY_INSERT dbo.Receipts OFF")
        con.execute("INSERT INTO dbo.ReceiptItems (ReceiptID, LineIndex, SKU, Description, Price) "
                    "VALUES (7, 0, '400271128951', N'Tote – café', 23.00), (7, 1, '400111111111', NULL, 7.00), "
                    "(12, 0, NULL, N'no price', NULL)")
        con.execute("DBCC CHECKIDENT ('dbo.Receipts', RESEED, 20)")  # 14..20 used and deleted
        con.execute("DBCC CHECKIDENT ('dbo.ReceiptItems', RESEED, 40)")
    finally:
        con.close()


def fresh_test_database(*taken) -> str:
    """A new, unused ResaleDB_Test_* name (the session one is `taken`)."""
    for s in range(1, 120):
        name = sqlserver.new_test_database_name(datetime.now(timezone.utc) + timedelta(seconds=s))
        if name not in taken and not sqlserver.database_exists(name):
            sqlserver.assert_test_database(name)
            return name
    raise RuntimeError("no free test database name")


@pytest.fixture
def receipts_db(mssql_db):
    name = fresh_test_database(mssql_db)
    _receipts_source(name)
    try:
        yield name
    finally:
        sqlserver.drop_test_database(name)


@pytest.fixture
def target(mssql_db, receipts_db):
    """An empty database for the migration (the session test database is
    already past the baseline, at the unique receipt indexes)."""
    name = fresh_test_database(mssql_db, receipts_db)
    try:
        yield name
    finally:
        sqlserver.drop_test_database(name)


@pytest.fixture
def source(tmp_path, monkeypatch):
    path = tmp_path / "ross.db"
    monkeypatch.setattr(db, "DB_PATH", str(path))
    monkeypatch.setattr(config, "DB_BACKEND", "sqlite")
    monkeypatch.setattr(config, "SQL_DATABASE", config.SQL_DATABASE)
    db.create_tables()
    a = db.create_item(["data/inbox/a/1.jpg", "data/inbox/a/2.jpg"])
    db.update_field(a, "identification", {"brand": "DKNY", "condition": "New With Tags",
                                          "condition_flags": ["NWT"]})
    db.update_field(a, "pricing", {"suggested_price": 84.99, "engine": "v2", "review_flags": ["thin"],
                                   "sold_comps": [{"price": 50.0, "title": "a"}, {"price": 55.0}],
                                   "reference": {"title": "r", "price": 60.0}})
    db.update_field(a, "listing", {"title": "DKNY Tote", "price": 69.99,
                                   "item_specifics": {"Brand": "DKNY", "Features": ["Zip", "Pocket"]}})
    recs = [{"order_id": "25-1", "sold_at": "2026-08-01T10:00:00.000Z", "units": 1, "sale": 40.0,
             "shipping": 8.0, "fees": 6.1, "label": 7.5, "ad_fee": 2.0, "refund": 0.0, "net": 32.4,
             "listing_id": "117", "fees_known": True}]
    db.update_field(a, "ebay", {"sku": "sku-a", "listing_id": "117", "quantity": 1,
                                "image_urls": ["https://i/1"], **orders.sale_db_fields(orders.totals_from_sales(recs))})
    db.update_field(a, "receipt", {"code": "400271128951", "reduced_price": 23.0})
    db.update_field(a, "listing_check", {"issues": [{"severity": "warn", "field": "title", "message": "m"}],
                                         "checked_at": db._now()})
    db.update_status(a, "sold")
    b = db.create_item(["data/inbox/b/1.jpg"])
    db.update_field(b, "ebay", {"quantity": 2})
    db.update_field(b, "stock", {"location": "B7", "added_at": db._now()})
    c = db.create_item([])
    db.update_field(c, "stock", {"location": "g3 shelf"})  # can't become a piece
    code = db.add_piece("O1-WATCHES")
    db.update_piece(code, info={"brand": "Coach"}, value=45.0)
    db.save_offer_plan([{"item_id": a, "listing_id": "117", "price": 69.99, "offer_price": 59.99}])
    db.record_llm_usage(provider="gemini", model="g-2", item_id="gone-item", input_tokens=10)
    db.save_ebay_token("ACCESS-secret", "REFRESH-secret", 1793000000.123456)
    return path


def _run(source, mssql_db, receipts_db, tmp_path, monkeypatch, *extra):
    monkeypatch.setattr(config, "DB_BACKEND", "sqlite")
    return mig.main(["--source", str(source), "--target", mssql_db, "--receipts-db", receipts_db,
                     "--report-dir", str(tmp_path / "logs"), "--snapshot-dir", str(tmp_path / "rehearsal"),
                     *extra])


def test_full_migration_passes_every_check(source, target, receipts_db, tmp_path,
                                           monkeypatch, capsys):
    assert _run(source, target, receipts_db, tmp_path, monkeypatch) == 0
    report = next((tmp_path / "logs").glob("migrate-*.md")).read_text(encoding="utf-8")
    assert "PASSED" in report and "FAIL |" not in report
    assert "secret" not in report and "secret" not in capsys.readouterr().out
    assert "can't become pieces" in report  # 'g3 shelf' is listed as a warning
    assert "llm_usage.item_id: 1 row" in report

    con = sqlserver.connect(target)
    try:
        # stopped at the baseline: the unique receipt indexes come after the cleanup
        assert con.execute("SELECT MAX(version) FROM dbo.schema_version").fetchone()[0] == 1
        assert tuple(con.execute("SELECT access_token, refresh_token FROM dbo.ebay_tokens").fetchone()) == \
            ("ACCESS-secret", "REFRESH-secret")
        assert [r[0] for r in con.execute("SELECT code FROM dbo.pieces ORDER BY code")] == \
            ["B7-01", "B7-02", "O1-WATCHES-01"]
        # same ids, the next id continues after the source's last one
        assert [r[0] for r in con.execute("SELECT ReceiptID FROM dbo.Receipts ORDER BY 1")] == [7, 12, 13]
        rows = dict(con.execute("SELECT ReceiptID, CreatedAt FROM dbo.Receipts").fetchall())
        assert rows[7] == datetime(2026, 1, 21, 5, 8, 45, 997000)    # EST +5
        assert rows[12] == datetime(2026, 7, 4, 17, 0)               # EDT +4
        assert rows[13] == datetime(2025, 11, 2, 5, 30)              # ambiguous: earlier (EDT)
        con.execute("INSERT INTO dbo.Receipts (ReceiptNumber) VALUES ('x')")
        con.execute("INSERT INTO dbo.ReceiptItems (ReceiptID, LineIndex) VALUES (7, 9)")
        assert con.execute("SELECT MAX(ReceiptID) FROM dbo.Receipts").fetchone()[0] == 21
        assert con.execute("SELECT MAX(ItemID) FROM dbo.ReceiptItems").fetchone()[0] == 41
        assert con.execute("SELECT StoreNumber FROM dbo.Receipts WHERE ReceiptID = 12").fetchone()[0] == "0457"
    finally:
        con.rollback()
        con.close()


def _prepare(name, upto):
    import migrations
    sqlserver.create_database(name)
    con = sqlserver.connect(name)
    try:
        migrations.migrate_mssql(con, upto=upto)
    finally:
        con.close()


def test_refuses_a_target_that_holds_rows(source, target, receipts_db, tmp_path, monkeypatch):
    _prepare(target, 1)
    con = sqlserver.connect(target)
    try:
        con.execute("INSERT INTO dbo.seen_orders (order_id, seen_at) VALUES ('1', SYSUTCDATETIME())")
        con.commit()
    finally:
        con.close()
    with pytest.raises(SystemExit, match="already holds rows"):
        _run(source, target, receipts_db, tmp_path, monkeypatch)


def test_refuses_a_target_past_the_baseline(source, target, receipts_db, tmp_path, monkeypatch):
    _prepare(target, None)  # at the unique receipt indexes already
    with pytest.raises(SystemExit, match="past 1"):
        _run(source, target, receipts_db, tmp_path, monkeypatch)


def test_a_lost_row_fails_loudly(source, target, receipts_db, tmp_path, monkeypatch, capsys):
    real = mig.loader._insert

    def lossy(cur, table, rows, **kw):
        if table == "item_photos":
            rows = rows[1:]
        return real(cur, table, rows, **kw)

    monkeypatch.setattr(mig.loader, "_insert", lossy)
    assert _run(source, target, receipts_db, tmp_path, monkeypatch) == 1
    assert "MIGRATION CHECKS FAILED" in capsys.readouterr().err
    report = next((tmp_path / "logs").glob("migrate-*.md")).read_text(encoding="utf-8")
    assert "FAILED" in report
    assert "List lengths | ❌ FAIL" in report and "Full round trip (every db.py read, every item) | ❌ FAIL" in report
