"""Parse the old eBay store's workbook (data/import/Sales_statistics_7302022.xlsx)
into one row per sold listing per period, for the old_store_sales table.

    venv\\Scripts\\python.exe scripts\\import_old_store.py parse      # rows + checks, no AI, no DB
    venv\\Scripts\\python.exe scripts\\import_old_store.py classify   # + AI brand/type/size, writes the CSV

The workbook is 40-odd period tabs of eBay's "sales by listing" report plus
the old store's own cost columns. Layouts differ between tabs and some rows
are shifted, so each row's money columns are found by the sums, not by
header position:
    total = item sales + taxes + shipping      net = item sales + shipping - costs
The label cost is located from the header (or a per-tab template for the
headerless tabs); the purchase price ("Price 1") is the number after net
whose next cell is Price 1 x qty x 1.17 (or 1.07 in early 2021).

Skipped: Calc, Sheet2 (loan) and Sheet4 (a copy of two 2021 tabs); totals
and expense rows; $0 sales (refunds, cancellations); rows of "Sales
statistics" that repeat a dedicated 2021 tab; and the first-name columns
("Type1" and its headerless twin), which are never read.

Nothing here touches a database: the CSV it writes is loaded by the bot
(pipeline/old_store.py), so production gets a backup first.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import openpyxl  # noqa: E402

import config  # noqa: E402

WORKBOOK = config.DATA_DIR / "import" / "Sales_statistics_7302022.xlsx"
OUT_CSV = config.DATA_DIR / "import" / "old_store_sales.csv"
AI_CACHE = config.DATA_DIR / "import" / "old_store_ai.json"

SKIP_TABS = {"Calc", "Sheet2", "Sheet4"}
SUMMARY_TAB = "Sales statistics"
TOL = 0.03

# Period of each tab: (start, end, year_inferred). Tab names often leave out
# the year; it comes from the tab order (newest first). "March 15 - May 15"
# follows "March 14 - April 14", so it is read as April 15 - May 15 2024.
PERIODS: dict[str, tuple[date, date, bool]] = {
    "May 1st - Aug 31 st 2026": (date(2026, 5, 1), date(2026, 8, 31), False),
    "Feb 1st - Apr 30st": (date(2026, 2, 1), date(2026, 4, 30), True),
    "Jan 1st - Jan 31st": (date(2026, 1, 1), date(2026, 1, 31), True),
    "Dec 21st - Dec 31st": (date(2025, 12, 21), date(2025, 12, 31), True),
    "Sep 21, 2025 - Dec 20th": (date(2025, 9, 21), date(2025, 12, 20), False),
    "May 26, 2025 to Sep 20, 2025": (date(2025, 5, 26), date(2025, 9, 20), False),
    "April 2nd - May 25 2025": (date(2025, 4, 2), date(2025, 5, 25), False),
    "March 7 - April 1st 2025": (date(2025, 3, 7), date(2025, 4, 1), False),
    "February 1 - March 6 2025": (date(2025, 2, 1), date(2025, 3, 6), False),
    "December 5 - January 31": (date(2024, 12, 5), date(2025, 1, 31), True),
    "October 16-  December 4": (date(2024, 10, 16), date(2024, 12, 4), True),
    "August 17- October 15": (date(2024, 8, 17), date(2024, 10, 15), True),
    "June 16-August 16": (date(2024, 6, 16), date(2024, 8, 16), True),
    "May 16 - June 15": (date(2024, 5, 16), date(2024, 6, 15), True),
    "March 15 - May 15": (date(2024, 4, 15), date(2024, 5, 15), True),
    "March 14 - April 14": (date(2024, 3, 14), date(2024, 4, 14), True),
    "February 13-March 13": (date(2024, 2, 13), date(2024, 3, 13), True),
    "January 10 - February 12": (date(2024, 1, 10), date(2024, 2, 12), True),
    "December 16 - January 9": (date(2023, 12, 16), date(2024, 1, 9), True),
    "November 10 - December 15": (date(2023, 11, 10), date(2023, 12, 15), True),
    "October 7 - November 10": (date(2023, 10, 7), date(2023, 11, 10), True),
    "20 August - October 6": (date(2023, 8, 20), date(2023, 10, 6), True),
    "19 July 2023 - 19 August": (date(2023, 7, 19), date(2023, 8, 19), False),
    "June 16 - 18 July 2023": (date(2023, 6, 16), date(2023, 7, 18), False),
    "May 15 - June 15 2023": (date(2023, 5, 15), date(2023, 6, 15), False),
    "April 16 - May 14 2023": (date(2023, 4, 16), date(2023, 5, 14), False),
    "March 13 - April 15 2023": (date(2023, 3, 13), date(2023, 4, 15), False),
    "Jan 25 - March 12 2023": (date(2023, 1, 25), date(2023, 3, 12), False),
    "Dec 9 - Jan 24 2023": (date(2022, 12, 9), date(2023, 1, 24), False),
    "Oct 17 - Dec 9 2022": (date(2022, 10, 17), date(2022, 12, 9), False),
    "July 29-Oct 16, 2022": (date(2022, 7, 29), date(2022, 10, 16), False),
    "May 31-July 29, 2022": (date(2022, 5, 31), date(2022, 7, 29), False),
    "Mar 16 - May 30, 2022": (date(2022, 3, 16), date(2022, 5, 30), False),
    "Jan 17 Mar-15, 2022": (date(2022, 1, 17), date(2022, 3, 15), False),
    "Nov 26, 2021 to Jan 16, 2022": (date(2021, 11, 26), date(2022, 1, 16), False),
    "Oct 11, 2021 to Nov 25, 2021": (date(2021, 10, 11), date(2021, 11, 25), False),
    "28 aug - 10 oct": (date(2021, 8, 28), date(2021, 10, 10), True),
    "23 july - 27 aug": (date(2021, 7, 23), date(2021, 8, 27), True),
    "19 june - 22 july": (date(2021, 6, 19), date(2021, 7, 22), True),
    "30 may - 18 june": (date(2021, 5, 30), date(2021, 6, 18), True),
    "23-may-29 may": (date(2021, 5, 23), date(2021, 5, 29), True),
    "24-Apr-23 May": (date(2021, 4, 24), date(2021, 5, 23), True),
    "4-Apr_x0009_24-Apr": (date(2021, 4, 4), date(2021, 4, 24), True),
    # Early 2021: the exact period of the rows found nowhere else is unknown.
    SUMMARY_TAB: (date(2021, 1, 1), date(2021, 4, 3), True),
}

# Headerless tabs: where the label cost sits, counted back from net
# (None = the tab has no label column).
LABEL_FROM_NET = {"4-Apr_x0009_24-Apr": None, "23-may-29 may": -2, "30 may - 18 june": -1}

COLUMNS = [
    "source", "source_tab", "source_row", "period_start", "period_end", "year_inferred",
    "title", "ebay_item_id", "quantity", "item_sales", "unit_price", "shipping_per_unit",
    "label_per_unit", "fees_per_unit", "net_per_unit", "price_paid", "cost_per_unit",
    "best_offer_units", "seller_offer_units", "status", "note",
    "brand", "parent_brand", "item_type", "bag_size", "msrp", "msrp_from",
]


def _num(v) -> float | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    return None


def _close(a: float, b: float, tol: float = TOL) -> bool:
    return abs(a - b) <= tol


def _header(rows) -> tuple[int | None, list]:
    for j, r in enumerate(rows[:3]):
        if r and isinstance(r[0], str) and r[0].strip().lower() == "listing title":
            return j, [str(c).strip().lower() if c is not None else "" for c in r]
    return None, []


def _find(header: list, *words) -> int | None:
    for i, h in enumerate(header):
        if all(w in h for w in words):
            return i
    return None


def solve_row(r: list) -> dict | None:
    """Locate qty, total, item sales, shipping, costs and net in one row by
    the sums. None when no placement adds up."""
    cells = list(r)
    ebay_id = None
    i = 1
    if i < len(cells) and isinstance(cells[i], (int, float)) and cells[i] >= 1e10:
        ebay_id = str(int(cells[i]))
        i += 1
    qty = _num(cells[i]) if i < len(cells) else None
    if qty is None or qty < 1 or qty != int(qty):
        return None
    q_idx = i
    # A duplicated quantity column (November 10 - December 15).
    starts = [q_idx + 1]
    if q_idx + 1 < len(cells) and _num(cells[q_idx + 1]) == qty:
        starts.append(q_idx + 2)
    for t_idx in starts:
        total, item = (_num(cells[t_idx]) if t_idx < len(cells) else None,
                       _num(cells[t_idx + 1]) if t_idx + 1 < len(cells) else None)
        if total is None or item is None:
            continue
        it_idx = t_idx + 1
        for s_idx in range(it_idx + 1, min(it_idx + 7, len(cells))):
            ship = _num(cells[s_idx])
            if ship is None:
                continue
            taxes = sum(_num(c) or 0 for c in cells[it_idx + 1:s_idx])
            if not _close(total, item + taxes + ship):
                continue
            for c_idx in range(s_idx + 1, min(s_idx + 3, len(cells))):
                costs = _num(cells[c_idx])
                if costs is None:
                    continue
                for n_idx in range(c_idx + 1, min(c_idx + 12, len(cells))):
                    net = _num(cells[n_idx])
                    if net is not None and _close(net, item + ship - costs):
                        return {"ebay_item_id": ebay_id, "q_idx": q_idx, "quantity": int(qty),
                                "total": total, "item_sales": item, "it_idx": it_idx,
                                "shipping": ship, "s_idx": s_idx, "costs": costs, "c_idx": c_idx,
                                "net": net, "n_idx": n_idx}
    return None


def _price_paid(cells: list, start: int, qty: int, header_idx: int | None):
    """(price paid per unit, our cost per unit, matched?) from the old store's
    Price 1 / Cost pair after net."""
    for k in range(start, min(start + 8, len(cells) - 1)):
        p, c = _num(cells[k]), _num(cells[k + 1])
        if p is None or c is None or p <= 0:
            continue
        for mult in (1.17, 1.07):
            if _close(c, p * qty * mult, 0.06):
                return p, round(p * 1.07, 2), True
    if header_idx is not None and header_idx < len(cells):
        p = _num(cells[header_idx])
        if p and p > 0:
            return p, round(p * 1.07, 2), False
    return None, None, False


def _units(cells: list, header_idx: int | None, shift: int, qty: int) -> float | None:
    """Units sold via an offer: a whole number no higher than the quantity,
    else None (in some tabs the column holds the purchase price instead)."""
    if header_idx is None or header_idx + shift >= len(cells):
        return None
    v = _num(cells[header_idx + shift])
    return v if v is not None and v == int(v) and 0 <= v <= qty else None


def parse(path: Path = WORKBOOK) -> tuple[list[dict], list[dict]]:
    """(rows, rejected). rejected = titled rows that aren't a usable sale,
    with the reason, for the report."""
    wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
    rows: list[dict] = []
    rejected: list[dict] = []
    for ws in wb.worksheets:
        tab = ws.title
        if tab in SKIP_TABS:
            continue
        if tab not in PERIODS:
            raise ValueError(f"tab {tab!r} has no period: add it to PERIODS")
        start, end, inferred = PERIODS[tab]
        raw = [list(r) for r in ws.iter_rows(values_only=True, max_row=2000)]
        h_row, header = _header(raw)
        lab_h = _find(header, "shipping labels")
        net_h = _find(header, "net sales")
        p1_h = _find(header, "price 1")
        bo_h = _find(header, "via best offer")
        so_h = _find(header, "seller initiated")
        for n, r in enumerate(raw, start=1):
            if h_row is not None and n - 1 <= h_row:
                continue
            title = r[0] if r else None
            if not isinstance(title, str) or not title.strip():
                continue          # totals rows have no title
            title = " ".join(title.split())
            s = solve_row(r)
            if s is None:
                rejected.append({"tab": tab, "row": n, "title": title[:60], "why": "sums don't add up"})
                continue
            if s["item_sales"] <= 0:
                rejected.append({"tab": tab, "row": n, "title": title[:60], "why": "$0 sale"})
                continue
            # Columns after shipping line up with the header (shifted rows
            # differ only before it), so the header's own offsets are used,
            # moved by however far net is from the header's net.
            shift = (s["n_idx"] - net_h) if net_h is not None else 0
            if tab in LABEL_FROM_NET:
                off = LABEL_FROM_NET[tab]
                lab_idx = None if off is None else s["n_idx"] + off
            elif lab_h is not None:
                lab_idx = lab_h + shift
            else:
                lab_idx = s["n_idx"] - 1
            label = _num(r[lab_idx]) if lab_idx is not None and lab_idx < len(r) else None
            note = []
            if label is not None and label > s["costs"] + TOL:
                note.append("label above costs, dropped")
                label = None
            q = s["quantity"]
            p1_idx = p1_h + shift if p1_h is not None else None
            paid, cost, matched = _price_paid(r, s["n_idx"] + 1, q, p1_idx)
            status = "ok"
            if paid is not None and not matched:
                status, note = "to_check", note + ["Cost doesn't match Price 1"]
            bo = _units(r, bo_h, shift, q)
            so = _units(r, so_h, shift, q)
            fees = s["costs"] - (label or 0)
            rows.append({
                "source": "old_store", "source_tab": tab, "source_row": n,
                "period_start": start.isoformat(), "period_end": end.isoformat(),
                "year_inferred": int(inferred), "title": title, "ebay_item_id": s["ebay_item_id"],
                "quantity": q, "item_sales": round(s["item_sales"], 2),
                "unit_price": round(s["item_sales"] / q, 2),
                "shipping_per_unit": round(s["shipping"] / q, 2),
                "label_per_unit": round(label / q, 2) if label is not None else None,
                "fees_per_unit": round(fees / q, 2), "net_per_unit": round(s["net"] / q, 2),
                "price_paid": paid, "cost_per_unit": cost,
                "best_offer_units": int(bo) if bo is not None else None,
                "seller_offer_units": int(so) if so is not None else None,
                "status": status, "note": "; ".join(note) or None,
            })
    return _dedupe(rows, rejected), rejected


def _key(r: dict) -> tuple:
    return (re.sub(r"[^a-z0-9]", "", r["title"].lower()), r["quantity"], r["item_sales"])


def _dedupe(rows: list[dict], rejected: list[dict]) -> list[dict]:
    """Drop rows of the Sales statistics tab that repeat a dedicated 2021 tab
    (same title, quantity and item sales). Repeats between period tabs stay:
    the same item often sold again at the same price."""
    seen: dict[tuple, str] = {}
    out = []
    for r in sorted(rows, key=lambda r: r["source_tab"] == SUMMARY_TAB):
        k = _key(r)
        if k in seen and r["source_tab"] == SUMMARY_TAB:
            rejected.append({"tab": r["source_tab"], "row": r["source_row"], "title": r["title"][:60],
                             "why": f"repeat of a row in {seen[k]!r}"})
            continue
        seen.setdefault(k, r["source_tab"])
        out.append(r)
    out.sort(key=lambda r: (r["period_start"], r["source_tab"], r["source_row"]))
    return out


# ---- MSRP from the title -------------------------------------------------

_MSRP_RE = re.compile(r"\b(?:msrp|retail|reg\.?|orig(?:inal)?)\s*(?:price)?\s*:?\s*\$\s*(\d{1,4}(?:\.\d{2})?)", re.I)
_BARE_RE = re.compile(r"\$\s*(\d{1,4}(?:\.\d{2})?)\b")


def msrp_from_title(title: str) -> tuple[float | None, str | None]:
    m = _MSRP_RE.search(title)
    if m:
        return float(m.group(1)), "title_msrp"
    m = _BARE_RE.search(title)
    if m:
        return float(m.group(1)), "title_price"
    return None, None


# ---- AI: brand, type, bag size ------------------------------------------

ITEM_TYPES = ("bag", "wallet", "backpack", "luggage", "watch_band", "watch", "jewelry",
              "sunglasses", "fragrance", "beauty", "bedding", "clothing", "shoes",
              "accessory", "home", "electronics", "toy", "other")
BAG_SIZES = ("large", "small")

PROMPT = """You label eBay listing titles from a resale store (mostly new bags).
For each title return brand, parent_brand, item_type and bag_size.

