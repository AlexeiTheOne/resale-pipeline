"""Leave positive feedback for buyers automatically.

eBay sellers can only ever leave Positive feedback for a buyer (Neutral and
Negative were removed for sellers years ago), so there is no judgement to make
per order: the only failure is not leaving it at all. And the window is 60 days
from the sale; after that the transaction drops off and the buyer never gets it.
On 2026-10-03 52 buyers were waiting, the oldest from 2026-08-06, days from
expiring.

Uses the Trading API (GetItemsAwaitingFeedback + LeaveFeedback) with the same
OAuth user token as ebay/listings.py. No extra scope is needed.

Only buyers with a PAID, non-cancelled order visible in the Fulfillment API get
feedback. GetItemsAwaitingFeedback lists cancelled sales too, and a cancelled
buyer may never have paid. If the order list can't be read, nobody gets feedback
on that run, because there's no way to tell a real sale from a cancelled one.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape

import httpx

from ebay.listings import TRADING_URL, _headers, _strip_ns, _text
from ebay.orders import get_orders

_PAGE = 200          # GetItemsAwaitingFeedback max entries per page
_MAX_PAGES = 10
COMMENT_MAX = 80     # eBay's CommentText limit


def _trading(call_name: str, inner: str) -> ET.Element:
    body = (
        '<?xml version="1.0" encoding="utf-8"?>'
        f'<{call_name}Request xmlns="urn:ebay:apis:eBLBaseComponents">'
        f"{inner}</{call_name}Request>"
    )
    r = httpx.post(TRADING_URL, headers=_headers(call_name),
                   content=body.encode(), timeout=60)
    if r.status_code != 200:
        raise RuntimeError(f"eBay Trading {call_name} failed [{r.status_code}]: {r.text[:300]}")
    root = ET.fromstring(r.text)
    if _text(root, "Ack") in ("Failure", "PartialFailure"):
        msg = _text(root, "LongMessage") or _text(root, "ShortMessage") or r.text[:200]
        raise RuntimeError(msg)
    return root


def awaiting_buyer_feedback() -> list[dict]:
    """Sales where this seller still owes the buyer feedback.

    Returns dicts of {item_id, transaction_id, order_line, buyer, title}.

    GetItemsAwaitingFeedback lists a sale until BOTH sides have left feedback,
    so it also returns sales where we already left ours and only the buyer's is
    outstanding. Those carry a <FeedbackLeft> element (the feedback we left) and
    are dropped here. Retrying them is what made LeaveFeedback fail with "either
    the item number you entered does not exist or you have not completed a
    transaction with this user" every day (12 sales on 2026-10-29, all of them
    already rated Positive by an earlier run). The Buyer element is present in
    both cases, so it can't tell them apart."""
    out, page = [], 1
    while page <= _MAX_PAGES:
        root = _trading(
            "GetItemsAwaitingFeedback",
            f"<Pagination><EntriesPerPage>{_PAGE}</EntriesPerPage>"
            f"<PageNumber>{page}</PageNumber></Pagination>")
        batch = [n for n in root.iter() if _strip_ns(n.tag) == "Transaction"]
        for tx in batch:
            buyer_node = next((c for c in tx if _strip_ns(c.tag) == "Buyer"), None)
            if buyer_node is None:
                continue
            if any(_strip_ns(c.tag) == "FeedbackLeft" for c in tx):
                continue  # we already rated this buyer; they owe us, not the reverse
            buyer = _text(buyer_node, "UserID")
            item_id = _text(tx, "ItemID")
            transaction_id = _text(tx, "TransactionID")
            if not (buyer and item_id and transaction_id):
                continue
            out.append({
                "item_id": item_id,
                "transaction_id": transaction_id,
                "order_line": (_text(tx, "OrderLineItemID")
                               or f"{item_id}-{transaction_id}"),
                "buyer": buyer,
                "title": _text(tx, "Title") or "",
            })
        if len(batch) < _PAGE:
            break
        page += 1
    return out


def paid_sales(days: int = 90) -> set[tuple[str, str]]:
    """(buyer username, legacy item id) for every paid, non-cancelled order line.
    Raises if the orders can't be read, so the caller leaves no feedback blind."""
    paid = set()
    for order in get_orders(days):
        cancel = ((order.get("cancelStatus") or {}).get("cancelState") or "").upper()
        if cancel in ("CANCELED", "CANCELLED", "CANCEL_CLOSED"):
            continue
        if (order.get("orderPaymentStatus") or "").upper() not in ("PAID", "PARTIALLY_REFUNDED", "FULLY_REFUNDED"):
            continue
        buyer = ((order.get("buyer") or {}).get("username") or "").lower()
        for li in order.get("lineItems") or []:
            if li.get("legacyItemId"):
                paid.add((buyer, str(li["legacyItemId"])))
    return paid


def leave_positive(item_id: str, transaction_id: str, buyer: str, comment: str,
                   order_line: str | None = None) -> None:
    """Leave Positive feedback on one sale. Raises with eBay's message on failure.

    Identifies the sale by OrderLineItemID, eBay's preferred key, falling back
    to ItemID + TransactionID. eBay answers a repeat attempt on a sale that
    already has our feedback with "the item number you entered does not exist
    or you have not completed a transaction", which awaiting_buyer_feedback
    now avoids by dropping those sales."""
    tail = ("<CommentType>Positive</CommentType>"
            f"<CommentText>{escape(comment[:COMMENT_MAX])}</CommentText>"
            f"<TargetUser>{escape(buyer)}</TargetUser>")
    if order_line:
        try:
            _trading("LeaveFeedback",
                     f"<OrderLineItemID>{escape(order_line)}</OrderLineItemID>" + tail)
            return
        except RuntimeError as e:
            first_err = e
    else:
        first_err = None
    try:
        _trading("LeaveFeedback",
                 f"<ItemID>{escape(item_id)}</ItemID>"
                 f"<TransactionID>{escape(transaction_id)}</TransactionID>" + tail)
    except RuntimeError:
        if first_err is not None:
            raise first_err
        raise


def leave_all_pending(comment: str) -> dict:
    """Leave positive feedback for every buyer who is owed it and has a paid order.

    Returns {left: [entry...], skipped: [(entry, reason)...], errors: [(entry, msg)...]}.
    One failing sale doesn't stop the rest."""
    pending = awaiting_buyer_feedback()
    result = {"left": [], "skipped": [], "errors": []}
    if not pending:
        return result
    paid = paid_sales()
    for entry in pending:
        if (entry["buyer"].lower(), entry["item_id"]) not in paid:
            result["skipped"].append((entry, "no paid order found (cancelled or unpaid?)"))
            continue
        try:
            leave_positive(entry["item_id"], entry["transaction_id"], entry["buyer"], comment,
                           order_line=entry.get("order_line"))
            result["left"].append(entry)
        except Exception as e:
            result["errors"].append((entry, str(e)[:150]))
    return result
