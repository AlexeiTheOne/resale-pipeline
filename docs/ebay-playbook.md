# eBay Listing Playbook

**Working reference for auditing and rewriting ~110 listings** — US site, fixed-price, $25–70 ASP, Ross/TJX clearance: handbags, watches & watch bands, bedding & home textiles, apparel, accessories. Written 2026-08-16.

**Confidence tags used throughout:** `[eBay]` = eBay's own published wording. `[measured]` = this seller's own settled-order data (`config.py`, re-derive with `python -m ebay.orders`). `[observed]` = consistent seller reports, no eBay source. `[contested]` = researchers or sources disagreed — read the note before applying. `[folklore]` = widely repeated, traces to no source. `[unverified — check live]` = could not be confirmed and must be before acting. **Never apply a `[folklore]` rule to 110 listings.**

---

## 0. How eBay actually works

**Three stages.** eBay's own ML publications describe search as: retrieve a candidate set → score it with a ranker trained on **purchase likelihood** → re-rank incorporating seller ad rate ([innovation.ebayinc.com](https://innovation.ebayinc.com/stories/multi-relevance-ranking-model-for-similar-item-recommendation/)). Everything below is a consequence of that.

**Retrieval reads the title, not the description.** Default keyword search does not index description body text — "Title and description" is an opt-in checkbox in [Advanced Search](https://www.ebay.com/sch/ebayadvsearch), which proves the default index. Matching is all-words-any-order, so word *presence* matters far more than word order. Since 2025 eBay layers on neural relevance: a BERT cross-encoder consuming literally `keyword [SEP] category name [SEP] item title` ([arxiv 2505.04209v2](https://arxiv.org/html/2505.04209v2)) and a billion-scale vector engine embedding titles, item aspects and images ([innovation.ebayinc.com](https://innovation.ebayinc.com/stories/ebays-blazingly-fast-billion-scale-vector-similarity-engine/)). eBay's own paper flags the consequence: **the relevance model receives only text.** An item that is visibly yellow with no colour word in the title or aspects is invisible on colour.

**Filters are a hard gate, not a ranking signal.** eBay: *"your item will only appear in those filtered search results if you've added the matching item specific"* ([sellercenter/item-specifics](https://www.ebay.com/sellercenter/listings/item-specifics)). Free shipping, free returns, condition, price range, buying format and location radius (10–1000 miles) are all hard filters too ([advanced search](https://www.ebay.com/sch/ebayadvsearch)). A blank aspect is **total exclusion** from that facet at any rank — this is the single most common cause of a near-zero-impression listing.

**Ranking factors: seven, unweighted.** eBay names exactly (1) match to search terms, (2) item popularity, (3) price, (4) listing quality (description, photos), (5) listing completeness, (6) terms of service — return policy and handling time, (7) seller track record ([help id=4166](https://www.ebay.com/help/selling/listings/listing-tips/optimising-listings-best-match?id=4166)). eBay publishes **no weights**. The widely-repeated "relevance 40–50% / seller performance 30–40% / listing quality 20–30%" split is `[folklore]` — a dozen 2026 blogs state it verbatim citing nothing. Do not build a score on it.

**Because the ranker predicts purchase probability**, views and watchers are not independent positives. High views with zero sales is a *negative* profile. No eBay document lists watchers as a ranking input.

**Account level is a global multiplier.** eBay Seller Center: *"Top Rated Sellers receive enhanced visibility in eBay search results"* ([top-rated-program](https://www.ebay.com/sellercenter/protections/top-rated-program)) — note eBay's own Help version of that page lists only the seal and fee discount, so eBay is inconsistent with itself. Below Standard brings reduced visibility plus a 5% additional FVF ([seller-standards-policy id=4347](https://www.ebay.com/help/policies/selling-policies/seller-standards-policy?id=4347)); the exact definition is in §5 and is narrower than most sources claim. Evaluated on the **20th of each month**; with <400 transactions/quarter the window is **12 months**, so one bad stretch suppresses the whole catalogue for a year.

**Ending and relisting is officially a myth.** eBay's Senior Director of Search, Pete Dainty, presented it as such at eBay Open 2025: ending and relisting *"hurts your long-term seller performance"* — it resets performance data, watchers and cart adds, breaks offsite search (**Google takes ~3 days to index 90% of eBay inventory**), and reduces organic *and* paid impressions over time. Recency is *"a smaller factor"* than relevance, competitiveness and engagement ([valueaddedresource.net](https://www.valueaddedresource.net/is-ebay-end-relist-a-myth/)). `[contested]`: eBay staff have elsewhere recommended relisting after 90 days of inactivity, and many sellers observe lifts. Revising in place keeps item ID, URL, views, watchers and sales history — it is strictly the safer lever.

**Timing constants that govern every audit.**

| Event | Latency | Source |
|---|---|---|
| New listing appears in search | up to 24h | [id=4166] |
| Title / item-specifics change moves impressions | 3–7 days | observed |
| Price / offer change converts existing watchers | hours | observed |
| Traffic report data | lags 24–48h | eBay |
| Listing Quality Report reflects a change | ~2 days (data refreshes 24h) | [eBay LQR docs] |
| Account seller-metric damage clears | full evaluation cycle (3 or 12 mo) | [id=4347] |

**Measure over 14 days. Change one variable per listing per window.** Anything less is noise.

---

## 0.5 Format rule & run order

**Fixed-price, Good 'Til Cancelled, on everything.** eBay's own framing is to use fixed price when you want items to show for more than 10 days (the auction maximum). Auction and fixed-price listings are ranked separately and then interleaved, so auction carries **no inherent ranking advantage**. GTC auto-renewal preserves item ID, URL, watchers and sales history; a finite 30-day fixed-price listing throws all of that away on every cycle — the same asset destruction as end-and-relist, just on a timer.
**Checkable:** `listingType == 'FixedPriceItem' AND duration == 'GTC'`. Flag any auction, and any fixed-price listing with a finite duration.

**Run order — once per audit, in this order.** A fix on a listing nobody is served is worth nothing, and a title rewrite under a suppressed account is worth less than nothing.

1. **Account gates and Below Standard** (§5). If it fails, stop — fix the account. Every listing-level finding is informational until it clears.
2. **Traffic-data sanity** (§6): ≥7 consecutive days of non-zero *account-total* organic impressions. Without this every number below is unusable.
3. **Live compliance blockers**: apparel/footwear Size holds, missing Required aspects that make a listing un-revisable (§2).
4. **Hard filters**: condition enum from the right family, leaf category, Required + 7 Recommended aspects (§2).
5. **Titles** (§1).
6. **Photos** (§3), then **descriptions** (§3.5).
7. **Price, offers, returns** (§4, §5).
8. **Portfolio and sourcing** (§7).

Within each step, work listings in **descending 30-day impressions** — highest-exposure listing first.

---

## 1. Titles

The title is the *only* free-text index. 80 characters, hard-enforced (Trading API error 70). Unused characters are queries the listing can never match.

**Length.** eBay's own stat: titles longer than 60 characters are **~1.5x more likely to sell** ([export.ebay.com](https://export.ebay.com/en/growth/promotion-strategies/promotion-strategies/)) — correlational, treat 60 as a floor not a lever. eBay says use the space for *accurate* terms and explicitly says do **not** add irrelevant words to reach 80. **Accuracy wins the tie-break:** a 58-character title in which every word is true and every aspect is represented is a **pass**. Short length is only a defect when attributes are actually missing (see checklist).

**Front-loading.** Every SEO blog claims Cassini weights the first 50 characters more heavily. `[folklore]` — no eBay source, and eBay's own published architecture is bag-of-words plus neural relevance, not left-to-right decay. **The defensible reason is display truncation:** the mobile app search grid truncates at roughly **48–55 characters**, desktop grid at **65–70**. Mobile share is quoted two ways and the units differ — **~53% of eBay GMV is mobile** (businessofapps, better sourced) and *"~two-thirds of eBay traffic is mobile"* `[observed, SEO blog]`. Either way the truncation argument stands on its own. Front-load; just don't believe the wrong reason.

**Policy landmines** ([search-browse-manipulation-policy id=4243](https://www.ebay.com/help/policies/listing-policies/search-browse-manipulation-policy?id=4243)):
- All words must be accurate and refer **only** to the item for sale.
- **Question marks are banned outright** — *"If any item details are unclear or unknown, they should be left out."*
- No comparisons ("like Coach", "similar to"), no cross-promotion ("see my other listings").
- **`fits` / `for` / `compatible with` are NOT allowed before brand names of jewelry, clothing and accessories.** "Band for Rolex", "Purse for Coach" on a handbag or fashion-accessory listing is a policy violation, not weak SEO. Penalties are graduated: demotion, hidden listing, administrative end, lowered rating, account restriction.
- **Watch bands are the exception `[contested]`.** For a genuine accessory, eBay *requires* "for"/"compatible with" before the compatible item's name — "for Apple Watch 45mm" is the correct form, and omitting "for" is what turns it into brand misuse. This directly conflicts with the fashion-accessory prohibition. **Route every watch-band title to manual review; do not automate either way.**
- **Multi-brand risk, ~Feb 2026 `[observed]`:** sellers report eBay's automation began blocking titles containing more than one brand name even when both are accurate (canonical case: an Invicta watch naming its Seiko movement; one seller had 10 of 20 listings repeatedly rejected on revision). Community-reported, not announced. Highest exposure here: watches (case + movement) and handbags.

**VeRO / IP exposure `[observed]`.** Michael Kors, Coach and similar brand-name goods — including made-for-outlet SKUs (§7) — are live VeRO targets. A VeRO takedown arrives as a **listing removal with no appeal path to eBay** (only to the rights owner), escalates to account restriction on repeats, and appears in **no traffic report** — so a listing that vanishes with no metric explaining it should be checked here before anything else. Rules: never use brand logos or brand marketing imagery you did not shoot; never use "authentic" as a keyword; and note the "Brand style" / "Brand inspired" / "like Coach" patterns already banned under Search Manipulation are **simultaneously VeRO triggers** — one phrase, two enforcement systems.

### Title checklist
- [ ] `len(title)` ≤ 80 always. **<65 is not an automatic fail** — first check whether Colour, Material, Size, Department, Style and Model from the specifics all appear in the title. If they do, the title is done at any length ≥50. If any is missing, short length is a defect: fix by adding the missing *true* attribute, never by padding. <50 chars is severe regardless.
- [ ] `title[:50]` alone contains Brand + head noun + one decisive attribute.
- [ ] Brand in the first 3 tokens (≤ char 25) and **string-identical** to `specifics['Brand']`.
- [ ] Exactly **one** brand name in the title.
- [ ] No `?`. No `L@@K WOW RARE HOT SALE NR "FREE SHIP" "MUST SEE"`, no `★ ♥ ✔ 🔥 [NEW]`, no `!`, no asterisks.
- [ ] Punctuation limited to letters, digits, space, hyphen, slash. eBay explicitly advises against asterisks/markers between words and against all-caps words. Flag `uppercase_ratio > 0.60`; allow brands that genuinely capitalise (CROCS, BCBG) and whitelisted acronyms (NWT, NWOT, NIB, OEM, XL).
- [ ] No duplicate stems (lowercase, drop stopwords, Porter-stem, count>1 = fail). eBay stems and synonym-expands queries already, so `Bag`/`Bags` is pure waste — a documented synonym set is handbag/bag/purse/pocketbook ([SIGIR eCom 2019](https://sigir-ecom.github.io/ecom2019/ecom19Papers/paper20.pdf)).
- [ ] At most **one** bridge synonym beyond the head noun. "Crossbody Shoulder Bag" fine; "Tote Crossbody Clutch Satchel" is stuffing *and* a lie.
- [ ] `Color` and `Material` aspect values appear verbatim in the title (the relevance model is text-only).
- [ ] Size in the title for apparel/shoes/bedding, in natural language ("Women's Small") — the **Size aspect** must separately carry the standard value ("S").
- [ ] Model / MPN / reference number present if the item has one. For watches this is often the entire query.
- [ ] Department word present for fashion (Women's / Men's / Girls / Boys / Unisex), matching `specifics['Department']`.
- [ ] Does not open with "New" (condition is a filter buyers click, not a word they type). Max one condition acronym; drop it if the title is ≥75 chars and missing colour/material/size/style.
- [ ] Readable as a sentence fragment, not keyword salad — eBay runs a conversational AI shopping agent in core search since May 2025 and BERT cross-encoders over the title.
- [ ] No store name, URL, @handle, trend-jacked keyword, "authentic", or brand-adjacent phrasing ("style", "inspired").
- [ ] **Do not pay for a Subtitle.** 55 chars for ~$2–3 per listing charged win or lose; 2026 sources say Cassini does not index it, older eBay dev docs say it does — `[contested]`, and at 110 listings the economics don't work either way.

---

## 2. Item Specifics & Category

Four tiers: **Required** (publish/revise blocked without it), **Required Soon** (with an `expectedRequiredByDate` eBay itself calls *"only an approximate date"*), **Recommended** (chosen *"based on buyer demand data and popular searches"*, ranked by searches over the trailing 30 days), **Additional**.

**The Required penalty is a publish block, not a demotion.** eBay's Taxonomy docs: *"a seller will be blocked from listing or revising an item without these aspects"* ([txn:AspectConstraint](https://developer.ebay.com/api-docs/commerce/taxonomy/types/txn:AspectConstraint)). A legacy GTC listing that renews into a newly-required aspect becomes **un-revisable** — your next price change silently fails. Re-audit against the live Taxonomy API monthly.

**How many is enough?** The only eBay-published number is from the Listing Quality Report, for Women's Shoes > Heels: *"Fill in 7 recommended item specifics per listing."* `[folklore]`: "complete specifics = 3x more likely to sell" and "36% more visibility" trace to no eBay source and appear only in blogs copying each other. One researcher cited the 36% figure as eBay-sourced; another traced it and found nothing. **Treat both as unverified.**

**Aspects are a conversion surface, not just a filter.** On the mobile item page item specifics render **above** the description — so an empty or filler aspect is read as a missing answer by the buyer who already clicked, on top of excluding the listing from that facet.

**LIVE ENFORCEMENT THIS MONTH — apparel & footwear Size.** eBay auto-normalises high-confidence sizes ("Small"→"S") and removed custom-value entry on new listings from **June 2026**; listings with non-standard, missing or non-compliant Size **and/or condition** values are **blocked or placed on hold**. `[contested]` on the date: one source says enforcement from July 2026, eBay's developer blog and the August 2026 Seller News say August 2026 — and the dev site carries a standing banner using the words *"blocked or hidden"* ([developer.ebay.com/updates/blog/size-standardization](https://developer.ebay.com/updates/blog/size-standardization)). Either way it is live now. eBay measured **17–21% of US apparel/footwear listings** as non-compliant, with API-created listings over-represented. Words like Women's / Men's / Petite / Plus and measurements belong in Department / Size Type, **not** in Size.

**Condition enums differ per category family** ([id=4765](https://www.ebay.com/help/selling/listings/creating-managing-listings/item-conditions-category?id=4765)) — they are not interchangeable:

| Family | Values |
|---|---|
| Clothing (CSA) | New with tags · New without tags · **New with imperfections** · Pre-owned Excellent/Good/Fair |
| Jewelry & Watches | New with tags · New without tags · **New with defects** · Pre-owned Excellent/Good/Fair · Refurbished tiers |
| Shoes | New with box · New without box · New with defects · Pre-owned Excellent/Good/Fair |
| Home & Garden (bedding) | **New** · Open box · Refurbished tiers · Seller refurbished · Used · For parts — **no "New with tags" exists here** |

Structured `conditionDescriptor` ID fields exist **only** for trading cards (183050, 183454, 261328) and coins (253, 256, 3377, 4733, 18466). For this catalogue the only structured condition signal is the enum plus free-text. Don't hunt for descriptor fields.

**Category.** Must be a **leaf**. Category determines which aspects are Required, which filters apply, which Top Rated Plus returns carve-out you fall under, and what the LQR benchmarks you against — getting it wrong corrupts all four. eBay may relocate the listing under the search-manipulation policy. **Never use a second category.** Since everything here is GTC (§0.5), a second category is a *recurring* insertion fee — charged per listing *and* per category, re-charged monthly on renewal: ~$0.25–0.35 × 110 × 12 ≈ **$330–460/yr** for a facet buyers rarely browse. On a variation listing it can also break the variation structure outright.

**Catalog / ePID matching.** Entering a UPC pre-fills brand, model, dimensions, colour and *professional photos*. But: *"It's against our search manipulation policy to use catalog details for a product you aren't listing"* — listing removal, buying/selling restrictions, possible suspension ([id=4653](https://www.ebay.com/help/selling/listings/creating-managing-listings/product-identifiers?id=4653)). Ross stock is frequently a make-for-outlet colourway that *looks* like the catalog SKU. Also note: **eBay may absorb your uploaded photos into its catalog** and show them on competitors' listings — *"you can't selectively choose which photos are considered."*

**AI autofill is a specific audit target.** eBay's Magical Listing generates title, category, specifics and description from a photo alone; ~30% of US mobile-app sellers used it daily by late 2025. Independent Feb 2026 testing found it misidentified a sea-turtle mousepad as a "ceramic decorative plaque", then a "fridge magnet" — and because identification failed first, **category, specifics and description were all wrong downstream** ([valueaddedresource.net](https://www.valueaddedresource.net/ebay-ai-magical-listing-revisited/)). A wrong category removes the listing from the filter tree entirely.

### Specifics & category checklist
- [ ] 100% of `aspectRequired == true` aspects filled — pull live from `GET /commerce/taxonomy/v1/category_tree/0/get_item_aspects_for_category?category_id=`.
- [ ] ≥ 7 Recommended aspects filled (or all of them if the category offers fewer).
- [ ] No filler anywhere: regex-fail `^(n/?a|does ?not ?apply|unknown|none|see (description|photos|measurements)|various|-|\.|\?)$`. Filler is **worse than blank** — it satisfies the lightning-bolt meter and Seller Hub's task list while landing in a junk facet.
- [ ] `aspectMode == SELECTION_ONLY` aspects contain an exact `aspectValues` member. Report nearest allowed value by string distance.
- [ ] Value length ≤ `aspectMaxLength`.
- [ ] `itemToAspectCardinality == MULTI` aspects (Color, Material, Features, Pattern, Occasion — up to 30 values) carry every true value. A two-tone bag entered as one colour misses half its buyers.
- [ ] Brand = the real manufacturer, exact eBay spelling, never Unbranded/Generic/blank on branded Ross stock.
- [ ] Colour is eBay's enumerated value ("Pink"), not the marketing name ("Blush Sunset").
- [ ] Apparel/footwear **Size** is an exact standard value. Sort failures by 30-day impressions descending — those are already being hidden.
- [ ] Condition set, from the correct family's enum.
- [ ] Real UPC/EAN from the hang tag; "Does not apply" only when the item genuinely has none. Missing GTIN is a top Google Shopping rejection reason.
- [ ] Category is a leaf; cross-check against `getCategorySuggestions(title)` top 3.
- [ ] **No second category, ever.**
- [ ] ≤3 custom aspect names, ≤25 total aspects (web form errors above ~25; the API has thrown "too many item specifics" below 45 `[contested]`, and a stale 2018 API doc says 15).
- [ ] Every value evidenced by a photo or the physical tag — never inferred, never trusted from AI autofill.
- [ ] **Never tick "Don't remind me about these recommendations again."** It removes the listing from the recommended quick filter, tasks and emails permanently — so a Seller-Hub-driven audit systematically skips your worst listings while the search consequence remains.

**Category-specific required facets** — derive from the API, but these are the ones buyers actually filter on:
- **Wristwatches (31387):** Department, Movement, Case Size, Band Material, Display.
- **Watch bands** (a *different* leaf): Band Material, Band Colour, Band/Lug Width, Buckle/Clasp Type, Compatible Brand. If the listing carries Movement/Case Size/Display, the category is wrong.
- **Women's Bags & Handbags (169291):** Department, Style, Exterior Material, Exterior Colour, Size, Bag H/W/D.
- **Bedding:** Size (Twin/Full/Queen/King/Cal King), Set Includes / Number of Pieces, Material, Thread Count, Pattern, Colour, Brand. Condition is plain **New**.

**Don't audit against [eBay's Item specifics requirements page](https://www.ebay.com/sellercenter/listings/item-specifics-requirements)** — its newest requirement wave is still dated **16 May 2023** (verified live 2026-08-16). It has been abandoned in favour of monthly Seller News and the Taxonomy API; its silence is not "no changes".

---

## 3. Photos

**Hard limits.** Minimum 500px on the longest side (policy floor, at least 1 photo required). **24 photos free** in nearly every category — doubled from 12 in 2022; the "12 free" figure is obsolete. `[contested]`: the April 2026 listing form says 40 but uploads still fail past 24 — treat **24** as the working limit. Variation listings: **12 per variation**. Max file **7 MB** on direct upload (12 MB when eBay copies from a URL; API rejects >12 MB with error 190201) `[contested]` — one researcher reported a flat 12 MB; use 7 MB as the safe bar. Formats: JPEG, PNG, GIF, BMP, TIFF, WEBP, HEIC, AVIF. eBay stores photos 90 days if unused.

**Resolution.** eBay Picture Services is documented as storing a **maximum of 1600 × 1600** and downscaling anything larger — but that traces to a legacy Trading API page (`uploadsitehostedpictures`) and is `[unverified — check live]` for 2026; do not assert that a 4000px upload is discarded detail. `[contested]`: one researcher puts the zoom-viewer threshold at **800px** (community-established, eBay's own text says only "high enough resolution"), another at **1600px**. **Shoot 1600 and the question is moot** — that advice is safe under every version.

**Aspect ratio.** eBay's design playbook: 1:1 is *"our dominant and recommended ratio"* and **search results and carousels use 1:1 exclusively** ([playbook.ebay.com](https://playbook.ebay.com/foundations/layout-in-product/image-ratio)). eBay preserves your ratio on the item page but centre-crops to square in the grid — a 3:4 phone photo loses the top and bottom 12.5% in the one image that decides the click. Crop to square yourself.

**Prohibitions** ([picture-policy id=4370](https://www.ebay.com/help/policies/listing-policies/picture-policy?id=4370)). eBay states plainly: *"Do not add graphics to images such as badges, logos, copyright notices or watermarks as they can affect your listing's placement in search results"* — one of the very few explicit, checkable eBay statements linking a listing attribute to ranking. Also banned: added borders/frames, added text/artwork/marketing material (eBay names "Free Shipping" and seller logos), placeholder images, images that don't accurately represent the item. **Watermarks of any type are banned, including for ownership attribution** — a legacy eBay Seller Center page still publishes a "5% of area / 50% opacity" allowance; **that page is stale, audit to the total ban.** Stock/catalog photos may not be the primary image on pre-owned/used/damaged items (exceptions only for Books, Movies, Music, Video Games).

**Background.** White is **recommended, never mandatory** in any of these categories, and the old Google white-background requirement was relaxed. The target is *plain and consistent*. eBay explicitly endorses **dark backdrops for reflective items** — watches, metal bands, polished hardware.

**What the evidence actually supports.** eBay's own study of **6.8 million listings**: listings meeting its photo standard are **4.5–5% more likely to sell** — and its bar is merely 500px, no added text, uploaded to eBay's picture service. Cornell/Ma et al. (WACV 2019, ~75,000 images): **handbags 1.25x more likely to sell** with higher-quality images, shoes 1.17x — handbags showed the largest effect of any category tested ([news.cornell.edu](https://news.cornell.edu/stories/2019/01/seeing-believing-depends-photo-quality-study-says)). Same study on what "quality" meant to the model: **brighter is better**; high product contrast with **low** background contrast; and a **high foreground-to-background ratio scored LOWER** — edge-to-edge crops read as amateur. Trust ratings: good seller photos 3.8/5, stock 3.7/5, **low-quality own photos 3.4/5** — a blurry self-shot loses to a stock image.

`[folklore]` — do not repeat: "8+ images = 30% higher conversion", "multiple angles increase sales 58%", "high-quality images attract 40% more views", "a pro gallery image 2–4x your CTR". All attributed to eBay; none traces to an eBay publication.

**Photo count.** `[contested]`, converge on: **hard-fail <6, target 8–12, ceiling 24 free.** The 6 floor comes from community-reported Listing Quality Report text stating that listings with the most impressions in a category average 6 photos `[observed, low confidence]`. 8–12 is where marginal shooting time stops paying at this ASP. **The per-category minimum below overrides the global 6** — a handbag at 7 photos fails.

**Image search.** eBay's "search by image" embeds the query photo with a CNN and ranks live listings by visual similarity to *their* images — live on iOS/Android in US/UK/DE/AU, integrated with Apple Visual Intelligence. Collages, hands, props and busy backgrounds pollute the vector for photo 1.

### Photo checklist — script-checkable
Every one of these is a deterministic measurement with a stated threshold. Build these; do not build the ones below them.
- [ ] Photo count ≥ **max(6, category minimum)**. Variations: 4–12 each, and each variation's images actually differ (pHash distance > 8 between variations).
- [ ] Every file ≥800px longest side (hard-fail <500 = policy violation); target **1600**; flag >2400 as pointless.
- [ ] Photo[0] aspect ratio 0.98–1.02.
- [ ] File ≤7 MB; flag >3 MB as needlessly large. Format in the allowed list.
- [ ] Zero rendered text: OCR every file. Fail on store name, "FREE SHIPPING", `$`, `%`, SALE, URL, phone, @handle, QR. (Text physically on the item — care label, brand tag, dial print, box print — is fine, so OCR hits route to the human pass, not to auto-fail.)
- [ ] **No added border:** sample the outer 2.5% ring. Flag when per-channel std within the ring **< 8** (on 0–255) **AND** Lab ΔE between ring mean and interior mean **> 15**.
- [ ] **Backdrop consistency:** pairwise Lab ΔE between corner-region means across a listing's photos **≤ 10**. Above that, flag as mixed backdrops.
- [ ] **Blur:** Laplacian variance on the 8-bit grayscale image resized to 1600px longest side, **< 100 = flag**.
- [ ] **Underexposure:** mean luminance ≥ ~110/255; no strong R/B channel cast on background pixels.
- [ ] **Flash hotspot:** any saturated (>250 luminance) connected region >2% of frame inside the subject — that's a hotspot erasing a dial or a texture.
- [ ] **pHash duplicate across the whole catalogue** (Hamming ≤ 5). Same file on two listings with different Colour/Size = accuracy violation *and* the two listings compete in image search.
- [ ] **Never enable Gallery Plus.** $0.35–$2.00/listing, free only in Collectibles, Art, Pottery & Glass, Antiques — not here. It's a desktop hover effect, ~53% of eBay GMV is mobile, and it silently re-bills on every GTC renewal.

### Photo checklist — human eyeball, ~10 seconds per listing
These need object detection and segmentation to automate, which will not get built. At 110 listings the human pass is **~20 minutes** and is strictly more reliable than the CV it replaces.
- [ ] Photo[0]: exactly **one item, centred, plain background, no hands, props, collage or lifestyle scene**. Lifestyle goes at position 2+.
- [ ] Subject fills roughly **60–90%** of frame — not lost in it, not cropped edge-to-edge (the Cornell result: tight crops read as low quality).
- [ ] Photo[0] survives a centred square crop with the whole item inside.
- [ ] Photo[0] is not a stock/catalog image when condition ≠ New. Tells: pure #FFFFFF zero-noise background, perfect symmetry, studio lighting you don't own. **EXIF Make/Model only works on local files pre-upload — eBay's editor strips EXIF, so it is useless on already-live listings.**
- [ ] Backdrop type matches the item: light for textiles/apparel, dark for watches and metal.
- [ ] Scale reference (coin/ruler/tape) present for handbags, watch bands, bedding.
- [ ] Every named flaw has a macro photo at ≥800px so it can be zoomed. **This is the evidence that wins an INAD case**, together with the description text (§3.5).
- [ ] Category shot list below is complete.

**Per-category shot lists** (eBay publishes lists for apparel/shoes/handbags only; watches and bedding are derived from general rules + return logic `[observed]`):
- **Apparel (≥6):** front, back, brand/size label, fabric-content & care tag, flat measurement shot **with the tape measure in frame**, flaw macro. On a mannequin or dress form — eBay recommends it to demonstrate fit. Cross-check the OCR'd size against the Size aspect; a mismatch is high severity.
- **Handbags (≥8):** exterior front, exterior back, base/feet, lit interior lining, interior brand stamp/serial, hardware macro, strap attachment point, scale reference. Base and strap-attachment are where wear shows first; the interior stamp defuses authenticity doubt.
- **Watches / bands (≥6):** dial square-on, caseback (carries the reference number), crown/side profile, clasp open, band material macro, **lug/strap width against a ruler** — the #1 pre-purchase question and #1 return reason on bands.
- **Bedding (≥6):** styled full-spread on an actual bed, flat/folded, **size + fibre-content label (mandatory)**, weave macro, hem/seam detail, packaging if NIP. Size is the biggest dispute vector; this is the one category where a busy background in a non-first position is correct.

**Apply Google Merchant Center's stricter bar** — eBay syndicates to Google Shopping free listings on your behalf, and rejections show only in the LQR's Google Shopping tab: min 500×500, recommended 1500×1500+, max 16 MB / 64 MP, hard bans on watermarks/overlays over the product, promotional text, borders, placeholders and added logos ([support.google.com/merchants/answer/6324350](https://support.google.com/merchants/answer/6324350)). Meeting Google's bar automatically clears eBay's.

---

## 3.5 Descriptions

**Not indexed by default search.** "Title and description" is an opt-in checkbox in [Advanced Search](https://www.ebay.com/sch/ebayadvsearch) — proof that description body text is outside the default index. A keyword block in the description therefore earns **nothing** and breaches the Search Manipulation Policy ([id=4243](https://www.ebay.com/help/policies/listing-policies/search-browse-manipulation-policy?id=4243)) at the same time: cost with no upside. What the description *does* feed is named Best Match factor #4 — *"listing quality (description, photos)"* — and only as genuine product copy.

**Active content is banned outright.** JavaScript, Flash, embedded forms and external active elements are prohibited by eBay and will not render. A legacy template carrying a `<script>` tag is simultaneously a policy exposure and a broken page.

**The description is half the INAD defence.** Condition, **full flat measurements**, fibre content and every named flaw, in text, alongside the macro photos (§3). "See photos" is not a measurement, and an unmeasured apparel or bedding listing loses an INAD case by default — which costs a seller-paid return label, a transaction defect, and a hit to the peer-benchmarked service metric (§5).

**Mobile.** Item specifics render **above** the description on the mobile item page, and fixed-width HTML tables break the mobile layout entirely. Write flowing text in short paragraphs; put anything structured into aspects, where it is both a filter and the first thing a mobile buyer reads.

### Description checklist
- [ ] No `<script>`, `<iframe>`, `<form>`, `<object>`, `<embed>` anywhere in the description HTML.
- [ ] No keyword stuffing: flag any comma- or pipe-separated run of **10+ tokens**, any trailing `keywords:` / `tags:` block, any repeated brand list.
- [ ] Condition ≠ New ⇒ a measurement block is present: regex `\d+(\.\d+)?\s*(in|inch|inches|"|cm)` within ~40 characters of chest/bust/waist/length/sleeve/inseam/shoulder/strap/drop, or an explicit H × W × D.
- [ ] Fibre / material content stated for all apparel and bedding, and string-consistent with the Material aspect.
- [ ] Every flaw named in text has a matching macro photo, and vice versa.
- [ ] No `<table width=...>` and no absolute pixel widths >600px.
- [ ] No contact details, off-eBay links, cross-promotion, or "authentic"-as-keyword.
- [ ] The WYSIWYG note and SHIPPING/RETURNS boilerplate (`config.WYSIWYG_NOTE`, `ebay/inventory.py`) present and identical on every listing; `/refreshdesc` pushes changes to live listings.

---

## 4. Price & Offers

**The real cost of a sale — measure, don't quote.** eBay's headline is 13.25% → 13.6% (raised 14 Feb 2025, capped at +0.35pp) on the first $7,500, then 2.35%, **plus $0.30 per order ≤$10 / $0.40 over $10**. Three of this catalogue's four categories are at **15%**, not 13.6%, and were excluded from the 2025 increase because they were already higher:

| Category | FVF |
|---|---|
| Women's Bags & Handbags | **15%** up to $2,000, then 9% (a cliff, not a tier) |
| Watches, Parts & Accessories (includes **bands/straps**) | **15%** on the first $1,000 |
| Jewelry (other) | **15%** up to $5,000 |
| Bedding / Home & Garden / general apparel | 13.6% |

**`[measured]` — this account's actual numbers, and the only ones to model with:**

| Constant | Value | Note |
|---|---|---|
| Effective FVF vs item+shipping | **15.5%** (range 13.5–17.0%, n=10) | eBay charges FVF on sales tax too; the headline rate understated fees on **all ten** settled orders |
| Effective ad cost | **5.0%** ($53.17 / $1,066.78) | against a 4% nominal ad rate |
| Fixed fee | $0.40 | |
| Total variable take | **20.5%** of (item + shipping) | |
| Median shipping charged / label cost | $10.86 / $10.09 | net +$0.77 per order |
| Median COGS | **29% of list** (p25 22%, p75 34%, n=63) | |
| Median time-to-sell | **9.8 days**; 3 fastest closed inside a day at full price | |

**Solved forms — use these, not `price − cost − 0.136×price`:**
```
net_promoted   = 0.795 × price − 1.856 − cost      # ad fee charged on price + shipping
net_unpromoted = 0.845 × price − 1.313 − cost
floor($5 net)  = 8.624 + 0.3648 × list_price       # assuming cost = 0.29 × list
```
Both constants derive from `config.py`: `(price + 10.86) × (1 − 0.155 [− 0.05]) − 0.40 − 10.09`. **The two intercepts are not interchangeable** — every promoted figure uses −1.856, every unpromoted figure uses −1.313.

Total take against **item price alone**: **31% at $25, 27% at $40, 24% at $70** — roughly double the headline. The $0.40 order fee and the fee-on-$10.86-shipping are what make cheap items disproportionately bad.

**Discounts cost 79.5 cents on the dollar** (84.5c unpromoted) — eBay's take shrinks with the price. A 15% offer costs ~12% of net. Sellers who model a discount as a full-price hit systematically under-discount and sit on stale stock.

**Auto-decline must be a dollar floor, never a blanket percentage.** Fixed costs don't scale, so the $5-net floor is **71% of list at $25, 58% at $40, 49% at $70**. One flat percentage simultaneously accepts loss-making offers on cheap items and rejects profitable ones on expensive ones.

**Best Offer mechanics.** Buyers get up to 3 offers per item; each side gets max 5 counteroffers; **a counteroffer auto-declines all previous offers** — you can no longer accept the earlier one. Auto-accept/auto-decline offers are handled by eBay and you never see them. **Since 24 March 2026 the counteroffer window is 96 hours, not 24** (US/UK) — most third-party guides still say 24 and are stale ([valueaddedresource.net](https://www.valueaddedresource.net/ebay-best-offers-4-day-counteroffers/)). **There is no eBay statement that Best Offer improves ranking** — eBay's Best Offer docs make no visibility claim at all. `[folklore]`.

**Send Offer to Buyers.** "Interested" = watchlisted, abandoned cart, or viewed more than once. Fixed-price only. Minimum discount **5%** under $200 (3% $200–1,000, 2% above); max reported 50%. Valid **96 hours** on eBay.com. **One offer per buyer per listing, permanently** — once sent, that watcher is disqualified unless they re-engage. `[contested]`: eBay's export site says the 30 most recent interested buyers, the US help page and community say 10 per send. Watchers who disabled offer messages won't appear; the "Listings not eligible for offers" error is also a known long-running eBay defect, so don't assume seller error. Acceptance rates observed are low and wildly variable: 53 offers → 1 acceptance (1.9%) in one report, 10–20% in another `[observed, low confidence]`.

**Offer vs reprice — the two are deliberately gated differently, and both gates are correct:**
- **Send an offer** at `watchers ≥ 1 AND days_live ≥ 14`. It is free, private, single-shot per watcher, and reversible. A weak signal is enough because a wrong offer costs nothing.
- **Reprice publicly** only at `watchers ≥ 5 AND days_live ≥ 30` (§6 Stage D). Below that the watcher count carries no information about price, and a public cut is permanent, visible to every future buyer, and fires an alert to everyone watching.

**Charm pricing** is real but old and not eBay-specific: JCR 2005 found 9-endings raised demand ~24%; the MIT/Chicago catalogue experiment found $39 outsold both $34 and $44. There is **no published eBay-specific A/B test**. The eBay-specific reason to price at .99 is different and solid: buyers set max-price filters at round numbers, and a $30.00 item is excluded from an "under $30" filtered result set.

**Free shipping is fee-neutral.** Both FVF and the ad fee are charged on item + shipping + tax either way, so rolling $10.86 into the price costs nothing extra in fees. What changes: you enter the free-shipping **hard filter** (eBay: >80% of customers look for free shipping; separately, **71% of eBay purchases ship free** `[observed — redstagfulfillment, not an eBay figure]`), the "+$10.86 shipping" line disappears from the tile — and the displayed price rises $10.86, which may push you out of a lower price-filter bucket. Currently $10.86 is **31–43% of item price** on a $25–35 item. If free shipping is adopted, `EBAY_SHIP_CHARGED` must be set to 0 or the profit floor is wrong by the full postage.

### Price & offers checklist
- [ ] Model take as `0.205 × (price + shipping) + 0.40`. Flag any listing where take > 30% of item price (trips below ~$28).
- [ ] No standalone single-quantity listing under $25 at ~$10 postage. Bundle, make multi-quantity, or don't list.
- [ ] `[manual — needs Terapeak]` Price ≤ **1.15×** the median of 90-day sold comps (same brand + type + size + condition). Flag <0.75× as leaving money on the table. There is no API path to sold comps (§6 data table).
- [ ] Price ends .99 and sits **just below** $25 / $30 / $40 / $50 / $75 / $100. Flag $30.00–$30.99, $50.00–$50.99 etc.
- [ ] Auto-decline set to the computed dollar floor, ±$2. Flag any flat-percentage auto-decline. **When no receipt exists the floor is computed from `ASSUMED_COST_RATIO = 0.29` and is an estimate, not a limit** — `profit.breakeven_price` returns `None` rather than guessing, and repricing refuses to cut.
- [ ] Best Offer enabled where `days_live > 14 AND watchers ≥ 1 AND sold == 0` (not as a ranking tactic — as a conversion path).
- [ ] Send offers where `days_live ≥ 14 AND watchers ≥ 1 AND post-discount net ≥ $15`. **Gate on watchers, not on margin ratio** — this account's config previously excluded its best listing (9 watchers, 5 units, unsold a month) for having a 41% rather than 50% margin, while offering on two quiet duvets.
- [ ] **Offer discount 8–15%** everywhere (§6 Stage D uses the same band). eBay hard-rejects <5%; <8% is below the observed converting band `[observed]`. Reserve **15–20% only for the stale tail** (≥90 days, zero sales). Note the config's flat dollar discounts break this band at the bottom of each bracket — `OFFER_DISCOUNT_SMALL=$5` is 20% at $25, `OFFER_DISCOUNT_LARGE=$10` is 25% at the $40 threshold; see the config reconciliation in §6.
- [ ] Track offers-sent per listing against watcher count; if `offers_sent ≥ watchers` the listing is exhausted, don't retry. Offers older than 96h have expired.
- [ ] Revise price in place. Never end-and-relist. Flag any SKU whose item_id changed.
- [ ] ≤2 price changes per 30 days per listing. Each cut fires a watcher alert; a stream of them teaches watchers to wait.
- [ ] Markdown / sale events (visible strikethrough + Sale badge **in search results**) for the stale tail at 10–25% off. Below 10% buyers don't register the strikethrough. **Requires a Store subscription** — see §5. `[contested, low confidence]`: blogs claim the price must hold ~14 days before the event or the strikethrough won't render; eBay has also shipped bugs where it renders on the item page but not in search. Verify empirically.
- [ ] Volume Pricing applies **only** to multi-quantity listings of the same item. For a single-quantity catalogue the lever is an **order-level discount** — the only thing that dilutes the $0.40 order fee and lets two items share one label.

---

## 5. Promotion, Store, Returns & Status

**The January 2026 attribution change is the central fact.** From 13 Jan 2026 (US/CA), a Promoted Listings **General** ad fee is charged when a buyer purchases the promoted item *"from a general ad that ANY buyer clicked on in the most recent 30 days"* — the window resets on every new click. The person who buys need never have seen an ad. UK/EU/AU sellers who got it in 2025 saw attributed share jump from ~30–40% to **80–90%+ with no increase in sales**; one US Top Rated seller reported 100% attribution starting exactly 13 Jan 2026 ([valueaddedresource.net](https://www.valueaddedresource.net/ebay-promoted-listings-ad-attribution-us-canada-2026/)). **This account's own data confirms it:** $53.17 billed on $1,066.78 = **5.0% effective against a 4% nominal rate**; since the fee base includes shipping and tax, 4% × ~1.07 ≈ 4.3% would be full attribution, so 5.0% is at or above it. **The dashboard's "attributed sales" number now carries no information about incrementality.**

**General campaigns lost the top-of-search slot in January 2026** `[observed — single blog (Frooition), no eBay announcement]` — it is said to be exclusive to Priority (CPC). Rate range 2%–100% (minimum raised from 1%). eBay's default suggestion often lands at **12–15%**, which exceeds the entire net margin on a $45 clearance flip.

**eBay's own numbers, useful as an experiment baseline:** *"over 100% more impressions"* when promoting with a general strategy vs non-promoted, Jan–Mar 2026 data — **impressions, not sales**. eBay explicitly does not promise a higher rate buys a better position, only that the listing becomes *"eligible to show more often and in more places."* Suggested rates are recalculated **daily** by ML from competitor ad rates, category volume and traffic trend, listing attributes, listing quality, and the listing's own past impressions/clicks/sales ([community announcement](https://community.ebay.com/forum/announcements-57928/topic/promote-with-confidence-a-deep-dive-into-suggested-ad-rate-8968/)). **A rising suggested rate on untouched content is a market signal, not a listing signal** — and June 2026's "Easy Boost" (one rate, all listings, one tap) is inflating rates catalogue-wide `[observed, low confidence]`, so compare against your own catalogue median.

**eBay's three-outcome reading of a promotion** — this is eBay telling you how to run it as an experiment:
1. Sells with more impressions/clicks → working.
2. More visibility, **no sale** → a listing-quality/price problem, not a visibility problem. **Stop paying.**
3. No visibility lift at all → you're losing the ad auction; category is saturated or the listing isn't competitive.

**Break-even.** Under any-click attribution the ad fee lands on *every* unit, so the extra units have to carry the ad cost on the baseline units too:
```
required_lift = 0.05 × (price + 10.86) / (0.795 × price − 1.856 − cost)
                └── ad cost per sale ──┘   └──── net_PROMOTED (§4) ────┘
```
At this account's economics (cost = 0.29 × list): **~17% at $25, ~14% at $40, ~12% at $70 — call it ~14% extra units just to pay for itself.** Below that, ads are a pure margin transfer. **Only a holdout measures this**: unpromote a random half of the catalogue for 45+ days (30-day attribution window plus buffer) and compare units/listing/week.

**Rate cap** — caps ad spend at 10% of what the listing would have netted unpromoted:
```
max_ad_rate = 0.10 × (0.845 × price − 1.313 − cost) / (price + 10.86)
```
At $40 that's 2.09/50.86 ≈ **4.1%** — the config's `EBAY_DEFAULT_AD_RATE_PCT = 4` is at the cap, not conservative. Never accept Dynamic rate. `[observed]`: home-textile keywords ("bath towel set") reportedly need >10% to place — for that category the honest answer is *don't promote*, not *pay more*.

**Never promote a multi-quantity listing without recomputing:** one click makes every subsequent unit sale chargeable for 30 days, restarting on each new click. Multi-quantity sellers were hit hardest by the change.

**Store subscription** `[contested]`. At 110 listings you're inside the **250 free listings/month** allowance ($0.35 each thereafter), and Basic Store at $21.95/mo (annual) buys ~0.5–1pp of FVF ≈ $5/mo at 11 sales — a net loss on fee arithmetic alone. **But** markdown/sale events require a Store, so decide on the sale-event value. **Re-run this decision the moment active listings exceed ~250:** GTC listings re-charge insertion monthly, so at 300 listings you pay 50 × $0.35 = **$17.50/mo** in insertion fees against the $21.95 subscription *plus* its FVF discount — the recommendation inverts to roughly break-even and improves from there.

**Returns.** eBay's recommended windows are 30 or 60 days. Returns policy is inside named Best Match factor #6 (terms of service), and **"Free returns" is a hard search filter** — without it the listing does not appear in that filtered result set at any rank. The economics are routinely computed wrong: **INAD returns are seller-paid regardless of your stated policy.** Offering free returns therefore adds only the *remorse*-return labels on top of a cost you already carry — it is not "a label per return", it is "a label per remorse return". An INAD additionally becomes a **transaction defect** (feeds the Top Rated gate) and feeds the peer-benchmarked service metric, whose **Very High** rating adds **up to 5% of FVF** in that category per month and withholds the TRS+ discount. Getting measurements into the description (§3.5) is the cheapest return-rate lever available.

**International reach `[unverified — check live]`.** eBay International Shipping — successor to the retired Global Shipping Program — is for a US seller an opt-in that exposes the whole catalogue to an international buyer population at **zero incremental handling**: you ship domestically to eBay's hub and eBay assumes responsibility from there. For a catalogue turning **1.2× per year** (§7) whose only stated growth paths are "more listings" or "higher ASP", switching on an extra demand pool is plausibly a larger lever than any title rewrite in §1. **Verify before enabling:** the programme's current 2026 name, opt-in mechanics and fee structure could not be confirmed, and no research pass covered international at all — this is a gap, not an established rule.

**Top Rated Plus** `[contested]`. Requires per listing: same/1-business-day handling **and** 30-day-or-longer **free** returns. Benefits: seal shown *"prominently in search results and in the listing description"* + **10% off the percentage portion of the FVF** (not the per-order fee). Carve-out: in **Jewelry & Watches and most Collectibles & Art, 14-day free returns** earns the fee discount **but not the seal**. Economics at $45 ASP: $0.61/sale if you compute 13.6% on item price alone — but three of four categories here are at **15%** and the fee base includes the $10.86 shipping, so the real figure is **~$0.84/sale ≈ $9/month at 11 sales**. Against that, free returns costs only the *remorse* labels (~$5–6 each), not all of them. **Break-even is roughly one remorse return per 11 sales** — a much easier bar than the naive version, and it ignores the free-returns hard filter entirely. One researcher says do it unconditionally; another says test it. **Count your remorse returns specifically, not all returns, before deciding.**

**Account gates, checked monthly on the 19th (evaluation is the 20th):** account age ≥90d; ≥100 transactions **and** ≥$1,000 US sales in 12 months; defect rate ≤0.5% **and** from ≤3 unique buyers; cases closed without seller resolution ≤0.3% **and** ≤2; late shipment ≤3% **and** ≤5; tracking uploaded within handling time and carrier-validated on ≥95%.

**Below Standard** = transaction defect rate **>2%** (and only when defects involve **more than 4** different buyers), **or** cases closed without seller resolution above **2 / 0.3%**. **Late shipment rate on its own can never trigger Below Standard** — eBay: *"A high late shipment rate on its own won't cause your account to be evaluated as Below Standard, but a low rate is required for Top Rated status."* It bites only through the Top Rated gate (≤3% and ≤5 late shipments). `[contested]`: a **7% late-shipment Below Standard threshold** circulates widely and traces to no eBay page — **do not act on it**; re-check [id=4347](https://www.ebay.com/help/policies/selling-policies/seller-standards-policy?id=4347) live before treating late shipments as an account-level risk.

**Handling time ≤1 day on every listing** — it's inside eBay's named Best Match factors, a TRS+ gate, it drives the estimated delivery date in the search tile, and the LQR literally issues "Reduce handling time to 1 day" as a recommendation. This is a business-policy fix applied to all listings at once.

---

## 6. DIAGNOSTIC: impressions → views → watchers → sales

**This is the section that matters.** It turns "this listing is bad" into "eBay never showed this to anyone, because X."

### What data you can actually get

Several rules below need inputs that are **not** in the stated audit input set (title, specifics, photo files, price, category, traffic stats). Mark them in the worklist rather than pretending they automate.

| Rule | Needs | Available? |
|---|---|---|
| §4 price ≤1.15× median 90-day sold comps; §7 Tests 1–4 | Sold comps | **No.** Marketplace Insights API is restricted-access (application required); Browse API returns no sold data. Terapeak is UI-only → `[manual — needs Terapeak]` |
| Stage A `top20_share`, `non_search_impressions` | Placement split | **No API path.** `getTrafficReport` exposes `LISTING_IMPRESSION_SEARCH_RESULTS_PAGE` / `_STORE` / `_TOTAL` — **not** top-20 vs rest-of-search. Per-item Seller Hub CSV only → `[manual]` |
| Stages C & D | Watcher counts | **Not in `getTrafficReport`.** Trading API `GetMyeBaySelling` / `GetItem` required |
| §4, §6 | `days_live`, `offers_sent`, item_id history | Local DB only — unavailable on a first run |
| §4 auto-decline floor, §5 rate cap | Per-item COGS | Receipt if captured, else `ASSUMED_COST_RATIO = 0.29`. **The floor is then an estimate, not a limit** |

### Read the definitions correctly — the two metrics don't chain
- **CTR = eBay page views ÷ impressions, EXCLUDING external page views.**
- **Sales conversion = quantity sold ÷ TOTAL page views, INCLUDING external.**
Different denominators, by eBay's own definition ([Seller Hub docs](https://export.ebay.com/en/services-tools/seller-hub/monitoring-your-business-in-seller-hub/)). Off-eBay traffic inflates views and conversion's denominator but never CTR.

**An "impression" is not "someone saw it."** eBay counts one every time a link to the listing is on a page the buyer loaded, *including results they never scrolled to*. That is why eBay CTRs are an order of magnitude below ad-industry CTRs. Impressions split three ways: **Top-20 search spot**, **Rest of search (21+)**, **Non-search** (Similar items / People-who-viewed modules on other listings' pages). Since a **November 2024 redesign the aggregate placement split was removed from the Seller Hub UI** and exists **only in the per-item CSV** — it is the most diagnostic column eBay publishes and most sellers stopped looking at it.

### Sanity-check the data before diagnosing anything
- Organic impressions and views **vanished entirely** from Traffic Reports for US/UK/CA sellers on 18 Oct 2025, apparently fixed 19 Oct, broke again 20 Oct — eBay never acknowledged it and its status page stayed green ([valueaddedresource.net](https://www.valueaddedresource.net/ebay-organic-impressions-views-missing-traffic-reports/)). PL sales have also been reported as organic. Item view counts showing 0 is a recurring known glitch.
- **Rule: require ≥7 consecutive days of non-zero *account-total* organic impressions.** Any day where the account total is 0 while other days aren't is invalid — exclude it from every per-listing calculation.
- Report lags 24–48h. A listing under 24h old has not been indexed yet and cannot be scored.
- Judge on a **rolling 30-day window**, never lifetime totals. A GTC listing with 4,000 lifetime views and 12 in the last 30 days is dead, and lifetime totals hide it from every threshold. **Exception: CTR, which needs 90 days (below).** `getTrafficReport` (Sell Analytics): max 90-day range, earliest start 2 years back, `dimension=LISTING` returns up to 200 listings without a filter — a 110-listing catalogue fits in one call.

### Expected magnitudes for this catalogue

| Metric | Realistic range | Source |
|---|---|---|
| Page views / listing / 30 days | **3–15** (clothing sellers reported 4.8, 6.8, 3.4, 11.9, 1.1, 6.6) | seller-reported, ~2022, possibly stale |
| Impressions / listing / 30 days | ~300–2,000 | derived |
| **CTR** | **0.2–1%** | 0.32% ranked **4th of 655 sellers** in one category's LQR |
| Sales conversion | platform ~1.35%; typical 1–5%; well-optimised 4–7% | [sellbrite](https://www.sellbrite.com/blog/ebay-sales-conversion-rate/) |
| Watcher → buyer | **2–5%** fixed-price | `[observed, low confidence]` |
| Top-20 impression share | ~18% healthy vs 3.7% struggling | `[observed, low confidence]` |
| Monthly seller STR (units sold ÷ active listings) | **3–12%**, 10% is a good target | eBay Community self-reports |

**These magnitudes are what make the thresholds below what they are.** Any rule requiring ≥50 views or ≥10 watchers in 30 days is unreachable at 3–15 views/month and will never fire — that is why Stages C and D are cumulative, not windowed.

**⚠ The single biggest disagreement in the research is the CTR threshold.** One researcher proposed flagging CTR <1%; a published third-party ladder uses <2%; a third traced real eBay CTRs to 0.2–1% and concluded blog benchmarks are imported ad-industry numbers. **Resolution: benchmark against your own account median, not an absolute.** Flag a listing below **0.5× the account median CTR**. Use **0.15%** as the only absolute floor.

**The CTR sample-size gate — the most important number in this section.** At the magnitudes above (~600 impressions, ~6 views per 30 days) the CTR numerator is a **count of 6**. The Poisson 95% interval on 6 counts is roughly **[2.2, 13.1]**, i.e. a true CTR anywhere from **0.37% to 2.2%** on the same listing. A single listing's 30-day CTR **cannot distinguish "half the median" from "the median"**, and a naive Stage B would fire on noise for roughly a quarter of the catalogue every month.
**Rule: do not assign Stage B until the listing has ≥1,500 cumulative impressions.** `getTrafficReport` accepts a 90-day range — use **90 days, not 30, for CTR specifically**. Below 1,500 impressions label the listing `insufficient data` and leave it in Stage A's worklist. Compute the account median over listings that clear the same gate.

**Second disagreement: the impressions floor.** Proposals were <50 organic search impressions/30d, <100/30d, and <100/7d. **Use: <100 impressions in 30 days = hard visibility failure.**

### Config reconciliation — RESOLVED against this account's own 194 snapshots

Everything above was written from published sources and third-party benchmarks. This section was then checked against `listing_stats` — 194 real weekly snapshots of this account — and **the measurements overrode the sources in two places out of three.** Where they disagree, the measurements win; this table is the authority, not the estimates elsewhere in this document.

**What this account actually measures** (194 weekly snapshots, `get_traffic_report` window = 7 days):

| Metric | min | p25 | median | p75 | p90 | max |
|---|---|---|---|---|---|---|
| impressions / week | 0 | 87 | **185** | 344 | 627 | 1,729 |
| views / week | 0 | 1 | **3** | 6 | 11 | 40 |
| watchers | 0 | 0 | **1** | 1 | 3 | 9 |
| CTR (views ÷ impressions) | 0.00% | 0.53% | **1.49%** | 2.75% | — | 16.95% |

| Config | Was | Now | Why |
|---|---|---|---|
| `REPRICE_MIN_IMPRESSIONS` | 50 / week | **50 — unchanged** | ⚠️ **This document's earlier recommendation (lower to 23) was wrong for this account.** It assumed ~600 impressions/month; the measurement is ~800/month median. At 50 the gate fails 23 of 194 snapshots (12%) — the intended rate. Lowering it to 23 would have made `INVISIBLE` nearly unreachable. |
| `REPRICE_MIN_CTR` | 0.01 | **0.0075** | Half this account's measured median of 1.49%, per the relative rule in §6. The absolute 0.0015 floor proposed above is far below anything this catalogue produces and would never fire. |
| `REPRICE_MIN_VIEWS_FOR_SIGNAL` | 15 / week | **40 cumulative** | ✅ Confirmed, and worse than described: 15/week was reached by 13 of 194 snapshots, and **`OVERPRICED` had never fired once in the account's entire history.** Now summed across every recorded week in `evaluate_item`. |

**The CTR field itself was the real defect, and it is not a threshold problem.** `_diagnose` compared `REPRICE_MIN_CTR` against eBay's `CLICK_THROUGH_RATE` metric, which **does not agree with the `LISTING_VIEWS_TOTAL` and `LISTING_IMPRESSION_TOTAL` returned in the same response**: `LOW_CTR` fired on listings whose views ÷ impressions worked out at 3.39%, 3.64% and 4.60% — several times the account median — while the digest printed those same impressions and views right beside the verdict. `ebay/analytics.py`'s module header already warned its metric-key mapping had never been confirmed against a live response. **Fixed by deriving CTR locally from the two integers** (`reprice._ctr`), which are unambiguous, are what the digest shows, and are what `listing_stats` stores. Do not restore the eBay field without confirming what it is measured against.

**Also fixed while verifying:** `stats_history(limit=1)` was returning the *current* week's row on any re-run inside the same week, because `record_listing_stats` upserts on `(item_id, week)`. The "watchers not shrinking" test was comparing a reading against itself.

**The `[open]` ad-fee discrepancy is RESOLVED — the code was right and this document was wrong.** `report.py`'s reconciliation computes the effective ad rate as `account_charges ÷ sold_revenue` where `sold_revenue = Σ(unit_price × qty)` — **item revenue only, no shipping**. So the measured 5.0% already absorbs the shipping component of what eBay bills, and `profit.project`'s `ad_fee = EBAY_AD_FEE_PCT × revenue` reproduces the real charge exactly. Re-basing it onto price + shipping, as §4's solved forms do, would apply a rate calibrated on one denominator to a 13.1% larger one (measured: $1,353.82 item revenue against $177.82 shipping collected across 17 sold rows) — making the floor **$0.54 pessimistic**, not fixing a $0.54 optimism. The sign was backwards. §4's solved forms are the approximation here; `profit.py` is the reference.

⚠️ One real caveat on that constant: because 5.0% is calibrated against a roughly fixed ~$10.86 shipping charge, it over-charges cheap items and under-charges dear ones. It is an average, correct at this catalogue's ASP, and drifts at the extremes.

**Offer discounts — structurally right, magnitude unverified.** `OFFER_DISCOUNT_SMALL` = $5 flat below $40 is 20% at $25; `OFFER_DISCOUNT_LARGE` = $10 at ≥$40 is 25% at the threshold. A flat dollar discount is a wildly varying percentage, and that criticism stands on arithmetic alone. But the 8–15% "converting band" it is measured against **has no source** (the research pass said so explicitly), so this is flagged, not changed. `_offer_plan` already refuses anything under eBay's 5% minimum, which is the only hard constraint that exists.

### The ladder — assign exactly one stage per listing

**Stage 0 — SUPPRESSED (impressions ≈ 0, listing >24h old).** Not a content problem. Rule out mechanically, in this order:
1. Quantity is 0 with Out-of-Stock Control on — the GTC listing **stays live but is completely hidden from search** until quantity >0. (And eBay **auto-ends** a listing that sits at qty 0 for 90 days, destroying its item ID, URL, watchers and sales history.)
2. A near-duplicate live listing from you in the same category — near-identical fixed-price listings **may be hidden from search without warning**. Fuzzy-match title (token-set >90) + same category + price within 10%.
3. Account is Below Standard → every listing demoted. Check once per audit; if it fails, downgrade **all 110** listing-level findings to informational and fix the account.
4. Policy/compliance hold — apparel Size non-compliance is the live one this month. Also check for a silent VeRO removal (§1): a listing that disappeared with no metric explaining it.
5. The traffic report itself (see sanity check above).
**Never apply a content fix at this stage.**

**Stage A — VISIBILITY / RETRIEVAL failure. `impressions_30d < 100`.**
The listing is not entering the candidate set for any query. **Fix: title, item specifics, category. Do not touch price** — price cannot fix a retrieval failure, and cutting it destroys margin for nothing. Every `insufficient data` listing from Stage B's gate also works here.
`[manual]` Sub-diagnostic from the per-item CSV (no API path): if `non_search_impressions / total > 0.5`, eBay is only showing the item as an accessory in "people who viewed this" modules, never as an answer to a query → **keyword matching failure**, rewrite title + aspects. If `top20_share < 5%`, it's ranked deep → relevance/ranking. If `top20_share > 15%`, ranking is fine and any failure is downstream.
**Counter-check first:** if required *and* ≥7 recommended aspects are already filled and impressions are still bottom-decile, suspect **wrong category**, not thin specifics. Run `getCategorySuggestions(title)` and compare required-aspect counts.

**Stage B — CLICK failure. `impressions_30d ≥ 100` AND `cumulative_impressions ≥ 1500` AND `CTR_90d < 0.5 × account median`.**
Buyers see the tile and reject it. The tile contains exactly four things. Fix in this order:
1. **Photo[0]** — square crop, ≥1600px, single item, plain background, bright, no text/border.
2. **`title[:40–50]`** — must carry brand + head noun. Flag titles whose first 40 chars are filler like "NEW WITH TAGS" or "FREE SHIPPING".
3. **Price + shipping as displayed** — including whether "+$10.86 shipping" is sitting next to it.
4. Delivery estimate (handling time).
**Do not do keyword work here** — the listing is already being retrieved. And note clicks are themselves a positive Cassini signal that counts *without* a sale, so a listing accumulating impressions with no clicks is earning a **negative** ranking signal, not sitting neutral.

**Stage C — PAGE failure. `cumulative_views ≥ 40` AND `watchers == 0` AND `days_live ≥ 30`.**
They clicked, then left. Something on the item page killed it. Audit: photo count and detail shots; item-specifics gaps that create doubt (Size, Material, measurements, condition); a description with no measurement block (§3.5); `shipping_cost / price > 0.25`; returns not accepted. For Ross clearance apparel and bedding the classic killers are missing size/measurement/material and a high shipping charge — the buyer can't tell if it fits or what it's made of.

**Stage D — PRICE / TRUST failure. `watchers ≥ 5` AND `sold == 0` AND `days_live ≥ 30`.**
Demand exists and the offer is nearly acceptable. Fix: **send offers to watchers first (8–15% off, one shot per watcher, free)**; only then reprice to at/below the median of 30-day sold comps, via a **markdown sale event** rather than a silent edit, so you get the search-results Sale badge for the margin you're giving up.
**Confidence note, not a gate:** at a 2–5% watcher→buyer rate, 5 watchers and no sale is the expected outcome (expected buyers ≈ 0.1–0.25) and is weak evidence about price. Treat the price conclusion as low-confidence below ~10 watchers and prefer the free lever (offers). A gate at 10 watchers is not usable here — at 3–15 views/month it needs roughly 7–13 months to accumulate, and the ladder would have no reachable price arm at all.

**Stage E — DEMAND / patience. Impressions, CTR and conversion all normal, no sale.**
Check category sell-through in Terapeak before touching anything (§7). At ~33% average monthly sell-through, a single-quantity $25–70 item is *statistically expected* to take ~3 months.

### The age question — the third research disagreement
One line of analysis says 90 days with no sale = failed listing. Another points out that at a 10%/month seller STR the **average** listing takes ~10 months, so **roughly half a healthy 110-item inventory will be 90+ days old** and age-based culling would destroy it.

**Resolution: 90 days is a review trigger, not a cull trigger.** Cull on **engagement**, not age: zero views AND zero new watchers AND zero offers AND zero messages over **8 consecutive weeks**, with age ≥80 days.
**And be precise about what "revising doesn't help" means:** polishing a zero-engagement listing's *description or photos* is wasted — you're optimising something buyers are not seeing. Rewriting its **title, aspects or category is not wasted**: that is exactly the retrieval repair Stage A prescribes. Revise the fields that determine whether the listing is retrieved; leave alone the fields that only matter once it is.
- End-and-relist is permitted **only** when age ≥90d, sales 0, watchers 0, **and** you are materially changing title, gallery photo or price at the same time. Wait 48–72h between End and Sell Similar. Blocked outright on any listing that has ever sold a unit.
- **Portfolio metric instead of per-listing panic:** bucket all listings 0–30 / 31–60 / 61–90 / 91–180 / 180+ days. Raise an alarm only when the **180+ bucket exceeds ~25%** of active listings.
- **Hard cut-loss:** 12 months live, 0 sales, <20 cumulative views → liquidate (auction, bundle, donate). Holding it inflates your own STR denominator and distorts every subsequent sourcing decision.

### Free eBay diagnostics you should be reading
- **Listing Quality Report** (Seller Hub > Performance > Traffic or Summary; Excel download). Deprecated 28 Feb 2023, re-released to **all sellers ~October 2025**. Covers your **10 categories with the most live listings**, last **31 days**, benchmarked against *"what the 10% most successful sellers are doing"*, structured as **Impressions → CTR → Conversion** with a rank against every seller in the category. Recommendations are literal ("Fill in 7 recommended item specifics", "Reduce handling time to 1 day"). **It excludes variation and non-BIN listings — audit those by hand.** Data refreshes every 24h, changes show ~2 days later.
- **Google Shopping Rejections tab** in the LQR — the only place a photo/GTIN defect produces a hard machine-readable flag. Common causes: watermarked or text-overlaid images, images <500×500, missing UPC/GTIN. A rejected listing silently loses its entire off-eBay traffic channel.
- **Active Listings Quick Filters** for missing Required / Required Soon / Recommended item specifics, plus the Overview > Tasks module. This is the closest thing eBay has to a "why isn't this getting impressions" diagnostic, and the bulk **Add item specifics** tool clears it across many listings in one pass. **Cross-check independently** — listings where "Don't remind me again" was ticked are missing from these filters.
- **"Sell it faster"** on Active Listings — almost entirely Send-Offer suggestions. Correct at Stage D, meaningless at Stage A (no impressions → no watchers → nothing to offer). Triage, don't execute.
- **Product Research / Terapeak** (free in Seller Hub > Research) — see §7. This is the only source for every `[manual — needs Terapeak]` rule in the table above.

---

## 7. Is it a genuinely bad item?

Some listings cannot be fixed because the *thing* is wrong. Ross/TJX sells brand-name closeout **and** goods manufactured specifically for the off-price channel ("made for outlet") with lower-grade construction to hit a price point. **Made-for-outlet SKUs never existed at full retail, so they have no MSRP anchor and frequently zero eBay sold comps.** This is the single biggest source of the genuinely-bad-item problem in Ross arbitrage — and, via §1, the biggest source of VeRO exposure.

**Tests 1–4 are all `[manual — needs Terapeak]`.** There is no API path to sold comps; do not schedule them as automated checks.

**Test 1 — Sold comps.** Search `Brand + type + key attribute` with `LH_Sold=1&LH_Complete=1` over 90 days. Zero? Re-run over Terapeak's **3-year** view. Still zero → **the item is un-priceable.** For mass-market apparel and home textiles, rarity is not a value driver. Auction it, bundle it, or don't list it. Never price from **active** listings — a bag listed at $120 that never sold tells you nothing.

**Test 2 — Brand identity.** Reject `Unbranded / No Brand / Handmade / Does Not Apply / blank` and Ross house labels. Then check the brand has **≥~50 sold listings site-wide in 90 days**. A brand with no eBay identity has no query that reaches it. And *STR without absolute volume is meaningless*: as one seller put it, there is a *"big difference between 120,000 Ralph Lauren shirts sold and 357 Duck Head shirts sold even though both may be 49% sell through."*

**Test 3 — Saturation.** `active listings ÷ units sold in 90 days`: **<5 healthy · 5–10 moderate · >10 warning · >20 avoid.** (Worked example: "iPhone 14 case" ≈ 850 active ÷ 38 sold = 22.4.) Alternative crude gauge: <200 active for a search term = manageable; **2,000+ = saturated commodity** `[low confidence]`. A listing with no brand token *and* no model token is a commodity — you are competing on price against sellers with $2 landed cost.

**Test 4 — Category demand (separates "my listing is bad" from "nobody wants this").** Terapeak market STR: **>50% healthy · 20–50% average · <20% weak**. `[contested]` — one experienced seller analysing hundreds of items found real Terapeak numbers cluster far lower (low performers ≤10%, mid ~15%, high ≥20%), so the blog bands may be inflated. **Critical caveat: Terapeak computes STR over a 90-day window only**, so it is structurally blind to seasonal items whose season is outside that window. Use the 3-year view for anything seasonal. **Never mix Terapeak STR (sold ÷ sold+unsold+active, capped at 100%) with third-party tool STR (units ÷ active, which produces 635%, 3,794%, 19,707%)** — they are not comparable.

**Test 5 — Margin floor at source.** Experienced resellers filter on **40–50% net margin AND $10–15 absolute profit**, both. The "3× rule": `median sold × 0.87 − shipping − cost` should exit at 3× cost on a portfolio average; below that *"your sourcing edge is gone."*

**Test 6 — Shipping weight.** Reject bulky home textiles under **$35** — comforters, quilts, duvet sets, curtain panels, blanket sets. USPS Ground Advantage via eBay Labels runs ~$4.50 (1 lb, zones 1–4) to ~$6.10 (zones 7–8), 3–5 lb rates rose 25 July 2026 with a further eBay Labels update 8 August 2026, and carrier peak surcharges run late Sep–mid Jan. A 5 lb comforter in a large box eats the entire spread. Home is ~35% of the TJX floor, which is exactly why this trap is easy to walk into.

**Test 7 — Structural headwinds.** Nothing sourced at Ross reaches eBay **Authenticity Guarantee** thresholds (handbags $500+ luxury only; watches $2,000+; sneakers $100+), so there is no authentication trust halo. And eBay Q2 2026 (GMV $22.4B, +14% organic FX-neutral, 136M active buyers) reported fashion growth *"predominantly led by luxury and pre-loved inventory"* — **the tailwind is not in new mid-tier mall brands.**

**Structural fixes that beat per-listing work:**
- **Consolidate.** eBay names this under *"rules that help achieve the best possible position in eBay's Best Match sort order"*: identical items into one multi-quantity listing; size/colour variants into one variation listing; multi-vehicle fitment via parts compatibility. All views, watchers and sales stack onto **one item ID**, and sales history is a ranking input. Two single-quantity listings of the same item split the signal and compete with each other. **Never mix different brands, different models, or new and used in one listing** — explicitly prohibited.
- **Watch the qty-0 clock.** eBay auto-ends a listing held at quantity 0 for **90 days**, destroying the accumulated ranking asset. Restock into the existing listing; never create a new one.
- **Seasonality** (documented anchors): sales peak Nov–Dec, revert Jan–Feb, with December front-loading into the first two weeks (USPS Ground Advantage Christmas cutoff ~17 Dec). Jan 5–15 is the fitness/gift-money window. June opens wedding season and peaks swimwear. August is back-to-school. `[low confidence]`: bedding peaks Aug–Sep (dorm) and January (white sales); handbags Nov–Dec plus a Mother's Day bump in early May; watches/bands Nov–Dec, mid-June (Father's Day), May–June (graduation). **Rotate seasonal stock on 2–4 weeks before the window, and never mark it down during its off-season based on Terapeak's 90-day STR.**

### The portfolio arithmetic — read this before optimising anything
Use §4's solved form, not `price − cost − 0.136 × price`. At **$45 ASP with cost at 29% of list ($13.05)**, promoted:
```
net = 0.795 × 45 − 1.856 − 13.05 = $20.87 per sale
```
At 5% / 10% / 15% monthly STR on 110 listings that is 5.5 / 11 / 16.5 sales = **~$115 / $230 / $345 per month**, less packaging (~$0.50/order) and ~one return per 15 sales (~$12). **Realistic band: $100–350 net/month.** (The naive `$45 − $6.12 FVF − $0.40 − $6.50 shipping − $12 COGS ≈ $20` lands in the same place by cancelling two errors — 13.6% instead of the measured 15.5%, no ad fee at all, and shipping as a $6.50 cost rather than the measured **net +$0.77** — and it breaks the moment ASP or ad rate moves. Don't use it.) External corroboration: the average US eBay seller does **$444.90/month in revenue**; realistic part-time net in 2026 is quoted at $200–500. Blog claims of "$2,000–5,000/month at 200+ listings" are inconsistent with sellers' own reported STR and should be treated as marketing.

**Capital is the hidden constraint:** 110 listings × ~$13 COGS ≈ **$1,430 tied up**, turning ~**1.2× per year** where retail arbitrage needs 3–4. The path to $1,000/month at this ASP is roughly **350–500 active listings at the same velocity, or a materially higher ASP — not a better title on the existing 110.** (At 350+ listings, re-run the Store decision in §5.) Run a **per-category P&L** (sales, revenue, actual fee rate, shipping, COGS, net, net per listing-month) and cut sourcing in any category below the portfolio median. One strong category can mask two that consume slots and capital for nothing. **International reach (§5) is the one untested lever that adds demand without adding capital** — evaluate it before adding 240 listings.

---

## 8. Tax

Absent from every research pass; verify against current IRS guidance rather than treating anything here as settled. It matters at $100–350/month net because the receipt capture already in the pipeline (`ASSUMED_COST_RATIO`, `receipt.reduced_price`) is what makes the deduction defensible.

- **1099-K reporting threshold `[unverified — must check live]`.** This figure has moved repeatedly: $20,000 / 200 transactions → $5,000 → $2,500, and the July 2025 reconciliation act is believed to have **restored $20,000 / 200**. **Do not act on any number in this document** — check the threshold for the current filing year before assuming you are under or over it. And note receiving no 1099-K does not make the income unreportable.
- **COGS is deductible — this is the whole reason receipt capture matters.** An item with no receipt falls back to `ASSUMED_COST_RATIO = 0.29`, which is a *modelling* estimate and **not a substantiated deduction**. Keep the paper. `profit.py` flags estimated costs and `report.py` colours the cell orange precisely so estimated and substantiated costs never get conflated.
- Also deductible: eBay and payment fees, shipping labels, packaging, mileage to Ross, the portion of home space used for inventory.
- **State sales tax needs no seller action.** eBay collects and remits as marketplace facilitator. It still lands inside your FVF base (§4) — which is exactly why measured FVF is **15.5%** against item+shipping rather than the 13.6% headline.

---

## Appendix: claims to actively disbelieve

| Claim | Status |
|---|---|
| "Relevance 40–50% / seller 30–40% / quality 20–30%" Best Match weighting | Fabricated. eBay publishes no weights. |
| "Cassini weights the first 50 characters more heavily" | No eBay source. Front-load for mobile truncation instead. |
| "Cassini rewards prices within 10% of median sold comp" | SEO blogs citing each other. |
| "Best Offer improves search ranking" | eBay's Best Offer docs make no visibility claim at all. |
| "Complete item specifics = 3x more likely to sell" / "36% more visibility" | Untraceable to eBay. Only published number: LQR's "fill in 7 recommended item specifics". |
| "8+ photos = 30% higher conversion" / "58% from multiple angles" / "40% more views" / "2–4x CTR" | Attributed to eBay, trace to nothing. eBay's only figure: **4.5–5%**, on a 6.8M-listing study, for a bar of merely 500px + no added text. |
| "Good eBay CTR is 1–6%" | Imported ad-industry benchmark. Real eBay CTRs are 0.2–1%. |
| "STR >70% = boost, <30% = suppression" | Sell-through is not named in any eBay ranking doc. eBay's proxy wording is "how popular the item is". |
| "Relist to get a freshness boost" | eBay's Senior Director of Search called this a myth publicly in 2025. |
| "eBay Guaranteed Delivery gives a huge visibility boost for 3-day delivery" | Dead. eGD's handling-time option was retired; the tag is now "Get it by". |
| "Watermarks are OK at ≤5% area / 50% opacity" | Stale eBay page. Live policy is a total ban. |
| "Best Offer counteroffers expire in 24 hours" | 96 hours in US/UK since 24 March 2026. |
| "12 free photos" | 24, since 2022. |
| "Late shipment rate above 7% makes you Below Standard" | Traces to no eBay page. eBay says late shipment alone **cannot** cause Below Standard. `[contested]` |
| "Keywords in the description help search" | Descriptions are outside the default index (Advanced Search's opt-in checkbox proves it) **and** stuffing them breaches id=4243. |
| "Auction listings rank better / get a visibility boost" | Auction and fixed-price are ranked separately then interleaved. No inherent advantage. |
| "Free returns costs you a label on every return" | INAD returns are seller-paid regardless of policy. Free returns adds only *remorse* labels. |
| "30/30/30/10" and "60% of eBay auctions don't sell" | Undated, category-blind, auction-era. |
| Great Price badge is worth chasing | Requires new or certified-refurbished condition matched to the catalog with identifiers — structurally unavailable to used clearance stock. |
