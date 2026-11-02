"""migrations.py: numbered steps, run once each, all-or-nothing."""
import sqlite3

import pytest

import migrations

# SQLite's numbered migrations; SQL Server's: test_mssql_schema.py.
pytestmark = pytest.mark.sqlite_only("SQLite's migrations on SQLite files")


def _con(tmp_path, name="m.db"):
    return sqlite3.connect(tmp_path / name)


def _tables(con):
    return {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def test_fresh_db_gets_every_table_and_latest_version(tmp_path):
    con = _con(tmp_path)
    ran = migrations.migrate(con)
    assert ran == [v for v, _, _ in migrations.MIGRATIONS]
    assert {"items", "listing_stats", "pieces", "llm_usage", "ebay_tokens",
            "taxonomy_cache", "schema_version"} <= _tables(con)
    assert migrations.current_version(con) == migrations.MIGRATIONS[-1][0]


def test_second_run_does_nothing(tmp_path):
    con = _con(tmp_path)
    migrations.migrate(con)
    assert migrations.migrate(con) == []
    assert con.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0] == len(migrations.MIGRATIONS)


def test_existing_db_without_versions_keeps_its_data(tmp_path):
    # A DB from before migrations: old items table, missing later columns.
    con = _con(tmp_path)
    con.execute("CREATE TABLE items (item_id TEXT PRIMARY KEY, status TEXT NOT NULL DEFAULT 'captured',"
                " photos TEXT, processed TEXT, identification TEXT, pricing TEXT, listing TEXT,"
                " ebay TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)")
    con.execute("INSERT INTO items (item_id, created_at, updated_at) VALUES ('a', 't', 't')")
    con.commit()
    migrations.migrate(con)
    cols = {r[1] for r in con.execute("PRAGMA table_info(items)")}
    assert {"receipt", "stock", "listing_check"} <= cols
    assert con.execute("SELECT item_id FROM items").fetchall() == [("a",)]
    assert migrations.current_version(con) == migrations.MIGRATIONS[-1][0]


def test_failed_step_changes_nothing_and_is_not_recorded(tmp_path):
    con = _con(tmp_path)
    migrations.migrate(con)

    def bad(c):
        c.execute("CREATE TABLE half_done (x)")
        raise RuntimeError("boom")

    steps = migrations.MIGRATIONS + [(99, "bad", bad)]
    with pytest.raises(RuntimeError):
        migrations.migrate(con, steps)
    assert "half_done" not in _tables(con)
    assert migrations.current_version(con) == migrations.MIGRATIONS[-1][0]


def test_new_step_runs_once_in_order(tmp_path):
    con = _con(tmp_path)
    migrations.migrate(con)
    calls = []
    last = migrations.MIGRATIONS[-1][0]
    steps = migrations.MIGRATIONS + [
        (last + 1, "next", lambda c: calls.append(last + 1)),
        (last + 2, "after", lambda c: calls.append(last + 2)),
    ]
    assert migrations.migrate(con, steps) == [last + 1, last + 2]
    assert migrations.migrate(con, steps) == []
    assert calls == [last + 1, last + 2]


def test_db_newer_than_code_warns_but_works(tmp_path, capsys):
    con = _con(tmp_path)
    future = migrations.MIGRATIONS[-1][0] + 1
    migrations.migrate(con, migrations.MIGRATIONS + [(future, "future", lambda c: None)])
    assert migrations.migrate(con) == []
    assert "Was a deploy rolled back?" in capsys.readouterr().out


def test_connection_left_usable_with_normal_transactions(tmp_path):
    con = _con(tmp_path)
    level = con.isolation_level
    migrations.migrate(con)
    assert con.isolation_level == level
    with con:
        con.execute("INSERT INTO seen_orders (order_id, seen_at) VALUES ('o', 't')")
    assert con.execute("SELECT COUNT(*) FROM seen_orders").fetchone()[0] == 1
