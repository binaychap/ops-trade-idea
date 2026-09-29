# Ops Trade Idea — project memory

## Fixed-dollar stock entries and duration policy (2026-09-28)

All stock buy paths now use the legacy-named `app/bullish/stock_bracket.py`
helper to submit one NORMAL MARKET / DAY / CORE order with AMOUNT and
`total_cash_amount=100.00`, without a quantity or attached exits. Legacy helper
quantity/price arguments cannot increase the dollar request. Main feed and
bullish runner enforce MAX_NOTIONAL_USD >= 100; the shared cap/default and option
budgets are unchanged. Manual API previews normalize buys to $100, expose the
DAY/market behavior and absent exits, and accept fractional market sell quantities.
Whole-share manual limit sells and option entry/exit defaults now use GTC;
market stock sells remain DAY. No live orders or environment edits made.

Webull's stock API docs restrict AMOUNT to less than one share's price and
fractional orders to MARKET. Helper rechecks a fresh stock quote; prices <= $100
are rejected, not converted to whole-share buys. Symbol/account fractional
eligibility, DAY amount acceptance and response schema remain unverified live.
Main dry runs remain broker-free and cannot establish quote/eligibility validity.

New scheduled stock jobs store entry_kind=amount, quantity=null and no exit IDs;
next-day reconciliation uses actual fractional fills. Amount BUY order details
need no requested total_quantity; missing fill counts remain errors. Existing
bracket jobs retain reconciliation/cancellation behavior and unknown submissions
are never blindly replayed. Standalone/manual entries still do not register
next-day jobs; account-wide morning sells remain separately configured.
Validation: 219 mocked tests pass; compilation and focused Ruff F821/F401
checks pass. Historical one-share/bracket descriptions below are superseded
by this section.


## GCP free-tier Terraform deployment (2026-09-26)

Terraform code lives in the separate repo `binaychap/ops-trade-idea-gcp`
(moved out of this repo on 2026-09-26 to manage infra independently). It
deploys the bot on GCP always-free tier (e2-micro, us-central1-a, firewall
TCP 8000). First-boot script installs uv, clones this repo to
`/opt/ops-paper-trade`, writes `.env` from Terraform variables, and enables
a systemd service running `uv run uvicorn app.main:app --host 0.0.0.0
--port 8000`. Secrets via gitignored `terraform.tfvars` or `TF_VAR_*` env
vars, never committed. Remote state via HCP Terraform Cloud (organization
`ops-trade-idea`, workspace `ops-trade-idea-gcp`); set `project_id`, secrets
and `GOOGLE_CREDENTIALS` as sensitive workspace variables after `terraform
login`, or switch the workspace to Local execution mode to keep secrets in
local `terraform.tfvars`. Config validated; not yet applied to a real GCP
project. Motivated by Oracle's idle-reclamation risk (GCP has none).

## Project name (2026-09-26)

Project metadata and uv lock entry now use ops-trade-idea. README/dashboard
branding and new-deployment examples use Ops Trade Idea / ops-trade-idea,
including the optional ops-trade-idea-bullish unit. Existing checkout directory,
Git remote, installed services, .env and database locations were not renamed.
The deployment guide notes migration requirements for existing installations.

## Additional functional boundaries (2026-09-25)

The initial common package has been separated into broker (clients, quotes,
stocks, errors), feeds (Optionomics client/decisions), persistence (ledger),
exits (morning_sell/next_day), options (shared contract/bracket tooling and
alternate runner), execution (submitter), and config (runtime/strategy settings).
Trading endpoints moved from ui to api/trading.py. common/paths.py centralizes
repository-root .env paths; main Settings remains importable via app.main while
its definition lives in config/settings.py. UI contains dashboard/records/assets.
Updated imports, compatibility launchers, tests and source references in docs.
208 tests pass; no settings, database schema, trading policy or deployment changed.
These locations supersede the initial functional-package map below.

## Functional packages (2026-09-25)

Code now lives in app/bullish (feed, runner, ledger, stock brackets), app/bearish
(PUT executor), app/ironcondor (neutral executor), app/ui (dashboard, records,
trading API and static assets), and app/common (broker, quotes, settings, ledger,
feed decisions, option builders and exit workers including morning_sell).
app/main.py remains the FastAPI composition/polling entry point. Hyphenated
root scripts are compatibility launchers; python -m app.bullish.runner is the
canonical bullish command. Internal imports and tests use the new paths.
Builder loaders now use normal package imports. Root .env discovery, database
paths/schema, UI routes and installed systemd commands are preserved. Older
source paths in historical entries below refer to the pre-reorganization layout.
Two existing time-dependent bullish tests now inject an open-market clock.
Validation: 208 tests pass; package compilation and focused undefined-name/unused
import checks pass. Fixed the older option runner's missing ZoneInfo import.
No services started, broker calls made, or environment values changed.

