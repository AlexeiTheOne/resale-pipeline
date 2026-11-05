"""One-off (approved by Person B, 2026-11-05): fix 6 wrong Ross codes in ResaleDB
with the codes decoded from their own tag photos.

    python scripts/fix_ross_codes.py            # dry run: old -> new per item, nothing written
    python scripts/fix_ross_codes.py --apply    # backup ResaleDB, then fix, then log

Found by decoding every stored tag photo (docs/check-command-design.md): 4
items got placeholder codes and 2 got a one-digit misread, all because the
intake barcode reader doesn't rotate the photo (each barcode decodes only at
15-90 degrees). Each code is decoded again here, with /check's rotating
reader, and must still match what was found.

What changes, per item, in item_tags only: ross_code -> the decoded code,
code_generated -> 0, ross_barcode -> the decoded barcode when it was empty.
The paid price (ross_price) is NOT changed. One transaction; a row whose
code is no longer the old one stops everything. Every change is appended to
logs/ross_code_fixes.jsonl. The eBay SKU is the item id, so eBay is untouched.
"""
import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
import sqlserver  # noqa: E402
from pipeline import check  # noqa: E402

DATABASE = sqlserver.PRODUCTION_DATABASE
PHOTOS_ROOT = Path(r"C:\Users\boris\Projects\ebay-list")     # production's data/ (photos read only)
LOG = Path(config.LOG_DIR) / "ross_code_fixes.jsonl"
# item_id prefix: (stored code now, code decoded from the tag photo on 2026-11-05)
FIXES = {
    "0d49692b": ("000000000111", "400252597608"),
    "55f92238": ("000000000108", "400256252145"),
    "7ad33913": ("000000000106", "400266922212"),
    "c47e6fb7": ("000000000105", "400254110232"),
    "db2380ba": ("490298209190", "400298209190"),
    "ece35ff3": ("400996782350", "400296782350"),
}


def plan(con, decode=check.read_ross_barcode, photos_root=PHOTOS_ROOT) -> list[dict]:
    """Per item: current row, the code decoded now, and whether it can be fixed."""
    out = []
    for short, (old, expected) in FIXES.items():
        rows = con.execute("SELECT item_id, ross_code, code_generated, ross_barcode, ross_price, tag_photo_path "
                           "FROM dbo.item_tags WHERE item_id LIKE ?", short + "%").fetchall()
        p = {"short": short, "old": old, "expected": expected, "ok": False}
        if len(rows) != 1:
            out.append({**p, "why": f"{len(rows)} rows for {short}"})
            continue
        r = rows[0]
        got = decode(str(photos_root / r.tag_photo_path)) if r.tag_photo_path else {}
        digits = f"{got['code']}{round(got['reduced_price'] * 100):06d}" if got.get("reduced_price") else None
        p.update(item_id=r.item_id, now=r.ross_code, generated=r.code_generated, barcode=r.ross_barcode,
                 paid=float(r.ross_price) if r.ross_price is not None else None, photo=r.tag_photo_path,
                 decoded=got.get("code"), decoded_barcode=digits, decoded_price=got.get("reduced_price"))
        if r.ross_code != old:
            p["why"] = f"stored code is {r.ross_code}, not {old} (changed since?)"
        elif got.get("code") != expected:
            p["why"] = f"the photo decodes as {got.get('code')}, not {expected}"
        else:
            p["ok"] = True
        out.append(p)
    return out


def show(rows) -> None:
    for p in rows:
        if not p.get("item_id"):
            print(f"  {p['short']}: SKIP {p['why']}")
            continue
        price = "" if p["decoded_price"] is None or p["paid"] == p["decoded_price"] else \
            f" (paid stored ${p['paid']:.2f}, tag barcode says ${p['decoded_price']:.2f}: price NOT changed)"
        print(f"  {p['item_id'][:8]}: {p['now']} -> {p['decoded']}  code_generated {p['generated']} -> 0"
              + (f", barcode -> {p['decoded_barcode']}" if not p["barcode"] and p["decoded_barcode"] else "")
              + price + ("" if p["ok"] else f"  SKIP: {p['why']}"))


