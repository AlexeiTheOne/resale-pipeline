"""Partial edits to live listings: item specifics, offer description/category,
and Trading ReviseItem for listings the Inventory API can't reach.

Every one of these is a full-replace write somewhere underneath, so the tests
pin down what must NOT change: photos, price, the aspects nobody asked about,
and the fields a ReviseItem didn't name.

Run with `python tests/test_listing_edits.py` or `pytest tests/`.
"""
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import ebay.inventory as inv
import ebay.listings as lst

FULL = [f"https://res.cloudinary.com/x/image/upload/v{1786894000 + n}/img{n}.jpg"
        for n in range(6)]
SKU = "179def75-0000-4000-8000-000000000000"


class _Resp:
    def __init__(self, payload=None, text="", status_code=200):
        self._payload = payload
        self.text = text
        self.status_code = status_code

    def json(self):
        return self._payload


def _patch_inventory(item, stored=FULL):
    sent = {}

    def fake_request(method, path, **kwargs):
        if method == "GET":
            return _Resp(item)
        sent["method"], sent["path"], sent["body"] = method, path, kwargs.get("json")
        return _Resp({})

    inv._request = fake_request
    inv._stored_image_urls = lambda sku: list(stored)
    return sent


def _item(**aspects):
    return {"product": {"title": "Ed Hardy Tote", "imageUrls": list(FULL),
                        "aspects": {k: list(v) for k, v in aspects.items()},
                        "brand": "Ed Hardy", "mpn": "Does not apply",
                        "upc": ["400299113298"]},
            "condition": "NEW_WITH_TAGS",
            "availability": {"shipToLocationAvailability": {"quantity": 1}}}


# --- update_inventory_aspects -------------------------------------------------

def test_set_replaces_only_the_named_aspect():
    sent = _patch_inventory(_item(Pattern=["Animal Print, Graphic Print"], Color=["Pink"]))
    inv.update_inventory_aspects(SKU, {"pattern": "Graphic Print"})
    aspects = sent["body"]["product"]["aspects"]
    assert aspects == {"Pattern": ["Graphic Print"], "Color": ["Pink"]}
    assert list(aspects) == ["Pattern", "Color"], "the listing's own name and order are kept"
    assert sent["body"]["product"]["title"] == "Ed Hardy Tote"
    assert sent["body"]["product"]["imageUrls"] == FULL


def test_removing_upc_clears_the_product_identifier_too():
    """A Ross store code in the aspect AND in product.upc: both must go, or
    Google Shopping keeps reading the bad code."""
    sent = _patch_inventory(_item(UPC=["400299113298"], Color=["Pink"]))
    inv.update_inventory_aspects(SKU, remove=["UPC"])
    product = sent["body"]["product"]
    assert "UPC" not in product["aspects"]
    assert "upc" not in product
    assert product["aspects"]["Color"] == ["Pink"]


def test_setting_upc_and_mpn_mirrors_identifiers():
    sent = _patch_inventory(_item(UPC=["199341188802"], Model=["EHH38442-8MT"]))
    inv.update_inventory_aspects(SKU, {"UPC": "199347188202", "Model": "EHH384-42-BMT",
                                       "MPN": "EHH384-42-BMT"})
    product = sent["body"]["product"]
    assert product["upc"] == ["199347188202"]
    assert product["mpn"] == "EHH384-42-BMT"
    assert product["aspects"]["Model"] == ["EHH384-42-BMT"]


def test_bad_gtin_is_refused_before_any_write():
    sent = _patch_inventory(_item(UPC=["199341188802"]))
    try:
        inv.update_inventory_aspects(SKU, {"UPC": "199347188203"})
    except ValueError:
        assert "body" not in sent
    else:
        raise AssertionError("expected ValueError for a bad check digit")


def test_removing_an_aspect_the_item_lacks_does_not_write():
    sent = _patch_inventory(_item(Color=["Pink"]))
    try:
        inv.update_inventory_aspects(SKU, remove=["Colour"])
    except KeyError:
        assert "body" not in sent, "it wrote anyway"
    else:
        raise AssertionError("expected KeyError")


def test_removing_mpn_falls_back_to_does_not_apply():
    item = _item(MPN=["X1"])
    item["product"]["mpn"] = "X1"
    sent = _patch_inventory(item)
    inv.update_inventory_aspects(SKU, remove=["MPN"])
    assert sent["body"]["product"]["mpn"] == "Does not apply"


def test_aspect_edit_repairs_a_short_image_read():
    item = _item(Color=["Pink"])
    item["product"]["imageUrls"] = FULL[:1]
    sent = _patch_inventory(item)
    inv.update_inventory_aspects(SKU, {"Department": "Women"})
    assert sent["body"]["product"]["imageUrls"] == FULL


def test_title_edit_still_works_through_the_shared_writer():
    sent = _patch_inventory(_item(Color=["Pink"]))
    assert inv.update_inventory_title(SKU, "New Title") == "New Title"
    assert sent["body"]["product"]["title"] == "New Title"
    assert sent["body"]["product"]["aspects"] == {"Color": ["Pink"]}


# --- update_offer_fields ------------------------------------------------------

OFFER = {"offerId": "1", "sku": SKU, "categoryId": "180000", "availableQuantity": 2,
         "listingDescription": "<p>old</p>", "merchantLocationKey": "loc",
         "listingPolicies": {"fulfillmentPolicyId": "f", "returnPolicyId": "r",
                             "bestOfferTerms": {"bestOfferEnabled": False}},
         "pricingSummary": {"price": {"value": "59.99", "currency": "USD"}},
         "listing": {"listingId": "117", "listingStatus": "ACTIVE"}, "status": "PUBLISHED"}