## Account-wide morning sells (2026-09-24)

Morning sell now enumerates broker equity positions only in the resolved
BULLISH_STOCK_ACCOUNT_NUMBER, including untracked/manual stocks. It rechecks
each holding and submits its full positive quantity, with no ledger quantity
cap. StockExecution.stock_positions excludes options, zero and short positions.
Daily reservations remain. Matching scheduled exits are marked complete after
submission; bullish rows are marked sold only if configured stock and bullish
runner accounts match. Existing bracket cancellation and fill reconciliation
limitations remain. This supersedes prior tracked-only scope/account routing
observations. Ten mocked morning-sell tests pass including untracked holdings,
account isolation, quantity, position filtering and restart reservation checks.
No broker orders placed; local .env and deployment were not changed.

## Morning sell review limitations (2026-09-24)

Source review: morning sell does not cancel existing bracket exits or confirm
market-sell fills before marking source rows sold/complete. Failed/reserved
symbols are not retried that day; the in-memory daily pass also prevents later
holdings being picked up until another day (unless restarted). It selects all
eligible tracked stocks regardless of purchase date, not options or every broker
holding. Bullish-runner rows resolve through BULLISH_STOCK_ACCOUNT_NUMBER even
though entries use TOP_BULLISH_ACCOUNT_NUMBER; differing accounts can misroute
position lookup/sales. Dry-run direct scheduler calls still query positions,
although application startup suppresses the worker entirely under DRY_RUN.
Eight existing mocked tests pass but do not verify cancellation/fill reconciliation.
These findings qualify the earlier safety claims below; no execution changed.

## Bullish overnight scans disabled (2026-09-24)

Restored market-hours gates in `app/main-top-bullish.py` using `ExitCalendar`
with explicit New York timezone and XNYS sessions. Closed sessions skip feed,
quotes and orders, including dry runs. Checks run before feed/per-item work,
after quotes before claiming, and in the before-submit callback. The process
stays alive and checks every 300 seconds; holidays, DST and early closes apply.
A close at the submission callback records a skipped permanent claim.
This supersedes the older observation below that these guards were absent.
Verified: all 25 tests in `tests/test_top_bullish.py` passed with the local venv,
including closed-market and close-during-submission cases. No broker calls made.

## Morning sell at 10 AM ET (2026-09-24)

`app/morning_sell.py` adds an optional worker that sells every bot-tracked
stock holding at market once per trading day at 10:00 AM New York time.
`MorningSellCalendar` uses `exchange-calendars` XNYS sessions (weekends,
holidays, DST and early closes handled); `MorningSellScheduler.run_once`
waits until the window, then per holding: (1) queries the SQLite ledger via
`Ledger.morning_sell_holdings()` — `scheduled_stock_exits` rows not complete
(account_id carried) plus `top_bullish_trades` rows with status 'submitted'
(account resolved from `BULLISH_STOCK_ACCOUNT_NUMBER`) — and (2) sells at
market through `StockExecution.market_sell` (SELL / MARKET / DAY / CORE),
the same order shape the scheduled-exit worker uses.

Per-symbol intent is reserved in `events` (`morning-sell:<date>:<account>:<symbol>`)
before the broker call, so restarts never double-sell; the sell quantity is the
broker-confirmed position capped at the tracked quantity. Sold rows are marked
complete/sold so the next-day exit worker skips them; if both workers are
enabled, whichever runs first wins. `MORNING_SELL_ENABLED` defaults false,
`MORNING_SELL_TIME=10:00`, `MORNING_SELL_TIMEZONE=America/New_York`,
`MORNING_SELL_POLL_SECONDS=60` (all in Settings and `.env.example`); the
FastAPI startup handler skips the worker when disabled or when `DRY_RUN=true`,
and refuses to start with a clear log when `BULLISH_STOCK_ACCOUNT_NUMBER` is
unset. `tests/test_morning_sell.py`: 8 passed (calendar, waiting, sell pass,
idempotency across restarts, zero-position skip, dry run). Full suite: 186
passed, 17 failed — all 17 pre-existing on the pristine tree (recorded bullish
market-hours interface). No broker orders placed.

## iOS trading API (2026-09-22)

