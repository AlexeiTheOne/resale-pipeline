import sqlite3
import uuid
import json
from datetime import datetime, timezone
from pathlib import Path

from config import DATA_DIR, resolve_path, stored_path

DB_PATH = str(DATA_DIR / "ross.db")

VALID_STATUSES = [
    "captured", "identified", "priced", "drafted",
    "review", "approved", "ebay_draft", "published", "sold", "rejected",
    "stocked",  # quick-added from /find: identified + priced, not listed yet
]

VALID_FIELDS = [
    "identification", "pricing", "listing", "ebay", "photos", "processed",
    "price_source_url", "receipt", "photo_layout", "listing_check", "stock",
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
    # pipeline/listing_check.py's verdict on the current draft: {"issues": [...],
    # "model", "cost_usd", "unchecked", "checked_at"}. Advisory, shown on review.
    "listing_check": "TEXT",
    # Physical stock: {"location": "G3", "quick_price": {...}, "added_at"}.
    "stock": "TEXT",
}

# listing_stats columns added after it shipped (same pattern as _MIGRATIONS).
_LISTING_STATS_MIGRATIONS = {
    "total_impressions": "INTEGER",   # TOTAL_IMPRESSION_TOTAL, promoted included
    "search_views": "INTEGER",        # LISTING_VIEWS_SOURCE_SEARCH_RESULTS_PAGE
}


def _conn():
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
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


# Item fields holding a local file path under a key (besides "photos", which is
# a list of them). Stored relative to BASE_DIR (data/inbox/<id>/...) and handed
# out absolute, so a read never depends on the current working directory.
_PATH_KEYS = {"receipt": "photo", "stock": "inventory_photo"}


def _map_paths(field: str, value, fn):
    if field == "photos" and isinstance(value, list):
        return [fn(p) if isinstance(p, str) and p else p for p in value]
    key = _PATH_KEYS.get(field)
    if key and isinstance(value, dict) and isinstance(value.get(key), str) and value[key]:
        return {**value, key: fn(value[key])}
    return value


def _resolved(p) -> str:
    return str(resolve_path(p))


