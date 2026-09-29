"""Fixed-dollar stock entries (legacy module name retained for callers)."""
import math

from app.broker.client import get_account_id as get_account_id
from app.broker.client import get_trade_client, new_id
from app.broker.stocks import checked_json
from app.broker.quotes import current_stock_quote

STOCK_BUY_USD = 100.0


def validate_stock_buy(reference_price, max_notional=STOCK_BUY_USD):
    if not math.isfinite(float(max_notional)) or max_notional < STOCK_BUY_USD:
        raise ValueError("$100 stock buy exceeds MAX_NOTIONAL_USD")
    if not math.isfinite(float(reference_price)) or reference_price <= STOCK_BUY_USD:
        raise ValueError("$100 fractional buy requires a share price above $100")


def buy_stock(account_id, symbol, quantity=None, entry_price=None, stop_price=None,
              target_price=None, trade_client=None, *, exit_time_in_force="GTC",
              before_submit=None, client_order_id=None):
    # Legacy quantity/price arguments never change the fixed-dollar market order.
    quote = current_stock_quote(symbol)
    validate_stock_buy(float(quote['price']))
    trade_client = trade_client or get_trade_client()
    entry_id = client_order_id or new_id()
    tracking = {"entry_id": entry_id, "combo_id": None, "profit_id": None,
                "stop_id": None, "notional_usd": STOCK_BUY_USD}
    order = {
        "client_order_id": entry_id, "combo_type": "NORMAL",
        "symbol": symbol.upper(), "instrument_type": "EQUITY", "market": "US",
        "side": "BUY", "order_type": "MARKET", "total_cash_amount": "100.00",
        "time_in_force": "DAY", "support_trading_session": "CORE",
        "entrust_type": "AMOUNT",
    }
    if before_submit is not None:
        before_submit(tracking)
    result = checked_json(trade_client.order_v3.place_order(account_id, [order]))
    if not isinstance(result, dict):
        raise RuntimeError("Unexpected stock order response")
    return {**result, **tracking}
