"""EBAY_WRITES_ENABLED=0 must stop every eBay write before a request is sent,
and leave reads alone. httpx is faked, so nothing here touches the network."""
import logging

import httpx
import pytest

import config
from ebay import feedback, guard, inventory, listings, marketing, negotiation, recommendation


class _Resp:
    status_code = 200
    text = "<GetItemResponse><Ack>Success</Ack></GetItemResponse>"
    content = b"{}"
    headers = {}

    def json(self):
        return {}


@pytest.fixture
def sent(monkeypatch):
    """Record every HTTP call instead of sending it."""
    calls = []

    def _record(method):
        def fake(*args, **kwargs):
            if method == "REQUEST":
                calls.append((args[0].upper(), args[1]))
            else:
                calls.append((method, args[0]))
            return _Resp()
        return fake

    for name in ("post", "put", "delete", "get", "patch"):
        monkeypatch.setattr(httpx, name, _record(name.upper()))
    monkeypatch.setattr(httpx, "request", _record("REQUEST"))
    # No token lookup: these tests must not depend on the DB.
    for mod in (inventory, listings, marketing, negotiation, recommendation):
        monkeypatch.setattr(mod, "get_access_token", lambda: "fake-token")
    return calls


@pytest.fixture
def writes_off(monkeypatch):
    monkeypatch.setattr(config, "EBAY_WRITES_ENABLED", False)


WRITES = {
    "publish": lambda: inventory.publish_offer("O1"),
    "create offer": lambda: inventory._create_offer({"sku": "S1"}),
    "delete offer": lambda: inventory.delete_offer("O1"),
    "withdraw (/end)": lambda: inventory.withdraw_offer("O1"),
    "reprice": lambda: inventory.update_offer_price("O1", 19.99),
    "quantity": lambda: inventory.update_offer_quantity("S1", "O1", 2),
    "draft (/post)": lambda: inventory.create_draft_offer("item-1"),
    "ReviseItem": lambda: listings.revise_item("123", title="New title"),
    "feedback": lambda: feedback.leave_positive("123", "456", "buyer", "Thanks!",
                                                order_line="123-456"),
    "promote": lambda: marketing.promote_listing("S1", 5),
    "create campaign": lambda: marketing._create_campaign(),
    "offer to watchers": lambda: negotiation.send_offer("123", 15.00),
}


@pytest.mark.parametrize("name", list(WRITES))
def test_write_is_refused_before_any_write_is_sent(name, sent, writes_off):
    with pytest.raises(guard.EbayWritesDisabled, match="EBAY_WRITES_ENABLED=0"):
        WRITES[name]()
    # Reads on the way (e.g. GET the offer before the PUT) are fine; nothing else
    # may go out. Every Trading call is a POST, so none of those either.
    assert all(method == "GET" for method, _ in sent), sent


def test_refusal_is_a_runtime_error_and_logged(writes_off, caplog):
    # Callers already handle RuntimeError from eBay, so a refusal is reported
    # like any other eBay failure instead of crashing the handler.
    assert issubclass(guard.EbayWritesDisabled, RuntimeError)
    with caplog.at_level(logging.WARNING, logger="ebay.guard"):
        with pytest.raises(guard.EbayWritesDisabled):
            guard.require_writes("publish offer O1")
    assert "refused publish offer O1" in caplog.text
    assert "Nothing was sent to eBay" in caplog.text


def test_reads_still_work_when_writes_are_off(sent, writes_off):
    guard.require_rest_write("GET", "/sell/inventory/v1/offer/O1")
    for call in guard.TRADING_READS:
        guard.require_trading_write(call)
    inventory.get_offer("O1")
    listings._trading_call("GetItem", "<ItemID>1</ItemID>")
    assert [m for m, _ in sent] == ["GET", "POST"]  # Trading reads are POSTs


def test_writes_go_through_when_enabled(sent, monkeypatch):
    monkeypatch.setattr(config, "EBAY_WRITES_ENABLED", True)
    inventory.update_offer_quantity("S1", "O1", 2)
    negotiation.send_offer("123", 15.00)
    assert [m for m, _ in sent] == ["POST", "POST"]


def test_default_is_on_so_production_is_unchanged(monkeypatch):
    monkeypatch.delenv("EBAY_WRITES_ENABLED", raising=False)
    assert config._env_bool("EBAY_WRITES_ENABLED", True) is True
    monkeypatch.setenv("EBAY_WRITES_ENABLED", "0")
    assert config._env_bool("EBAY_WRITES_ENABLED", True) is False


# --- the one REST POST that reads -------------------------------------------------

def test_the_read_only_post_list_is_exactly_the_recommendation_find():
    # Adding a path here lets a POST through with writes off: it needs a reason
    # in ebay/guard.py and a change to this test.
    assert guard.REST_READ_POSTS == frozenset({"/sell/recommendation/v1/find"})


def test_recommendation_find_still_runs_with_writes_off(sent, writes_off):
    assert recommendation.suggested_ad_rates(["L1"]) == {}
    assert sent == [("POST", "https://api.ebay.com/sell/recommendation/v1/find")]


@pytest.mark.parametrize("method, path", [
    ("POST", "/sell/recommendation/v1/find/"),                       # not the exact path
    ("POST", "/sell/recommendation/v1/find?filter=x"),               # query strings aren't stripped
    ("POST", "/sell/recommendation/v1/find/../../marketing/v1/ad_campaign"),
    ("POST", "/sell/recommendation/v1/findx"),
    ("POST", "/sell/recommendation/v2/find"),
    ("POST", "/SELL/RECOMMENDATION/V1/FIND"),
    ("PUT", "/sell/recommendation/v1/find"),                         # same path, other method
    ("DELETE", "/sell/recommendation/v1/find"),
    ("PATCH", "/sell/recommendation/v1/find"),
    ("POST", "/sell/marketing/v1/ad_campaign"),
    ("POST", "/sell/marketing/v1/ad_campaign/C1/bulk_update_ads_bid_by_inventory_reference"),
    ("POST", "/sell/inventory/v1/offer/O1/publish"),
    ("POST", "/sell/inventory/v1/offer"),
    ("POST", "/sell/negotiation/v1/send_offer_to_interested_buyers"),
])
def test_every_other_post_is_still_a_write(method, path, writes_off):
    with pytest.raises(guard.EbayWritesDisabled):
        guard.require_rest_write(method, path)


def test_the_allowed_post_is_let_through_and_lowercase_methods_too(writes_off):
    guard.require_rest_write("POST", "/sell/recommendation/v1/find")
    guard.require_rest_write("post", "/sell/recommendation/v1/find")
    with pytest.raises(guard.EbayWritesDisabled):
        guard.require_rest_write("put", "/sell/recommendation/v1/find")
