"""Keep the test run off the real data/ folder.

db.py, ebay/auth.py and ebay/taxonomy.py create their tables at import, and
telegram_bot.py makes data/inbox at import. Every data path is built from
config.DATA_DIR, so pointing it at a temp dir here — before any test module
imports those — means nothing under C:/Ebay-list/data is opened or created.
Test modules that also point db.DB_PATH at their own file still do so.
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import config  # noqa: E402

config.DATA_DIR = Path(tempfile.mkdtemp(prefix="ross_test_data_"))
config.PRICING_V2_CACHE_DIR = str(config.DATA_DIR / "cache" / "pricing_v2")
