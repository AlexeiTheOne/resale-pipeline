"""Weekly repricing: pull each live listing's traffic/watch signals from eBay,
diagnose why an unsold item isn't moving, and suggest a price where the evidence
supports one. Read-only against eBay except the price update a human explicitly
taps "Apply" on in Telegram (telegram_bot.py's reprice_callback).

Diagnosis, from cheapest-to-fix to most decisive:
  TOO_NEW      — not live long enough yet (REPRICE_MIN_WEEKS_LIVE) to trust any
                 of the below; every other check is skipped.
  NO_TRAFFIC_DATA — the traffic batch this listing was in failed, so there are
                 no impressions/views to judge. Diagnosed as nothing rather than
                 read as zero traffic (= INVISIBLE); never produces a price.
  INVISIBLE   — too few impressions: nobody's finding it in search at all.
                 Fix: promotion/title, not price.
  LOW_CTR      — enough impressions, few views: they see it, don't click.
                 Fix: cover photo or price-vs-thumbnail, not necessarily comps.
  STALE_UNSEEN — old and unsold, but with too little traffic to blame the price.
                 Fix: discovery (title/keywords/category/promotion). Never a cut:
                 a listing nobody has looked at hasn't been rejected on price.
  NO_WATCH_DATA— the watch-count call failed, so the one number every remaining
                 check depends on is missing. Diagnosed as nothing rather than
                 guessed at; never produces a price.
  SEND_OFFERS  — watchers present and not shrinking, still unsold: buyers are
                 interested but waiting — a private offer beats a public cut.
  OVERPRICED   — enough views, zero watchers, unsold: the listing page itself
                 isn't converting — price is above what the market will pay.
  STALE        — unsold past REPRICE_STALE_WEEKS despite real views, with no
                 other signal firing: people are looking and not buying, so the
                 price is the remaining variable.
  HEALTHY      — none of the above.

Impressions are eBay's TOTAL_IMPRESSION_TOTAL (promoted placements included),
not the organic-only LISTING_IMPRESSION_TOTAL this used to judge on — organic is
~7-9% of the total on this all-promoted account. See ebay/analytics.py.

A suggested cut (OVERPRICED / STALE) re-prices through config.PRICING_ENGINE
(v2 by default), never lands under the sold-comp median or the cost floor, and
never cuts more than REPRICE_MAX_CUT_PCT at once. Listings with watchers get a
private offer instead (plan_watcher_offers), sent only when the owner taps the
digest's one button (send_planned_offers).

"Enough views" is CUMULATIVE across every week recorded for the listing, not the
trailing week. A week peaks around 11 views on this account against a median of
3, so a weekly bar high enough to be meaningful is a bar nothing ever clears —
REPRICE_MIN_VIEWS_FOR_SIGNAL was 15 per week, reached by 13 of 194 real
snapshots, and OVERPRICED consequently never fired once. The question the
threshold is actually asking is "have enough people looked at this yet", which
is a running total.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import math
from datetime import date, datetime, timedelta, timezone

import config
from config import (
    EBAY_SHIP_CHARGED, OFFER_MESSAGE, OFFER_MIN_DAYS_LIVE, OFFER_MIN_WATCHERS,
    REPRICE_MAX_CUT_PCT, REPRICE_MIN_CTR, REPRICE_MIN_IMPRESSIONS,
    REPRICE_MIN_IMPRESSIONS_FOR_CTR, REPRICE_MIN_MARGIN_DOLLARS,
    REPRICE_MIN_VIEWS_FOR_SIGNAL, REPRICE_MIN_WEEKS_LIVE, REPRICE_PRICING_MAX_CALLS,
    REPRICE_STALE_WEEKS, WATCHER_OFFER_COOLDOWN_DAYS, WATCHER_OFFER_PCT,
    WATCHER_OFFER_PLAN_MAX_AGE_DAYS,
)
from db import (
    claim_offer_plan, get_item, get_offer_plan, list_items, offers_sent_since,
    record_listing_stats, record_offer_sent, save_offer_plan, stats_history,
)
from ebay.analytics import get_traffic_report, get_watch_count
from ebay.inventory import auto_decline_price
from ebay.negotiation import MIN_DISCOUNT_PCT, eligible_listing_ids, send_offer
from pipeline.price import get_pricing
from profit import breakeven_price


def _current_week() -> str:
    """ISO date of the Monday starting the current week — the listing_stats key,
    so re-running the check within the same week overwrites rather than
    duplicates that item's row."""
    today = date.today()
    return (today - timedelta(days=today.weekday())).isoformat()


