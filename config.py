import os
from pathlib import Path

from dotenv import load_dotenv

# Every data path hangs off the repo folder, never the current working directory:
# started from Task Scheduler (cwd C:\Windows\System32) a relative "data/ross.db"
# silently becomes a new empty database with no eBay login.
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
LOG_DIR = BASE_DIR / "logs"

# The only load_dotenv in the codebase. Every module imports config (directly or
# through db), so .env is loaded before any os.getenv runs, whatever the import
# order. Values already in the environment win. The tests set ROSS_SKIP_DOTENV
# (tests/conftest.py) so a run never depends on, or reads keys from, a real .env.
# scripts/notify.py is the one exception: it stays standalone for deploy.ps1.
ENV_FILE = BASE_DIR / ".env"
if not os.getenv("ROSS_SKIP_DOTENV"):
    load_dotenv(ENV_FILE)


def resolve_path(p) -> Path:
    """An absolute path as-is; a relative one (a stored "data/inbox/<id>/..."
    photo path, or a relative env value) resolved against BASE_DIR."""
    path = Path(p)
    return path if path.is_absolute() else BASE_DIR / path


def stored_path(p) -> str:
    """The form a path is written to the DB in: relative to BASE_DIR when it lies
    inside it (data/inbox/<id>/..., as rows have always held), so the data stays
    portable to another folder or PC. Anything else is kept as given."""
    try:
        return str(Path(p).relative_to(BASE_DIR))
    except ValueError:
        return str(p)


# --- Comp evidence quality (pipeline/pricing_v2.py's comp-formula cross-check) ---
# The same-item sold comps are "solid" evidence at or above this many, "thin"
# (usable, but shown for a human OK) at or above the thin count.
PRICE_SOLID_MIN_COMPS = int(os.getenv("PRICE_SOLID_MIN_COMPS", "5"))
PRICE_THIN_MIN_COMPS = int(os.getenv("PRICE_THIN_MIN_COMPS", "3"))
# Past this price dispersion (p90/p10) the comps disagree too much for the
# formula price to count: a median over comps spanning 9x isn't a price.
PRICE_THIN_MAX_DISPERSION = float(os.getenv("PRICE_THIN_MAX_DISPERSION", "4.0"))
# Below this fraction of the sold median, the cheapest active listing is treated
# as a different product rather than a competitor, and does NOT cap our price.
# Without this an unrelated cheap listing drags a good comp-backed price down.
ACTIVE_FLOOR_MIN_RATIO = float(os.getenv("ACTIVE_FLOOR_MIN_RATIO", "0.5"))


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


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
# Best Offer on every new listing. The auto-decline floor is the HIGHER of
# BEST_OFFER_MIN_PCT of the list price and the break-even
# (profit.breakeven_price), each rounded UP to a whole dollar; there is no
# auto-accept. Break-even alone let $15-$25 offers on $30-$50 bags through
# (44-50% of price, Oct 2026), while every offer that became a sale was 83-88%.
# No floor when it would reach the list price (that declines every offer).
# Set BEST_OFFER_ENABLED=0 to publish without Best Offer.
BEST_OFFER_MIN_PCT = float(os.getenv("BEST_OFFER_MIN_PCT", "75"))   # owner picked 75, 2026-10-28
BEST_OFFER_ENABLED = os.getenv("BEST_OFFER_ENABLED", "1").strip().lower() not in ("0", "false", "no", "off", "")
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
# The weekly digest's watcher-offer plan (pipeline/reprice.plan_watcher_offers,
# sent only when the owner taps its button). A percentage rather than /offers'
# $5/$10, per the 29 Oct review: listings with 3+ watchers sold ~50% of the time
# and 4 recent sales came from watcher offers, while public cuts sold nothing.
# The offer is never under the listing's Best Offer auto-decline floor
# (ebay.inventory.auto_decline_price) or the repricer's cost floor. Uses
# OFFER_MIN_WATCHERS and OFFER_MIN_DAYS_LIVE above.
WATCHER_OFFER_PCT = float(os.getenv("WATCHER_OFFER_PCT", "10"))
# Don't re-offer a listing within this many days of the last offer sent on it
# (db.offer_sends). eBay's own eligibility already blocks one still outstanding.
WATCHER_OFFER_COOLDOWN_DAYS = int(os.getenv("WATCHER_OFFER_COOLDOWN_DAYS", "7"))
# A digest's "Send offers" button stops working after this many days: its
# prices were worked out against that week's list prices.
WATCHER_OFFER_PLAN_MAX_AGE_DAYS = int(os.getenv("WATCHER_OFFER_PLAN_MAX_AGE_DAYS", "7"))

