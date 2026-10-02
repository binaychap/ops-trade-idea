# Live and paper trading: end-to-end behavior

Source review: September 30, 2026. This guide describes the current repository,
not a guarantee that Webull will accept or fill an order. Review used source code
and automated tests; no broker requests, live orders, or service restarts were made.
Configuration examples contain no credentials or account identifiers.

## 1. Processes and execution paths

| Entry point | Purpose | Bullish behavior |
| --- | --- | --- |
| `uv run uvicorn app.main:app --host 127.0.0.1 --port 8000` | Dashboard, trading API, Optionomics trade-idea polling, enabled exit workers | Paper stock brackets; live cash stock purchases |
| `uv run python -m app.bullish.runner` | Separate bullish-flow feed runner, normally every 300 seconds | Paper one-share stock brackets; live cash stock purchases |
| `app/options/runner.py` | Alternate option-oriented path | CALL contracts, not the live cash stock strategy |
| `/api/trading/orders` | Explicit manual stock tickets | Integer-quantity orders, not automatic $100 cash entries |
| Standalone broker/order scripts | Low-level utilities | Inspect each script before use; application dry-run protection is not universal |

The main application does not automatically start the separate bullish-flow runner.
Running both introduces two entry sources. They must use the intended accounts and
same live database if their daily budget and exit ownership are to be shared.
The function name `submit_paper_order` is historical: the shared implementation
routes both paper and live orders.

## 2. Mode, credentials, accounts and database

| Setting | Paper | Live |
| --- | --- | --- |
| `WEBULL_TRADING_MODE` | `paper` | `live` |
| `WEBULL_ENDPOINT` | `api.sandbox.webull.com` | `api.webull.com` |
| Credentials | `WEBULL_PAPER_APP_KEY`, `WEBULL_PAPER_APP_SECRET` | `WEBULL_LIVE_APP_KEY`, `WEBULL_LIVE_APP_SECRET` |
| Suggested database | `bot.sqlite3` | `bot-live.sqlite3` |
| `DRY_RUN=false` | Submit sandbox orders | Submit production orders |
| `DRY_RUN=true` | Suppress application order submission | Suppress application order submission |

A blank endpoint is resolved from mode; a conflicting explicit endpoint is rejected.
Live requires its own credential pair and never falls back to legacy credentials.
Paper uses `WEBULL_APP_KEY` / `WEBULL_APP_SECRET` only when both paper-specific
fields are blank. An incomplete selected pair is rejected.

Account fields use the prefix `WEBULL_PAPER_` or `WEBULL_LIVE_`:

| Suffix | Consumer |
| --- | --- |
| `BULLISH_STOCK_ACCOUNT_NUMBER` | Main bullish stock path, manual API and morning stock sells |
| `TOP_BULLISH_ACCOUNT_NUMBER` | Dedicated bullish-flow runner |
| `OPTIONS_MARGIN_ACCOUNT_NUMBER` | PUTs, iron condors and alternate live CALLs |

Account numbers are resolved to broker account IDs. Main strategy paths require
explicit matching accounts. Paper can use legacy account fields; live cannot.
The alternate paper CALL path retains first-account selection.
Credentials authorize API access; account numbers select where trades go.

Settings and SDK clients are cached. Restart each process after changing `.env`.
Exported shell variables override `.env`. Keep environments in separate databases:
ledger rows are not universally namespaced by mode. Starting a live process does
not stop a separately running paper process.

Source: [environment settings](app/config/webull.py),
[strategy settings](app/config/strategy.py), [clients](app/broker/client.py).

## 3. Main trade-idea lifecycle

1. Startup enables polling when configured and feed credentials are present.
2. Every `OPTIONOMICS_POLL_INTERVAL_SECONDS` (default 600), the poller checks
   Monday–Friday, 09:30–16:00 America/New_York before fetching ideas.
3. Each idea is checked against previously ordered trade records, saved as queued,
   and converted into a decision. Symbol and directional entry/target/stop levels
   are validated. Bearish ideas require `ALLOW_SHORT_SELLING=true` even though
   their execution buys PUTs rather than short-selling stock.
4. Neutral or Crush pipeline ideas receive the `iron_condor` strategy label.
   In live mode the condor disable gate checks both that label and neutral direction.
5. Submission rechecks ordered records and market hours, then dispatches to the
   shared submitter. Dry runs return previews; enabled real paths call the broker.
6. SQLite records `skipped`, `dry_run`, `ordered`, or `failed`. `ordered` indicates
   submission bookkeeping, not proof of a completed fill.

The main market-hours helper is a weekday/time check, not a holiday/early-close
calendar. The live cash helper, dedicated bullish runner and exit calendar use
XNYS sessions. Thus not every route has identical calendar protection.

Current main polling checks **ordered** records, not every previously seen row.
Skipped/failed ideas may be reconsidered. However, cash and condor reservations
can still block replay. `FORCE_REPROCESS` bypasses the outer check only; the
submission helper still checks ordered records. Do not use it as a replay guarantee.
`apply_risk_gates` exists but is not invoked by this main polling/submission path;
its presence does not establish universal risk enforcement.

