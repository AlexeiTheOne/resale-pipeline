"""eBay comps through Apify, and the small stats helpers pricing uses.

What is left of the v1 pricing engine (the old comps ladder, removed 1 Nov 2026,
docs/cleanup-plan.md Phase 4): the Apify client with its backup-token failover,
used by pipeline/pricing_v2.py, and the number helpers v2 and pipeline/msrp.py
share.
"""
import re
import threading
import time

import httpx
import os

import config  # noqa: F401  (loads .env before the tokens are read)

APIFY_TOKEN = os.getenv("APIFY_TOKEN")
# Second key (a second free-tier account), used when APIFY_TOKEN is rejected
# (401/403), out of credit (402), or about to run out (see _sync_token_choice).
# Optional: if unset, call's behavior is identical to before this existed.
APIFY_TOKEN_BACKUP = os.getenv("APIFY_TOKEN_BACKUP")

# Statuses that mean "this account can't run it", where retrying the same token
# is pointless but the other account may work. 402 is Apify's out-of-credit
# answer: both accounts are free tier ($5/month each), and the primary had $1.97
# left on 2026-10-17 while the backup sat unused — before 402 was in this set,
# running dry would have stopped pricing until the monthly reset.
_ACCOUNT_REJECTED = (401, 402, 403)

# Switch to the backup BEFORE the primary hits zero, not just on the 402: the
# sold actor is pay-per-result, and a run that exhausts the credit mid-scrape can
# come back 200 with a short dataset — a thin comp pool that looks like a real
# answer. Checked at most once per APIFY_CREDIT_CHECK_SECONDS.
APIFY_MIN_CREDIT_USD = float(os.getenv("APIFY_MIN_CREDIT_USD", "0.25"))
APIFY_CREDIT_CHECK_SECONDS = int(os.getenv("APIFY_CREDIT_CHECK_SECONDS", "3600"))
LIMITS_URL = "https://api.apify.com/v2/users/me/limits"
_credit_checked_at = 0.0

# Sticky, process-wide: once the primary token is confirmed rejected, every
# later call (v2 makes up to three per pricing run)
# should go straight to the backup instead of paying for a doomed round trip
# on a key already known to be dead. A bool is enough for this codebase's
# usage (pricing_v2 runs up to six fetches in a thread pool) — the
# lock only serializes the check-and-set so the "switched over" notice below
# prints exactly once even if two threads hit the 401/403 at the same moment.
_use_backup_token = False
_token_switch_lock = threading.Lock()

# Mapped by ACTUAL behavior (re-confirmed against the live actors):
#   SOLD   -> oTtB3VgfuE9GtxQt2 , input {"keyword": q, "count": N},
#             fields: soldPrice/totalPrice (strings), title, condition, thumbnailUrl
#             NOTE: this actor ignores "maxProductsPerSearch" and falls back to a
#             default of 100 rows — the size knob here is "count".
# (Active listings come from the eBay Browse API, ebay/browse.py.)
# It takes no a condition filter here: eBay's maps to "New" (1000) only, which
# throws away new-without-tags / open-box comps. Used rows are dropped locally
# instead (is_used), which keeps the pool wide and the filtering visible.
SOLD_URL = "https://api.apify.com/v2/acts/oTtB3VgfuE9GtxQt2/run-sync-get-dataset-items"


def _activate_backup_token():
    """Flip to APIFY_TOKEN_BACKUP for the rest of the process. Idempotent and
    thread-safe: only the caller that actually flips the flag prints the
    notice, so two threads racing can't print it twice."""
    global _use_backup_token
    with _token_switch_lock:
        first = not _use_backup_token
        _use_backup_token = True
    if first:
        print("⚠️ Apify: primary account can't run it (401/402/403) — switching "
              "to APIFY_TOKEN_BACKUP")


def _fetch_remaining(token) -> float | None:
    """USD left this billing cycle on the account behind `token`, or None if it
    can't be read. Never raises: a failed check must not block pricing."""
    try:
        r = httpx.get(LIMITS_URL, headers={"Authorization": f"Bearer {token}"},
                      timeout=20)
        if r.status_code != 200:
            return None
        d = r.json()["data"]
        return float(d["limits"]["maxMonthlyUsageUsd"]) - float(d["current"]["monthlyUsageUsd"])
    except Exception:
        return None


def _sync_token_choice():
    """Pick the account by remaining credit, at most once per
    APIFY_CREDIT_CHECK_SECONDS. Moves to the backup when the primary is under
    APIFY_MIN_CREDIT_USD and the backup isn't, and BACK to the primary once it
    has credit again (the monthly reset) — the plain sticky flag would otherwise
    keep draining the backup until the bot restarted. An unreadable balance
    leaves the current choice alone."""
    global _use_backup_token, _credit_checked_at
    if not APIFY_TOKEN_BACKUP:
        return
    now = time.monotonic()
    with _token_switch_lock:
        if _credit_checked_at and now - _credit_checked_at < APIFY_CREDIT_CHECK_SECONDS:
            return
        _credit_checked_at = now
    primary = _fetch_remaining(APIFY_TOKEN)
    if primary is None:
        return
    if primary >= APIFY_MIN_CREDIT_USD:
        if _use_backup_token:
            print(f"Apify: primary account has ${primary:.2f} again — switching back to it")
        _use_backup_token = False
        return
    backup = _fetch_remaining(APIFY_TOKEN_BACKUP)
    if backup is not None and backup >= APIFY_MIN_CREDIT_USD and not _use_backup_token:
        print(f"⚠️ Apify: primary account down to ${primary:.2f} — switching to "
              f"APIFY_TOKEN_BACKUP (${backup:.2f} left)")
        _use_backup_token = True


