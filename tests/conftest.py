"""Keep the test run off the real data/ folder and the real .env.

db.py, ebay/auth.py and ebay/taxonomy.py create their tables at import, and
telegram_bot.py makes data/inbox at import. Every data path is built from
config.DATA_DIR, so pointing it at a temp dir here — before any test module
imports those — means nothing under C:/Ebay-list/data is opened or created.
Test modules that also point db.DB_PATH at their own file still do so.

SQL Server (ResaleDB) tests use the `mssql_db` / `mssql` fixtures below. Each
run gets its own ResaleDB_Test_<pid>_<time> database, created on first use,
built with migrations.py and dropped at the end. Tests that use them are
marked `sqlserver` automatically. They skip if the server can't be reached,
unless ROSS_REQUIRE_SQLSERVER=1 (then they fail).
"""
import os
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

# Before config is imported: never load the real .env (keys, JOBS_ENABLED...).
os.environ["ROSS_SKIP_DOTENV"] = "1"

import config  # noqa: E402

config.DATA_DIR = Path(tempfile.mkdtemp(prefix="ross_test_data_"))
# Same for logs/: startup tests write the heartbeat file there.
config.LOG_DIR = Path(tempfile.mkdtemp(prefix="ross_test_logs_"))
config.PRICING_V2_CACHE_DIR = str(config.DATA_DIR / "cache" / "pricing_v2")
# No SQL Server database unless a test asks for its own (see mssql_db).
config.SQL_DATABASE = None

_MSSQL_FIXTURES = {"mssql_db", "mssql"}
# Kept between tests: the migration record and the seeded settings.
_KEEP_TABLES = {"schema_version", "app_settings"}


def pytest_configure(config):  # pytest's config object; the name is required
    config.addinivalue_line(
        "markers", "sqlserver: needs the local SQL Server (a ResaleDB_Test_* database)")
    config.addinivalue_line(
        "markers", "sqlite_only(reason): skipped when the run uses DB_BACKEND=sqlserver")
    config.addinivalue_line(
        "markers", "allow_inconsistent: the test corrupts a column on purpose (no consistency check after it)")


def pytest_collection_modifyitems(items):
    for item in items:
        if _MSSQL_FIXTURES & set(getattr(item, "fixturenames", ())):
            item.add_marker(pytest.mark.sqlserver)
        # sqlite_only("why"): tests of SQLite's own storage (its file, its JSON
        # text, PRAGMAs) or of a behaviour SQL Server changes on purpose.
        mark = item.get_closest_marker("sqlite_only")
        if mark and config.DB_BACKEND == "sqlserver":
            reason = mark.args[0] if mark.args else "tests SQLite's own storage"
            item.add_marker(pytest.mark.skip(reason=f"SQLite only: {reason}"))


def _unavailable(reason: str):
    if os.getenv("ROSS_REQUIRE_SQLSERVER") == "1":
        pytest.fail(f"SQL Server required (ROSS_REQUIRE_SQLSERVER=1): {reason}")
    pytest.skip(f"SQL Server not available: {reason}")


@pytest.fixture(scope="session")
def mssql_db():
    """Name of this run's own ResaleDB_Test_* database, at the latest schema."""
    try:
        import sqlserver
    except ImportError as e:  # pyodbc missing
        _unavailable(str(e))
    if not sqlserver.server_available():
        _unavailable(f"cannot connect to {config.SQL_SERVER}")

    import migrations

    sqlserver.sweep_test_databases()
    name = sqlserver.new_test_database_name()
    sqlserver.assert_test_database(name)
    sqlserver.create_database(name)
    old = config.SQL_DATABASE
    config.SQL_DATABASE = name
    try:
        con = sqlserver.connect(name)
        try:
            migrations.migrate_mssql(con)
        finally:
            con.close()
        yield name
    finally:
        config.SQL_DATABASE = old
        sqlserver.drop_test_database(name)


def _delete_order(cur) -> list[str]:
    """Tables, children before parents, so every row can be deleted."""
    cur.execute("SELECT name FROM sys.tables WHERE is_ms_shipped = 0")
    tables = {r[0] for r in cur.fetchall()} - _KEEP_TABLES
    cur.execute("""
        SELECT OBJECT_NAME(parent_object_id), OBJECT_NAME(referenced_object_id)
        FROM sys.foreign_keys WHERE parent_object_id <> referenced_object_id""")
    parents = {t: set() for t in tables}
    for child, parent in cur.fetchall():
        if child in parents and parent in tables:
            parents[child].add(parent)
    order, done = [], set()
    while len(done) < len(tables):
        # A table can go once no remaining table still references it.
        ready = sorted(t for t in tables - done
                       if not any(t in parents[c] for c in tables - done if c != t))
        if not ready:
            raise RuntimeError("foreign key cycle")
        order += ready
        done |= set(ready)
    return order


def _empty(con) -> None:
    """Delete every row (keeping the schema record and settings)."""
    import sqlserver

    cur = con.cursor()
    # Second lock: the connection itself must be on a test database.
    cur.execute("SELECT DB_NAME()")
    sqlserver.assert_test_database(cur.fetchone()[0])
    cur.execute("UPDATE dbo.locations SET parent_code = NULL")
    for table in _delete_order(cur):
        cur.execute(f"DELETE FROM dbo.[{table}]")
    cur.execute("UPDATE dbo.app_settings SET value = '0.07' WHERE name = 'SALES_TAX_RATE'")
    con.commit()
    cur.close()


@pytest.fixture(autouse=True)
def _sqlserver_backend(request):
    """With DB_BACKEND=sqlserver the whole suite runs on SQL Server: every test
    gets this run's test database, emptied first (as every test gets a fresh
    SQLite file today)."""
    if config.DB_BACKEND != "sqlserver":
        yield
        return
    name = request.getfixturevalue("mssql_db")
    import sqlserver

    con = sqlserver.connect(name)
    try:
        _empty(con)
    finally:
        con.close()
    yield
    # Whatever the test wrote, every column must still match its JSON copy.
    if config.DB_BACKEND == "sqlserver" and not request.node.get_closest_marker("allow_inconsistent"):
        import db_mssql
        mismatches = db_mssql.consistency_report()
        if mismatches:
            pytest.fail("column/JSON copy mismatch after the test:\n" + "\n".join(mismatches[:10]))


@pytest.fixture
def mssql(mssql_db):
    """An open connection to this run's test database, emptied first."""
    import sqlserver

    con = sqlserver.connect(mssql_db)
    _empty(con)
    try:
        yield con
    finally:
        try:
            con.rollback()
        finally:
            con.close()
