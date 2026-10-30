"""backup.py: --db-only (used before each deploy) skips the inbox zip."""
import sqlite3

import backup


def _setup(monkeypatch, tmp_path):
    db = tmp_path / "ross.db"
    sqlite3.connect(db).execute("create table t (x)").connection.close()
    inbox = tmp_path / "inbox" / "item1"
    inbox.mkdir(parents=True)
    (inbox / "a.jpg").write_bytes(b"jpg")
    out = tmp_path / "backups"
    monkeypatch.setattr(backup, "DB_PATH", db)
    monkeypatch.setattr(backup, "INBOX", tmp_path / "inbox")
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
