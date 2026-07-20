"""Shared resilience wrapper for Gemini calls.

Grounded (Google Search) requests on the free tier have a small quota, and the
API also returns transient 500/503s. A raw 429 otherwise crashes the listing
pipeline mid-item. generate_with_retry retries those, honoring the server's
suggested retryDelay when present, and re-raises anything non-retryable or once
the retry budget is spent so the caller can fail the item gracefully.
"""
import random
import re
import time

import httpx
from google import genai
from google.genai import errors, types

# All transient server-side failures. 502/504 matter as much as 503: a grounded
# research call (images + search + thinking) is slow enough to hit Google's
# deadline, and 504 DEADLINE_EXCEEDED was previously treated as fatal — it failed
# the whole identify stage on the first try instead of retrying a timeout that
# usually succeeds on a second attempt.
RETRYABLE_CODES = {429, 500, 502, 503, 504}
# 503 "overloaded" is Google-side capacity exhaustion — it hits paid keys too
# during peak load and can persist for a while. 3 retries (~2/4/8s) often isn't
# enough to ride out a sustained overload, so give it more shots with backoff.
MAX_RETRIES = 5
MAX_DELAY = 60.0

REQUEST_TIMEOUT_MS = 180_000


def make_client(api_key: str | None = None) -> "genai.Client":
    """Build the Gemini client used across the pipeline.

    Two settings matter for a long-running bot:
    - timeout: a stalled request can't hang the pipeline forever.
    - no HTTP keep-alive: pooled sockets go stale while the process sits idle
      between items; reusing a dead one hangs, and the SDK retries connection
      errors, so the hang stretches to minutes. A fresh connection per call (we
      make only a few per item) sidesteps that entirely.
    """
    return genai.Client(
        api_key=api_key,
        http_options=types.HttpOptions(
            timeout=REQUEST_TIMEOUT_MS,
            client_args={"limits": httpx.Limits(max_keepalive_connections=0)},
        ),
    )


def _code_of(exc: "errors.APIError"):
    return getattr(exc, "code", None) or getattr(exc, "status_code", None)


def _is_retryable(exc: "errors.APIError") -> bool:
    code = _code_of(exc)
    if code in RETRYABLE_CODES:
        return True
    # Fallback for errors whose code attribute isn't populated — match on the
    # status names, which are unambiguous (a bare "504" could appear in unrelated
    # numbers, but DEADLINE_EXCEEDED can't).
    text = str(exc)
    return any(s in text for s in (
        "RESOURCE_EXHAUSTED", "429", "503", "UNAVAILABLE", "DEADLINE_EXCEEDED", "INTERNAL"))


def _suggested_delay(exc: "errors.APIError") -> float | None:
    """Pull the server-suggested wait out of the error, e.g. 'retryDelay': '30s'
    or 'Please retry in 30.69s'. Returns None if it isn't stated."""
    text = str(exc)
    for pattern in (r"retry in ([\d.]+)s", r"retryDelay['\"]?:\s*['\"]?([\d.]+)s"):
        m = re.search(pattern, text)
        if m:
            return float(m.group(1))
    return None


# Transient glitches and timeouts. Waiting doesn't help these — the request just
# died or took too long — so retry quickly instead of serving a long backoff.
FAST_RETRY_CODES = {500, 502, 504}
FAST_RETRY_MAX = 6.0


def _jitter(base: float) -> float:
    """Equal jitter: keep half the computed backoff as a floor, randomize the
    other half. This de-correlates retries across items running concurrently
    (MAX_CONCURRENT_LISTINGS) — without it, a batch that all 503'd together backs
    off in lockstep and re-hits the same overloaded capacity window as a batch,
    so every attempt fails at once. Spreading them lets some land in a freer
    window."""
    half = base / 2
    return half + random.uniform(0, half)


