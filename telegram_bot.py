"""Entry point: `python telegram_bot.py` starts the bot (bot.app.main).

Everything lives in the bot/ package (docs/cleanup-plan.md Phase 2). This file
loads .env, re-exports the names the tests reach as telegram_bot.<name>, and
runs main(). No bot/ module imports it.
"""
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()
sys.path.insert(0, str(Path(__file__).parent))

from telegram.ext import (  # noqa: E402,F401  (re-export for tests)
    CallbackQueryHandler, CommandHandler, MessageHandler, TypeHandler,
)

from config import SYNC_DAYS  # noqa: E402,F401  (re-export for tests)
from db import get_item, list_items, update_field, update_status  # noqa: E402,F401  (re-export for tests)
from ebay.inventory import TitleTooLongError  # noqa: E402,F401  (re-export for tests)

# Re-exports for the tests (tb.<name>). The state names are the same dict/set
# objects as bot.state's, mutated in place, never rebound.
from bot.state import (  # noqa: E402,F401  (re-export for tests)
    pending, review, locks, last_item, capture_window, staging, awaiting_photos,
    appending, gate, gate_queue, find_mode, find_loc, last_find_photo, quick_add,
    inv_box, inv_session, list_after_photos, haul_mode, msg_target, MSG_TARGET_MAX,
    _in_flight,
)
from bot.util import (  # noqa: E402,F401  (re-export for tests)
    _parse_allowed_ids, _remember_target, _replied_item, _lock_for, _secrets, _scrub,
    _safe_reply, TELEGRAM_MAX_CHARS, _send_chunked, _chunk_lines, _send_chunked_to,
    _recent_errors, _record_error, _resolve_item_id, _ITEM_ID_PREFIX_LEN,
    _names_an_item, _claim, _release,
)
from bot.formatting import (  # noqa: E402,F401  (re-export for tests)
    _title_too_long_text, _fmt, format_identification, _EVIDENCE_LABEL, _short,
    _fmt_money, _format_pricing_v2, format_pricing, _price_evidence_line, format_draft,
)

from pipeline.stages import (  # noqa: E402,F401  (re-export)
    stage_identify as _stage_identify, stage_price as _stage_price,
    stage_draft as _stage_draft, brand_warnings as _brand_warnings,
    duplicate_warnings as _duplicate_warnings, find_tag_photo as _find_tag_photo,
    split_receipt as _split_receipt,
)
from pipeline.sync import run_sync, sync_one as _sync_one  # noqa: E402,F401  (re-export)
from pipeline.offers import offer_plan as _offer_plan  # noqa: E402,F401  (re-export)
from profit import (  # noqa: E402,F401  (re-export)
    sale_economics as _sale_economics, sale_profit as _sale_profit,
)
from db import (  # noqa: E402,F401  (re-export)
    create_item, REAL_CODE_FLOOR as _REAL_CODE_FLOOR,
    match_quantity_to_pieces as _match_quantity_to_pieces,
    next_receipt_code as _next_receipt_code, set_location as _set_location,
)

from bot.intake import (  # noqa: E402,F401  (re-export)
    INBOX, DEFAULT_CAPTURE_WINDOW, EXTENDED_CAPTURE_WINDOW, WAIT_EXTENSION,
    finalize_capture, _is_ross_tag, _split_haul, _run_haul, _haul_digest, haul_command,
    _confirmation_markup, _send_confirmation, _await_more_photos, _finalize_append,
    addphotos_command, photo_handler,
)
from bot.inventory_cmds import (  # noqa: E402,F401  (re-export)
    _FIND_DIR, find_command, done_command, _end_find, _find_one, _QUICK_ADD_WAIT_FIRST,
    _QUICK_ADD_WAIT_NEXT, quick_add_callback, _quick_add_photo, _quick_add_timer,
    _quick_add_item, _stock_line, stock_command, _PIECES_DIR, _inv_semaphore,
    inv_command, _inv_photo, _inv_process, _inv_done, where_command, move_command,
    mark_command, ghosts_command, list_command,
)
from bot.photos_cmds import (  # noqa: E402,F401  (re-export)
    _resync_photos, _send_photo_index, photos_command, _apply_photo_order, _photo_args,
    cover_command, arrange_command, stockphoto_command, photocheck_command,
)
from bot.flow import (  # noqa: E402,F401  (re-export)
    _pipeline_semaphore, _review_markup, _notifier, _run_stage, _retry_markup,
    _show_review, _publish_and_report, _publish_and_report_claimed,
    _identify_gate_reason, _price_gate_reason, advance, _advance_one, _gate_is_busy,
    _enqueue_gate, _activate_next_gate, _show_identify_gate, _show_price_gate,
    confirm_callback, _approve_item, review_callback, _activate_item, _promote_item,
    _retry_item,
)
from bot.text_router import (  # noqa: E402,F401  (re-export)
    _PRICE_CORRECTION, _price_from_correction, _apply_manual_price, _charm_price,
    _parse_price_override, _CONFIRM_WORDS, _explicit_target, _handle_targeted,
    _handle_gate, text_handler, _floor_refusal,
)
from bot.listing_cmds import (  # noqa: E402,F401  (re-export)
    _MAX_SANE_QTY, _MAX_SANE_PRICE, listing_command, notdup_command, delete_command,
    _refresh_descriptions, refreshdesc_command, offers_command, end_command,
    setprice_command, setqty_command, activate_command, promote_command, _reprice_markup,
    _offers_markup, _offer_plan_lines, _VERDICT_LABEL, _VERDICT_ADVICE,
    _format_reprice_line, _format_reprice_short, _reprice_health_line,
    _send_reprice_digest, pricecheck_command, _send_offer_plan, reprice_callback,
    retry_command,
)
from bot.sales_cmds import (  # noqa: E402,F401  (re-export)
    _ross_title, _ross_fields, ross_command, sold_command, _item_profit, profit_command,
    sync_command, _run_sync, _sync_days, report_command, _NO_CODE_WORDS, receipt_command,
    scoreboard_command,
)
from bot.ops_cmds import (  # noqa: E402,F401  (re-export)
    auth_command, whoami_command, STATUS_DEFAULT_LIMIT, status_command, errors_command,
    comps_command, usage_command, feedback_command, health_command, cancel_command,
    HELP_SECTIONS, help_command,
)
from bot.jobs import (  # noqa: E402,F401  (re-export)
    _sold_alert_loop, _JOB_REPRICE, _JOB_SYNC, _JOB_FEEDBACK, _previous_slot, _next_slot,
    _missed_slot, _run_reprice_job, _weekly_reprice_loop, _run_sync_job,
    _send_budget_warning, _daily_sync_loop, _feedback_report, _only_new_feedback_problems,
    _run_feedback_job, _daily_feedback_loop,
)

from bot.app import (  # noqa: E402,F401  (re-export)
    configure, _auth_guard, _post_init, _post_shutdown, error_handler, _ipv4_request,
    register_handlers, build_app, main,
)

if __name__ == "__main__":
    main()
