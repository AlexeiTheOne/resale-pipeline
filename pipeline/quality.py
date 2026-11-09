"""Listing quality standard for new listings (owner, 2026-11-09; plan in
docs/listing-performance-plan.md section 10).

Code checks, run after the draft and after every typed correction. They are
advisory, like the duplicate warning: they never block publishing. Their
issues join the AI listing check's on the review card
(listing_check.check_and_store). The ones a model can fix mechanically are
marked `redo`, and stage_draft sends those back to the copywriter once.

  - One bag type: the title, Style, Type and description name the same bag
    type. Generic words (bag, purse, handbag) are fine. A crossbody strap on
    a satchel is a feature, not a second type.
  - Description: 4-6 sentences in one paragraph, with the dimensions when
    the item's measurements are known.
  - Printed items: Color and Exterior Color = Multicolor (applied, not just
    checked: apply_multicolor).
  - Titles too similar to our own live listings get a warning.
  - Impressions at 3 days: zero_impression_alerts, run by a daily job.
"""
import re
from datetime import datetime, timedelta, timezone

import config

# --- one bag type ------------------------------------------------------------

# canonical type -> (pattern, eBay Style value, eBay Type value). Style and
# Type values are eBay's own for Women's Bags (169291, Taxonomy API
# 2026-11-09). Type has only Backpack, Briefcase, Clutch, Handbag, Pouch and
# Satchel, so most types map to Handbag.
BAG_TYPES = {
    "tote": (r"\btotes?\b", "Tote", "Handbag"),
    "satchel": (r"\bsatchels?\b|\btop[- ]handle\b", "Satchel/Top Handle Bag", "Satchel"),
    # "Shoulder" alone counts ("Skull Shoulder Crossbody Bag" is two types);
    # a shoulder strap or drop doesn't. A baguette is a shoulder bag.
    "shoulder bag": (r"\bshoulder\b(?!\s*(strap|drop))|\bbaguettes?\b", "Shoulder Bag", "Handbag"),
    "hobo": (r"\bhobos?\b", "Hobo Bag", "Handbag"),
    "crossbody": (r"\bcross[- ]?body\b(?!\s*strap)", "Crossbody", "Handbag"),
    "sling": (r"\bslings?\b", "Sling Bag", "Handbag"),
    "backpack": (r"\bback\s?packs?\b", None, "Backpack"),
    "clutch": (r"\bclutch(es)?\b|\bwristlets?\b", None, "Clutch"),
    "duffel": (r"\bduff(el|le)s?\b|\bweekenders?\b", "Duffle", "Handbag"),
    "messenger": (r"\bmessengers?\b", "Messenger Bag", "Handbag"),
    "bucket bag": (r"\bbucket\b", "Bucket & Drawstring Bag", "Handbag"),
    "belt bag": (r"\bbelt bags?\b|\bfanny packs?\b", "Belt Bag & Fanny Pack", "Handbag"),
}
_STYLE_TO_TYPE = {style.lower(): name for name, (_, style, _) in BAG_TYPES.items() if style}
_STYLE_TO_TYPE["top handle bag"] = "satchel"


def bag_types(text: str) -> list[str]:
    """The specific bag types a text names, in the order they first appear."""
    found = []
    for name, (pattern, _, _) in BAG_TYPES.items():
        m = re.search(pattern, text or "", re.I)
        if m:
            found.append((m.start(), name))
    return [name for _, name in sorted(found)]


def _spec(specifics: dict, name: str):
    value = next((v for k, v in (specifics or {}).items() if k.strip().lower() == name), None)
    if isinstance(value, list):
        value = value[0] if value else None
    return str(value).strip() if value not in (None, "") else None


def is_bag_category(category_id) -> bool:
    return str(category_id or "") in {c.strip() for c in config.QUALITY_BAG_CATEGORIES.split(",")}


def bag_type_issues(listing: dict) -> list[dict]:
    if not is_bag_category(listing.get("category_id")):
        return []
    title = listing.get("title") or ""
    specifics = listing.get("item_specifics") or {}
    issues = []
    in_title = bag_types(title)
    if len(in_title) > 1:
        issues.append(_issue("title", "fix", redo=True, message=(
            f"Title names {len(in_title)} bag types ({', '.join(in_title)}); keep one. "
            "A crossbody strap goes in Features.")))
    kind = in_title[0] if len(in_title) == 1 else None
    if kind is None:
        return issues
    style = _spec(specifics, "style")
    if style and _STYLE_TO_TYPE.get(style.lower()) not in (None, kind):
        issues.append(_issue("specifics", "fix", redo=True, message=(
            f'Style "{style}" but the title says {kind}; use "{BAG_TYPES[kind][1] or kind}".')))
    want_type = BAG_TYPES[kind][2]
    type_ = _spec(specifics, "type")
    if type_ and type_.lower() != want_type.lower():
        issues.append(_issue("specifics", "fix", redo=True, message=(
            f'Type "{type_}" but the title says {kind}; use "{want_type}".')))
    others = [t for t in bag_types(description_body(listing.get("description"))) if t != kind]
    if others:
        issues.append(_issue("description", "consider", redo=True, message=(
            f"Description also calls it {', '.join(others)}; the title says {kind}.")))
    return issues


