"""scripts/init_sqlserver_db.py refuses the wrong targets. No server needed."""
import importlib.util
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "init_sqlserver_db", Path(__file__).parent.parent / "scripts" / "init_sqlserver_db.py")
init = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(init)


@pytest.mark.parametrize("name", ["ResaleDB", "resaledb"])
def test_production_needs_the_flag(name):
    with pytest.raises(SystemExit, match="production"):
        init.check_target(name, production=False)
    assert init.check_target(name, production=True) == name


@pytest.mark.parametrize("name", [None, ""])
def test_no_database_is_an_error(name):
    with pytest.raises(SystemExit, match="SQL_DATABASE"):
        init.check_target(name, production=False)


def test_receiptsdb_is_never_a_target():
    with pytest.raises(SystemExit, match="receipt_pipeline"):
        init.check_target("ReceiptsDB", production=True)


def test_dev_is_fine():
    assert init.check_target("ResaleDB_Dev", production=False) == "ResaleDB_Dev"
