# Bearish direction and execution flow

Exit percentages are configurable with `BEARISH_PROFIT_PERCENT` and
`BEARISH_STOP_LOSS_PERCENT` in `.env`. Percentages shown below are defaults;
restart services after editing. Existing orders are unchanged.

This documents the current `app/main.py` polling path, verified from source on
2026-09-16. Direction comes from the Optionomics feed; the application does not
calculate a bearish signal from market prices.

## Decision logic

In [app/feeds/decisions.py](app/feeds/decisions.py),
`build_trade_decision_from_optionomics_payload` handles `direction == "bearish"`:

- If `ALLOW_SHORT_SELLING=false` (the default), skip the idea.
- If enabled, set the internal action to `sell_short`.
- Require all three feed levels: entry, target, and stop.
- Validate the bearish levels using `validate_optionomics_directional_levels`:

```python
if direction == "bearish":
    return target < entry and stop > entry
```

For example, entry 100, target 90, and stop 105 pass. Equal target/entry or
stop/entry values fail. Invalid or missing levels produce a skipped decision.
The decision uses `MAX_NOTIONAL_USD` and normalized confidence. The pipeline
label is `iron_condor` for Crush and `placeholder` otherwise; bearish execution
is routed by action, regardless of that label.

[app/main.py](app/main.py)'s `build_trade_decision` has a separate bearish branch
with equivalent direction and level checks. The polling path uses the Optionomics
builder through its compatibility wrapper.

## End-to-end flow

```mermaid
flowchart TD
    START[Polling worker wakes at configured interval] --> HOURS{Weekday 09:30 to before 16:00 ET?}
    HOURS -->|No| WAIT[Wait for next polling interval]
    HOURS -->|Yes| A[Fetch Optionomics ideas]
    A --> B{Already ordered matching trade ID and symbol?}
    B -->|Yes, unless FORCE_REPROCESS| END[Continue to next idea]
    B -->|No, or FORCE_REPROCESS| C[Save queued idea in SQLite]
    C --> D[Validate feed model and read bearish direction]
    D --> E{ALLOW_SHORT_SELLING enabled?}
    E -->|No| SKIP[Record skipped]
    E -->|Yes| F[Set action sell_short]
    F --> G{Entry, target and stop present?}
    G -->|No| SKIP
    G -->|Yes| H{Target below entry and stop above entry?}
    H -->|No| SKIP
    H -->|Yes| I{Decision symbol matches feed?}
    I -->|No| SKIP
    I -->|Yes| J[Build TradeIdea and call maybe_submit_order]
    J --> K{Already ordered matching trade ID and symbol?}
    K -->|Yes, even with FORCE_REPROCESS| END
    K -->|No| L{Weekday 09:30 to before 16:00 ET?}
    L -->|No| SKIP
    L -->|Yes| M[Compute fingerprint and call submit_paper_order]
    M --> N{DRY_RUN enabled?}
    N -->|Yes| DRY[Return preview and record dry_run]
    N -->|No| O[Route sell_short to BearishPutOptionExecutor]
    O --> P[Resolve account and derive option parameters]
    P --> Q[buy_put_with_bracket validates expiration and strike]
    Q -->|Contract validation fails| SKIP
    Q -->|Valid PUT| QUOTE[Fetch selected contract ask premium]
    QUOTE -->|Missing, stale or invalid| SKIP
    QUOTE -->|Fresh| R[Round entry and 20 percent profit / 10 percent stop to 0.05 tick]
    R --> S[Submit PUT bracket to Webull]
    S --> T[BUY LIMIT entry plus SELL profit and stop legs]
    T --> U[Successful response: record ordered]
    S -->|Submission error| FAIL[Polling handler records failed]
    D -.->|Unhandled processing error| FAIL
    SKIP --> END
    DRY --> END
    U --> END
    FAIL --> END
```

Unhandled exceptions elsewhere in per-idea processing also reach the polling
handler and mark the idea failed. Feed-fetch `RuntimeError` ends that scan.
The worker starts only when polling is enabled and feed credentials are present.
When both trade ID and symbol are supplied, deduplication requires both to match
an `ordered` row; it is not a permanent symbol-only exclusion.

## PUT bracket construction

[app/execution/submitter.py](app/execution/submitter.py) routes `sell_short` to
[BearishPutOptionExecutor](app/bearish/executor.py), which calls
`buy_put_with_bracket` in
[app/webull-buy-combo-option.py](app/webull-buy-combo-option.py).

