"""db.py: check_evidence (price evidence, also the competitor-search cache).
Runs on both backends: the SQLite run and the DB_BACKEND=sqlserver run see the
same dicts."""
from datetime import datetime, timedelta, timezone

import pytest

import db


def _row(**kw):
    base = {"model_key": "edhardy|code|ehh462", "source": "ebay_active", "found_by": "mpn",
            "store": "seller1", "title": "Ed Hardy Skull Bag EHH462-42-003", "condition": "New with tags",
            "price": 39.99, "shipping": 7.5, "total": 47.49, "url": "https://www.ebay.com/itm/1",
            "ebay_id": "1", "category_id": "169291", "photo_count": 9, "returns_accepted": True,
            "best_offer": False, "specifics": {"Style": "Shoulder Bag", "Color": "Black"},
            "query": "Ed Hardy EHH462-42-003", "item_id": "d7432192-95d6-42bb-b111-6656046ae27e"}
    base.update(kw)
    return base


def test_add_and_read_back():
    assert db.add_evidence([_row(), _row(ebay_id="2", best_offer=True, specifics=None, shipping=None,
                                         total=None)]) == 2
    rows = db.recent_evidence("edhardy|code|ehh462", "ebay_active",
                              datetime.now(timezone.utc) - timedelta(minutes=5))
    assert [r["ebay_id"] for r in rows] == ["1", "2"]
    a, b = rows
    assert a["price"] == 39.99 and isinstance(a["price"], float) and a["total"] == 47.49
    assert a["returns_accepted"] is True and a["best_offer"] is False and b["best_offer"] is True
    assert a["specifics"] == {"Style": "Shoulder Bag", "Color": "Black"} and b["specifics"] is None
    assert a["photo_count"] == 9 and a["found_by"] == "mpn" and b["shipping"] is None
    assert datetime.fromisoformat(a["fetched_at"]).tzinfo is not None


def test_recent_filters_by_model_source_and_age():
    old = (datetime.now(timezone.utc) - timedelta(hours=30)).isoformat()
    db.add_evidence([_row(model_key="k1", fetched_at=old), _row(model_key="k1"),
                     _row(model_key="k1", source="ebay_sold", event_date="2026-10-21T00:00:00Z"),
                     _row(model_key="k2")])
    since = datetime.now(timezone.utc) - timedelta(hours=24)
    assert len(db.recent_evidence("k1", "ebay_active", since)) == 1
    sold = db.recent_evidence("k1", "ebay_sold", since)
    assert len(sold) == 1 and sold[0]["event_date"].startswith("2026-10-21")
    assert len(db.recent_evidence("k1", "ebay_active", old)) == 2


def test_rows_need_a_model_and_a_source_and_known_columns():
    with pytest.raises(ValueError):
        db.add_evidence([{"source": "ebay_active"}])
    with pytest.raises(ValueError):
        db.add_evidence([_row(colour="red")])
    assert db.add_evidence([]) == 0
