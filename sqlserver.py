"""SQL Server connections and database admin for ResaleDB.

The only module that calls pyodbc.connect (tests/test_db_access.py checks).
db.py still runs on SQLite; this is step 1 of the migration (schema and test
infrastructure, see docs/schema-proposal.md).

Safety: dev (ResaleDB_Dev), production (ResaleDB) and ReceiptsDB share one
instance on the production PC. Only databases named ResaleDB_Test_* can be
dropped from here, and only those get created on the fly by tests.
"""
import os
import re
from datetime import datetime, timedelta, timezone

import pyodbc

import config

TEST_PREFIX = "ResaleDB_Test_"
# Letters, digits, underscore. Database names are put into DDL in [brackets],
# so anything else is refused rather than escaped.
_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,99}$")
_TEST_STAMP_RE = re.compile(r"^ResaleDB_Test_\d+_(\d{14})$")


def _check_name(name: str) -> str:
    if not isinstance(name, str) or not _NAME_RE.match(name):
        raise ValueError(f"not a safe database name: {name!r}")
    return name


def conn_str(database: str | None = None) -> str:
    """ODBC connection string for `database` (default: config.SQL_DATABASE)."""
    database = database or config.SQL_DATABASE
    if not database:
        raise RuntimeError("SQL_DATABASE is not set. Add it to .env "
                           "(ResaleDB_Dev on a dev copy).")
    _check_name(database)
    parts = [
        f"Driver={{{config.SQL_DRIVER}}}",
        f"Server={config.SQL_SERVER}",
        f"Database={database}",
        "Trusted_Connection=yes",
        "Encrypt=yes",
        f"TrustServerCertificate={'yes' if config.SQL_TRUST_SERVER_CERT else 'no'}",
        "APP=ross-resale-bot",
    ]
    return ";".join(parts) + ";"


def connect(database: str | None = None, *, autocommit: bool = False,
            timeout: int = 10) -> pyodbc.Connection:
    """A connection to `database` (default config.SQL_DATABASE). Close it when
    done; `with connect() as con:` commits or rolls back but does not close."""
    return pyodbc.connect(conn_str(database), autocommit=autocommit, timeout=timeout)


def server_available(timeout: int = 3) -> bool:
    """True if the instance answers (used to skip SQL Server tests)."""
    try:
        con = connect("master", autocommit=True, timeout=timeout)
    except pyodbc.Error:
        return False
    con.close()
    return True


def database_exists(name: str) -> bool:
    con = connect("master", autocommit=True)
    try:
        return con.execute("SELECT DB_ID(?)", _check_name(name)).fetchone()[0] is not None
    finally:
        con.close()


def create_database(name: str) -> bool:
    """Create `name` if missing, with READ_COMMITTED_SNAPSHOT on (readers never
    block writers, like WAL). Returns True if it was created."""
    _check_name(name)
    con = connect("master", autocommit=True)
    try:
        if con.execute("SELECT DB_ID(?)", name).fetchone()[0] is not None:
            return False
        con.execute(f"CREATE DATABASE [{name}]")
        con.execute(f"ALTER DATABASE [{name}] SET READ_COMMITTED_SNAPSHOT ON WITH ROLLBACK IMMEDIATE")
        return True
    finally:
        con.close()


def assert_test_database(name: str) -> None:
    """The safety lock: refuse any database that isn't a ResaleDB_Test_* one.
    Case-sensitive on purpose (the server itself is case-insensitive)."""
    _check_name(name)
    if not name.startswith(TEST_PREFIX) or len(name) == len(TEST_PREFIX):
        raise RuntimeError(f"refusing to touch {name!r}: only {TEST_PREFIX}* databases "
                           "may be created or dropped by tests")


def new_test_database_name(now: datetime | None = None) -> str:
    """ResaleDB_Test_<pid>_<UTC yyyymmddHHMMSS>: unique per run, so the deploy
    job's staging tests and a dev run on the same PC never share a database."""
    now = now or datetime.now(timezone.utc)
    return f"{TEST_PREFIX}{os.getpid()}_{now:%Y%m%d%H%M%S}"


