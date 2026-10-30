"""Small fake Telegram objects for driving telegram_bot handlers from tests.

No network: nothing here talks to Telegram. Every reply, send and edit is
appended to one shared Transcript (owned by the FakeContext), so a test can
assert on exactly what the user would have seen, in order:

    update, ctx = make_text_update("/whoami")
    run(tb.whoami_command, update, ctx)
    assert "Your Telegram user id" in sent_texts(ctx)[0]

Several updates in one scenario (a photo batch, then a button tap) share a
context: pass `context=ctx` to the builders. Handlers that start background
work do it with context.application.create_task; `run()` awaits those tasks
(and any they spawn) before returning, so a debounce timer such as
finalize_capture finishes inside the call — set bot.intake.DEFAULT_CAPTURE_WINDOW
(or state.capture_window[user_id]) to 0 with monkeypatch so it doesn't really sleep.

Module-level state in telegram_bot (pending, staging, review, gate, ...)
outlives a test; call reset_bot_state(tb) before and after (see bot_state()).
"""
from __future__ import annotations

import asyncio
import io
import itertools
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_USER_ID = 4242
DEFAULT_CHAT_ID = 4242  # private chat: chat id == user id

_TEST_IMAGES = Path(__file__).parent.parent / "test_images"
_ids = itertools.count(1000)


def _next_id() -> int:
    return next(_ids)


# --------------------------------------------------------------------------- #
# Transcript
# --------------------------------------------------------------------------- #

@dataclass
class Call:
    """One thing the bot did. `kind` is the Telegram method name
    (reply_text, send_message, edit_message_text, answer, ...)."""
    kind: str
    text: str | None = None
    kwargs: dict = field(default_factory=dict)


class Transcript(list):
    def record(self, kind: str, text: str | None = None, **kwargs) -> Call:
        call = Call(kind, text, kwargs)
        self.append(call)
        return call

    def of(self, *kinds: str) -> list[Call]:
        return [c for c in self if c.kind in kinds]


# Methods whose `text` the user actually sees in the chat.
_VISIBLE = ("reply_text", "reply_photo", "reply_document", "send_message",
            "send_photo", "edit_message_text", "edit_text")


def sent_texts(ctx_or_transcript) -> list[str]:
    """Every text the bot sent, replied, captioned or edited in, in order.
    Accepts a FakeContext or a Transcript. Media-group captions are included."""
    t = getattr(ctx_or_transcript, "transcript", ctx_or_transcript)
    out = []
    for c in t:
        if c.kind in _VISIBLE and c.text is not None:
            out.append(c.text)
        elif c.kind == "reply_media_group":
            out.extend(cap for cap in c.kwargs.get("captions", []) if cap)
    return out


# --------------------------------------------------------------------------- #
# Users, chats, messages
# --------------------------------------------------------------------------- #

@dataclass
class FakeUser:
    id: int = DEFAULT_USER_ID
    first_name: str = "Tester"
    username: str | None = "tester"
    is_bot: bool = False


@dataclass
class FakeChat:
    id: int = DEFAULT_CHAT_ID
    type: str = "private"


@dataclass
class FakePhotoSize:
    file_id: str
    file_unique_id: str = ""
    width: int = 64
    height: int = 64


class FakeMessage:
    """A message either from the user (built by a make_*_update) or sent by the
    bot (returned from reply_*/send_*). Bot-side calls go to `transcript`."""

    def __init__(self, transcript: Transcript, *, text=None, photo=None, caption=None,
                 media_group_id=None, message_id=None, chat=None, from_user=None,
                 reply_to_message=None, reply_markup=None):
        self.transcript = transcript
        self.text = text
        self.photo = list(photo or [])
        self.caption = caption
        self.media_group_id = media_group_id
        self.message_id = message_id if message_id is not None else _next_id()
        self.chat = chat or FakeChat()
        self.chat_id = self.chat.id
        self.from_user = from_user
        self.reply_to_message = reply_to_message
        self.reply_markup = reply_markup
        self.deleted = False

    def _child(self, **kw) -> "FakeMessage":
        return FakeMessage(self.transcript, chat=self.chat, **kw)

    async def reply_text(self, text, **kwargs):
        self.transcript.record("reply_text", text, **kwargs)
        return self._child(text=text, reply_markup=kwargs.get("reply_markup"))

    async def reply_photo(self, photo=None, caption=None, **kwargs):
        self.transcript.record("reply_photo", caption, photo=photo, **kwargs)
        return self._child(caption=caption, reply_markup=kwargs.get("reply_markup"))

    async def reply_document(self, document=None, caption=None, **kwargs):
        self.transcript.record("reply_document", caption, document=document, **kwargs)
        return self._child(caption=caption)

    async def reply_media_group(self, media, **kwargs):
        captions = [getattr(m, "caption", None) for m in media]
        self.transcript.record("reply_media_group", None, media=list(media),
                               captions=captions, **kwargs)
        return [self._child(caption=c) for c in captions]

    async def edit_text(self, text, **kwargs):
        self.transcript.record("edit_text", text, message_id=self.message_id, **kwargs)
        self.text = text
        return self

    async def edit_reply_markup(self, reply_markup=None, **kwargs):
        self.transcript.record("edit_reply_markup", None, reply_markup=reply_markup,
                               message_id=self.message_id, **kwargs)
        self.reply_markup = reply_markup
        return self

    async def delete(self):
        self.transcript.record("delete", None, message_id=self.message_id)
        self.deleted = True
        return True


