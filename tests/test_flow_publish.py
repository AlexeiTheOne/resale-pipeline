"""Characterisation tests for the eBay publish path, driven through the real
handlers with fake Telegram (tests/tg_fakes.py) and a fake eBay/Cloudinary at
the HTTP level.

The chain today is two steps, not one:

    review:approve button -> review_callback -> _approve_item
        -> ebay.inventory.create_draft_offer   (Cloudinary uploads, location,
           policies, PUT inventory_item, POST offer)  -> status 'ebay_draft'
    /activate (or /retry on an 'ebay_draft' item -> advance)
        -> _publish_and_report -> _activate_item -> publish_offer
           (POST offer/{id}/publish) -> status 'published'
        -> _promote_item -> ebay.marketing.promote_listing (ad rate)

Approve never makes anything live; only the publish POST does.

Stub boundary: the `httpx.request/get/post/put/delete` module functions that
ebay/inventory, ebay/taxonomy and ebay/marketing call (FakeHttp below routes
them by URL and records every request), the eBay token getters, Cloudinary
credentials, and claude_llm.ask (the stock-photo match model call). Everything
in telegram_bot and ebay/* above the HTTP call runs for real. httpx.Client.send
and AsyncClient.send raise, so any request that slips past the router fails
the test instead of reaching eBay.
"""
import asyncio
import io
import json
import math
import os
import re
import sys
import threading
import uuid
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import claude_llm  # noqa: E402
import retry_publish  # noqa: E402
import telegram_bot as tb  # noqa: E402
from ebay import inventory, marketing, taxonomy  # noqa: E402
from db import create_item, get_item, update_field, update_status  # noqa: E402
from tg_fakes import (  # noqa: E402
    FakeContext, bot_state, make_callback_update,
    make_text_update, run, sent_texts, tiny_jpeg,
)

PRICE = 44.99
CATEGORY = "169291"
STOCK_URL = "https://stock.example.test/coach-tote.jpg"


# --------------------------------------------------------------------------- #
# Fake HTTP
# --------------------------------------------------------------------------- #

class FakeResp:
    def __init__(self, status=200, body=None, headers=None, content=None):
        self.status_code = status
        self._body = body
        self.headers = headers or {"content-type": "application/json"}
        self.text = json.dumps(body) if body is not None else ""
        self.content = content if content is not None else self.text.encode()

    def json(self):
        return self._body


