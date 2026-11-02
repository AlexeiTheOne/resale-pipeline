"""Old SQLite item dict <-> ResaleDB rows. Pure Python, no database.

    split_item(item, ...) -> {table: [row, ...]}   what gets stored
    join_item(parts)      -> item dict             what db.get_item returns

The promise is join_item(split_item(x)) == x, types included, for every item.
Real columns hold every value they can hold exactly; that is what SQL filters,
joins and reports use. Anything a column can't reproduce exactly goes to the
group's leftover JSON, which wins on read:

  - None values, empty lists, keys with no column;
  - values of the "wrong" type (an int where the column is DECIMAL keeps its
    column value for SQL, but the int itself is kept in the leftover);
  - legacy timestamp formats ('...Z', naive local time): the column gets the
    UTC time and the leftover keeps the original string;
  - "__absent__": keys that a NOT NULL column default would otherwise add.

Leftovers live in the group's own *_raw column where there is one
(identification_raw, pricing_raw, ebay_raw), else in items.extra_raw under the
group's name. Nothing may read a leftover for logic (docs/schema-proposal.md,
"The JSON rule"); it is here only so get_item can rebuild the old dict.

Raw dicts here are as stored in SQLite: paths are not resolved (db.py does
that on the way out, the same for both backends).
"""
import math
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from ebay import orders

# Legacy timestamps without an offset were written in local time on the
# production PC (scripts/backfill_sales.py), which runs on Eastern time.
LOCAL_TZ = ZoneInfo("America/New_York")
ABSENT = "__absent__"
INT_MAX = 2**31 - 1


# --- column kinds -----------------------------------------------------------
# A kind says what a column stores and how a value comes back out of it.

def S(n=None, unicode=True):
    return ("str", n, unicode)


def DEC(p, s):
    return ("dec", p, s)


INT = ("int",)
BIT = ("bit",)
TS = ("ts",)            # Python isoformat ('...+00:00'); column = naive UTC
TS_EBAY = ("ts_ebay",)  # eBay's '2026-06-22T02:32:12.000Z'
DATE = ("date",)
CODE12 = ("code12",)    # Ross 12-digit code, CHAR(12)


def CSV(n):              # list of strings stored "a,b,c"
    return ("csv", n)


def _parse_ts(value):
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=LOCAL_TZ)
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


def render_ts(dt: datetime) -> str:
    return dt.replace(tzinfo=timezone.utc).isoformat()


