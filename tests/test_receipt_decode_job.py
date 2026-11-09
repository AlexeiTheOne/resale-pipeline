"""The daily receipt decoding job and the backfill script
(bot/jobs.py, scripts/decode_receipt_lines.py)."""
import asyncio
import importlib.util
import json
from pathlib import Path

import pytest
from telegram import Bot

import config
import db
from bot import app as bot_app
from bot import jobs, util
from pipeline import receipt_decode as rd


def _started(monkeypatch, **flags) -> list[str]:
    names = []
    monkeypatch.setattr(bot_app, "_start_loop", lambda app, fn: names.append(fn.__name__))

    async def set_my_commands(self, commands, *a, **k):
        return True
    monkeypatch.setattr(Bot, "set_my_commands", set_my_commands)
    monkeypatch.setattr(db, "last_job_run", lambda job: None)
    monkeypatch.setattr(util, "ALLOWED_USER_IDS", {4242})
    for k, v in {"JOBS_ENABLED": True, **flags}.items():
        monkeypatch.setattr(config, k, v)
    from telegram.ext import Application
    asyncio.run(bot_app._post_init(Application.builder().token("123:fake").build()))
    return names


def test_the_job_is_off_until_switched_on(monkeypatch):
    assert "_daily_receipt_decode_loop" not in _started(monkeypatch, RECEIPT_DECODE_JOB=False,
                                                        DB_BACKEND="sqlserver")
    assert "_daily_receipt_decode_loop" in _started(monkeypatch, RECEIPT_DECODE_JOB=True, DB_BACKEND="sqlserver")
    assert "_daily_receipt_decode_loop" not in _started(monkeypatch, RECEIPT_DECODE_JOB=True, DB_BACKEND="sqlite")


def test_a_run_is_stamped_and_a_failure_goes_to_errors(monkeypatch):
    stamps, errors = [], []
    monkeypatch.setattr(db, "record_job_run", lambda job, **kw: stamps.append((job, kw.get("detail"))))
    monkeypatch.setattr(util, "_record_error", lambda where, e: errors.append(where))
    monkeypatch.setattr(rd, "decode_pending", lambda **kw: {"saved": 3, "ai": 2, "ai_cost_usd": 0.0012,
                                                            "stopped": None, "waiting": 0})
    asyncio.run(jobs._run_receipt_decode_job(None))
    assert stamps == [("daily_receipt_decode", "3 saved, 2 by AI, $0.0012")]

    def boom(**kw):
        raise RuntimeError("db down")
    monkeypatch.setattr(rd, "decode_pending", boom)
    asyncio.run(jobs._run_receipt_decode_job(None))
    assert errors == ["receipt decode"] and len(stamps) == 1


# --- the backfill script ------------------------------------------------------------------------

def _script():
    spec = importlib.util.spec_from_file_location(
        "decode_receipt_lines", Path(__file__).resolve().parent.parent / "scripts" / "decode_receipt_lines.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("name, production", [("ResaleDB", False), ("ResaleDB_Dev", True),
                                              ("ResaleDB_Migrate_20261102", False), ("Other", False)])
def test_script_refuses_wrong_targets(name, production):
    with pytest.raises(SystemExit):
        _script().check_target(name, production)


def _count(mssql) -> int:
    cur = mssql.cursor()
    cur.execute("SELECT COUNT(*) FROM dbo.receipt_line_details")
    return cur.fetchone()[0]


@pytest.fixture
def lines(mssql, monkeypatch):
    monkeypatch.setattr(config, "DB_BACKEND", "sqlserver")
    cur = mssql.cursor()
    cur.execute("INSERT INTO dbo.Receipts (ReceiptNumber, ReceiptDate) OUTPUT INSERTED.ReceiptID "
                "VALUES ('2710-03-5447-6292-3', '2026-10-19')")
    rid = cur.fetchone()[0]
    for i, (sku, desc) in enumerate([("400100000001", "#ASST RUN/COURT"), ("400100000002", "ZXQ WIDGET")], 1):
        cur.execute("INSERT INTO dbo.ReceiptItems (ReceiptID, LineIndex, SKU, Description, Price) "
                    "VALUES (?, ?, ?, ?, 9.99)", rid, i, sku, desc)
    mssql.commit()


