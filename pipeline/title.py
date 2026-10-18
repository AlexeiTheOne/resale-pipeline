"""Keep an eBay title within 80 characters without losing the words buyers search.

eBay rejects a title over 80 characters (Trading API error 70). The copywriter
model is told the limit but does not reliably count, and the old safety net —
inventory._truncate_title — cut the overflow off the END at a word boundary.
The draft prompt puts brand and product name first and size/colour last, so the
end is exactly where the filter words live: seven live titles lost their size,
colour or model code that way ("...38/40/41mm Blush Pink" gone, "...Black Multi
One" left behind). See docs/ebay-playbook.md §1.

fit_title is the deterministic fallback: it removes the LEAST useful words
first — repeated words, connectors, a generic "Bag" next to a specific "Tote",
"One Size", condition words — then descriptive words from the right, and never
the brand, the item's head noun, the model code, the size or the colour. It is
pure (no I/O, no model), so it is safe to run over the whole catalogue in a
report before anything is pushed live.

The model-driven fitters (the existing Gemini copywriter today, a Claude-based
one later) plug in through ensure_title_fits(refit=...): a refit callable gets
the first shot, and fit_title only runs if the refit is missing, fails, or
still comes back too long.
"""
from __future__ import annotations

import re
from typing import Callable

TITLE_MAX_LEN = 80

# Shared wording for every prompt that writes or rewrites a title, so the draft
# and the revision (and any later fitter) state the same rule the same way.
TITLE_RULES = (
    f"The title must be AT MOST {TITLE_MAX_LEN} characters including spaces — a hard "
    "eBay limit; anything longer is rejected or cut. Count the characters. "
    "Priority order of what to keep: Brand, item type, model/style code, size, "
    "colour, then material, then department (Women's/Men's). Size and colour must "
    "NOT be the first words to go when shortening: drop repeated words, filler "
    "('with', 'and', 'New'), redundant category nouns ('Tote Bag' -> 'Tote') and "
    "decorative adjectives first. Never use ALL CAPS."
)

# Words that carry no search value in a title. "for" is deliberately absent:
# a watch band MUST say "for Apple Watch" (playbook §1), so dropping it would
# turn a compliant title into brand misuse.
_FILLER = {"with", "and", "&", "+", "the", "a", "an", "of", "in", "-", "/", "|"}

_CONDITION = {"new", "brand", "nwt", "nwot", "nib", "nwb"}

# Generic nouns that eBay already synonym-expands (handbag/bag/purse/pocketbook,
# SIGIR eCom 2019). Next to a specific style noun they are pure waste.
_GENERIC_NOUNS = {"bag", "bags", "purse", "purses", "handbag", "handbags",
                  "pocketbook", "set"}
_SPECIFIC_NOUNS = {"tote", "backpack", "crossbody", "satchel", "clutch",
                   "weekender", "hobo", "wristlet", "wallet", "duffel", "duffle",
                   "sling", "bucket", "messenger", "belt", "band", "strap",
                   "watch", "sunglasses", "necklace", "bracelet", "earrings",
                   "ring", "scarf", "hat", "cap", "dress", "top", "shirt",
                   "jacket", "coat", "sweater", "pants", "jeans", "shorts",
                   "skirt", "sneakers", "boots", "sandals", "heels", "shoes",
                   "blanket", "throw", "pillow", "luggage", "suitcase"}

_SIZE_WORDS = {"xxs", "xs", "s", "m", "l", "xl", "xxl", "xxxl", "1x", "2x", "3x",
               "small", "medium", "large", "petite", "plus", "mini", "oversized",
               "tall", "short", "regular", "slim", "wide"}

_DEPARTMENT = {"women's", "womens", "women", "men's", "mens", "men", "unisex",
               "girls", "girls'", "boys", "boys'", "kids", "kid's", "adult",
               "adults"}

_SIZE_TOKEN = re.compile(
    r"^\d+(\.\d+)?(/\d+(\.\d+)?)*\s*(mm|cm|in|inch|\"|oz|ml|l|qt|ft|x)?$", re.I)
_UNIT_TOKEN = re.compile(r"\d.*(mm|cm|in|\"|oz|ml)$", re.I)

# Priority tiers; lower is dropped first.
_T_DUP, _T_FILLER, _T_GENERIC, _T_ONESIZE, _T_CONDITION = 0, 1, 2, 3, 4
_T_DESCRIPTIVE, _T_SECONDARY, _T_PROTECTED = 5, 6, 9


