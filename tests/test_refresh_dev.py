"""scripts/refresh_dev_from_backup.py: production backup -> ResaleDB_Dev, no eBay token."""
import os
import time

import pytest

import config
from scripts import refresh_dev_from_backup as refresh


def test_newest_backup_takes_production_backups_only(tmp_path):
    for name in ("ResaleDB-20261104-070002.bak", "ResaleDB-20261105-070002.bak",
                 "ResaleDB_Dev-20261106-010101.bak", "ReceiptsDB-20261106-010101.bak", "notes.txt"):
        (tmp_path / name).write_text("x")
    t = time.time()
    for i, name in enumerate(("ResaleDB-20261104-070002.bak", "ResaleDB-20261105-070002.bak",
                              "ResaleDB_Dev-20261106-010101.bak", "ReceiptsDB-20261106-010101.bak")):
        os.utime(tmp_path / name, (t + i, t + i))
    assert refresh.newest_backup(tmp_path).name == "ResaleDB-20261105-070002.bak"


def test_newest_backup_none(tmp_path):
    with pytest.raises(FileNotFoundError):
        refresh.newest_backup(tmp_path)


@pytest.mark.parametrize("name", ["ResaleDB", "ResaleDB_Dev", "ResaleDB_Migrate_2026", "ReceiptsDB"])
def test_temporary_copy_names_only(name):
    with pytest.raises(ValueError):
        refresh.restore_copy("x.bak", name)
    with pytest.raises(ValueError):
        refresh.drop_copy(name)


def test_main_refuses_production_target():
    with pytest.raises(RuntimeError):
        refresh.main(["--target", "ResaleDB", "--backup", "x.bak"])


def test_copy_tables_copies_rows_but_never_the_token(mssql_db):
    import db
    import migrations
    import sqlserver

    old = config.DB_BACKEND
    config.DB_BACKEND = "sqlserver"
    try:
        item_id = db.create_item(["a.jpg"])
        db.save_ebay_token("ACCESS-SECRET", "REFRESH-SECRET", time.time() + 3600)
    finally:
        config.DB_BACKEND = old

    target = f"{mssql_db}_c"
    sqlserver.assert_test_database(target)
    sqlserver.reset_database(target)
    try:
        con = sqlserver.connect(target)
        try:
            migrations.migrate_mssql(con)
            counts = refresh.copy_tables(con, mssql_db)
            cur = con.cursor()
            assert cur.execute("SELECT COUNT(*) FROM dbo.ebay_tokens").fetchone()[0] == 0
            assert cur.execute("SELECT item_id FROM dbo.items").fetchall()[0][0] == item_id
            assert cur.execute("SELECT COUNT(*) FROM dbo.item_photos").fetchone()[0] == 1
        finally:
            con.close()
        assert "ebay_tokens" not in counts and "schema_version" not in counts
        assert counts["items"] == (1, 1)
        assert all(a == b for a, b in counts.values())
    finally:
        sqlserver.drop_test_database(target)
