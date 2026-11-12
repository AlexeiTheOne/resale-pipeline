"""The old eBay store's sales (2021 to Aug 2026) as /check evidence.

scripts/import_old_store.py turns the old store's workbook into a CSV (with
brand, parent brand, type and bag size from one AI pass). The owner sends
that CSV to the bot (bot/old_store_cmds.py), which backs up the DB and then
replaces every old_store row (import_csv). The repo is public, so the data
itself never goes into git.

/check uses it in level 3 (our own sales) when the current store has fewer
than CHECK_OLD_STORE_MIN_SALES sales of the brand and type: brand + type +
bag size when that has enough sales, else brand + type, else the parent
brand for a sub-line ("Luv Betsey" -> Betsey Johnson). Only periods ending
in the last CHECK_OLD_STORE_YEARS years count by default. "Enough sales"
counts listings (rows); medians and the count shown are over units sold
(a row of 3 units at $20 counts 3 times).
"""
from __future__ import annotations

import csv
import re
import statistics
import time
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

import config
import db

# Our type names -> msrp.type_bucket's (the ladder matches on those).
TYPE_BUCKETS = {"bag": "bag", "backpack": "bag", "wallet": "bag", "luggage": "bag",
                "watch_band": "watch band", "watch": "watch", "sunglasses": "sunglasses",
                "bedding": "bedding", "fragrance": "fragrance"}
ITEM_TYPES = ("bag", "wallet", "backpack", "luggage", "watch_band", "watch", "jewelry",
              "sunglasses", "fragrance", "beauty", "bedding", "clothing", "shoes",
              "accessory", "home", "electronics", "toy", "other")
_INT = {"source_row", "quantity", "best_offer_units", "seller_offer_units", "year_inferred"}
_FLOAT = {"item_sales", "unit_price", "shipping_per_unit", "label_per_unit", "fees_per_unit",
          "net_per_unit", "price_paid", "cost_per_unit", "msrp"}
MAX_ROWS = 20000
CACHE_S = 600

_cache: dict = {}


class CsvError(ValueError):
    """The file isn't the old store CSV (or a row is malformed)."""


def _date(v: str, what: str) -> str:
    try:
        return date.fromisoformat(v).isoformat()
    except ValueError:
        raise CsvError(f"{what} {v!r} is not a date (YYYY-MM-DD)") from None


def read_csv(path: Path) -> list[dict]:
    """Rows of the CSV written by scripts/import_old_store.py, checked and
    typed. Raises CsvError on anything else."""
    with Path(path).open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        if tuple(reader.fieldnames or ()) != db.OLD_STORE_COLUMNS:
            raise CsvError("the columns aren't the old store CSV's "
                           "(make it with scripts/import_old_store.py classify)")
        rows = []
        for n, raw in enumerate(reader, start=2):
            if n > MAX_ROWS:
                raise CsvError(f"more than {MAX_ROWS} rows")
            r: dict = {}
            try:
                for k, v in raw.items():
                    v = (v or "").strip()
                    if v == "":
                        r[k] = None
                    elif k in _INT:
                        r[k] = int(float(v))
                    elif k in _FLOAT:
                        r[k] = round(float(v), 2)
                    else:
                        r[k] = v
            except ValueError as e:
                raise CsvError(f"line {n}: {e}") from None
            where = f"line {n}:"
            if r["source"] != "old_store":
                raise CsvError(f"{where} source must be old_store")
            if not r["title"] or not r["source_tab"] or r["source_row"] is None:
                raise CsvError(f"{where} title, source_tab and source_row are required")
            if not r["quantity"] or r["quantity"] < 1 or r["item_sales"] is None or r["unit_price"] is None:
                raise CsvError(f"{where} quantity, item_sales and unit_price are required")
            r["period_start"] = _date(r["period_start"] or "", f"{where} period_start")
            r["period_end"] = _date(r["period_end"] or "", f"{where} period_end")
            if r["status"] not in ("ok", "to_check"):
                raise CsvError(f"{where} status must be ok or to_check")
            if r["bag_size"] not in (None, "large", "small"):
                raise CsvError(f"{where} bag_size must be large, small or empty")
            if r["item_type"] not in (None, *ITEM_TYPES):
                raise CsvError(f"{where} unknown item_type {r['item_type']!r}")
            r["year_inferred"] = 1 if r["year_inferred"] else 0
            rows.append(r)
    keys = Counter((r["source_tab"], r["source_row"]) for r in rows)
    dup = next((k for k, c in keys.items() if c > 1), None)
    if dup:
        raise CsvError(f"tab {dup[0]!r} row {dup[1]} appears twice")
    if not rows:
        raise CsvError("no rows")
    return rows


def import_csv(path: Path, backup_fn=None) -> dict:
    """Back up the DB, then replace every old_store row with the CSV's (one
    transaction). Nothing is written if the CSV or the backup fails. Returns
    {"rows", "replaced", "backup", "recent", "units"}."""
    rows = read_csv(path)
    if backup_fn is None:
        import backup
        backup_fn = lambda: backup.backup_db(config.DATA_DIR / "backups")  # noqa: E731
    bak = backup_fn()
    saved = db.replace_old_store_sales(rows)
    _cache.clear()
    since = window_start()
    return {**saved, "backup": str(bak), "units": sum(r["quantity"] for r in rows),
            "recent": sum(1 for r in rows if r["period_end"] >= since)}