# --- Weekly repricing (pipeline/reprice.py) ---
# A listing needs at least this many weeks of eBay search exposure before its
# traffic numbers are trusted for a diagnosis; a brand-new listing just hasn't
# had a chance to be seen yet.
REPRICE_MIN_WEEKS_LIVE = int(os.getenv("REPRICE_MIN_WEEKS_LIVE", "1"))
# Below this many TOTAL impressions (promoted + organic, eBay's
# TOTAL_IMPRESSION_TOTAL) in the trailing week, the listing isn't being shown —
# a visibility problem, not a price problem.
#
# Re-derived 2026-10-29 from a live 7-day traffic_report over 115 live listings:
# total impressions run p10 144 / p25 257 / median 566 / p90 3,169 a week. 150
# fails the bottom ~10% (11 listings), the rate this rule was always meant to
# fire at. It was 50 against LISTING_IMPRESSION_TOTAL, which is organic search
# only (~7-9% of the total on this all-promoted account; median 56/week), so it
# had drifted to flagging 53 of 115 listings as INVISIBLE while they were
# actually being shown ~500 times a week.
REPRICE_MIN_IMPRESSIONS = int(os.getenv("REPRICE_MIN_IMPRESSIONS", "150"))
# Below this click-through rate (views from every source / total impressions,
# same population on both sides), buyers are shown it and aren't clicking —
# points at the thumbnail/title/price, not comps.
#
# 0.0014 = half this account's measured median all-source CTR of 0.28% (same
# 2026-10-29 sample; p25 0.12%). The old 0.0075 was half the median of
# views / ORGANIC impressions — a ratio with all-source views over a search-only
# denominator, which is meaningless now the denominator is the total.
REPRICE_MIN_CTR = float(os.getenv("REPRICE_MIN_CTR", "0.0014"))
# ...and LOW_CTR is only judged once a week's impressions are big enough for a
# low view count to mean anything. Weekly views run a median of 2, so at a few
# hundred impressions 0 views is chance, not a verdict; at 1,000 the median CTR
# predicts ~3 views. With both gates, 8 of 101 listings fired on the sample.
REPRICE_MIN_IMPRESSIONS_FOR_CTR = int(os.getenv("REPRICE_MIN_IMPRESSIONS_FOR_CTR", "1000"))
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
# Re-measured 2026-10-30 from the per-order ledger (ebay.ad_fees on 44 sold
# items): $258 of ad fees on item sales = 4.6% (4.3% of item + shipping, since
# eBay charges the ad rate on shipping too). Was 5.0% from the older estimate.
EBAY_AD_FEE_PCT = float(os.getenv("EBAY_AD_FEE_PCT", "0.046"))
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
# Sales tax paid at Ross on top of the pre-tax price (tag or receipt line).
# Measured 2026-11-02 on the 107 ReceiptsDB receipts where Subtotal + Tax =
# Total: median 6.95%, 55 at 6.9-7.0% (7% after per-line rounding), none at 0%.
# That is Florida 6% + Miami-Dade 1%. Some receipts come out lower (about 6.3%
# blended), likely tax-exempt items, so 7% slightly overstates a few costs.
# Used by the SQL Server view v_item_cost (docs/schema-proposal.md).
SALES_TAX_RATE = float(os.getenv("SALES_TAX_RATE", "0.07"))

