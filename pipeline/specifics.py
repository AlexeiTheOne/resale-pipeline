"""Item specifics as filter values, not prose — and only what the item supports.

The audit of 120 live listings found the copywriter's item_specifics carrying:

  * "Animal Print" as Pattern on prints that are not animal skins (a teddy-bear
    backpack, a77c130a; tattoo-flash prints with a tiger in them, 5fdc486f,
    182a5ab3). It is the first value of eBay's alphabetical Pattern list, which
    the draft prompt showed as "choose one: Animal Print, ...".
  * "Unisex Adults" as Department on women's items (37 live listings carry a
    Unisex value), again the model picking from the hint list, not from
    anything the identification said.
  * Comma-joined free text in single-valued filters: Color "Pink, Red, Green,
    Blue, Yellow, White, Black", Pattern "Animal Print, Graphic, Rhinestone",
    Pattern "Repeating skull wearing a top hat and crossbones motif", Strap
    Color "Navy blue with white and red Tommy Hilfiger branding". Buyers filter
    on a single word; a sentence matches no filter.
  * Values cut at eBay's 65-character limit ("Apple Watch Series 1, ...,
    Apple Watch Series") by inventory._truncate.

clean_specifics() fixes the shape without inventing anything: a comma list
becomes separate values (single-valued aspects keep the first, or Multicolor
for 3+ colours), a sentence in a filter aspect is replaced by an allowed value
it names or dropped, a value over the limit is never cut, Pattern "Animal
Print" needs an animal-skin print in the identification, and Department
follows the identification when it states one, else the category's own
gender in place of an unsupported "Unisex". Pure: the category's aspect
metadata is passed in.
"""
from __future__ import annotations

import json
import re

ASPECT_VALUE_MAX_LEN = 65  # eBay's limit on one aspect value

# Aspects whose comma-separated text is a list of separate values.
_LIST_ASPECTS = {"color", "colour", "exterior color", "main color", "lining color",
                 "handle/strap color", "strap color", "band color", "frame color",
                 "lens color", "hardware color", "pattern", "style", "theme",
                 "features", "accents", "accessories", "character", "occasion",
                 "season", "compatible model", "compatible brand", "material",
                 "exterior material", "lining material", "handle/strap material",
                 "band material", "frame material", "closure", "finish"}

# Buyer-facing filters: a value here is a word or two, never a description.
_FILTER_ASPECTS = {"color", "colour", "exterior color", "main color", "lining color",
                   "handle/strap color", "strap color", "band color", "frame color",
                   "lens color", "hardware color", "pattern", "style", "theme",
                   "department", "type", "closure", "finish", "shape", "occasion"}
_FILTER_MAX_WORDS = 4

# Single-valued unless the category metadata says otherwise.
_DEFAULT_SINGLE = {"color", "colour", "exterior color", "main color", "pattern",
                   "style", "department", "type"}

_COLOR_ASPECTS = {"color", "colour", "exterior color", "main color"}

# An animal PRINT is an animal's skin pattern. A tiger in a tattoo graphic or
# a teddy bear motif is not one.
_ANIMAL_SKIN = re.compile(
    r"\b(leopard|cheetah|zebra|snake\s*-?skin|snake|python|croc(odile)?|alligator|"
    r"giraffe|cow\s*print|cowhide|jaguar|ocelot|tiger[- ]stripe|tiger print|"
    r"reptile|lizard|ostrich|calf\s*hair)\b", re.I)
_ANIMAL_PRINT = re.compile(r"\b(animal|leopard|cheetah|zebra|snake|tiger|giraffe)\s*print\b", re.I)

_WOMEN = re.compile(r"\b(women'?s?|womens|ladies|female)\b", re.I)
_MEN = re.compile(r"\b(men'?s?|mens|male)\b", re.I)
_UNISEX = re.compile(r"\bunisex\b", re.I)

_BASIC_COLOR = re.compile(
    r"\b(rose gold|black|white|ivory|cream|beige|tan|brown|cognac|camel|gray|grey|silver|gold|"
    r"red|burgundy|maroon|pink|blush|purple|lavender|blue|navy|teal|green|"
    r"olive|yellow|orange|coral|clear|multicolor)\b", re.I)

_PLACEHOLDER = {"", "null", "none", "n/a", "na", "unknown", "tbd"}


def _is_placeholder(value) -> bool:
    return value is None or str(value).strip().lower() in _PLACEHOLDER


