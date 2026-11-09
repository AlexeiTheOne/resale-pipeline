"""/rcpt <query>: find Ross receipt lines (what we bought) and say what each is.

Read only: no AI, no writes. The query is the last digits of a SKU, a full
SKU or tag barcode, a receipt number (or part of one, with dashes), a
#receipt id, or words (searched in the description, its decode and the item
behind the line's SKU). Each line: date, days since, store, price, the raw
description, what it is, misread flags, and the item it paid for with its
status and eBay link. docs/receipt-decoding.md.
"""
import asyncio
import re
from datetime import date, datetime

from telegram import Update
from telegram.ext import ContextTypes

import config
import db
from bot import util
from pipeline import check, owned, receipt_decode

USAGE = ("Usage: /rcpt 71436 (last digits of a SKU) · /rcpt 2710-03-5447-6292-3 (receipt number) · "
         "/rcpt #7341 (receipt id) · /rcpt ed hardy duffel (words)")
MAX_WORDS = 5
_GENDER_WORDS = {"men": "men", "mens": "men", "men's": "men", "women": "women", "womens": "women",
                 "women's": "women", "ladies": "women", "boys": "boys", "boy": "boys", "girls": "girls",
                 "girl": "girls", "kids": "kids", "kid": "kids", "baby": "baby", "unisex": "unisex"}


def _receipt_number(digits: str) -> str:
    """15 digits -> dddd-dd-dddd-dddd-d, how Receipts.ReceiptNumber is stored."""
    return f"{digits[:4]}-{digits[4:6]}-{digits[6:10]}-{digits[10:14]}-{digits[14]}"


def _terms(text: str) -> tuple[list[dict], int]:
    """Words to search, ANDed: a known brand name is one term that also finds
    its register code ("ed hardy" -> EH); a gender word matches the decoded
    gender exactly ("men" never finds "women"). Returns (terms, dropped)."""
    words = [w for w in re.findall(r"[a-z0-9&'.]+", text.lower()) if len(w.strip("'.")) >= 2]
    words = [w.strip("'.") for w in words]
    terms, i = [], 0
    names = sorted(receipt_decode.BRAND_CODES, key=lambda n: -len(n.split()))
    while i < len(words):
        for name in names:
            n = name.split()
            if words[i:i + len(n)] == n:
                terms.append({"text": name, "codes": list(receipt_decode.BRAND_CODES[name]), "gender": None})
                i += len(n)
                break
        else:
            w = words[i]
            terms.append({"text": w, "codes": [], "gender": _GENDER_WORDS.get(w)})
            i += 1
    return terms[:MAX_WORDS], max(0, len(terms) - MAX_WORDS)


def parse_query(text: str) -> tuple:
    """(kind, value) or ("error", message). kinds: receipt_id, receipt_number,
    receipt_number_part, sku, sku_suffix, words (value: (terms, dropped))."""
    q = (text or "").strip()
    if not q:
        return "error", USAGE
    m = re.fullmatch(r"#\s*(\d+)", q)
    if m:
        return "receipt_id", int(m.group(1))
    if q == "#":
        return "error", USAGE
    # pasted back from a reply: "SKU …371436", "…371436"
    q = re.sub(r"^(sku\s*[:#]?\s*|…|\.{2,})", "", q, flags=re.I).strip()
    compact = re.sub(r"\s+", "", q)
    if "-" in compact and re.fullmatch(r"[\d-]+", compact):
        digits = compact.replace("-", "")
        if len(digits) == 15:
            return "receipt_number", _receipt_number(digits)
        if len(digits) >= 4:
            return "receipt_number_part", digits
        return "error", "Give at least 4 digits of the receipt number."
    if compact.isdigit():
        n = len(compact)
        if n == 18:
            sku = owned.ross_code(compact)
            return ("sku", sku) if sku else ("error", "That's 18 digits but not a Ross tag barcode (they start 400).")
        if n == 12:
            return "sku", compact
        if n == 15:
            return "receipt_number", _receipt_number(compact)
        if 3 <= n <= 11:
            return "sku_suffix", compact
        if n < 3:
            return "error", "Give at least 3 digits of the SKU."
        return "error", (f"{n} digits isn't a Ross code (12, or 18 on the tag barcode) or a receipt number (15).")
    terms, dropped = _terms(q)
    if not terms:
        return "error", USAGE
    return "words", (terms, dropped)


