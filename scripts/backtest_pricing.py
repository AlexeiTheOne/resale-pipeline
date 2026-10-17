"""Backtest pricing v2 against the prices the owner actually set.

For items the owner priced (status published or sold), run get_pricing_v2 on
the stored identification, receipt and first stored photo, and compare:
  v1 machine_price (as stored at the time, NOT re-run), v2 code price,
  Claude's own price, the owner's listed price, and for sold items the sold
  price per unit.

Read-only on items. The only DB writes are the llm_usage rows claude_llm logs
for the judge calls. No eBay write calls. Apify and Browse results are cached
under data/cache/pricing_v2/, so a rerun of the same items costs nothing.

    python scripts/backtest_pricing.py --items 2f57c0f4,15d1415f
    python scripts/backtest_pricing.py --limit 10 --claude-cap 2 --apify-cap 2

Scoring is by OUTCOME, not by agreement with the owner, whose prices are
MSRP-and-feel, not ground truth (score_outcome):
  sold          the sold price is the achievable price: hit within +-20%. If it
                sold within FAST_DAYS of listing it was probably underpriced, so
                up to +40% above the sold price is also a hit.
  unsold, listed STALE_DAYS+   the owner's price is probably too high: at or
                below it (+5% for rounding) is a hit, above it is a miss.
  unsold, newer not scored, reported only.
Methods scored side by side: v2 this run, an earlier run (--compare-csv), v1's
stored machine price, and MSRP x brand ratio alone (pipeline/msrp.py,
leave-one-out).

Hard caps: before each item, the run stops if Claude spend so far plus a
worst-case item would pass --claude-cap; Apify refuses any uncached call whose
worst-case cost would pass --apify-cap (estimated from the actor's per-result
rate, see config.APIFY_SOLD_COST_PER_RESULT).
"""
import argparse
import csv
import json
import math
import os
import sqlite3
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)  # db.DB_PATH is relative: llm_usage rows must land in data/ross.db

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from pipeline import pricing_v2 as v2  # noqa: E402

DB = ROOT / "data" / "ross.db"
FAST_DAYS, STALE_DAYS = 7, 60
SOLD_TOL, FAST_UP, STALE_TOL = 0.20, 0.40, 0.05


def _ts(v):
    if not v:
        return None
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None


def score_outcome(price, *, status, owner, sold, days):
    """"hit" / "miss" / None (not scored). See the module docstring."""
    if status == "sold" and sold:
        if not price:
            return "miss"
        r = price / sold
        upper = 1 + (FAST_UP if days is not None and days <= FAST_DAYS else SOLD_TOL)
        return "hit" if 1 - SOLD_TOL <= r <= upper else "miss"
    if status != "sold" and days is not None and days >= STALE_DAYS:
        if not price:
            return "miss"
        return "hit" if price <= owner * (1 + STALE_TOL) else "miss"
    return None
OUT_DIR = ROOT / "data" / "cache" / "backtest"
# Headroom kept per item before the Claude cap: a Sonnet judge call plus an
# Opus escalation measured $0.06-0.09 (max 0.088) over 40 items on 17 Oct.
CLAUDE_ITEM_WORST = 0.10


def load_items(prefixes, limit):
    con = sqlite3.connect(f"file:{DB.as_posix()}?mode=ro", uri=True)
    rows = con.execute("SELECT item_id, status, identification, listing, pricing, ebay, receipt, "
                       "photos, created_at FROM items WHERE status IN ('published','sold') "
                       "ORDER BY item_id").fetchall()
    con.close()
    now = datetime.now(timezone.utc)
    items = []
    for item_id, status, ident, listing, pricing, ebay, receipt, photos, created in rows:
        if prefixes and not any(item_id.startswith(p) for p in prefixes):
            continue
        ident, listing = json.loads(ident or "{}"), json.loads(listing or "{}")
        if not ident or not listing.get("price"):
            continue
        e = json.loads(ebay or "{}")
        sold = None
        if status == "sold" and e.get("sale_price"):
            sold = round(e["sale_price"] / max(1, e.get("units_sold") or 1), 2)
        # Listed = published_at; items listed before that was recorded fall back
        # to created_at (capture), which is the same day or the day before.
        listed = _ts(e.get("published_at")) or _ts(created)
        end = _ts(e.get("sold_at")) if status == "sold" else now
        days = round((end - listed).total_seconds() / 86400, 1) if listed and end else None
        photo_list = json.loads(photos or "[]")
        inbox = ROOT / "data" / "inbox" / item_id
        if inbox.is_dir():
            photo_list = sorted(str(p) for p in inbox.glob("*.jpg")) + photo_list
        items.append({
            "item_id": item_id, "status": status, "ident": ident,
            "receipt": json.loads(receipt or "null"), "photos": photo_list,
            "owner_price": listing["price"], "sold_price": sold,
            "category_id": listing.get("category_id"),
            "v1_price": json.loads(pricing or "{}").get("machine_price"),
            "days": days,
        })
    if prefixes:  # keep the order given on the command line
        items.sort(key=lambda it: next(i for i, p in enumerate(prefixes) if it["item_id"].startswith(p)))
    return items[:limit] if limit else items