def _norm(word: str) -> str:
    """Comparison key: lowercase, outer punctuation stripped, plural/possessive
    folded so 'Bags'/'Bag' and "Women's"/'Women' count as the same word."""
    w = word.lower().strip(".,;:!()[]{}\"'")
    for suffix in ("'s", "s'"):
        if w.endswith(suffix):
            w = w[: -len(suffix)]
    if len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
        w = w[:-1]
    return w


def _words(value) -> list[str]:
    """Lowercased word keys from an attribute value, ignoring any parenthetical
    ('Black Multi (black background with grey monogram...)' -> black, multi)."""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        out = []
        for v in value:
            out += _words(v)
        return out
    text = re.sub(r"\(.*?\)", " ", str(value))
    return [_norm(w) for w in re.split(r"[\s,/;]+|(?<=\w)-(?=\w)", text) if w.strip()]


def _phrase_positions(tokens: list[str], phrase) -> set[int]:
    """Indices of every place `phrase` occurs as a contiguous token run."""
    keys = _words(phrase)
    keys = [k for k in keys if k]
    if not keys:
        return set()
    norm = [_norm(t) for t in tokens]
    hits = set()
    for i in range(len(norm) - len(keys) + 1):
        if norm[i:i + len(keys)] == keys:
            hits.update(range(i, i + len(keys)))
    return hits


def _first(*values):
    for v in values:
        if v not in (None, "", [], "null", "None"):
            return v
    return None


def _head_noun(item_type: str | None) -> str | None:
    """The item's most specific noun: 'Weekender Travel Tote Bag' -> tote,
    'Apple Watch Band Gift Set' -> band. Generic nouns only if nothing else."""
    words = [w for w in _words(item_type) if w]
    for w in reversed(words):
        if w not in _GENERIC_NOUNS and w in _SPECIFIC_NOUNS:
            return w
    for w in reversed(words):
        if w not in _GENERIC_NOUNS:
            return w
    return words[-1] if words else None


def _tiers(tokens: list[str], identification: dict, specifics: dict) -> list[int]:
    ident = identification or {}
    spec = specifics or {}
    norm = [_norm(t) for t in tokens]
    n = len(tokens)
    tier = [_T_DESCRIPTIVE] * n

    brand = _first(spec.get("Brand"), ident.get("brand"))
    brand_pos = _phrase_positions(tokens, brand)
    protected: set[int] = set(brand_pos)

    codes = {_norm(c) for c in (spec.get("Model"), spec.get("MPN"),
                                ident.get("model"), ident.get("style_number"))
             if c and re.search(r"\d", str(c))}
    head = _head_noun(_first(ident.get("item_type"), spec.get("Type"), spec.get("Style")))

    colour = set(_words(spec.get("Color"))) | set(_words(ident.get("color")))
    # Only short size values: "Compatible with 38mm, 40mm, and 41mm Apple Watch
    # cases" would otherwise protect "with", "Apple" and "Watch" as sizes.
    size_words = set()
    for value in (spec.get("Size"), ident.get("size")):
        if value and len(_words(value)) <= 4:
            size_words |= set(_words(value))
    size_words -= {"one", "size", "to"}

    secondary = (set(_words(spec.get("Material"))) | set(_words(ident.get("material")))
                 | set(_words(ident.get("item_type"))) | _DEPARTMENT
                 | set(_words(spec.get("Department"))))

    specific_present = any(w in _SPECIFIC_NOUNS and w not in _GENERIC_NOUNS for w in norm)

    # "for Apple Watch": on a band or accessory the compatibility phrase is
    # required wording (playbook §1) — losing "for" turns it into brand misuse.
    for i, w in enumerate(norm):
        if w == "for":
            protected.update(range(i, min(i + 3, n)))

    for i, (tok, w) in enumerate(zip(tokens, norm)):
        if i in protected:
            continue
        if w in codes or (head and w == head):
            protected.add(i)
        elif w in colour and w not in _FILLER:
            protected.add(i)
        elif w in size_words and w not in _FILLER:
            protected.add(i)
        elif _SIZE_TOKEN.match(tok) and ("/" in tok or _UNIT_TOKEN.match(tok)):
            protected.add(i)

    for i, w in enumerate(norm):
        if i in protected:
            tier[i] = _T_PROTECTED
        elif w in _FILLER or not re.search(r"\w", w):
            tier[i] = _T_FILLER
        elif w in _GENERIC_NOUNS and specific_present:
            tier[i] = _T_GENERIC
        elif w in _CONDITION:
            tier[i] = _T_CONDITION
        elif w in secondary or w in _SIZE_WORDS:
            tier[i] = _T_SECONDARY

    # "One Size" says nothing a buyer types.
    for i in range(n - 1):
        if norm[i] == "one" and norm[i + 1] == "size":
            tier[i] = tier[i + 1] = _T_ONESIZE

    # Repeats: keep one copy — the one sitting in the longest run of protected
    # words ('Black Floral' the colour over 'Floral Print'), else the first.
    def run_len(i):
        if tier[i] != _T_PROTECTED:
            return 0
        lo = hi = i
        while lo > 0 and tier[lo - 1] == _T_PROTECTED:
            lo -= 1
        while hi < n - 1 and tier[hi + 1] == _T_PROTECTED:
            hi += 1
        return hi - lo + 1

    seen: dict[str, list[int]] = {}
    for i, w in enumerate(norm):
        if re.search(r"\w", w) and w not in _FILLER:
            seen.setdefault(w, []).append(i)
    for w, idxs in seen.items():
        if len(idxs) < 2:
            continue
        keep = max(idxs, key=lambda i: (run_len(i), -i))
        for i in idxs:
            if i != keep and i not in brand_pos:
                tier[i] = _T_DUP
    return tier