`app/api_trading.py` adds a bearer-authenticated `/api/trading` surface for
the iOS companion client, mounted in `app/main.py`. `IOS_API_KEY` (new
`Settings` field, in `.env.example`, empty by default) gates all routes:
empty key returns 503, wrong/missing token returns 401 via
hmac.compare_digest. `get_settings` is imported lazily to avoid a main.py
import cycle. Endpoints: `GET /account` (balances and equity positions from
`account_v2.get_account_balance`/`get_account_position`, defensively
mapped), `GET /orders` (broker `order_v3.get_order_history` with ledger
`events` fallback for `manual:` fingerprints), `POST /orders/preview`
(validation only: symbol/quantity/price/notional_cap checks, reference
quote from `app/webull_quotes.py` with a 1200-second age tolerance for
delayed sandbox snapshots, market-open and position checks as warnings),
and `POST /orders` (requires `confirm: true`, re-runs blocking checks,
records a `manual:<client_order_id>` event). `DRY_RUN=true` records a
dry_run event without a broker call. Manual buys reuse the existing
`webull-buy-combo-stock.py` bracket submitter (entry plus configured
bullish stop/target); manual sells are single-leg NORMAL orders via
`order_v3.place_order` matching the scheduled-exit leg shape. Stocks only;
no option order support. README documents the endpoints and the
`uvicorn --host 0.0.0.0 --port 8000` LAN binding the iPhone needs. Broker
response shapes (balance fields, order history rows) are mapped
defensively and remain unverified against live sandbox responses; no
broker calls were made during implementation.

## Deployment service operations (2026-09-20)

The runbook also documents verbose SSH tunnel diagnostics, file logging with
`-E ~/ssh-tunnel.log`, and following systemd application logs from a Mac over SSH.

deployment.md now lists start, stop, status, recent-log and live-log commands
for the API systemd service. These commands run on the Oracle Linux VM, not
Oracle Cloud Shell. Ctrl+C exits live log viewing without stopping the service.

## Delayed iron-condor quotes for paper testing (2026-09-20)

IRON_CONDOR_QUOTE_MAX_AGE_SECONDS is a positive integer setting passed through
shared neutral submission to the executor's four-leg quote validation. Code
and .env.example default to 60 seconds; local .env uses 1200 for paper testing.
Every leg must pass; invalid/crossed prices, nonfinite timestamps and quotes
over five seconds in the future remain blocked. Age errors identify the leg,
age and limit. Bearish and stock limits remain independent. Restart services
after changing settings. Focused tests: 80 passed. No broker orders placed.

## Delayed PUT quotes for paper testing (2026-09-20)

BEARISH_QUOTE_MAX_AGE_SECONDS is a positive integer setting passed from shared
settings through bearish submission/executor to the option ask validator. Code
and .env.example default to 60; local .env is set to 1200 at user request for
delayed sandbox data. Five-second future tolerance and price/timestamp checks
remain unchanged. Stock and iron-condor quote age limits are unchanged. Restart
services to load the setting. No broker requests or orders placed.

## Listed bearish expiration selection (2026-09-16)

Shared bearish submission no longer requests today plus five days. The bracket
helper reads paginated PUT contracts and selects the earliest listed expiry
after today UTC, excluding same-day/expired dates, then the closest strike.
Explicit requested minima use exact-or-next-listed selection; no earlier-date
fallback. Empty chains skip. The pure resolver is shared in option_expiration.py
with webull-option-chain.py, whose chain loader no longer hardcodes CALL and
whose expiry candidates are type-filtered. main-option.py bearish submission
delegates to the shared submitter. CALL and iron-condor target-date policies
remain separate. Focused tests: 91 passed; no broker requests or orders placed.

## Bearish quote diagnostics (2026-09-16)

Option ask timestamp failures now distinguish stale age/limit, future offset
and nonfinite timestamps, with the contract symbol. The 60-second age limit
and five-second future tolerance are unchanged. Shared bearish submission logs
QuoteError and returns skipped, preserving the reason in the trade-idea ledger
instead of recording a generic failed broker submission. No order is sent.
Focused tests: 45 passed. The reported incident lacked the raw quote timestamp,
so its exact cause remains unverified; no live broker call was made.

## Options account selection (2026-09-16)

The main.py bullish stock path now requires BULLISH_STOCK_ACCOUNT_NUMBER in
.env/environment and resolves an exact unique account-number match through the
shared broker helper; missing/ambiguous matches block submission. The chosen
cash account value is only in local .env, not in this memory or example config.
Settings hide it from repr. TOP_BULLISH_ACCOUNT_NUMBER remains for the dedicated
bullish runner, OPTIONS_MARGIN_ACCOUNT_NUMBER for PUT/iron-condor execution.
Only the separate main-option.py CALL path retains first-account selection.
README, deployment.md and bullish-stock.md reflect the new routing. Restart
services after setting changes; no existing orders or reservations are moved.
No live account lookup or account-type/permission verification was performed.

