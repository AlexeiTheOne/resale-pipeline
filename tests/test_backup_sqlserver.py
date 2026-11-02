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


def test_server_folder_keeps_the_newest_per_database(tmp_path, monkeypatch):
    import os
    folder = tmp_path / "SQLBackups"
    folder.mkdir()
    names = ([f"ResaleDB-20261101-0000{n:02d}.bak" for n in range(5)]
             + ["ResaleDB-20261101-000099-pre-backfill-sales.bak"]
             + [f"ResaleDB_Dev-20261101-0000{n:02d}.bak" for n in range(3)]
             + ["ResaleDB-notes.txt", "Other-20261101-000000.bak"])
    for i, n in enumerate(names):
        (folder / n).write_bytes(b"x")
        os.utime(folder / n, (1_000_000 + i, 1_000_000 + i))      # later in the list = newer
    deleted = sqlserver.prune_backups("ResaleDB", 3, str(folder))
    left = sorted(p.name for p in folder.iterdir())
    assert sorted(os.path.basename(d) for d in deleted) == [
        "ResaleDB-20261101-000000.bak", "ResaleDB-20261101-000001.bak", "ResaleDB-20261101-000002.bak"]
    assert [n for n in left if n.startswith("ResaleDB-") and n.endswith(".bak")] == [
        "ResaleDB-20261101-000003.bak", "ResaleDB-20261101-000004.bak",
        "ResaleDB-20261101-000099-pre-backfill-sales.bak"]
    assert sum(n.startswith("ResaleDB_Dev-") for n in left) == 3        # other database untouched
    assert "ResaleDB-notes.txt" in left and "Other-20261101-000000.bak" in left
    monkeypatch.setattr(config, "SQL_BACKUP_DIR", None)
    assert sqlserver.prune_backups("ResaleDB", 3) == []                 # no folder set: nothing


def test_nightly_run_fails_when_the_offsite_copy_fails(tmp_path, monkeypatch):
    # D:\SQLBackups is on the same disk as C:; the copy into BACKUP_DIR
    # (OneDrive) is the only backup that leaves the machine.
    import pytest
    _fake_backup(tmp_path, monkeypatch, readable=False)
    with pytest.raises(RuntimeError, match="could not be copied"):
        backup.main(["--db-only", "--require-copy"])


def test_offsite_and_server_folders_keep_their_own_counts(tmp_path, monkeypatch):
    out, _ = _fake_backup(tmp_path, monkeypatch)
    pruned = []
    monkeypatch.setattr(sqlserver, "prune_backups", lambda name, keep: pruned.append(keep) or [])
    monkeypatch.setattr(backup, "KEEP", 30)       # BACKUP_KEEP: OneDrive copies
    monkeypatch.setattr(backup, "SQL_KEEP", 14)   # SQL_BACKUP_KEEP: D:\SQLBackups
    backup.main(["--db-only", "--require-copy"])
    assert pruned == [14]
    assert len(list(out.glob("ross-*.bak"))) == 1


def test_a_copy_that_fails_warns_but_does_not_fail(tmp_path, monkeypatch, capsys):
    out, made = _fake_backup(tmp_path, monkeypatch, readable=False)
    backup.main(["--db-only"])
    said = capsys.readouterr().out
    assert "[warn]" in said and "SQL_BACKUP_DIR" in said
    assert "[ok] DB backed up" in said
    assert not [p for p in out.iterdir() if p.name.startswith(backup.PARTIAL)]
