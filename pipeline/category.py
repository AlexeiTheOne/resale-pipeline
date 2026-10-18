"""Pick the eBay category from what the item IS, not from a model's guess.

The copywriter used to write category_id freehand ("best-fit eBay category ID")
and nothing checked it beyond "does eBay accept this number". Live results:

  a8ad7eba  Ed Hardy duffel          -> 180000 Underwater Cameras (Fishing)
  b50bd66b  MK Apple Watch band      -> 182060 Canes & Walking Sticks (Vintage)
  8a66a4b0  PUMA women's sunglasses  -> 180957 Eyeglass Frames
  7583e1e0  Coach men's wallet       -> 45258  Women's Wallets
  94c8a02f  MK men's messenger       -> 169291 Women's Bags & Handbags

Each id was a real category, so resolve_valid_category passed it. And the aspect
hints the model was given came from a different category again (the first
Taxonomy suggestion, unchecked).

So the category is now chosen in code:

1. KNOWN — the common item types this store sells map straight to the category
   its good listings already use (ids taken from the live catalogue in
   data/ross.db: 80 bags in 169291, 9 bands in 182068, bedding in 43397/37644/
   20460/54257, ...), split by department where eBay splits them.
2. Otherwise the Taxonomy suggestions, but only one whose category path shares
   a word with the item type and does not contradict a known department.
3. Otherwise None: the caller keeps what it had and says so.

Pure apart from the `suggest` callable it is handed, so it tests without eBay.
"""
from __future__ import annotations

import re
from typing import Callable

# (pattern on the item type, {department: category_id}); "*" = any/unknown
# department. A department missing from the map means "no known-good category"
# and falls through to the guarded suggestions. First match wins, so the
# specific patterns (watch band before watch, duffel before bag) come first.
KNOWN_CATEGORIES: list[tuple[re.Pattern, dict[str, str]]] = [
    (re.compile(r"\b(smart\s*)?watch\s*(band|strap)s?\b|\b(band|strap)s?\b.*\bwatch\b", re.I),
     {"*": "182068"}),                                   # Watch Bands
    (re.compile(r"\bsunglass", re.I),
     {"women": "45246", "men": "79720", "*": "79720"}),  # Women's / Men's Sunglasses
    (re.compile(r"\b(luggage|suitcase|carry[- ]on)\b", re.I),
     {"*": "16080"}),                                    # Luggage
    (re.compile(r"\b(fragrance|perfume|cologne|parfum|eau de|toilette)\b", re.I),
     {"women": "11848", "men": "29585"}),                # Women's / Men's Fragrances
    # Wallet only as the head noun: a "Wallet Crossbody Bag" is a bag (the store
    # lists those in 169291), a "Bifold Wallet" is a wallet.
    (re.compile(r"\b(wallet|card\s*(case|holder))s?\s*$", re.I),
     {"women": "45258", "*": "45258"}),                  # Women's Wallets (men: suggest)
    # Duffels and weekenders too: 6 of the 8 live ones sit in 169291.
    (re.compile(r"\b(hand\s*bag|purse|tote|cross\s*-?body|satchel|shoulder bag|backpack|"
                r"clutch|messenger|hobo|bucket bag|belt bag|fanny pack|sling|wristlet|"
                r"duffel|duffle|weekender)s?\b", re.I),
     {"women": "169291", "*": "169291", "men": "52357"}),  # Women's / Men's Bags
    (re.compile(r"\bpillow\s*cases?\b|\bpillowcases?\b", re.I), {"*": "54257"}),
    (re.compile(r"\bshams?\b", re.I), {"*": "43397"}),
    (re.compile(r"\bduvet\b", re.I), {"*": "37644"}),
    (re.compile(r"\bsheets?\b", re.I), {"*": "20460"}),
    (re.compile(r"\b(wrist\s*)?watch(es)?\b", re.I), {"*": "31387"}),  # Wristwatches
]

# Device names that make "watch" mean a smartwatch accessory, not a wristwatch.
_SMARTWATCH = re.compile(r"\b(apple|galaxy|samsung|fitbit|garmin|smart)\s*watch", re.I)

_WOMEN = re.compile(r"\b(women'?s?|womens|ladies|lady's|female|girls'?)\b", re.I)
_MEN = re.compile(r"\b(men'?s?|mens|male|boys'?)\b", re.I)

_STOP = {"the", "and", "with", "for", "of", "in", "a", "an", "set", "piece", "new",
         "women", "woman", "men", "man", "unisex", "adult", "kid", "girl", "boy",
         "other", "accessory", "accessories", "lady", "ladie"}
