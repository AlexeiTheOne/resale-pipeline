# Legacy tools assessment (post-cutover ideas)

Status: **proposal only, no code changed.** Written 2026-11-02 by Person B's
Claude for both developers. Pricing and listings are Person A's area, so both
proposals need Person A's agreement before anyone builds them. Planned for
**after** the SQL Server cutover.

**Sources.**
- The old tools, read-only: `C:\Users\boris\Projects\resale-pipeline-OLD-keep`,
  an earlier full copy of the project dated 2026-08-31 to 2026-09-29.
- The current code: `main` at `0cdc8ee`.

---

## 1. What each old tool does

### `comps.py` (727 lines, command line): pre-buy comps for a text description

You give it a description, plus optionally `--cost` (the Ross price),
`--ship`, `--size`, `--brand`, `--require` and `--exclude`.

**Sold comps**
- They come from the Apify sold-listings actor, through the **old**
  `pipeline/price.py`, which no longer exists.
- It fetches 25 rows. If they come back full but few match, it refetches 75
  once. If almost nothing matched, it does one "broad" pass that drops the
  style code and requires every word of the description instead.
- It prints how many Apify rows it was billed for.

**Active comps**
- They come from the eBay Browse API: NEW condition, US only, up to 600
  rows.
- The price is item + shipping, for a buyer zip
  (`X-EBAY-C-ENDUSERCTX`), so calculated shipping is real. A listing with no
  shipping cost is flagged, never treated as free.
- For up to 120 listings it calls Browse `getItem`, to read
  **`estimatedSoldQuantity`** (units already sold by that listing) and
  watchers.

**Matching rules, all deterministic, no LLM**
- It finds style codes in the text (at least 3 digits and a letter, not a
  unit like `128gb`) and requires them.
- It removes filler words (`new`, `nwt`, `with`…) from the search text.
- `EXCLUDE_RE` drops lots, bundles, testers, samples, decants, replicas,
  "for parts", "box only", "gift set" and similar.
- Sizes:
  - **oz ↔ ml** for fragrance: 1.6 oz = 1.7 oz = 50 ml;
  - **women's or UK/EU sizes are not counted as men's US sizes**;
  - **multi-size variation listings** are detected and kept out of the
    weighted price.
- The broad pass rejects titles that name a different style code.

**Statistics**
- Sold median, p10, p90 and dispersion, plus an evidence tier (solid / thin /
  none, the same bars as pricing).
- **Sell-through** = sold / (sold + active), and **saturation** = more than 3
  active per sale. Both are only computed when there are at least 5 sold.
- **Units-sold-weighted price** of the active listings, each listing capped
  at 10 units.
- The reference price falls back in order: sold median, then the weighted
  price, then the 25th percentile of the active listings.

**Profit math**
- Fees = total × (final value fee + ad fee) + the fixed fee.
- Net = total − fees − label.
- Cost = Ross price × **1.07** (Florida 6% + Miami-Dade 1%).
- It also computes ROI, and **break-even** =
  (cost + label + fixed fee) / (1 − FVF − ad rate).
- It shows the profit at the reference price **and** at the lowest active
  price.

**Verdict**
- **BUY:** profit ≥ $10 and ROI ≥ 50%, with solid sold data.
- **MAYBE:** the same numbers without solid data.
- **BORDERLINE:** profit ≥ $0.
- **SKIP:** otherwise.

**Output:** text, `--html` (a clickable report), `--json`, and eBay sold and
active search links.

### `comps_app.py` (349 lines): a local web page over `comps.get_comps`

- It runs on `127.0.0.1:8765`, using only the standard library.
- It has the same inputs as the command line, plus a **live calculator**:
  - you can change the sale price, Ross price, label and shipping charged
    without searching again;
  - preset buttons for the sold median, the weighted price, the active p25,
    the lowest and the median;
  - a fee breakdown and a "where the money goes" bar;
  - the same verdict rule;
  - an "only listings that have sold" filter.
- Checkboxes: *units sold lookup*, *Women's sizes*, *Only listings that have
  sold*.

### `check_keys.py` (145 lines): are the keys there, and do they work?

- It checks that each key is present, then makes one real call per service:
  - an eBay app token (client credentials);
  - an eBay refresh-token exchange;
  - the Apify user and the sold actor;
  - Telegram `getMe`;
  - the Gemini model list;
  - Cloudinary usage.
- It prints a pass/fail list.