Bearish PUT and iron-condor submissions now require OPTIONS_MARGIN_ACCOUNT_NUMBER
from .env/environment. Shared settings hide it from repr. The shared submitter
and main-option.py bearish branch use exact unique account-number lookup;
missing configuration, missing matches or ambiguous matches prevent submission,
without first-account fallback. The user’s
chosen value is saved only in local .env, not here or in example configuration.
Account type and permissions are not live-verified. Existing ledger reservations
are not reset by account changes. Mocked checks: 90 focused tests passed;
no broker orders placed. Restart running services to apply the setting.

## Strategy exit configuration (2026-09-16)

`app/strategy_settings.py` defines validated shared percentage settings inherited
by main.Settings, main-option.Settings and BullishSettings. `.env` and
`.env.example` now contain BULLISH_PROFIT_PERCENT=10,
BULLISH_STOP_LOSS_PERCENT=5, BEARISH_PROFIT_PERCENT=20,
BEARISH_STOP_LOSS_PERCENT=10, IRON_CONDOR_PROFIT_PERCENT=10 and
IRON_CONDOR_STOP_LOSS_PERCENT=5. Other .env values were preserved.
Main stock submission, bullish flow stock runner, separate CALL/PUT option
runner and shared neutral submitter pass the configured percentages to their
pricing/order builders. Low-level helper defaults remain available for direct
callers. All values must be finite and positive; long stop percentages and
condor profit must be below 100. Restart services after changes; existing
orders are not repriced. Process environment overrides .env. Tests with custom
percentages, dotenv loading/override and invalid values: 85 focused tests passed.
No broker orders placed.

## Neutral iron-condor submission (2026-09-16)

Neutral feed ideas now select iron_condor with internal action buy and require
positive finite levels with target < entry < stop. The shared submitter builds
an actual SELL LIMIT credit bracket; main-option.py's neutral polling/submission
also delegates to the shared path. ALLOW_SHORT_SELLING remains bearish-only.
The executor selects listed, standard OCC contracts with reported multiplier
100, one expiry in the 30–44 day window and equal wings. Four fresh bid/ask quotes
price the credit (short bids minus long asks). Entry rounds down to 0.05; exits
reverse all four legs as BUY combos at 90%/105% of entry credit, rounded to 0.05.
Entry is DAY, exits GTC. Quantity is one; maximum spread loss before fees must
fit MAX_NOTIONAL_USD and decision budget. Invalid data or over-budget setups skip.

Before submission, the existing events table atomically reserves
iron-condor:<fingerprint> and saves request/tracking IDs. Any prior reservation
blocks a replay, including timeout/failure, regardless of FORCE_REPROCESS.
Manual reconciliation is required; there is no automatic option fill/exit
reconciliation. Different trade IDs can still open overlapping positions.
Dry runs remain broker-free previews and do not validate quotes or reserve.
No environment values changed. No broker orders placed. Account permissions,
sandbox response fields and multi-leg bracket acceptance remain unverified.
`netural-iron-condor.md` contains the updated flow diagram and operating details.
Focused mocked suite: 74 passed. Full suite before final additional coverage:
130 passed, 17 failures in the already-recorded bullish market-hours interface.

## Bearish flow source verification (2026-09-16)

`bearish.md` documents the current main.py polling path with a Mermaid flowchart.
Current source supersedes older observations below: bearish `sell_short` routes
to `BearishPutOptionExecutor` and `buy_put_with_bracket` (BUY_TO_OPEN PUT), gated
by `ALLOW_SHORT_SELLING`. Feed levels require target < entry and stop > entry;
option exits use +20%/-10% premium brackets, one contract, DAY duration. This
branch does not register a scheduled stock exit. Neutral behavior is described above.
Polling and submission both check weekday 09:30–16:00 ET hours. Initial dedupe
now calls `has_ordered_trade`; when ID and symbol are supplied, both must match
an ordered row. Static source verification only; no broker orders were placed.


