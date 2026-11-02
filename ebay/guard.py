"""One switch for every call that changes the eBay account.

EBAY_WRITES_ENABLED=0 (dev and test copies) makes every eBay write refuse
before any request is sent: publish, offer create/update/delete/withdraw,
price, quantity, title and aspect edits, ReviseItem, promoted-listing ads,
offers to watchers and buyer feedback. Reads (orders, listings, GetItem,
taxonomy, search) still work.

The checks sit at the lowest layer of each ebay/ module (every non-GET REST
request and every Trading call not on the read list), so a new caller cannot
miss them. The setting is read on each call, so tests can flip it.
"""
import logging

import config

log = logging.getLogger("ebay.guard")

# Trading API calls that only read. Any other call name counts as a write.
TRADING_READS = frozenset({
    "GetItem",
    "GetMyeBaySelling",
    "GetItemsAwaitingFeedback",
})


class EbayWritesDisabled(RuntimeError):
    """Raised instead of sending a write to eBay while writes are off."""


def writes_enabled() -> bool:
    return bool(config.EBAY_WRITES_ENABLED)


def require_writes(action: str) -> None:
    """Refuse `action` (a short description of the write) when writes are off."""
    if writes_enabled():
        return
    msg = (f"eBay writes are OFF (EBAY_WRITES_ENABLED=0): refused {action}. "
           "Nothing was sent to eBay.")
    log.warning(msg)
    raise EbayWritesDisabled(msg)


def require_rest_write(method: str, path: str) -> None:
    """Guard a REST call: anything but GET counts as a write."""
    if method.upper() != "GET":
        require_writes(f"{method.upper()} {path}")


def require_trading_write(call_name: str) -> None:
    """Guard a Trading API call: anything not in TRADING_READS counts as a write."""
    if call_name not in TRADING_READS:
        require_writes(f"Trading {call_name}")
