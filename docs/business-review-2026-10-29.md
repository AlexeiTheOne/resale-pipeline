# Business review — 29 Oct 2026

Five parallel analyses of everything since the bot started (late June 2026):
sales speed, margins, pricing, traffic and operations. The money figures come
from eBay's own orders and finances APIs, not the DB, because the DB leaves out
ad fees and refunds. The detailed reports, with method and tables, are in the
session scratchpad: `1_velocity.md`, `2_margins.md`, `3_pricing.md`,
`4_traffic.md` and `5_operations.md`.

## The short version

**Each sale is very profitable. The money is stuck in the wrong stock.**

- Each sale makes good money: about 229% return on Ross cost, with a median of $27 profit per sale.
- But only about 30% of the units bought have sold. About $3.2k of Ross cost sits in live listings, three-quarters of it 60+ days old.
- Cash in is only about $380 ahead of cash out.

**What decides results is what gets bought and how steadily it gets listed.**
Titles, photos and specifics are already about as good as they'll get. Price
cuts haven't moved stock.

## 1. Money

| | |
|---|---|
| Orders (May 5 – Oct 28) | 86 |
| Item revenue | $6,957 |
| Shipping collected | $865 |
| eBay fees (15.7% of item+ship) | −$1,230 |
| Promoted Listings ad fees | −$306 |
| Shipping labels | −$681 |
| Refunds | −$302 |
| **Net proceeds** | **$5,304** |
| Profit on sold units with known Ross cost | $3,411 on $1,489 cost (**229% ROI**) |
| Excluding the Coach Cary watch (3 × $499.99, $1,050 profit) | 174% ROI |

- **Profit by month:**

  | Jul | Aug | Sep | Oct |
  |---|---|---|---|
  | $320 | $957 | $1,360 | $775 |

  September without the Coach watch was about $310.
- **Losses:** only 3 of 77 sales lost money, and all 3 were refunds (Patricia Nash ×2, Versace watch).
- **Best Offer:** discounts gave away $91, which is 1.4% of revenue. Not a problem.
- **Shipping:** +$184 overall, so not a leak. But 22 orders lost money on postage, mostly watches and heavy duvets, and another $13 went on "underpaid postage" charges.
- **Ads:** cost $306, which is 9% of profit. They were charged on 75 of 86 orders. Every listing is promoted at 4%, so there's no unpromoted control group to tell whether the ads actually help.

## 2. Capital and dead stock (the main problem)

- **Bought vs sold:** about $4.7k spent at Ross on about 257–275 units. About 82 units (30%) have sold.
- **Live stock:** 116 listings, 193 units, about $3.15k Ross cost. It's worth about $11k at list price, but only 10 live listings have ever sold a unit.
- **Old stock:**
  - 60+ days old: 89 listings, holding about $2.7k of cost.
  - Never sold and 60+ days old: 75–81 listings, about $2.4k.
- **Supply:** at about 21–30 units sold a month, that's 6–9 months of stock. Projected forward, about $2.2k of cost is still unsold six months from now.

## 3. What sells and what sits

| Category / brand | Sold | Speed | Return on capital |
|---|---|---|---|
| Sunglasses (Oakley etc.) | 7 of 8 (86%) | median **1 day** | ~323%/mo |
| RL / LRL duvet sets $100+ | 10 of 12 | fast | ~46%/mo; best profit per unit |
| Watches | 3 of 5 | | 4.7× cost |
| Handbags (55% of catalogue) | 21% | sold ones median ~50 days at $50–100 | ~6%/mo |
| Ed Hardy | 16 of 82 units | ~6–7 sales/mo no matter how many are listed | |
| Michael Kors / MK Access | **0 of 10** listings | — | $560–745 tied up |
| Shams / pillowcases | 24% | | |
| Items costing $50+ at Ross | 22% of capital | | $72 profit total |
| Listings with 5+ units | 16% of units sold | | |

Sales per 100 listing-days:

| Sunglasses | Watches | Watch bands | Bedding | Handbags |
|---|---|---|---|---|
| 6.1 | 4.0 | 0.9 | 0.6 | 0.46 |

