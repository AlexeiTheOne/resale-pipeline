import base64
import os
import sqlite3
import sys
import time
import urllib.parse
from pathlib import Path

import httpx
from dotenv import load_dotenv

load_dotenv()

CLIENT_ID = os.getenv("EBAY_CLIENT_ID")
CLIENT_SECRET = os.getenv("EBAY_CLIENT_SECRET")
REDIRECT_URI = os.getenv("EBAY_REDIRECT_URI")  # this is the eBay "RuName", not a real URL

AUTH_URL = "https://auth.ebay.com/oauth2/authorize"
TOKEN_URL = "https://api.ebay.com/identity/v1/oauth2/token"

SCOPES = [
    "https://api.ebay.com/oauth/api_scope/sell.inventory",
    "https://api.ebay.com/oauth/api_scope/sell.account",
    # Promoted Listings (ebay/marketing.py). Adding a scope does NOT extend an
    # already-issued token — after this change the seller must re-run
    # `python -m ebay.auth` and re-consent, or Marketing API calls will 403.
    "https://api.ebay.com/oauth/api_scope/sell.marketing",
]

DB_PATH = "data/ross.db"


def _conn():
    Path("data").mkdir(exist_ok=True)
    return sqlite3.connect(DB_PATH)


def _create_table() -> None:
    with _conn() as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS ebay_tokens (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                access_token TEXT,
                refresh_token TEXT,
                expires_at REAL
            )
        """)


_create_table()


def _basic_auth_header() -> str:
    raw = f"{CLIENT_ID}:{CLIENT_SECRET}".encode()
    return base64.b64encode(raw).decode()


def get_consent_url() -> str:
    """URL to send the seller to so they can grant this app a user token."""
    params = {
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": " ".join(SCOPES),
    }
    return f"{AUTH_URL}?{urllib.parse.urlencode(params)}"


def _save_token(token: dict) -> None:
    now = time.time()
    access_token = token["access_token"]
    expires_at = now + token.get("expires_in", 0)
    refresh_token = token.get("refresh_token")

    with _conn() as con:
        if refresh_token:
            con.execute(
                "INSERT INTO ebay_tokens (id, access_token, refresh_token, expires_at) "
                "VALUES (1, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET "
                "access_token = excluded.access_token, "
                "refresh_token = excluded.refresh_token, "
                "expires_at = excluded.expires_at",
                (access_token, refresh_token, expires_at),
            )
        else:
            con.execute(
                "INSERT INTO ebay_tokens (id, access_token, refresh_token, expires_at) "
                "VALUES (1, ?, NULL, ?) "
                "ON CONFLICT(id) DO UPDATE SET "
                "access_token = excluded.access_token, "
                "expires_at = excluded.expires_at",
                (access_token, expires_at),
            )


def _load_token() -> dict | None:
    with _conn() as con:
        cur = con.execute(
            "SELECT access_token, refresh_token, expires_at FROM ebay_tokens WHERE id = 1"
        )
        row = cur.fetchone()
        if row is None:
            return None
        return {"access_token": row[0], "refresh_token": row[1], "expires_at": row[2]}


def exchange_code(code: str) -> dict:
    """First-time setup: trade the authorization code from the consent redirect for tokens."""
    code = urllib.parse.unquote(code)
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Authorization": f"Basic {_basic_auth_header()}",
    }
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT_URI,
    }
    r = httpx.post(TOKEN_URL, headers=headers, data=data, timeout=30)
    if r.status_code >= 400:
        raise RuntimeError(f"eBay token exchange failed [{r.status_code}]: {r.text}")
    token = r.json()
    _save_token(token)
    return token


def _refresh(refresh_token: str) -> dict:
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Authorization": f"Basic {_basic_auth_header()}",
    }
    data = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "scope": " ".join(SCOPES),
    }
    r = httpx.post(TOKEN_URL, headers=headers, data=data, timeout=30)
    if r.status_code >= 400:
        raise RuntimeError(f"eBay token refresh failed [{r.status_code}]: {r.text}")
    token = r.json()
    _save_token(token)
    return token


_app_token_cache = {"access_token": None, "expires_at": 0}


def get_app_access_token() -> str:
    """Application-only token (client_credentials grant). No user consent needed —
    used for public catalog data like the Taxonomy API, kept separate from the
    seller's user token so it can't affect that token's stored scopes."""
    if _app_token_cache["access_token"] and time.time() < _app_token_cache["expires_at"] - 60:
        return _app_token_cache["access_token"]

    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Authorization": f"Basic {_basic_auth_header()}",
    }
    data = {
        "grant_type": "client_credentials",
        "scope": "https://api.ebay.com/oauth/api_scope",
    }
    r = httpx.post(TOKEN_URL, headers=headers, data=data, timeout=30)
    if r.status_code >= 400:
        raise RuntimeError(f"eBay app token request failed [{r.status_code}]: {r.text}")
    token = r.json()
    _app_token_cache["access_token"] = token["access_token"]
    _app_token_cache["expires_at"] = time.time() + token.get("expires_in", 0)
    return _app_token_cache["access_token"]


def get_access_token() -> str:
    """Return a valid user access token, refreshing it first if it's expired."""
    stored = _load_token()
    if stored is None or not stored["access_token"]:
        raise RuntimeError(
            "No eBay token on file. Run `python -m ebay.auth` to get a consent URL, "
            "then `python -m ebay.auth exchange <code>` once you've approved access."
        )

    if time.time() < stored["expires_at"] - 60:
        return stored["access_token"]

    if not stored["refresh_token"]:
        raise RuntimeError(
            "eBay access token expired and no refresh token is stored. "
            "Run `python -m ebay.auth` to re-authorize."
        )

    refreshed = _refresh(stored["refresh_token"])
    return refreshed["access_token"]


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "exchange":
        if len(sys.argv) < 3:
            print("Usage: python -m ebay.auth exchange <code>")
            sys.exit(1)
        token = exchange_code(sys.argv[2])
        print(f"Token stored. Expires in {token.get('expires_in')} seconds.")
    else:
        print("1. Open this URL, log in as the seller, and approve access:\n")
        print(get_consent_url())
        print("\n2. eBay will redirect to your RuName's configured URL with a `code` query param.")
        print("   Copy that code value, then run:\n")
        print("   python -m ebay.auth exchange <code>")