- brand: the maker as usually written ("Juicy Couture", "Betsey Johnson",
  "Michael Kors", "Herschel Supply Co."). A sub-line sold under its own name
  keeps that name ("Luv Betsey", "Lauren Ralph Lauren", "Polo Ralph Lauren",
  "Relic by Fossil"); "MICHAEL Michael Kors" is just "Michael Kors".
  null when the title names no brand.
- parent_brand: for a sub-line, the main brand it belongs to ("Luv Betsey"
  -> "Betsey Johnson", "Lauren Ralph Lauren" -> "Ralph Lauren", "Relic by
  Fossil" -> "Fossil"). null when brand is already the main brand.
- item_type: one of {types}. Handbags, totes, crossbody, slings, shoulder
  bags, satchels, clutches, duffels, weekenders, cosmetic bags, toiletry and
  travel kits, belt/waist bags -> "bag". Backpacks -> "backpack". Wallets,
  wristlets, card cases, lanyards -> "wallet". Suitcases and carry-on
  suitcases -> "luggage" (a "carry-on bag kit" or toiletry kit is a bag).
- bag_size: only for bag, backpack, wallet and luggage.
  "small": wallets, wristlets, card cases, pouches, cosmetic bags, toiletry
  and travel kits, coin purses, mini bags, crossbody and sling bags (unless
  the title says large), and kids' bags unless the title says full size.
  "large": totes, satchels, shoulder bags, duffels, weekenders, luggage,
  backpacks (unless mini or small), and anything the title calls "large"
  or "full size". null otherwise.

Return one entry per title, with its index i.

Titles:
{titles}"""


SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["items"],
    "properties": {"items": {"type": "array", "items": {
        "type": "object", "additionalProperties": False,
        "required": ["i", "brand", "parent_brand", "item_type", "bag_size"],
        "properties": {
            "i": {"type": "integer"},
            "brand": {"type": ["string", "null"]},
            "parent_brand": {"type": ["string", "null"]},
            "item_type": {"type": "string", "enum": list(ITEM_TYPES)},
            "bag_size": {"anyOf": [{"type": "string", "enum": list(BAG_SIZES)}, {"type": "null"}]},
        }}}},
}


def classify(titles: list[str], cache: dict, batch: int = 80) -> dict:
    """{title: {"brand", "item_type", "bag_size"}} for every title, from the
    cache or one Claude call per batch (the cheap model). The cache is saved
    after each batch, so a rerun only asks for what's missing."""
    import claude_llm
    todo = [t for t in titles if t not in cache or "parent_brand" not in cache[t]]
    spent = 0.0
    for start in range(0, len(todo), batch):
        chunk = todo[start:start + batch]
        listing = "\n".join(f"{i}. {t}" for i, t in enumerate(chunk))
        meta: dict = {}
        out = claude_llm.ask("old_store_classify",
                             PROMPT.format(types=", ".join(ITEM_TYPES), titles=listing),
                             model=config.CLAUDE_CHEAP_MODEL, max_tokens=16000,
                             output_schema=SCHEMA, meta=meta)
        spent += meta.get("cost_usd") or 0
        for d in out["items"]:
            if not 0 <= d["i"] < len(chunk):
                continue
            typ = d["item_type"] if d["item_type"] in ITEM_TYPES else "other"
            size = d["bag_size"] if typ in ("bag", "backpack", "wallet", "luggage") else None
            brand = (d["brand"] or "").strip() or None
            parent = (d["parent_brand"] or "").strip() or None
            cache[chunk[d["i"]]] = {"brand": brand,
                                    "parent_brand": parent if parent and brand and parent.lower() != brand.lower() else None,
                                    "item_type": typ, "bag_size": size}
        AI_CACHE.write_text(json.dumps(cache, indent=1, ensure_ascii=False), encoding="utf-8")
        print(f"  classified {min(start + batch, len(todo))}/{len(todo)} (${spent:.3f} so far)")
    missing = [t for t in titles if t not in cache]
    if missing:
        print(f"  {len(missing)} titles came back without a label; rerun to retry them")
    return cache


def _canonical_brands(labels: dict) -> dict[str, str]:
    """lowercase brand -> its most common spelling ("KORRES" and "Korres" ->
    "Korres"; a tie goes to the spelling with lowercase letters)."""
    counts: dict[str, dict[str, int]] = {}
    for lab in labels.values():
        for b in ((lab or {}).get("brand"), (lab or {}).get("parent_brand")):
            if not b:
                continue
            counts.setdefault(b.lower(), {}).setdefault(b, 0)
            counts[b.lower()][b] += 1
    return {k: max(v, key=lambda b: (v[b], b != b.upper())) for k, v in counts.items()}


def enrich(rows: list[dict], labels: dict) -> list[dict]:
    canon = _canonical_brands(labels)
    for r in rows:
        lab = labels.get(r["title"]) or {}
        r["brand"] = canon.get((lab.get("brand") or "").lower()) or None
        r["parent_brand"] = canon.get((lab.get("parent_brand") or "").lower()) or None
        r["item_type"] = lab.get("item_type")
        r["bag_size"] = lab.get("bag_size")
        r["msrp"], r["msrp_from"] = msrp_from_title(r["title"])
    return rows


def write_csv(rows: list[dict], path: Path = OUT_CSV) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k) for k in COLUMNS})


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("step", choices=["parse", "classify"])
    args = ap.parse_args(argv)
    rows, rejected = parse()
    print(f"{len(rows)} usable rows, {sum(r['quantity'] for r in rows)} units, "
          f"{len({r['title'] for r in rows})} distinct titles; {len(rejected)} rows left out")
    why: dict[str, int] = {}
    for x in rejected:
        k = "repeat" if x["why"].startswith("repeat") else x["why"]
        why[k] = why.get(k, 0) + 1
    print("  left out:", why)
    print("  to check:", sum(r["status"] == "to_check" for r in rows))
    if args.step == "classify":
        cache = json.loads(AI_CACHE.read_text(encoding="utf-8")) if AI_CACHE.exists() else {}
        labels = classify(sorted({r["title"] for r in rows}), cache)
        write_csv(enrich(rows, labels))
        print(f"wrote {OUT_CSV}")


if __name__ == "__main__":
    main()
