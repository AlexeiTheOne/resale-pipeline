---
name: prod-logs
description: Read the live bot's logs and database on the production PC safely (read-only) - recent activity, errors and tracebacks, crash output, and read-only SQL against the live SQL Server DB (ResaleDB). Use when asked why something failed on the live bot, what happened with an item, "check the logs", or to investigate a ❌ deploy.
---

# Production logs and DB (read-only)

Everything here is **read-only**. Never restart, kill processes, edit files,
or write to the DB on the production PC. Never print `.env` values or the
`ebay_tokens` table.

## Logs

The status script prints logs as well:
- **On the production PC** (a session in `C:\Users\boris\Projects\resale-dev`
  is already there, so don't use SSH):
  `powershell -NoProfile -ExecutionPolicy Bypass -File C:\Users\boris\Projects\ebay-list\scripts\prod_status.ps1 -Logs 60 -Errors`
- **From a laptop:**
  `ssh -o ConnectTimeout=15 boris@100.73.116.76 "powershell -NoProfile -ExecutionPolicy Bypass -File C:\Users\boris\Projects\ebay-list\scripts\prod_status.ps1 -Logs 60 -Errors"`

`-Logs N` gives the last N lines of `logs\bot.log`. `-Errors` gives the last
30 error and warning lines plus the tail of `logs\service.out.log`, which is
where crash output from the restart loop lands. `logs\deploy.log` has every
deploy step.

To search for one item or word, run over SSH (or locally on prod):
`powershell -NoProfile -Command "Select-String -Path C:\Users\boris\Projects\ebay-list\logs\bot.log -Pattern '<item id or word>' | Select-Object -Last 40 | ForEach-Object Line"`

## Database (read-only)

The live DB is SQL Server `.\SQLEXPRESS`, database `ResaleDB` (since 2 Nov
2026). `data/ross.db` is an old SQLite archive: don't read it as the current
state. Connect read-only, never a plain connect, and never through `db.py`
pointed at `ResaleDB`:

```
import pyodbc, sqlserver   # run from resale-dev with venv\Scripts\python.exe
c = pyodbc.connect(sqlserver.conn_str("ResaleDB") + "ApplicationIntent=ReadOnly;", readonly=True)
print(c.cursor().execute("<SELECT ...>").fetchall())
```

Anything longer than one query: write a small `.py` to a temp or scratch
folder and run it with `resale-dev`'s `venv\Scripts\python.exe`. Never put it
in `ebay-list`, because that dirties the live copy and blocks deploys.

The schema is normalized: `items` (status), `ebay_listings` (offer_id,
listing_id, quantity), `item_listing`, `item_pricing`, `sales` (one row per
order), `job_runs`. Keys with no column of their own are in `*_raw` JSON
columns (`ebay_raw` and so on; see `dbmap.py`). List columns with
`INFORMATION_SCHEMA.COLUMNS` before querying.

Report what you found in plain words. Quote at most the few log lines that
matter.
