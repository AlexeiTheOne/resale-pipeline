"""/diagnose between eBay's listing quality reports: conversion from the
traffic report, recommended item specifics from Taxonomy, eBay's suggested
ad rate (Recommendation API) as a fallback, and the monthly reminder to send
a fresh report. httpx and the DB are faked; nothing touches eBay."""
from datetime import datetime, timedelta, timezone

import httpx
import pytest

import config
import db
import profit
from bot import jobs
from ebay import analytics, marketing, recommendation, taxonomy
from pipeline import diagnose, quality_report

NOW = datetime(2026, 11, 10, 12, tzinfo=timezone.utc)

ASPECTS = [{"localizedAspectName": "Brand", "aspectConstraint": {"aspectRequired": True}},
           {"localizedAspectName": "Department", "aspectConstraint": {"aspectRequired": True}}] + \
          [{"localizedAspectName": n, "aspectConstraint": {"aspectUsage": "RECOMMENDED"}}
           for n in ("Color", "Material", "Style", "Size", "Pattern", "Theme", "Closure", "Features")] + \
          [{"localizedAspectName": "Vintage", "aspectConstraint": {"aspectUsage": "OPTIONAL"}}]


class _Resp:
    def __init__(self, status, body, text=""):
        self.status_code, self._body, self.text = status, body, text or str(body)

    def json(self):
        return self._body


# --- conversion in the traffic report ------------------------------------------------

def _traffic_body(keys, values):
    return {"header": {"metrics": [{"key": k} for k in keys]},
            "records": [{"dimensionValues": [{"value": "L1"}],
                         "metricValues": [{"value": v} for v in values]}]}


def test_traffic_report_reads_transactions(monkeypatch):
    monkeypatch.setattr(analytics, "_optional_ok", True)
    monkeypatch.setattr(analytics, "get_access_token", lambda: "t")
    asked = []

    def fake_get(url, params=None, **kw):
        asked.append(params["metric"])
        return _Resp(200, _traffic_body(["TOTAL_IMPRESSION_TOTAL", "LISTING_VIEWS_TOTAL", "TRANSACTION"],
                                        [3000, 40, 2]))
    monkeypatch.setattr(httpx, "get", fake_get)
    got = analytics.get_traffic_report(["L1"], 30)
    assert "TRANSACTION" in asked[0]
    assert got["L1"]["transactions"] == 2 and got["L1"]["views"] == 40


def test_a_refused_transaction_metric_is_dropped_not_fatal(monkeypatch):
    monkeypatch.setattr(analytics, "_optional_ok", True)
    monkeypatch.setattr(analytics, "get_access_token", lambda: "t")
    asked = []

    def fake_get(url, params=None, **kw):
        asked.append(params["metric"])
        if "TRANSACTION" in params["metric"]:
            return _Resp(400, {}, "invalid metric TRANSACTION")
        return _Resp(200, _traffic_body(["TOTAL_IMPRESSION_TOTAL", "LISTING_VIEWS_TOTAL"], [3000, 40]))
    monkeypatch.setattr(httpx, "get", fake_get)
    got = analytics.get_traffic_report(["L1"], 30)
    assert got["L1"]["impressions"] == 3000 and got["L1"]["transactions"] is None
    analytics.get_traffic_report(["L1"], 30)      # the next call doesn't ask again
    assert ["TRANSACTION" in a for a in asked] == [True, False, False]


def test_conversion_is_sales_over_views_and_averaged_per_category():
    item = {"item_id": "a-x", "listing": {"category_id": "c"}, "ebay": {"listing_id": "L1"}}
    m = diagnose.metrics(item, {"impressions": 3000, "views": 40, "transactions": 2}, 0, False, NOW, 30)
    assert m["conversion"] == 0.05
    unknown = diagnose.metrics(item, {"impressions": 3000, "views": 40}, 0, False, NOW, 30)
    assert unknown["conversion"] is None                     # no metric: unknown, not 0
    b = diagnose.benchmarks([m, {**m, "views": 60, "transactions": 0}, unknown])
    assert b["c"]["conversion"] == 2 / 100                   # the unknown one is left out


