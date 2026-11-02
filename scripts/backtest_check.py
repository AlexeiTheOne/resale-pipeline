"""Backtest /check's verdicts on our real items (docs/check-command-design.md §5).

For every item with a Ross tag price that has SOLD, or has been listed
STALE_DAYS+ without selling, pricing_v2 is re-run on its stored
identification (and first photo, when present). The verdict /check would have
given is computed for both exact and any mode with pipeline/check.evaluate,
the same code /check uses. Then it is compared with what happened:

  win    sold, actual profit per unit >= CHECK_BUY_MIN_PROFIT ($15)
  ok     sold, profit 0..15
  loss   sold at a loss
  stale  unsold STALE_DAYS+ after listing
  (unsold and newer: not scored)

Actual profit per unit = net_proceeds / units_sold (eBay's net: after fees,
ad fees, label and refunds) - Ross price x (1 + SALES_TAX_RATE).

Hit rate per verdict:
  BUY        wins / decided              (did BUY pick winners?)
  SKIP       (ok + loss + stale) / decided  (did SKIP avoid missing a win?)
  MAYBE, BORDERLINE: the outcome mix is reported.

Caveats, printed in the report too:
- Look-ahead: today's sold comps include sales after ours. Our own listing is
  dropped (own listing ids), but prices may have moved since.
- These are items we DID buy: what a SKIP saves on items passed over can't
  be measured here.
- To keep it cheap: one sold query per item (--one-sold-query, default on) and
  no escalation, like /check's quick judge; results are cached, so a rerun
  costs nothing.

Read-only on items. Writes: llm_usage rows (stage check_backtest), the comps
cache, and the report in logs/ plus a CSV in data/cache/backtest/.

    python scripts/backtest_check.py --limit 60 --claude-cap 3 --apify-cap 3
"""
import argparse
import csv
import os
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

import config  # noqa: E402
import db  # noqa: E402
from pipeline import check  # noqa: E402
from pipeline import pricing_v2 as v2  # noqa: E402

STAGE = "check_backtest"
STALE_DAYS = 60
CLAUDE_ITEM_WORST = 0.05      # one Sonnet judge call, no escalation (max seen $0.0375)
OUT_DIR = ROOT / "data" / "cache" / "backtest"


def _ts(v):
    if not v:
        return None
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None


def outcome(item: dict, now: datetime | None = None) -> dict:
    """What happened to an item: kind (win/ok/loss/stale/open), profit, days."""
    now = now or datetime.now(timezone.utc)
    e = item.get("ebay") or {}
    ross = (item.get("receipt") or {}).get("reduced_price")
    listed = _ts(e.get("published_at")) or _ts(item.get("created_at"))
    if item.get("status") == "sold" and e.get("net_proceeds") is not None and ross:
        units = max(1, e.get("units_sold") or 1)
        profit = round(e["net_proceeds"] / units - ross * (1 + config.SALES_TAX_RATE), 2)
        sold = _ts(e.get("first_sold_at") or e.get("sold_at"))
        days = round((sold - listed).total_seconds() / 86400, 1) if sold and listed else None
        kind = ("win" if profit >= config.CHECK_BUY_MIN_PROFIT else "ok" if profit >= 0 else "loss")
        return {"kind": kind, "profit": profit, "days": days}
    days = round((now - listed).total_seconds() / 86400, 1) if listed else None
    if item.get("status") == "published" and days is not None and days >= STALE_DAYS:
        return {"kind": "stale", "profit": None, "days": days}
    return {"kind": "open", "profit": None, "days": days}


def load_items(prefixes=None, limit=None) -> list[dict]:
    out = []
    for it in db.list_items():
        if prefixes and not any(it["item_id"].startswith(p) for p in prefixes):
            continue
        if not (it.get("receipt") or {}).get("reduced_price") or not it.get("identification"):
            continue
        o = outcome(it)
        if o["kind"] == "open":
            continue
        out.append({**it, "_outcome": o})
    out.sort(key=lambda it: (it["_outcome"]["kind"] == "stale", it["item_id"]))
    return out[:limit] if limit else out


def hit_rates(rows: list[dict], mode: str) -> dict:
    """{verdict: {"n", outcome counts, "hit", "avg_profit", "median_days"}}."""
    by = defaultdict(list)
    for r in rows:
        if r.get(f"{mode}_verdict"):
            by[r[f"{mode}_verdict"]].append(r)
    out = {}
    for v in check.VERDICTS:
        rs = by.get(v, [])
        c = Counter(r["outcome"] for r in rs)
        n = len(rs)
        hit = None
        if n and v == "BUY":
            hit = c["win"] / n
        elif n and v == "SKIP":
            hit = (c["ok"] + c["loss"] + c["stale"]) / n
        profits = [r["actual_profit"] for r in rs if r["actual_profit"] is not None]
        days = [r["days"] for r in rs if r["outcome"] != "stale" and r["days"] is not None]
        out[v] = {"n": n, **{k: c[k] for k in ("win", "ok", "loss", "stale")},
                  "hit": round(hit, 2) if hit is not None else None,
                  "avg_profit": round(statistics.mean(profits), 2) if profits else None,
                  "median_days": statistics.median(days) if days else None}
    return out


