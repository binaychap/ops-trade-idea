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

WEBULL_LIVE_APP_KEY=your-production-app-key
WEBULL_LIVE_APP_SECRET=your-production-app-secret

WEBULL_LIVE_BULLISH_STOCK_ACCOUNT_NUMBER=your-live-stock-account-number
WEBULL_LIVE_TOP_BULLISH_ACCOUNT_NUMBER=your-live-bullish-runner-account-number
WEBULL_LIVE_OPTIONS_MARGIN_ACCOUNT_NUMBER=your-live-options-account-number

# Live automated bullish stock sizing and daily allowance
WEBULL_LIVE_BULLISH_AMOUNT_USD=100
WEBULL_LIVE_BULLISH_DAILY_LIMIT_USD=1000

# Independent options allowance: 0 disables this cap
WEBULL_LIVE_OPTIONS_DAILY_LIMIT_USD=0

# Live strategy switches
WEBULL_LIVE_BEARISH_STOP_LOSS_ENABLED=false
WEBULL_LIVE_IRON_CONDOR_ENABLED=false
```

The example above uses your requested $100 per-stock amount and $1,000 daily
stock cap. These are example configuration values; the code's default stock
daily cap is $100. Credentials and account numbers are placeholders.

### Live flags and settings reference

| Setting                                            | Meaning                                                                                                        | Code default  |
| -------------------------------------------------- | -------------------------------------------------------------------------------------------------------------- | ------------- |
| `WEBULL_TRADING_MODE`                              | `live` selects production; `paper` selects sandbox.                                                            | `paper`       |
| `WEBULL_ENDPOINT`                                  | Must match the mode; blank selects the matching host automatically.                                            | Automatic     |
| `DRY_RUN`                                          | `true` prevents application submissions; `false` permits orders in the selected environment.                   | `true`        |
| `DATABASE_PATH`                                    | Use a separate live ledger, shared by all live workers that share a daily budget.                              | `bot.sqlite3` |
| `WEBULL_LIVE_APP_KEY` / `WEBULL_LIVE_APP_SECRET`   | Production credentials, required for live broker access.                                                       | Unset         |
| `WEBULL_PAPER_APP_KEY` / `WEBULL_PAPER_APP_SECRET` | Sandbox credentials; blank pair uses legacy `WEBULL_APP_KEY` / `WEBULL_APP_SECRET`.                            | Unset         |
| `WEBULL_LIVE_BULLISH_STOCK_ACCOUNT_NUMBER`         | Live account for main bullish stock entries, manual stock API and morning sells.                               | Empty         |
| `WEBULL_LIVE_TOP_BULLISH_ACCOUNT_NUMBER`           | Live account for the dedicated bullish runner.                                                                 | Empty         |
| `WEBULL_LIVE_OPTIONS_MARGIN_ACCOUNT_NUMBER`        | Live options account for enabled option strategies.                                                            | Empty         |
| `WEBULL_LIVE_BULLISH_AMOUNT_USD`                   | Requested cash per automated bullish stock purchase plan; minimum $5.                                          | `100`         |
| `WEBULL_LIVE_BULLISH_DAILY_LIMIT_USD`              | Daily stock-entry allowance; `0` blocks new automated live bullish stock buys.                                 | `100`         |
| `WEBULL_LIVE_OPTIONS_DAILY_LIMIT_USD`              | Independent daily options-entry allowance; `0` disables this cap.                                              | `0`           |
| `WEBULL_LIVE_BEARISH_STOP_LOSS_ENABLED`            | `false` omits new live PUT stop-loss legs while keeping profit legs; `true` includes stops.                    | `true`        |
| `WEBULL_LIVE_IRON_CONDOR_ENABLED`                  | `false` skips live neutral/IV Crush iron-condor entries; `true` permits the existing profit/stop bracket path. | `false`       |

The `WEBULL_LIVE_*` strategy switches and budgets apply only to live trading.
Sandbox retains its existing entry and bracket behavior. Setting the options
budget to a positive value does not re-enable iron condors when their switch is
false. Disabling bearish stops does not disable bearish entries: those remain
gated separately by `ALLOW_SHORT_SELLING=true`. That existing setting is required
for bearish PUT entry decisions even though the broker buys a PUT.

Live bearish PUT entries use a market order, so the premium is unknown until
Webull reports the fill. To avoid bypassing a configured dollar cap, the bearish
entry path skips while `WEBULL_LIVE_OPTIONS_DAILY_LIMIT_USD` is positive; set it
to `0` to disable that cap before enabling this market-entry flow. The app then
uses the actual fill price to submit broker-held GTC profit and stop exits.

This selects production only. Fill in the live account fields used by your
enabled strategies; live mode never falls back to paper accounts. Automated
bullish stock entries request a $100 total cash budget, split into eligible
fractional orders as needed. Options still use whole contracts.

Live bullish stock buys are market cash orders with no application-managed
profit, stop-loss, or next-day exit. The worker reconciles pending buy orders and
any sell orders that were already submitted. Monitor and close positions
yourself. See [README.md](README.md#live-bullish-stock-cash-orders) for execution
limits.

## Paper trading — sandbox orders

```ini
WEBULL_TRADING_MODE=paper
WEBULL_ENDPOINT=api.sandbox.webull.com
DRY_RUN=false
DATABASE_PATH=bot.sqlite3

