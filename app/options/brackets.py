"""Option bracket combo orders."""

import json
import sys
import logging
import math
from datetime import UTC, datetime, timedelta
from app.options.expiration import resolve_option_expiry
from app.config.webull import resolve_webull_endpoint, resolve_webull_credentials
from webull.core.client import ApiClient
from webull.data.data_client import DataClient

from app.broker.client import get_account_id, get_trade_client, new_id


# ============================================================
# COMMON OPTION LEG
# ============================================================

def option_leg(
    symbol: str,
    strike: float,
    expiration: str,
    option_type: str,
    side: str,
    quantity: int
):
    return {
        "side": side,
        "quantity": str(quantity),
        "symbol": symbol.upper(),
        "strike_price": f"{strike:.2f}",
        "option_expire_date": expiration,
        "instrument_type": "OPTION",
        "option_type": option_type.upper(),
        "market": "US",
    }

from decimal import Decimal, ROUND_HALF_UP


def round_to_tick(price: float, tick_size: float) -> float:
    price_decimal = Decimal(str(price))
    tick_decimal = Decimal(str(tick_size))

    ticks = (
        price_decimal / tick_decimal
    ).quantize(
        Decimal("1"),
        rounding=ROUND_HALF_UP
    )

    return float(ticks * tick_decimal)