class FakeHttp:
    """Routes the module-level httpx calls by method + URL. Thread-safe: the
    bot runs eBay work in asyncio.to_thread workers."""

    def __init__(self):
        self.lock = threading.Lock()
        self.calls: list[dict] = []
        self.offers: dict[str, str] = {}       # sku -> offer id
        self.fail: dict[str, FakeResp] = {}    # route name -> forced response
        self.stock_bytes: bytes | None = None
        self.publish_gate: threading.Event | None = None
        self.unrouted: list[str] = []
        self._n = 0

    # -- recording ------------------------------------------------------------
    def of(self, route: str) -> list[dict]:
        return [c for c in self.calls if c["route"] == route]

    def _record(self, route, method, url, kw):
        with self.lock:
            self.calls.append({"route": route, "method": method, "url": url,
                               "json": kw.get("json"), "files": kw.get("files")})

    # -- dispatch -------------------------------------------------------------
    def request(self, method, url, **kw):
        method = method.upper()
        route = self._route(method, url)
        self._record(route, method, url, kw)
        if route in self.fail:
            return self.fail[route]
        return getattr(self, "_" + route)(method, url, kw)

    def _route(self, method, url):
        rules = [
            ("POST", r"api\.cloudinary\.com/.*/image/upload$", "cloudinary"),
            ("GET", r"^" + re.escape(STOCK_URL) + "$", "stock_image"),
            ("GET", r"/commerce/taxonomy/v1/get_default_category_tree_id$", "tree_id"),
            ("GET", r"/get_item_aspects_for_category$", "aspects"),
            ("GET", r"/get_category_subtree$", "category_name"),
            ("GET", r"/sell/inventory/v1/location/[^/]+$", "location"),
            ("GET", r"/sell/account/v1/fulfillment_policy$", "fulfillment_policy"),
            ("GET", r"/sell/account/v1/return_policy$", "return_policy"),
            ("PUT", r"/sell/inventory/v1/inventory_item/[^/]+$", "inventory_item"),
            ("POST", r"/sell/inventory/v1/offer$", "create_offer"),
            ("PUT", r"/sell/inventory/v1/offer/[^/]+$", "update_offer"),
            ("POST", r"/sell/inventory/v1/offer/[^/]+/publish$", "publish"),
            ("GET", r"/sell/marketing/v1/ad_campaign$", "campaigns"),
            ("POST", r"/ad_campaign/[^/]+/create_ads_by_inventory_reference$", "create_ad"),
        ]
        for m, pattern, name in rules:
            if m == method and re.search(pattern, url):
                return name
        # Recorded as well as raised: the bot's own `except Exception` around
        # eBay work would otherwise turn this into an ordinary error message.
        self.unrouted.append(f"{method} {url}")
        raise AssertionError(f"Unstubbed HTTP call in publish test: {method} {url}")

    # -- handlers -------------------------------------------------------------
    def _cloudinary(self, m, url, kw):
        name = Path(kw["files"]["file"].name).name
        return FakeResp(200, {"secure_url": f"https://res.cloudinary.test/{name}"})

    def _stock_image(self, m, url, kw):
        return FakeResp(200, None, headers={"content-type": "image/jpeg"},
                        content=self.stock_bytes or b"")

    def _tree_id(self, m, url, kw):
        return FakeResp(200, {"categoryTreeId": "0"})

    def _aspects(self, m, url, kw):
        return FakeResp(200, {"aspects": []})

    def _category_name(self, m, url, kw):
        return FakeResp(200, {"categorySubtreeNode": {"category": {"categoryName": "Bags"}}})

    def _location(self, m, url, kw):
        return FakeResp(200, {})

    def _fulfillment_policy(self, m, url, kw):
        return FakeResp(200, {"fulfillmentPolicies": [{"fulfillmentPolicyId": "FP-TEST"}]})

    def _return_policy(self, m, url, kw):
        return FakeResp(200, {"returnPolicies": [{"returnPolicyId": "RP-TEST"}]})

    def _inventory_item(self, m, url, kw):
        return FakeResp(204)

    def _create_offer(self, m, url, kw):
        sku = kw["json"]["sku"]
        with self.lock:
            existing = self.offers.get(sku)
            if existing is None:
                self._n += 1
                self.offers[sku] = f"OFFER-{self._n}"
        if existing is not None:
            # What eBay really answers for a second createOffer on one SKU.
            return FakeResp(400, {"errors": [{"errorId": 25002, "parameters": [
                {"name": "offerId", "value": existing}]}]})
        return FakeResp(201, {"offerId": self.offers[sku]})

    def _update_offer(self, m, url, kw):
        return FakeResp(204)

    def _publish(self, m, url, kw):
        if self.publish_gate is not None:
            # Hold the first publish until a second one arrives (or 2s pass),
            # so a concurrent second /activate gets to its status check while
            # the first is still in flight, as it would against real eBay.
            if not self.publish_gate.is_set():
                self.publish_gate.set()
                self._second = threading.Event()
                self._second.wait(2)
            else:
                self._second.set()
        offer_id = url.rstrip("/").split("/")[-2]
        return FakeResp(200, {"listingId": f"LIST-{offer_id}"})

    def _campaigns(self, m, url, kw):
        return FakeResp(200, {"campaigns": [{"campaignName": marketing.EBAY_PROMOTED_CAMPAIGN_NAME,
                                             "campaignId": "CAMP-1"}]})

    def _create_ad(self, m, url, kw):
        return FakeResp(201, {})


