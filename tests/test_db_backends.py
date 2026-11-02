"""db.py reads give the same answers on both backends.

A SQLite DB is built with db.py's own write functions (plus a few raw legacy
rows), loaded into this run's ResaleDB_Test_* database with the dev loader,
and every read function is compared on both backends, types included.
"""
import importlib.util
import sqlite3
from pathlib import Path

import pytest

import config
import db
from ebay import orders

_spec = importlib.util.spec_from_file_location(
    "sqlite_to_mssql", Path(__file__).parent.parent / "scripts" / "sqlite_to_mssql.py")
loader = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(loader)


@pytest.fixture
def sqlite_db(tmp_path, monkeypatch):
    path = tmp_path / "ross.db"
    monkeypatch.setattr(db, "DB_PATH", str(path))
    monkeypatch.setattr(config, "DB_BACKEND", "sqlite")
    db.create_tables()
    return path


def _fill(path):
    a = db.create_item(["data/inbox/a/1.jpg", "data/inbox/a/2.jpg"])
    db.update_field(a, "identification", {"brand": "DKNY", "model": None, "retail_price": 118,
                                          "condition": "New With Tags", "condition_flags": ["NWT"],
                                          "specifications": {"Bag Height": "12 in"}, "confidence": 0.9})
    db.update_field(a, "pricing", {"suggested_price": 84.99, "rungs_tried": ["exact"], "review_flags": [],
                                   "engine": "v2", "query_used": "dkny tote",
                                   "sold_comps": [{"price": 50.0, "title": "a", "condition": "New",
                                                   "image": "i", "url": "u"}],
                                   "judged_comps": [{"id": "s1", "total": 61.0, "verdict": "same"}],
                                   "reference": {"title": "r", "price": 60.0}})
    db.update_field(a, "listing", {"title": "DKNY Tote ", "price": 69.99, "description": "x",
                                   "item_specifics": {"Brand": "DKNY", "Features": ["Zip", "Pocket"],
                                                      "Vintage": None}})
    recs = [{"order_id": "25-1", "sold_at": "2026-08-01T10:00:00.000Z", "units": 1, "sale": 40.0,
             "shipping": 8.0, "fees": 6.1, "label": 7.5, "ad_fee": 2.0, "refund": 0.0, "net": 32.4,
             "listing_id": "117", "fees_known": True}]
    db.update_field(a, "ebay", {"sku": "sku-a", "listing_id": "117", "offer_id": "191", "quantity": 1,
                                "published_at": "2026-06-22T02:32:12.000Z", "image_urls": ["https://i/1"],
                                **orders.sale_db_fields(orders.totals_from_sales(recs))})
    db.update_field(a, "receipt", {"code": "400271128951", "reduced_price": 23.0, "photo": "data/inbox/a/t.jpg"})
    db.update_field(a, "photo_layout", {"manual": True})
    db.update_field(a, "listing_check", {"issues": [{"severity": "warn", "field": "title", "message": "m"}],
                                         "model": "claude-x", "cost_usd": 0.0012, "checked_at": db._now()})
    db.update_status(a, "sold")

    b = db.create_item([])  # bare capture
    c = db.create_item(["data/inbox/c/1.jpg"])
    db.update_field(c, "stock", {"location": "G3", "quick_price": {"price": 30.0, "basis": "comps"},
                                 "post_ready": True, "source": "inventory", "added_at": db._now()})
    db.update_status(c, "stocked")
    # a piece found at home (/inv), linked to c
    code = db.add_piece("G3", photo="data/pieces/g3-01.jpg")
    db.update_piece(code, item_id=c, info={"brand": "Coach", "ross_code": "400111111111",
                                           "brand_visible": True}, value=45.0, cost=12)
    db.add_piece("O1-WATCHES")

    db.record_listing_stats(a, "2026-07-13", impressions=10, views=2, watchers=1, price=69.99,
                            verdict="HEALTHY", total_impressions=120)
    db.record_offer_sent("117", 59.99, item_id=a, list_price=69.99, source="digest")
    plan = db.save_offer_plan([{"item_id": a, "listing_id": "117", "price": 69.99, "offer_price": 59.99,
                                "pct": 14.0, "watchers": 3, "title": "DKNY Tote"}])
    db.record_job_run("daily_sync")
    db.record_job_run("weekly_reprice", status="partial", detail="2 batches failed")
    db.record_feedback_failure("117-9", "no such transaction")
    db.mark_not_duplicate(b, a)
    db.mark_orders_seen(["25-1", "25-2"])
    db.record_llm_usage(provider="anthropic", model="claude-x", stage="price", item_id=a,
                        input_tokens=1000, output_tokens=50, cost_usd=0.004837500000000001)
    db.record_llm_usage(provider="gemini", model="g-2", input_tokens=10)

    # Legacy rows no db.py function writes any more.
    con = sqlite3.connect(path)
    with con:
        con.execute("UPDATE items SET updated_at = ? WHERE item_id = ?", ("2026-09-03T14:10:00.123456", b))
    con.close()
    return {"a": a, "b": b, "c": c, "plan": plan}


