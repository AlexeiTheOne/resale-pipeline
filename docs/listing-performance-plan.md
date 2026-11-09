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

1. `/revise`: PR #14. Then revise d7432192 (approved text, Type = Handbag).
2. MPN fix.
3. `/offers` upgrade.
4. Free-shipping test.
5. `/diagnose` with report ingestion.

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

A second 10% offer repeats one that didn't convert. **Owner's call:** resend at
10%, or go to 15% where the floor allows. Also worth a look: the Kate Spade
Apple Watch band (f5ab5777) has 15 watchers and 0 sales and has never been
offered. eBay may not have listed it as eligible; only the live bot can tell.

## 5. Free-shipping test

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

## 7. `/diagnose` with report ingestion

**Not found:** no `/diagnose` command, no plan for it, and no competitor-search
module exists in any branch or doc. The nearest existing piece is the weekly
price check's verdicts (`reprice._diagnose`) and playbook §6. **The owner or
Person A should send the plan** these were built against, so this doesn't
guess at it.

What this report adds, whatever that plan is:
- **Ingest:** send the xlsx to the bot. It parses every category sheet: per
  listing (watchers, item age, sales 31d, daily impressions, CTR, conversion,
  the recommendations) and per category (our numbers, top 10%, bottom 10%).
  Stored with the report date. This is a new table, so it needs a
  `migrations.py` step: **Person B's area (DB)**, to agree with them.
- **Benchmarks:** compare each listing with its category's top and bottom 10%
  from the same report, not just our store average. Use the report's CTR
  against the report's benchmarks only. Its definition may differ from the
  Analytics API CTR the bot computes.
- **Verdicts:**
  - `WATCHERS, NO SALES` → send an offer (through `/offers`).
  - `CATEGORY FIX` → what eBay recommends for that category (free shipping,
    ad rate, specifics), linked to the action that applies it.

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

## 9. Open items

- The owner's listing quality list (to be pasted), for the quality standard
  for new listings, minus the UPC check.
- The `/diagnose` and competitor-search plan (section 7).
- Second offer to the first targets: 10% again or 15% (section 4).
