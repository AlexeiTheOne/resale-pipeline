"""ebay/browse.py: Browse API client for active listings by text and by photo.

No real eBay calls. browse._send (the network seam) and browse._token are
monkeypatched; fake responses are real httpx.Response objects so status and
JSON handling run for real. The things that matter: shipping stays separate
from price and "unknown" never turns into 0, the request carries the US
marketplace header, and a 429 retries then fails with a clear error.

Run with `python tests/test_browse.py` or `pytest tests/`.
"""
import base64
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import httpx

import ebay.browse as browse

SUMMARY = {
    "itemId": "v1|123|0",
    "legacyItemId": "123",
    "title": "Kate Spade Scallop Link Band Apple Watch KSS0089",
    "price": {"value": "59.99", "currency": "USD"},
    "shippingOptions": [
        {"shippingCostType": "FIXED", "shippingCost": {"value": "5.50", "currency": "USD"}},
        {"shippingCostType": "FIXED", "shippingCost": {"value": "0.00", "currency": "USD"}},
    ],
    "condition": "New with tags",
    "conditionId": "1000",
    "image": {"imageUrl": "https://i.ebayimg.com/x.jpg"},
    "itemWebUrl": "https://www.ebay.com/itm/123",
    "seller": {"username": "s", "feedbackPercentage": "99.5", "feedbackScore": 812},
    "buyingOptions": ["FIXED_PRICE"],
}


def _resp(status, payload=None, headers=None):
    req = httpx.Request("GET", "https://api.ebay.com/x")
    return httpx.Response(status, json=payload if payload is not None else {},
                          headers=headers or {}, request=req)


def _stub(responses):
    calls = []
    seq = list(responses)

    def fake(method, path, *, params, headers, json_body=None):
        calls.append({"method": method, "path": path, "params": params,
                      "headers": headers, "json": json_body})
        return seq.pop(0)

    browse._send = fake
    browse._token = lambda: "app-tok"
    browse.time.sleep = lambda _s: None
    return calls


def test_search_active_normalises_and_sends_marketplace():
    calls = _stub([_resp(200, {"itemSummaries": [SUMMARY]})])
    out = browse.search_active(" kate spade KSS0089 ", limit=10,
                               condition_ids=[1000, 1500], category_ids=[260325])
    assert calls[0]["method"] == "GET" and calls[0]["path"] == browse.SEARCH_PATH
    p = calls[0]["params"]
    assert p["q"] == "kate spade KSS0089" and p["limit"] == 10
    assert p["filter"] == "conditionIds:{1000|1500}" and p["category_ids"] == "260325"
    h = calls[0]["headers"]
    assert h["X-EBAY-C-MARKETPLACE-ID"] == "EBAY_US" and h["Authorization"] == "Bearer app-tok"
    r = out[0]
    assert r["item_id"] == "v1|123|0" and r["price"] == 59.99 and r["currency"] == "USD"
    assert r["shipping_cost"] == 0.0  # cheapest quoted option
    assert r["condition"] == "New with tags" and r["condition_id"] == "1000"
    assert r["image_url"].endswith("x.jpg") and r["item_web_url"].endswith("/123")
    assert r["seller_feedback_pct"] == 99.5 and r["seller_feedback_score"] == 812


def test_gtin_alone_is_a_valid_search():
    calls = _stub([_resp(200, {"itemSummaries": []})])
    browse.search_active(None, gtin=" 885919812544 ")
    p = calls[0]["params"]
    assert p["gtin"] == "885919812544" and "q" not in p
    try:
        browse.search_active("  ", gtin="")
    except ValueError:
        pass
    else:
        raise AssertionError("empty query and gtin must be refused")


def test_unknown_shipping_is_none_not_zero():
    calc = dict(SUMMARY, shippingOptions=[{"shippingCostType": "CALCULATED"}])
    none = {k: v for k, v in SUMMARY.items() if k != "shippingOptions"}
    _stub([_resp(200, {"itemSummaries": [calc, none]})])
    out = browse.search_active("x")
    assert [r["shipping_cost"] for r in out] == [None, None]


def test_auction_uses_current_bid_and_missing_seller_is_ok():
    auction = {k: v for k, v in SUMMARY.items() if k not in ("price", "seller")}
    auction["currentBidPrice"] = {"value": "12.00", "currency": "USD"}
    _stub([_resp(200, {"itemSummaries": [auction]})])
    r = browse.search_active("x")[0]
    assert r["price"] == 12.0 and r["seller_feedback_pct"] is None


def test_empty_result_and_limit_clamped():
    calls = _stub([_resp(200, {"total": 0})])
    assert browse.search_active("x", limit=999) == []
    assert calls[0]["params"]["limit"] == browse.MAX_LIMIT


def test_ship_to_zip_sets_enduser_context():
    calls = _stub([_resp(200, {})])
    browse.search_active("x", ship_to_zip="33172")
    assert "zip%3D33172" in calls[0]["headers"]["X-EBAY-C-ENDUSERCTX"]


def test_search_by_image_posts_base64():
    calls = _stub([_resp(200, {"itemSummaries": [SUMMARY]})])
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "a.jpg"
        p.write_bytes(b"\xff\xd8fakejpeg")
        out = browse.search_by_image(p, limit=20, category_ids=["169291"])
    c = calls[0]
    assert c["method"] == "POST" and c["path"] == browse.IMAGE_PATH
    assert base64.b64decode(c["json"]["image"]) == b"\xff\xd8fakejpeg"
    assert c["params"] == {"limit": 20, "category_ids": "169291"}
    assert out[0]["title"].startswith("Kate Spade")


def test_rate_limit_retries_then_succeeds():
    calls = _stub([_resp(429, headers={"Retry-After": "1"}), _resp(200, {"itemSummaries": [SUMMARY]})])
    assert len(browse.search_active("x")) == 1
    assert len(calls) == 2


def test_rate_limit_exhausted_raises_clear_error():
    err = {"errors": [{"errorId": 2001, "message": "Too many requests"}]}
    calls = _stub([_resp(429, err)] * (browse.RETRIES + 1))
    try:
        browse.search_active("x")
    except browse.BrowseRateLimited as e:
        assert e.status == 429 and "Too many requests" in str(e)
        assert "app-tok" not in str(e)
    else:
        raise AssertionError("expected BrowseRateLimited")
    assert len(calls) == browse.RETRIES + 1


def test_client_error_is_not_retried():
    err = {"errors": [{"errorId": 12001, "longMessage": "Bad image"}]}
    calls = _stub([_resp(400, err)])
    try:
        browse.search_active("x")
    except browse.BrowseError as e:
        assert e.status == 400 and "Bad image" in str(e)
    else:
        raise AssertionError("expected BrowseError")
    assert len(calls) == 1


def test_network_error_retries_then_raises():
    browse._token = lambda: "app-tok"
    browse.time.sleep = lambda _s: None
    n = {"calls": 0}

    def boom(*a, **k):
        n["calls"] += 1
        raise httpx.ConnectError("down")

    browse._send = boom
    try:
        browse.search_active("x")
    except browse.BrowseError as e:
        assert "ConnectError" in str(e)
    else:
        raise AssertionError("expected BrowseError")
    assert n["calls"] == browse.RETRIES + 1


def test_empty_query_rejected():
    try:
        browse.search_active("  ")
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError")


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
