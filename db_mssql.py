"""db.py's read functions on SQL Server (ResaleDB). Used when
config.DB_BACKEND == "sqlserver"; db.py dispatches here.

Each function returns exactly what its SQLite twin in db.py returns: the same
dict shapes, keys, types and order. dbmap.join_item rebuilds an item from its
rows; db.py's own path mapping is applied on the way out, as for SQLite.
Writes are not here yet (migration step 2b).
"""
import json
from datetime import timezone

import db
import dbmap
import sqlserver

_RAW = ("extra_raw", "identification_raw", "pricing_raw", "ebay_raw", "comp_raw", "info_raw")
_JSON_FIELDS = ("photos", "processed", "identification", "pricing", "listing", "ebay",
                "price_source_url", "receipt", "photo_layout", "listing_check", "stock")


def _dicts(cur) -> list[dict]:
    cols = [d[0] for d in cur.description]
    out = []
    for r in cur.fetchall():
        d = dict(zip(cols, r))
        for c in _RAW:
            if d.get(c) is not None:
                d[c] = json.loads(d[c])
        out.append(d)
    return out


def _query(sql: str, *params) -> list[dict]:
    con = sqlserver.connect()
    try:
        cur = con.cursor()
        cur.execute(sql, *params)
        return _dicts(cur)
    finally:
        con.close()


def _items(where: str, params: tuple) -> list[dict]:
    """Every item matching `where` (a predicate on item_id), in seq order."""
    con = sqlserver.connect()
    try:
        cur = con.cursor()

        def q(sql):
            cur.execute(sql.format(w=where), *params)
            return _dicts(cur)

        items = q("SELECT * FROM dbo.items WHERE {w} ORDER BY seq")
        if not items:
            return []
        latest_runs = "run_id IN (SELECT latest_run_id FROM dbo.item_pricing WHERE {w})"
        tables = {
            "photos": q("SELECT * FROM dbo.item_photos WHERE {w}"),
            "identification": q("SELECT * FROM dbo.item_identification WHERE {w}"),
            "flags": q("SELECT * FROM dbo.item_flags WHERE {w}"),
            "pricing": q("SELECT * FROM dbo.item_pricing WHERE {w}"),
            "run": q("SELECT * FROM dbo.pricing_runs WHERE " + latest_runs),
            "comps": q("SELECT c.*, r.item_id FROM dbo.pricing_comps c "
                       "JOIN dbo.pricing_runs r ON r.run_id = c.run_id WHERE c." + latest_runs),
            "listing": q("SELECT * FROM dbo.item_listing WHERE {w}"),
            "specifics": q("SELECT * FROM dbo.listing_specifics WHERE {w}"),
            "ebay": q("SELECT * FROM dbo.ebay_listings WHERE {w}"),
            "images": q("SELECT * FROM dbo.listing_images WHERE {w}"),
            "sales": q("SELECT * FROM dbo.sales WHERE {w}"),
            "tag": q("SELECT * FROM dbo.item_tags WHERE {w}"),
            "check": q("SELECT * FROM (SELECT *, ROW_NUMBER() OVER (PARTITION BY item_id "
                       "ORDER BY checked_at DESC, check_id DESC) AS rn FROM dbo.listing_checks "
                       "WHERE {w}) x WHERE rn = 1"),
            "pieces": q("SELECT * FROM dbo.pieces WHERE {w}"),
        }
        check_ids = {r["check_id"]: r["item_id"] for r in tables["check"]}
        issues = q("SELECT * FROM dbo.listing_check_issues WHERE check_id IN "
                   "(SELECT check_id FROM dbo.listing_checks WHERE {w})")
        tables["issues"] = [dict(r, item_id=check_ids[r["check_id"]]) for r in issues
                            if r["check_id"] in check_ids]
    finally:
        con.close()

    one = ("identification", "pricing", "run", "listing", "ebay", "tag", "check")
    by_item = {it["item_id"]: {"item": it} for it in items}
    for name, rows in tables.items():
        for r in rows:
            parts = by_item.get(r["item_id"])
            if parts is None:
                continue
            if name in one:
                parts[name] = r
            else:
                parts.setdefault(name, []).append(r)
    out = []
    for it in items:
        d = dbmap.join_item(by_item[it["item_id"]])
        for field in _JSON_FIELDS:
            if d.get(field) is not None:
                d[field] = db._map_paths(field, d[field], db._resolved)
        out.append(d)
    return out


def get_item(item_id: str) -> dict | None:
    found = _items("item_id = ?", (item_id,))
    return found[0] if found else None


def list_items(status: str | None = None) -> list[dict]:
    if status is None:
        return _items("1 = 1", ())
    return _items("item_id IN (SELECT item_id FROM dbo.items WHERE status = ?)", (status,))


def _since(since_iso: str):
    dt = dbmap._parse_ts(since_iso)
    if dt is None:
        raise ValueError(f"not an ISO timestamp: {since_iso!r}")
    return dt


def offers_sent_since(since_iso: str) -> set[str]:
    rows = _query("SELECT DISTINCT listing_id FROM dbo.offer_sends WHERE sent_at >= ?", _since(since_iso))
    return {r["listing_id"] for r in rows}


PLAN_ROW = {"item_id": ("item_id", dbmap.S(36, False)), "listing_id": ("listing_id", dbmap.S(20, False)),
            "price": ("price", dbmap.DEC(10, 2)), "offer_price": ("offer_price", dbmap.DEC(10, 2)),
            "pct": ("pct", dbmap.DEC(5, 2)), "watchers": ("watchers", dbmap.INT),
            "title": ("title", dbmap.S(80))}