def _weeks_live(item: dict) -> float | None:
    # published_at is stamped at activation and is the precise signal (a relist
    # resets it). Legacy listings published before that stamp existed fall back
    # to created_at — a close proxy in this workflow (capture->publish is usually
    # same-day) and far better than None, which would disable the STALE backstop
    # on the entire existing inventory.
    when = (item.get("ebay") or {}).get("published_at") or item.get("created_at")
    if not when:
        return None
    then = datetime.fromisoformat(when)
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - then).days / 7


def _charm(price: float) -> float:
    if price == int(price):
        price -= 0.01
    return round(max(price, 0.99), 2)


def _cost_floor(item: dict) -> float | None:
    """Lowest price a repricing suggestion may land on, so a cut never sells at a
    loss. None when the Ross cost is unknown — the caller must then refuse to
    suggest a cut at all rather than cutting blind.

    profit.py owns the solve (it's the profit formula rearranged for price); this
    just pins it to the minimum margin a suggestion has to clear."""
    return breakeven_price(item, REPRICE_MIN_MARGIN_DOLLARS)


def _ctr(traffic: dict) -> float:
    """Click-through, computed HERE from the two integers in the same response.

    eBay's traffic report also returns a CLICK_THROUGH_RATE metric and this used
    to trust it — but it does not agree with the LISTING_VIEWS_TOTAL and
    LISTING_IMPRESSION_TOTAL returned alongside it. Measured over 194 real
    snapshots, LOW_CTR was firing on listings whose views/impressions worked out
    at 3.4%, 3.6% and 4.6% — several times the account median, diagnosed as "seen
    but not clicked".

    Those two integers are unambiguous, they are what the digest prints, and they
    are what listing_stats stores, so deriving the ratio from them keeps the
    verdict consistent with the numbers a human is shown. (ebay/analytics.py's
    module header notes its metric-key mapping was never confirmed against a live
    response; this removes the dependency on the one field that disagreed.)

    Both sides are now all-source: views from every source over TOTAL
    impressions. Dividing all-source views by organic-only impressions (as this
    did until 2026-10-29) mixed two populations."""
    impressions = int(traffic.get("impressions") or 0)
    if impressions <= 0:
        return 0.0
    return int(traffic.get("views") or 0) / impressions


