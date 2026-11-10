"""/diagnose: one primary verdict per live listing, with a one-line reason and
one concrete fix (docs/listing-performance-plan.md 7.2-7.3). Read-only: the
fixes go through /revise and /offers.

Verdicts, in priority order (the first that applies is primary):
  NOT_ENOUGH_DATA      expected views < DIAGNOSE_MIN_EXPECTED_VIEWS, or no traffic
  WATCHERS_OFFER_SENT  watchers, no sales, an offer already went out: price
                       is above the market, suggest one from sold comps
  WATCHERS             watchers, no sales, no offer yet: send one, but only if
                       sold comps support the offer price (else PRICE)
  PRICE                enough views, no sales: compare with sold comps
  SEARCH_CARD          CTR well below the category: total price vs
                       competitors, and the thumbnail
  VISIBILITY           impressions well below the category: what competitors
                       have that we lack
  OK                   nothing to fix

Benchmarks are our own per-category averages until eBay's listing quality
report is ingested (next PR), and are labelled as such. Rules carried over
from the weekly check (pipeline/reprice.py): missing traffic or watcher data
is "unknown", never zero, and a low click-through is never answered with a
price cut.
"""
import statistics
from datetime import datetime, timedelta, timezone

import config
import db
import profit
from ebay import analytics
from pipeline import competitors

ORDER = ("NOT_ENOUGH_DATA", "WATCHERS_OFFER_SENT", "WATCHERS", "PRICE", "SEARCH_CARD", "VISIBILITY", "OK")
LABEL = {"NOT_ENOUGH_DATA": "NOT ENOUGH DATA", "WATCHERS_OFFER_SENT": "WATCHERS, NO SALES, OFFER SENT",
         "WATCHERS": "WATCHERS, NO SALES", "PRICE": "PRICE", "SEARCH_CARD": "SEARCH CARD",
         "VISIBILITY": "VISIBILITY", "OK": "OK"}
ICON = {"NOT_ENOUGH_DATA": "⏳", "WATCHERS_OFFER_SENT": "💸", "WATCHERS": "👀", "PRICE": "🏷️",
        "SEARCH_CARD": "🖼️", "VISIBILITY": "🔦", "OK": "✅"}


# --- metrics -------------------------------------------------------------------

def _when(raw) -> datetime | None:
    if not raw:
        return None
    try:
        d = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def traffic_for(items: list[dict], days: int, batch: int = 20) -> dict[str, dict | None]:
    """{listing_id: traffic or None}. A failed batch is None (unknown); a
    listing missing from a batch that succeeded had no traffic: a real zero."""
    ids = [str(i["ebay"]["listing_id"]) for i in items]
    out: dict[str, dict | None] = {}
    for start in range(0, len(ids), batch):
        chunk = ids[start:start + batch]
        try:
            got = analytics.get_traffic_report(chunk, days)
        except Exception as e:
            print(f"diagnose: traffic batch failed ({type(e).__name__})")
            out.update({lid: None for lid in chunk})
            continue
        out.update({lid: got.get(lid) or {"impressions": 0, "views": 0} for lid in chunk})
    return out


def metrics(item: dict, traffic: dict | None, watchers: int | None, offered: bool,
            now: datetime, days: int) -> dict:
    ebay = item.get("ebay") or {}
    published = _when(ebay.get("published_at"))
    live_days = max((now - published).total_seconds() / 86400, 0.5) if published else float(days)
    window = min(float(days), live_days)
    since = now - timedelta(days=window)
    sales = sum(int(s.get("units") or 1) for s in ebay.get("sales") or []
                if (_when(s.get("sold_at")) or datetime.min.replace(tzinfo=timezone.utc)) >= since)
    m = {"item_id": item["item_id"], "listing_id": str(ebay.get("listing_id")),
         "title": (item.get("listing") or {}).get("title") or "",
         "price": (item.get("listing") or {}).get("price"),
         "category_id": str((item.get("listing") or {}).get("category_id") or ""),
         "days": round(window, 1), "watchers": watchers, "offered": offered, "sales": sales,
         "traffic_known": traffic is not None, "impressions": None, "views": None,
         "impressions_per_day": None, "ctr": None}
    if traffic is not None:
        imp, views = int(traffic.get("impressions") or 0), int(traffic.get("views") or 0)
        m.update(impressions=imp, views=views, impressions_per_day=imp / window,
                 ctr=(views / imp) if imp else None)
    return m


