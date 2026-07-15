# Ross Resale Bot

A Telegram bot that turns photos of an item into a ready-to-publish eBay
listing. You send photos; it identifies the product, prices it against real eBay
comps, writes the listing, pushes it to eBay as a draft, and publishes it on your
command. You confirm the identification and the price along the way, and review
and approve every item before anything goes live.

## How it works

Each item moves through a pipeline. Every step saves its result to a local
SQLite database and advances the item's status, so a failure can be retried from
where it stopped instead of starting over. The pipeline pauses for your OK after
identification and after pricing, so you can correct either before it continues.

```
photos -> identify -> [confirm] -> price -> [confirm] -> draft -> [approve] -> eBay draft -> publish
```

1. **Capture.** You send photos to the bot. They are batched (it waits a few
   seconds for more photos), saved to `data/inbox/<id>/`, and recorded as a new
   item with status `captured`. Photo order matters:
   - the **1st** photo is the overview (used as the eBay gallery cover),
   - the **2nd** photo is the tag close-up (fed to Gemini to identify the item;
     on the eBay listing it is moved to appear as the *last* product image),
   - the **last** photo is always the Ross price tag/check. Its CODE128 barcode
     is decoded (`receipt.py`) for the 12-digit item code and the price you paid
     — Ross barcodes are `<12-digit code><6-digit price in cents>`, e.g.
     `400286461425000999` → code `400286461425`, paid `$9.99`. OCR reads the
     printed "Original $XX.XX" line (not in the barcode) and backs up the barcode
     if it can't be decoded. The result is stored as cost data and **never
     posted to eBay**. If neither the barcode nor OCR yields the price and code,
     the bot asks you to set them with `/receipt <price> <code>`.