# --- the reply ------------------------------------------------------------------------------------

def _age(d: str | None, today: date) -> str:
    if not d:
        return "no date"
    try:
        then = date.fromisoformat(str(d)[:10])
    except ValueError:
        return str(d)
    days = (today - then).days
    when = f"{then:%b} {then.day}" + (f" {then.year}" if then.year != today.year else "")
    ago = "today" if days == 0 else ("1 day ago" if days == 1 else f"{days} days ago")
    return f"{when} ({ago})"


def _store(line: dict, cities: dict) -> str:
    num = line.get("store_number")
    city = receipt_decode.clean_city(line.get("store_city")) or cities.get(num)
    if num and city:
        return f"{city} (store {num})"
    return f"store {num}" if num else (city or "store ?")


def _detail(line: dict) -> dict | None:
    """The saved decode of the line's description (d_<column> fields)."""
    if not line.get("d_source"):
        return None
    return {c: line.get(f"d_{c}") for c in ("category", "item_type", "gender", "color", "brand_guess",
                                             "product_model", "confidence", "source", "model")}


def what_it_is(line: dict, items: list) -> str:
    """Our item's identity, else the saved decode, else a sure rules reading
    (not saved yet), else "not decoded yet"."""
    if items:
        ident = items[0][0]
        text = owned.what_it_is(ident) or receipt_decode.describe(receipt_decode.item_fields(ident, None))
        return f"{text} (our item)" if text else "our item"
    saved = _detail(line)
    if saved:
        return receipt_decode.describe(saved) or "not decoded yet"
    r = receipt_decode.decode_rules(line.get("description") or "")
    if r["tier"] == "high":
        return receipt_decode.describe(receipt_decode.rules_fields(r)) + " (rules, not saved)"
    return "not decoded yet"


def _hit_line(hit: dict) -> str:
    text = owned.hit_text(hit).replace(f" · {owned.ROSS}", "")
    return text + (f" · {hit['url']}" if hit.get("url") else "")


def _links(sku: str, hits: dict) -> str:
    found = hits.get(sku) or {}
    items, checks = found.get("items") or [], found.get("checks") or []
    if len(items) == 1:
        return "📦 " + _hit_line(items[0][1])
    if items:
        return f"📦 {len(items)} items share this code: " + " | ".join(_hit_line(h) for _, h in items)
    if checks:
        return "🔎 checked before buying: " + _hit_line(checks[0][1])
    return "no item linked"


def _flags(line: dict, linked: bool) -> list[str]:
    receipt = {"subtotal": line.get("subtotal"), "total": line.get("total"), "tax": line.get("tax"),
               "line_count": line.get("line_count"), "lines_sum": line.get("lines_sum")}
    reasons = receipt_decode.misread_reasons(line, receipt, linked=linked)
    bad = [words for code, words in reasons if code not in receipt_decode.INFO]
    info = [words for code, words in reasons if code in receipt_decode.INFO]
    out = []
    if bad:
        out.append("⚠️ misread? " + "; ".join(bad))
    if info:
        out.append("✏️ " + "; ".join(info))
    return out


