"""dbmap: split_item then join_item gives back the exact item (types too).
Pure Python, no server: rows are passed straight from split to join, after
the same conversions SQL Server applies."""
import copy
from datetime import date, datetime
from decimal import Decimal

import pytest

import dbmap
from ebay import orders


def _as_stored(rows):
    """What SQL Server would hand back: Decimal stays Decimal, datetimes lose
    nothing at DATETIME2(6), None columns are absent."""
    return [{k: v for k, v in r.items() if v is not None} for r in rows]


def roundtrip(item, pieces=()):
    out = dbmap.split_item(copy.deepcopy(item), pieces_for_item=pieces, create_pieces=True)
    run = out["pricing_runs"][0] if out["pricing_runs"] else None
    check = out["listing_checks"][0] if out["listing_checks"] else None
    new_pieces = [{"code": f"{p['location_code']}-{i:02d}", **p}
                  for i, p in enumerate(out["new_pieces"], start=1)]
    defaults = {"post_ready": False, "photo_order_manual": False, "photos_as_sent": False}
    item_row = {**defaults, **_as_stored(out["items"])[0]}
    for row in out["ebay_listings"]:
        row.setdefault("quantity", 0)
    for row in out["listing_checks"]:
        row.setdefault("unchecked", False)
        row.setdefault("checked_at", datetime(2026, 1, 1))
    parts = {
        "item": item_row,
        "photos": out["item_photos"], "flags": out["item_flags"],
        "identification": (_as_stored(out["item_identification"]) or [None])[0],
        "pricing": (_as_stored(out["item_pricing"]) or [None])[0],
        "run": run, "comps": _as_stored(out["pricing_comps"]),
        "listing": (_as_stored(out["item_listing"]) or [None])[0],
        "specifics": out["listing_specifics"],
        "ebay": (_as_stored(out["ebay_listings"]) or [None])[0],
        "images": out["listing_images"], "sales": _as_stored(out["sales"]),
        "tag": (_as_stored(out["item_tags"]) or [None])[0],
        "check": (_as_stored(out["listing_checks"]) or [None])[0] if check else None,
        "issues": out["listing_check_issues"],
        "pieces": list(pieces) + new_pieces,
    }
    return dbmap.join_item(parts), out


def _base(**over):
    item = {k: None for k in dbmap.ITEM_FIELDS}
    item.update({"item_id": "11111111-2222-3333-4444-555555555555", "status": "published",
                 "photos": ["data/inbox/x/1.jpg", "data/inbox/x/2.jpg"],
                 "created_at": "2026-10-19T15:28:04.487014+00:00",
                 "updated_at": "2026-10-19T15:28:04+00:00"})
    item.update(over)
    return item


def _sales():
    recs = [
        {"order_id": "25-1", "sold_at": "2026-08-01T10:00:00.000Z", "units": 1, "sale": 40.0,
         "shipping": 8.0, "fees": 6.1, "label": 7.5, "ad_fee": 2.0, "refund": 0.0, "net": 32.4,
         "listing_id": "117", "fees_known": True},
        {"order_id": "25-2", "sold_at": "2026-09-01T10:00:00.123Z", "units": 2, "sale": 30.0,
         "shipping": 9.0, "fees": 4.0, "label": 8.0, "ad_fee": 0.0, "refund": 33.0, "net": -6.0,
         "listing_id": "117", "fees_known": False},
    ]
    return orders.sale_db_fields(orders.totals_from_sales(recs))


