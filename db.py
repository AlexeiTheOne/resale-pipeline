import sqlite3
import uuid
import json
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = "data/ross.db"

VALID_STATUSES = [
    "captured", "identified", "priced", "drafted",
    "review", "approved", "ebay_draft", "published", "sold", "rejected"
]

VALID_FIELDS = [
    "identification", "pricing", "listing", "ebay", "photos", "processed",
    "price_source_url", "receipt", "photo_layout",
]

# Columns added after the initial schema shipped. Each is created with an
# ALTER TABLE on startup if the existing table doesn't already have it.
_MIGRATIONS = {
    "price_source_url": "TEXT",  # link to the comp the suggested price is anchored to
    "receipt": "TEXT",           # OCR'd Ross receipt: cost, original price, 12-digit code
    # {"manual": true} once /cover or /arrange has set the photo order by hand.
    # Without it the listing builder would keep applying its own reorder (tag
    # photo to the end) and quietly undo the arrangement on the next rebuild.
    "photo_layout": "TEXT",
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
                  "ebay", "price_source_url", "receipt", "photo_layout"):
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

        # One row per item per ISO week (Monday date, e.g. "2026-07-13") — the
        # weekly repricing check's snapshot of eBay traffic/watch signals plus
        # the diagnosis it produced. PRIMARY KEY on (item_id, week) makes
        # re-running the check within the same week an upsert, not a duplicate.
        con.execute("""
            CREATE TABLE IF NOT EXISTS listing_stats (
                item_id         TEXT NOT NULL,
                week            TEXT NOT NULL,
                impressions     INTEGER,
                views           INTEGER,
                watchers        INTEGER,
                price           REAL,
                verdict         TEXT,
                suggested_price REAL,
                created_at      TEXT NOT NULL,
                PRIMARY KEY (item_id, week)
            )
        """)

        # When each scheduled background job last COMPLETED, so a job can tell
        # it missed its slot and catch up. Without this the bot's timers were
        # pure in-process sleeps: if the process wasn't running at the slot
        # (laptop asleep, Windows Update reboot), that run was skipped silently
        # and the next one was a full period away. One row per job name.
        con.execute("""
            CREATE TABLE IF NOT EXISTS job_runs (
                job         TEXT PRIMARY KEY,
                last_run_at TEXT NOT NULL,
                status      TEXT,
                detail      TEXT
            )
        """)


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


def record_listing_stats(item_id: str, week: str, *, impressions=None, views=None,
                          watchers=None, price=None, verdict=None, suggested_price=None) -> None:
    """Upsert this week's traffic/watch snapshot for an item (see listing_stats
    in create_tables). Re-running the weekly check the same week overwrites the
    existing row instead of adding a duplicate."""
    with _conn() as con:
        con.execute(
            "INSERT INTO listing_stats (item_id, week, impressions, views, watchers, "
            "price, verdict, suggested_price, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(item_id, week) DO UPDATE SET "
            "impressions=excluded.impressions, views=excluded.views, watchers=excluded.watchers, "
            "price=excluded.price, verdict=excluded.verdict, "
            "suggested_price=excluded.suggested_price, created_at=excluded.created_at",
            (item_id, week, impressions, views, watchers, price, verdict, suggested_price, _now()),
        )


def stats_history(item_id: str, limit: int = 8) -> list[dict]:
    """Most recent weekly snapshots for an item, newest week first."""
    with _conn() as con:
        cur = con.execute(
            "SELECT * FROM listing_stats WHERE item_id = ? ORDER BY week DESC LIMIT ?",
            (item_id, limit),
        )
        keys = [d[0] for d in cur.description]
        return [dict(zip(keys, row)) for row in cur.fetchall()]


def latest_stats(item_id: str) -> dict | None:
    history = stats_history(item_id, limit=1)
    return history[0] if history else None



def record_job_run(job: str, status: str = "ok", detail: str | None = None) -> None:
    """Stamp a scheduled job as having just completed. Called after the work,
    not before, so a crash mid-run leaves the old timestamp and the job retries
    on the next start rather than believing it already ran.

    A status other than "ok" (e.g. "partial": the weekly price check ran but
    some traffic batches failed) records the attempt without counting it as
    completed — last_job_run ignores it, so the slot is still caught up."""
    with _conn() as con:
        con.execute(
            "INSERT INTO job_runs (job, last_run_at, status, detail) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(job) DO UPDATE SET last_run_at=excluded.last_run_at, "
            "status=excluded.status, detail=excluded.detail",
            (job, _now(), status, detail),
        )


def job_run_status(job: str) -> str | None:
    """The status the last attempt at `job` recorded ("ok", "partial", ...), or
    None if it never recorded one."""
    with _conn() as con:
        row = con.execute(
            "SELECT status FROM job_runs WHERE job = ?", (job,)
        ).fetchone()
    return row[0] if row else None


def last_job_run(job: str) -> datetime | None:
    """When `job` last completed, as an aware UTC datetime, or None if never.
    An attempt recorded with a non-ok status didn't complete, so it reads as
    None too (NULL status is a legacy ok row)."""
    with _conn() as con:
        row = con.execute(
            "SELECT last_run_at FROM job_runs WHERE job = ? "
            "AND (status IS NULL OR status = 'ok')", (job,)
        ).fetchone()
    if not row or not row[0]:
        return None
    try:
        dt = datetime.fromisoformat(row[0])
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)

create_tables()