@pytest.fixture
def http(monkeypatch):
    fake = FakeHttp()

    def _boom(client, request, *a, **k):
        fake.unrouted.append(f"real send: {request.method} {request.url}")
        raise AssertionError("Real HTTP send attempted from a publish test")

    monkeypatch.setattr(httpx, "request", fake.request)
    for verb in ("get", "post", "put", "delete"):
        monkeypatch.setattr(httpx, verb, lambda url, *a, _v=verb, **kw: fake.request(_v, url, **kw))
    monkeypatch.setattr(httpx.Client, "send", _boom)
    monkeypatch.setattr(httpx.AsyncClient, "send", _boom)

    monkeypatch.setattr(inventory, "get_access_token", lambda: "test-token")
    monkeypatch.setattr(marketing, "get_access_token", lambda: "test-token")
    monkeypatch.setattr(taxonomy, "get_app_access_token", lambda: "test-app-token")
    monkeypatch.setattr(marketing, "_campaign_id_cache", None)
    monkeypatch.setattr(inventory, "CLOUDINARY_CLOUD_NAME", "test-cloud")
    monkeypatch.setattr(inventory, "CLOUDINARY_API_KEY", "test-key")
    monkeypatch.setattr(inventory, "CLOUDINARY_API_SECRET", "test-secret")
    # Unset so the policy lookups run (and are recorded) through the fake.
    monkeypatch.setattr(inventory, "EBAY_FULFILLMENT_POLICY_ID", None)
    monkeypatch.setattr(inventory, "EBAY_RETURN_POLICY_ID", None)

    def no_llm(stage, *a, **k):
        fake.unrouted.append(f"claude_llm.ask({stage})")
        raise AssertionError("Unstubbed claude_llm.ask in publish test")
    monkeypatch.setattr(claude_llm, "ask", no_llm)
    yield fake
    assert fake.unrouted == [], f"unstubbed outbound calls: {fake.unrouted}"


@pytest.fixture
def bot():
    with bot_state(tb):
        yield tb


# --------------------------------------------------------------------------- #
# Seeding and driving
# --------------------------------------------------------------------------- #

def _jpeg_file(path: Path, color) -> str:
    path.write_bytes(tiny_jpeg(color))
    return str(path)


def seed_review_item(*, title="Coach Signature Canvas Tote Bag Brown", price=PRICE,
                     receipt=None, listing_check=None, stock_url=None) -> str:
    """An item as _stage_draft leaves it: 3 photos on disk under tb.INBOX,
    identified, priced, drafted, status 'review'."""
    folder = tb.INBOX / f"flowpub_{uuid.uuid4().hex[:8]}"
    folder.mkdir(parents=True)
    photos = [_jpeg_file(folder / f"p{i}.jpg", c)
              for i, c in enumerate([(200, 30, 30), (30, 200, 30), (30, 30, 200)])]
    item_id = create_item(photos)
    ident = {"brand": "Coach", "product_name": "Signature Canvas Tote",
             "item_type": "tote bag", "confidence": 0.95}
    if stock_url:
        ident["product_image_url"] = stock_url
    update_field(item_id, "identification", ident)
    update_field(item_id, "pricing", {"suggested_price": price, "evidence": "solid",
                                      "needs_review": False})
    update_field(item_id, "listing", {
        "title": title,
        "description": "Coach signature canvas tote.\nGood used condition.",
        "price": price,
        "category_id": CATEGORY,
        "condition_id": "3000",
        "item_specifics": {"Brand": "Coach", "Color": "Brown", "Type": "Tote"},
    })
    if receipt:
        update_field(item_id, "receipt", receipt)
    if listing_check:
        update_field(item_id, "listing_check", listing_check)
    update_status(item_id, "drafted")
    update_status(item_id, "review")
    return item_id


