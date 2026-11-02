"""backup.py with DB_BACKEND=sqlserver: BACKUP DATABASE, then a copy into
BACKUP_DIR. sqlserver.backup_database is faked (its real run is in
test_db_moved.py), so no server or SQLite file is needed."""
import backup
import config
import sqlserver


def _fake_backup(tmp_path, monkeypatch, *, readable=True):
    server_dir = tmp_path / "server"
    server_dir.mkdir()
    made = []

    def fake(name, **kwargs):
        bak = server_dir / f"{name}-20261102-010203.bak"
        bak.write_bytes(b"BAK")
        made.append(name)
        return str(bak)

    monkeypatch.setattr(sqlserver, "backup_database", fake)
    monkeypatch.setattr(config, "DB_BACKEND", "sqlserver")
    monkeypatch.setattr(config, "SQL_DATABASE", "ResaleDB_Test_1_20261102010203")
    if not readable:
        def no_copy(*a, **k):
            raise PermissionError("Access is denied")
        monkeypatch.setattr(backup.shutil, "copy2", no_copy)
    out = tmp_path / "backups"
    monkeypatch.setattr(backup, "BACKUP_DIR", out)
    monkeypatch.setattr(backup, "INBOX", tmp_path / "no-inbox")
    monkeypatch.setattr(backup, "PIECES", tmp_path / "no-pieces")
    return out, made


def test_sqlserver_backup_is_copied_and_pruned_like_sqlite(tmp_path, monkeypatch):
    out, made = _fake_backup(tmp_path, monkeypatch)
    monkeypatch.setattr(backup, "KEEP", 2)
    for _ in range(3):
        backup.main(["--db-only"])
    names = sorted(p.name for p in out.iterdir())
    assert made == ["ResaleDB_Test_1_20261102010203"] * 3
    assert len(names) <= 2 and all(n.startswith("ross-") and n.endswith(".bak") for n in names)
    assert (out / names[0]).read_bytes() == b"BAK"


def test_a_copy_that_fails_warns_but_does_not_fail(tmp_path, monkeypatch, capsys):
    out, made = _fake_backup(tmp_path, monkeypatch, readable=False)
    backup.main(["--db-only"])
    said = capsys.readouterr().out
    assert "[warn]" in said and "SQL_BACKUP_DIR" in said
    assert "[ok] DB backed up" in said
    assert not [p for p in out.iterdir() if p.name.startswith(backup.PARTIAL)]
