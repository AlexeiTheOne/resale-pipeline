"""Import conventions for bot/*.py (docs/cleanup-plan.md Phase 2), AST-based.

(a) state is `from bot import state`, never `from bot.state import ...`: the
    dicts are shared and tests reset them through bot.state.
(b) no bot/ module imports telegram_bot: it is the entry point, not a library.
(c) callables from the dependency modules below are reached as module.attr
    (db.get_item, stock_lookup.lookup), never imported by name, at module level
    or inside a function. A test patching the source module then reaches every
    caller; a by-name import keeps its own reference and the fake is ignored.
    `from pipeline import stock_lookup` (a module) is fine.
"""
import ast
import importlib.util
from pathlib import Path

import pytest

BOT_DIR = Path(__file__).parent.parent / "bot"

DEP_MODULES = {"db", "identify", "ross_tag", "llm", "claude_llm", "llm_usage",
               "profit", "report"}
DEP_PACKAGES = {"pipeline", "ebay"}  # and every submodule

# (module, name) pairs that may be imported by name: exception classes, other
# classes and plain constants. Patching them in tests is not a thing, so the
# by-name reference can't go stale. Add "(module, name),  # why".
NAME_IMPORT_ALLOWLIST = {
    ("ebay.inventory", "TitleTooLongError"),  # exception class, a type hint in bot/formatting.py
}


def _bot_files():
    files = sorted(BOT_DIR.glob("*.py"))
    assert len(files) >= 10, f"found too few bot modules: {files}"
    return files


def _is_dep(module: str) -> bool:
    root = module.split(".")[0]
    return module in DEP_MODULES or root in DEP_PACKAGES


def _is_module(module: str, name: str) -> bool:
    """Is `from module import name` importing a submodule (allowed)?"""
    spec = importlib.util.find_spec(module)
    if spec is None or spec.submodule_search_locations is None:
        return False  # a plain module: `name` is an attribute, not a submodule
    return importlib.util.find_spec(f"{module}.{name}") is not None


def _violations(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out = []
    for node in ast.walk(tree):
        where = f"{path.name}:{node.lineno}" if hasattr(node, "lineno") else path.name
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name == "telegram_bot" or a.name.startswith("telegram_bot."):
                    out.append(f"{where}: imports telegram_bot (rule b)")
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if node.level:
                out.append(f"{where}: relative import; use absolute `from bot import ...`")
                continue
            if mod == "telegram_bot" or mod.startswith("telegram_bot."):
                out.append(f"{where}: imports from telegram_bot (rule b)")
            elif mod == "bot.state":
                out.append(f"{where}: `from bot.state import ...`; use `from bot import state` (rule a)")
            elif _is_dep(mod):
                for a in node.names:
                    if a.name == "*":
                        out.append(f"{where}: `from {mod} import *` (rule c)")
                    elif not _is_module(mod, a.name) and (mod, a.name) not in NAME_IMPORT_ALLOWLIST:
                        out.append(
                            f"{where}: `from {mod} import {a.name}`; import the module and "
                            f"call {mod.split('.')[-1]}.{a.name} (rule c), or add "
                            f"({mod!r}, {a.name!r}) to NAME_IMPORT_ALLOWLIST if it is a "
                            "class / exception / constant")
    return out


def test_bot_import_conventions():
    problems = [v for f in _bot_files() for v in _violations(f)]
    assert not problems, "\n" + "\n".join(problems)


def test_allowlist_entries_are_used():
    used = set()
    for f in _bot_files():
        for node in ast.walk(ast.parse(f.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom) and node.module:
                used |= {(node.module, a.name) for a in node.names}
    stale = NAME_IMPORT_ALLOWLIST - used
    assert not stale, f"NAME_IMPORT_ALLOWLIST has entries nothing imports: {sorted(stale)}"


@pytest.mark.parametrize("src, rule", [
    ("from bot.state import review\n", "rule a"),
    ("import telegram_bot\n", "rule b"),
    ("from telegram_bot import main\n", "rule b"),
    ("from db import get_item\n", "rule c"),
    ("def f():\n    from pipeline.stock_lookup import lookup\n", "rule c"),
    ("def f():\n    from ebay.auth import exchange_code\n", "rule c"),
    ("from profit import sale_profit as sp\n", "rule c"),
])
def test_checker_catches(tmp_path, src, rule):
    p = tmp_path / "sample.py"
    p.write_text(src, encoding="utf-8")
    found = _violations(p)
    assert found and rule in found[0], found


@pytest.mark.parametrize("src", [
    "from bot import state, util\n",
    "import db\n",
    "from pipeline import stock_lookup\n",
    "def f():\n    from ebay import orders as ebay_orders\n",
    "from ebay.inventory import TitleTooLongError\n",
    "from telegram.ext import Application\n",
])
def test_checker_allows(tmp_path, src):
    p = tmp_path / "sample.py"
    p.write_text(src, encoding="utf-8")
    assert _violations(p) == []