def _diagnose(traffic: dict | None, watchers: int | None, prev_watchers,
              weeks_live: float | None, cumulative_views: int) -> str:
    if weeks_live is not None and weeks_live < REPRICE_MIN_WEEKS_LIVE:
        return "TOO_NEW"
    if traffic is None:
        # The traffic fetch failed for this listing. Every check below starts
        # from impressions, and a missing number read as 0 lands on INVISIBLE —
        # "nobody's finding it in search". That is what happened on 2026-08-17
        # (every row 0 impressions, 71 INVISIBLE) and 2026-10-05 (97 INVISIBLE)
        # while the watch counts beside them were fine. Same rule as
        # NO_WATCH_DATA: absent evidence is not evidence of zero.
        return "NO_TRAFFIC_DATA"

    impressions = traffic.get("impressions", 0)
    # Views over the listing's whole recorded life, not just this week. A single
    # week peaks around 11 views on this account (median 3), so "nobody watched
    # it" can only mean anything once enough people have actually looked.
    views = cumulative_views
    ctr = _ctr(traffic)

    if impressions < REPRICE_MIN_IMPRESSIONS:
        verdict = "INVISIBLE"
    elif impressions >= REPRICE_MIN_IMPRESSIONS_FOR_CTR and ctr < REPRICE_MIN_CTR:
        # Only judged on a week big enough for a low view count to mean
        # something; weekly views run a median of 2 on this account.
        verdict = "LOW_CTR"
    elif watchers is None:
        # The watch-count call failed (or eBay didn't answer). Every branch left
        # below turns on how many people are watching — SEND_OFFERS, OVERPRICED,
        # and the STALE escalation at the bottom — so with that number missing
        # there is no evidence to act on, and stopping here is what keeps a failed
        # API call from being read as "zero watchers". Falling through used to
        # land on HEALTHY and then escalate an old listing to STALE, i.e. cut the
        # price of a listing that may have had a queue of watchers on it.
        return "NO_WATCH_DATA"
    elif watchers > 0 and (prev_watchers is None or watchers >= prev_watchers):
        verdict = "SEND_OFFERS"
    elif watchers == 0 and views >= REPRICE_MIN_VIEWS_FOR_SIGNAL:
        verdict = "OVERPRICED"
    else:
        verdict = "HEALTHY"

    # Age alone is not evidence about price. A cut is only justified when people
    # are demonstrably LOOKING and still not buying; if nobody is seeing the
    # listing, or they see it and don't click, the problem is discovery (title,
    # keywords, category, photo, promotion) and cutting the price just donates
    # margin without fixing anything.
    #
    # So LOW_CTR is never escalated to a cut, and an old listing with too little
    # traffic to judge becomes STALE_UNSEEN (visibility work) rather than STALE.
    if weeks_live is not None and weeks_live >= REPRICE_STALE_WEEKS and verdict == "HEALTHY":
        verdict = "STALE" if views >= REPRICE_MIN_VIEWS_FOR_SIGNAL else "STALE_UNSEEN"
    return verdict


class PricingBudget:
    """How many re-pricing calls (Claude + Apify, ~1 min each) a run may still
    make. One is shared across a run_weekly_check; see REPRICE_PRICING_MAX_CALLS."""

    def __init__(self, calls: int | None = None):
        self.remaining = REPRICE_PRICING_MAX_CALLS if calls is None else calls
        self.used = 0

    def take(self) -> bool:
        if self.remaining <= 0:
            return False
        self.remaining -= 1
        self.used += 1
        return True


def _reprice_via_engine(item: dict) -> dict:
    """Fresh comps for `item` through config.PRICING_ENGINE, called the way
    telegram_bot._stage_price calls it. Nothing is stored."""
    ident = item.get("identification") or {}
    if config.PRICING_ENGINE == "v2":
        from pipeline.pricing_v2 import get_pricing_v2
        return get_pricing_v2(ident, photo_paths=item.get("photos") or [],
                              receipt=item.get("receipt"), item_id=item.get("item_id"))
    return get_pricing(ident.get("search_query"), research=ident)


def _sold_evidence_floor(pricing: dict) -> float | None:
    """The sold-comp median as an ITEM price: the lowest a cut may go ("no
    undercut"). The 29 Oct review found listings priced under 0.9x their comp
    median sold 18% of the time, against 43-75% at 0.9-1.1x. v2 medians are on
    an item+shipping total basis, so our shipping charge comes off; v1's are
    item prices already. None when there's no sold median at all."""
    median = pricing.get("sold_median")
    if not median:
        return None
    median = float(median)
    if str(pricing.get("comp_basis") or "").startswith("item+shipping"):
        median -= EBAY_SHIP_CHARGED
    return median if median > 0 else None


def _round_up_to_cents(x: float) -> float:
    return math.ceil(round(x * 100, 6)) / 100