class FakeCallbackQuery:
    def __init__(self, transcript: Transcript, data: str, from_user: FakeUser,
                 message: FakeMessage):
        self.transcript = transcript
        self.id = str(_next_id())
        self.data = data
        self.from_user = from_user
        self.message = message

    async def answer(self, text=None, **kwargs):
        self.transcript.record("answer", text, **kwargs)
        return True

    async def edit_message_text(self, text, **kwargs):
        self.transcript.record("edit_message_text", text,
                               message_id=self.message.message_id, **kwargs)
        self.message.text = text
        return self.message

    async def edit_message_reply_markup(self, reply_markup=None, **kwargs):
        self.transcript.record("edit_message_reply_markup", None,
                               reply_markup=reply_markup,
                               message_id=self.message.message_id, **kwargs)
        self.message.reply_markup = reply_markup
        return self.message


class FakeUpdate:
    def __init__(self, *, user: FakeUser | None, chat: FakeChat | None,
                 message: FakeMessage | None = None,
                 callback_query: FakeCallbackQuery | None = None):
        self.update_id = _next_id()
        self.effective_user = user
        self.effective_chat = chat
        self.message = message
        self.callback_query = callback_query

    @property
    def effective_message(self):
        if self.message is not None:
            return self.message
        return self.callback_query.message if self.callback_query else None


# --------------------------------------------------------------------------- #
# Files, bot, application, context
# --------------------------------------------------------------------------- #

def tiny_jpeg(color=(200, 30, 30), size=(64, 64)) -> bytes:
    """A real, decodable JPEG, generated in memory."""
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "JPEG")
    return buf.getvalue()


def image_bytes(name: str) -> bytes:
    """Bytes of a file in the repo's test_images/ folder (e.g. 'check1.png')."""
    return (_TEST_IMAGES / name).read_bytes()


class FakeFile:
    def __init__(self, file_id: str, data: bytes):
        self.file_id = file_id
        self.data = data
        self.file_size = len(data)

    async def download_as_bytearray(self, *a, **k) -> bytearray:
        return bytearray(self.data)

    async def download_to_drive(self, custom_path=None, *a, **k) -> Path:
        p = Path(custom_path or f"{self.file_id}.jpg")
        p.write_bytes(self.data)
        return p


class FakeBot:
    """context.bot. Photos are looked up by file_id in `files`; register them
    with add_file() (make_photo_update does this) or get a tiny JPEG."""

    def __init__(self, transcript: Transcript):
        self.transcript = transcript
        self.files: dict[str, bytes] = {}
        self.id = 1
        self.username = "fake_ross_bot"

    def add_file(self, file_id: str, data: bytes) -> None:
        self.files[file_id] = data

    async def get_file(self, file_id, **kwargs) -> FakeFile:
        self.transcript.record("get_file", None, file_id=file_id)
        return FakeFile(file_id, self.files.get(file_id) or tiny_jpeg())

    def _msg(self, chat_id, **kw) -> FakeMessage:
        return FakeMessage(self.transcript, chat=FakeChat(id=chat_id), **kw)

    async def send_message(self, chat_id, text, **kwargs):
        self.transcript.record("send_message", text, chat_id=chat_id, **kwargs)
        return self._msg(chat_id, text=text, reply_markup=kwargs.get("reply_markup"))

    async def send_photo(self, chat_id, photo, caption=None, **kwargs):
        self.transcript.record("send_photo", caption, chat_id=chat_id, photo=photo, **kwargs)
        return self._msg(chat_id, caption=caption)

    async def send_document(self, chat_id, document, caption=None, **kwargs):
        self.transcript.record("send_document", caption, chat_id=chat_id,
                               document=document, **kwargs)
        return self._msg(chat_id, caption=caption)

    async def edit_message_text(self, text, chat_id=None, message_id=None, **kwargs):
        self.transcript.record("edit_message_text", text, chat_id=chat_id,
                               message_id=message_id, **kwargs)
        return self._msg(chat_id, text=text, message_id=message_id)

    async def edit_message_reply_markup(self, chat_id=None, message_id=None,
                                        reply_markup=None, **kwargs):
        self.transcript.record("edit_message_reply_markup", None, chat_id=chat_id,
                               message_id=message_id, reply_markup=reply_markup, **kwargs)
        return True

    async def set_my_commands(self, commands, **kwargs):
        self.transcript.record("set_my_commands", None, commands=commands)
        return True


