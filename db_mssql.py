"""db.py on SQL Server (ResaleDB). Used when config.DB_BACKEND == "sqlserver";
db.py dispatches every public function here.

Each function returns exactly what its SQLite twin in db.py returns: same dict
shapes, keys, types and order (dbmap.join_item rebuilds an item from its rows,
db.py's path mapping is applied on the way out).

Writes (docs/schema-proposal.md, section 5):
  - every write runs in one transaction; a deadlock (1205) is retried once;
    connections are always closed;
  - item writes lock the item row (UPDLOCK) first, so writers of one item
    queue up; they then write only the rows and columns that changed, with each
    column and its JSON copy computed together by dbmap.split_item;
  - update_field merges: the caller's dict is compared with the version it
    last read or wrote (per asyncio task / thread), and only the keys the
    caller changed are applied onto the current row. Two writers changing
    different keys of one group both survive; a value set back is written;
  - upserts are UPDATE, then INSERT if nothing was updated, under
    UPDLOCK + SERIALIZABLE (no MERGE); "insert or ignore" catches 2627/2601.
"""
import contextvars
import copy
import json
import uuid
from datetime import datetime, timezone

import pyodbc

import db
import dbmap
import sqlserver

_RAW = ("extra_raw", "identification_raw", "pricing_raw", "ebay_raw", "comp_raw", "info_raw",
        "row_raw")
_JSON_FIELDS = ("photos", "processed", "identification", "pricing", "listing", "ebay",
                "price_source_url", "receipt", "photo_layout", "listing_check", "stock")
_DUPLICATE = ("2627", "2601")


# --- plumbing ----------------------------------------------------------------

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
        rows = _dicts(cur)
        con.commit()
        return rows
    finally:
        con.close()


def _is_deadlock(e: Exception) -> bool:
    return any("1205" in str(a) or a == "40001" for a in getattr(e, "args", ()))


def _is_duplicate(e: Exception) -> bool:
    return isinstance(e, pyodbc.IntegrityError) and any(code in str(e) for code in _DUPLICATE)


def _write(fn, *args, **kwargs):
    """Run fn(cur, ...) in one transaction; retry once on a deadlock."""
    for attempt in (1, 2):
        con = sqlserver.connect()
        try:
            result = fn(con.cursor(), *args, **kwargs)
            con.commit()
            return result
        except pyodbc.Error as e:
            con.rollback()
            if attempt == 1 and _is_deadlock(e):
                continue
            raise
        except BaseException:
            con.rollback()
            raise
        finally:
            con.close()


def _clean(row: dict) -> dict:
    return {k: (json.dumps(v, ensure_ascii=False) if k in _RAW else v)
            for k, v in row.items() if v is not None}


def insert_rows(cur, table: str, rows, *, output: str | None = None) -> list:
    """Insert dict rows in order (None values: NULL or the column default).
    With `output`, returns the generated ids."""
    ids = []
    for r in rows:
        r = _clean(r)
        out = f" OUTPUT INSERTED.{output}" if output else ""
        cur.execute(f"INSERT INTO dbo.{table} ({', '.join(r)}){out} "
                    f"VALUES ({', '.join('?' * len(r))})", *r.values())
        if output:
            ids.append(cur.fetchone()[0])
    return ids


def _now() -> str:
    return db._now()


def _ts(value: str):
    return dbmap.to_db(value, dbmap.TS)[0]


# --- reading items -------------------------------------------------------------

def _load_parts(cur, where: str, params: tuple) -> list[dict]:
    """join_item inputs for every item matching `where` (an item_id
    predicate), in seq order."""
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
    }
    check_ids = {r["check_id"]: r["item_id"] for r in tables["check"]}
    issues = q("SELECT * FROM dbo.listing_check_issues WHERE check_id IN "
               "(SELECT check_id FROM dbo.listing_checks WHERE {w})")
    tables["issues"] = [dict(r, item_id=check_ids[r["check_id"]]) for r in issues
                        if r["check_id"] in check_ids]
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
    return [by_item[it["item_id"]] for it in items]


def _resolve(raw: dict) -> dict:
    d = copy.deepcopy(raw)
    for field in _JSON_FIELDS:
        if d.get(field) is not None:
            d[field] = db._map_paths(field, d[field], db._resolved)
    return d


def _items(where: str, params: tuple) -> list[dict]:
    con = sqlserver.connect()
    try:
        cur = con.cursor()
        raws = [dbmap.join_item(p) for p in _load_parts(cur, where, params)]
        con.commit()
    finally:
        con.close()
    _remember(*raws)
    return [_resolve(r) for r in raws]


def get_item(item_id: str) -> dict | None:
    found = _items("item_id = ?", (item_id,))
    return found[0] if found else None


def list_items(status: str | None = None) -> list[dict]:
    if status is None:
        return _items("1 = 1", ())
    return _items("item_id IN (SELECT item_id FROM dbo.items WHERE status = ?)", (status,))


# --- what each caller read (for update_field's merge) --------------------------
# The version of each (item, group) this caller last read or wrote, in stored
# form. "This caller" is the current context: every asyncio task (one per
# Telegram update or job) and every thread has its own, so two handlers never
# see each other's versions. update_field takes it as the version the caller
# started from. No version (the caller built the dict without reading) means
# the current row is the base, so the dict replaces the group, as on SQLite.
#
# Earlier this was one shared list of recent versions, and update_field took the
# one closest to the new dict. That lost undos: setting a price back from 35 to
# 40 matched the older 40 version exactly, so "nothing changed" and 35 stayed.

_base: contextvars.ContextVar = contextvars.ContextVar("db_mssql_base", default={})