def write_report(path: Path, rows: list[dict], stopped: str | None, spend: dict) -> None:
    w = []
    w.append(f"# /check verdict backtest ({datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC)\n")
    kinds = Counter(r["outcome"] for r in rows)
    w.append(f"Items scored: {len(rows)} ({', '.join(f'{k} {n}' for k, n in sorted(kinds.items()))}). "
             f"Thresholds: BUY ≥ ${config.CHECK_BUY_MIN_PROFIT:.0f} and ROI ≥ {config.CHECK_BUY_MIN_ROI:.0%} "
             f"with ≥ {config.CHECK_MIN_SALES} sales, ≥ ${config.CHECK_MIN_PROFIT_AT_CHEAPEST:.0f} at the "
             f"cheapest identical; BORDERLINE ≥ ${config.CHECK_MIN_PROFIT:.0f}.")
    w.append(f"Spend this run: Claude ${spend['claude']:.2f}, Apify ~${spend['apify']:.2f}."
             + (f" **Stopped early:** {stopped}" if stopped else "") + "\n")
    for mode in ("exact", "any"):
        hr = hit_rates(rows, mode)
        w.append(f"## {mode} matches\n")
        w.append("| Verdict | n | win | ok | loss | stale | hit rate | avg actual profit | median days to sell |")
        w.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|")
        for v, s in hr.items():
            hit = "-" if s["hit"] is None else f"{s['hit']:.0%}"
            ap = "-" if s["avg_profit"] is None else f"${s['avg_profit']}"
            md = "-" if s["median_days"] is None else f"{s['median_days']:.0f}"
            w.append(f"| {v} | {s['n']} | {s['win']} | {s['ok']} | {s['loss']} | {s['stale']} | {hit} | {ap} | {md} |")
        w.append("")
    w.append("Hit rate: BUY = wins / scored; SKIP = (ok + loss + stale) / scored. win = actual profit "
             f"≥ ${config.CHECK_BUY_MIN_PROFIT:.0f}; stale = unsold {STALE_DAYS}+ days.\n")
    w.append("Caveats: look-ahead (today's comps include later sales); only items we bought; one "
             "sold query per item and no escalation (cheaper than a real Full check).\n")
    w.append("## Items\n")
    w.append("| item | brand | outcome | actual profit | exact | any | exact price | any price | sales (exact/any) |")
    w.append("|---|---|---|---:|---|---|---:|---:|---|")
    for r in rows:
        w.append(f"| {r['item']} | {r['brand'] or ''} | {r['outcome']} | {r['actual_profit'] if r['actual_profit'] is not None else '-'} "
                 f"| {r['exact_verdict']} | {r['any_verdict']} | {r['exact_price'] or '-'} | {r['any_price'] or '-'} "
                 f"| {r['exact_sales']}/{r['any_sales']} |")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(w) + "\n", encoding="utf-8")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--items", help="comma-separated item id prefixes")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--claude-cap", type=float, default=3.0)
    ap.add_argument("--apify-cap", type=float, default=3.0)
    ap.add_argument("--all-sold-queries", action="store_true",
                    help="send every sold query to Apify (default: only search_query when uncached)")
    args = ap.parse_args(argv)
    if not args.items and not args.limit:
        raise SystemExit("refusing to run every item: pass --items or --limit")
    prefixes = [p.strip() for p in (args.items or "").split(",") if p.strip()]
    items = load_items(prefixes, args.limit)
    v2.apify_cap_usd = args.apify_cap
    if not args.all_sold_queries:
        v2.uncached_sold_queries = {"search_query"}
    start = datetime.now(timezone.utc).isoformat()
    rows, stopped = [], None
    for it in items:
        spent, _ = db.llm_stage_spend(start, STAGE)
        if spent + CLAUDE_ITEM_WORST > args.claude_cap:
            stopped = f"Claude cap: ${spent:.3f} spent"
            break
        photos = [p for p in it.get("photos") or [] if p][:1]
        try:
            pricing = v2.get_pricing_v2(it["identification"], photo_paths=photos, receipt=it.get("receipt"),
                                        item_id=it["item_id"], stage=STAGE, escalate=False)
        except v2.ApifyBudgetExceeded as e:
            stopped = f"Apify cap: {e}"
            break
        except Exception as e:          # one bad item must not end the run
            print(f"  {it['item_id'][:8]}: {type(e).__name__}: {str(e)[:120]}")
            continue
        ross = it["receipt"]["reduced_price"]
        ex = check.evaluate(pricing, it["identification"], ross, True)
        an = check.evaluate(pricing, it["identification"], ross, False)
        o = it["_outcome"]
        row = {"item": it["item_id"][:8], "brand": it["identification"].get("brand"),
               "outcome": o["kind"], "actual_profit": o["profit"], "days": o["days"],
               "ross": ross, "item_type": ex["item_type"],
               "exact_verdict": ex["verdict"], "exact_price": ex["pick"]["price"], "exact_sales": ex["pick"]["sales"],
               "exact_profit": (ex["money"] or {}).get("profit"),
               "any_verdict": an["verdict"], "any_price": an["pick"]["price"], "any_sales": an["pick"]["sales"],
               "any_profit": (an["money"] or {}).get("profit"),
               "cheapest_total": ex["cheapest_total"]}
        rows.append(row)
        print(f"  {row['item']} {row['outcome']:5} actual {row['actual_profit']!s:>7}  exact {row['exact_verdict']:10} "
              f"any {row['any_verdict']}")
    spend = {"claude": db.llm_stage_spend(start, STAGE)[0], "apify": v2.apify_spend["usd"]}
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    if rows:
        with open(OUT_DIR / f"check-{stamp}.csv", "w", newline="", encoding="utf-8") as f:
            wr = csv.DictWriter(f, fieldnames=list(rows[0]))
            wr.writeheader()
            wr.writerows(rows)
    report = Path(config.LOG_DIR) / f"backtest-check-{stamp}.md"
    write_report(report, rows, stopped, spend)
    print(f"{len(rows)} items scored. Claude ${spend['claude']:.2f}, Apify ~${spend['apify']:.2f}."
          + (f" Stopped: {stopped}" if stopped else ""))
    print(f"Report: {report}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
