# Dashboard design (2026-11-12)

Design only. Nothing here is built. Owner's brief:
- Scope and access:
  - a dashboard for the bot, with a backend API and a separate frontend;
  - runs on the production PC, reachable only through Tailscale, owner login;
  - single-user now, but structured for several accounts later.
- How it works:
  - every action reuses the bot's own functions, with no duplicated logic;
  - every change shows a preview and asks for confirmation;
  - phase 1 is read-only.

**Review (2026-11-12): approved by the owner and Person B, with the changes
below folded in:**
- a one-time login link from the bot (section 4);
- sliding sessions, revocation and a sessions list (section 4);
- crash recovery for actions (section 1);
- the SQL Server auth change and its restart (section 7, phase 0);
- the Smart App Control risk for the frontend build (section 6);
- desktop first, plus a simple phone view (section 3).

Also decided:
- intake (photos in, drafting) stays in Telegram;
- phase 3 (multi-account) is not planned now;
- **no dashboard code until the repo is private.** The `setprice` /
  `revise` refactor (section 2) goes ahead now as its own PR, because the bot
  needs it anyway.

## 1. Shape

```
browser (tailnet only)
   │  HTTPS, MagicDNS name, cert from Tailscale
tailscale serve ──► dashboard API (FastAPI, 127.0.0.1:8787) ──► SQL Server ResaleDB (read-only login)
                         │  writes an action request
                         ▼
                    actions table ◄── bot process: action runner loop
                                         (the same functions /setprice, /revise,
                                          /offers, /shiptest call; eBay first, then DB)
```

**The API never writes to eBay or to business tables.** Every change is a
row in a new `actions` table. The bot process picks the row up and runs it.
The bot process is where everything below already lives:
- the per-item locks (`util._claim`) and the deploy's `logs/busy.json`;
- the eBay token refresh;
- the "eBay first, then the DB" order.

Putting the executor anywhere else would mean a second eBay writer with its
own locks, and the deploy couldn't tell when it's busy. A web change and a
Telegram command on the same item still exclude each other, because they
take the same lock in the same process.

The queue also gives these for free:
- an audit log: who asked, the preview shown, what was confirmed and the result;
- a dashboard that keeps working while the bot restarts on a deploy. A
  queued action waits; it is never lost.

**An action's lifecycle.** Each step is a row update:

| Step | Who | What happens |
|---|---|---|
| `requested` | API | The owner asks for a change (e.g. bulk price on 12 listings) |
| `previewed` | bot | Runs the preview function and stores the plan (rows, fingerprints of the live state) with a 30-minute expiry |
| `confirmed` | API | The owner confirms that exact plan id. A changed plan means a new preview |
| `running` → `done` / `failed` | bot | Runs the apply function per row. Each row: eBay first, then the DB. Results are per row |

Read actions that need eBay (diagnose with live traffic, the free-shipping
test report) use the same queue with `kind = read`. They need no confirmation.
The bot runs them and stores the result.

**Crash recovery.** A crash or a deploy restart can stop the bot mid-action.
Each row of a plan stores a fingerprint of the live state it was planned
against (eBay price, Best Offer thresholds, shipping policy) and, once sent,
the state it should end in. On startup, before the runner takes new work, it
goes through every action left in `running` or `confirmed`:

| Left in | Row state | What the runner does |
|---|---|---|
| `running` | eBay already matches the target | Marks the row done and finishes the DB step (eBay first, then the DB, so the DB is the half that can be missing) |
| `running` | eBay still matches the fingerprint | The row never reached eBay. Retries it once |
| `running` | eBay matches neither | Someone changed it in between. Fails the row with both states shown and never retries |
| `confirmed` | not started, plan expired (30 min) | Expires the action. The owner previews again |
| `confirmed` | not started, plan still valid | Re-checks each row's fingerprint and runs the rows that still match. The rest fail as "changed since the preview" |

Reads of live eBay use the same calls the apply function uses
(`inventory.get_offer`). A row is never sent twice without this check, and the
dashboard shows recovered actions with a "recovered after restart" note.

## 2. No duplicate logic: what has to move first

Some actions are already split into "work out the plan" and "apply it",
outside Telegram. Others still have their logic inside the Telegram handler.
These need a small refactor first, with the Telegram command becoming a thin
caller:

| Action | Today | Needed |
|---|---|---|
| Free-shipping test | `pipeline/ship_test.preview / apply / report` | Nothing |
| Diagnose | `pipeline/diagnose.diagnose` returns rows | Nothing; store the last run's rows (below) |
| Watcher offers | `pipeline/offers.offer_plan`, `reprice.build_offer_plan / send_planned_offers` | A plan for chosen listings, not only "all watched" |
| Revise | `build_plan` / `apply_plan` in `bot/revise_cmds.py` | Move to `pipeline/revise.py` (no logic change) |
| Set price | Inside `setprice_command` (floor, test freeze, relist check, push, save) | New `pipeline/price_ops.preview / apply`. `/setprice` and the reprice button call it |

Rule for the codebase: a dashboard action may only call a `pipeline.*`
function that a Telegram command also calls. The dashboard has no eBay or
pricing logic of its own. A convention test can enforce it, the same way
`tests/test_bot_conventions.py` does for the bot.

## 3. Pages

| Page | Shows | Actions (phase 2) |
|---|---|---|
| **Listings** | One row per listing: title, price, days live, watchers, weekly impressions / views / CTR (`listing_stats`), weekly verdict, last `/diagnose` code and fix, cost, break-even, Best Offer thresholds (computed by `offer_thresholds`), test group. Sort, filter, saved views. | Select rows, then **set price / revise / send offers**, each through a preview |
| **Overview** | Sales, revenue, net, profit per week and month (`sales`, `profit.project`); stock: live units, cost tied up, age buckets; sell-through | None |
| **Free-shipping test** | Both groups, prices before and after, start/end, CTR / impressions / sales against the baseline (`ship_test.report`) | Start (already `/shiptest`), and later "end the test" |
| **Receipts and inventory** | Receipts with their lines and what each line became (`receipt_lines_search`, `owned`); inventory boxes and pieces | None at first. Fixes stay in the bot |
| **Check history** | `/check` runs: photo, identification, price ladder, evidence, verdict, status (`list_checks`, `recent_evidence`) | None |

A **Settings** page holds the active sessions (section 4) and, later, the
business settings.

**Screens.** Desktop first: the listings table and preview screens are built
for a wide screen. Overview and listings also get a simple phone view:
- overview as stacked cards;
- listings as a sortable, filterable list of cards (title, price, verdict,
  impressions / CTR), read-only.
Bulk actions and the other pages stay desktop only. On a phone they show
their data but no action buttons.

**Diagnose verdicts need storing.** `/diagnose` computes on demand and keeps
nothing. A small `diagnose_results` table (item, run time, code, fix, numbers)
lets the listings table show the last verdict without calling eBay on every
page load. The bot fills it on each `/diagnose` run, from the dashboard or
from Telegram.

## 4. Access and login

- **Network:**
  - The API listens on `127.0.0.1` only. `tailscale serve` publishes it on
    the tailnet with HTTPS (`https://<pc>.<tailnet>.ts.net`).
  - Nothing is opened on the router or the Windows firewall.
- **Login: a one-time link from the bot.** The `Tailscale-User-Login` header
  alone isn't enough: any process on this PC can reach `127.0.0.1` and send
  it. So:
  - `/login` in Telegram (owner only, checked like `/shiptest`) replies with
    `https://<pc>.<tailnet>.ts.net/login?code=...`;
  - the code is random (32 bytes, `secrets.token_urlsafe`), single use and
    valid 5 minutes. Only its hash is stored (`dashboard_login_codes`);
  - opening the link swaps the code for a session cookie (HttpOnly, Secure,
    SameSite=Strict) and marks the code used. A second use, or a use after 5
    minutes, gets "link expired, send /login again";
  - the Tailscale header is still checked against the owner allowlist as a
    second factor, so a leaked link alone doesn't work off the tailnet;
  - no passwords to manage.
- **Sessions:**
  - sliding expiry: `DASHBOARD_SESSION_DAYS` (default 60) from the last use,
    renewed on each request. A phone left unused for 60 days logs out;
  - stored server-side (`dashboard_sessions`: hash of the cookie, device from
    the user agent, created, last used, revoked). The cookie holds only a
    random id;
  - `/logout_all` in Telegram (owner only) revokes every session at once, for
    a lost phone;
  - the Settings page lists active sessions (device, last used) with a
    "log out" button for each, and marks the current one.
