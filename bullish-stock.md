# Bullish stock buys

All stock entry paths (the main feed, standalone bullish runner, and manual API)
use `app/bullish/stock_bracket.py`. The legacy filename is retained for callers.
Each order requests exactly $100 with `total_cash_amount="100.00"`,
`entrust_type=AMOUNT`, `combo_type=NORMAL`, `order_type=MARKET`,
`time_in_force=DAY`, and regular-session `CORE`. Share quantity is determined
by actual broker fills. No stop-loss or take-profit orders are attached.
Legacy helper quantity/entry/stop/target arguments do not change this policy.
Bullish profit/stop settings remain accepted for configuration compatibility.

The [Webull stock API documentation](https://developer.webull.com/apis/docs/trade-api/stock/)
restricts dollar-amount orders to amounts below the price of one share and
fractional trading to market orders. A fresh quote must exceed $100; lower-priced
stocks are skipped/rejected instead of buying a whole share or exceeding $100.
The shared helper rechecks the quote immediately before persisting submission
intent. Account/symbol fractional eligibility and sandbox acceptance remain
broker-dependent and have not been live-verified. Rejected orders are not
retried with larger quantities or different order types.

`MAX_NOTIONAL_USD` remains a cap shared with options; it does not size stock
buys. Values below $100 block stock entry. The existing default stays $250 so
option risk budgets are unaffected. Dry runs report $100; the main feed dry
run remains broker-free and does not validate quotes or fractional eligibility.

## Main feed

`app/execution/submitter.py` routes bullish buys to the stock helper and resolves
`BULLISH_STOCK_ACCOUNT_NUMBER` through exact account-number matching. Bearish PUT
and neutral iron-condor execution remain separate option paths.

With `NEXT_DAY_EXIT_ENABLED=true`, entry IDs and dollar-order intent are saved
before submission. The scheduler uses actual fractional fills and their timestamps,
cancels any entry remainder, confirms terminal status, checks positions and sells
only the remaining tracked quantity at market (DAY). Existing bracket-based jobs
remain supported. Unknown broker responses defer reconciliation and never trigger
a blind retry. Flat positions and the existing ledger lock are still required.

## Standalone bullish runner

Run `uv run python -m app.bullish.runner --once` for one scan, or omit `--once`
for five-minute polling. `DRY_RUN=true` is the default. Live submission requires
`TOP_BULLISH_ACCOUNT_NUMBER`; account routing does not reset symbol deduplication.

The runner checks XNYS regular-session hours before feed/quote work and before
submission. It reads up to 10 bullish-flow symbols, validates premiums/counts,
fetches quotes, then permanently claims each symbol/trade ID in
`top_bullish_trades`. Dry runs, failures and unknown submissions retain the claim.
Quote/cap skips occur before claims. Submission IDs are persisted before the
network call. Unknown outcomes require manual reconciliation.

This runner does not register next-day exit jobs. The optional account-wide
morning-sell worker can sell its holdings only when configured for the same
account. Neither ledger status nor HTTP submission success proves a fill.

## Manual API

Preview and submission normalize every buy to $100 MARKET / DAY and explicitly
report ignored quantity/limit inputs and the absence of attached exits. Manual
entries do not register next-day jobs. Sell quantities may be fractional; fractional
sales require market orders. See README for authentication and API usage.