Bearish entries now use the selected PUT contract's current snapshot ask instead
of the strike-based premium estimate. The SDK option snapshot must match the
contract and supply a positive finite ask and quote_time within 60 seconds
(up to five seconds future tolerance). PUT selection filters option_type=PUT.
Missing/stale quotes prevent submission. Entry stays LIMIT, rounded to the
existing 0.05 tick; exits are based on that limit, not actual fills. Invalid or
collapsed rounded brackets are rejected. Both main and main-option bearish
callers use 20% profit/10% stop. Dry runs return before quotes. US Webull API docs
state MARKET option orders are unsupported. Focused mocked tests: 41 passed;
no broker orders placed, and live option quote availability remains unverified.
Removed the duplicate `*` parameter separator from
`IronCondorOptionExecutor.submit` (2026-09-16). Full app compilation and executor
import now pass; all submit arguments after self remain keyword-only. This
syntax fix was followed by the neutral implementation described above.

Last reviewed: 2026-09-05. Scheduler implementation is covered by local fake-broker
tests; live sandbox execution has not been validated.

## Purpose and stack

Python service that polls Optionomics trade ideas, builds deterministic trading
decisions, records them in SQLite, and prepares or submits Webull paper orders.
FastAPI supplies startup orchestration and a `/health` endpoint; this is a
polling service, not an incoming webhook application.

Python 3.12+, uv with `uv.lock`, FastAPI, Pydantic settings, python-dotenv, and
the Webull OpenAPI Python SDK. Development dependencies: pytest and Ruff.

## Source map

- `app/main.py`: FastAPI app, Settings, TradeIdea/TradingDecision models,
  polling thread, orchestration, risk helpers, and compatibility wrappers.
- `app/optionomics_client.py`: generic `fetch_json(api_url, user_email, api_key)`
  HTTP retrieval and environment loading. `fetch_trade_ideas` requires a caller-
  supplied `api_url` keyword and retains trade-idea response normalization.
  Both polling entry points pass `settings.optionomics_api_url`; `TopBullish`
  passes its flow URL to `fetch_json`. Successful 403 retries no longer raise
  the previous attempt's error; covered by mocked HTTP tests.
- `app/top-bullish.py`: standalone `TopBullish` client for `/api/v1/flow/bullish`.
  Run `uv run python app/top-bullish.py`; reads `OPTIONOMICS_EMAIL` and
  `OPTIONOMICS_API_KEY` from environment or `.env`, defaults to limit 10,
  and prints the original JSON response. Does not start trading workers.
  Reuses `optionomics_client.build_headers`: verified the bullish endpoint
  returns Cloudflare 1010/HTTP 403 with urllib's default user agent and HTTP 200
  with the existing client's browser headers (2026-09-13). Credentials are not logged.
- `app/optionomics.py`: feed model, confidence normalization, directional
  level validation, and decision builder returning dictionaries.
- `app/ledger.py`: SQLite schema, deduplication, status and audit persistence,
  scheduled exit jobs, and single-host worker exclusion.
- `app/exit_scheduler.py`: next-session calendar and persistent exit reconciliation.
- `app/stock_execution.py`: strict order/position queries, cancellation, and market sells.
- `app/webull_submitter.py`: dry-run response and broker submission adapter.
- `app/webull-buy-combo-stock.py`: dynamically loaded equity bracket helper
  used by `webull_submitter.py`.
- `app/webull-buy-combo-option.py`: option bracket helper used by
  `app/main-option.py`.
- `app/webull_broker.py`: shared sandbox client, account lookup, and order IDs.
- `app/webull-option-chain.py`, `app/webull-buy-option.py`,
  `app/webull-client.py`, `app/main-option.py`: additional scripts; inspect
  their callers before treating them as part of the active service.
- `tests/test_apply_risk_gates.py`: regression coverage for decisions, risk
  gates, deduplication, order parameters, and rate-limit handling.
- `puml/`: architecture diagram sources and an image.
- `README.md`: setup and design overview; some descriptions are stale.

## Configuration and workflow

Settings reads `.env` and process environment. Defaults in source:

| Variable | Default |
| --- | --- |
| `DRY_RUN` | `true` |
| `MAX_NOTIONAL_USD` | `250` |
| `ALLOW_SHORT_SELLING` | `false` |
| `FORCE_REPROCESS` | `false` |
| `OPTIONOMICS_POLL_ENABLED` | `true` |
| `OPTIONOMICS_POLL_INTERVAL_SECONDS` | `600` |
| `WEBULL_ENDPOINT` | `api.sandbox.webull.com` |
| `DATABASE_PATH` | `bot.sqlite3` |

Feed credentials use `OPTIONOMICS_API_KEY` and `OPTIONOMICS_EMAIL`; the URL
uses `OPTIONOMICS_API_URL`. Broker credentials use `WEBULL_APP_KEY` and
`WEBULL_APP_SECRET`. Do not copy actual `.env` values into documentation.

Run from the repository root:

```bash
uv sync
PYTHONPATH=. uv run fastapi dev app/main.py
```