def get_offer_plan(plan_id: str) -> dict | None:
    plans = _query("SELECT * FROM dbo.offer_plans WHERE plan_id = ?", plan_id)
    if not plans:
        return None
    rows = _query("SELECT * FROM dbo.offer_plan_rows WHERE plan_id = ? ORDER BY position", plan_id)
    plan = plans[0]
    return {"plan_id": plan_id, "rows": [dbmap.join_cols(r, PLAN_ROW) for r in rows],
            "created_at": dbmap.from_db(plan["created_at"], dbmap.TS),
            "used_at": dbmap.from_db(plan["used_at"], dbmap.TS)}


STATS = {"impressions": dbmap.INT, "views": dbmap.INT, "watchers": dbmap.INT,
         "price": dbmap.DEC(10, 2), "verdict": dbmap.S(30), "suggested_price": dbmap.DEC(10, 2),
         "created_at": dbmap.TS, "total_impressions": dbmap.INT, "search_views": dbmap.INT}
# The SQLite table's column order, which stats_history's dicts follow.
STATS_ORDER = ("item_id", "week", "impressions", "views", "watchers", "price", "verdict",
               "suggested_price", "created_at", "total_impressions", "search_views")


def stats_history(item_id: str, limit: int = 8) -> list[dict]:
    rows = _query(f"SELECT TOP ({int(limit)}) * FROM dbo.listing_stats WHERE item_id = ? "
                  "ORDER BY week DESC", item_id)
    out = []
    for r in rows:
        d = {"item_id": r["item_id"], "week": dbmap.from_db(r["week"], dbmap.DATE)}
        d.update({k: dbmap.from_db(r[k], kind) for k, kind in STATS.items()})
        out.append({k: d[k] for k in STATS_ORDER})
    return out


def known_feedback_failures() -> set[str]:
    return {r["order_line"] for r in _query("SELECT order_line FROM dbo.feedback_failures")}


def not_duplicate_pairs() -> set[tuple[str, str]]:
    return {(r["a"], r["b"]) for r in _query("SELECT a, b FROM dbo.not_duplicates")}


def _like_prefix(prefix: str) -> str:
    return (prefix.replace("[", "[[]").replace("%", "[%]").replace("_", "[_]")) + "%"


def next_piece_code(box: str) -> str:
    prefix = f"{box.upper()}-"
    codes = [r["code"] for r in _query("SELECT code FROM dbo.pieces WHERE code LIKE ?",
                                       _like_prefix(prefix))]
    nums = [int(c[len(prefix):]) for c in codes if c[len(prefix):].isdigit()]
    return f"{prefix}{(max(nums) + 1) if nums else 1:02d}"


def _piece(row: dict) -> dict:
    p = dbmap.join_piece(row)
    if p.get("photo"):
        p["photo"] = db._resolved(p["photo"])
    return p


def get_piece(code: str) -> dict | None:
    rows = _query("SELECT * FROM dbo.pieces WHERE code = ?", code.upper())
    return _piece(rows[0]) if rows else None


def list_pieces(*, box=None, item_id=None, status=None) -> list[dict]:
    where, args = [], []
    for col, val in (("location_code", box), ("item_id", item_id), ("status", status)):
        if val is not None:
            where.append(f"{col} = ?")
            args.append(val.upper() if col == "location_code" else val)
    sql = "SELECT * FROM dbo.pieces" + (" WHERE " + " AND ".join(where) if where else "")
    # Number after the first dash, as SQLite's CAST does (0 if not a number),
    # so G3-100 sorts after G3-11; code breaks ties.
    sql += (" ORDER BY location_code, ISNULL(TRY_CAST(SUBSTRING(code, CHARINDEX('-', code) + 1, 50)"
            " AS BIGINT), 0), code")
    return [_piece(r) for r in _query(sql, *args)]


def seen_order_ids() -> set[str]:
    return {r["order_id"] for r in _query("SELECT order_id FROM dbo.seen_orders")}


def job_run_status(job: str) -> str | None:
    rows = _query("SELECT status FROM dbo.job_runs WHERE job = ?", job)
    return rows[0]["status"] if rows else None


def last_job_run(job: str):
    rows = _query("SELECT last_run_at FROM dbo.job_runs WHERE job = ? "
                  "AND (status IS NULL OR status = 'ok')", job)
    if not rows or rows[0]["last_run_at"] is None:
        return None
    return rows[0]["last_run_at"].replace(tzinfo=timezone.utc)


def llm_spend_since(since_iso: str, provider: str | None = None) -> float:
    sql = "SELECT COALESCE(SUM(cost_usd), 0) AS total FROM dbo.llm_usage WHERE ts >= ?"
    args = [_since(since_iso)]
    if provider is not None:
        sql += " AND provider = ?"
        args.append(provider)
    return float(_query(sql, *args)[0]["total"])


def llm_usage_breakdown(since_iso: str) -> list[dict]:
    rows = _query(
        "SELECT provider, model, COALESCE(stage, '-') AS stage, COUNT(*) AS calls, "
        "SUM(input_tokens) AS input_tokens, SUM(output_tokens) AS output_tokens, "
        "SUM(cache_read_tokens) AS cache_read_tokens, SUM(web_searches) AS web_searches, "
        "SUM(cost_usd) AS cost_usd, SUM(CASE WHEN cost_usd IS NULL THEN 1 ELSE 0 END) AS unpriced "
        "FROM dbo.llm_usage WHERE ts >= ? GROUP BY provider, model, stage "
        "ORDER BY COALESCE(SUM(cost_usd), 0) DESC", _since(since_iso))
    for r in rows:
        r["cost_usd"] = float(r["cost_usd"]) if r["cost_usd"] is not None else None
    return rows
