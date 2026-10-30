"""Catch-up scheduling for the bot's background jobs.

The weekly reprice loop used to compute its next slot from `now` alone, so a
process that wasn't alive at the slot skipped that week silently and waited a
full period -- which is why the 2026-08-03 digest never ran and nothing recorded
that it hadn't. The same gap left /sync manual-only, so every money figure built
on the DB understated itself for as long as a sync was overdue.

The fix is db.job_runs plus the three slot helpers exercised here. What matters:
a slot that passed while the bot was down is detected as missed, a slot already
served is NOT re-run (no duplicate digests on every restart), and a run that is
uselessly late is dropped rather than fired at startup as noise.

No Telegram or eBay calls are made -- the helpers are pure functions of the clock
plus the job_runs table, and the table is pointed at a temp DB before
telegram_bot is imported, so the real data/ross.db is never touched.

Run with `python tests/test_job_schedule.py` or `pytest tests/`.
"""
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

# Redirect the DB before importing anything that writes to it. db._conn() reads
# DB_PATH at call time, so telegram_bot's by-name imports of record_job_run /
# last_job_run follow this too.
_TMPDIR = tempfile.mkdtemp(prefix="jobsched_")
import db  # noqa: E402

db.DB_PATH = str(Path(_TMPDIR) / "test.db")
db.create_tables()

import telegram_bot as tb  # noqa: E402

MONDAY = 0


def utc(y, m, d, h=0, mi=0):
    return datetime(y, m, d, h, mi, tzinfo=timezone.utc)


def _stamp(job, when):
    """Force a job's last-completed time, bypassing record_job_run's own clock."""
    with db._conn() as con:
        con.execute(
            "INSERT INTO job_runs (job, last_run_at) VALUES (?, ?) "
            "ON CONFLICT(job) DO UPDATE SET last_run_at=excluded.last_run_at",
            (job, when if isinstance(when, str) else when.isoformat()),
        )


# --- slot arithmetic -------------------------------------------------------

def test_previous_slot_daily():
    # 2026-09-05 is a Saturday; daily slot at 11:00 UTC.
    assert tb._previous_slot(utc(2026, 9, 5, 9, 30), 11) == utc(2026, 9, 4, 11)
    assert tb._previous_slot(utc(2026, 9, 5, 12, 0), 11) == utc(2026, 9, 5, 11)
    # Exactly on the slot counts as served, not pending.
    assert tb._previous_slot(utc(2026, 9, 5, 11, 0), 11) == utc(2026, 9, 5, 11)


def test_next_slot_daily():
    assert tb._next_slot(utc(2026, 9, 5, 12, 0), 11) == utc(2026, 9, 6, 11)
    assert tb._next_slot(utc(2026, 9, 5, 9, 30), 11) == utc(2026, 9, 5, 11)


def test_previous_slot_weekly():
    # Monday 14:00 UTC.
    assert tb._previous_slot(utc(2026, 9, 5, 9), 14, MONDAY) == utc(2026, 8, 31, 14)
    # On the day but before the hour -> the PREVIOUS Monday, not today's.
    assert tb._previous_slot(utc(2026, 8, 31, 9), 14, MONDAY) == utc(2026, 8, 24, 14)
    assert tb._previous_slot(utc(2026, 8, 31, 15), 14, MONDAY) == utc(2026, 8, 31, 14)


def test_next_slot_weekly():
    assert tb._next_slot(utc(2026, 8, 31, 15), 14, MONDAY) == utc(2026, 9, 7, 14)


def test_slots_are_one_period_apart():
    for now in (utc(2026, 9, 5, 3), utc(2026, 9, 5, 11), utc(2026, 9, 5, 23, 59)):
        assert tb._next_slot(now, 11) - tb._previous_slot(now, 11) == timedelta(days=1)
        assert (tb._next_slot(now, 14, MONDAY)
                - tb._previous_slot(now, 14, MONDAY)) == timedelta(days=7)


# --- the catch-up decision -------------------------------------------------

NOW = utc(2026, 9, 5, 12, 0)   # Saturday, just after the 11:00 daily slot


def test_never_run_catches_up():
    assert tb._missed_slot("job_never", NOW, 11) is True


def test_ran_before_the_slot_catches_up():
    _stamp("job_before", utc(2026, 9, 5, 3, 0))
    assert tb._missed_slot("job_before", NOW, 11) is True


def test_already_served_does_not_repeat():
    # The restart case: without this the bot would re-post a digest every launch.
    _stamp("job_served", utc(2026, 9, 5, 11, 30))
    assert tb._missed_slot("job_served", NOW, 11) is False


def test_naive_timestamp_is_read_as_utc():
    _stamp("job_naive", "2026-09-05T11:30:00")
    assert tb._missed_slot("job_naive", NOW, 11) is False


def test_corrupt_timestamp_is_treated_as_never_run():
    _stamp("job_corrupt", "garbage")
    assert tb._missed_slot("job_corrupt", NOW, 11) is True


def test_grace_bound_drops_a_uselessly_late_run():
    # Sunday; the last Monday slot was 6 days ago, well past the 48h grace.
    assert tb._missed_slot("job_stale", utc(2026, 9, 6, 12), 14, MONDAY) is False


def test_grace_bound_allows_a_fresh_miss():
    assert tb._missed_slot("job_stale", utc(2026, 8, 31, 15), 14, MONDAY) is True