def _remember(*raws: dict) -> None:
    # A new dict each time: a task's context is a copy of its parent's, and
    # mutating the shared dict would leak versions between tasks.
    seen = dict(_base.get())
    for raw in raws:
        for field in db.VALID_FIELDS:
            value = raw.get(field)
            if isinstance(value, dict):
                seen[(raw["item_id"], field)] = copy.deepcopy(value)
            else:
                seen.pop((raw["item_id"], field), None)
    _base.set(seen)


def _forget() -> None:
    """Drop this context's remembered versions (tests)."""
    _base.set({})


def _read_base(item_id: str, field: str) -> dict | None:
    return _base.get().get((item_id, field))


def merge_group(current, new, base: dict | None):
    """The group to store: `current` plus the keys the caller changed since
    `base`, the version it last read or wrote (None: `current`, so `new`
    replaces the group)."""
    if not isinstance(current, dict) or not isinstance(new, dict):
        return new
    if base is None:
        base = current
    merged = dict(current)
    for k in set(base) | set(new):
        if k not in new:
            if k in base:
                merged.pop(k, None)  # the caller removed it
        elif k not in base or base[k] != new[k]:
            merged[k] = new[k]
    return merged


# --- writing items -------------------------------------------------------------

_ONE_TO_ONE = {"item_identification": "identification", "item_listing": "listing",
               "ebay_listings": "ebay", "item_tags": "receipt"}
_LISTS = {"item_photos": ("position",), "listing_images": ("position",),
          "listing_specifics": ("name", "position"), "sales": ("order_id",),
          "item_flags": ("kind", "position")}


def _split(raw: dict) -> dict:
    return dbmap.split_item(raw)


def _update_cols(cur, table, key_cols: dict, old: dict, new: dict, skip=()) -> None:
    """UPDATE only the columns whose value changed (None = NULL)."""
    changed = {c: new.get(c) for c in set(old) | set(new)
               if c not in key_cols and c not in skip and old.get(c) != new.get(c)}
    if not changed:
        return
    sets = ", ".join(f"[{c}] = ?" for c in changed)
    where = " AND ".join(f"[{c}] = ?" for c in key_cols)
    vals = [json.dumps(v, ensure_ascii=False) if c in _RAW and v is not None else v
            for c, v in changed.items()]
    cur.execute(f"UPDATE dbo.{table} SET {sets} WHERE {where}", *vals, *key_cols.values())


def _apply(cur, item_id: str, old: dict, new: dict) -> None:
    """Write the difference between two split_item results for one item."""
    _update_cols(cur, "items", {"item_id": item_id}, old["items"][0], new["items"][0])

    for table in _ONE_TO_ONE:
        o = old[table][0] if old[table] else None
        n = new[table][0] if new[table] else None
        if o is None and n is not None:
            insert_rows(cur, table, [n])
        elif o is not None and n is None:
            cur.execute(f"DELETE FROM dbo.{table} WHERE item_id = ?", item_id)
        elif o is not None:
            _update_cols(cur, table, {"item_id": item_id}, o, n)

    for table, order in _LISTS.items():
        def norm(rows):
            return sorted((tuple(sorted(r.items(), key=lambda kv: kv[0])) for r in rows),
                          key=lambda t: [str(v) for _, v in t])
        if norm(old[table]) != norm(new[table]):
            cur.execute(f"DELETE FROM dbo.{table} WHERE item_id = ?", item_id)
            insert_rows(cur, table, new[table])

    _apply_pricing(cur, item_id, old, new)
    _apply_check(cur, item_id, old, new)


def _strip(rows, *keys):
    return [{k: v for k, v in r.items() if k not in keys} for r in rows]


def _apply_pricing(cur, item_id, old, new):
    o = old["item_pricing"][0] if old["item_pricing"] else None
    n = new["item_pricing"][0] if new["item_pricing"] else None
    if n is None:
        if o is not None:
            cur.execute("DELETE FROM dbo.item_pricing WHERE item_id = ?", item_id)
        return
    run_old = _strip(old["pricing_runs"], "run_key", "priced_at")
    run_new = _strip(new["pricing_runs"], "run_key", "priced_at")
    comps_old = _strip(old["pricing_comps"], "run_key")
    comps_new = _strip(new["pricing_comps"], "run_key")
    cur.execute("SELECT latest_run_id FROM dbo.item_pricing WHERE item_id = ?", item_id)
    row = cur.fetchone()
    latest = row[0] if row else None
    skip = ("run_key", "priced_at", "latest_run_id")
    if latest is None or run_old != run_new or comps_old != comps_new:
        # A real pricing run: keep every run (pricing_runs), point at the new one.
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        run_id = insert_rows(cur, "pricing_runs", [{"item_id": item_id, "priced_at": now,
                                                    **(run_new[0] if run_new else {})}],
                             output="run_id")[0]
        insert_rows(cur, "pricing_comps", [{**c, "run_id": run_id} for c in comps_new])
        n_row = {**{k: v for k, v in n.items() if k not in skip}, "latest_run_id": run_id, "priced_at": now}
        if o is None:
            insert_rows(cur, "item_pricing", [n_row])
        else:
            _update_cols(cur, "item_pricing", {"item_id": item_id},
                         {k: v for k, v in o.items() if k not in skip} | {"latest_run_id": latest},
                         n_row, skip=("priced_at",))
            cur.execute("UPDATE dbo.item_pricing SET priced_at = ? WHERE item_id = ?", now, item_id)
    elif o is not None:
        _update_cols(cur, "item_pricing", {"item_id": item_id},
                     {k: v for k, v in o.items() if k not in skip},
                     {k: v for k, v in n.items() if k not in skip})


def _apply_check(cur, item_id, old, new):
    o = _strip(old["listing_checks"], "check_key")
    n = _strip(new["listing_checks"], "check_key")
    oi = _strip(old["listing_check_issues"], "check_key")
    ni = _strip(new["listing_check_issues"], "check_key")
    if not n:
        if o:
            cur.execute("DELETE FROM dbo.listing_checks WHERE item_id = ?", item_id)
        return
    if o != n or oi != ni:
        check_id = insert_rows(cur, "listing_checks", n, output="check_id")[0]
        insert_rows(cur, "listing_check_issues", [{**i, "check_id": check_id} for i in ni])


