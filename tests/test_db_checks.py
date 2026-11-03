"""db.py: the checks table (saved /check pre-buy checks). Runs on both
backends: the SQLite run and the DB_BACKEND=sqlserver run see the same dicts."""
from datetime import datetime, timedelta, timezone

import pytest

import db


def test_add_get_update_and_status():
    cid = db.add_check(user_id=7, ross_sku="400289769542", ross_price=14.99,
                       photos=["data/checks/1/a.jpg", "data/checks/1/tag.jpg"],
                       identification={"brand": "Michael Kors", "model": "MKJ3969710"},
                       mode="quick", exact=True, verdict="NO_DATA")
    c = db.get_check(cid)
    assert c["check_id"] == cid and c["status"] == "checked" and c["status_at"] is None
    assert c["photos"] == ["data/checks/1/a.jpg", "data/checks/1/tag.jpg"]
    assert c["identification"]["model"] == "MKJ3969710" and c["exact"] is True
    assert c["ross_price"] == 14.99 and isinstance(c["ross_price"], float)
    assert datetime.fromisoformat(c["created_at"]).tzinfo is not None

    db.update_check(cid, mode="full", verdict="BUY", suggested_price=44.99, profit=16.4, roi=1.02,
                    break_even=19.71, cheapest_identical=50.0, sold_count=3,
                    comps_summary={"basis": "same-item sales", "comps": [{"title": "t", "total": 52.0}]})
    db.set_check_status(cid, "bought")
    c = db.get_check(cid)
    assert (c["mode"], c["verdict"], c["profit"], c["sold_count"], c["status"]) == \
        ("full", "BUY", 16.4, 3, "bought")
    assert c["comps_summary"]["comps"][0]["total"] == 52.0 and c["status_at"]
    assert db.get_check(cid + 999) is None


def test_list_filters_newest_first():
    a = db.add_check(user_id=1, ross_sku="400000000001", ross_price=9.99)
    b = db.add_check(user_id=2, ross_sku="400000000002", ross_price=9.99)
    c = db.add_check(user_id=1, ross_sku="400000000001", ross_price=12.99)
    db.set_check_status(c, "bought")
    assert [x["check_id"] for x in db.list_checks()][:3] == [c, b, a]
    assert [x["check_id"] for x in db.list_checks(user_id=1)] == [c, a]
    assert [x["check_id"] for x in db.list_checks(sku="400000000001", status="checked")] == [a]
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    assert db.list_checks(since=future) == []
    assert len(db.list_checks(limit=2)) == 2


def test_bad_fields_are_refused():
    with pytest.raises(ValueError):
        db.add_check(user_id=1, color="red")
    cid = db.add_check(user_id=1)
    with pytest.raises(ValueError):
        db.set_check_status(cid, "maybe")


def test_a_check_is_not_an_item():
    before = len(db.list_items())
    db.add_check(user_id=1, ross_price=5.0)
    assert len(db.list_items()) == before