def _post(url, payload, token):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return httpx.post(url, headers=headers, json=payload, timeout=180)


def call(url, payload, attempts=3):
    """POST one Apify run-sync scrape, retrying transient failures.

    These are long-held connections (a scrape can run for minutes) and Apify
    drops one now and then — mid-run connection resets and 5xx/429s are routine.
    A single un-retried blip
    would fail the whole pricing run; retrying here keeps a network hiccup from
    costing the item its comps.

    A 401/402/403 is different: THIS account is rejected or out of credit, and retrying it
    unchanged just burns the attempt budget on a call that can't succeed. If
    APIFY_TOKEN_BACKUP is configured, the first 401/402/403 flips the sticky
    module-level flag (see _activate_backup_token) and immediately retries the
    same request with the backup token, in the same attempt — not a fresh one —
    so a rejection on the very last attempt still gets a real shot with the
    backup instead of being swallowed by the loop ending. If the backup is
    ALSO rejected, that's an account-level problem (both keys revoked, or a
    single quota shared by both), not a one-off blip, so it fails immediately
    with a clear combined message rather than retrying either dead token
    twice more. With no backup configured, a 401/402/403 behaves exactly as before
    this existed: raise_for_status() raises, the generic except below catches
    it like any other HTTPStatusError, and it gets retried `attempts` times
    against the same (rejected) primary token before giving up.

    The token goes in an Authorization header, NOT the query string. httpx builds
    HTTPStatusError's message as "... for url '<full url>'", query string
    included, and three separate paths put str(e) into a Telegram message —
    _run_stage's error reply, /pricecheck's, and the unprompted weekly digest.
    A 402 (quota exhausted) or 404 (actor retired) leaves the token perfectly
    valid and pastes it into a chat log that keeps it forever. The same goes
    for the combined-rejection message raised below: it names the env var, not
    the secret, and is built from nothing but the HTTP status code."""
    last = None
    _sync_token_choice()
    for attempt in range(attempts):
        using_backup = _use_backup_token
        token = APIFY_TOKEN_BACKUP if using_backup else APIFY_TOKEN
        try:
            r = _post(url, payload, token)
            if r.status_code in _ACCOUNT_REJECTED and APIFY_TOKEN_BACKUP and not using_backup:
                _activate_backup_token()
                using_backup = True
                r = _post(url, payload, APIFY_TOKEN_BACKUP)
            if r.status_code in _ACCOUNT_REJECTED and using_backup and APIFY_TOKEN_BACKUP:
                raise RuntimeError(
                    "Apify rejected both APIFY_TOKEN and APIFY_TOKEN_BACKUP "
                    f"(HTTP {r.status_code}). Both keys are bad, or the account "
                    "itself is the problem (suspended / out of quota) — swapping "
                    "keys again won't fix it.")
            if r.status_code == 429 or r.status_code >= 500:
                raise httpx.HTTPStatusError(
                    f"Apify returned {r.status_code}", request=r.request, response=r)
            r.raise_for_status()
            data = r.json()
            return data if isinstance(data, list) else []
        except (httpx.TransportError, httpx.HTTPStatusError) as e:
            last = e
            if attempt < attempts - 1:
                time.sleep(2 ** attempt)
    raise last


def to_float(value):
    if value is None:
        return None
    try:
        if isinstance(value, (int, float)):
            return float(value)
        return float(str(value).replace("$", "").replace(",", "").strip())
    except (ValueError, AttributeError):
        return None


def median(vals):
    n = len(vals)
    if not n:
        return None
    mid = n // 2
    return vals[mid] if n % 2 else (vals[mid - 1] + vals[mid]) / 2


def quantile(sorted_vals, q):
    if not sorted_vals:
        return None
    idx = int(round(q * (len(sorted_vals) - 1)))
    return sorted_vals[min(len(sorted_vals) - 1, max(0, idx))]


def dispersion(prices):
    """p90/p10 of the (already trimmed) comp prices — how much the "comparable"
    sales actually disagree. 1.0 is identical; past ~2.5 they aren't the same
    product and their median is not a price."""
    if len(prices) < 2:
        return None
    p10, p90 = quantile(prices, 0.10), quantile(prices, 0.90)
    if not p10:
        return None
    return round(p90 / p10, 2)


_USED_MARKERS = ("used", "pre-owned", "preowned", "pre owned", "refurb")


def is_used(condition):
    """Ross stock is new, so a used comp is the wrong market. Filtered here on
    the row's own condition text rather than via the scrapers' condition
    parameter: that parameter maps to eBay's "New" (condition 1000) alone and
    discards new-without-tags / open-box comps, which is most of the pool for
    apparel."""
    return any(m in (condition or "").lower() for m in _USED_MARKERS)


def upgrade_image(url):
    """Bump eBay thumbnails to a large size (s-l1600). Reject empty/non-ebay urls."""
    if not url or "ebayimg.com" not in url:
        return None
    return re.sub(r"s-l\d+", "s-l1600", url)


def research_resale(research: dict):
    """Best resale figure the identify step found. Prefer its considered
    resale_estimate (it already weighed condition and sold-vs-asking); fall back
    to the median of the eBay prices it cited. Returns None if it has neither."""
    estimate = to_float(research.get("resale_estimate"))
    if estimate is not None and estimate > 0:
        return estimate
    ebay_prices = sorted(
        p for e in (research.get("price_evidence") or [])
        if "ebay" in (e.get("source") or "").lower()
        and (p := to_float(e.get("price"))) is not None and 5 <= p <= 1000
    )
    return median(ebay_prices) if ebay_prices else None