class FakeApplication:
    """context.application. create_task schedules on the running loop and keeps
    the task so run()/drain() can wait for it."""

    def __init__(self, bot: FakeBot):
        self.bot = bot
        self.tasks: list[asyncio.Task] = []
        self.bot_data: dict = {}

    def create_task(self, coro, *args, **kwargs) -> asyncio.Task:
        task = asyncio.get_running_loop().create_task(coro)
        self.tasks.append(task)
        return task


class FakeJobQueue:
    """Records run_once/run_repeating/run_daily. The bot doesn't use PTB's
    job_queue today (its schedulers are create_task loops); this is here so a
    handler that starts using it fails loudly in an assertion, not an
    AttributeError."""

    def __init__(self):
        self.jobs: list[tuple[str, Any, tuple, dict]] = []

    def run_once(self, callback, when, *args, **kwargs):
        self.jobs.append(("run_once", callback, (when,) + args, kwargs))

    def run_repeating(self, callback, interval, *args, **kwargs):
        self.jobs.append(("run_repeating", callback, (interval,) + args, kwargs))

    def run_daily(self, callback, time, *args, **kwargs):
        self.jobs.append(("run_daily", callback, (time,) + args, kwargs))


class FakeContext:
    def __init__(self, args: list[str] | None = None):
        self.transcript = Transcript()
        self.bot = FakeBot(self.transcript)
        self.application = FakeApplication(self.bot)
        self.job_queue = FakeJobQueue()
        self.args = list(args or [])
        self.user_data: dict = {}
        self.chat_data: dict = {}
        self.bot_data = self.application.bot_data
        self.error = None


# --------------------------------------------------------------------------- #
# Builders
# --------------------------------------------------------------------------- #

def _ctx(context: FakeContext | None, args=None) -> FakeContext:
    ctx = context or FakeContext()
    if args is not None:
        ctx.args = list(args)
    return ctx


def make_text_update(text: str, *, user_id: int = DEFAULT_USER_ID,
                     chat_id: int | None = None, context: FakeContext | None = None,
                     reply_to: FakeMessage | None = None, message_id: int | None = None):
    """A typed message. A leading '/cmd a b' also sets context.args = ['a', 'b'],
    as PTB's CommandHandler would. Returns (update, context)."""
    args = text.split()[1:] if text.startswith("/") else None
    ctx = _ctx(context, args)
    user, chat = FakeUser(id=user_id), FakeChat(id=chat_id or user_id)
    msg = FakeMessage(ctx.transcript, text=text, chat=chat, from_user=user,
                      reply_to_message=reply_to, message_id=message_id)
    return FakeUpdate(user=user, chat=chat, message=msg), ctx


def make_photo_update(image: bytes | None = None, *, user_id: int = DEFAULT_USER_ID,
                      chat_id: int | None = None, context: FakeContext | None = None,
                      caption: str | None = None, media_group_id: str | None = None,
                      message_id: int | None = None):
    """A photo message. `image` is what get_file().download_* will return
    (default: a tiny generated JPEG). Like Telegram, message.photo lists sizes
    smallest first; the handler takes photo[-1]. Returns (update, context)."""
    ctx = _ctx(context)
    user, chat = FakeUser(id=user_id), FakeChat(id=chat_id or user_id)
    file_id = f"file_{_next_id()}"
    ctx.bot.add_file(file_id, image if image is not None else tiny_jpeg())
    sizes = [FakePhotoSize(file_id=f"{file_id}_thumb", width=32, height=32),
             FakePhotoSize(file_id=file_id, file_unique_id=file_id)]
    msg = FakeMessage(ctx.transcript, photo=sizes, caption=caption,
                      media_group_id=media_group_id, chat=chat, from_user=user,
                      message_id=message_id)
    return FakeUpdate(user=user, chat=chat, message=msg), ctx


