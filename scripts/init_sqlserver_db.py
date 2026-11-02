"""Create a ResaleDB database if missing and bring it to the latest schema.

    python scripts/init_sqlserver_db.py                 # config.SQL_DATABASE (.env)
    python scripts/init_sqlserver_db.py ResaleDB_Dev
    python scripts/init_sqlserver_db.py ResaleDB --production

Only ever adds: it creates the database if it doesn't exist and runs the
SQL Server migrations that haven't run yet (migrations.migrate_mssql). It
never drops or empties anything. Production (ResaleDB) needs --production,
because dev and production share the instance on the production PC.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
import migrations  # noqa: E402
import sqlserver  # noqa: E402

PRODUCTION = "ResaleDB"


def check_target(name: str | None, production: bool) -> str:
    """The database to initialise, or SystemExit with the reason."""
    if not name:
        raise SystemExit("No database: pass one or set SQL_DATABASE in .env (ResaleDB_Dev on dev).")
    if name.lower() == PRODUCTION.lower() and not production:
        raise SystemExit(f"{name} is production. Re-run with --production if you mean it.")
    if name.lower() == "receiptsdb":
        raise SystemExit("ReceiptsDB belongs to receipt_pipeline; it is not a ResaleDB database.")
    return name


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("database", nargs="?", default=config.SQL_DATABASE)
    ap.add_argument("--production", action="store_true", help="allow ResaleDB itself")
    args = ap.parse_args(argv)
    name = check_target(args.database, args.production)

    created = sqlserver.create_database(name)
    print(f"{name}: {'created' if created else 'already exists'} on {config.SQL_SERVER}")
    con = sqlserver.connect(name)
    try:
        ran = migrations.migrate_mssql(con)
        cur = con.cursor()
        cur.execute("SELECT MAX(version) FROM dbo.schema_version")
        version = cur.fetchone()[0]
        cur.execute("SELECT type_desc, COUNT(*) FROM sys.objects WHERE is_ms_shipped = 0 "
                    "AND type IN ('U', 'V') GROUP BY type_desc")
        counts = {r[0]: r[1] for r in cur.fetchall()}
        con.commit()
    finally:
        con.close()
    print(f"migrations run: {ran or 'none (up to date)'}; schema version {version}; "
          f"{counts.get('USER_TABLE', 0)} tables, {counts.get('VIEW', 0)} views")
    return 0


if __name__ == "__main__":
    sys.exit(main())