def _clear_json_barcode(cur, item_id: str) -> int:
    """The item's JSON copy of the tag kept "barcode": null (it had none);
    with the column filled that disagrees (db_mssql.consistency_report). The
    null is removed, so the column is the one value."""
    cur.execute("UPDATE dbo.items SET extra_raw = JSON_MODIFY(extra_raw, '$.receipt.barcode', NULL) "
                "WHERE item_id = ? AND ISJSON(extra_raw) = 1 "
                "AND JSON_VALUE(extra_raw, '$.receipt.barcode') IS NULL "
                "AND EXISTS (SELECT 1 FROM OPENJSON(extra_raw, '$.receipt') WHERE [key] = 'barcode')", item_id)
    return cur.rowcount


def json_fix(con) -> int:
    """For the rows fixed on 2026-11-05 before _clear_json_barcode existed."""
    cur = con.cursor()
    n = 0
    try:
        for short, (_old, new) in FIXES.items():
            row = cur.execute("SELECT item_id FROM dbo.item_tags WHERE item_id LIKE ? AND ross_code = ? "
                              "AND ross_barcode IS NOT NULL", short + "%", new).fetchone()
            if row:
                n += _clear_json_barcode(cur, row.item_id)
        con.commit()
    except Exception:
        con.rollback()
        raise
    with LOG.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"at": datetime.now(timezone.utc).isoformat(), "database": DATABASE,
                             "table": "items", "column": "extra_raw $.receipt.barcode",
                             "change": "removed the stale null (the column now holds the barcode)",
                             "rows": n, "approved_by": "Person B"}) + "\n")
    return n


def apply(con, rows, backup: str) -> int:
    cur = con.cursor()
    done = []
    try:
        for p in rows:
            cur.execute("UPDATE dbo.item_tags SET ross_code = ?, code_generated = 0, "
                        "ross_barcode = COALESCE(ross_barcode, ?) WHERE item_id = ? AND ross_code = ?",
                        p["decoded"], p["decoded_barcode"], p["item_id"], p["old"])
            if cur.rowcount != 1:
                raise RuntimeError(f"{p['item_id']}: {cur.rowcount} rows updated, expected 1")
            _clear_json_barcode(cur, p["item_id"])
            done.append(p)
        con.commit()
    except Exception:
        con.rollback()
        raise
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as fh:
        for p in done:
            fh.write(json.dumps({"at": datetime.now(timezone.utc).isoformat(), "database": DATABASE,
                                 "table": "item_tags", "item_id": p["item_id"], "column": "ross_code",
                                 "old": p["old"], "new": p["decoded"],
                                 "barcode_set": None if p["barcode"] else p["decoded_barcode"],
                                 "photo": p["photo"], "reason": "tag photo decodes when rotated",
                                 "approved_by": "Person B", "backup": backup}) + "\n")
    return len(done)


# --prices (approved by Person B, 2026-11-05, after the code fix): the 4 typed
# prices, a cent off the tag, set to the price in the tag's barcode.
# item_id prefix: (stored price now, barcode price)
PRICE_FIXES = {
    "0d49692b": (10.00, 9.99),
    "55f92238": (10.00, 9.99),
    "7ad33913": (10.00, 9.99),
    "c47e6fb7": (13.00, 12.99),
}


