"""The SQL Server safety lock and connection settings. No server needed."""
from datetime import datetime, timedelta, timezone

import pytest

import config
import sqlserver


@pytest.mark.parametrize("name", [
    "ResaleDB", "ResaleDB_Dev", "ReceiptsDB", "master", "tempdb",
    "resaledb_test_1_20260101000000",   # lock is case-sensitive on purpose
    "ResaleDB_Test_",                   # prefix alone is not a test database
    "ResaleDB_Testing",
])
def test_lock_refuses_anything_but_test_databases(name):
    with pytest.raises(RuntimeError, match="refusing"):
        sqlserver.assert_test_database(name)


def test_lock_accepts_a_test_database():
    sqlserver.assert_test_database("ResaleDB_Test_123_20261102010203")


@pytest.mark.parametrize("name", ["x]; DROP DATABASE ResaleDB; --", "a b", "", None, "1abc"])
def test_unsafe_names_are_refused_before_any_sql(name):
    with pytest.raises(ValueError):
        sqlserver.assert_test_database(name)
    with pytest.raises((ValueError, RuntimeError)):
        sqlserver.conn_str(name or "x]")


def test_drop_refuses_real_databases_without_connecting(monkeypatch):
    def no_connect(*a, **k):
        raise AssertionError("must refuse before connecting")
    monkeypatch.setattr(sqlserver, "connect", no_connect)
    for name in ("ResaleDB", "ResaleDB_Dev", "ReceiptsDB"):
        with pytest.raises(RuntimeError):
            sqlserver.drop_test_database(name)


def test_only_dev_and_test_databases_can_be_reset(monkeypatch):
    def no_connect(*a, **k):
        raise AssertionError("must refuse before connecting")
    monkeypatch.setattr(sqlserver, "connect", no_connect)
    for name in ("ResaleDB", "ReceiptsDB", "resaledb_dev", "master"):
        with pytest.raises(RuntimeError):
            sqlserver.reset_database(name)
    sqlserver.assert_resettable("ResaleDB_Dev")
    sqlserver.assert_resettable("ResaleDB_Test_1_20261102010203")


def test_new_names_are_unique_per_process_and_time():
    t = datetime(2026, 11, 2, 1, 2, 3, tzinfo=timezone.utc)
    name = sqlserver.new_test_database_name(t)
    assert name.startswith("ResaleDB_Test_") and name.endswith("_20261102010203")
    sqlserver.assert_test_database(name)


def test_only_old_test_databases_are_stale():
    now = datetime(2026, 11, 2, 12, 0, 0, tzinfo=timezone.utc)
    names = ["ResaleDB_Test_1_20261031120000",   # 2 days old
             "ResaleDB_Test_2_20261102110000",   # 1 hour old: maybe still running
             "ResaleDB", "ResaleDB_Dev", "ResaleDB_Test_x", None]
    assert sqlserver.stale_test_databases(names, now=now) == ["ResaleDB_Test_1_20261031120000"]
    assert sqlserver.stale_test_databases(names, now=now, older_than=timedelta(minutes=30)) == [
        "ResaleDB_Test_1_20261031120000", "ResaleDB_Test_2_20261102110000"]


def test_no_default_database(monkeypatch):
    # Dev and production share the instance: a missing setting must fail.
    monkeypatch.setattr(config, "SQL_DATABASE", None)
    with pytest.raises(RuntimeError, match="SQL_DATABASE is not set"):
        sqlserver.conn_str()


def test_conn_str_shape(monkeypatch):
    monkeypatch.setattr(config, "SQL_SERVER", r".\SQLEXPRESS")
    s = sqlserver.conn_str("ResaleDB_Dev")
    assert "Database=ResaleDB_Dev;" in s
    assert r"Server=.\SQLEXPRESS;" in s
    assert "Trusted_Connection=yes;" in s and "Encrypt=yes;" in s
    assert "PWD" not in s.upper()