# --- description: 4-6 sentences with dimensions --------------------------------

_POLICY_HEADINGS = ("SHIPPING", "RETURNS", "ABOUT US")
# A sentence ends at . ! ? followed by a space and a capital or digit, or the
# end. "11 x 9 x 5 in. wide" does not end a sentence; "in. It" does.
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(])")
_DIMENSIONS = re.compile(r"\d+(\.\d+)?\s*(\"|''|in\b|inch|cm\b|mm\b|\s*[x×]\s*\d)", re.I)
_DIMENSION_ASPECT = re.compile(r"width|height|depth|length|dimension", re.I)


def description_body(text: str | None) -> str:
    """The item's own description, without the store's SHIPPING/RETURNS part."""
    lines = []
    for line in (text or "").splitlines():
        if line.strip().rstrip(":").strip().upper() in _POLICY_HEADINGS:
            break
        lines.append(line)
    return "\n".join(lines).strip()


def sentences(text: str) -> list[str]:
    flat = " ".join(text.split())
    return [s for s in _SENTENCE_END.split(flat) if len(s.split()) >= 3]


def description_issues(listing: dict) -> list[dict]:
    body = description_body(listing.get("description"))
    if not body:
        return [_issue("description", "fix", redo=True, message="Description is empty.")]
    issues = []
    lines = [ln.strip() for ln in body.splitlines() if ln.strip()]
    if any(ln[:1] in "-•*✓✔" or (ln.endswith(":") and ln[:-1].isupper()) for ln in lines):
        issues.append(_issue("description", "fix", redo=True, message=(
            "Description has headings or bullets; write it as one paragraph.")))
    n = len(sentences(body))
    lo, hi = config.QUALITY_DESCRIPTION_SENTENCES
    if not lo <= n <= hi:
        issues.append(_issue("description", "fix", redo=True, message=(
            f"Description has {n} sentences; the standard is {lo}-{hi}.")))
    specifics = listing.get("item_specifics") or {}
    known = any(_DIMENSION_ASPECT.search(k) and re.search(r"\d", str(v))
                for k, v in specifics.items() if v)
    if known and not _DIMENSIONS.search(body):
        issues.append(_issue("description", "fix", redo=True, message=(
            "Description doesn't give the dimensions (they're in the specifics).")))
    return issues


# --- Multicolor for printed items ------------------------------------------------

_PRINT = re.compile(r"\b(print|printed|tattoo|floral|flowers?|graphic|camo|camouflage|leopard|cheetah|"
                    r"zebra|snake|animal|paisley|tie[- ]?dye)\b", re.I)


def is_printed(specifics: dict) -> bool:
    pattern = specifics.get("Pattern") or next(
        (v for k, v in specifics.items() if k.strip().lower() == "pattern"), None)
    values = pattern if isinstance(pattern, list) else [pattern]
    return any(v and _PRINT.search(str(v)) for v in values)


def apply_multicolor(specifics: dict, category_id=None) -> list[str]:
    """Printed item: Color and Exterior Color become Multicolor, in place.
    Exterior Color is added for bags, where it's eBay's colour filter. Returns
    the aspect names changed. The base colour belongs in the title."""
    if not isinstance(specifics, dict) or not is_printed(specifics):
        return []
    changed = []
    keys = {k.strip().lower(): k for k in specifics}
    for name in ("color", "exterior color"):
        key = keys.get(name)
        if key is None and name == "exterior color" and is_bag_category(category_id):
            key = "Exterior Color"
        if key and specifics.get(key) != "Multicolor":
            specifics[key] = "Multicolor"
            changed.append(key)
    return changed


# --- titles too similar to our own live listings ----------------------------------

_TITLE_NOISE = {"and", "with", "for", "the", "a", "of", "in", "nwt", "new", "women's", "womens",
                "women", "men's", "mens", "bag", "one", "size"}


def title_words(title: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9']+", (title or "").lower())
            if w not in _TITLE_NOISE and len(w) > 1}


