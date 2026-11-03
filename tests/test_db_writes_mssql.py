"""db.py writes on SQL Server: merges, races, retries, consistency.
Runs against this run's ResaleDB_Test_* database (see conftest.py)."""
import contextvars
import threading

import pyodbc
import pytest

import config
import db
import db_mssql
import sqlserver


@pytest.fixture
def ms(mssql, monkeypatch):
    monkeypatch.setattr(config, "DB_BACKEND", "sqlserver")
    db_mssql._forget()
    return mssql


def _published(**ebay):
    item = db.create_item(["data/inbox/a/1.jpg"])
    # sku and listing_id are unique on SQL Server (UX_ebay_listings_*).
    db.update_field(item, "ebay", {"sku": f"s-{item[:8]}", "listing_id": item[:12], "quantity": 1, **ebay})
    return item


# --- update_field merges the caller's changes onto the current row ------------

def test_two_writers_on_different_keys_both_survive(ms):
    item = _published()
    # Two handlers (each update runs in its own task, so its own context).
    first, second = contextvars.copy_context(), contextvars.copy_context()
    a = first.run(lambda: dict(db.get_item(item)["ebay"], quantity=3))
    b = second.run(lambda: dict(db.get_item(item)["ebay"], sku="s-2"))  # read quantity=1
    first.run(db.update_field, item, "ebay", a)
    second.run(db.update_field, item, "ebay", b)
    ebay = db.get_item(item)["ebay"]
    assert ebay["quantity"] == 3 and ebay["sku"] == "s-2"  # set by the second writer


def test_two_threads_on_different_keys_both_survive(ms):
    for round_ in range(5):
        item = _published()
        db_mssql._forget()
        barrier = threading.Barrier(2)
        errors = []

        def writer(key, value):
            try:
                read = db.get_item(item)["ebay"]
                barrier.wait()
                db.update_field(item, "ebay", dict(read, **{key: value}))
            except Exception as e:  # pragma: no cover - reported below
                errors.append(e)

        threads = [threading.Thread(target=writer, args=("quantity", 4)),
                   threading.Thread(target=writer, args=("view_item_url", "https://e/117"))]
        [t.start() for t in threads]
        [t.join() for t in threads]
        assert errors == []
        ebay = db.get_item(item)["ebay"]
        assert (ebay["quantity"], ebay["view_item_url"]) == (4, "https://e/117"), round_


def test_same_key_last_writer_wins(ms):
    item = _published()
    r1, r2 = db.get_item(item), db.get_item(item)
    db.update_field(item, "ebay", dict(r1["ebay"], quantity=2))
    db.update_field(item, "ebay", dict(r2["ebay"], quantity=5))
    assert db.get_item(item)["ebay"]["quantity"] == 5


def test_a_removed_key_is_removed(ms):
    item = _published(stock_note="shelf")
    e = dict(db.get_item(item)["ebay"])
    del e["stock_note"]
    db.update_field(item, "ebay", e)
    assert "stock_note" not in db.get_item(item)["ebay"]


def test_writes_without_a_fresh_read_still_apply(ms):
    item = _published()
    e = dict(db.get_item(item)["ebay"], quantity=2)
    db.update_field(item, "ebay", e)
    e["sku"] = "s-9"                                       # same flow, no re-read
    db.update_field(item, "ebay", e)
    ebay = db.get_item(item)["ebay"]
    assert (ebay["quantity"], ebay["sku"]) == (2, "s-9")


# A value set back to one it held recently must be written (an undo, a restock).

def test_a_value_set_back_after_a_fresh_read_is_written(ms):
    item = _published(price=40.0)
    db.update_field(item, "ebay", dict(db.get_item(item)["ebay"], price=35.0))
    db.update_field(item, "ebay", dict(db.get_item(item)["ebay"], price=40.0))
    assert db.get_item(item)["ebay"]["price"] == 40.0


def test_a_value_set_back_without_a_re_read_is_written(ms):
    item = _published()
    e = dict(db.get_item(item)["ebay"], quantity=0)
    db.update_field(item, "ebay", e)
    e["quantity"] = 1                                      # restock, same flow
    db.update_field(item, "ebay", e)
    assert db.get_item(item)["ebay"]["quantity"] == 1


