"""DEV loader: copy a SQLite ross.db into ResaleDB_Dev, then check that every
db.py read returns the same thing on both backends.

    python scripts/sqlite_to_mssql.py                    # data/ross.db -> ResaleDB_Dev
    python scripts/sqlite_to_mssql.py --source X.db --target ResaleDB_Test_...

A rough first version of the migration script (docs/schema-proposal.md, 7).
It WIPES the target, so only ResaleDB_Dev and ResaleDB_Test_* are accepted
(sqlserver.assert_resettable). The source is opened read-only. eBay tokens
are never copied: a dev database must not be able to act on eBay.

Exit code 0 only if the round trip found no differences.
"""
import argparse
import json
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
import db_mssql  # noqa: E402
import dbmap  # noqa: E402
import migrations  # noqa: E402
import sqlserver  # noqa: E402

ITEM_TABLE_ORDER = ["items", "item_photos", "item_identification", "item_flags", "pricing_runs",
                    "item_pricing", "pricing_comps", "item_listing", "listing_specifics",
                    "ebay_listings", "listing_images", "sales", "item_tags", "listing_checks",
                    "listing_check_issues"]
_JSON_COLS = {"extra_raw", "identification_raw", "pricing_raw", "ebay_raw", "comp_raw", "info_raw",
              "payload_raw"}


# --- read the SQLite file ----------------------------------------------------

def read_sqlite(path) -> dict:
    con = sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        def rows(sql):
            return [dict(r) for r in con.execute(sql)]

        items = []
        for r in rows("SELECT * FROM items ORDER BY rowid"):
            for f in dbmap.ITEM_FIELDS:
                if f in ("item_id", "status", "created_at", "updated_at"):
                    continue
                if r.get(f) is not None:
                    r[f] = json.loads(r[f])
            items.append({k: r.get(k) for k in dbmap.ITEM_FIELDS})
        pieces = rows("SELECT * FROM pieces ORDER BY rowid")
        for p in pieces:
            p["info"] = json.loads(p["info"]) if p.get("info") else {}
        return {
            "items": items, "pieces": pieces,
            "listing_stats": rows("SELECT * FROM listing_stats ORDER BY rowid"),
            "offer_sends": rows("SELECT * FROM offer_sends ORDER BY id"),
            "offer_plans": rows("SELECT * FROM offer_plans ORDER BY rowid"),
            "job_runs": rows("SELECT * FROM job_runs"),
            "feedback_failures": rows("SELECT * FROM feedback_failures"),
            "not_duplicates": rows("SELECT * FROM not_duplicates"),
            "seen_orders": rows("SELECT * FROM seen_orders"),
            "llm_usage": rows("SELECT * FROM llm_usage ORDER BY id"),
            "taxonomy_cache": rows("SELECT * FROM taxonomy_cache"),
        }
    finally:
        con.close()


# --- write SQL Server --------------------------------------------------------

def _clean(row: dict) -> dict:
    """None values are left out (NULL or the column default); JSON columns are
    serialised."""
    return {k: (json.dumps(v, ensure_ascii=False) if k in _JSON_COLS else v)
            for k, v in row.items() if v is not None}


def _insert(cur, table, rows, *, output=None, ordered=False) -> list:
    """Insert dict rows. `output`: one at a time, returning the generated ids.
    `ordered`: one at a time in the given order (items, whose seq is the
    order). Otherwise batched by column set."""
    rows = [_clean(r) for r in rows]
    if output or ordered:
        ids = []
        for r in rows:
            out = f" OUTPUT INSERTED.{output}" if output else ""
            cur.execute(f"INSERT INTO dbo.{table} ({', '.join(r)}){out} "
                        f"VALUES ({', '.join('?' * len(r))})", *r.values())
            if output:
                ids.append(cur.fetchone()[0])
        return ids
    groups = defaultdict(list)
    for r in rows:
        groups[tuple(r)].append(tuple(r.values()))
    for cols, batch in groups.items():
        cur.executemany(f"INSERT INTO dbo.{table} ({', '.join(cols)}) "
                        f"VALUES ({', '.join('?' * len(cols))})", batch)
    return []


def _ts(value, report, where):
    dbv, exact = dbmap.to_db(value, dbmap.TS)
    if value is not None and not exact:
        report["lossy"][where] += 1
    return dbv


def _num(value, kind, report, where):
    dbv, exact = dbmap.to_db(value, kind)
    if value is not None and not exact:
        report["lossy"][where] += 1
    return dbv