def _find_valid_contract(symbol: str, desired_expiration: str | None, desired_strike: float | None, *, option_type: str | None = None) -> tuple[str, float, str | None]:
    """Query Webull option contracts and return a validated (expiration, strike).

    If the exact expiration/strike aren't available, pick the closest matching values.
    Raises RuntimeError if no suitable contract found.
    """
    symbol = str(symbol or "").upper()
    app_key, app_secret = resolve_webull_credentials()
    endpoint = resolve_webull_endpoint()
    if not app_key or not app_secret:
        raise RuntimeError("WEBULL credentials not configured for contract validation")

    api_client = ApiClient(app_key, app_secret, "us")
    api_client.add_endpoint("us", endpoint)
    api_client.set_stream_logger(stream=sys.stdout, log_level=logging.INFO)

    data_client = DataClient(api_client)

    # Read the listed chain, not an assumed calendar expiration. Follow pages
    # so the closest eligible expiration/strike is not limited to page one.
    items = []
    cursor = None
    seen_cursors = set()
    for _ in range(20):
        kwargs = dict(category="US_OPTION", underlying_symbols=symbol, page_size=500)
        if option_type is not None:
            kwargs["option_type"] = option_type
        if cursor is not None:
            kwargs["last_instrument_id"] = cursor
        resp = data_client.instrument.get_option_contracts(**kwargs)
        if resp is None or getattr(resp, "status_code", 200) != 200:
            raise RuntimeError("Unable to query listed option contracts")
        payload = resp.json() if hasattr(resp, "json") else resp
        rows = payload if isinstance(payload, list) else None
        if isinstance(payload, dict):
            for key in ("data", "items", "contracts", "options", "result", "results"):
                if isinstance(payload.get(key), list):
                    rows = payload[key]
                    break
        if rows is None:
            raise RuntimeError("Unexpected option chain response")
        items.extend(rows)
        if len(rows) < 500:
            break
        cursor = rows[-1].get("instrument_id")
        if not cursor or cursor in seen_cursors:
            raise RuntimeError("Incomplete option chain pagination")
        seen_cursors.add(cursor)
    else:
        raise RuntimeError("Option chain pagination limit exceeded")

    expirations: dict[str, dict[float, dict]] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        if option_type is not None and str(item.get("option_type") or "").upper() != option_type:
            continue
        exp = item.get("expiration_date") or item.get("expiration") or item.get("exp_date") or item.get("expire_date") or item.get("expiry")
        if not exp:
            continue
        try:
            exp = datetime.fromisoformat(str(exp)).date().isoformat()
        except ValueError:
            continue
        strike_raw = item.get("strike_price") or item.get("strike") or item.get("strikePrice")
        try:
            strike = float(strike_raw)
        except Exception:
            continue
        if not math.isfinite(strike) or strike <= 0:
            continue
        # collect the full item for this expiration+strike
        expirations.setdefault(exp, {})[strike] = item

    if not expirations:
        raise RuntimeError(f"No option contracts found for {symbol}")

    # Without a requested minimum, exclude same-day and expired contracts.
    tomorrow = (datetime.now(UTC).date() + timedelta(days=1)).isoformat()
    minimum = max(desired_expiration or tomorrow, tomorrow)
    chosen_exp = resolve_option_expiry(minimum, list(expirations))

    available_strikes = sorted(expirations[chosen_exp].keys())
    if not available_strikes:
        raise RuntimeError(f"No strikes found for {symbol} expiration {chosen_exp}")

    if desired_strike is None:
        # choose nearest-to-the-money (median) as a fallback
        strike = available_strikes[len(available_strikes) // 2]
    else:
        # pick the closest available strike
        strike = min(available_strikes, key=lambda s: abs(s - float(desired_strike)))

    item = expirations[chosen_exp][strike]
    # try to read contract symbol fields commonly returned by Webull
    contract_symbol = (
        item.get("symbol")
        or item.get("ticker")
        or item.get("option_symbol")
        or item.get("contract_symbol")
        or None
    )

    logger = logging.getLogger(__name__)
    logger.info(
        "Validated contract for %s: chosen_expiration=%s chosen_strike=%.2f contract_symbol=%s (desired_expiration=%s desired_strike=%s)",
        symbol,
        chosen_exp,
        float(strike),
        contract_symbol,
        desired_expiration,
        desired_strike,
    )
    # For debugging, include available expirations count at DEBUG level
    logger.debug("Available expirations for %s: %s", symbol, {k: len(v) for k, v in expirations.items()})
    return chosen_exp, float(strike), contract_symbol


# ============================================================
# BUY OPTION + TAKE PROFIT + STOP LOSS
# ============================================================

def buy_call_with_bracket(
    account_id: str,
    symbol: str,
    strike: float,
    expiration: str,
    quantity: int,
    entry_limit: float,
    profit_percent: float = 10,
    stop_loss_percent: float = 5,
    trade_client=None,
    *,
    exit_time_in_force: str = "DAY",
):
    trade_client = trade_client or get_trade_client()
    """
    Example:

    Entry:
        BUY AAPL 220 CALL @ $11.25

    Take Profit:
        SELL @ +10% = $12.38

    Stop Loss:
        SELL when premium reaches -5% = $10.69
    """

    symbol = symbol.upper()
    tick_size = 0.05
    # Validate/adjust expiration and strike against broker data
    try:
        expiration, strike, contract_symbol = _find_valid_contract(symbol, expiration, strike)
    except Exception as exc:
        raise RuntimeError(f"Contract validation failed: {exc}")
    entry_limit = round_to_tick(
        entry_limit,
        tick_size
    )
    take_profit_price = round_to_tick(
        entry_limit * (1 + profit_percent / 100),
        tick_size
    )

    stop_price = round_to_tick(
        entry_limit * (1 - stop_loss_percent / 100),
        tick_size
    )

    combo_id = new_id()

    # --------------------------------------------------------
    # 1. MASTER ENTRY ORDER
    # --------------------------------------------------------

    master_order = {
        "client_order_id": new_id(),

        "combo_type": "MASTER",

        "option_strategy": "SINGLE",

        "instrument_type": "OPTION",
        "market": "US",

        "symbol": symbol,

        "order_type": "LIMIT",

        "limit_price": f"{entry_limit:.2f}",

        "quantity": str(quantity),

        "side": "BUY",

        "time_in_force": "DAY",

        "entrust_type": "QTY",

        # For combo options, position intent belongs on MASTER
        "position_intent": "BUY_TO_OPEN",

        "legs": [
            option_leg(
                symbol=symbol,
                strike=strike,
                expiration=expiration,
                option_type="CALL",
                side="BUY",
                quantity=quantity,
            )
        ]
    }

    # --------------------------------------------------------
    # 2. TAKE PROFIT
    #
    # +10%
    #
    # $11.25 * 1.10 = $12.375 -> $12.38
    # --------------------------------------------------------

    take_profit_order = {
        "client_order_id": new_id(),

        "combo_type": "STOP_PROFIT",

        "option_strategy": "SINGLE",

        "instrument_type": "OPTION",
        "market": "US",

        "symbol": symbol,

        "order_type": "LIMIT",

        "limit_price": f"{take_profit_price:.2f}",

        "quantity": str(quantity),

        "side": "SELL",

        # Webull option sell orders require DAY
        "time_in_force": exit_time_in_force,

        "entrust_type": "QTY",

        "legs": [
            option_leg(
                symbol=symbol,
                strike=strike,
                expiration=expiration,
                option_type="CALL",
                side="SELL",
                quantity=quantity,
            )
        ]
    }

    # --------------------------------------------------------
    # 3. STOP LOSS
    #
    # -5%
    #
    # $11.25 * 0.95 = $10.6875 -> $10.69
    # --------------------------------------------------------

    stop_loss_order = {
        "client_order_id": new_id(),

        "combo_type": "STOP_LOSS",

        "option_strategy": "SINGLE",

        "instrument_type": "OPTION",
        "market": "US",

        "symbol": symbol,

        "order_type": "STOP_LOSS",

        "stop_price": f"{stop_price:.2f}",

        "quantity": str(quantity),

        "side": "SELL",

        "time_in_force": exit_time_in_force,

        "entrust_type": "QTY",

        "legs": [
            option_leg(
                symbol=symbol,
                strike=strike,
                expiration=expiration,
                option_type="CALL",
                side="SELL",
                quantity=quantity,
            )
        ]
    }

    new_orders = [
        master_order,
        take_profit_order,
        stop_loss_order
    ]

    print("=" * 60)
    print("OPTION BRACKET ORDER")
    print("=" * 60)

    print(f"Contract:      {symbol} {strike} CALL")
    print(f"Expiration:    {expiration}")
    print(f"Quantity:      {quantity}")
    print(f"Entry Limit:   ${entry_limit:.2f}")
    print(
        f"Take Profit:   ${take_profit_price:.2f} "
        f"(+{profit_percent}%)"
    )
    print(
        f"Stop Loss:     ${stop_price:.2f} "
        f"(-{stop_loss_percent}%)"
    )
    print(f"Combo ID:      {combo_id}")

    print("\nOrders:")
    print(json.dumps(new_orders, indent=2))
    # --------------------------------------------------------
    # SUBMIT
    # Depending on SDK release, client_combo_order_id may be
    # accepted by the combo-order overload/body.
    # --------------------------------------------------------
    from app.execution.daily_budget import reserve_live_option_budget
    reserve_live_option_budget(Decimal(str(entry_limit)) * quantity * 100, master_order['client_order_id'])
    logger = logging.getLogger(__name__)
    logger.info("Submitting option combo for %s %s %s", symbol, strike, expiration)
    logger.debug("Order payload: %s", json.dumps(new_orders))
    try:
        response = trade_client.order_v3.place_order(
            account_id,
            new_orders,
            client_combo_order_id=combo_id,
        )
    except Exception as exc:
        logger.exception("Broker submission raised an exception for contract %s %s %s", symbol, strike, expiration)
        raise

    if response.status_code == 200:
        result = response.json()
        logger.info("Order submitted successfully: %s", result)
        return result

    raise RuntimeError(f"Order failed: {response.status_code} {response.text}")


def buy_put_with_bracket(
    account_id: str,
    symbol: str,
    strike: float,
    expiration: str | None,
    quantity: int,
    entry_limit: float | None = None,
    profit_percent: float = 20,
    stop_loss_percent: float = 10,
    trade_client=None,
    *,
    exit_time_in_force: str = "DAY",
    quote_max_age_seconds: int = 60,
    stop_loss_enabled: bool = True,
):
    trade_client = trade_client or get_trade_client()
    symbol = symbol.upper()
    # Validate/adjust expiration and strike against broker data
    try:
        expiration, strike, contract_symbol = _find_valid_contract(symbol, expiration, strike, option_type="PUT")
    except Exception as exc:
        raise RuntimeError(f"Contract validation failed: {exc}")
    if entry_limit is None:
        from app.broker.quotes import current_option_ask
        entry_limit = current_option_ask(contract_symbol, max_age_seconds=quote_max_age_seconds)["price"]
    tick_size = 0.05
    entry_limit = round_to_tick(entry_limit, tick_size)
    take_profit_price = round_to_tick(entry_limit * (1 + profit_percent / 100), tick_size)
    stop_price = (round_to_tick(entry_limit * (1 - stop_loss_percent / 100), tick_size)
                  if stop_loss_enabled else None)
    if not 0 < entry_limit < take_profit_price or (stop_loss_enabled and not 0 < stop_price < entry_limit):
        raise ValueError("PUT bracket prices collapse or are invalid after tick rounding")
    combo_id = new_id()

    master_order = {
        "client_order_id": new_id(),
        "combo_type": "MASTER",
        "option_strategy": "SINGLE",
        "instrument_type": "OPTION",
        "market": "US",
        "symbol": symbol,
        "order_type": "LIMIT",
        "limit_price": f"{entry_limit:.2f}",
        "quantity": str(quantity),
        "side": "BUY",
        "time_in_force": "DAY",
        "entrust_type": "QTY",
        "position_intent": "BUY_TO_OPEN",
        "legs": [
            option_leg(
                symbol=symbol,
                strike=strike,
                expiration=expiration,
                option_type="PUT",
                side="BUY",
                quantity=quantity,
            )
        ],
    }

    take_profit_order = {
        "client_order_id": new_id(),
        "combo_type": "STOP_PROFIT",
        "option_strategy": "SINGLE",
        "instrument_type": "OPTION",
        "market": "US",
        "symbol": symbol,
        "order_type": "LIMIT",
        "limit_price": f"{take_profit_price:.2f}",
        "quantity": str(quantity),
        "side": "SELL",
        "time_in_force": exit_time_in_force,
        "entrust_type": "QTY",
        "legs": [
            option_leg(
                symbol=symbol,
                strike=strike,
                expiration=expiration,
                option_type="PUT",
                side="SELL",
                quantity=quantity,
            )
        ],
    }

    new_orders = [master_order, take_profit_order]
    if stop_loss_enabled:
        stop_loss_order = {
            "client_order_id": new_id(),
            "combo_type": "STOP_LOSS",
            "option_strategy": "SINGLE",
            "instrument_type": "OPTION",
            "market": "US",
            "symbol": symbol,
            "order_type": "STOP_LOSS",
            "stop_price": f"{stop_price:.2f}",
            "quantity": str(quantity),
            "side": "SELL",
            "time_in_force": exit_time_in_force,
            "entrust_type": "QTY",
            "legs": [
                option_leg(
                    symbol=symbol,
                    strike=strike,
                    expiration=expiration,
                    option_type="PUT",
                    side="SELL",
                    quantity=quantity,
                )
            ],
        }

        new_orders.append(stop_loss_order)
    from app.execution.daily_budget import reserve_live_option_budget
    reserve_live_option_budget(Decimal(str(entry_limit)) * quantity * 100, master_order['client_order_id'])
    logger = logging.getLogger(__name__)
    logger.info("Submitting option combo for %s %s %s", symbol, strike, expiration)
    logger.debug("Order payload: %s", json.dumps(new_orders))
    try:
        response = trade_client.order_v3.place_order(account_id, new_orders, client_combo_order_id=combo_id)
    except Exception:
        logger.exception("Broker submission raised an exception for contract %s %s %s", symbol, strike, expiration)
        raise

    if response.status_code == 200:
        return response.json()

    raise RuntimeError(f"Order failed: {response.status_code} {response.text}")


# ============================================================
# RUN
# ============================================================

if __name__ == "__main__":

    account_id = get_account_id()
    print("Using account:", account_id)

    buy_call_with_bracket(
        account_id=account_id,

        symbol="AAPL",

        strike=220,

        expiration="2026-09-18",

        quantity=1,

        # Buy call at max $11.25
        entry_limit=11.25,

        # Sell for profit at +10%
        profit_percent=10,

        # Stop out at -5%
        stop_loss_percent=5,
    )

    # Example bearish put bracket:
    # buy_put_with_bracket(
    #     account_id=account_id,
    #     symbol="AAPL",
    #     strike=230,
    #     expiration="2026-09-18",
    #     quantity=1,
    #     entry_limit=11.25,
    #     profit_percent=10,
    #     stop_loss_percent=5,
    # )
