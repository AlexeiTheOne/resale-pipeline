"""What a Ross receipt line is, in words (docs/receipt-decoding.md).

Ross prints about 18 characters per line ("EH BLK SKULL CROSS", "#ASST RUN/COURT",
"NOEX Drunk Elephan"). Each unique description is decoded once and cached in
receipt_line_details (migration step 5), in this order:

  1. matched_item: every line with this description has a SKU that is one of
     our items (or pieces): the item's own identity, no AI;
  2. rules: brand codes and department markers, kept only when sure;
  3. ai: the cheap Claude model for the rest, 50 descriptions a call, with the
     rules' guess and the line prices as hints.

The item a line paid for is NOT taken from this cache: one description can
cover several SKUs, so /rcpt works it out per line from the line's own SKU
when it reads it. misread_reasons() flags lines the receipt reader got wrong;
it is computed live and never written back (receipt_pipeline owns the lines).
"""
import json
import re
from collections import Counter

import config

RULES_VERSION = "rules-v3"
AI_STAGE = "receipt_decode"

# --- vocabularies (closed lists; normalised in code, so no migration to change) ---

CATEGORIES = ("clothing", "shoes", "bags", "accessories", "jewelry_watches", "fragrance", "beauty", "health",
              "food", "household", "home", "toys", "sports_outdoor", "electronics", "tools", "baby_gear", "pet",
              "other")
TYPES = {
    "clothing": ("tee", "polo", "shirt", "top", "tank", "sweater", "hoodie", "fleece", "jacket", "vest", "jersey",
                 "jeans", "pants", "joggers", "leggings", "shorts", "dress", "skirt", "swim", "sleepwear", "set",
                 "baby", "socks", "underwear", "bra"),
    "shoes": ("sneakers", "cleats", "sandals", "slides", "boots", "flats", "loafers", "heels", "slip_on", "clogs",
              "slippers"),
    "bags": ("tote", "crossbody", "shoulder", "satchel", "backpack", "mini_backpack", "duffel", "luggage", "wallet",
             "card_case", "wristlet", "clutch", "belt_bag", "messenger", "pouch"),
    "accessories": ("belt", "hat", "sunglasses", "scarf", "gloves", "hair", "keychain", "tie"),
    "jewelry_watches": ("watch", "necklace", "bracelet", "earrings", "ring", "set"),
    "fragrance": ("fragrance", "gift_set", "body_mist"),
    "beauty": ("skincare", "makeup", "nails", "bath_body", "hair_care", "grooming", "oral_care", "gift_set"),
    "health": ("supplement", "otc"),
    "food": ("candy", "snack", "drink", "coffee_tea", "pantry"),
    "household": ("batteries", "cleaning", "baby_consumable", "paper"),
    "home": ("decor", "kitchen", "bedding", "bath", "rug", "storage", "seasonal"),
    "toys": ("building", "vehicle", "plush", "game", "puzzle", "arts_crafts", "figure"),
    "electronics": ("audio", "charger", "smart_home", "accessory"),
    "tools": ("power_tool", "hand_tool", "light"),
}
ALL_TYPES = tuple(dict.fromkeys(t for ts in TYPES.values() for t in ts))
GENDERS = ("men", "women", "unisex", "boys", "girls", "kids", "baby")
COLOURS = ("black", "white", "grey", "navy", "blue", "red", "pink", "purple", "green", "brown", "beige", "gold",
           "silver", "orange", "yellow", "multi")
WEARABLE = ("clothing", "shoes", "bags", "accessories", "jewelry_watches")
SOURCES = ("rules", "ai", "matched_item")
# What a rule's tier is worth: only "high" is saved as final; the rest go to
# the AI with the guess as a hint.
TIER_CONFIDENCE = {"high": 0.95, "medium": 0.85, "low": 0.6}
LOW_CONFIDENCE = 0.6          # /rcpt puts a "?" on decodes below this

# --- cleaning the text -------------------------------------------------------------------------

_BLEED = [  # OCR bleed-over from the next line or the footer
    (re.compile(r"\s*\*\*\s*Begin Return.*$", re.I), "return_marker"),
    (re.compile(r"\s+55\+?\s*Tu\w*\s+Disc.*$", re.I), "promo"),
    (re.compile(r"\s+551\s+Tu\w*.*$", re.I), "promo"),
    (re.compile(r"\s+Sub(total)?\b.*$", re.I), "subtotal_text"),
    (re.compile(r"\s*\$\s?\d+[.,]\s?\d{0,2}\s?[A-Z0-9?]?(\s.*)?$"), "price_text"),
    (re.compile(r"[\s\-/]*4[0OD][0-9OD:/?\"]{3}[0-9A-Za-z:/?\" ]{4,}.*$"), "merged_sku"),
    (re.compile(r"\s+4003\d{2,}.*$"), "merged_sku"),
]
_LEAD_JUNK = re.compile(r"^[\s\u2022\u00d7|\ufffd'`.&*]+")
_OCR_FIX = [
    (re.compile(r"(\d)0Z\b"), r"\1OZ"),               # 100Z -> 10OZ
    (re.compile(r"(\d)02\b(?=\s+[A-Z])"), r"\1OZ"),   # 1602 CHILI -> 16OZ
    (re.compile(r"\bB[1I\]]K\b"), "BLK"),
    (re.compile(r"\|"), "L"),
]
_PUNCT = str.maketrans({"\u2019": "'", "\u2018": "'", "\u201c": '"', "\u201d": '"', "\u2022": " ", "\u00d7": "x"})
_SEASON = re.compile(r"^(X?(?:H|F|SP|SF|FA|SS|S|W|M|XS)\d{2}|VDAY\d{2})\s+", re.I)
_DATE = re.compile(r"^(\d{1,2}/\d{1,2}(/\d{2,4})?|\d{1,2}/\d{2}F?|[b]/\d{2})\s*", re.I)
_MARKER = re.compile(r"^(#|NOEX\s+|NDEX\s+|OTC\s+|n!\s*|N!\s*|X\s+|x\s*(?=[a-z])|T\s*-\s*|V-|I\s+(?=NOEX|MC))")
_MARKER_NAMES = {"#": "hash", "NOEX": "noex", "NDEX": "noex", "OTC": "otc", "N!": "n!", "X": "x", "T-": "t-",
                 "V-": "v-", "I": "lead_junk"}
EMBEDDED_SKU = re.compile(r"4[0O]{2}[0-9O]{2}[0-9 /:?\"bOD]{4,}|4\d{9,11}")


def normalise(desc: str) -> tuple[str, list, list]:
    """(upper-case text without noise, markers like "hash"/"noex"/"dated",
    notes like "promo"/"merged_sku")."""
    notes, markers = [], []
    d = (desc or "").translate(_PUNCT).strip()
    for rx, tag in _BLEED:
        d2 = rx.sub("", d).strip()
        if d2 != d:
            notes.append(tag)
            d = d2
    d2 = _LEAD_JUNK.sub("", d)
    if d2 != d:
        notes.append("lead_junk")
        d = d2
    while True:
        m = _MARKER.match(d)
        if not m:
            s = _SEASON.match(d)
            if s:
                markers.append("season:" + s.group(1).upper())
                d = d[s.end():]
                continue
            t = _DATE.match(d)
            if t and not re.match(r"^\d{1,2}/(CD|\d+\s*X)", d, re.I):
                markers.append("dated")
                d = d[t.end():]
                continue
            break
        tok = m.group(1).strip().upper().replace(" ", "")
        markers.append(_MARKER_NAMES.get(tok, tok))
        d = d[m.end():]
    u = d.upper()
    for rx, rep in _OCR_FIX:
        u = rx.sub(rep, u)
    return u.strip(), markers, notes


def letters(text: str) -> int:
    return len(re.sub(r"[^A-Za-z]", "", text or ""))


def is_merged(desc: str) -> bool:
    """Two receipt lines read as one: long, with a SKU inside the text."""
    d = desc or ""
    return len(d) > 30 and bool(EMBEDDED_SKU.search(d))


# --- brands ------------------------------------------------------------------------------------

