"""ebay.inventory._request retries GETs on a dropped connection or an eBay 5xx
(the 2026-11-03 daily sync lost 5 items to these), and never retries writes."""
import httpx
import pytest

from ebay import inventory as inv

# Taken at import: some older tests assign inv._request and never restore it.
_request = inv._request


@pytest.fixture
def calls(monkeypatch):
    seen = []
    monkeypatch.setattr(inv, "_headers", lambda: {})
    monkeypatch.setattr(inv.time, "sleep", lambda s: None)
    monkeypatch.setattr(inv.guard, "require_rest_write", lambda *a: None)
    return seen


def _script(monkeypatch, calls, outcomes):
    def fake(method, url, **kw):
        calls.append(method)
        out = outcomes[len(calls) - 1]
        if isinstance(out, Exception):
            raise out
        return httpx.Response(out, request=httpx.Request(method, url), json={})
    monkeypatch.setattr(inv.httpx, "request", fake)


def test_get_survives_a_reset_then_a_500(monkeypatch, calls):
    _script(monkeypatch, calls, [httpx.ConnectError("reset"), 500, 200])
    assert _request("GET", "/sell/inventory/v1/offer/1").status_code == 200
    assert len(calls) == 3


def test_get_gives_up_after_three_tries(monkeypatch, calls):
    _script(monkeypatch, calls, [500, 500, 500])
    with pytest.raises(RuntimeError, match=r"\[500\]"):
        _request("GET", "/sell/inventory/v1/offer/1")
    assert len(calls) == 3


def test_get_4xx_is_not_retried(monkeypatch, calls):
    _script(monkeypatch, calls, [404])
    with pytest.raises(RuntimeError, match=r"\[404\]"):
        _request("GET", "/sell/inventory/v1/offer/1")
    assert len(calls) == 1


@pytest.mark.parametrize("outcome", [500, httpx.ConnectError("reset")])
def test_writes_are_never_retried(monkeypatch, calls, outcome):
    _script(monkeypatch, calls, [outcome])
    with pytest.raises((RuntimeError, httpx.ConnectError)):
        _request("POST", "/sell/inventory/v1/offer/1/publish")
    assert len(calls) == 1