# Words that name the same kind of thing in item types and category paths.
_SYNONYM = {"handbag": "bag", "purse": "bag", "duffle": "duffel", "tote": "bag",
            "crossbody": "bag", "satchel": "bag", "backpack": "bag", "clutch": "bag",
            "messenger": "bag", "hobo": "bag", "perfume": "fragrance",
            "cologne": "fragrance", "parfum": "fragrance", "eyewear": "sunglass",
            "pillowcase": "pillow", "sham": "pillow", "strap": "band",
            "wristwatch": "watch", "sheet": "sheet", "luggage": "luggage"}


def _key(word: str) -> str:
    w = word.lower().strip("'")
    for suffix in ("'s", "s'"):
        if w.endswith(suffix):
            w = w[: -len(suffix)]
    if w.endswith("es") and w[:-2].endswith(("ss", "sh", "ch", "x")):
        w = w[:-2]
    elif len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
        w = w[:-1]
    return _SYNONYM.get(w, w)


def _keys(text) -> set[str]:
    return {_key(w) for w in re.findall(r"[A-Za-z']+", str(text or ""))} - _STOP - {""}


def _dept_from(text) -> str | None:
    text = str(text or "")
    women, men = bool(_WOMEN.search(text)), bool(_MEN.search(text))
    if women != men:
        return "women" if women else "men"
    return None


def department_of(identification: dict | None, specifics: dict | None = None,
                  title: str | None = None) -> str | None:
    """'women', 'men' or None. Identification wins (it is photo-verified);
    the draft's Department and title are only used when it says nothing."""
    ident = identification or {}
    for value in (ident.get("department"), ident.get("gender"), ident.get("item_type"),
                  ident.get("product_name"), (specifics or {}).get("Department"), title):
        dept = _dept_from(value)
        if dept:
            return dept
    return None


def known_category(item_type: str | None, department: str | None) -> str | None:
    """The store's known-good category for this kind of item, or None."""
    text = str(item_type or "")
    if not text.strip():
        return None
    for pattern, by_dept in KNOWN_CATEGORIES:
        if not pattern.search(text):
            continue
        if by_dept.get("*") == "31387" and _SMARTWATCH.search(text):
            return None  # an Apple Watch case/charger is not a wristwatch
        if department and department in by_dept:
            return by_dept[department]
        if department and {"women", "men"} & by_dept.keys():
            return None  # eBay splits this type by gender; ours has no known-good id
        return by_dept.get("*")
    return None


def suggestion_path(suggestion: dict) -> list[str]:
    """Category path names, root first, from a Taxonomy suggestion."""
    cat = suggestion.get("category") or {}
    ancestors = suggestion.get("categoryTreeNodeAncestors") or []
    names = [a.get("categoryName") for a in sorted(
        ancestors, key=lambda a: a.get("categoryTreeNodeLevel") or 0)]
    return [n for n in names + [cat.get("categoryName")] if n]


def path_fits(path: list[str] | str, item_type: str | None,
              department: str | None = None) -> bool:
    """True when a category path is plausibly where this item belongs: it shares
    at least one word with the item type, and it is not the other department's
    category ("Women's Wallets" for a men's wallet)."""
    text = " > ".join(path) if isinstance(path, list) else str(path or "")
    if not _keys(text) & _keys(item_type):
        return False
    path_dept = _dept_from(text)
    return not (department and path_dept and path_dept != department)


def choose_category(identification: dict | None, specifics: dict | None = None,
                    title: str | None = None,
                    suggest: Callable[[str], list[dict]] | None = None) -> dict | None:
    """{"category_id", "source", "name"} for this item, or None when neither the
    known map nor a consistent suggestion gives one."""
    ident = identification or {}
    item_type = ident.get("item_type") or (specifics or {}).get("Type")
    dept = department_of(ident, specifics, title)

    for text in (item_type, ident.get("product_name")):
        cid = known_category(text, dept)
        if cid:
            return {"category_id": cid, "source": "known", "name": None,
                    "department": dept}

    if suggest is None or not item_type:
        return None
    query = " ".join(x for x in ({"women": "Women's", "men": "Men's"}.get(dept or ""),
                                  item_type) if x)
    try:
        suggestions = suggest(query) or []
    except Exception as e:  # the draft must not die on a Taxonomy hiccup
        print(f"⚠️ category suggestions failed ({type(e).__name__}: {e})")
        return None
    for s in suggestions:
        cid = (s.get("category") or {}).get("categoryId")
        path = suggestion_path(s)
        if cid and path_fits(path, item_type, dept):
            return {"category_id": str(cid), "source": "suggested",
                    "name": " > ".join(path), "department": dept}
    return None
