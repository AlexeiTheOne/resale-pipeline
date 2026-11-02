# legacy/: reference only

These are old standalone tools, copied **unchanged** on 2026-11-02 from
`C:\Users\boris\Projects\resale-pipeline-OLD-keep`. They are here so the
logic can be read and ported (see
[docs/legacy-tools-assessment.md](../docs/legacy-tools-assessment.md)). They
are **not imported anywhere and don't run against this repo**: `comps.py`
imports `pipeline.price`, which no longer exists, and the old `config.py`.

| File | What it was |
|---|---|
| `comps.py` | Pre-buy comps from a text description: Apify sold + eBay Browse active, matching rules, profit with 7% tax, break-even, BUY/MAYBE/BORDERLINE/SKIP |
| `comps_app.py` | Local web page (127.0.0.1:8765) over `comps.get_comps`, with a live profit calculator. The older version: no "Exact matches only" checkbox. |
| `check_keys.py` | Checks that each API key is present and makes one real test call per service. Prints the first 4 and last 3 characters of each key, so don't share its output. |

They contain no keys: all of them read the environment / `.env`. The repo
tests that scan every `.py` file (`test_dotenv_once`, `test_db_access`)
skip this folder.
