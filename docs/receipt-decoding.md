# Receipt line decoding and `/rcpt`

Person B, 2026-11-08. Code: `pipeline/receipt_decode.py`, `bot/receipt_cmds.py`,
`scripts/decode_receipt_lines.py`. Table: `receipt_line_details` (migration
step 5, `docs/schema-proposal.md` 1D).

## Why

Ross prints about 18 characters per receipt line: `EH BLK SKULL CROSS`,
`#ASST RUN/COURT`, `NOEX Drunk Elephan`. To answer "what did I buy for $16.99
on Oct 19?" each line needs to be decoded into words, and the item it paid
for needs a link.

## What is stored

One row per unique description (2,187 in ResaleDB on 2026-11-08), keyed by
`desc_key = UPPER(LTRIM(RTRIM(Description)))`. SQL makes the key; Python never
does (the dry run checks they agree). Blank descriptions get no row.

| Column | Meaning |
|---|---|
| category, item_type | closed lists (`CATEGORIES`, `TYPES` in the code) |
| gender | men, women, unisex, boys, girls, kids, baby, or NULL |
| color | up to two of 16 colours + multi, joined by `/`; only for wearables |
| brand_guess, product_model | the product's brand and model/style, when known |
| confidence | 0–1; `/rcpt` adds "?" below 0.6 |
| source | `matched_item`, `rules` or `ai` |
| model | the decoder: `rules-v2` or the Claude model that answered |
| item_id | the item, for `matched_item` rows only |
| decode_raw | the decoder's whole answer; `prev` holds an AI decode an item replaced |

## How a description is decoded

1. **matched_item:** every line with this description has a SKU that is one
   of our items or pieces (the same one). The item's identity is used, mapped
   to the lists; no AI. Items whose code was generated (`code_generated`) don't
   count. 59 descriptions, 103 lines.
2. **rules:** brand codes (EH, TR, VB, MK, …, about 120) and markers (`#` =
   shoe and handbag department, `NOEX` = beauty, `OTC` = health, a date prefix
   = consumable, `n!` = men's belt or wallet). Saved only when sure ("high",
   ~95% right): 277 descriptions. A "medium" guess (~88% right) goes to the AI
   as a hint instead: saving it would save about 3 cents.
3. **ai:** Claude Haiku (`RECEIPT_DECODE_MODEL`, default the cheap model), 50
   descriptions a call, `effort=low`, a JSON schema with the closed lists. It
   gets the price range (misread prices left out) and the rules' guess. Lines
   it skips are retried once. Two lines merged into one text are capped at
   confidence 0.5. When the AI names no brand but a brand code matched, the
   code's brand is kept. About 1,840 descriptions.

**Fashion codes** (`FASHION_CODES`, confirmed by Person B 2026-11-08): a line
whose first word is SM (Steve Madden), VB (Vera Bradley), EH (Ed Hardy), TR
(True Religion), BJ (Betsey Johnson), DKNY or Prezzo is always clothing,
shoes, bags, accessories or jewelry. The rules give it the brand's fallback
(bags; TR clothing) and a guard after the AI replaces a food, home or beauty
answer with it (confidence 0.5, `decode_raw.fixed = "fashion_code"`). Prezzo
is a sure rule: bags, duffel, no AI. Two exceptions, because licensed beauty
is real: a sure beauty/fragrance rule stays (EH MANI SET, EH 150 CT FLOSS,
DKNY BE DELICIOUS), and a fragrance answer stays when the line says EDT, EDP,
cologne, spray and so on. Before this, the AI read "SM BGIGI CHOCOLATE" (a
chocolate-brown bag) as candy.

Every run works the item and rules rows out again (free). An item row that
stops qualifying (a new line with another SKU) goes back to the AI; an AI row
that starts qualifying is replaced by the item and kept in `decode_raw.prev`.
An AI row is never paid for twice (`--redo-ai` to force).