Source: [application](app/main.py), [feed decisions](app/feeds/decisions.py),
[submitter](app/execution/submitter.py), [ledger](app/persistence/ledger.py).

## 4. Strategy comparison

| Strategy | Paper | Live |
| --- | --- | --- |
| Main bullish stock | One-share bracket, using feed/reference entry; LIMIT unless market execution requested | Configured cash amount, default $100; MARKET/DAY/CORE entries |
| Dedicated bullish stock runner | Fresh quote supplies one-share LIMIT entry and percentage bracket prices | Same live cash helper as main stock path |
| Bearish | One long PUT contract with profit and stop legs | One long PUT; profit retained; stop controlled by live flag |
| Neutral / iron condor | Validated four-leg option strategy with closing profit/stop combinations | Skipped by default; opt-in via live condor flag |
| Alternate bullish option runner | One CALL contract | One CALL contract; separate from stock cash amount/cap |
| Manual stock API | Requested integer quantity | Requested integer quantity; not automatically converted to $100 |

Options use whole contracts. The automatic main PUT path selects a listed future
expiration and a strike near its reference, uses an option ask snapshot to price
entry, and computes percentage exits from entry premium. The alternate CALL path
still estimates entry premium as `max(selected_strike * 0.08, 1.0)`; it does not
use the live stock cash helper or guarantee a fresh option-ask entry price.

Iron-condor construction checks contracts, quotes, credit, wing/risk constraints
and uses a quantity of one in the main path. Entry credit and closing debit levels
are persisted with order identifiers before submission. The main path requests GTC
closing legs. Actual broker combo support/acceptance remains an external dependency.

Source: [PUT executor](app/bearish/executor.py),
[option builders](app/options/brackets.py), [condor executor](app/ironcondor/executor.py),
[alternate options](app/options/runner.py), [stock brackets](app/bullish/stock_bracket.py).

## 5. Live bullish cash purchase lifecycle

1. Validate configured cash amount: at least $5, cent precision, within the configured
   per-order notional maximum and applicable decision budget.
2. A dry run returns a preview without quoting or reserving a live cash budget.
3. For actual submission, require an open XNYS session and a stock quote no older
   than 60 seconds.
4. Split the cash plan into cent-exact orders each at least $5 and strictly below
   one quoted share's price. This is a constraint implemented by this application.
5. Resolve the configured live account. Require no existing position or active
   exit job for that account/symbol; acquire the ledger worker lock.
6. Reserve the full planned daily amount and a permanent entry event. Save the
   live cash exit job and order identifiers before broker submission.
7. Submit individual BUY / MARKET / DAY / CORE / AMOUNT orders, persisting progress.
   Stop on failure or market closure. Never blindly replay ambiguous attempts.
8. The exit worker reconciles attempted orders and aggregates actual fills.

For a $100 plan:

| Quoted share price | Planned cash orders |
| --- | --- |
| $200 | One $100 order |
| $100 | Two $50 orders |
| $30 | Four $25 orders |
| $5 or below | Skip: split cannot satisfy the implemented minimum/size rules |

The plan totals $100; fills, failures, partial execution and fees mean actual spend
is not guaranteed to equal that amount. Prices can change after the quote; this
split is not a guarantee of broker acceptance. Quote failure prevents entry.

The dedicated runner also permanently claims trade IDs/symbols in its own table.
Its failed or dry-run claims can prevent subsequent attempts for that symbol.
Live `--once` is allowed; order status and position exits are handled manually.

Source: [live cash entries](app/execution/live_stock.py),
[bullish runner](app/bullish/runner.py), [quotes](app/broker/quotes.py).

## 6. Live cash stock exits

Live cash stock buys are market orders without app-managed profit, stop-loss,
next-day, or morning exits. The live-cash status worker has been removed. The app
does not poll order status, cancel partially filled buys, or submit sells. Check
ambiguous submissions and open orders manually in Webull; sell positions there
as well. Cash intent records remain for audit and the optional morning-sell path
skips tracked cash holdings. Existing broker orders and saved database rows are
not cancelled or deleted by this change.

### Paper stock brackets and scheduled exits

Stock brackets request entry plus broker take-profit and stop-loss legs. Main paper
stock entries with `NEXT_DAY_EXIT_ENABLED=true` additionally persist an exit job,
require a flat starting position and request GTC bracket exits. The next-day worker
uses fills and the exchange calendar to determine the next trading-day deadline,
cancels outstanding orders, reconciles them, and sells the remaining tracked shares.
Without the scheduler, stock bracket exits use their builder's default DAY duration.
Do not assume the dedicated paper bullish runner registers the same timed exit jobs.

### Options and morning liquidation

`WEBULL_LIVE_BEARISH_STOP_LOSS_ENABLED=false` omits the stop from **new live PUT**
orders while keeping profit. Default is true; paper always retains the stop.
There is no replacement app-managed PUT stop. Existing broker orders are unchanged.

