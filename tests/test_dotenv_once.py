"""Tripwire: .env is loaded in one place, config.py (cleanup plan Phase 3).

Before this, nine modules each called load_dotenv(), so a config value depended
on which module happened to be imported first. scripts/notify.py is the one
allowed exception: deploy.ps1 runs it standalone.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ALLOWED = {"config.py", "scripts/notify.py"}
SKIP_DIRS = {"tests", "venv", ".venv", "node_modules", "web", ".git"}


def test_only_config_loads_dotenv():
    offenders = []
    for path in ROOT.rglob("*.py"):
        rel = path.relative_to(ROOT).as_posix()
        if rel.split("/")[0] in SKIP_DIRS or rel in ALLOWED:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if re.search(r"\bload_dotenv\s*\(", text):
            offenders.append(rel)
    assert offenders == [], f"load_dotenv outside config.py: {offenders}"


def test_config_loads_repo_env_by_absolute_path():
    import config
    assert config.ENV_FILE == config.BASE_DIR / ".env"
    assert config.ENV_FILE.is_absolute()
