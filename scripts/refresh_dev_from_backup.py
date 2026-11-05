"""DEV refresh: rebuild ResaleDB_Dev from a production backup, without the eBay token.

    python scripts/refresh_dev_from_backup.py                # newest ResaleDB-*.bak in SQL_BACKUP_DIR
    python scripts/refresh_dev_from_backup.py --backup D:\\SQLBackups\\ResaleDB-20261105-070002.bak

Production (ResaleDB) is never opened. The .bak file is only read:
1. it is restored into a temporary ResaleDB_Migrate_<yyyymmdd> copy, its files
   moved to new names (never onto ResaleDB's own files);
2. ResaleDB_Dev is dropped and rebuilt at the latest schema (migrations.py);
3. every table except ebay_tokens is copied across, row for row;
4. the temporary copy is dropped, also when a step fails.

Like scripts/sqlite_to_mssql.py, the eBay token never reaches the dev
database: a dev database must not be able to act on eBay. The backup must be
at the schema version this code knows (else it refuses: pull first).
"""
import argparse
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
import migrations  # noqa: E402
import sqlserver  # noqa: E402

# Never copied into dev. ebay_tokens: the eBay user token. schema_version: the
# target has its own, from its migrations.
SKIP_TABLES = {"ebay_tokens", "schema_version"}
_BACKUP_RE = re.compile(r"^ResaleDB-\d{8}-\d{6}(-[A-Za-z0-9_-]+)?\.bak$", re.IGNORECASE)


def newest_backup(folder) -> Path:
    """The newest production backup (ResaleDB-<yyyymmdd>-<hhmmss>[-label].bak)
    in `folder`. ResaleDB_Dev-* and other databases' backups don't match."""
    files = [p for p in Path(folder).iterdir() if p.is_file() and _BACKUP_RE.match(p.name)]
    if not files:
        raise FileNotFoundError(f"no ResaleDB-*.bak in {folder}")
    return max(files, key=lambda p: p.stat().st_mtime)


def _drain(cur) -> None:
    while cur.nextset():
        pass


def _scalar(con, sql, *params):
    cur = con.cursor()
    cur.execute(sql, *params)
    value = cur.fetchone()[0]
    _drain(cur)
    cur.close()
    return value


def restore_copy(backup, name: str) -> None:
    """Restore `backup` (a ResaleDB backup) as the new database `name`, its
    files moved to <data dir>\\<name>.mdf / <name>_log.ldf. Refuses if `name`
    exists, if the backup isn't ResaleDB's, or if a target file is in use by
    any database."""
    if not re.match(r"^ResaleDB_Migrate_\d{8}$", name):
        raise ValueError(f"the temporary copy must be ResaleDB_Migrate_<yyyymmdd>: {name!r}")
    con = sqlserver.connect("master", autocommit=True)
    try:
        cur = con.cursor()
        if _scalar(con, "SELECT DB_ID(?)", name) is not None:
            raise RuntimeError(f"{name} already exists (a migration rehearsal?); not touching it")
        cur.execute("RESTORE HEADERONLY FROM DISK = ?", str(backup))
        cols = [d[0] for d in cur.description]
        header = [dict(zip(cols, r)) for r in cur.fetchall()]
        _drain(cur)
        if len(header) != 1 or header[0]["DatabaseName"] != sqlserver.PRODUCTION_DATABASE:
            raise RuntimeError(f"{backup} is not a single ResaleDB backup: "
                               f"{[h['DatabaseName'] for h in header]}")
        cur.execute("RESTORE FILELISTONLY FROM DISK = ?", str(backup))
        cols = [d[0] for d in cur.description]
        files = [dict(zip(cols, r)) for r in cur.fetchall()]
        _drain(cur)
        data_dir = _scalar(con, "SELECT CAST(SERVERPROPERTY('InstanceDefaultDataPath') AS NVARCHAR(400))")
        log_dir = _scalar(con, "SELECT CAST(SERVERPROPERTY('InstanceDefaultLogPath') AS NVARCHAR(400))")
        moves, params = [], []
        for i, f in enumerate(files):
            if f["Type"] == "D":
                target = f"{data_dir.rstrip(chr(92))}\\{name}{'' if i == 0 else f'_{i}'}.mdf"
            elif f["Type"] == "L":
                target = f"{log_dir.rstrip(chr(92))}\\{name}_log{'' if i <= 1 else f'_{i}'}.ldf"
            else:
                raise RuntimeError(f"unexpected file type {f['Type']!r} in {backup}")
            moves.append("MOVE ? TO ?")
            params += [f["LogicalName"], target]
        in_use = {r[0].casefold() for r in cur.execute("SELECT physical_name FROM sys.master_files").fetchall()}
        for target in params[1::2]:
            if target.casefold() in in_use:
                raise RuntimeError(f"refusing: {target} belongs to an existing database")
        cur.execute(f"RESTORE DATABASE [{name}] FROM DISK = ? WITH {', '.join(moves)}, CHECKSUM, RECOVERY",
                    str(backup), *params)
        _drain(cur)
    finally:
        con.close()


def drop_copy(name: str) -> None:
    """Drop the temporary ResaleDB_Migrate_<yyyymmdd> copy (nothing else)."""
    if not re.match(r"^ResaleDB_Migrate_\d{8}$", name):
        raise ValueError(f"not a temporary copy: {name!r}")
    con = sqlserver.connect("master", autocommit=True)
    try:
        if _scalar(con, "SELECT DB_ID(?)", name) is None:
            return
        con.execute(f"ALTER DATABASE [{name}] SET SINGLE_USER WITH ROLLBACK IMMEDIATE")
        con.execute(f"DROP DATABASE [{name}]")
    finally:
        con.close()