Startup launches a daemon polling thread when feed credentials are present
and polling is enabled. For local development without feed polling, prefix
the launch command with `OPTIONOMICS_POLL_ENABLED=false DRY_RUN=true`.

Development checks, to run as appropriate for code changes:

```bash
OPTIONOMICS_POLL_ENABLED=false DRY_RUN=true PYTHONPATH=. uv run pytest -q
PYTHONPATH=. uv run python -m compileall app
uv run ruff check app tests
```

These commands are guidance, not a recorded passing baseline.

## Behavior to preserve and understand

The polling path fetches ideas, checks trade-ID deduplication, inserts a queued
row, builds a decision, checks the symbol, creates a TradeIdea, and calls
`maybe_submit_order`. Outcomes include skipped, dry_run, ordered, and failed.

SQLite tables are `events` and `optionomics_trade_ideas`. Any existing trade-ID
row counts as seen regardless of status. `FORCE_REPROCESS` bypasses the initial
seen check; a separate ordered trade-ID/symbol check remains before submission.
Do not accidentally deduplicate the current idea against its newly queued row.

The feed builder requires entry, target, and stop levels. Bullish levels require
target > entry and stop < entry; bearish and neutral levels require target <
entry and stop > entry. Bearish decisions skip when shorting is disabled.
Neutral or Crush pipeline ideas receive the `iron_condor` strategy label;
other strategies currently receive `placeholder`. Confidence is normalized
to the range 0–1.

Dry-run submission returns before loading the broker helper. Non-dry-run
submission currently calls `buy_stock` with quantity 1 and entry/stop/target
prices; that helper builds an equity bracket order.

User-specified order pricing: stop price is 5% below entry (`entry * 0.95`)
and target price is 10% above entry (`entry * 1.10`), rounded to two decimal
places. `submit_paper_order` calculates these from entry even when the feed
supplies different stop/target levels. Feed decision validation still uses
the original feed levels.

## Known discrepancies to verify before related changes

These are observations from static source review, not fixes or a test report:

- README describes option-contract selection in the active submission flow,
  but `webull_submitter.py` currently uses the equity `buy_stock` helper.
  An `iron_condor` decision label does not establish multi-leg execution.
- `apply_risk_gates` exists but is not called by the current polling path or
  `maybe_submit_order`; do not assume its checks protect that path.
- Submission uses quantity 1 instead of sizing from the notional budget and
  calls `buy_stock` even for a `sell_short` decision. Returned side metadata
  alone does not establish the broker's actual order direction.
- The rate-limit log promises a retry on the next poll, but failed rows are
  still considered seen by the initial dedupe check under default settings.
- Stock submission loads its helper directly through
  `app.webull_submitter._load_webull_stock_module`; tests patch that loader.
  The former recursive compatibility hook through `app.main` was removed.

## Local state and maintenance

`deployment.md` is the Oracle Always Free deployment runbook: VM/network/SSH
setup, private dashboard access, SQLite migration, systemd templates, staged
strategy activation and backup guidance. Templates are documentation only;
cloud installation and ARM compatibility have not been verified by deployment.
The runbook targets the user's Oracle Linux 9 image, using `opc`, `dnf`, and
`/home/opc` service paths; Python is installed with uv, not the system package manager.

`.env`, `.venv`, SQLite runtime state, logs, caches, and editor configuration
are local artifacts. Preserve existing runtime state during development and
use temporary databases for tests. No service or broker submission was started
to create this memory.

Keep this file focused on durable project facts and unresolved findings.
Update or remove findings after verification or fixes, recording relevant
validation without retaining a running transcript of every session.

## Scheduled stock exit implementation

User approved next-trading-day market exits. `NEXT_DAY_EXIT_ENABLED` defaults
false; `NEXT_DAY_EXIT_TIME=09:35`, `NEXT_DAY_EXIT_TIMEZONE=America/New_York`, and
`NEXT_DAY_EXIT_POLL_SECONDS=30`. `DRY_RUN=true` suppresses the worker and all
submission. Local `.env` values were preserved. See README for the runbook.

Uses `exchange-calendars` XNYS sessions and actual entry fill timestamps. The
worker persists bracket IDs before entry submission, derives a deadline after
confirmed fills, cancels outstanding entry/exit orders, confirms terminal states,
reconciles fills and holdings, and sells the tracked remainder at market. GTC
exit legs are requested only when scheduling is enabled; entry remains DAY.
No profit condition is imposed on the scheduled exit.

