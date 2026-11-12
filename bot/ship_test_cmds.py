"""/shiptest: the free-shipping test (pipeline/ship_test.py), owner only.

Before the start, /shiptest shows the preview: every test listing with its new
price and Best Offer thresholds, the control group, and anything that can't
join. Apply creates the free-shipping policy and sends the test listings' changes
to eBay, one listing at a time. Once the test runs, /shiptest shows the results
so far against the same number of days before the start."""
from __future__ import annotations

import asyncio
import secrets
import time

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

import config
from bot import revise_cmds, state, util
from pipeline import ship_test

PREVIEW_TTL = 30 * 60


def _money(v) -> str:
    return f"${float(v):.2f}"


def _terms(terms: dict | None) -> str:
    t = terms or {}
    acc = (t.get("autoAcceptPrice") or {}).get("value")
    dec = (t.get("autoDeclinePrice") or {}).get("value")
    return f"accept ≥ ${acc or '—'}, decline < ${dec or '—'}"


def preview_lines(p: dict) -> list[str]:
    test, control = p["test"], p["control"]
    lines = ["🧪 Free-shipping test: preview. Nothing has been sent yet.",
             f"Test: {len(test)} listings move to free shipping, price +"
             f"{_money(config.SHIP_TEST_PRICE_BUMP)}, Best Offer reset for the new price.",
             f"Control: {len(control)} listings stay on $9.99 shipping.",
             f"Both groups: no reprices or offers for {config.SHIP_TEST_DAYS} days.", "",
             "TEST (price → new · Best Offer · break-even on free shipping)"]
    for r in test:
        be = f"{_money(r['breakeven'])}" if r.get("breakeven") is not None else "no cost"
        lines.append(f"• {r['item_id'][:8]} {r['title'][:34]}  {_money(r['price'])} → "
                     f"{_money(r['new_price'])} · {_terms(r['terms'])} · be {be}")
    lines += ["", "CONTROL (unchanged)"]
    lines += [f"• {r['item_id'][:8]} {r['title'][:34]}  {_money(r['price'])}" for r in control]
    if p["problems"]:
        lines += ["", "⚠️ Left out:"]
        lines += [f"• {pid} ({g}): {why}" for pid, g, why in p["problems"]]
    lines += ["", f"The free-shipping policy “{config.FREE_SHIPPING_POLICY_NAME}” is created on "
                  "Apply (a copy of the current one with the first domestic service free), "
                  "or reused if it exists."]
    return lines


def report_lines(r: dict) -> list[str]:
    lines = [f"🧪 Free-shipping test: day {r['days']} of {config.SHIP_TEST_DAYS} "
             f"({r['start']} → {r['end']})" + (" — over." if r["over"] else "."),
             f"Each group vs its own {r['days']} days before the start:"]
    for g in ("test", "control"):
        d = r["groups"].get(g)
        if not d:
            continue
        cur, base = d["during"], d["before"]

        def ctr(x):
            return f"{x['ctr'] * 100:.2f}%" if x["ctr"] is not None else "—"

        lines.append(f"{g.upper()} ({d['listings']}): CTR {ctr(base)} → {ctr(cur)} · "
                     f"impressions {base['impressions']:,} → {cur['impressions']:,} · "
                     f"views {base['views']} → {cur['views']} · sales {base['sales']} → {cur['sales']}")
    lines.append("Judge on CTR; sales are a direction only at this size.")
    return lines


async def ship_test_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if not revise_cmds.is_owner(update.effective_user):
        await util._safe_reply(message, "⛔ /shiptest is owner-only.")
        return
    p = await asyncio.to_thread(ship_test.preview)
    if p["started"]:
        try:
            r = await asyncio.to_thread(ship_test.report)
        except Exception as e:
            util._record_error("shiptest report", e)
            await util._safe_reply(message, f"⚠️ Couldn't read eBay traffic: {type(e).__name__}: "
                                            f"{str(e)[:200]}")
            return
        await util._send_chunked(message, report_lines(r))
        return
    await util._send_chunked(message, preview_lines(p))
    if not p["test"]:
        await util._safe_reply(message, "No test listing can join, so there's nothing to apply.")
        return
    token = secrets.token_hex(4)
    state.ship_test[token] = {"user": update.effective_user.id, "ts": time.time(), "rows": p}
    markup = InlineKeyboardMarkup([[
        InlineKeyboardButton(f"✅ Start the test ({len(p['test'])} to free shipping)",
                             callback_data=f"shipt:apply:{token}"),
        InlineKeyboardButton("✖ Cancel", callback_data=f"shipt:cancel:{token}")]])
    await util._safe_reply(message, "Start the test? Each test listing is sent to eBay first, "
                                    "then saved in the DB.", reply_markup=markup)


async def ship_test_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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
    if not revise_cmds.is_owner(update.effective_user):
        await util._safe_reply(query.message, "⛔ /shiptest is owner-only.")
        return
    pending = state.ship_test.pop(token, None)   # single use
    if pending is None or pending["user"] != update.effective_user.id:
        await util._safe_reply(query.message, "That preview has expired. Run /shiptest again. "
                                              "Nothing changed.")
        return
    if action != "apply":
        await util._safe_reply(query.message, "Cancelled. Nothing changed.")
        return
    if time.time() - pending["ts"] > PREVIEW_TTL:
        await util._safe_reply(query.message, "That preview is over 30 minutes old. "
                                              "Run /shiptest again. Nothing changed.")
        return
    if not util._claim("shiptest", "start"):
        await util._safe_reply(query.message, "The test is already being started.")
        return
    try:
        await util._safe_reply(query.message, "✍️ Creating the free-shipping policy and "
                                              "updating the test listings...")
        try:
            out = await asyncio.to_thread(ship_test.apply, pending["rows"])
        except Exception as e:
            util._record_error("shiptest apply", e)
            await util._safe_reply(query.message, f"❌ Stopped before any listing changed: "
                                                  f"{type(e).__name__}: {str(e)[:250]}")
            return
    finally:
        util._release("shiptest", "start")
    lines = [f"🧪 Test started {out['start']}, ends {out['end']}. Policy {out['policy']}.",
             f"Free shipping on {len(out['applied'])} of {len(pending['rows']['test'])} test "
             f"listings; {len(out['marked'])} control listings marked."]
    if out["failed"]:
        lines += ["⚠️ Not changed:"] + [f"• {pid}: {why}" for pid, why in out["failed"]]
    lines.append("/shiptest shows the results as they come in.")
    await util._send_chunked(query.message, lines)