# --- the job_runs table ----------------------------------------------------

def test_record_and_read_back():
    db.record_job_run("roundtrip", detail="7 listing(s)")
    got = db.last_job_run("roundtrip")
    assert got is not None and got.tzinfo is not None


def test_unknown_job_reads_as_never_run():
    assert db.last_job_run("no_such_job") is None


def test_record_job_run_is_an_upsert():
    db.record_job_run("upsert")
    first = db.last_job_run("upsert")
    db.record_job_run("upsert")
    assert db.last_job_run("upsert") >= first
    with db._conn() as con:
        rows = con.execute(
            "SELECT COUNT(*) FROM job_runs WHERE job = 'upsert'").fetchone()[0]
    assert rows == 1


# --- a partial run is not a completed run ----------------------------------
#
# The weekly price check used to be stamped ok whenever run_weekly_check
# returned, even when the traffic fetch had failed and the digest was 97
# listings of "invisible" read off missing data (2026-10-05). A run with failed
# traffic batches is now stamped "partial", which the catch-up logic doesn't
# count as having served the slot.

def test_partial_run_does_not_count_as_completed():
    db.record_job_run("job_partial", status="partial", detail="1/6 failed")
    assert db.job_run_status("job_partial") == "partial"
    assert db.last_job_run("job_partial") is None
    assert tb._missed_slot("job_partial", NOW, 11) is True
    db.record_job_run("job_partial")
    assert db.job_run_status("job_partial") == "ok"
    assert db.last_job_run("job_partial") is not None


def test_legacy_null_status_still_counts_as_completed():
    _stamp("job_legacy", utc(2026, 9, 5, 11, 30))     # status left NULL
    assert tb._missed_slot("job_legacy", NOW, 11) is False


class _FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, reply_markup=None):
        self.sent.append(text)


class _FakeApp:
    def __init__(self):
        self.bot = _FakeBot()


def _run_job_with(results, health):
    import asyncio

    import pipeline.reprice as rp
    from bot import util

    saved = (rp.run_weekly_check, util.ALLOWED_USER_IDS)
    rp.run_weekly_check = lambda: (results, health)
    util.ALLOWED_USER_IDS = [1]
    try:
        app = _FakeApp()
        asyncio.run(tb._run_reprice_job(app))
        return app.bot.sent
    finally:
        rp.run_weekly_check, util.ALLOWED_USER_IDS = saved


_NO_TRAFFIC = {"item_id": "x" * 36, "verdict": "NO_TRAFFIC_DATA", "impressions": None,
               "views": None, "cumulative_views": 0, "ctr": None, "watchers": 1,
               "current_price": 20.0, "suggested_price": None}


def test_reprice_job_with_failed_traffic_is_stamped_partial():
    sent = _run_job_with([_NO_TRAFFIC] * 3, {"traffic_batches": 6, "traffic_failed": 6,
                                            "watch_lookups": 3, "watch_failed": 0})
    assert db.job_run_status(tb._JOB_REPRICE) == "partial"
    assert db.last_job_run(tb._JOB_REPRICE) is None     # will be caught up
    header = sent[0]
    assert "6/6 traffic batch(es) failed" in header
    assert "watch count missing for 0/3" in header
    assert "3 not judged" in header and "Incomplete" in header
    # Not-judged listings are counted, not posted one message each.
    assert len(sent) == 1


def test_reprice_job_with_full_data_is_stamped_ok():
    sent = _run_job_with([dict(_NO_TRAFFIC, verdict="HEALTHY", impressions=500, views=20)],
                         {"traffic_batches": 1, "traffic_failed": 0,
                          "watch_lookups": 1, "watch_failed": 0})
    assert db.job_run_status(tb._JOB_REPRICE) == "ok"
    assert db.last_job_run(tb._JOB_REPRICE) is not None
    assert "0/1 traffic batch(es) failed" in sent[0]
    assert "Incomplete" not in sent[0]


# --- /sync argument parsing ------------------------------------------------

def test_sync_days_falls_back_to_the_configured_default():
    assert tb._sync_days(None) == tb.SYNC_DAYS
    assert tb._sync_days([]) == tb.SYNC_DAYS
    # A junk argument must not crash the command or silently mean "1 day".
    assert tb._sync_days(["abc"]) == tb.SYNC_DAYS


def test_sync_days_accepts_an_explicit_window():
    assert tb._sync_days(["30"]) == 30
    assert tb._sync_days(["0"]) == 1      # clamped, never a zero-day look-back


# --- chunking for unprompted messages --------------------------------------

def test_chunk_lines_respects_the_telegram_cap():
    chunks = tb._chunk_lines(["x" * 200] * 60)
    assert len(chunks) > 1
    assert all(len(c) <= tb.TELEGRAM_MAX_CHARS for c in chunks)


def test_chunk_lines_preserves_every_line():
    lines = [f"line {i}" for i in range(500)]
    rejoined = "\n".join(tb._chunk_lines(lines)).split("\n")
    assert rejoined == lines


if __name__ == "__main__":
    fails = 0
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_") or not callable(fn):
            continue
        try:
            fn()
            print(f"  ok   {name}")
        except AssertionError as e:
            fails += 1
            print(f"  FAIL {name}: {e}")
    print()
    if fails:
        print(f"{fails} FAILURE(S)")
        sys.exit(1)
    print("all job-schedule checks passed")
