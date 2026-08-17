"""eBay traffic + watch-count signals for weekly repricing (pipeline/reprice.py).

Two different eBay APIs, because watch count isn't exposed by the modern REST
APIs at all:
  - Sell Analytics API (`getTrafficReport`) — impressions/views/CTR. Needs the
    `sell.analytics.readonly` OAuth scope (added to ebay/auth.py's SCOPES; a
    token minted before that change won't carry it — re-run `python -m
    ebay.auth` to re-consent, same caveat as sell.marketing).
  - Trading API (`GetItem`, XML) — the only surface eBay still reports watch
    count on. Uses the same OAuth user token, passed as the legacy IAF header
    instead of a Bearer header.

NOTE: this hasn't been exercised against a live eBay account yet — the field
names below (metricKey values, WatchCount's XML path) are per eBay's published
schema, not confirmed against a real response the way pipeline/price.py's Apify
mapping was. Run /health and /pricecheck after deploying and check the raw
response if a listing's numbers look wrong or missing.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import time
import xml.etree.ElementTree as ET
from datetime import date, timedelta

import httpx

from config import EBAY_MARKETPLACE_ID
from ebay.auth import get_access_token

EBAY_API_BASE = "https://api.ebay.com"
TRADING_API_URL = "https://api.ebay.com/ws/api.dll"
_TRADING_NS = {"e": "urn:ebay:apis:eBLBaseComponents"}


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {get_access_token()}",
        "Accept": "application/json",
    }


def get_traffic_report(listing_ids: list[str], days: int = 7) -> dict[str, dict]:
    """Impressions/views/CTR per listing over the trailing `days`. Returns
    {listing_id: {"impressions": int, "views": int, "ctr": float}}; a listing
    eBay has no traffic to report for is simply omitted from the result."""
    if not listing_ids:
        return {}

    # eBay's Analytics API wants dates as yyyyMMdd (errorId 50013 otherwise), NOT
    # ISO yyyy-mm-dd — unlike most eBay REST date params.
    end = date.today()
    start = end - timedelta(days=days)
    filter_str = (
        f"marketplace_ids:{{{EBAY_MARKETPLACE_ID}}},"
        f"listing_ids:{{{'|'.join(listing_ids)}}},"
        f"date_range:[{start.strftime('%Y%m%d')}..{end.strftime('%Y%m%d')}]"
    )
    params = {
        "dimension": "LISTING",
        "filter": filter_str,
        "metric": "LISTING_IMPRESSION_TOTAL,LISTING_VIEWS_TOTAL,CLICK_THROUGH_RATE",
    }
    # eBay rate-limits this endpoint, and a weekly run makes one call per listing —
    # at ~110 listings the tail of the run reliably 429s (4 items lost their whole
    # diagnosis to it on the first real run). A 429 is not a failure of the item,
    # it's a failure of pacing, so back off and retry rather than reporting ERROR
    # on a listing we simply asked about too quickly.
    # Two quick retries only. A 429 here is usually the DAILY quota rather than a
    # burst — measured: after ~104 calls every subsequent request 429s and stays
    # 429 through 21s of backoff — and no wait short enough to be worth doing will
    # clear that. Batching (pipeline/reprice._traffic_for_all) is the actual fix;
    # this just absorbs a genuine burst without turning a quota failure into two
    # minutes of pointless sleeping.
    for attempt in range(3):
        r = httpx.get(f"{EBAY_API_BASE}/sell/analytics/v1/traffic_report",
                      headers=_headers(), params=params, timeout=30)
        if r.status_code != 429:
            break
        if attempt < 2:
            time.sleep(2 + attempt * 2)   # 2s, 4s
    if r.status_code >= 400:
        raise RuntimeError(f"eBay Analytics API traffic_report failed [{r.status_code}]: {r.text}")

    body = r.json()
    # The response is positional, not keyed: header.metrics lists the metric keys
    # in order, and each record's metricValues array lines up with that order (no
    # per-value key). The listing id is the record's single dimensionValue.
    metric_keys = [m.get("key") for m in body.get("header", {}).get("metrics", [])]

    def _num(metric_values, key):
        if key not in metric_keys:
            return 0
        cell = metric_values[metric_keys.index(key)]
        return cell.get("value") or 0

    out = {}
    for record in body.get("records", []):
        dims = record.get("dimensionValues") or []
        listing_id = dims[0].get("value") if dims else None
        if not listing_id:
            continue
        values = record.get("metricValues") or []
        out[listing_id] = {
            "impressions": int(float(_num(values, "LISTING_IMPRESSION_TOTAL"))),
            "views": int(float(_num(values, "LISTING_VIEWS_TOTAL"))),
            "ctr": float(_num(values, "CLICK_THROUGH_RATE")),
        }
    return out


def _trading_headers(call_name: str) -> dict:
    return {
        "X-EBAY-API-SITEID": "0",  # 0 = EBAY_US
        "X-EBAY-API-COMPATIBILITY-LEVEL": "1155",
        "X-EBAY-API-CALL-NAME": call_name,
        "X-EBAY-API-IAF-TOKEN": get_access_token(),
        "Content-Type": "text/xml",
    }


def get_watch_count(listing_id: str) -> int | None:
    """Current watcher count for a live listing, or None if the call failed or
    eBay didn't return the field (e.g. a listing with zero watchers so far)."""
    body = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<GetItemRequest xmlns="urn:ebay:apis:eBLBaseComponents">'
        f"<ItemID>{listing_id}</ItemID>"
        "<IncludeWatchCount>true</IncludeWatchCount>"
        "</GetItemRequest>"
    )
    try:
        r = httpx.post(TRADING_API_URL, headers=_trading_headers("GetItem"),
                        content=body, timeout=30)
    except httpx.HTTPError:
        return None
    if r.status_code >= 400:
        return None
    try:
        root = ET.fromstring(r.text)
    except ET.ParseError:
        return None
    ack = root.findtext("e:Ack", namespaces=_TRADING_NS)
    if ack not in ("Success", "Warning"):
        return None
    watch_count = root.findtext("e:Item/e:WatchCount", namespaces=_TRADING_NS)
    return int(watch_count) if watch_count is not None and watch_count.isdigit() else None


def traffic_status() -> str:
    """Non-destructive check for /health: confirms the token carries
    sell.analytics.readonly (a missing scope 403s here). Queries a throwaway
    listing id over a 1-day range — only the HTTP status is checked, not the
    (empty) data."""
    yesterday, today = date.today() - timedelta(days=1), date.today()
    params = {
        "dimension": "LISTING",
        "filter": (f"marketplace_ids:{{{EBAY_MARKETPLACE_ID}}},"
                   f"listing_ids:{{000000000000}},"
                   f"date_range:[{yesterday.strftime('%Y%m%d')}..{today.strftime('%Y%m%d')}]"),
        "metric": "LISTING_IMPRESSION_TOTAL",
    }
    r = httpx.get(f"{EBAY_API_BASE}/sell/analytics/v1/traffic_report",
                   headers=_headers(), params=params, timeout=30)
    if r.status_code == 403:
        raise RuntimeError(f"403 (scope missing?): {r.text[:200]}")
    if r.status_code >= 500:
        raise RuntimeError(f"eBay error [{r.status_code}]: {r.text[:200]}")
    return "OK"
