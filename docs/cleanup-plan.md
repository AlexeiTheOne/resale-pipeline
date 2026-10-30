# Cleanup and restructure plan

Review done 30 Oct 2026, before moving production to the partner's PC. Not a bug
hunt: this lists what makes the code hard to change, share, or migrate. Nothing
here has been implemented yet.

## Phase 0: required before the move

1. **One base folder for every path.** Every data path is relative to the
   current working directory (`db.py:7`, `ebay/auth.py:47`, `ebay/taxonomy.py:17`,
   `telegram_bot.py:85,686,925`, `backup.py:28-30`). Started from Task Scheduler
   (cwd `C:\Windows\System32`), the bot silently creates a new empty database and
   appears to lose its eBay login. Fix: `BASE_DIR`/`DATA_DIR` in `config.py`, used
   everywhere; set "Start in" on the scheduled task as a second guard. Stored
   photo paths are relative too (`data/inbox/<id>/...`) and must resolve from
   `BASE_DIR`.
2. **`.env.example`.** The README tells people to copy it; it doesn't exist.
3. **Logging to a file.** No logging config; a background task loses all output.
4. **Ignore local photo folders** (`photos_*/`) in `.gitignore`.

## Phase 1: safety net (before any restructure)

The 391 tests cover pricing/title/category logic well but only 4 of 54 bot
handlers; photo intake has no tests. Tests also patch names on
`telegram_bot` (`tb.update_field = ...`) which silently stop working once code
moves. Add first:

1. Photo intake through `photo_handler` with stubbed identify/price/draft.
2. `confirm_callback` publish path with `ebay.inventory` stubbed.
3. `review_callback` / `revise_draft` round trip.
4. `db.py` contract test on a temp database.
5. Registration test: every command/callback registered, every name tests patch
   still reachable.

## Phase 2: structure

Target layout (pragmatic, one atomic commit for the move + import rewrite,
`pyproject.toml` replaces the `sys.path.insert` hacks):

```
app/
  core/      config.py (single load_dotenv), backup.py
  storage/   connection.py (only place that opens SQLite), repos/*.py
  bot/       telegram_bot.py split by feature (see below)
  pipeline/  identify, stages, listing/, pricing/, stock/, sync
  ebay/      http.py (shared client) + existing modules
  llm/       gemini.py, claude.py, usage.py, jsonparse.py
  finance/   profit.py, report.py
  utils/     timeutil, images, money, retry
receipts/    partner's module, plugs in via one register(app) hook
scripts/     + retry_publish.py
```

**Split `telegram_bot.py`** (5,367 lines, ~230 functions) into `bot/`: `state.py`
(the ~20 per-user dicts and locks; always `from bot import state`, never import
the dicts by name), `util.py`, `formatting.py`, `intake.py`, `photos_cmds.py`,
`flow.py`, `text_router.py`, `inventory_cmds.py`, `sales_cmds.py`,
`listing_cmds.py`, `jobs.py`, `ops_cmds.py`, `app.py`. Keep `telegram_bot.py` as
a thin entry point that re-exports names the tests use.

**Move non-Telegram logic out of the bot:** pipeline stages (lines 1978-2213),
eBay sync (3094-3551), offer planning (3857-3982), sale economics (2901-2950),
DB queries (`_set_location`, `_next_receipt_code`, `_match_quantity_to_pieces`).

**Rename `receipt.py` to `ross_tag.py`.** It reads price tags, and the partner's
receipts module would collide with the name.

## Phase 3: architecture for migrations

- **Single data layer.** SQLite is opened in 7 places outside `db.py` (auth,
  taxonomy, backup, pricing_v2, 3 scripts). Route all through
  `storage/connection.py` + repository modules. SQLite-only SQL (`ON CONFLICT`,
  `INSERT OR IGNORE`, `PRAGMA`) goes behind small helpers, the swap point for
  SQL Server if that is ever chosen.
- **Numbered migrations** with a `schema_version` table instead of
  `PRAGMA table_info` checks.
- **Relational receipts table** (item_id, cost, original price, code, store,
  date), backfilled from the `items.receipt` JSON. Later, move hot fields (sale
  price, fees, location) out of JSON blobs so reports stop loading the whole
  table into Python.