- **Writes (phase 2):**
  - a CSRF token on every request;
  - confirmation by plan id;
  - re-confirmation for anything touching more than 10 listings or lowering
    a price below the break-even (the bot's own `force` cases).
- **The DB login:**
  - phase 1 uses a login with `db_datareader` only. Read-only is then
    enforced by the server, not by code.
  - Phase 2 adds `INSERT/UPDATE` on `actions` alone. Login codes and
    sessions also need `INSERT/UPDATE`, on those two tables only.
  - SQL Server here accepts Windows logins only (checked 2026-11-12:
    `IsIntegratedSecurityOnly = 1`). A SQL login needs mixed mode, which
    takes a SQL Server restart; see phase 0 in section 7.
- **Secrets:** the eBay token and `.env` are never readable through the API.

## 5. Ready for several accounts later

The model should be account-shaped from day one. The schema changes can
wait until they're needed.

- **API:**
  - every route is `/api/accounts/{account_id}/...`;
  - the session holds the accounts the login may see;
  - today there is one account, id 1.
- **Data (phase 3, Person B's area, through `migrations.py`):**
  - an `accounts` table;
  - `account_id INT NOT NULL DEFAULT 1` on the business tables (`items`,
    `ebay_listings`, `sales`, `receipts`, `checks`, `offer_plans`, `actions`,
    `quality_report_*`, `old_store_sales`), with an index;
  - `ebay_tokens` keyed by account.
- **Settings:**
  - an `account_settings (account_id, name, value)` table on top of `config`;
  - the per-account settings are the business ones: ad rate, Best Offer
    percentages, shipping charged and label cost, test settings, owner
    allowlist;
  - machine settings stay in `.env`.
- **eBay OAuth per account:**
  - a "Connect eBay" page runs eBay's consent flow and stores that account's
    refresh token;
  - the redirect goes to the tailnet HTTPS URL.
- **The bot stays single-account until there is a second one.** Its queries
  then take `account_id`, and its jobs loop over accounts.

## 6. Stack

| Part | Choice | Why |
|---|---|---|
| API | **FastAPI + uvicorn**, Python 3.13, in this repo (`dashboard/api/`) | Imports `db`, `profit` and `pipeline.*` directly, so no second implementation. Typed responses (Pydantic) give the frontend its types |
| Frontend | **React 19 + Vite + TypeScript** (`dashboard/web/`), with **TanStack Query** (server data, caching), **TanStack Table** (sort, filter, row selection, bulk), **Recharts** (overview) | Same toolchain as `web/`, so one way of building. TanStack Table covers the listings page without a heavy grid |
| Serving | The API serves the built frontend, so one port, one process | Nothing else to run or secure. The PC only ever needs the built files |
| Running | A scheduled task like the bot's, with its own heartbeat; `deploy.ps1` restarts it after the bot | Same deploy, same rollback |
| Tests | pytest for the API (fake DB as in `tests/`), Playwright for a few page checks (already in `web/`) | |

**The frontend build and Smart App Control.** Smart App Control is in
enforce mode on this PC (checked 2026-11-12). Vite builds with esbuild (and
Rollup), which ship unsigned native binaries, the same kind of file that got
`jiter` blocked. `web/` has never been installed here, so it's unproven. Plan:
1. **Default: build elsewhere.** The frontend is built on a laptop or in
   GitHub Actions, and only the built files (`dashboard/web/dist`: HTML, JS,
   CSS) reach this PC. They're plain files the API serves, so nothing native
   runs here. The deploy copies `dist` like any other file and never runs
   `npm`.
2. **Optional, before phase 0:** try `npm ci && npm run build` once in a
   scratch copy here. If Smart App Control allows it, building on the PC is
   allowed too. If it blocks it, nothing changes, because option 1 is
   already the plan.

Where the built files live is decided at phase 0: committed to the repo
(simple, and the deploy needs no change) or a release artifact the deploy
downloads. On the API side, FastAPI, Starlette and plain `uvicorn` are pure
Python. Their one native part, `pydantic_core`, is already in the live venv
and loads. Use plain `uvicorn`, not `uvicorn[standard]`, which pulls in the
native `httptools` and `watchfiles`. Each new Python dependency is checked
against Smart App Control on this PC before it's added.

**What `web/` already offers:**
- the React 19 / Vite / TypeScript setup, zustand, and Playwright screenshots
  (`web/scripts/shots.mjs`);
- the 3D house, rooms and boxes view of inventory, on fake data only.

That view could become the inventory page's "where is it" view once it
reads the real `pieces` and box locations. It isn't needed for phases 1–2.
I'd keep `web/` as it is and start `dashboard/web/` on the same toolchain,
moving the 3D view in later if it earns its place.

## 7. Phases and effort

Estimates are working days for one developer with Claude, tests included.

| Phase | What | Effort |
|---|---|---|
| **0. Skeleton** | API and frontend skeletons (frontend built off the PC), `tailscale serve`, `/login` link and sessions, `/logout_all`, read-only SQL login, scheduled task, deploy step, heartbeat | 2–3 days |
| **1. Read-only** | Listings table (DB data, weekly stats, cost, break-even, Best Offer, test group); overview; free-shipping test page (DB part); receipts and inventory; check history. Then the `actions` queue for **read** actions only (diagnose run, test report from eBay) and the `diagnose_results` table | 5–7 days |
| **2. Actions** | Refactor setprice and revise into `pipeline/` (section 2); confirmed write actions in the queue; preview and confirm screens; bulk set price, revise and offers; per-row results; audit page | 7–9 days |
| **3. Multi-account ready** | `accounts`, `account_id` columns, `account_settings` overlay, Connect eBay per account; the bot's queries take `account_id` | 5–8 days (not planned now) |

The phase 0 estimate grew by a day for the login link and sessions. Phase 2
includes crash recovery and the Settings page's sessions list.

**Before phase 0:**
1. **The repo is private.** No dashboard code before that.
2. **The read-only DB login.** There are two ways, both Person B's call:

   | | A. Mixed mode + SQL login (the brief) | B. Windows login for a dashboard user |
   |---|---|---|
   | How | Turn on SQL Server and Windows authentication, restart SQL Server, create a SQL login with `db_datareader` | Create a local Windows user (e.g. `resale-dash`), run the dashboard's scheduled task as it, and add a Windows login with `db_datareader` |
   | Restart | **Yes:** the `MSSQL$SQLEXPRESS` service | No |
   | Secret | A SQL password in the dashboard's `.env` | None: Windows signs it in |
   | Exposure | A new password-based login on the server (localhost only, but still one more way in) | None new |

   **Recommendation: B.** It's the same server-enforced read-only access,
   with no restart and no password to keep. If Person B prefers A, plan it
   like this:
   - **Downtime:** about 1–2 minutes of SQL Server restart. The bot can't
     reach the DB in that time. Telegram commands fail with a DB error, and
     a background job in flight fails its run and retries next cycle. eBay
     is unaffected.
   - **When:** after the evening sync, at a quiet hour, with `logs/busy.json`
     absent and no deploy running (pause `EbayBotDeploy` for the window).
   - **Steps:**
     1. take a backup;
     2. set mixed mode (SSMS, or the `LoginMode` registry value set to 2);
     3. restart only `MSSQL$SQLEXPRESS`, never the PC;
     4. restart the bot via `restart_ebay_bot.ps1` so it reconnects cleanly;
     5. check the heartbeat;
     6. re-enable the deploy task.
   - **Check `receipt_pipeline` first:** it shares the server. Person B
     confirms it can take the same 1–2 minute gap, since we never touch its
     processes.
3. **Optional:** try the frontend build on this PC (section 6).

Phase 1 gives a usable dashboard in about a week and changes nothing on eBay.
Phase 2 is where most of the risk is. The refactor in section 2 is worth
doing even without the dashboard, because it removes logic from Telegram
handlers.

## 8. A future SaaS version (not built)

What would have to be added for other sellers, on top of phase 3:

- **Hosting and access:** a public host instead of this PC; real accounts
  (email or Google login, MFA, password reset); tenant isolation enforced in
  the database (row-level security), not only in queries.
- **eBay:**
  - a production app reviewed by eBay for third-party use;
  - the required marketplace account-deletion notification endpoint;
  - per-tenant API quotas. Analytics and Browse limits are per app, so
    tenants share them, and the batching and caching need to be per tenant.
- **The bot's work per tenant:**
  - scheduled jobs (sync, weekly check, feedback) per account;
  - notifications by web or email as well as Telegram, each tenant with its
    own chat;
  - photo storage per tenant instead of `data/`.
- **Money:** billing and plans; usage metering for the paid APIs (Gemini,
  Claude, Apify) per tenant.
- **Data duties:**
  - export and deletion per tenant;
  - backups and restore per tenant;
  - terms and a privacy policy, which matter for receipts and buyer data;
  - secrets in a vault, not `.env`.
- **Operations:** monitoring and alerting, support tools, onboarding (connect
  eBay, import listings), a status page.
- **Product:** settings UI for what is now `config.py`; the Ross-specific
  parts (receipts, tags, store codes) made optional or generalised.

## 9. Decisions and what's open

- **Intake stays in Telegram.** The dashboard is for reviewing and managing
  listings (owner, 2026-11-12).
- **Person B approved** sections 1, 4 and 5 (2026-11-12). Their area
  includes:
  - the `actions`, `diagnose_results`, `dashboard_login_codes` and
    `dashboard_sessions` tables;
  - the read-only DB login;
  - the deploy step;
  - any phase 3 schema.
- **Open, for Person B:** login option A or B (section 7). The
  recommendation is B.
- **Open, for Person A:** review of the whole doc in its PR.
