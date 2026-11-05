"""One-off (approved by Person B, 2026-11-05): give the base-only style codes in
ResaleDB their full code, re-read from the items' own tag photos.

    python scripts/fix_style_codes.py            # dry run: old -> new per item, nothing written
    python scripts/fix_style_codes.py --apply    # backup ResaleDB, then fix, then log

Which items: in brands whose codes carry a print/colour suffix (pipeline.owned),
those stored with the base only. Each full code was read from the item's photos
(hang tag) by the vision model on 2026-11-05 (Gemini, stage
style_code_reread); EHH203-42-003 also by OCR. e5756e70 EHH108-26-TLP agrees
with c57cb4cc, the same Tillie print with the same Ross code.

Not here: 41d6e5f5 (11JCD9NJC: no code readable on its photos) and 3347c963
(EH-H02-26-DHO reads as EHH403-26-DHO: a misread, not a base-only code).

Per item, in item_identification only: model and style_code -> the full code,
only where model is still the old value; one transaction. Each change is
appended to logs/style_code_fixes.jsonl. The eBay listing is not changed.
"""
import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
import sqlserver  # noqa: E402

DATABASE = sqlserver.PRODUCTION_DATABASE
LOG = Path(config.LOG_DIR) / "style_code_fixes.jsonl"
# item_id prefix: (stored model now, full code from the photos, how it was read)
FIXES = {
    "e5756e70": ("EHH108", "EHH108-26-TLP", "vision, hang tag; = c57cb4cc (same Ross code 400298771819)"),
    "8b5ddc05": ("EHH203", "EHH203-42-003", "vision + OCR, hang tag"),
    "f027cb9c": ("XW09620", "XW09620 970 0IC", "vision, hang tag"),
    "f0665406": ("XW08963", "XW08963 971 0GG", "vision, hang tag"),
}


def plan(con) -> list[dict]:
    out = []
    for short, (old, new, how) in FIXES.items():
        rows = con.execute("SELECT i.item_id, i.model, i.style_code, i.brand, it.status FROM dbo.item_identification i "
                           "JOIN dbo.items it ON it.item_id = i.item_id WHERE i.item_id LIKE ?",
                           short + "%").fetchall()
        p = {"short": short, "old": old, "new": new, "how": how, "ok": False}
        if len(rows) != 1:
            out.append({**p, "why": f"{len(rows)} rows"})
            continue
        r = rows[0]
        p.update(item_id=r.item_id, now=r.model, style_code=r.style_code, brand=r.brand, status=r.status)
        if r.model != old:
            p["why"] = f"model is {r.model!r}, not {old!r} (changed since?)"
        else:
            p["ok"] = True
        out.append(p)
    return out


def apply(con, rows, backup: str) -> int:
    cur = con.cursor()
    try:
        for p in rows:
            cur.execute("UPDATE dbo.item_identification SET model = ?, style_code = ? WHERE item_id = ? AND model = ?",
                        p["new"], p["new"], p["item_id"], p["old"])
            if cur.rowcount != 1:
                raise RuntimeError(f"{p['item_id']}: {cur.rowcount} rows updated, expected 1")
        con.commit()
    except Exception:
        con.rollback()
        raise
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as fh:
        for p in rows:
            fh.write(json.dumps({"at": datetime.now(timezone.utc).isoformat(), "database": DATABASE,
                                 "table": "item_identification", "item_id": p["item_id"],
                                 "columns": ["model", "style_code"], "old": p["old"], "new": p["new"],
                                 "old_style_code": p["style_code"], "read": p["how"],
                                 "approved_by": "Person B", "backup": backup}) + "\n")
    return len(rows)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--apply", action="store_true", help="back up, then write (default: dry run)")
    args = ap.parse_args(argv)
    con = sqlserver.connect(DATABASE)
    try:
        rows = plan(con)
        print(f"{DATABASE}: {sum(p['ok'] for p in rows)} of {len(FIXES)} ready")
        for p in rows:
            print(f"  {p['short']} {p.get('brand', '')} ({p.get('status', '?')}): model {p.get('now')!r} -> "
                  f"{p['new']!r} (style_code {p.get('style_code')!r} -> {p['new']!r}) · {p['how']}"
                  + ("" if p["ok"] else f"  SKIP: {p['why']}"))
        if not args.apply:
            print("Dry run: nothing written. Run with --apply to back up and fix.")
            return 0
        if not all(p["ok"] for p in rows):
            print("Not applying: every item must be ready.")
            return 1
        backup = sqlserver.backup_database(DATABASE, label="before-style-code-fix")
        print(f"Backup: {backup}")
        print(f"Fixed {apply(con, rows, backup)}; logged to {LOG}")
        return 0
    finally:
        con.close()


if __name__ == "__main__":
    sys.exit(main())
