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
  CATEGORY_FIX         eBay's report recommends something for the category
                       that this listing lacks (free shipping, ad rate,
                       specifics). Account-wide, so it's last; when another
                       verdict is primary it's shown as an extra line.
                       Between reports two checks from eBay's APIs add to
                       it: recommended specifics from Taxonomy (every
                       listing), and eBay's suggested ad rate (Recommendation
                       API) for a listing the report doesn't cover.
  OK                   nothing to fix

Benchmarks: eBay's listing quality report (sent to the bot as the xlsx,
pipeline/quality_report.py) while it is at most QUALITY_REPORT_MAX_AGE_DAYS
old. The report's CTR and impressions are compared only with the report's
own figures (its definitions may differ from the Analytics API's), so
SEARCH CARD and VISIBILITY use the listing's report row. A listing newer
than the report, or with no report, falls back to our own per-category
averages, labelled "store average". Rules carried over from the weekly
check (pipeline/reprice.py): missing traffic or watcher data is "unknown",
never zero, and a low click-through is never answered with a price cut.

Conversion (sales / views, from the traffic report's TRANSACTION) is shown,
not judged: at about one sale a listing a month it is too noisy for a verdict.
"""
import collections
import statistics
from datetime import datetime, timedelta, timezone

import config
import db
import profit
from ebay import analytics, marketing, recommendation, taxonomy
from pipeline import competitors, quality_report

ORDER = ("NOT_ENOUGH_DATA", "WATCHERS_OFFER_SENT", "WATCHERS", "PRICE", "SEARCH_CARD", "VISIBILITY",
         "CATEGORY_FIX", "OK")
LABEL = {"NOT_ENOUGH_DATA": "NOT ENOUGH DATA", "WATCHERS_OFFER_SENT": "WATCHERS, NO SALES, OFFER SENT",
         "WATCHERS": "WATCHERS, NO SALES", "PRICE": "PRICE", "SEARCH_CARD": "SEARCH CARD",
         "VISIBILITY": "VISIBILITY", "CATEGORY_FIX": "CATEGORY FIX", "OK": "OK"}
ICON = {"NOT_ENOUGH_DATA": "⏳", "WATCHERS_OFFER_SENT": "💸", "WATCHERS": "👀", "PRICE": "🏷️",
        "SEARCH_CARD": "🖼️", "VISIBILITY": "🔦", "CATEGORY_FIX": "🧭", "OK": "✅"}


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
         "impressions_per_day": None, "ctr": None, "transactions": None, "conversion": None}
    if traffic is not None:
        imp, views = int(traffic.get("impressions") or 0), int(traffic.get("views") or 0)
        tx = traffic.get("transactions")
        m.update(impressions=imp, views=views, impressions_per_day=imp / window,
                 ctr=(views / imp) if imp else None, transactions=tx,
                 conversion=(tx / views) if tx is not None and views else None)
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
        conv = [m for m in ms if m.get("transactions") is not None]
        conv_views = sum(m["views"] for m in conv)
        out[cat] = {"impressions_per_day": statistics.mean(m["impressions_per_day"] for m in ms),
                    "ctr": (sum(m["views"] for m in ms) / imp) if imp else None,
                    "conversion": (sum(m["transactions"] for m in conv) / conv_views) if conv_views else None,
                    "n": len(ms), "source": "store average"}
    return out


# --- eBay's listing quality report -----------------------------------------------------

def usable_report(report: dict | None, now: datetime) -> tuple[dict | None, str]:
    """(report or None, a one-line note on which benchmarks this run uses)."""
    if not report:
        return None, "Benchmarks: store averages (no eBay listing quality report yet; send the xlsx to the bot)."
    date = _when(report["report_date"])
    age = (now - date).days if date else None
    label = f"eBay report {date:%m/%d}" if date else "eBay report"
    if age is None or age > config.QUALITY_REPORT_MAX_AGE_DAYS:
        return None, (f"Benchmarks: store averages ({label} is {age} days old; send a newer one).")
    return report, f"Benchmarks: {label} ({age} days old)."


def report_context(live: list[dict], report: dict | None) -> dict[str, dict]:
    """{item_id: {"section", "listing" (the report's row or None), "label"}}.
    A listing in the report gets its own row. A newer listing gets its
    category's section from the listings that are in it (by category_id)."""
    if not report:
        return {}
    date = _when(report["report_date"])
    label = f"eBay report {date:%m/%d}" if date else "eBay report"
    sections = {s["section"]: s for s in report["sections"]}
    by_item = {r["item_id"]: r for r in report["listings"] if r.get("item_id")}
    by_listing = {str(r["ebay_item_id"]): r for r in report["listings"]}
    out, votes = {}, collections.defaultdict(collections.Counter)
    for item in live:
        row = by_item.get(item["item_id"]) or by_listing.get(str((item.get("ebay") or {}).get("listing_id")))
        if row and row["section"] in sections:
            out[item["item_id"]] = {"section": sections[row["section"]], "listing": row, "label": label}
            cat = str((item.get("listing") or {}).get("category_id") or "")
            if cat:
                votes[cat][row["section"]] += 1
    for item in live:
        if item["item_id"] in out:
            continue
        cat = str((item.get("listing") or {}).get("category_id") or "")
        if votes.get(cat):
            out[item["item_id"]] = {"section": sections[votes[cat].most_common(1)[0][0]], "listing": None,
                                    "label": label}
    return out


def report_bench(ctx: dict) -> dict:
    """The benchmark dict /diagnose shows, from a report section."""
    s = ctx["section"]
    return {"impressions_per_day": s.get("impressions_per_day"), "ctr": s.get("ctr"),
            "conversion": s.get("conversion"),
            "top_ctr": s.get("top_ctr"), "bottom_ctr": s.get("bottom_ctr"),
            "top_impressions_per_day": s.get("top_impressions_per_day"),
            "bottom_impressions_per_day": s.get("bottom_impressions_per_day"),
            "section": s["section"], "source": ctx["label"]}


def category_fixes(ctx: dict | None, item: dict) -> list[dict]:
    """eBay's recommendations for the listing's section that apply to it:
    [{"section", "kind", "text" (eBay's words), "fix" (our one line)}]."""
    if not ctx:
        return []
    return [{"section": ctx["section"]["section"], "kind": rec.get("kind"), "text": rec.get("text"),
             "source": "report", "fix": quality_report.fix_text(rec, ctx["listing"], item["item_id"])}
            for rec in ctx["section"].get("recommendations") or []
            if quality_report.applies(rec, ctx["listing"], item)]


# --- between reports: eBay's APIs ---------------------------------------------------

def _filled(v) -> bool:
    if isinstance(v, (list, tuple, set)):
        return any(_filled(x) for x in v)
    return v is not None and str(v).strip() not in ("", "Does not apply")


def specifics_check(item: dict) -> dict | None:
    """The listing's category's required + recommended item specifics (eBay
    Taxonomy, cached 30 days) against what the listing has:
    {"recommended": n, "filled": k, "missing": [names]}. None if unknown."""
    listing = item.get("listing") or {}
    cat = str(listing.get("category_id") or "")
    if not cat:
        return None
    try:
        aspects = taxonomy.category_aspects(cat)
    except Exception as e:
        print(f"diagnose: aspects for {cat} failed ({type(e).__name__})")
        return None
    names = []
    for a in aspects or []:
        c = a.get("aspectConstraint") or {}
        name = a.get("localizedAspectName")
        if name and (c.get("aspectRequired") or c.get("aspectUsage") == "RECOMMENDED"):
            names.append(name)
    if not names:
        return None
    have = {str(k).strip().lower() for k, v in (listing.get("item_specifics") or {}).items() if _filled(v)}
    missing = [n for n in names if n.strip().lower() not in have]
    return {"recommended": len(names), "filled": len(names) - len(missing), "missing": missing}


def specifics_fix(check: dict | None, item_id: str, section: str) -> dict | None:
    """A fix when fewer than DIAGNOSE_MIN_RECOMMENDED_SPECIFICS (eBay's report
    asks for 8) of eBay's recommended specifics are filled."""
    if not check:
        return None
    target = min(config.DIAGNOSE_MIN_RECOMMENDED_SPECIFICS, check["recommended"])
    if check["filled"] >= target:
        return None
    shown = check["missing"][:6]
    more = len(check["missing"]) - len(shown)
    return {"section": section, "kind": "specifics", "source": "taxonomy",
            "text": f"Fill at least {target} of eBay's recommended item specifics",
            "fix": (f"eBay Taxonomy: {check['filled']} of {check['recommended']} recommended item specifics "
                    f"filled; missing: {', '.join(shown)}" + (f" and {more} more" if more > 0 else "")
                    + f". /revise {item_id[:8]} with the ones that apply.")}


def ad_rate_fix(suggested: dict | None, current: float | None, section: str) -> dict | None:
    """eBay's suggested ad rate (Recommendation API) when it is at least
    DIAGNOSE_AD_RATE_GAP_PCT points above the listing's own rate."""
    if not suggested:
        return None
    now = current if current is not None else config.EBAY_DEFAULT_AD_RATE_PCT / 100
    rate = suggested["rate"]
    if rate < now + config.DIAGNOSE_AD_RATE_GAP_PCT / 100 - 1e-9:
        return None
    basis = "for this listing" if suggested.get("basis") == "ITEM" else "trending in the category"
    return {"section": section, "kind": "ad_rate", "source": "recommendation",
            "text": "eBay suggests a higher ad rate",
            "fix": (f"eBay suggests ad rate {rate:.1%} ({basis}); this listing: {now:.1%}"
                    + (" (our default)" if current is None else "")
                    + ". A guide, not the best rate: owner decision (plan §6).")}


def _section_name(ctx: dict | None, item: dict, names: dict) -> str:
    if ctx:
        return ctx["section"]["section"]
    cat = str((item.get("listing") or {}).get("category_id") or "")
    if cat not in names:
        try:
            names[cat] = taxonomy.category_name(cat) if cat else None
        except Exception:
            names[cat] = None
    return names[cat] or (f"category {cat}" if cat else "no category")


def api_fixes(item: dict, ctx: dict | None, have: set, suggested: dict | None, current: float | None,
              names: dict) -> list[dict]:
    """The between-report checks for one listing. `have`: kinds the report
    already gave it (the report's own recommendation wins). The ad rate is a
    fallback: only for a listing the report doesn't cover at all, because a
    report section without an ad-rate line means eBay finds ours fine."""
    section = _section_name(ctx, item, names)
    out = []
    if "specifics" not in have:
        out.append(specifics_fix(specifics_check(item), item["item_id"], section))
    if ctx is None and "ad_rate" not in have:
        out.append(ad_rate_fix(suggested, current, section))
    return [f for f in out if f]


_WHO = {"taxonomy": "eBay's recommended item specifics", "recommendation": "eBay's suggested ad rate"}


_HOW = {"free_shipping": "decide through the free-shipping test (plan §5)",
        "ad_rate": "owner decision (plan §6)",
        "specifics": "/diagnose <id> lists the missing ones; add them with /revise"}


def category_summary(results: list[dict]) -> list[str]:
    """/diagnose all: eBay's category recommendations once per section, with
    how many listings each applies to (not repeated on every listing)."""
    groups: dict[tuple, int] = collections.Counter()
    for r in results:
        for f in r.get("category_fixes") or []:
            groups[(f["section"], f["kind"], f["text"])] += 1
    lines = []
    for (section, kind, text), n in sorted(groups.items(), key=lambda g: -g[1]):
        how = _HOW.get(kind)
        lines.append(f"  {section}, {n} listing{'s' if n != 1 else ''}: {text}" + (f" → {how}." if how else "."))
    return lines


def _below(value, ours, bottom) -> bool:
    """Under LOW_FACTOR x our category figure, or under eBay's bottom 10%."""
    if value is None:
        return False
    return bool((ours and value < ours * config.DIAGNOSE_LOW_FACTOR) or (bottom and value < bottom))


# --- verdict -----------------------------------------------------------------------

def verdict(m: dict, bench: dict | None, ctx: dict | None = None, fixes=()) -> str:
    """The primary verdict code from the numbers alone (comps come after).
    `ctx` is the listing's report context (report_context), `fixes` its
    applicable category recommendations."""
    if not m["traffic_known"]:
        return "NOT_ENOUGH_DATA"
    section = (ctx or {}).get("section") or {}
    ctr_ref = section.get("ctr") or (bench or {}).get("ctr") or config.DIAGNOSE_FALLBACK_CTR
    if m["impressions"] * ctr_ref < config.DIAGNOSE_MIN_EXPECTED_VIEWS:
        return "NOT_ENOUGH_DATA"
    no_sales = m["sales"] == 0
    if no_sales and m["watchers"]:
        return "WATCHERS_OFFER_SENT" if m["offered"] else "WATCHERS"
    if no_sales and m["views"] >= config.DIAGNOSE_MIN_VIEWS_FOR_PRICE:
        return "PRICE"
    row = (ctx or {}).get("listing")
    if row:
        # The report's own numbers against the report's benchmarks.
        if _below(row.get("ctr"), section.get("ctr"), section.get("bottom_ctr")):
            return "SEARCH_CARD"
        if _below(row.get("impressions_per_day"), section.get("impressions_per_day"),
                  section.get("bottom_impressions_per_day")):
            return "VISIBILITY"
    else:
        if bench and bench.get("ctr") and m["ctr"] is not None \
                and m["ctr"] < bench["ctr"] * config.DIAGNOSE_LOW_FACTOR:
            return "SEARCH_CARD"
        if bench and m["impressions_per_day"] < bench["impressions_per_day"] * config.DIAGNOSE_LOW_FACTOR:
            return "VISIBILITY"
    if fixes:
        return "CATEGORY_FIX"
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


def explain(code: str, m: dict, bench: dict | None, item: dict, comps: dict | None,
            ctx: dict | None = None, fixes=()) -> tuple[str, str, str]:
    """(verdict, reason, fix), one line each. WATCHERS becomes PRICE when the
    sold comps sit below the offer price: then the fix is a price, not an offer."""
    b = bench or {}
    src = b.get("source", "store average")
    row = (ctx or {}).get("listing")
    section = (ctx or {}).get("section") or {}
    if code == "CATEGORY_FIX":
        first = fixes[0] if fixes else {}
        where = first.get("section") or section.get("section", "the category")
        if first.get("source") in _WHO:
            return code, f"{_WHO[first['source']]} for {where}.", first["fix"]
        return (code, f"eBay's report recommends this for {where}.",
                first.get("fix") or "See eBay's listing quality report.")
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
        if row:
            why = (f"CTR {row['ctr']:.2%} ({ctx['label']}, 31 days) vs {section['ctr']:.2%} for our "
                   f"{section['section']} listings" + _top_bottom(section, "ctr", "{:.2%}") + ".")
        else:
            why = f"CTR {m['ctr']:.2%} vs {b['ctr']:.2%} {src} for the category."
        return (code, why + vs, "Check the first photo and the title start; don't cut the price for this.")
    if code == "VISIBILITY":
        gaps = competitor_gaps(item, comps) if comps else []
        if row:
            why = (f"{row['impressions_per_day']:.0f} impressions/day ({ctx['label']}) vs "
                   f"{section['impressions_per_day']:.0f} for our {section['section']} listings"
                   + _top_bottom(section, "impressions_per_day", "{:.0f}") + ".")
        else:
            why = (f"{m['impressions_per_day']:.0f} impressions/day vs {b['impressions_per_day']:.0f} "
                   f"{src} for the category.")
        return (code, why,
                ("Competitors have what we lack: " + "; ".join(gaps) + ".") if gaps else
                "Check the category, item specifics and title keywords.")
    return code, "Nothing stands out.", "No action."


def _top_bottom(section: dict, metric: str, fmt: str) -> str:
    top, bottom = section.get(f"top_{metric}"), section.get(f"bottom_{metric}")
    parts = ([f"top 10% {fmt.format(top)}"] if top is not None else []) + \
            ([f"bottom 10% {fmt.format(bottom)}"] if bottom is not None else [])
    return f" (eBay: {', '.join(parts)})" if parts else ""


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
             search=competitors.search, info: dict | None = None) -> list[dict]:
    """Verdicts for the given live items (all live items when None), worst
    first. Benchmarks always come from every live listing. Blocking (eBay and
    Apify calls); run in a thread. At most DIAGNOSE_SOLD_MAX sold searches a
    run (Apify costs money); the rest say their comps were skipped. `info`,
    if given, gets "benchmarks": a line saying which benchmarks were used."""
    now = now or datetime.now(timezone.utc)
    days = config.DIAGNOSE_DAYS
    live = _live(db.list_items())
    targets = live if item_ids is None else [i for i in live if i["item_id"] in set(item_ids)]
    report, note = usable_report(db.latest_quality_report(), now)
    if info is not None:
        info["benchmarks"] = note
    contexts = report_context(live, report)
    traffic = traffic_for(live, days)
    # eBay's suggested ad rate, only for listings the report doesn't cover.
    uncovered = [str(i["ebay"]["listing_id"]) for i in targets if i["item_id"] not in contexts]
    suggested, rates = {}, {}
    if uncovered:
        try:
            suggested = recommendation.suggested_ad_rates(uncovered)
        except Exception as e:
            print(f"diagnose: suggested ad rates failed ({type(e).__name__})")
        if suggested:
            try:
                rates = marketing.ad_rates()
            except Exception as e:
                print(f"diagnose: our ad rates failed ({type(e).__name__}); using the default")
    names: dict = {}
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
        ctx = contexts.get(item["item_id"])
        fixes = category_fixes(ctx, item)
        sku = (item.get("ebay") or {}).get("sku")
        current = rates.get(lid, rates.get(str(sku)) if sku else None)
        fixes += api_fixes(item, ctx, {f["kind"] for f in fixes}, suggested.get(lid), current, names)
        # A listing in the report is judged on the report's numbers.
        b = report_bench(ctx) if ctx and ctx["listing"] else bench.get(m["category_id"])
        code = verdict(m, bench.get(m["category_id"]), ctx, fixes)
        comps = None
        if code not in ("NOT_ENOUGH_DATA", "CATEGORY_FIX", "OK"):
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
        code, reason, fix = explain(code, m, b, item, comps, ctx, fixes)
        also = [f["fix"] for f in (fixes if code != "CATEGORY_FIX" else fixes[1:])]
        results.append({**m, "verdict": code, "reason": reason, "fix": fix, "also": also,
                        "category_fixes": fixes,
                        "benchmark": b, "report": (ctx or {}).get("listing"),
                        "comps_found": bool(comps and (comps["active"] or comps["sold"]))})
    results.sort(key=lambda r: (ORDER.index(r["verdict"]), -(r["impressions"] or 0)))
    return results


def format_line(r: dict, also: bool = True) -> str:
    return (f"{ICON[r['verdict']]} {LABEL[r['verdict']]} · {r['item_id'][:8]} {r['title'][:45]}\n"
            f"   {r['reason']}\n   → {r['fix']}"
            + ("".join(f"\n   {ICON['CATEGORY_FIX']} {a}" for a in r.get("also") or []) if also else ""))


def format_detail(r: dict) -> list[str]:
    b = r.get("benchmark") or {}
    ctr = f"{r['ctr']:.2%}" if r.get("ctr") is not None else "n/a"
    lines = [f"{ICON[r['verdict']]} {LABEL[r['verdict']]} · {r['item_id'][:8]} (eBay {r['listing_id']})",
             r["title"], "",
             f"Last {r['days']:g} days: {r['impressions'] if r['impressions'] is not None else '?'} impressions"
             f" ({(r['impressions_per_day'] or 0):.0f}/day), {r['views'] if r['views'] is not None else '?'} views,"
             f" CTR {ctr}"
             + (f", conversion {r['conversion']:.1%} ({r['transactions']} sales)" if r.get("conversion") is not None
                else "")
             + f"; watchers {r['watchers'] if r['watchers'] is not None else 'unknown'};"
             f" sales {r['sales']}; offer sent: {'yes' if r['offered'] else 'no'}."]
    rep = r.get("report")
    if rep:
        lines.append(f"{b.get('source', 'eBay report')} (31 days): "
                     f"{rep['impressions_per_day'] or 0:.0f} impressions/day, CTR "
                     + (f"{rep['ctr']:.2%}" if rep.get("ctr") is not None else "n/a")
                     + (f", conversion {rep['conversion']:.1%}" if rep.get("conversion") is not None else "")
                     + f", {rep.get('sales_31d') or 0} sales, {rep.get('watchers') if rep.get('watchers') is not None else '?'}"
                     " watchers.")
    if b and b.get("section"):
        lines.append(f"{b['section']} ({b['source']}): our listings {b['impressions_per_day']:.0f} "
                     f"impressions/day, CTR {b['ctr']:.2%}"
                     + (f", conversion {b['conversion']:.1%}" if b.get("conversion") is not None else "")
                     + (f"; top 10% {b['top_impressions_per_day']:.0f}/day, {b['top_ctr']:.2%}"
                        if b.get("top_ctr") is not None and b.get("top_impressions_per_day") is not None else "")
                     + (f"; bottom 10% {b['bottom_impressions_per_day']:.0f}/day, {b['bottom_ctr']:.2%}"
                        if b.get("bottom_ctr") is not None and b.get("bottom_impressions_per_day") is not None
                        else "") + ".")
    elif b:
        lines.append(f"Category ({b.get('source')}, {b.get('n')} listings): "
                     f"{b['impressions_per_day']:.0f} impressions/day"
                     + (f", CTR {b['ctr']:.2%}" if b.get("ctr") else "")
                     + (f", conversion {b['conversion']:.1%}" if b.get("conversion") is not None else "") + ".")
    lines += ["", f"Why: {r['reason']}", f"Fix: {r['fix']}"]
    lines += [f"{ICON['CATEGORY_FIX']} {a}" for a in r.get("also") or []]
    return lines
