"""Strip what must never reach a live description, in code.

The copywriter prompt already forbids these, and it still drifts. What gets
removed, and why (docs/ebay-playbook.md §1):

  * Authenticity claims — "authentic", "100% authentic", "genuine article",
    "guaranteed genuine", "not a fake/replica". The exact phrasing rights
    owners' automated VeRO sweeps look for; Michael Kors, Coach, Kate Spade
    and Ralph Lauren (most of this catalogue) are active participants, and a
    VeRO removal has no appeal path to eBay. "Genuine Leather" as a material,
    and an included "authenticity card", are facts, not claims, and stay.
  * Hashtags (#michaelkors) and keyword blocks ("Keywords: ..." / a bare run of
    comma-separated search terms) — keyword spam under eBay's search
    manipulation policy, and worthless anyway: default search does not index
    the description body.
  * Other stores' names (Ross, TJ Maxx, Macy's, Amazon, Poshmark, ...) —
    "bought at Ross", "compare at Macy's $199" invite a VeRO/IP complaint or a
    policy hit for referencing outside sales, and say nothing about the item.

The offending sentence goes, or the whole line when it is a bullet or a
heading-less fragment; a leading "Authentic"/"Genuine" adjective in front of a
name is just dropped ("Authentic Michael Kors tote" -> "Michael Kors tote").
Pure: no I/O.
"""
from __future__ import annotations

import re

_MATERIAL = (r"leather|suede|calf(skin)?|lamb(skin)?|cow(hide)?|sheep(skin)?|goat(skin)?|"
             r"snake(skin)?|python|croc(odile)?|ostrich|fur|shearling|wool|silk|cashmere|"
             r"down|gem(stone)?s?|diamonds?|stones?|pearls?|wood|silver|gold|sterling|"
             r"crystals?|glass|cotton|linen")

# A claim, not a material: "Genuine Leather" / "authenticity card" stay.
CLAIM = re.compile(
    r"\b(100\s*%\s*)?authentic(ated|ally)?\b"
    r"|\bauthenticity\b(?!\s+(card|certificate|tag)s?\b)"
    r"|\bgenuine\b(?!\s+(" + _MATERIAL + r")\b)"
    r"|\bguaranteed\s+(real|original|legit)\b"
    r"|\bnot\s+(a\s+)?(fake|replica|knock-?off|counterfeit|dupe)s?\b"
    r"|\b(100\s*%\s*)?legit\b",
    re.I)
_LEADING_ADJ = re.compile(
    r"\b(?:100\s*%\s*)?(?i:authentic|genuine)\s+(?!(?i:" + _MATERIAL + r")\b)(?=[A-Z0-9])")

# "#michaelkors", but not "Style #CF134" / "Model #MKS8042" (a code) or "&#39;".
HASHTAG = re.compile(
    r"(?<![\w&])(?<!style )(?<!model )(?<!sku )(?<!item )(?<!no\. )(?<!ref )"
    r"(?<!Style )(?<!Model )(?<!SKU )(?<!Item )(?<!No\. )(?<!Ref )#[A-Za-z][\w-]*")
_KEYWORD_LABEL = re.compile(
    r"^\W*(keywords?|tags?|search\s*(terms|tags|keywords)|seo|hashtags?)\s*:", re.I)

# Other retailers and resale marketplaces. Ross is matched case-sensitively and
# not as Ross-Simons (a jewelry brand); Amazon not as the colour "Amazon Green".
STORES = re.compile(
    r"\bRoss(?![-\s]?Simons)\b(\s+Dress\s+for\s+Less)?"
    r"|\bdd'?s\s+discounts\b"
    r"|\bT\.?\s?J\.?\s?Maxx\b|\bMarshalls\b|\bHome\s?Goods\b|\bBurlington\b"
    r"|\bNordstrom(\s+Rack)?\b|\bSaks(\s+Off\s+5th|\s+Fifth\s+Avenue)?\b"
    r"|\bMacy'?s\b|\bDillard'?s\b|\bKohl'?s\b|\bWal-?mart\b|\bCostco\b|\bSam'?s\s+Club\b"
    r"|\bAmazon(?!\s+(green|blue|parrot|lily))\b|\bPoshmark\b|\bMercari\b|\bDepop\b"
    r"|\bThredUp\b|\bThe\s+RealReal\b|\bFacebook\s+Marketplace\b|\bOfferUp\b"
    r"|\bBloomingdale'?s\b|\bBelk\b|\bJ\.?C\.?\s?Penney\b",
    re.I)
_CASE_SENSITIVE_STORES = re.compile(r"\bRoss\b|\bTarget\b")

_BULLET = re.compile(r"^\s*([-–—•*✓✔]\s*)")
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")


def _mentions_store(text: str, brand: str | None) -> str | None:
    for m in list(STORES.finditer(text)) + list(_CASE_SENSITIVE_STORES.finditer(text)):
        word = m.group(0)
        if word.lower() == "ross" and not word[0].isupper():
            continue
        if brand and word.lower() in brand.lower():
            continue  # the item's own brand (e.g. a Nordstrom house label)
        if word == "Target" and not re.search(r"\b(at|from|in|by)\s+Target\b", text):
            continue  # "target", "Target Audience": only the store as a place
        return word
    return None


def _offence(text: str, brand: str | None) -> str | None:
    m = CLAIM.search(text)
    if m:
        return f"authenticity claim {m.group(0)!r}"
    if HASHTAG.search(text):
        return "hashtag"
    store = _mentions_store(text, brand)
    if store:
        return f"store name {store!r}"
    return None


def _is_keyword_block(line: str) -> bool:
    if _KEYWORD_LABEL.match(line):
        return True
    if _BULLET.match(line) or ":" in line:
        return False
    items = [i.strip() for i in line.split(",") if i.strip()]
    # A bare run of 8+ short comma-separated terms with no sentence in it.
    return (len(items) >= 8 and all(len(i.split()) <= 3 for i in items)
            and not re.search(r"[.!?]\s", line))


def sanitize_description(text: str | None, brand: str | None = None) -> tuple[str, list[str]]:
    """Return (cleaned description, what was removed). Line structure is kept;
    only offending sentences/lines/words go."""
    removed: list[str] = []
    out_lines: list[str] = []
    for line in (text or "").splitlines():
        if _is_keyword_block(line):
            removed.append(f"keyword block: {line.strip()[:60]!r}")
            continue

        new = _LEADING_ADJ.sub("", line)
        if new != line:
            removed.append(f"authenticity adjective in {line.strip()[:60]!r}")
            line = new

        # Hashtags are dropped as tokens; a line of nothing but hashtags goes.
        if HASHTAG.search(line):
            removed.append(f"hashtags: {' '.join(HASHTAG.findall(line))[:60]}")
            stripped = HASHTAG.sub("", line)
            if not re.search(r"\w", _BULLET.sub("", stripped)):
                continue
            line = re.sub(r"[ \t]{2,}", " ", stripped).rstrip()

        why = _offence(line, brand)
        if not why:
            out_lines.append(line)
            continue
        bullet = _BULLET.match(line)
        prefix = bullet.group(0) if bullet else re.match(r"^\s*", line).group(0)
        body = line[len(prefix):]
        kept = [s for s in _SENTENCE_SPLIT.split(body) if not _offence(s, brand)]
        removed.append(f"{why} in {line.strip()[:60]!r}")
        if kept and not bullet:
            out_lines.append(prefix + " ".join(kept))
        # A bullet with an offence goes whole: half a bullet reads as an error.

    cleaned = "\n".join(out_lines)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip("\n")
    return cleaned, removed