| Parameter | Current implementation |
| --- | --- |
| Reference level | First truthy payload entry, target, or stop; otherwise `max(notional_usd / 100, 1)` |
| Expiration | Earliest listed PUT expiration after today (UTC); excludes expired and same-day contracts |
| Requested strike | `round(reference_level / 5) * 5` |
| Entry premium | Selected PUT contract snapshot ask, rounded to a 0.05 tick; no estimated-premium fallback |
| Contract selection | Paginated PUT chain; earliest eligible listed expiration and closest available strike |
| Quantity | One contract |
| Entry | PUT `BUY`, `BUY_TO_OPEN`, LIMIT, GTC |
| Take profit | PUT SELL LIMIT at entry premium plus 20% |
| Stop loss | PUT SELL STOP_LOSS at entry premium minus 10% |
| Price rounding | Entry and exit premiums rounded to a 0.05 tick |
| Exit duration | GTC by default |

The feed target and stop validate the underlying bearish setup. The submitted
exit prices are calculated separately from the option entry premium. Contract
selection adjusts the expiration/strike before fetching the premium. The quote must
match the selected contract, have a positive finite ask, and have `quote_time`
within the last 60 seconds (at most five seconds in the future). Missing or
invalid quotes prevent submission; the shared submitter logs the reason and the
polling handler records skipped. Timestamp diagnostics include stale age/limit
or future offset. The quote freshness limit remains 60 seconds.

The entry remains a LIMIT order at the quoted ask, rounded to the existing 0.05
tick. Exits use that rounded entry limit, not the actual fill price. For example,
a 2.00 entry limit gives a 2.40 profit limit and a 1.80 stop. Tick rounding can
change the exact percentages; collapsed or invalid brackets are rejected.
Webull’s [US options API documentation](https://developer.webull.com/apis/docs/trade-api/options/)
states that MARKET option orders are unsupported. Snapshot access and sandbox
response fields still require live validation; these changes were tested with
mocked data and broker clients. Dry runs still return before quote retrieval.

## Implementation limits and naming discrepancies

- `sell_short`, the short-selling setting, and returned `side: SELL` metadata
  describe the internal routing. The actual entry order buys a PUT; it does not
  short stock. The executor's strategy label is `sell_next_way`.
- The market-hours check applies before dry-run handling too. It checks weekdays
  and clock time, without exchange holiday or early-close handling.
- `apply_risk_gates` is not called by this polling/submission path. Quantity is
  fixed at one contract rather than sized to the decision's notional budget.
- The PUT contract lookup filters for `option_type: PUT` before selecting
  expiration and strike. Missing contract type or unavailable PUTs fail validation.
- This bearish branch returns before the stock scheduling path. It does not
  register a next-day stock exit job or reconcile option fills and exits.
- `ordered` records successful submission handling, not a confirmed fill or a
  completed bracket exit. This document is source verification, not a live broker
  execution test.

Related regression coverage lives in
[tests/test_apply_risk_gates.py](tests/test_apply_risk_gates.py), including bearish
level validation, PUT builder selection, and submission routing.

## Account selection

`OPTIONS_MARGIN_ACCOUNT_NUMBER` in `.env` selects the shared individual margin
account for bearish PUT and iron-condor submissions. The broker account list
must contain exactly one matching account number with a valid API account ID;
missing configuration or unmatched/ambiguous accounts stop submission. No
first-account fallback is used. Restart services after changing this setting.
The account type and permissions have not been verified with a live lookup.

This is the same shared `get_account_id(account_number=...)` lookup used by
`main-top-bullish.py`, which reads `TOP_BULLISH_ACCOUNT_NUMBER`. Setting both
variables to the same value routes these strategies to the same account.
The local values matched when checked on 2026-09-16; no live lookup was made.
See [the account comparison](README.md#strategy-account-selection) for the
separate main-service bullish/CALL paths and restart requirements.

Expiration selection now queries listed PUT contracts without an exact-date
filter. The hardcoded five-day offset is removed from the shared bearish path;
main-option.py bearish submission delegates to that path too. The existing
exact-or-next-listed resolver is shared through `app/options/expiration.py`.
Empty chains skip execution; no synthetic expiration is used.

## Delayed PUT quotes for paper testing

`BEARISH_QUOTE_MAX_AGE_SECONDS` controls the bearish PUT ask timestamp age limit.
The code and `.env.example` default to 60 seconds. Local `.env` is set to 1200
seconds (20 minutes) to allow Webull's approximately 15-minute-delayed sandbox
quotes. Entry and profit/stop prices therefore use the delayed premium, not a
real-time price. Restart the service after editing the setting.

Values must be positive integer seconds. Quotes beyond the configured limit,
invalid/nonfinite prices or timestamps, and quotes more than five seconds in
the future remain rejected. This setting does not change stock or iron-condor
quote limits. Both bearish entry points use the shared submitter. No orders
were placed to enable this setting.