def price_plan(con, read=None, photos_root=PHOTOS_ROOT) -> list[dict]:
    import ross_tag
    read = read or ross_tag.read_barcodes
    out = []
    for short, (old, expected) in PRICE_FIXES.items():
        rows = con.execute("SELECT item_id, ross_code, ross_price, tag_photo_path FROM dbo.item_tags "
                           "WHERE item_id LIKE ?", short + "%").fetchall()
        p = {"short": short, "old": old, "expected": expected, "ok": False}
        if len(rows) != 1:
            out.append({**p, "why": f"{len(rows)} rows"})
            continue
        r = rows[0]
        got = ross_tag.split_codes(read(str(photos_root / r.tag_photo_path))) if r.tag_photo_path else {}
        p.update(item_id=r.item_id, code=r.ross_code, now=float(r.ross_price) if r.ross_price is not None else None,
                 decoded_code=got.get("code"), decoded=got.get("reduced_price"), photo=r.tag_photo_path)
        if p["now"] != old:
            p["why"] = f"stored price is {p['now']}, not {old}"
        elif got.get("code") != r.ross_code or got.get("reduced_price") != expected:
            p["why"] = f"the photo reads {got.get('code')} ${got.get('reduced_price')}, expected {r.ross_code} ${expected}"
        else:
            p["ok"] = True
        out.append(p)
    return out


def apply_prices(con, rows, backup: str) -> int:
    cur = con.cursor()
    try:
        for p in rows:
            cur.execute("UPDATE dbo.item_tags SET ross_price = ? WHERE item_id = ? AND ross_price = ?",
                        p["decoded"], p["item_id"], p["old"])
            if cur.rowcount != 1:
                raise RuntimeError(f"{p['item_id']}: {cur.rowcount} rows updated, expected 1")
        con.commit()
    except Exception:
        con.rollback()
        raise
    with LOG.open("a", encoding="utf-8") as fh:
        for p in rows:
            fh.write(json.dumps({"at": datetime.now(timezone.utc).isoformat(), "database": DATABASE,
                                 "table": "item_tags", "item_id": p["item_id"], "column": "ross_price",
                                 "old": p["old"], "new": p["decoded"], "photo": p["photo"],
                                 "reason": "typed price, a cent off the tag barcode",
                                 "approved_by": "Person B", "backup": backup}) + "\n")
    return len(rows)


def prices(con, do_apply: bool) -> int:
    rows = price_plan(con)
    print(f"{DATABASE} prices: {sum(p['ok'] for p in rows)} of {len(PRICE_FIXES)} ready")
    for p in rows:
        print(f"  {p['short']}: ${p.get('now')} -> ${p.get('decoded')} (code {p.get('code')})"
              + ("" if p["ok"] else f"  SKIP: {p['why']}"))
    if not do_apply:
        print("Dry run: nothing written. Run with --prices --apply to back up and fix.")
        return 0
    if not all(p["ok"] for p in rows):
        print("Not applying: every item must be ready.")
        return 1
    backup = sqlserver.backup_database(DATABASE, label="before-ross-price-fix")
    print(f"Backup: {backup}")
    print(f"Fixed {apply_prices(con, rows, backup)}; logged to {LOG}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--apply", action="store_true", help="back up, then write (default: dry run)")
    ap.add_argument("--json-fix", action="store_true", help="only the JSON-copy follow-up for applied rows")
    ap.add_argument("--prices", action="store_true", help="the 4 typed prices (dry run unless --apply)")
    args = ap.parse_args(argv)
    con = sqlserver.connect(DATABASE)
    try:
        if args.prices:
            return prices(con, args.apply)
        if args.json_fix:
            print(f"JSON copies fixed: {json_fix(con)}; logged to {LOG}")
            return 0
        rows = plan(con)
        print(f"{DATABASE}: {sum(p['ok'] for p in rows)} of {len(FIXES)} ready")
        show(rows)
        if not args.apply:
            print("Dry run: nothing written. Run with --apply to back up and fix.")
            return 0
        if not all(p["ok"] for p in rows):
            print("Not applying: every item must be ready.")
            return 1
        backup = sqlserver.backup_database(DATABASE, label="before-ross-code-fix")
        print(f"Backup: {backup}")
        n = apply(con, rows, backup)
        print(f"Fixed {n}; logged to {LOG}")
        return 0
    finally:
        con.close()


if __name__ == "__main__":
    sys.exit(main())
