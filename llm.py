"""Shared resilience wrapper for Gemini calls.

Grounded (Google Search) requests on the free tier have a small quota, and the
API also returns transient 500/503s. A raw 429 otherwise crashes the listing
pipeline mid-item. generate_with_retry retries those, honoring the server's
suggested retryDelay when present, and re-raises anything non-retryable or once
the retry budget is spent so the caller can fail the item gracefully.
"""
import re
import time

import httpx
from google import genai
from google.genai import errors, types

RETRYABLE_CODES = {429, 500, 503}
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


def _is_retryable(exc: "errors.APIError") -> bool:
    code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
    if code in RETRYABLE_CODES:
        return True
    text = str(exc)
    return any(s in text for s in ("RESOURCE_EXHAUSTED", "429", "503", "UNAVAILABLE"))


def _suggested_delay(exc: "errors.APIError") -> float | None:
    """Pull the server-suggested wait out of the error, e.g. 'retryDelay': '30s'
    or 'Please retry in 30.69s'. Returns None if it isn't stated."""
    text = str(exc)
    for pattern in (r"retry in ([\d.]+)s", r"retryDelay['\"]?:\s*['\"]?([\d.]+)s"):
        m = re.search(pattern, text)
        if m:
            return float(m.group(1))
    return None


def generate_with_retry(client, **kwargs):
    """client.models.generate_content(**kwargs) with backoff on rate limits and
    transient server errors."""
    attempt = 0
    while True:
        try:
            return client.models.generate_content(**kwargs)
        except errors.APIError as exc:
            if not _is_retryable(exc) or attempt >= MAX_RETRIES:
                raise
            attempt += 1
            delay = _suggested_delay(exc)
            if delay is None:
                delay = min(2 ** attempt, MAX_DELAY)
            delay = min(delay + 0.5, MAX_DELAY)
            code = getattr(exc, "code", None) or getattr(exc, "status_code", "?")
            print(f"Gemini {code}: retrying in {delay:.0f}s (attempt {attempt}/{MAX_RETRIES})")
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
