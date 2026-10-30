"""Pure message formatting: identification, pricing and draft text."""
from ebay.inventory import TitleTooLongError
from pipeline import listing_check


def _title_too_long_text(e: TitleTooLongError, item_id: str) -> str:
    """What to tell the owner when eBay would have received an over-long title.

    Spelled out in full (not str(e)[:250]) because the useful part — the fitted
    suggestion — is at the end of the message."""
    return (
        f"✂️ {item_id[:8]}: the title is {len(e.title)} characters and eBay's limit "
        f"is 80. Nothing was sent to eBay — it is no longer cut to fit, because "
        f"that chopped size and colour off the end.\n\n"
        f"Now: {e.title}\n"
        f"Suggested ({len(e.suggestion)}): {e.suggestion}\n"
        f"Drops: {', '.join(e.dropped) or 'nothing'}\n\n"
        f"/retry {item_id[:8]} to reopen the draft, then type a correction such as "
        f"'title: {e.suggestion}' and approve again.")


def _fmt(value) -> str:
    return "—" if value in (None, "", []) else str(value)


def format_identification(ident: dict) -> str:
    lines = [
        "🔍 Identified:",
        f"Brand: {_fmt(ident.get('brand'))}",
        f"Type: {_fmt(ident.get('item_type'))}",
        f"Product: {_fmt(ident.get('product_name'))}",
        f"Model/SKU: {_fmt(ident.get('model'))}",
        f"Color: {_fmt(ident.get('color'))}",
        f"Size: {_fmt(ident.get('size'))}",
        f"Condition: {_fmt(ident.get('condition'))}",
        f"Confidence: {_fmt(ident.get('confidence'))}",
    ]
    if ident.get("upc"):
        lines.append(f"UPC: {ident['upc']}")
    if ident.get("condition_flags"):
        lines.append(f"⚠️ Flags: {', '.join(ident['condition_flags'])}")
    if not ident.get("brand") and not ident.get("product_name"):
        lines.append("⚠️ Item is unconfirmed — please correct it before pricing.")
    return "\n".join(lines)


# How much the comps behind a price can be trusted (pipeline/price.py's tiers).
_EVIDENCE_LABEL = {
    "solid": "✅ solid comps",
    "thin": "🟡 thin comps — worth a look",
    "none": "❌ no usable comps",
}


def _short(text: str, n: int = 50) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[:n - 1].rstrip() + "…"


def _fmt_money(v) -> str:
    try:
        return f"{float(v):.2f}"
    except (TypeError, ValueError):
        return "?"


def _format_pricing_v2(pricing: dict) -> str:
    """Pricing v2 (pipeline/pricing_v2.py): Claude's price leads, with the comps
    it leaned on, so the price can be checked from the phone in one glance."""
    sp = pricing.get("suggested_price")
    head = f"💰 Suggested price: ${sp}" if sp is not None else "💰 Price: none — type one"
    conf = pricing.get("claude_confidence") or "none"
    if pricing.get("price_basis") == "manual":
        lines = [f"{head}  (typed; Claude said ${_fmt(pricing.get('claude_price'))}, {conf})"]
    else:
        lines = [f"{head}  (Claude, {conf} confidence)"]
    if "priced_from_asks" in (pricing.get("review_flags") or []):
        lines.append("  ⚠️ no sales found, priced from asking prices")
    ev = pricing.get("claude_evidence") or {}
    if ev.get("reason"):
        lines.append(f"  {ev['reason']}")
    for c in ev.get("comps") or []:
        kind = "sold" if c.get("source") == "sold" else "active"
        line = f"  • ${_fmt_money(c.get('price'))} {kind} — {_short(c.get('title'))}"
        if c.get("url"):
            line += f"\n    {c['url'].split('?')[0]}"
        lines.append(line)
    n_same = pricing.get("same_item_sold_count") or 0
    tail = f"  {n_same} same-item sale(s) of {pricing.get('candidate_count') or 0} comps"
    if pricing.get("formula_price"):
        tail += f"; formula ${pricing['formula_price']}"
    lines.append(tail + " (comp prices include shipping)")
    warning = pricing.get("comp_warning")
    if warning:   # the asks line above already says this part
        warning = "; ".join(w for w in warning.split("; ")
                            if w != "no sales found, priced from asking prices")
    if warning:
        lines.append(f"  ⚠️ {warning}")
    return "\n".join(lines)