# --- /check: the pre-buy check (docs/check-command-design.md) ---------------
# Verdict thresholds (design §5; Person B, 2026-11-02). Backtest them with
# scripts/backtest_check.py before trusting them.
CHECK_BUY_MIN_PROFIT = float(os.getenv("CHECK_BUY_MIN_PROFIT", "15"))
CHECK_BUY_MIN_ROI = float(os.getenv("CHECK_BUY_MIN_ROI", "0.75"))
CHECK_MIN_SALES = int(os.getenv("CHECK_MIN_SALES", "3"))
CHECK_MIN_PROFIT = float(os.getenv("CHECK_MIN_PROFIT", "5"))
CHECK_MIN_PROFIT_AT_CHEAPEST = float(os.getenv("CHECK_MIN_PROFIT_AT_CHEAPEST", "5"))
# A price resting only on asking prices (no sales) is discounted by this, on
# top of the usual UNDERCUT_V2_PCT. Measured 2026-11-02 on our own 44 sold
# items: sold price / asking price, mean 0.98 (84% sold at the full ask).
# Revisit with pilot outcomes from the checks table.
CHECK_ASK_DISCOUNT = float(os.getenv("CHECK_ASK_DISCOUNT", "0.98"))
# /check's eBay fees (Person B, 2026-11-07): EBAY_FVF_PCT x (item + shipping)
# + EBAY_FIXED_FEE per order + the listing's ad rate (EBAY_DEFAULT_AD_RATE_PCT)
# x (item + shipping). Fitted on our 85 orders: fees 15.5% + $0.40, ads ~4%,
# all-in median 19.6%. (The 24.7% of 2026-11-06 was one small order with a
# 6.5% ad rate.) The label is subtracted on its own (CHECK_LABEL_COSTS).
# Checks per day (US Eastern), and when to warn.
CHECK_DAILY_MAX = int(os.getenv("CHECK_DAILY_MAX", "50"))
CHECK_DAILY_WARN = int(os.getenv("CHECK_DAILY_WARN", "40"))
# Cached sold rows older than this are not used by /check (pricing for stored
# items keeps any age until Person A agrees otherwise).
CHECK_SOLD_CACHE_DAYS = float(os.getenv("CHECK_SOLD_CACHE_DAYS", "7"))
# Dropship-style listings (copied Amazon titles, huge sellers, wrong
# category): flagged on 2 signals, kept out of the sold evidence, still a
# ceiling. A seller at or above this feedback score counts as one signal.
CHECK_DROPSHIP_FEEDBACK = int(os.getenv("CHECK_DROPSHIP_FEEDBACK", "10000"))
# Full check: look up this many identical listings (listing age, units sold).
CHECK_DEMAND_LISTINGS = int(os.getenv("CHECK_DEMAND_LISTINGS", "3"))
# Saturation (Person B, 2026-11-06), full check: sales in the last
# CHECK_SUPPLY_DAYS vs listings for sale now, same item and same model. Over
# CHECK_SUPPLY_HIGH_MONTHS of supply (listings / sales per month), or no
# sales and at least CHECK_SUPPLY_MIN_ACTIVE listings, is high supply: the
# verdict drops one step and a price from sales moves to their lower quartile.
CHECK_SUPPLY_DAYS = int(os.getenv("CHECK_SUPPLY_DAYS", "30"))
CHECK_SUPPLY_HIGH_MONTHS = float(os.getenv("CHECK_SUPPLY_HIGH_MONTHS", "3"))
CHECK_SUPPLY_MIN_ACTIVE = int(os.getenv("CHECK_SUPPLY_MIN_ACTIVE", "4"))
# Listings fetched for the same-model count (eBay Browse allows 200 a page).
CHECK_SUPPLY_ACTIVE_COUNT = int(os.getenv("CHECK_SUPPLY_ACTIVE_COUNT", "200"))
# Best Offer from the /check estimate (Person B, 2026-11-02): list at
# estimate x LIST, auto-accept at x ACCEPT, auto-decline under x DECLINE.
CHECK_BO_LIST = float(os.getenv("CHECK_BO_LIST", "1.25"))
CHECK_BO_ACCEPT = float(os.getenv("CHECK_BO_ACCEPT", "1.05"))
CHECK_BO_DECLINE = float(os.getenv("CHECK_BO_DECLINE", "0.90"))
# Shipping label we pay, by item type: "type=dollars,..." Starting estimates;
# correct them from real label costs. small_bags: slings, crossbodies, minis,
# wallets (Person B, 2026-11-02: ~$5-6 Ground Advantage); bags: totes,
# weekenders, duffels and any bag of unknown size. "other" is EBAY_SHIP_COST, the measured
# average.
CHECK_LABEL_COSTS = os.getenv(
    "CHECK_LABEL_COSTS", f"bags=11.00,small_bags=5.50,jewelry=4.50,shoes=12.00,clothing=7.20,"
                         f"other={EBAY_SHIP_COST:.2f}")
