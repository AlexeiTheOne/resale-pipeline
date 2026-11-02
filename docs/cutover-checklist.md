# Cutover checklist: SQLite → SQL Server (ResaleDB)

Living document: update it whenever something is added to or done for the
cutover. Design: [schema-proposal.md](schema-proposal.md). Branch:
`sqlserver-migration`. Last updated 2026-11-02.

**How the cutover works.** The SQL Server code is inert until a production
`.env` says `DB_BACKEND=sqlserver`: the default is still SQLite. So the branch
can be merged and deployed first, and the switch is an `.env` change plus a
data migration, done while the bot is stopped.

Status: ✅ done · ⬜ to do at cutover · 🔧 not built yet (blocks cutover) ·
💤 after cutover.

---

## 1. Before the cutover day

### Code still to build
- 🔧 **Production migration script.** `scripts/sqlite_to_mssql.py` is a dev
  loader. It refuses `ResaleDB`, never copies eBay tokens and has no receipts.
  The production version must:
  - write `ResaleDB` behind an explicit `--production` flag;
  - **copy `ebay_tokens`** (otherwise the bot loses eBay access);
  - copy `Receipts` and `ReceiptItems` from ReceiptsDB with `IDENTITY_INSERT`,
    reseed the identities, and convert `CreatedAt` from Eastern time to UTC;
  - write the verification report (counts, money totals, round trip,
    consistency) to `logs/`;
  - refuse to run if the target already holds rows.
- 🔧 **Receipts cleanup script** (schema-proposal section 4): decimal-loss
  prices (31), the duplicate receipt, receipt 195's duplicate lines, every
  change logged in `receipt_fixes`, with a dry run first.
- 🔧 **Migration step 2: unique indexes** on `Receipts.ReceiptNumber`
  (filtered, not null) and `ReceiptItems (ReceiptID, LineIndex)`. It ships only
  after the cleanup, and after the receipt bot's "already saved" message (below).
- 🔧 **`SALES_TAX_RATE` sync:** db.py should write `config.SALES_TAX_RATE`
  into `app_settings` at startup. Today the baseline seeds 0.07, which matches
  the config default, so this only matters if the rate changes.