def approve(item_id, ctx=None):
    tap, ctx = make_callback_update(f"review:approve:{item_id}", context=ctx)
    run(tb.review_callback, tap, ctx)
    return ctx


def activate(item_id, ctx):
    upd, ctx = make_text_update(f"/activate {item_id}", context=ctx)
    run(tb.activate_command, upd, ctx)
    return ctx


def expected_floor(price, item=None):
    """auto-decline floor per config: ceil(price * BEST_OFFER_MIN_PCT%) when the
    Ross cost is unknown (no break-even to compare against)."""
    pct = inventory.BEST_OFFER_MIN_PCT
    floor = math.ceil(round(price * pct / 100, 2)) if pct > 0 else None
    return floor if floor is not None and 1 <= floor < price else None


# --------------------------------------------------------------------------- #
# Happy path
# --------------------------------------------------------------------------- #

def test_approve_creates_one_draft_offer_with_expected_payload_and_nothing_live(bot, http):
    item_id = seed_review_item()
    photos = get_item(item_id)["photos"]
    ctx = approve(item_id)

    # Buttons were stripped from the draft message before anything else.
    assert any(c.kind == "edit_message_reply_markup" and c.kwargs["reply_markup"] is None
               for c in ctx.transcript)

    # Photos uploaded in listing order: the 2nd (tag close-up) moves to the end.
    uploaded = [Path(c["files"]["file"].name).name for c in http.of("cloudinary")]
    assert uploaded == [Path(photos[i]).name for i in (0, 2, 1)]

    [inv_put] = http.of("inventory_item")
    assert inv_put["url"].endswith(f"/inventory_item/{item_id}")
    body = inv_put["json"]
    assert body["product"]["title"] == "Coach Signature Canvas Tote Bag Brown"
    assert body["product"]["brand"] == "Coach"
    assert body["product"]["imageUrls"] == [f"https://res.cloudinary.test/{n}" for n in uploaded]
    assert body["condition"] == "USED_GOOD"
    assert body["availability"]["shipToLocationAvailability"]["quantity"] == 1

    [post] = http.of("create_offer")
    offer = post["json"]
    assert offer["sku"] == item_id
    assert offer["format"] == "FIXED_PRICE"
    assert offer["availableQuantity"] == 1
    assert offer["categoryId"] == CATEGORY
    assert offer["pricingSummary"]["price"] == {"value": str(PRICE),
                                                "currency": inventory.EBAY_CURRENCY}
    assert offer["marketplaceId"] == inventory.EBAY_MARKETPLACE_ID
    assert offer["merchantLocationKey"] == inventory.EBAY_MERCHANT_LOCATION_KEY
    policies = offer["listingPolicies"]
    assert policies["fulfillmentPolicyId"] == "FP-TEST"
    assert policies["returnPolicyId"] == "RP-TEST"
    if inventory.BEST_OFFER_ENABLED:
        terms = policies["bestOfferTerms"]
        assert terms["bestOfferEnabled"] is True
        floor = expected_floor(PRICE)
        if floor is None:
            assert "autoDeclinePrice" not in terms
        else:
            assert terms["autoDeclinePrice"] == {"value": f"{floor:.2f}",
                                                 "currency": inventory.EBAY_CURRENCY}
    else:
        assert "bestOfferTerms" not in policies

    # Approve does NOT publish or promote.
    assert http.of("publish") == [] and http.of("create_ad") == []

    item = get_item(item_id)
    assert item["status"] == "ebay_draft"
    assert item["ebay"]["sku"] == item_id and item["ebay"]["offer_id"] == "OFFER-1"
    assert item["ebay"]["quantity"] == 1
    assert "listing_id" not in item["ebay"]

    texts = sent_texts(ctx)
    assert texts[0] == "✅ Approved. Creating eBay draft..."
    assert texts[1].startswith(f"📝 Draft created on eBay (SKU {item_id}, offer OFFER-1).")
    assert "/activate" in texts[1]