def render_ts_ebay(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def to_db(value, kind):
    """(value for the column or None, exact). exact=False means the original
    must also go to the leftover; a non-None column value is still stored, so
    SQL sees the best approximation."""
    if value is None:
        return None, False
    tag = kind[0]
    if tag == "str":
        _, n, unicode = kind
        ok = type(value) is str and (n is None or len(value) <= n) and (unicode or value.isascii())
        return (value, True) if ok else (None, False)
    if tag == "dec":
        _, p, s = kind
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None, False
        try:
            d = Decimal(repr(value)) if isinstance(value, float) else Decimal(value)
            q = d.quantize(Decimal(1).scaleb(-s))
        except (InvalidOperation, ValueError):
            return None, False
        if len(q.as_tuple().digits) > p or not q.is_finite():
            return None, False
        return q, (type(value) is float and q == d)
    if tag == "int":
        if type(value) is int and -INT_MAX - 1 <= value <= INT_MAX:
            return value, True
        if type(value) is float and value.is_integer() and abs(value) <= INT_MAX:
            return int(value), False
        return None, False
    if tag == "bit":
        return (value, True) if type(value) is bool else (None, False)
    if tag in ("ts", "ts_ebay"):
        dt = _parse_ts(value)
        if dt is None:
            return None, False
        render = render_ts if tag == "ts" else render_ts_ebay
        return dt, render(dt) == value
    if tag == "date":
        if type(value) is not str:
            return None, False
        try:
            d = date.fromisoformat(value)
        except ValueError:
            return None, False
        return d, d.isoformat() == value
    if tag == "code12":
        ok = type(value) is str and len(value) == 12 and value.isdigit()
        return (value, True) if ok else (None, False)
    if tag == "csv":
        _, n = kind
        ok = (type(value) is list and value and all(type(x) is str and x and "," not in x for x in value)
              and len(",".join(value)) <= n)
        return (",".join(value), True) if ok else (None, False)
    raise ValueError(kind)


def from_db(value, kind):
    if value is None:
        return None
    tag = kind[0]
    if tag == "dec":
        return float(value)
    if tag == "int":
        return int(value)
    if tag == "bit":
        return bool(value)
    if tag == "ts":
        return render_ts(value)
    if tag == "ts_ebay":
        return render_ts_ebay(value)
    if tag == "date":
        return value.isoformat()
    if tag == "csv":
        return value.split(",")
    return value


# --- flat key <-> column groups --------------------------------------------

def split_cols(data: dict, spec: dict, leftover: dict, notnull=()) -> dict:
    """Columns for the keys in `spec` ({key: (column, kind)}); misfits go to
    `leftover`. `notnull` columns have a DB default, so an absent key is
    recorded in leftover[ABSENT] rather than invented on read."""
    cols = {}
    for key, (col, kind) in spec.items():
        if key not in data:
            if col in notnull:
                leftover.setdefault(ABSENT, []).append(key)
            continue
        dbv, exact = to_db(data[key], kind)
        if dbv is not None:
            cols[col] = dbv
        if not exact:
            leftover[key] = data[key]
    return cols


def join_cols(row: dict, spec: dict) -> dict:
    out = {}
    for key, (col, kind) in spec.items():
        v = row.get(col)
        if v is not None:
            out[key] = from_db(v, kind)
    return out


def finish(out: dict, leftover: dict | None) -> dict:
    leftover = leftover or {}
    for key in leftover.get(ABSENT, ()):
        out.pop(key, None)
    out.update({k: v for k, v in leftover.items() if k != ABSENT})
    return out


def _rest(data: dict, handled, leftover: dict) -> None:
    """Keys nobody claimed go to the leftover as they are."""
    for k, v in data.items():
        if k not in handled and k not in leftover:
            leftover[k] = v


def _str_list_ok(value, n) -> bool:
    """A non-empty list of distinct strings that fit and won't collide in a
    case-insensitive, trailing-space-blind primary key."""
    if type(value) is not list or not value:
        return False
    if not all(type(x) is str and len(x) <= n and x == x.rstrip() for x in value):
        return False
    return len({x.casefold() for x in value}) == len(value)


# --- the groups --------------------------------------------------------------

IDENTIFICATION = {
    "brand": ("brand", S(100)), "product_name": ("product_name", S(300)),
    "model": ("model", S(150)), "item_type": ("item_type", S(100)),
    "color": ("color", S(200)), "size": ("size", S(200)), "material": ("material", S(200)),
    "condition": ("condition", S(50)), "upc": ("upc", S(14, False)),
    "upc_decoded": ("upc_decoded", BIT), "style_code": ("style_code", S(60, False)),
    "department": ("department", S(30)), "gender": ("gender", S(20)),
    "search_query": ("search_query", S(300)), "retail_price": ("retail_price", DEC(10, 2)),
    "resale_estimate": ("resale_estimate", DEC(10, 2)), "confidence": ("confidence", DEC(4, 3)),
    "product_image_url": ("product_image_url", S(1000)),
}

# Q10: one spelling per condition in the column. The original text is kept
# (leftover) so get_item returns exactly what was stored.
_CONDITIONS = ["New with tags", "New without tags", "New with defects", "New with box",
               "New without box", "New", "Pre-owned", "Used", "For parts or not working"]
CONDITION_SPELLING = {c.casefold(): c for c in _CONDITIONS}


def normalise_condition(value):
    if type(value) is not str:
        return value
    return CONDITION_SPELLING.get(value.strip().casefold(), value)


PRICING = {
    "suggested_price": ("suggested_price", DEC(10, 2)), "machine_price": ("machine_price", DEC(10, 2)),
    "manual_price": ("manual_price", DEC(10, 2)), "manual_price_at": ("manual_price_at", TS),
    "claude_price": ("claude_price", DEC(10, 2)), "formula_price": ("formula_price", DEC(10, 2)),
    "price_basis": ("price_basis", S(20, False)), "confidence": ("confidence", S(20, False)),
    "claude_confidence": ("claude_confidence", S(20, False)), "evidence": ("evidence", S(10, False)),
    "needs_review": ("needs_review", BIT), "sold_count": ("sold_count", INT),
    "active_count": ("active_count", INT), "sold_median": ("sold_median", DEC(10, 2)),
    "sold_p10": ("sold_p10", DEC(10, 2)), "sold_p90": ("sold_p90", DEC(10, 2)),
    "active_floor": ("active_floor", DEC(10, 2)), "raw_active_floor": ("raw_active_floor", DEC(10, 2)),
    "dispersion": ("dispersion", DEC(6, 2)), "research_resale": ("research_resale", DEC(10, 2)),
    "retail_price": ("retail_price", DEC(10, 2)), "resale_estimate": ("resale_estimate", DEC(10, 2)),
    "comp_warning": ("comp_warning", S(400)), "query_rung": ("query_rung", S(20, False)),
    "rungs_tried": ("rungs_tried", CSV(100)), "stock_image_url": ("stock_image_url", S(1000)),
    "value_notes": ("value_notes", S(2000)),
}
PRICING_RUN = {"engine": ("engine", S(10, False)), "query_used": ("query_used", S(300))}
COMP_KINDS = {"sold_comps": "sold", "active_listings": "active", "judged_comps": "judged",
              "reference": "reference"}
COMP = {
    "price": ("price", DEC(10, 2)), "item_price": ("item_price", DEC(10, 2)),
    "shipping": ("shipping", DEC(10, 2)), "shipping_imputed": ("shipping_imputed", BIT),
    "title": ("title", S(300)), "condition": ("condition", S(60)), "url": ("url", S(1000)),
    "image": ("image_url", S(1000)), "verdict": ("verdict", S(20, False)),
    "seller": ("seller", S(100)), "source": ("source", S(30, False)), "id": ("ext_id", S(40, False)),
}

LISTING = {
    "title": ("title", S(80)), "description": ("description", S()),
    "price": ("list_price", DEC(10, 2)), "category_id": ("category_id", S(10, False)),
    "condition_id": ("condition_id", S(10, False)), "stock_image_url": ("stock_image_url", S(1000)),
}

EBAY = {
    "sku": ("sku", S(50, False)), "offer_id": ("offer_id", S(20, False)),
    "listing_id": ("listing_id", S(20, False)), "view_item_url": ("view_item_url", S(500)),
    "quantity": ("quantity", INT), "category_id": ("category_id", S(10, False)),
    "category_name": ("category_name", S(200)), "reselected_from": ("reselected_from", S(10, False)),
    "reselected_from_name": ("reselected_from_name", S(200)),
    "published_at": ("published_at", TS), "stock_note": ("stock_note", S(300)),
}
EBAY_NOTNULL = ("quantity",)
SALE = {
    "order_id": ("order_id", S(30, False)), "sold_at": ("sold_at", TS_EBAY), "units": ("units", INT),
    "sale": ("sale", DEC(10, 2)), "shipping": ("shipping", DEC(10, 2)), "fees": ("fees", DEC(10, 2)),
    "label": ("label", DEC(10, 2)), "ad_fee": ("ad_fee", DEC(10, 2)), "refund": ("refund", DEC(10, 2)),
    "net": ("net", DEC(10, 2)), "listing_id": ("listing_id", S(20, False)),
    "fees_known": ("fees_known", BIT),
}
# The ebay keys a recorded sale owns, rebuilt from the sales rows on read.
SALE_TOTAL_KEYS = tuple(k for k in orders.SALE_DB_FIELDS if k != "sales")

TAG = {  # items.receipt: the Ross price tag read at intake
    "code": ("ross_code", CODE12), "code_generated": ("code_generated", BIT),
    "barcode": ("ross_barcode", S(18, False)), "reduced_price": ("ross_price", DEC(10, 2)),
    "original_price": ("ross_original_price", DEC(10, 2)), "peeled": ("peeled", BIT),
    "source": ("tag_source", S(10, False)), "ocr_variant": ("ocr_variant", S(20, False)),
    "error": ("tag_error", S(200)), "photo": ("tag_photo_path", S(400)), "raw_text": ("tag_raw", S()),
}

PHOTO_LAYOUT = {"manual": ("photo_order_manual", BIT), "as_sent": ("photos_as_sent", BIT)}
PHOTO_LAYOUT_NOTNULL = ("photo_order_manual", "photos_as_sent")
STOCK = {"post_ready": ("post_ready", BIT), "post_blocker": ("post_blocker", S(300)),
         "source": ("origin", S(20, False))}
STOCK_NOTNULL = ("post_ready",)
QUICK_PRICE = {"price": ("quick_price", DEC(10, 2)), "basis": ("quick_price_basis", S(100)),
               "rough": ("quick_price_rough", BIT)}
# stock keys that live on the item's pieces (pieces = the one place location lives)
STOCK_ON_PIECES = ("location", "inventory_photo", "added_at")

LISTING_CHECK = {
    "model": ("model", S(60, False)), "cost_usd": ("cost_usd", DEC(18, 10)),
    "input_tokens": ("input_tokens", INT), "output_tokens": ("output_tokens", INT),
    "unchecked": ("unchecked", BIT), "checked_at": ("checked_at", TS),
}
LISTING_CHECK_NOTNULL = ("unchecked", "checked_at")
ISSUE = {"severity": ("severity", S(10, False)), "field": ("field", S(40, False)),
         "message": ("message", S(500))}

ITEM = {"status": ("status", S(20, False)), "created_at": ("created_at", TS),
        "updated_at": ("updated_at", TS), "price_source_url": ("price_source_url", S(1000))}

PIECE_INFO = {
    "brand": ("brand", S(100)), "model": ("model", S(150)), "item_type": ("item_type", S(100)),
    "color": ("color", S(200)), "product_name": ("product_name", S(300)),
    "upc": ("upc", S(14, False)), "style_code": ("style_code", S(60, False)),
    "msrp": ("msrp", DEC(10, 2)), "ross_code": ("ross_code", CODE12),
    "ross_barcode": ("ross_barcode", S(18, False)), "ross_price": ("ross_price", DEC(10, 2)),
    "ross_original_price": ("ross_original_price", DEC(10, 2)), "notes": ("notes", S(500)),
}
PIECE = {"status": ("status", S(10, False)), "value": ("value", DEC(10, 2)),
         "cost": ("cost", DEC(10, 2)), "order_id": ("order_id", S(30, False)),
         "photo": ("photo_path", S(400)), "item_id": ("item_id", S(36, False)),
         "created_at": ("created_at", TS), "updated_at": ("updated_at", TS),
         "sold_at": ("sold_at", TS)}
PIECE_KEYS = ("code", "box", "item_id", "status", "photo", "info", "value", "cost",
              "created_at", "updated_at", "sold_at", "order_id")

ITEM_FIELDS = ("item_id", "status", "photos", "processed", "identification", "pricing",
               "listing", "ebay", "created_at", "updated_at", "price_source_url", "receipt",
               "photo_layout", "listing_check", "stock")


# --- split -------------------------------------------------------------------

def _flags(item_id, kind, value, rows) -> bool:
    if not _str_list_ok(value, 100):
        return False
    rows += [{"item_id": item_id, "kind": kind, "flag": f, "position": i} for i, f in enumerate(value)]
    return True


def _split_identification(item_id, data, out):
    left = {}
    cols = split_cols(data, IDENTIFICATION, left)
    if "condition" in cols:
        norm = normalise_condition(cols["condition"])
        if norm != cols["condition"]:
            cols["condition"] = norm
            left["condition"] = data["condition"]
    if "condition_flags" in data and not _flags(item_id, "condition", data["condition_flags"], out["item_flags"]):
        left["condition_flags"] = data["condition_flags"]
    _rest(data, set(IDENTIFICATION) | {"condition_flags"}, left)
    out["item_identification"].append({"item_id": item_id, **cols, "identification_raw": left or None})


def _split_comp(comp: dict) -> tuple[dict, dict]:
    left = {}
    cols = split_cols(comp, COMP, left)
    _rest(comp, COMP, left)
    return cols, left


def _split_pricing(item_id, data, out, run_key, priced_at):
    left = {}
    cols = split_cols(data, PRICING, left)
    run = split_cols(data, PRICING_RUN, left)
    if "review_flags" in data and not _flags(item_id, "review", data["review_flags"], out["item_flags"]):
        left["review_flags"] = data["review_flags"]
    comps = []
    for key, kind in COMP_KINDS.items():
        if key not in data:
            continue
        value = data[key]
        entries = [value] if kind == "reference" and type(value) is dict else value
        if kind == "reference" and type(value) is not dict:
            left[key] = value
            continue
        if type(entries) is not list or not entries or not all(type(c) is dict for c in entries):
            left[key] = value
            continue
        for pos, comp in enumerate(entries):
            c, cl = _split_comp(comp)
            comps.append({"run_key": run_key, "kind": kind, "position": pos, **c, "comp_raw": cl or None})
    _rest(data, set(PRICING) | set(PRICING_RUN) | {"review_flags"} | set(COMP_KINDS), left)
    out["pricing_runs"].append({"run_key": run_key, "item_id": item_id, "priced_at": priced_at, **run})
    out["pricing_comps"] += comps
    out["item_pricing"].append({"item_id": item_id, "run_key": run_key, **cols,
                                "priced_at": priced_at, "pricing_raw": left or None})


def _split_listing(item_id, data, out, extra):
    left = {}
    cols = split_cols(data, LISTING, left)
    if "item_specifics" in data:
        rows = _specifics_rows(item_id, data["item_specifics"])
        if rows is None:
            left["item_specifics"] = data["item_specifics"]
        else:
            out["listing_specifics"] += rows
    _rest(data, set(LISTING) | {"item_specifics"}, left)
    out["item_listing"].append({"item_id": item_id, **cols})
    if left:
        extra["listing"] = left


def _specifics_rows(item_id, spec):
    """Position 0 = a single value (None allowed); 1..n = a list. None if the
    dict can't be stored exactly (the whole dict then stays in the leftover)."""
    if type(spec) is not dict or not spec:
        return None
    names = list(spec)
    if not all(type(n) is str and 0 < len(n) <= 65 and n == n.rstrip() for n in names):
        return None
    if len({n.casefold() for n in names}) != len(names):
        return None
    rows = []
    for name, value in spec.items():
        if value is None or (type(value) is str and len(value) <= 400):
            rows.append({"item_id": item_id, "name": name, "position": 0, "value": value})
        elif type(value) is list and value and all(type(v) is str and len(v) <= 400 for v in value):
            rows += [{"item_id": item_id, "name": name, "position": i, "value": v}
                     for i, v in enumerate(value, start=1)]
        else:
            return None
    return rows


def _sale_rows(item_id, sales):
    """Sales rows, or None if any record can't be stored exactly."""
    if type(sales) is not list or not sales:
        return None
    rows = []
    for rec in sales:
        if type(rec) is not dict or set(rec) != set(SALE):
            return None
        left = {}
        cols = split_cols(rec, SALE, left)
        if left:
            return None
        rows.append({"item_id": item_id, **cols})
    return rows


def sale_totals(records: list[dict]) -> dict:
    """The ebay sale keys /sync derives from per-order records (ebay/orders.py)."""
    fields = orders.sale_db_fields(orders.totals_from_sales(records))
    fields.pop("sales", None)
    return fields


def _split_ebay(item_id, data, out):
    left = {}
    cols = split_cols(data, EBAY, left, notnull=EBAY_NOTNULL)
    if "image_urls" in data:
        urls = data["image_urls"]
        if type(urls) is list and urls and all(type(u) is str and len(u) <= 1000 for u in urls):
            out["listing_images"] += [{"item_id": item_id, "position": i, "url": u} for i, u in enumerate(urls)]
        else:
            left["image_urls"] = urls
    handled = set(EBAY) | {"image_urls", "sales"} | set(SALE_TOTAL_KEYS)
    rows = _sale_rows(item_id, data["sales"]) if "sales" in data else None
    if rows is None:
        # No usable rows: keep the sale keys exactly as stored.
        for k in ("sales",) + SALE_TOTAL_KEYS:
            if k in data:
                left[k] = data[k]
    else:
        out["sales"] += rows
        # Rows come back oldest first (totals_from_sales order); keep any
        # other stored order exactly.
        if orders.totals_from_sales(data["sales"])["sales"] != data["sales"]:
            left["sales"] = data["sales"]
        derived = sale_totals(data["sales"])
        for k in SALE_TOTAL_KEYS:
            if k in data:
                if k not in derived or derived[k] != data[k] or type(derived[k]) is not type(data[k]):
                    left[k] = data[k]
            elif k in derived:
                left.setdefault(ABSENT, []).append(k)
    _rest(data, handled, left)
    out["ebay_listings"].append({"item_id": item_id, **cols, "ebay_raw": left or None})


def _split_tag(item_id, data, out, extra):
    left = {}
    cols = split_cols(data, TAG, left)
    _rest(data, TAG, left)
    out["item_tags"].append({"item_id": item_id, **cols})
    if left:
        extra["receipt"] = left


def _split_listing_check(item_id, data, out, extra, check_key):
    left = {}
    cols = split_cols(data, LISTING_CHECK, left, notnull=LISTING_CHECK_NOTNULL)
    if "issues" in data:
        issues = data["issues"]
        rows = []
        if type(issues) is list and issues and all(type(i) is dict and set(i) == set(ISSUE) for i in issues):
            for pos, issue in enumerate(issues):
                il = {}
                rows.append({"check_key": check_key, "position": pos, **split_cols(issue, ISSUE, il)})
                if il:
                    rows = None
                    break
        else:
            rows = None
        if rows is None:
            left["issues"] = issues
        else:
            out["listing_check_issues"] += rows
    _rest(data, set(LISTING_CHECK) | {"issues"}, left)
    out["listing_checks"].append({"check_key": check_key, "item_id": item_id, **cols})
    if left:
        extra["listing_check"] = left


def _split_stock(item_id, data, item_cols, extra, pieces_for_item, new_pieces, units):
    left = {}
    item_cols.update(split_cols(data, STOCK, left, notnull=STOCK_NOTNULL))
    if "quick_price" in data:
        qp = data["quick_price"]
        ql = {}
        qcols = split_cols(qp, QUICK_PRICE, ql) if type(qp) is dict else {}
        if type(qp) is dict and qp and set(qp) <= set(QUICK_PRICE) and not ql:
            item_cols.update(qcols)
        else:
            left["quick_price"] = qp
    # location, inventory_photo, added_at: from the item's pieces (created here
    # when the item has none, one per unit). Whatever the pieces can't say
    # exactly stays in the leftover.
    if any(k in data for k in STOCK_ON_PIECES):
        if pieces_for_item:
            first = sorted(pieces_for_item, key=lambda p: p["code"])[0]
            for key, want in (("location", first.get("location_code")),
                              ("inventory_photo", first.get("photo_path")),
                              ("added_at", render_ts(first["created_at"]) if first.get("created_at") else None)):
                if key in data and data[key] != want:
                    left[key] = data[key]
                elif key not in data and want is not None:
                    left.setdefault(ABSENT, []).append(key)
        else:
            loc = data.get("location")
            if type(loc) is str and loc and loc == loc.strip().upper() and len(loc) <= 40:
                created, exact_added = to_db(data.get("added_at"), TS)
                photo = data.get("inventory_photo")
                photo_ok = photo is None or (type(photo) is str and len(photo) <= 400)
                for n in range(max(units, 1)):
                    new_pieces.append({"location_code": loc, "item_id": item_id,
                                       "photo_path": photo if photo_ok else None,
                                       "created_at": created})
                if "added_at" in data and not exact_added:
                    left["added_at"] = data["added_at"]
                if "added_at" not in data:
                    left.setdefault(ABSENT, []).append("added_at")
                if "inventory_photo" in data and (photo is None or not photo_ok):
                    left["inventory_photo"] = photo
            else:
                for k in STOCK_ON_PIECES:
                    if k in data:
                        left[k] = data[k]
    _rest(data, set(STOCK) | {"quick_price"} | set(STOCK_ON_PIECES), left)
    extra["stock"] = left  # present even when empty: the group exists


def split_item(item: dict, *, pieces_for_item=(), seq=None) -> dict:
    """Rows for one raw item. Child rows that need an id assigned by SQL Server
    (pricing run, listing check) carry a "run_key"/"check_key" the loader
    resolves. New pieces (from stock.location) are listed under "new_pieces"
    without codes; the loader numbers them."""
    out = {t: [] for t in ("items", "item_photos", "item_identification", "item_flags",
                           "pricing_runs", "item_pricing", "pricing_comps", "item_listing",
                           "listing_specifics", "ebay_listings", "listing_images", "sales",
                           "item_tags", "listing_checks", "listing_check_issues", "new_pieces")}
    item_id = item["item_id"]
    extra, top_left = {}, {}
    cols = split_cols(item, ITEM, top_left)
    for key in ("created_at", "updated_at", "price_source_url"):
        if item.get(key) is None:
            top_left.pop(key, None)  # NULL column already means None
    if item.get("processed") is not None:
        top_left["processed"] = item["processed"]
    photos = item.get("photos")
    if photos is not None:
        if type(photos) is list and photos and all(type(p) is str and len(p) <= 400 for p in photos):
            out["item_photos"] += [{"item_id": item_id, "position": i, "path": p} for i, p in enumerate(photos)]
        else:
            top_left["photos"] = photos
    _rest({k: v for k, v in item.items() if k not in ITEM_FIELDS}, (), top_left)

    if item.get("identification") is not None:
        _split_identification(item_id, item["identification"], out)
    if item.get("pricing") is not None:
        priced_at = None
        for candidate in ((item["pricing"] or {}).get("manual_price_at"), item.get("updated_at")):
            priced_at = _parse_ts(candidate)
            if priced_at:
                break
        _split_pricing(item_id, item["pricing"], out, run_key=item_id, priced_at=priced_at)
    if item.get("listing") is not None:
        _split_listing(item_id, item["listing"], out, extra)
    if item.get("ebay") is not None:
        _split_ebay(item_id, item["ebay"], out)
    if item.get("receipt") is not None:
        _split_tag(item_id, item["receipt"], out, extra)
    if item.get("listing_check") is not None:
        _split_listing_check(item_id, item["listing_check"], out, extra, check_key=item_id)
    if item.get("photo_layout") is not None:
        pl = item["photo_layout"]
        left = {}
        if type(pl) is dict:
            cols.update(split_cols(pl, PHOTO_LAYOUT, left, notnull=PHOTO_LAYOUT_NOTNULL))
            _rest(pl, PHOTO_LAYOUT, left)
            extra["photo_layout"] = left
        else:
            top_left["photo_layout"] = pl
    if item.get("stock") is not None:
        st = item["stock"]
        if type(st) is dict:
            units = (item.get("ebay") or {}).get("quantity") if type(item.get("ebay")) is dict else None
            _split_stock(item_id, st, cols, extra, list(pieces_for_item), out["new_pieces"],
                         units if type(units) is int else 1)
        else:
            top_left["stock"] = st
    if top_left:
        extra["_item"] = top_left
    row = {"item_id": item_id, **cols, "extra_raw": extra or None}
    if seq is not None:
        row["seq"] = seq
    out["items"].append(row)
    return out


# --- join --------------------------------------------------------------------

def join_item(p: dict) -> dict:
    """The raw item dict from one item's rows. `p` holds: item (row dict),
    photos, identification, flags, pricing, run, comps, listing, specifics,
    ebay, images, sales, tag, check, issues, pieces (row dicts, any order)."""
    row = p["item"]
    extra = row.get("extra_raw") or {}
    top = extra.get("_item", {})
    item = {k: None for k in ITEM_FIELDS}
    item["item_id"] = row["item_id"]
    item.update(join_cols(row, ITEM))
    photos = sorted(p.get("photos") or [], key=lambda r: r["position"])
    if photos:
        item["photos"] = [r["path"] for r in photos]
    flags = {"condition": [], "review": []}
    for r in sorted(p.get("flags") or [], key=lambda r: r["position"]):
        flags[r["kind"]].append(r["flag"])

    ident = p.get("identification")
    if ident is not None:
        d = join_cols(ident, IDENTIFICATION)
        if flags["condition"]:
            d["condition_flags"] = flags["condition"]
        item["identification"] = finish(d, ident.get("identification_raw"))

    pricing = p.get("pricing")
    if pricing is not None:
        d = join_cols(pricing, PRICING)
        if p.get("run") is not None:
            d.update(join_cols(p["run"], PRICING_RUN))
        if flags["review"]:
            d["review_flags"] = flags["review"]
        by_kind = {}
        for c in sorted(p.get("comps") or [], key=lambda r: (r["kind"], r["position"])):
            comp = finish(join_cols(c, COMP), c.get("comp_raw"))
            by_kind.setdefault(c["kind"], []).append(comp)
        for key, kind in COMP_KINDS.items():
            if kind in by_kind:
                d[key] = by_kind[kind][0] if kind == "reference" else by_kind[kind]
        item["pricing"] = finish(d, pricing.get("pricing_raw"))

    listing = p.get("listing")
    if listing is not None:
        d = join_cols(listing, LISTING)
        spec = {}
        for r in sorted(p.get("specifics") or [], key=lambda r: (r["position"])):
            if r["position"] == 0:
                spec[r["name"]] = r["value"]
            else:
                spec.setdefault(r["name"], []).append(r["value"])
        if spec:
            d["item_specifics"] = spec
        item["listing"] = finish(d, extra.get("listing"))

    eb = p.get("ebay")
    if eb is not None:
        d = join_cols(eb, EBAY)
        images = sorted(p.get("images") or [], key=lambda r: r["position"])
        if images:
            d["image_urls"] = [r["url"] for r in images]
        sales = p.get("sales") or []
        if sales:
            records = [join_cols(r, SALE) for r in sales]
            totals = sale_totals(records)
            # totals_from_sales sorts records oldest first; so does /sync.
            d["sales"] = orders.totals_from_sales(records)["sales"]
            d.update(totals)
        item["ebay"] = finish(d, eb.get("ebay_raw"))

    tag = p.get("tag")
    if tag is not None:
        item["receipt"] = finish(join_cols(tag, TAG), extra.get("receipt"))

    check = p.get("check")
    if check is not None:
        d = join_cols(check, LISTING_CHECK)
        issues = sorted(p.get("issues") or [], key=lambda r: r["position"])
        if issues:
            d["issues"] = [join_cols(r, ISSUE) for r in issues]
        item["listing_check"] = finish(d, extra.get("listing_check"))

    if "photo_layout" in extra:
        item["photo_layout"] = finish(join_cols(row, PHOTO_LAYOUT), extra["photo_layout"])

    if "stock" in extra:
        d = join_cols(row, STOCK)
        qp = join_cols(row, QUICK_PRICE)
        if qp:
            d["quick_price"] = qp
        pieces = sorted(p.get("pieces") or [], key=lambda r: r["code"])
        if pieces:
            first = pieces[0]
            d["location"] = first["location_code"]
            if first.get("photo_path") is not None:
                d["inventory_photo"] = first["photo_path"]
            if first.get("created_at") is not None:
                d["added_at"] = render_ts(first["created_at"])
        item["stock"] = finish(d, extra["stock"])

    for k, v in top.items():
        item[k] = v
    return item


# --- pieces ------------------------------------------------------------------

def split_piece(piece: dict) -> dict:
    """pieces row (+ location) for one raw SQLite pieces row."""
    left = {}
    info = piece.get("info")
    info = info if type(info) is dict else {}
    cols = split_cols(info, PIECE_INFO, left)
    _rest(info, PIECE_INFO, left)
    plain = {}
    pcols = split_cols(piece, PIECE, plain)
    for key in ("item_id", "photo", "value", "cost", "order_id", "sold_at"):
        if piece.get(key) is None:
            plain.pop(key, None)  # NULL column already means None
    if piece.get("info") is not None and type(piece.get("info")) is not dict:
        plain["info"] = piece["info"]
    raw = dict(left)
    if plain:
        raw["__piece__"] = plain
    return {"code": piece["code"], "location_code": piece["box"], **pcols, **cols,
            "info_raw": raw or None}


def join_piece(row: dict) -> dict:
    raw = dict(row.get("info_raw") or {})
    plain = raw.pop("__piece__", {})
    info = finish(join_cols(row, PIECE_INFO), raw)
    piece = {k: None for k in PIECE_KEYS}
    piece.update({"code": row["code"], "box": row["location_code"], "info": info})
    piece.update(join_cols(row, PIECE))
    piece.update(plain)
    return piece


# --- comparing two results ----------------------------------------------------

def diff(a, b, path="", out=None, approx=False):
    """Every difference between a (SQLite) and b (SQL Server), types included."""
    out = [] if out is None else out
    if type(a) is not type(b):
        if approx and isinstance(a, (int, float)) and isinstance(b, (int, float)) \
                and not isinstance(a, bool) and not isinstance(b, bool) and math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-9):
            return out
        out.append(f"{path}: {a!r} ({type(a).__name__}) != {b!r} ({type(b).__name__})")
    elif isinstance(a, dict):
        for k in sorted(set(a) | set(b), key=str):
            if k not in a:
                out.append(f"{path}.{k}: missing in SQLite, SQL Server has {b[k]!r}")
            elif k not in b:
                out.append(f"{path}.{k}: missing in SQL Server, SQLite has {a[k]!r}")
            else:
                diff(a[k], b[k], f"{path}.{k}", out, approx)
    elif isinstance(a, (list, tuple)):
        if len(a) != len(b):
            out.append(f"{path}: length {len(a)} != {len(b)}")
        for i, (x, y) in enumerate(zip(a, b)):
            diff(x, y, f"{path}[{i}]", out, approx)
    elif isinstance(a, float) and approx:
        if not math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-9):
            out.append(f"{path}: {a!r} != {b!r}")
    elif a != b:
        out.append(f"{path}: {a!r} != {b!r}")
    return out
