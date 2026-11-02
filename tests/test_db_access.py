"""All DB access goes through db.py (cleanup-plan Phase 3)."""
import sqlite3
from pathlib import Path

import pytest

import db

ROOT = Path(__file__).parent.parent

# Files allowed to call sqlite3.connect themselves, and why.
ALLOWED = {
    "db.py": "the data layer itself",
    "backup.py": "must work even when the app code is broken",
    "scripts/backfill_sales.py": "writes a backup copy to a new file",
    "scripts/sqlite_to_mssql.py": "reads the raw stored JSON of a source file, read-only, for migration",
}


@pytest.mark.sqlite_only("SQLite's own read-only error; both backends: test_db_moved.py")
def test_readonly_connection_refuses_writes():
    con = db.connect_readonly()
    try:
        assert con.execute("SELECT COUNT(*) FROM items").fetchone()[0] >= 0
        with pytest.raises(sqlite3.OperationalError):
            con.execute("DELETE FROM items")
    finally:
        con.close()


def test_only_sqlserver_py_opens_sql_server_connections():
    offenders = []
    for path in ROOT.rglob("*.py"):
        rel = path.relative_to(ROOT).as_posix()
        if rel.startswith(("tests/", "venv/", ".venv/", "web/", ".claude/")) or rel == "sqlserver.py":
            continue
        if "pyodbc.connect(" in path.read_text(encoding="utf-8", errors="ignore"):
            offenders.append(rel)
    assert offenders == [], f"use sqlserver.connect() instead: {offenders}"


def test_no_new_direct_sqlite_connects():
    offenders = []
    for path in ROOT.rglob("*.py"):
        rel = path.relative_to(ROOT).as_posix()
        if rel.startswith(("tests/", "venv/", ".venv/", "web/", ".claude/")) or rel in ALLOWED:
            continue
        if "sqlite3.connect(" in path.read_text(encoding="utf-8", errors="ignore"):
            offenders.append(rel)
    assert offenders == [], f"use db.connect() / db.connect_readonly() instead: {offenders}"