def benchmarks(all_metrics: list[dict]) -> dict[str, dict]:
    """Per category: our average impressions/day and CTR over listings with
    known traffic (CTR as total views / total impressions). Labelled
    'store average' until eBay's report benchmarks are ingested."""
    by: dict[str, list[dict]] = {}
    for m in all_metrics:
        if m["traffic_known"]:
            by.setdefault(m["category_id"], []).append(m)
    out = {}
    for cat, ms in by.items():
        imp = sum(m["impressions"] for m in ms)
        out[cat] = {"impressions_per_day": statistics.mean(m["impressions_per_day"] for m in ms),
                    "ctr": (sum(m["views"] for m in ms) / imp) if imp else None,
                    "n": len(ms), "source": "store average"}
    return out


# --- verdict -----------------------------------------------------------------------

def verdict(m: dict, bench: dict | None) -> str:
    """The primary verdict code from the numbers alone (comps come after)."""
    if not m["traffic_known"]:
        return "NOT_ENOUGH_DATA"
    ctr_ref = (bench or {}).get("ctr") or config.DIAGNOSE_FALLBACK_CTR
    if m["impressions"] * ctr_ref < config.DIAGNOSE_MIN_EXPECTED_VIEWS:
        return "NOT_ENOUGH_DATA"
    no_sales = m["sales"] == 0
    if no_sales and m["watchers"]:
        return "WATCHERS_OFFER_SENT" if m["offered"] else "WATCHERS"
    if no_sales and m["views"] >= config.DIAGNOSE_MIN_VIEWS_FOR_PRICE:
        return "PRICE"
    if bench and bench.get("ctr") and m["ctr"] is not None \
            and m["ctr"] < bench["ctr"] * config.DIAGNOSE_LOW_FACTOR:
        return "SEARCH_CARD"
    if bench and m["impressions_per_day"] < bench["impressions_per_day"] * config.DIAGNOSE_LOW_FACTOR:
        return "VISIBILITY"
    return "OK"


def _charm(x: float) -> float:
    return max(int(x) - 0.01, 0.99) if x >= 1 else round(x, 2)


def sold_summary(comps: dict) -> dict | None:
    sold = [r for r in comps.get("sold") or [] if r.get("price")]
    if not sold:
        return None
    prices = sorted(r["price"] for r in sold)
    return {"n": len(prices), "median": statistics.median(prices), "low": prices[0], "high": prices[-1],
            "how": comps["found_by"].get("sold") or "?"}


def _suggested_price(item: dict, s: dict) -> tuple[float, float | None]:
    floor = profit.breakeven_price(item, config.OFFER_MIN_NET)
    price = _charm(s["median"])
    return (max(price, _charm(floor + 1)) if floor else price), floor


