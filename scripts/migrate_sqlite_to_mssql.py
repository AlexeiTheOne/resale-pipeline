"""Production migration: SQLite ross.db + ReceiptsDB -> ResaleDB
(docs/schema-proposal.md, section 7).

    # rehearsal: snapshot the live file (read-only, online backup API), then migrate
    python scripts/migrate_sqlite_to_mssql.py --snapshot-from C:\\...\\ebay-list\\data\\ross.db
    # from an existing snapshot / backup file
    python scripts/migrate_sqlite_to_mssql.py --source data\\rehearsal\\ross-....db
    # cutover day (section 2 of docs/cutover-checklist.md)
    python scripts/migrate_sqlite_to_mssql.py --source <backup> --target ResaleDB --production

Targets: ResaleDB_Migrate_<yyyymmdd> (default: today), or ResaleDB with
--production. The target must hold no rows. Everything is copied, including
the eBay token and the receipts (same ReceiptID/ItemID values, identities
reseeded, CreatedAt Eastern -> UTC). Receipt cleanup and the unique receipt
indexes are a separate, later step.

Rehearsal databases hold production data and the real eBay token, so
sqlserver.connect() refuses them unless this script unlocks one.

Every check of section 7 runs after the copy. A markdown report goes to
logs/. Exit code 0 only if every check passed.
"""
import argparse
import hashlib
import importlib.util
import json
import sqlite3
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
import dbmap  # noqa: E402
import migrations  # noqa: E402
import sqlserver  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "sqlite_to_mssql", Path(__file__).resolve().parent / "sqlite_to_mssql.py")
loader = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(loader)

EASTERN = ZoneInfo("America/New_York")
RECEIPT_COLS = ["ReceiptNumber", "Store", "StoreCity", "StoreZip", "TransactionType", "ReceiptDate",
                "ReceiptTime", "Subtotal", "Tax", "Total", "Confidence", "Flags", "RawFileID",
                "ItemsCount", "SourceFile", "Status"]
LINE_COLS = ["ReceiptID", "LineIndex", "SKU", "Description", "Price"]
# Tables the bootstrap fills by itself (not "rows" for the empty-target rule).
_SEEDED = {"schema_version", "app_settings"}
# SQLite tables the loader copies (plus items/pieces/ebay_tokens and SQLite's own).
_HANDLED_SQLITE = {"items", "pieces", "listing_stats", "offer_sends", "offer_plans", "job_runs",
                   "feedback_failures", "not_duplicates", "seen_orders", "llm_usage",
                   "taxonomy_cache", "ebay_tokens", "schema_version", "sqlite_sequence"}
_DIRECT_TABLES = ["listing_stats", "offer_sends", "offer_plans", "job_runs", "feedback_failures",
                  "not_duplicates", "seen_orders", "llm_usage", "taxonomy_cache"]


# --- target rules ------------------------------------------------------------

def check_target(name: str, production: bool) -> None:
    """ResaleDB_Migrate_<yyyymmdd> (unlocked for this process), ResaleDB only
    with --production, ResaleDB_Test_* for the tests. Anything else: refused."""
    sqlserver._check_name(name)
    if name.casefold() == sqlserver.PRODUCTION_DATABASE.casefold():
        if not production:
            raise SystemExit(f"refusing {name!r}: the production database needs --production")
        return
    if production:
        raise SystemExit(f"--production is only for {sqlserver.PRODUCTION_DATABASE!r}, not {name!r}")
    if sqlserver.is_migrate_database(name):
        sqlserver.allow_migrate_database(name)  # raises unless ResaleDB_Migrate_<8 digits>
        return
    if name.startswith(sqlserver.TEST_PREFIX):
        sqlserver.assert_test_database(name)
        return
    raise SystemExit(f"refusing {name!r}: the target must be ResaleDB_Migrate_<yyyymmdd> "
                     f"(or {sqlserver.PRODUCTION_DATABASE} with --production)")


def target_rows(con) -> dict:
    """{table: rows} for user tables holding rows, bootstrap-seeded ones aside."""
    rows = con.execute(
        "SELECT t.name, SUM(p.rows) FROM sys.tables t JOIN sys.partitions p "
        "ON p.object_id = t.object_id AND p.index_id IN (0, 1) GROUP BY t.name").fetchall()
    return {n: c for n, c in rows if c and n not in _SEEDED}


# --- source ------------------------------------------------------------------

def take_snapshot(live: Path, dest_dir: Path, name: str | None = None) -> Path:
    """Copy `live` with SQLite's online backup API. The live file is opened
    read-only (mode=ro), which is safe while the bot runs; nothing else in the
    live folder is touched. Unlike a file copy, this includes what is still in
    the WAL file."""
    live = Path(live).resolve()
    if not live.is_file():
        raise SystemExit(f"no such file: {live}")
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / (name or f"ross-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}.db")
    if dest.exists():
        raise SystemExit(f"already exists: {dest}")
    src = sqlite3.connect(f"file:{live.as_posix()}?mode=ro", uri=True)
    try:
        dst = sqlite3.connect(dest)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    return dest


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def read_extras(path: Path) -> dict:
    """What the loader doesn't read: the token, the table list, integrity."""
    con = sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro", uri=True)
    try:
        tables = {}
        for (name,) in con.execute("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"):
            tables[name] = con.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
        token = None
        if "ebay_tokens" in tables:
            row = con.execute("SELECT access_token, refresh_token, expires_at FROM ebay_tokens "
                              "WHERE id = 1").fetchone()
            if row:
                token = {"access_token": row[0], "refresh_token": row[1], "expires_at": row[2]}
        version = None
        if "schema_version" in tables:
            version = con.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
        integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        con.close()
    return {"tables": tables, "token": token, "schema_version": version, "integrity": integrity}


