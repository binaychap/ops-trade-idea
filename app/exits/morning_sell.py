"""Daily market sells of all long equities in BULLISH_STOCK_ACCOUNT_NUMBER.

Uses broker positions, including stocks absent from the ledger. Reservations
prevent same-day replay; submission acceptance is not confirmation of a fill.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from uuid import uuid4
from zoneinfo import ZoneInfo

import exchange_calendars as calendars

logger = logging.getLogger('optionomics_bot')


class MorningSellCalendar:
    """Next scheduled sell time on an XNYS trading session."""

    def __init__(self, sell_time=None, timezone=None):
        if sell_time is None or timezone is None:
            from app.main import get_settings

            settings = get_settings()
            if sell_time is None:
                sell_time = settings.morning_sell_time
            if timezone is None:
                timezone = settings.morning_sell_timezone
        self.zone = ZoneInfo(timezone)
        self.sell_time = time.fromisoformat(sell_time)
        self.calendar = calendars.get_calendar('XNYS')

    def _run_at_utc(self, session):
        scheduled = datetime.combine(session.date(), self.sell_time, self.zone)
        opening = self.calendar.session_open(session).to_pydatetime()
        closing = self.calendar.session_close(session).to_pydatetime()
        # A configured time past an early close runs just before close.
        scheduled = max(opening, min(scheduled, closing - timedelta(minutes=1)))
        return scheduled.astimezone(UTC)

    def next_run(self, after):
        """Next scheduled sell time strictly after `after`."""
        day = after.astimezone(self.zone).date()
        while True:
            session = self.calendar.date_to_session(day.isoformat(), direction='next')
            run_at = self._run_at_utc(session)
            if run_at > after:
                return run_at
            day = session.date() + timedelta(days=1)

    def today_run_at_utc(self, now):
        """Today's scheduled sell time, or None when today is not a session."""
        day = now.astimezone(self.zone).date().isoformat()
        if not self.calendar.is_session(day):
            return None
        session = self.calendar.date_to_session(day, direction='next')
        if session.date().isoformat() != day:
            return None
        return self._run_at_utc(session)

    def is_open(self, now):
        day = now.astimezone(self.zone).date().isoformat()
        if not self.calendar.is_session(day):
            return False
        return self.calendar.session_open(day) <= now < self.calendar.session_close(day)