**October went the wrong way.** 20–22 of the 26–27 October buys were Ed Hardy
bags, the line that was already backed up (40+ live listings, 34% of the
store). That batch sold 9% within 14 days, against 19% for August's.

## 4. Speed and listing cadence

- **Time to sell:** median 20 days among sold items (p25 5, p75 46). The August playbook said 9.8 days.
- **Sell-through, counting unsold items:**

  | 30 days | 60 days | 90 days | 120 days |
  |---|---|---|---|
  | 21% | 33% | 37% | 43% |

- **New listings carry the store.**
  - 35% of sales come in a listing's first 13 days.
  - Organic impressions drop from about 223 in week 1 to about 60 by week 8.
  - Store impressions per listing halved between August (78 new listings) and October, after a September with about 1 new listing.
- **Store-wide sales stay at about 5 orders a week**, whether 50 or 116 listings are live. More stale listings don't add sales; fresh listings do.
- **The bot is not the bottleneck.** Capture to live takes a median of 5 minutes. The limit is store trips: 25 capture days in 4 months, and almost nothing from 18 Aug to 3 Oct.

## 5. Pricing

- **Buyers pay full price.** The median sale/ask is 1.00; only 7 of 50 sold below 97% of ask.
- **The v1 engine priced about 25% low.**
  - Owner raises sold at a median 1.35× the machine price.
  - All 4 raises made after a quick sale sold again at the higher price.
  - Pricing below 0.9× the comp median sold only 18% of the time, against 43–75% at 0.9–1.1×. Cheap isn't what moves stock; it usually means the comps didn't match the item.
- **v2 (the Claude judge) has no real track record yet.**
  - Only 3 live items use it.
  - Its confidence label is almost always "medium".
  - It has never had 3+ same-item sold comps.
  - **The weekly repricer still calls v1.**
- **Public price cuts haven't worked.**
  - 25 listings were cut 20–33% the week of 19 Oct. 14 of them were "INVISIBLE", where a cut can't help (see §6). None has sold since.
  - Overall, 3% sold within about 2 weeks of a cut, against 5.5% for any unsold listing-week.
- **Offers to watchers have worked.**
  - Listings with 3+ watchers sold 41–62% of the time, against about 16–25% for 0–2 watchers.
  - 4 recent sales came from watcher offers.
  - 78 of 116 live listings have watchers.
- **Best Offer:** every discounted sale cleared at 79–88% of ask (median 83%), and every buyer offer below 75% failed. **The 75% auto-decline is right.** Two Oakleys took offers at 79–86% within 1–4 days of listing, which left money on the table.

## 6. Traffic and listing quality

- **Funnel (90 days):** 2.25M impressions → 7,277 views (0.32% CTR, 1.56% on search) → 66 sales.
  - Clicks are healthy.
  - **View → sale is weak**: 0.9%, against a typical 1–5%. That points back to the stock mix and price fit, not the listings.
- **Listing quality is already good.** The spot-check found titles matching the DB 20/20, consistent photos and complete specifics. Rewriting titles across the catalogue isn't worth it.
- **The bot measures the wrong impressions number.**
  - It stores organic search impressions only, about 9% of eBay's total.
  - So it flags 53 of 114 listings as INVISIBLE when they actually get about 500 impressions a week.
  - That bad flag drove the "fix the title" advice and some of the cuts.
- **Ads setup:** all listings are promoted at 4%. An empty, uncapped **Dynamic** campaign created 21 Sep is still running.
- **Weakest listings:** the 4 adopted Seller-Hub listings have no returns policy and few specifics.

## 7. Operations and costs

- **AI cost:** about $0.10 per listed item, or about $0.20 per unit sold. That's under $5 a month now and about $15 a month at plan volume, a small part of the $100 budget. Apify's free credits are the tighter limit.
- **Ad fees:** about 5% of sales, roughly 20× the AI cost.
- **Price overrides:** the owner overrode the bot's price on 59% of items, as often up as down.
- **Errors:**
  - 354 network retries, none of which lost a listing.
  - 17 weekly suggestions were lost to Telegram rate limits. The grouped digest fixed this on 28 Oct.
  - The weekly check didn't run 24 Aug – 14 Sep.
