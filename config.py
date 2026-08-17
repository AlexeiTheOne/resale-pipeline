import os

COMPS_COUNT = 10
ACTIVE_COUNT = 5
UNDERCUT_PCT = 0.15

# --- Comp evidence quality (pipeline/price.py) ---
# How many raw sold rows to pull per search before filtering and outlier-trimming.
# Deliberately larger than COMPS_COUNT (which caps what's KEPT): the trim throws
# away a chunk of every fetch, and a wide fetch that survives trimming beats a
# narrow one that leaves nothing to reason about.
COMPS_FETCH_COUNT = int(os.getenv("COMPS_FETCH_COUNT", "25"))
ACTIVE_FETCH_COUNT = int(os.getenv("ACTIVE_FETCH_COUNT", "15"))
# An item's comps are "solid" evidence at or above this many trimmed comps AND at
# or below this price dispersion (p90/p10). Dispersion is the check that matters:
# a median over comps spanning 9x isn't a price, it's a coin flip — those comps
# are different products that happen to share search words.
PRICE_SOLID_MIN_COMPS = int(os.getenv("PRICE_SOLID_MIN_COMPS", "5"))
PRICE_SOLID_MAX_DISPERSION = float(os.getenv("PRICE_SOLID_MAX_DISPERSION", "2.5"))
# "Thin" evidence: usable, but shown for a human OK rather than trusted silently.
PRICE_THIN_MIN_COMPS = int(os.getenv("PRICE_THIN_MIN_COMPS", "3"))
PRICE_THIN_MAX_DISPERSION = float(os.getenv("PRICE_THIN_MAX_DISPERSION", "4.0"))
# Below this fraction of the sold median, the cheapest active listing is treated
# as a different product rather than a competitor, and does NOT cap our price.
# Without this an unrelated cheap listing drags a good comp-backed price down.
ACTIVE_FLOOR_MIN_RATIO = float(os.getenv("ACTIVE_FLOOR_MIN_RATIO", "0.5"))
# If the comp price and the research estimate disagree by more than this ratio,
# the item is flagged for a human look. The comps still set the price — research
# is a sanity check here, never a price source (an LLM resale guess priced 60% of
# past inventory and was overridden most of the time).
RESEARCH_SANITY_RATIO = float(os.getenv("RESEARCH_SANITY_RATIO", "2.0"))


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


# When true, pricing pulls only 3 sold + 3 active comps to save time/cost — fine
# for local testing, WRONG for production: it can't clear the >=3 sold-comp bar
# that pricing needs to trust comps, so items silently fall back to the research
# estimate. Defaults to False (full comps); set DEBUG_MODE=true in .env to speed
# up local runs. This was hardcoded True and is the reason real pricing has been
# running on debug-sized samples.
DEBUG_MODE = _env_bool("DEBUG_MODE", False)

# --- Auto-confirm (telegram_bot.py's advance()) ---
# When on, the pipeline clears its own identify/price gates for items where the
# evidence is strong enough, and stops only for the ones that genuinely need a
# decision. The review gate before anything reaches eBay is NEVER skipped —
# nothing goes live without an explicit approve.
AUTO_CONFIRM = _env_bool("AUTO_CONFIRM", True)
# An identification clears its gate unattended only at or above this model
# confidence (identify.py returns 0.0-1.0)...
AUTO_CONFIRM_MIN_IDENT_CONFIDENCE = float(
    os.getenv("AUTO_CONFIRM_MIN_IDENT_CONFIDENCE", "0.8"))
# ...or at or above this lower bar when the product's UPC barcode was decoded off
# the photos, which is hard evidence of exactly which product this is and worth
# more than the model's own self-assessment.
AUTO_CONFIRM_MIN_IDENT_CONFIDENCE_WITH_UPC = float(
    os.getenv("AUTO_CONFIRM_MIN_IDENT_CONFIDENCE_WITH_UPC", "0.6"))

# The grounded research step (identify stage 1). Defaults to gemini-3.5-flash:
# the 2.5 generation's search-grounding path frequently returned empty responses
# (finish_reason=STOP, zero searches) and 503s, which stalled identification; the
# newer 3.5 grounding stack is markedly more reliable on the same grounded calls.
# Override GEMINI_MODEL in .env to pin a different model.
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash")

