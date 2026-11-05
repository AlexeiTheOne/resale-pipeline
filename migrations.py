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


def _checks_table(con: sqlite3.Connection) -> None:
    """/check pre-buy checks (docs/check-command-design.md, section 00). Not
    items: one row per thing looked at in a shop."""
    con.execute("""
        CREATE TABLE IF NOT EXISTS checks (
            check_id           INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id            INTEGER,
            created_at         TEXT NOT NULL,
            updated_at         TEXT NOT NULL,
            ross_sku           TEXT,
            ross_price         REAL,
            photos             TEXT,
            identification     TEXT,
            mode               TEXT,
            exact              INTEGER,
            verdict            TEXT,
            suggested_price    REAL,
            profit             REAL,
            roi                REAL,
            break_even         REAL,
            cheapest_identical REAL,
            sold_count         INTEGER,
            comps_summary      TEXT,
            status             TEXT NOT NULL DEFAULT 'checked'
                               CHECK (status IN ('checked', 'bought', 'skipped')),
            status_at          TEXT,
            receipt_item_id    INTEGER,
            item_id            TEXT
        )
    """)
    con.execute("CREATE INDEX IF NOT EXISTS idx_checks_sku ON checks (ross_sku)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_checks_created ON checks (created_at)")


def _checks_sku_main(con: sqlite3.Connection) -> None:
    """The main tag's Ross code when the REDUCED sticker's (ross_sku) differs.
    Items and pieces keep theirs in their JSON (receipt.main_code,
    info.ross_code_main)."""
    con.execute("ALTER TABLE checks ADD COLUMN ross_sku_main TEXT")


