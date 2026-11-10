"""eBay's listing quality report: parsing the xlsx (pipeline/quality_report.py),
storing it (db.save_quality_report / latest_quality_report, both backends)
and the bot handler for the file (bot/report_cmds.py)."""
from datetime import datetime, timezone

import openpyxl
import pytest

import config
import db
from bot import report_cmds
from pipeline import quality_report as qr
from tests import qr_fixture, tg_fakes

OWNER = tg_fakes.DEFAULT_USER_ID


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    """Each test its own SQLite file (the SQL Server run empties its test DB
    before every test already)."""
    if config.DB_BACKEND == "sqlite":
        monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "ross.db"))
        db.create_tables()


@pytest.fixture
def report_path(tmp_path):
    return qr_fixture.write_report(tmp_path / "report.xlsx")


# --- parsing ----------------------------------------------------------------------------

def test_report_date_is_utc():
    assert qr.report_date(qr_fixture.GENERATED) == datetime(2026, 11, 6, 14, 38, tzinfo=timezone.utc)
    assert qr.report_date("Report generated: US - 07/01/2026 09-05 PDT") == \
        datetime(2026, 7, 1, 16, 5, tzinfo=timezone.utc)
    assert qr.report_date("no date here") is None


def test_sections_funnel_benchmarks_and_recommendations(report_path):
    r = qr.parse(report_path)
    assert r["report_date"] == datetime(2026, 11, 6, 14, 38, tzinfo=timezone.utc)
    bags, pillow = r["sections"]
    assert bags["section"] == "Women's Bags & Handbags" and bags["conditions"] == "Listings condition: New"
    assert pillow["conditions"] == "Listings condition: New, Price range: $35 - $11,405"
    assert (bags["gmv_rank"], bags["sellers"]) == (1397, 444519)
    assert (bags["impressions_per_day"], bags["impressions_rank"], bags["ctr"], bags["ctr_rank"]) == \
        (163, 731, 0.0038, 12686)
    assert (bags["top_ctr"], bags["bottom_ctr"], bags["top_impressions_per_day"]) == (0.0389, 0.0013, 499)
    assert bags["top_conversion"] == 0.3269 and pillow["bottom_impressions_per_day"] == 1
    [rec] = bags["recommendations"]
    assert rec == {"stage": "impressions", "text": "Add free shipping to 81% of your listings",
                   "kind": "free_shipping", "target": None, "ours": 0, "top": 0.81, "bottom": 0.6077}
    [rec] = pillow["recommendations"]
    assert rec["kind"] == "ad_rate" and rec["target"] == pytest.approx(0.07)
    # The click-through block shares the first block's column labels.
    assert bags["benchmarks"]["ctr"]["top"] == {"Click-through rate": 0.0389,
                                                "Recommended item specifics filled": 8,
                                                "Brand": "Not relevant", "Number of photos": 6.3}


def test_listings_are_read_by_label_whatever_the_column_order(report_path):
    r = qr.parse(report_path)
    by = {x["item_id"]: x for x in r["listings"]}
    assert set(by) == {"bag00001-aaaa", "bag00002-bbbb", "pil00001-cccc"}
    hobo, pillow = by["bag00001-aaaa"], by["pil00001-cccc"]
    assert hobo["ebay_item_id"] == "117262064558" and hobo["section"] == "Women's Bags & Handbags"
    assert (hobo["impressions_per_day"], hobo["ctr"], hobo["conversion"]) == (30, 0.0012, 0.0)  # "No sales"
    assert (hobo["free_shipping"], hobo["ad_rate"], hobo["watchers"], hobo["sales_31d"]) == (False, 0.04, 2, 0)
    assert (hobo["price"], hobo["shipping_fee"], hobo["age_days"], hobo["specifics_filled"]) == (69.99, 9.99, 138, 15)
    assert hobo["specifics_to_add"] is None and hobo["raw"]["Action"] == "Edit"
    assert pillow["ad_rate"] == 0.04 and pillow["free_shipping"] is False and pillow["watchers"] == 0
    assert pillow["ctr"] == 0.0                                    # "No clicks": a real zero, not unknown
    assert by["bag00002-bbbb"]["conversion"] == 0.02 and by["bag00002-bbbb"]["sales_31d"] == 1


def test_other_files_are_refused(tmp_path):
    other = tmp_path / "other.xlsx"
    wb = openpyxl.Workbook()
    wb.active.cell(1, 1, "Charges Apr-Oct")
    wb.save(other)
    with pytest.raises(qr.ReportError, match="no category sheets"):
        qr.parse(other)
    junk = tmp_path / "junk.xlsx"
    junk.write_bytes(b"not a zip")
    with pytest.raises(qr.ReportError, match="not a readable xlsx"):
        qr.parse(junk)


def test_recommendation_kinds():
    assert qr.recommendation_kind("Add free shipping to 81% of your listings") == ("free_shipping", None)
    assert qr.recommendation_kind("Consider increasing promoted listings ad rate to 9%")[0] == "ad_rate"
    assert qr.recommendation_kind("Fill in at least 8 recommended item specifics") == ("specifics", 8.0)
    assert qr.recommendation_kind("Add more photos") == ("other", None)