def test_activate_publishes_once_stores_listing_id_and_shows_link(bot, http):
    item_id = seed_review_item()
    ctx = approve(item_id)
    n_before = len(sent_texts(ctx))
    activate(item_id, ctx)

    [pub] = http.of("publish")
    assert pub["url"].endswith("/sell/inventory/v1/offer/OFFER-1/publish")

    item = get_item(item_id)
    assert item["status"] == "published"
    assert item["ebay"]["listing_id"] == "LIST-OFFER-1"
    assert item["ebay"]["view_item_url"] == "https://www.ebay.com/itm/LIST-OFFER-1"
    assert item["ebay"]["published_at"]
    assert item["ebay"]["offer_id"] == "OFFER-1"  # merged, not replaced

    new = sent_texts(ctx)[n_before:]
    assert new[0].startswith("🎉 Published! https://www.ebay.com/itm/LIST-OFFER-1")
    # Uploads/offer creation happen once, on approve; publish doesn't redo them.
    assert len(http.of("create_offer")) == 1 and len(http.of("inventory_item")) == 1


# --------------------------------------------------------------------------- #
# Config-driven payload: best-offer floor, ad rate
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("pct", [None, 60.0, 90.0])
def test_best_offer_floor_follows_config(bot, http, monkeypatch, pct):
    monkeypatch.setattr(inventory, "BEST_OFFER_ENABLED", True)
    if pct is not None:
        monkeypatch.setattr(inventory, "BEST_OFFER_MIN_PCT", pct)
    item_id = seed_review_item()
    approve(item_id)
    terms = http.of("create_offer")[0]["json"]["listingPolicies"]["bestOfferTerms"]
    floor = expected_floor(PRICE)
    assert floor is not None  # any sane config floor sits between $1 and the price
    assert terms == {"bestOfferEnabled": True,
                     "autoDeclinePrice": {"value": f"{floor:.2f}",
                                          "currency": inventory.EBAY_CURRENCY}}


def test_best_offer_floor_uses_breakeven_when_it_is_higher(bot, http, monkeypatch):
    monkeypatch.setattr(inventory, "BEST_OFFER_ENABLED", True)
    monkeypatch.setattr(inventory, "BEST_OFFER_MIN_PCT", 1.0)  # % floor ~ $1
    item_id = seed_review_item(receipt={"reduced_price": 19.99, "peeled": True})
    approve(item_id)
    terms = http.of("create_offer")[0]["json"]["listingPolicies"]["bestOfferTerms"]
    be = math.ceil(round(inventory.breakeven_price(get_item(item_id)), 2))
    assert terms["autoDeclinePrice"]["value"] == f"{be:.2f}"


def test_best_offer_off_sends_no_terms(bot, http, monkeypatch):
    monkeypatch.setattr(inventory, "BEST_OFFER_ENABLED", False)
    item_id = seed_review_item()
    approve(item_id)
    assert "bestOfferTerms" not in http.of("create_offer")[0]["json"]["listingPolicies"]


@pytest.mark.parametrize("rate", [None, 7.5, 0])
def test_ad_rate_after_publish_follows_config(bot, http, monkeypatch, rate):
    if rate is not None:
        monkeypatch.setattr(tb, "EBAY_DEFAULT_AD_RATE_PCT", rate)
    rate = tb.EBAY_DEFAULT_AD_RATE_PCT
    item_id = seed_review_item()
    ctx = approve(item_id)
    activate(item_id, ctx)
    ads = http.of("create_ad")
    if rate and rate > 0:
        [ad] = ads
        assert "/ad_campaign/CAMP-1/" in ad["url"]
        assert ad["json"] == {"bidPercentage": f"{float(rate):.1f}",
                              "inventoryReferenceId": item_id,
                              "inventoryReferenceType": "INVENTORY_ITEM"}
        assert sent_texts(ctx)[-1] == (f"📣 Set ad rate to {float(rate):.1f}% for "
                                       f"{item_id[:8]} (Promoted Listings).")
    else:
        assert ads == [] and http.of("campaigns") == []
        assert sent_texts(ctx)[-1].startswith("🎉 Published!")


