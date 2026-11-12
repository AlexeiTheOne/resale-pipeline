"""The free-shipping test (docs/listing-performance-plan.md §5; owner go,
2026-11-12).

18 women's bags move to free shipping with the price raised by
SHIP_TEST_PRICE_BUMP ($9, the measured bag label rounded up). 18 matched bags
stay on $9.99 shipping. For SHIP_TEST_DAYS (21) neither group gets repriced or
offered, so nothing else moves the comparison. It's judged on click-through
(views / impressions), with sales as a direction only.

State lives on each item, in ebay.ship_test:
{"group": "test"|"control", "start", "end", "price_before",
 "policy_before", "policy"}. A test item also gets ebay.free_shipping, which
profit.shipping reads, so its break-even and Best Offer floor use $0 shipping
charged and the $8.17 label.

Groups were drawn from eBay's 11/06 quality report: women's bags with no sale
in 31 days, at least 21 days live, ordered by impressions with neighbours
alternating. Redrawn 2026-11-12:
- 0e48e50b sold on 8 Nov and was replaced by f0665406.
- d60d9e01's price was cut that day, so it was swapped for 53b36c34.
Both newcomers are the next women's bags by impressions.
"""
from __future__ import annotations

import copy
from datetime import date, timedelta

import config
import db
import profit
from ebay import analytics, inventory
from pipeline import sync

TEST = ("a8041fe2", "fc08e55b", "3347c963", "4a66bcb4", "e6dc7974", "f027cb9c",
        "57a73dda", "5fdc486f", "b88e085d", "71226811", "6d35cfd0", "61e7c55b",
        "fe5be314", "182a5ab3", "e5756e70", "11b3217a", "2aa10063", "f93268fd")
CONTROL = ("991936e3", "c57cb4cc", "a84043d6", "2f57c0f4", "5af2cebd", "6ec3c44a",
           "db2380ba", "f296137d", "4e8cefb4", "a129b1b5", "0895b282", "434f5ea0",
           "a06a637c", "179def75", "2da4f006", "f637882c", "f0665406", "53b36c34")


def new_price(price: float) -> float:
    """The test price: the bump keeps the .99 ending ($44.99 -> $53.99)."""
    return round(float(price) + config.SHIP_TEST_PRICE_BUMP, 2)


def on_free_shipping(item: dict) -> dict:
    """A copy of `item` as it will be on free shipping, for its floors."""
    out = copy.deepcopy(item)
    out.setdefault("ebay", {})["free_shipping"] = True
    return out


def record(item: dict) -> dict | None:
    return (item.get("ebay") or {}).get("ship_test")


def frozen(item: dict, today: date | None = None) -> bool:
    """True while the item is in a running test: no reprices, no offers."""
    rec = record(item)
    if not rec or not rec.get("end"):
        return False
    return (today or date.today()).isoformat() <= rec["end"]


def _items_by_prefix() -> dict[str, dict]:
    return {i["item_id"][:8]: i for i in db.list_items()}


def _problem(item: dict | None) -> str | None:
    """Why this item can't join the test now, or None."""
    if item is None:
        return "not in the DB"
    ebay = item.get("ebay") or {}
    if item["status"] != "published":
        return f"status is {item['status']}"
    if not ebay.get("offer_id"):
        return "no eBay offer"
    if sync.detached(item):
        return "relisted in Seller Hub (the offer can't reach it)"
    if record(item):
        return "already in the test"
    if ebay.get("free_shipping"):
        return "already on free shipping"
    if (item.get("listing") or {}).get("price") is None:
        return "no price"
    return None


def _row(item: dict, group: str) -> dict:
    price = round(float(item["listing"]["price"]), 2)
    row = {"item_id": item["item_id"], "title": (item.get("listing") or {}).get("title") or "",
           "group": group, "price": price}
    if group == "test":
        free = on_free_shipping(item)
        row["new_price"] = new_price(price)
        row["terms"] = inventory.best_offer_terms(free, row["new_price"])
        row["breakeven"] = profit.breakeven_price(free)
    return row


def preview() -> dict:
    """What applying would do now: {"test": rows, "control": rows,
    "problems": [(prefix, group, reason)], "started": bool}. Read-only."""
    items = _items_by_prefix()
    out = {"test": [], "control": [], "problems": [], "started": False}
    for group, ids in (("test", TEST), ("control", CONTROL)):
        for pid in ids:
            item = items.get(pid)
            if item is not None and record(item):
                out["started"] = True
            why = _problem(item)
            if why:
                out["problems"].append((pid, group, why))
            else:
                out[group].append(_row(item, group))
    return out


def _template_policy(rows: list[dict]) -> str:
    """The fulfillment policy the test listings use now (the copy's template)."""
    for r in rows:
        offer_id = (db.get_item(r["item_id"]) or {}).get("ebay", {}).get("offer_id")
        if offer_id:
            pid = (inventory.get_offer(offer_id).get("listingPolicies") or {}).get("fulfillmentPolicyId")
            if pid:
                return str(pid)
    raise RuntimeError("couldn't read the current shipping policy from any test listing")