def load(src: dict, con) -> dict:
    """Write everything in `src` (read_sqlite output) into the empty database
    behind `con`. Returns a report of what was stored where."""
    report = {"counts": Counter(), "leftover": Counter(), "lossy": Counter(), "new_pieces": []}
    cur = con.cursor()

    # pieces and locations first (items' stock reads from them)
    piece_rows = [dbmap.split_piece(p) for p in src["pieces"]]
    pieces_by_item = defaultdict(list)
    for pr in piece_rows:
        if pr.get("item_id"):
            pieces_by_item[pr["item_id"]].append(pr)

    rows = {t: [] for t in ITEM_TABLE_ORDER}
    new_pieces = []
    for seq, item in enumerate(src["items"], start=1):
        out = dbmap.split_item(item, pieces_for_item=pieces_by_item.get(item["item_id"], ()))
        for t in ITEM_TABLE_ORDER:
            rows[t] += out[t]
        new_pieces += out["new_pieces"]
        for r in out["items"]:
            for group, left in (r.get("extra_raw") or {}).items():
                for k in left:
                    report["leftover"][f"{group}.{k}"] += 1
        for t, col, group in (("item_identification", "identification_raw", "identification"),
                              ("item_pricing", "pricing_raw", "pricing"),
                              ("ebay_listings", "ebay_raw", "ebay")):
            for r in out[t]:
                for k in (r.get(col) or {}):
                    report["leftover"][f"{group}.{k}"] += 1

    # number new pieces per location, after the existing codes
    used = defaultdict(set)
    for pr in piece_rows:
        prefix, _, n = pr["code"].rpartition("-")
        if n.isdigit():
            used[prefix].add(int(n))
    created = []
    for np_ in new_pieces:
        loc = np_["location_code"]
        n = max(used[loc], default=0) + 1
        used[loc].add(n)
        piece_rows.append({"code": f"{loc}-{n:02d}", **np_})
        created.append(f"{loc}-{n:02d}")
    report["new_pieces"] = created

    locations = sorted({pr["location_code"] for pr in piece_rows})
    _insert(cur, "locations", [{"location_code": loc} for loc in locations])
    _insert(cur, "items", rows["items"], ordered=True)
    _insert(cur, "pieces", piece_rows)
    for t in ("item_photos", "item_identification", "item_flags"):
        _insert(cur, t, rows[t])

    run_ids = dict(zip([r["run_key"] for r in rows["pricing_runs"]],
                       _insert(cur, "pricing_runs", [{k: v for k, v in r.items() if k != "run_key"}
                                                     for r in rows["pricing_runs"]], output="run_id")))
    _insert(cur, "item_pricing", [{**{k: v for k, v in r.items() if k != "run_key"},
                                   "latest_run_id": run_ids[r["run_key"]]} for r in rows["item_pricing"]])
    _insert(cur, "pricing_comps", [{**{k: v for k, v in r.items() if k != "run_key"},
                                    "run_id": run_ids[r["run_key"]]} for r in rows["pricing_comps"]])
    for t in ("item_listing", "listing_specifics", "ebay_listings", "listing_images", "sales", "item_tags"):
        _insert(cur, t, rows[t])
    check_ids = dict(zip([r["check_key"] for r in rows["listing_checks"]],
                         _insert(cur, "listing_checks", [{k: v for k, v in r.items() if k != "check_key"}
                                                         for r in rows["listing_checks"]], output="check_id")))
    _insert(cur, "listing_check_issues", [{**{k: v for k, v in r.items() if k != "check_key"},
                                           "check_id": check_ids[r["check_key"]]}
                                          for r in rows["listing_check_issues"]])

    # operations tables
    _insert(cur, "listing_stats", [{
        "item_id": r["item_id"], "week": dbmap.to_db(r["week"], dbmap.DATE)[0],
        "impressions": r["impressions"], "views": r["views"], "watchers": r["watchers"],
        "total_impressions": r.get("total_impressions"), "search_views": r.get("search_views"),
        "price": _num(r["price"], dbmap.DEC(10, 2), report, "listing_stats.price"),
        "suggested_price": _num(r["suggested_price"], dbmap.DEC(10, 2), report, "listing_stats.suggested_price"),
        "verdict": r["verdict"], "created_at": _ts(r["created_at"], report, "listing_stats.created_at"),
    } for r in src["listing_stats"]])
    _insert(cur, "offer_sends", [{
        "item_id": r["item_id"], "listing_id": r["listing_id"],
        "list_price": _num(r["list_price"], dbmap.DEC(10, 2), report, "offer_sends.list_price"),
        "offer_price": _num(r["offer_price"], dbmap.DEC(10, 2), report, "offer_sends.offer_price"),
        "source": r["source"], "sent_at": _ts(r["sent_at"], report, "offer_sends.sent_at"),
    } for r in src["offer_sends"]])
    for plan in src["offer_plans"]:
        _insert(cur, "offer_plans", [{"plan_id": plan["plan_id"],
                                      "created_at": _ts(plan["created_at"], report, "offer_plans.created_at"),
                                      "used_at": _ts(plan["used_at"], report, "offer_plans.used_at")}])
        plan_rows = []
        for pos, pr in enumerate(json.loads(plan["rows"])):
            left = {}
            cols = dbmap.split_cols(pr, db_mssql.PLAN_ROW, left)
            if left or set(pr) - set(db_mssql.PLAN_ROW):
                report["lossy"]["offer_plan_rows"] += 1
            plan_rows.append({"plan_id": plan["plan_id"], "position": pos, **cols})
        _insert(cur, "offer_plan_rows", plan_rows)
    _insert(cur, "job_runs", [{"job": r["job"], "last_run_at": _ts(r["last_run_at"], report, "job_runs.last_run_at"),
                               "status": r["status"], "detail": r["detail"]} for r in src["job_runs"]])
    _insert(cur, "feedback_failures", [{
        "order_line": r["order_line"], "first_failed_at": _ts(r["first_failed"], report, "feedback_failures.first_failed"),
        "last_error": r["last_error"]} for r in src["feedback_failures"]])
    _insert(cur, "not_duplicates", src["not_duplicates"])
    _insert(cur, "seen_orders", [{"order_id": r["order_id"], "seen_at": _ts(r["seen_at"], report, "seen_orders.seen_at")}
                                 for r in src["seen_orders"]])
    _insert(cur, "llm_usage", [{
        "ts": _ts(r["ts"], report, "llm_usage.ts"), "provider": r["provider"], "model": r["model"],
        "stage": r["stage"], "item_id": r["item_id"], "input_tokens": r["input_tokens"],
        "output_tokens": r["output_tokens"], "cache_read_tokens": r["cache_read_tokens"],
        "cache_write_tokens": r["cache_write_tokens"], "web_searches": r["web_searches"],
        "cost_usd": _num(r["cost_usd"], dbmap.DEC(18, 10), report, "llm_usage.cost_usd"),
    } for r in src["llm_usage"]])
    _insert(cur, "taxonomy_cache", [{
        "cache_key": r["cache_key"], "payload_raw": json.loads(r["payload"]),
        "fetched_at": datetime.fromtimestamp(r["fetched_at"], timezone.utc).replace(tzinfo=None),
    } for r in src["taxonomy_cache"]])
    con.commit()

    cur.execute("SELECT t.name, SUM(p.rows) FROM sys.tables t JOIN sys.partitions p "
                "ON p.object_id = t.object_id AND p.index_id IN (0, 1) GROUP BY t.name")
    report["counts"] = Counter({name: n for name, n in cur.fetchall()})
    return report