_BRANDS = [  # (regex on the normalised text, brand)
    (r"^(EH|ED HARDY|EHPINK|ER|CH)\b|^EH|INFANTED HARDY|INFANT ED HARDY|K ED HARDY", "Ed Hardy"),
    (r"^(TR|TRUE RELIG\w*|TRU RELIG|TRUE RELIE|TRRGN)\b|TRUE RELIGION|TRUE RELIGON", "True Religion"),
    (r"^VB\b", "Vera Bradley"), (r"^(LB|LUV)\b", "Luv Betsey"), (r"^BJ\d?|BETSEY", "Betsey Johnson"),
    (r"^MK\b|MICHAEL KORS", "Michael Kors"), (r"^(CK|CKJ)\b|CALVIN KLEIN", "Calvin Klein"),
    (r"^KL\b|LAGERFELD", "Karl Lagerfeld"), (r"^UA\b|UNDER ARMOUR|\bUA\b", "Under Armour"),
    (r"^ADI\b|ADIDAS|ADIDA\b|XADI", "adidas"), (r"\bJORDANS?\b|MICHAEL JORDAN|\bMJ\b", "Jordan"),
    (r"\bNIKE\b|XNIKE|\bNKCI\b|AIR MAX|\bSB\b|SHOX|GATO|METCON|PEGA", "Nike"),
    (r"^(POLO|RL|FOLO)\b|RALPH", "Polo Ralph Lauren"),
    (r"^(TOMMY|TH)\b|TOMMY", "Tommy Hilfiger"), (r"DKNY", "DKNY"),
    (r"^NAUT|NAUTICA", "Nautica"), (r"^(KS|KSPADE)\b|KATE SPADE", "Kate Spade"),
    (r"^SM\b", "Steve Madden"), (r"^(TB|TBAKER)\b", "Ted Baker"), (r"^JC\b|JUICY", "Juicy Couture"),
    (r"^QS\b|QUIK", "Quiksilver"), (r"^TNF\b", "The North Face"), (r"^KC\b|KENNETH COLE", "Kenneth Cole"),
    (r"^AK\b|ANNE KLEIN", "Anne Klein"), (r"PNASH", "Patricia Nash"), (r"NANETTE", "Nanette Lepore"),
    (r"^TAH\b|TAHARI", "Tahari"), (r"REEB(OK)?", "Reebok"), (r"PUMA", "Puma"), (r"LACOSTE", "Lacoste"),
    (r"GUESS", "Guess"), (r"CARHARTT|CALHARTT", "Carhartt"), (r"DICKIES", "Dickies"), (r"LEVI", "Levi's"),
    (r"WRANGLER|WRNGL", "Wrangler"), (r"VON DUTCH", "Von Dutch"), (r"BILLABONG", "Billabong"),
    (r"VOLCOM", "Volcom"), (r"KIPLING", "Kipling"), (r"SAMSONITE", "Samsonite"), (r"TUMI", "Tumi"),
    (r"JANSPORT", "JanSport"), (r"EASTPAK", "Eastpak"), (r"CHAMP(ION)?\b", "Champion"),
    (r"BEN SHERMAN", "Ben Sherman"), (r"PERRY ELLIS", "Perry Ellis"), (r"HARLEY DAVIDSON", "Harley-Davidson"),
    (r"COACH", "Coach"), (r"ALDO", "Aldo"), (r"DR\.? MARTENS|^DM\b", "Dr. Martens"),
    (r"CONVERSE|CHUCK TAYLOR|ONE STAR", "Converse"), (r"SKECHERS|^LPD SKX", "Skechers"),
    (r"TIMBERLAND", "Timberland"), (r"ARIAT", "Ariat"), (r"FILA\b", "Fila"), (r"RBX", "RBX"),
    (r"ECKO", "Ecko"), (r"SOUTHPOLE", "Southpole"), (r"CHAPS", "Chaps"), (r"IZOD", "Izod"),
    (r"BCBG", "BCBG"), (r"JESSICA SIMPSON", "Jessica Simpson"), (r"BURBERRY", "Burberry"),
    (r"AZZARO", "Azzaro"), (r"GUCCI", "Gucci"), (r"ARMANI", "Armani"), (r"LATTAFA", "Lattafa"),
    (r"PARIS CORNER", "Paris Corner"), (r"JIMMY CHOO", "Jimmy Choo"), (r"HUGO BOSS", "Hugo Boss"),
    (r"DRUNK ELEPHAN", "Drunk Elephant"), (r"TATA HA", "Tata Harper"), (r"DR\.? JART", "Dr. Jart+"),
    (r"STRIVECTIN", "StriVectin"), (r"KIEH[LI]S", "Kiehl's"), (r"SHISEIDO", "Shiseido"),
    (r"ELLIS BROOKLYN", "Ellis Brooklyn"), (r"HOT WHEELS|^HW\b", "Hot Wheels"),
    (r"^M&D\b|MELISSA", "Melissa & Doug"), (r"LEGO", "Lego"), (r"NERF", "Nerf"),
    (r"FISHER[- ]PRICE", "Fisher-Price"), (r"ENERGIZER", "Energizer"), (r"ACDEL|ADELCO", "ACDelco"),
    (r"WORKPRO", "WorkPro"), (r"AIWA", "Aiwa"), (r"VIVITAR", "Vivitar"), (r"WILSON", "Wilson"),
    (r"DOVE", "Dove"), (r"CREMO", "Cremo"), (r"OLD SPICE", "Old Spice"), (r"BODY GLOVE", "Body Glove"),
    (r"VOX POPULI", "Vox Populi"), (r"FUBU", "FUBU"), (r"PRPS", "PRPS"), (r"PREZZO", "Prezzo"),
]
_BRANDS = [(re.compile(rx), b) for rx, b in _BRANDS]
BAG_BRANDS = {"Ed Hardy", "Vera Bradley", "Luv Betsey", "Betsey Johnson", "Nanette Lepore", "Patricia Nash",
              "Kipling", "Samsonite", "Tumi", "JanSport", "Steve Madden", "DKNY", "Prezzo"}
# Fashion brand codes (confirmed by Person B, 2026-11-08). A line that starts with one is always
# clothing, shoes, bags, accessories or jewelry (or a fragrance when the line says so), never food
# or beauty: the AI once read "SM BGIGI CHOCOLATE" (a Steve Madden bag) as candy.
# Added 2026-11-09: UA, POLO/RL, KL, LB, NAUT, TB.
# Brand-only codes (fallback category None, also confirmed 2026-11-09): CK, MK, JC and AK sell
# bags, watches, jewelry AND fragrance or gift sets, so only the brand is set and the product
# words decide the category; they are never food, health, household, toys, tools, electronics,
# pet or sports ("MK VANILLA RETRO J" was read as a snack).
# first word -> (brand, fallback category, fallback type, sure); sure = a rule, no AI needed.
FASHION_CODES = {
    "SM": ("Steve Madden", "bags", None, False), "VB": ("Vera Bradley", "bags", None, False),
    "EH": ("Ed Hardy", "bags", None, False), "TR": ("True Religion", "clothing", None, False),
    "BJ": ("Betsey Johnson", "bags", None, False), "DKNY": ("DKNY", "bags", None, False),
    "PREZZO": ("Prezzo", "bags", "duffel", True),
    "UA": ("Under Armour", "clothing", None, False), "POLO": ("Polo Ralph Lauren", "clothing", None, False),
    "RL": ("Polo Ralph Lauren", "clothing", None, False), "KL": ("Karl Lagerfeld", "clothing", None, False),
    "LB": ("Luv Betsey", "bags", None, False), "NAUT": ("Nautica", "clothing", None, False),
    "TB": ("Ted Baker", "clothing", None, False),
    "CK": ("Calvin Klein", None, None, False), "CKJ": ("Calvin Klein", None, None, False),
    "MK": ("Michael Kors", None, None, False), "JC": ("Juicy Couture", None, None, False),
    "AK": ("Anne Klein", None, None, False),
}
NOT_FOR_BRAND_CODES = ("food", "health", "household", "toys", "tools", "electronics", "pet", "sports_outdoor",
                       "baby_gear")
_FRAGRANCE_WORDS = re.compile(r"\b(EDT|EDP|EDC|COLOGNE|PERFUME|PARFUM|SPRAY|BODY MIST|FRAG\w*)\b")


def fashion_code(u: str) -> tuple | None:
    """FASHION_CODES entry for a normalised line's first word (BJ2 counts as BJ)."""
    m = re.match(r"([A-Z]+)\d?\b", u)
    return FASHION_CODES.get(m.group(1)) if m else None


def fashion_ok(row: dict, desc: str, fc: tuple | list | None = None) -> bool:
    """A row's category is allowed for a fashion-code line (fc: its FASHION_CODES entry,
    found from desc when not given)."""
    fc = fc or fashion_code(normalise(desc)[0])
    if fc and fc[1] is None:      # brand only: anything but the non-fashion departments
        return row.get("category") not in NOT_FOR_BRAND_CODES
    return row.get("category") in WEARABLE or (
        row.get("category") == "fragrance" and bool(_FRAGRANCE_WORDS.search(normalise(desc)[0])))


def fashion_guard(row: dict, rules: dict | None, desc: str) -> dict:
    """Keep an AI row for a fashion-code line in a wearable category. Changes `row` in place."""
    fc = (rules or {}).get("fashion")
    if not fc:
        return row
    brand, cat, typ, _sure = fc
    if not row.get("brand_guess"):
        row["brand_guess"] = brand
    if not fashion_ok(row, desc, fc):
        if cat is None:           # brand only: no category to fall back on, so say unknown
            row.update(category="other", item_type=None, color=None,
                       confidence=min(row.get("confidence") or 0.3, 0.3))
        else:
            row.update(category=cat, item_type=typ, color=(rules or {}).get("color"),
                       confidence=min(row.get("confidence") or 0.5, 0.5))
        row["decode_raw"] = {**(row.get("decode_raw") or {}),
                             "fixed": "brand_code" if cat is None else "fashion_code"}
    return row