class MorningSellScheduler:
    """Runs one market-sell pass per trading day at the calendar's sell time."""

    def __init__(self, ledger, broker, calendar=None, resolve_account=None, dry_run=True, mark_bullish_rows=False):
        self.ledger = ledger
        self.broker = broker
        self.calendar = calendar or MorningSellCalendar()
        # Resolve exactly BULLISH_STOCK_ACCOUNT_NUMBER, never a ledger account.
        self.mark_bullish_rows = mark_bullish_rows
        self.resolve_account = resolve_account
        self.dry_run = dry_run
        self._last_run_date = None

    def run_once(self, now=None):
        now = now or datetime.now(UTC)
        today = now.astimezone(self.calendar.zone).date()
        if self._last_run_date == today:
            return {'status': 'already_ran', 'run_date': today.isoformat()}
        run_at = self.calendar.today_run_at_utc(now)
        if run_at is None:
            return {'status': 'no_session', 'run_date': today.isoformat()}
        if now < run_at:
            return {'status': 'waiting', 'run_at': run_at.isoformat(), 'run_date': today.isoformat()}
        if not self.calendar.is_open(now):
            # Missed today's window after close; retry at the next session.
            self._last_run_date = today
            return {'status': 'missed', 'run_date': today.isoformat()}
        # Serializes against entry submission and the exit worker; the lock is
        # released on process death, so a crash never blocks the next pass.
        with self.ledger.exit_worker_lock() as acquired:
            if not acquired:
                return {'status': 'deferred', 'reason': 'worker_busy', 'run_date': today.isoformat()}
            summary = self._sell_all(today)
        self._last_run_date = today
        return summary

    def _sell_all(self, today):
        run_date = today.isoformat()
        sold, skipped, errors = [], [], []
        if self.resolve_account is None:
            raise ValueError('Morning sell requires the configured stock account')
        account_id = self.resolve_account()
        positions = self.broker.stock_positions(account_id)
        sources = self.ledger.morning_sell_holdings()
        for symbol, shares in positions.items():
            holding = {
                'symbol': symbol, 'source': 'broker_positions',
                'tracked_quantity': str(shares),
                'sources': [row for row in sources if row['symbol'] == symbol and (
                    row['account_id'] == account_id or
                    (row['source'] == 'top_bullish_trades' and self.mark_bullish_rows)
                )],
            }
            cash_jobs = [row['job'] for row in holding['sources']
                         if row.get('job') and row['job'].get('kind') == 'live_cash']
            if cash_jobs:
                skipped.append({'symbol': symbol, 'reason': 'manual_cash_position'})
                continue
            fingerprint = f'morning-sell:{run_date}:{account_id}:{symbol}'
            intent = {
                'run_date': run_date, 'account_id': account_id, 'symbol': symbol,
                'tracked_quantity': holding['tracked_quantity'], 'source': holding['source'],
            }
            # Atomic reservation precedes the network call; a crash or restart
            # replays only symbols that were never reserved.
            if not self.ledger.reserve(fingerprint, intent):
                skipped.append({'symbol': symbol, 'reason': 'already_attempted_today'})
                continue
            try:
                result = self._sell_holding(account_id, symbol, holding, fingerprint, intent)
            except Exception as exc:
                self.ledger.fail(fingerprint, f'{type(exc).__name__}: {exc}')
                logger.warning('Morning sell %s failed: %s', symbol, exc)
                errors.append({'symbol': symbol, 'error': str(exc)[:200]})
                continue
            (sold if result.get('sold') else skipped).append(result)
        summary = {'status': 'completed', 'run_date': run_date,
                   'sold': sold, 'skipped': skipped, 'errors': errors}
        logger.info('Morning sell pass %s: %d sold, %d skipped, %d errors',
                    run_date, len(sold), len(skipped), len(errors))
        return summary

    @staticmethod
    def _decimal(value):
        try:
            result = Decimal(str(value))
        except (InvalidOperation, ValueError, TypeError):
            return Decimal(0)
        return result if result.is_finite() and result >= 0 else Decimal(0)

    def _sell_holding(self, account_id, symbol, holding, fingerprint, intent):
        held = self.broker.position(account_id, symbol)
        held = self._decimal(held)
        if held <= 0:
            self.ledger.finish(fingerprint, 'skipped', {**intent, 'reason': 'no_broker_position'})
            return {'symbol': symbol, 'sold': False, 'reason': 'no position held at broker'}
        shares = held
        if self.dry_run:
            self.ledger.finish(fingerprint, 'dry_run', {**intent, 'shares': str(shares)})
            return {'symbol': symbol, 'sold': False, 'reason': 'dry_run', 'shares': str(shares)}
        order_id = f'morning-sell-{uuid4().hex[:16]}'
        # Commit the submission intent BEFORE the network call.
        self.ledger.finish(fingerprint, 'submitting', {**intent, 'order_id': order_id, 'shares': str(shares)})
        response = self.broker.market_sell(account_id, symbol, shares, order_id)
        self.ledger.finish(fingerprint, 'ordered',
                           {**intent, 'order_id': order_id, 'shares': str(shares), 'broker': response})
        for source in holding['sources']:
            self._mark_source_sold(source, run_date=intent['run_date'], order_id=order_id, shares=shares)
        logger.info('Morning sell %s: submitted market sell for %s shares (order %s)', symbol, shares, order_id)
        return {'symbol': symbol, 'sold': True, 'shares': str(shares), 'order_id': order_id}

    def _mark_source_sold(self, holding, *, run_date, order_id, shares):
        """Mark the ledger row sold so other workers do not sell it again."""
        if holding['source'] == 'scheduled_stock_exits':
            job = holding['job']
            job['status'] = 'complete'
            job['last_error'] = None
            job['morning_sell'] = {'run_date': run_date, 'order_id': order_id, 'shares': str(shares)}
            self.ledger.save_exit_job(job)
        elif holding['source'] == 'top_bullish_trades':
            marker = getattr(self.ledger, 'mark_morning_sold', None)
            if marker is not None:
                marker(holding['symbol'])
