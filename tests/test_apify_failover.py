"""APIFY_TOKEN_BACKUP failover in pipeline/price.py's _call.

The primary Apify token started returning 403 in production. The fix: on a
401/403, retry with APIFY_TOKEN_BACKUP, stick to it for the rest of the
process once it's proven necessary (the query ladder can make a dozen calls
in one pricing run), and if the backup is rejected too, fail with one clear
message instead of the raw httpx error. The one constraint that matters more
than the feature working at all: neither token value may ever show up in a
print, a log line, or anything that reaches a raised exception's text — three
separate paths format str(e) straight into a Telegram message.

No real Apify calls are made. pipeline.price._post — the seam between _call's
retry/failover logic and the actual network request — is monkeypatched, the
same way tests/test_images.py stubs ebay.inventory._request. Fake responses
are built with real httpx.Response/httpx.Request objects (constructing those
performs no I/O) so raise_for_status() and its message-building run for real;
that's what actually proves the token can't leak through httpx's own
exception formatting.

Run with `python tests/test_apify_failover.py` or `pytest tests/`.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import httpx

import pipeline.price as price

PRIMARY = "primary-tok-AAAAAAAAAAAAAAAAAAAAAAAA"
BACKUP = "backup-tok-BBBBBBBBBBBBBBBBBBBBBBBBBB"
URL = "https://api.apify.com/v2/acts/fake/run-sync-get-dataset-items"


def _reset(primary=PRIMARY, backup=None):
    """Every test starts from a clean slate: _use_backup_token is a sticky
    module global by design (that's the point of it), so left alone it would
    leak state between tests and make results depend on execution order."""
    price.APIFY_TOKEN = primary
    price.APIFY_TOKEN_BACKUP = backup
    price._use_backup_token = False
    price.time.sleep = lambda _seconds: None  # keep retry backoff out of test time


def _fake_post(status_by_token, ok_payload=None):
    """Stub for price._post(url, payload, token). Returns a REAL httpx.Response
    built from the token's scripted status, and records every token used.

    The fake httpx.Request carries the real Authorization header production
    would send (see price._post), not a bare URL — so the leak-text assertions
    prove httpx's own exception formatting omits headers, rather than merely
    proving the stub never had the token to begin with."""
    calls = []

    def fake(url, payload, token):
        calls.append(token)
        status = status_by_token(token)
        body = ok_payload if status == 200 else None
        req = httpx.Request("POST", url, headers={"Authorization": f"Bearer {token}"},
                             json=payload)
        return httpx.Response(status, request=req, json=body)

    return fake, calls


def test_primary_403_falls_back_to_backup_and_succeeds():
    _reset(backup=BACKUP)
    fake, calls = _fake_post(
        lambda tok: 200 if tok == BACKUP else 403,
        ok_payload=[{"price": "19.99"}])
    price._post = fake

    result = price._call(URL, {"keyword": "x", "count": 3})

    assert result == [{"price": "19.99"}]
    assert calls == [PRIMARY, BACKUP], "should try primary once, then fall back"
    assert price._use_backup_token is True, "the switch must stick for later calls"


def test_backup_also_rejected_raises_one_clear_combined_error():
    _reset(backup=BACKUP)
    fake, calls = _fake_post(lambda tok: 403)
    price._post = fake

    try:
        price._call(URL, {"keyword": "x", "count": 3})
        raise AssertionError("expected a RuntimeError")
    except RuntimeError as e:
        msg = str(e)
        assert "APIFY_TOKEN" in msg and "APIFY_TOKEN_BACKUP" in msg
        assert PRIMARY not in msg and BACKUP not in msg
    # Exactly one shot at each token — no reason to retry either dead key 3x.
    assert calls == [PRIMARY, BACKUP], calls


def test_sticky_flag_skips_the_dead_primary_on_the_next_call():
    """Once a prior call has proven the primary is rejected, a fresh _call
    should never spend a round trip on it again — it should go straight to
    the backup."""
    _reset(backup=BACKUP)
    price._use_backup_token = True  # simulate an earlier call having switched

    fake, calls = _fake_post(
        lambda tok: 200 if tok == BACKUP else 403,
        ok_payload=[{"price": "5.00"}])
    price._post = fake

    result = price._call(URL, {"keyword": "x", "count": 3})

    assert result == [{"price": "5.00"}]
    assert calls == [BACKUP], "must not touch the primary once already switched"


def test_no_backup_configured_behaves_exactly_as_before():
    """With APIFY_TOKEN_BACKUP unset, a 403 must retry the SAME (only) token
    up to `attempts` times and then raise the plain httpx error — unchanged
    from before this feature existed."""
    _reset(backup=None)
    fake, calls = _fake_post(lambda tok: 403)
    price._post = fake

    try:
        price._call(URL, {"keyword": "x", "count": 3}, attempts=3)
        raise AssertionError("expected an httpx.HTTPStatusError")
    except RuntimeError:
        raise AssertionError("must not take the failover path with no backup configured")
    except httpx.HTTPStatusError as e:
        assert calls == [PRIMARY, PRIMARY, PRIMARY], calls
        assert PRIMARY not in str(e)


def test_no_token_ever_appears_in_the_raised_error_text():
    """Belt-and-braces sweep: whatever _call raises, in every branch, the raw
    token values must not be substrings of it — this is the one requirement
    that matters more than the feature working."""
    _reset(backup=BACKUP)
    fake, _ = _fake_post(lambda tok: 403)
    price._post = fake
    try:
        price._call(URL, {"keyword": "x", "count": 3})
    except Exception as e:
        assert PRIMARY not in str(e), "primary token leaked into an exception"
        assert BACKUP not in str(e), "backup token leaked into an exception"
    else:
        raise AssertionError("expected an exception")

    _reset(backup=None)
    fake, _ = _fake_post(lambda tok: 403)
    price._post = fake
    try:
        price._call(URL, {"keyword": "x", "count": 3}, attempts=1)
    except Exception as e:
        assert PRIMARY not in str(e), "primary token leaked into an exception"
    else:
        raise AssertionError("expected an exception")


def test_failover_notice_is_printed_without_the_token():
    """Requirement: a visible one-line notice when failover triggers, and it
    must not contain the token."""
    import contextlib
    import io

    _reset(backup=BACKUP)
    fake, _ = _fake_post(
        lambda tok: 200 if tok == BACKUP else 403,
        ok_payload=[{"price": "1.00"}])
    price._post = fake

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        price._call(URL, {"keyword": "x", "count": 3})
    printed = buf.getvalue()

    assert printed.strip(), "failover should print something visible"
    assert PRIMARY not in printed and BACKUP not in printed
    assert "backup" in printed.lower() or "APIFY_TOKEN_BACKUP" in printed


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
