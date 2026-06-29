import os

COMPS_COUNT = 10
ACTIVE_COUNT = 5
DEBUG_MODE = True
UNDERCUT_PCT = 0.15

# Pro handles the grounded research step (identify stage 1), where reasoning over
# web/eBay results pays off.
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-pro")

# Faster, cheaper model for steps that only structure data we already have: the
# identify format stage and the draft writer. These don't need pro's grounded
# reasoning, and flash avoids pro's slow, thinking-heavy responses.
GEMINI_FAST_MODEL = os.getenv("GEMINI_FAST_MODEL", "gemini-2.5-flash")

# How many of the uploaded photos to send to Gemini for identification. All
# photos are still kept for the eBay listing; only the first N (the overview +
# tag close-up the user sends first) go through the paid API to save tokens.
GEMINI_PHOTO_LIMIT = int(os.getenv("GEMINI_PHOTO_LIMIT", "3"))

EBAY_MARKETPLACE_ID = "EBAY_US"
EBAY_CURRENCY = "USD"
EBAY_MERCHANT_LOCATION_KEY = "ross-resale-warehouse"
EBAY_SHIP_FROM_ADDRESS = {
    "city": "Doral",
    "stateOrProvince": "FL",
    "postalCode": "33172",
    "country": "US",
}
