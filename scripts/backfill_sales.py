"""One-off: recompute the recorded sale money on already-sold items from eBay.

Why: before 2026-10-29, /sync never attributed Promoted Listings ad fees to
orders, missed refunds that posted after a sale was recorded, ignored
deliveryDiscount, and kept only the newest sold_at per item. ebay/orders.py now
gets all of that right; this rewrites the stored figures that were recorded the
old way, and adds the per-order `sales` records.

Scope, on purpose:
  - Only orders ALREADY recorded on a row (its order_ids) are recomputed. An
    order the row doesn't know yet is left for /sync, so its "Sold" message
    still goes out.
  - Only money/provenance keys are written (ebay.orders.SALE_DB_FIELDS); status
    is never touched.
  - `published_at` is filled where it is missing, from Trading GetItem's
    ListingDetails.StartTime (when the current listing went live).

eBay access is read-only: Fulfillment getOrders, Finances getTransactions and
Trading GetItem. Nothing on eBay is changed.

Usage (from the repo root):
    python scripts/backfill_sales.py            # dry run: print before/after
    python scripts/backfill_sales.py --apply    # back up the DB, then write

The DB is reached only through db.py, so this works on either backend
(DB_BACKEND). Writes stamp updated_at in UTC, like every other db.py write.
"""
import argparse
import sys
from pathlib import Path
from xml.sax.saxutils import escape

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import db  # noqa: E402

DAYS = 700   # every order this account has (first sale May 2026); eBay caps at 2 years


def _known_orders(ebay: dict) -> list[str]:
    ids = list(ebay.get("order_ids") or [])
    if not ids and ebay.get("order_id"):
        ids = [ebay["order_id"]]
    return ids


def _start_time(listing_id: str) -> str | None:
    """When the listing went live, per Trading GetItem. None if unreadable."""
    from ebay.listings import _strip_ns, _trading_call
    try:
        root, _ = _trading_call(
            "GetItem", f"<ItemID>{escape(str(listing_id))}</ItemID>"
                       "<OutputSelector>Item.ListingDetails.StartTime</OutputSelector>",
            timeout=30)
    except Exception as e:   # ended too long ago, deleted, network
        print(f"    GetItem {listing_id}: {type(e).__name__}: {str(e)[:100]}")
        return None
    for node in root.iter():
        if _strip_ns(node.tag) == "StartTime" and (node.text or "").strip():
            return node.text.strip()
    return None


def plan(sold_map: dict, *, fetch_published: bool = True) -> list[dict]:
    """[{item_id, title, before, after, published_at}] for every row that changes."""
    from ebay.orders import SALE_DB_FIELDS, sale_db_fields, totals_from_sales

    changes = []
    for it in sorted(db.list_items(), key=lambda it: it["created_at"]):
        item_id, status = it["item_id"], it["status"]
        ebay = it.get("ebay") or {}
        title = ((it.get("listing") or {}).get("title") or "")[:40]
        row = {"item_id": item_id, "title": title, "status": status,
               "before": {k: ebay.get(k) for k in SALE_DB_FIELDS if k in ebay},
               "after": None, "published_at": None, "note": None}

        known = _known_orders(ebay)
        if ebay.get("sale_price") is not None and known:
            sale = sold_map.get(item_id)
            if sale is None and ebay.get("listing_id"):
                sale = sold_map.get(str(ebay["listing_id"]))
            records = [r for r in (sale or {}).get("sales") or [] if r["order_id"] in known]
            found = {r["order_id"] for r in records}
            if set(known) - found:
                row["note"] = f"orders not found on eBay: {sorted(set(known) - found)}"
            elif records:
                after = sale_db_fields(totals_from_sales(records))
                if any(row["before"].get(k) != after.get(k) for k in after):
                    row["after"] = after
        elif ebay.get("sale_price") is not None:
            row["note"] = "sale recorded by hand (no order id): left alone"

        if fetch_published and ebay.get("listing_id") and not ebay.get("published_at"):
            row["published_at"] = _start_time(ebay["listing_id"])

        if row["after"] or row["published_at"] or row["note"]:
            changes.append(row)
    return changes


def _money(v) -> str:
    return "—" if v is None else f"{v:.2f}"


