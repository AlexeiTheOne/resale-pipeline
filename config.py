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

# --- Weekly repricing (pipeline/reprice.py) ---
# A listing needs at least this many weeks of eBay search exposure before its
# traffic numbers are trusted for a diagnosis; a brand-new listing just hasn't
# had a chance to be seen yet.
REPRICE_MIN_WEEKS_LIVE = int(os.getenv("REPRICE_MIN_WEEKS_LIVE", "1"))
# Below this many impressions in the trailing week, the listing isn't being
# surfaced in eBay search at all — a visibility problem, not a price problem.
REPRICE_MIN_IMPRESSIONS = int(os.getenv("REPRICE_MIN_IMPRESSIONS", "50"))
# Below this click-through rate (views / impressions), buyers see it in search
# results but aren't clicking — points at the thumbnail/title/price, not comps.
REPRICE_MIN_CTR = float(os.getenv("REPRICE_MIN_CTR", "0.01"))
# Minimum trailing-week views before "zero watchers" is trusted as evidence the
# price itself is the problem, rather than just low traffic.
REPRICE_MIN_VIEWS_FOR_SIGNAL = int(os.getenv("REPRICE_MIN_VIEWS_FOR_SIGNAL", "15"))
# Weeks unsold after which a listing is flagged stale (and gets an escalating
# suggested cut) regardless of what the traffic signals show.
REPRICE_STALE_WEEKS = int(os.getenv("REPRICE_STALE_WEEKS", "4"))
# eBay fee assumptions used to compute the price floor a repricing suggestion
# must clear (cost + fees + minimum margin) — same figures as report.py's Excel
# assumptions, kept here as the defaults an env var can override.
EBAY_FVF_PCT = float(os.getenv("EBAY_FVF_PCT", "0.1325"))
EBAY_FIXED_FEE = float(os.getenv("EBAY_FIXED_FEE", "0.40"))
# Shipping, both directions. These mirror report.py's editable assumptions so the
# repricing floor and the profit report agree on what an item actually nets. With
# charged == cost the postage cancels out, but eBay's FVF still applies to the
# shipping you charge, so leaving both at zero understates the floor.
# SET THESE TO YOUR REAL NUMBERS — if you ship free (charge 0, pay postage), the
# floor is wrong by the full postage until EBAY_SHIP_CHARGED is 0 here too.
EBAY_SHIP_CHARGED = float(os.getenv("EBAY_SHIP_CHARGED", "10"))
EBAY_SHIP_COST = float(os.getenv("EBAY_SHIP_COST", "10"))
# Smallest NET margin (after eBay fees, ad rate, and shipping) a repricing
# suggestion may land on — never suggest a cut that sells at a loss.
REPRICE_MIN_MARGIN_DOLLARS = float(os.getenv("REPRICE_MIN_MARGIN_DOLLARS", "5"))
# When the automatic weekly digest runs, in UTC. Default: Monday 14:00 UTC
# (~9-10am US Eastern/Central).
REPRICE_WEEKDAY = int(os.getenv("REPRICE_WEEKDAY", "0"))  # 0 = Monday
REPRICE_HOUR_UTC = int(os.getenv("REPRICE_HOUR_UTC", "14"))
