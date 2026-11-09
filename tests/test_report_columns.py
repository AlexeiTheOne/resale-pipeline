"""/report: Ross price, code, cost source, sold date, days listed; red rows for
an assumed cost; the "Last 7 days" sheet (report.py)."""
from datetime import datetime, timedelta, timezone

import pytest
from openpyxl import load_workbook

import db
import report

NOW = datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc)


def _item(iid, status="sold", *, price=0.49, source="barcode", code="400267105188", generated=False,
          sales=(), published="2026-08-17T02:31:53+00:00", listed=39.99):
    rec = {"code": code, "source": source, "code_generated": generated}
    if price is not None:
        rec["reduced_price"] = price
    ebay = {"published_at": published, "quantity": 0 if status == "sold" else 1, "sales": list(sales)}
    if sales:
        ebay.update(sale_price=sum(s["sale"] for s in sales), first_sold_at=sales[0]["sold_at"],
                    sold_at=sales[-1]["sold_at"], units_sold=sum(s["units"] for s in sales), fees_known=True,
                    shipping_collected=9.99, ebay_fees=8.0, shipping_label_cost=6.0)
    return {"item_id": iid + "-0000", "status": status, "created_at": "2026-08-17", "photos": [],
            "receipt": rec, "ebay": ebay, "listing": {"title": f"Item {iid}", "price": listed}}


def _sale(day, sale=39.99, net=30.0, units=1, refund=0.0):
    return {"order_id": f"o{day}", "sold_at": f"2026-11-{day:02d}T05:00:00.000Z", "units": units, "sale": sale,
            "shipping": 9.99, "fees": 8.0, "ad_fee": 2.0, "label": 6.0, "refund": refund, "net": net,
            "fees_known": True}


def test_cost_source():
    receipts = {("400267105188", 0.49)}
    assert report.cost_source(_item("a"), receipts) == "receipt"
    assert report.cost_source(_item("a", price=21.99), {("400267105188", 22.0)}) == "receipt"   # stored a cent high
    assert report.cost_source(_item("a")) == "tag"
    assert report.cost_source(_item("a", source="vision")) == "tag"
    assert report.cost_source(_item("a", source="manual")) == "typed"
    assert report.cost_source(_item("a", price=None)) == "assumed"


def test_days_listed_and_sold_on():
    sold = _item("a", sales=[_sale(3)])
    assert report.sold_on(sold).date().isoformat() == "2026-11-03"
    assert report.days_listed(sold, NOW) == 78
    assert report.days_listed(_item("b", status="published"), NOW) == 84
    assert report.days_listed(_item("c", published=None), NOW) is None


def test_recent_sales_totals():
    items = [_item("a", price=10.0, sales=[_sale(3, net=30.0)]),
             _item("b", price=None, listed=100.0, sales=[_sale(8, sale=50.0, net=40.0)]),
             _item("c", price=5.0, sales=[_sale(1, net=20.0)]),          # 8 days ago: out
             _item("d", status="published")]
    rec = report.recent_sales(items, 7, NOW)
    assert [r["item_id"][:1] for r in rec["rows"]] == ["a", "b"]
    t = rec["totals"]
    assert (t["orders"], t["units"], t["revenue"], t["fees"]) == (2, 2, 89.99, 20.0)
    cost_a = round(10.0 * (1 + report.profit.SALES_TAX_RATE), 2)
    cost_b = round(100.0 * report.profit.ASSUMED_COST_RATIO, 2)
    assert t["profit"] == round(30.0 - cost_a + 40.0 - cost_b, 2)
    assert t["avg_profit"] == round(t["profit"] / 2, 2) and t["assumed"] == 1
    assert rec["rows"][1]["cost_source"] == "assumed" and rec["rows"][0]["days"] == 78


def test_the_sheet_has_the_new_columns_red_rows_and_last_7_days(tmp_path, monkeypatch):
    import ebay.orders
    monkeypatch.setattr(ebay.orders, "account_charges", lambda d: (_ for _ in ()).throw(RuntimeError("no eBay")))
    recent = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%dT05:00:00.000Z")
    sale = {**_sale(1), "sold_at": recent}
    items = [_item("a", sales=[sale]), _item("b", status="published", price=None, code="000000000006",
                                             generated=True)]
    monkeypatch.setattr(report, "list_items", lambda: items)
    monkeypatch.setattr(db, "receipt_lines_for_skus", lambda skus: [])
    wb = load_workbook(report.build_report(str(tmp_path / "r.xlsx")))
    ws = wb["Profit"]
    assert [c.value for c in ws[11]][15:] == ["Ross price (pre-tax)", "Ross code", "Cost source", "Sold",
                                              "Days listed"]
    rows = {ws.cell(row=r, column=2).value: r for r in range(12, 14)}
    a, b = rows["a-0000"[:8]], rows["b-0000"[:8]]
    assert ws.cell(row=a, column=16).value == 0.49 and ws.cell(row=a, column=17).value == "400267105188"
    assert ws.cell(row=a, column=18).value == "tag" and ws.cell(row=a, column=19).value == recent[:10]
    assert ws.cell(row=b, column=18).value == "assumed" and ws.cell(row=b, column=17).value is None
    assert ws.cell(row=b, column=3).fill.fgColor.rgb.endswith("FFC7CE")          # red row
    assert not ws.cell(row=a, column=3).fill.fgColor.rgb.endswith("FFC7CE")
    last = wb["Last 7 days"]
    assert last["A1"].value.startswith("Sold in the last 7 days")
    assert last["A3"].value == "Orders" and last["B3"].value == 1
    labels = [last.cell(row=r, column=1).value for r in range(3, 14)]
    assert {"Units", "Revenue (item prices)", "Fees (eBay + ads)", "Profit", "Average profit per order",
            "Average days to sell"} <= set(labels)