def claude_spend(since_iso, item_id=None):
    con = sqlite3.connect(f"file:{DB.as_posix()}?mode=ro", uri=True)
    sql = "SELECT COALESCE(SUM(cost_usd),0), COUNT(*) FROM llm_usage WHERE ts >= ? AND stage = ?"
    args = [since_iso, v2.JUDGE_STAGE]
    if item_id:
        sql += " AND item_id = ?"
        args.append(item_id)
    cost, calls = con.execute(sql, args).fetchone()
    con.close()
    return float(cost), int(calls)


def ratio(a, b):
    return round(a / b, 3) if a and b else None


def within(rows, key, pct):
    vals = [r[key] for r in rows if r[key] is not None]
    if not vals:
        return None, 0
    return round(sum(1 for v in vals if abs(v - 1) <= pct) / len(vals), 2), len(vals)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--items", help="comma-separated item id prefixes")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--claude-cap", type=float, default=2.0)
    ap.add_argument("--apify-cap", type=float, default=2.0)
    ap.add_argument("--compare-csv", default="",
                    help="comma-separated earlier backtest CSVs; their v2_price is scored "
                         "as 'previous' (later files win per item)")
    ap.add_argument("--one-sold-query", action="store_true",
                    help="for items not already cached, send only the search_query "
                         "sold query to Apify (cached queries are still used)")
    args = ap.parse_args()
    if not args.items and not args.limit:
        raise SystemExit("refusing to run every item: pass --items or --limit")

    prefixes = [p.strip() for p in (args.items or "").split(",") if p.strip()]
    items = load_items(prefixes, args.limit)
    v2.apify_cap_usd = args.apify_cap
    from pipeline import msrp as msrp_mod
    history = msrp_mod.load_history()
    previous = {}
    for path in [p for p in args.compare_csv.split(",") if p.strip()]:
        with open(path.strip(), encoding="utf-8") as f:
            for prow in csv.DictReader(f):
                previous[prow["item"]] = float(prow["v2_price"]) if prow.get("v2_price") else None
    if args.one_sold_query:
        v2.uncached_sold_queries = {"search_query"}
    start = datetime.now(timezone.utc).isoformat()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    rows, stopped = [], None

    for it in items:
        spent, _ = claude_spend(start)
        if spent + CLAUDE_ITEM_WORST > args.claude_cap:
            stopped = f"Claude cap: ${spent:.3f} spent, next item could reach ${args.claude_cap}"
            break
        t = time.perf_counter()
        apify_before = v2.apify_spend["usd"]
        esc_before = v2.judge_stats["escalations"]
        err = None
        try:
            r = v2.get_pricing_v2(it["ident"], photo_paths=it["photos"], receipt=it["receipt"],
                                  item_id=it["item_id"])
        except v2.ApifyBudgetExceeded as e:
            stopped = f"Apify cap: {e}"
            break
        except Exception as e:      # one bad item must not end the run
            r, err = {}, f"{type(e).__name__}: {str(e)[:200]}"
        cost, calls = claude_spend(start, it["item_id"])
        owner, sold = it["owner_price"], it["sold_price"]
        price, formula = r.get("suggested_price"), r.get("formula_price")
        store = msrp_mod.store_estimate(it["ident"], it["receipt"], it["item_id"], history)
        judged = r.get("judged_comps") or []
        row = {
            "item": it["item_id"][:8], "status": it["status"], "category_id": it["category_id"],
            "brand": it["ident"].get("brand"), "type": it["ident"].get("item_type"),
            "candidates": r.get("candidate_count"),
            "same_sold": r.get("same_item_sold_count"), "same_active": r.get("same_item_active_count"),
            "own_dropped": (r.get("dropped") or {}).get("own"),
            "variant_sold": sum(1 for c in judged if c["source"] == "sold"
                                and c["verdict"] == "variant"),
            "days": it["days"],
            "msrp": store["msrp"], "msrp_flags": ",".join(store["msrp_flags"]),
            "ratio_level": f"{store['level']}:{store['key']}:{store['n']}",
            "msrp_est": store["estimate"],
            "prev_price": previous.get(it["item_id"][:8]),
            "sold_queries": len(r.get("rungs_tried") or []),
            # v2_price IS Claude's price now; formula_price is the cross-check.
            "v1_price": it["v1_price"], "v2_price": price, "formula_price": formula,
            "claude_conf": r.get("claude_confidence"),
            "formula_disagrees": "formula_disagrees" in (r.get("review_flags") or []),
            "owner_price": owner, "sold_price": sold,
            "v1_ratio": ratio(it["v1_price"], owner), "v2_ratio": ratio(price, owner),
            "formula_ratio": ratio(formula, owner),
            "v2_vs_sold": ratio(price, sold), "formula_vs_sold": ratio(formula, sold),
            "v1_vs_sold": ratio(it["v1_price"], sold),
            "evidence_ids": " ".join((r.get("claude_evidence") or {}).get("ids") or []),
            "judge_model": r.get("judge_model"), "escalated": v2.judge_stats["escalations"] > esc_before,
            "thumbnails": r.get("thumbnails_used"), "claude_calls": calls,
            "claude_cost": round(cost, 4), "apify_cost_est": round(v2.apify_spend["usd"] - apify_before, 4),
            "seconds": round(time.perf_counter() - t, 1),
            "flags": ",".join(r.get("review_flags") or []),
            "reasoning": (r.get("claude_reasoning") or "")[:200],
            "disagreements": " | ".join(r.get("disagreements") or [])[:300],
            "error": err,
        }
        rows.append(row)
        detail = OUT_DIR / f"{stamp}-{it['item_id'][:8]}.json"
        detail.write_text(json.dumps(r, indent=1, default=str), encoding="utf-8")
        print(f"{row['item']} {row['status']:9} cands={row['candidates']} same_sold={row['same_sold']} "
              f"v1={row['v1_price']} v2={row['v2_price']} formula={row['formula_price']} "
              f"owner={owner} sold={sold} ${cost:.3f} {err or ''}", flush=True)

    out = OUT_DIR / f"backtest-{stamp}.csv"
    if rows:
        with out.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)

    total_claude, total_calls = claude_spend(start)
    summary = {
        "items_run": len(rows), "stopped": stopped, "csv": str(out),
        "claude_cost_total": round(total_claude, 4), "claude_calls": total_calls,
        "claude_cost_per_item": round(total_claude / len(rows), 4) if rows else None,
        "escalations": v2.judge_stats["escalations"],
        "thumbnail_fallbacks": v2.judge_stats["thumbnail_fallbacks"],
        "apify_cost_est_total": round(v2.apify_spend["usd"], 4),
        "apify_calls": v2.apify_spend["calls"], "apify_rows": v2.apify_spend["rows"],
        "apify_cache_hits": v2.apify_spend["cache_hits"],
    }
    methods = {"v2": "v2_price", "previous": "prev_price", "v1": "v1_price",
               "msrp_x_ratio": "msrp_est"}
    for r in rows:
        for m, col in methods.items():
            r[f"score_{m}"] = score_outcome(r[col], status=r["status"], owner=r["owner_price"],
                                            sold=r["sold_price"], days=r["days"])
    groups = {"sold": [r for r in rows if r["status"] == "sold"],
              "unsold_stale": [r for r in rows if r["status"] != "sold" and r["days"] is not None
                               and r["days"] >= STALE_DAYS],
              "unsold_new": [r for r in rows if r["status"] != "sold" and not (
                  r["days"] is not None and r["days"] >= STALE_DAYS)]}
    summary["groups"] = {g: len(v) for g, v in groups.items()}
    summary["sold_fast"] = sum(1 for r in groups["sold"] if r["days"] is not None
                               and r["days"] <= FAST_DAYS)
    table = {}
    for m in methods:
        t = {}
        for g in ("sold", "unsold_stale"):
            scored = [r[f"score_{m}"] for r in groups[g] if r[f"score_{m}"]]
            t[g] = f"{scored.count('hit')}/{len(scored)}"
        allsc = [r[f"score_{m}"] for r in rows if r[f"score_{m}"]]
        t["all_scored"] = f"{allsc.count('hit')}/{len(allsc)}"
        t["hit_rate"] = round(allsc.count("hit") / len(allsc), 2) if allsc else None
        vs = [r[methods[m]] / r["sold_price"] for r in groups["sold"] if r[methods[m]]]
        t["median_vs_sold"] = round(statistics.median(vs), 3) if vs else None
        t["priced"] = sum(1 for r in rows if r[methods[m]])
        table[m] = t
    summary["outcome"] = table
    summary["v2_far_below_owner_on_stale"] = [
        r["item"] for r in groups["unsold_stale"] if r["v2_price"] and r["v2_price"] < 0.5 * r["owner_price"]]
    summary["unsold_new_report"] = [{k: r[k] for k in ("item", "days", "v2_price", "owner_price",
                                                        "msrp_est")} for r in groups["unsold_new"]]
    summary["msrp_coverage"] = msrp_mod.coverage(history)
    summary["ratio_table"] = msrp_mod.ratio_table(history)[:10]
    summary["comps"] = {"same_item_sold": sum(r["same_sold"] or 0 for r in rows),
                        "variant_sold": sum(r["variant_sold"] for r in rows),
                        "items_with_same_sold": sum(1 for r in rows if r["same_sold"])}
    misses = [r for r in rows if r["score_v2"] == "miss"]
    misses.sort(key=lambda r: -abs(math.log((r["v2_price"] or 0.01) /
                                             (r["sold_price"] or r["owner_price"]))))
    summary["worst_misses"] = [{k: r[k] for k in ("item", "status", "brand", "type", "days",
                                                  "v2_price", "msrp_est", "owner_price",
                                                  "sold_price", "same_sold", "variant_sold",
                                                  "reasoning")} for r in misses[:8]]
    if rows:      # again, now with the score columns
        with out.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)
    (OUT_DIR / f"summary-{stamp}.json").write_text(json.dumps(summary, indent=1), encoding="utf-8")
    print(json.dumps(summary, indent=1))


if __name__ == "__main__":
    main()
