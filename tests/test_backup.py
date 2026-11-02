"""backup.py: --db-only (used before each deploy) skips the inbox zip."""
import sqlite3

import pytest

import backup

# The SQLite backup (its online backup API); SQL Server: test_backup_sqlserver.py.
pytestmark = pytest.mark.sqlite_only("backs up a SQLite file")


def _setup(monkeypatch, tmp_path):
    db = tmp_path / "ross.db"
    sqlite3.connect(db).execute("create table t (x)").connection.close()
    inbox = tmp_path / "inbox" / "item1"
    inbox.mkdir(parents=True)
    (inbox / "a.jpg").write_bytes(b"jpg")
    out = tmp_path / "backups"
    monkeypatch.setattr(backup, "DB_PATH", db)
    monkeypatch.setattr(backup, "INBOX", tmp_path / "inbox")
    pieces = tmp_path / "pieces"
    pieces.mkdir()
    (pieces / "O1-01.jpg").write_bytes(b"jpg")
    monkeypatch.setattr(backup, "PIECES", pieces)
    monkeypatch.setattr(backup, "BACKUP_DIR", out)
    return out


def test_db_only_skips_inbox(monkeypatch, tmp_path):
    out = _setup(monkeypatch, tmp_path)
    backup.main(["--db-only"])
    names = [p.name for p in out.iterdir()]
    assert any(n.startswith("ross-") for n in names)
    assert not any(n.startswith("inbox-") for n in names)


def test_default_backs_up_both(monkeypatch, tmp_path):
    out = _setup(monkeypatch, tmp_path)
    backup.main([])
    names = [p.name for p in out.iterdir()]
    assert any(n.startswith("ross-") for n in names)
    assert any(n.startswith("inbox-") for n in names)


def test_photo_zip_holds_inbox_and_inventory_pieces(monkeypatch, tmp_path):
    import zipfile
    out = _setup(monkeypatch, tmp_path)
    backup.main([])
    zip_path = next(out.glob("inbox-*.zip"))
    names = set(zipfile.ZipFile(zip_path).namelist())
    assert {"inbox/item1/a.jpg", "pieces/O1-01.jpg"} <= names