def format_pricing(pricing: dict) -> str:
    if pricing.get("engine") == "v2":
        return _format_pricing_v2(pricing)
    sp = pricing.get("suggested_price")
    head = f"💰 Suggested price: ${sp}" if sp is not None else "💰 Price: none — type one"
    lines = [head]

    evidence = pricing.get("evidence")
    if evidence:
        n, disp = pricing.get("sold_count"), pricing.get("dispersion")
        detail = f"{n} sold comp(s)"
        if disp:
            detail += f", {disp}x spread"
        if pricing.get("query_rung") and pricing["query_rung"] != "exact":
            detail += f", matched on '{pricing.get('query_used')}'"
        lines.append(f"  {_EVIDENCE_LABEL.get(evidence, evidence)} — {detail}")

    lines.append(
        f"  sold median: ${_fmt(pricing.get('sold_median'))} | "
        f"active floor: ${_fmt(pricing.get('active_floor'))} | {_fmt(pricing.get('confidence'))}")

    # Only useful when there's no comp-backed price to compare it against —
    # otherwise it invites pricing off an LLM estimate, which is exactly what
    # the evidence tiers exist to stop.
    if sp is None and pricing.get("research_resale"):
        lines.append(f"  research estimate (not a comp): ${pricing['research_resale']}")
    if pricing.get("price_source_url"):
        lines.append(f"  source: {pricing['price_source_url']}")
    if pricing.get("comp_warning"):
        lines.append(f"  ⚠️ {pricing['comp_warning']}")
    return "\n".join(lines)


def _price_evidence_line(pricing: dict) -> str:
    """What a price rests on, in one line. v2 prices come from the Claude judge
    over same-item comps and have no sold median, so the v1 wording ("from 5
    solid comps", "sold median: $None") misdescribed them — a price built on one
    sale was announced as solid."""
    if pricing.get("engine") == "v2":
        sold, active = pricing.get("sold_count") or 0, pricing.get("active_count") or 0
        who = (f"set by hand (Claude said ${pricing.get('machine_price')})"
               if pricing.get("manual_price") is not None
               else f"Claude, {pricing.get('confidence')} confidence")
        return (f"{who} · "
                f"{sold} same-item sale(s), {active} active")
    return (f"sold median: ${pricing.get('sold_median')} | active floor: "
            f"${pricing.get('active_floor')} | {pricing.get('confidence')} "
            f"({pricing.get('sold_count')} comps)")


def format_draft(result) -> str:
    ident = result["identification"]
    pricing = result["pricing"]
    listing = result["listing"]

    desc = listing.get("description", "")
    short_desc = desc[:300] + ("..." if len(desc) > 300 else "")

    lines = [
        f"✓ {ident['brand']} {ident['model']}",
        f"Confidence: {ident['confidence']}",
    ]

    if ident.get("condition_flags"):
        lines.append(f"⚠️ FLAGS: {ident['condition_flags']}")

    lines += [
        "",
        f"TITLE ({len(listing['title'])} chars):",
        listing["title"],
        "",
        f"PRICE: ${listing['price']}",
        "  " + _price_evidence_line(pricing),
    ]

    if pricing.get("price_source_url"):
        lines.append(f"  price source: {pricing['price_source_url']}")

    check = listing_check.format_check(result.get("listing_check"))
    if check:
        lines += ["", check]

    lines += [
        "",
        "DESCRIPTION:",
        short_desc,
        "",
        "Tap Approve / Reject below, or type a correction (e.g. 'color is yellow not orange').",
    ]

    return "\n".join(lines)
