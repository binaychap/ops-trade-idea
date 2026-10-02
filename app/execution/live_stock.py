"""Live-only bullish cash entries with durable intent and managed exits."""
from decimal import Decimal, ROUND_FLOOR
from hashlib import sha256

from app.broker.quotes import current_stock_quote
from app.broker.stocks import StockExecution, checked_json
from app.exits.next_day import ExitCalendar
from app.persistence.ledger import Ledger


def cash_amount(settings, budget=None):
    amount = Decimal(str(getattr(settings, 'live_bullish_amount_usd', 100)))
    cap = Decimal(str(settings.max_notional_usd))
    if budget is not None:
        cap = min(cap, Decimal(str(budget)))
    if not amount.is_finite() or amount < 5 or amount != amount.quantize(Decimal('.01')):
        raise ValueError('Live bullish cash amount must be at least $5 with at most two decimals')
    if not cap.is_finite() or amount > cap:
        raise ValueError('Live bullish cash amount exceeds the notional budget')
    return amount



def split_cash_amount(amount, price):
    """Even cent-exact cash orders, each >= $5 and strictly below one share."""
    amount, price = Decimal(str(amount)), Decimal(str(price))
    if not price.is_finite() or price <= 0:
        raise ValueError('Invalid live stock quote')
    count = int((amount / price).to_integral_value(rounding=ROUND_FLOOR)) + 1
    cents = int(amount * 100)
    if count > cents // 500:
        raise ValueError('Cannot split budget into cash orders of at least $5 below one share')
    base, extra = divmod(cents, count)
    amounts = [Decimal(base + (i < extra)) / 100 for i in range(count)]
    if any(value < 5 or value >= price for value in amounts):
        raise ValueError('Cannot split budget into cash orders of at least $5 below one share')
    return amounts


def submit_live_stock(symbol, settings, fingerprint, *, account_resolver,
                      quote_provider=None, trade_client=None, before_submit=None,
                      market_open=None, budget=None):
    if settings.webull_trading_mode != 'live':
        raise ValueError('Cash stock execution is live-only')
    symbol = symbol.strip().upper()
    amount = cash_amount(settings, budget)
    order_id = 'cash-' + sha256(fingerprint.encode()).hexdigest()[:27]
    request = {
        'client_order_id': order_id, 'combo_type': 'NORMAL', 'symbol': symbol,
        'instrument_type': 'EQUITY', 'market': 'US', 'side': 'BUY',
        'order_type': 'MARKET', 'time_in_force': 'DAY',
        'support_trading_session': 'CORE', 'entrust_type': 'AMOUNT',
        'total_cash_amount': f'{amount:.2f}',
    }
    result = {'dry_run': settings.dry_run, 'client_order_id': order_id,
              'id': order_id, 'symbol': symbol, 'side': 'BUY', 'broker': 'webull',
              'notional_usd': float(amount), 'order': request,
              'exit_management': 'none', 'status': 'dry_run' if settings.dry_run else 'submitted'}
    if settings.dry_run:
        result['requires_quote_for_split'] = True
        return result
    calendar = ExitCalendar(exit_time='09:35', timezone='America/New_York')
    if market_open is None:
        from datetime import UTC, datetime
        def market_open():
            return calendar.is_open(datetime.now(UTC))
    if not market_open():
        return {'skipped': True, 'reason': 'outside_market_hours'}
    quote_provider = quote_provider or (lambda s: current_stock_quote(s, max_age_seconds=60))
    price = Decimal(str(quote_provider(symbol)['price']))
    if not price.is_finite() or price <= 0:
        raise ValueError('Invalid live stock quote')
    try:
        amounts = split_cash_amount(amount, price)
    except ValueError as exc:
        return {'skipped': True, 'reason': str(exc)}
    orders = [dict(request, total_cash_amount=f'{part:.2f}', client_order_id=(
        order_id if index == 0 else 'cash-' + sha256(f'split-leg:{fingerprint}:{index}'.encode()).hexdigest()[:27]
    )) for index, part in enumerate(amounts)]
    result['order'] = orders[0]
    result['orders'] = orders

    account_id = account_resolver()
    if trade_client is None:
        from app.broker.client import get_trade_client
        trade_client = get_trade_client()
    broker = StockExecution(trade_client)
    ledger = Ledger(settings.database_path)
    with ledger.exit_worker_lock() as acquired:
        if not acquired:
            return {'skipped': True, 'reason': 'Stock exit worker busy'}
        if ledger.has_active_exit_job(account_id=account_id, symbol=symbol):
            return {'skipped': True, 'reason': 'Stock already has an active exit job'}
        if broker.position(account_id, symbol) != 0:
            return {'skipped': True, 'reason': 'Live cash entry requires a flat stock position'}
        if not market_open():
            return {'skipped': True, 'reason': 'outside_market_hours'}
        job = {
            'id': order_id, 'kind': 'live_cash', 'account_id': account_id,
            'symbol': symbol, 'entry_id': order_id, 'status': 'waiting_entry',
            'manage_exits': False,
            'cash_amount': str(amount), 'quantity': '0', 'market_orders': [],
            'entries': [{'id': row['client_order_id'], 'cash_amount': row['total_cash_amount'],
                         'submission_status': 'planned'} for row in orders],
            'due_at': None, 'last_error': None,
        }
        from app.execution.daily_budget import reserve_daily_budget, DailyBudgetExceeded
        try:
            reserve_daily_budget(settings.database_path, 'live_bullish_stocks', order_id,
                                 amount, getattr(settings, 'live_bullish_daily_limit_usd', 100))
        except DailyBudgetExceeded as exc:
            return {'skipped': True, 'reason': str(exc)}
        # Permanent reservation also prevents a replay after a completed exit.
        if not ledger.reserve('live-stock:' + order_id, {'orders': orders}):
            return {'skipped': True, 'reason': 'Live stock attempt already recorded; reconcile before retrying'}
        ledger.register_exit_job(job)
        if before_submit is not None:
            try:
                before_submit({'entry_id': order_id})
            except Exception:
                job['status'] = 'complete'
                job['last_error'] = 'Pre-submission callback stopped entry; no broker request sent'
                ledger.save_exit_job(job)
                raise
        try:
            receipts = []
            submitted_amount = Decimal(0)
            for entry, row in zip(job['entries'], orders):
                # Planned legs are never replayed after a crash. Only submitting or
                # acknowledged legs can have broker fills and must be reconciled.
                if not market_open():
                    break
                entry['submission_status'] = 'submitting'
                ledger.save_exit_job(job)
                receipt = checked_json(trade_client.order_v3.place_order(account_id, [row]))
                if not isinstance(receipt, dict):
                    raise ValueError('Invalid cash-order receipt')
                entry['submission_status'] = 'acknowledged'
                ledger.save_exit_job(job)
                receipts.append(receipt)
                submitted_amount += Decimal(row['total_cash_amount'])
            result['submitted_cash_amount'] = float(submitted_amount)
            ledger.finish('live-stock:' + order_id, 'ordered' if receipts else 'skipped', {'orders': orders}, {'receipts': receipts})
            if not receipts:
                job['status'] = 'complete'
                ledger.save_exit_job(job)
                return {'skipped': True, 'reason': 'outside_market_hours'}
        except Exception:
            job['last_error'] = 'Entry submission unconfirmed; reconcile the saved client order ID before retrying'
            ledger.save_exit_job(job)
            raise
    return result
