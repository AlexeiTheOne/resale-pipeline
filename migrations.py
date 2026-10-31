"""Numbered database migrations.

Every schema change is a step in MIGRATIONS: (version, name, function). The
`schema_version` table records which steps have run, so each runs exactly
once, in order, inside one transaction (a failed step changes nothing).

To change the schema: append a new step with the next number. Never edit or
reorder a step that has shipped, because the live DB has already run it.
scripts/deploy.ps1 backs up the DB before each deploy, so a bad step can be
undone by restoring that backup.

db.create_tables() calls migrate() at import, so every process (bot, scripts,
tests) brings the DB up to date before touching it.
"""
import sqlite3
from datetime import datetime, timezone

# Columns added after the initial schema shipped. Each is created with an
# ALTER TABLE on startup if the existing table doesn't already have it.
_ITEM_COLUMNS = {
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

# listing_stats columns added after it shipped (same pattern as _ITEM_COLUMNS).
_LISTING_STATS_COLUMNS = {
    "total_impressions": "INTEGER",   # TOTAL_IMPRESSION_TOTAL, promoted included
    "search_views": "INTEGER",        # LISTING_VIEWS_SOURCE_SEARCH_RESULTS_PAGE
}


def _baseline(con: sqlite3.Connection) -> None:
    """The schema as it was when migrations started (31 Oct 2026). Idempotent:
    on a DB that already has these tables it only adds missing columns, so the
    live DB is recorded as version 1 without being changed."""
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
    for column, coltype in _ITEM_COLUMNS.items():
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
    for column, coltype in _LISTING_STATS_COLUMNS.items():
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

    # Created at import by ebay/auth.py and ebay/taxonomy.py before this file
    # existed; kept here so the whole schema lives in one place.
    con.execute("""
        CREATE TABLE IF NOT EXISTS ebay_tokens (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            access_token TEXT,
            refresh_token TEXT,
            expires_at REAL
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS taxonomy_cache (
            cache_key TEXT PRIMARY KEY,
            payload TEXT NOT NULL,
            fetched_at REAL NOT NULL
        )
    """)


# (version, name, function). Append only.
MIGRATIONS = [
    (1, "baseline", _baseline),
]


def current_version(con: sqlite3.Connection) -> int:
    con.execute("""
        CREATE TABLE IF NOT EXISTS schema_version (
            version    INTEGER PRIMARY KEY,
            name       TEXT NOT NULL,
            applied_at TEXT NOT NULL
        )
    """)
    row = con.execute("SELECT MAX(version) FROM schema_version").fetchone()
    return row[0] or 0


def migrate(con: sqlite3.Connection, migrations=None) -> list[int]:
    """Run every step newer than the DB's version. Returns the versions run.

    Uses its own transactions, so `con` must not be inside one. BEGIN
    IMMEDIATE takes the write lock before the version is read, so two
    processes starting at once can't both run a step."""
    migrations = MIGRATIONS if migrations is None else migrations
    known = max((v for v, _, _ in migrations), default=0)
    old_level = con.isolation_level
    con.isolation_level = None  # manual BEGIN/COMMIT, DDL included
    ran = []
    try:
        for version, name, fn in migrations:
            con.execute("BEGIN IMMEDIATE")
            try:
                if current_version(con) >= version:
                    con.execute("COMMIT")
                    continue
                fn(con)
                con.execute(
                    "INSERT INTO schema_version (version, name, applied_at) VALUES (?, ?, ?)",
                    (version, name, datetime.now(timezone.utc).isoformat()),
                )
                con.execute("COMMIT")
                ran.append(version)
            except BaseException:
                con.execute("ROLLBACK")
                raise
        db_version = current_version(con)
        if db_version > known:
            # Code older than the DB, e.g. after a rollback. Steps only add
            # things, so older code still works; warn instead of refusing.
            print(f"WARNING: database schema is version {db_version}, this code "
                  f"knows up to {known}. Was a deploy rolled back?")
    finally:
        con.isolation_level = old_level
    return ran