def test_which_recommendations_apply_to_a_listing():
    free = {"kind": "free_shipping", "top": 0.81}
    ad = {"kind": "ad_rate", "target": 0.07}
    spec = {"kind": "specifics", "target": 8.0}
    assert qr.applies(free, {"free_shipping": False}) and not qr.applies(free, {"free_shipping": True})
    assert qr.applies(free, None)                                   # newer than the report: unknown applies
    assert qr.applies(ad, {"ad_rate": 0.04}) and not qr.applies(ad, {"ad_rate": 0.07})
    assert qr.applies(spec, {"specifics_filled": 6}) and not qr.applies(spec, {"specifics_filled": 8})
    item = {"listing": {"item_specifics": {str(n): n for n in range(9)}}}
    assert not qr.applies(spec, None, item)                         # 9 in the DB
    assert "/revise abcd1234" in qr.fix_text(spec, {"specifics_filled": 6, "specifics_to_add": "Style, Size"},
                                             "abcd1234-x")
    assert "free-shipping test" in qr.fix_text(free, None, "abcd1234-x")
    assert "ad rate 7% for this category (this listing: 4%)" in qr.fix_text(ad, {"ad_rate": 0.04}, "x")


# --- storing it ---------------------------------------------------------------------------

def test_save_and_read_back_the_latest(report_path):
    assert db.latest_quality_report() is None
    r = qr.parse(report_path)
    assert db.save_quality_report(r) == {"sections": 2, "listings": 3, "replaced": False}
    got = db.latest_quality_report()
    assert datetime.fromisoformat(got["report_date"]) == r["report_date"]
    bags = got["sections"][0]
    assert bags["section"] == "Women's Bags & Handbags" and bags["ctr"] == pytest.approx(0.0038)
    assert isinstance(bags["ctr"], float) and bags["recommendations"][0]["kind"] == "free_shipping"
    assert bags["benchmarks"]["ctr"]["top"]["Click-through rate"] == pytest.approx(0.0389)
    hobo = next(x for x in got["listings"] if x["item_id"] == "bag00001-aaaa")
    assert hobo["free_shipping"] is False and hobo["conversion"] == 0.0 and hobo["watchers"] == 2
    assert hobo["ad_rate"] == pytest.approx(0.04) and hobo["raw"]["Action"] == "Edit"


def test_the_same_report_again_replaces_it_and_a_newer_one_wins(report_path, tmp_path):
    r = qr.parse(report_path)
    db.save_quality_report(r)
    assert db.save_quality_report(r) == {"sections": 2, "listings": 3, "replaced": True}
    assert len(db.latest_quality_report()["listings"]) == 3
    newer = qr.parse(qr_fixture.write_report(tmp_path / "newer.xlsx", sections=(qr_fixture.PILLOWCASES,),
                                             generated="Report generated: US - 12/01/2026 08-00 PST"))
    db.save_quality_report(newer)
    got = db.latest_quality_report()
    assert got["report_date"].startswith("2026-12-01T16:00") and [s["section"] for s in got["sections"]] == \
        ["Pillowcases"]


# --- the handler -------------------------------------------------------------------------------

def _send(monkeypatch, tmp_path, data, user=OWNER, name="Listing quality report.xlsx"):
    monkeypatch.setattr(config, "OWNER_USER_IDS", {OWNER})
    monkeypatch.setattr(report_cmds, "REPORTS_DIR", tmp_path / "reports")
    update, ctx = tg_fakes.make_document_update(data, name, user_id=user)
    tg_fakes.run(report_cmds.report_document, update, ctx)
    return "\n".join(tg_fakes.sent_texts(ctx))


def test_owner_sends_the_report_and_gets_a_summary(monkeypatch, tmp_path, report_path):
    monkeypatch.setattr(db, "list_items", lambda: [{"item_id": "bag00001-aaaa", "status": "published"},
                                                   {"item_id": "pil00001-cccc", "status": "sold"}])
    text = _send(monkeypatch, tmp_path, report_path.read_bytes())
    assert "report of 11/06/2026 saved: 2 categories, 3 listings, 1 of them live in our DB" in text
    assert "Women's Bags & Handbags: 2 listings. Us 163 impressions/day, CTR 0.38% (top 10% 3.89%)" in text
    assert "eBay: Consider increasing promoted listings ad rate to 7%." in text
    assert db.latest_quality_report()["sections"][1]["section"] == "Pillowcases"
    saved = list((tmp_path / "reports").iterdir())
    assert len(saved) == 1 and saved[0].name.startswith("quality-report-")   # our name, not the sender's
    again = _send(monkeypatch, tmp_path, report_path.read_bytes())
    assert "(replaced the copy sent before)" in again


def test_not_the_owner_or_not_a_report(monkeypatch, tmp_path, report_path):
    text = _send(monkeypatch, tmp_path, report_path.read_bytes(), user=5151)
    assert "owner-only" in text and db.latest_quality_report() is None
    text = _send(monkeypatch, tmp_path, b"not a zip")
    assert "isn't eBay's listing quality report" in text and "Nothing saved" in text
    monkeypatch.setattr(config, "QUALITY_REPORT_MAX_BYTES", 10)
    monkeypatch.setattr(report_cmds.config, "QUALITY_REPORT_MAX_BYTES", 10)
    assert "Nothing saved" in _send(monkeypatch, tmp_path, report_path.read_bytes())
    assert db.latest_quality_report() is None
