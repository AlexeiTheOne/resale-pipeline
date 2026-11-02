"""Claude Code hook: run the test suite before a git commit and block the
commit if it fails. Wired up in .claude/settings.json.

Exit 0 lets the commit go ahead. Exit 2 blocks it, and stderr (the end of the
pytest output) is shown to Claude so it can fix the failure.
"""
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
# The hook is started with whatever `python` is on PATH, which may not be the
# project's Python (or have pytest). Use the repo's venv when there is one.
VENV_PY = REPO / "venv" / "Scripts" / "python.exe"


def main() -> int:
    python = str(VENV_PY) if VENV_PY.exists() else sys.executable
    # A private --basetemp: pytest's shared Temp/pytest-of-<user> folder can hold
    # a link this process may not read, which crashes pytest after the tests pass.
    basetemp = tempfile.mkdtemp(prefix="ross_precommit_")
    try:
        r = subprocess.run(
            [python, "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider",
             f"--basetemp={basetemp}"],
            cwd=REPO, capture_output=True, text=True, timeout=600,
        )
    finally:
        shutil.rmtree(basetemp, ignore_errors=True)
    if r.returncode == 0:
        return 0
    tail = "\n".join((r.stdout + r.stderr).strip().splitlines()[-30:])
    print(f"Tests failed, commit blocked. Fix them first.\n{tail}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