def _delay_for(exc: "errors.APIError", attempt: int) -> float:
    """How long to wait before the next attempt, matched to WHY it failed.

    A server-suggested delay always wins (429s carry one). Otherwise: 429/503
    mean "you're rate-limited / we're out of capacity", which genuinely needs an
    escalating wait; 500/502/504 are one-off glitches or a deadline overrun,
    where sleeping 30s accomplishes nothing but making the user wait. All paths
    are jittered so concurrent items don't retry in lockstep.
    """
    suggested = _suggested_delay(exc)
    if suggested is not None:
        # Honor the server's ask as a floor, then add a little jitter ON TOP
        # (never below it) so clients handed the same retryDelay don't resume
        # simultaneously.
        return min(suggested + random.uniform(0.5, 2.5), MAX_DELAY)
    if _code_of(exc) in FAST_RETRY_CODES:
        return _jitter(min(1.5 * attempt, FAST_RETRY_MAX))
    return _jitter(min(2 ** attempt + 0.5, MAX_DELAY))


def error_code(exc: "errors.APIError"):
    """Public accessor for an API error's HTTP/status code (or None)."""
    return _code_of(exc)


def describe_error(exc: "errors.APIError") -> str:
    """Compact, INFORMATIVE one-liner for an API error: code + status +
    Google's own message (e.g. '503 UNAVAILABLE: The model is overloaded' vs
    '504 DEADLINE_EXCEEDED: ...'). We were previously logging only the numeric
    code, which threw away the one field that says WHY it failed — overload vs a
    deadline overrun vs a bad request all look identical as a bare '503'."""
    code = _code_of(exc) or "?"
    status = getattr(exc, "status", None) or ""
    message = getattr(exc, "message", None)
    head = f"{code} {status}".strip()
    if message:
        return f"{head}: {message}"[:400]
    # message not populated (some transport errors) — fall back to the full repr.
    return f"{head} {exc}".strip()[:400]


def generate_with_retry(client, *, max_total_seconds: float | None = None, **kwargs):
    """client.models.generate_content(**kwargs) with backoff on rate limits and
    transient server errors.

    max_total_seconds caps the wall-clock spent retrying. It exists for the slow
    grounded research call, which can sit at Google's ~180s deadline before 504'ing:
    once we've already spent this long across attempts, stop retrying and re-raise
    so the caller can fall back gracefully instead of burning several more
    multi-minute attempts on a request that clearly can't finish in time. Left
    None (the default) for the fast calls, whose retries are cheap.
    """
    attempt = 0
    started = time.monotonic()
    while True:
        call_start = time.monotonic()
        try:
            return client.models.generate_content(**kwargs)
        except errors.APIError as exc:
            took = time.monotonic() - call_start
            elapsed = time.monotonic() - started
            over_budget = max_total_seconds is not None and elapsed >= max_total_seconds
            if not _is_retryable(exc) or attempt >= MAX_RETRIES or over_budget:
                # Always log the REAL reason on the final failure — this is the
                # line that tells us overload vs deadline vs bad-request.
                reason = ("time budget spent" if over_budget
                          else "not retryable" if not _is_retryable(exc)
                          else "retries exhausted")
                print(f"Gemini giving up ({reason}) after {took:.0f}s this call / "
                      f"{elapsed:.0f}s total: {describe_error(exc)}")
                raise
            attempt += 1
            delay = _delay_for(exc, attempt)
            # Report the failed call's duration AND Google's message: most of the
            # wall-clock between retries is the request dying (a 504 sits for ages
            # before Google gives up), not our sleep — and the message distinguishes
            # a genuine overload from a self-inflicted deadline overrun.
            print(f"Gemini {describe_error(exc)} — after {took:.0f}s, retrying in "
                  f"{delay:.0f}s (attempt {attempt}/{MAX_RETRIES}, {elapsed:.0f}s elapsed)")
            time.sleep(delay)


def response_text(response, stage: str) -> str:
    """Return the model's text, or raise with the finish reason when it produced
    none. gemini-2.5-pro is a thinking model and thinking tokens count against
    max_output_tokens, so an exhausted budget yields finish_reason=MAX_TOKENS and
    a null .text — this surfaces that instead of a bare 'None' downstream."""
    text = (response.text or "").strip()
    if text:
        return text
    reason = None
    if response.candidates:
        reason = getattr(response.candidates[0], "finish_reason", None)
    raise RuntimeError(
        f"Gemini {stage} returned no text (finish_reason={reason}). "
        "If MAX_TOKENS, raise max_output_tokens — thinking tokens count against it."
    )
