"""/revise: change a live listing's title, item specifics and description in
place (the same eBay listing). Owner only, one listing at a time.

    /revise d7432192
    title: New title
    Style: Shoulder Bag
    Theme: Skull, Tattoo     <- a comma gives several values where eBay allows several
    Pattern: -               <- a dash removes the specific
    description:
    The new description, to the end of the message.

A new description with no SHIPPING/RETURNS section keeps the listing's current
one underneath.

Nothing is written until the owner taps Apply under the preview. The "before"
in the preview is what eBay shows now, read live. On Apply:
  1. eBay and the DB are read again; if either changed since the preview,
     nothing is written.
  2. Title and specifics go to eBay in one inventory update.
  3. The description goes to eBay (offer update). If it fails, step 2 is put
     back.
  4. The DB listing (title, specifics, description) is saved in one write, and
     only after every eBay write succeeded.
Failures are logged (/errors and bot.log).
"""
import asyncio
import copy
import hashlib
import html
import json
import re
import secrets
import time

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

import config
import db
from bot import state, util
from ebay import inventory, taxonomy
from pipeline import sync

PREVIEW_TTL = 30 * 60   # seconds an Apply button stays valid
REMOVE = "-"
USAGE = ("Usage, all in one message:\n"
         "/revise <id>\n"
         "title: New title\n"
         "Style: Shoulder Bag\n"
         "Theme: Skull, Tattoo   (comma = several values, where eBay allows it)\n"
         "Pattern: -   (removes it)\n"
         "description:\n"
         "New description text, to the end of the message.\n\n"
         "Every line is optional except the first.\n\n"
         "Several listings: separate them with a line of ---, each block starting with its id:\n"
         "/revise c646d6f0\ntitle: ...\n---\n5fdc486f\ntitle: ...\nExterior Color: Multicolor\n"
         "One preview for all, one Apply.")


class ReviseError(ValueError):
    """A problem the owner can fix; the message is shown as is."""


def is_owner(user) -> bool:
    return user is not None and user.id in config.OWNER_USER_IDS


# --- parsing -------------------------------------------------------------------

_FIELD = re.compile(r"^\s*([^:\n]{1,60}?)\s*:\s*(.*)$")


def parse(text: str) -> tuple[list[str], dict]:
    """Split a /revise message into the id argument(s) and the requested change:
    {"title", "set" (name -> raw value), "remove" (names), "description"}."""
    lines = (text or "").splitlines()
    args = lines[0].split()[1:] if lines else []
    change = {"title": None, "set": {}, "remove": [], "description": None}
    seen = set()
    rest = lines[1:]
    for i, line in enumerate(rest):
        if not line.strip():
            continue
        m = _FIELD.match(line)
        if not m:
            raise ReviseError(f"Can't read this line (expected 'Name: value'):\n{line.strip()}")
        name, value = m.group(1).strip(), m.group(2).strip()
        key = name.lower()
        if key in seen:
            raise ReviseError(f"'{name}' is given twice.")
        seen.add(key)
        if key == "description":
            change["description"] = "\n".join([value] + rest[i + 1:]).strip()
            if not change["description"]:
                raise ReviseError("'description:' has no text after it.")
            break
        if not value:
            raise ReviseError(f"'{name}:' has no value. Use '{name}: -' to remove it.")
        if key == "title":
            change["title"] = value
        elif value == REMOVE:
            change["remove"].append(name)
        else:
            change["set"][name] = value
    return args, change


_SEPARATOR = re.compile(r"^\s*-{3,}\s*$")


