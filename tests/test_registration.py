"""Tripwire for the telegram_bot.py split (docs/cleanup-plan.md, Phase 1 item 5).

Part A: every command / callback / message handler main() registers, and the
        Telegram "/" menu from _post_init.
Part B: every telegram_bot attribute the tests patch must still exist AND still
        be read by code living in telegram_bot, otherwise the patch is a no-op.

No network, bot never started: main() runs against a fake Application.
"""
import ast
import asyncio
import types
from pathlib import Path

import pytest

import telegram_bot as tb

TESTS_DIR = Path(__file__).parent

EXPECTED_COMMANDS = {
    "haul", "find", "done", "stock", "list", "inv", "where", "move", "mark",
    "ghosts", "status", "ross", "errors", "comps", "sold", "profit", "report",
    "scoreboard", "listing", "receipt", "addphotos", "delete", "notdup", "end",
    "refreshdesc", "photos", "cover", "arrange", "stockphoto", "offers",
    "setprice", "setqty", "sync", "feedback", "activate", "promote",
    "pricecheck", "retry", "photocheck", "health", "usage", "auth", "whoami",
    "help", "cancel",
}
# pattern -> callback function name
EXPECTED_CALLBACKS = {
    r"^confirm:": "confirm_callback",
    r"^review:": "review_callback",
    r"^reprice:": "reprice_callback",
    r"^qa:": "quick_add_callback",
}
# callback name -> substrings that must appear in the lowercased filters repr
EXPECTED_MESSAGE_HANDLERS = {
    "photo_handler": ("photo",),
    "text_handler": ("text", "command", "inverted"),
}
# (handler class, callback name, group)
EXPECTED_OTHER_HANDLERS = {("TypeHandler", "_auth_guard", -1)}
EXPECTED_ERROR_HANDLERS = {"error_handler"}

# Registered commands knowingly absent from the "/" menu (HELP_SECTIONS).
MENU_MISSING_ALLOWLIST = {"done"}  # /done is registered but not in HELP_SECTIONS, so not in the "/" menu (as of 2026-10-30)

# Patched names that fail check (2) today (patched but never read by code in
# telegram_bot). Add "name",  # why   -- empty means none.
UNREACHED_PATCH_ALLOWLIST = set()


# ---------------------------------------------------------------- Part A ----

class _FakeApp:
    def __init__(self):
        self.handlers = []        # (handler, group)
        self.error_handlers = []

    def add_handler(self, handler, group=0):
        self.handlers.append((handler, group))

    def add_error_handler(self, cb, *a, **k):
        self.error_handlers.append(cb)

    def run_polling(self, *a, **k):
        pass


class _FakeBuilder:
    def __init__(self, app):
        self._app = app

    def __getattr__(self, name):
        if name == "build":
            return lambda: self._app
        return lambda *a, **k: self


@pytest.fixture(scope="module")
def registered():
    app = _FakeApp()
    mp = pytest.MonkeyPatch()
    try:
        mp.setattr(tb, "Application", types.SimpleNamespace(builder=lambda: _FakeBuilder(app)))
        mp.setattr(tb, "_ipv4_request", lambda *a, **k: None)
        import logging_setup
        mp.setattr(logging_setup, "setup_logging", lambda *a, **k: None)
        tb.main()
    finally:
        mp.undo()
    return app


def _command_handlers(app):
    return [h for h, _ in app.handlers if isinstance(h, tb.CommandHandler)]


def _registered_command_names(app):
    names = set()
    for h in _command_handlers(app):
        names |= set(h.commands)
    return names


def test_command_set_is_exact(registered):
    names = _registered_command_names(registered)
    assert names == EXPECTED_COMMANDS, (
        f"missing: {sorted(EXPECTED_COMMANDS - names)}; "
        f"unexpected: {sorted(names - EXPECTED_COMMANDS)}. If you added/removed a "
        "command on purpose, update EXPECTED_COMMANDS; if not, a handler was "
        "dropped in the split.")
    assert len(_command_handlers(registered)) == len(EXPECTED_COMMANDS), \
        "a command is registered twice"