FULL = _base(
    identification={"brand": "DKNY", "model": None, "retail_price": 118, "resale_estimate": 75.0,
                    "condition": "New With Tags", "condition_flags": ["NWT", "missing tags"],
                    "specifications": {"Bag Height": "12 in"}, "upc": "0193624034479",
                    "upc_decoded": True, "confidence": 0.95, "price_evidence": [{"source": "x"}]},
    pricing={"suggested_price": 84.99, "manual_price": 79.99,
             "manual_price_at": "2026-10-19T15:26:08.868853+00:00", "confidence": "low",
             "needs_review": False, "sold_count": 3, "rungs_tried": ["exact", "product"],
             "review_flags": [], "engine": "v2", "query_used": "DKNY tote",
             "sold_comps": [{"price": 50.0, "title": "a", "condition": "New", "image": "i", "url": "u"},
                            {"price": 55.5, "title": "b", "condition": "New", "image": "i"}],
             "active_listings": [],
             "judged_comps": [{"id": "s1", "source": "sold", "verdict": "same", "title": "t",
                               "total": 61.0, "item_price": 55.0, "shipping": 6.0,
                               "shipping_imputed": False, "condition": "New", "url": "u",
                               "seller": "z"}],
             "reference": None, "store_estimate": {"msrp": 118}},
    listing={"title": "DKNY Sullivan Tote Cityscape Print ", "price": 69.99, "description": "x\ny",
             "category_id": "169291", "condition_id": "1000", "stock_image_url": None,
             "item_specifics": {"Brand": "DKNY", "Features": ["Zip", "Pocket"], "Vintage": None,
                                "MPN": "R84"}},
    ebay={"sku": "5a58e7ac-3e19-4496-80ee-ef7b63bae489", "listing_id": "117", "offer_id": "191",
          "quantity": 0, "published_at": "2026-06-22T02:32:12.000Z", "reselected_from": None,
          "image_urls": ["https://i/1", "https://i/2"], "source": "external", **_sales()},
    receipt={"code": "400271128951", "barcode": "400271128951000449", "reduced_price": 23.0,
             "original_price": None, "peeled": False, "source": "barcode", "raw_text": "ROSS\n23.00",
             "photo": "data/inbox/x/tag.jpg"},
    photo_layout={"manual": True},
    listing_check={"issues": [{"severity": "warn", "field": "title", "message": "long"}],
                   "model": "claude-x", "cost_usd": 0.0123456789012, "input_tokens": 10,
                   "output_tokens": 2, "checked_at": "2026-10-20T01:02:03.000004+00:00"},
    price_source_url="https://www.ebay.com/itm/1",
)


def strict_diff(a, b):
    return dbmap.diff(a, b, "item")


@pytest.mark.parametrize("item", [
    FULL,
    _base(status="captured", photos=[]),
    _base(updated_at="2026-09-03T14:10:00.123456"),       # naive local time (backfill_sales.py)
    _base(processed={"legacy": 1}, stock={"quick_price": {"price": 30.0, "rough": True}},
          photo_layout={"manual": False, "as_sent": True}),
    _base(pricing={"rungs_tried": ["Luv Betsey Johnson LBPERCY black white polka dot backpack, x"] * 3}),
    _base(ebay={"quantity": 2, "sales": [], "net_proceeds": 5.0}),
    _base(listing={"item_specifics": {"Color": "Red", "color": "red"}}),  # names collide in SQL
], ids=["full", "minimal", "naive-local", "odd-groups", "long-rungs", "odd-sales", "case-names"])
def test_split_then_join_is_exact(item):
    back, _ = roundtrip(item)
    assert strict_diff(item, back) == []


def test_columns_get_the_values_sql_needs():
    _, out = roundtrip(FULL)
    ident = out["item_identification"][0]
    assert ident["brand"] == "DKNY"
    assert ident["condition"] == "New with tags"           # Q10 spelling in the column
    assert ident["identification_raw"]["condition"] == "New With Tags"  # original kept
    assert ident["retail_price"] == Decimal("118.00")      # int still queryable
    assert ident["identification_raw"]["retail_price"] == 118
    assert out["item_listing"][0]["list_price"] == Decimal("69.99")
    assert out["item_tags"][0]["ross_price"] == Decimal("23.00")
    assert out["ebay_listings"][0]["published_at"] == datetime(2026, 6, 22, 2, 32, 12)
    assert {r["order_id"] for r in out["sales"]} == {"25-1", "25-2"}
    # Sale totals are rebuilt from the sales rows, not stored.
    assert "net_proceeds" not in (out["ebay_listings"][0]["ebay_raw"] or {})
    assert [r["flag"] for r in out["item_flags"] if r["kind"] == "condition"] == ["NWT", "missing tags"]
    judged = [c for c in out["pricing_comps"] if c["kind"] == "judged"][0]
    assert judged["comp_raw"] == {"total": 61.0} and judged["ext_id"] == "s1"


