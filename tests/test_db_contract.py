"""Characterisation tests for db.py's public surface, on a fresh temp database.

These pin what the code does today so a later move into storage/ repositories
or numbered migrations can't change behaviour unnoticed. Where today's
behaviour looks wrong it is asserted anyway, with a "NOTE: looks like a bug".
"""
import json
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import config
import db


@pytest.fixture
def conn_db(tmp_path):
    old = db.DB_PATH
    db.DB_PATH = str(tmp_path / "ross.db")
    db.create_tables()
    yield db
    db.DB_PATH = old


def raw(sql, args=()):
    """Query the temp DB directly, bypassing db.py's decoding."""
    con = sqlite3.connect(db.DB_PATH)
    try:
        return con.execute(sql, args).fetchall()
    finally:
        con.close()


def raw_exec(sql, args=()):
    con = sqlite3.connect(db.DB_PATH)
    try:
        with con:
            con.execute(sql, args)
    finally:
        con.close()


def inside(*parts):
    return str(config.BASE_DIR.joinpath(*parts))


def rel(p):
    return str(Path(p))


# --- schema bootstrap ----------------------------------------------------------

EXPECTED_TABLES = {"items", "listing_stats", "offer_sends", "offer_plans", "job_runs",
                   "feedback_failures", "not_duplicates", "pieces", "seen_orders",
                   "llm_usage"}


def cols(table):
    return [r[1] for r in raw(f"PRAGMA table_info({table})")]


def test_create_tables_makes_all_tables(conn_db):
    names = {r[0] for r in raw("SELECT name FROM sqlite_master WHERE type='table'")}
    assert EXPECTED_TABLES <= names


def test_create_tables_idempotent_and_keeps_data(conn_db):
    iid = db.create_item(["a.jpg"])
    before = cols("items")
    db.create_tables()
    db.create_tables()
    assert cols("items") == before
    assert db.get_item(iid) is not None


def test_items_has_all_expected_columns(conn_db):
    c = set(cols("items"))
    assert set(db.VALID_FIELDS) <= c
    assert {"item_id", "status", "created_at", "updated_at"} <= c


def test_listing_stats_has_migrated_columns(conn_db):
    assert {"total_impressions", "search_views"} <= set(cols("listing_stats"))