# --- recommended specifics (Taxonomy) --------------------------------------------------

def _item(specifics, cat="169291", n=1):
    return {"item_id": f"abcd123{n}-x", "status": "published",
            "listing": {"title": "Bag", "price": 60.0, "category_id": cat, "item_specifics": specifics},
            "ebay": {"listing_id": f"L{n}", "sku": f"SKU{n}",
                     "published_at": (NOW - timedelta(days=60)).isoformat()}}


def test_specifics_count_required_and_recommended_only(monkeypatch):
    monkeypatch.setattr(taxonomy, "category_aspects", lambda cat: ASPECTS)
    check = diagnose.specifics_check(_item({"brand": "DKNY", "Department": "Women", "Color": ["Black"],
                                            "Material": "", "Vintage": "No", "Theme": "Does not apply"}))
    assert check["recommended"] == 10 and check["filled"] == 3
    assert check["missing"][:3] == ["Material", "Style", "Size"]
    monkeypatch.setattr(config, "DIAGNOSE_MIN_RECOMMENDED_SPECIFICS", 8)
    fix = diagnose.specifics_fix(check, "abcd1234-x", "Women's Bags & Handbags")
    assert fix["kind"] == "specifics" and fix["source"] == "taxonomy"
    assert fix["text"] == "Fill at least 8 of eBay's recommended item specifics"
    assert fix["fix"] == ("eBay Taxonomy: 3 of 10 recommended item specifics filled; missing: Material, Style, "
                          "Size, Pattern, Theme, Closure and 1 more. /revise abcd1234 with the ones that apply.")
    assert diagnose.specifics_fix({**check, "filled": 8}, "x", "s") is None
    # A category with only 4 recommended needs all 4.
    assert diagnose.specifics_fix({"recommended": 4, "filled": 3, "missing": ["A"]}, "x", "s")["text"] == \
        "Fill at least 4 of eBay's recommended item specifics"


def test_taxonomy_failure_is_unknown(monkeypatch):
    def boom(cat):
        raise RuntimeError("503")
    monkeypatch.setattr(taxonomy, "category_aspects", boom)
    assert diagnose.specifics_check(_item({})) is None


# --- eBay's suggested ad rate --------------------------------------------------------------

def test_suggested_ad_rates_read_without_the_write_switch(monkeypatch):
    monkeypatch.setattr(config, "EBAY_WRITES_ENABLED", False)    # a read: still allowed
    monkeypatch.setattr(recommendation, "get_access_token", lambda: "t")
    sent = []

    def fake_post(url, params=None, json=None, headers=None, **kw):
        sent.append((params, json, headers["X-EBAY-C-MARKETPLACE-ID"]))
        return _Resp(200, {"listingRecommendations": [
            {"listingId": "L1", "marketing": {"ad": {"bidPercentages": [{"basis": "TRENDING", "value": "8.5"}]}}},
            {"listingId": "L2", "marketing": {"ad": {"bidPercentages": [{"basis": "TRENDING", "value": "6"},
                                                                         {"basis": "ITEM", "value": "7.2"}]}}},
            {"listingId": "L3", "marketing": {"ad": {}}}]})
    monkeypatch.setattr(httpx, "post", fake_post)
    got = recommendation.suggested_ad_rates(["L1", "L2", "L3"])
    assert got == {"L1": {"rate": 0.085, "basis": "TRENDING"}, "L2": {"rate": pytest.approx(0.072), "basis": "ITEM"}}
    params, body, market = sent[0]
    assert params == {"filter": "recommendationTypes:{AD}", "limit": 3} and body == {"listingIds": ["L1", "L2", "L3"]}
    assert market == config.EBAY_MARKETPLACE_ID


