"""Strict Webull stock queries and scheduled market exits.

Response schemas: https://developer.webull.com/apis/docs/reference/order-detail.md
and https://developer.webull.com/apis/docs/reference/account-position.md
"""
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

TERMINAL = frozenset({'FILLED', 'CANCELLED', 'FAILED'})
KNOWN_STATUSES = TERMINAL | {'PENDING', 'SUBMITTED', 'PARTIAL_FILLED'}


class OrderNotFound(RuntimeError):
    """A saved client order ID is not currently queryable, not proof of no fill."""


def is_order_not_found(code, message):
    return code == 'OPENAPI_PARAM_ERR' and 'order not present' in str(message).lower()


def quantity(value) -> Decimal:
    result = Decimal(str(value))
    if not result.is_finite() or result < 0:
        raise ValueError('Invalid broker quantity')
    return result


def checked_json(response):
    if response.status_code != 200:
        raise RuntimeError(f'Webull request failed: HTTP {response.status_code}')
    data = response.json()
    if isinstance(data, dict) and data.get('error_code'):
        raise RuntimeError(f"Webull request failed: {data['error_code']}")
    return data


@dataclass(frozen=True)
class StockOrder:
    client_order_id: str
    status: str
    filled: Decimal
    total: Decimal | None
    filled_at: datetime | None

    @property
    def terminal(self):
        return self.status in TERMINAL


class StockExecution:
    def __init__(self, client):
        self.client = client

    def order(self, account_id, order_id, symbol, side):
        try:
            response = self.client.order_v3.get_order_detail(account_id, order_id)
        except Exception as exc:
            if is_order_not_found(getattr(exc, 'error_code', None), getattr(exc, 'error_msg', '')):
                raise OrderNotFound('Webull cannot find the saved order ID') from None
            raise
        if response.status_code == 417:
            error = response.json()
            if isinstance(error, dict) and is_order_not_found(error.get('error_code'), error.get('message') or error.get('error_msg')):
                raise OrderNotFound('Webull cannot find the saved order ID')
        data = checked_json(response)
        if not isinstance(data, dict) or not isinstance(data.get('orders'), list):
            raise ValueError('Missing order details; reconciliation deferred')
        matches = [o for o in data['orders'] if o.get('client_order_id') == order_id]
        if len(matches) != 1:
            raise ValueError('Order not uniquely identified; reconciliation deferred')
        raw = matches[0]
        if raw.get('symbol') != symbol or raw.get('side') != side or raw.get('instrument_type') != 'EQUITY':
            raise ValueError('Broker order identity mismatch')
        status = raw.get('status')
        if status not in KNOWN_STATUSES:
            raise ValueError(f'Unrecognized broker status: {status}')
        # Never interpret a missing fill count as zero, including cancelled orders.
        filled = quantity(raw['filled_quantity'])
        amount_order = raw.get('entrust_type') == 'AMOUNT' and side == 'BUY'
        total = None if amount_order else quantity(raw['total_quantity'])
        if total is not None and (total <= 0 or filled > total or (status == 'FILLED' and filled != total)):
            raise ValueError('Inconsistent broker fill quantities')
        filled_at = None
        if filled:
            if raw.get('filled_time_at'):
                filled_at = datetime.fromisoformat(raw['filled_time_at'].replace('Z', '+00:00'))
                if filled_at.tzinfo is None:
                    raise ValueError('Broker fill timestamp must include timezone')
            elif raw.get('filled_time'):
                filled_at = datetime.fromtimestamp(int(raw['filled_time']) / 1000, UTC)
        return StockOrder(order_id, status, filled, total, filled_at)

    def position(self, account_id, symbol):
        rows = checked_json(self.client.account_v2.get_account_position(account_id))
        if not isinstance(rows, list):
            raise ValueError('Missing position list; reconciliation deferred')
        total = Decimal(0)
        for row in rows:
            if not isinstance(row, dict) or 'symbol' not in row or 'instrument_type' not in row:
                raise ValueError('Malformed position response')
            if row['symbol'] == symbol and row['instrument_type'] == 'EQUITY':
                total += quantity(row['quantity'])
        return total

    def stock_positions(self, account_id):
        """Return all positive equity positions in exactly the requested account."""
        rows = checked_json(self.client.account_v2.get_account_position(account_id))
        if not isinstance(rows, list):
            raise ValueError('Missing position list; reconciliation deferred')
        positions = {}
        for row in rows:
            if not isinstance(row, dict) or 'instrument_type' not in row:
                raise ValueError('Malformed position response')
            if row['instrument_type'] != 'EQUITY':
                continue
            symbol = row.get('symbol')
            if not isinstance(symbol, str) or not symbol.strip():
                raise ValueError('Missing equity symbol')
            shares = Decimal(str(row['quantity']))
            if not shares.is_finite():
                raise ValueError('Invalid broker quantity')
            symbol = symbol.strip().upper()
            positions[symbol] = positions.get(symbol, Decimal(0)) + shares
        return {symbol: shares for symbol, shares in positions.items() if shares > 0}

    def cancel(self, account_id, order_id):
        # Acceptance is not confirmation. The scheduler must query again.
        checked_json(self.client.order_v3.cancel_order(account_id, order_id))

    def market_sell(self, account_id, symbol, shares, order_id):
        shares = quantity(shares)
        if shares <= 0:
            raise ValueError('Market exit quantity must be positive')
        return checked_json(self.client.order_v3.place_order(account_id, [{
            'client_order_id': order_id, 'combo_type': 'NORMAL',
            'symbol': symbol, 'instrument_type': 'EQUITY', 'market': 'US',
            'side': 'SELL', 'order_type': 'MARKET', 'quantity': str(shares),
            'time_in_force': 'DAY', 'support_trading_session': 'CORE',
            'entrust_type': 'QTY',
        }]))