def split_blocks(text: str) -> list[tuple[list[str], dict]]:
    """One (args, change) per listing. A message without --- lines is one
    listing, exactly as parse() reads it. With them, each block after the
    first starts with its id on its own line; the first block's id is on the
    /revise line (or, if not, on the block's first line)."""
    lines = (text or "").splitlines()
    if not any(_SEPARATOR.match(ln) for ln in lines[1:]):
        return [parse(text)]
    head = lines[0].split()[1:] if lines else []
    blocks, current = [], []
    for ln in lines[1:]:
        if _SEPARATOR.match(ln):
            blocks.append(current)
            current = []
        else:
            current.append(ln)
    blocks.append(current)
    out = []
    for n, block in enumerate(blocks):
        body = list(block)
        if n == 0 and head:
            ids = head
        else:
            while body and not body[0].strip():
                body.pop(0)
            if not body:
                raise ReviseError(f"Listing {n + 1} is empty.")
            ids = body.pop(0).split()
        if len(ids) != 1 or ":" in ids[0]:
            raise ReviseError(f"Listing {n + 1} must start with one id on its own line.")
        out.append(parse("/revise " + ids[0] + "\n" + "\n".join(body)))
    return out


def _multi_aspects(category_id: str | None) -> set[str]:
    """Aspect names (lowercase) that eBay allows several values for here."""
    if not category_id:
        return set()
    try:
        specs = taxonomy.category_aspects(str(category_id))
    except Exception:
        return set()
    return {s["localizedAspectName"].lower() for s in specs
            if s.get("localizedAspectName")
            and (s.get("aspectConstraint") or {}).get("itemToAspectCardinality") == "MULTI"}


def _split_values(set_raw: dict, multi: set[str]) -> dict:
    """'Skull, Tattoo' becomes two values where eBay takes several; anywhere
    else a comma is part of the one value."""
    out = {}
    for name, value in set_raw.items():
        if name.lower() in multi and "," in value:
            out[name] = [v.strip() for v in value.split(",") if v.strip()]
        else:
            out[name] = value
    return out


def _is_policy_heading(line: str) -> bool:
    return line.strip().rstrip(":").strip().upper() in inventory._POLICY_HEADINGS


def with_footer(new_text: str, old_text: str | None) -> str:
    """The new description, plus the current SHIPPING/RETURNS/... section when
    the new text has none of its own."""
    if any(_is_policy_heading(line) for line in new_text.splitlines()):
        return new_text
    old_lines = (old_text or "").splitlines()
    start = next((i for i, line in enumerate(old_lines) if _is_policy_heading(line)), None)
    if start is None:
        return new_text
    return new_text.rstrip() + "\n\n" + "\n".join(old_lines[start:]).strip()


def html_to_text(description_html: str) -> str:
    """A rough plain-text view of a live description, for the preview."""
    t = re.sub(r"(?i)<li[^>]*>", "- ", description_html or "")
    t = re.sub(r"(?i)</(p|h\d|li|div)>|<br\s*/?>", "\n", t)
    t = html.unescape(re.sub(r"<[^>]+>", "", t))
    return re.sub(r"\n{3,}", "\n\n", t).strip()


def _fmt(values) -> str:
    if values is None:
        return "(none)"
    return ", ".join(values) if isinstance(values, list) else str(values)


# --- plan (preview) --------------------------------------------------------------

