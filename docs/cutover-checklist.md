# Cutover checklist: SQLite → SQL Server (ResaleDB)

Living document: update it whenever something is added to or done for the
cutover. Design: [schema-proposal.md](schema-proposal.md). Branch:
`sqlserver-migration`. Last updated 2026-11-02.

**How the cutover works.** One cutover for both bots (Q1, changed
2026-11-02: the migration takes about 10 s and the rehearsal was clean). The
SQL Server code is inert until a production `.env` says
`DB_BACKEND=sqlserver`, so the branch is merged on the cutover day just
before the switch (section 1). The switch itself is a data migration, the
receipts cleanup and an `.env` change, done while both bots are stopped.

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
- ✅ **Production migration script:** `scripts/migrate_sqlite_to_mssql.py`
  (built 2026-11-02 on top of the dev loader).
  - **Targets:** only `ResaleDB_Migrate_<yyyymmdd>`, or `ResaleDB` with
    `--production`. It refuses a target that already holds rows (the
    bootstrap's `schema_version` / `app_settings` rows aside).
  - **Copies everything:** items, pieces as they are, operations tables,
    **`ebay_tokens`**, and the receipts (same ids with `IDENTITY_INSERT`,
    identities reseeded to ReceiptsDB's last value, `CreatedAt` Eastern → UTC).
    New pieces only where `stock.location` exists and the item has none.
  - **Runs every section 7 check:** the full `get_item` round trip on every
    item and every other db.py read, columns vs JSON, row counts, list
    lengths, pricing runs, pieces, money sums, sale totals vs `v_item_sales`,
    constraints, orphans, identity, receipts row by row, the token (never
    printed), unmapped keys.
  - **Output:** a markdown report in `logs/`, exit 1 with a loud message on any
    failed check, and the time each phase takes.
  - `--snapshot-from <live ross.db>` takes a copy first with SQLite's online
    backup API, opened read-only, safe while the bot runs.
- ✅ **Rehearsal databases are locked.** `sqlserver.connect()` refuses
  `ResaleDB_Migrate_*` (any case) unless the migration script unlocked it in
  its own process. The bot, the tests and every other script fail at the first
  DB call, so a dev `.env` naming a rehearsal database can't start the bot on
  the real eBay token.
- ✅ **Receipts cleanup script:** `scripts/receipts_cleanup.py` (schema-proposal
  section 4). It's a **dry run unless `--apply`**, and accepts the same targets as
  the migration script.
  - **Duplicate receipt number:** the copy with the most lines is kept. A line
    of the other copy whose SKU is already on it is deleted; any other line is
    moved onto it. Then the copy is deleted.
  - **Duplicate line numbers:** the receipt's lines are split into import
    batches. A batch is deleted only if all its lines are also in the biggest
    batch; otherwise the receipt is flagged.
  - **Decimal-loss prices:** fixed only with evidence. Either the raw text shows
    price / 100 after that SKU, or dividing makes the lines add up to the
    subtotal (within $0.05). The old price goes to `PriceOriginal`; everything
    else is flagged.
  - **Logging and safety:** every change gets a row in `receipt_fixes`.
    `--apply` runs in one transaction and stops if a row changed since the plan
    was made. The report goes to `logs\receipts-cleanup-*.md`.
- ✅ **Migration step 2: unique indexes** `UX_Receipts_ReceiptNumber`
  (filtered, not null) and `UX_ReceiptItems_ReceiptID_LineIndex`.
  - The step refuses, and changes nothing, while duplicates remain. Its error
    says to run the cleanup first.
  - The migration script stops at the baseline (`MSSQL_BEFORE_RECEIPT_CLEANUP`
    = 1) and refuses a target that's already past it. Step 2 then runs from
    `init_sqlserver_db.py`, or at the bot's startup.
- ✅ **Rehearsal 2 + cleanup dry run (2026-11-02).** A fresh snapshot passed
  every migration check (about 10 s of downtime). The dry run proposes **18
  changes, 26 flagged, no duplicates left**: report
  `resale-dev\logs\receipts-cleanup-ResaleDB_Migrate_20261102-20261102-142839-dry-run.md`.
  - **Receipt 183** (1 line, a bad parse of 178 with the subtotal as its
    price): its line's SKU is on 178, so the line and the receipt are
    deleted.
  - **Receipt 195**, imported twice: the earlier 11-line import is deleted.
    All its lines are in the later import, which matches the raw text (15
    items sold). The card/auth line 789 ($128.24) stays, as it's not in scope;
    a person can delete it later.
  - **5 prices fixed:** 4 because the lines then add up exactly to the
    subtotal (lines 4547, 4570, 8366, 8428), and 1 because the raw text shows
    $7.49 (line 8297).
  - **26 prices flagged** with no evidence: 12 have no raw text; others show a
    different price, e.g. raw `$0,99` stored as 998.00. They stay as they are,
    for a person.
- ⬜ **Person B approves the cleanup plan** (the dry-run report above),
  before it's applied at cutover step 8.
- 🔧 **`SALES_TAX_RATE` sync:** db.py should write `config.SALES_TAX_RATE`
  into `app_settings` at startup. Today the baseline seeds 0.07, which matches
  the config default, so this only matters if the rate changes.
- ✅ **`scripts/deploy.ps1`** (Person A's area; on the branch only, live with
  the cutover merge):
  - Staging tests run with `DB_BACKEND=sqlserver` and
    `ROSS_REQUIRE_SQLSERVER=1`, so a missing SQL Server fails the deploy
    instead of skipping tests.
  - Step 2 runs `backup.py --db-only --expect-backend sqlserver`: a SQL Server
    BACKUP DATABASE into `D:\SQLBackups`. If the live `.env` isn't on SQL
    Server, the backup and so the deploy fail, loudly.
  - The header comment is updated.
- ✅ **Nightly backup: `scripts/backup_task.ps1`**, the replacement for
  `C:\Users\boris\Projects\backup_ebay_bot.ps1` (task `EbayBotBackup`, outside
  the repo). The old script backs up `data\ross.db` with inline SQLite code,
  which after cutover would quietly back up a frozen file. **Swap it at
  cutover step 11.**
  - **DB:** `backup.py --db-only --require-copy`, which follows `DB_BACKEND`.
    On SQL Server: a verified BACKUP DATABASE into `D:\SQLBackups` (keep 14),
    copied to `OneDrive\EbayBot-backups\db` as `ross-<UTC time>.bak` (keep
    30). A failed OneDrive copy fails the run.
  - **Photos:** the same two robocopy mirrors; robocopy exit 8 or more fails
    the run.
  - **Any failure:** a line in `logs\backup.log`, a **Telegram alert**
    through `scripts\notify.py`, and exit 1.
  - **Tested** on dev (`-Repo resale-dev -Dest <scratch folder>`): a real
    `ResaleDB_Dev` backup, copied, exit 0.
  - Old SQLite backups in `OneDrive\EbayBot-backups\db` are named
    `ross_<day>.db` (underscore). backup.py prunes only `ross-*`, so those
    stay; delete them by hand once they're no longer wanted.
  - The `EbayBotBackup` task must run as boris, with the OneDrive client
    signed in. A copy into the local OneDrive folder is not off-machine until
    the client has uploaded it.
- 🔧 **`prod-logs` skill and `README.md`:** both query
  `data/ross.db` with raw `sqlite3`. Switch them to read-only SQL Server
  queries (`db.connect_readonly()`, or `sqlcmd -K ReadOnly`, or the views
  `v_items` / `v_item_sales`).
- ✅ **Receipt bot "already saved"** (Person B sign-off). `receipt_pipeline`
  is now a local git repo:
  - `0c8cf36`: the code as it ran, tracked first. `.env`, the venvs and the
    receipt data are ignored.
  - `8f16893`: the change, 6 lines in `bot.py`. A duplicate-key error
    (2627/2601) replies "✅ Receipt <number> already saved" instead of a
    database error.
  - **Effect:** none until the receipt bot restarts on ResaleDB (step 13),
    because ReceiptsDB has no unique indexes.

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
- ⬜ **Merge `sqlserver-migration` into `main` on the cutover day, right
  before section 2** (through a PR that Person A reviews).
  - **Why that day:** the merge commit is still deployed by the old live
    `deploy.ps1` (SQLite backup, ordinary tests), so the bot comes back on the
    new code, still on SQLite. Wait for the ✅ on Telegram.
  - **Why not earlier:** from then on, the live `deploy.ps1` is the new one,
    which needs SQL Server (tests and backup). Any later deploy fails until
    the `.env` is switched. So push nothing else between the merge and step 9.
- ✅ **Rehearsal 1 (2026-11-02): every check passed.** From `resale-dev`:
  `venv\Scripts\python.exe scripts\migrate_sqlite_to_mssql.py --snapshot-from C:\Users\boris\Projects\ebay-list\data\ross.db --compare-dev data\ross.db`
  into `ResaleDB_Migrate_20261102`. The report is
  `resale-dev\logs\migrate-ResaleDB_Migrate_20261102-20261102-140028.md`.
  - **Data:** 163 items, 1 piece, 214 receipts, 2,717 receipt lines, 1,075
    `listing_stats` rows, 314 `llm_usage` rows, 83 sales and the token.
  - **Results:** round trip 0 differences, all money sums equal.
  - **Time:** about 7 s to copy and 3 s to verify, so **about 10 s of
    downtime** for the migration itself.
  - **Pieces:** production has 1 piece (`O1W-01`), linked to the only
    `stocked` item, so no pieces were created from `stock.location`.
  - **Production-only keys** (not in the dev copy): only from that `/inv`
    item (`stock.*`, `identification.source`, `piece.info.*`). All are kept
    exactly.
- ⬜ **Rehearse again shortly before the cutover** (same command, the next
  day's date), especially after any deploy that changes stored data.
- ⬜ **Clean up the rehearsal copies before the cutover.** They hold the
  real eBay token.
  - Drop each `ResaleDB_Migrate_*` from an admin `sqlcmd`:
    `DROP DATABASE [ResaleDB_Migrate_<date>]` (nothing in the code can open
    them).
  - Delete `resale-dev\data\rehearsal\*`.
  - Done for rehearsals 1 and 2 on 2026-11-02; repeat after any new one.
- 💤 **Person A:** install SQL Server Express on their machine (Q12) and
  confirm the Person A answers (Q8–Q14) that Person B relayed.

---

## 2. Cutover day (in this order)

One window for both the bot and the receipt bot (Q1). Pick a quiet time. Production is `C:\Users\boris\Projects\ebay-list`. The
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
3. ⬜ **Stop the receipt bot** (Person B). It runs as the `ReceiptBot`
   scheduled task (`C:\Users\boris\Projects\run_receipt_bot.cmd`, log
   `receipt_pipeline\service.out.log`):
   `Disable-ScheduledTask ReceiptBot; Stop-ScheduledTask ReceiptBot`.
   Then check that no `bot.py` is left (from an admin PowerShell, or the
   command line stays hidden):
   `Get-CimInstance Win32_Process -Filter "CommandLine like '%receipt_pipeline%' or CommandLine like '%bot.py%'"`.
   If one is left, Person B ends that one PID by hand. Never kill `python.exe`
   broadly, and don't start it with `run_bot.bat` while the task exists (two
   copies on one token fight).
4. ⬜ **Final backups:**
   - `python backup.py --db-only` in `ebay-list` (SQLite);
   - a copy-only backup of ReceiptsDB:
     `BACKUP DATABASE ReceiptsDB TO DISK = 'D:\SQLBackups\ReceiptsDB-<stamp>-pre-cutover.bak' WITH COPY_ONLY, CHECKSUM`.
5. ⬜ **Mixed mode + `resale_reader`** (section 1), if not done earlier.
6. ⬜ **Don't create ResaleDB by hand.** The migration script (step 7) creates
   it at the baseline. `init_sqlserver_db.py` would also build the unique
   receipt indexes, and the migration would then refuse the database.
7. ⬜ **Migrate:** SQLite + ReceiptsDB → ResaleDB. Run it in `ebay-list`
   with the bot and the receipt bot stopped:
   `venv\Scripts\python.exe scripts\migrate_sqlite_to_mssql.py --source <the ross.db backup from step 4> --target ResaleDB --production`.
   It must end with "All checks passed" (exit 0); read the report in
   `logs\migrate-ResaleDB-*.md`, especially the leftover keys and warnings.
   **Stop here if any check failed.** Then drop ResaleDB and fix before
   retrying; the script refuses a target with rows.
8. ⬜ **Receipts cleanup, then the unique indexes**, in `ebay-list`:
   1. Dry run:
      `venv\Scripts\python.exe scripts\receipts_cleanup.py --target ResaleDB --production`.
      The plan should match the approved one (section 1); new receipts since
      may add a few lines.
   2. Apply: the same command with `--apply`. It must end with "Duplicates
      left: none".
   3. Run `venv\Scripts\python.exe scripts\init_sqlserver_db.py ResaleDB --production`,
      which runs step 2 (the unique indexes). It refuses, and changes nothing,
      if duplicates remain.
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
11. ⬜ **Backups:** swap in the new nightly script. Keep the old one as
    `.old`, then copy the new one over it:
    `Copy-Item C:\Users\boris\Projects\backup_ebay_bot.ps1 C:\Users\boris\Projects\backup_ebay_bot.ps1.old`;
    `Copy-Item ebay-list\scripts\backup_task.ps1 C:\Users\boris\Projects\backup_ebay_bot.ps1`.
    Run it once by hand (`powershell -ExecutionPolicy Bypass -File C:\Users\boris\Projects\backup_ebay_bot.ps1`). It must exit 0, a `ResaleDB-*.bak` must appear in
    `D:\SQLBackups`, and a `ross-*.bak` of the same size in
    `OneDrive\EbayBot-backups\db`. Check with
    `Get-FileHash` that the two files match.
12. ⬜ **Start the bot:** `Enable-ScheduledTask EbayBot; Start-ScheduledTask EbayBot`.
    Watch `logs\heartbeat.json` and `logs\service.out.log`, then try `/health`
    and a read command (`/report`) in Telegram.
13. ⬜ **Receipt bot:** set `SQL_CONN_STR` in `receipt_pipeline\.env` to
    `Driver={ODBC Driver 18 for SQL Server};Server=localhost\SQLEXPRESS;Database=ResaleDB;Trusted_Connection=yes;Encrypt=no;`
    and start it: `Enable-ScheduledTask ReceiptBot; Start-ScheduledTask ReceiptBot`
    (not `run_bot.bat`). The restart also loads the "already saved" change.
    Paste one test receipt and check it lands in `ResaleDB.dbo.Receipts`.
    Then paste it again: the reply must be "already saved", and no second
    row may appear.
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
6. The nightly backup needs no change: `backup_task.ps1` follows
   `DB_BACKEND`.
7. Deploys: the new `deploy.ps1` insists on SQL Server (tests and backup),
   so keep `EbayBotDeploy` disabled until a fix sets it back. Otherwise every
   deploy stops at "backup failed".

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
- 💤 **When receipts merge into the main bot:** disable and delete the
  `ReceiptBot` task, so the old receipt bot can't come back at the next boot.
  From an admin PowerShell:
  `Disable-ScheduledTask ReceiptBot; Stop-ScheduledTask ReceiptBot; Unregister-ScheduledTask ReceiptBot -Confirm:$false`.
  Check that no receipt `bot.py` is left (section 2, step 3), then delete
  `C:\Users\boris\Projects\run_receipt_bot.cmd`.
- 💤 After the first day, the SQLite fallback is retired (section 3). Then,
  when convenient: remove the frozen SQLite migration steps from
  `migrations.py`, the SQLite branch of db.py and backup.py, and the
  sqlite_only tests.
- 💤 `.claude/settings.json`: the pre-commit hook also fires on Bash commands
  that contain a `for` loop, not only on `git commit`.

---

## Log
- 2026-11-02: **Q1 changed** (Person B): one cutover for both bots. Built the
  receipts cleanup script, migration step 2 (unique receipt indexes, refused
  while duplicates remain), the receipt bot's "already saved" reply
  (`receipt_pipeline` now in git), `deploy.ps1` on SQL Server (branch only)
  and `scripts/backup_task.ps1` (nightly, with a Telegram alert). Rehearsal 2
  plus the cleanup dry run: 18 changes, 26 flagged, no duplicates left.
  Rehearsal databases and snapshots dropped. The merge now happens on the
  cutover day.
- 2026-11-02: production migration script built
  (`scripts/migrate_sqlite_to_mssql.py`). `ResaleDB_Migrate_*` databases are
  locked in `sqlserver.connect()`. Rehearsal 1 from a read-only snapshot of
  live `ross.db` passed every check: about 10 s of migration downtime, 1
  piece, 0 created from `stock.location`.
- 2026-11-02: the receipt bot gets its own scheduled task, `ReceiptBot`
  (Person B). It is a copy of EbayBot's settings: boot trigger, runs whether
  logged on or not, no time limit, restart on failure. It runs
  `C:\Users\boris\Projects\run_receipt_bot.cmd`, which restarts `bot.py` 15 s
  after it exits and appends to `receipt_pipeline\service.out.log`.
  receipt_pipeline's code is unchanged. Steps 3 and 13 now use the task, and
  section 4 says to delete it when receipts merge into the main bot.
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