def explain(code: str, m: dict, bench: dict | None, item: dict, comps: dict | None) -> tuple[str, str, str]:
    """(verdict, reason, fix), one line each. WATCHERS becomes PRICE when the
    sold comps sit below the offer price: then the fix is a price, not an offer."""
    b = bench or {}
    src = b.get("source", "store average")
    if code == "NOT_ENOUGH_DATA":
        if not m["traffic_known"]:
            return code, "eBay's traffic couldn't be read.", "Run /diagnose again later."
        return (code, f"{m['impressions']} impressions in {m['days']:g} days: too few clicks to judge yet.",
                "Wait; check again in a week.")
    s = sold_summary(comps) if comps else None
    if code in ("WATCHERS_OFFER_SENT", "WATCHERS", "PRICE"):
        who = (f"{m['watchers']} watching, " if m["watchers"] else "") + f"{m['views']} views, no sales"
        if not s or s["n"] < config.DIAGNOSE_MIN_SOLD_COMPS:
            n = s["n"] if s else 0
            if comps and comps.get("sold_skipped"):
                extra = " (sold comps skipped this run: Apify budget; /diagnose this listing alone for them)"
            elif comps:
                extra = f" ({n} sold comp{'s' if n != 1 else ''} in 90 days, too few for a price)"
            else:
                extra = ""
            if code == "WATCHERS":
                return code, f"{who}.{extra}", "Send an offer with /offers."
            return code, f"{who}.{extra}", "Check sold listings by hand before changing the price."
        sug, floor = _suggested_price(item, s)
        comp = (f"sold comps ({s['how']}) median ${s['median']:.2f}, ${s['low']:.2f}–${s['high']:.2f}, "
                f"n={s['n']}")
        price = m["price"] or 0
        if code == "WATCHERS":
            offer = round(price * (1 - config.DIAGNOSE_OFFER_PCT / 100), 2)
            if s["median"] >= offer:
                return code, f"{who}; {comp}.", f"Send an offer around ${offer:.2f} with /offers."
            code = "PRICE"
        floor_note = f" (floor ${floor:.2f})" if floor else ""
        if code == "WATCHERS_OFFER_SENT":
            return (code, f"{who}, an offer already went out; {comp}.",
                    f"Price is above the market: /setprice ${sug:.2f}{floor_note}.")
        return code, f"{who}; {comp}.", f"/setprice ${sug:.2f}{floor_note}, or hold if the comps look off."
    if code == "SEARCH_CARD":
        ours = (m["price"] or 0) + config.EBAY_SHIP_CHARGED
        totals = [r["total"] for r in (comps or {}).get("active") or []
                  if r.get("total") and r.get("found_by") in ("upc", "mpn")]
        vs = (f" Our total ${ours:.2f} vs same-item competitors' median ${statistics.median(totals):.2f}."
              if totals else "")
        return (code, f"CTR {m['ctr']:.2%} vs {b['ctr']:.2%} {src} for the category.{vs}",
                "Check the first photo and the title start; don't cut the price for this.")
    if code == "VISIBILITY":
        gaps = competitor_gaps(item, comps) if comps else []
        return (code, f"{m['impressions_per_day']:.0f} impressions/day vs {b['impressions_per_day']:.0f} "
                f"{src} for the category.",
                ("Competitors have what we lack: " + "; ".join(gaps) + ".") if gaps else
                "Check the category, item specifics and title keywords.")
    return code, "Nothing stands out.", "No action."


def competitor_gaps(item: dict, comps: dict) -> list[str]:
    """What the best-matched competitors have and we don't: more specifics,
    more photos, free shipping."""
    detailed = [r for r in comps.get("active") or [] if r.get("photo_count") is not None]
    if not detailed:
        return []
    gaps = []
    ours_specs = len((item.get("listing") or {}).get("item_specifics") or {})
    theirs_specs = statistics.median(len(r.get("specifics") or {}) for r in detailed)
    if theirs_specs > ours_specs + 2:
        gaps.append(f"{theirs_specs:.0f} item specifics vs our {ours_specs}")
    ours_photos = len(item.get("photos") or [])
    theirs_photos = statistics.median(r["photo_count"] for r in detailed)
    if ours_photos and theirs_photos > ours_photos + 1:
        gaps.append(f"{theirs_photos:.0f} photos vs our {ours_photos}")
    free = [r for r in comps.get("active") or [] if r.get("shipping") == 0]
    if comps.get("active") and len(free) / len(comps["active"]) >= 0.5:
        gaps.append(f"{len(free)} of {len(comps['active'])} offer free shipping")
    return gaps


# --- running it ----------------------------------------------------------------------

def _live(items: list[dict]) -> list[dict]:
    return [i for i in items if i.get("status") == "published" and (i.get("ebay") or {}).get("listing_id")]