# --- copy: tokens and receipts -------------------------------------------------

def copy_token(cur, token) -> None:
    if not token:
        return
    exp = token["expires_at"]
    cur.execute("INSERT INTO dbo.ebay_tokens (id, access_token, refresh_token, expires_at) "
                "VALUES (1, ?, ?, ?)", token["access_token"], token["refresh_token"],
                None if exp is None else datetime.fromtimestamp(float(exp), timezone.utc).replace(tzinfo=None))


def receipts_identity(con, receipts_db: str) -> dict:
    """{table: (last identity value, max id, rows)} in the receipts source."""
    out = {}
    for table, key in (("Receipts", "ReceiptID"), ("ReceiptItems", "ItemID")):
        last = con.execute(
            f"SELECT CAST(ic.last_value AS BIGINT) FROM [{receipts_db}].sys.identity_columns ic "
            f"JOIN [{receipts_db}].sys.tables t ON t.object_id = ic.object_id WHERE t.name = ?",
            table).fetchone()
        mx, n = con.execute(f"SELECT MAX({key}), COUNT(*) FROM [{receipts_db}].dbo.{table}").fetchone()
        out[table] = (last[0] if last else None, mx, n)
    return out


def copy_receipts(cur, receipts_db: str, ident: dict) -> None:
    """Same ids (IDENTITY_INSERT), CreatedAt from Eastern (getdate() on this
    PC) to UTC, identities reseeded to the source's last value so a deleted
    id is never handed out again."""
    cols = ", ".join(RECEIPT_COLS)
    cur.execute("SET IDENTITY_INSERT dbo.Receipts ON")
    cur.execute(
        f"INSERT INTO dbo.Receipts (ReceiptID, {cols}, CreatedAt) "
        f"SELECT ReceiptID, {cols}, CAST(CAST(CreatedAt AS DATETIME2(3)) "
        f"AT TIME ZONE 'Eastern Standard Time' AT TIME ZONE 'UTC' AS DATETIME2(6)) "
        f"FROM [{receipts_db}].dbo.Receipts")
    cur.execute("SET IDENTITY_INSERT dbo.Receipts OFF")
    lcols = ", ".join(LINE_COLS)
    cur.execute("SET IDENTITY_INSERT dbo.ReceiptItems ON")
    cur.execute(f"INSERT INTO dbo.ReceiptItems (ItemID, {lcols}) "
                f"SELECT ItemID, {lcols} FROM [{receipts_db}].dbo.ReceiptItems")
    cur.execute("SET IDENTITY_INSERT dbo.ReceiptItems OFF")
    for table in ("Receipts", "ReceiptItems"):
        last = ident[table][0]
        if last is not None:
            cur.execute(f"DBCC CHECKIDENT ('dbo.{table}', RESEED, {int(last)}) WITH NO_INFOMSGS")


# --- checks --------------------------------------------------------------------

class Checks:
    def __init__(self):
        self.rows = []  # (name, status, summary, details)

    def add(self, name, ok, summary="", details=(), warn=False):
        status = "PASS" if ok else ("WARN" if warn else "FAIL")
        self.rows.append((name, status, summary, list(details)))

    @property
    def failed(self):
        return [r for r in self.rows if r[1] == "FAIL"]


def _num(v):
    return Decimal(str(v)) if type(v) in (int, float) else None


def _q(con, sql, *args):
    cur = con.cursor()
    cur.execute(sql, *args)
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _list_len(v):
    return len(v) if type(v) is list else 0


def _spec_len(spec):
    if type(spec) is not dict:
        return 0
    return sum(len(v) if type(v) is list else 1 for v in spec.values())


def _json(v):
    return json.loads(v) if v else {}


def source_pieces_plan(items, pieces):
    """Items that should get pieces from stock.location: {item_id: (loc, units)}.
    Also the locations that can't become a piece (dbmap's rule), for the report."""
    has_piece = {p["item_id"] for p in pieces if p.get("item_id")}
    plan, unusable = {}, []
    for it in items:
        st = it.get("stock")
        if type(st) is not dict or not st.get("location") or it["item_id"] in has_piece:
            continue
        loc = st["location"]
        if type(loc) is str and loc == loc.strip().upper() and len(loc) <= 40:
            q = (it.get("ebay") or {}).get("quantity") if type(it.get("ebay")) is dict else None
            plan[it["item_id"]] = (loc, max(q if type(q) is int else 1, 1))
        else:
            unusable.append((it["item_id"], loc))
    return plan, unusable


def leftover_keys(items, pieces) -> Counter:
    """The keys that land in leftover JSON (no column), as the loader counts them."""
    c = Counter()
    by_item = defaultdict(list)
    for p in pieces:
        row = dbmap.split_piece(p)
        if row.get("item_id"):
            by_item[row["item_id"]].append(row)
        for k, v in (row.get("info_raw") or {}).items():
            if k == "__piece__":
                for kk in v:
                    c[f"piece.{kk}"] += 1
            else:
                c[f"piece.info.{k}"] += 1
    for item in items:
        out = dbmap.split_item(item, pieces_for_item=by_item.get(item["item_id"], ()),
                               create_pieces=True, derive_priced_at=True)
        for r in out["items"]:
            for group, left in (r.get("extra_raw") or {}).items():
                for k in left:
                    c[f"{group}.{k}"] += 1
        for t, col, group in (("item_identification", "identification_raw", "identification"),
                              ("item_pricing", "pricing_raw", "pricing"),
                              ("ebay_listings", "ebay_raw", "ebay")):
            for r in out[t]:
                for k in (r.get(col) or {}):
                    c[f"{group}.{k}"] += 1
    return c