def _header(receipt: dict, cities: dict, today: date) -> str:
    bits = [f"🧾 Receipt #{receipt['receipt_id']}", receipt.get("receipt_number") or "no number",
            _age(receipt.get("receipt_date"), today), _store(receipt, cities)]
    if receipt.get("total") is not None:
        bits.append(f"total {check._usd(receipt['total'])}")
    problems = []
    if not receipt.get("receipt_date"):
        problems.append("no date")
    if receipt.get("total") is None:
        problems.append("no total")
    gap = receipt.get("subtotal_gap")
    if gap is not None and abs(float(gap)) > 1:
        problems.append(f"lines differ from the subtotal by {check._usd(abs(float(gap)))}")
    if receipt.get("has_returns"):
        problems.append("has returns")
    if problems:
        bits.append("⚠️ " + ", ".join(problems))
    return " · ".join(bits)


def _label(kind: str, value) -> str:
    if kind == "sku_suffix":
        return f"SKU …{value}"
    if kind == "sku":
        return f"SKU {value}"
    if kind == "receipt_number_part":
        return f"receipt number containing {value}"
    if kind == "words":
        return '"' + " ".join(t["text"] for t in value[0]) + '"'
    return str(value)


def format_reply(kind: str, value, res: dict, hits: dict, today: date | None = None) -> list[str]:
    today = today or datetime.now(check.EASTERN).date()
    cities = receipt_decode.store_cities(res.get("store_rows") or [])
    lines, total = res.get("lines") or [], res.get("total") or 0
    receipt = res.get("receipt")
    out = []
    if kind in ("receipt_id", "receipt_number"):
        if not receipt:
            return [f"🧾 No receipt {'#' + str(value) if kind == 'receipt_id' else value}."]
        out.append(_header(receipt, cities, today))
        if not lines:
            out.append("No lines were read from this receipt.")
    elif not lines:
        return [f"🧾 No receipt lines for {_label(kind, value)}."]
    else:
        out.append(f"🧾 {total} receipt line{'' if total == 1 else 's'} for {_label(kind, value)}"
                   + (" (newest first)" if total > 1 else ""))
    if kind == "words" and value[1]:
        out.append(f"(searched the first {MAX_WORDS} words only)")
    whole = kind in ("receipt_id", "receipt_number")
    for n, ln in enumerate(lines, 1):
        sku = ln.get("sku") or ""
        found = (hits.get(sku) or {}).get("items") or []
        price = check._usd(ln["price"]) if ln.get("price") is not None else "no price"
        desc = (ln.get("description") or "").strip()
        out.append("")
        if whole:
            out.append(f"{ln.get('line_index')}. {price} · \"{desc}\" · SKU {sku or '?'}")
        else:
            out.append(f"{n}. {_age(ln.get('receipt_date'), today)} · {_store(ln, cities)} · {price}")
            out.append(f"   \"{desc}\" · SKU {sku or '?'} · receipt #{ln.get('receipt_id')}")
        out.append(f"   = {what_it_is(ln, found)}")
        out += [f"   {f}" for f in _flags(ln, bool(found))]
        out.append(f"   {_links(sku, hits)}")
    if total > len(lines):
        more = total - len(lines)
        out.append("")
        out.append(f"…and {more} more on this receipt." if whole else
                   f"…and {more} more. Add a word or more digits.")
    return out


async def rcpt_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    kind, value = parse_query(" ".join(context.args or []))
    if kind == "error":
        await util._safe_reply(message, value)
        return
    if config.DB_BACKEND != "sqlserver":
        await util._safe_reply(message, "Receipts are on SQL Server only.")
        return
    try:
        search = value[0] if kind == "words" else value
        res = await asyncio.to_thread(db.receipt_lines_search, kind, search, config.RCPT_MAX_LINES)
        skus = {ln["sku"] for ln in res.get("lines") or [] if ln.get("sku")}
        hits = await asyncio.to_thread(owned.ross_hits, skus) if skus else {}
    except Exception as e:
        util._record_error("rcpt", e)
        await util._safe_reply(message, f"⚠️ Couldn't read receipts: {type(e).__name__}: {str(e)[:150]}")
        return
    for chunk in util._chunk_lines(format_reply(kind, value, res, hits)):
        await util._safe_reply(message, chunk, disable_web_page_preview=True)