def make_callback_update(data: str, *, user_id: int = DEFAULT_USER_ID,
                         chat_id: int | None = None, context: FakeContext | None = None,
                         message: FakeMessage | None = None):
    """A button tap. `message` is the bot message the button sat on (e.g. one
    returned from a reply_text/send_message); a blank one is made otherwise.
    update.message is None, as in real callback updates. Returns (update, context)."""
    ctx = _ctx(context)
    user, chat = FakeUser(id=user_id), FakeChat(id=chat_id or user_id)
    msg = message or FakeMessage(ctx.transcript, text="(button message)", chat=chat)
    query = FakeCallbackQuery(ctx.transcript, data, user, msg)
    return FakeUpdate(user=user, chat=chat, callback_query=query), ctx


def last_markup_buttons(ctx: FakeContext) -> list[tuple[str, str]]:
    """(label, callback_data) of the inline keyboard on the most recent bot
    message that carried one — handy for 'tap the Start button' steps."""
    for c in reversed(ctx.transcript):
        markup = c.kwargs.get("reply_markup")
        if markup is not None and getattr(markup, "inline_keyboard", None):
            return [(b.text, b.callback_data) for row in markup.inline_keyboard for b in row]
    return []


# --------------------------------------------------------------------------- #
# Running handlers
# --------------------------------------------------------------------------- #

async def drain(ctx: FakeContext, timeout: float = 10.0) -> None:
    """Await every task started through context.application.create_task,
    including tasks those tasks start. Cancelled tasks (a debounce timer
    restarted by a newer photo) are fine; any other exception is raised."""
    seen = 0
    while seen < len(ctx.application.tasks):
        batch = ctx.application.tasks[seen:]
        seen = len(ctx.application.tasks)
        results = await asyncio.wait_for(
            asyncio.gather(*batch, return_exceptions=True), timeout)
        for r in results:
            if isinstance(r, BaseException) and not isinstance(r, asyncio.CancelledError):
                raise r


def run(handler, update, ctx: FakeContext, *, wait: bool = True):
    """Run one async handler from a sync test (asyncio.run, as the existing
    tests do) and, unless wait=False, drain its background tasks."""
    async def _go():
        result = await handler(update, ctx)
        if wait:
            await drain(ctx)
        return result
    return asyncio.run(_go())


def run_steps(*steps, ctx: FakeContext, drain_each: bool = False):
    """Run several (handler, update) steps on ONE event loop, then drain.

    By default nothing is drained between steps, so photos sent back to back
    land in the same debounce window, like a real batch (each photo cancels the
    previous finalize timer). drain_each=True waits for background work after
    every step instead, e.g. photos then a button tap that needs them staged."""
    async def _go():
        for handler, update in steps:
            await handler(update, ctx)
            if drain_each:
                await drain(ctx)
        await drain(ctx)
    asyncio.run(_go())


# --------------------------------------------------------------------------- #
# telegram_bot module state
# --------------------------------------------------------------------------- #

# Per-user in-memory state at the top of telegram_bot.py. None of it is cleared
# between tests by anything else.
BOT_STATE_NAMES = (
    "pending", "review", "locks", "last_item", "capture_window", "staging",
    "awaiting_photos", "appending", "gate", "gate_queue", "find_mode", "find_loc",
    "last_find_photo", "quick_add", "inv_box", "inv_session", "list_after_photos",
    "haul_mode", "msg_target", "_in_flight",
)


def reset_bot_state(tb) -> None:
    """Empty every per-user dict/set in bot.state (and the /errors buffer).

    Reads bot.state / bot.util first, the modules that own the objects;
    telegram_bot re-exports the same objects, so it is only the fallback."""
    from bot import state, util
    for name in BOT_STATE_NAMES:
        obj = getattr(state, name, None)
        if obj is None:
            obj = getattr(tb, name, None)
        if obj is not None:
            obj.clear()
    errs = getattr(util, "_recent_errors", None)
    if errs is None:
        errs = getattr(tb, "_recent_errors", None)
    if errs is not None:
        errs.clear()


@contextmanager
def allowed_users(tb, *user_ids: int):
    """Set tb.ALLOWED_USER_IDS for the block (default: just DEFAULT_USER_ID).
    Only _auth_guard (handler group -1) reads it for gating; the handlers
    themselves don't, so tests calling a handler directly needn't use this."""
    saved = tb.ALLOWED_USER_IDS
    tb.ALLOWED_USER_IDS = set(user_ids or (DEFAULT_USER_ID,))
    try:
        yield tb.ALLOWED_USER_IDS
    finally:
        tb.ALLOWED_USER_IDS = saved


@contextmanager
def bot_state(tb, *user_ids: int):
    """Clean telegram_bot state + default user allowed, restored afterwards.

        with bot_state(tb):
            ...
    Or as a fixture:
        @pytest.fixture
        def bot():
            with bot_state(tb): yield tb
    """
    reset_bot_state(tb)
    try:
        with allowed_users(tb, *user_ids):
            yield tb
    finally:
        reset_bot_state(tb)