**Neither comps tool runs against today's code.** They import
`pipeline.price` (removed) and the old `config.py`.

---

## 2. What `pricing_v2` and the rest of the pipeline already cover

| Old feature | Today (`main`) |
|---|---|
| Sold comps from Apify | ✅ Same actor, 15 rows × up to 3 queries (brand + style code, `search_query`, brand + product), cached, backup-token failover (`pipeline/comps.py`, `pricing_v2.sold_queries`). |
| Active comps from Browse, total price, buyer zip | ✅ `ebay/browse.search_active`, plus a **GTIN (UPC) search** and an **image search**, which the old tool lacked. Unknown shipping is imputed at $9.88 and flagged. |
| "Is this the same item?" | ✅ Better: an **LLM judge** (`pricing_v2.judge`, Sonnet at low effort, escalating to Opus when unsure). It sees titles, thumbnails and our photo, and returns same_item / variant / wrong / bundle. The formula counts only same_item comps. |
| Evidence tiers, dispersion | ✅ `price_from_judged`: solid ≥ 5, thin ≥ 3; dispersion p90/p10 against 4.0. |
| Own listings left out of comps | ✅ `is_own` (the old tool didn't do this). |
| Fees, label, ROI, break-even | ⚠️ `profit.py` has them (FVF 15.5% effective, ad 4.6%, $0.40, label $7.20), but break-even **needs a receipt cost on an existing item**. **No sales tax** on cost in `main`; `SALES_TAX_RATE` 0.07 arrives with the SQL Server branch. |
| BUY / SKIP for one item before buying | ❌ Not present. `/scoreboard` judges brands and categories from **our own** sales (buy more / ok / pause / stop), not from the market. |
| Market sell-through, saturation | ❌ Only our own store's figures (`scoreboard.py`). |
| Units sold per active listing, weighted price | ❌ Not present (no `estimatedSoldQuantity`). |
| Multi-size listings, oz ↔ ml, women's / UK sizes | ❌ Not present as rules. The judge may catch some of these, but nothing checks. |
| Lots / testers / parts filter | ⚠️ Only "used" rows are dropped by rule; the rest is left to the judge. |
| Pricing from a text description, no item | ⚠️ Possible in code: `get_pricing_v2(identification, photo_paths=None, …)` stores nothing. **No command exposes it.** `/comps` only shows an item's saved pricing, and `/find` quick-add needs a photo and creates an item. |
| Key check | ⚠️ `/health` checks the eBay token and policies; nothing checks Apify, Gemini or Claude keys. |

---

## 3. New logic worth porting (best first)

1. **Pre-buy profit math with tax, break-even and a verdict.** These are pure
   functions, about 40 lines in `profit.py`, taking cost as an argument
   instead of needing an item. They use `SALES_TAX_RATE` (SQL Server branch)
   and the existing fee settings. This is the core of proposal (b).
2. **Profit at the lowest active price.** It's the "what if I have to
   undercut" number. It's cheap, because the active comps are already
   fetched.
3. **Market sell-through and saturation from same_item comps**, gated on at
   least 5 sold, as in the old tool. It's a direct demand signal before
   buying.
4. **Units sold per active listing (`estimatedSoldQuantity`) and the
   units-weighted price.**
   - Free Browse calls, one per listing. Cap it at, say, the 20 cheapest
     same_item listings.
   - Most useful when the Apify sold data is thin, because a live listing
     that has sold 30 units is evidence of demand.
5. **Cheap rule-based pre-filters before the judge**: `EXCLUDE_RE` (lots,
   testers, decants, parts, box only…), multi-size detection, oz ↔ ml, and
   women's / UK size guards. They are deterministic and testable, and they
   cut judge tokens and mistakes. They would be extra guards, not a
   replacement for the judge.
6. **Search links** (sold / active on eBay) in replies. It's trivial, and
   lets a person check by eye.
7. *Small:* extend `/health` with an Apify user check and Gemini / Claude key
   checks (from `check_keys.py`), so a dead key shows up before a job fails.

**Not worth porting:**
- The Apify widen and broad-pass logic: the multiple queries and the judge
  replace it.
- The HTML report and the local web page: Telegram is the interface now.
- The duplicated fee constants: `profit.py` already holds them.

---

## 4. Proposal (a): visibility diagnosis for INVISIBLE listings

**What happens today.** The weekly job (`reprice.run_weekly_check`, Mondays
14:00 UTC) marks a listing **INVISIBLE** when it gets fewer than 150 total
impressions a week. It marks it **STALE_UNSEEN** when it's been live for 4
weeks or more with fewer than 40 views. Neither verdict gets a suggested
price, which is correct, because a price cut can't fix a listing nobody sees.
But the digest gives no reason and no next step.

**Proposal.** For INVISIBLE and STALE_UNSEEN listings only, run a short,
read-only diagnosis after `_diagnose`. It uses only the bot's own eBay access
(the app token for Browse, the user token for Sell and Trading). Each step
stops at the first cause it finds:

| # | Question | How (existing access) | Result if it fails |
|---|---|---|---|
| 1 | Is it live? | `inventory.get_offer` → `listing.listingStatus`, quantity available > 0 (the same as `sync.sync_one`) | **NOT LIVE**: ended or out of stock; `/sync` should fix the record |
| 2 | Can buyers see it at all? | Browse `getItemByLegacyId` with the app token. A 404 or "not available" means it isn't in the buy-side index (policy block, restricted category). | **NOT INDEXED**: check Seller Hub for a policy hold |
| 3 | Does it come up for its own search? | Browse `item_summary/search` with the item's `search_query` (or its title's key words), best match, 200 results. Find our item id. Also try with `filter=sellers:{our username}` to tell "not indexed" from "ranked deep". | **NOT RANKED** (rank > 200), or rank *N*. A deep rank means a title or item-specifics problem. |
| 4 | Are we dearer than identical listings? | The stored same_item comps from its last pricing run, plus a fresh Browse GTIN search when there's a UPC. Compare our total (price + shipping) with the cheapest identical. | **OVERPRICED vs IDENTICAL** when ours is more than 10% above the cheapest. eBay ranks price-competitive listings higher. |
| 5 | Do we have a duplicate? | Our own live listings (`listings.get_active_listings`), grouped with the same keys as `stages.duplicate_warnings` (GTIN, style code, normalised title). | **DUPLICATE**: eBay's duplicate-listing policy hides all but one; end or merge it |
| 6 | Are required or recommended item specifics missing? | `ebay/taxonomy` aspects for its category vs its `listing_specifics` | **MISSING SPECIFICS**: list them (playbook §6, Stage A: fix title, aspects and category) |
| — | None of the above | | **UNEXPLAINED**: consider a small promotion bump; check the title words against the top-ranked listings |