# Thinking level for that grounded research call. Gemini 3.x flash uses "dynamic
# thinking" that, left unset, can balloon to ~160M tokens at its high level (2x+
# Gemini 2.5 flash). Combined with iterative Google-Search grounding, a single
# unbounded call churns past Google's per-request server deadline — surfacing as
# 504 DEADLINE_EXCEEDED, and as 503 UNAVAILABLE when the overrun coincides with
# load (the long-latency-then-5xx pattern we were seeing on PAID tier). Pinning an
# explicit level makes latency predictable; the web-search grounding compensates
# for shallower internal reasoning. LOW is a good balance; set MINIMAL for max
# speed or MEDIUM/HIGH if identification quality needs more reasoning.
GEMINI_RESEARCH_THINKING_LEVEL = os.getenv("GEMINI_RESEARCH_THINKING_LEVEL", "LOW").upper()

# Cheaper model for steps that only STRUCTURE data we already have (no search):
# the identify format stage and the draft writer. These never had the grounding
# flakiness that pushed research to 3.5, and 3.5-flash costs several times more
# per token — so keep these on 2.5-flash. The draft call in particular carries a
# lot of input (aspect hints), where the price gap matters most.
GEMINI_FAST_MODEL = os.getenv("GEMINI_FAST_MODEL", "gemini-2.5-flash")

# How many of the uploaded photos to send to Gemini for identification. All
# photos are still kept for the eBay listing; only the first N (the overview +
# tag close-up the user sends first) go through the paid API to save tokens.
GEMINI_PHOTO_LIMIT = int(os.getenv("GEMINI_PHOTO_LIMIT", "3"))

# How many listings may run through the identify→price→draft pipeline at once.
# Each item makes several Gemini + Apify calls; too many in parallel raises the
# odds of 429/503 rate-limit and overload errors, so keep this modest.
MAX_CONCURRENT_LISTINGS = int(os.getenv("MAX_CONCURRENT_LISTINGS", "3"))

EBAY_FULFILLMENT_POLICY_ID = os.getenv("EBAY_FULFILLMENT_POLICY_ID")
EBAY_RETURN_POLICY_ID = os.getenv("EBAY_RETURN_POLICY_ID")

EBAY_MARKETPLACE_ID = "EBAY_US"
EBAY_CURRENCY = "USD"

# --- Listing copy -------------------------------------------------------------
# The what-you-see-is-what-you-get promise, put in bold at the foot of every
# description by ebay/inventory.py — just above the SHIPPING/RETURNS boilerplate,
# so it's the last word on the item itself rather than a banner over it. It's
# added there rather than by the copywriter so it's on EVERY listing identically —
# including ones drafted before this existed — instead of being whatever the model
# felt like writing that run. /refreshdesc pushes it to listings already live.
#
# The parenthetical is deliberately hedged. A manufacturer's catalog photo is
# sometimes in the gallery and sometimes not, and it isn't reliably the last
# image, so any wording that pins it down ("the final image is...") is wrong on
# some listings. "May include" is true of every listing either way, which is what
# a promise printed on all of them has to be. Set to "" to drop the line.
WYSIWYG_NOTE = os.getenv(
    "WYSIWYG_NOTE",
    "WHAT YOU SEE IS WHAT YOU GET — you will receive the exact item photographed. "
    "(Photos may include manufacturer's photos.)")

# Promoted Listings (ads). The ad rate is a percentage of the final sale price
# eBay charges only when the item sells via a promoted placement.
EBAY_PROMOTED_CAMPAIGN_NAME = os.getenv("EBAY_PROMOTED_CAMPAIGN_NAME", "ross-auto-promoted")
# Ad rate applied automatically when a listing is published (/activate). Every
# listing is promoted at this floor rate by default; bump an individual one with
# /promote <id> <pct>. Set to 0 to disable auto-promotion. eBay accepts 2–100%.
EBAY_DEFAULT_AD_RATE_PCT = float(os.getenv("EBAY_DEFAULT_AD_RATE_PCT", "4"))
EBAY_MERCHANT_LOCATION_KEY = "ross-resale-warehouse"
EBAY_SHIP_FROM_ADDRESS = {
    "city": "Doral",
    "stateOrProvince": "FL",
    "postalCode": "33172",
    "country": "US",
}

