# Price objections — watch bands and watch, 16 Aug 2026

Kept for reference. **No prices were changed**; the seller declined. This is the
evidence as it stood on 16 Aug 2026, so it can be re-checked rather than
re-researched.

## Why this is worth revisiting

All seven items were priced `manual`, above the machine suggestion — including
the two where the machine had **solid** evidence:

| item | machine | evidence | listed | override |
|---|---|---|---|---|
| `dbc5c441` | $56.99 | solid, sold median $67 | $89.99 | +58% |
| `45654c28` | $16.99 | solid, sold median $18.95, n=10 | $29.99 | +76% |

The likely cause is documented separately below: `identification.price_evidence`
carried MSRP figures mislabelled as readings off the physical Ross tag, and
those inflated anchors are what the manual prices were set against.

## Per-item

### cbfaefec — kate spade KSS0131SET bumper + strap set, $89.99, cost $19.99
**Strongest objection of the set.**
- The stored "sold comps" behind its $36.99 machine price are Kate Spade
  **handbags** ($37.07 satchel, $37.77 Lola, $41.99 Hudson) — not this product,
  not even this category.
- No sold record found for the exact set on any reachable marketplace.
- The only active ask for the exact set is **$79 on Poshmark, unsold**.
- Sibling sets did sell at $80–$115 — but those are *acetate* and *stainless*,
  retailing $148–$180. This is the glitter-jelly line: case retails $38, band
  ~$68.
- Sold separately, glitter-jelly components go for **$3–$20** (case, n=5,
  median $10) and **$12–$58** (band, median ~$30).
- Suggested band was **$20–$40**, i.e. roughly $44.99 as a defensible top.
- Breakeven is $24.20. At true market this item barely clears — the $19.99 buy
  was the mistake as much as the price.

### ece35ff3 — Michael Kors MK4734, $129.99, cost $49.99
- An identical brand-new unit **sold at $99.89 and the same listing is still
  live at $99.89**.
- Michael Kors sells it direct at $159, and has run it at ~$125.
- The nine other "sold comps" the pipeline used are MK handbags, ballet flats,
  a dress and eyeglass frames — which is why its machine price came out $32.99.
  Both $32.99 and the $49.43 median are garbage; only the $99.89 is real.
- Suggested $104.99. Nothing sells used above new retail.

### 4e7f8068 — kate spade KSS0089 pavé scallop link, $109.99 × 2, cost $19.99
- Poshmark sold median **$90** (n=5: $50 / $85 / $90 / $110 / $135).
- No eBay exact sold comp exists — the pipeline logged `no usable sold comps`.
- Highest Kate Spade metal band in its own comp set sold at $86.60.
- MSRP $148 (Nordstrom / Watch Station), $129 (katespade.com). Discontinued and
  sold out at both, which genuinely supports a premium over KSS0067.
- Suggested $94.99.

### dbc5c441 — kate spade KSS0067 scallop link, $89.99, cost $19.99
- **Exact-model eBay sold: $76.80**, Brand New — this is the listing's own
  `price_source_url`.
- Poshmark median $71.50–$79 (n=8). Its 42/44/45mm sibling KSS0121 sold $64.00
  and $76.80.
- Still purchasable new at Best Buy and katespade.com, which caps resale.
- Suggested $74.99.
- The $20 gap to KSS0089 is directionally right (MSRP $128 vs $148); the issue
  is that both are anchored high, not that the gap is wrong.

### 45654c28 — kate spade KSS0022 leopard silicone, $29.99 × 3, cost $11.99
- **Exact-model eBay sold $18.95** NIB, plus $19.95 / $20.39 / $17.78 (n=10,
  evidence *solid*, median $18.95).
- Poshmark median $26 (n=32, NWT clusters $20–40) — Poshmark runs high on
  accessories.
- MSRP $68, confirmed on the manufacturer sticker in photo 4956.
- Suggested $24.99. Breakeven $14.14, so ~$4.81/unit of headroom against the
  eBay comp — thinnest margin of the group, and any error repeats ×3.

### ed54b249 — kate spade KSS0011 floral silicone, $29.99, cost $12.99
- **FAIR.** eBay exact sold $23.99; Poshmark median $29 (n=12, $12–$45).
- $29.99 sits on the Poshmark median, above the eBay comps. Defensible.

### c68ae768 — bebe BB-3260-RO chain link, $29.99, cost $5.49
- **NO DATA.** The single sold comp found was a bebe *watch gift set*, not a
  band. Poshmark asking $11–$33, zero sold.
- Best margin of the seven ($19.10 net) and nothing contradicts the price.
- Keep. Room to test downward later if it sits.

## Caveats that materially limit all of the above

1. **eBay was unreachable to the research agents** — 15+ attempts across
   `ebay.com`, `m.ebay.com`, item pages, browse pages and four proxies, all
   timing out or 403ing. The eBay sold figures quoted here come from the
   project's own stored comps, not from fresh scraping. Fresh eBay sold data is
   the material gap.
2. **Poshmark sold prices are the listing price at time of sale.** Privately
   accepted offers close lower, so those numbers skew high.
3. eBay generally clears accessories *below* Poshmark, so Poshmark-derived
   suggestions are, if anything, still optimistic.

## The right way to redo this

Don't re-research by hand. `/pricecheck <id>` runs the project's own sold-comp
pipeline, which is what the Apify actors exist for. That path was returning 403
on the primary token, which is why these numbers came from manual research
instead. With the `APIFY_TOKEN_BACKUP` failover added on 16 Aug 2026, re-run it
and trust that over anything here.

## Related data-integrity finding

`identification.price_evidence` contains entries labelled `source: "tag"` whose
price is not on the physical tag. Measured across the catalogue: **74 items
claim a tag-sourced price, and 8 of those have a "tag" price byte-identical to
the web MSRP** — e.g. a $148 "original price tag" where the tag reads $19.99 /
comparable value $40.00, and a $275 "MSRP on Ross tag" where the tag reads
$99.99. Those anchors drove the manual overrides above. Worth auditing wherever
`source: "tag"` appears before trusting it again.

Affected (tag price == web MSRP): `94c8a02f`, `2da4f006`, `155333d1`,
`3b078a55`, `bc15a68c`, `3c344ecb`, `4e7f8068`, `45654c28`.