def test_our_ad_rates_come_from_the_campaign(monkeypatch):
    monkeypatch.setattr(marketing, "_find_campaign_id", lambda: "C1")
    pages = [{"ads": [{"listingId": "L1", "inventoryReferenceId": "SKU1", "bidPercentage": "4.0"}],
              "next": "x", "total": 2},
             {"ads": [{"inventoryReferenceId": "SKU2", "bidPercentage": "9.0"}], "total": 2}]
    calls = []

    def fake_request(method, path, **kw):
        calls.append((method, kw["params"]["offset"]))
        return _Resp(200, pages[len(calls) - 1])
    monkeypatch.setattr(marketing, "_request", fake_request)
    assert marketing.ad_rates() == {"L1": 0.04, "SKU1": 0.04, "SKU2": 0.09}
    assert calls == [("GET", 0), ("GET", 1)]


def test_ad_rate_fix_needs_a_real_gap(monkeypatch):
    monkeypatch.setattr(config, "DIAGNOSE_AD_RATE_GAP_PCT", 1.0)
    monkeypatch.setattr(config, "EBAY_DEFAULT_AD_RATE_PCT", 4.0)
    assert diagnose.ad_rate_fix({"rate": 0.045, "basis": "ITEM"}, 0.04, "s") is None
    fix = diagnose.ad_rate_fix({"rate": 0.085, "basis": "TRENDING"}, None, "Pillowcases")
    assert fix["fix"] == ("eBay suggests ad rate 8.5% (trending in the category); this listing: 4.0% (our default). "
                          "A guide, not the best rate: owner decision (plan §6).")
    assert diagnose.ad_rate_fix({"rate": 0.085, "basis": "ITEM"}, 0.09, "s") is None   # already above it


# --- the whole run -------------------------------------------------------------------------

def _run(monkeypatch, items, report=None, suggested=None, rates=None):
    monkeypatch.setattr(db, "list_items", lambda: items)
    monkeypatch.setattr(db, "offers_sent_since", lambda since: set())
    monkeypatch.setattr(db, "latest_quality_report", lambda: report)
    monkeypatch.setattr(analytics, "get_traffic_report",
                        lambda ids, days: {i: {"impressions": 3000, "views": 20, "transactions": 1} for i in ids})
    monkeypatch.setattr(analytics, "get_watch_count", lambda lid: 0)
    monkeypatch.setattr(profit, "breakeven_price", lambda item, margin=0: 40.0)
    monkeypatch.setattr(taxonomy, "category_aspects", lambda cat: ASPECTS)
    monkeypatch.setattr(taxonomy, "category_name", lambda cat: "Women's Bags & Handbags")
    asked = []

    def fake_suggested(ids):
        asked.append(list(ids))
        return suggested or {}
    monkeypatch.setattr(recommendation, "suggested_ad_rates", fake_suggested)
    monkeypatch.setattr(marketing, "ad_rates", lambda: rates or {})
    monkeypatch.setattr(config, "DIAGNOSE_MIN_RECOMMENDED_SPECIFICS", 8)
    monkeypatch.setattr(config, "DIAGNOSE_AD_RATE_GAP_PCT", 1.0)
    return diagnose.diagnose(now=NOW, search=lambda *a, **k: None), asked


FULL = {n: "x" for n in ("Brand", "Department", "Color", "Material", "Style", "Size", "Pattern", "Theme")}


def test_no_report_uses_taxonomy_and_the_suggested_ad_rate(monkeypatch):
    items = [_item({"Brand": "DKNY"}, n=1), _item(FULL, n=2)]
    results, asked = _run(monkeypatch, items, suggested={"L1": {"rate": 0.08, "basis": "ITEM"},
                                                         "L2": {"rate": 0.08, "basis": "ITEM"}},
                          rates={"SKU2": 0.09})
    by = {r["item_id"]: r for r in results}
    assert asked == [["L1", "L2"]]
    one = by["abcd1231-x"]
    assert one["verdict"] == "CATEGORY_FIX"
    assert one["reason"] == "eBay's recommended item specifics for Women's Bags & Handbags."
    assert one["fix"].startswith("eBay Taxonomy: 1 of 10 recommended item specifics filled")
    assert one["also"] == ["eBay suggests ad rate 8.0% (for this listing); this listing: 4.0% (our default). "
                           "A guide, not the best rate: owner decision (plan §6)."]
    # 8 specifics filled, ad rate 9% already above eBay's 8%.
    assert by["abcd1232-x"]["verdict"] == "OK"
    detail = "\n".join(diagnose.format_detail(one))
    assert "CTR 0.67%, conversion 5.0% (1 sales)" in detail and "conversion 5.0%." in detail
    summary = diagnose.category_summary(results)
    assert "  Women's Bags & Handbags, 1 listing: Fill at least 8 of eBay's recommended item specifics → " \
           "/diagnose <id> lists the missing ones; add them with /revise." in summary
    assert "  Women's Bags & Handbags, 1 listing: eBay suggests a higher ad rate → owner decision (plan §6)." \
        in summary