def test_ad_rate_failure_still_reports_listing_as_published(bot, http, monkeypatch):
    monkeypatch.setattr(tb, "EBAY_DEFAULT_AD_RATE_PCT", 5)
    http.fail["create_ad"] = FakeResp(500, {"errors": [{"message": "boom"}]})
    item_id = seed_review_item()
    ctx = approve(item_id)
    activate(item_id, ctx)
    assert get_item(item_id)["status"] == "published"
    texts = sent_texts(ctx)
    assert texts[-2].startswith("🎉 Published!")
    assert texts[-1].startswith("⚠️ Could not set the ad rate: RuntimeError")


# --------------------------------------------------------------------------- #
# Double taps
# --------------------------------------------------------------------------- #

def test_second_approve_tap_after_first_finished_is_refused(bot, http):
    item_id = seed_review_item()
    ctx = approve(item_id)
    approve(item_id, ctx)
    assert len(http.of("create_offer")) == 1
    assert len(http.of("cloudinary")) == 3
    assert sent_texts(ctx)[-1] == f"Item {item_id[:8]} is 'ebay_draft' — nothing to review."


def _slow_markup_edit(update):
    """Make the 'drop the buttons' edit yield to the loop, as the real Telegram
    round trip does. tg_fakes' version returns without suspending."""
    q = update.callback_query
    orig = q.edit_message_reply_markup

    async def slow(*a, **kw):
        await asyncio.sleep(0.01)
        return await orig(*a, **kw)
    q.edit_message_reply_markup = slow


def test_concurrent_double_tap_on_approve_creates_the_draft_once(bot, http):
    """The app runs with concurrent_updates(True), so two taps on Approve are
    handled at once. review_callback checks the status, then awaits the
    button-removal edit; the item is claimed between the two, so the second tap
    is turned away instead of building the draft again."""
    item_id = seed_review_item()
    ctx = FakeContext()
    t1, _ = make_callback_update(f"review:approve:{item_id}", context=ctx)
    t2, _ = make_callback_update(f"review:approve:{item_id}", context=ctx)
    _slow_markup_edit(t1)
    _slow_markup_edit(t2)

    async def both():
        await asyncio.gather(tb.review_callback(t1, ctx), tb.review_callback(t2, ctx))
    asyncio.run(both())

    assert len(http.of("create_offer")) == 1
    assert http.of("update_offer") == []
    assert len(http.of("cloudinary")) == 3
    assert http.of("publish") == []
    texts = sent_texts(ctx)
    assert texts.count("✅ Approved. Creating eBay draft...") == 1
    assert f"⏳ Already handling {item_id[:8]}." in texts
    item = get_item(item_id)
    assert item["status"] == "ebay_draft" and item["ebay"]["offer_id"] == "OFFER-1"
    assert tb._in_flight == set()


def test_second_activate_after_first_finished_does_not_republish(bot, http):
    item_id = seed_review_item()
    ctx = approve(item_id)
    activate(item_id, ctx)
    activate(item_id, ctx)
    assert len(http.of("publish")) == 1
    assert sent_texts(ctx)[-1] == (f"Item {item_id[:8]} is at status 'published', "
                                   "not 'ebay_draft'. Nothing to activate.")


