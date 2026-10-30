"""Send a Telegram message to the bot's owners. Used by scripts/deploy.ps1.

    venv\\Scripts\\python.exe scripts\\notify.py "✅ Deployed abc123"

Reads TELEGRAM_BOT_TOKEN and TELEGRAM_ALLOWED_USER_IDS from the repo's .env and
sends the text to every allowed id. Exit 0 if at least one message went out,
1 otherwise. Never prints the token.

Standalone on purpose: importing the bot pulls in db.py, which opens the
database at import time.
"""
import os
import sys
from pathlib import Path

import httpx
from dotenv import load_dotenv

REPO = Path(__file__).resolve().parent.parent
MAX_LEN = 4000  # Telegram's limit is 4096


def parse_ids(raw: str | None) -> list[int]:
    """Same format as the bot's allowlist: ids split by commas or semicolons."""
    ids = []
    for tok in (raw or "").replace(";", ",").split(","):
        tok = tok.strip()
        if tok.isdigit() and int(tok) not in ids:
            ids.append(int(tok))
    return ids


def send(text: str, token: str, chat_ids: list[int], client: httpx.Client) -> int:
    """Send text to each chat. Returns how many sends worked."""
    if len(text) > MAX_LEN:
        text = text[:MAX_LEN - 15] + "\n…(truncated)"
    ok = 0
    for chat_id in chat_ids:
        try:
            r = client.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": text, "disable_web_page_preview": True},
            )
            if r.status_code == 200:
                ok += 1
            else:
                print(f"notify: chat {chat_id} failed, HTTP {r.status_code}", file=sys.stderr)
        except httpx.HTTPError as e:
            # The exception text can include the URL, which holds the token.
            print(f"notify: chat {chat_id} failed, {type(e).__name__}", file=sys.stderr)
    return ok


def main(argv: list[str]) -> int:
    text = " ".join(argv).strip()
    if not text:
        print('usage: notify.py "message"', file=sys.stderr)
        return 1
    load_dotenv(REPO / ".env")
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_ids = parse_ids(os.getenv("TELEGRAM_ALLOWED_USER_IDS"))
    if not token or not chat_ids:
        print("notify: TELEGRAM_BOT_TOKEN or TELEGRAM_ALLOWED_USER_IDS is not set", file=sys.stderr)
        return 1
    # IPv4 only, like the bot: IPv6 to api.telegram.org has failed TLS here.
    transport = httpx.HTTPTransport(local_address="0.0.0.0", retries=2)
    with httpx.Client(transport=transport, timeout=20) as client:
        sent = send(text, token, chat_ids, client)
    return 0 if sent else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