WEBULL_PAPER_APP_KEY=your-sandbox-app-key
WEBULL_PAPER_APP_SECRET=your-sandbox-app-secret

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

Run the main feed service and API:

```bash
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Or run the dedicated bullish-feed strategy continuously:

```bash
uv run python -m app.bullish.runner
```

These are separate entry strategies, not equivalent launch commands. Main feed
polling requires feed credentials and `OPTIONOMICS_POLL_ENABLED=true`. The
dedicated runner polls the bullish feed. Live `--once` runs one scan and exits;
check order status and manage sells manually in Webull.

## Switch environments

1. Stop every existing API/runner process or service using the old configuration.
2. Update the mode, endpoint, credentials, account fields and database path in `.env`.
3. Restart the desired application processes.

Clients and settings are cached per process. Editing `.env` does not switch or
stop a running worker. A mode/endpoint mismatch is rejected. Keep live and paper
databases separate: saved orders, reservations and exit jobs are not namespaced
by environment. Switching modes does not close positions or cancel existing
orders. Live cash positions have no app-managed exits and must be closed manually.

Keep infrastructure-only variables such as `TFE_API_TOKEN` outside the shared
application `.env`; strict application settings reject unrecognized keys.

## Daily live entry budgets

```ini
# Requested cash per automated bullish stock purchase
WEBULL_LIVE_BULLISH_AMOUNT_USD=100
# Total reserved for automated live bullish stock buys each New York date
WEBULL_LIVE_BULLISH_DAILY_LIMIT_USD=1000
# Separate options budget; 0 disables the options cap. Set your desired amount.
WEBULL_LIVE_OPTIONS_DAILY_LIMIT_USD=0
```

The example allows up to ten $100 purchase plans per day across both bullish
entry processes and all stock accounts using the same DATABASE_PATH. The code
default of a $100 daily cap allows one such plan. Reduce the per-purchase amount
if you want more, smaller purchases within your chosen daily allowance. A plan that exceeds the remaining budget is skipped, not resized.
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
than fill date. No reset scheduler deletes or clears records: before each buy,
the app sums reservations for the current New York date. If the allowance is
exhausted, new buys skip while the app and exit monitoring keep running.
Existing trades placed before this feature are not backfilled.
Paper trading and manual stock tickets are outside these caps. All related
workers must share one database; separate databases have separate allowances.
Restart processes after changing limits.

## Live bearish stop-loss toggle

```ini
WEBULL_LIVE_BEARISH_STOP_LOSS_ENABLED=true
```

This omits the stop-loss leg from new live bearish PUT entries while retaining
the configured take-profit LIMIT leg. Paper bearish entries retain both exit
legs. The code default and `.env.example` use `true`; set `true` to restore stops
for future live PUT entries. Restart application/runner processes after changing
this setting. Existing orders are not cancelled or modified, and the profit
leg's existing time-in-force is unchanged. Bullish and iron-condor exits are
unaffected. A disabled PUT stop is not replaced by an app-managed stop.

## Iron condors

```ini
WEBULL_LIVE_IRON_CONDOR_ENABLED=false
```

With this flag false (the default), iron-condor entries are skipped in live
mode, including neutral ideas and IV Crush ideas labeled `iron_condor`. This happens before broker lookup or budget
reservation, including live dry runs. Paper/sandbox iron-condor behavior is
unchanged. Existing broker positions and orders are not closed or cancelled.
Set the flag to `true` to permit new live iron-condor entries again, with the
existing profit/stop bracket behavior. Restart workers after changing it.
The options daily limit still applies to enabled live option entries such as
CALLs and PUTs, and to iron condors if re-enabled.

## Test the live Webull connection

Run this from the project root after filling in `WEBULL_LIVE_APP_KEY` and
`WEBULL_LIVE_APP_SECRET` in `.env`. It requests the live account list without
printing account details, placing orders, or starting trading workers. It works
while the market is closed. Exported shell variables take precedence over `.env`.

```bash
WEBULL_TRADING_MODE=live WEBULL_ENDPOINT=api.webull.com uv run python - <<'PY'
from app.broker.client import get_trade_client
from app.broker.errors import describe_webull_error

