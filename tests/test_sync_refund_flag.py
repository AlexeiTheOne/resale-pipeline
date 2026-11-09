"""/sync asks about an order refunded in full ("returned?") instead of leaving
the item silently 'sold' with $0 kept (the Versus Versace watch, c1872c91).
No eBay or Telegram calls: sync_one and the eBay reads are faked."""
import asyncio

import db
from ebay import listings, orders
from pipeline import sync

_VERSACE = {"item_id": "c1872c91-aaaa", "status": "sold", "listing": {"title": "Versus Versace Tokyo Watch"},
            "ebay": {"offer_id": "o1"}}
_NASH = {"item_id": "331dc4a9-bbbb", "status": "sold", "listing": {"title": "Patricia Nash Ava"},
         "ebay": {"offer_id": "o2"}}
_REFUND = [{"order_id": "15-15080-62452", "refund": 99.98}]


def _run(monkeypatch, items, live=()):
    monkeypatch.setattr(db, "list_items", lambda: [dict(i) for i in items])
    monkeypatch.setattr(db, "update_status", lambda *a: None)
    monkeypatch.setattr(db, "update_field", lambda *a: None)
    monkeypatch.setattr(orders, "sales_by_item", lambda days: {})
    monkeypatch.setattr(listings, "get_active_listings", lambda: list(live))

    def fake_sync_one(item, sold_map=None):
        return {"item_id": item["item_id"], "changes": [], "auto_sold": False, "new_sale": None,
                "full_refunds": _REFUND, "likely_sold": False, "ended_unsold": False,
                "listing_status": "ENDED"}
    monkeypatch.setattr(sync, "sync_one", fake_sync_one)
    return asyncio.run(sync.run_sync(30))


def test_a_full_refund_is_asked_about_in_the_brief(monkeypatch):
    lines, brief = _run(monkeypatch, [_VERSACE])
    ask = [b for b in brief if b.startswith("↩️")]
    assert len(ask) == 1
    assert "returned?" in ask[0] and "c1872c91" in ask[0] and "$99.98" in ask[0]
    assert "15-15080-62452" in ask[0] and "Versus Versace" in ask[0]
    assert any("Refunded in full (1)" in line for line in lines)


def test_a_relisted_item_is_not_asked_about(monkeypatch):
    live = [{"sku": _NASH["item_id"], "listing_id": "L9", "price": 149.99, "quantity": 1, "sold_quantity": 1,
             "view_item_url": "u"}]
    lines, brief = _run(monkeypatch, [_NASH], live)
    assert not [b for b in brief if b.startswith("↩️")]
    assert not any("Refunded in full" in line for line in lines)