# A bag counts as small (Ground Advantage, small_bags label) when no side is
# over this many inches, or by name: sling, crossbody, mini, wallet ...
CHECK_SMALL_BAG_MAX_IN = float(os.getenv("CHECK_SMALL_BAG_MAX_IN", "11"))
# "Do I already have this?" at the top of every /check reply (pipeline/owned.py).
# The AI title match runs only when no UPC, style code or Ross code matched and
# there are things of the same brand to compare; 0 turns it off. Model: empty
# = CLAUDE_CHEAP_MODEL. CHECK_OWNED_SHOW: hits listed in the reply (all in Details).
CHECK_OWNED_AI = os.getenv("CHECK_OWNED_AI", "1") == "1"
CHECK_OWNED_AI_MODEL = os.getenv("CHECK_OWNED_AI_MODEL", "")
CHECK_OWNED_SHOW = int(os.getenv("CHECK_OWNED_SHOW", "3"))

# Smallest NET margin (after eBay fees, ad rate, and shipping) a repricing
# suggestion may land on — never suggest a cut that sells at a loss.
REPRICE_MIN_MARGIN_DOLLARS = float(os.getenv("REPRICE_MIN_MARGIN_DOLLARS", "5"))
# Largest single public cut a suggestion may make, as a fraction of the current
# price. The owner prefers gentle cuts plus monitoring (and offers to watchers
# over deep cuts): the 19-26 Oct cuts of 20-33% sold nothing, and the one earlier
# deep cut that sold (Vera Bradley 79.99 -> 39.99) later re-sold at $54.99.
REPRICE_MAX_CUT_PCT = float(os.getenv("REPRICE_MAX_CUT_PCT", "0.12"))
# Most re-pricing calls (pipeline.pricing_v2: Claude + Apify comps) one weekly
# run may make. Only listings that already cleared every cheap gate for a cut
# reach a call, so this is a ceiling on a bad week, not the usual count. Apify's
# free credits are the tight budget here, not Claude.
REPRICE_PRICING_MAX_CALLS = int(os.getenv("REPRICE_PRICING_MAX_CALLS", "15"))
# When the automatic weekly digest runs, in UTC. Default: Monday 14:00 UTC
# (~9-10am US Eastern/Central).
# v2 pricing: a price backed by fewer matching SOLD comps than this stops at the
# price gate instead of auto-confirming (flag "few_sales").
PRICE_V2_MIN_SOLD_TO_AUTO = int(os.getenv("PRICE_V2_MIN_SOLD_TO_AUTO", "2"))

REPRICE_WEEKDAY = int(os.getenv("REPRICE_WEEKDAY", "0"))  # 0 = Monday
REPRICE_HOUR_UTC = int(os.getenv("REPRICE_HOUR_UTC", "14"))

# Master switch for every background job (weekly price check, sold alerts,
# daily sync, buyer feedback). Set JOBS_ENABLED=0 on a dev copy so a test bot
# never acts on the live eBay account on its own.
JOBS_ENABLED = _env_bool("JOBS_ENABLED", True)

# --- SQL Server (ResaleDB; sqlserver.py, docs/schema-proposal.md) ---
# Windows auth on the local Express instance. SQL_DATABASE has NO default on
# purpose: dev (ResaleDB_Dev) and production (ResaleDB) share this instance on
# the production PC, so a missing setting must fail instead of quietly picking
# production. Tests use their own ResaleDB_Test_* database (tests/conftest.py).
SQL_SERVER = os.getenv("SQL_SERVER", r".\SQLEXPRESS")
SQL_DATABASE = os.getenv("SQL_DATABASE") or None
SQL_DRIVER = os.getenv("SQL_DRIVER", "ODBC Driver 18 for SQL Server")
# The local instance has a self-signed certificate: encrypt, but trust it.
SQL_TRUST_SERVER_CERT = _env_bool("SQL_TRUST_SERVER_CERT", True)
# Where SQL Server writes BACKUP DATABASE files: a local folder the SQL Server
# service account can write (doc 7.1, e.g. D:\SQLBackups). Unset = the
# instance's own Backup folder, which a normal user may not be able to read.
SQL_BACKUP_DIR = os.getenv("SQL_BACKUP_DIR") or None
# Where db.py reads from: "sqlite" (data/ross.db, the default and what
# production runs until cutover) or "sqlserver" (SQL_DATABASE, through
# db_mssql.py: the same functions, reads and writes).
DB_BACKEND = (os.getenv("DB_BACKEND") or "sqlite").strip().lower()