- **Bulk buys:** SKUs bought 3+ at a time sold only 29% of units, leaving $1,007 unsold. Coach and Oakley multiples worked. 15 MK bands and 7–8 Ed Hardy weekenders didn't.

## 8. Recommendations

### Buying (owner) — the biggest lever

1. **Buy rule:** buy only if **Ross tag × 3.5 ≤ the realistic eBay sold price**, the expected profit is ≥ $15, and the tag is ≤ $40.
   - The data has a sharp step at cost = 30% of the sold price. Below it, capital returned 33%+ a month; at 30–50% it returned 5–13%; above 50% it lost money.
2. **Buy more:**
   - Sport/branded sunglasses (Oakley, Quay).
   - RL/LRL duvet and sheet sets that sell for $100+.
   - Name-brand watches.
   - Kate Spade watch bands.
3. **Pause or stop:**
   - Ed Hardy, until the backlog is under about 15 listings.
   - Michael Kors.
   - DKNY, Betsey Johnson, Tommy Hilfiger and Juicy Couture bags.
   - Shams and pillowcases.
   - Bags that list at $50–100.
   - Anything costing over $50 at Ross.
4. **Buy one, then restock winners.** Cap at about 3 units per listing.
5. **Make trips regular.** Two fixed trips a week, about 10–20 new listings a week, spread out. Fresh listings bring most of the traffic.

### Clearing stock

6. **Send offers to watchers** on stock 14+ days old: 10–12% off, starting with the most-watched:
   - Patricia Nash tote (20 watchers).
   - Ed Hardy top-hat satchel (18).
   - LRL Isla duvet (16).
   - Kate Spade band (15).
   - MK bands.
7. **Stop public price cuts on INVISIBLE or low-CTR listings.** If cutting, keep it to 8–12% once a listing is 30+ days old with watchers.
   - Re-score the 19 Oct cuts on 9 Nov.
   - Roll the 25–33% cuts back to about 10% if they haven't moved.
8. **Michael Kors:** re-price toward real sold prices, or bundle and exit.

### Bot changes (Claude)

9. **Fix the traffic metric.** Store total impressions and views by source, and base INVISIBLE on total impressions plus listing age.
10. **Repricer:** move to v2. Price no lower than the sold median (no undercut), and auto-raise 10–15% after a sale within 7 days.
11. **Automate offers to watchers** on a ladder (day 14+, 1+ watcher) instead of suggesting cuts.
12. **Make auto-decline depend on age:** 85% for days 0–14, 80% for days 15–60, 75% after that. Break-even still overrides.
13. **Add a brand/category scoreboard to `/report`.** Sales per listing-day and capital tied up, to guide the next Ross trip.
    - Warn when a brand has more than about 8 live listings.
    - Optional: an in-store `/check` that applies the buy rule.
14. **Data fixes:**
    - Record ad fees and refunds per order. The DB overstates net by about $390.
    - Fix the `sold_at` merge.
    - Record `published_at` everywhere.
15. **Housekeeping:**
    - Remove the empty Dynamic campaign.
    - Backfill the 4 adopted listings (returns policy, specifics).
    - Run `listing_check` on all live listings.
    - Revoke the unused OpenAI key.
    - Keep the PC awake.

### Small money

16. **Shipping:** charge about $12.99 on watches, duvets and king bedding, and weigh every package.
17. **Ads:** test Promoted Listings with a holdout group, or drop the rate. It costs 9% of profit with no proof it adds sales.

## Data caveats

- **Small samples:** most segments have fewer than 15 listings. The sunglasses, duvet, Ed Hardy and Michael Kors findings are solid; the rest are directional.
- **One outlier:** the Coach watch is about 30% of all profit.
- **Weekly stats:** 2 zero weeks and 4 missing weeks.
- **Missing dates:** 42 listings have no `published_at`.
- **Deleted items:** rejected items are deleted, so abandonment can't be measured.
- **Agreement:** the five analyses used slightly different cut-offs for "dead stock" ($2.0–2.7k), but they all agree on direction and order of size.