def shape(src, extras) -> dict:
    """Key sets per group, statuses and tables: to compare production with dev."""
    keys = defaultdict(set)
    for it in src["items"]:
        keys["status"].add(it["status"])
        for g in ("identification", "pricing", "listing", "ebay", "receipt", "listing_check",
                  "photo_layout", "stock"):
            if type(it.get(g)) is dict:
                keys[g] |= set(it[g])
        ebay = it.get("ebay")
        if type(ebay) is dict and type(ebay.get("sales")) is list:
            for rec in ebay["sales"]:
                if type(rec) is dict:
                    keys["ebay.sales[]"] |= set(rec)
    for p in src["pieces"]:
        keys["piece.status"].add(p.get("status"))
        if type(p.get("info")) is dict:
            keys["piece.info"] |= set(p["info"])
    keys["sqlite tables"] = set(extras["tables"])
    return keys


def run_checks(con, src, extras, report, receipts_db, ident, target, source_path, work_path) -> Checks:
    ck = Checks()
    items = src["items"]
    counts = report["counts"]

    # 1. full round trip, every db.py read (the main check)
    results = loader.verify(work_path, target, report["new_pieces"])
    total = sum(len(v) for v in results.values())
    seen = len({d.split(")")[0] for d in results.get("get_item (every item)", [])})
    ck.add("Full round trip (every db.py read, every item)", total == 0,
           f"{len(items)} items; {total} difference(s)"
           + (f" in {seen} item(s)" if seen else ""),
           [f"{k}: {d}" for k, v in results.items() for d in v])

    # 2. columns vs their JSON copies
    import db_mssql
    mism = db_mssql.consistency_report()
    ck.add("Columns vs JSON copies", not mism, f"{len(mism)} mismatch(es)", mism)

    # 3. row counts
    exp = {"items": len(items), "ebay_tokens": 1 if extras["token"] else 0,
           "pieces": len(src["pieces"]) + len(report["new_pieces"]),
           "Receipts": ident["Receipts"][2], "ReceiptItems": ident["ReceiptItems"][2]}
    for t in _DIRECT_TABLES:
        exp[t] = len(src[t])
    for group, tables in (("identification", ["item_identification"]),
                          ("pricing", ["item_pricing", "pricing_runs"]),
                          ("listing", ["item_listing"]), ("ebay", ["ebay_listings"]),
                          ("receipt", ["item_tags"]), ("listing_check", ["listing_checks"])):
        n = sum(1 for it in items if it.get(group) is not None)
        for t in tables:
            exp[t] = n
    bad = [f"{t}: expected {n}, got {counts.get(t, 0)}" for t, n in exp.items() if counts.get(t, 0) != n]
    ck.add("Row counts", not bad, f"{len(exp)} tables compared", bad)
    report["expected_counts"] = exp

    # 4. list lengths: rows, or the whole list kept in leftover JSON
    ext = {r["item_id"]: r for r in _q(con, "SELECT i.item_id, i.extra_raw, ii.identification_raw, "
                                       "ip.pricing_raw, e.ebay_raw FROM dbo.items i "
                                       "LEFT JOIN dbo.item_identification ii ON ii.item_id = i.item_id "
                                       "LEFT JOIN dbo.item_pricing ip ON ip.item_id = i.item_id "
                                       "LEFT JOIN dbo.ebay_listings e ON e.item_id = i.item_id")}

    def per_item(sql):
        return {r["k"]: r["n"] for r in _q(con, sql)}

    rows_by = {
        "photos": per_item("SELECT item_id k, COUNT(*) n FROM dbo.item_photos GROUP BY item_id"),
        "flags:condition": per_item("SELECT item_id k, COUNT(*) n FROM dbo.item_flags "
                                    "WHERE kind = 'condition' GROUP BY item_id"),
        "flags:review": per_item("SELECT item_id k, COUNT(*) n FROM dbo.item_flags "
                                 "WHERE kind = 'review' GROUP BY item_id"),
        "specifics": per_item("SELECT item_id k, COUNT(*) n FROM dbo.listing_specifics GROUP BY item_id"),
        "images": per_item("SELECT item_id k, COUNT(*) n FROM dbo.listing_images GROUP BY item_id"),
        "sales": per_item("SELECT item_id k, COUNT(*) n FROM dbo.sales GROUP BY item_id"),
        "issues": per_item("SELECT c.item_id k, COUNT(*) n FROM dbo.listing_check_issues x "
                           "JOIN dbo.listing_checks c ON c.check_id = x.check_id GROUP BY c.item_id"),
    }
    for kind in dbmap.COMP_KINDS.values():
        rows_by[f"comps:{kind}"] = per_item(
            f"SELECT r.item_id k, COUNT(*) n FROM dbo.pricing_comps c JOIN dbo.pricing_runs r "
            f"ON r.run_id = c.run_id WHERE c.kind = '{kind}' GROUP BY r.item_id")
    lists = {}  # name: [expected, rows, kept in leftover]
    for it in items:
        iid = it["item_id"]
        e = ext.get(iid) or {}
        extra, idr, prr, ebr = (_json(e.get("extra_raw")), _json(e.get("identification_raw")),
                                _json(e.get("pricing_raw")), _json(e.get("ebay_raw")))
        ident_, pricing, listing, ebay, check = (
            it.get(g) if type(it.get(g)) is dict else {}
            for g in ("identification", "pricing", "listing", "ebay", "listing_check"))
        cases = [
            ("photos", _list_len(it.get("photos")), _list_len((extra.get("_item") or {}).get("photos"))),
            ("flags:condition", _list_len(ident_.get("condition_flags")), _list_len(idr.get("condition_flags"))),
            ("flags:review", _list_len(pricing.get("review_flags")), _list_len(prr.get("review_flags"))),
            ("specifics", _spec_len(listing.get("item_specifics")),
             _spec_len((extra.get("listing") or {}).get("item_specifics"))),
            ("images", _list_len(ebay.get("image_urls")), _list_len(ebr.get("image_urls"))),
            ("sales", _list_len(ebay.get("sales")),
             0 if rows_by["sales"].get(iid) else _list_len(ebr.get("sales"))),
            ("issues", _list_len(check.get("issues")),
             _list_len((extra.get("listing_check") or {}).get("issues"))),
        ]
        for key, kind in dbmap.COMP_KINDS.items():
            v = pricing.get(key)
            n = (1 if type(v) is dict else 0) if kind == "reference" else _list_len(v)
            kept = prr.get(key)
            k = (1 if type(kept) is dict else 0) if kind == "reference" else _list_len(kept)
            cases.append((f"comps:{kind}", n, k))
        for name, expected, kept in cases:
            got = rows_by[name].get(iid, 0)
            acc = lists.setdefault(name, [0, 0, 0, []])
            acc[0] += expected
            acc[1] += got
            acc[2] += kept
            if expected != got + kept:
                acc[3].append(f"{iid}: {name} expected {expected}, rows {got}, kept {kept}")
    plan_rows = per_item("SELECT plan_id k, COUNT(*) n FROM dbo.offer_plan_rows GROUP BY plan_id")
    acc = lists.setdefault("offer plan rows", [0, 0, 0, []])
    for plan in src["offer_plans"]:
        n = _list_len(json.loads(plan["rows"]))
        acc[0] += n
        acc[1] += plan_rows.get(plan["plan_id"], 0)
        if n != plan_rows.get(plan["plan_id"], 0):
            acc[3].append(f"plan {plan['plan_id']}: expected {n}, rows {plan_rows.get(plan['plan_id'], 0)}")
    bad = [d for v in lists.values() for d in v[3]]
    ck.add("List lengths", not bad,
           "; ".join(f"{k} {v[0]}={v[1]}+{v[2]}" for k, v in lists.items()), bad)
    report["lists"] = {k: v[:3] for k, v in lists.items()}

    # 5. pricing runs
    n_pricing = sum(1 for it in items if it.get("pricing") is not None)
    wrong = _q(con, "SELECT ip.item_id FROM dbo.item_pricing ip LEFT JOIN dbo.pricing_runs r "
                    "ON r.run_id = ip.latest_run_id WHERE r.run_id IS NULL OR r.item_id <> ip.item_id")
    ck.add("Pricing runs", counts.get("pricing_runs", 0) == n_pricing and not wrong,
           f"{counts.get('pricing_runs', 0)} runs for {n_pricing} priced items; {len(wrong)} bad latest_run_id",
           [r["item_id"] for r in wrong])

    # 6. pieces
    plan, unusable = source_pieces_plan(items, src["pieces"])
    want = sum(u for _, u in plan.values())
    created = report["new_pieces"]
    made = Counter(c.rpartition("-")[0] for c in created)
    want_loc = Counter()
    for loc, u in plan.values():
        want_loc[loc] += u
    no_loc = _q(con, "SELECT COUNT(*) n FROM dbo.pieces p LEFT JOIN dbo.locations l "
                     "ON l.location_code = p.location_code WHERE l.location_code IS NULL")[0]["n"]
    ok = len(created) == want and made == want_loc and no_loc == 0
    ck.add("Pieces", ok,
           f"{len(src['pieces'])} copied as-is; {len(created)} new from stock.location "
           f"(expected {want} for {len(plan)} items); {no_loc} without a location",
           [f"{loc}: made {made[loc]}, expected {want_loc[loc]}"
            for loc in sorted(set(made) | set(want_loc)) if made[loc] != want_loc[loc]])
    if unusable:
        ck.add("Locations that can't become pieces", False,
               f"{len(unusable)} item(s) keep stock.location as text only",
               [f"{i}: {loc!r}" for i, loc in unusable], warn=True)
    report["pieces_plan"] = (plan, unusable)

    # 7. money
    money = []
    old = defaultdict(Decimal)
    for it in items:
        ebay = it.get("ebay")
        if type(ebay) is dict and type(ebay.get("sales")) is list:
            for rec in ebay["sales"]:
                if type(rec) is dict:
                    for f in ("sale", "shipping", "fees", "label", "ad_fee", "refund", "net", "units"):
                        v = _num(rec.get(f))
                        if v is not None:
                            old[f] += v
    new = _q(con, "SELECT SUM(sale) sale, SUM(shipping) shipping, SUM(fees) fees, SUM(label) label, "
                  "SUM(ad_fee) ad_fee, SUM(refund) refund, SUM(net) net, SUM(units) units FROM dbo.sales")[0]
    for f in ("sale", "shipping", "fees", "label", "ad_fee", "refund", "net", "units"):
        money.append((f"sales.{f}", old[f], Decimal(new[f] or 0)))
    tags_old = sum((_num((it.get("receipt") or {}).get("reduced_price")) or Decimal(0))
                   for it in items if type(it.get("receipt")) is dict)
    money.append(("item_tags.ross_price", tags_old,
                  Decimal(_q(con, "SELECT SUM(ross_price) s FROM dbo.item_tags")[0]["s"] or 0)))
    list_old = sum((_num((it.get("listing") or {}).get("price")) or Decimal(0))
                   for it in items if type(it.get("listing")) is dict)
    money.append(("item_listing.list_price", list_old,
                  Decimal(_q(con, "SELECT SUM(list_price) s FROM dbo.item_listing")[0]["s"] or 0)))
    rs = _q(con, f"SELECT (SELECT COUNT(*) FROM [{receipts_db}].dbo.Receipts) n, "
                 f"(SELECT SUM(Total) FROM [{receipts_db}].dbo.Receipts) total, "
                 f"(SELECT SUM(Subtotal) FROM [{receipts_db}].dbo.Receipts) sub, "
                 f"(SELECT SUM(Tax) FROM [{receipts_db}].dbo.Receipts) tax, "
                 f"(SELECT COUNT(*) FROM [{receipts_db}].dbo.ReceiptItems) lines, "
                 f"(SELECT SUM(Price) FROM [{receipts_db}].dbo.ReceiptItems) price")[0]
    rt = _q(con, "SELECT (SELECT COUNT(*) FROM dbo.Receipts) n, (SELECT SUM(Total) FROM dbo.Receipts) total, "
                 "(SELECT SUM(Subtotal) FROM dbo.Receipts) sub, (SELECT SUM(Tax) FROM dbo.Receipts) tax, "
                 "(SELECT COUNT(*) FROM dbo.ReceiptItems) lines, (SELECT SUM(Price) FROM dbo.ReceiptItems) price")[0]
    for k, label in (("n", "Receipts COUNT"), ("total", "Receipts.Total"), ("sub", "Receipts.Subtotal"),
                     ("tax", "Receipts.Tax"), ("lines", "ReceiptItems COUNT"), ("price", "ReceiptItems.Price")):
        money.append((label, Decimal(rs[k] or 0), Decimal(rt[k] or 0)))
    bad = [f"{name}: old {a}, new {b}" for name, a, b in money if a != b]
    ck.add("Money sums", not bad, f"{len(money)} sums compared", bad)
    report["money"] = money

    # 8. sale totals vs v_item_sales
    view = {r["item_id"]: r for r in _q(con, "SELECT * FROM dbo.v_item_sales")}
    bad, no_records = [], []
    for it in items:
        ebay = it.get("ebay")
        if type(ebay) is not dict or not any(k in ebay for k in ("net_proceeds", "units_sold", "sold_at")):
            continue
        v = view.get(it["item_id"])
        if v is None:
            no_records.append(f"{it['item_id']}: status {it['status']}, net_proceeds "
                              f"{ebay.get('net_proceeds')!r}, sold_at {ebay.get('sold_at')!r}")
            continue
        for key in ("net_proceeds", "sale_price"):
            if key in ebay and (_num(ebay[key]) is None or _num(ebay[key]).quantize(Decimal("0.01")) != v[key]):
                bad.append(f"{it['item_id']}: {key} {ebay[key]!r} vs view {v[key]}")
        if "units_sold" in ebay and ebay["units_sold"] != v["units_sold"]:
            bad.append(f"{it['item_id']}: units_sold {ebay['units_sold']!r} vs view {v['units_sold']}")
        for key in ("sold_at", "first_sold_at"):
            if key in ebay and dbmap.to_db(ebay[key], dbmap.TS_EBAY)[0] != v[key]:
                bad.append(f"{it['item_id']}: {key} {ebay[key]!r} vs view {v[key]}")
    ck.add("Sale totals vs v_item_sales", not bad, f"{len(view)} sold items in the view", bad)
    if no_records:
        ck.add("Sold items without per-order records", False,
               f"{len(no_records)} item(s): totals kept as stored (leftover), not in v_item_sales",
               no_records, warn=True)

    # 9. unmapped keys (reported for review, never a failure by themselves)
    report["leftover"] = leftover_keys(items, src["pieces"])

    # 10. constraints and orphans
    cur = con.cursor()
    cur.execute("DBCC CHECKCONSTRAINTS WITH ALL_CONSTRAINTS, NO_INFOMSGS")
    violations = []
    while True:
        if cur.description:
            violations += [" | ".join(str(x) for x in r) for r in cur.fetchall()]
        if not cur.nextset():
            break
    ck.add("Constraints (DBCC CHECKCONSTRAINTS)", not violations, f"{len(violations)} violation(s)", violations)
    orphans = []
    for table, col in (("llm_usage", "item_id"), ("listing_stats", "item_id"), ("offer_sends", "item_id"),
                       ("offer_plan_rows", "item_id"), ("not_duplicates", "a"), ("not_duplicates", "b")):
        n = _q(con, f"SELECT COUNT(*) n FROM dbo.{table} x WHERE x.{col} IS NOT NULL AND NOT EXISTS "
                    f"(SELECT 1 FROM dbo.items i WHERE i.item_id = x.{col})")[0]["n"]
        if n:
            orphans.append(f"{table}.{col}: {n} row(s) name an item that doesn't exist (no FK, by design)")
    ck.add("Orphans (no FK by design)", True, f"{len(orphans)} kind(s)", orphans)

    # 11. identity
    bad = []
    for table, key in (("Receipts", "ReceiptID"), ("ReceiptItems", "ItemID")):
        last, mx, _ = ident[table]
        tmx = _q(con, f"SELECT MAX({key}) m FROM dbo.{table}")[0]["m"]
        tcur = _q(con, f"SELECT CAST(IDENT_CURRENT('dbo.{table}') AS BIGINT) c")[0]["c"]
        if tmx != mx:
            bad.append(f"{table}: max {key} {tmx}, source {mx}")
        if last is not None and tcur != last:
            bad.append(f"{table}: next identity after {tcur}, source after {last}")
    ck.add("Identity", not bad,
           ", ".join(f"{t} max {ident[t][1]} / last {ident[t][0]}" for t in ("Receipts", "ReceiptItems")), bad)

    # 12. receipts row by row, CreatedAt Eastern -> UTC
    cols = ", ".join(RECEIPT_COLS)
    a = _q(con, f"SELECT ReceiptID, {cols}, CreatedAt FROM [{receipts_db}].dbo.Receipts ORDER BY ReceiptID")
    b = _q(con, f"SELECT ReceiptID, {cols}, CreatedAt FROM dbo.Receipts ORDER BY ReceiptID")
    bad, ambiguous = [], 0
    for x, y in zip(a, b):
        for c in ["ReceiptID"] + RECEIPT_COLS:
            if x[c] != y[c]:
                bad.append(f"Receipt {x['ReceiptID']}: {c} {x[c]!r} vs {y[c]!r}")
        if x["CreatedAt"] is not None:
            early = x["CreatedAt"].replace(tzinfo=EASTERN, fold=0)
            late = x["CreatedAt"].replace(tzinfo=EASTERN, fold=1)
            if early.utcoffset() != late.utcoffset():
                ambiguous += 1
            if early.astimezone(timezone.utc).replace(tzinfo=None) != y["CreatedAt"]:
                bad.append(f"Receipt {x['ReceiptID']}: CreatedAt {x['CreatedAt']} -> {y['CreatedAt']}")
        elif y["CreatedAt"] is not None:
            bad.append(f"Receipt {x['ReceiptID']}: CreatedAt NULL -> {y['CreatedAt']}")
    if len(a) != len(b):
        bad.append(f"{len(a)} source receipts, {len(b)} copied")
    la = _q(con, f"SELECT ItemID, {', '.join(LINE_COLS)} FROM [{receipts_db}].dbo.ReceiptItems ORDER BY ItemID")
    lb = _q(con, f"SELECT ItemID, {', '.join(LINE_COLS)} FROM dbo.ReceiptItems ORDER BY ItemID")
    bad += [f"line {x['ItemID']}: {x} vs {y}" for x, y in zip(la, lb) if x != y]
    if len(la) != len(lb):
        bad.append(f"{len(la)} source lines, {len(lb)} copied")
    ck.add("Receipts row by row (CreatedAt Eastern -> UTC)", not bad,
           f"{len(b)} receipts, {len(lb)} lines; {ambiguous} CreatedAt in a DST-ambiguous hour "
           "(earlier offset used)", bad)

    # 13. the eBay token (never printed)
    tok = extras["token"]
    got = _q(con, "SELECT access_token, refresh_token, expires_at FROM dbo.ebay_tokens WHERE id = 1")
    if not tok:
        ck.add("eBay token", not got, "no token in the source", warn=True)
    else:
        g = got[0] if got else {}
        exp_ok = (tok["expires_at"] is None and g.get("expires_at") is None) or (
            g.get("expires_at") is not None and tok["expires_at"] is not None
            and abs(g["expires_at"].replace(tzinfo=timezone.utc).timestamp() - float(tok["expires_at"])) < 1e-5)
        ok = bool(got) and g["access_token"] == tok["access_token"] \
            and g["refresh_token"] == tok["refresh_token"] and exp_ok
        ck.add("eBay token", ok, "copied, identical (values not shown)" if ok else "DIFFERS (values not shown)")

    # 14. source health
    processed = sum(1 for it in items if it.get("processed") is not None)
    ck.add("items.processed is NULL everywhere", processed == 0,
           f"{processed} item(s) with a value (kept in leftover)", warn=True)
    ck.add("SQLite integrity_check", extras["integrity"] == "ok", extras["integrity"])
    extra_tables = {t: n for t, n in extras["tables"].items() if t not in _HANDLED_SQLITE}
    ck.add("SQLite tables not copied", not any(extra_tables.values()),
           ", ".join(f"{t} ({n})" for t, n in extra_tables.items()) or "none",
           [f"{t}: {n} rows" for t, n in extra_tables.items()], warn=not any(extra_tables.values()))
    return ck


