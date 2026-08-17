"""A title edit must never be able to strip a listing's photos.

eBay's getInventoryItem returns a lossy projection of the record: it omits
product.description on every item, and on a large minority of items it returns
imageUrls collapsed to the single first URL while the live listing is still
showing every photo. update_inventory_title reads that response and PUTs it
back, and the inventory_item PUT is a FULL REPLACE — so echoing a short read is
what converts eBay's bad read into a real, buyer-visible loss.

Measured on the live account: 30 of 109 items reported exactly one image, at
statistically identical rates whether or not update_inventory_title had ever
touched them (27 of 96 pushed vs 3 of 13 never pushed), and one item reverted to
a single URL twice inside twenty minutes with nothing writing to it.

Run with `python tests/test_images.py` or `pytest tests/`.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import ebay.inventory as inv

FULL = [f"https://res.cloudinary.com/x/image/upload/v{1786894000 + n}/img{n}.jpg"
        for n in range(8)]
SKU = "af7e3380-e6db-4148-8ac4-90187061494c"


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


def _patch(monkey_returns, stored):
    """Stub the eBay round-trip and the DB, capturing what would be PUT."""
    sent = {}

    def fake_request(method, path, **kwargs):
        if method == "GET":
            return _Resp(monkey_returns)
        sent["body"] = kwargs.get("json")
        return _Resp({})

    inv._request = fake_request
    inv._stored_image_urls = lambda sku: list(stored)
    return sent


def test_short_read_is_repaired_not_echoed():
    """The whole incident in one assertion: eBay hands back 1 of 8 images and
    the PUT must still carry all 8."""
    sent = _patch({"product": {"title": "old", "imageUrls": [FULL[0]]}}, FULL)
    inv.update_inventory_title(SKU, "New Title")
    assert sent["body"]["product"]["imageUrls"] == FULL, "the loss was echoed back"
    assert sent["body"]["product"]["title"] == "New Title"


def test_full_read_is_left_alone():
    """When eBay returns everything, nothing is rewritten."""
    sent = _patch({"product": {"title": "old", "imageUrls": list(FULL)}}, FULL)
    inv.update_inventory_title(SKU, "New Title")
    assert sent["body"]["product"]["imageUrls"] == FULL


def test_refuses_when_there_is_nothing_to_repair_from():
    """A short read with no stored URLs is unrecoverable, so it must not write."""
    sent = _patch({"product": {"title": "old", "imageUrls": []}}, [])
    try:
        inv.update_inventory_title(SKU, "New Title", known_images=8)
    except inv.InventoryImagesMissingError:
        assert "body" not in sent, "it wrote anyway"
    else:
        raise AssertionError("expected InventoryImagesMissingError")


def test_zero_weight_is_still_stripped():
    """The 25709 fix must survive the images change — eBay returns a 0.0 weight
    on GET and rejects that exact payload on PUT."""
    sent = _patch({"product": {"title": "old", "imageUrls": list(FULL)},
                   "packageWeightAndSize": {"weight": {"value": 0.0, "unit": "POUND"},
                                            "shippingIrregular": False}}, FULL)
    inv.update_inventory_title(SKU, "New Title")
    assert "packageWeightAndSize" not in sent["body"]


def test_real_weight_is_preserved():
    sent = _patch({"product": {"title": "old", "imageUrls": list(FULL)},
                   "packageWeightAndSize": {"weight": {"value": 2.5, "unit": "POUND"}}}, FULL)
    inv.update_inventory_title(SKU, "New Title")
    assert sent["body"]["packageWeightAndSize"]["weight"]["value"] == 2.5


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"\n{len(tests)}/{len(tests)} passed")