def print_plan(rows: list[dict]) -> None:
    print(f"{'item':8}  {'title':40}  {'net before':>10} {'net after':>10} {'Δ':>8}  "
          f"{'ads':>6} {'refund':>7}")
    d_net = d_ads = d_refund = 0.0
    n_money = 0
    for r in rows:
        if r["after"]:
            n_money += 1
            b, a = r["before"].get("net_proceeds") or 0.0, r["after"]["net_proceeds"]
            d_net += a - b
            d_ads += r["after"].get("ad_fees") or 0
            d_refund += (r["after"].get("refunded") or 0)
            flag = "  ⚠ refund" if (r["after"].get("refunded") or 0) else ""
            print(f"{r['item_id'][:8]}  {r['title']:40}  {_money(b):>10} {_money(a):>10} "
                  f"{a - b:>+8.2f}  {_money(r['after'].get('ad_fees')):>6} "
                  f"{_money(r['after'].get('refunded')):>7}{flag}")
    print()
    for r in rows:
        if r["note"]:
            print(f"  note {r['item_id'][:8]} {r['title']}: {r['note']}")
    pub = [r for r in rows if r["published_at"]]
    for r in pub:
        print(f"  published_at {r['item_id'][:8]} ← {r['published_at']}  ({r['title']})")

    total_before = total_after = 0.0
    n_sold = 0
    after_by_id = {r["item_id"]: r["after"] for r in rows if r["after"]}
    for it in db.list_items():
        item_id, ebay = it["item_id"], it.get("ebay") or {}
        if ebay.get("sale_price") is None:
            continue
        n_sold += 1
        net = ebay.get("net_proceeds") or 0.0
        total_before += net
        total_after += (after_by_id[item_id]["net_proceeds"] if item_id in after_by_id else net)
    print()
    print(f"Rows with money changes: {n_money}; Δ net {d_net:+.2f} "
          f"(ad fees now attributed {d_ads:.2f}, gross refunds {d_refund:.2f})")
    print(f"published_at to fill: {len(pub)}")
    print(f"Recorded net across all {n_sold} sold rows: {total_before:.2f} → {total_after:.2f} "
          f"({total_after - total_before:+.2f})")


def backup() -> str:
    """A labelled backup before writing (db.snapshot: SQLite's WAL-safe backup
    API, or BACKUP DATABASE on SQL Server)."""
    return db.snapshot("pre-backfill-sales")


def apply(rows: list[dict]) -> int:
    """Write each row in its own short transaction, re-reading the row first so a
    concurrent write by the live bot (a price or quantity sync) is kept: only the
    sale keys and a missing published_at are replaced (db.modify_field)."""
    from ebay.orders import SALE_DB_FIELDS
    written = 0
    for r in rows:
        if not (r["after"] or r["published_at"]):
            continue
        skipped = []

        def change(ebay, r=r, skipped=skipped):
            ebay = dict(ebay or {})
            if r["after"]:
                if {k: ebay.get(k) for k in SALE_DB_FIELDS if k in ebay} != r["before"]:
                    skipped.append(True)
                    return None
                ebay.update(r["after"])
            if r["published_at"] and not ebay.get("published_at"):
                ebay["published_at"] = r["published_at"]
            return ebay

        if db.modify_field(r["item_id"], "ebay", change):
            written += 1
        elif skipped:
            print(f"  skipped {r['item_id'][:8]}: its sale changed since the plan was made")
    return written


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--apply", action="store_true", help="back up the DB, then write")
    ap.add_argument("--no-published", action="store_true",
                    help="skip the published_at fill (no GetItem calls)")
    args = ap.parse_args()

    from ebay.orders import get_orders, get_transactions, sales_by_item

    print(f"Reading eBay orders + transactions ({DAYS}d, read-only)...")
    o, t = get_orders(DAYS), get_transactions(DAYS)
    print(f"  {len(o)} orders, {len(t)} transactions")
    sold_map = sales_by_item(DAYS, orders=o, transactions=t)
    ledger_net = sum({r["order_id"]: r["net"] for v in sold_map.values()
                      for r in v["sales"]}.values())
    print(f"  eBay cash ledger: net proceeds {ledger_net:.2f} across all orders\n")

    rows = plan(sold_map, fetch_published=not args.no_published)
    print_plan(rows)

    if not args.apply:
        print("\nDry run — nothing written. Re-run with --apply to write.")
        return
    dest = backup()
    print(f"\nBacked up to {dest}")
    print(f"Wrote {apply(rows)} row(s).")


if __name__ == "__main__":
    main()