def similar_titles(title: str, live: list[tuple[str, str]], exclude_id: str | None = None) -> list[tuple]:
    """(score, item_id, title) for live titles at or above the threshold, closest
    first. Score = shared words / all words (noise words left out). Measured on
    the 123 live titles (2026-11-09): 0.6 caught 7 pairs, each two near-twins
    (one sham in red and blue, two pink Ed Hardy skull crossbodies)."""
    mine = title_words(title)
    out = []
    for item_id, other in live:
        if item_id == exclude_id or not other:
            continue
        theirs = title_words(other)
        union = mine | theirs
        score = len(mine & theirs) / len(union) if union else 0.0
        if score >= config.QUALITY_SIMILAR_TITLE:
            out.append((round(score, 2), item_id, other))
    return sorted(out, reverse=True)


def similar_title_issues(listing: dict, live: list[tuple[str, str]], item_id: str | None) -> list[dict]:
    hits = similar_titles(listing.get("title") or "", live, exclude_id=item_id)
    if not hits:
        return []
    score, other_id, other = hits[0]
    more = f" (+{len(hits) - 1} more)" if len(hits) > 1 else ""
    return [_issue("title", "consider", message=(
        f'Title is {score:.0%} like live {other_id[:8]} "{other[:45]}"{more}; '
        "add what tells them apart (colour, print name).")[:config.QUALITY_MESSAGE_MAX])]


# --- all checks ------------------------------------------------------------------------

def _issue(field: str, severity: str, *, message: str, redo: bool = False) -> dict:
    return {"severity": severity, "field": field, "message": message[:config.QUALITY_MESSAGE_MAX],
            "source": "standard", "redo": redo}


def check(item: dict, live: list[tuple[str, str]] | None = None) -> list[dict]:
    """Every quality-standard issue for an item's current draft."""
    listing = item.get("listing") or {}
    if not listing:
        return []
    issues = bag_type_issues(listing) + description_issues(listing)
    if live:
        issues += similar_title_issues(listing, live, item.get("item_id"))
    return issues


def redo_instruction(issues: list[dict]) -> str | None:
    """A correction for revise_draft from the issues a model can fix, or None."""
    fixable = [i["message"] for i in issues if i.get("redo")]
    if not fixable:
        return None
    return ("Fix only these problems and change nothing else: "
            + " ".join(f"({n}) {m}" for n, m in enumerate(fixable, 1))
            + " The description must be one paragraph of 4-6 sentences that gives the dimensions, "
              "followed by the unchanged SHIPPING and RETURNS sections.")


# --- impressions at 3 days -------------------------------------------------------------------

def _published_at(item: dict) -> datetime | None:
    raw = (item.get("ebay") or {}).get("published_at")
    if not raw:
        return None
    try:
        d = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def due_for_impression_check(items: list[dict], now: datetime | None = None) -> list[dict]:
    """Published listings at least QUALITY_IMPRESSION_CHECK_DAYS old and not yet
    checked. Older than QUALITY_IMPRESSION_CHECK_MAX_DAYS is left alone, so the
    first run doesn't alert on the whole back catalogue."""
    now = now or datetime.now(timezone.utc)
    lo = timedelta(days=config.QUALITY_IMPRESSION_CHECK_DAYS)
    hi = timedelta(days=config.QUALITY_IMPRESSION_CHECK_MAX_DAYS)
    out = []
    for item in items:
        ebay = item.get("ebay") or {}
        at = _published_at(item)
        if (item.get("status") == "published" and ebay.get("listing_id") and at
                and lo <= now - at <= hi and "impressions_check" not in ebay):
            out.append(item)
    return out


def zero_impression_alerts(items: list[dict], traffic: dict) -> tuple[list[str], dict]:
    """Alert lines for listings with zero impressions, and the result to store
    per item ({item_id: {"impressions": n}}). A listing missing from the traffic
    report counts as unknown, not zero, and isn't stored, so it's tried again."""
    lines, results = [], {}
    for item in items:
        lid = str((item.get("ebay") or {}).get("listing_id"))
        row = traffic.get(lid)
        if row is None:
            continue
        n = int(row.get("impressions") or 0)
        results[item["item_id"]] = {"impressions": n}
        if n == 0:
            title = ((item.get("listing") or {}).get("title") or "")[:60]
            lines.append(f"👻 {item['item_id'][:8]} {title}: 0 impressions since it went live "
                         f"({(item.get('ebay') or {}).get('view_item_url') or lid})")
    if lines:
        lines.insert(0, f"👻 {len(lines)} new listing(s) with no impressions after "
                        f"{config.QUALITY_IMPRESSION_CHECK_DAYS} days. Check the category, title and "
                        "specifics. (eBay's traffic data lags 1-2 days, so this covers the first day or two.)")
    return lines, results
