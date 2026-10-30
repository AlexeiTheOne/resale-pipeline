"""Per-user bot state: module-level dicts and sets, mutated in place.

Always `from bot import state` and use `state.review` etc.; never rebind these.
"""
from collections import OrderedDict

pending = {}        # user_id -> {"files": [bytearray,...], "task": asyncio.Task}
review = {}         # user_id -> item_id  (item currently awaiting this user's reply)
locks = {}          # user_id -> asyncio.Lock guarding pending[user_id]
last_item = {}      # user_id -> item_id  (last-touched item, survives past the review window)
capture_window = {} # user_id -> seconds to batch photos (default DEFAULT_CAPTURE_WINDOW)
staging = {}        # user_id -> {item_id, folder, paths, chat_id, task} awaiting Start/Wait/Cancel
awaiting_photos = {}# user_id -> item_id  (photos should attach to this existing item, set by /addphotos)
appending = {}      # user_id -> {item_id, folder, base, new, chat_id, task} while an append batch is collected
gate = {}           # user_id -> {item_id, stage}  paused at "identify"/"price" awaiting confirm/correction
gate_queue = {}     # user_id -> [{item_id, stage, reason}]  gates waiting their turn (a /haul runs many
                    # items at once, and only one can hold the single gate slot; the rest queue here
                    # instead of overwriting each other and getting lost)
find_mode = set()   # user_ids in /find: each photo is looked up, not listed
find_loc = {}       # user_id -> box/location typed with /find (e.g. "G3")
last_find_photo = {}  # user_id -> path of the latest /find photo (seeds Quick add)
quick_add = {}      # user_id -> {"photos": [...], "task": timer, "loc": str|None}
inv_box = {}        # user_id -> current box in inventory mode (/inv G3)
inv_session = {}    # user_id -> piece codes captured this session (for the summary)
list_after_photos = {}  # user_id -> item_id: /list waiting for real listing photos
haul_mode = set()   # user_ids whose next photo batch is a multi-item haul to be split on Ross tags
# message_id of a gate/draft the bot sent -> the item it was about. A haul puts
# several drafts on screen at once, so "which item did you mean?" can't be
# answered by a single most-recent pointer: replying to a specific message is how
# the user says which one. Bounded so a long session can't grow it forever.
msg_target: "OrderedDict[int, str]" = OrderedDict()
MSG_TARGET_MAX = 400

# (kind, item_id) pairs with work in flight. The app runs with
# concurrent_updates(True), so two taps / two commands for the same item are
# handled at once; a status check followed by an await lets both through.
# _claim must be called with no await between the status check and it.
_in_flight: set[tuple[str, str]] = set()