# Master switch for every call that changes the eBay account (publish, offers,
# price/quantity/title edits, ads, offers to watchers, feedback). On by default
# so production is unchanged. Set EBAY_WRITES_ENABLED=0 on a dev copy: writes
# are refused with a log line before anything is sent (ebay/guard.py).
EBAY_WRITES_ENABLED = _env_bool("EBAY_WRITES_ENABLED", True)

# Automatic /sync. Until this existed, /sync was manual-only and nothing else
# wrote sold orders into the DB, so every money figure (/profit, /report, COGS)
# silently understated whenever a sync was overdue — on 2026-09-03 the DB was
# 10 days stale and missing 8 August orders, putting August COGS 35% low.
# Daily rather than weekly because the reprice digest reads price/quantity that
# a sale changes, and it should not run on a week-old picture of the account.
SYNC_ENABLED = os.getenv("SYNC_ENABLED", "1") not in ("0", "false", "False")
# Poll orders this often and announce each sale with the box to pull it from.
SOLD_ALERTS_ENABLED = os.getenv("SOLD_ALERTS_ENABLED", "1") not in ("0", "false", "False")
SOLD_ALERT_MINUTES = int(os.getenv("SOLD_ALERT_MINUTES", "15"))
SYNC_HOUR_UTC = int(os.getenv("SYNC_HOUR_UTC", "11"))   # 3 hours before the digest
# Order look-back per run. Matches /sync's own default; a window comfortably
# wider than the sync interval means a run that gets missed loses nothing.
SYNC_DAYS = int(os.getenv("SYNC_DAYS", "90"))
# How late a scheduled job may be before a catch-up run is pointless rather than
# useful. Inside the window, a job that missed its slot (laptop asleep, reboot)
# runs shortly after startup; outside it, it just waits for the next slot.
JOB_CATCHUP_GRACE_HOURS = int(os.getenv("JOB_CATCHUP_GRACE_HOURS", "48"))

# Automatic positive feedback for buyers (ebay/feedback.py). Sellers can only
# leave Positive, and the window closes 60 days after the sale, so the only way
# to get it wrong is to forget. Daily, an hour after the sync.
FEEDBACK_ENABLED = os.getenv("FEEDBACK_ENABLED", "1") not in ("0", "false", "False")
FEEDBACK_HOUR_UTC = int(os.getenv("FEEDBACK_HOUR_UTC", "12"))
# eBay caps this at 80 characters; anything longer is cut off.
FEEDBACK_COMMENT = os.getenv(
    "FEEDBACK_COMMENT",
    "Great buyer, fast payment. Thank you for your purchase! A+")

# --- Claude (claude_llm.py) and LLM spend tracking (llm_usage table) ---
# Sonnet is the everyday model; callers escalate to Opus for the hard cases and
# drop to Haiku for cheap structuring. The owner's Anthropic credit is $100 a
# MONTH, so every call is logged and the guard below steps models down as the
# month's spend approaches it rather than letting a busy week eat the budget.
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-5-5")
CLAUDE_STRONG_MODEL = os.getenv("CLAUDE_STRONG_MODEL", "claude-opus-5-5")
CLAUDE_CHEAP_MODEL = os.getenv("CLAUDE_CHEAP_MODEL", "claude-haiku-5-5")
# /find: which listed item is in this photo. Sonnet reads prints and tags well
# enough; about 2 cents a photo with the whole shop as the item list.
FIND_MODEL = os.getenv("FIND_MODEL", CLAUDE_MODEL)
# Quick add: a fast price from MSRP habits + up to 2 web searches.
QUICK_PRICE_MODEL = os.getenv("QUICK_PRICE_MODEL", CLAUDE_MODEL)
# Price notes (pipeline/value_notes.py): short facts with links shown at the
# price gate, from Claude + a few web searches while pricing runs.
VALUE_NOTES_ENABLED = _env_bool("VALUE_NOTES_ENABLED", True)
VALUE_NOTES_MODEL = os.getenv("VALUE_NOTES_MODEL", CLAUDE_MODEL)
VALUE_NOTES_SEARCHES = int(os.getenv("VALUE_NOTES_SEARCHES", "5"))
# Per-attempt HTTP timeout and SDK retries (connection errors, 408/409/429/5xx).
# Worst-case wall clock is timeout x (retries + 1), so keep both modest.
CLAUDE_TIMEOUT_S = float(os.getenv("CLAUDE_TIMEOUT_S", "120"))
CLAUDE_MAX_RETRIES = int(os.getenv("CLAUDE_MAX_RETRIES", "3"))
# Server-side refusal fallback ("default" routing) on Opus/Sonnet: a request a
# safety classifier declines is re-run on Anthropic's recommended model inside
# the same call instead of failing the item. Product pricing should almost never
# trip it; the fallback attempt is billed at that model's rates and logged.
CLAUDE_REFUSAL_FALLBACK = _env_bool("CLAUDE_REFUSAL_FALLBACK", True)