- **One `load_dotenv`.** Today it runs in 9 modules; config values depend on
  import order.

## Phase 4: merge duplicates

- **Pricing:** v1 (`pipeline/price.py`) is off (`PRICING_ENGINE=v2`) but v2,
  `msrp.py` use its private helpers. Move them to `pricing/comps.py`, delete
  the v1 ladder and the engine flag. Engine dispatch exists twice
  (`telegram_bot._stage_price`, `reprice._reprice_via_engine`): make one
  `price_item()`.
- **Charm pricing** exists 3 times with different rounding
  (`telegram_bot.py:2382`, `reprice.py:100`, `pricing_v2.py:548`).
- **Profit vs report:** `profit.price_for` and `report._price_for` compute unit
  price with different rules. `report.py` should use `profit.py`.
- **Stock:** `pipeline/inventory.py` and `stock_lookup.py` each read barcodes;
  merge into `pipeline/stock/`.
- **eBay HTTP:** `_headers()` in 8 modules, base URL in ~7, retry logic in 4.
  One `ebay/http.py`.
- **Helpers:** UTC time (~30 inline copies, inconsistent "Z" parsing), image
  open/downscale (9), retry/backoff (5), money parse/format (6), LLM JSON
  extraction (4; only `identify.py` repairs broken JSON), env parsing (4).
  One `utils/` module each.
- **Gemini clients:** three separate clients (`identify.py`, `draft.py`,
  `receipt.py`). One cached client per provider.

## Dead and legacy code

Little true dead code; no commented-out blocks; every config flag is read.

- **Old pricing engine.** Only reachable with `PRICING_ENGINE=v1`. Branches at
  `telegram_bot.py:2181-2189`, `pipeline/reprice.py:216-219,284`. Removed as
  part of the Phase 4 pricing merge (helpers move first).
- **Used only by tests:** `ebay/listings.py:189` `get_item_details`,
  `pipeline/msrp.py:150` `ratio_table`, `:163` `coverage`.
- **One-off scripts, likely obsolete (confirm with the owner):**
  `retry_publish.py`, `scripts/backfill_sales.py` (already run 29 Oct),
  `scripts/backtest_pricing.py` (v1 vs v2), `scripts/title_fit_report.py`.

## Phase 5: cost and speed

- `stock_lookup.match_by_claude` sends the full shop list each `/find` without
  prompt caching; `quick_price` doesn't cache either.
- Pricing re-scans the items table on every call (`pricing_v2.py:367,383`,
  `msrp.py:107`); memoise per run.
- `_stage_price` loads the same item twice (`telegram_bot.py:2178,2184`).
- Identify, tag OCR and Gemini tag read run sequentially; they can run
  concurrently.

## Phase 6: polish

Comments where they help, stale docstrings (`pricing_v2.py:6` says v1 is the
default), `CLAUDE.md` with layout, test command and area ownership for both
developers' AI assistants, trim the 37 KB README into `docs/`.

## Phase 7: Claude setup for two developers

There is no `CLAUDE.md` yet. Add:

- **`CLAUDE.md` (root):** layout, how to run the bot and tests, `main` deploys
  to production automatically, never run the production bot locally, secrets
  never in git, which areas each developer owns.
- **Folder-level `CLAUDE.md`** where rules are specific: `bot/` (shared state
  rule), `storage/` (all SQL goes here), `pipeline/pricing/` (cost per item,
  model choices), `receipts/` (partner's module).
- **Shared `.claude/settings.json`** in git: permission allowlist for tests and
  read-only commands, and a hook that runs the test suite before a commit.

Skills and commands worth using or building:

- Built in: `/code-review` on each change before merging to main, `/simplify`
  after a refactor, `/security-review` before anything touching keys or eBay
  auth, `/fewer-permission-prompts` once both setups exist.
- Project skills to write: `deploy-status` (what is live on the partner's PC,
  last deploy, rollback), `prod-logs` (read live logs and DB read-only over
  SSH), `new-command` (how to add a Telegram command in the split layout),
  `db-migration` (how to add a numbered migration).