def _suggest_price(item: dict, verdict: str, current_price: float | None,
                   weeks_live: float | None, watchers: int | None = None,
                   budget: PricingBudget | None = None) -> tuple[float | None, str | None]:
    """(suggested_price, note). The note explains a withheld suggestion — a cut
    that can't be computed safely, or a listing already sitting on its floor —
    because "no suggestion" and "no suggestion, and here's the problem" are very
    different messages to get in a weekly digest.

    Every cheap gate runs BEFORE the pricing engine, so the expensive call
    (Claude + Apify) happens only for a listing that would actually get a cut,
    and at most `budget` times a run. Then the cut is held to three floors: the
    cost floor, the sold-comp median (no undercut), and REPRICE_MAX_CUT_PCT."""
    if current_price is None:
        return None, None
    if verdict not in ("OVERPRICED", "STALE"):
        return None, None
    current_price = float(current_price)

    if watchers:
        # STALE can land here with (shrinking) watchers. Somebody still wants it:
        # a private offer converts them without a public cut (plan_watcher_offers).
        return None, f"{watchers} watcher(s) — an offer to them beats a public cut"

    floor = _cost_floor(item)
    if floor is None:
        # No recorded Ross cost means no floor can be computed. Cutting anyway is
        # how a listing gets discounted below what it cost — refuse, and say so.
        return None, "no Ross cost recorded — can't compute a safe floor. /receipt to set it"

    if current_price <= floor:
        return None, (f"already at the ${_charm(floor):.2f} floor (cost + fees + margin) — "
                      f"cutting further loses money. Send offers to watchers, relist, or accept it")

    # Gentle cuts only: never more than REPRICE_MAX_CUT_PCT in one go.
    gentlest = current_price * (1 - REPRICE_MAX_CUT_PCT)
    target = None
    if verdict == "STALE":
        cut_pct = min(0.12 if (weeks_live or 0) >= REPRICE_STALE_WEEKS * 2 else 0.07,
                      REPRICE_MAX_CUT_PCT)
        target = current_price * (1 - cut_pct)
        if max(target, floor) >= current_price:
            return None, f"a {int(cut_pct * 100)}% cut would land under the floor — held"
    elif config.PRICING_ENGINE != "v2" and not (item.get("identification") or {}).get("search_query"):
        return None, "no search query to re-pull comps with"

    if budget is not None and not budget.take():
        return None, (f"this run's re-pricing budget ({budget.used} call(s)) is used up — "
                      f"re-checked next week")
    try:
        pricing = _reprice_via_engine(item) or {}
    except Exception as e:
        return None, f"re-pricing failed ({type(e).__name__}) — no cut suggested"

    flags = list(pricing.get("review_flags") or [])
    if pricing.get("needs_review") or "few_sales" in flags:
        why = ", ".join(flags) or "needs review"
        return None, f"fresh comps want a human look ({why}) — no cut suggested"
    evidence = _sold_evidence_floor(pricing)
    if evidence is None:
        return None, "no sold comps to anchor a cut against — held"
    if verdict == "OVERPRICED":
        target = pricing.get("suggested_price")
        if target is None:
            return None, "no comp-backed price available to re-price against"
        target = float(target)
        if target >= current_price:
            return None, "comps don't support a lower price"

    lowest = max(floor, evidence, gentlest)
    candidate = _round_up_to_cents(max(target, lowest))
    charmed = _charm(candidate)
    if charmed >= lowest:
        candidate = charmed
    if candidate >= current_price:
        if evidence >= current_price:
            return None, f"sold comps (median ${evidence:.2f}) don't support going lower"
        return None, "no room for a cut above the floors — held"
    return round(candidate, 2), None


def _traffic_for_all(items: list[dict], batch: int = 20) -> dict[str, dict]:
    """One traffic map for the whole run, fetched in batches.

    get_traffic_report has always accepted a LIST of listing ids — the filter is
    `listing_ids:{id1|id2|...}` — but evaluate_item was calling it with a single
    id, so a 110-listing run made 110 separate Analytics calls. eBay rate-limits
    that resource and the tail of the run 429'd: four listings lost their entire
    diagnosis to "Too many requests", and no amount of backoff helps because it's
    a quota, not a burst. Batching turns the same run into ~6 calls.

    A failed batch doesn't take the run down, but its ids are mapped to None —
    "no data" — not left out. Left out reads as zero traffic, and zero traffic
    is INVISIBLE: on 2026-08-17 and 2026-10-05 a failed fetch put 71 and 97
    listings in the digest as "nobody's finding it in search". A listing absent
    from a batch that DID succeed is different: eBay omits listings it has no
    traffic for, so that one is a genuine zero (evaluate_item's default).

    Returns (traffic_map, {"batches": n, "failed": k})."""
    ids = [str((i.get("ebay") or {}).get("listing_id")) for i in items
           if (i.get("ebay") or {}).get("listing_id")]
    out: dict[str, dict | None] = {}
    batches = failed = 0
    for start in range(0, len(ids), batch):
        chunk = ids[start:start + batch]
        batches += 1
        try:
            out.update(get_traffic_report(chunk))
        except Exception as e:
            failed += 1
            print(f"traffic batch {start // batch + 1} failed ({type(e).__name__}) — "
                  f"{len(chunk)} listing(s) will be marked NO_TRAFFIC_DATA")
            for listing_id in chunk:
                out[listing_id] = None
    return out, {"batches": batches, "failed": failed}