def test_naive_local_time_becomes_utc_in_the_column():
    _, out = roundtrip(_base(updated_at="2026-09-03T14:10:00.123456"))
    row = out["items"][0]
    assert row["updated_at"] == datetime(2026, 9, 3, 18, 10, 0, 123456)  # EDT is UTC-4
    assert row["extra_raw"]["_item"]["updated_at"] == "2026-09-03T14:10:00.123456"


def test_sales_totals_that_disagree_with_the_rows_are_kept():
    ebay = dict(FULL["ebay"], net_proceeds=999.0)
    item = dict(FULL, ebay=ebay)
    back, out = roundtrip(item)
    assert back["ebay"]["net_proceeds"] == 999.0
    assert out["ebay_listings"][0]["ebay_raw"]["net_proceeds"] == 999.0


def test_stock_location_becomes_pieces():
    item = _base(ebay={"quantity": 2}, stock={"location": "G3", "added_at": "2026-10-01T00:00:00+00:00",
                                              "post_ready": True, "source": "inventory"})
    back, out = roundtrip(item)
    assert [p["location_code"] for p in out["new_pieces"]] == ["G3", "G3"]  # one per unit
    assert out["items"][0]["origin"] == "inventory"
    assert strict_diff(item, back) == []


def test_no_location_means_no_piece():
    item = _base(stock={"quick_price": {"price": 10.0}})
    back, out = roundtrip(item)
    assert out["new_pieces"] == []
    assert strict_diff(item, back) == []


def test_stock_from_existing_pieces():
    piece = {"code": "O1-WATCHES-01", "location_code": "O1-WATCHES", "photo_path": "data/pieces/a.jpg",
             "created_at": datetime(2026, 10, 1, 12, 0, 0)}
    item = _base(stock={"location": "O1-WATCHES", "inventory_photo": "data/pieces/a.jpg",
                        "added_at": "2026-10-01T12:00:00+00:00"})
    back, out = roundtrip(item, pieces=[piece])
    assert out["new_pieces"] == []
    assert strict_diff(item, back) == []


@pytest.mark.parametrize("value,kind,expect", [
    (84.99, dbmap.DEC(10, 2), (Decimal("84.99"), True)),
    (84.999, dbmap.DEC(10, 2), (Decimal("85.00"), False)),
    (5, dbmap.DEC(10, 2), (Decimal("5.00"), False)),
    (True, dbmap.DEC(10, 2), (None, False)),
    ("2026-06-22T02:32:12.000Z", dbmap.TS_EBAY, (datetime(2026, 6, 22, 2, 32, 12), True)),
    ("2026-06-22T02:32:12.000Z", dbmap.TS, (datetime(2026, 6, 22, 2, 32, 12), False)),
    ("2026-07-13", dbmap.DATE, (date(2026, 7, 13), True)),
    ("ëlan", dbmap.S(10, False), (None, False)),
    ("000000000006", dbmap.CODE12, ("000000000006", True)),
    ("12345", dbmap.CODE12, (None, False)),
])
def test_to_db(value, kind, expect):
    assert dbmap.to_db(value, kind) == expect


def test_piece_roundtrip():
    piece = {"code": "G3-07", "box": "G3", "item_id": None, "status": "home", "photo": "data/pieces/g.jpg",
             "info": {"brand": "DKNY", "ross_code": "400271128951", "brand_visible": True, "msrp": 59},
             "value": 30.0, "cost": 12, "created_at": "2026-10-01T12:00:00+00:00",
             "updated_at": "2026-10-01T12:00:00+00:00", "sold_at": None, "order_id": None}
    row = {k: v for k, v in dbmap.split_piece(piece).items() if v is not None}
    assert strict_diff(piece, dbmap.join_piece(row)) == []
