"""
check_keys.py - verify EbRossLister credentials on this PC.

Looks for each key in environment variables (and a .env file in this folder,
if python-dotenv is installed), then makes a real test call to each service.
Values are masked in the output.

If your project uses different variable names, edit the NAMES dict below.

pip install requests python-dotenv
"""

import os
import base64
import requests

try:
    from dotenv import load_dotenv
    load_dotenv()
    print("Loaded .env file (if present)\n")
except ImportError:
    print("python-dotenv not installed - reading system environment only\n")

# Edit these to match the variable names in your project's .env
NAMES = {
    "EBAY_CLIENT_ID": "eBay App ID (Client ID)",
    "EBAY_CLIENT_SECRET": "eBay Cert ID (Client Secret)",
    "EBAY_REFRESH_TOKEN": "eBay user refresh token (needed to publish listings)",
    "APIFY_TOKEN": "Apify API token",
    "APIFY_SOLD_ACTOR": "Apify actor ID for sold listings",
    "TELEGRAM_BOT_TOKEN": "Telegram bot token",
    "GEMINI_API_KEY": "Google Gemini API key",
    "CLOUDINARY_CLOUD_NAME": "Cloudinary cloud name",
    "CLOUDINARY_API_KEY": "Cloudinary API key",
    "CLOUDINARY_API_SECRET": "Cloudinary API secret",
}

TIMEOUT = 20
results = []


def get(name):
    return os.getenv(name)


def mask(v):
    if not v:
        return "-"
    return v[:4] + "..." + v[-3:] if len(v) > 10 else "***"


def record(service, ok, detail):
    results.append((service, ok, detail))


# ------------------------------------------------------------ presence
print("=== Which keys are present ===")
for name, desc in NAMES.items():
    v = get(name)
    status = "FOUND  " if v else "MISSING"
    print(f"{status}  {name:<24} {mask(v):<14} {desc}")
print()

# ------------------------------------------------------------ live tests
print("=== Live tests ===")

# eBay app token (needed for Browse API / active listings)
cid, csec = get("EBAY_CLIENT_ID"), get("EBAY_CLIENT_SECRET")
basic = base64.b64encode(f"{cid}:{csec}".encode()).decode() if cid and csec else None
if basic:
    try:
        r = requests.post(
            "https://api.ebay.com/identity/v1/oauth2/token",
            headers={"Authorization": f"Basic {basic}",
                     "Content-Type": "application/x-www-form-urlencoded"},
            data={"grant_type": "client_credentials",
                  "scope": "https://api.ebay.com/oauth/api_scope"},
            timeout=TIMEOUT)
        record("eBay app keys", r.ok, "OK" if r.ok else f"{r.status_code} {r.text[:150]}")
    except Exception as e:
        record("eBay app keys", False, str(e))
else:
    record("eBay app keys", False, "client ID or secret missing")

# eBay user refresh token (needed for Inventory/Offer publishing)
rt = get("EBAY_REFRESH_TOKEN")
if basic and rt:
    try:
        r = requests.post(
            "https://api.ebay.com/identity/v1/oauth2/token",
            headers={"Authorization": f"Basic {basic}",
                     "Content-Type": "application/x-www-form-urlencoded"},
            data={"grant_type": "refresh_token", "refresh_token": rt,
                  "scope": "https://api.ebay.com/oauth/api_scope/sell.inventory"},
            timeout=TIMEOUT)
        record("eBay refresh token", r.ok, "OK" if r.ok else f"{r.status_code} {r.text[:150]}")
    except Exception as e:
        record("eBay refresh token", False, str(e))
else:
    record("eBay refresh token", False, "missing (or app keys missing)")

# Apify
tok = get("APIFY_TOKEN")
if tok:
    try:
        r = requests.get("https://api.apify.com/v2/users/me",
                         params={"token": tok}, timeout=TIMEOUT)
        user = r.json().get("data", {}).get("username") if r.ok else None
        record("Apify token", r.ok, f"OK (user: {user})" if r.ok else f"{r.status_code}")
    except Exception as e:
        record("Apify token", False, str(e))

    actor = get("APIFY_SOLD_ACTOR")
    if actor:
        try:
            r = requests.get(f"https://api.apify.com/v2/acts/{actor.replace('/', '~')}",
                             params={"token": tok}, timeout=TIMEOUT)
            record("Apify actor", r.ok, "OK (actor exists)" if r.ok else f"{r.status_code} not found")
        except Exception as e:
            record("Apify actor", False, str(e))
    else:
        record("Apify actor", False, "APIFY_SOLD_ACTOR not set")
else:
    record("Apify token", False, "missing")

# Telegram
tg = get("TELEGRAM_BOT_TOKEN")
if tg:
    try:
        r = requests.get(f"https://api.telegram.org/bot{tg}/getMe", timeout=TIMEOUT)
        name = r.json().get("result", {}).get("username") if r.ok else None
        record("Telegram bot", r.ok, f"OK (@{name})" if r.ok else f"{r.status_code}")
    except Exception as e:
        record("Telegram bot", False, str(e))
else:
    record("Telegram bot", False, "missing")

# Gemini
gk = get("GEMINI_API_KEY")
if gk:
    try:
        r = requests.get("https://generativelanguage.googleapis.com/v1beta/models",
                         params={"key": gk}, timeout=TIMEOUT)
        record("Gemini key", r.ok, "OK" if r.ok else f"{r.status_code} {r.text[:150]}")
    except Exception as e:
        record("Gemini key", False, str(e))
else:
    record("Gemini key", False, "missing")

# Cloudinary
cn, ck, cs = get("CLOUDINARY_CLOUD_NAME"), get("CLOUDINARY_API_KEY"), get("CLOUDINARY_API_SECRET")
if cn and ck and cs:
    try:
        r = requests.get(f"https://api.cloudinary.com/v1_1/{cn}/usage",
                         auth=(ck, cs), timeout=TIMEOUT)
        record("Cloudinary", r.ok, "OK" if r.ok else f"{r.status_code} {r.text[:150]}")
    except Exception as e:
        record("Cloudinary", False, str(e))
else:
    record("Cloudinary", False, "cloud name, key or secret missing")

# ------------------------------------------------------------ summary
print()
for service, ok, detail in results:
    print(f"{'PASS' if ok else 'FAIL'}  {service:<20} {detail}")

failed = [s for s, ok, _ in results if not ok]
print(f"\n{len(results) - len(failed)}/{len(results)} passed.")
if failed:
    print("Fix:", ", ".join(failed))