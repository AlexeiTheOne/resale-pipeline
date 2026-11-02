# `/check`: pre-buy check in Telegram (design)

Status: **reviewed by Person B on 2026-11-02; v1 is being built on branch
`check-command`.** Written for Person B (owner) and Person A, who owns
pricing and reviews the PR. Builds on proposal (b) in
`docs/legacy-tools-assessment.md` and the logic in `legacy/comps.py` and
`legacy/comps_app.py` (both on branch `post-cutover-ideas`). Every fact about
today's code is from `main` at `9428e3a`.

## 0. Decisions (Person B's review, 2026-11-02). These override the sections below.

| Topic | Decision |
|---|---|
| Thresholds | As proposed in §5. **Backtested on our real sold items before they're trusted**, with the hit rate reported per verdict (`scripts/backtest_check.py`, §5). |
| Text input | Plain text, no LLM (v1). |
| **Mode** | **Quick is the default**, aiming at about 20 s. It uses live active comps (Browse text, GTIN, and image when there's a photo), plus sold comps **only from the cache and only if under 7 days old** (no new Apify call), and the judge **without escalation**. A **"Full check" button** runs the full pricing: live sold queries, escalation allowed. |
| **Matches** | **Exact matches only is the default**: price, counts and "cheapest identical" use the judge's same_item comps. An **"Any matches" button** (or `any` in the message) widens to Claude's price, which also leans on variants. |
| **Label cost** | **By item type** (bags, jewelry, shoes, clothing, other). Configurable with `CHECK_LABEL_COSTS`. Starting values in §4. |
| Daily cap | **50 checks a day**, with a warning at **40** (`CHECK_DAILY_MAX`, `CHECK_DAILY_WARN`). The day is US Eastern. |
| Access | Person B and Person A only: the bot's existing allowed-users guard (`TELEGRAM_ALLOWED_USER_IDS`) already covers every update. |
| Add to stock | v2, later. |
| Builder | Person B with Claude; **Person A reviews** (pricing is Person A's area). |
| **Sold-comps cache** | **7-day max age for `/check`** (approved). Repricing and intake keep today's behaviour (no expiry) until **Person A agrees**; this is noted in the PR. |
| **Timings** | Every check appends one line to `logs/check_timings.jsonl`: mode, exact, input (text or photo), and seconds for identify, comps, judge and total. The pilot reads it for real response times. The same file counts checks for the daily cap. |

**What quick mode means for the verdict.** BUY needs at least 3 matching
sales (§5). Quick mode only has sales when a recent cached search exists, so
a quick check is usually **at most MAYBE**, and the reply says "tap Full check
for sales". It still shows the money at the cheapest identical listing, and
break-even.

## 1. What it does

Standing in a Ross aisle, the user sends the bot a description or a photo,
plus the Ross price. Within about a minute the reply says whether the item is
worth buying.

```
/check Coach Tabby 26 CH857 49.99
/check exact Burberry London EDT 1.7 oz 14.99      ← exact matches only
[photo of the item or its maker's tag]  caption: /check 24.99
[photo of the Ross price tag]           caption: /check          ← price read from the tag's barcode
```

Reply (one message, edited in place while it works):

```
✅ BUY: profit $16.21 (ROI 101%)
Coach Tabby 26 shoulder bag (CH857)
Ross $14.99 → cost $16.04 with 7% tax
Sell at $39.99 + $9.88 shipping (buyer pays $49.87)
  fees + ads $10.42 · label $7.20 · you keep $32.25
Break-even: $19.71 + shipping
Cheapest identical listing now: $44.50 total → profit there $11.92
Sold (same item, 90 days): 6 · median $52.10 · evidence: solid
⚠ 1 of our own listings matches: already in stock? (item 3f2a…)
🔎 eBay sold · eBay active
[Details]  [Add to stock (later)]
```

Nothing is stored except the LLM-cost rows in `llm_usage` (so `/usage` stays
true) and the Apify result cache that pricing already keeps. No item is
created.

## 2. Flow

```
/check message
  │
  ├─ parse: Ross price (last number, or "$x"), "exact" flag, description text
  ├─ photo attached?
  │    ├─ Ross tag barcode found (ross_tag.decode_barcode → parse_barcode)
  │    │     → Ross code + Ross price, no LLM; price from the caption wins if given
  │    └─ identify.identify_item([photo], scan_paths=[photo]) → identification
  ├─ text only → build_identification(text)              (new, §3)
  │
  ├─ reply "🔎 Checking… (about a minute)", then work in a thread (asyncio.to_thread)
  │
  ├─ pricing_v2.get_pricing_v2(identification, photo_paths=[photo] or None,
  │                            item_id=None, stage="check")   (reused, §3)
  ├─ duplicate check: stages.same_item_keys vs our items (already in stock/listed?)
  ├─ profit.check_economics(sale_item_price, ross_price, …)  (new pure function, §4)
  ├─ verdict(…)                                             (new pure function, §5)
  └─ edit the message with the result + buttons
```

**Concurrency.** Nothing per item is written, so no `util._claim` is
needed. Allow **one running check per user** (a second `/check` while one
runs gets "still working on the last one"). Checks run in a thread pool like
the intake pipeline, so the bot keeps answering.

## 3. What is reused from `pricing_v2` and the rest (and what is new)

| Piece | Reused as is | Notes |
|---|---|---|
| `pricing_v2.get_pricing_v2(identification, photo_paths, item_id=None)` | ✅ | The whole engine: sold comps (Apify, up to 3 queries × 15 rows), active comps (Browse text + GTIN + image search, buyer-zip shipping), own listings dropped, the Claude judge (same_item / variant / wrong / bundle, Sonnet → Opus when unsure), `price_from_judged`, flags. **Stores nothing** (its docstring and `price_item` confirm). |
| Its result keys | ✅ | `suggested_price` (Claude's item price), `formula_price` (same-item sold formula), `same_item_sold_count`, `same_item_active_count`, `raw_active_floor` (cheapest same-item active **total**, known shipping only), `sold_median/p10/p90`, `evidence` (solid ≥ 5 / thin ≥ 3 / none), `confidence`, `review_flags`, `judged_comps`, `reference` (URL of the best comp). |
| `identify.identify_item` | ✅ | Photo path only. Same as intake: tag detect + vision + research + format (Gemini). |
| `ross_tag.decode_barcode` / `parse_barcode` | ✅ | The Ross tag barcode = 12-digit Ross code + price in cents. Free, local. |
| `stages.same_item_keys` / duplicate logic | ✅ | "You already have this" warning. |
| `profit.py` fee settings | ✅ | `EBAY_FVF_PCT` 0.155, `EBAY_AD_FEE_PCT` 0.046, `EBAY_FIXED_FEE` 0.40, `EBAY_SHIP_CHARGED` 9.88, `EBAY_SHIP_COST` 7.20, `SALES_TAX_RATE` 0.07 (all in `config.py`). |
| **`build_identification(text)`** | 🆕 | Text → identification dict, **no LLM by default**: `search_query` = cleaned text (filler words dropped, as `legacy/comps.py clean_query`), `model` = style code found in the text (`auto_required_tokens` rule: ≥ 3 digits and a letter, not a unit like `128gb`), `upc` = a 12–13 digit number, `brand` = first word if it matches a brand seen in our items, `size` from the text. Open question Q2 for an optional LLM step. |
| **`profit.check_economics(...)`** | 🆕 pure | §4. Today's `profit.breakeven_price` / `project` need a stored item with a receipt, so a small standalone function with the same settings. |
| **`verdict(...)`** | 🆕 pure | §5. |
| **`stage` argument** on `get_pricing_v2` → `judge` | 🆕 small | Today the judge always logs `stage="price_judge"`. Pass `"check_judge"` so `/usage` separates check spend from listing spend. |
| **Cache max age for sold rows** | 🆕 small | See §7: the Apify cache never expires today. |

**Exact matches only** (`/check exact …`):

- The price is the **formula price over same-item sold comps** (`formula_price`:
  median of same-item sold totals, −5% undercut, capped at the cheapest
  same-item active, minus our shipping), never Claude's estimate.
- Sold count, median and "cheapest identical" use **same_item only** (variants
  excluded).
- Fewer than 3 same-item sales → no formula price. Then the reference is the
  cheapest same-item active total minus 5% (labelled "from asking prices"), and
  the verdict is capped at MAYBE.
- Default mode (no `exact`): Claude's `suggested_price`, which may lean on
  variants and asking prices; the reply shows which.

This is the "Exact matches only" checkbox the newer `comps_app.py` had, in
v2 terms: the judge decides what is identical, not keyword rules.

## 4. Money (all from config; buyer-paid total basis, as in `legacy/comps.py`)

```
P   = suggested item price (default mode) or formula price (exact)
S   = shipping we charge                      EBAY_SHIP_CHARGED   9.88
T   = P + S                                   (what the buyer pays)
fees= T × (EBAY_FVF_PCT + EBAY_AD_FEE_PCT) + EBAY_FIXED_FEE  = T × 0.201 + 0.40
L   = shipping label we pay                   EBAY_SHIP_COST      7.20  (or "ship 12" in the message)
C   = Ross price × (1 + SALES_TAX_RATE)       ×1.07
profit = T − fees − L − C          ROI = profit / C
break-even total  T_be = (C + L + 0.40) / (1 − 0.201)     → break-even item price = T_be − S
profit_at_cheapest = same formula at T = raw_active_floor (cheapest identical total)
```

Worked example: Ross $14.99, P = $39.99 → T = $49.87, fees $10.42, label $7.20,
C = $16.04 → **profit $16.21, ROI 101%**, break-even T = $29.59 (item $19.71).

`EBAY_FVF_PCT` 15.5% is already the effective rate measured on our orders
(it includes eBay's fee on the sales tax the buyer pays). `EBAY_AD_FEE_PCT`
4.6% assumes every listing is promoted at the default rate, as today.

**Label cost by item type** (`CHECK_LABEL_COSTS`; these are starting
estimates, to be corrected from real label costs). The type comes from the
identification's `item_type` / product name, or from words in the text.

| Type | Matches (examples) | Label |
|---|---|---:|
| bags | bag, tote, backpack, purse, handbag, crossbody, wallet, luggage | $11.00 |
| jewelry | necklace, bracelet, earring, ring, watch, jewelry | $4.50 |
| shoes | shoe, sneaker, boot, sandal, heel, loafer, slipper | $12.00 |
| clothing | shirt, dress, jacket, hoodie, pants, jeans, sweater, top, shorts, skirt | $7.20 |
| other | anything else | $7.20 (`EBAY_SHIP_COST`, the measured average) |

## 5. Verdict rules (proposed numbers; all settings)

| Verdict | Rule (all must hold) |
|---|---|
| ✅ **BUY** | profit ≥ **$15** (`CHECK_BUY_MIN_PROFIT`) · ROI ≥ **75%** (`CHECK_BUY_MIN_ROI`) · ≥ **3 matching sales** (`CHECK_MIN_SALES`): judged same_item **or variant** in default mode, the same set today's auto-price gate counts; same_item only in exact mode · profit at the cheapest identical active ≥ **$5** (or no identical active) · judge confidence not low |
| 🟡 **MAYBE** | the BUY money tests pass, but the evidence doesn't: < 3 matching sales, priced from asking prices, low judge confidence, or `formula_disagrees` |
| 🟠 **BORDERLINE** | profit ≥ **$5** (`CHECK_MIN_PROFIT`) but below the BUY money tests |
| ❌ **SKIP** | profit < $5, or a loss at the cheapest identical active with < 3 sales, or no price at all |

Why these numbers:

- **$15 and 75%** (the old tool had $10 and 50%). The margin has to cover
  what the formula leaves out: handling time, storage, returns, and items that
  never sell. *These are assumptions, not measured.* The backtest below
  measures the real unsold share.
- **3 sales**: today's "thin" bar (`PRICE_THIN_MIN_COMPS`).
  - Same item **or variant** in default mode, because `pricing_v2`'s pilot
    note (17 Oct 2026) found only 0–1 **same-item** sales on all 10 pilot
    items. Counting same-item only, almost nothing would be BUY outside exact
    mode.
  - Exact mode is stricter by design: expect more MAYBE there.
- **The cheapest-identical test**: an item is only a BUY if we can still make
  money when we have to match the cheapest identical listing.

**Calibrate before trusting them** (CLAUDE.md: judge pricing by sales
outcomes). Extend `scripts/backtest_pricing.py` with a `--verdicts` mode.
For every sold or stale item with a Ross cost, compute the verdict its stored
pricing would have given, then compare it with what happened:

- the actual net;
- days to sell;
- whether it is still unsold after 60 days.

Tune the four numbers so that BUY rarely meets a loss or a stale item.

**Hit rate per verdict (as built: `scripts/backtest_check.py`).** It
replays the verdict for every item that has a Ross tag price and stored
pricing, using the stored suggested price, the judged comps and the cheapest
identical listing, in both exact and any mode. It then compares each verdict
with what happened:

- **win:** sold, with an actual profit (`net_proceeds` − cost with tax) of at
  least $15;
- **ok:** sold, with a profit of $0–15;
- **loss:** sold at a loss;
- **stale:** unsold more than 60 days after publishing;
- **open:** unsold for less than that (left out).

**Hit rate:**
- for BUY: wins / (all decided);
- for SKIP: (loss + stale + ok) / decided, i.e. "skipping didn't miss a win";
- for MAYBE and BORDERLINE: the plain outcome mix.

Caveat: these are items we **did** buy, so the backtest can't show what a
SKIP would have saved on items we passed over.

## 6. Cost and response time

**Cost per check** (from `llm_usage` history: 166 Sonnet judge calls averaged
$0.026; 43 Opus escalations, about 26% of runs, averaged $0.049; the intake
identify stages averaged about $0.015 together; Apify costs $0.004 per sold
row):

| Part | Text | Photo | Repeat check (cached sold rows) |
|---|---:|---:|---:|
| Apify sold: up to 3 queries × 15 rows | $0.18 | $0.18 | $0 |
| eBay Browse (text, GTIN, image) | free | free | free |
| Identify (Gemini) | $0 | ~$0.015 | ~$0.015 |
| Judge: Sonnet, +Opus in ~26% | ~$0.04 | ~$0.04 | ~$0.04 |
| **Total** | **~$0.22** | **~$0.24** | **~$0.04–0.06** |

**Guards.**
- `CHECK_DAILY_MAX` = 30 checks a day caps the cost at about **$7 a day**.
- The existing `ApifyBudgetExceeded` and the `ANTHROPIC_MONTHLY_BUDGET` warnings
  still apply.
- `/usage` shows a `check_judge` line.

**Response time** (estimated from the parts; no timings are logged today,
so the pilot measures them):

- Sold queries, Browse and image search run in parallel. The Apify actor
  dominates: about 20–45 s, and close to 0 when cached.
- The judge takes about 15–30 s, plus 30–60 s when it escalates to Opus.
- Identifying from a photo adds about 15–30 s.
- **Expect about 40–75 s for text and 60–100 s for a photo, up to about 2½
  min when the judge escalates.** The reply says "about a minute" at once and
  is edited when done.
- **Quick mode** (Q3) could answer in about 20 s from active listings only:
  no Apify, no judge, cheapest identical from GTIN and image search.

## 7. Things found while designing

- **The Apify sold cache never expires** (`pricing_v2._cached`, no age check).
  Repeat checks are cheap, but a cache from months ago would price on stale
  sales, and repricing has the same exposure. Proposal: a max age,
  `PRICING_V2_SOLD_CACHE_DAYS` = 7, checked on read, for both pricing and
  `/check`.
- **The judge stage name is fixed** (`price_judge`), so check spend would mix
  with listing spend. Hence the small `stage` argument in §3.
- **The Ross tag barcode carries the Ross price** (`parse_barcode`: the last 6
  digits are cents). A tag photo fills in the cost with no typing and no LLM.
  The Ross code is **not** searchable on eBay, so identifying the product still
  needs the item or its maker's tag (UPC or style code).

## 8. "Add to stock" (optional, later)

The button hands the check's identification and pricing to the existing
quick-add flow (`inventory_cmds._quick_add_item`), so nothing is fetched
twice. It needs the photo (text-only checks would ask for one), and it is
the only path that creates an item. The result is kept in memory (`state`)
for 30 minutes after the check, keyed by message id.

## 9. Tests

- The pure functions `check_economics`, `verdict`, `build_identification`
  and the message parser get table tests, including the worked example and
  each verdict edge.
- The command handler is tested with `tests/tg_fakes.py`, with
  `get_pricing_v2` monkeypatched, so no API calls are made. The tests check:
  - that nothing is written to `items`;
  - that exact mode uses `formula_price` and same-item counts only;
  - the daily cap and the one-check-per-user rule.

## 10. Open questions for Person B

1. **Thresholds:** are $15 profit / 75% ROI / 3 sales / $5 at the cheapest
   identical right for you, or do you have numbers from experience? (They get
   backtested either way.)
2. **Text input:** good enough without an LLM (style code and UPC pulled from
   the text; the rest goes to search as typed)? Or allow one cheap Gemini call
   (about $0.004) to structure messy text like "kids nike hoodie grey M"?
3. **Quick mode:** do you want `/check quick …` (about 20 s, active listings
   only, no Apify or judge, at most MAYBE) for when you're in a hurry?
4. **Default mode:** should "exact matches only" be the **default** (stricter,
   more SKIP and MAYBE), with `/check all …` for the wider estimate? Or the
   other way round?
5. **Label cost:** keep one default ($7.20), or default by item type (e.g.
   bags and backpacks $10–14, as the old page hinted)?
6. **Daily cap and budget:** is 30 checks a day (about $7) right?
7. **Who can use it:** only the allowed Telegram ids (both of you), like every
   other command?
8. **Add to stock:** wanted in the first version, or later?
9. **Ownership:** pricing is Person A's area. Should Person A build it, or
   Person B's Claude with Person A reviewing?