2. **Identify** (`identify.py`). Before calling the model, the product's
   UPC/EAN barcode is decoded directly off the photos with `pyzbar` (scanning
   every photo, since the tag isn't always in the first few) — the exact digits,
   not the model's unreliable read of a tiny barcode. That UPC, plus any
   style/SKU codes read off the tag, are handed to the model as authoritative
   input. Then two Gemini calls run:
   - *Research* (model `gemini-2.5-flash`, with Google Search) searches the UPC
     first, then the style number and visual details, to determine the exact
     product, its real specifications, and its market price, and writes a
     plain-text findings report. This call is retried if it comes back empty, and
     if it still can't confirm the item it says so rather than guessing.
   - *Format* (model `gemini-2.5-flash`) turns that report into a structured
     JSON object: brand, product name, color, condition, specifications, price
     evidence, and an eBay search query.

   Result is saved; status becomes `identified`. The bot shows you what it found
   and waits: reply `confirm` to price it, or type a correction (e.g. "brand is
   Tommy Jeans, color navy") that the model applies before re-showing it.

3. **Price** (`pipeline/price.py`). Two eBay scrapers run in parallel via Apify:
   sold listings and active listings. The bot filters to brand-relevant comps,
   takes the median of sold prices, undercuts it (and the cheapest active
   competitor), and rounds to a `.99` price. If there are too few sold comps, it
   falls back to the resale estimate found during identification. The eBay URL of
   the best-match comp the price is anchored to is saved on the item
   (`price_source_url`). Status becomes `priced`. The bot shows the suggested
   price and waits: reply `confirm` to continue, or type a price to override it
   (a whole number is charm-priced, e.g. `35` → `$34.99`).

4. **Draft** (`pipeline/draft.py`). Gemini (`gemini-2.5-flash`) writes the
   listing — title, description, item specifics, category, and price — using the
   identification data plus a hint block listing the exact item specifics eBay
   defines for the likely category. A standard shipping/returns/about section is
   appended. Status becomes `review`.

5. **Review.** The bot sends you the draft. You reply:
   - `approve` to push it to eBay,
   - `reject` to discard it, or
   - a free-text correction (e.g. "color is navy not black"), which the model
     applies and re-sends for review.

6. **Create eBay draft** (`ebay/inventory.py`). On approval, the bot validates
   the category, fills any required item specifics, uploads the photos to
   Cloudinary (eBay needs hosted image URLs), and creates an unpublished offer.
   If the identify step found an official brand/retailer product image, it is
   re-hosted and appended as a secondary image (after your real photos, so it's
   never the gallery cover) for items of any condition. Status becomes
   `ebay_draft`. Nothing is live yet.

7. **Publish.** `/activate` publishes the offer; the item goes live and status
   becomes `published`. The bot replies with the listing URL. If a default ad
   rate is set (`EBAY_DEFAULT_AD_RATE_PCT`, 4% by default), the listing is
   automatically enrolled in Promoted Listings at that rate on publish; adjust
   any individual listing with `/promote <id> <pct>`.

You can also attach more photos to an existing item at any time with
`/addphotos <id>` — new photos are appended, and if the item is already an eBay
draft or live listing, the offer is rebuilt so the photos reach eBay.

## Architecture

| File | Responsibility |
| ---- | -------------- |
| `telegram_bot.py` | Bot entry point, commands, and the pipeline orchestration |
| `identify.py` | Step 2: two-stage Gemini product identification |
| `pipeline/price.py` | Step 3: eBay comps (Apify) and pricing logic |
| `pipeline/draft.py` | Step 4: listing copy generation |
| `receipt.py` | Decode the Ross tag (last photo): barcode → paid price + 12-digit code, OCR → original price |
| `ebay/auth.py` | eBay OAuth: user token (seller) and app token (catalog) |
| `ebay/inventory.py` | Step 6/7: build, create, and publish eBay offers |
| `ebay/marketing.py` | Promoted Listings: campaign + per-listing ad rate |
| `ebay/taxonomy.py` | eBay category validation and item-aspect metadata (cached) |
| `db.py` | SQLite item store |
| `llm.py` | Shared Gemini client factory and retry wrapper |
| `config.py` | Settings and defaults |
| `retry_publish.py` | Helper script to rebuild an eBay draft for one item |
| `backup.py` | Standalone backup of `data/ross.db` (online snapshot) + `data/inbox/` |

### Models

The grounded research step defaults to `gemini-3.5-flash`. The 2.5 generation's
search-grounding path frequently returned empty responses (`finish_reason=STOP`
with zero searches) and 503s, which stalled identification; the newer 3.5
grounding stack is markedly more reliable on the same grounded calls. The format
and draft steps only structure data the pipeline already has (no search) and were
never affected, so they stay on the cheaper `gemini-2.5-flash` — 3.5-flash costs
several times more per token, and there's no reason to pay it where grounding
isn't involved. Both are overridable via `GEMINI_MODEL` / `GEMINI_FAST_MODEL`.

### Data

Everything lives in one SQLite file, `data/ross.db`:

- `items` — one row per item, with JSON columns for `photos`, `identification`,
  `pricing`, `listing`, `ebay`, and `receipt` (the OCR'd Ross receipt: paid
  price, original price, and 12-digit code), a `price_source_url` (the comp the
  price is anchored to), plus a `status` that tracks pipeline progress
  (`captured`, `identified`, `priced`, `drafted`, `review`, `approved`,
  `ebay_draft`, `published`, `sold`, `rejected`). A sold item also stores its
  `sale_price` in the `ebay` JSON column, used by `/profit`. New columns are added
  by an automatic `ALTER TABLE` migration on startup.
- `ebay_tokens` — the seller's OAuth access and refresh tokens.
- `taxonomy_cache` — eBay category and aspect lookups, cached for 30 days.

Photos are stored on disk under `data/inbox/`.

## Requirements

- Python 3.11+
- Accounts and API keys for: Google Gemini, Telegram, Apify, eBay Developer,
  and Cloudinary
- The **Tesseract OCR** binary (for the printed "Original" price on the Ross
  tag). On Windows, install the UB-Mannheim build and either add it to `PATH` or
  set `TESSERACT_CMD` in `.env` to the full path of `tesseract.exe`. The Python
  wrappers (`pytesseract`, `Pillow`, `pyzbar`) come from `requirements.txt`;
  `pyzbar` bundles the zbar barcode library on Windows, so no extra system
  install is needed for barcode decoding.

## Setup

1. Install dependencies:

   ```
   pip install -r requirements.txt
   ```

2. Copy `.env.example` to `.env` and fill in your keys:

   ```
   cp .env.example .env
   ```

3. Authorize the bot with your eBay seller account (one time). This prints a
   consent URL; approve it, then exchange the returned code:

   ```
   python -m ebay.auth
   python -m ebay.auth exchange <code>
   ```

   You need Business Policies (shipping and returns) set up in eBay Seller Hub
   first. The requested scopes include `sell.marketing` (for Promoted Listings) —
   if you authorized before that was added, re-run these two commands to
   re-consent, or ad-rate calls will 403.

4. Run the bot:

   ```
   python telegram_bot.py
   ```

## Telegram commands

| Command          | What it does                                          |
| ---------------- | ----------------------------------------------------- |
| _(send photos)_  | Start a new item, then step through the confirm gates |
| `confirm`        | At a gate: accept the identification / price and continue |
| _(free text)_    | At a gate: correct the identification, or set the price; at review, correct the draft or `approve`/`reject` |
| `wait`           | Extend the photo-batching window for a large batch    |
| `/status [status]` | List items and their pipeline status; optional filter (e.g. `/status published`), with a count-per-status header |
| `/listing [id]`  | Show the current draft for an item                    |
| `/comps [id]`    | Show the sold/active comps the price was built from   |
| `/addphotos [id]`| Attach more photos to an existing item                |
| `/receipt [id] <price> <code>` | Manually set the Ross cost + 12-digit code (when the tag barcode couldn't be read) |
| `/setprice [id] <price>` | Set the price (charm-priced); pushes to eBay if the item has an offer |
| `/activate [id]` | Publish an eBay draft, making it a live listing       |
| `/end [id]`      | End a live listing (withdraw it); drops back to a draft to relist |
| `/sold [id] [price]` | Mark an item sold and record the sale price; replies with profit vs. Ross cost |
| `/profit`        | Summarize profit across all sold items (before eBay fees/shipping) |
| `/promote [id] <pct>` | Set/adjust a listing's Promoted Listings ad rate (2–100%) |
| `/retry [id]`    | Re-run the failed pipeline step for an item (honors the confirm gates) |
| `/delete [id]`   | Delete an item, its photos, and its eBay offer (ends it first if live) |
| `/health`        | Check eBay token, business policies, ad scope, and Cloudinary |
| `/auth [url]`    | Re-consent the eBay account: no arg prints the consent URL; pass the redirect URL to finish |
| `/whoami`        | Show your Telegram user id (to fill `TELEGRAM_ALLOWED_USER_IDS`) |
| `/errors`        | Show recent errors (the server console isn't visible from the phone) |
| `/cancel`        | Discard photos being captured, or drop out of a confirm gate |

`[id]` accepts a full item id or a unique prefix (as shown by `/status`). If
omitted, it defaults to the most recently touched item.

## Configuration

Defaults are in `config.py`; the marked ones can be overridden in `.env`:

- `GEMINI_MODEL` / `GEMINI_FAST_MODEL` — research vs. format/draft models
- `GEMINI_PHOTO_LIMIT` — how many photos are sent to the paid API per item
- `MAX_CONCURRENT_LISTINGS` — how many items may run the pipeline at once
- `COMPS_COUNT` / `ACTIVE_COUNT` — how many comps to pull when pricing
- `UNDERCUT_PCT` — how far below the comp median to price
- `DEBUG_MODE` — pulls fewer comps (3 sold / 3 active) to save time and cost when
  set. **Defaults to `false`.** Leave it off in production: with only 3 sold comps
  the pipeline can't clear the ≥3 threshold it needs to trust comps and silently
  falls back to the research estimate. Set `DEBUG_MODE=true` only for local testing.
- `TELEGRAM_ALLOWED_USER_IDS` — comma-separated Telegram user ids allowed to use
  the bot. **Unset means the bot is open to anyone** (a loud warning prints on
  startup). Send `/whoami` to the bot to get your id, then set this and restart.
- `EBAY_FULFILLMENT_POLICY_ID` / `EBAY_RETURN_POLICY_ID` — pin specific Business
  Policies (otherwise the first policy on the account is used)
- `EBAY_DEFAULT_AD_RATE_PCT` — Promoted Listings ad rate auto-applied on publish
  (default `4`; set `0` to disable), and `EBAY_PROMOTED_CAMPAIGN_NAME`
- eBay marketplace, currency, merchant location, and ship-from address

## Notes

- The bot forces IPv4 for Telegram and disables HTTP keep-alive on the Gemini
  client, both to avoid intermittent connection hangs on long-running processes.
- eBay item specifics are validated against the live category before publishing;
  required ones are auto-filled where it is safe, and you are told which ones
  could not be (rather than publishing something wrong).
- The eBay description is rendered to HTML before sending, because eBay collapses
  plain-text line breaks.
- The price gate flags **comp starvation**: if the Apify scrapers return rows but
  none have a parseable price (a sign the third-party actor changed its output
  schema), the suggested price shows a ⚠️ warning instead of quietly falling back
  to the research estimate.

## Backups

`data/` is not in git, yet `data/ross.db` holds the items, receipt/cost history,
and the eBay OAuth tokens, and `data/inbox/` holds the only copy of item photos
until they reach Cloudinary. `backup.py` snapshots both:

```
python backup.py
```

It uses SQLite's online-backup API, so it's safe to run while the bot is live.
Point it at a synced folder to get the data off the machine, and schedule it
(Windows Task Scheduler / cron):

- `BACKUP_DIR` — where snapshots go (default `data/backups`; set to a OneDrive/
  Dropbox path for offsite copies)
- `BACKUP_KEEP` — how many snapshots of each kind to retain (default 14)

## License

MIT. See [LICENSE](LICENSE).
