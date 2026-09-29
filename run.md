# Running live or sandbox trading

Run commands from the repository root. Put one configuration combination below
in `.env`. Keep the existing feed credentials and strategy settings you need.
Process environment variables override `.env` values.

## Live trading — real orders

```ini
WEBULL_TRADING_MODE=live
WEBULL_ENDPOINT=api.webull.com
DRY_RUN=false
DATABASE_PATH=bot-live.sqlite3

WEBULL_APP_KEY=your-production-app-key
WEBULL_APP_SECRET=your-production-app-secret

WEBULL_LIVE_BULLISH_STOCK_ACCOUNT_NUMBER=your-live-stock-account-number
WEBULL_LIVE_TOP_BULLISH_ACCOUNT_NUMBER=your-live-bullish-runner-account-number
WEBULL_LIVE_OPTIONS_MARGIN_ACCOUNT_NUMBER=your-live-options-account-number
WEBULL_LIVE_BULLISH_AMOUNT_USD=100
```

This selects production only. Fill in the live account fields used by your
enabled strategies; live mode never falls back to paper accounts. Automated
bullish stock entries request a $100 total cash budget, split into eligible
fractional orders as needed. Options still use whole contracts.

Live bullish profit/stop exits are managed by the application. Keep the service
or continuous bullish runner running to monitor these positions. See
[README.md](README.md#live-bullish-stock-cash-orders) for execution limits.

## Paper trading — sandbox orders

```ini
WEBULL_TRADING_MODE=paper
WEBULL_ENDPOINT=api.sandbox.webull.com
DRY_RUN=false
DATABASE_PATH=bot.sqlite3

WEBULL_APP_KEY=your-sandbox-app-key
WEBULL_APP_SECRET=your-sandbox-app-secret

WEBULL_PAPER_BULLISH_STOCK_ACCOUNT_NUMBER=your-paper-stock-account-number
WEBULL_PAPER_TOP_BULLISH_ACCOUNT_NUMBER=your-paper-bullish-runner-account-number
WEBULL_PAPER_OPTIONS_MARGIN_ACCOUNT_NUMBER=your-paper-options-account-number
```

This submits simulated orders to Webull's sandbox using the existing paper
strategy behavior. `DRY_RUN=false` enables submission to the selected environment;
it does not select live trading by itself.

## Preview without submitting orders

Use either environment combination above and change:

```ini
DRY_RUN=true
```

Application order submission is disabled. Some preview paths still fetch feed,
account or quote data and write ledger records. Use a separate preview database
to avoid reserving symbols in a trading ledger:

```ini
DATABASE_PATH=bot-preview.sqlite3
```

Standalone low-level broker order examples do not implement the application's
dry-run gate. Use the application commands below.

## Start the application

Install dependencies:

```bash
uv sync
```

Run the main feed service, API and configured exit workers:

```bash
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Or run the dedicated bullish-feed strategy continuously:

```bash
uv run python -m app.bullish.runner
```

These are separate entry strategies, not equivalent launch commands. Main feed
polling requires feed credentials and `OPTIONOMICS_POLL_ENABLED=true`. The
dedicated runner polls the bullish feed. Live non-dry-run `--once` is rejected
because managed exits require continuous execution.

## Switch environments

1. Stop every existing API/runner process or service using the old configuration.
2. Update the mode, endpoint, credentials, account fields and database path in `.env`.
3. Restart the desired application processes.

Clients and settings are cached per process. Editing `.env` does not switch or
stop a running worker. A mode/endpoint mismatch is rejected. Keep live and paper
databases separate: saved orders, reservations and exit jobs are not namespaced
by environment. Switching modes does not close positions or cancel existing
orders; stopping live monitoring also stops its app-managed exits.

Keep infrastructure-only variables such as `TFE_API_TOKEN` outside the shared
application `.env`; strict application settings reject unrecognized keys.

## Daily live entry budgets

```ini
# Requested cash per automated bullish stock purchase
WEBULL_LIVE_BULLISH_AMOUNT_USD=100
# Total reserved for automated live bullish stock buys each New York date
WEBULL_LIVE_BULLISH_DAILY_LIMIT_USD=100
# Separate options budget; 0 disables the options cap. Set your desired amount.
WEBULL_LIVE_OPTIONS_DAILY_LIMIT_USD=0
```

The default stock settings allow one $100 purchase plan per day across both
bullish entry processes and all stock accounts using the same DATABASE_PATH.
For several smaller buys, reduce the per-purchase amount while retaining the
$100 daily cap. A plan that exceeds the remaining budget is skipped, not resized.
Setting the stock daily cap to 0 blocks new automated live bullish stock buys.

For example, `WEBULL_LIVE_OPTIONS_DAILY_LIMIT_USD=500` enables a separate $500
options cap. Long CALL/PUT entries reserve limit premium × contracts × 100;
iron condors reserve their calculated maximum defined loss. This is an entry
commitment limit, not a realized-loss, cash-debit or fees-inclusive limit.

Reservations are atomic and stored in SQLite's `daily_entry_budgets` table before
broker submission. They survive restarts and count pending, failed, interrupted
and partially filled plans conservatively; unused money is not automatically
refunded. Sells do not consume budget or replenish it. New daily allowance starts
at midnight America/New_York (including DST), based on submission date rather
than fill date. Existing trades placed before this feature are not backfilled.
Paper trading and manual stock tickets are outside these caps. All related
workers must share one database; separate databases have separate allowances.
Restart processes after changing limits.

## Live bearish stop-loss toggle

```ini
WEBULL_LIVE_BEARISH_STOP_LOSS_ENABLED=false
```

This omits the stop-loss leg from new live bearish PUT entries while retaining
the configured take-profit LIMIT leg. Paper bearish entries retain both exit
legs. The code default and `.env.example` use `true`; set `true` to restore stops
for future live PUT entries. Restart application/runner processes after changing
this setting. Existing orders are not cancelled or modified, and the profit
leg's existing time-in-force is unchanged. Bullish and iron-condor exits are
unaffected. A disabled PUT stop is not replaced by an app-managed stop.