# A manufacturer style / part number: one token of letters and digits with at
# least one digit, e.g. R34D1B53, 87F2LEVB9U, EHH462-42-003. A product name
# ("The J Marc", "Ava Tote") is not one, and neither is a word-only code
# (LBPERCY): too easy to confuse with a name, so a person fills those.
_STYLE_CODE = re.compile(r"^(?=\S*\d)[A-Za-z0-9][A-Za-z0-9\-./]{3,24}$")


def _get_ci(d: dict, name: str):
    return next((v for k, v in (d or {}).items() if k.strip().lower() == name), None)


def style_code(specifics: dict | None, identification: dict | None) -> str | None:
    """The item's style code, from the Model specific, then the identification's
    style_code, then its model. A value with spaces counts by its first token
    when that token is a code: "XW09620 970 0IC" is style XW09620 plus colour
    and size suffixes."""
    ident = identification or {}
    for value in (_get_ci(specifics, "model"), ident.get("style_code"), ident.get("model")):
        if isinstance(value, list):
            value = value[0] if len(value) == 1 else None
        if _is_placeholder(value) or not isinstance(value, (str, int)):
            continue
        text = str(value).strip()
        if _STYLE_CODE.match(text):
            return text
        first = text.split()[0]
        if first != text and _STYLE_CODE.match(first):
            return first
    return None


def fill_mpn_from_style_code(specifics: dict, identification: dict | None) -> str | None:
    """Put the style code in MPN when MPN is empty or "Does not apply", in place.
    Model is left as it is. Returns the code written, else None.

    The draft model files the tag's style code under Model and leaves MPN null,
    so eBay got "Does not apply" and showed "MPN: Not provided" on 60 of 72 bag
    listings (eBay listing quality report, 2026-11-06), while 53 of them had
    the code under Model."""
    key = next((k for k in specifics if k.strip().lower() == "mpn"), "MPN")
    current = specifics.get(key)
    if not _is_placeholder(current) and str(current).strip().lower() != "does not apply":
        return None
    code = style_code(specifics, identification)
    if code:
        specifics[key] = code
    return code


def _identification_text(identification: dict | None) -> str:
    ident = identification or {}
    return json.dumps({k: v for k, v in ident.items()
                       if k not in ("price_evidence", "product_image_url", "search_query")},
                      ensure_ascii=False)


def identification_department(identification: dict | None) -> str | None:
    """'Women', 'Men', 'Unisex' or None — from the identification only (the
    photo-verified record), never from the copywriter's own output."""
    ident = identification or {}
    spec = {k.lower(): v for k, v in (ident.get("specifications") or {}).items()}
    for value in (ident.get("department"), ident.get("gender"), spec.get("department"),
                  spec.get("gender"), ident.get("item_type"), ident.get("product_name")):
        text = str(value or "")
        women, men = bool(_WOMEN.search(text)), bool(_MEN.search(text))
        if women != men:
            return "Women" if women else "Men"
        if _UNISEX.search(text) or (women and men):
            return "Unisex"
    return None


def _aspect_meta(category_aspects: list[dict] | None) -> dict[str, dict]:
    meta = {}
    for spec in category_aspects or []:
        name = spec.get("localizedAspectName")
        if not name:
            continue
        constraint = spec.get("aspectConstraint") or {}
        meta[name.strip().lower()] = {
            "single": constraint.get("itemToAspectCardinality") == "SINGLE",
            "selection": constraint.get("aspectMode") == "SELECTION_ONLY",
            "allowed": [v.get("localizedValue") for v in (spec.get("aspectValues") or [])
                        if v.get("localizedValue")],
        }
    return meta


def is_shoe_size_aspect(name: str) -> bool:
    """'US Shoe Size' and its variants. eBay's shoe categories name the size
    aspect this way; the draft prompt and our identification call it 'Size'."""
    return "shoe size" in str(name or "").lower()


_SIZE_NUM = r"(\d{1,2}(?:\.[05])?)"
_OTHER_SIZE_SYSTEM = re.compile(r"\b(uk|eu|eur|euro|cm|mm|jp)\b", re.I)
_KIDS_SIZE = re.compile(r"\d(?:\.5)?\s*[yck]\b", re.I)  # 5Y, 11C, 3K: kids' sizing


