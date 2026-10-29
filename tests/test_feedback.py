"""The feedback job must only try sales where we still owe the buyer feedback.

GetItemsAwaitingFeedback keeps listing a sale until BOTH sides have rated, so
after the job left its Positive the sale stayed in the list (now with a
<FeedbackLeft> element) and every later run retried it. eBay refused each retry
with "the item number you entered does not exist or you have not completed a
transaction with this user". On 2026-10-29 that happened for 12 sales, all
already rated Positive. The XML below has the real response's shape.

No network calls. Run with `pytest tests/`.
"""
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from ebay import feedback  # noqa: E402

_NS = "urn:ebay:apis:eBLBaseComponents"


def _tx(buyer, item_id, tx_id, title, left=False):
    left_xml = "<FeedbackLeft><CommentType>Positive</CommentType></FeedbackLeft>" if left else ""
    return (f"<Transaction><Buyer><UserID>{buyer}</UserID></Buyer>"
            f"<Item><ItemID>{item_id}</ItemID><Title>{title}</Title></Item>"
            f"<TransactionID>{tx_id}</TransactionID>{left_xml}"
            f"<OrderLineItemID>{item_id}-{tx_id}</OrderLineItemID></Transaction>")


_RESPONSE = (
    f'<GetItemsAwaitingFeedbackResponse xmlns="{_NS}"><Ack>Success</Ack>'
    "<ItemsAwaitingFeedback><TransactionArray>"
    + _tx("new_buyer", "117000000001", "10080000000001", "Owed bag")
    + _tx("rayborn2007", "117304630616", "10085016619101", "Pillow sham", left=True)
    # a sale where the BUYER owes us: no Buyer element at all
    + "<Transaction><Seller><UserID>someone</UserID></Seller>"
      "<Item><ItemID>117000000002</ItemID></Item>"
      "<TransactionID>10080000000002</TransactionID></Transaction>"
    + "</TransactionArray></ItemsAwaitingFeedback></GetItemsAwaitingFeedbackResponse>"
)


def _patched(fn):
    def run():
        saved = (feedback._trading, feedback.paid_sales)
        calls = []

        def fake_trading(call, inner):
            calls.append((call, inner))
            if call == "GetItemsAwaitingFeedback":
                return ET.fromstring(_RESPONSE)
            return ET.fromstring(f'<LeaveFeedbackResponse xmlns="{_NS}"><Ack>Success</Ack>'
                                 "</LeaveFeedbackResponse>")
        feedback._trading = fake_trading
        feedback.paid_sales = lambda days=90: {("new_buyer", "117000000001"),
                                               ("rayborn2007", "117304630616")}
        try:
            fn(calls)
        finally:
            feedback._trading, feedback.paid_sales = saved
    run.__name__ = fn.__name__
    return run


@_patched
def test_already_left_sales_are_not_pending(calls):
    pending = feedback.awaiting_buyer_feedback()
    assert [p["item_id"] for p in pending] == ["117000000001"]
    assert pending[0]["order_line"] == "117000000001-10080000000001"


@_patched
def test_job_only_calls_leave_feedback_for_owed_sales(calls):
    result = feedback.leave_all_pending("Thanks!")
    assert [e["buyer"] for e in result["left"]] == ["new_buyer"]
    assert result["skipped"] == [] and result["errors"] == []
    leaves = [inner for call, inner in calls if call == "LeaveFeedback"]
    assert len(leaves) == 1
    assert "<OrderLineItemID>117000000001-10080000000001</OrderLineItemID>" in leaves[0]
    assert "<CommentType>Positive</CommentType>" in leaves[0]


if __name__ == "__main__":
    test_already_left_sales_are_not_pending()
    test_job_only_calls_leave_feedback_for_owed_sales()
    print("ok")