try:
    response = get_trade_client().account_v2.get_account_list()
    print(f"HTTP status: {response.status_code}")
    if response.status_code == 200:
        print("SUCCESS: Live Webull account request succeeded.")
    else:
        print("FAILED: Live Webull account request was rejected.")
except Exception as exc:
    print("FAILED:", describe_webull_error(exc))
    raise SystemExit(1)
PY
```

HTTP 200 confirms live account access; it does not test order permissions.
A 401 / invalid credentials response means authentication failed; see below.
Successful dashboard requests do not verify Webull access.

## Webull 401 / invalid credentials

If the API starts but Webull client initialization returns `401 UNAUTHORIZED`,
the broker rejected authentication before order submission. For live mode, use
production `WEBULL_LIVE_APP_KEY` and `WEBULL_LIVE_APP_SECRET` with `api.webull.com`; for
paper mode, use sandbox credentials with `api.sandbox.webull.com`. Account-number
settings select accounts after authentication and cannot fix invalid API keys.
Check for stale exported variables overriding `.env`, then restart all workers.
Do not post credentials in logs or support messages.

No live-cash exit worker is started. The application does not poll live cash
order status; inspect uncertain submissions and open orders manually in Webull.
Other enabled application features may still make broker requests.

Webull credentials are selected by `WEBULL_TRADING_MODE`: live requires
`WEBULL_LIVE_APP_KEY` and `WEBULL_LIVE_APP_SECRET`; paper uses
`WEBULL_PAPER_APP_KEY` and `WEBULL_PAPER_APP_SECRET`. When both paper fields
are blank, legacy `WEBULL_APP_KEY` / `WEBULL_APP_SECRET` remain a paper-only
fallback. Partial pairs are rejected. Restart processes after changes.

## Dashboard refresh

```ini
DASHBOARD_REFRESH_INTERVAL_SECONDS=3600
```

The dashboard loads immediately, then auto-refreshes hourly by default while
visible and auto-refresh is enabled. Returning to a hidden tab refreshes only
when the interval has elapsed. The Refresh button always allows a manual update.
This controls local ledger reads, independently of feed polling and exit workers.
Restart the app and reload the browser after changing this setting.

## fix .env file malform

cd ~/ops-paper-trade
sed -i '/dashboard_refresh_interval_seconds/Id' .env
head -1 .env