def _lock_item(cur, item_id: str) -> bool:
    cur.execute("SELECT 1 FROM dbo.items WITH (UPDLOCK, ROWLOCK) WHERE item_id = ?", item_id)
    return cur.fetchone() is not None


def _rewrite(cur, item_id: str, change) -> dict | None:
    """Lock the item, rebuild it, let change(raw) edit it in place, write the
    difference. Returns the new raw item, or None if there is no such item."""
    if not _lock_item(cur, item_id):
        return None
    parts = _load_parts(cur, "item_id = ?", (item_id,))
    current = dbmap.join_item(parts[0])
    new = copy.deepcopy(current)
    change(new)
    _apply(cur, item_id, _split(current), _split(new))
    return new


def create_item(photos: list[str]) -> str:
    item_id = str(uuid.uuid4())
    now = _now()
    raw = {k: None for k in dbmap.ITEM_FIELDS}
    raw.update({"item_id": item_id, "status": "captured", "created_at": now, "updated_at": now,
                "photos": db._map_paths("photos", photos, db.stored_path)})
    rows = _split(raw)

    def go(cur):
        insert_rows(cur, "items", rows["items"])
        insert_rows(cur, "item_photos", rows["item_photos"])
    _write(go)
    _remember(raw)
    return item_id


def update_status(item_id: str, status: str) -> None:
    if status not in db.VALID_STATUSES:
        raise ValueError(f"Invalid status '{status}'. Must be one of: {db.VALID_STATUSES}")

    def change(raw):
        raw["status"] = status
        raw["updated_at"] = _now()
    _write(_rewrite, item_id, change)


def update_field(item_id: str, field: str, data) -> None:
    if field not in db.VALID_FIELDS:
        raise ValueError(f"Invalid field '{field}'. Must be one of: {db.VALID_FIELDS}")
    # Stored form, as SQLite stores it (and a JSON round trip, as SQLite does).
    data = json.loads(json.dumps(db._map_paths(field, data, db.stored_path)))
    base = _read_base(item_id, field)
    written = {}

    def change(raw):
        raw[field] = merge_group(raw.get(field), data, base)
        raw["updated_at"] = _now()
        written["raw"] = raw
    _write(_rewrite, item_id, change)
    if written:
        _remember(written["raw"])


def delete_item(item_id: str) -> None:
    def go(cur):
        if not _lock_item(cur, item_id):
            return
        cur.execute("SELECT COUNT(*) FROM dbo.sales WHERE item_id = ?", item_id)
        if cur.fetchone()[0]:
            # Decided (Q8): a sold item's sales are history; it is never deleted.
            raise ValueError(f"item {item_id} has sales and can't be deleted")
        cur.execute("DELETE FROM dbo.items WHERE item_id = ?", item_id)
    _write(go)


# --- upserts ---------------------------------------------------------------------

def _upsert(cur, table: str, keys: dict, values: dict, insert_only: dict | None = None) -> None:
    """UPDATE `values` where `keys` match; if no row, INSERT keys + values (+
    insert_only). UPDLOCK + SERIALIZABLE holds the key range, so two racing
    callers can't both insert."""
    sets = ", ".join(f"[{c}] = ?" for c in values)
    where = " AND ".join(f"[{c}] = ?" for c in keys)
    ins = {**keys, **values, **(insert_only or {})}
    cur.execute(
        f"SET NOCOUNT ON; UPDATE dbo.{table} WITH (UPDLOCK, SERIALIZABLE) SET {sets} WHERE {where}; "
        f"IF @@ROWCOUNT = 0 INSERT INTO dbo.{table} ({', '.join(f'[{c}]' for c in ins)}) "
        f"VALUES ({', '.join('?' * len(ins))});",
        *values.values(), *keys.values(), *ins.values())


def _dec(v, p=10, s=2):
    return dbmap.to_db(v, dbmap.DEC(p, s))[0] if v is not None else None


def record_listing_stats(item_id, week, *, impressions=None, views=None, watchers=None, price=None,
                         verdict=None, suggested_price=None, total_impressions=None,
                         search_views=None) -> None:
    values = {"impressions": impressions, "views": views, "watchers": watchers, "price": _dec(price),
              "verdict": verdict, "suggested_price": _dec(suggested_price),
              "created_at": _ts(_now()), "total_impressions": total_impressions,
              "search_views": search_views}
    _write(_upsert, "listing_stats", {"item_id": item_id, "week": dbmap.to_db(week, dbmap.DATE)[0]}, values)


# --- /check pre-buy checks -------------------------------------------------------

_CHECK_MONEY = ("ross_price", "suggested_price", "profit", "roi", "break_even", "cheapest_identical")
_CHECK_TIMES = ("created_at", "updated_at", "status_at")


def _check_encode(fields: dict) -> dict:
    db._check_fields(fields)
    row = {}
    for k, v in fields.items():
        if k in db.CHECK_JSON and v is not None:
            v = json.dumps(v)
        elif k == "status_at" and v is not None:
            v = _ts(v)
        row[k] = v
    return row


def _check_decode(row: dict | None) -> dict | None:
    if row is None:
        return None
    out = dict(row)
    for k in _CHECK_MONEY:
        if out.get(k) is not None:
            out[k] = float(out[k])
    for k in _CHECK_TIMES:
        if out.get(k) is not None:
            out[k] = out[k].replace(tzinfo=timezone.utc).isoformat()
    return db._check_row(out)


