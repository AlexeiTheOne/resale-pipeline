"""scripts/backtest_check.py: outcomes and hit rates (no API calls)."""
import importlib.util
from datetime import datetime, timezone
from pathlib import Path

import config

_spec = importlib.util.spec_from_file_location(
    "backtest_check", Path(__file__).parent.parent / "scripts" / "backtest_check.py")
bt = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bt)

NOW = datetime(2026, 11, 2, tzinfo=timezone.utc)


def item(status, net=None, units=1, ross=10.0, published="2026-09-01T00:00:00Z", sold="2026-09-11T00:00:00Z"):
    return {"status": status, "receipt": {"reduced_price": ross}, "created_at": published,
            "ebay": {"net_proceeds": net, "units_sold": units, "published_at": published,
                     "first_sold_at": sold if status == "sold" else None}}


def test_outcomes(monkeypatch):
    monkeypatch.setattr(config, "SALES_TAX_RATE", 0.07)
    monkeypatch.setattr(config, "CHECK_BUY_MIN_PROFIT", 15.0)
    assert bt.outcome(item("sold", net=30.0), NOW) == {"kind": "win", "profit": 19.3, "days": 10.0}
    assert bt.outcome(item("sold", net=20.0), NOW)["kind"] == "ok"
    assert bt.outcome(item("sold", net=8.0), NOW)["kind"] == "loss"
    assert bt.outcome(item("sold", net=60.0, units=2), NOW)["profit"] == 19.3     # per unit
    assert bt.outcome(item("published"), NOW)["kind"] == "stale"                  # 62 days
    assert bt.outcome(item("published", published="2026-10-20T00:00:00Z"), NOW)["kind"] == "open"


def test_hit_rates():
    rows = [
        {"exact_verdict": "BUY", "outcome": "win", "actual_profit": 20.0, "days": 5},
        {"exact_verdict": "BUY", "outcome": "loss", "actual_profit": -2.0, "days": 30},
        {"exact_verdict": "SKIP", "outcome": "stale", "actual_profit": None, "days": 70},
        {"exact_verdict": "SKIP", "outcome": "win", "actual_profit": 16.0, "days": 3},
        {"exact_verdict": "MAYBE", "outcome": "ok", "actual_profit": 5.0, "days": 9},
    ]
    hr = bt.hit_rates(rows, "exact")
    assert hr["BUY"]["hit"] == 0.5 and hr["BUY"]["avg_profit"] == 9.0
    assert hr["SKIP"]["hit"] == 0.5 and hr["SKIP"]["stale"] == 1
    assert hr["MAYBE"]["hit"] is None and hr["MAYBE"]["ok"] == 1 and hr["BORDERLINE"]["n"] == 0
    assert hr["BUY"]["avg_days"] == 17.5 and hr["NO_DATA"]["n"] == 0


def test_price_errors_and_the_msrp_table():
    s = bt._errors([(50.0, 40.0), (30.0, 40.0), (44.0, 40.0), (None, 10.0)])
    assert s["n"] == 3 and s["mae"] == 0.25 and s["bias"] == 0.10 and round(s["within20"], 2) == 0.33
    assert bt._errors([]) is None
    rows = [{"item": "a", "brand": "coach", "bucket": "bag", "msrp": 100.0, "actual": 40.0, "estimate": 47.0,
             "level": "type", "key": "bag", "n": 15, "flags": False},
            {"item": "b", "brand": "coach", "bucket": "bag", "msrp": 300.0, "actual": 40.0, "estimate": 141.0,
             "level": "type", "key": "bag", "n": 15, "flags": True}]
    text = "\n".join(bt.msrp_section(rows, "MSRP"))
    assert "| all | 1 | 18% | +18% | 100% |" in text and "1 item(s) skipped" in text
    acc = "\n".join(bt.price_accuracy([{"sold_item_price": 40.0, "exact_price": 39.99, "exact_used": "1"},
                                       {"sold_item_price": None, "exact_price": 20.0, "exact_used": "4"}]))
    assert "| all | 1 | 0% | -0% | 100% |" in acc and "| 1 | 1 |" in acc