def drop_test_database(name: str) -> None:
    """Drop a ResaleDB_Test_* database (anything else is refused)."""
    assert_test_database(name)
    con = connect("master", autocommit=True)
    try:
        if con.execute("SELECT DB_ID(?)", name).fetchone()[0] is None:
            return
        # Pooled connections from the test run may still hold it open.
        con.execute(f"ALTER DATABASE [{name}] SET SINGLE_USER WITH ROLLBACK IMMEDIATE")
        con.execute(f"DROP DATABASE [{name}]")
    finally:
        con.close()


DEV_DATABASE = "ResaleDB_Dev"


def assert_resettable(name: str) -> None:
    """Only the dev database and test databases may be wiped and rebuilt."""
    _check_name(name)
    if name != DEV_DATABASE:
        assert_test_database(name)


def reset_database(name: str) -> None:
    """Drop and recreate ResaleDB_Dev or a ResaleDB_Test_* database (empty,
    no schema). Anything else is refused before connecting."""
    assert_resettable(name)
    con = connect("master", autocommit=True)
    try:
        if con.execute("SELECT DB_ID(?)", name).fetchone()[0] is not None:
            con.execute(f"ALTER DATABASE [{name}] SET SINGLE_USER WITH ROLLBACK IMMEDIATE")
            con.execute(f"DROP DATABASE [{name}]")
    finally:
        con.close()
    create_database(name)


def default_backup_dir() -> str:
    """config.SQL_BACKUP_DIR, else the instance's own backup folder (which the
    SQL Server service can write, but a normal Windows user may not read)."""
    if config.SQL_BACKUP_DIR:
        return config.SQL_BACKUP_DIR
    con = connect("master", autocommit=True)
    try:
        return con.execute("SELECT CAST(SERVERPROPERTY('InstanceDefaultBackupPath') AS NVARCHAR(400))"
                           ).fetchone()[0]
    finally:
        con.close()


def backup_database(name: str, *, label: str | None = None, path: str | None = None) -> str:
    """BACKUP DATABASE `name` WITH COPY_ONLY, CHECKSUM (no COMPRESSION: Express
    doesn't have it), then RESTORE VERIFYONLY. The file is written by the SQL
    Server service, so the folder must be one it can write (doc 7.1). Returns
    the file path. `path` overrides the file ("NUL" writes nothing: tests)."""
    _check_name(name)
    if label is not None and not _NAME_RE.match(label.replace("-", "_")):
        raise ValueError(f"not a safe backup label: {label!r}")
    if path is None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        folder = default_backup_dir().rstrip("\\/")
        path = f"{folder}\\{name}-{stamp}{'-' + label if label else ''}.bak"
    con = connect("master", autocommit=True)
    try:
        cur = con.cursor()
        cur.execute(f"BACKUP DATABASE [{name}] TO DISK = ? WITH COPY_ONLY, CHECKSUM, INIT", path)
        while cur.nextset():   # let the server finish and report every message
            pass
        if path.upper() != "NUL":
            cur.execute("RESTORE VERIFYONLY FROM DISK = ? WITH CHECKSUM", path)
            while cur.nextset():
                pass
    finally:
        con.close()
    return path


def stale_test_databases(names, now: datetime | None = None,
                         older_than: timedelta = timedelta(hours=24)) -> list[str]:
    """Which of `names` are ResaleDB_Test_* databases older than `older_than`,
    judged by the timestamp in the name. Younger ones may belong to a run that
    is still going (the deploy job's, for example), so they are left alone."""
    now = now or datetime.now(timezone.utc)
    stale = []
    for name in names:
        m = _TEST_STAMP_RE.match(name or "")
        if not m:
            continue
        made = datetime.strptime(m.group(1), "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
        if now - made > older_than:
            stale.append(name)
    return stale


def sweep_test_databases(older_than: timedelta = timedelta(hours=24)) -> list[str]:
    """Drop leftover ResaleDB_Test_* databases from crashed runs."""
    con = connect("master", autocommit=True)
    try:
        names = [r[0] for r in con.execute(
            "SELECT name FROM sys.databases WHERE name LIKE 'ResaleDB[_]Test[_]%'")]
    finally:
        con.close()
    dropped = []
    for name in stale_test_databases(names, older_than=older_than):
        try:
            drop_test_database(name)
            dropped.append(name)
        except pyodbc.Error:
            pass  # in use or already gone; the next sweep tries again
    return dropped
