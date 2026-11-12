import re
import sqlite3
import uuid
import json
from datetime import datetime, timezone
from pathlib import Path

import config
import migrations
from config import DATA_DIR, resolve_path, stored_path

DB_PATH = str(DATA_DIR / "ross.db")


# --- backend switch (SQL Server migration, docs/schema-proposal.md) ----------
# config.DB_BACKEND picks the database. "sqlite" (default) is this file as it
# always was. "sqlserver" sends every public function below to db_mssql.py,
# which reads and writes ResaleDB and returns the same shapes.

def _on_sqlserver() -> bool:
    backend = config.DB_BACKEND
    if backend not in ("sqlite", "sqlserver"):
        raise ValueError(f"DB_BACKEND must be 'sqlite' or 'sqlserver', not {backend!r}")
    return backend == "sqlserver"


def _sqlserver():
    import db_mssql  # only needs pyodbc when the backend is actually used
    return db_mssql



VALID_STATUSES = [
    "captured", "identified", "priced", "drafted",
    "review", "approved", "ebay_draft", "published", "sold", "rejected",
    "stocked",  # quick-added from /find: identified + priced, not listed yet
]

VALID_FIELDS = [
    "identification", "pricing", "listing", "ebay", "photos", "processed",
    "price_source_url", "receipt", "photo_layout", "listing_check", "stock",
]


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


def connect():
    """A read-write connection to the bot's DB (WAL, busy timeout). The one
    way in for code outside db.py; close it when done. On SQL Server, a
    pyodbc connection to config.SQL_DATABASE (T-SQL, autocommit off)."""
    if _on_sqlserver():
        import sqlserver
        return sqlserver.connect()
    return _conn()


def connect_readonly(path=None):
    """A read-only connection: to the bot's DB, or to `path` (a backup copy).
    Writes through it fail, so reports can't change data by mistake. On SQL
    Server `path` may name another database; only SELECT/WITH statements run,
    and the connection never commits."""
    if _on_sqlserver():
        return _sqlserver().connect_readonly(path)
    p = Path(path or DB_PATH)
    con = sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True)
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
    """Bring the DB schema up to date (see migrations.py). On SQL Server the
    SQL Server steps, when a database is configured; no SQLite file is opened."""
    if _on_sqlserver():
        if config.SQL_DATABASE:
            import sqlserver
            con = sqlserver.connect()
            try:
                migrations.migrate_mssql(con)
            finally:
                con.close()
        return
    con = _conn()
    try:
        migrations.migrate(con)
    finally:
        con.close()


def create_item(photos: list[str]) -> str:
    if _on_sqlserver():
        return _sqlserver().create_item(photos)
    item_id = str(uuid.uuid4())
    now = _now()
    with _conn() as con:
        con.execute(
            "INSERT INTO items (item_id, status, photos, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            (item_id, "captured", json.dumps(_map_paths("photos", photos, stored_path)), now, now)
        )
    return item_id


def get_item(item_id: str) -> dict | None:
    if _on_sqlserver():
        return _sqlserver().get_item(item_id)
    with _conn() as con:
        cur = con.execute("SELECT * FROM items WHERE item_id = ?", (item_id,))
        row = cur.fetchone()
        if row is None:
            return None
        return _row_to_dict(row, cur)


def update_status(item_id: str, status: str) -> None:
    if _on_sqlserver():
        return _sqlserver().update_status(item_id, status)
    if status not in VALID_STATUSES:
        raise ValueError(f"Invalid status '{status}'. Must be one of: {VALID_STATUSES}")
    with _conn() as con:
        con.execute(
            "UPDATE items SET status = ?, updated_at = ? WHERE item_id = ?",
            (status, _now(), item_id)
        )


def update_field(item_id: str, field: str, data: dict) -> None:
    if _on_sqlserver():
        return _sqlserver().update_field(item_id, field, data)
    if field not in VALID_FIELDS:
        raise ValueError(f"Invalid field '{field}'. Must be one of: {VALID_FIELDS}")
    with _conn() as con:
        con.execute(
            f"UPDATE items SET {field} = ?, updated_at = ? WHERE item_id = ?",
            (json.dumps(_map_paths(field, data, stored_path)), _now(), item_id)
        )


def list_items(status: str | None = None) -> list[dict]:
    if _on_sqlserver():
        return _sqlserver().list_items(status)
    with _conn() as con:
        if status is not None:
            cur = con.execute("SELECT * FROM items WHERE status = ?", (status,))
        else:
            cur = con.execute("SELECT * FROM items")
        rows = cur.fetchall()
        return [_row_to_dict(row, cur) for row in rows]