- 🔧 **`scripts/deploy.ps1`** (Person A's area):
  - run the staging tests with `ROSS_REQUIRE_SQLSERVER=1`, so a missing SQL
    Server fails the deploy instead of skipping about 100 tests;
  - step 2 (`backup.py --db-only`) already follows `DB_BACKEND`; check that it
    blocks the deploy if the backup fails;
  - update the comment at line 26 ("Never touches data\ross.db").
- 🔧 **Nightly backup wrapper** `C:\Users\boris\Projects\backup_ebay_bot.ps1`
  (run by the `EbayBotBackup` task, outside the repo). It does **not** use
  backup.py: lines 4–8 back up `data\ross.db` with inline SQLite code. After
  cutover that file is frozen, so it would quietly back up stale data. Replace
  lines 4–8 with:
  ```powershell
  $env:BACKUP_DIR = "$dest\db"
  $env:BACKUP_KEEP = "30"
  Push-Location $repo
  & "$repo\venv\Scripts\python.exe" backup.py --db-only
  Pop-Location
  ```
  Keep the two robocopy lines (photos). backup.py follows `DB_BACKEND`: on SQL
  Server it runs BACKUP DATABASE into `D:\SQLBackups`, copies the `.bak` to
  OneDrive and prunes both folders.
- 🔧 **`prod-logs` skill and `README.md`:** both query
  `data/ross.db` with raw `sqlite3`. Switch them to read-only SQL Server
  queries (`db.connect_readonly()`, or `sqlcmd -K ReadOnly`, or the views
  `v_items` / `v_item_sales`).
- 🔧 **Receipt bot "already saved"** (`receipt_pipeline/bot.py`, Person B's
  code, outside the repo). It already catches `pyodbc.IntegrityError`. Make the
  reply "already saved" when the error is a duplicate key (2601/2627), before
  the unique indexes go on.

### Setup
- ✅ `D:\SQLBackups` exists. `NT Service\MSSQL$SQLEXPRESS` can modify it and
  users can read it. A real backup and copy of ResaleDB_Dev were verified.
- ✅ ODBC Driver 18 is installed (this PC is the production PC).
- ✅ `pyodbc` and `tzdata` are pinned in `requirements.txt`. deploy.ps1
  installs them when requirements change; check after the merge with
  `venv\Scripts\python.exe -c "import pyodbc, tzdata"` in `ebay-list`.
- ⬜ **Mixed-mode auth + `resale_reader` login (Q7).** This needs a SQL Server
  service restart, which also drops the receipt bot's connections, so do it
  inside the cutover window. As admin:
  ```sql
  EXEC xp_instance_regwrite N'HKEY_LOCAL_MACHINE',
       N'Software\Microsoft\MSSQLServer\MSSQLServer', N'LoginMode', REG_DWORD, 2;
  -- restart the service: Restart-Service 'MSSQL$SQLEXPRESS' (elevated)
  CREATE LOGIN resale_reader WITH PASSWORD = '<strong, into .env as SQL_READONLY_PASSWORD>',
       DEFAULT_DATABASE = ResaleDB, CHECK_POLICY = ON;
  USE ResaleDB; CREATE USER resale_reader FOR LOGIN resale_reader;
  ALTER ROLE db_datareader ADD MEMBER resale_reader;
  ```
  Then point `db.connect_readonly()` at it (🔧 a small code change).
- ⬜ **Login language.** The receipt bot sends `MM/DD/YY` text dates, so
  boris's login must stay `us_english`. Check:
  `SELECT name, default_language_name FROM sys.server_principals WHERE name LIKE '%boris%'`.
- ⬜ **Merge `sqlserver-migration` into `main`** (through a PR, Person A
  reviews). This is safe before cutover: production stays on SQLite until its
  `.env` changes. The deploy then runs the SQL Server tests on this PC, in
  per-run `ResaleDB_Test_*` databases.
- ⬜ **Rehearsal.** Back up production `ross.db`, then run the production
  migration script into `ResaleDB_Migrate_<date>` and read the report:
  round trip identical, columns consistent, counts and money totals equal.
- 💤 **Person A:** install SQL Server Express on their machine (Q12) and
  confirm the Person A answers (Q8–Q14) that Person B relayed.

---

## 2. Cutover day (in this order)

Pick a quiet time. Production is `C:\Users\boris\Projects\ebay-list`. The
restart script ends **every** `telegram_bot.py` on this PC, a dev bot
included.

1. ⬜ **Stop deploys:** `Disable-ScheduledTask EbayBotDeploy`.
2. ⬜ **Stop the bot.** Wait until `logs\busy.json` is gone (idle), then:
   `Stop-ScheduledTask EbayBot; Disable-ScheduledTask EbayBot`
   (this ends the `run_ebay_bot.cmd` restart loop), then
   `C:\Users\boris\Projects\restart_ebay_bot.ps1` to end `telegram_bot.py`.
   Confirm nothing is left:
   `Get-CimInstance Win32_Process -Filter "CommandLine like '%telegram_bot.py%'"`.
   Never kill `python.exe` broadly.
3. ⬜ **Stop the receipt bot by hand** (Person B; started with
   `run_bot.bat`). Never touch its processes from a script.
4. ⬜ **Final backups:**
   - `python backup.py --db-only` in `ebay-list` (SQLite);
   - a copy-only backup of ReceiptsDB:
     `BACKUP DATABASE ReceiptsDB TO DISK = 'D:\SQLBackups\ReceiptsDB-<stamp>-pre-cutover.bak' WITH COPY_ONLY, CHECKSUM`.
5. ⬜ **Mixed mode + `resale_reader`** (section 1), if not done earlier.
6. ⬜ **Create ResaleDB:**
   `python scripts/init_sqlserver_db.py ResaleDB --production` (from
   `ebay-list`, with its `.env`).
7. ⬜ **Migrate:** the production migration script, SQLite + ReceiptsDB →
   ResaleDB. **Stop here if the report shows any difference.**
8. ⬜ **Receipts cleanup** (dry run, review, apply), then the migration step
   with the unique indexes (`init_sqlserver_db.py ResaleDB --production`
   runs pending steps).
9. ⬜ **Production `.env`** (`ebay-list\.env`):
   ```
   DB_BACKEND=sqlserver
   SQL_DATABASE=ResaleDB
   SQL_BACKUP_DIR=D:\SQLBackups
   ```
   `EBAY_WRITES_ENABLED` must stay **unset** (default on) and `JOBS_ENABLED`
   must stay unset or 1. Check that neither is set to 0. (On dev they are 0.)
10. ⬜ **Check before starting:** in `ebay-list`,
    `venv\Scripts\python.exe -c "import db; print(db.get_item(db.list_items()[0]['item_id'])['status'])"`.
    It must print without error, and `db.load_ebay_token()` must not be
    None.
11. ⬜ **Backups:** the new `backup_ebay_bot.ps1` (section 1) is in place.
    Run it once by hand; a `ross-*.bak` should appear in
    `OneDrive\EbayBot-backups\db` and a `ResaleDB-*.bak` in `D:\SQLBackups`.
12. ⬜ **Start the bot:** `Enable-ScheduledTask EbayBot; Start-ScheduledTask EbayBot`.
    Watch `logs\heartbeat.json` and `logs\service.out.log`, then try `/health`
    and a read command (`/report`) in Telegram.
13. ⬜ **Receipt bot:** set `SQL_CONN_STR` in `receipt_pipeline\.env` to
    `Driver={ODBC Driver 18 for SQL Server};Server=localhost\SQLEXPRESS;Database=ResaleDB;Trusted_Connection=yes;Encrypt=no;`
    and start it with `run_bot.bat`. Paste one test receipt and check it
    lands in `ResaleDB.dbo.Receipts`.
14. ⬜ **ReceiptsDB read-only:** `ALTER DATABASE ReceiptsDB SET READ_ONLY`.
    Keep it for **30 days** (Q2), then take a final backup and drop it.
15. ⬜ **Re-enable deploys:** `Enable-ScheduledTask EbayBotDeploy`.
16. ⬜ **Keep the old SQLite file** read-only as the fallback:
    `attrib +R ebay-list\data\ross.db` (never delete it; CLAUDE.md).

---

## 3. Rollback

**Before the bot has written anything to ResaleDB** (steps 1–11): nothing
is lost.
1. Remove `DB_BACKEND=sqlserver` from `ebay-list\.env`.
2. `attrib -R data\ross.db` if it was set.
3. Re-enable and start `EbayBot`, re-enable `EbayBotDeploy`.
4. Receipt bot: point `SQL_CONN_STR` back at ReceiptsDB (or remove it) and
   run `ALTER DATABASE ReceiptsDB SET READ_WRITE`.

**After the bot has run on ResaleDB**: going back to SQLite loses every
write since the switch (new items, sales syncs, price changes, receipts).
There is **no SQL Server → SQLite export** (🔧 a decision: build one, or
accept "fix forward" plus SQL Server restores).
- Fix forward is the default: deploy a fix, or set `DB_BACKEND` back only if
  losing the writes since the switch is acceptable.
- If ResaleDB's data is damaged, restore the latest good backup:
  ```sql
  ALTER DATABASE ResaleDB SET SINGLE_USER WITH ROLLBACK IMMEDIATE;
  RESTORE DATABASE ResaleDB FROM DISK = 'D:\SQLBackups\ResaleDB-<stamp>.bak' WITH REPLACE, CHECKSUM;
  ALTER DATABASE ResaleDB SET MULTI_USER;
  ```
  (Stop the bot and the receipt bot first, as in section 2.)
- A code rollback by deploy.ps1 restores code only, never the DB. Every
  schema step must work with the previous release for one deploy.

---

## 4. After cutover
- 💤 Phase 2 (Q14): targeted writers (`set_list_price`, `add_sale`,
  `move_piece`), and `stock.location` moved onto pieces at runtime, one path at
  a time.
- 💤 Fill `stores` for the 18 store numbers (clean names, city, zip).
- 💤 Receipt matching (`receipt_matches`): the matcher and its Telegram review.
- 💤 Drop ReceiptsDB after 30 days (final backup first).
- 💤 Remove the frozen SQLite migration steps from `migrations.py`, and the
  SQLite branch of db.py, once rollback to SQLite is no longer wanted.
- 💤 `.claude/settings.json`: the pre-commit hook also fires on Bash commands
  that contain a `for` loop, not only on `git commit`.

---

## Log
- 2026-11-02: list started.
  - Backups go to `D:\SQLBackups` with pruning (14 per database).
  - The old test backup in the instance's Backup folder was deleted
    (`xp_delete_file`).
  - Found that `backup_ebay_bot.ps1` backs up SQLite itself (section 1).
