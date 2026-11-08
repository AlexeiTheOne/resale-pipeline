"""Sold searches shared across items of the same model (Person B, 2026-11-07).

Apify bills per sold row, and each item's searches are worded from its own
identification: the brown cow print bag searches "Von Dutch brown cow print
bowling bag", the purple one "... purple ...". The rows are the same model's
sales either way. So every sold search is filed under the model it was made
for, and a later item of the same model (another colour, a /check after the
intake, a reprice after a /check) reuses the same kind of search made in the
last SOLD_MODEL_REUSE_DAYS instead of calling Apify.

- The model: the brand and the base style code (EHH108 of EHH108-26-WIP),
  and the brand and 2+ distinctive product-name words (check.model_tokens).
  An item is filed under each key it has.
- Like for like: a "fewer words" search is replaced by the model's "fewer
  words" search, a product-name search by a product-name search. The
  exact-item searches (the full style code, the UPC) always run: they are
  what tells this colour from the others, and they rarely return rows, so
  they cost almost nothing.

The index lives next to the pricing cache (data/cache/pricing_v2/sold_models)
and holds query texts only; the rows stay in the sold cache, whose
fetched_at decides freshness.
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from pathlib import Path

# Kinds of search a later item of the same model may take over. "code" and
# "upc" are this exact item; anything else (a backtest's extra) is left alone.
REUSABLE = ("search_query", "product", "fewer", "similar")
# Kinds filed in the index: the reusable ones plus the exact-item searches,
# whose rows are this model's sales too.
FILED = REUSABLE + ("code", "upc")
MIN_WORDS = 2       # a words-only key needs this many distinctive words
_lock = threading.Lock()


def model_keys(ident: dict | None) -> list[str]:
    """'vondutch|code|vdbb1036', 'vondutch|words|bowling cow print'. Empty
    without a brand, or with neither a base code nor 2+ distinctive words."""
    from pipeline import check, owned
    ident = ident or {}
    brand = "".join(re.findall(r"[a-z0-9]+", str(ident.get("brand") or "").lower()))
    if not brand:
        return []
    _, bases, _ = owned.style_codes(ident)
    out = [f"{brand}|code|{b}" for b in sorted(bases)]
    words = check.model_tokens(ident)
    if len(words) >= MIN_WORDS:
        out.append(f"{brand}|words|{' '.join(sorted(words))}")
    return out


def _dir() -> Path:
    from pipeline import pricing_v2
    return pricing_v2._cache_dir() / "sold_models"


def _path(key: str) -> Path:
    return _dir() / f"{hashlib.sha1(key.encode()).hexdigest()[:20]}.json"


def _read(key: str) -> dict:
    p = _path(key)
    try:
        return json.loads(p.read_text(encoding="utf-8")).get("queries") or {}
    except (OSError, ValueError):
        return {}


def record(ident: dict, queries: list[tuple[str, str]]) -> None:
    """File (kind, query) searches under the item's model. Never raises:
    the index only saves money, a failed write loses nothing else."""
    keep = [(n, q) for n, q in queries if n in FILED and q]
    keys = model_keys(ident)
    if not keep or not keys:
        return
    try:
        with _lock:
            for key in keys:
                known = _read(key)
                for n, q in keep:
                    known[q] = {"kind": n, "filed_at": time.time()}
                p = _path(key)
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(json.dumps({"key": key, "queries": known}), encoding="utf-8")
    except OSError:
        pass


def fresh(ident: dict, max_age_s: float, exclude=()) -> dict[str, list[str]]:
    """{kind: [query, ...]} filed under the item's model whose sold rows are
    cached and at most max_age_s old, newest first. `exclude`: the item's own
    queries (already in its search list)."""
    from pipeline import pricing_v2
    skip = {q.lower() for q in exclude}
    found: dict[str, tuple[str, float]] = {}
    for key in model_keys(ident):
        for q, meta in _read(key).items():
            if q.lower() in skip or q in found:
                continue
            age = pricing_v2.sold_cache_age(q)
            if age is not None and age <= max_age_s:
                found[q] = (meta.get("kind") or "", age)
    out: dict[str, list[str]] = {}
    for q, (kind, _) in sorted(found.items(), key=lambda kv: kv[1][1]):
        out.setdefault(kind, []).append(q)
    return out
