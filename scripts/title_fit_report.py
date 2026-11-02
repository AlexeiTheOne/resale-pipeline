"""Show what fit_title would do to over-long titles — read-only, for approval.

Nothing here writes: the DB is only read (through db.py) and no eBay call is made.
The output is for the owner to approve before any title is pushed live.

    python scripts/title_fit_report.py              # the 7 titles eBay truncated
    python scripts/title_fit_report.py bff24df0 ... # specific item id prefixes
    python scripts/title_fit_report.py --all        # every stored title over 80
    python scripts/title_fit_report.py --db C:/Ebay-list/data/ross.db ...   # SQLite only
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import db  # noqa: E402
from pipeline.title import TITLE_MAX_LEN, fit_title  # noqa: E402

# The live listings whose titles the old _truncate_title cut (verified 2026).
TRUNCATED = ["bff24df0", "a129b1b5", "2f57c0f4", "ed54b249",
             "182a5ab3", "0e48e50b", "db2380ba"]


def main(argv: list[str]) -> None:
    if argv[:1] == ["--db"]:
        if db._on_sqlserver():
            raise SystemExit("--db is a SQLite file; with DB_BACKEND=sqlserver use SQL_DATABASE")
        db_path, argv = Path(argv[1]), argv[2:]
        if not db_path.exists():
            raise SystemExit(f"{db_path} not found")
        db.DB_PATH = str(db_path)
    elif not db._on_sqlserver() and not Path(db.DB_PATH).exists():
        raise SystemExit(f"{db.DB_PATH} not found")
    items = db.list_items()
    if argv == ["--all"]:
        rows = [it for it in items if it.get("listing") is not None
                and len((it["listing"] or {}).get("title") or "") > TITLE_MAX_LEN]
    else:
        rows = []
        for prefix in argv or TRUNCATED:
            found = next((it for it in items if it["item_id"].startswith(prefix)), None)
            if found is None:
                print(f"{prefix}: not found")
            else:
                rows.append(found)

    for it in rows:
        item_id = it["item_id"]
        ident = it.get("identification") or {}
        listing = it.get("listing") or {}
        title = listing.get("title") or ""
        fitted, dropped = fit_title(title, ident, listing.get("item_specifics"))
        print(f"{item_id[:8]}  {len(title)} -> {len(fitted)}")
        print(f"  stored:   {title}")
        print(f"  proposed: {fitted}")
        print(f"  dropped:  {', '.join(dropped) or '(nothing)'}")
        print()


if __name__ == "__main__":
    main(sys.argv[1:])