**The item a line paid for is never taken from this table.** One description
can cover several SKUs, so `/rcpt` looks up each line's own SKU when it reads
it (`owned.ross_hits`, the same index as `/check`'s "already have"). Ross
reuses codes: 2 items on one code are shown as "2 items share this code",
never one picked silently. Dates and prices are never used to pick an item.

## Cost

Measured 2026-11-08: one call's fixed prompt is about 2,700 tokens; 3 lines
cost $0.00035. The backfill (about 1,900 descriptions, 38 calls) is about
$0.07–0.10, capped at $0.25 (`RECEIPT_DECODE_MAX_USD`). A new receipt adds
about 10 new descriptions: well under a cent a month.

## Backfill (`scripts/decode_receipt_lines.py`)

1. `--target ResaleDB --production`: free dry run. Tiers, keys left for the
   AI, estimated cost, the key check. Reads only.
2. `... --ai`: the paid run. Results go to `logs/receipt-decode-<db>-<stamp>.jsonl`
   with a report: the cost, a check against our own items (the AI's category,
   brand and type on descriptions whose SKU is one of our items; mostly bags)
   and a 30-line sample (10 random lines, the rest spread over rules, AI, our
   items and misread lines). **Nothing is saved.** The AI's token usage is
   logged to the `.env` database (dev), not the target; the script totals it.
3. After the sample is approved: `... --apply --from <jsonl>`. Needs step 5
   on the target (the deploy runs it), backs the target up, writes only
   `receipt_line_details`.

`--reuse <jsonl>` keeps an earlier run's AI rows (not paid again);
`--redo-category <cat>` and `--redo-fashion` (fashion-code lines that got a
non-fashion category) decode those again.

Then `RECEIPT_DECODE_JOB=1` turns on the daily top-up (`bot/jobs.py`, at
`RECEIPT_DECODE_HOUR_UTC`, at most `RECEIPT_DECODE_MAX_KEYS` new descriptions
and `RECEIPT_DECODE_MAX_USD` a run). It is silent; failures go to `/errors`.

## `/rcpt <query>`

| Query | Finds |
|---|---|
| `71436` (3–11 digits) | SKUs ending in those digits |
| 12 digits, or the 18-digit tag barcode | that SKU |
| `2710-03-5447-6292-3` (15 digits, dashes optional) | that receipt, all its lines in order |
| part of a receipt number with dashes (`6292-3`, at least 4 digits) | receipts containing it |
| `#7341` | receipt id |
| words (`ed hardy duffel`) | all words must match the description, the saved decode or the item behind the SKU. A brand name also finds its code (`ed hardy` → `EH …`). Gender words match the decoded gender exactly (`men` never finds `WOMEN`). At most 5 words. |

Each line: date and days since (Eastern), store city and number (the city is
the most common cleaned one for that store: OCR gives "Press For Less Doral",
"Ress For Less Mimi"), price, the raw description, what it is (our item's
identity; else the saved decode; else a sure rules reading marked "rules, not
saved"; else "not decoded yet"), misread flags, and the item it paid for with
its status, price, what we paid and eBay link (or a pre-buy check, or "no item
linked"). At most `RCPT_MAX_LINES` (10), newest first, then "…and N more".
Link previews are off. Read only: no AI, no writes. `/receipt` is a
different command (it sets an item's cost).

## Misread flags (per line, computed live, never written back)

| Flag | Rule |
|---|---|
| no description read | blank |
| garbled text | U+FFFD, `??`, Cyrillic/Greek, or an accented letter inside a word. Curly quotes, `•` and `×` are normal. |
| description cut off | fewer than 3 letters, unless dated, a quantity, or the SKU is our item |
| two lines merged into one | over 30 characters with a SKU inside |
| promo text | `55+ Tuesday Disc…` / `Original Price` |
| price differs from the text | no `$x.xx` in the text is within $0.05 of the price (B/R suffixes fine) |
| decimal lost? 4998 → $4.99? | price over $500; the `B` read as `8` is undone when it fits |
| no price read / price looks too low | NULL / under $0.49 ($0.49 itself is real clearance) |
| SKU misread? (info) | 12 digits not starting 40 |
| not an item (card text) | Auth. No., AID:, MASTERCARD… |
| return marker | `**Begin Return***` |
| this receipt lists this code at N prices | counted over the whole receipt |
| receipt total read as an item? | price ≥ $20 equals the subtotal, total or tax on a receipt with 2+ lines, and the other lines add up to it or it's over half the receipt |
| fixed by hand (info) | a `receipt_fixes` price fix (5 lines) |

Receipt-level problems (no date, no total, lines ≠ subtotal by over $1 from
`v_receipt_checks`, returns) are shown once in the header of a whole-receipt
reply, not on every line: half the receipts have a subtotal gap.

## Open

- What the prefixes `X ` (118 descriptions), `6.19 ` (25), `T-` and `V-` mean.
  Until someone knows, they are ignored and the AI decides from the rest.