# --- compare both backends ---------------------------------------------------

def verify(sqlite_path, database, created_pieces=()) -> dict:
    """Call every db.py read on both backends; return {check: [differences]}.
    `created_pieces`: codes the loader made from stock.location (they exist
    only on SQL Server, by design) are left out of the piece comparison."""
    import db

    old = (config.DB_BACKEND, config.SQL_DATABASE, db.DB_PATH)
    db.DB_PATH = str(sqlite_path)
    config.SQL_DATABASE = database
    epoch = "1970-01-01T00:00:00+00:00"

    def both(fn, *args, **kwargs):
        config.DB_BACKEND = "sqlite"
        a = fn(*args, **kwargs)
        config.DB_BACKEND = "sqlserver"
        b = fn(*args, **kwargs)
        return a, b

    results = {}
    try:
        config.DB_BACKEND = "sqlite"
        items = db.list_items()
        ids = [it["item_id"] for it in items]
        statuses = sorted({it["status"] for it in items})
        boxes = sorted({p["box"] for p in db.list_pieces()})
        plans = []
        con = sqlite3.connect(f"file:{Path(sqlite_path).as_posix()}?mode=ro", uri=True)
        try:
            plans = [r[0] for r in con.execute("SELECT plan_id FROM offer_plans")]
            jobs = [r[0] for r in con.execute("SELECT job FROM job_runs")]
            stats_items = [r[0] for r in con.execute("SELECT DISTINCT item_id FROM listing_stats")]
        finally:
            con.close()

        diffs = []
        for item_id in ids:
            diffs += dbmap.diff(*both(db.get_item, item_id), f"get_item({item_id[:8]})")
        results["get_item (every item)"] = diffs
        results["list_items()"] = dbmap.diff(*both(db.list_items), "list_items()")
        results["list_items(status)"] = sum((dbmap.diff(*both(db.list_items, s), f"list_items({s})")
                                             for s in statuses), [])
        results["stats_history"] = sum((dbmap.diff(*both(db.stats_history, i, 100000), f"stats({i[:8]})")
                                        for i in stats_items), [])
        results["offers_sent_since"] = dbmap.diff(*both(db.offers_sent_since, epoch), "offers")
        results["get_offer_plan"] = sum((dbmap.diff(*both(db.get_offer_plan, p), f"plan({p[:8]})") for p in plans), [])
        results["known_feedback_failures"] = dbmap.diff(*both(db.known_feedback_failures), "feedback")
        results["not_duplicate_pairs"] = dbmap.diff(*both(db.not_duplicate_pairs), "not_dup")
        results["seen_order_ids"] = dbmap.diff(*both(db.seen_order_ids), "seen")
        results["job_runs"] = sum((dbmap.diff(*both(db.job_run_status, j), f"status({j})")
                                   + dbmap.diff(*both(db.last_job_run, j), f"last({j})") for j in jobs), [])
        a, b = both(db.list_pieces)
        b = [p for p in b if p["code"] not in set(created_pieces)]
        results["list_pieces / get_piece"] = dbmap.diff(a, b, "pieces") + sum(
            (dbmap.diff(*both(db.get_piece, p["code"]), f"get_piece({p['code']})") for p in a), []) + sum(
            (dbmap.diff(*both(db.next_piece_code, bx), f"next({bx})") for bx in boxes
             if not any(c.startswith(f"{bx}-") for c in created_pieces)), [])
        results["next_receipt_code"] = dbmap.diff(*both(db.next_receipt_code), "next_receipt_code")
        results["llm_spend_since"] = dbmap.diff(*both(db.llm_spend_since, epoch), "spend", approx=True)
        a, b = both(db.llm_usage_breakdown, epoch)
        key = lambda r: (r["provider"], r["model"], r["stage"])  # noqa: E731
        results["llm_usage_breakdown"] = dbmap.diff(sorted(a, key=key), sorted(b, key=key), "breakdown", approx=True)
    finally:
        config.DB_BACKEND, config.SQL_DATABASE, db.DB_PATH = old
    return results


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--source", default=str(config.DATA_DIR / "ross.db"))
    ap.add_argument("--target", default=sqlserver.DEV_DATABASE)
    args = ap.parse_args(argv)
    sqlserver.assert_resettable(args.target)

    src = read_sqlite(args.source)
    sqlserver.reset_database(args.target)
    con = sqlserver.connect(args.target)
    try:
        migrations.migrate_mssql(con)
        report = load(src, con)
    finally:
        con.close()

    print(f"Loaded {args.source} -> {args.target}")
    for t in ["items", "item_photos", "item_identification", "item_flags", "item_pricing",
              "pricing_runs", "pricing_comps", "item_listing", "listing_specifics", "ebay_listings",
              "listing_images", "sales", "item_tags", "listing_checks", "listing_check_issues",
              "pieces", "locations", "listing_stats", "offer_sends", "offer_plans", "job_runs",
              "feedback_failures", "not_duplicates", "seen_orders", "llm_usage", "taxonomy_cache",
              "ebay_tokens"]:
        print(f"  {t:22} {report['counts'].get(t, 0)}")
    print(f"  new pieces from stock.location: {len(report['new_pieces'])}")
    print("Kept in leftover JSON (key: items):")
    for k, n in sorted(report["leftover"].items(), key=lambda kv: (-kv[1], kv[0])):
        print(f"  {k:40} {n}")
    if report["lossy"]:
        print("Values the operations tables could not store exactly:")
        for k, n in sorted(report["lossy"].items()):
            print(f"  {k:40} {n}")

    results = verify(args.source, args.target, report["new_pieces"])
    total = 0
    print("Round trip, SQLite vs SQL Server:")
    for check, diffs in results.items():
        total += len(diffs)
        print(f"  {check:28} {'OK' if not diffs else f'{len(diffs)} difference(s)'}")
        for d in diffs[:20]:
            print(f"      {d}")
        if len(diffs) > 20:
            print(f"      ... {len(diffs) - 20} more")
    print("RESULT:", "identical" if total == 0 else f"{total} difference(s)")
    return 0 if total == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
