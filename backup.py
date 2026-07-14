"""Offsite-friendly backup of the bot's state.

Two things hold the whole business and neither is in git (data/ is gitignored):
  • data/ross.db — items, receipt/cost history, the eBay OAuth tokens, and the
    taxonomy cache.
  • data/inbox/  — the only copy of each item's photos until they reach Cloudinary.

Run this on a schedule (Windows Task Scheduler / cron). Point BACKUP_DIR at a
synced folder (OneDrive / Dropbox / Google Drive) to get the data off the machine:

    python backup.py

The DB is copied with SQLite's online-backup API, so it's safe to run while the
bot is live — a plain file copy of a WAL database can miss committed rows still
sitting in the -wal file. The online backup takes a consistent snapshot instead.
"""
import os
import sqlite3
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

DB_PATH = Path("data/ross.db")
INBOX = Path("data/inbox")
BACKUP_DIR = Path(os.getenv("BACKUP_DIR", "data/backups"))
KEEP = int(os.getenv("BACKUP_KEEP", "14"))  # snapshots of each kind to retain


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def backup_db(dest: Path) -> Path:
    if not DB_PATH.exists():
        raise FileNotFoundError(f"{DB_PATH} not found — nothing to back up.")
    out = dest / f"ross-{_stamp()}.db"
    src = sqlite3.connect(str(DB_PATH))
    try:
        dst = sqlite3.connect(str(out))
        try:
            src.backup(dst)  # consistent, WAL-safe online snapshot
        finally:
            dst.close()
    finally:
        src.close()
    return out


def backup_inbox(dest: Path) -> Path | None:
    if not INBOX.exists() or not any(p.is_file() for p in INBOX.rglob("*")):
        return None
    out = dest / f"inbox-{_stamp()}.zip"
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for p in INBOX.rglob("*"):
            if p.is_file():
                z.write(p, p.relative_to(INBOX.parent))
    return out


def prune(dest: Path, prefix: str, keep: int) -> None:
    snaps = sorted(dest.glob(f"{prefix}*"), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in snaps[keep:]:
        old.unlink()


def main() -> None:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)

    db_out = backup_db(BACKUP_DIR)
    print(f"[ok] DB backed up  -> {db_out} ({db_out.stat().st_size / 1024:.0f} KB)")

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
