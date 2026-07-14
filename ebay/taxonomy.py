import json
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import httpx

from config import EBAY_MARKETPLACE_ID
from ebay.auth import get_app_access_token

TAXONOMY_API_BASE = "https://api.ebay.com/commerce/taxonomy/v1"

DB_PATH = "data/ross.db"
CACHE_TTL = 30 * 24 * 60 * 60  # 30 days


class InvalidCategoryError(Exception):
    """Raised when the Taxonomy API rejects a category id as invalid (error 62005)."""


def _conn():
    Path("data").mkdir(exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=5000")
    return con


def _create_cache_table() -> None:
    with _conn() as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS taxonomy_cache (
                cache_key TEXT PRIMARY KEY,
                payload TEXT NOT NULL,
                fetched_at REAL NOT NULL
            )
        """)


_create_cache_table()


def _cache_get(key: str):
    with _conn() as con:
        cur = con.execute(
            "SELECT payload, fetched_at FROM taxonomy_cache WHERE cache_key = ?", (key,)
        )
        row = cur.fetchone()
    if row is None:
        return None
    payload, fetched_at = row
    if time.time() - fetched_at > CACHE_TTL:
        return None
    return json.loads(payload)


def _cache_set(key: str, value) -> None:
    with _conn() as con:
        con.execute(
            "INSERT INTO taxonomy_cache (cache_key, payload, fetched_at) VALUES (?, ?, ?) "
            "ON CONFLICT(cache_key) DO UPDATE SET "
            "payload = excluded.payload, fetched_at = excluded.fetched_at",
            (key, json.dumps(value), time.time()),
        )


def _headers() -> dict:
    return {"Authorization": f"Bearer {get_app_access_token()}"}


def _has_error_id(body_text: str, error_id: int) -> bool:
    try:
        body = json.loads(body_text)
    except (ValueError, TypeError):
        return False
    return any(e.get("errorId") == error_id for e in body.get("errors", []))


def category_tree_id() -> str:
    cache_key = f"category_tree_id:{EBAY_MARKETPLACE_ID}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    r = httpx.get(
        f"{TAXONOMY_API_BASE}/get_default_category_tree_id",
        headers=_headers(),
        params={"marketplace_id": EBAY_MARKETPLACE_ID},
        timeout=30,
    )
    if r.status_code >= 400:
        raise RuntimeError(f"eBay Taxonomy category tree lookup failed [{r.status_code}]: {r.text}")
    tree_id = r.json()["categoryTreeId"]
    _cache_set(cache_key, tree_id)
    return tree_id


def category_aspects(category_id: str) -> list[dict]:
    """Full aspect metadata for a category, required and optional alike —
    cached for 30 days. Raises InvalidCategoryError on eBay error 62005."""
    tree_id = category_tree_id()
    cache_key = f"aspects:{tree_id}:{category_id}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    r = httpx.get(
        f"{TAXONOMY_API_BASE}/category_tree/{tree_id}/get_item_aspects_for_category",
        headers=_headers(),
        params={"category_id": category_id},
        timeout=30,
    )
    if r.status_code >= 400:
        if _has_error_id(r.text, 62005):
            raise InvalidCategoryError(f"eBay rejected category {category_id} as invalid (62005)")
        raise RuntimeError(f"eBay Taxonomy aspects lookup failed [{r.status_code}]: {r.text}")
    aspects = r.json().get("aspects", [])
    _cache_set(cache_key, aspects)
    return aspects


def category_suggestions(query: str) -> list[dict]:
    r = httpx.get(
        f"{TAXONOMY_API_BASE}/category_tree/{category_tree_id()}/get_category_suggestions",
        headers=_headers(),
        params={"q": query},
        timeout=30,
    )
    if r.status_code >= 400:
        raise RuntimeError(f"eBay Taxonomy category suggestions failed [{r.status_code}]: {r.text}")
    return r.json().get("categorySuggestions", [])


def category_name(category_id: str) -> str | None:
    """Best-effort human-readable name for a category id via get_category_subtree.
    Returns None if the id can't be resolved (e.g. it's genuinely invalid), so
    callers can fall back to showing the bare id."""
    tree_id = category_tree_id()
    cache_key = f"category_name:{tree_id}:{category_id}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached
    try:
        r = httpx.get(
            f"{TAXONOMY_API_BASE}/category_tree/{tree_id}/get_category_subtree",
            headers=_headers(),
            params={"category_id": category_id},
            timeout=30,
        )
        if r.status_code >= 400:
            return None
        node = (r.json() or {}).get("categorySubtreeNode") or {}
        name = (node.get("category") or {}).get("categoryName")
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        return None
    if name:
        _cache_set(cache_key, name)
    return name


def resolve_valid_category(category_id: str, title: str) -> tuple[str, str | None]:
    """Return (valid_category_id, category_name). If the given id is invalid
    (62005), re-select via the category suggestions endpoint using the listing
    title and return the first suggested leaf category that validates, along with
    the name carried in the suggestion. When the original is already valid, the
    name is None (it isn't needed unless a correction happened)."""
    try:
        category_aspects(category_id)  # cached; doubles as a validity probe
        return category_id, None
    except InvalidCategoryError:
        pass

    for suggestion in category_suggestions(title):
        cat = suggestion.get("category") or {}
        suggested_id = cat.get("categoryId")
        if not suggested_id or suggested_id == category_id:
            continue
        try:
            category_aspects(suggested_id)
            return suggested_id, cat.get("categoryName")
        except InvalidCategoryError:
            continue

    raise RuntimeError(
        f"Category {category_id} is invalid (62005) and no usable replacement was found "
        f"via category suggestions for title {title!r}."
    )