def delete_item(item_id: str) -> None:
    if _on_sqlserver():
        return _sqlserver().delete_item(item_id)
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
    if _on_sqlserver():
        return _sqlserver().record_listing_stats(item_id, week, impressions=impressions, views=views, watchers=watchers, price=price, verdict=verdict, suggested_price=suggested_price, total_impressions=total_impressions, search_views=search_views)
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
    if _on_sqlserver():
        return _sqlserver().record_offer_sent(listing_id, offer_price, item_id=item_id, list_price=list_price, source=source)
    with _conn() as con:
        con.execute(
            "INSERT INTO offer_sends (item_id, listing_id, list_price, offer_price, source, "
            "sent_at) VALUES (?, ?, ?, ?, ?, ?)",
            (item_id, str(listing_id), list_price, offer_price, source, _now()))


def offers_sent_since(since_iso: str) -> set[str]:
    """Listing ids that had an offer to watchers sent at or after `since_iso`."""
    if _on_sqlserver():
        return _sqlserver().offers_sent_since(since_iso)
    with _conn() as con:
        return {row[0] for row in con.execute(
            "SELECT DISTINCT listing_id FROM offer_sends WHERE sent_at >= ?", (since_iso,))}


def save_offer_plan(rows: list[dict]) -> str:
    if _on_sqlserver():
        return _sqlserver().save_offer_plan(rows)
    plan_id = str(uuid.uuid4())
    with _conn() as con:
        con.execute("INSERT INTO offer_plans (plan_id, rows, created_at) VALUES (?, ?, ?)",
                    (plan_id, json.dumps(rows), _now()))
    return plan_id


def get_offer_plan(plan_id: str) -> dict | None:
    if _on_sqlserver():
        return _sqlserver().get_offer_plan(plan_id)
    with _conn() as con:
        row = con.execute("SELECT rows, created_at, used_at FROM offer_plans WHERE plan_id = ?",
                          (plan_id,)).fetchone()
    if row is None:
        return None
    return {"plan_id": plan_id, "rows": json.loads(row[0]), "created_at": row[1],
            "used_at": row[2]}


def claim_offer_plan(plan_id: str) -> bool:
    """Mark a plan used. True only for the one caller that got there first."""
    if _on_sqlserver():
        return _sqlserver().claim_offer_plan(plan_id)
    with _conn() as con:
        cur = con.execute("UPDATE offer_plans SET used_at = ? WHERE plan_id = ? "
                          "AND used_at IS NULL", (_now(), plan_id))
        return cur.rowcount == 1


def stats_history(item_id: str, limit: int = 8) -> list[dict]:
    """Most recent weekly snapshots for an item, newest week first."""
    if _on_sqlserver():
        return _sqlserver().stats_history(item_id, limit)
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


# --- /check pre-buy checks (docs/check-command-design.md, section 00) -----------
# Not items: one row per thing looked at in a shop. Both backends return the
# same dict: JSON columns decoded, money as float, exact as bool, times as UTC
# ISO strings.

CHECK_FIELDS = ("user_id", "ross_sku", "ross_sku_main", "ross_price", "photos", "identification", "mode", "exact",
                "verdict", "suggested_price", "profit", "roi", "break_even", "cheapest_identical",
                "sold_count", "comps_summary", "status", "status_at", "receipt_item_id", "item_id")
CHECK_JSON = ("photos", "identification", "comps_summary")
CHECK_STATUSES = ("checked", "bought", "skipped")


def _check_fields(fields: dict) -> dict:
    bad = set(fields) - set(CHECK_FIELDS)
    if bad:
        raise ValueError(f"unknown check field(s): {sorted(bad)}")
    if "status" in fields and fields["status"] not in CHECK_STATUSES:
        raise ValueError(f"bad check status {fields['status']!r}")
    return fields


def _check_row(row: dict | None) -> dict | None:
    if row is None:
        return None
    out = dict(row)
    for k in CHECK_JSON:
        if isinstance(out.get(k), str):
            out[k] = json.loads(out[k])
    if out.get("exact") is not None:
        out["exact"] = bool(out["exact"])
    return out


def add_check(**fields) -> int:
    """Save a new check; returns its check_id."""
    if _on_sqlserver():
        return _sqlserver().add_check(**fields)
    _check_fields(fields)
    now = _now()
    row = {k: (json.dumps(v) if k in CHECK_JSON and v is not None else v) for k, v in fields.items()}
    row.update(created_at=now, updated_at=now)
    with _conn() as con:
        cur = con.execute(f"INSERT INTO checks ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})",
                          tuple(row.values()))
        return cur.lastrowid


def update_check(check_id: int, **fields) -> None:
    """Change a check (a rerun's results, photos, status...)."""
    if _on_sqlserver():
        return _sqlserver().update_check(check_id, **fields)
    _check_fields(fields)
    if not fields:
        return
    row = {k: (json.dumps(v) if k in CHECK_JSON and v is not None else v) for k, v in fields.items()}
    row["updated_at"] = _now()
    with _conn() as con:
        con.execute(f"UPDATE checks SET {', '.join(f'{k} = ?' for k in row)} WHERE check_id = ?",
                    (*row.values(), check_id))