def _row_to_dict(row, cursor):
    keys = [d[0] for d in cursor.description]
    d = dict(zip(keys, row))
    for field in ("photos", "processed", "identification", "pricing", "listing",
                  "ebay", "price_source_url", "receipt", "photo_layout",
                  "listing_check", "stock"):
        if d.get(field) is not None:
            d[field] = _map_paths(field, json.loads(d[field]), _resolved)
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
        # Added 2026-10-29. `impressions` keeps its historical meaning — eBay's
        # LISTING_IMPRESSION_TOTAL, organic search + store only — so older rows
        # stay comparable. total_impressions is TOTAL_IMPRESSION_TOTAL (promoted
        # placements included, ~11-14x organic on this account) and is what the
        # verdicts are judged on now; NULL on rows written before it existed.
        stats_cols = {row[1] for row in con.execute("PRAGMA table_info(listing_stats)")}
        for column, coltype in _LISTING_STATS_MIGRATIONS.items():
            if column not in stats_cols:
                con.execute(f"ALTER TABLE listing_stats ADD COLUMN {column} {coltype}")

        # Offers to watchers that were actually sent (the weekly digest's
        # button and /offers apply), so a listing isn't re-offered every week.
        con.execute("""
            CREATE TABLE IF NOT EXISTS offer_sends (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                item_id      TEXT,
                listing_id   TEXT NOT NULL,
                list_price   REAL,
                offer_price  REAL NOT NULL,
                source       TEXT,
                sent_at      TEXT NOT NULL
            )
        """)
        # A weekly digest's watcher-offer plan, kept until its one button is
        # tapped: Telegram's 64-byte callback can't carry a dozen prices, and
        # the stored rows are the record of what the tap authorised. used_at is
        # set once, so a second tap (or a second chat) can't send twice.
        con.execute("""
            CREATE TABLE IF NOT EXISTS offer_plans (
                plan_id      TEXT PRIMARY KEY,
                rows         TEXT NOT NULL,
                created_at   TEXT NOT NULL,
                used_at      TEXT
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

        # Feedback attempts that eBay rejected, keyed by order line. The daily
        # feedback job retries them quietly but reports each one only once, so a
        # sale eBay won't take feedback on doesn't re-post the same error daily.
        con.execute("""
            CREATE TABLE IF NOT EXISTS feedback_failures (
                order_line   TEXT PRIMARY KEY,
                first_failed TEXT NOT NULL,
                last_error   TEXT
            )
        """)

        # Pairs the owner confirmed are different physical items even though
        # they share a UPC/model (e.g. two Ed Hardy bags whose print sits in a
        # different place). The duplicate warning skips them. Stored a<b.
        con.execute("""
            CREATE TABLE IF NOT EXISTS not_duplicates (
                a TEXT NOT NULL,
                b TEXT NOT NULL,
                PRIMARY KEY (a, b)
            )
        """)

        # One row per physical piece at home (inventory mode). The code
        # (G3-07) never changes, even when the piece moves; `box` is where it
        # is now. item_id links it to the product/listing row in `items`.
        # status: home | sold | lot | donated ("listed" is derived from the
        # linked item being published).
        con.execute("""
            CREATE TABLE IF NOT EXISTS pieces (
                code        TEXT PRIMARY KEY,
                box         TEXT NOT NULL,
                item_id     TEXT,
                status      TEXT NOT NULL DEFAULT 'home',
                photo       TEXT,
                info        TEXT,
                value       REAL,
                cost        REAL,
                created_at  TEXT NOT NULL,
                updated_at  TEXT NOT NULL,
                sold_at     TEXT,
                order_id    TEXT
            )
        """)
        con.execute("CREATE INDEX IF NOT EXISTS pieces_item ON pieces(item_id)")

        # Orders the sold-alert poll has already announced.
        con.execute("""
            CREATE TABLE IF NOT EXISTS seen_orders (
                order_id TEXT PRIMARY KEY,
                seen_at  TEXT NOT NULL
            )
        """)

        # One row per paid model call (Claude and Gemini), so spend is a query
        # rather than a guess. The monthly Anthropic budget guard reads this; a
        # refusal fallback writes one row per model that ran. cost_usd is NULL
        # when the model has no price on file.
        con.execute("""
            CREATE TABLE IF NOT EXISTS llm_usage (
                id                 INTEGER PRIMARY KEY AUTOINCREMENT,
                ts                 TEXT NOT NULL,
                provider           TEXT NOT NULL,
                model              TEXT NOT NULL,
                stage              TEXT,
                item_id            TEXT,
                input_tokens       INTEGER NOT NULL DEFAULT 0,
                output_tokens      INTEGER NOT NULL DEFAULT 0,
                cache_read_tokens  INTEGER NOT NULL DEFAULT 0,
                cache_write_tokens INTEGER NOT NULL DEFAULT 0,
                web_searches       INTEGER NOT NULL DEFAULT 0,
                cost_usd           REAL
            )
        """)
        con.execute("CREATE INDEX IF NOT EXISTS idx_llm_usage_ts ON llm_usage (ts)")


def create_item(photos: list[str]) -> str:
    item_id = str(uuid.uuid4())
    now = _now()
    with _conn() as con:
        con.execute(
            "INSERT INTO items (item_id, status, photos, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            (item_id, "captured", json.dumps(_map_paths("photos", photos, stored_path)), now, now)
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
            (json.dumps(_map_paths(field, data, stored_path)), _now(), item_id)
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
                          watchers=None, price=None, verdict=None, suggested_price=None,
                          total_impressions=None, search_views=None) -> None:
    """Upsert this week's traffic/watch snapshot for an item (see listing_stats
    in create_tables). Re-running the weekly check the same week overwrites the
    existing row instead of adding a duplicate.

    `impressions` is ORGANIC (search + store), its historical meaning;
    `total_impressions` includes promoted placements."""
    with _conn() as con:
        con.execute(
            "INSERT INTO listing_stats (item_id, week, impressions, views, watchers, "
            "price, verdict, suggested_price, created_at, total_impressions, search_views) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(item_id, week) DO UPDATE SET "
            "impressions=excluded.impressions, views=excluded.views, watchers=excluded.watchers, "
            "price=excluded.price, verdict=excluded.verdict, "
            "suggested_price=excluded.suggested_price, created_at=excluded.created_at, "
            "total_impressions=excluded.total_impressions, search_views=excluded.search_views",
            (item_id, week, impressions, views, watchers, price, verdict, suggested_price, _now(),
             total_impressions, search_views),
        )


def record_offer_sent(listing_id: str, offer_price: float, *, item_id: str | None = None,
                      list_price: float | None = None, source: str | None = None) -> None:
    """Note that an offer to watchers went out (see offer_sends)."""
    with _conn() as con:
        con.execute(
            "INSERT INTO offer_sends (item_id, listing_id, list_price, offer_price, source, "
            "sent_at) VALUES (?, ?, ?, ?, ?, ?)",
            (item_id, str(listing_id), list_price, offer_price, source, _now()))


def offers_sent_since(since_iso: str) -> set[str]:
    """Listing ids that had an offer to watchers sent at or after `since_iso`."""
    with _conn() as con:
        return {row[0] for row in con.execute(
            "SELECT DISTINCT listing_id FROM offer_sends WHERE sent_at >= ?", (since_iso,))}


def save_offer_plan(rows: list[dict]) -> str:
    plan_id = str(uuid.uuid4())
    with _conn() as con:
        con.execute("INSERT INTO offer_plans (plan_id, rows, created_at) VALUES (?, ?, ?)",
                    (plan_id, json.dumps(rows), _now()))
    return plan_id


def get_offer_plan(plan_id: str) -> dict | None:
    with _conn() as con:
        row = con.execute("SELECT rows, created_at, used_at FROM offer_plans WHERE plan_id = ?",
                          (plan_id,)).fetchone()
    if row is None:
        return None
    return {"plan_id": plan_id, "rows": json.loads(row[0]), "created_at": row[1],
            "used_at": row[2]}


def claim_offer_plan(plan_id: str) -> bool:
    """Mark a plan used. True only for the one caller that got there first."""
    with _conn() as con:
        cur = con.execute("UPDATE offer_plans SET used_at = ? WHERE plan_id = ? "
                          "AND used_at IS NULL", (_now(), plan_id))
        return cur.rowcount == 1


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


def known_feedback_failures() -> set[str]:
    """Order lines whose feedback failure has already been reported."""
    with _conn() as con:
        return {r[0] for r in con.execute("SELECT order_line FROM feedback_failures")}


def record_feedback_failure(order_line: str, error: str) -> None:
    with _conn() as con:
        con.execute(
            "INSERT INTO feedback_failures (order_line, first_failed, last_error) "
            "VALUES (?, ?, ?) ON CONFLICT(order_line) DO UPDATE SET last_error=excluded.last_error",
            (order_line, _now(), error))


def clear_feedback_failure(order_line: str) -> None:
    with _conn() as con:
        con.execute("DELETE FROM feedback_failures WHERE order_line = ?", (order_line,))


def mark_not_duplicate(item_a: str, item_b: str) -> None:
    a, b = sorted((item_a, item_b))
    with _conn() as con:
        con.execute("INSERT OR IGNORE INTO not_duplicates (a, b) VALUES (?, ?)", (a, b))


def not_duplicate_pairs() -> set[tuple[str, str]]:
    with _conn() as con:
        return {(a, b) for a, b in con.execute("SELECT a, b FROM not_duplicates")}


# --- pieces (physical stock) -------------------------------------------------

PIECE_STATUSES = ("home", "sold", "lot", "donated")


def _piece_row(row, cursor) -> dict:
    d = dict(zip([c[0] for c in cursor.description], row))
    d["info"] = json.loads(d["info"]) if d.get("info") else {}
    if d.get("photo"):
        d["photo"] = _resolved(d["photo"])
    return d


def next_piece_code(box: str) -> str:
    """G3 -> G3-01, G3-02, ... Numbering follows the code prefix, so a piece
    that moved out of G3 still holds its number."""
    prefix = f"{box.upper()}-"
    with _conn() as con:
        codes = [r[0] for r in con.execute(
            "SELECT code FROM pieces WHERE code LIKE ?", (prefix + "%",))]
    nums = [int(c[len(prefix):]) for c in codes if c[len(prefix):].isdigit()]
    return f"{prefix}{(max(nums) + 1) if nums else 1:02d}"


def add_piece(box: str, photo: str | None = None) -> str:
    """Reserve the next code in `box` and store the piece. Retries if two
    photos race for the same number."""
    box = box.upper()
    for _ in range(5):
        code = next_piece_code(box)
        try:
            with _conn() as con:
                con.execute(
                    "INSERT INTO pieces (code, box, photo, info, created_at, updated_at) "
                    "VALUES (?, ?, ?, '{}', ?, ?)",
                    (code, box, stored_path(photo) if photo else photo, _now(), _now()))
            return code
        except sqlite3.IntegrityError:
            continue
    raise RuntimeError(f"couldn't reserve a code in {box}")


_PIECE_COLUMNS = {"box", "item_id", "status", "photo", "info", "value", "cost",
                  "sold_at", "order_id"}


def update_piece(code: str, **fields) -> None:
    code = code.upper()
    bad = set(fields) - _PIECE_COLUMNS
    if bad:
        raise ValueError(f"unknown piece field(s): {bad}")
    if "status" in fields and fields["status"] not in PIECE_STATUSES:
        raise ValueError(f"bad piece status {fields['status']!r}")
    if not fields:
        return
    if isinstance(fields.get("box"), str):
        fields["box"] = fields["box"].upper()
    if "info" in fields:
        fields["info"] = json.dumps(fields["info"])
    if fields.get("photo"):
        fields["photo"] = stored_path(fields["photo"])
    sets = ", ".join(f"{k} = ?" for k in fields) + ", updated_at = ?"
    with _conn() as con:
        con.execute(f"UPDATE pieces SET {sets} WHERE code = ?",
                    (*fields.values(), _now(), code))


def get_piece(code: str) -> dict | None:
    with _conn() as con:
        cur = con.execute("SELECT * FROM pieces WHERE code = ?", (code.upper(),))
        row = cur.fetchone()
        return _piece_row(row, cur) if row else None


def list_pieces(*, box: str | None = None, item_id: str | None = None,
                status: str | None = None) -> list[dict]:
    where, args = [], []
    for col, val in (("box", box), ("item_id", item_id), ("status", status)):
        if val is not None:
            where.append(f"{col} = ?")
            args.append(val.upper() if col == "box" else val)
    sql = "SELECT * FROM pieces" + (" WHERE " + " AND ".join(where) if where else "")
    with _conn() as con:
        # Numeric part of "G3-100" so it sorts after "G3-11"; code breaks ties.
        cur = con.execute(
            sql + " ORDER BY box, CAST(substr(code, instr(code, '-') + 1) AS INTEGER), code",
            args)
        return [_piece_row(r, cur) for r in cur.fetchall()]


def seen_order_ids() -> set[str]:
    with _conn() as con:
        return {r[0] for r in con.execute("SELECT order_id FROM seen_orders")}


def mark_orders_seen(order_ids) -> None:
    with _conn() as con:
        con.executemany("INSERT OR IGNORE INTO seen_orders (order_id, seen_at) VALUES (?, ?)",
                        [(o, _now()) for o in order_ids])


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


def record_llm_usage(*, provider: str, model: str, stage: str | None = None,
                     item_id: str | None = None, input_tokens: int = 0,
                     output_tokens: int = 0, cache_read_tokens: int = 0,
                     cache_write_tokens: int = 0, web_searches: int = 0,
                     cost_usd: float | None = None, ts: str | None = None) -> None:
    with _conn() as con:
        con.execute(
            "INSERT INTO llm_usage (ts, provider, model, stage, item_id, input_tokens, "
            "output_tokens, cache_read_tokens, cache_write_tokens, web_searches, cost_usd) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (ts or _now(), provider, model, stage, item_id, input_tokens, output_tokens,
             cache_read_tokens, cache_write_tokens, web_searches, cost_usd),
        )


def llm_spend_since(since_iso: str, provider: str | None = None) -> float:
    """Total logged cost (USD) from since_iso onward, optionally for one provider."""
    sql = "SELECT COALESCE(SUM(cost_usd), 0) FROM llm_usage WHERE ts >= ?"
    args: list = [since_iso]
    if provider is not None:
        sql += " AND provider = ?"
        args.append(provider)
    with _conn() as con:
        return float(con.execute(sql, args).fetchone()[0])


def llm_usage_breakdown(since_iso: str) -> list[dict]:
    """Spend grouped by provider, model and stage from since_iso onward, dearest first."""
    with _conn() as con:
        cur = con.execute(
            "SELECT provider, model, COALESCE(stage, '-') AS stage, COUNT(*) AS calls, "
            "SUM(input_tokens) AS input_tokens, SUM(output_tokens) AS output_tokens, "
            "SUM(cache_read_tokens) AS cache_read_tokens, SUM(web_searches) AS web_searches, "
            "SUM(cost_usd) AS cost_usd, SUM(cost_usd IS NULL) AS unpriced "
            "FROM llm_usage WHERE ts >= ? GROUP BY provider, model, stage "
            "ORDER BY COALESCE(SUM(cost_usd), 0) DESC",
            (since_iso,),
        )
        keys = [d[0] for d in cur.description]
        return [dict(zip(keys, row)) for row in cur.fetchall()]

create_tables()