# --- Offers to watchers (/offers, ebay/negotiation.py) ---
# A private discount to the people already watching an item.
#
# The gate is INTENT, not margin. A margin gate answers "can this item afford a
# discount", which sounds prudent and picks the wrong listings: it sent offers to
# two quiet duvets while excluding the best listing in the account — 9 watchers,
# 5 units in stock, unsold for a month — purely because its margin was 41% rather
# than 50%. Margin doesn't predict whether an offer converts; a watcher does. A
# watcher is someone who found the item, saved it, and is waiting for a reason.
#
# So: offer where people are waiting, and protect the downside with a floor on
# the money left afterwards rather than a floor on the ratio.
OFFER_MIN_WATCHERS = int(os.getenv("OFFER_MIN_WATCHERS", "1"))
# ...and only once the listing has had a fair run at its full price. Measured
# median time-to-sell here is 9.8 days, and the three fastest sales on record all
# closed inside a DAY at full price — so a watcher on a two-day-old listing is
# still mid-decision, and discounting to them buys a sale that was probably
# coming anyway. 14 days puts the listing past its own median before it's
# treated as stuck.
OFFER_MIN_DAYS_LIVE = int(os.getenv("OFFER_MIN_DAYS_LIVE", "14"))
# What one unit must still net after the discount for the offer to be worth
# making. $15 is roughly the point below which a sale stops paying for the
# handling, and one return would wipe out several of them.
OFFER_MIN_NET = float(os.getenv("OFFER_MIN_NET", "15"))
# The discount itself, in dollars. Small items get the smaller one — $10 off a
# $29.99 bag is a third of the price, which is a public price cut wearing a
# private offer's clothes.
OFFER_DISCOUNT_SMALL = float(os.getenv("OFFER_DISCOUNT_SMALL", "5"))
OFFER_DISCOUNT_LARGE = float(os.getenv("OFFER_DISCOUNT_LARGE", "10"))
OFFER_LARGE_THRESHOLD = float(os.getenv("OFFER_LARGE_THRESHOLD", "40"))
# Note shown to the buyer with the offer. Kept short — eBay caps it at 250 chars.
OFFER_MESSAGE = os.getenv(
    "OFFER_MESSAGE",
    "Thanks for watching! Here's a discount to save you a few dollars. "
    "Ships within 1 business day.")

