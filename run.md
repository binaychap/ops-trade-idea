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
