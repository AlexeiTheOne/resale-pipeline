"""Offsite-friendly backup of the bot's state.

Two things hold the whole business and neither is in git (data/ is gitignored):
  • data/ross.db — items, receipt/cost history, the eBay OAuth tokens, and the
    taxonomy cache.
  • data/inbox/  — the only copy of each item's photos until they reach Cloudinary.
  • data/pieces/ — the /inv inventory photos (the tag readings are in the DB).

Run this on a schedule (Windows Task Scheduler / cron). Point BACKUP_DIR at a
synced folder (OneDrive / Dropbox / Google Drive) to get the data off the machine:

    python backup.py

The DB is copied with SQLite's online-backup API, so it's safe to run while the
bot is live — a plain file copy of a WAL database can miss committed rows still
sitting in the -wal file. The online backup takes a consistent snapshot instead.
"""
import os
import shutil
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import config  # noqa: E402
from config import DATA_DIR, resolve_path  # noqa: E402

DB_PATH = DATA_DIR / "ross.db"
INBOX = DATA_DIR / "inbox"
PIECES = DATA_DIR / "pieces"
BACKUP_DIR = resolve_path(os.getenv("BACKUP_DIR") or DATA_DIR / "backups")
KEEP = int(os.getenv("BACKUP_KEEP", "14"))  # snapshots of each kind to retain


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


# Snapshots are written under this prefix and renamed into place only once they
# are complete. Two reasons a half-written file must never carry its final name:
# prune() keeps the KEEP most recent by mtime, so a truncated snapshot from an
# interrupted run counts as a keeper and evicts a good older one; and a restore
# reaches for the newest file, which is exactly the broken one. The prefix is
# deliberately outside prune()'s "ross-"/"inbox-" globs so leftovers can't be
# mistaken for snapshots — _sweep_partials clears them at the start of each run.
PARTIAL = ".partial-"


def _sweep_partials(dest: Path) -> None:
    for p in dest.glob(f"{PARTIAL}*"):
        p.unlink(missing_ok=True)


def backup_db(dest: Path) -> Path:
    if config.DB_BACKEND == "sqlserver":
        return backup_sqlserver(dest)
    import sqlite3
    if not DB_PATH.exists():
        raise FileNotFoundError(f"{DB_PATH} not found — nothing to back up.")
    out = dest / f"ross-{_stamp()}.db"
    tmp = dest / f"{PARTIAL}{out.name}"
    # Deliberately raw sqlite3, not db.connect(): importing db runs the
    # migrations, and a backup must still work when the app code is broken.
    src = sqlite3.connect(str(DB_PATH))
    try:
        dst = sqlite3.connect(str(tmp))
        try:
            src.backup(dst)  # consistent, WAL-safe online snapshot
        finally:
            dst.close()
    finally:
        src.close()
    tmp.replace(out)
    return out


def backup_inbox(dest: Path) -> Path | None:
    """Zip the photo folders (inbox/ and pieces/) into one inbox-<stamp>.zip."""
    files = [(p, p.relative_to(d.parent)) for d in (INBOX, PIECES) if d.exists()
             for p in d.rglob("*") if p.is_file()]
    if not files:
        return None
    out = dest / f"inbox-{_stamp()}.zip"
    tmp = dest / f"{PARTIAL}{out.name}"
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        for p, name in files:
            z.write(p, name)
    tmp.replace(out)
    return out


def backup_sqlserver(dest: Path) -> Path:
    """DB_BACKEND=sqlserver: BACKUP DATABASE (COPY_ONLY, CHECKSUM, verified),
    written by the SQL Server service to SQL_BACKUP_DIR, then copied here as
    ross-<time>.bak so prune() and the offsite folder work as for SQLite.

    If this user can't read the server's folder (e.g. SQL_BACKUP_DIR unset, so
    the instance's own Backup folder), the backup still exists there: say so
    and return its path, rather than failing the deploy that called us. Set up
    SQL_BACKUP_DIR as docs/schema-proposal.md 7.1 describes."""
    import sqlserver  # not db: importing db runs the migrations
    bak = Path(sqlserver.backup_database(config.SQL_DATABASE))
    out = dest / f"ross-{_stamp()}.bak"
    tmp = dest / f"{PARTIAL}{out.name}"
    try:
        shutil.copy2(bak, tmp)
    except OSError as e:
        tmp.unlink(missing_ok=True)
        print(f"[warn] SQL Server backup made at {bak}, but it could not be copied to {dest} "
              f"({type(e).__name__}). Set SQL_BACKUP_DIR to a folder this user can read (doc 7.1).")
        return bak
    tmp.replace(out)
    return out


def prune(dest: Path, prefix: str, keep: int) -> None:
    snaps = sorted(dest.glob(f"{prefix}*"), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in snaps[keep:]:
        old.unlink()


def main(argv: list[str] | None = None) -> None:
    """Back up the DB and the inbox. With --db-only (used by scripts/deploy.ps1
    before each deploy) only the DB: the inbox zip is ~200 MB and only the
    nightly run needs it."""
    db_only = "--db-only" in (sys.argv[1:] if argv is None else argv)
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    _sweep_partials(BACKUP_DIR)

    db_out = backup_db(BACKUP_DIR)
    size = f" ({db_out.stat().st_size / 1024:.0f} KB)" if db_out.parent == BACKUP_DIR else ""
    print(f"[ok] DB backed up  -> {db_out}{size}")
    if db_only:
        prune(BACKUP_DIR, "ross-", KEEP)
        print(f"[--] DB only. Kept the {KEEP} most recent DB backups in {BACKUP_DIR}.")
        return

    inbox_out = backup_inbox(BACKUP_DIR)
    if inbox_out:
        print(f"[ok] Inbox zipped  -> {inbox_out} ({inbox_out.stat().st_size / 1024 / 1024:.1f} MB)")
    else:
        print("[--] No inbox photos to back up.")

    prune(BACKUP_DIR, "ross-", KEEP)
    prune(BACKUP_DIR, "inbox-", KEEP)
    print(f"[--] Kept the {KEEP} most recent of each kind in {BACKUP_DIR}.")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"[FAIL] Backup failed: {type(e).__name__}: {e}")
        sys.exit(1)