def test_a_value_set_back_in_separate_tasks_is_written(ms):
    item = _published(price=40.0)
    for price in (35.0, 40.0):                             # two handlers, one after the other
        contextvars.copy_context().run(
            lambda p=price: db.update_field(item, "ebay", dict(db.get_item(item)["ebay"], price=p)))
    assert db.get_item(item)["ebay"]["price"] == 40.0


def test_a_write_drops_the_stale_json_copy(ms, mssql):
    item = db.create_item([])
    cur = mssql.cursor()
    cur.execute("UPDATE dbo.items SET extra_raw = ? WHERE item_id = ?",
                '{"_item": {"updated_at": "2026-09-03T14:10:00.123456"}}', item)
    mssql.commit()
    assert db.get_item(item)["updated_at"] == "2026-09-03T14:10:00.123456"
    db.update_status(item, "rejected")
    cur.execute("SELECT extra_raw FROM dbo.items WHERE item_id = ?", item)
    assert cur.fetchone()[0] is None
    assert db.get_item(item)["updated_at"].endswith("+00:00")
    assert db_mssql.consistency_report() == []


def test_a_new_pricing_run_per_reprice_and_none_for_a_manual_price(ms, mssql):
    item = db.create_item([])
    comps = [{"price": 50.0, "title": "a", "condition": "New", "image": "i", "url": "u"}]
    db.update_field(item, "pricing", {"suggested_price": 60.0, "engine": "v2", "sold_comps": comps})
    db.update_field(item, "pricing", dict(db.get_item(item)["pricing"], manual_price=55.0))
    db.update_field(item, "pricing", dict(db.get_item(item)["pricing"],
                                          sold_comps=comps + [dict(comps[0], price=52.0)]))
    cur = mssql.cursor()
    cur.execute("SELECT COUNT(*) FROM dbo.pricing_runs WHERE item_id = ?", item)
    assert cur.fetchone()[0] == 2
    p = db.get_item(item)["pricing"]
    assert p["manual_price"] == 55.0 and [c["price"] for c in p["sold_comps"]] == [50.0, 52.0]


# --- upserts and insert-or-ignore under races --------------------------------

def _hammer(fn, n_threads=8, n_calls=15):
    errors = []
    barrier = threading.Barrier(n_threads)

    def run(i):
        try:
            barrier.wait()
            for j in range(n_calls):
                fn(i, j)
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=run, args=(i,)) for i in range(n_threads)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    return errors


def test_record_job_run_race(ms, mssql):
    errors = _hammer(lambda i, j: db.record_job_run("daily_sync", status="ok", detail=f"{i}-{j}"))
    assert errors == []
    cur = mssql.cursor()
    cur.execute("SELECT COUNT(*) FROM dbo.job_runs WHERE job = 'daily_sync'")
    assert cur.fetchone()[0] == 1
    assert db.job_run_status("daily_sync") == "ok"


def test_mark_orders_seen_race(ms):
    errors = _hammer(lambda i, j: db.mark_orders_seen([f"o-{j}", f"o-{j + 1}", "o-shared"]))
    assert errors == []
    assert db.seen_order_ids() == {f"o-{j}" for j in range(16)} | {"o-shared"}


def test_feedback_failure_upsert_keeps_first_failed(ms, mssql):
    db.record_feedback_failure("117-1", "first")
    cur = mssql.cursor()
    cur.execute("SELECT first_failed_at FROM dbo.feedback_failures")
    first = cur.fetchone()[0]
    db.record_feedback_failure("117-1", "second")
    cur.execute("SELECT first_failed_at, last_error FROM dbo.feedback_failures")
    assert tuple(cur.fetchone()) == (first, "second")


def test_mark_not_duplicate_twice_is_fine(ms):
    db.mark_not_duplicate("B-item", "a-item")
    db.mark_not_duplicate("a-item", "B-item")
    assert db.not_duplicate_pairs() == {("B-item", "a-item")}  # Python's sorted() order


def test_add_piece_race_gives_unique_codes(ms):
    codes, lock = [], threading.Lock()

    def add(i, j):
        code = db.add_piece("g3")
        with lock:
            codes.append(code)

    errors = _hammer(add, n_threads=4, n_calls=4)
    assert errors == []
    assert len(set(codes)) == 16 and all(c.startswith("G3-") for c in codes)