# How the register abbreviates a brand: /rcpt finds "ed hardy" as "EH ...".
BRAND_CODES = {
    "ed hardy": ("EH",), "true religion": ("TR",), "vera bradley": ("VB",), "luv betsey": ("LB", "LUV"),
    "betsey johnson": ("BJ",), "michael kors": ("MK",), "calvin klein": ("CK", "CKJ"), "karl lagerfeld": ("KL",),
    "under armour": ("UA",), "adidas": ("ADI",), "ralph lauren": ("RL", "POLO"), "polo ralph lauren": ("RL", "POLO"),
    "tommy hilfiger": ("TOMMY", "TH"), "nautica": ("NAUT",), "kate spade": ("KS", "KSPADE"),
    "steve madden": ("SM",), "ted baker": ("TB", "TBAKER"), "juicy couture": ("JC",), "patricia nash": ("PNASH",),
    "the north face": ("TNF",), "north face": ("TNF",), "kenneth cole": ("KC",), "anne klein": ("AK",),
    "hot wheels": ("HW",), "melissa & doug": ("M&D",), "quiksilver": ("QS",), "tahari": ("TAH",),
}

_COLOURS = {
    "black": r"BLK|BLACK|BLAC|JET BLK", "white": r"WHT|WHITE|WHTE|WHTT", "navy": r"NVY|NAVY",
    "grey": r"GRY|GREY|GRAY|CHAR|CHARCOAL|HTHR|HEATHER",
    "red": r"RED|BURG\w*|OXBLOOD|CHERRY|WINE", "blue": r"BLU|BLUE|DNM|DENIM|INDIG\w*|AQUA|TURQ\w*|TEAL",
    "green": r"GRN|GREEN|OLV|OLIVE|SAGE|CAMO", "pink": r"PNK|PINK|BLUSH|FUSCHIA|FUCHSIA|ROSE",
    "purple": r"PRPL|PURPLE|LAV|LAVENDER|LILAC|MAUVE|PURP",
    "brown": r"BRN|BROWN|BRWN|CHOC|MOCHA|COGNAC|COG|ESPRESSO|TAN|CAMEL|KHAKI",
    "beige": r"BEIGE|OAT|OATMILK|IVORY|IVRY|CREAM|BONE|SAND|NAT|NTRL|PUTTY",
    "silver": r"SIL|SILV|SILVER|SLVR", "gold": r"GLD|GOLD|GP", "orange": r"ORG|ORANGE|CORAL",
    "yellow": r"YLW|YELLOW",
    "multi": r"MULTI\w*|MULT1|AOP|A/O|LEOP\w*|LEOPARD|CHEETAH|ZEBRA|FLORAL|FLRL|PRINT|PRNT|TIE DYE",
}
_COLOURS = [(c, re.compile(r"(?<![A-Z])(" + rx + r")(?![A-Z])")) for c, rx in _COLOURS.items()]

_GENDER = [
    ("women", r"\bWMNS\b|\bW\b(?= NIKE)|\bWOMEN|\bLADIES\b|\bMISSES\b|\bWO\b"),
    ("men", r"\bMENS?\b|\bMN\b|\bMANKI"),
    ("boys", r"\bBOYS?\b"), ("girls", r"\bGIRLS?\b|\bGIRLZ\b"),
    ("baby", r"\bINF\b|\bINFANT|\bBABY\b|\bTODDLER\b|SWADDLE|BODYSU"),
    ("kids", r"\bKIDS?\b|\bYTH\b|\bYOUTH\b|\(GS\b|\bGS\b|\bJR\b|\bLITTLE KID"),
]
_GENDER = [(g, re.compile(rx)) for g, rx in _GENDER]


def _w(*words) -> str:
    return r"(?<![A-Z])(" + "|".join(words) + r")(?![A-Z])"


