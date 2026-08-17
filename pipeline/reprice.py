"""Weekly repricing: pull each live listing's traffic/watch signals from eBay,
diagnose why an unsold item isn't moving, and suggest a price where the evidence
supports one. Read-only against eBay except the price update a human explicitly
taps "Apply" on in Telegram (telegram_bot.py's reprice_callback).

Diagnosis, from cheapest-to-fix to most decisive:
  TOO_NEW      — not live long enough yet (REPRICE_MIN_WEEKS_LIVE) to trust any
                 of the below; every other check is skipped.
  INVISIBLE    — too few impressions: nobody's finding it in search at all.
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

from datetime import date, datetime, timedelta, timezone

from config import (
    REPRICE_MIN_CTR, REPRICE_MIN_IMPRESSIONS, REPRICE_MIN_MARGIN_DOLLARS,
    REPRICE_MIN_VIEWS_FOR_SIGNAL, REPRICE_MIN_WEEKS_LIVE, REPRICE_STALE_WEEKS,
)
from db import list_items, record_listing_stats, stats_history
from ebay.analytics import get_traffic_report, get_watch_count
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
    response; this removes the dependency on the one field that disagreed.)"""
    impressions = int(traffic.get("impressions") or 0)
    if impressions <= 0:
        return 0.0
    return int(traffic.get("views") or 0) / impressions


def _diagnose(traffic: dict, watchers: int | None, prev_watchers,
              weeks_live: float | None, cumulative_views: int) -> str:
    if weeks_live is not None and weeks_live < REPRICE_MIN_WEEKS_LIVE:
        return "TOO_NEW"

    impressions = traffic.get("impressions", 0)
    # Views over the listing's whole recorded life, not just this week. A single
    # week peaks around 11 views on this account (median 3), so "nobody watched
    # it" can only mean anything once enough people have actually looked.
    views = cumulative_views
    ctr = _ctr(traffic)

    if impressions < REPRICE_MIN_IMPRESSIONS:
        verdict = "INVISIBLE"
    elif ctr < REPRICE_MIN_CTR:
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


def _suggest_price(item: dict, verdict: str, current_price: float | None,
                   weeks_live: float | None) -> tuple[float | None, str | None]:
    """(suggested_price, note). The note explains a withheld suggestion — a cut
    that can't be computed safely, or a listing already sitting on its floor —
    because "no suggestion" and "no suggestion, and here's the problem" are very
    different messages to get in a weekly digest."""
    if current_price is None:
        return None, None
    if verdict not in ("OVERPRICED", "STALE"):
        return None, None

    floor = _cost_floor(item)
    if floor is None:
        # No recorded Ross cost means no floor can be computed. Cutting anyway is
        # how a listing gets discounted below what it cost — refuse, and say so.
        return None, "no Ross cost recorded — can't compute a safe floor. /receipt to set it"

    if current_price <= floor:
        return None, (f"already at the ${_charm(floor):.2f} floor (cost + fees + margin) — "
                      f"cutting further loses money. Send offers to watchers, relist, or accept it")

    if verdict == "OVERPRICED":
        ident = item.get("identification") or {}
        query = ident.get("search_query")
        if not query:
            return None, "no search query to re-pull comps with"
        pricing = get_pricing(query, research=ident)
        candidate = pricing.get("suggested_price")
        if candidate is None:
            return None, "no comp-backed price available to re-price against"
        if candidate >= current_price:
            return None, "comps don't support a lower price"
        candidate = max(candidate, floor)
        return (_charm(candidate), None) if candidate < current_price else (None, None)

    # STALE
    cut_pct = 0.15 if (weeks_live or 0) >= REPRICE_STALE_WEEKS * 2 else 0.07
    candidate = max(current_price * (1 - cut_pct), floor)
    if candidate >= current_price:
        return None, f"a {int(cut_pct * 100)}% cut would land under the floor — held"
    return _charm(candidate), None


def _traffic_for_all(items: list[dict], batch: int = 20) -> dict[str, dict]:
    """One traffic map for the whole run, fetched in batches.

    get_traffic_report has always accepted a LIST of listing ids — the filter is
    `listing_ids:{id1|id2|...}` — but evaluate_item was calling it with a single
    id, so a 110-listing run made 110 separate Analytics calls. eBay rate-limits
    that resource and the tail of the run 429'd: four listings lost their entire
    diagnosis to "Too many requests", and no amount of backoff helps because it's
    a quota, not a burst. Batching turns the same run into ~6 calls.

    A failed batch degrades to an empty map for those ids rather than taking the
    run down — they simply read as zero traffic, which is what the old per-item
    error path effectively did anyway."""
    ids = [str((i.get("ebay") or {}).get("listing_id")) for i in items
           if (i.get("ebay") or {}).get("listing_id")]
    out: dict[str, dict] = {}
    for start in range(0, len(ids), batch):
        chunk = ids[start:start + batch]
        try:
            out.update(get_traffic_report(chunk))
        except Exception as e:
            print(f"traffic batch {start // batch + 1} failed ({type(e).__name__}) — "
                  f"{len(chunk)} listing(s) will read as zero traffic")
    return out


def evaluate_item(item: dict, traffic_map: dict | None = None) -> dict | None:
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
    traffic = traffic_map.get(listing_id, {"impressions": 0, "views": 0, "ctr": 0})
    watchers = get_watch_count(listing_id)
    cumulative_views = (sum(int(h.get("views") or 0) for h in history)
                        + int(traffic.get("views") or 0))

    current_price = (item.get("listing") or {}).get("price")
    weeks_live = _weeks_live(item)
    verdict = _diagnose(traffic, watchers, prev_watchers, weeks_live, cumulative_views)
    suggested, floor_note = _suggest_price(item, verdict, current_price, weeks_live)

    record_listing_stats(
        item["item_id"], week,
        impressions=traffic.get("impressions"), views=traffic.get("views"),
        watchers=watchers, price=current_price, verdict=verdict, suggested_price=suggested,
    )

    return {
        "item_id": item["item_id"], "verdict": verdict,
        "impressions": traffic.get("impressions"), "views": traffic.get("views"),
        "cumulative_views": cumulative_views,
        "ctr": _ctr(traffic), "watchers": watchers,
        "current_price": current_price, "suggested_price": suggested,
        "floor_note": floor_note,
        "weeks_live": weeks_live,
    }


def run_weekly_check() -> list[dict]:
    """Evaluate everything with stock live on eBay. Returns one dict per evaluated
    item; a failure on an individual item is captured as its own ERROR entry
    rather than aborting the whole run.

    Candidates are drawn from 'sold' as well as 'published' — evaluate_item does
    the real filtering — because a listing that sold one of its units keeps the
    'sold' status while the rest stays for sale."""
    candidates = list_items("published") + list_items("sold")
    traffic_map = _traffic_for_all(candidates)   # ~6 batched calls, not one per item
    results = []
    for item in candidates:
        try:
            result = evaluate_item(item, traffic_map)
        except Exception as e:
            results.append({"item_id": item["item_id"], "verdict": "ERROR", "error": str(e)})
            continue
        if result is not None:
            results.append(result)
    return results
