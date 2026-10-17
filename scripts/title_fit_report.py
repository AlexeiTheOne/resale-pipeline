"""Show what fit_title would do to over-long titles — read-only, for approval.

Nothing here writes: the DB is opened with mode=ro and no eBay call is made.
The output is for the owner to approve before any title is pushed live.

    python scripts/title_fit_report.py              # the 7 titles eBay truncated
    python scripts/title_fit_report.py bff24df0 ... # specific item id prefixes
    python scripts/title_fit_report.py --all        # every stored title over 80
    python scripts/title_fit_report.py --db C:/Ebay-list/data/ross.db ...
"""
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pipeline.title import TITLE_MAX_LEN, fit_title  # noqa: E402

# The live listings whose titles the old _truncate_title cut (verified 2026).
TRUNCATED = ["bff24df0", "a129b1b5", "2f57c0f4", "ed54b249",
             "182a5ab3", "0e48e50b", "db2380ba"]


def main(argv: list[str]) -> None:
    db = ROOT / "data" / "ross.db"
    if argv[:1] == ["--db"]:
        db, argv = Path(argv[1]), argv[2:]
    if not db.exists():
        raise SystemExit(f"{db} not found")
    con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    if argv == ["--all"]:
        rows = con.execute("SELECT item_id, identification, listing FROM items "
                           "WHERE listing IS NOT NULL").fetchall()
        rows = [r for r in rows
                if len((json.loads(r[2]) or {}).get("title") or "") > TITLE_MAX_LEN]
    else:
        rows = []
        for prefix in argv or TRUNCATED:
            r = con.execute("SELECT item_id, identification, listing FROM items "
                            "WHERE item_id LIKE ?", (prefix + "%",)).fetchone()
            if r is None:
                print(f"{prefix}: not found")
            else:
                rows.append(r)

    for item_id, ident_raw, listing_raw in rows:
        ident = json.loads(ident_raw or "{}") or {}
        listing = json.loads(listing_raw or "{}") or {}
        title = listing.get("title") or ""
        fitted, dropped = fit_title(title, ident, listing.get("item_specifics"))
        print(f"{item_id[:8]}  {len(title)} -> {len(fitted)}")
        print(f"  stored:   {title}")
        print(f"  proposed: {fitted}")
        print(f"  dropped:  {', '.join(dropped) or '(nothing)'}")
        print()


if __name__ == "__main__":
    main(sys.argv[1:])