# (category, type, regex, tier): the first match wins
_RULES = [
    ("fragrance", "fragrance", _w(r"EDT", r"EDP", r"SPRAY", r"COLOGNE", r"PARFUM", r"EAU", r"AQUA DI GIO",
        r"ETERNITY", r"CK ONE", r"CK DEFY", r"AZZARO\b.*", r"LATTAFA\b.*", r"PARIS CORNER\b.*", r"DRAKKAR", r"BLOOM",
        r"BE DELICIOUS", r"ILLICIT", r"BRISA", r"SEDUCTIVE", r"LONDON ME\w*", r"CHROME", r"MOD", r"BELLAVITA",
        r"ELLIS BROOKLYN\b.*", r"MIST", r"COFFRET", r"VIVA LA JUIC\w*", r"POUR HOMME", r"ENERGIZE", r"DIESEL FUEL"),
     "high"),
    ("jewelry_watches", "watch", r"\d{2}MM\b|WATCH", "high"),
    ("jewelry_watches", "bracelet", _w(r"BRAC\w*"), "medium"),
    ("jewelry_watches", "earrings", _w(r"EARRING\w*", r"STUD"), "medium"),
    ("jewelry_watches", "necklace", _w(r"NECKLACE", r"PND", r"PENDANT", r"FIGARO", r"CUBAN", r"CHN", r"CHAIN"),
     "medium"),
    ("jewelry_watches", "ring", _w(r"RING"), "medium"),
    ("jewelry_watches", None, _w(r"CZ", r"PAVE", r"CRSS", r"HRT BX"), "medium"),
    ("accessories", "sunglasses", _w(r"LENS\w*", r"MIRR", r"POLARIZED", r"GRADIE\w*", r"SMOKE", r"SUNGLASS\w*",
        r"EYEWEAR"), "high"),
    ("shoes", "sneakers", _w(r"RUN", r"RUNNING", r"RUNNER", r"COURT", r"BASKETBALL", r"BSKT", r"SNEAKER\w*",
        r"AIR MAX", r"AF1", r"TRAINER", r"JORDANS", r"SAMBAE?", r"GAZELLE", r"SUPERSTAR", r"CAMPUS", r"SL 72",
        r"S 72", r"SHOX", r"GATO", r"JANOSKI", r"OLD SKOOL", r"RUN 60'?S", r"HOOPS", r"FASTBREAK", r"MEGARIDE",
        r"ADIZERO", r"ADISTAR", r"PEGA", r"KILLSHOT", r"CHUCK TAYLOR", r"CLUB C", r"METCON", r"ZOOM FLY",
        r"CHARGED ASSERT", r"HOVR", r"FLEX RUNNER", r"ARUKU", r"GIANNIS", r"LEBR", r"FIELD GENERA\w*",
        r"FREE 2005"), "high"),
    ("shoes", "cleats", _w(r"CLEATS?"), "high"),
    ("shoes", None, _w(r"SHOES?", r"FOOTWEAR"), "medium"),
    ("clothing", "jeans", r"^TR\b.*\b(RICKY|ROCCO|GENO|JESSE|BOBBY|BILLY|JOEY)\b|\bSN (FLP|NF|AOP)\b", "high"),
    ("bags", None, r"^TOMMY-[A-Z\u0100-\u017f]+ (II|TI)\b", "high"),
    ("shoes", "sandals", _w(r"SANDALS?", r"FLIP FLOPS?"), "medium"),
    ("shoes", "slides", _w(r"SLIDES?"), "medium"),
    ("shoes", None, _w(r"TOE", r"TTOE", r"LUG", r"GUM BTM", r"MJ FLAT", r"SLIP"), "medium"),
    ("shoes", "boots", _w(r"BOOTS?", r"COMBAT", r"1460"), "medium"),
    ("shoes", "flats", _w(r"BALLET", r"OXFORDS?", r"ESPADR\w*", r"SLINGBACK", r"MULE", r"PLATFORM"), "medium"),
    ("shoes", "loafers", _w(r"LOAFERS?"), "medium"),
    ("shoes", "clogs", _w(r"CLOGS?"), "medium"),
    ("bags", "luggage", _w(r"SAMSONITE\b.*", r"TRAVEL PRO\b.*", r"TUMI\b.*", r"LUGGAGE", r"SPINNER", r"CARRY ?ON",
        r"TRAVELPACK"), "high"),
    ("bags", "duffel", _w(r"WEEKEN\w*", r"DUFF\w*"), "high"),
    ("bags", "backpack", _w(r"BACKPA\w*", r"BACK PACK", r"BPK", r"BKPK", r"BACKP"), "high"),
    ("bags", "crossbody", _w(r"CROSSBODY", r"CRSS BDY", r"XBODY", r"XB", r"CROSSBO\w*", r"SLING", r"CAMERA"), "high"),
    ("bags", "tote", _w(r"TOTE", r"TOT", r"SHOPPER"), "high"),
    ("bags", "shoulder", _w(r"SHOULDER", r"HOBO"), "high"),
    ("bags", "satchel", _w(r"SATCHEL", r"SATCH", r"BARREL", r"DOME", r"HANDBAG", r"PURSE"), "high"),
    ("bags", "wallet", _w(r"WALLET", r"BIFOLD", r"BIFLD", r"BI-FOLD", r"MONEY", r"WLT"), "medium"),
    ("bags", "card_case", _w(r"CARD CASE", r"CARD"), "medium"),
    ("bags", "wristlet", _w(r"WRISTLET", r"WRST\w*"), "medium"),
    ("bags", "clutch", _w(r"CLUTCH"), "medium"),
    ("bags", "belt_bag", _w(r"FANNY PACK", r"BELT BAG", r"WAIST"), "high"),
    ("bags", None, _w(r"BAGS?", r"SM BAG", r"SMALL BAGS?"), "medium"),
    ("bags", "messenger", _w(r"MESSENG\w*"), "medium"),
    ("bags", "pouch", _w(r"POUCH"), "medium"),
    ("clothing", "socks", _w(r"SOCKS?", r"QTR", r"CREW SOCK", r"NO SHOW", r"NONTHONG", r"1/2 TER\w*"), "high"),
    ("clothing", "underwear", _w(r"BRIEFS?", r"BOXERS?", r"BB", r"BIKINI\w*", r"THONG", r"CTN/SPAN", r"CTN SPAN",
        r"PANTY", r"BRIE"), "high"),
    ("clothing", "bra", r"(?<![A-Z])BRA(?= |$)(?!.*SPONGE)", "medium"),
    ("clothing", "jersey", _w(r"JERSEY", r"HOME CONT\w*", r"AWAY CONT\w*", r"HOME SUBLI", r"GAMEDAY", r"AWAY",
        r"HOME", r"CONTR"), "medium"),
    ("clothing", "hoodie", _w(r"HOODIE", r"HOODE", r"HOO", r"HOOD", r"FLC HD(?= |$)"), "high"),
    ("clothing", "fleece", _w(r"FLEECE", r"FLEEC", r"FLEE", r"FLC", r"SWEATSHIRT", r"CREW FLEE"), "high"),
    ("clothing", "jacket", _w(r"JKTS?", r"JACKET", r"PUFFER", r"WINDBREAK\w*", r"COAT", r"3-IN-1"), "high"),
    ("clothing", "vest", _w(r"VESTS?"), "high"),
    ("clothing", "polo", _w(r"POLO", r"PIQUE", r"PIQ", r"KNITPOLO"), "medium"),
    ("clothing", "tank", _w(r"TANK"), "medium"),
    ("clothing", "tee", _w(r"TEES?", r"T-SHIRT", r"SS", r"S/S", r"L/S", r"LS", r"TOP", r"TOPS", r"HENL\w*",
        r"CREW", r"TECH TO"), "medium"),
    ("clothing", "shirt", _w(r"BUTTON", r"OXFORD SHIRT", r"WOVEN", r"WV", r"FLANNEL", r"CAMP", r"POPLIN",
        r"CHAMBRAY\w*", r"SHIRTS?"), "medium"),
    ("clothing", "jeans", _w(r"JEANS?", r"DENIM", r"WASH", r"WSH", r"STRAIGHT", r"SKINNY", r"INSEAM"), "medium"),
    ("clothing", "tee", r"SHORT SLE", "medium"),
    ("clothing", "skirt", r"SHORT SK", "medium"),
    ("clothing", "shorts", _w(r"SHORTS?", r"JORTS?", r"CARGO SHORT", r"BIKE SHORT"), "high"),
    ("clothing", "joggers", _w(r"JOGGERS?", r"TRACK PAN\w*"), "medium"),
    ("clothing", "leggings", _w(r"LEGGINGS?", r"LEGGIN"), "medium"),
    ("clothing", "pants", _w(r"PANTS?", r"CARGO", r"TWILL", r"BOTTOMS?", r"CHINO"), "medium"),
    ("clothing", "skirt", _w(r"SKIRT", r"SKORT"), "high"),
    ("clothing", "dress", _w(r"DRESS(ES)?", r"MAXI", r"MIDI", r"ROMPER", r"JUMPSUIT"), "high"),
    ("clothing", "sweater", _w(r"SWEATER", r"CARDIGAN", r"KNIT"), "medium"),
    ("clothing", "swim", _w(r"SWIM", r"TRUNKS?", r"RASHGUARD"), "medium"),
    ("clothing", "top", _w(r"SQ NK", r"SQNK", r"V NK", r"CORSE\w*", r"BODICE", r"BUSTIER", r"HALTER", r"RUFFL\w*",
        r"CROP", r"CAMI", r"BLOUSE", r"TIER", r"SLVLS", r"SLVS"), "medium"),
    ("clothing", None, r"^(ADIDAS|ADI|PUMA|RL|POLO|DKNY|TIMBERLAND|DANSKIN|SKECHERS|FIFA|AERO|RVCA|UA|JORDAN|NIKE"
        r"|REEBOK|CK|TOMMY|TAHARI|LEVIS|VOLCOM|ELEMENT|ABERCROMBIE|BEBE|LA|BJ|EMJ|ED HARDY|SOUTHPOLE|FOT|ARIAT"
        r"|FOX RIVER|ETHIKA|NF|PD|TF|TS)\b.*\b\d+ ?(PK|PACK)\b|^\d+ ?(PK|PACK)\b.*\b(BB|BRIEF|CREW|QTR|TOE|LOW"
        r"|NO SHOW)\b", "medium"),
    ("clothing", "jersey", _w(r"MEXICO", r"ARGENTINA", r"PORTUGAL", r"COLOMBIA", r"CROATIA", r"URUGUAY", r"JAPAN",
        r"GERMANY", r"CLUB AMERICA", r"MANCHESTER CITY", r"LIVERPOOL", r"CHELSEA", r"LAFC", r"ATLANTA UNITED",
        r"SAN DIEGO FC", r"FCB", r"MLS", r"USMNT", r"MESSI", r"BRAZ\w*", r"ITALY"), "low"),
    ("clothing", "baby", _w(r"INF", r"INFANT", r"BODYSU\w*", r"ONESIE", r"TODDLER"), "medium"),
    ("clothing", "set", r"^\d?PC\b|^\dPC|\b2PC\b|\b3PC\b", "low"),
    ("accessories", "belt", _w(r"BELT"), "high"),
    ("accessories", "hat", _w(r"HATS?", r"CAP", r"BEANIE", r"BUCKET", r"SNAPBACK", r"VISOR", r"HEADBAND"), "medium"),
    ("accessories", "hair", r"^\d+/CD\b|\bBOWS?\b|CLIPS?\b|SCRUNCH|HAIR TIE", "medium"),
    ("accessories", "scarf", r"(?<!BODY )SCARF", "medium"),
    ("accessories", "gloves", r"(?<!BODY )(GLOVES?|MITTENS?|THINSULATE)", "medium"),
    ("accessories", "keychain", _w(r"KEYCHAIN", r"KEY CHAIN"), "high"),
    ("beauty", "nails", _w(r"MANI\w*", r"MANICURE", r"NAIL", r"TWEEZE\w*", r"FOOT FILE", r"PEDI\w*"), "high"),
    ("beauty", "skincare", _w(r"SERUM", r"MOIST\w*", r"CLEANS\w*", r"RETINOL", r"HYAL\w*", r"COLL", r"SPF",
        r"LACTIC", r"FACIAL", r"EYE PATCH", r"TONER", r"MASK", r"CRM", r"LOTION", r"FOAMING", r"FOA", r"MTHD", r"ACN",
        r"SKINCARE", r"SKINFIX", r"BHA|AHA", r"CICA", r"BALM"), "medium"),
    ("beauty", "bath_body", _w(r"BODY WASH", r"BW", r"BDY", r"SHOWER", r"SHW", r"BATH", r"BTH", r"SOAP", r"SCRUB",
        r"BBL"), "medium"),
    ("beauty", "hair_care", _w(r"SHAMPOO", r"SP", r"CONDITION\w*", r"BIOTIN", r"KERATIN", r"HAIR", r"DRY SCA\w*",
        r"H&S", r"PANTENE"), "medium"),
    ("beauty", "grooming", _w(r"RAZOR", r"SHAVE", r"SHAVIN\w*", r"CLIPPER", r"TRIMMER", r"DEO", r"DEGRE\w*",
        r"BEARD", r"JUST FOR MEN", r"BLADE"), "medium"),
    ("beauty", "oral_care", _w(r"FLOSS", r"TOOTH\w*", r"COLGATE", r"PEPSODENT", r"ORAL", r"TONGUE"), "high"),
    ("health", "supplement", _w(r"\d+CT", r"GUMM\w*", r"GMY", r"VITAMIN\w*", r"VIT\w*", r"MELATONIN", r"PROBIOTIC",
        r"BIOTIC", r"CPSL", r"OMEGA", r"MULT", r"EMERGEN\w*", r"ZARBEE'S", r"IMM\+?", r"GUT"), "low"),
    ("food", "candy", _w(r"CHOC\w*", r"CANDY", r"GUMMI\w*", r"GUMMY", r"HARIBO", r"PRALI\w*", r"PEPPERMINT",
        r"TRUFFLE\w*", r"BLOW POP", r"PEZ", r"KINDER\w*", r"NUTELLA"), "medium"),
    ("food", "snack", _w(r"COOKIE\w*", r"WAFER\w*", r"PRETZEL\w*", r"POPCORN", r"SNACK", r"BAR", r"NUTS?",
        r"SEEDS", r"GINGERSNAP", r"PANET\w*", r"FRIES"), "medium"),
    ("food", "drink", _w(r"WATER", r"JUICE", r"SODA", r"SPARK\w*"), "medium"),
    ("food", "coffee_tea", _w(r"COFFEE", r"CAFE", r"TEA"), "medium"),
    ("food", "pantry", _w(r"EVOO", r"OLIVE OIL", r"JAM", r"HONEY", r"PASTA", r"ANGEL HAIR", r"SAUCE", r"MARG\w*",
        r"BOYSEN\w*", r"YOGURT", r"MIX(ED)?", r"BRUSC\w*", r"FIG", r"CHILI", r"MANGO"), "medium"),
    ("food", None, r"\b\d+(\.\d+)?\s?(OZ|G)\b", "low"),
    ("household", "batteries", _w(r"AAA?", r"BATTER\w*", r"LITHIU\w*", r"LITH"), "high"),
    ("household", "baby_consumable", _w(r"DIAPER\w*", r"HUGGIES", r"PAMPERS", r"SWADDLERS", r"WIPES"), "high"),
    ("household", "cleaning", _w(r"DETERGENT", r"CLEANER", r"SPONGE", r"PAPER TOWEL", r"TRASH", r"DISH", r"DSHP",
        r"LIQUID H\w*"), "medium"),
    ("electronics", "audio", _w(r"SPKR", r"SPEAKER", r"TWS", r"EARBUD\w*", r"HEADPHONE\w*", r"HEA",
        r"SOUNDCORE\b.*", r"JBL\b.*", r"BLUETOOTH"), "medium"),
    ("electronics", "charger", _w(r"QI", r"CHARG\w*", r"CHRG", r"CABLE", r"USB", r"TYPE C", r"MAH", r"WIRELESS"),
     "medium"),
    ("electronics", None, _w(r"LED", r"RCA\b.*", r"SMART HOME", r"ALARM", r"SHIATSU", r"MASSAG\w*"), "medium"),
    ("tools", "power_tool", _w(r"DRILL\w*", r"DRIL", r"20V", r"CORDLESS", r"RECIPROCATING"), "medium"),
    ("tools", "light", _w(r"FLASHLI\w*", r"FLASH", r"LUMEN"), "medium"),
    ("tools", "hand_tool", _w(r"WRENCH", r"PLIERS?", r"SCREWDRIVER", r"SOCKET", r"HAMMER", r"MAGNETIC", r"UTILITY",
        r"KNIFE", r"KNI", r"TOOL\w*", r"MECHANICS", r"WELD", r"REACH\w*"), "medium"),
    ("toys", "building", _w(r"LEGO", r"DUPLO", r"BLOCKS?", r"BUILDS?", r"TILE TOWN"), "medium"),
    ("toys", "vehicle", _w(r"DIE ?CAST", r"PULL ?BACK", r"R/C", r"TRUCK", r"VEHI\w*", r"MONSTER TRUCK",
        r"HOT WHEELS", r"HW", r"TEAMSTERZ"), "medium"),
    ("toys", "puzzle", _w(r"PUZZ\w*"), "medium"),
    ("toys", "plush", _w(r"PLUSH"), "medium"),
    ("toys", "game", _w(r"GAME", r"BATTLESHIP", r"BANANAGRAM\w*"), "medium"),
    ("toys", "arts_crafts", _w(r"STICKE\w*", r"SCRATCH", r"COLORING", r"DOT-TO-DOT"), "medium"),
    ("toys", None, _w(r"PLAYSET", r"NERF", r"TOY\w*", r"ROBO\w*", r"DINO\w*", r"M&D", r"EVERPLAY", r"KIDS@WORK",
        r"PLAYGO", r"FIGURE", r"FEATURE", r"LAUNCHER"), "medium"),
    ("sports_outdoor", None, _w(r"BALL", r"BALLS", r"PICKLEB\w*", r"GOLF", r"SOCCER", r"FOOTBALL", r"HUNTING",
        r"FISHING", r"CAMPING", r"YOGA", r"WEIGHT\w*", r"SKATEB\w*", r"BIKE", r"TACTICAL"), "low"),
    ("home", "bedding", _w(r"PILLOW", r"THROW", r"QUILT", r"BLANKET", r"SHEETS?", r"COMFORTER", r"DUVET",
        r"BEDDING"), "low"),
    ("home", "rug", _w(r"RUG", r"MAT", r"DOORMAT"), "low"),
    ("home", "kitchen", _w(r"PLATE", r"BOWL", r"MUG", r"GLASS", r"CUP", r"TUMBLER", r"POT", r"PAN", r"SKILLET",
        r"CAST ALUM", r"KTCHN", r"KITCHEN", r"MICROWAVE"), "low"),
    ("home", "storage", _w(r"STORAGE", r"BASKET\w*", r"BOX"), "low"),
    ("home", "seasonal", _w(r"GARLAND", r"ORNAMENT", r"WREATH"), "low"),
    ("home", "decor", _w(r"CURTAIN", r"FRAME", r"CANDLE", r"VASE", r"PLAQU\w*", r"WALLPAPE\w*", r"S/\d+", r"DECOR",
        r"FLOWER", r"BOUQUET", r"\d+'X\d+'", r"\d+X\d+"), "low"),
    ("home", "bath", _w(r"TOWEL\w*"), "low"),
    ("pet", None, _w(r"DOG", r"CAT TOY", r"SQUEAKER", r"PET"), "medium"),
]
_RULES = [(c, t, re.compile(rx), tier) for c, t, rx, tier in _RULES]
_BELT_BRANDS = {"True Religion", "Ed Hardy", "Nike", "Wrangler", "Levi's"}