def test_claim_offer_plan_once(ms):
    plan = db.save_offer_plan([{"item_id": "x", "listing_id": "117", "price": 69.99,
                                "offer_price": 59.99, "pct": 14.0, "watchers": 3, "title": "t",
                                "note": None}])
    assert db.get_offer_plan(plan)["rows"][0]["note"] is None   # exact round trip
    results = []
    _hammer(lambda i, j: results.append(db.claim_offer_plan(plan)), n_threads=6, n_calls=1)
    assert results.count(True) == 1


# --- plumbing: deadlock retry, connections closed -----------------------------

def test_a_deadlock_is_retried_once(ms):
    calls = []

    def flaky(cur):
        calls.append(1)
        if len(calls) == 1:
            raise pyodbc.Error("40001", "[40001] Transaction was deadlocked (1205)")
        return "ok"

    assert db_mssql._write(flaky) == "ok" and len(calls) == 2

    def always(cur):
        raise pyodbc.Error("40001", "[40001] deadlocked (1205)")

    with pytest.raises(pyodbc.Error):
        db_mssql._write(always)


def test_connections_always_close(ms, monkeypatch):
    opened, closed = [], []
    real = sqlserver.connect

    class Tracked:
        def __init__(self, con):
            self._con = con
            opened.append(self)

        def close(self):
            closed.append(self)
            self._con.close()

        def __getattr__(self, name):
            return getattr(self._con, name)

    monkeypatch.setattr(sqlserver, "connect", lambda *a, **k: Tracked(real(*a, **k)))
    item = _published()
    db.get_item(item)
    db.record_job_run("j")
    db.mark_orders_seen(["a", "a"])
    with pytest.raises(ValueError):
        db.update_status(item, "bogus")
    with pytest.raises(pyodbc.Error):
        db_mssql._write(lambda cur: cur.execute("SELECT * FROM dbo.no_such_table"))
    assert opened and len(opened) == len(closed)


# --- decisions -----------------------------------------------------------------

def test_a_sold_item_cannot_be_deleted(ms):
    item = db.create_item([])
    recs = [{"order_id": "25-1", "sold_at": "2026-08-01T10:00:00.000Z", "units": 1, "sale": 40.0,
             "shipping": 8.0, "fees": 6.1, "label": 7.5, "ad_fee": 2.0, "refund": 0.0, "net": 32.4,
             "listing_id": "117", "fees_known": True}]
    db.update_field(item, "ebay", {"sales": recs})
    with pytest.raises(ValueError, match="sales"):
        db.delete_item(item)
    assert db.get_item(item) is not None
    other = db.create_item([])
    db.delete_item(other)
    assert db.get_item(other) is None


@pytest.mark.allow_inconsistent
def test_consistency_check_finds_a_column_that_disagrees(ms, mssql):
    item = db.create_item([])
    db.update_field(item, "identification", {"brand": "DKNY", "retail_price": 118})
    assert db_mssql.consistency_report() == []
    cur = mssql.cursor()
    cur.execute("UPDATE dbo.item_identification SET retail_price = 99 WHERE item_id = ?", item)
    mssql.commit()
    report = db_mssql.consistency_report()
    assert len(report) == 1 and "retail_price" in report[0]


def test_two_asyncio_tasks_on_different_keys_both_survive(ms):
    # The bot runs each Telegram update and job as its own asyncio task.
    import asyncio
    item = _published()

    async def handler(key, value, read_done, go):
        read = db.get_item(item)["ebay"]
        read_done.set()
        await go.wait()
        db.update_field(item, "ebay", dict(read, **{key: value}))

    async def main():
        r1, r2, go = asyncio.Event(), asyncio.Event(), asyncio.Event()
        t1 = asyncio.create_task(handler("quantity", 4, r1, go))
        t2 = asyncio.create_task(handler("sku", "s-7", r2, go))
        await r1.wait(); await r2.wait()                  # both read before either writes
        go.set()
        await asyncio.gather(t1, t2)

    asyncio.run(main())
    ebay = db.get_item(item)["ebay"]
    assert (ebay["quantity"], ebay["sku"]) == (4, "s-7")