def window_start(today: date | None = None, years: float | None = None) -> str:
    years = config.CHECK_OLD_STORE_YEARS if years is None else years
    return ((today or date.today()) - timedelta(days=round(365.25 * years))).isoformat()


def brand_key(brand) -> str | None:
    """Lowercase words, no punctuation. Unlike msrp.brand_key there are no
    aliases: sub-lines stay apart ("Lauren Ralph Lauren" is not "Ralph Lauren")."""
    b = re.sub(r"\s+", " ", re.sub(r"[^a-z0-9&]", " ", str(brand or "").lower())).strip()
    return b or None


def resolve_brand(brand, keys) -> str | None:
    """The old store brand key for an identified brand: the same words, else
    the longest old store brand the name starts with ("Luv Betsey by Betsey
    Johnson" -> "luv betsey", "Kate Spade New York" -> "kate spade"), else
    one that starts with the name ("Herschel" -> "herschel supply co")."""
    b = brand_key(brand)
    if not b:
        return None
    if b in keys:
        return b
    longer = [k for k in keys if b.startswith(k + " ")]
    if longer:
        return max(longer, key=len)
    shorter = sorted(k for k in keys if k.startswith(b + " "))
    return shorter[0] if len(shorter) == 1 else None


def _shape(r: dict) -> dict:
    return {"brand": r.get("brand"), "brand_key": brand_key(r.get("brand")),
            "parent_key": brand_key(r.get("parent_brand")), "parent_brand": r.get("parent_brand"),
            "bucket": TYPE_BUCKETS.get(r.get("item_type") or "", "other"), "bag_size": r.get("bag_size"),
            "price": r["unit_price"], "units": int(r.get("quantity") or 1),
            "period_end": r["period_end"]}


def recent_rows(years: float | None = None) -> list[dict]:
    """Old store sales whose period ended in the last `years` years
    (default CHECK_OLD_STORE_YEARS), shaped for match(). Cached for 10 min."""
    since = window_start(years=years)
    hit = _cache.get(since)
    if hit and time.monotonic() - hit[0] < CACHE_S:
        return hit[1]
    rows = [_shape(r) for r in db.old_store_sales(since)]
    _cache.clear()
    _cache[since] = (time.monotonic(), rows)
    return rows


def _median_units(rows: list[dict]) -> float:
    return round(statistics.median([r["price"] for r in rows for _ in range(r["units"])]), 2)


def _result(rows: list[dict], brand: str, bucket: str, size: str | None, match: str) -> dict:
    return {"value": _median_units(rows), "n": sum(r["units"] for r in rows), "brand": brand,
            "bucket": bucket, "size": size, "match": match,
            "since": min(r["period_end"] for r in rows)[:4]}


def match(brand, bucket: str, size: str | None, rows: list[dict], min_sales: int | None = None) -> dict | None:
    """The old store's median for this brand and type bucket, or None.
    Order, each needing min_sales listings: brand + type + size (bags, when
    the size is known), the parent brand's family (parent + its sub-lines)
    + type + size for a sub-line, brand + type, parent family + type; then
    brand + type with whatever it has.
    The median and n are over units sold."""
    min_sales = config.CHECK_OLD_STORE_MIN_SALES if min_sales is None else min_sales
    if not rows:
        return None
    bk = resolve_brand(brand, {r["brand_key"] for r in rows if r["brand_key"]})
    if not bk:
        return None
    same = [r for r in rows if r["brand_key"] == bk and r["bucket"] == bucket]
    label = Counter(r["brand"] for r in same).most_common(1)[0][0] if same else brand
    sized = bucket == "bag" and size in ("large", "small")

    fam, plabel = [], None
    parents = Counter(r["parent_key"] for r in rows if r["brand_key"] == bk and r["parent_key"])
    if parents:
        pk = parents.most_common(1)[0][0]
        fam = [r for r in rows if r["bucket"] == bucket and pk in (r["brand_key"], r["parent_key"])]
        plabel = Counter(r["parent_brand"] for r in rows if r["parent_key"] == pk).most_common(1)[0][0]
    # Same size first (a weekender's price says little about a wallet's,
    # and wallets share the bag bucket), then the brand alone.
    steps = []
    if sized:
        steps += [([r for r in same if r["bag_size"] == size], label, size, "brand + type + size"),
                  ([r for r in fam if r["bag_size"] == size], plabel, size, "parent brand + type + size")]
    steps += [(same, label, None, "brand + type"), (fam, plabel, None, "parent brand + type")]
    for rs, name, sz, how in steps:
        # listings, not units: one listing of 3 at a clearance price is one sale
        if len(rs) >= min_sales:
            return _result(rs, name, bucket, sz, how)
    if same:
        return _result(same, label, bucket, None, "brand + type")
    return None
