"""Generic Telegram helpers (replies, chunking, error buffer, item-id
resolution, in-flight claims). No feature logic."""
import asyncio
import json
import os
import traceback
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

import config
import db
from bot import state


def _parse_allowed_ids(raw: str | None) -> set[int]:
    ids = set()
    for tok in (raw or "").replace(";", ",").split(","):
        tok = tok.strip()
        if tok.isdigit():
            ids.add(int(tok))
    return ids


# Telegram user ids allowed to drive the bot. bot.app.configure() parses it into
# here at startup, after loading .env (see the comment there). Read it as
# util.ALLOWED_USER_IDS, never import the name: startup and tests rebind it.
ALLOWED_USER_IDS: set[int] = set()


def _remember_target(sent, item_id: str) -> None:
    """Tie a message the bot just sent to the item it's about, so a reply to it
    is unambiguous even with a dozen drafts on screen."""
    if sent is None or not getattr(sent, "message_id", None):
        return
    state.msg_target[sent.message_id] = item_id
    while len(state.msg_target) > state.MSG_TARGET_MAX:
        state.msg_target.popitem(last=False)


def _replied_item(update) -> str | None:
    """The item the user is replying to, if they replied to one of our messages."""
    replied = getattr(update.message, "reply_to_message", None)
    if replied is None:
        return None
    item_id = state.msg_target.get(replied.message_id)
    return item_id if item_id and db.get_item(item_id) else None


def _lock_for(user_id):
    if user_id not in state.locks:
        state.locks[user_id] = asyncio.Lock()
    return state.locks[user_id]


# Every secret this process holds, longest first so an overlapping value can't
# leave a fragment behind. Read once at import: these come from the environment
# and don't change while the bot runs.
_SECRET_ENV_NAMES = (
    "TELEGRAM_BOT_TOKEN", "TELEGRAM_TOKEN", "ANTHROPIC_API_KEY",
    "APIFY_TOKEN", "APIFY_TOKEN_BACKUP",
    "GEMINI_API_KEY", "GOOGLE_API_KEY", "OPENAI_API_KEY",
    "EBAY_CLIENT_SECRET", "EBAY_CLIENT_ID", "EBAY_REFRESH_TOKEN",
    "CLOUDINARY_API_SECRET", "CLOUDINARY_API_KEY", "CLOUDINARY_URL",
)


def _secrets() -> list[str]:
    """Read at call time, not import time: an import that runs before
    load_dotenv() must not leave the scrub list empty."""
    return sorted(
        (v for v in (os.getenv(k) for k in _SECRET_ENV_NAMES) if v and len(v) >= 8),
        key=len, reverse=True)


def _scrub(text: str) -> str:
    """Replace any credential that made it into an outbound string.

    Belt and braces, not the primary defence — the real fix is not putting
    secrets somewhere they can be formatted into a message (see pipeline/price.py's
    _call). But error text reaches Telegram through several paths, str(e) on a
    third-party exception can embed anything, and a chat log is forever. Cheap
    insurance against the next library that decides to be helpful."""
    if not text:
        return text
    for secret in _secrets():
        if secret in text:
            text = text.replace(secret, "***REDACTED***")
    return text


async def _safe_reply(message, text: str, **kwargs):
    """Best-effort status ping. A transient network failure sending this
    message must never abort the actual work that follows it. Returns the sent
    Message (or None if it failed) so callers can tie it to an item — see
    _remember_target."""
    try:
        return await message.reply_text(_scrub(text), **kwargs)
    except Exception as e:
        print(f"WARNING: reply_text failed (continuing anyway): {type(e).__name__}: {e}")
        return None


TELEGRAM_MAX_CHARS = 3900  # under the 4096 hard limit, leaving headroom


async def _send_chunked(message, lines: list[str]) -> None:
    """Send a list of lines as one or more messages, each under Telegram's 4096-char
    cap. /status and /comps can outgrow a single message as the item count grows."""
    buf, size = [], 0
    for line in lines:
        if buf and size + len(line) + 1 > TELEGRAM_MAX_CHARS:
            await _safe_reply(message, "\n".join(buf))
            buf, size = [], 0
        buf.append(line)
        size += len(line) + 1
    if buf:
        await _safe_reply(message, "\n".join(buf))



def _chunk_lines(lines: list[str]) -> list[str]:
    """Group lines into messages that each fit under Telegram's cap."""
    out, buf, size = [], [], 0
    for line in lines:
        if buf and size + len(line) + 1 > TELEGRAM_MAX_CHARS:
            out.append("\n".join(buf))
            buf, size = [], 0
        buf.append(line)
        size += len(line) + 1
    if buf:
        out.append("\n".join(buf))
    return out


