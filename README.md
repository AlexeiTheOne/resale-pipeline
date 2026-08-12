# Ross Resale Bot

A Telegram bot that turns photos of an item into a ready-to-publish eBay
listing. You send photos; it identifies the product, prices it against real eBay
comps, writes the listing, pushes it to eBay as a draft, and publishes it on your
command. It asks about the items it isn't sure of, and you review and approve
every item before anything goes live.

## How it works

Each item moves through a pipeline. Every step saves its result to a local
SQLite database and advances the item's status, so a failure can be retried from
where it stopped instead of starting over.

The pipeline can pause for your OK after identification and after pricing, but it
only does so when the evidence is weak — a confident identification and a
solid-comp price go straight through. See
[Gates only where they earn their keep](#gates-only-where-they-earn-their-keep).

```
photos -> identify -> [confirm?] -> price -> [confirm?] -> draft -> [approve] -> eBay draft -> publish
                          \_ skipped when the evidence is strong _/
```

1. **Capture.** You send photos to the bot. They are batched (it waits a few
   seconds for more photos), saved to `data/inbox/<id>/`, and recorded as a new
   item with status `captured`. Photo order matters:
   - the **1st** photo is the overview (used as the eBay gallery cover),
   - the **2nd** photo is the tag close-up (fed to Gemini to identify the item;
     on the eBay listing it is moved to appear as the *last* product image),
   - the **last** photo should be the Ross price tag. It carries the 12-digit
     item code and the price you paid — Ross barcodes are `<12-digit code><6-digit
     price in cents>`, e.g. `400286461425000999` → code `400286461425`, paid
     `$9.99`. The tag is stored as cost data and **never posted to eBay**.

     The tag is *located* rather than assumed: if a photo elsewhere in the set
     decodes a Ross barcode, that one is peeled instead (`/addphotos` can append
     photos after the tag). If none decodes, the last photo is peeled anyway —
     that fallback is load-bearing, because only 44% of real tag photos decode
     and refusing to peel would leave the tag, showing what you paid, in the
     listing. When the price can't be read at all, the bot asks for it with
     `/receipt <price> [code]`.

   Reading the tag is a three-tier chain, each tier only reached when the one
   above it fails to produce the **paid price** (the number every profit
   calculation rests on):

   1. **Barcode** — exact and free, but it only decodes on 44% of real tag
      photos. Glare, angle, a crease through the bars and Telegram's compression
      defeat the rest.
   2. **OCR, multiple renderings** — plain, upscaled, and rotated 180/90/270.
      Tags are frequently photographed upside down, or as two stickers
      overlapping at 180° to each other, so a single rendering isn't enough; the
      old single `--psm 6` pass recovered nothing at all on the hard tags.
   3. **Vision model** — one cheap `gemini-2.5-flash` call. Reading a creased,
      rotated, double-stickered tag is ordinary work for it and hopeless for
      Tesseract. Disable with `TAG_VISION_FALLBACK=false`.

   Measured over the real library, this takes automatic cost capture from 26 of
   59 tags to **41 of 59**. The vision tier is validated against the 26 tags
   whose barcode gives ground truth: **26/26 prices correct**, with its three
   misses being *omissions* rather than wrong values. It is never consulted when
   the barcode already gave an exact price, its code must match a real Ross shape
   (12 digits, `400` prefix) to be accepted, and when a tag genuinely doesn't
   show a price it returns nothing rather than guessing.

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
   sold listings and active listings. **A price is only ever derived from real
   comps** — when the comps aren't good enough, the bot returns *no price* and
   asks you for one, rather than emitting a number it can't stand behind.

   - **Query ladder.** The identify step's `search_query` is tried first. If it
     doesn't yield usable comps, the search broadens — `brand + product_name`,
     then `brand + item_type` — stopping at the first rung that works. Rungs only
     run on a miss, so a well-identified item still costs a single pass.
   - **Trim, then judge.** Comps are filtered to ones that actually name the
     brand, used items are dropped, and the rest are outlier-trimmed to the
     interquartile range. The surviving set is graded on **count and
     dispersion** (p90/p10): 10 comps spanning 9× are worse evidence than 4 that
     agree, so both bars must clear.
     - `solid` — 5+ comps, ≤2.5× spread
     - `thin` — 3+ comps, ≤4× spread (shown for a human look)
     - `none` — no price is derived; you're asked for one
   - **Price floors.** The median is undercut and capped by the cheapest active
     competitor, but never below `sold_p10` — the 10th percentile of *proven*
     sales. Undercutting past what people demonstrably paid isn't competing,
     it's donating margin. An active listing far below the sold median is
     treated as a different product and ignored entirely.
   - **Research is a sanity check, not a price.** The resale estimate from
     identification is *never* used as the price. If it disagrees with the comps
     by more than ~2×, that's flagged for you to look at.

   The eBay URL of the best-match comp is saved on the item
   (`price_source_url`), and the machine's own suggestion is recorded
   immutably as `machine_price` — a manual override replaces `suggested_price`
   and is kept as `manual_price`, so the gap between the two stays measurable.
   Status becomes `priced`. The bot shows the suggested price with its evidence
   grade and waits: reply `confirm` to continue, or type a price to override it
   (a whole number is charm-priced, e.g. `35` → `$34.99`).

4. **Draft** (`pipeline/draft.py`). Gemini (`gemini-2.5-flash`) writes the
   listing — title, description, item specifics, and category — using the
   identification data plus a hint block listing the exact item specifics eBay
   defines for the likely category. A standard shipping/returns/about section is
   appended. Status becomes `review`.

   **The copywriter does not set the price.** It's asked to echo the computed
   price and mostly does, but on 6 of 64 real listings it wrote its own number
   instead — from −22% to +71% off — and that number went live. The price is now
   overwritten with the computed one after generation, and a typed correction at
   review can't move it either. Price changes go through the price gate or
   `/setprice`, which record what changed and respect the margin floor.

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

### Gates only where they earn their keep

The identify and price gates clear themselves when the evidence is strong enough
(`AUTO_CONFIRM`, on by default), so a clean item runs photos → draft without
stopping and you're only asked about the ones that are genuinely ambiguous. A
gate that does appear says why:

- **identify** — auto-confirms at model confidence ≥ 0.8, or ≥ 0.6 when the
  product's UPC barcode was decoded off the photos (hard evidence of exactly
  which product it is, worth more than the model's self-assessment). Stops for an
  unconfirmed item or any condition flags.
- **price** — auto-confirms only on `solid` comp evidence with no review flags.
  Thin comps, no comps, scraper schema drift, or a comps-vs-research conflict all
  stop and ask.

**The review gate before anything reaches eBay is never skipped** — nothing goes
live without your explicit approve.

### Hauls

`/haul` arms multi-item capture. Shoot each item the way you normally do,
ending with a photo of its Ross tag — the tag itself is the divider. All items
then run at once, capped by `MAX_CONCURRENT_LISTINGS`.

**How items are divided.** Three boundaries, cheapest first:

| Divider | Reliability | Needs |
| ------- | ----------- | ----- |
| Ross tag barcode decodes | 44% of tag photos | nothing (local) |
| Ross tag **recognised** by the vision model | 21/21 tags found, 0 false positives on 6 merchandise sets | one API call per haul |
| **Blackout frame** — one dark photo, lens covered | absolute | nothing (local) |

Recognising a tag is a much easier problem than reading one, which is why the
middle row works where the barcode doesn't: on a tag creased through the bars, or
photographed as two stickers at 180° to each other, or whose price a human can't
make out, "is that a Ross tag?" is still obvious. It runs as one call per batch of
photos, not one per photo.

The blackout frame remains the guaranteed override — no network, no model, and
nothing can argue with it. Use it when a tag is missing or unclear; the three can
be mixed freely within one haul. Across 413 real photos the darkest averaged
56/255 while a covered lens lands near zero, so `DARK_FRAME_MAX_LUMA` (default 25)
sits in a gap nothing real occupies: measured false positives, **0 of 413**.

No divider is needed after the last item. Consecutive blackouts collapse rather
than creating empty items. If a batch arrives with no tag found *and* no blackout,
the bot **refuses** and asks rather than listing several products as one item.

Haul mode **stays armed until `/cancel`**, so a pause longer than the capture
window doesn't silently drop you back to single-item mode and turn the next
armful of photos into one item containing several products.

Because many items run together but only one can hold your attention, gates
**queue**: you're asked about one item at a time, and answering it brings up the
next. Deleting an item, or hitting one that was already deleted, hands the slot
to the next in line rather than stranding the rest. A closing digest summarizes
where the whole batch landed. Photos after the last Ross tag mean an item without
a tag — you're told, and it's left out rather than silently folded into the
previous item.

While a haul is running, `approve` and `reject` always go to the draft waiting at
review, never to whichever item happens to be sitting at a gate.

**Aiming a correction at one item.** A haul puts several drafts on screen at
once, so "which one did you mean?" can't be answered by a most-recent pointer —
that lands the correction on whichever draft was shown last, rarely the one
you're looking at. Two ways to aim:

- **Reply** to that item's message (the natural one), or
- **lead with its id**: `a1b2c3d4 color is navy`.

Every gate and draft is labelled with its item id so you can tell them apart, and
an aimed message wins over whatever else is active — including pulling an item
back to its gate, with the displaced one returned to the front of the queue.
Un-aimed messages behave exactly as they do for a single item.

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
| `ebay/listings.py` | Enumerate every live listing on the account (Trading API), including ones the bot didn't create |
| `ebay/marketing.py` | Promoted Listings: campaign + per-listing ad rate |
| `ebay/taxonomy.py` | eBay category validation and item-aspect metadata (cached) |
| `db.py` | SQLite item store |
| `llm.py` | Shared Gemini client factory and retry wrapper |
| `config.py` | Settings and defaults |
| `retry_publish.py` | Helper script to rebuild an eBay draft for one item |
| `backup.py` | Standalone backup of `data/ross.db` (online snapshot) + `data/inbox/` |
| `report.py` | Build the Excel profit report (photos, fees, live formulas); also `/report` |

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
| `confirm`        | At a gate: accept the identification / price and continue (strong-evidence items skip the gate entirely) |
| _(free text)_    | At a gate: correct the identification, or set the price; at review, correct the draft or `approve`/`reject`. During a haul, **reply** to an item's message or prefix its id (`a1b2c3d4 color is navy`) to aim at that one |
| `wait`           | Extend the photo-batching window for a large batch    |
| `/haul`          | Multi-item mode: dump a whole Ross run, split into items on each Ross tag (or a blackout frame — one dark photo — where a tag is missing) |
| `/status [status]` | List items and their pipeline status; optional filter (e.g. `/status published`), with a count-per-status header |
| `/listing [id]`  | Show the current draft for an item                    |
| `/comps [id]`    | Show the sold/active comps the price was built from   |
| `/addphotos [id]`| Attach more photos to an existing item                |
| `/receipt [id] <price> <code>` | Manually set the Ross cost + 12-digit code (when the tag barcode couldn't be read) |
| `/setprice [id] <price>` | Set the price (charm-priced); pushes to eBay if the item has an offer |
| `/setqty [id] <n>` | Set the available quantity on eBay (once the item has an offer); listings default to 1 |
| `/sync`          | Reconcile with eBay: pull live price/quantity, auto-record sold orders, **adopt listings you made by hand in Seller Hub**, and re-link items that were relisted under a new listing id |
| `/activate [id]` | Publish an eBay draft, making it a live listing       |
| `/end [id]`      | End a live listing (withdraw it); drops back to a draft to relist |
| `/sold [id] [price]` | Mark an item sold and record the sale price; replies with profit vs. Ross cost |
| `/profit`        | Summarize profit across all sold items (before eBay fees/shipping) |
| `/report`        | Build & send an Excel profit report: photo, title, price, shipping, cost, and profit net of eBay fees + ad rate (assumptions editable in the sheet). Totals band **SOLD (realized)** / **STILL LISTED (projected)** / TOTAL. Money columns are line totals (per-unit × qty); **Net / unit** is what one unit makes. **Sold rows use eBay's real figures** — shipping collected, fees charged, and the postage you actually paid for the label — while unsold rows use the assumptions. Items with no scanned receipt are costed from an editable % of list price rather than as free |
| `/promote [id] <pct>` | Set/adjust a listing's Promoted Listings ad rate (2–100%) |
| `/retry [id]`    | Re-run the failed pipeline step for an item (honors the confirm gates) |
| `/delete [id]`   | Delete an item, its photos, and its eBay offer (ends it first if live) |
| `/health`        | Check eBay token, business policies, ad scope, and Cloudinary |
| `/auth [url]`    | Re-consent the eBay account: no arg prints the consent URL; pass the redirect URL to finish |
| `/whoami`        | Show your Telegram user id (to fill `TELEGRAM_ALLOWED_USER_IDS`) |
| `/errors`        | Show recent errors (the server console isn't visible from the phone) |
| `/help`          | List every command and what it does                    |
| `/cancel`        | Discard photos being captured, or drop out of a confirm gate |

`[id]` accepts a full item id or a unique prefix (as shown by `/status`). If
omitted, it defaults to the most recently touched item.

## Configuration

Defaults are in `config.py`; the marked ones can be overridden in `.env`:

- `GEMINI_MODEL` / `GEMINI_FAST_MODEL` — research vs. format/draft models
- `GEMINI_PHOTO_LIMIT` — how many photos are sent to the paid API per item
- `MAX_CONCURRENT_LISTINGS` — how many items may run the pipeline at once
- `COMPS_COUNT` / `ACTIVE_COUNT` — how many comps to *keep* after trimming
- `COMPS_FETCH_COUNT` / `ACTIVE_FETCH_COUNT` — how many raw rows to *pull* per
  search (wider than the keep count, because outlier-trimming discards some)
- `UNDERCUT_PCT` — how far below the comp median to price
- `PRICE_SOLID_MIN_COMPS` / `PRICE_SOLID_MAX_DISPERSION` and
  `PRICE_THIN_MIN_COMPS` / `PRICE_THIN_MAX_DISPERSION` — the bars a comp set must
  clear to count as solid or thin evidence (see step 3)
- `ACTIVE_FLOOR_MIN_RATIO` — how far below the sold median an active listing may
  be before it's treated as a different product and ignored
- `RESEARCH_SANITY_RATIO` — comp-vs-research disagreement that triggers a flag
- `AUTO_CONFIRM` — let strong-evidence items clear their own identify/price gates
  (default on; the eBay review gate is never skipped), plus
  `AUTO_CONFIRM_MIN_IDENT_CONFIDENCE` and
  `AUTO_CONFIRM_MIN_IDENT_CONFIDENCE_WITH_UPC`
- `EBAY_SHIP_CHARGED` / `EBAY_SHIP_COST` — shipping both directions, used by the
  repricing floor. **Set these to your real numbers** — they default to `10`/`10`
  to match `report.py`'s assumptions, and if you actually ship free the floor is
  wrong by the full postage until `EBAY_SHIP_CHARGED` is `0` here too.
- `DEBUG_MODE` — pulls only 3 sold / 3 active comps to save time and cost when
  set. **Defaults to `false`.** Leave it off in production: 3 comps can't clear
  the `solid` bar, so every item lands on `thin` at best and stops to ask you.
  Set `DEBUG_MODE=true` only for local testing.
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
  schema), the suggested price shows a ⚠️ warning. It also explains itself when a
  price was held up off the active floor, or when comps and research disagree.
- Neither scraper is sent a condition filter. eBay's maps to "New" (condition
  1000) only, which throws away new-without-tags and open-box comps — most of the
  pool for apparel. Used rows are dropped locally instead, on the row's own
  condition text, which keeps the pool wide and the filtering visible.
- `_call` retries transient Apify failures (connection resets, 5xx, 429). The
  query ladder makes up to one scrape per rung, so an un-retried blip would cost
  an item its comps.

## Listings the bot didn't create

The Sell Inventory API only knows about offers created *through* it, so a listing
you make by hand in Seller Hub is invisible to it — and therefore to `/status`,
`/report`, `/profit` and the weekly price check. `ebay/listings.py` closes that
hole using the Trading API's `GetMyeBaySelling`, which returns every active
listing regardless of origin (authenticated with the same OAuth token, passed as
`X-EBAY-API-IAF-TOKEN`).

`/sync` then reconciles three ways:

- **Adopts** a live listing with no bot SKU as a local item at status `published`,
  with its real title, price and quantity. It has no photos and no Ross cost —
  the report flags the missing cost in orange rather than reporting the whole
  sale price as profit.
- **Re-links** an item whose listing id changed. A relist mints a *new* id, which
  leaves the stored one pointing at a dead listing — and the weekly price check
  reading traffic for the wrong thing.
- **Flags, without changing,** any item that eBay says is live while the local
  status says otherwise. That's usually a relist after a sale, but it can equally
  be a mistaken `/sold`, and guessing either way would rewrite sales history.

## Weekly price check: price problems vs. findability problems

A price cut is only ever suggested when people are demonstrably **looking and not
buying**. If nobody is seeing the listing, the problem is discovery, and cutting
the price donates margin without fixing anything:

| Verdict | Means | Fix |
| ------- | ----- | --- |
| `OVERPRICED` | Real views, zero watchers | **Price cut** |
| `STALE` | Old, real views, nothing else firing | **Price cut** |
| `SEND_OFFERS` | Watchers building, still unsold | Offer to watchers first |
| `LOW_CTR` | Seen in search, not clicked | Cover photo, then title |
| `INVISIBLE` | Barely any impressions | Title keywords, category, `/promote` |
| `STALE_UNSEEN` | Old, but too few views to blame the price | Findability, or relist to refresh ranking |
| `TOO_NEW` / `HEALTHY` | — | Nothing |

Every suggested cut is still floored at cost + fees + shipping + minimum margin,
and an item with no recorded Ross cost is never cut at all.

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