# (version, name, function). Append only.
MIGRATIONS = [
    (1, "baseline", _baseline),
    (2, "checks_table", _checks_table),
    (3, "checks_sku_main", _checks_sku_main),
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


# =============================================================================
# SQL Server (ResaleDB). The design is docs/schema-proposal.md; tests/
# test_mssql_schema.py checks this DDL against that document.
#
# A separate step list with its own numbering, started at 1 for the new
# database. The SQLite steps above stay frozen until cutover, then go.
# Same rules: append only, never edit a step that has shipped.
#
# Constraint and index names are explicit (PK_, FK_, UX_, IX_, CK_, DF_) so
# later steps can alter them by name. Every timestamp is UTC DATETIME2(6).
# =============================================================================

def _json_check(table: str, col: str) -> str:
    return f"CONSTRAINT CK_{table}_{col}_json CHECK (ISJSON({col}) = 1)"


def _ross_code_check(table: str) -> str:
    return (f"CONSTRAINT CK_{table}_ross_code CHECK "
            "(LEN(ross_code) = 12 AND ross_code NOT LIKE '%[^0-9]%')")


_ITEM_STATUSES = ("captured", "identified", "priced", "drafted", "review", "approved",
                  "ebay_draft", "published", "sold", "rejected", "stocked")


def _in(values) -> str:
    return ", ".join(f"'{v}'" for v in values)


_MSSQL_BASELINE = [
    # --- 1A. items and its 1:1 groups ----------------------------------------
    f"""
    CREATE TABLE dbo.items (
        item_id            VARCHAR(36)   NOT NULL CONSTRAINT PK_items PRIMARY KEY,
        status             VARCHAR(20)   NOT NULL CONSTRAINT DF_items_status DEFAULT 'captured'
                           CONSTRAINT CK_items_status CHECK (status IN ({_in(_ITEM_STATUSES)})),
        origin             VARCHAR(20)   NULL,
        quick_price        DECIMAL(10,2) NULL,
        quick_price_basis  NVARCHAR(100) NULL,
        quick_price_rough  BIT           NULL,
        post_ready         BIT           NOT NULL CONSTRAINT DF_items_post_ready DEFAULT 0,
        post_blocker       NVARCHAR(300) NULL,
        photo_order_manual BIT           NOT NULL CONSTRAINT DF_items_photo_order_manual DEFAULT 0,
        photos_as_sent     BIT           NOT NULL CONSTRAINT DF_items_photos_as_sent DEFAULT 0,
        price_source_url   NVARCHAR(1000) NULL,
        extra_raw          NVARCHAR(MAX) NULL {_json_check('items', 'extra_raw')},
        created_at         DATETIME2(6)  NOT NULL CONSTRAINT DF_items_created_at DEFAULT SYSUTCDATETIME(),
        updated_at         DATETIME2(6)  NOT NULL CONSTRAINT DF_items_updated_at DEFAULT SYSUTCDATETIME(),
        row_ver            ROWVERSION    NOT NULL,
        -- Insertion order, like SQLite's rowid: list_items() returns items in it.
        seq                BIGINT IDENTITY(1,1) NOT NULL
    )""",
    "CREATE UNIQUE INDEX UX_items_seq ON dbo.items (seq)",
    "CREATE INDEX IX_items_status ON dbo.items (status)",
    "CREATE INDEX IX_items_updated_at ON dbo.items (updated_at)",

    f"""
    CREATE TABLE dbo.item_identification (
        item_id            VARCHAR(36)   NOT NULL CONSTRAINT PK_item_identification PRIMARY KEY
                           CONSTRAINT FK_item_identification_items REFERENCES dbo.items (item_id) ON DELETE CASCADE,
        brand              NVARCHAR(100) NULL,
        product_name       NVARCHAR(300) NULL,
        model              NVARCHAR(150) NULL,
        item_type          NVARCHAR(100) NULL,
        color              NVARCHAR(200) NULL,
        size               NVARCHAR(200) NULL,
        material           NVARCHAR(200) NULL,
        condition          NVARCHAR(50)  NULL,
        upc                VARCHAR(14)   NULL,
        upc_decoded        BIT           NULL,
        style_code         VARCHAR(60)   NULL,
        department         NVARCHAR(30)  NULL,
        gender             NVARCHAR(20)  NULL,
        search_query       NVARCHAR(300) NULL,
        retail_price       DECIMAL(10,2) NULL,
        resale_estimate    DECIMAL(10,2) NULL,
        confidence         DECIMAL(4,3)  NULL,
        product_image_url  NVARCHAR(1000) NULL,
        identification_raw NVARCHAR(MAX) NULL {_json_check('item_identification', 'identification_raw')},
        identified_at      DATETIME2(6)  NULL
    )""",
    "CREATE INDEX IX_item_identification_brand ON dbo.item_identification (brand)",
    "CREATE INDEX IX_item_identification_upc ON dbo.item_identification (upc)",
    "CREATE INDEX IX_item_identification_item_type ON dbo.item_identification (item_type)",

    """
    CREATE TABLE dbo.pricing_runs (
        run_id      INT IDENTITY(1,1) NOT NULL CONSTRAINT PK_pricing_runs PRIMARY KEY,
        item_id     VARCHAR(36)   NOT NULL
                    CONSTRAINT FK_pricing_runs_items REFERENCES dbo.items (item_id) ON DELETE CASCADE,
        priced_at   DATETIME2(6)  NOT NULL CONSTRAINT DF_pricing_runs_priced_at DEFAULT SYSUTCDATETIME(),
        engine      VARCHAR(10)   NULL,
        query_used  NVARCHAR(300) NULL
    )""",
    "CREATE INDEX IX_pricing_runs_item ON dbo.pricing_runs (item_id, priced_at DESC)",

    f"""
    CREATE TABLE dbo.item_pricing (
        item_id           VARCHAR(36)   NOT NULL CONSTRAINT PK_item_pricing PRIMARY KEY
                          CONSTRAINT FK_item_pricing_items REFERENCES dbo.items (item_id) ON DELETE CASCADE,
        -- NO ACTION: items already cascades to pricing_runs; two cascade paths
        -- are not allowed. Deleting an item still removes both rows.
        latest_run_id     INT           NULL
                          CONSTRAINT FK_item_pricing_latest_run REFERENCES dbo.pricing_runs (run_id),
        suggested_price   DECIMAL(10,2) NULL,
        machine_price     DECIMAL(10,2) NULL,
        manual_price      DECIMAL(10,2) NULL,
        manual_price_at   DATETIME2(6)  NULL,
        claude_price      DECIMAL(10,2) NULL,
        formula_price     DECIMAL(10,2) NULL,
        price_basis       VARCHAR(20)   NULL,
        confidence        VARCHAR(20)   NULL,
        claude_confidence VARCHAR(20)   NULL,
        evidence          VARCHAR(10)   NULL,
        needs_review      BIT           NULL,
        sold_count        INT           NULL,
        active_count      INT           NULL,
        sold_median       DECIMAL(10,2) NULL,
        sold_p10          DECIMAL(10,2) NULL,
        sold_p90          DECIMAL(10,2) NULL,
        active_floor      DECIMAL(10,2) NULL,
        raw_active_floor  DECIMAL(10,2) NULL,
        dispersion        DECIMAL(6,2)  NULL,
        research_resale   DECIMAL(10,2) NULL,
        retail_price      DECIMAL(10,2) NULL,
        resale_estimate   DECIMAL(10,2) NULL,
        comp_warning      NVARCHAR(400) NULL,
        query_rung        VARCHAR(20)   NULL,
        rungs_tried       VARCHAR(100)  NULL,
        stock_image_url   NVARCHAR(1000) NULL,
        value_notes       NVARCHAR(2000) NULL,
        pricing_raw       NVARCHAR(MAX) NULL {_json_check('item_pricing', 'pricing_raw')},
        priced_at         DATETIME2(6)  NULL
    )""",
    "CREATE INDEX IX_item_pricing_latest_run ON dbo.item_pricing (latest_run_id)",

    """
    CREATE TABLE dbo.item_listing (
        item_id         VARCHAR(36)   NOT NULL CONSTRAINT PK_item_listing PRIMARY KEY
                        CONSTRAINT FK_item_listing_items REFERENCES dbo.items (item_id) ON DELETE CASCADE,
        title           NVARCHAR(80)  NULL,
        description     NVARCHAR(MAX) NULL,
        list_price      DECIMAL(10,2) NULL,
        category_id     VARCHAR(10)   NULL,
        condition_id    VARCHAR(10)   NULL,
        stock_image_url NVARCHAR(1000) NULL,
        drafted_at      DATETIME2(6)  NULL
    )""",
    "CREATE INDEX IX_item_listing_list_price ON dbo.item_listing (list_price)",

    f"""
    CREATE TABLE dbo.ebay_listings (
        item_id              VARCHAR(36)   NOT NULL CONSTRAINT PK_ebay_listings PRIMARY KEY
                             CONSTRAINT FK_ebay_listings_items REFERENCES dbo.items (item_id) ON DELETE CASCADE,
        sku                  VARCHAR(50)   NULL,
        offer_id             VARCHAR(20)   NULL,
        listing_id           VARCHAR(20)   NULL,
        view_item_url        NVARCHAR(500) NULL,
        quantity             INT           NOT NULL CONSTRAINT DF_ebay_listings_quantity DEFAULT 0
                             CONSTRAINT CK_ebay_listings_quantity CHECK (quantity >= 0),
        category_id          VARCHAR(10)   NULL,
        category_name        NVARCHAR(200) NULL,
        reselected_from      VARCHAR(10)   NULL,
        reselected_from_name NVARCHAR(200) NULL,
        published_at         DATETIME2(6)  NULL,
        stock_note           NVARCHAR(300) NULL,
        ebay_raw             NVARCHAR(MAX) NULL {_json_check('ebay_listings', 'ebay_raw')}
    )""",
    "CREATE UNIQUE INDEX UX_ebay_listings_sku ON dbo.ebay_listings (sku) WHERE sku IS NOT NULL",
    "CREATE UNIQUE INDEX UX_ebay_listings_offer_id ON dbo.ebay_listings (offer_id) WHERE offer_id IS NOT NULL",
    "CREATE UNIQUE INDEX UX_ebay_listings_listing_id ON dbo.ebay_listings (listing_id) WHERE listing_id IS NOT NULL",

    f"""
    CREATE TABLE dbo.item_tags (
        item_id             VARCHAR(36)   NOT NULL CONSTRAINT PK_item_tags PRIMARY KEY
                            CONSTRAINT FK_item_tags_items REFERENCES dbo.items (item_id) ON DELETE CASCADE,
        ross_code           CHAR(12)      NULL {_ross_code_check('item_tags')},
        code_generated      BIT           NULL,
        ross_barcode        VARCHAR(18)   NULL,
        ross_price          DECIMAL(10,2) NULL,
        ross_original_price DECIMAL(10,2) NULL,
        peeled              BIT           NULL,
        tag_source          VARCHAR(10)   NULL,
        ocr_variant         VARCHAR(20)   NULL,
        tag_error           NVARCHAR(200) NULL,
        tag_photo_path      NVARCHAR(400) NULL,
        tag_raw             NVARCHAR(MAX) NULL,
        read_at             DATETIME2(6)  NULL
    )""",
    "CREATE INDEX IX_item_tags_ross_code ON dbo.item_tags (ross_code)",

    # --- 1B. child tables -----------------------------------------------------
    """
    CREATE TABLE dbo.item_photos (
        item_id  VARCHAR(36)   NOT NULL
                 CONSTRAINT FK_item_photos_items REFERENCES dbo.items (item_id) ON DELETE CASCADE,
        position TINYINT       NOT NULL,
        path     NVARCHAR(400) NOT NULL,
        CONSTRAINT PK_item_photos PRIMARY KEY (item_id, position)
    )""",
    """
    CREATE TABLE dbo.listing_images (
        item_id  VARCHAR(36)    NOT NULL
                 CONSTRAINT FK_listing_images_items REFERENCES dbo.items (item_id) ON DELETE CASCADE,
        position TINYINT        NOT NULL,
        url      NVARCHAR(1000) NOT NULL,
        CONSTRAINT PK_listing_images PRIMARY KEY (item_id, position)
    )""",
    """
    CREATE TABLE dbo.listing_specifics (
        item_id  VARCHAR(36)   NOT NULL
                 CONSTRAINT FK_listing_specifics_items REFERENCES dbo.items (item_id) ON DELETE CASCADE,
        name     NVARCHAR(65)  NOT NULL,
        position TINYINT       NOT NULL,
        value    NVARCHAR(400) NULL,
        CONSTRAINT PK_listing_specifics PRIMARY KEY (item_id, name, position)
    )""",
    "CREATE INDEX IX_listing_specifics_name_value ON dbo.listing_specifics (name, value)",
    """
    CREATE TABLE dbo.item_flags (
        item_id VARCHAR(36)   NOT NULL
                CONSTRAINT FK_item_flags_items REFERENCES dbo.items (item_id) ON DELETE CASCADE,
        kind    VARCHAR(20)   NOT NULL CONSTRAINT CK_item_flags_kind CHECK (kind IN ('condition', 'review')),
        flag    NVARCHAR(100) NOT NULL,
        position SMALLINT     NOT NULL,
        CONSTRAINT PK_item_flags PRIMARY KEY (item_id, kind, flag)
    )""",
    "CREATE INDEX IX_item_flags_kind_flag ON dbo.item_flags (kind, flag)",
    """
    CREATE TABLE dbo.pricing_comps (
        comp_id          INT IDENTITY(1,1) NOT NULL CONSTRAINT PK_pricing_comps PRIMARY KEY,
        run_id           INT            NOT NULL
                         CONSTRAINT FK_pricing_comps_runs REFERENCES dbo.pricing_runs (run_id) ON DELETE CASCADE,
        kind             VARCHAR(10)    NOT NULL
                         CONSTRAINT CK_pricing_comps_kind CHECK (kind IN ('sold', 'active', 'judged', 'reference')),
        position         SMALLINT       NOT NULL,
        price            DECIMAL(10,2)  NULL,
        item_price       DECIMAL(10,2)  NULL,
        shipping         DECIMAL(10,2)  NULL,
        shipping_imputed BIT            NULL,
        title            NVARCHAR(300)  NULL,
        condition        NVARCHAR(60)   NULL,
        url              NVARCHAR(1000) NULL,
        image_url        NVARCHAR(1000) NULL,
        verdict          VARCHAR(20)    NULL,
        seller           NVARCHAR(100)  NULL,
        source           VARCHAR(30)    NULL,
        ext_id           VARCHAR(40)    NULL,
        comp_raw         NVARCHAR(MAX)  NULL CONSTRAINT CK_pricing_comps_comp_raw_json CHECK (ISJSON(comp_raw) = 1)
    )""",
    "CREATE INDEX IX_pricing_comps_run_kind ON dbo.pricing_comps (run_id, kind)",
    # Money columns NOT NULL DEFAULT 0: ebay/orders.py totals_from_sales treats a
    # missing refund/ad fee as 0 and requires the rest; fees_known defaults true
    # there too.
    """
    CREATE TABLE dbo.sales (
        sale_id    INT IDENTITY(1,1) NOT NULL CONSTRAINT PK_sales PRIMARY KEY,
        -- NO ACTION: a sold item can't be deleted (Q8).
        item_id    VARCHAR(36)   NOT NULL CONSTRAINT FK_sales_items REFERENCES dbo.items (item_id),
        order_id   VARCHAR(30)   NOT NULL,
        listing_id VARCHAR(20)   NULL,
        sold_at    DATETIME2(6)  NULL,
        units      INT           NOT NULL CONSTRAINT DF_sales_units DEFAULT 1,
        sale       DECIMAL(10,2) NOT NULL CONSTRAINT DF_sales_sale DEFAULT 0,
        shipping   DECIMAL(10,2) NOT NULL CONSTRAINT DF_sales_shipping DEFAULT 0,
        fees       DECIMAL(10,2) NOT NULL CONSTRAINT DF_sales_fees DEFAULT 0,
        label      DECIMAL(10,2) NOT NULL CONSTRAINT DF_sales_label DEFAULT 0,
        ad_fee     DECIMAL(10,2) NOT NULL CONSTRAINT DF_sales_ad_fee DEFAULT 0,
        refund     DECIMAL(10,2) NOT NULL CONSTRAINT DF_sales_refund DEFAULT 0,
        net        DECIMAL(10,2) NOT NULL CONSTRAINT DF_sales_net DEFAULT 0,
        fees_known BIT           NOT NULL CONSTRAINT DF_sales_fees_known DEFAULT 1
    )""",
    "CREATE UNIQUE INDEX UX_sales_order_item ON dbo.sales (order_id, item_id)",
    "CREATE INDEX IX_sales_item ON dbo.sales (item_id)",
    "CREATE INDEX IX_sales_sold_at ON dbo.sales (sold_at)",
    """
    CREATE TABLE dbo.listing_checks (
        check_id      INT IDENTITY(1,1) NOT NULL CONSTRAINT PK_listing_checks PRIMARY KEY,
        item_id       VARCHAR(36)   NOT NULL
                      CONSTRAINT FK_listing_checks_items REFERENCES dbo.items (item_id) ON DELETE CASCADE,
        checked_at    DATETIME2(6)  NOT NULL CONSTRAINT DF_listing_checks_checked_at DEFAULT SYSUTCDATETIME(),
        model         VARCHAR(60)   NULL,
        cost_usd      DECIMAL(18,10) NULL,
        input_tokens  INT           NULL,
        output_tokens INT           NULL,
        unchecked     BIT           NOT NULL CONSTRAINT DF_listing_checks_unchecked DEFAULT 0
    )""",
    "CREATE INDEX IX_listing_checks_item ON dbo.listing_checks (item_id, checked_at)",
    """
    CREATE TABLE dbo.listing_check_issues (
        check_id INT           NOT NULL
                 CONSTRAINT FK_listing_check_issues_checks REFERENCES dbo.listing_checks (check_id) ON DELETE CASCADE,
        position TINYINT       NOT NULL,
        severity VARCHAR(10)   NULL,
        field    VARCHAR(40)   NULL,
        message  NVARCHAR(500) NULL,
        CONSTRAINT PK_listing_check_issues PRIMARY KEY (check_id, position)
    )""",

    # --- 1C. physical stock ---------------------------------------------------
    # Upper case is checked with a binary collation: the database collation is
    # case-insensitive, so a plain code = UPPER(code) would always pass.
    """
    CREATE TABLE dbo.locations (
        location_code VARCHAR(40)   NOT NULL CONSTRAINT PK_locations PRIMARY KEY
                      CONSTRAINT CK_locations_upper CHECK (location_code = UPPER(location_code) COLLATE Latin1_General_BIN2),
        label         NVARCHAR(100) NULL,
        kind          VARCHAR(20)   NULL
                      CONSTRAINT CK_locations_kind CHECK (kind IN ('box', 'shelf', 'bin', 'room', 'other')),
        parent_code   VARCHAR(40)   NULL
                      CONSTRAINT FK_locations_parent REFERENCES dbo.locations (location_code),
        active        BIT           NOT NULL CONSTRAINT DF_locations_active DEFAULT 1,
        created_at    DATETIME2(6)  NOT NULL CONSTRAINT DF_locations_created_at DEFAULT SYSUTCDATETIME()
    )""",
    f"""
    CREATE TABLE dbo.pieces (
        code                VARCHAR(50)   NOT NULL CONSTRAINT PK_pieces PRIMARY KEY
                            CONSTRAINT CK_pieces_upper CHECK (code = UPPER(code) COLLATE Latin1_General_BIN2),
        location_code       VARCHAR(40)   NOT NULL
                            CONSTRAINT FK_pieces_locations REFERENCES dbo.locations (location_code),
        item_id             VARCHAR(36)   NULL
                            CONSTRAINT FK_pieces_items REFERENCES dbo.items (item_id) ON DELETE SET NULL,
        status              VARCHAR(10)   NOT NULL CONSTRAINT DF_pieces_status DEFAULT 'home'
                            CONSTRAINT CK_pieces_status CHECK (status IN ('home', 'sold', 'lot', 'donated')),
        photo_path          NVARCHAR(400) NULL,
        brand               NVARCHAR(100) NULL,
        model               NVARCHAR(150) NULL,
        item_type           NVARCHAR(100) NULL,
        color               NVARCHAR(200) NULL,
        product_name        NVARCHAR(300) NULL,
        upc                 VARCHAR(14)   NULL,
        style_code          VARCHAR(60)   NULL,
        msrp                DECIMAL(10,2) NULL,
        ross_code           CHAR(12)      NULL {_ross_code_check('pieces')},
        ross_barcode        VARCHAR(18)   NULL,
        ross_price          DECIMAL(10,2) NULL,
        ross_original_price DECIMAL(10,2) NULL,
        value               DECIMAL(10,2) NULL,
        cost                DECIMAL(10,2) NULL,
        notes               NVARCHAR(500) NULL,
        info_raw            NVARCHAR(MAX) NULL {_json_check('pieces', 'info_raw')},
        order_id            VARCHAR(30)   NULL,
        sold_at             DATETIME2(6)  NULL,
        created_at          DATETIME2(6)  NOT NULL CONSTRAINT DF_pieces_created_at DEFAULT SYSUTCDATETIME(),
        updated_at          DATETIME2(6)  NOT NULL CONSTRAINT DF_pieces_updated_at DEFAULT SYSUTCDATETIME()
    )""",
    "CREATE INDEX IX_pieces_location ON dbo.pieces (location_code)",
    "CREATE INDEX IX_pieces_item_status ON dbo.pieces (item_id, status)",
    "CREATE INDEX IX_pieces_ross_code ON dbo.pieces (ross_code)",
    "CREATE INDEX IX_pieces_status ON dbo.pieces (status)",

    # --- 1D. receipts ---------------------------------------------------------
    # Same names, columns, types and nullability as ReceiptsDB, so
    # receipt_pipeline/bot.py works with only a new SQL_CONN_STR. Additions are
    # invisible to its INSERTs (computed or nullable). The unique indexes on
    # ReceiptNumber and (ReceiptID, LineIndex) come in a later step, after the
    # receipt cleanup removes today's duplicates.
    """
    CREATE TABLE dbo.Receipts (
        ReceiptID       INT IDENTITY(1,1) NOT NULL CONSTRAINT PK_Receipts PRIMARY KEY,
        ReceiptNumber   VARCHAR(30)   NULL,
        Store           VARCHAR(100)  NULL,
        StoreCity       VARCHAR(100)  NULL,
        StoreZip        VARCHAR(10)   NULL,
        TransactionType VARCHAR(10)   NULL,
        ReceiptDate     DATE          NULL,
        ReceiptTime     TIME(7)       NULL,
        Subtotal        DECIMAL(10,2) NULL,
        Tax             DECIMAL(10,2) NULL,
        Total           DECIMAL(10,2) NULL,
        Confidence      DECIMAL(4,2)  NULL,
        Flags           VARCHAR(200)  NULL,
        RawFileID       VARCHAR(50)   NULL,
        CreatedAt       DATETIME2(6)  NULL CONSTRAINT DF_Receipts_CreatedAt DEFAULT SYSUTCDATETIME(),
        ItemsCount      INT           NULL,
        SourceFile      NVARCHAR(255) NULL,
        Status          NVARCHAR(100) NULL,
        StoreNumber     AS CAST(LEFT(ReceiptNumber, 4) AS CHAR(4)) PERSISTED,
        ReviewStatus    VARCHAR(20)   NULL
                        CONSTRAINT CK_Receipts_ReviewStatus CHECK (ReviewStatus IN ('ok', 'wrong'))
    )""",
    "CREATE INDEX IX_Receipts_ReceiptNumber ON dbo.Receipts (ReceiptNumber)",
    "CREATE INDEX IX_Receipts_StoreNumber ON dbo.Receipts (StoreNumber)",
    """
    CREATE TABLE dbo.ReceiptItems (
        ItemID        INT IDENTITY(1,1) NOT NULL CONSTRAINT PK_ReceiptItems PRIMARY KEY,
        ReceiptID     INT           NOT NULL
                      CONSTRAINT FK_ReceiptItems_Receipts REFERENCES dbo.Receipts (ReceiptID),
        LineIndex     INT           NOT NULL,
        SKU           VARCHAR(20)   NULL,
        Description   NVARCHAR(500) NULL,
        Price         DECIMAL(10,2) NULL,
        PriceOriginal DECIMAL(10,2) NULL
    )""",
    "CREATE INDEX IX_ReceiptItems_SKU ON dbo.ReceiptItems (SKU)",
    "CREATE INDEX IX_ReceiptItems_SKU_Price ON dbo.ReceiptItems (SKU, Price)",
    "CREATE INDEX IX_ReceiptItems_ReceiptID ON dbo.ReceiptItems (ReceiptID)",
    """
    CREATE TABLE dbo.stores (
        store_number CHAR(4)       NOT NULL CONSTRAINT PK_stores PRIMARY KEY,
        name         NVARCHAR(100) NULL,
        city         NVARCHAR(60)  NULL,
        zip          VARCHAR(10)   NULL
    )""",
    # item_id CASCADE: a match to a deleted item means nothing. piece_code and
    # receipt_item_id are NO ACTION (pieces and receipt lines aren't deleted).
    """
    CREATE TABLE dbo.receipt_matches (
        match_id        INT IDENTITY(1,1) NOT NULL CONSTRAINT PK_receipt_matches PRIMARY KEY,
        receipt_item_id INT           NOT NULL
                        CONSTRAINT FK_receipt_matches_lines REFERENCES dbo.ReceiptItems (ItemID),
        piece_code      VARCHAR(50)   NULL
                        CONSTRAINT FK_receipt_matches_pieces REFERENCES dbo.pieces (code),
        item_id         VARCHAR(36)   NULL
                        CONSTRAINT FK_receipt_matches_items REFERENCES dbo.items (item_id) ON DELETE CASCADE,
        sku             CHAR(12)      NOT NULL,
        price_delta     DECIMAL(10,2) NULL,
        days_apart      INT           NULL,
        method          VARCHAR(30)   NOT NULL
                        CONSTRAINT CK_receipt_matches_method CHECK (method IN
                            ('sku_price_unique', 'sku_price', 'sku_unique', 'sku_only', 'manual')),
        confidence      DECIMAL(3,2)  NOT NULL
                        CONSTRAINT CK_receipt_matches_confidence CHECK (confidence BETWEEN 0 AND 1),
        status          VARCHAR(10)   NOT NULL CONSTRAINT DF_receipt_matches_status DEFAULT 'proposed'
                        CONSTRAINT CK_receipt_matches_status CHECK (status IN ('proposed', 'confirmed', 'rejected')),
        matched_by      VARCHAR(30)   NULL,
        created_at      DATETIME2(6)  NOT NULL CONSTRAINT DF_receipt_matches_created_at DEFAULT SYSUTCDATETIME(),
        decided_at      DATETIME2(6)  NULL,
        CONSTRAINT CK_receipt_matches_target CHECK (piece_code IS NOT NULL OR item_id IS NOT NULL)
    )""",
    # A receipt line, and a piece, can each be confirmed once.
    ("CREATE UNIQUE INDEX UX_receipt_matches_line_confirmed ON dbo.receipt_matches (receipt_item_id) "
     "WHERE status = 'confirmed'"),
    ("CREATE UNIQUE INDEX UX_receipt_matches_piece_confirmed ON dbo.receipt_matches (piece_code) "
     "WHERE status = 'confirmed' AND piece_code IS NOT NULL"),
    "CREATE INDEX IX_receipt_matches_line ON dbo.receipt_matches (receipt_item_id)",
    "CREATE INDEX IX_receipt_matches_piece ON dbo.receipt_matches (piece_code)",
    "CREATE INDEX IX_receipt_matches_item ON dbo.receipt_matches (item_id)",
    """
    CREATE TABLE dbo.receipt_fixes (
        fix_id      INT IDENTITY(1,1) NOT NULL CONSTRAINT PK_receipt_fixes PRIMARY KEY,
        table_name  VARCHAR(30)   NOT NULL,
        row_id      INT           NOT NULL,
        column_name VARCHAR(30)   NULL,
        old_value   NVARCHAR(200) NULL,
        new_value   NVARCHAR(200) NULL,
        reason      VARCHAR(40)   NOT NULL,
        fixed_by    VARCHAR(30)   NULL,
        fixed_at    DATETIME2(6)  NOT NULL CONSTRAINT DF_receipt_fixes_fixed_at DEFAULT SYSUTCDATETIME()
    )""",

    # --- 1E. operations -------------------------------------------------------
    """
    -- History: no foreign key, so a week's stats outlive a deleted item (as on
    -- SQLite, and like llm_usage).
    CREATE TABLE dbo.listing_stats (
        item_id           VARCHAR(36)   NOT NULL,
        week              DATE          NOT NULL,
        impressions       INT           NULL,
        views             INT           NULL,
        watchers          INT           NULL,
        total_impressions INT           NULL,
        search_views      INT           NULL,
        price             DECIMAL(10,2) NULL,
        suggested_price   DECIMAL(10,2) NULL,
        verdict           VARCHAR(30)   NULL,
        created_at        DATETIME2(6)  NOT NULL CONSTRAINT DF_listing_stats_created_at DEFAULT SYSUTCDATETIME(),
        CONSTRAINT PK_listing_stats PRIMARY KEY (item_id, week)
    )""",
    """
    CREATE TABLE dbo.offer_sends (
        id          INT IDENTITY(1,1) NOT NULL CONSTRAINT PK_offer_sends PRIMARY KEY,
        item_id     VARCHAR(36)   NULL,
        listing_id  VARCHAR(20)   NOT NULL,
        list_price  DECIMAL(10,2) NULL,
        offer_price DECIMAL(10,2) NOT NULL,
        source      VARCHAR(20)   NULL,
        sent_at     DATETIME2(6)  NOT NULL CONSTRAINT DF_offer_sends_sent_at DEFAULT SYSUTCDATETIME()
    )""",
    "CREATE INDEX IX_offer_sends_listing_sent ON dbo.offer_sends (listing_id, sent_at)",
    "CREATE INDEX IX_offer_sends_sent_at ON dbo.offer_sends (sent_at)",
    """
    CREATE TABLE dbo.offer_plans (
        plan_id    VARCHAR(36)  NOT NULL CONSTRAINT PK_offer_plans PRIMARY KEY,
        created_at DATETIME2(6) NOT NULL CONSTRAINT DF_offer_plans_created_at DEFAULT SYSUTCDATETIME(),
        used_at    DATETIME2(6) NULL
    )""",
    """
    CREATE TABLE dbo.offer_plan_rows (
        plan_id     VARCHAR(36)   NOT NULL
                    CONSTRAINT FK_offer_plan_rows_plans REFERENCES dbo.offer_plans (plan_id) ON DELETE CASCADE,
        position    SMALLINT      NOT NULL,
        item_id     VARCHAR(36)   NULL,
        listing_id  VARCHAR(20)   NULL,
        price       DECIMAL(10,2) NULL,
        offer_price DECIMAL(10,2) NULL,
        pct         DECIMAL(5,2)  NULL,
        watchers    INT           NULL,
        title       NVARCHAR(80)  NULL,
        row_raw     NVARCHAR(MAX) NULL CONSTRAINT CK_offer_plan_rows_row_raw_json CHECK (ISJSON(row_raw) = 1),
        CONSTRAINT PK_offer_plan_rows PRIMARY KEY (plan_id, position)
    )""",
    """
    CREATE TABLE dbo.job_runs (
        job         VARCHAR(40)    NOT NULL CONSTRAINT PK_job_runs PRIMARY KEY,
        last_run_at DATETIME2(6)   NOT NULL,
        status      VARCHAR(20)    NULL,
        detail      NVARCHAR(1000) NULL
    )""",
    """
    CREATE TABLE dbo.feedback_failures (
        order_line      VARCHAR(40)    NOT NULL CONSTRAINT PK_feedback_failures PRIMARY KEY,
        first_failed_at DATETIME2(6)   NOT NULL CONSTRAINT DF_feedback_failures_first DEFAULT SYSUTCDATETIME(),
        last_error      NVARCHAR(1000) NULL
    )""",
    """
    CREATE TABLE dbo.not_duplicates (
        a VARCHAR(36) NOT NULL,
        b VARCHAR(36) NOT NULL,
        CONSTRAINT PK_not_duplicates PRIMARY KEY (a, b),
        -- Binary order, like Python's sorted() in db.mark_not_duplicate (the
        -- database collation is case-insensitive).
        CONSTRAINT CK_not_duplicates_order CHECK (a <= b COLLATE Latin1_General_BIN2)
    )""",
    """
    CREATE TABLE dbo.seen_orders (
        order_id VARCHAR(30)  NOT NULL CONSTRAINT PK_seen_orders PRIMARY KEY,
        seen_at  DATETIME2(6) NOT NULL CONSTRAINT DF_seen_orders_seen_at DEFAULT SYSUTCDATETIME()
    )""",
    """
    CREATE TABLE dbo.llm_usage (
        id                 BIGINT IDENTITY(1,1) NOT NULL CONSTRAINT PK_llm_usage PRIMARY KEY,
        ts                 DATETIME2(6)  NOT NULL CONSTRAINT DF_llm_usage_ts DEFAULT SYSUTCDATETIME(),
        provider           VARCHAR(20)   NOT NULL,
        model              VARCHAR(60)   NOT NULL,
        stage              VARCHAR(40)   NULL,
        item_id            VARCHAR(36)   NULL,
        input_tokens       INT           NOT NULL CONSTRAINT DF_llm_usage_input DEFAULT 0,
        output_tokens      INT           NOT NULL CONSTRAINT DF_llm_usage_output DEFAULT 0,
        cache_read_tokens  INT           NOT NULL CONSTRAINT DF_llm_usage_cache_read DEFAULT 0,
        cache_write_tokens INT           NOT NULL CONSTRAINT DF_llm_usage_cache_write DEFAULT 0,
        web_searches       INT           NOT NULL CONSTRAINT DF_llm_usage_web DEFAULT 0,
        cost_usd           DECIMAL(18,10) NULL
    )""",
    "CREATE INDEX IX_llm_usage_ts ON dbo.llm_usage (ts)",
    # NVARCHAR(MAX): today's access token is ~2,400 characters and eBay
    # doesn't promise a maximum.
    """
    CREATE TABLE dbo.ebay_tokens (
        id            TINYINT       NOT NULL CONSTRAINT PK_ebay_tokens PRIMARY KEY
                      CONSTRAINT CK_ebay_tokens_single CHECK (id = 1),
        access_token  NVARCHAR(MAX) NULL,
        refresh_token NVARCHAR(MAX) NULL,
        expires_at    DATETIME2(6)  NULL
    )""",
    f"""
    CREATE TABLE dbo.taxonomy_cache (
        cache_key   VARCHAR(200)  NOT NULL CONSTRAINT PK_taxonomy_cache PRIMARY KEY,
        -- Any JSON value: some cached payloads are plain strings (category names).
        payload_raw NVARCHAR(MAX) NOT NULL CONSTRAINT CK_taxonomy_cache_payload_raw_json CHECK (ISJSON(payload_raw, VALUE) = 1),
        fetched_at  DATETIME2(6)  NOT NULL
    )""",
    """
    CREATE TABLE dbo.app_settings (
        name       VARCHAR(40)   NOT NULL CONSTRAINT PK_app_settings PRIMARY KEY,
        value      NVARCHAR(200) NOT NULL,
        updated_at DATETIME2(6)  NOT NULL CONSTRAINT DF_app_settings_updated_at DEFAULT SYSUTCDATETIME()
    )""",
    # The default the view uses until db.py syncs config.SALES_TAX_RATE at
    # startup (Q17: 7%, measured on receipts).
    "INSERT INTO dbo.app_settings (name, value) VALUES ('SALES_TAX_RATE', '0.07')",

    # --- views ----------------------------------------------------------------
    # Same maths as ebay/orders.py totals_from_sales: a refund comes off the
    # item price first (floored at 0 per order), then off shipping; newest order
    # by (sold_at, order_id).
    """
    CREATE VIEW dbo.v_item_sales AS
    SELECT s.item_id,
           CAST(SUM(CASE WHEN s.sale - s.refund > 0 THEN s.sale - s.refund ELSE 0 END) AS DECIMAL(12,2)) AS sale_price,
           CAST(SUM(s.sale) AS DECIMAL(12,2)) AS gross_sale_price,
           CAST(SUM(CASE WHEN s.shipping - CASE WHEN s.refund > s.sale THEN s.refund - s.sale ELSE 0 END > 0
                         THEN s.shipping - CASE WHEN s.refund > s.sale THEN s.refund - s.sale ELSE 0 END
                         ELSE 0 END) AS DECIMAL(12,2)) AS shipping_collected,
           CAST(SUM(s.fees) AS DECIMAL(12,2)) AS ebay_fees,
           CAST(SUM(s.label) AS DECIMAL(12,2)) AS shipping_label_cost,
           CAST(SUM(s.ad_fee) AS DECIMAL(12,2)) AS ad_fees,
           CAST(SUM(s.refund) AS DECIMAL(12,2)) AS refunded,
           CAST(SUM(s.net) AS DECIMAL(12,2)) AS net_proceeds,
           SUM(s.units) AS units_sold,
           newest.order_id,
           newest.listing_id,
           MAX(s.sold_at) AS sold_at,
           MIN(s.sold_at) AS first_sold_at,
           CAST(MIN(CAST(s.fees_known AS INT)) AS BIT) AS fees_known,
           COUNT(*) AS order_count
    FROM dbo.sales s
    CROSS APPLY (SELECT TOP (1) n.order_id, n.listing_id FROM dbo.sales n
                 WHERE n.item_id = s.item_id
                 ORDER BY n.sold_at DESC, n.order_id DESC) newest
    GROUP BY s.item_id, newest.order_id, newest.listing_id
    """,
    # Effective unit cost. A receipt is never required: confirmed receipt line
    # price, else the tag price, else NULL. Prices are before tax.
    """
    CREATE VIEW dbo.v_item_cost AS
    WITH receipt AS (
        SELECT COALESCE(m.item_id, p.item_id) AS item_id, AVG(ri.Price) AS price
        FROM dbo.receipt_matches m
        JOIN dbo.ReceiptItems ri ON ri.ItemID = m.receipt_item_id
        LEFT JOIN dbo.pieces p ON p.code = m.piece_code
        WHERE m.status = 'confirmed' AND ri.Price IS NOT NULL
          AND COALESCE(m.item_id, p.item_id) IS NOT NULL
        GROUP BY COALESCE(m.item_id, p.item_id)
    )
    SELECT i.item_id,
           CAST(COALESCE(r.price, t.ross_price) AS DECIMAL(10,2)) AS unit_cost,
           CAST(CASE WHEN r.price IS NOT NULL THEN 'receipt'
                     WHEN t.ross_price IS NOT NULL THEN 'tag' END AS VARCHAR(10)) AS cost_source,
           rate.tax_rate,
           CAST(ROUND(COALESCE(r.price, t.ross_price) * (1 + rate.tax_rate), 2) AS DECIMAL(10,2))
               AS unit_cost_with_tax
    FROM dbo.items i
    LEFT JOIN dbo.item_tags t ON t.item_id = i.item_id
    LEFT JOIN receipt r ON r.item_id = i.item_id
    OUTER APPLY (SELECT TOP (1) TRY_CAST(value AS DECIMAL(6,4)) AS tax_rate
                 FROM dbo.app_settings WHERE name = 'SALES_TAX_RATE') rate
    """,
    # One flat row per item, for reports, the prod-logs skill and the web site.
    """
    CREATE VIEW dbo.v_items AS
    SELECT i.item_id, i.status, i.origin,
           ii.brand, ii.product_name, ii.model, ii.item_type, ii.color, ii.size, ii.condition, ii.upc,
           il.title, il.list_price,
           ip.suggested_price, ip.manual_price, ip.price_basis,
           el.sku, el.listing_id, el.offer_id, el.quantity, el.published_at,
           it.ross_code,
           c.unit_cost, c.cost_source, c.unit_cost_with_tax,
           s.sale_price, s.net_proceeds, s.units_sold, s.first_sold_at, s.sold_at,
           ISNULL(pc.pieces_home, 0) AS pieces_home,
           ISNULL(pc.pieces_total, 0) AS pieces_total,
           i.created_at, i.updated_at
    FROM dbo.items i
    LEFT JOIN dbo.item_identification ii ON ii.item_id = i.item_id
    LEFT JOIN dbo.item_listing il ON il.item_id = i.item_id
    LEFT JOIN dbo.item_pricing ip ON ip.item_id = i.item_id
    LEFT JOIN dbo.ebay_listings el ON el.item_id = i.item_id
    LEFT JOIN dbo.item_tags it ON it.item_id = i.item_id
    LEFT JOIN dbo.v_item_cost c ON c.item_id = i.item_id
    LEFT JOIN dbo.v_item_sales s ON s.item_id = i.item_id
    LEFT JOIN (SELECT item_id,
                      SUM(CASE WHEN status = 'home' THEN 1 ELSE 0 END) AS pieces_home,
                      COUNT(*) AS pieces_total
               FROM dbo.pieces WHERE item_id IS NOT NULL GROUP BY item_id) pc ON pc.item_id = i.item_id
    """,
    # Receipt problems, computed live so a flag clears once the data is fixed.
    """
    CREATE VIEW dbo.v_receipt_checks AS
    SELECT r.ReceiptID, r.ReceiptNumber, r.ReceiptDate, r.StoreNumber,
           r.Subtotal, r.Tax, r.Total,
           li.line_count,
           li.items_sum,
           CAST(r.Subtotal - li.items_sum AS DECIMAL(12,2)) AS subtotal_gap,
           CAST(r.Total - (r.Subtotal + r.Tax) AS DECIMAL(12,2)) AS total_gap,
           CAST(CASE WHEN r.Flags LIKE '%contains[_]returns%' OR r.Status LIKE '%return%'
                     THEN 1 ELSE 0 END AS BIT) AS has_returns,
           CAST(CASE WHEN r.ReceiptNumber IS NULL OR r.ReceiptDate IS NULL OR r.Subtotal IS NULL
                       OR r.Tax IS NULL OR r.Total IS NULL THEN 1 ELSE 0 END AS BIT) AS missing_fields,
           ISNULL(li.suspect_prices, 0) AS suspect_prices,
           r.ReviewStatus AS review_status
    FROM dbo.Receipts r
    OUTER APPLY (SELECT COUNT(*) AS line_count,
                        CAST(SUM(x.Price) AS DECIMAL(12,2)) AS items_sum,
                        SUM(CASE WHEN x.Price > 500 THEN 1 ELSE 0 END) AS suspect_prices
                 FROM dbo.ReceiptItems x WHERE x.ReceiptID = r.ReceiptID) li
    """,
]


def _mssql_baseline(cur) -> None:
    """Every table, view, index and constraint in docs/schema-proposal.md."""
    for statement in _MSSQL_BASELINE:
        cur.execute(statement)


def mssql_receipt_duplicates(cur) -> dict:
    """What blocks the unique receipt indexes: {"receipt_numbers": [...],
    "lines": [(ReceiptID, LineIndex), ...]}. scripts/receipts_cleanup.py
    resolves them."""
    cur.execute("SELECT ReceiptNumber FROM dbo.Receipts WHERE ReceiptNumber IS NOT NULL "
                "GROUP BY ReceiptNumber HAVING COUNT(*) > 1 ORDER BY ReceiptNumber")
    numbers = [r[0] for r in cur.fetchall()]
    cur.execute("SELECT ReceiptID, LineIndex FROM dbo.ReceiptItems GROUP BY ReceiptID, LineIndex "
                "HAVING COUNT(*) > 1 ORDER BY ReceiptID, LineIndex")
    return {"receipt_numbers": numbers, "lines": [tuple(r) for r in cur.fetchall()]}


def _mssql_receipt_unique(cur) -> None:
    """One row per receipt number, one line per (receipt, line number). The
    receipt bot already checks before inserting; these catch its double-submit
    race (it replies "already saved"). Built only once the receipt cleanup has
    removed today's duplicates: with any left, the step fails and changes
    nothing."""
    dups = mssql_receipt_duplicates(cur)
    if dups["receipt_numbers"] or dups["lines"]:
        raise RuntimeError(
            "receipt duplicates remain, so the unique receipt indexes can't be built: "
            f"{len(dups['receipt_numbers'])} receipt number(s) "
            f"{dups['receipt_numbers'][:5]}, {len(dups['lines'])} (ReceiptID, LineIndex) pair(s) "
            f"{dups['lines'][:5]}. Run scripts/receipts_cleanup.py --apply first.")
    cur.execute("CREATE UNIQUE INDEX UX_Receipts_ReceiptNumber ON dbo.Receipts (ReceiptNumber) "
                "WHERE ReceiptNumber IS NOT NULL")
    cur.execute("CREATE UNIQUE INDEX UX_ReceiptItems_ReceiptID_LineIndex "
                "ON dbo.ReceiptItems (ReceiptID, LineIndex)")


def _mssql_checks_table(cur) -> None:
    """/check pre-buy checks (docs/schema-proposal.md 1E, check-command-design
    section 00). No FKs: a check outlives a deleted item or receipt line, like
    listing_stats; receipt_item_id / item_id are filled by later steps."""
    cur.execute("""
    CREATE TABLE dbo.checks (
        check_id           INT IDENTITY(1,1) NOT NULL CONSTRAINT PK_checks PRIMARY KEY,
        user_id            BIGINT        NULL,
        created_at         DATETIME2(6)  NOT NULL CONSTRAINT DF_checks_created_at DEFAULT SYSUTCDATETIME(),
        updated_at         DATETIME2(6)  NOT NULL CONSTRAINT DF_checks_updated_at DEFAULT SYSUTCDATETIME(),
        ross_sku           VARCHAR(20)   NULL,
        ross_price         DECIMAL(10,2) NULL,
        photos             NVARCHAR(MAX) NULL CONSTRAINT CK_checks_photos_json CHECK (ISJSON(photos) = 1),
        identification     NVARCHAR(MAX) NULL CONSTRAINT CK_checks_ident_json CHECK (ISJSON(identification) = 1),
        mode               VARCHAR(10)   NULL,
        exact              BIT           NULL,
        verdict            VARCHAR(12)   NULL,
        suggested_price    DECIMAL(10,2) NULL,
        profit             DECIMAL(10,2) NULL,
        roi                DECIMAL(8,2)  NULL,
        break_even         DECIMAL(10,2) NULL,
        cheapest_identical DECIMAL(10,2) NULL,
        sold_count         INT           NULL,
        comps_summary      NVARCHAR(MAX) NULL CONSTRAINT CK_checks_comps_json CHECK (ISJSON(comps_summary) = 1),
        status             VARCHAR(10)   NOT NULL CONSTRAINT DF_checks_status DEFAULT 'checked'
                           CONSTRAINT CK_checks_status CHECK (status IN ('checked', 'bought', 'skipped')),
        status_at          DATETIME2(6)  NULL,
        receipt_item_id    INT           NULL,
        item_id            VARCHAR(36)   NULL
    )""")
    cur.execute("CREATE INDEX IX_checks_ross_sku ON dbo.checks (ross_sku)")
    cur.execute("CREATE INDEX IX_checks_created_at ON dbo.checks (created_at)")


def _mssql_ross_code_main(cur) -> None:
    """A Ross tag can carry two codes: the main tag's and a REDUCED sticker's
    (what we paid; ross_code / ross_sku stay that one). The main tag's, when
    it differs, so receipts and "already have" match on either. Nullable, no
    backfill: existing rows read one code."""
    for table in ("item_tags", "pieces"):
        cur.execute(f"ALTER TABLE dbo.{table} ADD ross_code_main CHAR(12) NULL "
                    f"CONSTRAINT CK_{table}_ross_code_main CHECK "
                    "(LEN(ross_code_main) = 12 AND ross_code_main NOT LIKE '%[^0-9]%')")
        cur.execute(f"CREATE INDEX IX_{table}_ross_code_main ON dbo.{table} (ross_code_main)")
    cur.execute("ALTER TABLE dbo.checks ADD ross_sku_main VARCHAR(20) NULL")


# (version, name, function). Append only.
MSSQL_MIGRATIONS = [
    (1, "resaledb_baseline", _mssql_baseline),
    (2, "receipt_unique_indexes", _mssql_receipt_unique),
    (3, "checks_table", _mssql_checks_table),
    (4, "ross_code_main", _mssql_ross_code_main),
]
# The production migration copies the receipts into a database at this
# version: the unique indexes (step 2) come after the receipt cleanup.
MSSQL_BEFORE_RECEIPT_CLEANUP = 1


def mssql_current_version(cur) -> int:
    cur.execute("""
        IF OBJECT_ID(N'dbo.schema_version', N'U') IS NULL
        CREATE TABLE dbo.schema_version (
            version    INT          NOT NULL CONSTRAINT PK_schema_version PRIMARY KEY,
            name       VARCHAR(100) NOT NULL,
            applied_at DATETIME2(6) NOT NULL CONSTRAINT DF_schema_version_applied_at DEFAULT SYSUTCDATETIME()
        )""")
    cur.execute("SELECT MAX(version) FROM dbo.schema_version")
    return cur.fetchone()[0] or 0


def migrate_mssql(con, migrations=None, upto: int | None = None) -> list[int]:
    """Run every SQL Server step newer than the database's version, each in one
    transaction (DDL included: a failed step changes nothing). An exclusive
    application lock makes a second process wait instead of running the same
    step twice. `con` must not be in autocommit mode. `upto`: stop after that
    version (the production migration). Returns the versions run."""
    if con.autocommit:
        raise ValueError("migrate_mssql needs a connection with autocommit off")
    migrations = MSSQL_MIGRATIONS if migrations is None else migrations
    known = max((v for v, _, _ in migrations), default=0)
    if upto is not None:
        migrations = [m for m in migrations if m[0] <= upto]
    ran = []
    cur = con.cursor()
    # Session-owned, so it spans the per-step transactions; released below.
    cur.execute("""
        SET NOCOUNT ON;
        DECLARE @r INT;
        EXEC @r = sp_getapplock @Resource = N'resaledb_migrate', @LockMode = 'Exclusive',
                                @LockOwner = 'Session', @LockTimeout = 60000;
        SELECT @r;""")
    got = cur.fetchone()[0]
    con.commit()
    if got < 0:
        cur.close()
        raise RuntimeError("could not get the migration lock (another migration running?)")
    try:
        for version, name, fn in migrations:
            try:
                if mssql_current_version(cur) >= version:
                    con.commit()
                    continue
                fn(cur)
                cur.execute("INSERT INTO dbo.schema_version (version, name) VALUES (?, ?)",
                            version, name)
                con.commit()
                ran.append(version)
            except BaseException:
                con.rollback()
                raise
        db_version = mssql_current_version(cur)
        con.commit()
    finally:
        try:
            cur.execute("EXEC sp_releaseapplock @Resource = N'resaledb_migrate', @LockOwner = 'Session'")
            con.commit()
        finally:
            cur.close()
    if db_version > known:
        print(f"WARNING: SQL Server schema is version {db_version}, this code "
              f"knows up to {known}. Was a deploy rolled back?")
    return ran
