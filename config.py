import os

COMPS_COUNT = 10
ACTIVE_COUNT = 5
UNDERCUT_PCT = 0.15


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
# Smallest gross margin (paid price vs. suggested price, before shipping) a
# repricing suggestion may land on — never suggest a cut that sells at a loss.
REPRICE_MIN_MARGIN_DOLLARS = float(os.getenv("REPRICE_MIN_MARGIN_DOLLARS", "5"))
# When the automatic weekly digest runs, in UTC. Default: Monday 14:00 UTC
# (~9-10am US Eastern/Central).
REPRICE_WEEKDAY = int(os.getenv("REPRICE_WEEKDAY", "0"))  # 0 = Monday
REPRICE_HOUR_UTC = int(os.getenv("REPRICE_HOUR_UTC", "14"))