def set_check_status(check_id: int, status: str) -> None:
    update_check(check_id, status=status, status_at=_now())


def get_check(check_id: int) -> dict | None:
    if _on_sqlserver():
        return _sqlserver().get_check(check_id)
    with _conn() as con:
        con.row_factory = sqlite3.Row
        row = con.execute("SELECT * FROM checks WHERE check_id = ?", (check_id,)).fetchone()
    return _check_row(dict(row) if row else None)


def list_checks(*, user_id: int | None = None, status: str | None = None, sku: str | None = None,
                since: str | None = None, limit: int = 100) -> list[dict]:
    """Newest first. since: an ISO time (UTC)."""
    if _on_sqlserver():
        return _sqlserver().list_checks(user_id=user_id, status=status, sku=sku, since=since, limit=limit)
    where, args = [], []
    for col, val in (("user_id", user_id), ("status", status), ("ross_sku", sku)):
        if val is not None:
            where.append(f"{col} = ?")
            args.append(val)
    if since:
        where.append("created_at >= ?")
        args.append(datetime.fromisoformat(since.replace("Z", "+00:00")).astimezone(timezone.utc).isoformat())
    sql = "SELECT * FROM checks" + (" WHERE " + " AND ".join(where) if where else "") + \
          " ORDER BY check_id DESC LIMIT ?"
    with _conn() as con:
        con.row_factory = sqlite3.Row
        rows = con.execute(sql, (*args, int(limit))).fetchall()
    return [_check_row(dict(r)) for r in rows]


# --- check_evidence: price evidence, also the competitor-search cache ---------
# docs/check-command-design.md Part 2; pipeline/competitors.py.

EVIDENCE_COLUMNS = ("check_id", "item_id", "model_key", "source", "found_by", "store", "title",
                    "condition", "price", "shipping", "total", "url", "ebay_id", "category_id",
                    "photo_count", "returns_accepted", "best_offer", "specifics", "event_date",
                    "match", "match_by", "query", "fetched_at")
_EVIDENCE_BOOL = ("returns_accepted", "best_offer")
_EVIDENCE_MONEY = ("price", "shipping", "total")


def _evidence_check(row: dict) -> None:
    unknown = set(row) - set(EVIDENCE_COLUMNS)
    if unknown:
        raise ValueError(f"unknown check_evidence column(s): {sorted(unknown)}")
    if not row.get("model_key") or not row.get("source"):
        raise ValueError("check_evidence rows need a model_key and a source")


def _utc_iso(value) -> str | None:
    """An ISO time (or datetime) as UTC ISO text; naive means UTC."""
    if value is None or value == "":
        return None
    d = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    d = d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    return d.astimezone(timezone.utc).isoformat()


def _evidence_row(row: dict) -> dict:
    """One stored row as both backends return it: floats, bools, specifics as
    a dict, times as UTC ISO text."""
    out = dict(row)
    if isinstance(out.get("specifics"), str):
        out["specifics"] = json.loads(out["specifics"])
    for k in _EVIDENCE_BOOL:
        if out.get(k) is not None:
            out[k] = bool(out[k])
    for k in _EVIDENCE_MONEY:
        if out.get(k) is not None:
            out[k] = float(out[k])
    return out


def add_evidence(rows) -> int:
    """Save evidence rows (dicts with EVIDENCE_COLUMNS keys; model_key and
    source required). fetched_at defaults to now. Returns how many."""
    rows = [{k: v for k, v in r.items() if v is not None} for r in rows]
    for r in rows:
        _evidence_check(r)
    if not rows:
        return 0
    if _on_sqlserver():
        return _sqlserver().add_evidence(rows)
    now = _now()
    with _conn() as con:
        for r in rows:
            r = dict(r)
            r.setdefault("fetched_at", now)
            for k in ("event_date", "fetched_at"):
                if k in r:
                    r[k] = _utc_iso(r[k])
            if "specifics" in r:
                r["specifics"] = json.dumps(r["specifics"])
            for k in _EVIDENCE_BOOL:
                if k in r:
                    r[k] = int(bool(r[k]))
            con.execute(f"INSERT INTO check_evidence ({', '.join(r)}) VALUES ({', '.join('?' * len(r))})",
                        tuple(r.values()))
    return len(rows)


def recent_evidence(model_key: str, source: str, since) -> list[dict]:
    """Rows for a model and source fetched at or after `since` (ISO time or
    datetime), oldest first."""
    since = _utc_iso(since)
    if _on_sqlserver():
        return _sqlserver().recent_evidence(model_key, source, since)
    with _conn() as con:
        con.row_factory = sqlite3.Row
        rows = con.execute("SELECT * FROM check_evidence WHERE model_key = ? AND source = ? "
                           "AND fetched_at >= ? ORDER BY evidence_id", (model_key, source, since)).fetchall()
    return [_evidence_row(dict(r)) for r in rows]