def evaluate_item(item: dict, traffic_map: dict | None = None,
                  budget: "PricingBudget | None" = None) -> dict | None:
    """Pull this week's signals for one live item, store them, and return a
    diagnosis dict — or None if it's not eligible (nothing live on eBay, or no
    eBay listing id yet)."""
    ebay = item.get("ebay") or {}
    listing_id = ebay.get("listing_id")
    if not listing_id:
        return None
    # 'published' OR a sold item with stock still on the listing. Gating on the
    # status alone silently excluded live inventory: a multi-quantity listing
    # that sold one unit is marked sold, and five such listings — real stock,
    # really for sale — went unchecked week after week.
    try:
        remaining = int(ebay.get("quantity") or 0)
    except (TypeError, ValueError):
        remaining = 0
    if item.get("status") != "published" and not (item.get("status") == "sold" and remaining > 0):
        return None

    week = _current_week()
    # The current week's own row is excluded. record_listing_stats upserts on
    # (item_id, week), so on a re-run inside the same week the "previous"
    # snapshot is the one this function wrote minutes ago — the watcher-shrinking
    # comparison was measuring a reading against itself, and the cumulative sum
    # below would count this week twice.
    history = [h for h in stats_history(item["item_id"], limit=52) if h["week"] != week]
    prev_watchers = history[0].get("watchers") if history else None

    if traffic_map is None:                    # single-item call (e.g. a retry)
        traffic_map = get_traffic_report([listing_id])
    # Absent from the map = eBay had nothing to report = a real zero. Present as
    # None = the fetch failed = unknown (see _traffic_for_all).
    traffic = traffic_map.get(str(listing_id), {"impressions": 0, "organic_impressions": 0,
                                                "views": 0, "search_views": 0, "ctr": 0})
    watchers = get_watch_count(listing_id)
    cumulative_views = (sum(int(h.get("views") or 0) for h in history)
                        + int((traffic or {}).get("views") or 0))

    current_price = (item.get("listing") or {}).get("price")
    weeks_live = _weeks_live(item)
    verdict = _diagnose(traffic, watchers, prev_watchers, weeks_live, cumulative_views)
    suggested, floor_note = _suggest_price(item, verdict, current_price, weeks_live,
                                           watchers=watchers,
                                           budget=budget if budget is not None else PricingBudget())

    # A failed fetch is stored as NULL impressions/views, never 0: listing_stats
    # is history that later weeks sum over, and a fabricated zero would sit in it
    # as if eBay had measured it. Readers already treat NULL as "nothing to add"
    # (the cumulative-views sum above uses `or 0`).
    # The `impressions` column keeps its historical meaning (organic); the
    # total that verdicts are judged on goes to total_impressions.
    record_listing_stats(
        item["item_id"], week,
        impressions=(traffic or {}).get("organic_impressions"),
        total_impressions=(traffic or {}).get("impressions"),
        views=(traffic or {}).get("views"), search_views=(traffic or {}).get("search_views"),
        watchers=watchers, price=current_price, verdict=verdict, suggested_price=suggested,
    )

    return {
        "item_id": item["item_id"], "verdict": verdict,
        "impressions": (traffic or {}).get("impressions"),
        "organic_impressions": (traffic or {}).get("organic_impressions"),
        "views": (traffic or {}).get("views"),
        "cumulative_views": cumulative_views,
        "ctr": _ctr(traffic) if traffic is not None else None, "watchers": watchers,
        "current_price": current_price, "suggested_price": suggested,
        "floor_note": floor_note,
        "weeks_live": weeks_live,
        "listing_id": str(listing_id),
    }