def _join(tokens: list[str], keep: list[bool]) -> str:
    return " ".join(t for t, k in zip(tokens, keep) if k)


def fit_title(title: str, identification: dict | None = None,
              specifics: dict | None = None, limit: int = TITLE_MAX_LEN
              ) -> tuple[str, list[str]]:
    """Shorten `title` to at most `limit` characters, dropping low-priority words
    first. Returns (title, dropped_words). A title already within the limit is
    returned unchanged (whitespace-normalised) with nothing dropped."""
    tokens = (title or "").split()
    if len(" ".join(tokens)) <= limit:
        return " ".join(tokens), []

    tiers = _tiers(tokens, identification or {}, specifics or {})
    keep = [True] * len(tokens)
    order = sorted(range(len(tokens)), key=lambda i: (tiers[i], -i))
    dropped_order: list[int] = []
    for i in order:
        if len(_join(tokens, keep)) <= limit:
            break
        if tiers[i] >= _T_PROTECTED or not keep[i]:
            continue
        group = [i]
        if tiers[i] == _T_ONESIZE:  # "One Size" goes as a pair or not at all
            group = [j for j in (i - 1, i + 1)
                     if 0 <= j < len(tokens) and tiers[j] == _T_ONESIZE] + [i]
        for j in group:
            keep[j] = False
            dropped_order.append(j)

    # Still too long with only protected words left: drop protected words from
    # the right, but never the brand or the head noun.
    if len(_join(tokens, keep)) > limit:
        brand = _first((specifics or {}).get("Brand"), (identification or {}).get("brand"))
        never = _phrase_positions(tokens, brand)
        head = _head_noun((identification or {}).get("item_type"))
        for i in reversed(range(len(tokens))):
            if len(_join(tokens, keep)) <= limit:
                break
            if keep[i] and i not in never and _norm(tokens[i]) != head:
                keep[i] = False
                dropped_order.append(i)

    # Greedy drops can overshoot (a short word went, then a long one was still
    # needed). Put back any real descriptive word that fits, most valuable
    # first. Zero-value words stay out — restoring the "One" of "One Size" on
    # its own is how the old truncation produced "...Black Multi One".
    for i in reversed(dropped_order):
        if tiers[i] < _T_DESCRIPTIVE:
            continue
        keep[i] = True
        if len(_join(tokens, keep)) > limit:
            keep[i] = False

    result = _join(tokens, keep)
    if len(result) > limit:  # a single token longer than the limit; nothing else to do
        result = result[:limit].rstrip()
    return result, [tokens[i] for i in range(len(tokens)) if not keep[i]]