def test_every_read_matches_on_both_backends(sqlite_db, mssql, mssql_db):
    _fill(sqlite_db)
    report = loader.load(loader.read_sqlite(sqlite_db), mssql)
    results = loader.verify(sqlite_db, mssql_db, report["new_pieces"])
    assert {k: v for k, v in results.items() if v} == {}
    assert report["new_pieces"] == []  # c already had its piece from /inv


def test_stock_without_a_piece_gets_one_per_unit(sqlite_db, mssql, mssql_db):
    item = db.create_item([])
    db.update_field(item, "ebay", {"quantity": 2})
    db.update_field(item, "stock", {"location": "B7", "added_at": db._now()})
    report = loader.load(loader.read_sqlite(sqlite_db), mssql)
    assert report["new_pieces"] == ["B7-01", "B7-02"]
    results = loader.verify(sqlite_db, mssql_db, report["new_pieces"])
    assert {k: v for k, v in results.items() if v} == {}
    config.DB_BACKEND = "sqlserver"
    try:
        assert db.get_item(item)["stock"]["location"] == "B7"
        assert [p["code"] for p in db.list_pieces(item_id=item)] == ["B7-01", "B7-02"]
    finally:
        config.DB_BACKEND = "sqlite"


def test_no_tokens_are_copied(sqlite_db, mssql):
    con = sqlite3.connect(sqlite_db)
    with con:
        con.execute("INSERT INTO ebay_tokens (id, access_token, refresh_token, expires_at) "
                    "VALUES (1, 'secret', 'secret', 0)")
    con.close()
    loader.load(loader.read_sqlite(sqlite_db), mssql)
    cur = mssql.cursor()
    cur.execute("SELECT COUNT(*) FROM dbo.ebay_tokens")
    assert cur.fetchone()[0] == 0


def test_loader_only_accepts_dev_and_test_targets():
    for name in ("ResaleDB", "ReceiptsDB", "master"):
        with pytest.raises(RuntimeError):
            loader.main(["--target", name, "--source", "nowhere.db"])


def test_reads_route_by_backend(monkeypatch, sqlite_db, mssql, mssql_db):
    item = db.create_item(["data/inbox/z/1.jpg"])
    loader.load(loader.read_sqlite(sqlite_db), mssql)
    con = sqlite3.connect(sqlite_db)
    with con:  # change SQLite only: the SQL Server copy keeps the old status
        con.execute("UPDATE items SET status = 'rejected' WHERE item_id = ?", (item,))
    con.close()
    assert db.get_item(item)["status"] == "rejected"
    # verify() must see it, or "identical" would mean nothing.
    results = loader.verify(sqlite_db, mssql_db)
    assert any("status: 'rejected' != 'captured'" in d for d in results["get_item (every item)"])
    monkeypatch.setattr(config, "DB_BACKEND", "sqlserver")
    assert db.get_item(item)["status"] == "captured"


def test_unknown_backend_is_an_error(monkeypatch):
    monkeypatch.setattr(config, "DB_BACKEND", "postgres")
    with pytest.raises(ValueError, match="DB_BACKEND"):
        db.get_item("x")


def test_sqlite_is_the_default():
    # Production behaves exactly as before until cutover. A fresh process, so
    # this run's own DB_BACKEND doesn't matter.
    import os
    import subprocess
    import sys
    env = {k: v for k, v in os.environ.items() if k != "DB_BACKEND"}
    env["ROSS_SKIP_DOTENV"] = "1"
    out = subprocess.run([sys.executable, "-c", "import config; print(config.DB_BACKEND)"],
                         cwd=Path(__file__).parent.parent, env=env, capture_output=True, text=True)
    assert out.stdout.strip() == "sqlite", out.stderr
