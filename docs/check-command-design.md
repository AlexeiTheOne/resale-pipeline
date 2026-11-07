# `/check`: pre-buy check in Telegram (design)

Status: **reviewed by Person B on 2026-11-02; v1 is being built on branch
`check-command`.** Written for Person B (owner) and Person A, who owns
pricing and reviews the PR. Builds on proposal (b) in
`docs/legacy-tools-assessment.md` and the logic in `legacy/comps.py` and
`legacy/comps_app.py` (both on branch `post-cutover-ideas`). Every fact about
today's code is from `main` at `9428e3a`.

## 00. v1 change: shopping mode, photos only (Person B, 2026-11-02). This overrides everything below where they differ.

**No typing in the aisle.**
- **`/shop`** turns shopping mode on; `/shop off` ends it. It also ends by
  itself **3 hours** after it was turned on: the bot posts "🛒 Shopping mode
  ended", and a later photo is normal intake again.
- While it's on, every photo or album is a check. Normal intake, `/find`,
  `/inv` and `/haul` are untouched, and resume when it's off.
- **It's clearly visible:** turning it on replies "🛒 Shopping mode ON until
  HH:MM: every photo or album is a check, not a new item". Every check reply
  starts with "🛒".
- Typed `/check …` still works as a fallback, and is saved the same way.

**One album = one check** (1–3 photos, any order).
- **Batching:** photos that arrive within 3 s of each other (an album, or
  quick separate shots) form one check. Beyond 3 photos, the first 3 are used
  and the reply says so.
