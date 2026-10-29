"""A fast price for stock that isn't listed yet (Quick add in /find).

The owner prices by MSRP and feel: list at some % of MSRP that depends on
the brand. This gives Claude that habit as numbers, i.e. what % of MSRP the
owner usually lists the brand at and what % it actually sold at, plus up to
two web searches to confirm the MSRP or see current prices, and asks for one
price. Full comps pricing still runs later, when the item is really listed.
"""
import json
import statistics

from config import QUICK_PRICE_MODEL
from db import list_items
from pipeline import msrp as msrp_mod

_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "price": {"type": "number"},
        "low": {"type": "number"},
        "high": {"type": "number"},
        "msrp": {"type": ["number", "null"]},
        "pct_of_msrp": {"type": ["number", "null"]},
        "reasoning": {"type": "string"},
    },
    "required": ["price", "low", "high", "msrp", "pct_of_msrp", "reasoning"],
}


def _ratio(rows):
    rs = sorted(r for r in rows if r)
    if not rs:
        return None
    return {"n": len(rs), "median": round(statistics.median(rs), 2),
            "low": round(rs[len(rs) // 4], 2), "high": round(rs[(3 * len(rs)) // 4], 2)}


def owner_habits(ident: dict, items: list[dict] | None = None) -> dict:
    """How this brand (else item type) has been listed and sold, as % of MSRP."""
    items = list_items() if items is None else items
    bk = msrp_mod.brand_key(ident.get("brand"))
    tb = msrp_mod.type_bucket(ident.get("item_type"))
    rows = []
    for it in items:
        i = it.get("identification") or {}
        m = msrp_mod.msrp_of(i)
        price = (it.get("listing") or {}).get("price")
        if not m or not price:
            continue
        e = it.get("ebay") or {}
        units = max(1, e.get("units_sold") or e.get("units") or 1)
        sold = (e.get("sale_price") / units) if e.get("sale_price") else None
        rows.append({"brand": msrp_mod.brand_key(i.get("brand")),
                     "bucket": msrp_mod.type_bucket(i.get("item_type")),
                     "title": ((it.get("listing") or {}).get("title") or "")[:70],
                     "msrp": m, "listed": float(price), "sold": sold,
                     "status": it.get("status")})
    for level, key, subset in (("brand", bk, [r for r in rows if bk and r["brand"] == bk]),
                               ("type", tb, [r for r in rows if r["bucket"] == tb])):
        if len(subset) >= 3:
            break
    else:
        level, key, subset = "all", "all", rows
    return {
        "level": level, "key": key,
        "listed_pct": _ratio([r["listed"] / r["msrp"] for r in subset]),
        "sold_pct": _ratio([r["sold"] / r["msrp"] for r in subset if r["sold"]]),
        "examples": sorted(subset, key=lambda r: r["sold"] is None)[:8],
    }


def quick_price(ident: dict, photo: str | None = None, receipt: dict | None = None,
                ask=None, item_id: str | None = None) -> dict:
    if ask is None:
        from claude_llm import ask
    habits = owner_habits(ident)
    cost = (receipt or {}).get("reduced_price")
    ex = "\n".join(
        f"- {r['title']} | MSRP ${r['msrp']:.0f} | listed ${r['listed']:.2f}"
        + (f" | sold ${r['sold']:.2f}" if r["sold"] else f" | {r['status']}, not sold")
        for r in habits["examples"]) or "- none yet"
    prompt = (
        "Price this item for eBay (Buy It Now, new with tags). The seller prices from MSRP: "
        "a percentage of MSRP that depends on the brand, checked against what actually sold.\n\n"
        f"Item: {json.dumps({k: ident.get(k) for k in ('brand', 'product_name', 'item_type', 'model', 'color', 'size', 'upc', 'retail_price')})}\n"
        f"Ross cost: {'$' + str(cost) if cost else 'unknown'}\n\n"
        f"Seller's history for {habits['level']} '{habits['key']}':\n"
        f"- listed at % of MSRP: {habits['listed_pct']}\n"
        f"- sold at % of MSRP: {habits['sold_pct']}\n"
        f"Examples:\n{ex}\n\n"
        "Use at most two web searches: confirm the MSRP if it's missing or doubtful, and/or "
        "check what this exact item sells for. Then give one price ending in .99, a low-high "
        "range, the MSRP you used and the % of MSRP. Lean on what SOLD over what was listed. "
        "Keep reasoning to two sentences.")
    out = ask("quick_price", prompt, model=QUICK_PRICE_MODEL, images=[photo] if photo else None,
              max_tokens=4000, effort="low", output_schema=_SCHEMA, item_id=item_id,
              tools=[{"type": "web_search_20260209", "name": "web_search", "max_uses": 2}])
    out["engine"] = "quick"
    out["suggested_price"] = out["price"]
    out["basis"] = {"level": habits["level"], "key": habits["key"],
                    "listed_pct": habits["listed_pct"], "sold_pct": habits["sold_pct"]}
    return out