**Output.**
- One line per listing in the weekly digest (`_send_reprice_digest`): cause,
  evidence (e.g. "rank 143 of 200 for 'coach tabby 26'"), and the next step.
- Optionally a `visibility_checks` table: listing, week, cause, rank, our
  price vs the cheapest. It would be a SQL Server migration step, after the
  cutover.

**Limits.**
- Browse "best match" isn't the personalised ranking a buyer sees on
  ebay.com, so rank is a hint, not proof.
- `top20_share` and non-search impressions have **no API path** (playbook §6
  says so); they stay manual.
- Listings under 48 hours old are skipped, because the traffic report lags.

**Cost and safety.**
- About 3–6 Browse or Sell calls per invisible listing. These are free,
  within eBay's daily limits. Cap the run at, say, 30 listings.
- Read-only: there are no eBay writes, so it works even with
  `EBAY_WRITES_ENABLED=0`.
- Tests use the fakes in `tests/tg_fakes.py` and canned API responses.

**Owner.** Person A (repricing). Size: about 1–2 days with tests.

---

## 5. Proposal (b): `/check`, a pre-buy Telegram command built on `pricing_v2`

**Usage.**
- `/check Coach Tabby 26 CH857 cost 49.99`, or a photo with the caption
  `/check 49.99`, standing in a Ross aisle.
- Optional: `ship 12` (a different label cost).

**How it works.**
1. **Identify.**
   - With a photo: the existing `identify.py`.
   - Text only: one cheap LLM call that turns the text into an
     identification dict (brand, product name, model / style code, UPC if
     given, size, `search_query`).
2. **Price.** `pricing_v2.get_pricing_v2(identification, photo_paths=…,
   item_id=None)`.
   - It reuses the sold and active comps, the GTIN and image searches, and
     the judge.
   - **Nothing is stored and no item is created.** LLM spend is still
     recorded in `llm_usage`, under a `check` stage.
3. **Profit.** The new pure functions from §3.1:
   - cost = Ross price × (1 + `SALES_TAX_RATE`);
   - fees from `profit.py` settings;
   - the label from `EBAY_SHIP_COST`, or the argument.
   - Profit and ROI at the suggested price and at the lowest same_item
     active price, plus break-even.
