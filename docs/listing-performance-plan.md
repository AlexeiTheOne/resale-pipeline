# Listing performance plan (2026-11-09)

Sources: eBay's *Listing quality report* for premium_designer_outlet, generated
11/06/2026, covering the last 31 days and "Buy it now" listings only. 111 live
listings in the 10 categories it covers. Joined with the live DB (ResaleDB,
read-only). The analysis scripts read the report; nothing on eBay or in the DB
was changed.

## 1. What the report says

Women's Bags & Handbags (72 of our listings):

| | Us | Top 10% | Bottom 10% |
|---|---|---|---|
| Impressions per listing per day | 163 (rank **731** of 444,519) | 499 | 2 |
| Click-through | **0.38%** (rank 12,686) | 3.89% | 0.13% |
| Sales conversion | **1.04%** (rank 3,014) | 32.7% | 0.57% |
| Sales (GMV) rank | 1,397 of 444,519 | | |

- **Visibility is fine; clicks are the problem.** Our impressions per listing
  rank 731st. Click-through is a tenth of the top 10%.
- **Content is at or above the top sellers:** 9.2 photos (top: 7.3), 11.8 title
  keywords (top: 10.1), 13 recommended specifics filled (top: 8). Best Offer is
  on for all 111 listings.
- **Free shipping** is eBay's top recommendation in 5 of 10 categories: Women's
  Bags (81% of the top-impression listings offer it), Pillow Shams (70%), Watch
  Bands (78%), Men's Bags (79%), Sheets (85%). We offer it on 1 of 111.
- **Ad rate** in 4 categories: Pillowcases 7%, Wristwatch Bands 8%,
  Wristwatches 9%, Men's Fragrances 9%. We're at 4% on 109 listings. (eBay's
  "why" text for these says "% of their EANs filled out", which is a bug in
  their wording.)
- **Athletic Shoes:** fill at least 8 recommended specifics.
- **MPN** is "Not provided" on 60 of 72 bag listings (section 3).

## 2. Order of work

1. `/revise`: PR #14, merged once `OWNER_USER_IDS` is in the live `.env`. Then
   revise d7432192 (approved text, Type = Handbag, no hangtag sentence).
2. MPN fix: new listings in PR #16. The 53 live listings go through `/revise`
   afterwards.
3. `/offers` upgrade. Second offers are on hold (section 4).
4. Free-shipping test: approved (section 5).
5. Competitor search, then `/diagnose` with report ingestion (section 7).

Done: PR #13 (shipping charged $9.99) merged 2026-11-09.

The ad-rate list (section 6) is for the owner to decide on; nothing changes
until then.

## 3. MPN fix

**Cause (checked in the live DB):** the MPN isn't failing to reach eBay; we
never had it. All 60 bags with "Not provided" have **no MPN in our DB**. The
style code is under the **Model** specific instead:

- 53 of 60: Model holds the style code (R34D1B53, 87F2LEVB9U, XW09620...).
- 4: Model holds a name ("The J Marc", "Ava Tote", "LBNDELITA", "LBPERCY").
- 3: nothing at all.

The draft prompt (`pipeline/draft.py`) asks for `"Model": "string or null"` and
`"MPN": "manufacturer part number from Google Search or null"`. The AI files
the tag's style code as Model and leaves MPN null. `_build_product` then sends
`mpn = "Does not apply"`, which eBay reports as "Not provided".

**Work:**
- New listings: when MPN is empty and Model (or `identification.style_code`)
  looks like a style code (letters and digits, no spaces), use it as MPN. Fix
  the draft prompt so the tag's style code goes into MPN as well as Model.
- Existing 53 listings: set MPN from Model through `/revise`'s path (the same
  inventory update, UPC untouched). This is the first bulk case. Per the
  "single listing first" rule: do 2–3 one at a time with `/revise`, check them
  on eBay, then decide whether a bulk mode (one preview for all 53, one
  confirmation) is wanted.
- The 7 without a code: list them for a person to look up the tag.

**Can't verify from dev:** whether eBay accepts every Model value as MPN.
eBay can reject an MPN that conflicts with its catalog entry for the same UPC.
The first single revises will show it.

## 4. `/offers` upgrade (offers to watchers)