def ensure_title_fits(title: str, identification: dict | None = None,
                      specifics: dict | None = None,
                      refit: Callable[[str], str | None] | None = None,
                      ) -> tuple[str, list[str], str]:
    """The seam every title producer goes through.

    Returns (title, dropped_words, how) where `how` is "ok" (already fitted),
    "refit" (the model shortened it) or "fallback" (fit_title did). `refit` is
    any callable that takes the over-long title and returns a shorter one — the
    Gemini copywriter today, a Claude-based fitter later. A refit that raises,
    returns nothing, or is still over the limit is ignored and fit_title runs.
    """
    title = " ".join((title or "").split())
    if len(title) <= TITLE_MAX_LEN:
        return title, [], "ok"
    if refit is not None:
        try:
            candidate = " ".join((refit(title) or "").split())
        except Exception as e:  # a failed re-ask must not lose the draft
            print(f"⚠️ title refit failed ({type(e).__name__}: {e}); using fit_title")
            candidate = ""
        if candidate and len(candidate) <= TITLE_MAX_LEN:
            before = {_norm(w) for w in title.split()}
            after = {_norm(w) for w in candidate.split()}
            dropped = [w for w in title.split() if _norm(w) in before - after]
            return candidate, dropped, "refit"
    fitted, dropped = fit_title(title, identification, specifics)
    return fitted, dropped, "fallback"


# --- "Band for Apple Watch" ----------------------------------------------------
#
# A band made to fit an Apple Watch must be titled "<Brand> ... Band for Apple
# Watch ...", never "<Brand> Apple Watch Band": the second reads as an Apple
# product made by Michael Kors / Kate Spade, which is brand misuse in eBay's
# terms and a VeRO hook for Apple (playbook §1). The prompt says so, and 9 of 15
# band titles in the catalogue still came back as "... Apple Watch Band ..."
# (b50bd66b, 69881d8e, 00c8d805, dbc5c441, ...). So it is enforced here.

_DEVICE = re.compile(
    r"\b(?:Apple(?:\s*/\s*(?:Samsung\s+)?Galaxy)?|Samsung\s+Galaxy|Galaxy|Fitbit|Garmin)"
    r"\s+Watch\b", re.I)
_BAND_WORD = re.compile(r"\b(band|bands|strap|straps|bracelet)\b", re.I)
_FOR_DEVICE = re.compile(r"\bfor\s+(?:an?\s+|the\s+)?" + _DEVICE.pattern, re.I)


def is_smartwatch_band(identification: dict | None, specifics: dict | None = None,
                       title: str | None = None) -> bool:
    """A band/strap made for a smartwatch (Apple Watch above all), as opposed to
    a watch, or a band for an ordinary watch."""
    ident = identification or {}
    spec = specifics or {}
    kind = " ".join(str(v) for v in (ident.get("item_type"), ident.get("product_name"),
                                     spec.get("Type")) if v)
    if not _BAND_WORD.search(kind) and not re.search(r"\bband\b", str(title or ""), re.I):
        return False
    text = " ".join(str(v) for v in (kind, title, spec.get("Compatible Brand"),
                                     spec.get("Compatible Model"), ident.get("search_query")) if v)
    return bool(_DEVICE.search(text) or re.search(r"\bsmart\s*watch\b", kind, re.I))


def enforce_band_for_device(title: str, identification: dict | None = None,
                            specifics: dict | None = None) -> str:
    """Rewrite '<Brand> Apple Watch Band ...' as '<Brand> Band for Apple Watch ...'.
    A title that already says 'for Apple Watch', or an item that is not a
    smartwatch band, is returned unchanged (whitespace-normalised). The result
    may exceed 80 characters; the caller fits it afterwards."""
    title = " ".join((title or "").split())
    if not title or _FOR_DEVICE.search(title) or not is_smartwatch_band(identification, specifics, title):
        return title
    m = _DEVICE.search(title)
    if m:
        device = re.sub(r"\s+", " ", m.group(0))
        rest = (title[:m.start()] + " " + title[m.end():])
    else:
        device = "Apple Watch"
        rest = title
    rest = " ".join(rest.split())
    words = rest.split()
    band_at = next((i for i, w in enumerate(words) if _BAND_WORD.fullmatch(w.strip(",.;:"))), None)
    if band_at is None:
        # No band noun at all: put "Band for <device>" after the brand.
        brand = str((specifics or {}).get("Brand") or (identification or {}).get("brand") or "")
        n = len(brand.split()) if brand and rest.lower().startswith(brand.lower()) else 0
        words[n:n] = ["Band", "for", *device.split()]
        return " ".join(words)
    insert = band_at + 1
    if insert < len(words) and words[insert].lower() == "set":
        insert += 1
    words[insert:insert] = ["for", *device.split()]
    return " ".join(words)