def diagnose(item_ids: list[str] | None = None, *, now: datetime | None = None,
             search=competitors.search) -> list[dict]:
    """Verdicts for the given live items (all live items when None), worst
    first. Benchmarks always come from every live listing. Blocking (eBay and
    Apify calls); run in a thread. At most DIAGNOSE_SOLD_MAX sold searches a
    run (Apify costs money); the rest say their comps were skipped."""
    now = now or datetime.now(timezone.utc)
    days = config.DIAGNOSE_DAYS
    live = _live(db.list_items())
    targets = live if item_ids is None else [i for i in live if i["item_id"] in set(item_ids)]
    traffic = traffic_for(live, days)
    offered = db.offers_sent_since((now - timedelta(days=days)).isoformat())
    watch_cache: dict[str, int | None] = {}

    def watchers(lid):
        if lid not in watch_cache:
            try:
                watch_cache[lid] = analytics.get_watch_count(lid)
            except Exception:
                watch_cache[lid] = None
        return watch_cache[lid]

    all_m = [metrics(i, traffic.get(str(i["ebay"]["listing_id"])), None, False, now, days) for i in live]
    bench = benchmarks(all_m)
    results, sold_budget = [], config.DIAGNOSE_SOLD_MAX
    for item in targets:
        lid = str(item["ebay"]["listing_id"])
        m = metrics(item, traffic.get(lid), watchers(lid), lid in offered, now, days)
        b = bench.get(m["category_id"])
        code = verdict(m, b)
        comps = None
        if code not in ("NOT_ENOUGH_DATA", "OK"):
            need_sold = code in ("WATCHERS_OFFER_SENT", "WATCHERS", "PRICE") and sold_budget > 0
            sold_budget -= need_sold
            listing = item.get("listing") or {}
            try:
                comps = search(item.get("identification"), specifics=listing.get("item_specifics"),
                               title=listing.get("title"), item_id=item["item_id"], sold=need_sold)
            except Exception as e:
                print(f"diagnose: competitor search failed for {item['item_id'][:8]}: {type(e).__name__}")
            if comps is not None and code in ("WATCHERS_OFFER_SENT", "WATCHERS", "PRICE") and not need_sold:
                comps = {**comps, "sold_skipped": True}
        code, reason, fix = explain(code, m, b, item, comps)
        results.append({**m, "verdict": code, "reason": reason, "fix": fix,
                        "benchmark": b, "comps_found": bool(comps and (comps["active"] or comps["sold"]))})
    results.sort(key=lambda r: (ORDER.index(r["verdict"]), -(r["impressions"] or 0)))
    return results


def format_line(r: dict) -> str:
    return (f"{ICON[r['verdict']]} {LABEL[r['verdict']]} · {r['item_id'][:8]} {r['title'][:45]}\n"
            f"   {r['reason']}\n   → {r['fix']}")


def format_detail(r: dict) -> list[str]:
    b = r.get("benchmark") or {}
    ctr = f"{r['ctr']:.2%}" if r.get("ctr") is not None else "n/a"
    lines = [f"{ICON[r['verdict']]} {LABEL[r['verdict']]} · {r['item_id'][:8]} (eBay {r['listing_id']})",
             r["title"], "",
             f"Last {r['days']:g} days: {r['impressions'] if r['impressions'] is not None else '?'} impressions"
             f" ({(r['impressions_per_day'] or 0):.0f}/day), {r['views'] if r['views'] is not None else '?'} views,"
             f" CTR {ctr}; watchers {r['watchers'] if r['watchers'] is not None else 'unknown'};"
             f" sales {r['sales']}; offer sent: {'yes' if r['offered'] else 'no'}."]
    if b:
        lines.append(f"Category ({b.get('source')}, {b.get('n')} listings): "
                     f"{b['impressions_per_day']:.0f} impressions/day"
                     + (f", CTR {b['ctr']:.2%}" if b.get("ctr") else "") + ".")
    lines += ["", f"Why: {r['reason']}", f"Fix: {r['fix']}"]
    return lines
