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

Hard caps: before each item, the run stops if Claude spend so far plus a
worst-case item would pass --claude-cap; Apify refuses any uncached call whose
worst-case cost would pass --apify-cap (estimated from the actor's per-result
rate, see config.APIFY_SOLD_COST_PER_RESULT).
"""
import argparse
import csv
import json
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
OUT_DIR = ROOT / "data" / "cache" / "backtest"
# Worst case for one item: a Sonnet judge call plus an Opus escalation.
CLAUDE_ITEM_WORST = 0.35


def load_items(prefixes, limit):
    con = sqlite3.connect(f"file:{DB.as_posix()}?mode=ro", uri=True)
    rows = con.execute("SELECT item_id, status, identification, listing, pricing, ebay, receipt, "
                       "photos FROM items WHERE status IN ('published','sold') "
                       "ORDER BY item_id").fetchall()
    con.close()
    items = []
    for item_id, status, ident, listing, pricing, ebay, receipt, photos in rows:
        if prefixes and not any(item_id.startswith(p) for p in prefixes):
            continue
        ident, listing = json.loads(ident or "{}"), json.loads(listing or "{}")
        if not ident or not listing.get("price"):
            continue
        e = json.loads(ebay or "{}")
        sold = None
        if status == "sold" and e.get("sale_price"):
            sold = round(e["sale_price"] / max(1, e.get("units_sold") or 1), 2)
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
    args = ap.parse_args()
    if not args.items and not args.limit:
        raise SystemExit("refusing to run every item: pass --items or --limit")

    prefixes = [p.strip() for p in (args.items or "").split(",") if p.strip()]
    items = load_items(prefixes, args.limit)
    v2.apify_cap_usd = args.apify_cap
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
        row = {
            "item": it["item_id"][:8], "status": it["status"], "category_id": it["category_id"],
            "brand": it["ident"].get("brand"), "type": it["ident"].get("item_type"),
            "candidates": r.get("candidate_count"),
            "same_sold": r.get("same_item_sold_count"), "same_active": r.get("same_item_active_count"),
            "own_dropped": (r.get("dropped") or {}).get("own"),
            "evidence": r.get("evidence"),
            "v1_price": it["v1_price"], "v2_price": r.get("suggested_price"),
            "claude_price": r.get("claude_price"), "claude_conf": r.get("claude_confidence"),
            "owner_price": owner, "sold_price": sold,
            "v1_ratio": ratio(it["v1_price"], owner), "v2_ratio": ratio(r.get("suggested_price"), owner),
            "claude_ratio": ratio(r.get("claude_price"), owner),
            "v2_vs_sold": ratio(r.get("suggested_price"), sold),
            "claude_vs_sold": ratio(r.get("claude_price"), sold),
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
              f"v1={row['v1_price']} v2={row['v2_price']} claude={row['claude_price']} "
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
    for key in ("v1_ratio", "v2_ratio", "claude_ratio", "v2_vs_sold", "claude_vs_sold"):
        vals = [r[key] for r in rows if r[key] is not None]
        summary[key] = {
            "n": len(vals), "median": round(statistics.median(vals), 3) if vals else None,
            "within_10pct": within(rows, key, 0.10)[0], "within_20pct": within(rows, key, 0.20)[0]}
    worst = sorted((r for r in rows if r["v2_ratio"] or r["claude_ratio"]),
                   key=lambda r: -abs(__import__("math").log(r["v2_ratio"] or r["claude_ratio"])))[:10]
    summary["worst_10"] = [{k: r[k] for k in ("item", "brand", "type", "v2_price", "claude_price",
                                              "owner_price", "sold_price", "v2_ratio",
                                              "claude_ratio", "same_sold")} for r in worst]
    (OUT_DIR / f"summary-{stamp}.json").write_text(json.dumps(summary, indent=1), encoding="utf-8")
    print(json.dumps(summary, indent=1))


if __name__ == "__main__":
    main()
