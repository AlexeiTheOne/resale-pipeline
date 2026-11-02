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

> **Backups: OneDrive is the only off-machine copy.** C: and D: are the same
> physical disk, so `D:\SQLBackups` protects against a bad write or a broken
> database, **not** against losing the disk. The nightly job must copy each
> SQL Server `.bak` to OneDrive (keep 30), a failed copy must fail the job, and
> the copy must be seen **online** (section 2, step 17).

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
  $env:BACKUP_DIR = "$dest\db"     # OneDrive: the only off-machine copy
  $env:BACKUP_KEEP = "30"           # .bak copies kept in OneDrive
  $env:SQL_BACKUP_KEEP = "14"       # .bak files kept in D:\SQLBackups (same disk)
  Push-Location $repo
  & "$repo\venv\Scripts\python.exe" backup.py --db-only --require-copy
  $code = $LASTEXITCODE
  Pop-Location
  if ($code -ne 0) { throw "DB backup failed or did not reach OneDrive (exit $code)" }
  ```
  Keep the two robocopy lines (photos). backup.py follows `DB_BACKEND`: on SQL
  Server it runs BACKUP DATABASE into `D:\SQLBackups`, copies the `.bak` to
  OneDrive as `ross-<UTC time>.bak` and prunes both folders.
  - ✅ `--require-copy` (built): a `.bak` that can't be copied to OneDrive
    fails the run (exit 1) instead of only warning. The pre-deploy backup
    (deploy.ps1 step 2) still only warns, because the local `.bak` is enough to
    roll back a deploy.
  - Old SQLite backups in `OneDrive\EbayBot-backups\db` are named
    `ross_<day>.db` (underscore). backup.py prunes only `ross-*`, so those
    stay; delete them by hand once they're no longer wanted.
  - The `EbayBotBackup` task must run as boris, with the OneDrive client
    signed in. A copy into the local OneDrive folder is not off-machine until
    the client has uploaded it.
  - 🔧 A failed nightly backup should alert on Telegram (scripts/notify),
    like deploys do. Today the wrapper only exits non-zero, which nobody
    sees unless they look at Task Scheduler.
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
    Run it once by hand. It must exit 0, a `ResaleDB-*.bak` must appear in
    `D:\SQLBackups`, and a `ross-*.bak` of the same size in
    `OneDrive\EbayBot-backups\db`. Check with
    `Get-FileHash` that the two files match.
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
    `attrib +R ebay-list\data\ross.db` (never delete it; CLAUDE.md). It is
    the rollback target for the **first day only** (section 3c). Note the
    switch time: the day starts there.
17. ⬜ **The morning after the first nightly run: check OneDrive *online*.**
    The local folder alone doesn't prove it left the machine. Open
    onedrive.live.com → *EbayBot-backups* → *db* (or the OneDrive app on a
    phone) and confirm:
    - a `ross-<date>.bak` from last night is there;
    - its size matches the `ResaleDB-*.bak` in `D:\SQLBackups` (about 25 MB
      on dev data).
    - In Explorer, the local file must show the green "synced" mark, not
      "sync pending".
    If it's missing online, the off-machine backup isn't working: fix it
    before anything else (OneDrive signed in? the task's user? the wrapper's
    exit code?).

---

## 3. Rollback

**Decided (2026-11-02, Person B): fix forward + SQL Server restores. No
SQLite export will be built.** Going back to SQLite is allowed **only within
the first day after cutover**, and it accepts losing every write made since
the switch.

### a) Before the bot has written anything to ResaleDB (steps 1–11)
Nothing is lost.
1. Remove `DB_BACKEND=sqlserver` from `ebay-list\.env`.
2. `attrib -R data\ross.db` if it was set.
3. Re-enable and start `EbayBot`, re-enable `EbayBotDeploy`.
4. Receipt bot: point `SQL_CONN_STR` back at ReceiptsDB (or remove it) and
   run `ALTER DATABASE ReceiptsDB SET READ_WRITE`.

### b) Default once the bot runs on ResaleDB: fix forward
- A **code** problem: push a fix (the normal deploy), or let deploy.ps1 roll
  the code back. A code rollback restores code only, never the DB, so every
  schema step must work with the previous release for one deploy.
- **Damaged data**: restore ResaleDB from the latest good backup. Stop the
  bot and the receipt bot first, as in section 2 steps 2–3.
  ```sql
  ALTER DATABASE ResaleDB SET SINGLE_USER WITH ROLLBACK IMMEDIATE;
  RESTORE DATABASE ResaleDB FROM DISK = 'D:\SQLBackups\ResaleDB-<stamp>.bak' WITH REPLACE, CHECKSUM;
  ALTER DATABASE ResaleDB SET MULTI_USER;
  ```
  If D: is unreadable, take the newest `ross-*.bak` from
  `OneDrive\EbayBot-backups\db` (download it first if it is cloud-only).
  Writes after that backup are lost. Then run `/sync` to pull eBay orders
  back in.

### c) Back to SQLite: within the first day only
Only if fixing forward isn't possible. Writes made since the switch are
**lost**, by decision:
- **Lost:**
  - items captured or edited since the switch (their photos stay in
    `data\inbox`);
  - manual prices and repricing results;
  - pieces added or moved with `/inv`;
  - offers sent, feedback records and job stamps.
- **Recovered:** sold orders, because eBay sync re-pulls them. Run `/sync`
  right after the switch back.
- **Check after switching back:**
  - listings published since the switch exist on eBay but not in
    `ross.db`; compare with `/health` / the sync report and re-link them;
  - receipts entered since the switch are in ResaleDB only. Re-load them into
    ReceiptsDB from `receipt_pipeline\storage\parsed` with
    `upload_parsed_to_sql.py` (its file names are timestamps; take those after
    the switch).

Steps:
1. Stop the bot (section 2, step 2) and the receipt bot (step 3).
2. Remove `DB_BACKEND=sqlserver` from `ebay-list\.env`.
   `attrib -R data\ross.db`.
3. Receipt bot: point `SQL_CONN_STR` back at ReceiptsDB and run
   `ALTER DATABASE ReceiptsDB SET READ_WRITE`. Start it.
4. Start `EbayBot` and run `/sync`. Re-enable `EbayBotDeploy`.
5. Keep ResaleDB as it is (don't drop it): it holds the lost writes for
   reference. Take a backup first.
6. Put the nightly backup wrapper back to the SQLite version.

**After the first day:** the SQLite fallback is retired. `data\ross.db`
stays read-only as an archive (never deleted, CLAUDE.md), and only (b)
applies.

---

## 4. After cutover
- 💤 Phase 2 (Q14): targeted writers (`set_list_price`, `add_sale`,
  `move_piece`), and `stock.location` moved onto pieces at runtime, one path at
  a time.
- 💤 Fill `stores` for the 18 store numbers (clean names, city, zip).
- 💤 Receipt matching (`receipt_matches`): the matcher and its Telegram review.
- 💤 Drop ReceiptsDB after 30 days (final backup first).
- 💤 After the first day, the SQLite fallback is retired (section 3). Then,
  when convenient: remove the frozen SQLite migration steps from
  `migrations.py`, the SQLite branch of db.py and backup.py, and the
  sqlite_only tests.
- 💤 `.claude/settings.json`: the pre-commit hook also fires on Bash commands
  that contain a `for` loop, not only on `git commit`.

---

## Log
- 2026-11-02: rollback decided (Person B): fix forward + SQL Server
  restores, no SQLite export. Back to SQLite only within the first day,
  losing the writes since the switch (eBay sync re-pulls orders). Section 3
  rewritten with what is lost, what comes back and the steps.
- 2026-11-02: C: and D: are one physical disk, so OneDrive is the only
  off-machine backup. backup.py gained `--require-copy` (nightly: a failed
  OneDrive copy fails the run) and `SQL_BACKUP_KEEP` (D: keeps 14, OneDrive
  keeps 30 via BACKUP_KEEP). Added step 17, the online check after the first
  nightly run, and the Telegram alert item.
- 2026-11-02: list started.
  - Backups go to `D:\SQLBackups` with pruning (14 per database).
  - The old test backup in the instance's Backup folder was deleted
    (`xp_delete_file`).
  - Found that `backup_ebay_bot.ps1` backs up SQLite itself (section 1).
