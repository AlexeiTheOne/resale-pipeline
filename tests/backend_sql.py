"""Raw SQL in tests, on whichever backend this run uses (DB_BACKEND).

A few tests plant rows no db.py function writes (an old timestamp, a legacy
NULL status) or count rows directly. They give one statement per backend;
results come back as SQLite would return them (ISO strings, floats).
"""
from datetime import date, datetime, timezone
from decimal import Decimal

import config
import db


def on_sqlserver() -> bool:
    return config.DB_BACKEND == "sqlserver"


def _plain(v):
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, datetime):
        return v.replace(tzinfo=timezone.utc).isoformat()
    if isinstance(v, date):
        return v.isoformat()
    return v


def rows(sqlite_sql: str, mssql_sql: str | None = None, args=()) -> list[dict]:
    if on_sqlserver():
        import sqlserver
        con = sqlserver.connect()
        try:
            cur = con.cursor()
            cur.execute(mssql_sql or sqlite_sql, *args)
            keys = [d[0] for d in cur.description]
            out = [{k: _plain(v) for k, v in zip(keys, r)} for r in cur.fetchall()]
            con.commit()
            return out
        finally:
            con.close()
    with db._conn() as con:
        cur = con.execute(sqlite_sql, args)
        keys = [d[0] for d in cur.description]
        return [dict(zip(keys, r)) for r in cur.fetchall()]


def execute(sqlite_sql: str, mssql_sql: str | None = None, args=()) -> None:
    if on_sqlserver():
        import sqlserver
        con = sqlserver.connect()
        try:
            con.cursor().execute(mssql_sql or sqlite_sql, *args)
            con.commit()
        finally:
            con.close()
        return
    with db._conn() as con:
        con.execute(sqlite_sql, args)