`ExitCalendar()` resolves omitted time/timezone arguments through `get_settings()`,
using `NEXT_DAY_EXIT_TIME` and `NEXT_DAY_EXIT_TIMEZONE` from `.env` or process
environment. Explicit constructor arguments override those configured values.

Jobs live in `scheduled_stock_exits`; state JSON retains IDs, deadline, quantities,
order snapshots, market attempts, and last error. One active job per account/symbol
is enforced by SQLite. A POSIX file lock beside the database excludes concurrent
entry submission and scheduler workers and releases on process death. Single-host
local SQLite is required. Existing positions/trades are not adopted automatically.

Scheduled entries require a flat stock position for the symbol. Unknown submission
results are reconciled by persisted client ID, never blindly replayed; if the ID
never becomes queryable, manual reconciliation is required. Confirmed terminal
partial exits can create a new market order for the remainder. Malformed or stale
responses and position shortfalls defer execution. Manual trades/corporate actions
can invalidate tracked ownership; use a dedicated paper account.

The polling model/dictionary boundary and Pydantic ledger serialization were fixed
as prerequisites. Submission fingerprints now use a stable trade-ID hash when
available. Scheduler tests use temporary databases and fake broker clients. Actual
sandbox GTC acceptance and response fields remain unverified; keep the scheduler
disabled until checked. No broker orders were placed during implementation.

## Trade status dashboard

`/records` provides a separate local read-only database browser linked from the
dashboard. `/api/records` supports the four application tables (events, trade
ideas, scheduled exits and bullish trades), created/updated time filtering,
and 50-row server pagination. Date filters require timezone offsets, include
the start and exclude the end; the UI uses browser-local time. Scheduled exits
have only updated time. The full stored row, including JSON payloads and any
account data therein, is visible in details, unlike the curated dashboard.
No authentication is added; this is intended for the existing localhost app.
Reads use SQLite mode=ro and never initialize missing databases. Tests cover
timezone boundaries, missing databases/columns, table validation and pagination.

`app/dashboard.py` serves a read-only dashboard at `/`, static CSS/JS from
`app/static/`, and a no-cache ledger snapshot at `/api/trades`. No frontend build
is required. The page has search/status/attention filters, 20-row pagination,
15-second optional refresh, and a details dialog. Trade-idea status and exit job
status remain distinct. Times display in New York time.

The snapshot reads SQLite in read-only mode in one transaction. It handles an
absent ledger or older schema without scheduler tables without initializing the
database. Jobs link by bracket entry ID or the stable trade-ID hash; never by
symbol alone. Unlinked jobs remain visible. Only selected fields are returned;
account IDs and raw feed payloads are omitted. The page shows saved observations,
not live broker state. Completed jobs can include unfilled cancelled entries.

Dashboard routes have no authentication; use the existing app locally. A UI-only
preview can use `uvicorn app.main:app --host 127.0.0.1 --port 8001 --lifespan off`
without starting trading workers. Configuration badges do not prove a worker is
running. Tests cover status pairing, orphan jobs, old/missing/corrupt databases,
read-only access, and withholding account/raw payload fields.

## Unconfirmed entry lookup handling (2026-09-07)

A pasted SDK HTTP 417 `OPENAPI_PARAM_ERR / Order not present` referred to a saved
master BUY. Read-only ledger inspection found a failed idea, a waiting_entry job,
no broker receipt, no recorded fill, and no scheduled exit date. The original
submission failure reason was not retained, so this does not establish whether
Webull rejected the entry or the submission result was otherwise ambiguous.

`StockExecution` now recognizes this specific missing-order error from either SDK
exceptions or HTTP responses. The scheduler preserves the unresolved job, performs
no new sale, and backs off lookups from 60 seconds to a maximum 15 minutes with a
persisted next_check_at. Successful reconciliation clears the backoff. Missing
orders are never treated as cancelled, unfilled, or complete. Future entry
submission exceptions are retained on the exit job for troubleshooting; the UI
shows that error and the next lookup time. No existing jobs were edited or deleted.

Shared SDK logging now uses INFO, disables propagation to the root logger, and
replaces core client ERROR dumps (which can contain signed request headers) with
a short message. Application errors retain the actionable reason.

## Bullish flow stock runner

`bullish-stock.md` documents the dedicated runner end to end, with a Mermaid diagram,
account lookup, configured exits, permanent claims and submission failure states.
Source review confirms no current market-hours gate; total_premium float coercion
accepts booleans, while trade_count rejects them. Documentation-only update.

