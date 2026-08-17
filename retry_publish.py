import sys

from db import get_item, list_items, update_field, update_status
from ebay.inventory import create_draft_offer


def _resolve_id(arg: str) -> str | None:
    """Accept a full item id or a unique prefix (as /status displays it)."""
    if get_item(arg) is not None:
        return arg
    matches = [i["item_id"] for i in list_items() if i["item_id"].startswith(arg)]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        print(f"'{arg}' matches {len(matches)} items — use a longer prefix.")
    else:
        print(f"No item found for id {arg}")
    return None


def retry_publish(arg: str) -> None:
    item_id = _resolve_id(arg)
    if item_id is None:
        return

    print("Retrying", item_id)
    try:
        result = create_draft_offer(item_id)
    except Exception as e:
        print(" -> still failing:", e)
        return

    # MERGE, don't replace. create_draft_offer returns only what it just made
    # (sku, offer_id, quantity); writing that over the whole blob wipes every
    # other key on it — listing_id, view_item_url, published_at, and on a retry
    # after a partial sale the sale_price/order_ids/units_sold that /report and
    # /profit are computed from. Losing those silently reports the money as never
    # having come in.
    ebay = dict((get_item(item_id) or {}).get("ebay") or {})
    ebay.update(result)
    update_field(item_id, "ebay", ebay)
    update_status(item_id, "ebay_draft")
    print(f" -> draft created: SKU {result['sku']}, offer {result['offer_id']}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python retry_publish.py <item_id>")
        sys.exit(1)
    retry_publish(sys.argv[1])
