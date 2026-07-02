# Ross Resale Bot

A Telegram bot that turns photos of an item into a ready-to-publish eBay
listing. You send photos; it identifies the product, prices it against real eBay
comps, writes the listing, pushes it to eBay as a draft, and publishes it on your
command. You review and approve every item before anything goes live.

## How it works

Each item moves through a pipeline. Every step saves its result to a local
SQLite database and advances the item's status, so a failure can be retried from
where it stopped instead of starting over.

```
photos -> identify -> price -> draft -> [you review] -> eBay draft -> publish
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
     posted to eBay**.

2. **Identify** (`identify.py`). Two Gemini calls:
   - *Research* (model `gemini-2.5-pro`, with Google Search) reads the tag —
     UPC, style number, color code — and searches the web and eBay to determine
     the exact product, its real specifications, and its market price. It writes
     a plain-text findings report.
   - *Format* (model `gemini-2.5-flash`) turns that report into a structured
     JSON object: brand, product name, color, condition, specifications, price
     evidence, and an eBay search query.

   Result is saved to the item; status becomes `identified`.

3. **Price** (`pipeline/price.py`). Two eBay scrapers run in parallel via Apify:
   sold listings and active listings. The bot filters to brand-relevant comps,
   takes the median of sold prices, undercuts it (and the cheapest active
   competitor), and rounds to a `.99` price. If there are too few sold comps, it
   falls back to the resale estimate found during identification. The eBay URL of
   the best-match comp the price is anchored to is saved on the item
   (`price_source_url`). Status becomes `priced`.

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
   Status becomes `ebay_draft`. Nothing is live yet.

7. **Publish.** `/activate` publishes the offer; the item goes live and status
   becomes `published`. The bot replies with the listing URL.

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
| `ebay/taxonomy.py` | eBay category validation and item-aspect metadata (cached) |
| `db.py` | SQLite item store |
| `llm.py` | Shared Gemini client factory and retry wrapper |
| `config.py` | Settings and defaults |
| `retry_publish.py` | Helper script to rebuild an eBay draft for one item |

### Models

The research step uses `gemini-2.5-pro` because grounded reasoning over search
results pays off there. The format and draft steps only structure data the
pipeline already has, so they use the faster, cheaper `gemini-2.5-flash`. Both
are overridable via `GEMINI_MODEL` and `GEMINI_FAST_MODEL`.

### Data

Everything lives in one SQLite file, `data/ross.db`:

- `items` — one row per item, with JSON columns for `photos`, `identification`,
  `pricing`, `listing`, `ebay`, and `receipt` (the OCR'd Ross receipt: paid
  price, original price, and 12-digit code), a `price_source_url` (the comp the
  price is anchored to), plus a `status` that tracks pipeline progress
  (`captured`, `identified`, `priced`, `drafted`, `review`, `approved`,
  `ebay_draft`, `published`, `rejected`). New columns are added by an automatic
  `ALTER TABLE` migration on startup.
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
   first.

4. Run the bot:

   ```
   python telegram_bot.py
   ```

## Telegram commands

| Command          | What it does                                          |
| ---------------- | ----------------------------------------------------- |
| _(send photos)_  | Start a new item. Reply `approve` / `reject` / a note |
| `wait`           | Extend the photo-batching window for a large batch    |
| `/status`        | List all items and their pipeline status              |
| `/listing [id]`  | Show the current draft for an item                    |
| `/activate [id]` | Publish an eBay draft, making it a live listing       |
| `/retry [id]`    | Re-run the failed pipeline step for an item           |
| `/delete [id]`   | Delete an item, its photos, and its eBay offer        |
| `/health`        | Check eBay token, business policies, and Cloudinary    |
| `/cancel`        | Discard photos currently being captured               |

`[id]` accepts a full item id or a unique prefix (as shown by `/status`). If
omitted, it defaults to the most recently touched item.

## Configuration

Defaults are in `config.py`; the marked ones can be overridden in `.env`:

- `GEMINI_MODEL` / `GEMINI_FAST_MODEL` — research vs. format/draft models
- `GEMINI_PHOTO_LIMIT` — how many photos are sent to the paid API per item
- `COMPS_COUNT` / `ACTIVE_COUNT` — how many comps to pull when pricing
- `UNDERCUT_PCT` — how far below the comp median to price
- `DEBUG_MODE` — when true, pulls fewer comps to save time and cost
- eBay marketplace, currency, merchant location, and ship-from address

## Notes

- The bot forces IPv4 for Telegram and disables HTTP keep-alive on the Gemini
  client, both to avoid intermittent connection hangs on long-running processes.
- eBay item specifics are validated against the live category before publishing;
  required ones are auto-filled where it is safe, and you are told which ones
  could not be (rather than publishing something wrong).
- The eBay description is rendered to HTML before sending, because eBay collapses
  plain-text line breaks.

## License

MIT. See [LICENSE](LICENSE).
