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
  SEND_OFFERS  — watchers present and not shrinking, still unsold: buyers are
                 interested but waiting — a private offer beats a public cut.
  OVERPRICED   — enough views, zero watchers, unsold: the listing page itself
                 isn't converting — price is above what the market will pay.
  STALE        — unsold past REPRICE_STALE_WEEKS with no other signal firing —
                 the backstop for listings the traffic heuristics can't diagnose.
  HEALTHY      — none of the above.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from datetime import date, datetime, timedelta, timezone

from config import (
    EBAY_DEFAULT_AD_RATE_PCT, EBAY_FIXED_FEE, EBAY_FVF_PCT,
    REPRICE_MIN_CTR, REPRICE_MIN_IMPRESSIONS, REPRICE_MIN_MARGIN_DOLLARS,
    REPRICE_MIN_VIEWS_FOR_SIGNAL, REPRICE_MIN_WEEKS_LIVE, REPRICE_STALE_WEEKS,
)
from db import list_items, record_listing_stats, stats_history
from ebay.analytics import get_traffic_report, get_watch_count
from pipeline.price import get_pricing


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
    """Lowest price a repricing suggestion may land on: what was paid at Ross
    plus eBay's final-value fee, fixed fee, and ad rate, plus a minimum margin —
    so a suggested cut never sells at a loss. None if the receipt cost is
    unknown (no floor can be computed, so no cut is suggested)."""
    paid = (item.get("receipt") or {}).get("reduced_price")
    if paid is None:
        return None
    ad_rate = (EBAY_DEFAULT_AD_RATE_PCT or 0) / 100
    denom = 1 - EBAY_FVF_PCT - ad_rate
    if denom <= 0:
        return None
    return (REPRICE_MIN_MARGIN_DOLLARS + EBAY_FIXED_FEE + paid) / denom


def _diagnose(traffic: dict, watchers: int | None, prev_watchers, weeks_live: float | None) -> str:
    if weeks_live is not None and weeks_live < REPRICE_MIN_WEEKS_LIVE:
        return "TOO_NEW"

    impressions = traffic.get("impressions", 0)
    views = traffic.get("views", 0)
    ctr = traffic.get("ctr", 0)

    if impressions < REPRICE_MIN_IMPRESSIONS:
        verdict = "INVISIBLE"
    elif ctr < REPRICE_MIN_CTR:
        verdict = "LOW_CTR"
    elif watchers is not None and watchers > 0 and (prev_watchers is None or watchers >= prev_watchers):
        verdict = "SEND_OFFERS"
    elif watchers == 0 and views >= REPRICE_MIN_VIEWS_FOR_SIGNAL:
        verdict = "OVERPRICED"
    else:
        verdict = "HEALTHY"

    if weeks_live is not None and weeks_live >= REPRICE_STALE_WEEKS and verdict in ("HEALTHY", "LOW_CTR"):
        verdict = "STALE"
    return verdict


def _suggest_price(item: dict, verdict: str, current_price: float | None, weeks_live: float | None) -> float | None:
    if current_price is None:
        return None
    floor = _cost_floor(item)

    if verdict == "OVERPRICED":
        ident = item.get("identification") or {}
        query = ident.get("search_query")
        if not query:
            return None
        pricing = get_pricing(query, research=ident)
        candidate = pricing.get("suggested_price")
        if candidate is None or candidate >= current_price:
            return None
        if floor is not None:
            candidate = max(candidate, floor)
        return _charm(candidate) if candidate < current_price else None

    if verdict == "STALE":
        cut_pct = 0.15 if (weeks_live or 0) >= REPRICE_STALE_WEEKS * 2 else 0.07
        candidate = current_price * (1 - cut_pct)
        if floor is not None:
            candidate = max(candidate, floor)
        return _charm(candidate) if candidate < current_price else None

    return None


def evaluate_item(item: dict) -> dict | None:
    """Pull this week's signals for one published item, store them, and return a
    diagnosis dict — or None if it's not eligible (not published, or has no
    eBay listing id yet)."""
    if item.get("status") != "published":
        return None
    listing_id = (item.get("ebay") or {}).get("listing_id")
    if not listing_id:
        return None

    week = _current_week()
    prev_history = stats_history(item["item_id"], limit=1)
    prev_watchers = prev_history[0].get("watchers") if prev_history else None

    traffic_map = get_traffic_report([listing_id])
    traffic = traffic_map.get(listing_id, {"impressions": 0, "views": 0, "ctr": 0})
    watchers = get_watch_count(listing_id)

    current_price = (item.get("listing") or {}).get("price")
    weeks_live = _weeks_live(item)
    verdict = _diagnose(traffic, watchers, prev_watchers, weeks_live)
    suggested = _suggest_price(item, verdict, current_price, weeks_live)

    record_listing_stats(
        item["item_id"], week,
        impressions=traffic.get("impressions"), views=traffic.get("views"),
        watchers=watchers, price=current_price, verdict=verdict, suggested_price=suggested,
    )

    return {
        "item_id": item["item_id"], "verdict": verdict,
        "impressions": traffic.get("impressions"), "views": traffic.get("views"),
        "ctr": traffic.get("ctr"), "watchers": watchers,
        "current_price": current_price, "suggested_price": suggested,
        "weeks_live": weeks_live,
    }


def run_weekly_check() -> list[dict]:
    """Evaluate every published, unsold item. Returns one dict per evaluated
    item; a failure on an individual item is captured as its own ERROR entry
    rather than aborting the whole run."""
    results = []
    for item in list_items("published"):
        try:
            result = evaluate_item(item)
        except Exception as e:
            results.append({"item_id": item["item_id"], "verdict": "ERROR", "error": str(e)})
            continue
        if result is not None:
            results.append(result)
    return results