def test_a_listing_in_the_report_gets_no_ad_rate_fallback(monkeypatch):
    section = {"section": "Athletic Shoes", "impressions_per_day": 100.0, "ctr": 0.006,
               "recommendations": [{"stage": "ctr", "kind": "specifics", "target": 8.0,
                                    "text": "Fill in at least 8 recommended item specifics"}]}
    row = {"section": "Athletic Shoes", "item_id": "abcd1231-x", "ebay_item_id": "L1", "impressions_per_day": 100.0,
           "ctr": 0.006, "specifics_filled": 5, "specifics_to_add": "Style, Color", "ad_rate": 0.04}
    report = {"report_date": "2026-11-06T14:38:00+00:00", "sections": [section], "listings": [row]}
    results, asked = _run(monkeypatch, [_item({"Brand": "Nike"}, n=1)], report=report,
                          suggested={"L1": {"rate": 0.09, "basis": "ITEM"}})
    [r] = results
    assert asked == []                                   # covered by the report: not even asked
    assert [f["source"] for f in r["category_fixes"]] == ["report"]   # the report's specifics line wins
    assert "missing: Style, Color" in r["fix"]


# --- the monthly reminder ----------------------------------------------------------------------

def test_reminder_is_due_monthly_and_only_for_an_old_report():
    rd = lambda days: NOW - timedelta(days=days)   # noqa: E731
    text = quality_report.reminder_text(None, None, NOW, 28, 30)
    assert text.startswith("📊 Time to upload a fresh listing quality report") and "no report yet" in text
    assert quality_report.reminder_text(rd(10), None, NOW, 28, 30) is None          # fresh enough
    text = quality_report.reminder_text(rd(28), None, NOW, 28, 30)
    assert "(28 days old); /diagnose stops using it after 30 days." in text
    assert "stopped using it" in quality_report.reminder_text(rd(45), None, NOW, 28, 30)
    assert quality_report.reminder_text(rd(45), rd(5), NOW, 28, 30) is None         # sent 5 days ago
    assert quality_report.reminder_text(rd(60), rd(29), NOW, 28, 30) is not None


class _Bot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text):
        self.sent.append((chat_id, text))


class _App:
    def __init__(self):
        self.bot = _Bot()


def test_reminder_job_sends_once_and_remembers(monkeypatch):
    import asyncio
    monkeypatch.setattr(config, "OWNER_USER_IDS", {42})
    monkeypatch.setattr(db, "latest_quality_report",
                        lambda: {"report_date": "2026-10-01T15:00:00+00:00", "sections": [], "listings": []})
    stamps = {}
    monkeypatch.setattr(db, "last_job_run", lambda job: stamps.get(job))
    monkeypatch.setattr(db, "record_job_run", lambda job, status="ok", detail=None: stamps.__setitem__(job, NOW))
    app = _App()
    asyncio.run(jobs._send_report_reminder(app, now=NOW))
    asyncio.run(jobs._send_report_reminder(app, now=NOW + timedelta(days=1)))   # the next day's sync
    assert len(app.bot.sent) == 1 and app.bot.sent[0][0] == 42
    assert "from 10/01 (39 days old); /diagnose stopped using it" in app.bot.sent[0][1]
    assert stamps == {"quality_report_reminder": NOW}