# --- Weekly repricing (pipeline/reprice.py) ---
# A listing needs at least this many weeks of eBay search exposure before its
# traffic numbers are trusted for a diagnosis; a brand-new listing just hasn't
# had a chance to be seen yet.
REPRICE_MIN_WEEKS_LIVE = int(os.getenv("REPRICE_MIN_WEEKS_LIVE", "1"))
# Below this many impressions in the trailing week, the listing isn't being
# surfaced in eBay search at all — a visibility problem, not a price problem.
#
# Kept at 50 against 194 measured weekly snapshots of this account: impressions
# run a median of 185/week (p25 87, p90 627), so 50 fails only the bottom ~12%,
# which is the intended rate. (docs/ebay-playbook.md proposes lowering this to
# 23 on an assumed ~600 impressions/month; this account measures roughly 800, so
# that recommendation doesn't apply here. Re-derive before changing it.)
REPRICE_MIN_IMPRESSIONS = int(os.getenv("REPRICE_MIN_IMPRESSIONS", "50"))
# Below this click-through rate, buyers see it in search results but aren't
# clicking — points at the thumbnail/title/price, not comps.
#
# 0.0075 = half this account's own measured median CTR of 1.49% (179 snapshots,
# p25 0.53% / p75 2.75%). An absolute benchmark borrowed from an SEO blog is the
# wrong shape when the spread is that wide; half your own median is a listing
# genuinely underperforming ITS catalogue.
#
# Was 0.01, and reprice.py compared it against eBay's own CLICK_THROUGH_RATE
# field, which does NOT equal the views/impressions returned beside it — LOW_CTR
# fired on listings measuring 3.4%, 3.6% and 4.6%. reprice.py now computes the
# ratio itself from the two integers, so this threshold means what it says.
REPRICE_MIN_CTR = float(os.getenv("REPRICE_MIN_CTR", "0.0075"))
# Minimum CUMULATIVE views before "zero watchers" is trusted as evidence the
# price itself is the problem, rather than just low traffic.
#
# Cumulative across every recorded week, NOT a single week. As a weekly figure
# this was 15 against a measured median of 3 views/week (p90 11) — reached by 13
# of 194 snapshots, which made OVERPRICED unreachable: it has never once fired.
# 40 is still weak evidence on its own (at a 2-5% watch rate, 40 views yields
# 0 watchers by chance ~30% of the time), which is why it only ever contributes
# to a diagnosis rather than deciding one.
REPRICE_MIN_VIEWS_FOR_SIGNAL = int(os.getenv("REPRICE_MIN_VIEWS_FOR_SIGNAL", "40"))
# Weeks unsold after which a listing is flagged stale (and gets an escalating
# suggested cut) regardless of what the traffic signals show.
REPRICE_STALE_WEEKS = int(os.getenv("REPRICE_STALE_WEEKS", "4"))
# --- What a sale actually costs -----------------------------------------------
# These drive BOTH the repricing floor and the profit report's editable
# assumptions, so the two can't disagree about what an item nets.
#
# Every default below is measured from real settled orders rather than quoted
# from eBay's rate card, because the rate card understates the bill:
#
#   EBAY_FVF_PCT — eBay's headline rate is 13.25%, and the itemised
#     FINAL_VALUE_FEE does come to 13.60% (some categories 15.00%). But it is
#     charged on the order total INCLUDING sales tax, while everything here
#     reckons against item + shipping. Measured against that base the real bill
#     is 15.5% (per-item range 13.5-17.0%, n=10). Using 13.25% understated fees
#     on all ten sold items without exception.
#   EBAY_AD_FEE_PCT — Promoted Listings billed $53.17 against $1066.78 of sold
#     revenue = 5.0%, not the 4% ad rate set on the listings. eBay posts these as
#     account-level charges with no order id, so they can't be attributed per
#     item; this is the effective cost rate to model with. Distinct from
#     EBAY_DEFAULT_AD_RATE_PCT above, which is the rate applied TO a listing.
#   EBAY_SHIP_CHARGED / EBAY_SHIP_COST — medians of what buyers actually paid
#     and what the eBay labels actually cost. Means are higher ($12.80 / $10.52)
#     but skewed by one heavy item, so the medians are the safer default.
#
# Re-derive these from your own orders any time with `python -m ebay.orders`.
EBAY_FVF_PCT = float(os.getenv("EBAY_FVF_PCT", "0.155"))
EBAY_FIXED_FEE = float(os.getenv("EBAY_FIXED_FEE", "0.40"))
EBAY_AD_FEE_PCT = float(os.getenv("EBAY_AD_FEE_PCT", "0.05"))
# SET THESE TO YOUR REAL NUMBERS if your shipping differs — and if you ship free
# (charge 0, pay postage), EBAY_SHIP_CHARGED must be 0 or the floor is wrong by
# the full postage.
#
# Both re-measured 2026-08-16 against far more data than the n=10 medians above.
# The postage figure was the badly wrong one: $10.09 against two INDEPENDENT
# samples that agree closely — this account's 18 settled units at $7.20/unit, and
# the previous account's full 2025 tax export, 156 units at $7.13/unit. 174 units
# across two accounts is not a sampling accident; the old median was drawn from
# ten orders and sat ~40% high.
#
# It matters because it lands in the break-even numerator directly: on a $13 Ross
# item the floor was $18.00 and is $15.42 measured. The bot was refusing $2.58 of
# perfectly profitable discount on every listing — the reason /offers and the
# repricer kept reporting "already at the floor" on items with room left.
#
# (EBAY_FVF_PCT and ASSUMED_COST_RATIO were checked the same way and are RIGHT:
# 15.50% and 0.28 measured on this account against 0.155 and 0.29 configured. The
# old account measures 16.49% / 0.38 — a different category and buying mix, so
# don't import those. EBAY_AD_FEE_PCT can't be re-derived here, since eBay posts
# ad spend as account-level charges with no order id; the old account's 2025
# export puts it at 3.77% of item sales against the 5.0% configured, which if it
# holds here means the floor is still a little conservative.)
EBAY_SHIP_CHARGED = float(os.getenv("EBAY_SHIP_CHARGED", "9.88"))
EBAY_SHIP_COST = float(os.getenv("EBAY_SHIP_COST", "7.20"))
# What to assume an item cost at Ross when no receipt was captured. Items with no
# receipt used to be costed at ZERO, which reported their entire sale price as
# profit — one showed a 68% margin purely because its cost was missing. A share of
# the list price beats a flat guess: across 63 items with a known cost it lands at
# a median 29% of list (p25 22%, p75 34%), so it scales with the item instead of
# costing a $110 listing the same as a $30 one. Always presented as an estimate —
# profit.py flags it, report.py colours the cell orange.
ASSUMED_COST_RATIO = float(os.getenv("ASSUMED_COST_RATIO", "0.29"))
# Smallest NET margin (after eBay fees, ad rate, and shipping) a repricing
# suggestion may land on — never suggest a cut that sells at a loss.
REPRICE_MIN_MARGIN_DOLLARS = float(os.getenv("REPRICE_MIN_MARGIN_DOLLARS", "5"))
# When the automatic weekly digest runs, in UTC. Default: Monday 14:00 UTC
# (~9-10am US Eastern/Central).
REPRICE_WEEKDAY = int(os.getenv("REPRICE_WEEKDAY", "0"))  # 0 = Monday
REPRICE_HOUR_UTC = int(os.getenv("REPRICE_HOUR_UTC", "14"))