ANTHROPIC_MONTHLY_BUDGET = float(os.getenv("ANTHROPIC_MONTHLY_BUDGET", "100"))
# Fractions of the budget, month to date (calendar month, UTC). At WARN the owner
# gets one Telegram warning. At DOWNGRADE every Claude call steps down one tier
# (opus -> sonnet -> haiku); at 100% everything runs on haiku.
ANTHROPIC_BUDGET_WARN_PCT = float(os.getenv("ANTHROPIC_BUDGET_WARN_PCT", "0.80"))
ANTHROPIC_BUDGET_DOWNGRADE_PCT = float(os.getenv("ANTHROPIC_BUDGET_DOWNGRADE_PCT", "0.95"))

# USD per 1M tokens. "cache_read" / "cache_write" are the prompt-cache rates
# (5-minute writes bill at 1.25x input). The fallback models are listed so a
# refusal fallback is costed at its own rates. An unknown Claude model is costed
# at the dearest row, so the budget guard can over-count but never under-count.
ANTHROPIC_PRICES = {
    "claude-opus-5-5":   {"in": 4.00, "out": 20.00, "cache_read": 0.20, "cache_write": 5.00},
    "claude-sonnet-5-5": {"in": 2.00, "out": 10.00, "cache_read": 0.20, "cache_write": 2.50},
    "claude-haiku-5-5":  {"in": 0.10, "out": 0.50, "cache_read": 0.01, "cache_write": 0.125,
                          # Haiku 5.5 bills a prompt over 100K tokens at a higher rate.
                          "long_threshold": 100_000, "long_in": 0.50, "long_out": 2.50},
    "claude-opus-5":     {"in": 5.00, "out": 25.00, "cache_read": 0.50, "cache_write": 6.25},
    "claude-sonnet-5":   {"in": 2.00, "out": 10.00, "cache_read": 0.20, "cache_write": 2.50},
    "claude-opus-4-8":   {"in": 5.00, "out": 25.00, "cache_read": 0.50, "cache_write": 6.25},
}
ANTHROPIC_WEB_SEARCH_PER_1K = 10.00

# Gemini rates: APPROXIMATE, for visibility only (Gemini isn't on the Anthropic
# budget). Matched by regex on the model id, first match wins; an unmatched model
# is logged with no cost. Thinking tokens bill as output. Cached prompt tokens
# are charged at the full input rate, which slightly over-counts. Grounded
# searches are free within the paid tier's monthly allowance, so they are counted
# but not costed.
GEMINI_PRICES = [
    (r"^gemini-2\.5-flash$", {"in": 0.30, "out": 2.50}),
    (r"^gemini-3(\.\d+)?-flash$", {"in": 0.75, "out": 3.75}),
]

