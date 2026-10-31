"""Claude Code hook: run the test suite before a git commit and block the
commit if it fails. Wired up in .claude/settings.json.

Exit 0 lets the commit go ahead. Exit 2 blocks it, and stderr (the end of the
pytest output) is shown to Claude so it can fix the failure.
"""
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def main() -> int:
    r = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider"],
        cwd=REPO, capture_output=True, text=True, timeout=600,
    )
    if r.returncode == 0:
        return 0
    tail = "\n".join((r.stdout + r.stderr).strip().splitlines()[-30:])
    print(f"Tests failed, commit blocked. Fix them first.\n{tail}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