`WEBULL_LIVE_IRON_CONDOR_ENABLED=false` skips new live condors entirely; default false.
True restores their original profit/stop construction. Paper ignores this live flag.

`MORNING_SELL_ENABLED` defaults false. In live mode, the application does not
start this worker; live stock positions are manually managed in Webull. In paper
mode, when enabled, it targets equity holdings in the configured main stock
account, including manual/untracked holdings. It skips active `live_cash` records.
The legacy paper direct-sale path has weaker reconciliation: it does not cancel
all bracket exits or confirm fills before marking records complete.

Source: [next-day exits](app/exits/next_day.py), [morning sells](app/exits/morning_sell.py),
[stock broker adapter](app/broker/stocks.py).

## 7. Daily limits

| Setting | Default | Meaning of zero |
| --- | --- | --- |
| `WEBULL_LIVE_BULLISH_AMOUNT_USD` | 100 | Invalid; minimum $5 |
| `WEBULL_LIVE_BULLISH_DAILY_LIMIT_USD` | 100 | Blocks automatic live cash entries |
| `WEBULL_LIVE_OPTIONS_DAILY_LIMIT_USD` | 0 | Disables this options cap |

Stock plans reserve full requested cash. Long options reserve limit premium ×
contracts × 100; condors reserve defined maximum loss. These are separate buckets.
A $1,000 stock cap with $100 plans permits at most ten full reservations that date.
An order exceeding the remaining allowance is skipped rather than resized.

Reservations use SQLite `BEGIN IMMEDIATE` and the New York submission date.
At midnight, new submissions sum the new date's rows: no scheduler resets a counter.
Restarting does not clear reservations. Failed or partial orders are not automatically
refunded; selling does not replenish the allowance. Exits continue after the cap.
Prior trades from before this feature are not backfilled.

Budgets apply across accounts sharing the same database, not across separate files
or hosts. Manual stock API orders do not consume the automatic stock budget.
Option builders read the process environment; keep it consistent with app settings.

Source: [daily budget implementation](app/execution/daily_budget.py).

## 8. Manual API, dashboard and persistence

`/api/trading/*` requires `IOS_API_KEY` bearer authentication. The manual API previews
and validates integer stock quantities, notional constraints, market state and
positions. Actual submission requires confirmation and a persisted intent. Buys use
stock brackets; sells use explicit orders. It is not the automated cash strategy.
Preview can contact broker services even when actual submission is dry-run.

`/api/trades` reads local SQLite, not live broker state. Dashboard HTTP 200 confirms
that route works, not broker authentication or successful order fills. The dashboard
loads immediately, then uses `DASHBOARD_REFRESH_INTERVAL_SECONDS` (default 3600).
It refreshes automatically only when enabled and visible; returning to a tab refreshes
when due. Zero is not accepted. This timer does not change feed/exit polling.

Main persistence includes `optionomics_trade_ideas`, `events`,
`scheduled_stock_exits`, `top_bullish_trades` and `daily_entry_budgets`.
An unfinished cash job means persisted work to reconcile, not necessarily a filled
position. Conversely, no cash jobs does not prove the broker account is empty.
Exit locks are single-host file locks; this is not a distributed execution design.
The dashboard/records routes lack the manual trading API's bearer protection.

Source: [manual API](app/api/trading.py), [dashboard](app/ui/dashboard.py),
[browser refresh](app/ui/static/dashboard.js), [persistence](app/persistence/ledger.py).

## 9. Operating and verifying

Use [run.md](run.md) for full environment combinations and the read-only live account
test. Use [deployment.md](deployment.md) for service commands and logs.

Before switching modes, stop the old process, select matching credentials/accounts
and the intended database, then restart. Deploy source changes alongside new `.env`
fields; strict settings reject unknown keys on an older deployment.

The SDK may request token verification during client initialization. Account-list
HTTP 200 proves that read request succeeded, not order permissions or market-data
entitlements. A quiet startup with no pending cash jobs is not a connection test.
Never include credentials, saved token contents or private payloads in shared logs.

## 10. Review findings and validation scope

Important limitations confirmed in this review:

- Market calendars and sizing controls differ by entry point; no universal guarantee
  follows from a helper existing elsewhere in the repository.
- Main paper stock quantity and main PUT quantity remain one; a decision notional
  value is not evidence that every execution route sizes against that value.
- Legacy stock/PUT submission paths do not provide the same persistent ambiguous
  submission protection as live cash and condor paths. A failed response can require
  broker reconciliation before allowing another attempt.
- The alternate CALL premium heuristic and legacy morning-sell reconciliation remain
  as described above; this documentation review did not change trading behavior.
- A market quote is needed for live cash splitting and price-triggered exits, while
  actual fill information comes from the trading API. Neither replaces the other.
- Broker acceptance, entitlement, fractional fill response compatibility and actual
  execution outcomes were not verified by this source review.

Automated validation for this review is recorded in MEMORY.md. Tests use mocked
broker interfaces and isolated configuration; they do not certify live execution.
