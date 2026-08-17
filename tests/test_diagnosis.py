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


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
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
