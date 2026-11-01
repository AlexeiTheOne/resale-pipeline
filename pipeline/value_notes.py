"""Price notes: up to 4 short facts that could make a buyer pay more or less for
this item (buzz on Reddit/forums, a sought-after collab, standout material or
features), each with a link. They back up the owner's own call on price; they
never set it. Claude with web search, run alongside pricing in stage_price.

Why: the Polo x Whitehouse Cox belt (af2d12de) was priced at $89.99 by the bot.
The owner listed it at $169.99 after finding a Reddit post hyping it, and it
sold in 2 days.
"""
import json

import config

MAX_NOTES = 4
MAX_WORDS = 14

_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["notes"],
    "properties": {"notes": {"type": "array", "items": {
        "type": "object",
        "additionalProperties": False,
        "required": ["text", "effect", "url"],
        "properties": {"text": {"type": "string"},
                       "effect": {"type": "string", "enum": ["up", "down"]},
                       "url": {"type": ["string", "null"]}},
    }}},
}

_PROMPT = (
    "I resell this item on eBay and set the price myself. Run a few web searches "
    "BEFORE answering (one of them on Reddit; skip results that are just eBay or "
    "shop listings) and give me facts that would push its price UP or DOWN, beyond "
    "comparable sales. These count:\n"
    "- scarcity: discontinued, limited run, sold out, brand or factory closed (with "
    "the date)\n"
    "- the original retail price, or people paying over retail\n"
    "- real hype with evidence: a Reddit or forum thread about this item (give "
    "upvotes or reply count if shown), press coverage, a celebrity seen with it\n"
    "- who made it or what it is made of, when that justifies a premium, said as "
    "a concrete fact, e.g. 'Whitehouse Cox (English leather maker since 1875)'\n"
    "- known flaws or complaints buyers mention, or a recent cheap restock\n"
    "Facts you reliably know without a search also count (url null).\n\n"
    "Item: {item}\n\n"
    "Rules:\n"
    "- At most 4 notes, strongest first. At most 12 words each, plain English.\n"
    "- NOT a note: descriptions, features, aesthetics, or 'popular with X', "
    "'remains in demand', 'high quality'. If it would not change what I charge, "
    "leave it out.\n"
    "- effect: 'up' if it supports a higher price, 'down' if lower. A high "
    "original retail price is 'up' (buyers see it as a bargain).\n"
    "- Explain any brand, material or slang term a non-expert might not know in a "
    "few words in parentheses, e.g. 'Whitehouse Cox (old English leather maker)'.\n"
    "- url: the exact page the fact came from, not a home page; null if none.\n"
    "- No notes about eBay listings or asking prices: I already see those.\n"
    "- Only after searching: if nothing that specific turned up, return an empty "
    "list. Empty is fine."
)


def _is_deep_link(url: str) -> bool:
    """A real page, not just a site's home page."""
    if not url.startswith(("http://", "https://")):
        return False
    path = url.split("://", 1)[1].split("?", 1)[0].split("#", 1)[0]
    return "/" in path.rstrip("/")


def _shorten(text: str) -> str:
    words = " ".join(str(text or "").split()).split(" ")
    if len(words) <= MAX_WORDS:
        return " ".join(words)
    return " ".join(words[:MAX_WORDS]).rstrip(",;:") + "…"


def clean(notes) -> list[dict]:
    """At most 4, short, text required, url only if it is a real page."""
    out = []
    for n in notes if isinstance(notes, list) else []:
        if isinstance(n, str):
            n = {"text": n}
        if not isinstance(n, dict):
            continue
        text = _shorten(n.get("text"))
        if not text or text.lower().rstrip(".") in ("none", "none found"):
            continue
        url = str(n.get("url") or "").strip()
        if "ebay." in url.lower():   # a listing, often our own: comps cover those
            continue
        effect = n.get("effect") if n.get("effect") in ("up", "down") else None
        out.append({"text": text, "effect": effect,
                    "url": url if _is_deep_link(url) else None})
        if len(out) == MAX_NOTES:
            break
    return out


def find_notes(ident: dict, item_id: str | None = None, ask=None) -> list[dict]:
    """The notes for one item, or [] on any failure (they are advisory)."""
    if ask is None:
        from claude_llm import ask
    item = {k: ident.get(k) for k in ("brand", "product_name", "item_type", "model",
                                      "color", "material", "size", "retail_price")
            if ident.get(k)}
    try:
        out = ask("value_notes", _PROMPT.format(item=json.dumps(item)),
                  model=config.VALUE_NOTES_MODEL, max_tokens=8000, effort="medium",
                  output_schema=_SCHEMA, item_id=item_id,
                  tools=[{"type": "web_search_20260209", "name": "web_search",
                          "max_uses": config.VALUE_NOTES_SEARCHES}])
    except Exception as e:
        print(f"WARNING: price notes failed: {type(e).__name__}: {e}")
        return []
    return clean((out or {}).get("notes"))