# --- Pricing engine v2 (pipeline/pricing_v2.py, docs/pricing-design.md) ---------
# The only engine: pools sold + active comps, has Claude judge which are the
# same item and set the price, with the comp formula and the store's MSRP range
# as cross-checks computed after Claude answers. Confirmed from cache on 33
# items: 18/32 outcome hits and a median 1.00x the sold price, against the old
# v1 ladder's 11/32 and 0.74x. v1 and its PRICING_ENGINE switch were removed on
# 1 Nov 2026; bot/app.py warns at startup if .env still sets PRICING_ENGINE.
PRICING_JUDGE_MODEL = os.getenv("PRICING_JUDGE_MODEL", "claude-sonnet-5-5")
# Low, not medium: the judge's job is sorting 30-60 titles+thumbnails into four
# buckets and naming a price, which is perception more than reasoning; low keeps
# thinking to a few hundred tokens a call. The price itself is computed in code.
# A low-confidence answer is re-asked on the strong model (below), which is
# where extra reasoning is paid for only when it is needed.
PRICING_JUDGE_EFFORT = os.getenv("PRICING_JUDGE_EFFORT", "low")
PRICING_ESCALATE_MODEL = os.getenv("PRICING_ESCALATE_MODEL", CLAUDE_STRONG_MODEL)
PRICING_ESCALATE_ON_LOW = _env_bool("PRICING_ESCALATE_ON_LOW", True)
# v1 undercuts the sold median by 15% and then again under the cheapest active
# listing, and the owner raised 54 of 101 machine prices. v2 starts at 5%; the
# backtest is what sets it.
UNDERCUT_V2_PCT = float(os.getenv("UNDERCUT_V2_PCT", "0.05"))
# Rows per sold-comps query. The sold actor bills per result row ($0.004 on the
# FREE Apify tier, plus $0.0001 per run start), and v2 runs up to 3 queries.
PRICING_V2_SOLD_COUNT = int(os.getenv("PRICING_V2_SOLD_COUNT", "15"))
PRICING_V2_ACTIVE_COUNT = int(os.getenv("PRICING_V2_ACTIVE_COUNT", "30"))
PRICING_V2_IMAGE_COUNT = int(os.getenv("PRICING_V2_IMAGE_COUNT", "20"))
# Most candidates sent to the judge in one call (sold kept first).
PRICING_V2_MAX_CANDIDATES = int(os.getenv("PRICING_V2_MAX_CANDIDATES", "60"))
# Buyer location for Browse shipping quotes: our own ship-from zip, so the quote
# is what a nearby buyer would see. Without one ~38% of results have no cost.
PRICING_V2_SHIP_TO_ZIP = os.getenv("PRICING_V2_SHIP_TO_ZIP", EBAY_SHIP_FROM_ADDRESS["postalCode"])
# This account's eBay seller name(s): search results include our own listings
# (image search ranks them first), which must never count as comps.
OWN_SELLER_USERNAMES = tuple(
    s.strip().lower() for s in os.getenv("OWN_SELLER_USERNAMES", "premium_designer_outlet").split(",")
    if s.strip())
PRICING_V2_CACHE_DIR = str(resolve_path(os.getenv("PRICING_V2_CACHE_DIR") or DATA_DIR / "cache" / "pricing_v2"))
# Apify sold actor (oTtB3VgfuE9GtxQt2) pay-per-event rates, FREE tier, read from
# the actor's pricingInfos on 17 Oct 2026. Used only to ESTIMATE spend; Apify's
# own usage page is the bill.
APIFY_SOLD_COST_PER_RESULT = float(os.getenv("APIFY_SOLD_COST_PER_RESULT", "0.004"))
APIFY_RUN_START_COST = float(os.getenv("APIFY_RUN_START_COST", "0.0001"))

# --- Final listing check (pipeline/listing_check.py) ----------------------------
# One Claude call after the draft is written (and again after a correction that
# rewrites it) that reads title, specifics, condition, description, price
# evidence and 3 photos together and names concrete contradictions between them
# on the review screen. Advisory only: it never blocks or edits anything, and a
# failure shows as "not checked". About $0.02 an item.
LISTING_CHECK_ENABLED = _env_bool("LISTING_CHECK_ENABLED", True)
LISTING_CHECK_MODEL = os.getenv("LISTING_CHECK_MODEL", "claude-sonnet-5-5")
LISTING_CHECK_EFFORT = os.getenv("LISTING_CHECK_EFFORT", "low")
# Shorter than CLAUDE_TIMEOUT_S with one retry: this runs inline before the
# review screen, and a check that holds the draft up for minutes isn't worth it.
LISTING_CHECK_TIMEOUT_S = float(os.getenv("LISTING_CHECK_TIMEOUT_S", "60"))
LISTING_CHECK_PHOTOS = int(os.getenv("LISTING_CHECK_PHOTOS", "3"))
