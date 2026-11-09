"""Claude Code hook: run the test suite before a git commit and block the
commit if it fails. Wired up in .claude/settings.json.

Exit 0 lets the command go ahead. Exit 2 blocks it, and stderr (the end of the
pytest output) is shown to Claude so it can fix the failure.

The settings' `if: "...(git commit*)"` filter still fires on commands it can't
parse (pipes, loops, long PowerShell lines), so this script checks the command
itself (the hook's JSON on stdin) and does nothing unless it runs `git commit`.
"""
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
# The hook is started with whatever `python` is on PATH, which may not be the
# project's Python (or have pytest). Use the repo's venv when there is one.
VENV_PY = REPO / "venv" / "Scripts" / "python.exe"

# `git commit`, also with options before the subcommand (`git -C dir commit`).
_COMMIT = re.compile(r"\bgit(?:\s+-{1,2}[\w.-]+(?:[= ]\S+)?)*\s+commit\b")


def is_commit(command: str) -> bool:
    return bool(_COMMIT.search(command or ""))


def command_from_stdin(raw: str) -> str | None:
    """The tool's command, or None when stdin isn't the hook JSON."""
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    cmd = (data.get("tool_input") or {}).get("command") if isinstance(data, dict) else None
    return cmd if isinstance(cmd, str) else None


def run_tests() -> int:
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


def main(stdin: str | None = None) -> int:
    raw = sys.stdin.read() if stdin is None else stdin
    cmd = command_from_stdin(raw)
    # Unknown input: run the tests anyway, as before (safe side).
    if cmd is not None and not is_commit(cmd):
        return 0
    return run_tests()


if __name__ == "__main__":
    sys.exit(main())