def test_concurrent_double_activate_publishes_and_promotes_once(bot, http, monkeypatch):
    """Two overlapping /activate (concurrent_updates=True) for one item: the
    first claims it before the worker-thread publish, the second is told it's
    already publishing."""
    monkeypatch.setattr(tb, "EBAY_DEFAULT_AD_RATE_PCT", 5)
    item_id = seed_review_item()
    ctx = approve(item_id)
    http.publish_gate = threading.Event()
    u1, _ = make_text_update(f"/activate {item_id}", context=ctx)
    u2, _ = make_text_update(f"/activate {item_id}", context=ctx)

    async def both():
        await asyncio.gather(tb.activate_command(u1, ctx), tb.activate_command(u2, ctx))
    asyncio.run(both())

    assert len(http.of("publish")) == 1
    assert len(http.of("create_ad")) == 1
    texts = sent_texts(ctx)
    assert sum(t.startswith("🎉 Published!") for t in texts) == 1
    assert f"⏳ {item_id[:8]} is already publishing." in texts
    assert get_item(item_id)["status"] == "published"
    assert tb._in_flight == set()


# --------------------------------------------------------------------------- #
# eBay errors and retry
# --------------------------------------------------------------------------- #

def test_publish_error_leaves_item_unlisted_and_retry_publishes(bot, http, monkeypatch):
    monkeypatch.setattr(tb, "EBAY_DEFAULT_AD_RATE_PCT", 5)
    item_id = seed_review_item()
    ctx = approve(item_id)
    http.fail["publish"] = FakeResp(500, {"errors": [{"message": "System error"}]})
    activate(item_id, ctx)

    item = get_item(item_id)
    assert item["status"] == "ebay_draft"
    assert "listing_id" not in item["ebay"] and "view_item_url" not in item["ebay"]
    assert http.of("create_ad") == []  # no promote after a failed publish
    assert sent_texts(ctx)[-1].startswith(
        "⚠️ Activate failed: RuntimeError: eBay API POST "
        "/sell/inventory/v1/offer/OFFER-1/publish failed [500]")

    # In-bot retry: /retry on an 'ebay_draft' item goes advance -> publish.
    del http.fail["publish"]
    upd, _ = make_text_update(f"/retry {item_id}", context=ctx)
    run(tb.retry_command, upd, ctx)
    assert len(http.of("publish")) == 2
    assert len(http.of("create_offer")) == 1  # the draft isn't rebuilt
    item = get_item(item_id)
    assert item["status"] == "published" and item["ebay"]["listing_id"] == "LIST-OFFER-1"
    assert any(t.startswith("🎉 Published! https://www.ebay.com/itm/LIST-OFFER-1")
               for t in sent_texts(ctx))


def test_create_offer_error_leaves_item_approved_and_bot_retry_recovers(bot, http):
    item_id = seed_review_item()
    http.fail["create_offer"] = FakeResp(500, {"errors": [{"message": "System error"}]})
    ctx = approve(item_id)

    item = get_item(item_id)
    assert item["status"] == "approved"
    assert item.get("ebay") is None
    assert sent_texts(ctx)[-1].startswith(
        "⚠️ Approved, but eBay draft creation failed: RuntimeError: eBay API POST "
        "/sell/inventory/v1/offer failed [500]")
    assert http.of("publish") == []

    del http.fail["create_offer"]
    upd, _ = make_text_update(f"/retry {item_id}", context=ctx)
    run(tb.retry_command, upd, ctx)
    item = get_item(item_id)
    assert item["status"] == "ebay_draft" and item["ebay"]["offer_id"] == "OFFER-1"
    assert http.of("publish") == []  # retry rebuilds the draft; /activate still needed


def test_retry_publish_script_recreates_the_draft_and_keeps_other_ebay_keys(bot, http):
    item_id = seed_review_item()
    http.fail["create_offer"] = FakeResp(500, {"errors": []})
    approve(item_id)
    update_field(item_id, "ebay", {"note": "kept"})
    del http.fail["create_offer"]

    retry_publish.retry_publish(item_id[:8])
    item = get_item(item_id)
    assert item["status"] == "ebay_draft"
    assert item["ebay"]["offer_id"] == "OFFER-1" and item["ebay"]["note"] == "kept"
    assert http.of("publish") == []