def _fingerprint(item: dict, live: dict) -> str:
    """The fields a revise changes, on eBay and in the DB. Price and quantity
    are left out: /sync moving them between preview and Apply is harmless."""
    listing = item.get("listing") or {}
    stored = {k: listing.get(k) for k in ("title", "description", "item_specifics")}
    blob = json.dumps({"db": stored, "live": live}, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _check_item(item: dict | None, item_id: str) -> dict:
    if item is None:
        raise ReviseError(f"No item {item_id[:8]}.")
    ebay = item.get("ebay") or {}
    if item["status"] != "published" or not ebay.get("offer_id") or not ebay.get("sku"):
        raise ReviseError(f"{item_id[:8]} isn't a live listing the bot made "
                          f"(status '{item['status']}'). /revise only edits those.")
    if ebay.get("source") == "external":
        raise ReviseError(f"{item_id[:8]} was made in Seller Hub, so the bot can't edit it. "
                          "Edit it in Seller Hub.")
    if sync.detached(item):
        raise ReviseError(f"{item_id[:8]} was relisted in Seller Hub, so the bot's edits can't "
                          f"reach the live listing ({ebay.get('listing_id')}). Edit it in Seller Hub.")
    return ebay


def build_plan(item_id: str, change: dict) -> dict:
    """Read eBay and the DB, validate the change, and work out the before/after.
    Blocking (eBay reads); run in a thread. Raises ReviseError."""
    item = db.get_item(item_id)
    ebay = _check_item(item, item_id)
    listing = item.get("listing") or {}
    live = inventory.read_listing_content(ebay["sku"], ebay["offer_id"])

    title = None
    if change["title"] is not None:
        try:
            title = inventory._checked_title(change["title"])
        except inventory.TitleTooLongError:
            n = len(" ".join(change["title"].split()))
            raise ReviseError(f"The title is {n} characters; eBay allows 80.") from None
        if title == live["title"]:
            title = None

    set_aspects = _split_values(change["set"], _multi_aspects(listing.get("category_id")))
    after_aspects = live["aspects"]
    if set_aspects or change["remove"]:
        try:
            editor = inventory._aspects_editor(ebay["sku"], set_aspects, change["remove"])
            product = {"aspects": copy.deepcopy(live["aspects"])}
            editor(product)
        except (ValueError, KeyError) as e:
            raise ReviseError(str(e).strip("'\"")) from None
        after_aspects = product["aspects"]
    before_l = {k.lower(): (k, v) for k, v in live["aspects"].items()}
    after_l = {k.lower(): (k, v) for k, v in after_aspects.items()}
    aspect_diff = []   # (name, before, after); after None = removed
    for key in list(before_l) + [k for k in after_l if k not in before_l]:
        b = before_l.get(key, (None, None))[1]
        a = after_l.get(key, (None, None))[1]
        if a != b:
            aspect_diff.append(((after_l.get(key) or before_l.get(key))[0], b, a))
    changed = {name.lower() for name, _, _ in aspect_diff}
    set_aspects = {n: v for n, v in set_aspects.items() if n.lower() in changed}
    remove = [n for n in change["remove"] if n.lower() in changed]

    desc_text = desc_html = None
    if change["description"] is not None:
        desc_text = with_footer(change["description"], listing.get("description"))
        desc_html = inventory.description_html(desc_text)
        if desc_html == live["description_html"]:
            desc_text = desc_html = None

    if title is None and not aspect_diff and desc_text is None:
        raise ReviseError(f"Nothing to change: eBay already shows exactly that for {item_id[:8]}.")

    # What the description looks like now. The stored text when eBay still has
    # what the bot published; otherwise the live HTML, flattened.
    db_text = listing.get("description") or ""
    ours = db_text and (inventory._strip_banner(inventory.description_html(db_text))
                        == inventory._strip_banner(live["description_html"]))
    desc_before = db_text if ours else html_to_text(live["description_html"])

    return {"item_id": item_id, "listing_id": ebay.get("listing_id"),
            "sku": ebay["sku"], "offer_id": ebay["offer_id"],
            "fingerprint": _fingerprint(item, live),
            "title_before": live["title"], "title": title,
            "set": set_aspects, "remove": remove, "aspect_diff": aspect_diff,
            "unchanged_aspects": {k: v for k, v in live["aspects"].items()
                                  if k.lower() not in changed},
            "desc_before": desc_before, "desc_before_is_ours": bool(ours),
            "desc_text": desc_text, "desc_html": desc_html}


def preview_lines(plan: dict) -> list[str]:
    lines = [f"✏️ Revise {plan['item_id'][:8]} (eBay {plan['listing_id']}). Nothing sent yet.", ""]
    if plan["title"] is not None:
        lines += ["TITLE", f"  before: {plan['title_before']}",
                  f"  after:  {plan['title']} ({len(plan['title'])}/80)", ""]
    else:
        lines += [f"TITLE (unchanged): {plan['title_before']}", ""]
    if plan["aspect_diff"]:
        lines.append("SPECIFICS")
        for name, before, after in plan["aspect_diff"]:
            lines.append(f"  {name}: {_fmt(before)} → {'(removed)' if after is None else _fmt(after)}")
        kept = plan["unchanged_aspects"]
        ids = [f"{k} {_fmt(v)}" for k, v in kept.items() if k.lower() in ("upc", "ean", "isbn", "mpn")]
        lines.append(f"  unchanged: {len(kept)} others" + (f", incl. {'; '.join(ids)}" if ids else ""))
        lines.append("")
    else:
        lines += ["SPECIFICS: unchanged", ""]
    if plan["desc_text"] is not None:
        note = "" if plan["desc_before_is_ours"] else " (as eBay shows it; edited outside the bot)"
        lines += [f"DESCRIPTION before{note}:", plan["desc_before"], "",
                  "DESCRIPTION after:", plan["desc_text"]]
    else:
        lines.append("DESCRIPTION: unchanged")
    return lines


# --- apply -------------------------------------------------------------------------

def _db_specifics(old: dict, set_aspects: dict, remove: list) -> dict:
    """The stored specifics with the same change applied: names matched
    case-insensitively, one value stored as a string."""
    out = dict(old or {})
    keys = {k.lower(): k for k in out}
    for name in remove:
        out.pop(keys.get(name.lower(), name), None)
    for name, value in set_aspects.items():
        values = value if isinstance(value, list) else [value]
        out[keys.get(name.lower(), name)] = values[0] if len(values) == 1 else values
    return out


def apply_plan(plan: dict) -> dict:
    """Write the plan: eBay first, the DB only if every eBay write succeeded.
    Blocking; run in a thread. Returns {"ok": bool, "lines": [...]}."""
    item_id, id8 = plan["item_id"], plan["item_id"][:8]
    item = db.get_item(item_id)
    try:
        ebay = _check_item(item, item_id)
        live = inventory.read_listing_content(ebay["sku"], ebay["offer_id"])
    except ReviseError as e:
        return {"ok": False, "lines": [f"⚠️ Nothing changed. {e}"]}
    if _fingerprint(item, live) != plan["fingerprint"]:
        return {"ok": False, "lines": [
            f"⚠️ Nothing changed: {id8} was changed on eBay or in the bot since the preview. "
            "Run /revise again to see the current state."]}

    sku, offer_id = plan["sku"], plan["offer_id"]
    inventory_change = plan["title"] is not None or plan["set"] or plan["remove"]
    saved: dict = {}
    sent_product = None

    def put_back(reason: str) -> list[str]:
        if not saved:
            return [f"{reason} Nothing else was changed."]
        try:
            inventory.restore_inventory_product(sku, saved)
            print(f"✏️ revise {id8}: title/specifics put back")
            return [f"{reason} The title and specifics were put back as they were."]
        except Exception as e:
            util._record_error(f"revise put-back ({id8})", e)
            print(f"❌ revise {id8}: put-back FAILED: {type(e).__name__}: {e}")
            return [f"{reason}",
                    f"❌ Putting the title/specifics back ALSO failed ({type(e).__name__}: "
                    f"{str(e)[:200]}). eBay may now show the new title/specifics with the old "
                    "description. The DB was not changed. Check the listing in Seller Hub."]

    if inventory_change:
        try:
            sent_product = inventory.revise_inventory_product(
                sku, title=plan["title"], set_aspects=plan["set"] or None,
                remove=plan["remove"], saved=saved)
            print(f"✏️ revise {id8}: title/specifics sent to eBay")
        except Exception as e:
            util._record_error(f"revise title/specifics ({id8})", e)
            print(f"❌ revise {id8}: title/specifics refused: {type(e).__name__}: {e}")
            reason = f"❌ eBay refused the title/specifics: {type(e).__name__}: {str(e)[:300]}"
            # eBay can save the record and still refuse the listing update, so
            # put the saved fields back rather than trust that nothing landed.
            return {"ok": False, "lines": put_back(reason) + ["The DB was not changed."]}

    if plan["desc_html"] is not None:
        try:
            inventory.update_offer_fields(offer_id, description_html=plan["desc_html"])
            print(f"✏️ revise {id8}: description sent to eBay")
        except Exception as e:
            util._record_error(f"revise description ({id8})", e)
            print(f"❌ revise {id8}: description refused: {type(e).__name__}: {e}")
            reason = f"❌ eBay refused the description: {type(e).__name__}: {str(e)[:300]}"
            lines = put_back(reason) if inventory_change else [f"{reason} Nothing else was changed."]
            return {"ok": False, "lines": lines + ["The DB was not changed."]}

    listing = dict(item.get("listing") or {})
    if plan["title"] is not None:
        listing["title"] = (sent_product or {}).get("title") or plan["title"]
    if plan["set"] or plan["remove"]:
        listing["item_specifics"] = _db_specifics(listing.get("item_specifics"),
                                                  plan["set"], plan["remove"])
    if plan["desc_text"] is not None:
        listing["description"] = plan["desc_text"]
    try:
        db.update_field(item_id, "listing", listing)
    except Exception as e:
        util._record_error(f"revise DB write ({id8})", e)
        print(f"❌ revise {id8}: eBay updated but the DB write failed: {type(e).__name__}: {e}")
        return {"ok": False, "lines": [
            f"⚠️ eBay was updated, but saving it to the DB failed ({type(e).__name__}). "
            "The DB still has the old title/specifics/description. It's in /errors; "
            "tell the developer before revising this item again."]}
    print(f"✏️ revise {id8}: done, DB updated")
    done = [w for w, on in (("title", plan["title"] is not None),
                            ("specifics", bool(plan["set"] or plan["remove"])),
                            ("description", plan["desc_text"] is not None)) if on]
    return {"ok": True, "lines": [f"✅ Revised {id8} on eBay (listing {plan['listing_id']}, "
                                  f"same listing) and in the DB: {', '.join(done)}."]}


# --- Telegram handlers ----------------------------------------------------------------

def _expire_old() -> None:
    now = time.time()
    for token in [t for t, p in state.revise.items() if now - p["ts"] > PREVIEW_TTL]:
        state.revise.pop(token, None)


def _no_change(change: dict) -> bool:
    return (change["title"] is None and not change["set"] and not change["remove"]
            and change["description"] is None)


async def revise_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if not is_owner(update.effective_user):
        await util._safe_reply(message, "⛔ /revise is owner-only."
                               + ("" if config.OWNER_USER_IDS else
                                  " No owner is set: add OWNER_USER_IDS to .env and restart."))
        return
    try:
        blocks = split_blocks(message.text or "")
    except ReviseError as e:
        await util._safe_reply(message, f"⚠️ {e}\n\n{USAGE}")
        return
    if len(blocks) > config.REVISE_MAX_LISTINGS:
        await util._safe_reply(message, f"⚠️ {len(blocks)} listings; one /revise takes up to "
                                        f"{config.REVISE_MAX_LISTINGS}. Nothing changed.")
        return
    if len(blocks) == 1 and len(blocks[0][0]) != 1:
        await util._safe_reply(message, "One listing per block: /revise <id> on the first line.\n\n" + USAGE)
        return
    if any(_no_change(change) for _, change in blocks):
        await util._safe_reply(message, USAGE)
        return

    item_ids, errors = [], []
    for args, _ in blocks:
        item_id, err = util._resolve_item_id(update.effective_user.id, args)
        if err:
            errors.append(f"{args[0]}: {err}")
        elif item_id in item_ids:
            errors.append(f"{item_id[:8]} is given twice.")
        item_ids.append(item_id)
    plans = []
    if not errors:
        for item_id, (_, change) in zip(item_ids, blocks):
            try:
                plans.append(await asyncio.to_thread(build_plan, item_id, change))
            except ReviseError as e:
                errors.append(f"{item_id[:8]}: {e}")
            except Exception as e:
                util._record_error(f"revise preview ({item_id[:8]})", e)
                errors.append(f"{item_id[:8]}: couldn't read the listing from eBay "
                              f"({type(e).__name__}: {str(e)[:150]})")
    if errors:
        # All or nothing at preview time: a partial preview is easy to Apply by mistake.
        await util._send_chunked(message, ["⚠️ Nothing changed:"] + [f"  {e}" for e in errors])
        return

    _expire_old()
    token = secrets.token_hex(4)
    state.revise[token] = {"user": update.effective_user.id, "ts": time.time(), "plans": plans}
    if len(plans) == 1:
        lines = preview_lines(plans[0])
        ask = f"Apply these changes to eBay listing {plans[0]['listing_id']}? Nothing has been sent yet."
        apply_label = "✅ Apply to eBay"
    else:
        lines = [f"✏️ Revise {len(plans)} listings. Nothing sent yet.", ""]
        for n, plan in enumerate(plans, 1):
            lines += [f"── {n} of {len(plans)} ──"] + preview_lines(plan)[1:] + [""]
        ask = (f"Apply all {len(plans)} to eBay? Each is sent to eBay first, then saved in the DB. "
               "Nothing has been sent yet.")
        apply_label = f"✅ Apply all {len(plans)} to eBay"
    await util._send_chunked(message, lines)
    markup = InlineKeyboardMarkup([[
        InlineKeyboardButton(apply_label, callback_data=f"rev:apply:{token}"),
        InlineKeyboardButton("✖ Cancel", callback_data=f"rev:cancel:{token}")]])
    await util._safe_reply(message, ask, reply_markup=markup)


async def _apply_one(plan: dict) -> dict:
    item_id = plan["item_id"]
    if not util._claim("revise", item_id):
        return {"ok": False, "lines": [f"⚠️ {item_id[:8]} is already being revised. Not changed."]}
    try:
        return await asyncio.to_thread(apply_plan, plan)
    except Exception as e:
        util._record_error(f"revise ({item_id[:8]})", e)
        return {"ok": False, "lines": [f"❌ {item_id[:8]}: revise failed: {type(e).__name__}: "
                                       f"{str(e)[:200]}. Check the listing before trying again; see /errors."]}
    finally:
        util._release("revise", item_id)


async def revise_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    try:
        _, action, token = query.data.split(":", 2)
    except ValueError:
        return
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass
    if not is_owner(update.effective_user):
        await util._safe_reply(query.message, "⛔ /revise is owner-only.")
        return
    pending = state.revise.get(token)
    if pending is None or pending["user"] != update.effective_user.id:
        await util._safe_reply(query.message, "That preview has expired or isn't yours. "
                                              "Run /revise again. Nothing changed.")
        return
    state.revise.pop(token, None)   # single use
    if action == "cancel":
        await util._safe_reply(query.message, "Cancelled. Nothing changed.")
        return
    if action != "apply":
        return
    if time.time() - pending["ts"] > PREVIEW_TTL:
        await util._safe_reply(query.message, "That preview is over 30 minutes old. "
                                              "Run /revise again. Nothing changed.")
        return

    plans = pending["plans"]
    await util._safe_reply(query.message, "✍️ Sending the changes for "
                                          f"{', '.join(p['item_id'][:8] for p in plans)} to eBay...")
    # One at a time, each eBay first and then the DB. A failure stops only its
    # own listing: the others are independent and were previewed.
    results = [await _apply_one(plan) for plan in plans]
    lines = [line for r in results for line in r["lines"]]
    if len(plans) > 1:
        ok = sum(r["ok"] for r in results)
        lines.append(f"Done: {ok} of {len(plans)} revised."
                     + ("" if ok == len(plans) else " The others are as described above."))
    await util._send_chunked(query.message, lines)