def colours_in(text: str) -> str | None:
    """Up to two colours from the vocabulary, joined by '/': 'PNK/BLK' -> 'pink/black'."""
    u = (text or "").upper()
    found = [c for c, rx in _COLOURS if rx.search(u)]
    return "/".join(found[:2]) or None


def brand_in(text: str) -> str | None:
    u = (text or "").upper()
    return next((b for rx, b in _BRANDS if rx.search(u)), None)


def _first_rule(u: str):
    for c, t, rx, tier in _RULES:
        if rx.search(u):
            return c, t, tier
    return None


def decode_rules(desc: str) -> dict:
    """The rules' reading of one description: category, item_type, gender,
    color, brand, tier ("high" | "medium" | "low" | None) and the notes."""
    u, markers, notes = normalise(desc)
    out = {"category": None, "item_type": None, "gender": None, "color": None, "brand": None,
           "product_model": None, "tier": None, "markers": markers, "notes": notes}
    if letters(u) < 3:
        out["notes"] = notes + ["too_short"]
        return out
    out["brand"] = brand_in(u)
    out["gender"] = next((g for g, rx in _GENDER if rx.search(u)), None)
    out["color"] = colours_in(u)

    if "noex" in markers or "otc" in markers:
        prior = ("health", "otc") if "otc" in markers else ("beauty", None)
    elif any(m.startswith("season:H") for m in markers):
        prior = ("beauty", "gift_set")
    elif "dated" in markers:
        if re.search(r"BADER|KIEH|SHISEIDO|SULWHA|INNISFREE|IF |RETINOL|STUDIO TONE|TANOLOGIST|FACIAL|SPF|SERUM"
                     r"|CREAM|CRM|LOT", u):
            prior = ("beauty", None)
        elif re.search(r"\d+ ?CT|VIT|MELATONIN|PROBIOTIC|GMY|GUMM|ZARBEE|EMERGEN|CPSL|EMC|RENEW LIFE|AKKERM", u):
            prior = ("health", "supplement")
        else:
            prior = ("food", None)
    else:
        prior = None

    hit = _first_rule(u)
    brand = out["brand"]
    if "hash" in markers:  # the shoe AND handbag department
        if hit and hit[0] == "bags":
            pass
        elif brand in BAG_BRANDS:
            hit = ("bags", None, "medium")
        else:
            shoe_type = hit[1] if hit and hit[0] == "shoes" else (
                "sneakers" if re.search(r"ASST|NIKE|ADI|JORDAN|UA\b|PUMA|REEBOK", u) else None)
            hit = ("shoes", shoe_type, "high" if hit and hit[0] == "shoes" else "medium")
    if prior and (not hit or hit[2] == "low" or hit[0] in ("food", "health", "home", "toys", "tools")):
        if prior[0] == "beauty" and hit and hit[0] == "beauty":
            pass
        elif prior[0] == "food" and hit and hit[0] in ("health", "beauty", "food", "household"):
            pass
        else:
            hit = (prior[0], prior[1] if not (hit and hit[0] == prior[0]) else hit[1], "medium")
    if (not hit or hit[2] == "low") and brand in BAG_BRANDS:
        hit = ("bags", None, "low")
    # a belt brand with a waist size: TR 38 BLK
    if re.search(r"(?<!\d)(3[0-9]|4[0-4])(?!\d|\.|MM|OZ)", u) and brand in _BELT_BRANDS and \
            not (hit and hit[0] == "clothing" and hit[1] in ("jeans", "shorts", "pants")):
        if not hit or (hit[0] in ("bags", "clothing", "beauty") and hit[2] != "high"):
            hit = ("accessories", "belt", "medium")
    # women's denim by waist-inseam: 27-16, 29 -13
    if re.match(r"^(SZ\s*)?\d{1,2}(\.\d+)?\s*-\s*\d{1,2}", u) or \
            re.search(r"\b\d{2}[- ]\d{2}(\.\d+)?\b.*\b(WASH|WSH|BLK|BLUE|DENIM)\b", u):
        hit = ("clothing", "jeans", "medium")
    if hit and hit[0] == "clothing" and hit[1] == "jeans" and brand in BAG_BRANDS and \
            not re.search(r"JEANS?|SHORTS?|WASH|WSH", u):
        hit = ("bags", None, "low")
    fc = fashion_code(u)
    if fc:
        out["brand"], out["fashion"] = fc[0], list(fc)
        if fc[1] is None:         # brand only: drop a non-fashion guess, let the AI read the words
            if hit and hit[0] in NOT_FOR_BRAND_CODES:
                hit = None
        elif fc[3]:
            hit = (fc[1], hit[1] if hit and hit[0] == fc[1] and hit[1] else fc[2], "high")
        # licensed beauty is real (EH MANI SET, EH 150 CT FLOSS, DKNY BE DELICIOUS): a sure beauty or
        # fragrance rule stays; food, home and anything unsure becomes the brand's fashion category
        elif not hit or (hit[0] not in WEARABLE and not (hit[0] in ("beauty", "fragrance") and hit[2] == "high")
                         and not (hit[0] == "fragrance" and _FRAGRANCE_WORDS.search(u))):
            hit = (fc[1], fc[2], "low")
    if hit:
        out["category"], out["item_type"], out["tier"] = hit
    if out["category"] not in WEARABLE:
        out["color"] = None
    if out["gender"] is None and "n!" in markers:
        out["gender"] = "men"
    return out