def run_weekly_check() -> tuple[list[dict], dict]:
    """Evaluate everything with stock live on eBay. Returns (results, health):
    one dict per evaluated item — a failure on an individual item is captured as
    its own ERROR entry rather than aborting the whole run — plus how much of the
    eBay data actually arrived:

      traffic_batches / traffic_failed — batched Analytics calls, and how many failed
      watch_lookups / watch_failed     — per-listing watch-count calls that came back empty

    The caller needs `health` to tell a complete run from one that was largely
    "no data": the scheduler records a run with failed traffic batches as
    partial rather than ok, so it is caught up instead of counted as done.

    Candidates are drawn from 'sold' as well as 'published' — evaluate_item does
    the real filtering — because a listing that sold one of its units keeps the
    'sold' status while the rest stays for sale."""
    candidates = list_items("published") + list_items("sold")
    traffic_map, traffic_health = _traffic_for_all(candidates)   # ~6 batched calls
    budget = PricingBudget()
    results = []
    for item in candidates:
        try:
            result = evaluate_item(item, traffic_map, budget)
        except Exception as e:
            results.append({"item_id": item["item_id"], "verdict": "ERROR", "error": str(e)})
            continue
        if result is not None:
            results.append(result)
    looked_up = [r for r in results if r["verdict"] != "ERROR"]
    health = {
        "traffic_batches": traffic_health["batches"],
        "traffic_failed": traffic_health["failed"],
        "watch_lookups": len(looked_up),
        "watch_failed": sum(1 for r in looked_up if r.get("watchers") is None),
        "pricing_calls": budget.used,
        "offers": build_offer_plan(candidates, results),
    }
    return results, health


# --- offers to watchers --------------------------------------------------------
#
# The 29 Oct review: listings with 3+ watchers sold ~50% of the time and four
# recent sales came from watcher offers, while 25 public cuts sold nothing. So
# the weekly run plans a private offer for every watched listing that's had a
# fair run, and the digest sends the lot on ONE tap. Never without the tap.

def _offer_price(item: dict, price: float) -> tuple[float | None, str | None]:
    """(offer price, None) or (None, why not). WATCHER_OFFER_PCT off, raised to
    the floors when it would land under them: the Best Offer auto-decline floor
    (an offer the listing itself would auto-decline from a buyer is not one to
    send) and the repricer's cost floor. Skipped when the floors leave less than
    eBay's minimum discount, or when there's no Ross cost to floor on."""
    cost_floor = _cost_floor(item)
    if cost_floor is None:
        return None, "no Ross cost recorded — no safe floor"
    decline = auto_decline_price(item, price)
    floor = max(cost_floor, float(decline or 0))
    offer = round(price * (1 - WATCHER_OFFER_PCT / 100), 2)
    if offer < floor:
        offer = _round_up_to_cents(floor)
    max_offer = round(price * (1 - MIN_DISCOUNT_PCT / 100), 2)
    if offer > max_offer:
        return None, (f"floor ${floor:.2f} leaves under eBay's {MIN_DISCOUNT_PCT:.0f}% "
                      f"minimum discount")
    return offer, None


def plan_watcher_offers(items: list[dict], results: list[dict], eligible: set[str],
                        recently_offered: set[str]) -> list[dict]:
    """Which listings get a private offer, from this run's results. Pure (no
    I/O): the caller supplies eBay's eligibility and the recent sends.

    Gates: eBay says eligible (ebay.negotiation.eligible_listing_ids is the
    authority; it excludes a listing with an offer already out); at least
    OFFER_MIN_WATCHERS watchers in THIS run's live read (None = unknown = skip);
    live OFFER_MIN_DAYS_LIVE+ days; no offer sent in WATCHER_OFFER_COOLDOWN_DAYS;
    and a price that clears both floors (_offer_price). Most-watched first."""
    by_id = {i["item_id"]: i for i in items}
    plan = []
    for r in results:
        item = by_id.get(r.get("item_id"))
        listing_id = str(r.get("listing_id") or "")
        watchers = r.get("watchers")
        price = r.get("current_price")
        if (item is None or not listing_id or r.get("verdict") == "ERROR"
                or watchers is None or watchers < OFFER_MIN_WATCHERS or price is None):
            continue
        if listing_id not in eligible or listing_id in recently_offered:
            continue
        weeks = _weeks_live(item)
        if weeks is None or weeks * 7 < OFFER_MIN_DAYS_LIVE:
            continue
        price = round(float(price), 2)
        offer, _why = _offer_price(item, price)
        if offer is None:
            continue
        plan.append({
            "item_id": item["item_id"], "listing_id": listing_id, "price": price,
            "offer_price": offer, "pct": round((price - offer) / price * 100, 1),
            "watchers": watchers,
            "title": ((item.get("listing") or {}).get("title") or "")[:40],
        })
    plan.sort(key=lambda row: -row["watchers"])
    return plan