def test_category_move_keeps_price_quantity_and_policies():
    sent = _patch_inventory(dict(OFFER))
    inv.update_offer_fields("1", category_id="169291")
    body = sent["body"]
    assert sent["method"] == "PUT" and sent["path"].endswith("/offer/1")
    assert body["categoryId"] == "169291"
    assert body["pricingSummary"]["price"]["value"] == "59.99"
    assert body["availableQuantity"] == 2
    assert body["listingPolicies"] == OFFER["listingPolicies"]
    assert body["listingDescription"] == "<p>old</p>"
    assert "listing" not in body and "status" not in body


def test_description_edit_keeps_category():
    sent = _patch_inventory(dict(OFFER))
    inv.update_offer_fields("1", description_html="<p>new</p>")
    assert sent["body"]["listingDescription"] == "<p>new</p>"
    assert sent["body"]["categoryId"] == "180000"


def test_offer_edit_with_nothing_to_change_is_refused():
    try:
        inv.update_offer_fields("1")
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError")


# --- Trading: revise_item / get_item_details ----------------------------------

_OK = ('<?xml version="1.0"?><ReviseItemResponse xmlns="urn:ebay:apis:eBLBaseComponents">'
       '<Ack>Success</Ack></ReviseItemResponse>')


def _patch_trading(response_text):
    sent = {}

    def fake_post(url, headers=None, content=None, timeout=None):
        sent["call"] = headers["X-EBAY-API-CALL-NAME"]
        sent["xml"] = content.decode("utf-8")
        return _Resp(text=response_text)

    # Replace only this module's reference: patching httpx.post itself would
    # leak into every other test in the session.
    lst.httpx = types.SimpleNamespace(post=fake_post, HTTPError=Exception)
    lst.get_access_token = lambda: "token"
    return sent


def test_revise_item_sends_only_the_named_fields():
    sent = _patch_trading(_OK)
    lst.revise_item("117", title="Kate Spade Band for Apple Watch 38-45mm & Black")
    xml = sent["xml"]
    assert sent["call"] == "ReviseItem"
    assert "<Title>Kate Spade Band for Apple Watch 38-45mm &amp; Black</Title>" in xml
    for absent in ("PictureDetails", "StartPrice", "Quantity", "ItemSpecifics",
                   "Description", "PrimaryCategory"):
        assert absent not in xml, f"{absent} was sent"


def test_revise_item_specifics_and_description():
    sent = _patch_trading(_OK)
    lst.revise_item("117", description="<p>a ]]> b</p>",
                    item_specifics={"Brand": "Kate Spade", "Features": ["Vegan", "Gift Set"],
                                    "Empty": ""})
    xml = sent["xml"]
    assert "<Name>Features</Name><Value>Vegan</Value><Value>Gift Set</Value>" in xml
    assert "<Name>Empty</Name>" not in xml
    assert "<![CDATA[<p>a ]]]]><![CDATA[> b</p>]]>" in xml


def test_revise_item_failure_raises():
    _patch_trading('<?xml version="1.0"?><ReviseItemResponse xmlns="urn:ebay:apis:eBLBaseComponents">'
                   '<Ack>Failure</Ack><Errors><SeverityCode>Error</SeverityCode>'
                   '<LongMessage>Inventory-based listing management is not supported.</LongMessage>'
                   '</Errors></ReviseItemResponse>')
    try:
        lst.revise_item("117", title="x")
    except RuntimeError as e:
        assert "Inventory-based" in str(e)
    else:
        raise AssertionError("expected RuntimeError")


def test_revise_item_refuses_long_title_before_sending():
    sent = _patch_trading(_OK)
    try:
        lst.revise_item("117", title="x" * 81)
    except ValueError:
        assert "xml" not in sent
    else:
        raise AssertionError("expected ValueError")


def test_get_item_details_parses_the_buyer_view():
    _patch_trading(
        '<?xml version="1.0"?><GetItemResponse xmlns="urn:ebay:apis:eBLBaseComponents"><Ack>Success</Ack>'
        '<Item><ItemID>117</ItemID><Title>Band &amp; Strap</Title><SKU>abc</SKU>'
        '<Description>&lt;p&gt;hi&lt;/p&gt;</Description>'
        '<PrimaryCategory><CategoryID>182068</CategoryID></PrimaryCategory>'
        '<Quantity>3</Quantity><ConditionID>1000</ConditionID>'
        '<SellingStatus><CurrentPrice currencyID="USD">48.99</CurrentPrice>'
        '<QuantitySold>1</QuantitySold><ListingStatus>Active</ListingStatus></SellingStatus>'
        '<PictureDetails><PictureURL>u1</PictureURL><PictureURL>u2</PictureURL></PictureDetails>'
        '<ItemSpecifics><NameValueList><Name>Brand</Name><Value>Kate Spade</Value></NameValueList>'
        '<NameValueList><Name>Features</Name><Value>A</Value><Value>B</Value></NameValueList>'
        '</ItemSpecifics></Item></GetItemResponse>')
    d = lst.get_item_details("117")
    assert d["title"] == "Band & Strap"
    assert d["description"] == "<p>hi</p>"
    assert d["category_id"] == "182068"
    assert d["picture_count"] == 2
    assert d["price"] == 48.99
    assert d["quantity"] == 2
    assert d["item_specifics"] == {"Brand": ["Kate Spade"], "Features": ["A", "B"]}


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