def test_retry_publish_script_refuses_a_live_item(bot, http, capsys):
    item_id = seed_review_item()
    ctx = approve(item_id)
    activate(item_id, ctx)
    assert get_item(item_id)["status"] == "published"
    before = len(http.calls)

    retry_publish.retry_publish(item_id)
    item = get_item(item_id)
    assert item["status"] == "published"
    assert item["ebay"]["listing_id"] == "LIST-OFFER-1"
    assert len(http.calls) == before  # nothing sent to eBay or Cloudinary
    out = capsys.readouterr().out
    assert f"Refusing: {item_id} is already published (listing LIST-OFFER-1)" in out


def test_retry_publish_script_refuses_an_item_with_a_listing_id(bot, http, capsys):
    """A listing_id means it was live at some point, whatever the status says."""
    item_id = seed_review_item()
    http.fail["create_offer"] = FakeResp(500, {"errors": []})
    approve(item_id)
    update_field(item_id, "ebay", {"listing_id": "LIST-OLD"})
    before = len(http.calls)

    retry_publish.retry_publish(item_id)
    assert get_item(item_id)["status"] == "approved"
    assert len(http.calls) == before
    assert "Refusing:" in capsys.readouterr().out


def test_cloudinary_failure_sends_nothing_to_ebay(bot, http):
    item_id = seed_review_item()
    http.fail["cloudinary"] = FakeResp(500, {"error": {"message": "quota"}})
    ctx = approve(item_id)
    assert get_item(item_id)["status"] == "approved"
    assert http.of("inventory_item") == [] and http.of("create_offer") == []
    assert "Cloudinary upload failed [500]" in sent_texts(ctx)[-1]


def test_title_too_long_goes_back_to_review_before_any_http(bot, http):
    item_id = seed_review_item(title="Coach " + "Signature Canvas Leather Trim " * 4 + "Tote")
    ctx = approve(item_id)
    assert http.calls == []
    assert get_item(item_id)["status"] == "review"
    assert "80" in sent_texts(ctx)[-1]


# --------------------------------------------------------------------------- #
# Listing check / stock-photo match
# --------------------------------------------------------------------------- #

def test_listing_check_issues_do_not_block_approval(bot, http):
    """The LISTING_CHECK_ENABLED review check runs at draft time and is
    advisory: approve doesn't consult it."""
    check = {"issues": [{"severity": "high",
                         "message": "photos show a black bag, title says brown"}]}
    item_id = seed_review_item(listing_check=check)
    approve(item_id)
    assert get_item(item_id)["status"] == "ebay_draft"
    assert len(http.of("create_offer")) == 1


def _noisy_jpeg() -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.frombytes("RGB", (120, 120), os.urandom(120 * 120 * 3)).save(buf, "JPEG")
    return buf.getvalue()


@pytest.mark.parametrize("same", [False, True])
def test_stock_photo_is_only_added_when_the_model_says_it_matches(bot, http, monkeypatch, same):
    http.stock_bytes = _noisy_jpeg()
    asked = []

    def fake_ask(stage, prompt, **kw):
        asked.append((stage, len(kw["images"])))
        return ({"same_product": True, "differences": [], "reason": "same tote"} if same else
                {"same_product": False, "differences": ["roller buckle vs plain"],
                 "reason": "different"})
    monkeypatch.setattr(claude_llm, "ask", fake_ask)

    item_id = seed_review_item(stock_url=STOCK_URL)
    approve(item_id)

    assert asked == [("stock_photo_check", 4)]  # 3 own photos + the stock one
    uploads = [Path(c["files"]["file"].name).name for c in http.of("cloudinary")]
    if same:
        assert len(uploads) == 4 and uploads[-1].startswith(f"stock_{item_id}_")
    else:
        assert len(uploads) == 3 and not any(u.startswith("stock_") for u in uploads)
    # Either way the listing goes ahead.
    assert get_item(item_id)["status"] == "ebay_draft"
    assert len(http.of("inventory_item")[0]["json"]["product"]["imageUrls"]) == len(uploads)