4. **Market signals** (§3.3–3.4): same_item sold count, sell-through and
   saturation (only with at least 5 sold), and units sold by active
   listings.
5. **Reply.**
   - One message: the verdict (**BUY / MAYBE / BORDERLINE / SKIP**), the
     suggested price and evidence tier, profit and ROI, break-even, the
     cheapest identical active listing, the counts, and sold/active eBay
     links.
   - Buttons: **Add to stock** (the existing quick-add, so the comps aren't
     fetched twice) and **Details** (the judged comps, like `/comps`).

**Rules.**
- **Verdict thresholds:** start with the old tool's (BUY = profit ≥ $10 and
  ROI ≥ 50% with solid evidence), as settings (`CHECK_MIN_PROFIT`,
  `CHECK_MIN_ROI`). As CLAUDE.md says, judge them by **sales outcomes**:
  backtest the verdicts with `scripts/backtest_pricing.py` on sold items
  before trusting them.
- **Cost guard:** a pricing run is about 3 Apify queries plus one judge
  call. Add a daily cap (`CHECK_DAILY_MAX`), and show the cost of each check
  in `/usage`.
- **Concurrency:** none needed, because nothing per-item is written.

**Owner.** Person A (pricing). It depends on `SALES_TAX_RATE` (SQL Server
branch), so build it after the cutover. Size: about 1–2 days with tests.

---

## 6. API keys hard-coded in the old files

**None of the three tools has a hard-coded key.**
- `comps.py` reads `EBAY_CLIENT_ID` and `EBAY_CLIENT_SECRET` from `.env`
  (lines 86–87), and its Apify token comes from the old `pipeline/price.py`
  environment settings.
- `check_keys.py` reads every key from the environment (lines 25–43).

The whole folder was scanned: `.py`, `.html`, `.md` and `.json`, the archive
aside. The only hit:
- `config.py` line 131: `EBAY_MERCHANT_LOCATION_KEY`. This is the name of
  the eBay inventory location, an identifier, **not a secret**.

Two notes:
- **`check_keys.py` line 49** (`mask`) prints the first 4 and last 3
  characters of every secret. Don't paste its output anywhere.
- **`ebay-handoff-20260831.7z`** (174 MB) in that folder was **not opened**.
  A handoff archive may contain a `.env`. Check it or delete it, and keep it
  out of any repo or shared folder.

## 7. Does this `comps_app.py` have "Exact matches only"?

**No, so this is the older version.**
- Its only checkboxes are *Look up units sold per listing*, *Women's sizes*
  and *Only listings that have sold* (lines 187–188, 313).
- `comps.py` has no exact-only switch either. Its `get_comps` always falls
  back to the "broad" pass (style code dropped) when exact matches are few
  (lines 524–529 sold, 535–539 active).

The newer version with "Exact matches only" was not found here. In today's
code the same idea already holds: the pricing formula only counts comps the
judge calls `same_item`.

## 8. Later: Best Offer set automatically when publishing (Person A's area)

Added by Person B, 2026-11-02. Not built; for Person A to decide.

`/check` now shows Best Offer prices next to the fixed price, from the
evidence-ladder estimate (branch `check-command`, settings in `config.py`):

| Setting | Default | Meaning |
|---|---|---|
| `CHECK_BO_LIST` | 1.25 | list price = estimate × 1.25 |
| `CHECK_BO_ACCEPT` | 1.05 | auto-accept at estimate × 1.05 |
| `CHECK_BO_DECLINE` | 0.90 | auto-decline below estimate × 0.90 |

All three are rounded to .99. Example: estimate $39.99 → "list $49.99 BO
(auto-accept $41.99, decline < $35.99) or $39.99 fixed".

**The idea:** when an item is published, turn Best Offer on with these
thresholds (eBay's `bestOfferTerms`: `autoAcceptPrice`, `autoDeclinePrice`)
instead of a plain fixed price.

**Questions for Person A:**
- Which estimate is the base: the `/check` ladder, or the listing price
  `pricing_v2` sets at intake?
- How does it fit with repricing and offers to watchers (`pipeline/reprice.py`,
  `pipeline/offers.py`)? A price cut should move the thresholds too.
- Judge it by sales outcomes (`scripts/backtest_pricing.py`), as for any
  pricing change.
