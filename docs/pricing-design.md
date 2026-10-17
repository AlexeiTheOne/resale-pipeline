# Pricing and in-store check: design (draft, 17 Oct 2026)

Nothing here is built yet. This is the plan to agree on before writing code.

## Why change

Today one Gemini grounded search decides what the item is, its output becomes a
text query, and an Apify scrape of eBay prices it. Each step inherits the last
one's mistakes. From `data/ross.db` (Oct 2026):

- On 101 items with a machine price, the listed price matched it (within 2%)
  on only 14. The owner raised 54 and lowered 33. Every "none" price was typed.
- Raised prices still sold: 14 of 24 sold items with a machine price had been
  raised, typically selling within about 2 weeks. The machine prices low.
- In the median item only ~30% of sold comps share half the product name.
  The only relevance check is the brand appearing in the comp title
  (`pipeline/price.py:302-313`). 45 items had comps spanning more than 3x.
- The formula undercuts the sold median by 15%, then caps under the cheapest
  active listing, comparing sold totals (likely with shipping) against active
  item prices (`pipeline/price.py:267-272`, `:324`, `:339`).
- The first query rung that returns anything wins; rungs are never pooled
  (`pipeline/price.py:414-419`).

## Shape: a hub, not a chain

### Stage 1: identity (all in parallel)

| Signal | Gives | Cost |
|---|---|---|
| Ross tag: barcode | UPC | free, already read |
| Gemini grounded research (kept) | brand, model, style code | ~$5 credit left |
| eBay Browse API `search_by_image` | similar eBay listings from the photo | free, needs testing |

Output: one identity record (brand, model/style code, UPC, size, colour), each
field tagged with which sources agree. Conflicts go to a human, never a guess.

Paid reverse image search (Google Lens via a third-party API) is deferred until
its price is researched.

### Stage 2: price evidence (in parallel)

- eBay sold comps (existing Apify actor), querying style code AND product name,
  results pooled and de-duplicated.
- eBay active listings via the Browse API (replaces the Apify active actor,
  whose `condition` field holds subtitles, not conditions).
- Retail ceiling: the brand's retail price from the product pages Gemini's
  research finds (`retail_price`, already returned). Weak evidence: a ceiling
  only, never the price itself. NOT the tag's `original_price`: that is Ross's
  own pre-markdown price (e.g. $30 reduced to $15), it says nothing about eBay
  value. Only `reduced_price` (cost) comes from the tag.
- Own history: past sales and the ~100 prices the owner set by hand, passed as
  calibration examples.

All prices normalised to one basis (item price, shipping separate) before any
comparison.

### Stage 3: one Claude decision (Sonnet 5.5)

Input: identity record + every comp (title, price, sold/active, thumbnail).
Output: per comp `same_item | variant | wrong | bundle`; a price with reasoning;
confidence; which sources disagreed. Price from same-item comps only.

Low confidence escalates the same call to Opus 5.5. No web search on the Claude
side: Gemini does the research, Claude judges.

Undercut drops from 15% to ~5% (to be set by the backtest below).

## In-store check (`/check`)

1. Partner sends 2-3 rough photos + the Ross tag (for UPC and cost).
2. Bot runs stages 1-3, skips the draft, replies with: likely sell price,
   cost from the tag, fees + shipping, expected profit, how fast same-item comps
   sold, confidence. Target under 90 seconds; a "still searching" message if
   slower.
3. "Bought" button: drafts and publishes from those photos (the check result is
   reused, no second charge).
4. Daily reminder lists items still on store photos, oldest and most-viewed
   first. New photos go through the existing photo-safe edit path.

Stock photos: prefer eBay catalog images (`/stockphoto`). Retailer images found
by image search carry a takedown risk and are not used without a decision.

## Cost (to verify with real token logs)

- Claude decision on Sonnet 5.5: ~15k in (mostly thumbnails) + ~1.5k out,
  about $0.05 per check.
- Listing draft, title fitter and final check (Sonnet 5.5) per published item:
  to be measured.
- Gemini: draws on the remaining ~$5 credit. When it runs out, research moves
  to Claude web search ($10 per 1,000 searches + tokens), which must be costed
  before that happens.
- Volume ceiling assumed: 20-25 checks per week, about 100 a month, so about
  $5 a month for checks. Budget is $100 a month of Anthropic credit.

Every Claude call logs its token usage; a monthly guard warns at $80 and drops
to cheaper settings after $95.

## Validate before building further

Backtest stages 1-3 on items already in the DB: the 37 sold items and the ~100
the owner priced by hand. Compare the new price to the price actually chosen
(and the sold price). Build `/check` only once the new pricing agrees with the
owner on most of them. Report the agreement rate and the worst misses.

## Build order

1. Commit the uncommitted work.
2. Claude client with token logging and the monthly guard.
3. Browse API (active listings + `search_by_image` test).
4. Identity record + pooled comps + Claude decision + formula fix.
5. Backtest. Stop and review.
6. Title fitter, refuse-to-truncate at publish, re-fit the 7 cut live titles.
7. Final check before review (Sonnet 5.5).
8. `/check` + Bought button + photo reminders.

## Research findings (17 Oct 2026, from vendor docs; verify at build time)

- eBay `search_by_image`: open to any app token (client credentials, base
  scope), returns active listings only, no text query alongside. Forum reports
  say it returns similar items rather than exact matches: test before relying.
- eBay Browse `search`: about 5,000 calls/day by default, returns shipping
  cost, filters by `conditionIds`. No sanctioned sold-price API for us
  (Marketplace Insights is still limited release), so Apify stays for sold comps.
- Gemini: grounding on the paid tier is free within a monthly allowance far
  above our volume, so $5 covers tokens only: roughly 9 months on 3.6/3.8
  Flash (about 5 after the 1 Jan 2027 price rise), ~16 months on 2.5 Flash.
  `gemini-3.5-flash` (our `GEMINI_MODEL` default) is no longer on the pricing
  page; check whether it still serves.
- Google Lens via Apify actors costs about $0.002-0.005 per image (some offer a
  free monthly allowance), so ~100 checks/month is well under $1. Reuses the
  existing Apify account. SerpApi's free plan is 250 searches/month.

## Open questions

- Is 20-25 per week checks, or trips? At 15 items per trip, 25 trips a week
  would be ~1,500 checks a month, about $75 on Sonnet alone.
- Does `search_by_image` return usable matches for this stock (bags, watch
  bands, bedding)? Test in step 3.
- Gemini cost per grounded call, so we know how long $5 lasts.