def shoe_size_value(text, allowed: list[str] | None = None) -> str | None:
    """The US shoe size named in `text`, in the catalog's exact form, or None.

    'Men's 10', 'US 10.5', '10 M', '10W', '10 1/2' and '10½' all give a number.
    Nothing is converted or guessed: a UK/EU size with no US one, a kids' size
    (5Y, 11C), or two different numbers with nothing saying which is US give
    None. A US Shoe Size is SELECTION_ONLY, and the catalog's values are plain
    numbers ('10', '10.5'), so the number is matched against them."""
    if _is_placeholder(text):
        return None
    s = str(text).replace("½", ".5")
    s = re.sub(r"(\d)\s*-?\s*1/2\b", r"\1.5", s)
    if _KIDS_SIZE.search(s):
        return None
    us = (re.search(r"\bUS\s*(?:men'?s|women'?s|[mw])?\s*" + _SIZE_NUM + r"(?![\d.])", s, re.I)
          or re.search(r"(?<![\d.])" + _SIZE_NUM + r"\s*(?:\(\s*)?US\b", s, re.I))
    if us:
        number = us.group(1)
    else:
        if _OTHER_SIZE_SYSTEM.search(s):
            return None
        numbers = {float(n) for n in re.findall(r"(?<![\d.])" + _SIZE_NUM + r"(?![\d.])", s)}
        if len(numbers) != 1:
            return None
        number = str(numbers.pop())
    value = float(number)
    if not 1 <= value <= 20:
        return None
    shown = f"{value:g}"
    if not allowed:
        return shown
    for a in allowed:
        try:
            if float(str(a).strip()) == value:
                return a
        except ValueError:
            continue
    return None


_SHOE_WORD = re.compile(r"\b(shoes?|sneakers?|trainers?|cleats?|footwear|runners?)\b", re.I)
_SHOE_TYPES = [
    (re.compile(r"\bcleats?\b", re.I), "cleats"),
    (re.compile(r"\b(sneakers?|trainers)\b", re.I), "sneakers"),
    (re.compile(r"\b(running|runners?|training|athletic|basketball|tennis|walking|trail|"
                r"gym|cross[- ]?training|workout|jogging|track|volleyball|pickleball)\b",
                re.I), "athletic"),
    (re.compile(r"\b(casual|skate|lifestyle|canvas|slip[- ]on|fashion)\b", re.I), "sneakers"),
]


def shoe_type_value(text, allowed: list[str] | None) -> str | None:
    """The shoe category's Type (Athletic / Cleats / Sneakers) that `text`
    names, in the catalog's casing. Only for text that is about shoes ('Running
    Shoes', 'Sneakers'), so a 'Gym Bag' never becomes Athletic; None when the
    words don't say which, or the catalog lacks that value."""
    if _is_placeholder(text) or not allowed or not _SHOE_WORD.search(str(text)):
        return None
    canonical = {a.lower(): a for a in allowed}
    for pattern, want in _SHOE_TYPES:
        if pattern.search(str(text)):
            return canonical.get(want)
    return None


def _split(value) -> list[str]:
    values = value if isinstance(value, list) else [value]
    out = []
    for v in values:
        if _is_placeholder(v):
            continue
        out += [p.strip() for p in re.split(r"[,;]", str(v)) if p.strip()]
    return out


def _allowed_in(text: str, allowed: list[str]) -> str | None:
    """The allowed value equal to `text`, else the longest one named inside it
    as a whole phrase ('Graphic print of a city skyline' -> 'Graphic')."""
    low = text.strip().lower()
    for a in allowed:
        if a.lower() == low:
            return a
    for a in sorted(allowed, key=len, reverse=True):
        if len(a) >= 3 and re.search(r"(?<!\w)" + re.escape(a.lower()) + r"(?!\w)", low):
            return a
    return None


