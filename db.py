import sqlite3
import uuid
import json
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = "data/ross.db"

VALID_STATUSES = [
    "captured", "identified", "priced", "drafted",
    "review", "approved", "ebay_draft", "published", "rejected"
]

VALID_FIELDS = [
    "identification", "pricing", "listing", "ebay", "photos", "processed",
    "price_source_url", "receipt",
]

# Columns added after the initial schema shipped. Each is created with an
# ALTER TABLE on startup if the existing table doesn't already have it.
_MIGRATIONS = {
    "price_source_url": "TEXT",  # link to the comp the suggested price is anchored to
    "receipt": "TEXT",           # OCR'd Ross receipt: cost, original price, 12-digit code
}


def _conn():
    Path("data").mkdir(exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    # WAL lets readers and a writer coexist; busy_timeout makes a contended write
    # wait for the lock instead of raising "database is locked" immediately. Both
    # matter because concurrent_updates + to_thread workers hit this file from
    # several threads at once. (WAL is a persistent DB property; busy_timeout is
    # per-connection, so it's set on every connect.)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=5000")
    return con


def _now():
    return datetime.now(timezone.utc).isoformat()


def _row_to_dict(row, cursor):
    keys = [d[0] for d in cursor.description]
    d = dict(zip(keys, row))
    for field in ("photos", "processed", "identification", "pricing", "listing",
                  "ebay", "price_source_url", "receipt"):
        if d.get(field) is not None:
            d[field] = json.loads(d[field])
    return d


def create_tables() -> None:
    with _conn() as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS items (
                item_id     TEXT PRIMARY KEY,
                status      TEXT NOT NULL DEFAULT 'captured',
                photos      TEXT,
                processed   TEXT,
                identification TEXT,
                pricing     TEXT,
                listing     TEXT,
                ebay        TEXT,
                created_at  TEXT NOT NULL,
                updated_at  TEXT NOT NULL
            )
        """)
        existing = {row[1] for row in con.execute("PRAGMA table_info(items)")}
        for column, coltype in _MIGRATIONS.items():
            if column not in existing:
                con.execute(f"ALTER TABLE items ADD COLUMN {column} {coltype}")


def create_item(photos: list[str]) -> str:
    item_id = str(uuid.uuid4())
    now = _now()
    with _conn() as con:
        con.execute(
            "INSERT INTO items (item_id, status, photos, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            (item_id, "captured", json.dumps(photos), now, now)
        )
    return item_id


def get_item(item_id: str) -> dict | None:
    with _conn() as con:
        cur = con.execute("SELECT * FROM items WHERE item_id = ?", (item_id,))
        row = cur.fetchone()
        if row is None:
            return None
        return _row_to_dict(row, cur)


def update_status(item_id: str, status: str) -> None:
    if status not in VALID_STATUSES:
        raise ValueError(f"Invalid status '{status}'. Must be one of: {VALID_STATUSES}")
    with _conn() as con:
        con.execute(
            "UPDATE items SET status = ?, updated_at = ? WHERE item_id = ?",
            (status, _now(), item_id)
        )


def update_field(item_id: str, field: str, data: dict) -> None:
    if field not in VALID_FIELDS:
        raise ValueError(f"Invalid field '{field}'. Must be one of: {VALID_FIELDS}")
    with _conn() as con:
        con.execute(
            f"UPDATE items SET {field} = ?, updated_at = ? WHERE item_id = ?",
            (json.dumps(data), _now(), item_id)
        )


def list_items(status: str | None = None) -> list[dict]:
    with _conn() as con:
        if status is not None:
            cur = con.execute("SELECT * FROM items WHERE status = ?", (status,))
        else:
            cur = con.execute("SELECT * FROM items")
        rows = cur.fetchall()
        return [_row_to_dict(row, cur) for row in rows]


def delete_item(item_id: str) -> None:
    with _conn() as con:
        con.execute("DELETE FROM items WHERE item_id = ?", (item_id,))


create_tables()