def rules_fields(r: dict) -> dict:
    """A rules reading as a receipt_line_details row (without desc_key)."""
    return {"category": r["category"], "item_type": r["item_type"], "gender": r["gender"], "color": r["color"],
            "brand_guess": r["brand"], "product_model": r.get("product_model"),
            "confidence": TIER_CONFIDENCE.get(r["tier"]), "source": "rules", "model": RULES_VERSION,
            "item_id": None,
            "decode_raw": {k: r[k] for k in ("category", "item_type", "gender", "color", "brand", "tier",
                                             "markers", "notes")}}


# --- our own items ----------------------------------------------------------------------------

_DEPARTMENT = {"women": "women", "womens": "women", "ladies": "women", "men": "men", "mens": "men",
               "unisex": "unisex", "boys": "boys", "girls": "girls", "kids": "kids", "baby": "baby"}


def item_fields(ident: dict, item_id: str | None, rules: dict | None = None) -> dict:
    """A matched item's identity in the table's terms. Its free-text
    item_type ("Weekender Duffel Bag") goes through the same keyword rules;
    gender falls back to the description's rules ((GS) -> kids)."""
    ident = ident or {}
    text = " ".join(str(ident.get(k) or "") for k in ("item_type", "product_name", "title")).upper()
    hit = _first_rule(str(ident.get("item_type") or "").upper()) or _first_rule(text)
    category, item_type = (hit[0], hit[1]) if hit else (None, None)
    gender = None
    for k in ("gender", "department"):
        g = re.sub(r"[^a-z]", "", str(ident.get(k) or "").lower())
        if g in _DEPARTMENT:
            gender = _DEPARTMENT[g]
            break
    if gender is None and rules:
        gender = rules.get("gender")
    colour = colours_in(str(ident.get("color") or "")) if category in WEARABLE or category is None else None
    model = ident.get("style_code") or ident.get("model")
    return {"category": category, "item_type": item_type, "gender": gender, "color": colour,
            "brand_guess": (str(ident.get("brand")).strip()[:100] or None) if ident.get("brand") else None,
            "product_model": str(model).strip()[:100] if model else None,
            "confidence": 1.0 if category else 0.8, "source": "matched_item", "model": None,
            "item_id": item_id,
            "decode_raw": {"item_id": item_id, "item_type": ident.get("item_type"), "brand": ident.get("brand"),
                           "color": ident.get("color"), "model": model}}


# --- AI ---------------------------------------------------------------------------------------

_SYSTEM = """You decode lines from Ross Dress for Less store receipts. Each line is the register's
description, cut at about 18 characters, often abbreviated or misread by OCR. Say what product it is.

Markers: '#' = shoe and handbag department (a bag brand or bag word means a bag); NOEX/NDEX = prestige
beauty; OTC = over-the-counter health; a date prefix (01/26, 08/09/26) = dated consumable (beauty,
supplement or food); a season code (H25, SP26) = holiday gift set; 'n!' = men's belt or wallet;
'S/3' = set of 3 (home); oz/ml/CT/PK = consumable or multipack; 'X ', '6.19 ', 'T-' prefixes carry
no meaning.
Brand codes: EH = Ed Hardy (mostly bags), TR = True Religion (jeans RICKY/ROCCO/GENO; 'TR 38' is a
belt), VB = Vera Bradley (QLT = quilted), LB/LUV = Luv Betsey, BJ = Betsey Johnson, MK = Michael Kors
(MK 40MM = watch), CK/CKJ = Calvin Klein, KL = Karl Lagerfeld, UA = Under Armour, ADI = adidas,
RL/POLO = Polo Ralph Lauren, TH/TOMMY = Tommy Hilfiger (TOMMY-<NAME> II = bag), NAUT = Nautica,
KS = Kate Spade, SM = Steve Madden, TB = Ted Baker, JC = Juicy Couture, TNF = The North Face,
HW = Hot Wheels, Prezzo = bags (duffels). A line starting SM, VB, EH, TR, BJ, DKNY, Prezzo, UA, POLO,
RL, KL, LB, NAUT or TB is always clothing, shoes, bags, accessories or jewelry: "SM BGIGI CHOCOLATE" is a
chocolate-brown bag, not candy. CK, MK, JC and AK lines are fashion, jewelry, fragrance or beauty (the
words decide), never food: "MK VANILLA RETRO J" is not a snack. OCR often reads EH as cH, CH or ER. FLP = flap pocket, SN = straight, AOP = all-over print, SS/LS = short/long sleeve,
(GS) = grade school (kids), WMNS = women's.
Colours: BLK black, WHT white, GRY grey, NVY navy, PNK pink, BRN brown, OLV olive (green), SIL silver.

Answer every line. Use "none" for a type or gender you can't tell, "" for an unknown colour, brand or
model. cat must be one of the categories; type should fit the category. Give a type only when the
words show it (BKPK, TOTE, DUFFEL, WALLET, SS = tee, JOGGER): an Ed Hardy line with only colours and a
print is a bag of type "none", not a tote. Don't guess a type from the brand or the price. color: up to two of
black, white, grey, navy, blue, red, pink, purple, green, brown, beige, gold, silver, orange, yellow,
multi, joined by '/', only for clothing, shoes, bags, accessories and jewelry. model: a product model
or style name if the line shows one (JORDAN MVP 92 -> "MVP 92"), else "". conf: 0 to 1, how sure you
are of cat and type. The price helps: $0.99 is rarely a bag. A rules guess, when given, may be wrong.

Examples:
"#ASST RUN/COURT" $24.99 -> shoes, sneakers
"TR ROCCO SN FLP" $29.99 -> clothing, jeans, men, True Religion, model Rocco
"NOEX Drunk Elephan" $19.99 -> beauty, skincare, Drunk Elephant
"n! TR 38 blk" $14.99 -> accessories, belt, men, black, True Religion
"LS PNK GRINCH RFFL" $9.99 -> clothing, top, pink
"01/15/26 20CT GMY" $5.99 -> health, supplement
"X HW 5PK" $4.99 -> toys, vehicle, Hot Wheels
"VB QLT BKPK" $34.99 -> bags, backpack, Vera Bradley
"EH PNK LEOP SKULL" $16.99 -> bags, none, Ed Hardy, pink/multi
"S/3 GLASS VASE" $12.99 -> home, decor
"MK 40MM GLD" $59.99 -> jewelry_watches, watch, gold, Michael Kors"""