# --- eBay listing quality report (pipeline/quality_report.py) ---------------------

QR_SECTION_COLUMNS = (
    "section", "conditions", "sellers", "gmv_rank", "impressions_per_day", "impressions_rank", "ctr",
    "ctr_rank", "conversion", "conversion_rank", "top_impressions_per_day", "bottom_impressions_per_day",
    "top_ctr", "bottom_ctr", "top_conversion", "bottom_conversion", "recommendations", "benchmarks")
QR_LISTING_COLUMNS = (
    "section", "ebay_item_id", "item_id", "title", "price", "shipping_fee", "free_shipping",
    "impressions_per_day", "ctr", "conversion", "ad_rate", "specifics_filled", "specifics_to_add",
    "watchers", "age_days", "sales_31d", "raw")
_QR_JSON = {"recommendations", "benchmarks", "raw"}
_QR_FLOAT = {"impressions_per_day", "ctr", "conversion", "top_impressions_per_day",
             "bottom_impressions_per_day", "top_ctr", "bottom_ctr", "top_conversion", "bottom_conversion",
             "price", "shipping_fee", "ad_rate"}


def _qr_rows(report: dict) -> tuple[str, list[dict], list[dict]]:
    """(report_date as UTC ISO, section rows, listing rows): only the stored
    columns, JSON columns as text."""
    date = _utc_iso(report["report_date"])
    if not date:
        raise ValueError("a quality report needs its report_date")

    def pick(row, cols):
        out = {}
        for c in cols:
            v = row.get(c)
            if v is None:
                continue
            if c in _QR_JSON:
                v = json.dumps(v, ensure_ascii=False, default=str)
            elif c == "free_shipping":
                v = int(bool(v))
            out[c] = v
        return out
    return (date, [pick(s, QR_SECTION_COLUMNS) for s in report.get("sections") or []],
            [pick(x, QR_LISTING_COLUMNS) for x in report.get("listings") or []])


def _qr_decode(row: dict) -> dict:
    out = dict(row)
    for k in _QR_JSON:
        if isinstance(out.get(k), str):
            out[k] = json.loads(out[k])
    for k in _QR_FLOAT:
        if out.get(k) is not None:
            out[k] = float(out[k])
    if out.get("free_shipping") is not None:
        out["free_shipping"] = bool(out["free_shipping"])
    for k in ("report_date", "ingested_at"):
        if out.get(k) is not None:
            out[k] = _utc_iso(out[k])
    return out


def save_quality_report(report: dict) -> dict:
    """Store a parsed report (pipeline/quality_report.parse). The same report
    sent again replaces its own rows, in one transaction. Returns
    {"sections": n, "listings": n, "replaced": bool}."""
    date, sections, listings = _qr_rows(report)
    if _on_sqlserver():
        return _sqlserver().save_quality_report(date, sections, listings)
    now = _now()
    with _conn() as con:
        replaced = con.execute("SELECT COUNT(*) FROM quality_report_sections WHERE report_date = ?",
                               (date,)).fetchone()[0] > 0
        con.execute("DELETE FROM quality_report_sections WHERE report_date = ?", (date,))
        con.execute("DELETE FROM quality_report_listings WHERE report_date = ?", (date,))
        for table, rows in (("quality_report_sections", sections), ("quality_report_listings", listings)):
            for r in rows:
                r = {"report_date": date, **r, "ingested_at": now}
                con.execute(f"INSERT INTO {table} ({', '.join(r)}) VALUES ({', '.join('?' * len(r))})",
                            tuple(r.values()))
    return {"sections": len(sections), "listings": len(listings), "replaced": replaced}


def latest_quality_report() -> dict | None:
    """The newest stored report: {"report_date": UTC ISO, "sections":
    [...], "listings": [...]}, or None when none was sent yet."""
    if _on_sqlserver():
        got = _sqlserver().latest_quality_report()
    else:
        with _conn() as con:
            con.row_factory = sqlite3.Row
            date = con.execute("SELECT MAX(report_date) FROM quality_report_sections").fetchone()[0]
            if not date:
                return None
            got = {"report_date": date,
                   "sections": [dict(r) for r in con.execute(
                       "SELECT * FROM quality_report_sections WHERE report_date = ? ORDER BY section_row_id",
                       (date,))],
                   "listings": [dict(r) for r in con.execute(
                       "SELECT * FROM quality_report_listings WHERE report_date = ? ORDER BY listing_row_id",
                       (date,))]}
    if not got:
        return None
    return {"report_date": _utc_iso(got["report_date"]),
            "sections": [_qr_decode(r) for r in got["sections"]],
            "listings": [_qr_decode(r) for r in got["listings"]]}


# --- the old eBay store's sales (pipeline/old_store.py) ---------------------------