def test_bootstrap_adds_missing_columns_to_old_schema(tmp_path):
    """A DB created before the ALTER TABLE migrations gains the new columns and
    keeps its rows."""
    old = db.DB_PATH
    db.DB_PATH = str(tmp_path / "old.db")
    try:
        con = sqlite3.connect(db.DB_PATH)
        with con:
            con.execute("""CREATE TABLE items (item_id TEXT PRIMARY KEY,
                status TEXT NOT NULL DEFAULT 'captured', photos TEXT, processed TEXT,
                identification TEXT, pricing TEXT, listing TEXT, ebay TEXT,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")
            con.execute("""CREATE TABLE listing_stats (item_id TEXT NOT NULL,
                week TEXT NOT NULL, impressions INTEGER, views INTEGER, watchers INTEGER,
                price REAL, verdict TEXT, suggested_price REAL, created_at TEXT NOT NULL,
                PRIMARY KEY (item_id, week))""")
            con.execute("INSERT INTO items (item_id, photos, created_at, updated_at) "
                        "VALUES ('x', '[\"p.jpg\"]', 't', 't')")
        con.close()
        db.create_tables()
        db.create_tables()
        assert {"price_source_url", "receipt", "photo_layout", "listing_check",
                "stock"} <= set(cols("items"))
        assert {"total_impressions", "search_views"} <= set(cols("listing_stats"))
        got = db.get_item("x")
        assert got["photos"] == [str(config.BASE_DIR / "p.jpg")]
        assert got["receipt"] is None
    finally:
        db.DB_PATH = old


def test_conn_creates_missing_parent_dir_and_uses_wal(tmp_path):
    old = db.DB_PATH
    db.DB_PATH = str(tmp_path / "nested" / "dir" / "x.db")
    try:
        db.create_tables()
        assert (tmp_path / "nested" / "dir" / "x.db").exists()
        assert raw("PRAGMA journal_mode")[0][0] == "wal"
    finally:
        db.DB_PATH = old


def test_constants():
    assert db.VALID_STATUSES == [
        "captured", "identified", "priced", "drafted", "review", "approved",
        "ebay_draft", "published", "sold", "rejected", "stocked"]
    assert db.VALID_FIELDS == [
        "identification", "pricing", "listing", "ebay", "photos", "processed",
        "price_source_url", "receipt", "photo_layout", "listing_check", "stock"]
    assert db.PIECE_STATUSES == ("home", "sold", "lot", "donated")


# --- item lifecycle -------------------------------------------------------------

def test_create_item_shape(conn_db):
    iid = db.create_item(["a.jpg", "b.jpg"])
    assert isinstance(iid, str) and len(iid) == 36
    item = db.get_item(iid)
    assert item["item_id"] == iid
    assert item["status"] == "captured"
    assert item["photos"] == [str(config.BASE_DIR / "a.jpg"), str(config.BASE_DIR / "b.jpg")]
    for k in ("processed", "identification", "pricing", "listing", "ebay",
              "price_source_url", "receipt", "photo_layout", "listing_check", "stock"):
        assert item[k] is None
    assert item["created_at"] == item["updated_at"]
    assert datetime.fromisoformat(item["created_at"]).tzinfo is not None


def test_create_item_ids_unique(conn_db):
    assert db.create_item([]) != db.create_item([])


def test_get_item_missing_returns_none(conn_db):
    assert db.get_item("nope") is None


def test_get_item_has_exactly_the_table_columns(conn_db):
    item = db.get_item(db.create_item([]))
    assert list(item) == cols("items")


@pytest.mark.parametrize("field", [
    "identification", "pricing", "listing", "ebay", "processed", "price_source_url",
    "receipt", "photo_layout", "listing_check", "stock"])
def test_update_field_json_roundtrip(conn_db, field):
    iid = db.create_item([])
    data = {"a": 1, "nested": {"b": [1, 2, "x"]}, "u": "café", "n": None}
    db.update_field(iid, field, data)
    assert db.get_item(iid)[field] == data
    stored = raw(f"SELECT {field} FROM items WHERE item_id=?", (iid,))[0][0]
    assert isinstance(stored, str) and json.loads(stored) == data


def test_update_field_photos_list(conn_db):
    iid = db.create_item(["a.jpg"])
    db.update_field(iid, "photos", ["x.jpg", "y.jpg"])
    assert db.get_item(iid)["photos"] == [str(config.BASE_DIR / "x.jpg"),
                                          str(config.BASE_DIR / "y.jpg")]


def test_update_field_accepts_non_dict_json(conn_db):
    iid = db.create_item([])
    db.update_field(iid, "processed", ["a", "b"])
    assert db.get_item(iid)["processed"] == ["a", "b"]
    db.update_field(iid, "price_source_url", "https://x.example/1")
    assert db.get_item(iid)["price_source_url"] == "https://x.example/1"


def test_update_field_none_reads_back_none(conn_db):
    iid = db.create_item([])
    db.update_field(iid, "pricing", {"a": 1})
    db.update_field(iid, "pricing", None)
    # stored as the JSON text "null", which decodes to None
    assert raw("SELECT pricing FROM items WHERE item_id=?", (iid,))[0][0] == "null"
    assert db.get_item(iid)["pricing"] is None


def test_update_field_overwrites_whole_value(conn_db):
    iid = db.create_item([])
    db.update_field(iid, "pricing", {"a": 1, "b": 2})
    db.update_field(iid, "pricing", {"c": 3})
    assert db.get_item(iid)["pricing"] == {"c": 3}


def test_update_field_invalid_field_raises(conn_db):
    iid = db.create_item([])
    for bad in ("status", "item_id", "bogus", "created_at", "pricing; DROP TABLE items"):
        with pytest.raises(ValueError, match="Invalid field"):
            db.update_field(iid, bad, {})
    assert db.get_item(iid) is not None


def test_update_field_touches_updated_at_only(conn_db):
    iid = db.create_item([])
    raw_exec("UPDATE items SET updated_at='2000-01-01T00:00:00+00:00' WHERE item_id=?", (iid,))
    created = db.get_item(iid)["created_at"]
    db.update_field(iid, "pricing", {"a": 1})
    item = db.get_item(iid)
    assert item["updated_at"] > "2000"
    assert item["created_at"] == created
    assert item["status"] == "captured"


def test_update_field_missing_item_is_silent_noop(conn_db):
    db.update_field("nope", "pricing", {"a": 1})
    assert db.get_item("nope") is None
    assert raw("SELECT COUNT(*) FROM items")[0][0] == 0


@pytest.mark.parametrize("status", db.VALID_STATUSES)
def test_update_status_valid(conn_db, status):
    iid = db.create_item([])
    raw_exec("UPDATE items SET updated_at='2000-01-01T00:00:00+00:00' WHERE item_id=?", (iid,))
    db.update_status(iid, status)
    item = db.get_item(iid)
    assert item["status"] == status
    assert item["updated_at"] > "2000"


@pytest.mark.parametrize("bad", ["", "Captured", "PUBLISHED", "listed", "home", None])
def test_update_status_invalid_raises_and_leaves_row(conn_db, bad):
    iid = db.create_item([])
    with pytest.raises(ValueError, match="Invalid status"):
        db.update_status(iid, bad)
    assert db.get_item(iid)["status"] == "captured"


def test_update_status_missing_item_is_silent_noop(conn_db):
    db.update_status("nope", "sold")
    assert db.get_item("nope") is None


def test_list_items_all_and_filter(conn_db):
    assert db.list_items() == []
    a, b, c = db.create_item(["a"]), db.create_item(["b"]), db.create_item(["c"])
    db.update_status(b, "published")
    db.update_status(c, "published")
    assert {i["item_id"] for i in db.list_items()} == {a, b, c}
    assert {i["item_id"] for i in db.list_items("published")} == {b, c}
    assert [i["item_id"] for i in db.list_items("captured")] == [a]
    assert db.list_items("sold") == []
    # unknown status is not validated on read: just no matches
    assert db.list_items("bogus") == []
    assert db.list_items("") == []   # '' is not None, so it filters


def test_list_items_decodes_json_and_paths(conn_db):
    iid = db.create_item(["a.jpg"])
    db.update_field(iid, "pricing", {"price": 9.5})
    [item] = db.list_items()
    assert item["pricing"] == {"price": 9.5}
    assert item["photos"] == [str(config.BASE_DIR / "a.jpg")]


def test_list_items_order_is_insertion_order_in_practice(conn_db):
    # NOTE: list_items has no ORDER BY; this only pins SQLite's rowid scan order
    # (item_id is TEXT PRIMARY KEY, so the table is still rowid-ordered).
    ids = [db.create_item([]) for _ in range(5)]
    assert [i["item_id"] for i in db.list_items()] == ids


def test_delete_item(conn_db):
    a, b = db.create_item([]), db.create_item([])
    db.delete_item(a)
    assert db.get_item(a) is None
    assert db.get_item(b) is not None
    db.delete_item(a)       # already gone: no error
    db.delete_item("nope")


def test_delete_item_does_not_cascade(conn_db):
    iid = db.create_item([])
    db.record_listing_stats(iid, "2026-10-26", views=1)
    code = db.add_piece("G3")
    db.update_piece(code, item_id=iid)
    db.delete_item(iid)
    assert db.latest_stats(iid) is not None
    assert db.get_piece(code)["item_id"] == iid


# --- photo path handling --------------------------------------------------------

def test_photos_inside_base_dir_stored_relative_returned_absolute(conn_db):
    p = inside("data", "inbox", "abc", "1.jpg")
    iid = db.create_item([p])
    stored = json.loads(raw("SELECT photos FROM items WHERE item_id=?", (iid,))[0][0])
    assert stored == [rel("data/inbox/abc/1.jpg")]
    assert db.get_item(iid)["photos"] == [p]


def test_photos_outside_base_dir_stored_unchanged(conn_db, tmp_path):
    p = str(tmp_path / "elsewhere.jpg")
    iid = db.create_item([p])
    stored = json.loads(raw("SELECT photos FROM items WHERE item_id=?", (iid,))[0][0])
    assert stored == [p]
    assert db.get_item(iid)["photos"] == [p]


def test_photos_mixed_and_update_field(conn_db, tmp_path):
    inner, outer = inside("data", "x.jpg"), str(tmp_path / "y.jpg")
    iid = db.create_item([])
    db.update_field(iid, "photos", [inner, outer])
    stored = json.loads(raw("SELECT photos FROM items WHERE item_id=?", (iid,))[0][0])
    assert stored == [rel("data/x.jpg"), outer]
    assert db.get_item(iid)["photos"] == [inner, outer]


def test_photos_empty_and_non_string_entries_untouched(conn_db):
    iid = db.create_item([])
    assert db.get_item(iid)["photos"] == []
    db.update_field(iid, "photos", ["", None, 5])
    assert db.get_item(iid)["photos"] == ["", None, 5]


def test_existing_absolute_rows_read_back_correctly(conn_db, tmp_path):
    inner, outer = inside("data", "inbox", "z", "1.jpg"), str(tmp_path / "o.jpg")
    raw_exec("INSERT INTO items (item_id, photos, receipt, stock, created_at, updated_at) "
             "VALUES ('abs', ?, ?, ?, 't', 't')",
             (json.dumps([inner, outer]),
              json.dumps({"photo": inner, "cost": 3}),
              json.dumps({"inventory_photo": outer, "location": "G3"})))
    item = db.get_item("abs")
    assert item["photos"] == [inner, outer]
    assert item["receipt"] == {"photo": inner, "cost": 3}
    assert item["stock"] == {"inventory_photo": outer, "location": "G3"}


def test_existing_relative_rows_read_back_absolute(conn_db):
    raw_exec("INSERT INTO items (item_id, photos, created_at, updated_at) "
             "VALUES ('rel', ?, 't', 't')", (json.dumps(["data/inbox/r/1.jpg"]),))
    assert db.get_item("rel")["photos"] == [inside("data", "inbox", "r", "1.jpg")]


def test_receipt_photo_path_mapping(conn_db):
    p = inside("data", "inbox", "i", "receipt.jpg")
    iid = db.create_item([])
    db.update_field(iid, "receipt", {"photo": p, "cost": 4.5, "code": "123"})
    stored = json.loads(raw("SELECT receipt FROM items WHERE item_id=?", (iid,))[0][0])
    assert stored == {"photo": rel("data/inbox/i/receipt.jpg"), "cost": 4.5, "code": "123"}
    assert db.get_item(iid)["receipt"] == {"photo": p, "cost": 4.5, "code": "123"}


def test_receipt_photo_outside_base_dir_unchanged(conn_db, tmp_path):
    p = str(tmp_path / "r.jpg")
    iid = db.create_item([])
    db.update_field(iid, "receipt", {"photo": p})
    assert json.loads(raw("SELECT receipt FROM items")[0][0]) == {"photo": p}
    assert db.get_item(iid)["receipt"] == {"photo": p}


def test_stock_inventory_photo_path_mapping(conn_db):
    p = inside("data", "inbox", "i", "inv.jpg")
    iid = db.create_item([])
    db.update_field(iid, "stock", {"location": "G3", "inventory_photo": p})
    stored = json.loads(raw("SELECT stock FROM items")[0][0])
    assert stored == {"location": "G3", "inventory_photo": rel("data/inbox/i/inv.jpg")}
    assert db.get_item(iid)["stock"] == {"location": "G3", "inventory_photo": p}


def test_path_mapping_leaves_missing_empty_or_other_keys(conn_db):
    iid = db.create_item([])
    db.update_field(iid, "receipt", {"cost": 1})
    assert db.get_item(iid)["receipt"] == {"cost": 1}
    db.update_field(iid, "receipt", {"photo": ""})
    assert db.get_item(iid)["receipt"] == {"photo": ""}
    db.update_field(iid, "receipt", {"photo": None})
    assert db.get_item(iid)["receipt"] == {"photo": None}
    # a photo key under a non-path field is not touched
    p = inside("data", "a.jpg")
    db.update_field(iid, "pricing", {"photo": p})
    assert json.loads(raw("SELECT pricing FROM items")[0][0]) == {"photo": p}
    # nor is a stock that isn't a dict
    db.update_field(iid, "stock", ["x"])
    assert db.get_item(iid)["stock"] == ["x"]


def test_path_mapping_does_not_mutate_callers_data(conn_db):
    p = inside("data", "a.jpg")
    photos, rec = [p], {"photo": p}
    iid = db.create_item(photos)
    db.update_field(iid, "receipt", rec)
    assert photos == [p] and rec == {"photo": p}


def test_piece_photo_path_mapping(conn_db, tmp_path):
    p = inside("data", "inbox", "pieces", "1.jpg")
    code = db.add_piece("G3", photo=p)
    assert raw("SELECT photo FROM pieces WHERE code=?", (code,))[0][0] == \
        rel("data/inbox/pieces/1.jpg")
    assert db.get_piece(code)["photo"] == p
    out = str(tmp_path / "o.jpg")
    db.update_piece(code, photo=out)
    assert raw("SELECT photo FROM pieces")[0][0] == out
    assert db.get_piece(code)["photo"] == out
    db.update_piece(code, photo=p)
    assert db.list_pieces()[0]["photo"] == p


def test_piece_photo_absolute_legacy_row_and_none(conn_db):
    p = inside("data", "inbox", "legacy.jpg")
    raw_exec("INSERT INTO pieces (code, box, status, photo, info, created_at, updated_at) "
             "VALUES ('G9-01', 'G9', 'home', ?, '{}', 't', 't')", (p,))
    assert db.get_piece("G9-01")["photo"] == p
    code = db.add_piece("G3")
    assert db.get_piece(code)["photo"] is None


# --- pieces ---------------------------------------------------------------------

def test_next_piece_code_and_add_piece_sequence(conn_db):
    assert db.next_piece_code("G3") == "G3-01"
    assert db.add_piece("G3") == "G3-01"
    assert db.add_piece("G3") == "G3-02"
    assert db.add_piece("B1") == "B1-01"
    assert db.next_piece_code("G3") == "G3-03"
    assert db.next_piece_code("ZZ") == "ZZ-01"


def test_add_piece_defaults(conn_db):
    code = db.add_piece("G3")
    p = db.get_piece(code)
    assert p["code"] == code and p["box"] == "G3"
    assert p["status"] == "home"
    assert p["info"] == {}
    assert p["photo"] is None and p["item_id"] is None
    assert p["value"] is None and p["cost"] is None
    assert p["sold_at"] is None and p["order_id"] is None
    assert p["created_at"] == p["updated_at"] or p["created_at"] < p["updated_at"]


def test_piece_numbering_follows_code_prefix_not_current_box(conn_db):
    a = db.add_piece("G3")
    db.update_piece(a, box="B1")           # moved; keeps its number
    assert db.add_piece("G3") == "G3-02"
    assert db.get_piece("G3-01")["box"] == "B1"


def test_piece_numbering_after_gap_uses_max(conn_db):
    for _ in range(3):
        db.add_piece("G3")
    raw_exec("DELETE FROM pieces WHERE code='G3-02'")
    assert db.next_piece_code("G3") == "G3-04"
    raw_exec("DELETE FROM pieces WHERE code='G3-03'")
    assert db.next_piece_code("G3") == "G3-02"


def test_piece_numbering_ignores_non_numeric_suffix(conn_db):
    raw_exec("INSERT INTO pieces (code, box, status, info, created_at, updated_at) "
             "VALUES ('G3-X', 'G3', 'home', '{}', 't', 't')")
    assert db.next_piece_code("G3") == "G3-01"


def test_piece_codes_unique_primary_key(conn_db):
    code = db.add_piece("G3")
    with pytest.raises(sqlite3.IntegrityError):
        raw_exec("INSERT INTO pieces (code, box, status, info, created_at, updated_at) "
                 "VALUES (?, 'G3', 'home', '{}', 't', 't')", (code,))


def test_add_piece_retries_on_code_collision(conn_db, monkeypatch):
    db.add_piece("G3")
    seq = iter(["G3-01", "G3-01", "G3-05"])
    monkeypatch.setattr(db, "next_piece_code", lambda box: next(seq))
    assert db.add_piece("G3") == "G3-05"


def test_add_piece_gives_up_after_five_collisions(conn_db, monkeypatch):
    db.add_piece("G3")
    monkeypatch.setattr(db, "next_piece_code", lambda box: "G3-01")
    with pytest.raises(RuntimeError, match="couldn't reserve"):
        db.add_piece("G3")


def test_piece_code_widens_past_99(conn_db):
    raw_exec("INSERT INTO pieces (code, box, status, info, created_at, updated_at) "
             "VALUES ('G3-99', 'G3', 'home', '{}', 't', 't')")
    assert db.next_piece_code("G3") == "G3-100"


def test_list_pieces_sorts_codes_numerically(conn_db):
    # Once a box passes 99 pieces, "G3-100" must still sort after "G3-99".
    for code in ("G3-99", "G3-100", "G3-11"):
        raw_exec("INSERT INTO pieces (code, box, status, info, created_at, updated_at) "
                 "VALUES (?, 'G3', 'home', '{}', 't', 't')", (code,))
    assert [p["code"] for p in db.list_pieces()] == ["G3-11", "G3-99", "G3-100"]


def test_get_piece_missing_and_case_insensitive(conn_db):
    assert db.get_piece("G3-01") is None
    db.add_piece("G3")
    assert db.get_piece("g3-01")["code"] == "G3-01"


def test_lowercase_box_is_normalised_to_upper(conn_db):
    code = db.add_piece("g3")
    assert code == "G3-01"
    assert db.get_piece(code)["box"] == "G3"
    assert db.next_piece_code("g3") == "G3-02"
    assert [p["code"] for p in db.list_pieces(box="g3")] == ["G3-01"]


def test_update_piece_fields(conn_db):
    code = db.add_piece("G3")
    raw_exec("UPDATE pieces SET updated_at='2000-01-01T00:00:00+00:00'")
    iid = db.create_item([])
    db.update_piece(code, item_id=iid, value=12.5, cost=3, order_id="O1",
                    sold_at="2026-10-01T00:00:00+00:00", status="sold",
                    info={"brand": "Coach", "tags": ["a"]})
    p = db.get_piece(code)
    assert p["item_id"] == iid and p["value"] == 12.5 and p["cost"] == 3
    assert p["order_id"] == "O1" and p["status"] == "sold"
    assert p["sold_at"] == "2026-10-01T00:00:00+00:00"
    assert p["info"] == {"brand": "Coach", "tags": ["a"]}
    assert p["updated_at"] > "2000"
    assert p["created_at"] != p["updated_at"]
    assert raw("SELECT info FROM pieces")[0][0] == json.dumps(p["info"])


def test_update_piece_empty_info_reads_as_dict(conn_db):
    code = db.add_piece("G3")
    db.update_piece(code, info={})
    assert db.get_piece(code)["info"] == {}
    raw_exec("UPDATE pieces SET info=NULL")
    assert db.get_piece(code)["info"] == {}


@pytest.mark.parametrize("status", db.PIECE_STATUSES)
def test_update_piece_valid_status(conn_db, status):
    code = db.add_piece("G3")
    db.update_piece(code, status=status)
    assert db.get_piece(code)["status"] == status


@pytest.mark.parametrize("bad", ["listed", "Home", "", None, "published"])
def test_update_piece_invalid_status(conn_db, bad):
    code = db.add_piece("G3")
    with pytest.raises(ValueError, match="bad piece status"):
        db.update_piece(code, status=bad)
    assert db.get_piece(code)["status"] == "home"


def test_update_piece_unknown_field(conn_db):
    code = db.add_piece("G3")
    for bad in ({"created_at": "x"}, {"updated_at": "x"}, {"nope": 1}):
        with pytest.raises(ValueError, match="unknown piece field"):
            db.update_piece(code, **bad)
    assert db.get_piece(code) is not None


def test_update_piece_validates_before_any_write(conn_db):
    code = db.add_piece("G3")
    with pytest.raises(ValueError):
        db.update_piece(code, value=9, status="bogus")
    assert db.get_piece(code)["value"] is None


def test_update_piece_missing_code_is_silent_noop(conn_db):
    db.update_piece("G3-99", value=1)
    assert db.get_piece("G3-99") is None


def test_update_piece_code_lookup_is_case_insensitive(conn_db):
    code = db.add_piece("G3")
    db.update_piece(code.lower(), value=5)
    assert db.get_piece(code)["value"] == 5


def test_update_piece_with_no_fields_is_noop(conn_db):
    code = db.add_piece("G3")
    before = db.get_piece(code)
    db.update_piece(code)
    assert db.get_piece(code) == before


def test_update_piece_none_photo_clears_it(conn_db):
    code = db.add_piece("G3", photo=inside("data", "a.jpg"))
    db.update_piece(code, photo=None)
    assert db.get_piece(code)["photo"] is None


def test_list_pieces_filters_and_order(conn_db):
    a, b, c, d = db.add_piece("G3"), db.add_piece("G3"), db.add_piece("B1"), db.add_piece("A2")
    iid = db.create_item([])
    db.update_piece(b, status="sold", item_id=iid)
    db.update_piece(c, item_id=iid)
    assert [p["code"] for p in db.list_pieces()] == [d, c, a, b]   # ORDER BY code
    assert [p["code"] for p in db.list_pieces(box="G3")] == [a, b]
    assert [p["code"] for p in db.list_pieces(status="home")] == [d, c, a]
    assert [p["code"] for p in db.list_pieces(item_id=iid)] == [c, b]
    assert [p["code"] for p in db.list_pieces(box="G3", status="sold")] == [b]
    assert [p["code"] for p in db.list_pieces(box="G3", item_id=iid, status="sold")] == [b]
    assert db.list_pieces(box="nope") == []
    assert db.list_pieces(status="bogus") == []   # filter value not validated


def test_list_pieces_box_follows_current_box_not_code(conn_db):
    code = db.add_piece("G3")
    db.update_piece(code, box="B1")
    assert db.list_pieces(box="G3") == []
    assert [p["code"] for p in db.list_pieces(box="B1")] == [code]


def test_list_pieces_empty(conn_db):
    assert db.list_pieces() == []


# --- listing_stats ---------------------------------------------------------------

def test_record_listing_stats_and_history(conn_db):
    db.record_listing_stats("i1", "2026-10-12", impressions=10, views=2, watchers=1,
                            price=20.0, verdict="hold", suggested_price=18.0,
                            total_impressions=100, search_views=3)
    row = db.latest_stats("i1")
    assert {k: row[k] for k in ("item_id", "week", "impressions", "views", "watchers",
                                "price", "verdict", "suggested_price",
                                "total_impressions", "search_views")} == {
        "item_id": "i1", "week": "2026-10-12", "impressions": 10, "views": 2,
        "watchers": 1, "price": 20.0, "verdict": "hold", "suggested_price": 18.0,
        "total_impressions": 100, "search_views": 3}
    assert datetime.fromisoformat(row["created_at"]).tzinfo is not None
    assert set(row) == set(cols("listing_stats"))


def test_record_listing_stats_defaults_to_none(conn_db):
    db.record_listing_stats("i1", "2026-10-12")
    row = db.latest_stats("i1")
    assert all(row[k] is None for k in ("impressions", "views", "watchers", "price",
                                        "verdict", "suggested_price",
                                        "total_impressions", "search_views"))


def test_record_listing_stats_upserts_same_week_overwriting_everything(conn_db):
    db.record_listing_stats("i1", "2026-10-12", impressions=10, views=2, verdict="a",
                            total_impressions=50)
    db.record_listing_stats("i1", "2026-10-12", views=5)
    assert len(db.stats_history("i1")) == 1
    row = db.latest_stats("i1")
    assert row["views"] == 5
    # omitted keywords overwrite with None, they are not "keep the old value"
    assert row["impressions"] is None and row["verdict"] is None
    assert row["total_impressions"] is None


def test_stats_history_order_limit_and_isolation(conn_db):
    for w in ("2026-10-05", "2026-10-19", "2026-10-12", "2026-09-28"):
        db.record_listing_stats("i1", w, views=1)
    db.record_listing_stats("i2", "2026-10-26", views=9)
    weeks = [r["week"] for r in db.stats_history("i1")]
    assert weeks == ["2026-10-19", "2026-10-12", "2026-10-05", "2026-09-28"]
    assert [r["week"] for r in db.stats_history("i1", limit=2)] == weeks[:2]
    assert db.latest_stats("i1")["week"] == "2026-10-19"
    assert db.latest_stats("i2")["week"] == "2026-10-26"


def test_stats_default_limit_is_eight(conn_db):
    for n in range(10):
        db.record_listing_stats("i1", f"2026-01-{n + 1:02d}")
    assert len(db.stats_history("i1")) == 8


def test_stats_missing_item(conn_db):
    assert db.stats_history("nope") == []
    assert db.latest_stats("nope") is None


# --- offers ----------------------------------------------------------------------

def test_record_offer_sent_and_offers_sent_since(conn_db):
    db.record_offer_sent(12345, 9.5, item_id="i1", list_price=12.0, source="digest")
    db.record_offer_sent("999", 5)
    assert raw("SELECT item_id, listing_id, list_price, offer_price, source "
               "FROM offer_sends ORDER BY id") == [
        ("i1", "12345", 12.0, 9.5, "digest"), (None, "999", None, 5, None)]
    assert db.offers_sent_since("2000-01-01") == {"12345", "999"}   # ids stringified
    assert db.offers_sent_since("9999-01-01") == set()


def test_offers_sent_since_boundary_is_inclusive_and_distinct(conn_db):
    db.record_offer_sent("L1", 1)
    db.record_offer_sent("L1", 2)
    sent = raw("SELECT sent_at FROM offer_sends ORDER BY id")[0][0]
    assert db.offers_sent_since(sent) == {"L1"}
    raw_exec("UPDATE offer_sends SET sent_at='2020-01-01T00:00:00+00:00' WHERE id=1")
    raw_exec("UPDATE offer_sends SET sent_at='2020-01-02T00:00:00+00:00' WHERE id=2")
    assert db.offers_sent_since("2020-01-02T00:00:00+00:00") == {"L1"}
    assert db.offers_sent_since("2020-01-02T00:00:01+00:00") == set()


def test_offers_sent_since_empty(conn_db):
    assert db.offers_sent_since("2000-01-01") == set()


def test_offer_plan_save_get_claim(conn_db):
    rows = [{"listing_id": "1", "offer": 4.5, "note": "café"}, {"listing_id": "2"}]
    pid = db.save_offer_plan(rows)
    assert isinstance(pid, str) and len(pid) == 36
    plan = db.get_offer_plan(pid)
    assert plan["plan_id"] == pid and plan["rows"] == rows
    assert plan["used_at"] is None
    assert set(plan) == {"plan_id", "rows", "created_at", "used_at"}
    assert datetime.fromisoformat(plan["created_at"]).tzinfo is not None


def test_claim_offer_plan_only_first_caller_wins(conn_db):
    pid = db.save_offer_plan([])
    assert db.get_offer_plan(pid)["rows"] == []
    assert db.claim_offer_plan(pid) is True
    assert db.claim_offer_plan(pid) is False
    assert db.get_offer_plan(pid)["used_at"] is not None


def test_offer_plan_missing(conn_db):
    assert db.get_offer_plan("nope") is None
    assert db.claim_offer_plan("nope") is False


def test_offer_plans_have_distinct_ids(conn_db):
    assert db.save_offer_plan([]) != db.save_offer_plan([])


# --- job runs --------------------------------------------------------------------

def test_job_run_never_run(conn_db):
    assert db.last_job_run("weekly") is None
    assert db.job_run_status("weekly") is None


def test_record_job_run_ok(conn_db):
    before = datetime.now(timezone.utc)
    db.record_job_run("weekly")
    got = db.last_job_run("weekly")
    assert got.tzinfo is not None
    assert before - timedelta(seconds=1) <= got <= datetime.now(timezone.utc)
    assert db.job_run_status("weekly") == "ok"
    assert raw("SELECT detail FROM job_runs")[0][0] is None


def test_record_job_run_non_ok_does_not_count_as_completed(conn_db):
    db.record_job_run("weekly", "partial", "2 batches failed")
    assert db.last_job_run("weekly") is None
    assert db.job_run_status("weekly") == "partial"
    assert raw("SELECT detail FROM job_runs")[0][0] == "2 batches failed"


def test_record_job_run_upserts_one_row_per_job(conn_db):
    db.record_job_run("a", "partial")
    db.record_job_run("a", "ok")
    db.record_job_run("b")
    assert raw("SELECT COUNT(*) FROM job_runs")[0][0] == 2
    assert db.job_run_status("a") == "ok"
    assert db.last_job_run("a") is not None


def test_last_job_run_legacy_null_status_counts_as_ok(conn_db):
    raw_exec("INSERT INTO job_runs (job, last_run_at, status) VALUES "
             "('legacy', '2026-10-01T10:00:00+00:00', NULL)")
    assert db.last_job_run("legacy") == datetime(2026, 10, 1, 10, tzinfo=timezone.utc)
    assert db.job_run_status("legacy") is None


def test_last_job_run_naive_timestamp_assumed_utc(conn_db):
    raw_exec("INSERT INTO job_runs (job, last_run_at, status) VALUES "
             "('n', '2026-10-01T10:00:00', 'ok')")
    assert db.last_job_run("n") == datetime(2026, 10, 1, 10, tzinfo=timezone.utc)


def test_last_job_run_garbage_timestamp_is_none(conn_db):
    raw_exec("INSERT INTO job_runs (job, last_run_at, status) VALUES "
             "('bad', 'not a date', 'ok')")
    assert db.last_job_run("bad") is None
    assert db.job_run_status("bad") == "ok"


# --- feedback failures / not-duplicates / seen orders ----------------------------

def test_feedback_failures(conn_db):
    assert db.known_feedback_failures() == set()
    db.record_feedback_failure("L1", "err1")
    db.record_feedback_failure("L2", "err2")
    assert db.known_feedback_failures() == {"L1", "L2"}
    first = raw("SELECT first_failed FROM feedback_failures WHERE order_line='L1'")[0][0]
    time.sleep(0.01)
    db.record_feedback_failure("L1", "newer")
    row = raw("SELECT first_failed, last_error FROM feedback_failures WHERE order_line='L1'")[0]
    assert row == (first, "newer")          # error updated, first_failed kept
    assert len(db.known_feedback_failures()) == 2
    db.clear_feedback_failure("L1")
    assert db.known_feedback_failures() == {"L2"}
    db.clear_feedback_failure("L1")         # absent: no error
    db.clear_feedback_failure("never")


def test_not_duplicate_pairs(conn_db):
    assert db.not_duplicate_pairs() == set()
    db.mark_not_duplicate("b", "a")
    assert db.not_duplicate_pairs() == {("a", "b")}      # stored sorted a<b
    db.mark_not_duplicate("a", "b")                      # idempotent either order
    db.mark_not_duplicate("a", "c")
    assert db.not_duplicate_pairs() == {("a", "b"), ("a", "c")}


def test_not_duplicate_with_self_is_allowed(conn_db):
    db.mark_not_duplicate("a", "a")
    assert db.not_duplicate_pairs() == {("a", "a")}


def test_seen_orders(conn_db):
    assert db.seen_order_ids() == set()
    db.mark_orders_seen(["o1", "o2"])
    db.mark_orders_seen(("o2", "o3"))          # overlap ignored, any iterable of ids
    assert db.seen_order_ids() == {"o1", "o2", "o3"}
    first = raw("SELECT seen_at FROM seen_orders WHERE order_id='o2'")[0][0]
    time.sleep(0.01)
    db.mark_orders_seen(["o2"])
    assert raw("SELECT seen_at FROM seen_orders WHERE order_id='o2'")[0][0] == first
    db.mark_orders_seen([])
    assert len(db.seen_order_ids()) == 3


# --- llm usage -------------------------------------------------------------------

def test_record_llm_usage_defaults(conn_db):
    db.record_llm_usage(provider="anthropic", model="sonnet")
    row = raw("SELECT provider, model, stage, item_id, input_tokens, output_tokens, "
              "cache_read_tokens, cache_write_tokens, web_searches, cost_usd "
              "FROM llm_usage")[0]
    assert row == ("anthropic", "sonnet", None, None, 0, 0, 0, 0, 0, None)
    assert datetime.fromisoformat(raw("SELECT ts FROM llm_usage")[0][0]).tzinfo is not None


def test_record_llm_usage_explicit_ts_and_fields(conn_db):
    db.record_llm_usage(provider="gemini", model="flash", stage="identify", item_id="i1",
                        input_tokens=10, output_tokens=5, cache_read_tokens=2,
                        cache_write_tokens=1, web_searches=3, cost_usd=0.5,
                        ts="2026-10-01T00:00:00+00:00")
    assert raw("SELECT ts, stage, item_id, input_tokens, web_searches, cost_usd "
               "FROM llm_usage")[0] == ("2026-10-01T00:00:00+00:00", "identify", "i1",
                                        10, 3, 0.5)


def test_record_llm_usage_requires_keywords(conn_db):
    with pytest.raises(TypeError):
        db.record_llm_usage("anthropic", "sonnet")


def test_llm_spend_since(conn_db):
    assert db.llm_spend_since("2000") == 0.0
    assert isinstance(db.llm_spend_since("2000"), float)
    db.record_llm_usage(provider="anthropic", model="m", cost_usd=1.0, ts="2026-10-01T00:00:00")
    db.record_llm_usage(provider="anthropic", model="m", cost_usd=2.0, ts="2026-10-15T00:00:00")
    db.record_llm_usage(provider="gemini", model="g", cost_usd=0.25, ts="2026-10-20T00:00:00")
    db.record_llm_usage(provider="gemini", model="g", cost_usd=None, ts="2026-10-21T00:00:00")
    assert db.llm_spend_since("2026-10-01T00:00:00") == 3.25      # inclusive, NULL ignored
    assert db.llm_spend_since("2026-10-02") == 2.25
    assert db.llm_spend_since("2026-10-01", "anthropic") == 3.0
    assert db.llm_spend_since("2026-10-01", provider="gemini") == 0.25
    assert db.llm_spend_since("2026-10-01", "nobody") == 0.0
    assert db.llm_spend_since("2027") == 0.0


def test_llm_usage_breakdown(conn_db):
    assert db.llm_usage_breakdown("2000") == []
    t = "2026-10-10T00:00:00"
    db.record_llm_usage(provider="anthropic", model="sonnet", stage="draft", input_tokens=100,
                        output_tokens=10, cache_read_tokens=5, web_searches=1,
                        cost_usd=1.0, ts=t)
    db.record_llm_usage(provider="anthropic", model="sonnet", stage="draft", input_tokens=50,
                        output_tokens=5, cost_usd=0.5, ts=t)
    db.record_llm_usage(provider="gemini", model="flash", stage=None, cost_usd=None, ts=t)
    db.record_llm_usage(provider="gemini", model="flash", stage="old", cost_usd=9.0,
                        ts="2020-01-01T00:00:00")
    rows = db.llm_usage_breakdown("2026-01-01")
    assert [(r["provider"], r["model"], r["stage"]) for r in rows] == [
        ("anthropic", "sonnet", "draft"), ("gemini", "flash", "-")]   # dearest first
    top = rows[0]
    assert top["calls"] == 2 and top["input_tokens"] == 150 and top["output_tokens"] == 15
    assert top["cache_read_tokens"] == 5 and top["web_searches"] == 1
    assert top["cost_usd"] == 1.5 and top["unpriced"] == 0
    assert rows[1]["cost_usd"] is None and rows[1]["unpriced"] == 1 and rows[1]["calls"] == 1
    assert set(top) == {"provider", "model", "stage", "calls", "input_tokens",
                        "output_tokens", "cache_read_tokens", "web_searches",
                        "cost_usd", "unpriced"}
