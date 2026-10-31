---
name: prod-logs
description: Read the live bot's logs and database on the production PC safely (read-only) - recent activity, errors and tracebacks, crash output, and read-only SQL against data/ross.db. Use when asked why something failed on the live bot, what happened with an item, "check the logs", or to investigate a ❌ deploy.
---

# Production logs and DB (read-only)

Everything here is **read-only**. Never restart, kill processes, edit files,
or write to the DB on the production PC. Never print `.env` values or the
`ebay_tokens` table.

## Logs

The status script prints logs as well:
- **On the production PC:**
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

Open SQLite in read-only mode, never a plain connect:

```
C:\Users\boris\Projects\ebay-list\venv\Scripts\python.exe -c "import sqlite3; c=sqlite3.connect('file:C:/Users/boris/Projects/ebay-list/data/ross.db?mode=ro', uri=True); print(c.execute('<SELECT ...>').fetchall())"
```

Over SSH, wrap the whole thing in double quotes for cmd.exe, and use single
quotes inside. Anything longer than one query: write a small `.py` to the
remote home folder, run it with the venv python, then delete it. Never put it
in the repo folder, because that dirties the live copy and blocks deploys.

Useful tables: `items` (one row per item: status, prices, ebay ids, receipt
code), `job_runs` (when scheduled jobs last ran). Check `db.py` for columns
before querying.

Report what you found in plain words. Quote at most the few log lines that
matter.