def apply(rows: dict, today: date | None = None) -> dict:
    """Start the test from a preview()'s rows. Creates (or finds) the
    free-shipping policy, then for each test listing sends the new price, the
    policy and the Best Offer thresholds in one eBay update. The DB changes only
    after eBay accepts. A listing whose price moved since the preview is skipped.
    Control listings are only marked.

    Returns {"policy", "applied", "marked", "failed": [(prefix, why)], "start", "end"}."""
    today = today or date.today()
    start, end = today.isoformat(), (today + timedelta(days=config.SHIP_TEST_DAYS)).isoformat()
    out = {"policy": None, "applied": [], "marked": [], "failed": [], "start": start, "end": end}
    test_rows = rows.get("test") or []
    if test_rows:
        before = _template_policy(test_rows)
        out["policy"] = inventory.ensure_free_shipping_policy(before, config.FREE_SHIPPING_POLICY_NAME)
    for r in test_rows:
        item = db.get_item(r["item_id"])
        why = _problem(item)
        if why is None and round(float(item["listing"]["price"]), 2) != r["price"]:
            why = f"price changed since the preview (now ${item['listing']['price']})"
        if why:
            out["failed"].append((r["item_id"][:8], why))
            continue
        ebay = item["ebay"]
        try:
            live = inventory.get_offer(ebay["offer_id"])
            policy_before = (live.get("listingPolicies") or {}).get("fulfillmentPolicyId")
            inventory.update_offer_price(ebay["offer_id"], r["new_price"], on_free_shipping(item),
                                         fulfillment_policy_id=out["policy"])
        except Exception as e:
            out["failed"].append((r["item_id"][:8], f"eBay: {type(e).__name__}: {str(e)[:150]}"))
            continue
        listing = dict(item["listing"])
        listing["price"] = r["new_price"]
        db.update_field(item["item_id"], "listing", listing)
        db.update_field(item["item_id"], "ebay", {
            **ebay, "free_shipping": True,
            "ship_test": {"group": "test", "start": start, "end": end, "price_before": r["price"],
                          "policy_before": policy_before, "policy": out["policy"]}})
        out["applied"].append(r)
    for r in rows.get("control") or []:
        item = db.get_item(r["item_id"])
        why = _problem(item)
        if why:
            out["failed"].append((r["item_id"][:8], why))
            continue
        db.update_field(item["item_id"], "ebay", {
            **item["ebay"], "ship_test": {"group": "control", "start": start, "end": end,
                                          "price_before": r["price"]}})
        out["marked"].append(r)
    return out


# --- measuring -------------------------------------------------------------------

def _sales_between(item: dict, start: str, end: str) -> int:
    units = 0
    for s in (item.get("ebay") or {}).get("sales") or []:
        day = str(s.get("sold_at") or "")[:10]
        if start <= day <= end:
            units += int(s.get("units") or 1)
    return units


def _traffic(listing_ids: list[str], start: date, end: date) -> dict:
    out = {}
    for i in range(0, len(listing_ids), 20):
        out.update(analytics.get_traffic_report(listing_ids[i:i + 20], start=start, end=end))
    return out


def report(today: date | None = None) -> dict | None:
    """Impressions, views, CTR and sales per group: during the test so far, and
    over the same number of days just before it (the baseline). None before the
    test starts."""
    today = today or date.today()
    members = [i for i in db.list_items() if record(i)]
    if not members:
        return None
    rec = record(members[0])
    start = date.fromisoformat(rec["start"])
    end = min(today, date.fromisoformat(rec["end"]))
    days = max((end - start).days + 1, 1)
    base_end = start - timedelta(days=1)
    base_start = base_end - timedelta(days=days - 1)
    ids = [str(i["ebay"]["listing_id"]) for i in members if (i.get("ebay") or {}).get("listing_id")]
    during, before = _traffic(ids, start, end), _traffic(ids, base_start, base_end)
    groups = {}
    for g in ("test", "control"):
        rows = [i for i in members if record(i)["group"] == g]
        lids = [str(i["ebay"].get("listing_id")) for i in rows]

        def tot(traffic, key):
            return sum(int((traffic.get(lid) or {}).get(key) or 0) for lid in lids)

        cur = {"impressions": tot(during, "impressions"), "views": tot(during, "views")}
        base = {"impressions": tot(before, "impressions"), "views": tot(before, "views")}
        for d in (cur, base):
            d["ctr"] = d["views"] / d["impressions"] if d["impressions"] else None
        cur["sales"] = sum(_sales_between(i, start.isoformat(), end.isoformat()) for i in rows)
        base["sales"] = sum(_sales_between(i, base_start.isoformat(), base_end.isoformat())
                            for i in rows)
        groups[g] = {"listings": len(rows), "during": cur, "before": base}
    return {"start": start.isoformat(), "end": rec["end"], "days": days, "groups": groups,
            "over": today.isoformat() > rec["end"]}
