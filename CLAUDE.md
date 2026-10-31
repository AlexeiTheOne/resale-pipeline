# Ross Resale Bot

A Telegram bot for a live eBay resale business. Photos in, then identify,
price, draft, review and publish on eBay. It also handles inventory, sales
sync, repricing, offers and buyer feedback. Python 3.13, Windows.

## Two developers

- **Person A:** the bot, the code, and the business side: pricing, listings,
  repricing and offers.
- **Person B:** the databases, product photos, and buying (receipts, Ross
  inventory).

Each person runs their own Claude. Before changing something in the other
person's area, say so in the commit message and tell them. Never silently
undo the other person's decision. If you disagree, write up both sides and
give a recommendation.

## Production: pushing to `main` goes live

The live bot runs on Person B's PC, not on a laptop. Task `EbayBotDeploy` runs
`scripts/deploy.ps1` every 5 minutes. When `origin/main` has a new commit it:

1. runs the tests in a staging copy
2. backs up the DB
3. waits until the bot is idle (`logs/busy.json` gone)
4. fast-forwards the live copy and restarts the bot
5. checks `logs/heartbeat.json`
6. sends ✅ or ❌ on Telegram, and rolls back on failure

Its log is `logs/deploy.log`.

Project skills: `deploy-status` (what's live, did the deploy work, rollback)
and `prod-logs` (read live logs and DB, read-only). Both use
`scripts/prod_status.ps1`.

So:
- Push only code that passed the tests locally. A push restarts the live bot.
- Never commit on the production PC. A dirty or diverged live copy blocks
  deploys.
- A failed commit is not retried. Push a fix, or run `deploy.ps1 -Force`.

## Hard rules

- **Never run the bot with the live token anywhere but production.** One
  token allows one poller, and a second one breaks the live bot.
- **Secrets never go in git.** `.env` is gitignored. Never print `.env` values.
  The eBay user token lives in `data/ross.db`.
- **`data/` is not in git** (DB and photos). Never overwrite or delete
  `data/ross.db`. Back up with `python backup.py` first.
- **On the production PC:**
  - Never touch `receipt_pipeline` or its processes.
  - Never kill `python.exe` broadly.
  - Restart only via `restart_ebay_bot.ps1`.
  - No reboots without both people agreeing.

## Running locally

```
pip install -r requirements-dev.txt
python -m pytest -q          # needs no API keys and never touches data/
python telegram_bot.py       # the TEST bot, with the laptop .env
```

`.claude/settings.json` runs the tests before every `git commit` Claude makes
and blocks the commit if they fail (`scripts/precommit_tests.py`).

The laptop `.env` uses a separate test bot token and `JOBS_ENABLED=0` (no
background jobs). The laptop DB has no eBay user token, so the test bot cannot
change eBay. Use `.env.example` as the template.

## Layout

- `telegram_bot.py`: entry point only. It calls `bot.app.main()`.
- `bot/`: Telegram layer.
  - `app.py`: startup, handlers, background loops.
  - `intake.py`, `flow.py`, `text_router.py`: photos to published listing.
  - `*_cmds.py`: commands, by area.
  - `jobs.py`: scheduled loops.
  - `state.py`: per-user state.
  - `util.py`: helpers.
- `pipeline/`: the listing pipeline.
  - `stages.py`: stage runner.
  - `draft.py`, `title.py`, `specifics.py`, `category.py`: listing content.
  - `price.py`, `pricing_v2.py`, `quick_price.py`, `reprice.py`: pricing.
  - `offers.py`, `sync.py`: offers and sales.
- `ebay/`: eBay API clients (inventory, listings, orders, browse, marketing,
  negotiation, auth).
- `db.py`: SQLite (`data/ross.db`).
- `config.py`: every setting, read from env. `BASE_DIR`, `DATA_DIR` and
  `LOG_DIR` live here.
- `identify.py`, `ross_tag.py`: product identification and Ross tag reading
  (Gemini).
- `llm.py`: Gemini client. Use `LazyClient`, never create a client at import.
- `claude_llm.py`: Claude client.
- `scripts/`: deploy, notify and one-off reports.
- `web/`: the 3D site (Vite). Not wired to real data yet.
- `docs/`: playbook, pricing design, cleanup plan, business reviews.

## Code conventions

These are enforced by `tests/test_bot_conventions.py`:

- `from bot import state`, then use `state.review` and so on. Never import
  names from `bot.state`, and never rebind its dicts.
- Reach `db`, `identify`, `llm`, `pipeline.*`, `ebay.*` and similar as
  `module.attr` (`db.get_item(...)`). Don't import functions by name, or test
  patches miss them.
- `bot/` never imports `telegram_bot`.

Other conventions:

- Paths come from `config` (`BASE_DIR`/`DATA_DIR`), never from the working
  directory.
- Updates are handled concurrently. Guard per-item work with
  `util._claim` and `util._release`.
- New behaviour gets a test. Shared Telegram fakes are in `tests/tg_fakes.py`.

## Pricing

Manual prices come from MSRP and gut feel, so they are not ground truth. Judge pricing changes by sales outcomes, using `scripts/backtest_pricing.py`.
Prefer small price cuts plus monitoring, and offers to watchers over deep cuts.
See `docs/pricing-design.md` and `docs/ebay-playbook.md`.