async def _send_chunked_to(bot, chat_id, lines: list[str]) -> None:
    """_send_chunked for an unprompted message — the scheduled jobs post to a
    chat id rather than replying to an update, so there is no Message to reply
    to. Same chunking, same best-effort behaviour."""
    for chunk in _chunk_lines(lines):
        try:
            await bot.send_message(chat_id=chat_id, text=_scrub(chunk))
        except Exception as e:
            print(f"WARNING: send_message failed (continuing anyway): {type(e).__name__}: {e}")


# Ring buffer of recent errors, dumped by /errors. The console isn't visible from
# the phone, so failures that only print a traceback there are otherwise invisible.
_recent_errors: deque = deque(maxlen=10)


def _record_error(where: str, exc: BaseException) -> None:
    ts = datetime.now(timezone.utc).strftime("%m-%d %H:%M:%SZ")
    tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    # Scrubbed at capture, not at display: /errors is one reader of this buffer
    # and a traceback holds request URLs, headers and locals.
    _recent_errors.append((ts, where, _scrub(tb.strip())))


def _resolve_item_id(user_id: int, args: list[str]) -> tuple[str | None, str | None]:
    """Resolve an item_id from a command's args (full id or unique prefix),
    falling back to last_item[user_id] when no arg is given. Returns
    (item_id, error_message) — exactly one of which is None."""
    if args:
        arg = args[0]
        if db.get_item(arg) is not None:
            return arg, None
        matches = [i["item_id"] for i in db.list_items() if i["item_id"].startswith(arg)]
        if len(matches) == 1:
            return matches[0], None
        if len(matches) > 1:
            return None, f"'{arg}' matches {len(matches)} items — use a longer prefix."
        return None, f"No item found matching '{arg}'."

    item_id = state.last_item.get(user_id)
    if item_id is None:
        return None, "No item specified and no recent item to fall back to. Provide an item_id."
    return item_id, None


# Every id shown back to the user — /status, /photos, error messages, all of
# it — is item_id[:8]. That's the shortest prefix anyone actually types or
# pastes, so it's also the shortest prefix _names_an_item should ever credit as
# "the user meant an id". See its docstring for why a shorter floor breaks it.
_ITEM_ID_PREFIX_LEN = 8


def _names_an_item(token: str) -> bool:
    """Whether this token identifies an existing item (full id or prefix).

    Commands that take an optional trailing number — /sold, /setqty, /setprice —
    must ask this BEFORE trying to parse one. Item ids are uuid4 hex, so roughly
    2.3% of the 8-char prefixes /status prints are all digits: `/sold 40286146`
    parsed as a $40,286,146 sale price, then fell through to `last_item` and
    booked it against whatever item was touched last.

    Getting it wrong in this direction is cheap (the arg resolves to an item and
    no value is set — visible immediately); getting it wrong the other way
    writes nonsense to an item the user never named.

    The prefix check below only fires past _ITEM_ID_PREFIX_LEN characters. Below
    that, hex digits and decimal digits are the same alphabet and there simply
    isn't enough token left to tell "item id" from "quantity" apart: with ~90
    items in the store, a plain "2" is a startswith-prefix of *some* uuid4 far
    more often than not, so every single-digit /setqty and every two-digit
    /sold price was being misread as the id — in both cases the number the user
    typed, not any id, and both directions of `/setqty <id> <n>` broke because
    of it. No real quantity or sane price (see _MAX_SANE_QTY/_MAX_SANE_PRICE)
    is long enough to reach the floor, so genuine short values never get
    swept up here — only the 8+-char strings this bot actually calls an id."""
    if not token:
        return False
    if db.get_item(token) is not None:
        return True
    if len(token) < _ITEM_ID_PREFIX_LEN:
        return False
    return any(i["item_id"].startswith(token) for i in db.list_items())


def _claim(kind: str, item_id: str) -> bool:
    """Claim (kind, item_id) synchronously. False if someone else holds it."""
    global _busy_since
    key = (kind, item_id)
    if key in state._in_flight:
        return False
    if not state._in_flight:
        _busy_since = datetime.now(timezone.utc).isoformat(timespec="seconds")
    state._in_flight.add(key)
    write_busy()
    return True


def _release(kind: str, item_id: str) -> None:
    state._in_flight.discard((kind, item_id))
    write_busy()


# When _in_flight last went from empty to non-empty (UTC ISO).
_busy_since = ""


def write_busy() -> None:
    """Mirror _in_flight into logs/busy.json for the deploy script: the file
    exists only while work is in flight, and is removed when idle. Temp file +
    replace, like the heartbeat. A file error never breaks a claim/release."""
    path = Path(config.LOG_DIR) / "busy.json"
    try:
        if not state._in_flight:
            path.unlink(missing_ok=True)
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "pid": os.getpid(),
            "since": _busy_since,
            "work": sorted(f"{k}:{i}" for k, i in state._in_flight),
        }
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as e:
        print(f"WARNING: could not update busy.json (non-fatal): {e}")