def _shape_value(name: str, value: str, meta: dict | None) -> str | None:
    """One value made filter-shaped, or None to drop it. Never truncated."""
    value = re.sub(r"\s+", " ", value).strip()
    allowed = (meta or {}).get("allowed") or []
    if is_shoe_size_aspect(name):
        # "Men's 10" holds no allowed value as a phrase (sizes are 1-4
        # characters, below _allowed_in's 3-character floor for "10"), so the
        # size used to be dropped and the draft failed on a missing size.
        return shoe_size_value(value, allowed)
    if name == "type" and allowed:
        shoe = shoe_type_value(value, allowed)
        if shoe:
            return shoe
    is_filter = name in _FILTER_ASPECTS
    if is_filter:  # "Natural (Beige/Cream)" -> "Natural"
        value = re.sub(r"\s*\([^)]*\)", "", value).strip() or value
    too_wordy = is_filter and (len(value.split()) > _FILTER_MAX_WORDS or "(" in value)
    if allowed and (too_wordy or (meta or {}).get("selection") or len(value) > ASPECT_VALUE_MAX_LEN):
        match = _allowed_in(value, allowed)
        if match:
            return match
        if (meta or {}).get("selection"):
            return None
    if too_wordy and name in _COLOR_ASPECTS | {"lining color", "handle/strap color",
                                                 "strap color", "band color", "frame color",
                                                 "lens color", "hardware color"}:
        # "Beige/Tan Monogram with White Trim" -> "Beige": the first plain
        # colour it names, not a guess.
        m = _BASIC_COLOR.search(value)
        return m.group(0).title() if m else None
    if too_wordy or len(value) > ASPECT_VALUE_MAX_LEN:
        return None
    return value


def _dedup(values: list[str]) -> list[str]:
    seen, out = set(), []
    for v in values:
        if v.lower() not in seen:
            seen.add(v.lower())
            out.append(v)
    return out


def clean_specifics(specifics: dict, identification: dict | None = None,
                    category_aspects: list[dict] | None = None) -> tuple[dict, list[str]]:
    """Return (cleaned specifics, notes on what changed). Never adds an aspect
    the input did not have, except replacing a value by a shorter valid one."""
    meta_by_name = _aspect_meta(category_aspects)
    ident_text = _identification_text(identification)
    ident_dept = identification_department(identification)
    out, notes = {}, []

    for name, raw in (specifics or {}).items():
        key = name.strip().lower()
        meta = meta_by_name.get(key)
        if isinstance(raw, (dict, bool)):
            out[name] = raw
            continue
        if isinstance(raw, (int, float)):
            raw = str(raw)
        if raw is None or (isinstance(raw, str) and _is_placeholder(raw)):
            out[name] = raw  # left as the draft had it; publish skips placeholders
            continue

        if key in _LIST_ASPECTS:
            parts = _split(raw)
        else:
            parts = [str(v) for v in (raw if isinstance(raw, list) else [raw])
                     if not _is_placeholder(v)]

        values = _dedup([v for v in (_shape_value(key, p, meta) for p in parts) if v])

        if key == "pattern":
            if not _ANIMAL_SKIN.search(ident_text):
                # Not an animal-skin print per the identification: a tiger in
                # a tattoo graphic or a teddy bear is a graphic, not this.
                values = [v for v in values if not _ANIMAL_PRINT.search(v)]

        if key == "department":
            allowed = (meta or {}).get("allowed") or []
            category_gender = [a for a in allowed if a.lower() in ("women", "men")]
            if ident_dept in ("Women", "Men"):
                # The identification says whose it is: that wins over the model.
                values = [next((a for a in allowed if a.lower() == ident_dept.lower()), ident_dept)]
            elif ident_dept == "Unisex":
                values = [v for v in values if _UNISEX.search(v)] or values
            elif any(_UNISEX.search(v) for v in values) and len(category_gender) == 1:
                # "Unisex Adults" picked off the hint list for an item in a
                # gendered category (Women's Bags): nothing said unisex, and the
                # category already says whose it is.
                values = category_gender
            elif (len(category_gender) == 1 and values
                  and re.fullmatch(r"(wo)?men", values[0], re.I)
                  and values[0].lower() != category_gender[0].lower()):
                values = category_gender  # the model contradicting the category
            if values != (raw if isinstance(raw, list) else [raw]):
                notes.append(f"Department: {raw!r} -> {values or 'dropped'!r}")

        single = meta["single"] if meta else key in _DEFAULT_SINGLE
        if single and len(values) > 1:
            if key in _COLOR_ASPECTS and len(values) >= 3:
                multi = "Multicolor"
                if meta and meta["allowed"]:
                    multi = next((a for a in meta["allowed"] if a.lower() in ("multicolor", "multi-color")), None)
                values = [multi] if multi else values[:1]
            else:
                values = values[:1]

        before = raw if isinstance(raw, list) else [raw]
        if [str(v) for v in before] != values and key != "department":
            notes.append(f"{name}: {raw!r} -> {values or 'dropped'!r}")
        if not values:
            continue
        out[name] = values[0] if len(values) == 1 else values
    return out, notes
