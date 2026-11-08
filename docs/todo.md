# To do (not started)

Requests waiting for a free slot. Newest first. Each entry says what was asked,
what exists already, and a suggested design. Nothing here is decided beyond
what it says; ask Person A before building, since all three are in Person A's
area (bot, listings).

Remove an entry when it ships.

---

## 1. Find an item by name, not ID (asked 2026-11-08)

**Ask:** changing an item's photos or quantity needs its ID, and finding the ID
is tedious. Match a few keywords instead.

**What exists:**
- `util._resolve_item_id(user_id, args)` (bot/util.py) takes a full ID or a
  prefix, else falls back to `state.last_item`. Every ID-taking command
  goes through it: `/photos`, `/addphotos`, `/cover`, `/arrange`, `/setqty`,
  `/setprice`, `/listing`, `/retry`, `/delete`, `/end` and others.
- `/where oakley` searches by words, but over **pieces** (physical stock codes,
  `pipeline/inventory.search`), not listings.

**Suggested design:**
- Extend `_resolve_item_id`: when the args aren't an ID prefix, treat them as
  keywords. Match all words (case-insensitive) against title, brand, style
  code, UPC and receipt code. Rank live items first, then the rest.
- One match: use it. Several: reply with up to 8 buttons (`id8 · title ·
  price · status`). The tap re-runs the original command with that ID.
- Commands with a trailing value (`/setqty ed hardy skull 2`,
  `/setprice … 34.99`): the last arg is the value, everything before it is the
  query. `/setqty` already has a check for "names an item, not a quantity"
  (bot/listing_cmds.py); keep that working.
- Tests: one match, several (buttons), none, an ID prefix still wins, a value
  arg isn't swallowed into the query.

## 2. Several sizes in one listing (asked 2026-11-08)

**Ask:** for example, the same pair of shoes in different sizes posted as one
listing.

**What exists:** one item = one inventory item = one offer = one listing
(`ebay/inventory.create_draft_offer`). Size is a single item specific
(see `pipeline/specifics.shoe_size_value`, added in 54e5ce4).

**Suggested design:**
- eBay multi-variation listing: one inventory item per size (SKU
  `<item>-<size>`), grouped with an **inventory item group**
  (`PUT /sell/inventory/v1/inventory_item_group/{key}`, `variesBy` = the
  category's size aspect, e.g. `US Shoe Size`), one offer per SKU, then
  `publishByInventoryItemGroup`.
- Intake: photos as usual, then the owner types `sizes 8, 9, 10` or
  `9 x2, 10 x1` at review. Stored on the item (new `variations` field or
  table, via a `migrations.py` step on both backends).
- Knock-on work: sales sync (an order line names the SKU, so quantity is per
  size), `/setqty` per size, repricing (one price for all sizes to start),
  `/listing` display, ending and relisting.
- New listings only. Merging existing single-size listings means ending them
  and losing watchers and history.
- Check first: the category must allow variations
  (`getItemAspectsForCategory` `aspectEnabledForVariations`), and some
  categories don't.
- Biggest of the three. Estimate a few days with tests.

## 3. Zero-view listings: act on the data (found 2026-11-08)

**Not a code task yet, a check.** Live data from the 2026-11-02 snapshot:
24 of 107 listings had 0 views that week, 15 two weeks running, 6 for four
weeks. Of the 15, 10 are Ed Hardy and 12 are Women's Bags (169291), mostly
listed in August.

They are being shown (11–53 organic impressions a week, account median 54)
but not clicked. So it's mostly playbook Stage B (click failure) plus
weak demand, not retrieval. Likely factors:
- ~10 similar Ed Hardy bags around $39.99 compete with each other, and
  near-duplicates from one seller may be hidden (playbook Stage 0, item 2).
- The first photo, the start of the title, and price plus shipping.

Next steps, in line with the pricing rules in CLAUDE.md (offers before cuts):
send offers to watchers, check the first photos, then small cuts with
monitoring. **Don't rewrite titles for this.**

**Title practice checked at the same time:** 2 live titles start with
NWT/NEW (both hand-edited in Seller Hub), and none mention MSRP. Conclusion:
"NWT" first is fine (eBay's own guides put condition first:
ebay.com.au/sellercentre/create-listings, export.ebay.com). MSRP in a title
isn't worth it: nobody searches it, mobile cuts it off, and the characters
are better spent on colour, size or material. The bot's titles need no change.
