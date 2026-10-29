"""The two places the bot acts on evidence it might not actually have.

Both are the same class of bug: a missing value read as a real one. The price
check treated an unreadable watch count as "zero watchers" and cut the price;
the auto-confirm gate treated a model-guessed UPC as a decoded barcode and
lowered its own bar. Absent evidence must never be the strongest evidence.

Run with `python tests/test_diagnosis.py` or `pytest tests/`.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from config import (
    AUTO_CONFIRM, AUTO_CONFIRM_MIN_IDENT_CONFIDENCE,
    AUTO_CONFIRM_MIN_IDENT_CONFIDENCE_WITH_UPC, REPRICE_MIN_CTR,
    REPRICE_MIN_IMPRESSIONS, REPRICE_MIN_VIEWS_FOR_SIGNAL, REPRICE_STALE_WEEKS,
)
from pipeline.reprice import _diagnose

# Traffic good enough to clear the visibility checks, so every test below turns
# purely on the watcher signal — which is the variable under test. CTR is derived
# from these two integers now, so impressions/views have to produce a passing
# ratio rather than a separate "ctr" key being taken on trust.
SEEN = {"impressions": 1000, "views": 40}
ENOUGH = REPRICE_MIN_VIEWS_FOR_SIGNAL * 2      # cumulative views, all-time
OLD = REPRICE_STALE_WEEKS + 1


def test_ctr_is_derived_not_taken_from_ebay():
    """eBay's CLICK_THROUGH_RATE field disagrees with the impressions and views
    it returns beside it, and trusting it flagged listings at 3-4.6% CTR as
    'seen but not clicked'. A supplied ctr key must now be ignored."""
    from pipeline.reprice import _ctr

    assert abs(_ctr({"impressions": 1000, "views": 40}) - 0.04) < 1e-9
    assert _ctr({"impressions": 1000, "views": 40, "ctr": 0.0}) == 0.04
    assert _ctr({"impressions": 0, "views": 0}) == 0.0   # no divide-by-zero


def test_an_unreadable_watch_count_is_not_zero_watchers():
    """get_watch_count returns None when the call fails. Falling through used to
    land on HEALTHY and then escalate to STALE on age alone — a price cut on a
    listing that may have had a queue of watchers on it."""
    assert _diagnose(SEEN, None, None, OLD, ENOUGH) == "NO_WATCH_DATA"
    assert _diagnose(SEEN, None, None, 2, ENOUGH) == "NO_WATCH_DATA"


def test_a_real_zero_still_reads_as_overpriced():
    """The fix must not blunt the actual signal: eBay reporting zero watchers
    against real views is the evidence OVERPRICED exists for."""
    assert _diagnose(SEEN, 0, None, 2, ENOUGH) == "OVERPRICED"


def test_overpriced_needs_cumulative_views_not_one_good_week():
    """The threshold is a running total. Under it, a listing with no watchers is
    simply unseen — cutting its price is acting on absent evidence."""
    thin = REPRICE_MIN_VIEWS_FOR_SIGNAL - 1
    assert _diagnose(SEEN, 0, None, 2, thin) == "HEALTHY"
    assert _diagnose(SEEN, 0, None, OLD, thin) == "STALE_UNSEEN"
    assert _diagnose(SEEN, 0, None, OLD, ENOUGH) == "OVERPRICED"


def test_watchers_still_beat_a_price_cut():
    assert _diagnose(SEEN, 3, None, OLD, ENOUGH) == "SEND_OFFERS"


def test_visibility_verdicts_are_reached_before_the_watcher_check():
    """A listing nobody is finding is diagnosed on its impressions, whether or not
    the watch count came back — those verdicts never carry a price anyway."""
    invisible = {"impressions": 1, "views": 0}
    assert _diagnose(invisible, None, None, OLD, 0) == "INVISIBLE"
    # Real impressions, almost no clicks: 1/4000 is far below any plausible bar.
    unclicked = {"impressions": 4000, "views": 1}
    assert _diagnose(unclicked, None, None, OLD, ENOUGH) == "LOW_CTR"


def test_a_healthy_ctr_is_not_flagged():
    """The regression that started this: 3-4.6% CTR read as 'seen but not
    clicked' because eBay's own field was trusted over its own numbers."""
    for views, impressions in ((34, 1000), (36, 1000), (46, 1000)):
        assert _diagnose({"impressions": impressions, "views": views},
                         0, None, 2, ENOUGH) == "OVERPRICED", (views, impressions)


def test_only_a_decoded_upc_lowers_the_auto_confirm_bar():
    """config.py promises the lower bar applies when the UPC "was decoded off the
    photos". identify.py only overwrites `upc` when pyzbar actually read one —
    otherwise it holds whatever digits the vision model thought it saw, which is
    the least reliable field it produces."""
    import telegram_bot as tb

    if not AUTO_CONFIRM:
        return  # the gate is off entirely; nothing to assert
    assert AUTO_CONFIRM_MIN_IDENT_CONFIDENCE_WITH_UPC < AUTO_CONFIRM_MIN_IDENT_CONFIDENCE

    # Confidence that clears the UPC bar but not the plain one — the whole
    # disputed range.
    between = (AUTO_CONFIRM_MIN_IDENT_CONFIDENCE
               + AUTO_CONFIRM_MIN_IDENT_CONFIDENCE_WITH_UPC) / 2
    base = {"brand": "Bebe", "product_name": "Watch Band", "confidence": between}

    assert tb._identify_gate_reason({**base, "upc": "198928675698",
                                     "upc_decoded": True}) is None
    assert tb._identify_gate_reason({**base, "upc": "198928675698",
                                     "upc_decoded": False}) is not None
    # Legacy items predate the flag; absence must read as "not decoded".
    assert tb._identify_gate_reason({**base, "upc": "198928675698"}) is not None


# --- a failed traffic fetch is not zero traffic ------------------------------
#
# The same bug again, one signal over: _traffic_for_all swallowed a failed batch,
# the listings in it read as 0 impressions, and _diagnose called them INVISIBLE.
# 2026-08-17: every row 0 impressions, 71 INVISIBLE. 2026-10-05: 97 INVISIBLE.
# Watch counts were fine both weeks — only the traffic fetch had failed.

def test_missing_traffic_is_not_invisible():
    assert _diagnose(None, 3, None, OLD, ENOUGH) == "NO_TRAFFIC_DATA"
    assert _diagnose(None, None, None, OLD, ENOUGH) == "NO_TRAFFIC_DATA"
    # Too-new is decided on age alone and still wins.
    assert _diagnose(None, 0, None, 0, 0) == "TOO_NEW"


def test_a_genuine_zero_is_still_invisible():
    """eBay omits listings it had no traffic for from a successful report; that
    really is zero, and must stay INVISIBLE."""
    assert _diagnose({"impressions": 0, "views": 0}, 0, None, OLD, 0) == "INVISIBLE"


class _Patch:
    """Swap module attributes for the duration of a with-block (no pytest
    fixtures, so the file still runs as a plain script)."""
    def __init__(self, module, **attrs):
        self.module, self.attrs, self.saved = module, attrs, {}

    def __enter__(self):
        for k, v in self.attrs.items():
            self.saved[k] = getattr(self.module, k)
            setattr(self.module, k, v)

    def __exit__(self, *exc):
        for k, v in self.saved.items():
            setattr(self.module, k, v)


def _live(listing_id, item_id=None):
    return {"item_id": item_id or f"item-{listing_id}", "status": "published",
            "created_at": "2025-01-01T00:00:00+00:00",
            "ebay": {"listing_id": listing_id}, "listing": {"price": 20.0}}


def _flaky_report(bad: set):
    """Fails any batch containing a `bad` id; otherwise reports traffic for every
    id except those ending in '0' (eBay omitting a zero-traffic listing)."""
    def report(ids):
        if bad & set(ids):
            raise RuntimeError("eBay Analytics API traffic_report failed [429]")
        return {i: {"impressions": 500, "views": 20, "ctr": 0.0}
                for i in ids if not i.endswith("0")}
    return report


def test_a_failed_batch_maps_to_none_not_absent():
    import pipeline.reprice as rp

    items = [_live(str(100 + n)) for n in range(5)]   # ids 100..104
    with _Patch(rp, get_traffic_report=_flaky_report({"103"})):
        traffic, health = rp._traffic_for_all(items, batch=2)
    # Batches: [100,101] ok, [102,103] failed, [104] ok.
    assert health == {"batches": 3, "failed": 1}
    assert "100" not in traffic                        # genuine zero: omitted
    assert traffic["101"]["impressions"] == 500
    assert traffic["102"] is None and traffic["103"] is None
    assert traffic["104"]["impressions"] == 500


def _temp_db():
    import tempfile

    import db
    path = str(Path(tempfile.mkdtemp(prefix="diag_")) / "test.db")
    return db, path


def test_failed_traffic_is_stored_as_null_not_zero():
    """listing_stats is history later weeks sum over. A failed fetch must leave
    NULL impressions/views there, while the watch count that DID arrive is kept."""
    import pipeline.reprice as rp
    db, path = _temp_db()
    with _Patch(db, DB_PATH=path):
        db.create_tables()
        with _Patch(rp, get_watch_count=lambda _id: 2):
            result = rp.evaluate_item(_live("555", "item-555"), {"555": None})
        row = db.latest_stats("item-555")
    assert result["verdict"] == "NO_TRAFFIC_DATA"
    assert result["impressions"] is None and result["views"] is None
    assert result["suggested_price"] is None
    assert row["impressions"] is None and row["views"] is None
    assert row["watchers"] == 2 and row["verdict"] == "NO_TRAFFIC_DATA"


def test_null_traffic_history_does_not_break_cumulative_views():
    import pipeline.reprice as rp
    db, path = _temp_db()
    with _Patch(db, DB_PATH=path):
        db.create_tables()
        db.record_listing_stats("item-777", "2026-08-17", impressions=None,
                                views=None, watchers=1, verdict="NO_TRAFFIC_DATA")
        db.record_listing_stats("item-777", "2026-08-10", impressions=300,
                                views=12, watchers=1, verdict="HEALTHY")
        with _Patch(rp, get_watch_count=lambda _id: 1):
            result = rp.evaluate_item(
                _live("777", "item-777"),
                {"777": {"impressions": 400, "views": 8, "ctr": 0.0}})
    assert result["cumulative_views"] == 20


def test_run_reports_how_much_data_arrived():
    import pipeline.reprice as rp
    db, path = _temp_db()
    items = [_live(str(200 + n)) for n in range(25)]   # 2 batches of 20
    watch = lambda lid: None if lid == "201" else 1
    with _Patch(db, DB_PATH=path):
        db.create_tables()
        with _Patch(rp, list_items=lambda s: items if s == "published" else [],
                    get_traffic_report=_flaky_report({"224"}),
                    get_watch_count=watch, eligible_listing_ids=lambda: set()):
            results, health = rp.run_weekly_check()
    assert {k: health[k] for k in ("traffic_batches", "traffic_failed",
                                   "watch_lookups", "watch_failed")} == {
        "traffic_batches": 2, "traffic_failed": 1, "watch_lookups": 25, "watch_failed": 1}
    assert health["offers"]["rows"] == [] and health["pricing_calls"] == 0
    verdicts = [r["verdict"] for r in results]
    assert verdicts.count("NO_TRAFFIC_DATA") == 5        # the failed batch of 5
    assert "INVISIBLE" in verdicts                       # 200/210: genuine zeros


def test_digest_line_shows_unknown_traffic_as_question_marks():
    import telegram_bot as tb
    line = tb._format_reprice_line({
        "item_id": "abcdef0123", "verdict": "NO_TRAFFIC_DATA", "impressions": None,
        "views": None, "cumulative_views": 12, "ctr": None, "watchers": 2,
        "current_price": 20.0, "suggested_price": None})
    assert "? impr" in line and "? views" in line and "None" not in line


if __name__ == "__main__":
    tests =[v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"  FAIL  {t.__name__}\n          {e}")
        except Exception as e:
            failed += 1
            print(f" ERROR  {t.__name__}\n          {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