def _ai_schema() -> dict:
    return {"type": "object", "additionalProperties": False, "required": ["lines"],
            "properties": {"lines": {"type": "array", "items": {
                "type": "object", "additionalProperties": False,
                "required": ["n", "cat", "type", "gender", "color", "brand", "model", "conf"],
                "properties": {
                    "n": {"type": "integer"},
                    "cat": {"type": "string", "enum": list(CATEGORIES)},
                    "type": {"type": "string", "enum": list(ALL_TYPES) + ["none"]},
                    "gender": {"type": "string", "enum": list(GENDERS) + ["none"]},
                    "color": {"type": "string"},
                    "brand": {"type": "string"},
                    "model": {"type": "string"},
                    "conf": {"type": "number"}}}}}}


def price_hint(prices) -> str:
    """'$4.99–$9.99' from a description's line prices, leaving out misread
    ones (missing, over $500)."""
    ok = sorted({round(float(p), 2) for p in prices or () if p is not None and 0 < float(p) <= 500})
    if not ok:
        return "?"
    return f"${ok[0]:.2f}" if ok[0] == ok[-1] else f"${ok[0]:.2f}–${ok[-1]:.2f}"


def _hint(r: dict | None) -> str:
    if not r or not r.get("category"):
        return "-"
    return "/".join(str(x) for x in (r["category"], r.get("item_type"), r.get("brand")) if x)


def _norm_colour(text: str, category: str) -> str | None:
    if category not in WEARABLE or not text:
        return None
    words = [w.strip().lower() for w in re.split(r"[/,&]| and ", text) if w.strip()]
    out = []
    for w in words:
        c = w if w in COLOURS else (colours_in(w) or "").split("/")[0] or None
        if c and c not in out:
            out.append(c)
    return "/".join(out[:2]) or None


def ai_fields(answer: dict, model: str, *, merged: bool = False) -> dict:
    """One AI answer, normalised to the vocabularies, as a row."""
    cat = answer.get("cat") if answer.get("cat") in CATEGORIES else "other"
    typ = answer.get("type") if answer.get("type") in TYPES.get(cat, ()) else None
    gender = answer.get("gender") if answer.get("gender") in GENDERS else None
    try:
        conf = max(0.0, min(1.0, float(answer.get("conf"))))
    except (TypeError, ValueError):
        conf = 0.5
    if merged:           # two products in one text: never sure
        conf = min(conf, 0.5)
    brand = str(answer.get("brand") or "").strip()[:100] or None
    pmodel = str(answer.get("model") or "").strip()[:100] or None
    return {"category": cat, "item_type": typ, "gender": gender,
            "color": _norm_colour(str(answer.get("color") or ""), cat), "brand_guess": brand,
            "product_model": pmodel, "confidence": round(conf, 2), "source": "ai", "model": model,
            "item_id": None, "decode_raw": dict(answer)}


def ai_decode(batch: list[dict], ask=None, meta: dict | None = None) -> dict:
    """{desc_key: row} for one batch of {"key", "desc", "prices", "rules"}
    (rules: a decode_rules result as the hint) from one cheap Claude call."""
    import claude_llm
    ask = ask or claude_llm.ask
    lines = "\n".join(f'{n} | "{b["desc"]}" | {price_hint(b.get("prices"))} | rules: {_hint(b.get("rules"))}'
                      for n, b in enumerate(batch, 1))
    prompt = f"Decode these {len(batch)} receipt lines (n | description | price | rules guess):\n{lines}"
    info = {} if meta is None else meta
    out = ask(AI_STAGE, prompt, system=_SYSTEM, model=config.RECEIPT_DECODE_MODEL or config.CLAUDE_CHEAP_MODEL,
              effort="low", max_tokens=config.RECEIPT_DECODE_MAX_TOKENS, output_schema=_ai_schema(), meta=info)
    model = info.get("model") or config.RECEIPT_DECODE_MODEL or config.CLAUDE_CHEAP_MODEL
    found = {}
    for a in (out or {}).get("lines") or []:
        n = a.get("n")
        if isinstance(n, int) and 1 <= n <= len(batch) and batch[n - 1]["key"] not in found:
            b = batch[n - 1]
            row = ai_fields(a, model, merged=is_merged(b["desc"]))
            # the brand codes are reliable (EH, TR, VB...): keep the rules' brand when the AI gave none
            if not row["brand_guess"] and (b.get("rules") or {}).get("brand"):
                row["brand_guess"] = b["rules"]["brand"]
            fashion_guard(row, b.get("rules"), b["desc"])
            found[b["key"]] = row
    return found


def ai_decode_all(pending: list[dict], *, max_usd: float, ask=None, batch_size: int | None = None,
                  log=print) -> tuple[dict, dict]:
    """Decode `pending` in batches until done or the spend reaches max_usd.
    Lines the AI skipped are retried once in a batch of their own. Returns
    ({desc_key: row}, {"cost_usd", "calls", "input_tokens", "output_tokens",
    "model", "stopped", "left"})."""
    size = batch_size or config.RECEIPT_DECODE_BATCH
    done, spent = {}, {"cost_usd": 0.0, "calls": 0, "input_tokens": 0, "output_tokens": 0, "model": None,
                       "stopped": None, "left": 0}
    queue = [pending[i:i + size] for i in range(0, len(pending), size)]
    retried = set()
    largest = 0.0
    while queue:
        batch = queue.pop(0)
        if spent["cost_usd"] + largest > max_usd:
            spent["stopped"] = f"spend cap ${max_usd:.2f} reached"
            queue.insert(0, batch)
            break
        meta = {}
        try:
            got = ai_decode(batch, ask=ask, meta=meta)
        finally:
            cost = float(meta.get("cost_usd") or 0.0)
            spent["cost_usd"] += cost
            spent["calls"] += 1
            largest = max(largest, cost)
            for k in ("input_tokens", "output_tokens"):
                spent[k] += int(meta.get(k) or 0)
            spent["model"] = meta.get("model") or spent["model"]
        done.update(got)
        missing = [b for b in batch if b["key"] not in got and b["key"] not in retried]
        if missing:
            retried |= {b["key"] for b in missing}
            queue.append(missing)
        log(f"  AI batch {spent['calls']}: {len(got)}/{len(batch)} decoded, ${spent['cost_usd']:.4f} so far")
    spent["left"] = sum(len(b) for b in queue)
    return done, spent


# --- which description gets which decode -------------------------------------------------------

def _row_differs(old: dict | None, new: dict) -> bool:
    if not old:
        return True
    keys = ("category", "item_type", "gender", "color", "brand_guess", "product_model", "source", "model",
            "item_id")
    if any((old.get(k) or None) != (new.get(k) or None) for k in keys):
        return True
    oc, nc = old.get("confidence"), new.get("confidence")
    return (oc is None) != (nc is None) or (oc is not None and abs(float(oc) - float(nc)) > 0.005)


def plan(lines: list[dict], details: dict, hits: dict, *, redo_ai: bool = False) -> dict:
    """Sort every description into what to do. Free: no AI here.
    lines: receipt lines (desc_key, description, sku, price, ...);
    details: {desc_key: existing row}; hits: owned.ross_hits() of the SKUs.
    Returns {"write": {key: row} (matched_item and rules rows that are new or
    changed), "ai": [{"key", "desc", "prices", "rules"}], "skip": [keys],
    "tiers": {key: tier}, "validate": {key: item row}, "info": {key: the
    same dict as an "ai" entry, for every key}}."""
    by_key = {}
    for ln in lines:
        k = ln.get("desc_key")
        if k:
            by_key.setdefault(k, []).append(ln)
    write, ai, skip, tiers, validate, info = {}, [], [], {}, {}, {}
    for key, ls in by_key.items():
        desc = Counter((ln.get("description") or "").strip() for ln in ls).most_common(1)[0][0] or key
        rules = decode_rules(desc)
        info[key] = {"key": key, "desc": desc, "rules": rules,
                     "prices": [] if is_merged(desc) else [ln.get("price") for ln in ls]}
        line_items = [((hits.get(ln.get("sku")) or {}).get("items") or []) for ln in ls]
        ids = {it[1]["id"] for its in line_items for it in its}
        # a check for the AI: the item of any line with exactly one item
        one = next((its[0] for its in line_items if len(its) == 1), None)
        if one:
            validate[key] = item_fields(one[0], one[1]["id"] if one[1]["source"] == "item" else None, rules)
        old = details.get(key)
        if all(len(its) == 1 for its in line_items) and len(ids) == 1:
            ident, hit = line_items[0][0]
            row = item_fields(ident, hit["id"] if hit["source"] == "item" else None, rules)
            if old and old.get("source") == "ai":
                row["decode_raw"] = {**row["decode_raw"], "prev": {k: old.get(k) for k in (
                    "category", "item_type", "gender", "color", "brand_guess", "product_model", "confidence",
                    "model")}}
            tiers[key] = "matched_item"
            if _row_differs(old, row):
                write[key] = row
            continue
        if rules["tier"] == "high":
            tiers[key] = "rules"
            row = rules_fields(rules)
            if old is None or old.get("source") in ("rules", "matched_item") or redo_ai:
                if _row_differs(old, row):
                    write[key] = row
            continue
        if "too_short" in rules["notes"]:
            tiers[key] = "skip"
            skip.append(key)
            continue
        tiers[key] = "ai"
        if old is None or old.get("source") != "ai" or redo_ai:
            ai.append(info[key])
    return {"write": write, "ai": ai, "skip": skip, "tiers": tiers, "validate": validate, "info": info}