def _tables(cur, db: str) -> list[str]:
    cur.execute(f"SELECT name FROM [{db}].sys.tables WHERE is_ms_shipped = 0 ORDER BY name")
    return [r[0] for r in cur.fetchall()]


def _columns(cur, db: str, table: str) -> dict:
    """{column: (is_identity, insertable)}; computed and rowversion columns
    can't be inserted."""
    cur.execute(f"SELECT c.name, c.is_identity, c.is_computed, c.system_type_id FROM [{db}].sys.columns c "
                f"WHERE c.object_id = OBJECT_ID(?) ORDER BY c.column_id", f"[{db}].dbo.[{table}]")
    return {n: (bool(ident), not comp and st != 189) for n, ident, comp, st in cur.fetchall()}


def copy_tables(con, source_db: str, skip=SKIP_TABLES) -> dict:
    """Replace every table's rows in the database behind `con` with `source_db`'s,
    except `skip`. Constraints are off during the copy and checked after.
    Returns {table: (source rows, copied rows)}."""
    sqlserver._check_name(source_db)
    cur = con.cursor()
    cur.execute("SELECT DB_NAME()")
    target_db = cur.fetchone()[0]
    sqlserver.assert_resettable(target_db)   # only ResaleDB_Dev or a ResaleDB_Test_*
    tables = [t for t in _tables(cur, target_db) if t not in skip]
    source_tables = set(_tables(cur, source_db))
    missing = sorted(source_tables - set(tables) - set(skip))
    if missing:
        raise RuntimeError(f"tables in {source_db} that {target_db} doesn't have: {missing}")
    for t in tables:
        cur.execute(f"ALTER TABLE dbo.[{t}] NOCHECK CONSTRAINT ALL")
        cur.execute(f"DISABLE TRIGGER ALL ON dbo.[{t}]")
    counts = {}
    try:
        for t in tables:
            cur.execute(f"DELETE FROM dbo.[{t}]")
            if t not in source_tables:
                counts[t] = (0, 0)
                continue
            dst, src = _columns(cur, target_db, t), _columns(cur, source_db, t)
            cols = [c for c, (_, ok) in dst.items() if ok and c in src]
            identity = any(dst[c][0] for c in cols)
            col_sql = ", ".join(f"[{c}]" for c in cols)
            if identity:
                cur.execute(f"SET IDENTITY_INSERT dbo.[{t}] ON")
            cur.execute(f"INSERT INTO dbo.[{t}] ({col_sql}) SELECT {col_sql} FROM [{source_db}].dbo.[{t}]")
            if identity:
                cur.execute(f"SET IDENTITY_INSERT dbo.[{t}] OFF")
            cur.execute(f"SELECT COUNT_BIG(*) FROM [{source_db}].dbo.[{t}]")
            n_src = cur.fetchone()[0]
            cur.execute(f"SELECT COUNT_BIG(*) FROM dbo.[{t}]")
            counts[t] = (n_src, cur.fetchone()[0])
    finally:
        for t in tables:
            cur.execute(f"ENABLE TRIGGER ALL ON dbo.[{t}]")
            cur.execute(f"ALTER TABLE dbo.[{t}] WITH CHECK CHECK CONSTRAINT ALL")
    con.commit()
    return counts


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--backup", help="a ResaleDB-*.bak (default: the newest in SQL_BACKUP_DIR)")
    ap.add_argument("--target", default=sqlserver.DEV_DATABASE)
    args = ap.parse_args(argv)
    sqlserver.assert_resettable(args.target)
    if not config.SQL_BACKUP_DIR and not args.backup:
        raise SystemExit("SQL_BACKUP_DIR is not set; pass --backup")
    backup = Path(args.backup) if args.backup else newest_backup(config.SQL_BACKUP_DIR)
    copy_name = f"{sqlserver.MIGRATE_PREFIX}{datetime.now():%Y%m%d}"
    print(f"Backup: {backup}")
    print(f"Temporary copy: {copy_name}")

    restore_copy(backup, copy_name)
    try:
        con = sqlserver.connect("master", autocommit=True)
        try:
            version = _scalar(con, f"SELECT MAX(version) FROM [{copy_name}].dbo.schema_version")
        finally:
            con.close()
        known = max(v for v, _, _ in migrations.MSSQL_MIGRATIONS)
        if version != known:
            raise SystemExit(f"backup is at schema version {version}, this code at {known}: "
                             "pull the matching code first")
        sqlserver.reset_database(args.target)
        con = sqlserver.connect(args.target)
        try:
            migrations.migrate_mssql(con)
            counts = copy_tables(con, copy_name)
            tokens = _scalar(con, "SELECT COUNT(*) FROM dbo.ebay_tokens")
        finally:
            con.close()
    finally:
        drop_copy(copy_name)
    print(f"Copied into {args.target} (schema {version}); {copy_name} dropped.")
    bad = 0
    for t, (n_src, n_dst) in sorted(counts.items()):
        flag = "" if n_src == n_dst else "   <-- MISMATCH"
        bad += bool(flag)
        print(f"  {t:24} {n_dst}{flag}")
    print(f"  {'ebay_tokens':24} {tokens} (never copied)")
    if tokens:
        print("ERROR: the dev database has an eBay token")
        return 1
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