**Already built:** `/offers` (dry run, then `/offers apply`), the weekly digest's
"Send offers" button, `ebay/negotiation.py` (eligible listings, send offer).
Gates: at least 1 watcher, at least 14 days live, at least $15 net after the
offer, 7-day cooldown per listing. Discount: flat $5, or $10 at $40 and up.

**The token has the scope:** 46 offers were sent between Nov 2 and today (the
latest batch at 10% off), which needs `sell.negotiation`.

**Changes:**
- Owner-only (`OWNER_USER_IDS`, from PR #14).
- Discount: default **10%**, never below the profit floor (break-even plus the
  $15 minimum net). Shown per listing.
- One listing per confirmation: `/offers` lists listings with watchers (watchers,
  price, last offer sent, suggested offer, floor). Each has a Send button that
  shows the exact offer before sending. Nothing goes out without the tap. The
  `apply` text argument goes away.

**First targets:** all three already got about 10% on Nov 2. None sold, and
the cooldown ended today.

| Listing | Watchers | Sales (31d) | Price | Offered Nov 2 |
|---|---|---|---|---|
| Patricia Nash Ava Rose tote (331dc4a9) | 23 | 1 | $149.99 | $134.99 |
| MK Apple Watch band, silver pavé (00c8d805) | 15 | 0 | $100.00 | $90.00 |
| MK Access band, rose gold pavé (bff24df0) | 13 | 0 | $139.00 | $125.10 |

**Second offers: on hold (owner, 2026-11-09).** Rule: before any second offer,
pull 90-day sold comps. If they're below our offer price, the fix is a price
cut, not another offer.

Pulled 2026-11-09 with the bot's Apify sold search (4 searches, about $0.07):

| Listing | Our price (+$9.99) | Nov 2 offer | Sold, last 90 days (item price) | Verdict |
|---|---|---|---|---|
| Patricia Nash Ava Rose tote | $149.99 ($159.98) | $134.99 | **1 exact**, new: $86.00 (+$15.75), Oct 11. Nearest other new tooled Nash tote: $85.00. The rest are used, $18–70. | Below offer → **price cut** |
| MK band silver pavé MKS8006 | $100.00 ($109.99) | $90.00 | **1 exact**, new: $77.99 (+$7.00), Oct 21. New MK Apple Watch bands of any model: 6 sales, $67.50–$94.99, median $78.00. | Below offer → **price cut** |
| MK Access rose gold pavé MKS8028SET | $139.00 ($148.99) | $125.10 | **No sale** of this set in two searches. Nearest: MK Access silver "bling" band, $80.00 (new, other). | Too little evidence; what there is sits below the offer |

All three point to a price cut, not an offer. The evidence is one exact sale
for two of them and none for the third, so the size of each cut is the owner's
call. The house rule is small cuts plus monitoring. Nothing has been changed.

Also worth a look: the Kate Spade
Apple Watch band (f5ab5777) has 15 watchers and 0 sales and has never been
offered. eBay may not have listed it as eligible; only the live bot can tell.

## 5. Free-shipping test

**Built (owner go, 2026-11-12): `/shiptest`, `pipeline/ship_test.py`.**
- **Groups, redrawn 2026-11-12:**
  - 0e48e50b sold on 8 Nov, and its control slot went to f0665406.
  - d60d9e01 was swapped for 53b36c34 because the owner cut its price that day.
  - Both are the next women's bags by impressions. The test group is unchanged.
- **Start:** `/shiptest` shows the preview, and its button starts the test:
  - The bot creates the policy "Free shipping (USPS Ground Advantage)", a copy of the current one with the first domestic service free.
  - Each test listing then gets the price +$9, the policy and the new Best Offer thresholds in one eBay update. The DB changes only after eBay accepts.
  - Both groups get `ebay.ship_test` (start, end, price before).
- **Floors:** `ebay.free_shipping` makes `profit.shipping` charge $0 and use the $8.17 label, so the break-even and Best Offer floors follow.
- **Freeze (step 6):**
  - The weekly digest records stats but suggests nothing for the 36 listings, and its header counts them.
  - `/offers` and the watcher-offer plan skip them.
  - `/setprice` refuses them without `force`.
- **Measuring (step 7):** `/shiptest` reads eBay's traffic report for the test days and for the same number of days just before the start, per group. This replaces the daily snapshot: the Analytics API answers any date range, so a snapshot table would only copy it.
- **Not built yet:** ending the test, which means putting test listings back on $9.99 shipping or keeping free shipping. That decision comes after the 21 days.

**Approved (owner, 2026-11-09)** as drafted: 18 vs 18, 21 days, judged by CTR,
sales as a direction only. **The Patricia Nash Ava Rose tote (331dc4a9) is in
neither group.** It had a sale in the last 31 days, so the 0-sales rule
already leaves it out. The re-draw on the start day also excludes it by id.

**$9.88:** `EBAY_SHIP_CHARGED` was an old (2026-08-16) measurement of what
buyers pay for shipping. Buyers paid $9.99 on 82 of 87 orders. PR #13 sets it
to $9.99. It isn't net of anything.

**Work:**
1. **Shipping policy:** a second fulfillment policy, free USPS Ground Advantage.
   Either create it in Seller Hub (2 minutes) or through the Account API from
   the live bot (`sell.account` is among the scopes the bot requests). Its id
   goes in a new setting, `EBAY_FREE_SHIPPING_POLICY_ID`.
2. **Profit floor per item:** today `profit.py` uses the global
   `EBAY_SHIP_CHARGED` and `EBAY_SHIP_COST` for every item. Free-shipping
   items need shipping charged = $0. Their floor rises by about $8.44
   ($9.99 × (1 − 15.5% FVF)), and the +$9 price covers it. The label cost for
   bags is measured at $8.17 median, against the $7.20 the floor assumes for
   everything; use $8.17 for bags.
3. **`/revise` gets price and shipping policy.** Both are on the offer, so one
   offer update changes them together. That keeps the preview and confirmation
   and goes through eBay first, then the DB.
4. **Groups** (draft, from the report): Women's Bags with 0 sales in 31 days,
   most impressions first. Excludes listings under 21 days old, d7432192 (being
   revised), and anything sold or adopted. Neighbours by impressions alternate
   between test and control:

   | | Test (free, +$9) | Control ($9.99) |
   |---|---|---|
   | Listings | 18 | 18 |
   | Avg impressions/day | 130 | 120 |
   | Avg CTR | 0.57% | 0.50% |
   | Avg price | $53.05 | $60.44 |
   | Watchers | 59 | 45 |

   Test: a8041fe2, fc08e55b, 3347c963, 4a66bcb4, e6dc7974, f027cb9c,
   57a73dda, 5fdc486f, b88e085d, 71226811, 6d35cfd0, 61e7c55b, fe5be314,
   182a5ab3, e5756e70, 11b3217a, 2aa10063, f93268fd.
   Control: 991936e3, c57cb4cc, a84043d6, 2f57c0f4, 0e48e50b, 5af2cebd,
   d60d9e01, 6ec3c44a, db2380ba, f296137d, 4e8cefb4, a129b1b5, 0895b282,
   434f5ea0, a06a637c, 179def75, 2da4f006, f637882c.

   Re-draw on the day it starts, after a `/sync` on the new code, so any Seller
   Hub relist is flagged and dropped. **0 items are flagged today, because no
   /sync has run since PR #11 deployed.**
5. **Price:** +$9.00 (the $8.17 median label, rounded up), keeping the .99
   endings: $44.99 → $53.99.
6. **During the 21 days:** no offers, price changes or revisions on either
   group, or the comparison is confounded. The weekly digest and `/offers`
   skip these 36 listings until the test ends.
7. **Measure** in this order: impressions, then CTR, then sales. Take a daily
   snapshot of impressions and views per listing (Analytics API) for all 36.
   Compute CTR from views ÷ impressions; the playbook (§6) found eBay's own
   CTR field unreliable.

**What 21 days can show:** about 130 impressions/day × 18 listings × 21 days
is about 49,000 impressions per group. At 0.5% CTR that's about 250 clicks per
group: enough to see CTR move from 0.5% to 0.6% or more. At about 1% of views
converting, expect 2–3 sales per group. **The sales comparison won't be
conclusive at that size;** treat it as a direction, and CTR as the result.

## 6. Ad rate (owner to decide; nothing changes)

eBay's recommendation is per category. The report doesn't give a per-listing
suggested rate.

| Category (eBay suggests) | Listing | Now | Price | Imp/day | CTR | Sales 31d | Watchers |
|---|---|---|---|---|---|---|---|
| Men's Fragrances (9%) | True Religion Cologne 3.4 oz (117288288230) | 4% | $44.99 | 49 | 0.26% | 0 | 1 |
| | True Religion for Men 3.4 oz (117363556737) | 4% | $20.00 | 42 | 0.45% | 0 | 1 |
| Wristwatches (9%) | Versace VEDFA0224 Greca Goddess (117316906380) | 4% | $549.99 | 29 | 0.54% | 0 | 1 |
| | MK Oversized Slim Runway 42mm (117363536275) | 4% | $129.99 | 69 | 0.46% | 0 | 2 |
| Wristwatch Bands (8%) | MK band silver pavé (117361213532) | 4% | $100.00 | 1,019 | 0.25% | 0 | 15 |
| | MK Access rose gold pavé (117363652355) | 4% | $139.00 | 1,087 | 0.15% | 0 | 13 |
| | MK Access red-coated steel (117474994246) | 4% | $90.00 | 1,630 | 0.07% | 0 | 4 |
| Pillowcases (7%) | RL Estella Trellis King (117306549500) | 4% | $39.99 | 63 | 0.15% | 0 | 0 |
| | LRL Millerton Plaid Flannel King (117311888412) | 4% | $29.99 | 55 | 0.29% | 0 | 0 |
| | LRL Estella Trellis Standard (117347886751) | 4% | $37.99 | 145 | 0.71% | 1 | 3 |

Worth weighing: the three MK bands already get 1,000–1,600 impressions a day
with CTR from 0.07% to 0.25%. A higher rate buys more impressions, which
aren't their problem. 117347886751 is a Seller Hub relist (155333d1), so check
that any rate change lands on the live listing.

## 7. Competitor search and `/diagnose` (owner spec, 2026-11-09)

Built after the MPN fix. Thresholds live in `config.py`.

### 7.1 Shared module: competitor search

Used by `/check`, new listings and `/diagnose`.

- **Match:** UPC first, then MPN, then brand + title keywords. Each result
  records which method matched.
- **Active listings** (Browse API): price, shipping, total, title, category,
  item specifics, photo count, returns, Best Offer. Browse *search* results
  carry price, shipping, title, category and buying options (Best Offer). They
  don't carry specifics, photo count or return terms; those need one
  `getItem` call per listing. So only the best matches get the extra call
  (`COMPETITOR_DETAIL_MAX`, e.g. 10). `/diagnose all` on 123 listings is then
  about 1,300 calls, inside Browse's 5,000 a day.
- **Sold listings** (Apify; not Marketplace Insights): sold price, date, title.
- **Cache:** a DB table with a timestamp; no re-search within 24 hours.
  - New table, so a `migrations.py` step on both backends: **Person B's area.**
  - **Agreed with Person B (2026-11-09): the comps cache shares the
    `check_evidence` table** planned for `/check`
    (`docs/check-command-design.md` §0000), rather than a second table.
- **Apify budget:** on 2026-11-09 the primary account was at $0.00 and the
  backup at about $0.73. A sold search costs about $0.02. So: the 24-hour
  cache, a daily spend cap in config, and `/diagnose all` searches sold
  listings only for listings whose verdict needs them (PRICE, WATCHERS).

### 7.2 Verdicts

One primary verdict per listing. Each comes with a one-line reason and one
concrete fix.

| Verdict | When | Fix |
|---|---|---|
| NOT ENOUGH DATA | expected views < 3 | Wait; check again later. Says when. |
| WATCHERS, NO SALES, OFFER ALREADY SENT | watchers, offer sent, no sale | Price is above the market: suggest a price from sold comps. |
| WATCHERS, NO SALES | watchers, no offer yet, no sale | Send an offer (`/offers`), but only if sold comps are at or above the offer price. Otherwise this becomes PRICE. |
| PRICE | views or watchers, no sales | Compare with sold comps; suggest a price. |
| SEARCH CARD | CTR below the category benchmark | Total price vs competitors; flag the thumbnail. |
| VISIBILITY | impressions below the category benchmark | What competitors have that we lack (specifics, photos, returns, free shipping). |
| CATEGORY FIX | eBay's report recommendation for the category | Apply it (free shipping, ad rate, specifics). |

Decisions this spec leaves open, decided here (change in config if wrong):

- **Order when several verdicts apply:** top to bottom as in the table. Data
  first, then the money signals closest to a sale, then clicks, then
  impressions. CATEGORY FIX comes last because it's account-wide; it's shown
  as a second line when another verdict is primary.
- **Expected views** = the listing's impressions in the window × the
  category's CTR for our store (from the report; 0.38% for bags). Below
  `DIAGNOSE_MIN_EXPECTED_VIEWS` (3), click-through can't be judged yet.
- **Too few sold comps:** fewer than 3 in 90 days means PRICE can't suggest a
  number. It says so and shows what there is, as with MKS8028SET above.
- **Benchmarks:** the report's top and bottom 10% for the listing's category.
  For a category the report doesn't cover, fall back to our store average,
  labelled as such. Compare the report's CTR only with the report's
  benchmarks; its definition may differ from the Analytics API CTR.

### 7.3 `/diagnose`

- Owner-only, read-only.
- `/diagnose <listing>`, or `/diagnose all`: ranked worst first, by verdict
  order, then by impressions (the most exposure wasted).
- Fixes go through `/revise` (and `/offers`); `/diagnose` changes nothing.

### 7.4 Report ingestion

- Send the eBay listing quality report (xlsx) to the bot. It parses every
  category sheet: per listing (watchers, item age, sales 31d, daily
  impressions, CTR, conversion, recommendations) and per category (ours, top
  10%, bottom 10%). Stored with the report date.
- A new table for the reports: **approved by Person B (2026-11-09).** Built
  as two (migration step 7: `quality_report_sections` and
  `quality_report_listings`), because a category row and a listing row have
  different columns. Same report sent again: its rows are replaced.
- **Built (2026-11-10):** the owner sends the xlsx to the bot
  (`bot/report_cmds.py`, `pipeline/quality_report.py`). `/diagnose` uses the
  newest report while it is at most `QUALITY_REPORT_MAX_AGE_DAYS` (30) old:
  - expected views use the report's CTR for the listing's category;
  - SEARCH CARD and VISIBILITY compare the listing's report row with the
    report's figures: under half of our category figure, or under eBay's
    bottom 10%. Analytics numbers are never compared with report numbers;
  - a listing newer than the report gets its category from the listings that
    are in it (same eBay category id), and store averages for clicks;
  - **CATEGORY FIX:** eBay's recommendation for the category, when it applies
    to the listing (no free shipping; ad rate under eBay's; fewer recommended
    specifics than eBay's minimum). Primary only when nothing else applies;
    otherwise an extra line. `/diagnose all` groups them, one line per
    category with a count, instead of repeating them on every listing.
  - Checked against the 11/06 report and live ResaleDB (read-only): 109 of
    111 report listings match live items (2 sold since), 4 newer ones match by
    category, 9 are in categories the report doesn't cover.
- **No eBay API gives the report** (checked 2026-11-10): the category
  benchmarks (rank, top/bottom 10%) and the free-shipping advice exist only in
  the xlsx. The bot asks for a fresh one about once a month (the daily sync
  sends it when the newest report is at least `QUALITY_REPORT_REMIND_DAYS`,
  28, old, at most once per 28 days).
- **Between reports (owner-approved 2026-11-10), from eBay's APIs:**
  - **Conversion:** the traffic report's `TRANSACTION` / views, shown in
    `/diagnose <id>` with the store average. Not a verdict: about one sale per
    listing per month is too few to judge. If eBay refuses the metric, the
    bot stops asking for it, and the weekly check is unaffected.
  - **Recommended specifics (Taxonomy):** required + recommended aspects for
    the listing's category against what it has. Fewer than
    `DIAGNOSE_MIN_RECOMMENDED_SPECIFICS` (8, eBay's own minimum in the report)
    is a CATEGORY FIX naming the missing ones. Every listing; the report's own
    specifics line wins where it has one. On 118 live listings: 7 flagged.
  - **Suggested ad rate (Recommendation API, `findListingRecommendations`):**
    only for listings the report doesn't cover (a report section without an
    ad-rate line means eBay finds ours fine). Shown when it is at least
    `DIAGNOSE_AD_RATE_GAP_PCT` (1 point) above the listing's own rate, read
    from our Promoted Listings campaign (4% default if that read fails). eBay
    calls it a guide, not the best rate, so it stays an owner decision (§6).

### 7.5 New listings

- Run competitor search before the draft. Give Gemini the competitors'
  keywords, category and specifics.
- **Never copy competitor titles or descriptions verbatim**, enforced in code
  as well as the prompt. The draft is refused and redone if its title matches
  a competitor's, or if its description repeats a run of 8+ words from one.
- Show our price against sold comps in the confirmation.

### 7.6 First test listings

| Listing | Expected |
|---|---|
| 202eb699 Pro Standard Liga MX Club América hoodie (listed Nov 5) | NOT ENOUGH DATA: too new |
| d7432192 Ed Hardy shoulder bag (after its `/revise`) | to see |
| 00c8d805 MK band silver pavé MKS8006 | WATCHERS, NO SALES, OFFER ALREADY SENT, with sold comps around $78 |

## 8. Can't verify from this dev session

The dev copy has no eBay seller token and eBay writes are off. So these are
unchecked:

- `/revise` against real eBay. The first preview of d7432192 is the first real
  run.
- Per-listing suggested ad rates (Marketing API, needs the seller token). Only
  the report's category-level figures are above.
- Shipping policy names and ids. That 119 of 123 listings share one $9.99
  policy is inferred from identical buyer-facing shipping.
- Which listings are Seller Hub relists: unknown until a /sync runs on PR #11's
  code.
- Whether eBay lets us send a second offer to the same watchers, and whether
  the Nov 2 watchers are still watching.
- Why the Kate Spade band was never offered (eBay's eligibility at the time).
- Whether eBay accepts each Model code as MPN.
- That the live token was actually granted `sell.account` for creating a
  policy. It's requested, and policy reads work (`/health`), but a create
  hasn't been tried.

- Our app's Browse API daily quota. 7.1 assumes eBay's default of 5,000 calls
  a day; check it on the eBay developer account before `/diagnose all` runs.

## 9. Open items

- Price cuts for the three offer targets (section 4): the owner decides,
  from the profit tables shown 2026-11-09.
- The buyer-words / style-code title rule (section 10) ships with the
  competitor search.

## 10. Listing quality standard for new listings (owner, 2026-11-09)

Code checks (`pipeline/quality.py`) after the draft and after every typed
correction. Their issues join the AI listing check's "Check" block on the
review card. They warn and never block, like the duplicate warning. Where a
model can fix the problem, the draft goes back to the copywriter once
automatically (`stages._meet_standard`).

| Rule | How |
|---|---|
| One bag type (Women's and Men's Bags) | Title, Style, Type and description name the same type. Bag, purse and handbag are fine alongside it. **A crossbody strap on a satchel or tote is a feature, not a second type** (owner); "crossbody" is the type only for crossbody-only bags. Type follows eBay's short list: Handbag, Satchel, Backpack, Clutch. |
| Description | One paragraph of 4–6 sentences, with the dimensions when they're known, then the store's SHIPPING and RETURNS sections. Replaces the SPECIFICATIONS / FEATURES bullet layout. |
| Printed items | Color and **Exterior Color = Multicolor** (owner), applied to the specifics automatically. The base colour stays in the title. |
| Similar titles | Warn when a title shares 60% or more of its words with one of our live titles. On the 123 live titles that's 7 pairs, all near-twins. |
| Impressions at 3 days | A daily job (15:00 UTC) checks listings 3–10 days old once each and alerts the owners on zero total impressions. eBay's traffic data lags 1–2 days, so day 3 covers the first day or two. |
| Buyer search words first; style code only if searched | The title leads with buyer words and puts a style code at the end. Whether buyers search a code needs the competitor search (≥2 sold comps or ≥3 active competitors using it), so that part ships with section 7.1. |

New listings only. d7432192 gets Exterior Color = Multicolor through the
owner's own `/revise`.