def test_dry_run_writes_nothing(lines, mssql, mssql_db, tmp_path, monkeypatch):
    s = _script()
    monkeypatch.setattr(rd, "ai_decode_all", lambda *a, **k: pytest.fail("the dry run must not call the AI"))
    assert s.main(["--target", mssql_db, "--report-dir", str(tmp_path)]) == 0
    assert _count(mssql) == 0
    [report] = tmp_path.glob("*-dry-run.md")
    text = report.read_text(encoding="utf-8")
    assert "SQL key vs Python mirror: 0 difference(s)" in text and "| ai | 1 | 1 |" in text


def test_ai_run_writes_a_file_not_the_db_and_apply_reuses_it(lines, mssql, mssql_db, tmp_path, monkeypatch):
    s = _script()
    calls = []

    def fake_all(pending, **kw):
        calls.append([p["key"] for p in pending])
        return ({p["key"]: rd.ai_fields({"cat": "toys", "type": "game", "gender": "none", "color": "", "brand": "",
                                         "model": "", "conf": 0.8}, "claude-haiku-5-5") for p in pending},
                {"cost_usd": 0.001, "calls": 1, "input_tokens": 10, "output_tokens": 10, "model": "claude-haiku-5-5",
                 "stopped": None, "left": 0})
    monkeypatch.setattr(rd, "ai_decode_all", fake_all)
    assert s.main(["--target", mssql_db, "--ai", "--report-dir", str(tmp_path)]) == 0
    assert calls == [["ZXQ WIDGET"]] and _count(mssql) == 0
    [jsonl] = tmp_path.glob("receipt-decode-*.jsonl")
    assert json.loads(jsonl.read_text(encoding="utf-8").splitlines()[1])["desc_key"] == "ZXQ WIDGET"
    assert "## Sample" in next(tmp_path.glob("receipt-decode-*[0-9].md")).read_text(encoding="utf-8")

    monkeypatch.setattr(rd, "ai_decode_all", lambda *a, **k: pytest.fail("apply must not call the AI"))
    assert s.main(["--target", mssql_db, "--apply", "--from", str(jsonl), "--report-dir", str(tmp_path)]) == 0
    cur = mssql.cursor()
    cur.execute("SELECT desc_key, source, category FROM dbo.receipt_line_details ORDER BY desc_key")
    assert [tuple(r) for r in cur.fetchall()] == [("#ASST RUN/COURT", "rules", "shoes"), ("ZXQ WIDGET", "ai", "toys")]
    assert (tmp_path / "receipt_decode.jsonl").exists()


def test_reuse_keeps_paid_rows_and_redoes_a_category(lines, mssql, mssql_db, tmp_path, monkeypatch):
    old = tmp_path / "old.jsonl"
    row = rd.ai_fields({"cat": "bags", "type": "tote", "gender": "none", "color": "", "brand": "", "model": "",
                        "conf": 0.6}, "m")
    old.write_text('{"run": "x"}\n' + json.dumps({"desc_key": "ZXQ WIDGET", "row": row}) + "\n", encoding="utf-8")
    calls = []

    def fake_all(pending, **kw):
        calls.append([p["key"] for p in pending])
        return {}, {"cost_usd": 0.0, "calls": 0, "input_tokens": 0, "output_tokens": 0, "model": None,
                    "stopped": None, "left": 0}
    monkeypatch.setattr(rd, "ai_decode_all", fake_all)
    s = _script()
    assert s.main(["--target", mssql_db, "--ai", "--reuse", str(old), "--report-dir", str(tmp_path)]) == 0
    assert calls[-1] == []                                   # reused: nothing paid again
    assert s.main(["--target", mssql_db, "--ai", "--reuse", str(old), "--redo-category", "bags",
                   "--report-dir", str(tmp_path)]) == 0
    assert calls[-1] == ["ZXQ WIDGET"]


def test_apply_refuses_before_step_5(lines, mssql, mssql_db, tmp_path, monkeypatch):
    import db_mssql
    monkeypatch.setattr(db_mssql, "_has_line_details", lambda cur: False)
    f = tmp_path / "x.jsonl"
    f.write_text('{"run": "x"}\n', encoding="utf-8")
    with pytest.raises(SystemExit, match="step 5"):
        _script().main(["--target", mssql_db, "--apply", "--from", str(f), "--report-dir", str(tmp_path)])
