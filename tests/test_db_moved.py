"""The SQL moved into db.py from ebay/, pipeline/ and scripts/ (Q13): same
results on both backends. Each test runs on SQLite and on SQL Server."""
import time

import pytest

import config
import db


@pytest.fixture(params=["sqlite", "sqlserver"])
def backend(request, tmp_path, monkeypatch):
    if request.param == "sqlite":
        monkeypatch.setattr(config, "DB_BACKEND", "sqlite")
        monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "ross.db"))
        db.create_tables()
    else:
        request.getfixturevalue("mssql")       # this run's test DB, emptied
        monkeypatch.setattr(config, "DB_BACKEND", "sqlserver")
    return request.param


def test_ebay_token_roundtrip_and_refresh_kept(backend):
    assert db.load_ebay_token() is None
    db.save_ebay_token("a1", "r1", 1_800_000_000.25)
    assert db.load_ebay_token() == {"access_token": "a1", "refresh_token": "r1",
                                    "expires_at": pytest.approx(1_800_000_000.25, abs=1e-5)}
    db.save_ebay_token("a2", None, 1_800_003_600.0)     # a refresh: no new refresh token
    tok = db.load_ebay_token()
    assert (tok["access_token"], tok["refresh_token"]) == ("a2", "r1")
    assert tok["expires_at"] == pytest.approx(1_800_003_600.0)


def test_ebay_auth_uses_db(backend):
    from ebay import auth
    auth._save_token({"access_token": "x", "refresh_token": "y", "expires_in": 7200})
    stored = auth._load_token()
    assert stored["access_token"] == "x" and stored["refresh_token"] == "y"
    assert stored["expires_at"] == pytest.approx(time.time() + 7200, abs=60)


def test_taxonomy_cache_roundtrip_and_ttl(backend, monkeypatch):
    from ebay import taxonomy
    assert taxonomy._cache_get("aspects:1") is None
    taxonomy._cache_set("aspects:1", [{"name": "Brand"}])
    taxonomy._cache_set("category_name:1", "Women's Bags")   # a plain JSON string
    assert taxonomy._cache_get("aspects:1") == [{"name": "Brand"}]
    assert taxonomy._cache_get("category_name:1") == "Women's Bags"
    taxonomy._cache_set("aspects:1", [{"name": "Color"}])    # upsert
    assert taxonomy._cache_get("aspects:1") == [{"name": "Color"}]
    monkeypatch.setattr(taxonomy.time, "time", lambda: time.time_ns() / 1e9 + taxonomy.CACHE_TTL + 60)
    assert taxonomy._cache_get("aspects:1") is None           # expired


def test_own_listings_and_sold_items(backend):
    a = db.create_item([])
    db.update_field(a, "ebay", {"listing_id": "117", "sale_price": 40.0, "units_sold": 1})
    db.update_field(a, "listing", {"title": "DKNY Tote"})
    db.update_field(a, "identification", {"brand": "DKNY"})
    db.update_status(a, "sold")
    b = db.create_item([])
    assert sorted(db.own_listings(), key=str) == sorted([("117", "DKNY Tote"), (None, None)], key=str)
    sold = db.sold_items_for_calibration()
    assert [s["item_id"] for s in sold] == [a]
    assert sold[0]["identification"] == {"brand": "DKNY"} and sold[0]["receipt"] is None
    assert b


def test_llm_stage_spend(backend):
    db.record_llm_usage(provider="anthropic", model="m", stage="judge", item_id="i1",
                        cost_usd=0.5, ts="2026-10-01T00:00:00+00:00")
    db.record_llm_usage(provider="anthropic", model="m", stage="judge", item_id="i2",
                        cost_usd=0.25, ts="2026-10-02T00:00:00+00:00")
    db.record_llm_usage(provider="anthropic", model="m", stage="other", cost_usd=9.0,
                        ts="2026-10-02T00:00:00+00:00")
    assert db.llm_stage_spend("2026-10-01T00:00:00+00:00", "judge") == (0.75, 2)
    assert db.llm_stage_spend("2026-10-02", "judge") == (0.25, 1)
    assert db.llm_stage_spend("2026", "judge", "i1") == (0.5, 1)


def test_modify_field_is_a_checked_write(backend):
    item = db.create_item([])
    db.update_field(item, "ebay", {"sale_price": 10.0, "quantity": 1})
    assert db.modify_field(item, "ebay", lambda e: None) is False      # leave it
    assert db.modify_field(item, "ebay", lambda e: {**e, "sale_price": 12.0}) is True
    item_now = db.get_item(item)
    assert item_now["ebay"] == {"sale_price": 12.0, "quantity": 1}
    assert item_now["updated_at"].endswith("+00:00")                    # UTC, not local
    assert db.modify_field("no-such-item", "ebay", lambda e: {}) is False


def test_readonly_connection_refuses_writes(backend):
    item = db.create_item([])
    con = db.connect_readonly()
    try:
        assert con.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 1
        with pytest.raises(Exception):
            con.execute("DELETE FROM items")
    finally:
        con.close()
    assert db.get_item(item) is not None