- **The tag photo** is found by its **Ross barcode**
  (`ross_tag.decode_barcode` → `parse_barcode`: 12-digit Ross SKU + price in
  cents). It's local, with **no AI**. The other photos go to identification
  (`identify.identify_item`, with all photos as scan paths, so UPC and style
  codes on the maker's tag are read too). If the tag is the only photo, it is
  also the identification photo.
- **No tag barcode found:** the bot asks "No Ross tag found: what did it
  cost?".
  - Quick buttons: $4.99, $7.99, $9.99, $12.99, $14.99, $19.99, $24.99,
    $29.99.
  - Or the user sends **one number**, as the next message.
  - The check waits for 10 minutes. Identification already runs meanwhile,
    so the answer comes faster.

**Then pricing and the verdict, as now:** quick by default, and ❓ NO DATA
goes on to the full check automatically. The buttons on the reply:

| Button | Does |
|---|---|
| 🔬 Full check | Rerun in full (if the result was quick) |
| Any matches / Exact only | Widen or narrow the matching |
| 🛍 Bought | Status → `bought` (stock creation is v2) |

**Every check is saved** (no longer "store nothing"). It's a new table
`checks` (schema-proposal 1E; SQL Server migration step 3, plus the same
table on SQLite). It is **not an item**: nothing appears in `/status`,
listings or reports.

| Column | Notes |
|---|---|
| check_id | INT IDENTITY PK |
| user_id | Telegram user |
| created_at / updated_at | UTC |
| ross_sku / ross_price | from the tag barcode, or the price typed/tapped (SKU then NULL) |
| photos | JSON list of paths under `data/checks/<check_id>/` (kept; `data/` is not in git) |
| identification | JSON, as used for pricing |
| mode / exact | `quick` / `full`, and the match mode of the latest run |
| verdict | BUY / MAYBE / BORDERLINE / SKIP / NO_DATA, latest run |
| suggested_price / profit / roi / break_even / cheapest_identical / sold_count | the latest run's numbers |
| comps_summary | JSON: price basis, sales count, sold median, up to 5 comps (title, total, url, verdict), warnings |
| status | `checked` (default) / `bought` / `skipped` |
| status_at | when the status last changed |
| receipt_item_id | the receipt line it matched (NULL until receipt matching; see below) |
| item_id | the item made from it (NULL until v2's "add to stock") |

A rerun (Full check, Any/Exact) **updates the same row**: one row per thing
looked at.

**Receipt link (design only; built with receipt matching).** When a receipt
arrives (the receipt bot inserts `ReceiptItems`), each line's `SKU` is looked
up in `checks.ross_sku`:

- The candidates are checks by either allowed user in the **3 days before**
  the receipt date (a check after the receipt is impossible).
- **One match:** status → `bought`, and `receipt_item_id` = the line. The
  line's price wins over the tag price for cost.
- **Several:** the newest unlinked one, or a person picks one in Telegram, as
  `receipt_matches` does.
- **No match:** nothing happens.
- Checks still `checked` 3 days after a receipt from the same store and day →
  `skipped`. That's the only automatic route to `skipped`. There's no Skip
  button in v1.

**Still to do (noted, not in v1):**
- Add `data/checks` to the nightly photo backup. That means one robocopy line
  in `scripts/backup_task.ps1`, then copying it over the production
  `backup_ebay_bot.ps1`.
- Receipt matching, as above.
- Stock creation from a bought check (v2).

## 000. Pricing: the evidence ladder (Person B, 2026-11-02). This replaces the price choice in §3 and §0.

Every level is an **item price**: eBay totals minus the $9.88 shipping we
charge.

| Level | Evidence | Rule |
|---|---|---|
| 1 | eBay sold, **same item** (the judge's `same_item`) | **2+ sales: their median, done** |
| 2 | eBay sold, same model in another colour or size (the judge's `variant`) | 1+ counts |
| 3 | **Our own sales** (ResaleDB), same brand + type bucket | their median |
| 4 | **MSRP × brand ratio**: median sold/MSRP over our sold items | the type bucket when the brand has fewer than 3 sales; never the all-sales fallback; skipped when the MSRP looks wrong |
| 5 | eBay **asking prices**: the cheapest identical listing × `CHECK_ASK_DISCOUNT` | **a ceiling only, never the price** |

- **No level 1:** the price is the **median of levels 2–4** that exist.
- **Ceiling:** the price is never above level 5, so never above the
  cheapest identical listing.
- **Range:** when the levels used differ by more than 40%, the reply shows
  the range.
- **No level 1–4 at all:** ❓ NO DATA; asking prices alone never set a price.
- **Any mode:** variants count as level 1.
- **BUY** still needs `CHECK_MIN_SALES` eBay sales. A price estimated from
  levels 2–4 says so ("~ … (est.)") and gives at most MAYBE.
- **The evidence line** shows each level found, in plain words and whole
  dollars: "0 sold same · 1 sold same model $21 · our Juicy Couture bags ~$58
  (12d) · MSRP×47% $51 · ask ≤$92 · 1 for sale 146d, 0 sold". Level 3 adds
  our median days to sell. Details lists every level with its numbers.
- **The backtest** leaves each item's own sale out of levels 3–4.

### Comps, demand and dropship listings (part C)

- **Details lists every comp used**, each with its link: eBay sold (same
  item and same model, newest first, with the sale date), then identical
  listings for sale (cheapest first). At most 12 lines, then "… and N more".
- **Demand (full check only):** eBay's item lookup (Browse, app token, read
  only, free) on the `CHECK_DEMAND_LISTINGS` (3) cheapest identical listings
  gives the listing date, units sold, seller feedback and category. The
  evidence line ends with "N for sale 12–146d, 0 sold". **eBay gives no
  watcher counts** for other sellers' listings: not in Browse, not in Trading
  GetItem (tested on 117471801433 and 298394126856), and the listing page
  blocks scripts.
- **Dropship-style listings** are flagged on **2 of 3 signals**: a copied
  title (ends in "..." or has two Amazon phrases like "for women", "large
  capacity", "stylish & versatile"); a seller with at least
  `CHECK_DROPSHIP_FEEDBACK` (10,000) feedback; a category outside the item's
  department (a bag under Baby > Diaper Bags). Sold rows have no category, so
  they need both of the other two. Flagged sales are left out of levels 1–2;
  flagged listings still count for the ceiling. Details shows the flag and
  its signals.

### Competition, Best Offer and links in the reply (Person B, 2026-11-02)

- **Competition:** an identical listing for sale that is not dropship-style
  counts as competition (dropship-style ones stay the ceiling).
  - **None, and level 2 or 3 shows demand:** the price is the **upper end**
    of levels 2–4; the evidence line says "no competition, unique".
  - **None, and no demand signal:** the median stays; "no competition, no
    demand data".
  - **With competition:** the median, never above the cheapest identical
    listing (level 5).
  - Level 1 (2+ same-item sales) is unchanged; with no competition it adds
    "no competition".
- **Best Offer line:** from the estimate, rounded to .99:
  list × `CHECK_BO_LIST` (1.25), auto-accept × `CHECK_BO_ACCEPT` (1.05),
  auto-decline under × `CHECK_BO_DECLINE` (0.90). For example "List $49.99 BO
  (auto-accept $41.99, decline < $35.99) or $39.99 fixed". Profit is shown
  at the fixed price. Setting Best Offer when publishing is a later idea for
  Person A (`docs/legacy-tools-assessment.md` §8, branch
  `post-cutover-ideas`).
- **Links under the evidence line:** up to 2 best matches (sold first, same
  item before same model; then identical listings, real sellers before
  dropship-style), labelled like "🔗 Sold $30.49 (pink)". Then always "🔎 Sold
  on eBay" and "🔎 For sale on eBay" searches for the product name (else the
  style code), even with no data. The reply is sent as Telegram HTML so the
  labels are tappable; plain text if Telegram refuses it.
- **The reply is now up to 8 lines** (this replaces the 5-line limit):
  verdict, item, Ross · sell · profit, Best Offer, evidence, best matches,
  searches, check number · time · mode. Details keeps every comp.

### "Same model" needs proof; labels; the brand ratio (Person B, 2026-11-02)

- **Same model (level 2, and level 1 in any mode)** now needs the sold
  title to carry **our style code** (or its base without a colour suffix:
  11JCDA0JC-JTL → 11JCDA0JC), **or every distinctive word of our product
  name** (not brand, type, size or material words: "Juicy To Go Weekender
  Tote" → "go", "weekender"; "LBAVERY Extra Large Canvas Tote" → "lbavery").
  A title cut short at 80 characters may end in the start of the last word.
  Whatever the judge said, a "variant" that fails this becomes
  `other_style`: left out of the price, counted in Details ("N other
  style(s) left out"), and in the log next to the judge's own counts. With
  no style code and no distinctive name there is nothing to prove a match,
  so no variant counts. Check #3's cat-face faux-fur tote no longer counts
  for the LBAVERY canvas tote.
- **Sewn-in labels:** identification (`identify.py` research prompt) now
  reads size, care and content labels for the style code and the brand's
  short product name when there is no hang tag, and ignores factory/lot
  lines. On `test_images/label_ed_hardy_slipper.jpg` (label only, Ed Hardy
  "EH06521655/EH SLIPPER CUP W"), two live runs gave model EH06521655 both
  times (before: none), product "Slipper Cup W" once, brand "EH" or none.
  The label alone doesn't confirm the brand; a photo of the item does.
  Local OCR can't read this label (tilted, dim), so the model does it.
- **Brand ratio (level 4) is already brand-first:** `msrp.ratio_for` uses
  the brand's own median sold/MSRP once it has 3+ sales (any type), else
  the type bucket (3+ sales), else all sales, which `/check` never uses.
  Juicy Couture and Luv Betsey have no sales yet, so they get the bag ratio.
  A later refinement: brand + type when that pair has 3+ sales (a brand's
  watches and bags sell at different ratios). That is `pipeline/msrp.py`,
  Person A's file: not changed here.

### Check #5 fixes: SKIP needs sales, label by bag size (Person B, 2026-11-02)

Check #5 (Vans Detached Sling Bag VN000Q9BFS8, Ross $12.99, MSRP $45) said
❌ SKIP from MSRP × 47% alone, with 0 sales.

- **SKIP only when real sold prices back it:** 2+ same-item sales (level 1)
  **or our own sales** of the brand + type (level 3; approved 2026-11-03,
  after the backtest showed 9 of its 12 right SKIPs had level 3). An
  estimate from MSRP × ratio or a single same-model sale with failing
  money is 🟠 BORDERLINE, and the
  headline says "(estimated)" for every estimated verdict, e.g.
  "🟠 BORDERLINE (estimated): no sales found, 2 listings".
- **Label by bag size:** `small_bags` in `CHECK_LABEL_COSTS` ($5.50, Ground
  Advantage) for slings, crossbodies, minis, wallets, wristlets, pouches,
  belt bags; `bags` ($11) for totes, weekenders, duffels, backpacks and any
  bag of unknown size. The name decides (a soft sling folds into a poly
  mailer whatever its length: check #5's sling measures 21"); "mini" beats
  "backpack". Only a name that says neither ("handbag") goes by the
  measured sides: none over `CHECK_SMALL_BAG_MAX_IN` (11") = small.
  Details shows "label $5.50 (bags, small)".
- **Ratio from other brands:** when the brand has no sales of its own, level
  4 uses the type ratio (47% from our handbags). The evidence line says
  "MSRP×47% $21 (other brands)", and Details says so too.
- **Losses** read "−$0.63", with a real minus sign.

### Check #10 fixes: real sold comps, fees, Max buy (Person B, 2026-11-06)

Check #10 was a Von Dutch VDBB-1036PRP purple cow print bowling bag. The bot
said "0 sold same", priced it from MSRP × other brands' ratio ($60.99), and
capped that at the asking prices. eBay's own Sold link showed the bag sold
in four colours in 30 days, the purple one included.

- **Why no sales:** it was a quick check, and quick checks read only cached
  sold searches. None of its 5 searches was cached, so it looked at no
  sales at all. Live, the two long queries ("… crossbody NWT", the full
  product name) find nothing anyway. Only the style code (1 sale) and
  fewer words find them.
- **A quick check with no sold search cached** now goes straight on to the
  full check, as NO DATA does. The message says "no eBay sales cached for
  this item yet". The pair counts as one check. When a quick reply does
  have no sales looked at, the evidence says "sold not searched", never
  "0 sold same".
- **Fewer words** (`fewer_words_query`): the full check also searches the
  brand + up to 4 distinctive words of the product name (no colour,
  material or size) + the item type's noun, like eBay's "results matching
  fewer words": "Von Dutch cow print bowling bag".
- **Same model** ignores colour and material words in the product name
  ("Purple & White … Vegan Pony Hair") but keeps pattern words (leopard,
  plaid, …). So the brown cow print is the same model, and the "flying
  eyeball" bowling bag still isn't.
- **Real sales win the ladder:** level 1 if there is one. Else level 2 (+
  level 3, our own sales). MSRP × ratio (level 4) is used only when there are
  no sales, and is otherwise shown but not used. **Asking prices cap only a
  price without eBay sales**: an unsold ask doesn't lower a price that sales
  back. A **single same-item sale** (too few for level 1) joins level 2.
- **Fees** (corrected 2026-11-07): 15.5% of what the buyer pays + $0.40
  per order + the listing's ad rate (4%), about 20% in all. See §4. A flat
  24.7% (`CHECK_FEE_PCT`) was used for one day; it came from one small
  order with a 6.5% ad rate and has been removed. The label is still
  subtracted on its own. The repricing floor and the profit report are
  unchanged (Person A's).
- **Max buy** on every reply, and in Details: the highest Ross tag price
  (before tax) that leaves $15 profit (`CHECK_BUY_MIN_PROFIT`) at the sell
  price. "~" when the price is an estimate. "none" when even $0 doesn't
  make it.

Replayed (dev, full): **BORDERLINE, sell ~$45.99** from 7 sales (1 the
same purple bag, 6 other colours), profit ~$9.83 at $24.99, **Max buy
~$20.15** (with the 24.7% fees; with the 2026-11-07 fees: profit ~$12.34,
Max buy ~$22.50). It was MAYBE at $60.99. The saturation signal below then
lowers it further.

### Saturation: sold in 30 days vs listings for sale (Person B, 2026-11-06)

Full check only (`supply_lookup`, `count_supply`). For the **same item** and
the **same model** (all colours, the same item included):

- **Sold in the last 30 days** (`CHECK_SUPPLY_DAYS`), from the sold searches
  the full check just made. They are re-read from the cache, so there is
  no new Apify call.
- **For sale now**: the pricing searches, plus one free Browse search on the
  name in fewer words with up to 200 rows (`CHECK_SUPPLY_ACTIVE_COUNT`).
  The long-name search finds few listings of the other colours.
- **Which rows count:**
  - Same model: `same_model`, or the judge said variant / same item.
  - Same item: the judge said so, or the title is the same model with our
    full style code, or with our main colour and no colour we don't have.
    A mini is not the same item as a bag that isn't one.
  - A match by title alone needs our brand in the title.
  - Our own listings are left out.
- **Months of supply** = listings / sales in 30 days.
- **High supply:**
  - over `CHECK_SUPPLY_HIGH_MONTHS` (3) months, or no sales and at least
    `CHECK_SUPPLY_MIN_ACTIVE` (4) listings;
  - on either line, same item or same model.
- **What high supply does:**
  - The verdict drops one step: BUY → MAYBE → BORDERLINE → SKIP. The
    first reason says why.
  - A price from eBay sales (levels 1-2) goes no higher than the **lower
    quartile** of those sales. An estimate without sales keeps its price
    and only loses the step.
- **A full page is a floor.** Sometimes a search comes back as a full page
  (sold: 15 rows, all inside 30 days) and most of its rows are the item or
  model. Then there are more than the page shows. The count is shown as
  "15+" ("fast seller"), and a sales floor is never called high supply. A
  full page of mostly other styles ("Von Dutch Bowling Bag") is not a
  floor.
- **Shown** as one line after the evidence, also in Details, and saved in
  `comps_summary.supply`: "⚠️ Supply: same model 7 sold / 37 active in 30d
  (~5 months of supply, high) · same item 1 sold / 3 active in 30d (~3
  months of supply)".

Check #10 replayed (dev, full):
- same model 7 sold / 37 active, same item 1 sold / 3 active (the three
  purple asks, $82.70–85.15 with shipping);
- the price goes from the $45.99 median to the lower quartile, **$41.99**;
- the verdict goes from BORDERLINE to **SKIP**;
- profit ~$9.12 at $24.99; Max buy ~$19.49 (2026-11-07 fees).

There are 37 active, not ~20, because the 200-row search finds more
colours than eBay's first page shows.

### "Do I already have this?" (Person B, 2026-11-05)

The first line of every reply, before the verdict: `pipeline/owned.py`
(replaces the old "⚠️ in stock" note on line 2).

- **Searched:** items (every status), pieces, Ross receipt lines
  (`db.receipt_lines_for_skus`, SQL Server only) and earlier checks (not
  this one).
- **Keys, strongest first:**
  1. brand UPC: a valid GTIN, never a Ross store code;
  2. style code + colour: "Multi" or no colour counts as unknown, which
     matches any colour. The same style in another colour is listed as
     "same style, other colour";
  3. Ross code, the 12-digit tag SKU. A Ross code stored in the UPC field
     counts as the Ross code. Either code matches: the REDUCED sticker's
     (`ross_code`, what we paid) or the main tag's when it differs
     (`ross_code_main`; checks `ross_sku_main`). Every barcode on the tag is
     read, and the lowest price is the sticker (`ross_tag.split_codes`).
     Measured 2026-11-05:
     - 0 of 157 stored tag photos and 7 check photos decoded two different
       codes.
     - Reduced items are on receipts as often as full-price ones (13 of 17
       vs 44 of 94), so the stored code is the one the register prints.
     - Of 2,616 receipt lines with no item, 1 more would match. That one is
       a misread code (490… for 400298209190), not a second code.
- **AI fallback:** a title match runs only when no key matched and we have
  things of the same brand (at most 8, by shared name words). It is one
  `CLAUDE_CHEAP_MODEL` call, stage `check_owned`. `CHECK_OWNED_AI=0` turns it
  off.
- **Each hit shows:**
  - status and price;
  - days listed, or the sold date;
  - what we paid (the Ross price, else the piece cost);
  - profit if sold: net per unit − paid × (1 + tax);
  - the box code if at home;
  - the eBay link.
- **Receipt lines:**
  - Lines already entered as an item or piece with that Ross code are that
    item.
  - The remaining lines show as one reply line: "Bought 10× (Aug 11 –
    Sep 14) · paid $0.99–$15.99".
- **Runs before the eBay search (2026-11-05, check `/check 400301430764 15.99`):**
  - A UPC, style code or Ross code match on an item, piece or earlier
    check lends that identification to the search.
  - With nothing of its own (a typed code, no brand), the check takes it
    whole and searches the listing title. A photo's identification only
    has its gaps filled.
  - Ross codes are taken out of every search query, product name, UPC and
    the 🔎 links. When nothing searchable is left, there is no eBay search
    and no automatic full check, and the reply says to send a photo.
  - Details says "Searched as our item …".
- **First line:** what the matched thing is, e.g. `Ed Hardy "Love Kills
  Slowly" … Weekender Duffel Bag · bag: weekender duffel · pink
  multicolor`. With only receipt hits, it's the receipt description.
- **Listed and sold hits** link with "🔗 <listing title>".
- **Receipts are counted per receipt:**
  - A receipt that lists one code at several prices is "misread" and is
    left out of the price range.
  - Measured on 213 receipts: 57 times one code at several prices (37 on
    adjacent lines), and only 63 of 177 subtotals add up.
  - No Ross code covers clearly different products: 302 codes on 2+
    lines, 0 with different descriptions.
- **Same item vs same model (Person B, 2026-11-05, check #5).** Ed Hardy
  EHH108-26-WIP (Wilma print, Ross 400298771833) had matched other EHH108
  prints (Tillie) on the base code as "style code + colour".
  - **The same item** is a matching UPC, Ross code or FULL style code, the
    print/colour suffix included (EHH108-26-WIP).
  - A code with no suffix (LB102100) also needs the colour to agree. If we
    hold that base with a suffix elsewhere (EHH108 → EHH108-26-TLP), a bare
    EHH108 is only the model.
  - **The same model** (base code only) shows as one line of its own: "Same
    model EHH108, other prints/colours: 2 listed, 1 sold". It doesn't count
    as "already have" and lends nothing to pricing.
  - **Colours:** "Multi" doesn't match a named print ("Wilma Print"), and a
    named print must appear on both sides. "Tattoo print" names none.
  - **A brand tag's barcode that is a Ross code** (Ed Hardy tags carry one)
    is a Ross code. `identify` keeps it as `ross_code`, not as the UPC, and
    matching uses it.
  - **Intake stores the full style code:** the prompt asks for it, and a
    longer OCR'd tag code wins over a shortened one. `item_identification.style_code`
    was empty on every item.
  - **Production, 164 items:** 62 full codes (base + suffix), 45 one-part
    codes, 57 none. In the 13 brands whose codes carry suffixes, 7 items
    store a base only (4 of them Ed Hardy).
  - **Brand concentration:** "Ed Hardy: 40 listed, 12 sold" after the
    hits, or "✅ New · Ed Hardy: 40 listed".
- **The reply shows** "✅ New" when there are no hits. Otherwise
  "🔁 Already have: 1 listed, 1 sold, 2 on receipts", then up to
  `CHECK_OWNED_SHOW` (3) hits. Details lists every hit.
- **Coverage (production copy of 2026-11-05, 167 items):**
  - UPC: 138 items;
  - style code: 106 items (stored in `model`; the `style_code` column is
    empty);
  - Ross code: 117 items;
  - at least one key: 162. Only 5 rely on the AI match.
  - 2,726 receipt lines (2,275 SKUs). Only 58 SKUs are also an item's
    Ross code.

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

**❓ NO DATA (Person B, 2026-11-02, after the first real runs).** A quick check
that finds **nothing at all** (no recent sales, no identical listing for sale,
so no price) gets the status **❓ NO DATA**, not SKIP or MAYBE. The bot then
**continues straight into the full check**, with no tap needed. It edits its
message to "Checking more sources…", and the full result replaces it. The
pair counts as **one** check toward the daily cap (the quick line in
`logs/check_timings.jsonl` is marked `"counted": false`), and the
identification is reused, so a photo isn't identified twice.

**Changed (Person B, 2026-11-02, after the backtest):** a **full** check that
finds no price is also **❓ NO DATA**, not SKIP. In the backtest, 14 of 20
such SKIPs were winners, while the 7 SKIPs backed by real prices had no
winners. **SKIP is only given when real prices back it.**

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
a   = the listing's ad rate                   EBAY_DEFAULT_AD_RATE_PCT  4%
fees= T × (EBAY_FVF_PCT + a) + EBAY_FIXED_FEE = T × (0.155 + 0.04) + 0.40   (2026-11-07)
L   = shipping label we pay                   EBAY_SHIP_COST      7.20  (or "ship 12" in the message)
C   = Ross price × (1 + SALES_TAX_RATE)       ×1.07
profit = T − fees − L − C          ROI = profit / C
break-even total  T_be = (C + L + 0.40) / (1 − 0.195)     → break-even item price = T_be − S
profit_at_cheapest = same formula at T = raw_active_floor (cheapest identical total)
max buy = (T − fees − L − 15) / 1.07          (the Ross price for $15 profit)
```

Worked example: Ross $14.99, P = $39.99 → T = $49.87, fees $10.12,
label $7.20, C = $16.04 → **profit $16.51, ROI 103%**, break-even T = $29.37
(item $19.49), max buy $16.40.

Fitted on our 85 orders (Person B, 2026-11-07):
- Fees without ads are 15.5% × (item + shipping) + $0.40 (`EBAY_FVF_PCT`,
  `EBAY_FIXED_FEE`), off by $0.79 an order on average. The 15.5% is the
  effective rate: it includes eBay's fee on the sales tax the buyer pays.
- Ads were charged on 77 of the 85 orders, at a median 4.3% of item +
  shipping. /check uses the ad rate a new listing gets
  (`EBAY_DEFAULT_AD_RATE_PCT`, 4%).
- All-in median: 19.6%.

The flat 24.7% of 2026-11-06 was one small order with a 6.5% ad rate.

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