def add_check(**fields) -> int:
    row = _check_encode(fields)

    def go(cur):
        cols = ", ".join(row) if row else None
        if cols:
            cur.execute(f"INSERT INTO dbo.checks ({cols}) OUTPUT INSERTED.check_id "
                        f"VALUES ({', '.join('?' * len(row))})", *row.values())
        else:
            cur.execute("INSERT INTO dbo.checks OUTPUT INSERTED.check_id DEFAULT VALUES")
        return int(cur.fetchone()[0])
    return _write(go)


def update_check(check_id: int, **fields) -> None:
    row = _check_encode(fields)
    if not row:
        return
    _write(lambda cur: cur.execute(
        f"UPDATE dbo.checks SET {', '.join(f'{k} = ?' for k in row)}, updated_at = SYSUTCDATETIME() "
        "WHERE check_id = ?", *row.values(), check_id))


def add_evidence(rows: list[dict]) -> int:
    """db.add_evidence on ResaleDB (rows already checked by db.py)."""
    def go(cur):
        for r in rows:
            r = dict(r)
            for k in ("event_date", "fetched_at"):
                if r.get(k) is not None:
                    r[k] = _ts(db._utc_iso(r[k]))
            if "specifics" in r:
                r["specifics"] = json.dumps(r["specifics"])
            cur.execute(f"INSERT INTO dbo.check_evidence ({', '.join(r)}) "
                        f"VALUES ({', '.join('?' * len(r))})", *r.values())
        return len(rows)
    return _write(go)


def recent_evidence(model_key: str, source: str, since: str) -> list[dict]:
    rows = _query("SELECT * FROM dbo.check_evidence WHERE model_key = ? AND source = ? "
                  "AND fetched_at >= ? ORDER BY evidence_id", model_key, source, _ts(since))
    out = []
    for row in rows:
        for k in ("event_date", "fetched_at"):
            if row.get(k) is not None:
                row[k] = row[k].replace(tzinfo=timezone.utc).isoformat()
        out.append(db._evidence_row(row))
    return out


def save_quality_report(date: str, sections: list[dict], listings: list[dict]) -> dict:
    """db.save_quality_report on ResaleDB (rows already shaped by db.py)."""
    when = _ts(date)

    def go(cur):
        cur.execute("SELECT COUNT(*) FROM dbo.quality_report_sections WHERE report_date = ?", when)
        replaced = cur.fetchone()[0] > 0
        cur.execute("DELETE FROM dbo.quality_report_sections WHERE report_date = ?", when)
        cur.execute("DELETE FROM dbo.quality_report_listings WHERE report_date = ?", when)
        for table, rows in (("quality_report_sections", sections), ("quality_report_listings", listings)):
            for r in rows:
                r = {"report_date": when, **r}
                cur.execute(f"INSERT INTO dbo.{table} ({', '.join(r)}) "
                            f"VALUES ({', '.join('?' * len(r))})", *r.values())
        return {"sections": len(sections), "listings": len(listings), "replaced": replaced}
    return _write(go)


def latest_quality_report() -> dict | None:
    rows = _query("SELECT MAX(report_date) AS d FROM dbo.quality_report_sections")
    date = rows[0]["d"] if rows else None
    if date is None:
        return None
    iso = date.replace(tzinfo=timezone.utc).isoformat()
    return {"report_date": iso,
            "sections": _query("SELECT * FROM dbo.quality_report_sections WHERE report_date = ? "
                               "ORDER BY section_row_id", date),
            "listings": _query("SELECT * FROM dbo.quality_report_listings WHERE report_date = ? "
                               "ORDER BY listing_row_id", date)}


def replace_old_store_sales(rows: list[dict]) -> dict:
    """db.replace_old_store_sales on ResaleDB (rows already shaped by db.py)."""
    def go(cur):
        cur.execute("SELECT COUNT(*) FROM dbo.old_store_sales")
        replaced = cur.fetchone()[0]
        cur.execute("DELETE FROM dbo.old_store_sales")
        if rows:
            cols = list(rows[0])
            cur.executemany(f"INSERT INTO dbo.old_store_sales ({', '.join(cols)}) "
                            f"VALUES ({', '.join('?' * len(cols))})",
                            [tuple(r[c] for c in cols) for r in rows])
        return {"rows": len(rows), "replaced": replaced}
    return _write(go)


def old_store_sales(since: str | None) -> list[dict]:
    if since:
        return _query("SELECT * FROM dbo.old_store_sales WHERE period_end >= ? "
                      "ORDER BY period_start, row_id", since)
    return _query("SELECT * FROM dbo.old_store_sales ORDER BY period_start, row_id")


def get_check(check_id: int) -> dict | None:
    rows = _query("SELECT * FROM dbo.checks WHERE check_id = ?", check_id)
    return _check_decode(rows[0]) if rows else None