OLD_STORE_COLUMNS = (
    "source", "source_tab", "source_row", "period_start", "period_end", "year_inferred",
    "title", "ebay_item_id", "quantity", "item_sales", "unit_price", "shipping_per_unit",
    "label_per_unit", "fees_per_unit", "net_per_unit", "price_paid", "cost_per_unit",
    "best_offer_units", "seller_offer_units", "status", "note",
    "brand", "parent_brand", "item_type", "bag_size", "msrp", "msrp_from")
_OS_FLOAT = {"item_sales", "unit_price", "shipping_per_unit", "label_per_unit", "fees_per_unit",
             "net_per_unit", "price_paid", "cost_per_unit", "msrp"}


def replace_old_store_sales(rows: list[dict]) -> dict:
    """Replace every old_store row with `rows` (dicts keyed by
    OLD_STORE_COLUMNS), in one transaction. Returns {"rows": n, "replaced": n}."""
    rows = [{c: r.get(c) for c in OLD_STORE_COLUMNS} for r in rows]
    if _on_sqlserver():
        return _sqlserver().replace_old_store_sales(rows)
    now = _now()
    with _conn() as con:
        replaced = con.execute("SELECT COUNT(*) FROM old_store_sales").fetchone()[0]
        con.execute("DELETE FROM old_store_sales")
        cols = (*OLD_STORE_COLUMNS, "imported_at")
        con.executemany(f"INSERT INTO old_store_sales ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                        [(*r.values(), now) for r in rows])
    return {"rows": len(rows), "replaced": replaced}


def old_store_sales(since: str | None = None) -> list[dict]:
    """Old store rows whose period ends on or after `since` (ISO date; None =
    all), oldest first. Dates as ISO strings, money as floats. Read only."""
    if _on_sqlserver():
        got = _sqlserver().old_store_sales(since)
    else:
        with _conn() as con:
            con.row_factory = sqlite3.Row
            sql = "SELECT * FROM old_store_sales"
            got = [dict(r) for r in (con.execute(sql + " WHERE period_end >= ? ORDER BY period_start, row_id",
                                                 (since,)) if since
                                     else con.execute(sql + " ORDER BY period_start, row_id"))]
    out = []
    for r in got:
        r = dict(r)
        for k in ("period_start", "period_end"):
            if r.get(k) is not None and not isinstance(r[k], str):
                r[k] = r[k].isoformat()
        for k in _OS_FLOAT:
            if r.get(k) is not None:
                r[k] = float(r[k])
        r["year_inferred"] = bool(r.get("year_inferred"))
        r.pop("imported_at", None)
        out.append(r)
    return out


def receipt_lines_for_skus(skus) -> list[dict]:
    """Ross receipt lines (what we bought) with one of these 12-digit SKUs,
    newest first: line_id, sku, description, price, receipt_date (date),
    receipt_number, store_city. Read only. Receipts are only on SQL Server
    (ResaleDB.Receipts/ReceiptItems); on SQLite there are none: []."""
    skus = sorted({str(s) for s in skus or () if s})
    if not skus:
        return []
    if _on_sqlserver():
        return _sqlserver().receipt_lines_for_skus(skus)
    return []


# --- receipt line decoding and /rcpt (docs/receipt-decoding.md) -----------------
# Receipts are only on SQL Server. On SQLite the reads are empty and the one
# write (receipt_line_details) refuses.

RLD_SOURCES = migrations.RLD_SOURCES


def receipt_decode_inputs() -> dict:
    """Every receipt line, for the decoder: {"lines": [{line_id, receipt_id,
    line_index, sku, description, desc_key, price, receipt_number,
    receipt_date, receipt_time, store_number, store_city, subtotal, total,
    tax, same_code_prices, line_count, lines_sum, fixed}], "details":
    {desc_key: saved receipt_line_details row}, "has_table": bool}.
    desc_key = UPPER(LTRIM(RTRIM(Description))), made by SQL. Read only."""
    if _on_sqlserver():
        return _sqlserver().receipt_decode_inputs()
    return {"lines": [], "details": {}, "has_table": False}


def save_receipt_line_details(rows) -> int:
    """Insert or update receipt_line_details rows by desc_key (the only table
    this writes). Each row: desc_key + category, item_type, gender, color,
    brand_guess, product_model, confidence, source, model, item_id,
    decode_raw (a dict). Returns how many."""
    rows = list(rows)
    for r in rows:
        if not str(r.get("desc_key") or "").strip():
            raise ValueError("receipt_line_details row without a desc_key")
        if r.get("source") not in RLD_SOURCES:
            raise ValueError(f"bad receipt_line_details source {r.get('source')!r}")
    if _on_sqlserver():
        return _sqlserver().save_receipt_line_details(rows)
    raise RuntimeError("receipt_line_details is on SQL Server only")


def receipt_lines_search(kind: str, value, limit: int = 10) -> dict:
    """/rcpt: receipt lines, newest first (a whole receipt: in line order).
    kind: receipt_id | receipt_number | receipt_number_part | sku |
    sku_suffix | words (value: [{"text", "codes", "gender"}], all must match).
    Returns {"total", "lines" (line fields + d_<column> of its saved decode),
    "receipt" (the header, for a whole receipt), "store_rows"
    [(store, city, n)]}. Read only."""
    if _on_sqlserver():
        return _sqlserver().receipt_lines_search(kind, value, limit)
    return {"total": 0, "lines": [], "receipt": None, "store_rows": []}



def record_job_run(job: str, status: str = "ok", detail: str | None = None) -> None:
    """Stamp a scheduled job as having just completed. Called after the work,
    not before, so a crash mid-run leaves the old timestamp and the job retries
    on the next start rather than believing it already ran.

    A status other than "ok" (e.g. "partial": the weekly price check ran but
    some traffic batches failed) records the attempt without counting it as
    completed — last_job_run ignores it, so the slot is still caught up."""
    if _on_sqlserver():
        return _sqlserver().record_job_run(job, status, detail)
    with _conn() as con:
        con.execute(
            "INSERT INTO job_runs (job, last_run_at, status, detail) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(job) DO UPDATE SET last_run_at=excluded.last_run_at, "
            "status=excluded.status, detail=excluded.detail",
            (job, _now(), status, detail),
        )


def known_feedback_failures() -> set[str]:
    """Order lines whose feedback failure has already been reported."""
    if _on_sqlserver():
        return _sqlserver().known_feedback_failures()
    with _conn() as con:
        return {r[0] for r in con.execute("SELECT order_line FROM feedback_failures")}


def record_feedback_failure(order_line: str, error: str) -> None:
    if _on_sqlserver():
        return _sqlserver().record_feedback_failure(order_line, error)
    with _conn() as con:
        con.execute(
            "INSERT INTO feedback_failures (order_line, first_failed, last_error) "
            "VALUES (?, ?, ?) ON CONFLICT(order_line) DO UPDATE SET last_error=excluded.last_error",
            (order_line, _now(), error))


def clear_feedback_failure(order_line: str) -> None:
    if _on_sqlserver():
        return _sqlserver().clear_feedback_failure(order_line)
    with _conn() as con:
        con.execute("DELETE FROM feedback_failures WHERE order_line = ?", (order_line,))


def mark_not_duplicate(item_a: str, item_b: str) -> None:
    if _on_sqlserver():
        return _sqlserver().mark_not_duplicate(item_a, item_b)
    a, b = sorted((item_a, item_b))
    with _conn() as con:
        con.execute("INSERT OR IGNORE INTO not_duplicates (a, b) VALUES (?, ?)", (a, b))


def not_duplicate_pairs() -> set[tuple[str, str]]:
    if _on_sqlserver():
        return _sqlserver().not_duplicate_pairs()
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
    if _on_sqlserver():
        return _sqlserver().next_piece_code(box)
    prefix = f"{box.upper()}-"
    with _conn() as con:
        codes = [r[0] for r in con.execute(
            "SELECT code FROM pieces WHERE code LIKE ?", (prefix + "%",))]
    nums = [int(c[len(prefix):]) for c in codes if c[len(prefix):].isdigit()]
    return f"{prefix}{(max(nums) + 1) if nums else 1:02d}"


def add_piece(box: str, photo: str | None = None) -> str:
    """Reserve the next code in `box` and store the piece. Retries if two
    photos race for the same number."""
    if _on_sqlserver():
        return _sqlserver().add_piece(box, photo)
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
    if _on_sqlserver():
        return _sqlserver().update_piece(code, **fields)
    if "info" in fields:
        fields["info"] = json.dumps(fields["info"])
    if fields.get("photo"):
        fields["photo"] = stored_path(fields["photo"])
    sets = ", ".join(f"{k} = ?" for k in fields) + ", updated_at = ?"
    with _conn() as con:
        con.execute(f"UPDATE pieces SET {sets} WHERE code = ?",
                    (*fields.values(), _now(), code))


def get_piece(code: str) -> dict | None:
    if _on_sqlserver():
        return _sqlserver().get_piece(code)
    with _conn() as con:
        cur = con.execute("SELECT * FROM pieces WHERE code = ?", (code.upper(),))
        row = cur.fetchone()
        return _piece_row(row, cur) if row else None


def list_pieces(*, box: str | None = None, item_id: str | None = None,
                status: str | None = None) -> list[dict]:
    if _on_sqlserver():
        return _sqlserver().list_pieces(box=box, item_id=item_id, status=status)
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
    if _on_sqlserver():
        return _sqlserver().seen_order_ids()
    with _conn() as con:
        return {r[0] for r in con.execute("SELECT order_id FROM seen_orders")}


def mark_orders_seen(order_ids) -> None:
    if _on_sqlserver():
        return _sqlserver().mark_orders_seen(order_ids)
    with _conn() as con:
        con.executemany("INSERT OR IGNORE INTO seen_orders (order_id, seen_at) VALUES (?, ?)",
                        [(o, _now()) for o in order_ids])


def job_run_status(job: str) -> str | None:
    """The status the last attempt at `job` recorded ("ok", "partial", ...), or
    None if it never recorded one."""
    if _on_sqlserver():
        return _sqlserver().job_run_status(job)
    with _conn() as con:
        row = con.execute(
            "SELECT status FROM job_runs WHERE job = ?", (job,)
        ).fetchone()
    return row[0] if row else None


def last_job_run(job: str) -> datetime | None:
    """When `job` last completed, as an aware UTC datetime, or None if never.
    An attempt recorded with a non-ok status didn't complete, so it reads as
    None too (NULL status is a legacy ok row)."""
    if _on_sqlserver():
        return _sqlserver().last_job_run(job)
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
    if _on_sqlserver():
        return _sqlserver().record_llm_usage(provider=provider, model=model, stage=stage, item_id=item_id, input_tokens=input_tokens, output_tokens=output_tokens, cache_read_tokens=cache_read_tokens, cache_write_tokens=cache_write_tokens, web_searches=web_searches, cost_usd=cost_usd, ts=ts)
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
    if _on_sqlserver():
        return _sqlserver().llm_spend_since(since_iso, provider)
    sql = "SELECT COALESCE(SUM(cost_usd), 0) FROM llm_usage WHERE ts >= ?"
    args: list = [since_iso]
    if provider is not None:
        sql += " AND provider = ?"
        args.append(provider)
    with _conn() as con:
        return float(con.execute(sql, args).fetchone()[0])


def llm_usage_breakdown(since_iso: str) -> list[dict]:
    """Spend grouped by provider, model and stage from since_iso onward, dearest first."""
    if _on_sqlserver():
        return _sqlserver().llm_usage_breakdown(since_iso)
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


# --- small item queries (moved from telegram_bot.py, cleanup-plan Phase 2) ---

def set_location(item_id: str, loc: str | None) -> None:
    if not loc:
        return
    item = get_item(item_id) or {}
    stock = dict(item.get("stock") or {})
    stock["location"] = loc
    update_field(item_id, "stock", stock)


# Real Ross tag codes are full 12-digit numbers (≥ 1e11). Generated placeholder
# codes count up from 0 and stay far below that ceiling, so a generated code can
# never collide with a real barcode no matter how many we mint.
REAL_CODE_FLOOR = 100_000_000_000  # smallest true 12-digit code (1e11)


def next_receipt_code() -> str:
    """Next synthetic 12-digit Ross code, for an item you paid for but have no tag
    code on hand. Continues the PLACEHOLDER sequence from the highest placeholder
    on file (+1) — deliberately ignoring real barcodes, so it counts up 0, 1, 2…
    in its own low range and can't ever match a real code. Zero-padded to 12
    digits; starts at 0 when no placeholder has been recorded yet."""
    highest = -1
    for it in list_items():
        c = (it.get("receipt") or {}).get("code")
        digits = re.sub(r"\D", "", str(c)) if c else ""
        if digits:
            value = int(digits)
            if value < REAL_CODE_FLOOR:
                highest = max(highest, value)
    return str(highest + 1).zfill(12)


def match_quantity_to_pieces(item_id: str) -> None:
    """Before a stocked item is drafted, list as many as were found at home."""
    n = len(list_pieces(item_id=item_id, status="home"))
    if n > 1:
        item = get_item(item_id) or {}
        update_field(item_id, "ebay", {**(item.get("ebay") or {}), "quantity": n})


# --- moved here from ebay/, pipeline/ and scripts/ (Q13) --------------------
# The SQL those modules ran themselves. Same results as before on SQLite; on
# SQL Server the same functions run on ResaleDB, so no caller opens a SQLite
# file.

def save_ebay_token(access_token: str, refresh_token: str | None, expires_at: float) -> None:
    """Store the eBay user token (ebay/auth.py). Without a new refresh token
    the stored one is kept. expires_at is epoch seconds."""
    if _on_sqlserver():
        return _sqlserver().save_ebay_token(access_token, refresh_token, expires_at)
    with _conn() as con:
        if refresh_token:
            con.execute(
                "INSERT INTO ebay_tokens (id, access_token, refresh_token, expires_at) "
                "VALUES (1, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET "
                "access_token = excluded.access_token, "
                "refresh_token = excluded.refresh_token, "
                "expires_at = excluded.expires_at",
                (access_token, refresh_token, expires_at),
            )
        else:
            con.execute(
                "INSERT INTO ebay_tokens (id, access_token, refresh_token, expires_at) "
                "VALUES (1, ?, NULL, ?) "
                "ON CONFLICT(id) DO UPDATE SET "
                "access_token = excluded.access_token, "
                "expires_at = excluded.expires_at",
                (access_token, expires_at),
            )


def load_ebay_token() -> dict | None:
    """{access_token, refresh_token, expires_at (epoch seconds)} or None."""
    if _on_sqlserver():
        return _sqlserver().load_ebay_token()
    with _conn() as con:
        row = con.execute(
            "SELECT access_token, refresh_token, expires_at FROM ebay_tokens WHERE id = 1"
        ).fetchone()
    if row is None:
        return None
    return {"access_token": row[0], "refresh_token": row[1], "expires_at": row[2]}


def taxonomy_cache_get(key: str):
    """(cached value, fetched_at epoch seconds) or None (ebay/taxonomy.py
    applies its own TTL)."""
    if _on_sqlserver():
        return _sqlserver().taxonomy_cache_get(key)
    with _conn() as con:
        row = con.execute(
            "SELECT payload, fetched_at FROM taxonomy_cache WHERE cache_key = ?", (key,)
        ).fetchone()
    if row is None:
        return None
    return json.loads(row[0]), row[1]


def taxonomy_cache_set(key: str, value, fetched_at: float) -> None:
    if _on_sqlserver():
        return _sqlserver().taxonomy_cache_set(key, value, fetched_at)
    with _conn() as con:
        con.execute(
            "INSERT INTO taxonomy_cache (cache_key, payload, fetched_at) VALUES (?, ?, ?) "
            "ON CONFLICT(cache_key) DO UPDATE SET "
            "payload = excluded.payload, fetched_at = excluded.fetched_at",
            (key, json.dumps(value), fetched_at),
        )


def own_listings() -> list[tuple]:
    """(eBay listing id, listing title) for every item, either may be None
    (pipeline/pricing_v2.own_listings builds its sets from these)."""
    return [((it.get("ebay") or {}).get("listing_id"), (it.get("listing") or {}).get("title"))
            for it in list_items()]


def sold_items_for_calibration() -> list[dict]:
    """Sold items as {item_id, identification, ebay, receipt}, by item_id
    (pricing_v2's calibration examples, msrp's sold history)."""
    return [{"item_id": it["item_id"], "identification": it.get("identification"),
             "ebay": it.get("ebay"), "receipt": it.get("receipt")}
            for it in sorted(list_items("sold"), key=lambda it: it["item_id"])]


def llm_stage_spend(since_iso: str, stage: str, item_id: str | None = None) -> tuple[float, int]:
    """(cost USD, calls) for one stage since since_iso, optionally one item."""
    if _on_sqlserver():
        return _sqlserver().llm_stage_spend(since_iso, stage, item_id)
    sql = "SELECT COALESCE(SUM(cost_usd),0), COUNT(*) FROM llm_usage WHERE ts >= ? AND stage = ?"
    args = [since_iso, stage]
    if item_id:
        sql += " AND item_id = ?"
        args.append(item_id)
    with _conn() as con:
        cost, calls = con.execute(sql, args).fetchone()
    return float(cost), int(calls)


def snapshot(label: str) -> str:
    """A labelled backup before a risky write (scripts/backfill_sales.py).
    SQLite: the online backup API (WAL-safe) to data/backups/ross-<time>-<label>.db.
    SQL Server: BACKUP DATABASE ... COPY_ONLY to SQL_BACKUP_DIR (sqlserver.py).
    Returns where it went."""
    if _on_sqlserver():
        import sqlserver
        return sqlserver.backup_database(config.SQL_DATABASE, label=label)
    dest_dir = Path(DATA_DIR) / "backups"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"ross-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{label}.db"
    src, dst = _conn(), sqlite3.connect(str(dest))
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    return str(dest)


def modify_field(item_id: str, field: str, change) -> bool:
    """Atomic read-modify-write of one item field: change(value) gets the
    current value (as get_item returns it) and returns the new one, or None to
    leave the item alone. True if written. Nothing else can write the item in
    between (SQLite: BEGIN IMMEDIATE; SQL Server: the item row's UPDLOCK)."""
    if field not in VALID_FIELDS:
        raise ValueError(f"Invalid field '{field}'. Must be one of: {VALID_FIELDS}")
    if _on_sqlserver():
        return _sqlserver().modify_field(item_id, field, change)
    con = _conn()
    con.execute("PRAGMA busy_timeout=30000")
    con.isolation_level = None
    try:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute(f"SELECT {field} FROM items WHERE item_id = ?", (item_id,)).fetchone()
        if row is None:
            con.execute("ROLLBACK")
            return False
        current = _map_paths(field, json.loads(row[0]), _resolved) if row[0] is not None else None
        new = change(current)
        if new is None:
            con.execute("ROLLBACK")
            return False
        con.execute(f"UPDATE items SET {field} = ?, updated_at = ? WHERE item_id = ?",
                    (json.dumps(_map_paths(field, new, stored_path)), _now(), item_id))
        con.execute("COMMIT")
        return True
    except BaseException:
        if con.in_transaction:
            con.execute("ROLLBACK")
        raise
    finally:
        con.close()