def _cooldown_cutoff() -> str:
    return (datetime.now(timezone.utc) - timedelta(days=WATCHER_OFFER_COOLDOWN_DAYS)).isoformat()


def build_offer_plan(items: list[dict], results: list[dict]) -> dict:
    """This week's offer plan, saved so the digest's button sends exactly it:
    {"plan_id", "rows", "error"}. One read-only eBay call (findEligibleItems);
    nothing is sent here. A failed eligibility read is an error line in the
    digest, never a guess."""
    try:
        eligible = eligible_listing_ids()
    except Exception as e:
        return {"plan_id": None, "rows": [], "error": f"{type(e).__name__}: {str(e)[:120]}"}
    rows = plan_watcher_offers(items, results, eligible, offers_sent_since(_cooldown_cutoff()))
    return {"plan_id": save_offer_plan(rows) if rows else None, "rows": rows, "error": None}


def send_planned_offers(plan_id: str) -> dict:
    """Send a saved plan. Only ever called from the owner's tap on the digest
    button. Each row is re-checked first against eBay's live eligibility, the
    recent-send record and the listing's CURRENT price: an offer worked out
    against last week's price isn't what was approved if the price has moved.

    Returns {"sent": [row], "skipped": [(row, why)], "failed": [(row, why)],
    "error": str | None}. A plan sends at most once (claim_offer_plan)."""
    out = {"sent": [], "skipped": [], "failed": [], "error": None}
    plan = get_offer_plan(plan_id)
    if plan is None:
        out["error"] = "that offer plan no longer exists — run /pricecheck again"
        return out
    if plan.get("used_at"):
        out["error"] = "these offers were already sent from this digest"
        return out
    created = datetime.fromisoformat(plan["created_at"])
    if datetime.now(timezone.utc) - created > timedelta(days=WATCHER_OFFER_PLAN_MAX_AGE_DAYS):
        out["error"] = (f"that plan is over {WATCHER_OFFER_PLAN_MAX_AGE_DAYS} days old and "
                        f"prices may have moved — run /pricecheck or /offers for a fresh one")
        return out
    try:
        eligible = eligible_listing_ids()
    except Exception as e:
        out["error"] = f"couldn't read eBay's eligible listings ({type(e).__name__}) — nothing sent"
        return out
    if not claim_offer_plan(plan_id):
        out["error"] = "these offers were already sent from this digest"
        return out
    recent = offers_sent_since(_cooldown_cutoff())
    for row in plan["rows"]:
        item = get_item(row["item_id"])
        price = ((item or {}).get("listing") or {}).get("price")
        if item is None:
            out["skipped"].append((row, "item no longer exists"))
        elif price is None or round(float(price), 2) != row["price"]:
            out["skipped"].append((row, f"price changed since the digest (now ${price})"))
        elif row["listing_id"] in recent:
            out["skipped"].append((row, "an offer already went out this week"))
        elif row["listing_id"] not in eligible:
            out["skipped"].append((row, "eBay no longer lists it as eligible"))
        else:
            try:
                send_offer(row["listing_id"], row["offer_price"], OFFER_MESSAGE)
            except Exception as e:
                out["failed"].append((row, f"{type(e).__name__}: {str(e)[:90]}"))
                continue
            record_offer_sent(row["listing_id"], row["offer_price"], item_id=row["item_id"],
                              list_price=row["price"], source="digest")
            out["sent"].append(row)
    return out