def test_callback_query_handlers(registered):
    got = {h.pattern.pattern: h.callback.__name__
           for h, _ in registered.handlers
           if isinstance(h, tb.CallbackQueryHandler)}
    assert got == EXPECTED_CALLBACKS


def test_message_handlers(registered):
    got = {h.callback.__name__: repr(h.filters).lower()
           for h, _ in registered.handlers if isinstance(h, tb.MessageHandler)}
    assert set(got) == set(EXPECTED_MESSAGE_HANDLERS)
    for cb, needles in EXPECTED_MESSAGE_HANDLERS.items():
        for n in needles:
            assert n in got[cb], f"{cb} filters changed: {got[cb]}"


def test_other_handlers_and_error_handler(registered):
    got = {(type(h).__name__, h.callback.__name__, g) for h, g in registered.handlers
           if isinstance(h, tb.TypeHandler)}
    assert got == EXPECTED_OTHER_HANDLERS
    assert {cb.__name__ for cb in registered.error_handlers} == EXPECTED_ERROR_HANDLERS


def test_total_handler_count(registered):
    assert len(registered.handlers) == (
        len(EXPECTED_COMMANDS) + len(EXPECTED_CALLBACKS)
        + len(EXPECTED_MESSAGE_HANDLERS) + len(EXPECTED_OTHER_HANDLERS))


def test_every_callback_reachable_on_telegram_bot(registered):
    for cb in [h.callback for h, _ in registered.handlers] + registered.error_handlers:
        name = cb.__name__
        assert getattr(tb, name, None) is cb, (
            f"handler callback {name!r} is not reachable as telegram_bot.{name} "
            f"(or is a different object). After the split, re-export it in "
            f"telegram_bot.py: `from bot.<module> import {name}`.")


def test_menu_lists_only_registered_commands(registered):
    sent = []

    class _Bot:
        async def set_my_commands(self, commands):
            sent.extend(commands)

    class _App:
        bot = _Bot()

        def create_task(self, coro):
            coro.close()

    mp = pytest.MonkeyPatch()
    try:
        mp.setattr(tb, "ALLOWED_USER_IDS", [])  # skip background loops
        asyncio.run(tb._post_init(_App()))
    finally:
        mp.undo()
    assert sent, "_post_init published no commands"
    menu = {c.command for c in sent}
    names = _registered_command_names(registered)
    assert menu <= names, f"menu lists commands with no handler: {sorted(menu - names)}"
    missing = names - menu - MENU_MISSING_ALLOWLIST
    assert not missing, (
        "registered but absent from the '/' menu (add to HELP_SECTIONS or to "
        f"MENU_MISSING_ALLOWLIST): {sorted(missing)}")


# ---------------------------------------------------------------- Part B ----

