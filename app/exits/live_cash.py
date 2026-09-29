"""Reconcile live cash entries and sell fractional fills at app-managed triggers."""
from datetime import datetime
from decimal import Decimal
import logging
import threading
import time
from uuid import uuid4

from app.broker.quotes import current_stock_quote
from app.broker.stocks import StockExecution, StockOrder
from app.exits.next_day import ExitCalendar, ExitScheduler
from app.persistence.ledger import Ledger


class CashStockExecution(StockExecution):
    # Webull production Order Detail permits 2 requests per 2 seconds.
    # All cash workers in this process share pacing; the ledger lock excludes
    # simultaneous cash reconciliation across processes sharing the database.
    _query_lock = threading.Lock()
    _next_query_at = 0.0

    def order(self, *args, **kwargs):
        with self._query_lock:
            delay = type(self)._next_query_at - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            type(self)._next_query_at = time.monotonic() + 1.05
            return super().order(*args, **kwargs)


class CashExitScheduler(ExitScheduler):
    def __init__(self, ledger, broker, calendar=None, quote_provider=None):
        super().__init__(ledger, broker, calendar or ExitCalendar('09:35', 'America/New_York'))
        self.quote_provider = quote_provider or (lambda s: current_stock_quote(s, max_age_seconds=60))

    def jobs(self):
        return [job for job in self.ledger.exit_jobs() if job.get('kind') == 'live_cash']

    @staticmethod
    def _is_non_trading_hours_rejection(job, message):
        # An exception is never proof that a saved cash order had no fill.
        return False

    def _order(self, job, order_id, side='SELL'):
        if side != 'BUY':
            return super()._order(job, order_id, side)
        entries = job.get('entries') or [{'id': order_id, 'cash_amount': job['cash_amount'],
                                          'submission_status': 'acknowledged'}]
        filled, cost = Decimal(0), Decimal(0)
        times, active = [], []
        for leg in entries:
            if leg['submission_status'] == 'planned':
                # Holding the shared lock proves no entry submitter is still running.
                leg['submission_status'] = 'not_submitted'
            if leg['submission_status'] == 'not_submitted':
                continue
            order = self.broker.order(job['account_id'], leg['id'], job['symbol'], side,
                                      cash_amount=leg['cash_amount'])
            if order.filled < Decimal(leg.get('filled_quantity', '0')) or (leg.get('terminal') and not order.terminal):
                raise ValueError('Entry state moved backwards; reconciliation deferred')
            leg.update(filled_quantity=str(order.filled), terminal=order.terminal)
            filled += order.filled
            if order.filled:
                if order.filled_price is None or order.filled_price <= 0:
                    raise ValueError('Cash entry average fill price missing')
                cost += order.filled * order.filled_price
                times.append(order.filled_at)
            if not order.terminal:
                active.append(order.client_order_id)
        if filled < Decimal(job.get('entry_filled_quantity', '0')):
            raise ValueError('Entry state moved backwards; reconciliation deferred')
        job['active_entry_ids'] = active
        job['entry_terminal'] = not active
        job['entry_filled_quantity'] = str(filled)
        job['quantity'] = str(filled)
        job['entry_cost'] = str(cost)
        timestamp = min(times) if times and all(times) else None
        return StockOrder(order_id, 'SUBMITTED' if active else ('FILLED' if filled else 'CANCELLED'),
                          filled, filled, timestamp, cost / filled if filled else None)

    def reconcile(self, job, now):
        entry = self._order(job, job['entry_id'], 'BUY')
        # Cancel a partially filled entry before liquidating its filled portion.
        if not entry.terminal:
            if entry.filled and self.calendar.is_open(now):
                for active_id in job['active_entry_ids']:
                    self.broker.cancel(job['account_id'], active_id)
            return
        if entry.filled == 0:
            job['status'] = 'complete'
            return
        if entry.filled_price is None or entry.filled_price <= 0:
            raise ValueError('Cash entry average fill price missing')
        job['entry_price'] = str(entry.filled_price)
        job['target_price'] = str(entry.filled_price * (1 + Decimal(str(job['profit_percent'])) / 100))
        job['stop_price'] = str(entry.filled_price * (1 - Decimal(str(job['stop_loss_percent'])) / 100))
        if job['next_day_exit'] and not job.get('due_at'):
            if entry.filled_at is None:
                raise ValueError('Entry fill timestamp missing')
            calendar = ExitCalendar(job['exit_time'], 'America/New_York')
            job['due_at'] = calendar.next_exit(entry.filled_at).isoformat()
        remaining = entry.filled
        active = False
        for attempt in job['market_orders']:
            order = self._order(job, attempt['id'])
            if order.total != Decimal(attempt['quantity']):
                raise ValueError('Cash exit quantity differs from persisted intent')
            remaining -= order.filled
            attempt.update(status=order.status, filled_quantity=str(order.filled))
            active |= not order.terminal
        if remaining < 0:
            raise ValueError('Exit fills exceed cash entry')
        job['remaining_quantity'] = str(remaining)
        if active:
            job['status'] = 'submitted'
            return
        if remaining == 0:
            job['status'] = 'complete'
            return
        job['status'] = 'scheduled'
        if not self.calendar.is_open(now):
            return
        if not job.get('exit_trigger'):
            if job.get('liquidate_requested'):
                job['exit_trigger'] = 'morning_sell'
            elif job.get('due_at') and now >= datetime.fromisoformat(job['due_at']):
                job['exit_trigger'] = 'next_day'
            else:
                price = Decimal(str(self.quote_provider(job['symbol'])['price']))
                if not price.is_finite() or price <= 0:
                    raise ValueError('Invalid cash exit quote')
                if price <= Decimal(job['stop_price']):
                    job['exit_trigger'] = 'stop_loss'
                elif price >= Decimal(job['target_price']):
                    job['exit_trigger'] = 'take_profit'
                else:
                    return
        if self.broker.position(job['account_id'], job['symbol']) < remaining:
            raise ValueError('Holding below tracked cash fills; manual reconciliation required')
        # Fractional quantity orders must not contain a whole-share component.
        sell_quantity = Decimal(int(remaining)) if remaining >= 1 else remaining
        attempt = {'id': uuid4().hex, 'quantity': str(sell_quantity), 'status': 'submitting'}
        job['market_orders'].append(attempt)
        job['status'] = 'submitted'
        self.ledger.save_exit_job(job)
        self.broker.market_sell(job['account_id'], job['symbol'], sell_quantity, attempt['id'])
        attempt['status'] = 'acknowledged'


def start_cash_exit_worker(settings):
    """Both entry processes service persisted cash jobs; the ledger lock excludes races."""
    if getattr(settings, 'webull_trading_mode', 'paper') != 'live' or settings.dry_run:
        return None
    stop = threading.Event()
    ledger = Ledger(settings.database_path)

    def run():
        from app.broker.client import get_trade_client
        while not stop.is_set():
            try:
                scheduler = CashExitScheduler(ledger, CashStockExecution(get_trade_client()))
                scheduler.run_once()
            except Exception:
                logging.getLogger(__name__).exception('Live cash exit worker failed; retrying')
            stop.wait(10)

    thread = threading.Thread(target=run, name='live-cash-exits', daemon=True)
    thread.start()
    return stop, thread