# --- report ----------------------------------------------------------------------

def write_report(path: Path, *, ck, timing, args, source_path, source_hash, extras, report,
                 src, target, dev_shape=None, prod_shape=None, dev_leftover=None) -> None:
    lines = []
    w = lines.append
    status = "PASSED" if not ck.failed else f"FAILED ({len(ck.failed)} check(s))"
    w(f"# Migration {'rehearsal' if sqlserver.is_migrate_database(target) else 'run'}: {status}\n")
    w(f"- When: {datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S} UTC")
    w(f"- Source: `{source_path}` ({source_path.stat().st_size:,} bytes, sha256 `{source_hash[:16]}…`)")
    if args.snapshot_from:
        w(f"- Snapshot of: `{args.snapshot_from}` (online backup API, opened read-only)")
    w(f"- SQLite schema_version: {extras['schema_version']}")
    w(f"- Receipts from: `{args.receipts_db}`")
    w(f"- Target: `{target}`\n")
    w("## Timing\n")
    w("| Phase | Seconds |\n|---|---:|")
    for k, v in timing.items():
        w(f"| {k} | {v:.1f} |")
    down = sum(v for k, v in timing.items() if k != "snapshot")
    w(f"| **Downtime (read + copy + verify)** | **{down:.1f}** |\n")
    w("## Checks\n")
    w("| Check | Result | Summary |\n|---|---|---|")
    for name, st, summary, _ in ck.rows:
        w(f"| {name} | {'✅' if st == 'PASS' else ('⚠️' if st == 'WARN' else '❌')} {st} | {summary} |")
    for name, st, summary, details in ck.rows:
        if details and st != "PASS" or (details and name.startswith("Orphans")):
            w(f"\n### {name}\n")
            for d in details[:60]:
                w(f"- {d}")
            if len(details) > 60:
                w(f"- … {len(details) - 60} more")
    w("\n## Row counts (expected from SQLite / ReceiptsDB vs SQL Server)\n")
    w("| Table | Expected | Copied |\n|---|---:|---:|")
    exp = report.get("expected_counts", {})
    for t in sorted(set(exp) | set(report["counts"])):
        if t in exp or report["counts"].get(t):
            w(f"| {t} | {exp.get(t, '')} | {report['counts'].get(t, 0)} |")
    w("\n## Lists (expected = rows + kept whole in leftover JSON)\n")
    w("| List | Expected | Rows | Kept |\n|---|---:|---:|---:|")
    for k, (e, r, kept) in report.get("lists", {}).items():
        w(f"| {k} | {e} | {r} | {kept} |")
    w("\n## Money\n")
    w("| Sum | Old | New |\n|---|---:|---:|")
    for name, a, b in report.get("money", []):
        w(f"| {name} | {a} | {b} |")
    plan, unusable = report.get("pieces_plan", ({}, []))
    w("\n## Pieces\n")
    w(f"- Copied as-is: {len(src['pieces'])} (in {len({p['box'] for p in src['pieces']})} locations)")
    w(f"- New from `stock.location`: {len(report['new_pieces'])} for {len(plan)} items")
    by_loc = Counter(c.rpartition('-')[0] for c in report["new_pieces"])
    if by_loc:
        w("- New per location: " + ", ".join(f"{loc} {n}" for loc, n in sorted(by_loc.items())))
    multi = [(i, u) for i, (_, u) in plan.items() if u > 1]
    if multi:
        w(f"- Items with more than one unit: {len(multi)} (" + ", ".join(f"{i[:8]}×{u}" for i, u in multi[:20]) + ")")
    if unusable:
        w(f"- Locations that can't become pieces: {len(unusable)}")
    w("\n## Leftover JSON keys (no column; review)\n")
    w("| Key | Items | In dev |\n|---|---:|---|")
    for k, n in sorted(report["leftover"].items(), key=lambda kv: (-kv[1], kv[0])):
        indev = "" if dev_leftover is None else ("yes" if k in dev_leftover else "**production only**")
        w(f"| `{k}` | {n} | {indev} |")
    if dev_shape is not None:
        w("\n## Production vs dev (keys, statuses, tables only production has)\n")
        any_ = False
        for g in sorted(prod_shape):
            only = sorted(str(x) for x in prod_shape[g] - dev_shape.get(g, set()))
            if only:
                any_ = True
                w(f"- **{g}**: " + ", ".join(f"`{x}`" for x in only))
        if not any_:
            w("- nothing: every key, status and table in production also exists in dev")
    w("\n## Source tables (SQLite)\n")
    w("| Table | Rows |\n|---|---:|")
    for t, n in extras["tables"].items():
        w(f"| {t} | {n} |")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# --- main ------------------------------------------------------------------------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    src_group = ap.add_mutually_exclusive_group(required=True)
    src_group.add_argument("--snapshot-from", help="live ross.db to snapshot read-only (rehearsal)")
    src_group.add_argument("--source", help="an existing snapshot or backup file")
    ap.add_argument("--target", default=f"{sqlserver.MIGRATE_PREFIX}{datetime.now():%Y%m%d}")
    ap.add_argument("--production", action="store_true", help="allow --target ResaleDB")
    ap.add_argument("--receipts-db", default="ReceiptsDB")
    ap.add_argument("--snapshot-dir", default=str(config.DATA_DIR / "rehearsal"))
    ap.add_argument("--compare-dev", help="a dev ross.db: list what the source has that it doesn't")
    ap.add_argument("--report-dir", default=str(config.LOG_DIR))
    args = ap.parse_args(argv)

    target = args.target
    check_target(target, args.production)
    sqlserver._check_name(args.receipts_db)
    if args.receipts_db.casefold() == target.casefold():
        raise SystemExit("the receipts source and the target are the same database")
    config.SQL_DATABASE = target  # db_mssql reads (verify, consistency) go to the target

    timing = {}
    t0 = time.perf_counter()
    if args.snapshot_from:
        source_path = take_snapshot(Path(args.snapshot_from), Path(args.snapshot_dir))
        timing["snapshot"] = time.perf_counter() - t0
        print(f"Snapshot: {source_path}")
    else:
        source_path = Path(args.source).resolve()
        if not source_path.is_file():
            raise SystemExit(f"no such file: {source_path}")
    source_hash = sha256(source_path)

    # preflight
    import pyodbc
    if config.SQL_DRIVER not in pyodbc.drivers():
        raise SystemExit(f"ODBC driver {config.SQL_DRIVER!r} is not installed")
    master = sqlserver.connect("master", autocommit=True)
    try:
        lang, tz = master.execute("SELECT @@LANGUAGE, CURRENT_TIMEZONE_ID()").fetchone()
        if lang != "us_english":
            raise SystemExit(f"login language is {lang!r}; the receipt bot needs us_english")
        if tz != "Eastern Standard Time":
            raise SystemExit(f"the server's time zone is {tz!r}; ReceiptsDB CreatedAt is converted "
                             "from Eastern, so stop and check")
        if master.execute("SELECT DB_ID(?)", args.receipts_db).fetchone()[0] is None:
            raise SystemExit(f"receipts database {args.receipts_db!r} not found")
    finally:
        master.close()
    created = sqlserver.create_database(target)
    con = sqlserver.connect(target)
    try:
        if not created:
            held = target_rows(con)
            if held:
                raise SystemExit(f"refusing {target!r}: it already holds rows "
                                 f"({', '.join(f'{t} {n}' for t, n in sorted(held.items())[:8])})")
        rcsi = con.execute("SELECT is_read_committed_snapshot_on FROM sys.databases WHERE name = ?",
                           target).fetchone()[0]
        if not rcsi:
            raise SystemExit(f"READ_COMMITTED_SNAPSHOT is off on {target!r}")
        # Only up to the baseline: the unique receipt indexes (step 2) are
        # built after scripts/receipts_cleanup.py has removed the duplicates.
        version = migrations.mssql_current_version(con.cursor())
        con.commit()
        if version > migrations.MSSQL_BEFORE_RECEIPT_CLEANUP:
            raise SystemExit(f"refusing {target!r}: its schema is at version {version}, past "
                             f"{migrations.MSSQL_BEFORE_RECEIPT_CLEANUP}. The receipts must be copied "
                             "before the unique receipt indexes; create the database with this script.")
        migrations.migrate_mssql(con, upto=migrations.MSSQL_BEFORE_RECEIPT_CLEANUP)
        con.commit()

        # read
        t = time.perf_counter()
        src = loader.read_sqlite(source_path)
        extras = read_extras(source_path)
        ident = receipts_identity(con, args.receipts_db)
        timing["read source"] = time.perf_counter() - t

        # copy: receipts and token first, then the loader, whose commit makes
        # it all one transaction (all or nothing)
        t = time.perf_counter()
        cur = con.cursor()
        copy_receipts(cur, args.receipts_db, ident)
        timing["copy receipts"] = time.perf_counter() - t
        t = time.perf_counter()
        copy_token(cur, extras["token"])
        report = loader.load(src, con)
        timing["copy items and operations"] = time.perf_counter() - t

        # verify (on a working copy, so the snapshot stays byte-identical)
        t = time.perf_counter()
        work_dir = Path(args.snapshot_dir) / "work"
        for old in work_dir.glob(source_path.name + "*"):
            old.unlink()
        work_path = take_snapshot(source_path, work_dir, name=source_path.name)
        try:
            ck = run_checks(con, src, extras, report, args.receipts_db, ident, target, source_path, work_path)
        finally:
            import gc
            gc.collect()  # db.py's SQLite connections close when collected
            for p in work_dir.glob(source_path.name + "*"):
                try:
                    p.unlink()
                except PermissionError:
                    print(f"warning: could not delete the working copy {p} (it holds the "
                          "eBay token); delete it by hand", file=sys.stderr)
        timing["verify"] = time.perf_counter() - t
    finally:
        con.close()

    dev_shape = prod_shape = dev_leftover = None
    if args.compare_dev:
        dev_src = loader.read_sqlite(args.compare_dev)
        dev_shape = shape(dev_src, read_extras(args.compare_dev))
        prod_shape = shape(src, extras)
        dev_leftover = set(leftover_keys(dev_src["items"], dev_src["pieces"]))

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    out = Path(args.report_dir) / f"migrate-{target}-{stamp}.md"
    write_report(out, ck=ck, timing=timing, args=args, source_path=source_path, source_hash=source_hash,
                 extras=extras, report=report, src=src, target=target, dev_shape=dev_shape,
                 prod_shape=prod_shape, dev_leftover=dev_leftover)

    for name, st, summary, _ in ck.rows:
        print(f"  {st:4}  {name}: {summary}")
    down = sum(v for k, v in timing.items() if k != "snapshot")
    print(f"Timing: " + ", ".join(f"{k} {v:.1f}s" for k, v in timing.items()) + f"; downtime {down:.1f}s")
    print(f"Report: {out}")
    if ck.failed:
        print("\n" + "!" * 70 + f"\nMIGRATION CHECKS FAILED: {', '.join(r[0] for r in ck.failed)}\n"
              "Do NOT switch to this database. See the report.\n" + "!" * 70, file=sys.stderr)
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