def list_checks(*, user_id=None, status=None, sku=None, since=None, limit: int = 100) -> list[dict]:
    where, args = [], []
    for col, val in (("user_id", user_id), ("status", status), ("ross_sku", sku)):
        if val is not None:
            where.append(f"{col} = ?")
            args.append(val)
    if since:
        where.append("created_at >= ?")
        args.append(_ts(since))
    sql = (f"SELECT TOP ({int(limit)}) * FROM dbo.checks"
           + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY check_id DESC")
    return [_check_decode(r) for r in _query(sql, *args)]


def receipt_lines_for_skus(skus) -> list[dict]:
    skus = list(skus)[:500]
    if not skus:
        return []
    marks = ", ".join("?" * len(skus))
    rows = _query(f"""
        SELECT ri.ItemID AS line_id, ri.SKU AS sku, ri.Description AS description, ri.Price AS price,
               r.ReceiptDate AS receipt_date, r.ReceiptNumber AS receipt_number, r.StoreCity AS store_city
        FROM dbo.ReceiptItems ri JOIN dbo.Receipts r ON r.ReceiptID = ri.ReceiptID
        WHERE ri.SKU IN ({marks})
        ORDER BY r.ReceiptDate DESC, ri.ItemID DESC""", *skus)
    for r in rows:
        if r["price"] is not None:
            r["price"] = float(r["price"])
        if r["receipt_date"] is not None and hasattr(r["receipt_date"], "isoformat"):
            r["receipt_date"] = r["receipt_date"].isoformat()[:10]
    return rows


# --- receipt line decoding and /rcpt (docs/receipt-decoding.md) --------------------
# Receipts and ReceiptItems belong to receipt_pipeline: only read here. The one
# table written is receipt_line_details (migration step 5).

RLD_COLUMNS = ("category", "item_type", "gender", "color", "brand_guess", "product_model", "confidence",
               "source", "model", "item_id", "decode_raw")
_LINE_MONEY = ("price", "subtotal", "total", "tax", "lines_sum", "subtotal_gap", "d_confidence", "confidence")
# Per line: how many prices its SKU has on its receipt (the whole receipt, not
# just the lines a search returned), the receipt's line count and sum, and
# whether receipts_cleanup fixed the line by hand.
_LINE_CONTEXT = """
    OUTER APPLY (SELECT COUNT(DISTINCT x.Price) AS same_code_prices FROM dbo.ReceiptItems x
                 WHERE x.ReceiptID = ri.ReceiptID AND x.SKU = ri.SKU) sc
    OUTER APPLY (SELECT COUNT(*) AS line_count, SUM(x.Price) AS lines_sum FROM dbo.ReceiptItems x
                 WHERE x.ReceiptID = ri.ReceiptID) rs"""
_LINE_FIXED = """CAST(CASE WHEN EXISTS (SELECT 1 FROM dbo.receipt_fixes f WHERE f.table_name = 'ReceiptItems'
                 AND f.row_id = ri.ItemID AND f.column_name <> '*') THEN 1 ELSE 0 END AS BIT) AS fixed"""
_LINE_COLUMNS = """ri.ItemID AS line_id, ri.ReceiptID AS receipt_id, ri.LineIndex AS line_index, ri.SKU AS sku,
    ri.Description AS description, UPPER(LTRIM(RTRIM(ri.Description))) AS desc_key, ri.Price AS price,
    r.ReceiptNumber AS receipt_number, r.ReceiptDate AS receipt_date, r.ReceiptTime AS receipt_time,
    r.StoreNumber AS store_number, r.StoreCity AS store_city, r.Subtotal AS subtotal, r.Total AS total,
    r.Tax AS tax, sc.same_code_prices, rs.line_count, rs.lines_sum"""


def _has_line_details(cur) -> bool:
    """Step 5 may not have run yet (a dry run against production before the
    deploy): then there are no saved decodes."""
    cur.execute("SELECT OBJECT_ID(N'dbo.receipt_line_details', N'U')")
    return cur.fetchone()[0] is not None


def _receipt_values(rows: list[dict]) -> list[dict]:
    for r in rows:
        for k in _LINE_MONEY:
            if r.get(k) is not None:
                r[k] = float(r[k])
        for k in ("receipt_date",):
            if r.get(k) is not None and hasattr(r[k], "isoformat"):
                r[k] = r[k].isoformat()[:10]
        if r.get("receipt_time") is not None:
            r["receipt_time"] = str(r["receipt_time"])[:8]
        for k in ("decode_raw", "d_decode_raw"):
            if isinstance(r.get(k), str):
                r[k] = json.loads(r[k])
        if "fixed" in r:
            r["fixed"] = bool(r["fixed"])
    return rows


def receipt_decode_inputs() -> dict:
    con = sqlserver.connect()
    try:
        cur = con.cursor()
        cur.execute(f"SELECT {_LINE_COLUMNS}, {_LINE_FIXED} FROM dbo.ReceiptItems ri "
                    f"JOIN dbo.Receipts r ON r.ReceiptID = ri.ReceiptID {_LINE_CONTEXT} "
                    "ORDER BY ri.ReceiptID, ri.LineIndex")
        lines = _receipt_values(_dicts(cur))
        details = {}
        has = _has_line_details(cur)
        if has:
            cur.execute("SELECT * FROM dbo.receipt_line_details")
            details = {r["desc_key"]: r for r in _receipt_values(_dicts(cur))}
        con.commit()
        return {"lines": lines, "details": details, "has_table": has}
    finally:
        con.close()


def save_receipt_line_details(rows) -> int:
    rows = list(rows)

    def go(cur):
        now = _ts(_now())
        for r in rows:
            vals = {c: r.get(c) for c in RLD_COLUMNS}
            vals["confidence"] = _dec(vals["confidence"], 3, 2)
            if vals["decode_raw"] is not None:
                vals["decode_raw"] = json.dumps(vals["decode_raw"], ensure_ascii=False, default=str)
            vals["decoded_at"] = now
            _upsert(cur, "receipt_line_details", {"desc_key": r["desc_key"]}, vals)
        return len(rows)
    return _write(go) if rows else 0


def _like(text: str) -> str:
    """A LIKE pattern for text, with % _ [ \\ taken literally (ESCAPE '\\')."""
    return "%" + "".join("\\" + ch if ch in "%_[\\" else ch for ch in text) + "%"


def _rcpt_where(kind: str, value, has_details: bool) -> tuple[str, list]:
    if kind == "sku":
        return "ri.SKU = ?", [value]
    if kind == "sku_suffix":
        return "ri.SKU LIKE ?", ["%" + value]
    if kind == "receipt_number_part":
        return "REPLACE(r.ReceiptNumber, '-', '') LIKE ?", [f"%{value}%"]
    if kind != "words":
        raise ValueError(f"unknown /rcpt search kind {kind!r}")
    parts, args = [], []
    for term in value:
        if term.get("gender"):
            parts.append("d.gender = ?" if has_details else "1 = 0")
            args += [term["gender"]] if has_details else []
            continue
        like = _like(term["text"])
        ors = ["ri.Description LIKE ? ESCAPE '\\'"]
        args.append(like)
        if has_details:
            for col in ("brand_guess", "category", "item_type", "color", "product_model"):
                ors.append(f"d.{col} LIKE ? ESCAPE '\\'")
                args.append(like)
        # what the item behind the line's SKU is (real Ross codes only)
        ors.append("EXISTS (SELECT 1 FROM dbo.item_tags t JOIN dbo.item_identification i ON i.item_id = t.item_id "
                   "WHERE (t.ross_code = ri.SKU OR t.ross_code_main = ri.SKU) AND ISNULL(t.code_generated, 0) = 0 "
                   "AND (i.brand LIKE ? ESCAPE '\\' OR i.item_type LIKE ? ESCAPE '\\' "
                   "OR i.product_name LIKE ? ESCAPE '\\'))")
        args += [like, like, like]
        for code in term.get("codes") or ():
            c = "".join("\\" + ch if ch in "%_[\\" else ch for ch in code)
            ors.append("(ri.Description LIKE ? ESCAPE '\\' OR ri.Description LIKE ? ESCAPE '\\' "
                       "OR ri.Description LIKE ? ESCAPE '\\' OR ri.Description = ?)")
            args += [f"{c} %", f"% {c} %", f"#{c} %", code]
        parts.append("(" + " OR ".join(ors) + ")")
    return " AND ".join(parts) or "1 = 0", args


def receipt_lines_search(kind: str, value, limit: int = 10) -> dict:
    con = sqlserver.connect()
    try:
        cur = con.cursor()
        has = _has_line_details(cur)
        cur.execute("SELECT StoreNumber, StoreCity, COUNT(*) FROM dbo.Receipts "
                    "WHERE StoreNumber IS NOT NULL GROUP BY StoreNumber, StoreCity")
        store_rows = [tuple(r) for r in cur.fetchall()]
        header = None
        if kind in ("receipt_id", "receipt_number"):
            col = "r.ReceiptID" if kind == "receipt_id" else "r.ReceiptNumber"
            cur.execute(f"""
                SELECT r.ReceiptID AS receipt_id, r.ReceiptNumber AS receipt_number, r.ReceiptDate AS receipt_date,
                       r.ReceiptTime AS receipt_time, r.StoreNumber AS store_number, r.StoreCity AS store_city,
                       r.Subtotal AS subtotal, r.Tax AS tax, r.Total AS total, r.Flags AS flags,
                       c.line_count, c.subtotal_gap, c.has_returns, c.missing_fields
                FROM dbo.Receipts r LEFT JOIN dbo.v_receipt_checks c ON c.ReceiptID = r.ReceiptID
                WHERE {col} = ?""", value)
            found = _receipt_values(_dicts(cur))
            if not found:
                return {"total": 0, "lines": [], "receipt": None, "store_rows": store_rows}
            header = found[0]
            where, args, order = "ri.ReceiptID = ?", [header["receipt_id"]], "ri.LineIndex"
        else:
            where, args = _rcpt_where(kind, value, has)
            order = ("CASE WHEN r.ReceiptDate IS NULL THEN 1 ELSE 0 END, r.ReceiptDate DESC, r.ReceiptTime DESC, "
                     "ri.ReceiptID DESC, ri.LineIndex")
        cols = ", ".join(f"{'d.' + c if has else 'NULL'} AS d_{c}" for c in RLD_COLUMNS)
        join = ("LEFT JOIN dbo.receipt_line_details d ON d.desc_key = UPPER(LTRIM(RTRIM(ri.Description)))"
                if has else "")
        cur.execute(f"""
            SELECT TOP (?) COUNT(*) OVER () AS match_count, {_LINE_COLUMNS}, {_LINE_FIXED}, {cols}
            FROM dbo.ReceiptItems ri JOIN dbo.Receipts r ON r.ReceiptID = ri.ReceiptID {join} {_LINE_CONTEXT}
            WHERE {where} ORDER BY {order}""", int(limit), *args)
        lines = _receipt_values(_dicts(cur))
        con.commit()
        return {"total": lines[0]["match_count"] if lines else 0, "lines": lines, "receipt": header,
                "store_rows": store_rows}
    finally:
        con.close()


def record_job_run(job: str, status: str = "ok", detail: str | None = None) -> None:
    _write(_upsert, "job_runs", {"job": job}, {"last_run_at": _ts(_now()), "status": status, "detail": detail})


def record_feedback_failure(order_line: str, error: str) -> None:
    _write(_upsert, "feedback_failures", {"order_line": order_line}, {"last_error": error},
           insert_only={"first_failed_at": _ts(_now())})


def clear_feedback_failure(order_line: str) -> None:
    _write(lambda cur: cur.execute("DELETE FROM dbo.feedback_failures WHERE order_line = ?", order_line))


def _insert_or_ignore(cur, table: str, row: dict) -> bool:
    """INSERT unless the key exists; a racing duplicate (2627/2601) is ignored."""
    try:
        insert_rows(cur, table, [row])
        return True
    except pyodbc.IntegrityError as e:
        if _is_duplicate(e):
            return False
        raise


def mark_not_duplicate(item_a: str, item_b: str) -> None:
    a, b = sorted((item_a, item_b))
    _write(_insert_or_ignore, "not_duplicates", {"a": a, "b": b})


def mark_orders_seen(order_ids) -> None:
    ids = list(order_ids)
    now = _ts(_now())

    def go(cur):
        for o in ids:
            _insert_or_ignore(cur, "seen_orders", {"order_id": o, "seen_at": now})
    _write(go)


# --- plain inserts and updates ------------------------------------------------------

def record_offer_sent(listing_id, offer_price, *, item_id=None, list_price=None, source=None) -> None:
    _write(insert_rows, "offer_sends", [{
        "item_id": item_id, "listing_id": str(listing_id), "list_price": _dec(list_price),
        "offer_price": _dec(offer_price), "source": source, "sent_at": _ts(_now())}])


def save_offer_plan(rows: list[dict]) -> str:
    plan_id = str(uuid.uuid4())
    rows = json.loads(json.dumps(rows))

    def go(cur):
        insert_rows(cur, "offer_plans", [{"plan_id": plan_id, "created_at": _ts(_now())}])
        insert_rows(cur, "offer_plan_rows", [dbmap.split_plan_row(plan_id, i, r) for i, r in enumerate(rows)])
    _write(go)
    return plan_id


def claim_offer_plan(plan_id: str) -> bool:
    def go(cur):
        cur.execute("UPDATE dbo.offer_plans SET used_at = ? WHERE plan_id = ? AND used_at IS NULL",
                    _ts(_now()), plan_id)
        return cur.rowcount == 1
    return _write(go)


def record_llm_usage(*, provider, model, stage=None, item_id=None, input_tokens=0, output_tokens=0,
                     cache_read_tokens=0, cache_write_tokens=0, web_searches=0, cost_usd=None,
                     ts=None) -> None:
    _write(insert_rows, "llm_usage", [{
        "ts": _ts(ts or _now()), "provider": provider, "model": model, "stage": stage, "item_id": item_id,
        "input_tokens": input_tokens, "output_tokens": output_tokens,
        "cache_read_tokens": cache_read_tokens, "cache_write_tokens": cache_write_tokens,
        "web_searches": web_searches, "cost_usd": _dec(cost_usd, 18, 10)}])


# --- pieces ----------------------------------------------------------------------------

def _ensure_location(cur, code: str) -> None:
    """A piece's location row, created on first use (as /inv G3 does)."""
    cur.execute("SELECT 1 FROM dbo.locations WITH (UPDLOCK, HOLDLOCK) WHERE location_code = ?", code)
    if cur.fetchone() is None:
        _insert_or_ignore(cur, "locations", {"location_code": code})


def _next_code(cur, box: str) -> str:
    prefix = f"{box}-"
    cur.execute("SELECT code FROM dbo.pieces WITH (UPDLOCK, HOLDLOCK) WHERE code LIKE ?", _like_prefix(prefix))
    nums = [int(c[len(prefix):]) for (c,) in cur.fetchall() if c[len(prefix):].isdigit()]
    return f"{prefix}{(max(nums) + 1) if nums else 1:02d}"


def add_piece(box: str, photo: str | None = None) -> str:
    """The box's location row is locked first, so pieces added to one box at
    once queue up and get consecutive codes instead of colliding."""
    box = box.upper()
    stored = db.stored_path(photo) if photo else photo

    def go(cur):
        _ensure_location(cur, box)   # UPDLOCK + HOLDLOCK on the box's row
        code = _next_code(cur, box)
        now = _now()
        insert_rows(cur, "pieces", [dbmap.split_piece({
            "code": code, "box": box, "item_id": None, "status": "home", "photo": stored, "info": {},
            "value": None, "cost": None, "created_at": now, "updated_at": now,
            "sold_at": None, "order_id": None})])
        return code

    for _ in range(5):
        try:
            return _write(go)
        except pyodbc.IntegrityError as e:
            if not _is_duplicate(e):
                raise
    raise RuntimeError(f"couldn't reserve a code in {box}")


def update_piece(code: str, **fields) -> None:
    """Same checks as SQLite (db.update_piece did them before dispatching)."""
    code = code.upper()

    def go(cur):
        cur.execute("SELECT * FROM dbo.pieces WITH (UPDLOCK, ROWLOCK) WHERE code = ?", code)
        rows = _dicts(cur)
        if not rows:
            return
        current = dbmap.join_piece(rows[0])
        new = copy.deepcopy(current)
        for k, v in fields.items():
            if k == "box" and isinstance(v, str):
                v = v.upper()
            if k == "photo" and v:
                v = db.stored_path(v)
            new[k] = json.loads(json.dumps(v)) if k == "info" else v
        new["updated_at"] = _now()
        if new["box"] != current["box"]:
            _ensure_location(cur, new["box"])
        old_row, new_row = dbmap.split_piece(current), dbmap.split_piece(new)
        _update_cols(cur, "pieces", {"code": code}, old_row, new_row)
    _write(go)


# --- other reads -------------------------------------------------------------------------

def _since(since_iso: str):
    """A lower bound as SQLite's text comparison reads it: "2000" and
    "2026-10" are the start of that year/month; no offset means UTC."""
    s = since_iso.strip() if isinstance(since_iso, str) else since_iso
    if isinstance(s, str) and len(s) == 4 and s.isdigit():
        s += "-01-01"
    elif isinstance(s, str) and len(s) == 7 and s[4] == "-":
        s += "-01"
    dt = dbmap._parse_ts(s)
    if dt is None:
        raise ValueError(f"not an ISO timestamp: {since_iso!r}")
    return dt


def offers_sent_since(since_iso: str) -> set[str]:
    rows = _query("SELECT DISTINCT listing_id FROM dbo.offer_sends WHERE sent_at >= ?", _since(since_iso))
    return {r["listing_id"] for r in rows}


def get_offer_plan(plan_id: str) -> dict | None:
    plans = _query("SELECT * FROM dbo.offer_plans WHERE plan_id = ?", plan_id)
    if not plans:
        return None
    rows = _query("SELECT * FROM dbo.offer_plan_rows WHERE plan_id = ? ORDER BY position", plan_id)
    plan = plans[0]
    return {"plan_id": plan_id, "rows": [dbmap.join_plan_row(r) for r in rows],
            "created_at": dbmap.from_db(plan["created_at"], dbmap.TS),
            "used_at": dbmap.from_db(plan["used_at"], dbmap.TS)}


STATS = {"impressions": dbmap.INT, "views": dbmap.INT, "watchers": dbmap.INT,
         "price": dbmap.DEC(10, 2), "verdict": dbmap.S(30), "suggested_price": dbmap.DEC(10, 2),
         "created_at": dbmap.TS, "total_impressions": dbmap.INT, "search_views": dbmap.INT}
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


# --- moved from ebay/, pipeline/ and scripts/ (Q13) --------------------------------------

def _epoch_to_dt(epoch: float):
    return datetime.fromtimestamp(float(epoch), timezone.utc).replace(tzinfo=None)


def _dt_to_epoch(dt) -> float | None:
    return dt.replace(tzinfo=timezone.utc).timestamp() if dt is not None else None


def save_ebay_token(access_token, refresh_token, expires_at) -> None:
    values = {"access_token": access_token, "expires_at": _epoch_to_dt(expires_at)}
    if refresh_token:
        values["refresh_token"] = refresh_token
    _write(_upsert, "ebay_tokens", {"id": 1}, values)


def load_ebay_token() -> dict | None:
    rows = _query("SELECT access_token, refresh_token, expires_at FROM dbo.ebay_tokens WHERE id = 1")
    if not rows:
        return None
    r = rows[0]
    return {"access_token": r["access_token"], "refresh_token": r["refresh_token"],
            "expires_at": _dt_to_epoch(r["expires_at"])}


def taxonomy_cache_get(key: str):
    rows = _query("SELECT payload_raw, fetched_at FROM dbo.taxonomy_cache WHERE cache_key = ?", key)
    if not rows:
        return None
    return json.loads(rows[0]["payload_raw"]), _dt_to_epoch(rows[0]["fetched_at"])


def taxonomy_cache_set(key: str, value, fetched_at: float) -> None:
    _write(_upsert, "taxonomy_cache", {"cache_key": key},
           {"payload_raw": json.dumps(value), "fetched_at": _epoch_to_dt(fetched_at)})


def llm_stage_spend(since_iso: str, stage: str, item_id: str | None = None) -> tuple[float, int]:
    sql = ("SELECT COALESCE(SUM(cost_usd), 0) AS cost, COUNT(*) AS calls FROM dbo.llm_usage "
           "WHERE ts >= ? AND stage = ?")
    args = [_since(since_iso), stage]
    if item_id:
        sql += " AND item_id = ?"
        args.append(item_id)
    r = _query(sql, *args)[0]
    return float(r["cost"]), int(r["calls"])


def modify_field(item_id: str, field: str, change) -> bool:
    done = {}

    def edit(raw):
        current = raw.get(field)
        shown = copy.deepcopy(db._map_paths(field, current, db._resolved)) if current is not None else None
        new = change(shown)
        if new is None:
            return
        raw[field] = json.loads(json.dumps(db._map_paths(field, new, db.stored_path)))
        raw["updated_at"] = _now()
        done["raw"] = raw
    _write(_rewrite, item_id, edit)
    if done:
        _remember(done["raw"])
    return bool(done)


_READ_ONLY_FIRST_WORDS = ("SELECT", "WITH")


class ReadOnlyError(RuntimeError):
    """A statement other than SELECT/WITH on a read-only connection."""


def _check_read_only(sql: str) -> None:
    body = "\n".join(line for line in sql.splitlines() if not line.strip().startswith("--")).strip()
    if body.endswith(";"):
        body = body[:-1]
    first = body.split(None, 1)[0].upper() if body else ""
    if first not in _READ_ONLY_FIRST_WORDS or ";" in body:
        raise ReadOnlyError(f"read-only connection: only one SELECT/WITH statement, not {sql[:60]!r}")


class _ReadOnlyCursor:
    def __init__(self, cur):
        self._cur = cur

    def execute(self, sql, *params):
        _check_read_only(sql)
        self._cur.execute(sql, *params)
        return self

    def __getattr__(self, name):
        return getattr(self._cur, name)


class ReadOnlyConnection:
    """db.connect_readonly() on SQL Server. Only SELECT/WITH runs, and the
    transaction is always rolled back, so nothing can be changed through it.
    (Q7's read-only SQL login needs mixed-mode auth, a server setting.)"""

    def __init__(self, con):
        self._con = con

    def cursor(self):
        return _ReadOnlyCursor(self._con.cursor())

    def execute(self, sql, *params):
        return self.cursor().execute(sql, *params)

    def commit(self):
        raise ReadOnlyError("read-only connection: commit refused")

    def close(self):
        try:
            self._con.rollback()
        finally:
            self._con.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def connect_readonly(path=None) -> ReadOnlyConnection:
    if path is not None:
        name = str(path)
        if any(ch in name for ch in "/\\.:"):
            raise ValueError(f"{name!r} looks like a file; on SQL Server pass a database name")
        return ReadOnlyConnection(sqlserver.connect(name))
    return ReadOnlyConnection(sqlserver.connect())


# --- consistency ------------------------------------------------------------------------

def consistency_report() -> list[str]:
    """Every place where a column and its JSON copy disagree (should be none)."""
    con = sqlserver.connect()
    try:
        cur = con.cursor()
        out = []
        for parts in _load_parts(cur, "1 = 1", ()):
            out += dbmap.check_item(parts)
        cur.execute("SELECT * FROM dbo.pieces")
        for row in _dicts(cur):
            out += dbmap.check_piece(row)
        cur.execute("SELECT * FROM dbo.offer_plan_rows")
        for row in _dicts(cur):
            out += dbmap.check_plan_row(row)
        con.commit()
        return out
    finally:
        con.close()