def decode_pending(*, max_keys: int | None = None, max_usd: float | None = None, ask=None, log=print) -> dict:
    """The daily top-up: matched-item and rules rows for every description,
    the AI for new ones (at most max_keys, spend capped at max_usd). Writes
    only receipt_line_details. Returns a summary."""
    import db
    from pipeline import owned
    data = db.receipt_decode_inputs()
    hits = owned.ross_hits({ln["sku"] for ln in data["lines"] if ln.get("sku")})
    p = plan(data["lines"], data["details"], hits)
    pending = p["ai"][:max_keys] if max_keys is not None else p["ai"]
    rows = dict(p["write"])
    spent = {"cost_usd": 0.0, "calls": 0, "stopped": None, "left": 0}
    if pending:
        got, spent = ai_decode_all(pending, max_usd=config.RECEIPT_DECODE_MAX_USD if max_usd is None else max_usd,
                                   ask=ask, log=log)
        rows.update(got)
    if rows:
        db.save_receipt_line_details([{"desc_key": k, **r} for k, r in rows.items()])
    return {"saved": len(rows), "ai": len(pending), "ai_cost_usd": round(spent["cost_usd"], 4),
            "stopped": spent.get("stopped"), "waiting": len(p["ai"]) - len(pending) + spent.get("left", 0)}


# --- in words ---------------------------------------------------------------------------------

_GENDER_WORDS = {"men": "men's", "women": "women's", "boys": "boys'", "girls": "girls'", "kids": "kids'",
                 "baby": "baby", "unisex": "unisex"}
_CATEGORY_WORDS = {"jewelry_watches": "jewelry", "sports_outdoor": "sports", "baby_gear": "baby gear"}


def describe(row: dict | None) -> str:
    """'Ed Hardy · bags: duffel · pink/multi'; a '?' when unsure."""
    if not row or not row.get("category"):
        return ""
    cat = _CATEGORY_WORDS.get(row["category"], row["category"])
    kind = f"{cat}: {str(row['item_type']).replace('_', ' ')}" if row.get("item_type") else cat
    bits = [str(row["brand_guess"]) if row.get("brand_guess") else None,
            str(row["product_model"]) if row.get("product_model") else None,
            kind, _GENDER_WORDS.get(row.get("gender")), row.get("color")]
    text = " · ".join(b for b in bits if b)
    conf = row.get("confidence")
    return text + ("?" if conf is not None and float(conf) < LOW_CONFIDENCE else "")


def clean_city(city: str | None) -> str | None:
    """'Dress For Less Miami' / 'Press For Less Doral' -> 'Miami' / 'Doral'."""
    c = re.sub(r"^\s*\w*ress\s+For\s+Less\s*", "", str(city or ""), flags=re.I).strip(" ,-")
    return c.title() or None


def store_cities(rows) -> dict:
    """{store_number: the most common cleaned city} from (store, city, count)."""
    votes = {}
    for store, city, n in rows:
        c = clean_city(city)
        if store and c:
            votes.setdefault(store, Counter())[c] += int(n or 1)
    return {s: v.most_common(1)[0][0] for s, v in votes.items()}


# --- misread lines ----------------------------------------------------------------------------

_GARBLED = re.compile(r"\ufffd|\?\?|[\u0370-\u03ff\u0400-\u04ff]|[A-Za-z][\u00c0-\u024f]|[\u00c0-\u024f][A-Za-z]")
_PRICE_TEXT = re.compile(r"\$\s?(\d+)[.,]\s?(\d{2})")
_ORIGINAL = re.compile(r"Original Price:?\s*-?\$\s?(\d+)[.,](\d{2})", re.I)
_CARD = re.compile(r"Auth\.? No|AID:|MASTERCARD|VISA|DEBIT", re.I)


def decimal_fix(price: float) -> float | None:
    """A price that lost its decimal point: 4998 ('$4.99B' with the B read
    as 8) -> 4.99; 4399 -> 43.99. None when it doesn't look like either."""
    if price is None or price <= 500 or price != int(price):
        return None
    s = str(int(price))
    if len(s) in (4, 5) and s.endswith("8") and s[-3:-1] in ("99", "49", "00", "50"):
        return int(s[:-1]) / 100
    if s[-2:] in ("99", "49", "00", "50"):
        return int(s) / 100
    return None


def misread_reasons(line: dict, receipt: dict | None = None, *, linked: bool = False) -> list[tuple[str, str]]:
    """[(code, words)] for one receipt line. line: description, price, sku,
    same_code_prices (distinct prices of its SKU on its receipt), fixed (a
    receipt_fixes row). receipt: subtotal, total, tax, line_count, lines_sum.
    linked: the SKU is one of our items (a cut-off description is then fine).
    Codes in INFO are not misreads, just worth knowing."""
    out = []
    desc = (line.get("description") or "").strip()
    price = line.get("price")
    price = float(price) if price is not None else None
    sku = str(line.get("sku") or "")
    receipt = receipt or {}
    if not desc:
        out.append(("blank", "no description read"))
    else:
        clean = desc.translate(_PUNCT)
        if _GARBLED.search(clean):
            out.append(("garbled", "garbled text"))
        u, markers, _ = normalise(desc)
        if letters(u) < 3 and "dated" not in markers and not re.search(r"\d\s?(OZ|ML|CT|PK)\b", u) and not linked:
            out.append(("cut_off", "description cut off"))
        if is_merged(desc):
            out.append(("merged", "two lines merged into one"))
        if re.search(r"55\+\s*Tu|Tuesudy|Disc 10%|Original Price", desc, re.I):
            out.append(("promo", "promo text in the line"))
        if re.search(r"Begin Return", desc, re.I):
            out.append(("return", "return marker"))
        if _CARD.search(desc):
            out.append(("not_item", "not an item (card text)"))
        amounts = [float(f"{a}.{b}") for a, b in _PRICE_TEXT.findall(desc)]
        orig = _ORIGINAL.search(desc)
        if amounts and price is not None and not any(abs(a - price) <= 0.05 for a in amounts) and \
                not (orig and abs(float(f"{orig.group(1)}.{orig.group(2)}") - price) <= 0.05):
            out.append(("price_shift", f"price differs from the text (${amounts[0]:.2f})"))
    if price is None:
        out.append(("no_price", "no price read"))
    elif price > 500:
        fix = decimal_fix(price)
        out.append(("decimal", f"decimal lost? {price:g} → ${fix:.2f}?" if fix else
                    f"decimal lost? ${price:,.2f}"))
    elif 0 < price < 0.49:
        out.append(("tiny", "price looks too low"))
    if sku and re.fullmatch(r"\d{12}", sku) and not sku.startswith("40") and not _CARD.search(desc):
        out.append(("sku", "SKU misread?"))
    n_prices = int(line.get("same_code_prices") or 0)
    if n_prices > 1:
        out.append(("several_prices", f"this receipt lists this code at {n_prices} prices"))
    # the receipt's total or subtotal read as an item
    if price is not None and price >= 20 and int(receipt.get("line_count") or 0) >= 2:
        totals = [receipt.get(k) for k in ("subtotal", "total", "tax")]
        lines_sum = float(receipt.get("lines_sum") or 0)
        others = lines_sum - price
        if any(t is not None and abs(float(t) - price) <= 0.01 for t in totals) and \
                (abs(others - price) <= 1.0 or price > 0.5 * lines_sum):
            out.append(("total", "receipt total read as an item?"))
    if line.get("fixed"):
        out.append(("fixed", "fixed by hand"))
    return out


INFO = ("fixed", "sku")