Bullish live submission requires `TOP_BULLISH_ACCOUNT_NUMBER` in environment
or `.env`. Before claiming a symbol, the runner resolves an exact, unique
`account_number` match from the sandbox account list to the API `account_id`.
Missing/ambiguous matches stop submission without falling back to the first
account. Other callers of `get_account_id()` retain first-account selection.
Changing accounts does not reset the bullish ledger's symbol deduplication.
`main.py.Settings` also accepts `TOP_BULLISH_ACCOUNT_NUMBER` from the shared
`.env` so FastAPI startup does not fail with `extra_forbidden`. This field
does not change main.py's account selection; other unknown settings remain
rejected. Account numbers are excluded from this field's model repr.

`app/main-top-bullish.py` provides `MainTopBullish.run(limit=10)`, a single-scan
method loading `TopBullish` from `app/top-bullish.py` and calling the existing
`webull-buy-combo-stock.py` helper. Each feed entry uses a Webull snapshot price
for a one-share LIMIT entry, a 5% stop and 10% target (two decimal places), with
DAY exits. Entries above `MAX_NOTIONAL_USD` are skipped. It does not start the
existing polling service or next-day exit worker.

The CLI now runs immediately and every 300 seconds until Ctrl+C; `--once`
preserves one-shot execution. `run_forever` uses monotonic deadlines, skips
missed ticks without overlapping scans, and continues after scan exceptions.
Existing permanent symbol deduplication still applies across cycles. The
feed-only `app/top-bullish.py` still fetches once. Scheduler checks use mocked
time/feed calls, without starting a broker-connected scheduler.
Scheduler logs now show cycle numbers, completion status counts and the next
scan's local timestamp with UTC offset. Simulated cycles verify repeat execution,
recovery after a scan exception and skipping missed ticks. Ledger updates alone
do not establish polling activity: duplicate-only scans do not update rows.

The previously implemented bullish market-hours checks are absent from the
current source (verified 2026-09-15); their tests remain and fail. The following
describes that earlier implementation, not current protection:
Bullish scans (including dry runs) required an open XNYS regular session,
using the existing `ExitCalendar` with explicit settings. Holidays, weekends,
early closes and DST are respected. Checks precede feed/quote requests and
ledger claims, with a final check in the broker's before-submit callback.
Closed-market results use `outside_market_hours`; the five-minute loop stays
running. A close before claim leaves the symbol retryable; a close in the final
callback records a skipped claim, subject to existing permanent deduplication.
`main.py` still uses its weekday 09:30–16:00 check and does not handle holidays
or early closes. Its `apply_risk_gates` remains unused by its polling path.
Bullish validation also rejects boolean premiums and nonfinite bracket prices.

`app/webull_quotes.py` uses the installed SDK's `DataClient.market_data.get_snapshot`
and validates symbol, positive finite price, and `last_trade_time` (milliseconds);
quotes older than five minutes or over five seconds in the future are rejected.
`webull_broker.py` shares a cached signed sandbox API client between trade/data
clients. Live quote permissions and sandbox snapshot behavior remain unverified.

`BullishLedger` creates `top_bullish_trades` in `DATABASE_PATH` on initialization.
It stores feed metrics/payload, quote, order parameters, status, broker tracking
IDs/response, exception type and timestamps. Atomic unique trade-ID and symbol
claims precede broker calls; tracking IDs are persisted through `before_submit`.
Feed entries without an ID use `bullish:SYMBOL`. Deduplication is permanent within
this table, including dry runs, failed attempts and unknown submissions; it does
not check other strategies' tables or current broker positions. Quote/budget
skips are returned without reserving a symbol, allowing later valid attempts.
Unknown submissions require manual reconciliation and are never auto-retried.

Bullish runner logs quote/order failures with symbol and SDK HTTP status, error
code and message, also included in final JSON. `app/webull_errors.py` formats
only selected SDK exception fields and redacts configured secrets; raw SDK
request logging remains suppressed. Unexpected exceptions retain type-only
reporting. Non-200 quote responses expose selected error fields as well.

Preview with `DRY_RUN=true DATABASE_PATH=/tmp/bullish-preview.sqlite3 uv run python
app/main-top-bullish.py --limit 10`. The preview uses real feed/quote requests but
never submits orders; a separate database avoids reserving production symbols.
`DRY_RUN=false` enables sandbox submission. Verified with temporary databases and
mocked feed/quote/broker calls: full suite 84 passed; no broker orders placed.

SE read-only sandbox snapshot check on 2026-09-13 returned a quote about 38 hours
old, rejected by the five-minute freshness limit. `QuoteError` now exposes safe
validation details (including age/limit) in runner skip reasons; unexpected SDK
exceptions still show only their type. No freshness limit was relaxed or order
submitted during diagnosis.