def _module_aliases(tree):
    """Names bound to the telegram_bot module anywhere in this file."""
    aliases = {"telegram_bot"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name == "telegram_bot":
                    aliases.add(a.asname or a.name)
    return aliases


def _str(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _test_sources():
    for path in sorted(TESTS_DIR.glob("*.py")):
        if path.name == Path(__file__).name:
            continue
        yield path.name, path.read_text(encoding="utf-8")


def _patched_names(sources=None):
    """{name: [files]} of telegram_bot attributes the tests patch.
    `sources` is (file name, text) pairs; default: every test file but this one."""
    found = {}

    def add(name, f):
        if name:
            found.setdefault(name, set()).add(f)

    for fname_, text in (_test_sources() if sources is None else sources):
        tree = ast.parse(text)
        al = _module_aliases(tree)

        def is_mod(n):
            return isinstance(n, ast.Name) and n.id in al

        for node in ast.walk(tree):
            targets = []
            if isinstance(node, ast.Assign):
                targets = node.targets
            elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
                targets = [node.target]
            for t in targets:
                for leaf in (t.elts if isinstance(t, (ast.Tuple, ast.List)) else [t]):
                    if isinstance(leaf, ast.Attribute) and is_mod(leaf.value):
                        add(leaf.attr, fname_)
            if isinstance(node, ast.Call):
                fn = node.func
                fname = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
                args = node.args
                if not args:
                    continue
                first = _str(args[0])
                if fname in ("setattr", "patch") and first and first.startswith("telegram_bot."):
                    add(first.split(".")[1], fname_)
                elif fname in ("setattr", "object") and is_mod(args[0]) and len(args) > 1:
                    if _str(args[1]):
                        add(_str(args[1]), fname_)
                    elif fname == "setattr":
                        # setattr(tb, name, ...) in a loop: take the string
                        # literals of tuples/lists in the enclosing function.
                        for fdef in ast.walk(tree):
                            if isinstance(fdef, (ast.FunctionDef, ast.AsyncFunctionDef))                                     and node in list(ast.walk(fdef)):
                                for seq in ast.walk(fdef):
                                    if isinstance(seq, (ast.Tuple, ast.List)):
                                        for e in seq.elts:
                                            if _str(e) and _str(e).isidentifier():
                                                add(_str(e), fname_)
    return {k: sorted(v) for k, v in found.items()}


def _names_read_by_telegram_bot():
    seen = set()

    def walk(code):
        seen.update(code.co_names)
        for c in code.co_consts:
            if isinstance(c, types.CodeType):
                walk(c)

    g = vars(tb)
    for v in list(g.values()):
        if isinstance(v, types.FunctionType) and v.__globals__ is g:
            walk(v.__code__)
        elif isinstance(v, type) and getattr(v, "__module__", None) == tb.__name__:
            for m in vars(v).values():
                m = getattr(m, "__func__", m)
                if isinstance(m, types.FunctionType) and m.__globals__ is g:
                    walk(m.__code__)
    return seen


_SCAN_SAMPLE = '''
import telegram_bot as tb
from unittest import mock

def test_a(monkeypatch):
    monkeypatch.setattr(tb, "by_setattr", 1)
    monkeypatch.setattr("telegram_bot.by_dotted_string", 1)
    tb.by_assign = 1
    tb.by_augassign += 1
    mock.patch("telegram_bot.by_patch")
    mock.patch.object(tb, "by_patch_object")

def test_loop(monkeypatch):
    for name in ("by_loop_one", "by_loop_two"):
        monkeypatch.setattr(tb, name, None)
'''


def test_patch_scan_finds_names():
    # The scanner itself, on every patch form it claims to understand. (Most
    # tests now patch the module that owns a name, not telegram_bot, so a count
    # of real telegram_bot patches no longer says whether the scanner works.)
    assert set(_patched_names([("sample.py", _SCAN_SAMPLE)])) == {
        "by_setattr", "by_dotted_string", "by_assign", "by_augassign", "by_patch",
        "by_patch_object", "by_loop_one", "by_loop_two"}, "scanner is broken"
    assert _patched_names(), "AST scan of the real tests found nothing; scanner is broken"


def test_patched_names_exist_and_take_effect():
    patched = _patched_names()
    read = _names_read_by_telegram_bot()
    problems = []
    for name, files in sorted(patched.items()):
        where = ", ".join(files)
        if not hasattr(tb, name):
            problems.append(
                f"{name}: tests ({where}) patch telegram_bot.{name} but it no "
                "longer exists. Re-export it from telegram_bot or point the test "
                "at the module that now owns it.")
        elif name not in read and name not in UNREACHED_PATCH_ALLOWLIST:
            problems.append(
                f"{name}: tests ({where}) patch telegram_bot.{name}, but no "
                "function defined in telegram_bot reads it as a global any more. "
                "The code that uses it probably moved to bot/<module>.py, so the "
                "fake is silently ignored and the real code runs. Patch it where "
                f"it is used (monkeypatch.setattr(bot.<module>, '{name}', fake)) "
                "or, for shared state, use `from bot import state` and patch "
                f"state.{name}.")
    assert not problems, "\n" + "\n".join(problems)
    stale = UNREACHED_PATCH_ALLOWLIST - set(patched)
    assert not stale, f"UNREACHED_PATCH_ALLOWLIST has names no test patches: {sorted(stale)}"
