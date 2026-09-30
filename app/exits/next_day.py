"""Persistent next-session stock exits. No broker calls occur on import."""
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
import logging
from zoneinfo import ZoneInfo
from uuid import uuid4

import exchange_calendars as calendars
from app.broker.stocks import OrderNotFound

logger = logging.getLogger('optionomics_bot')


class ExitCalendar:
    def __init__(self, exit_time=None, timezone=None):
        if exit_time is None or timezone is None:
            from app.main import get_settings

            settings = get_settings()
            if exit_time is None:
                exit_time = settings.next_day_exit_time
            if timezone is None:
                timezone = settings.next_day_exit_timezone
        self.zone = ZoneInfo(timezone)
        self.exit_time = time.fromisoformat(exit_time)
        self.calendar = calendars.get_calendar('XNYS')

    def next_exit(self, filled_at):
        # Start strictly after the fill's local date, even across holidays/DST.
        next_date = filled_at.astimezone(self.zone).date() + timedelta(days=1)
        session = self.calendar.date_to_session(next_date.isoformat(), direction='next')
        scheduled = datetime.combine(session.date(), self.exit_time, self.zone)
        opening = self.calendar.session_open(session).to_pydatetime()
        closing = self.calendar.session_close(session).to_pydatetime()
        # A configured afternoon exit on an early-close day runs just before close.
        scheduled = max(opening, min(scheduled, closing - timedelta(minutes=1)))
        return scheduled.astimezone(UTC)

    def is_open(self, now):
        day = now.astimezone(self.zone).date().isoformat()
        if not self.calendar.is_session(day):
            return False
        return self.calendar.session_open(day) <= now < self.calendar.session_close(day)


class ExitScheduler:
    def __init__(self, ledger, broker, calendar=None):
        self.ledger, self.broker = ledger, broker
        self.calendar = calendar or ExitCalendar()

    @staticmethod
    def _is_non_trading_hours_rejection(job, message):
        text = (str(message) + ' ' + str(job.get('entry_submission_error') or '')).lower()
        return 'non_trading_hours' in text or 'non trading hours' in text or 'can_not_trading' in text or 'cannot be placed at this time' in text

    def jobs(self):
        return [job for job in self.ledger.exit_jobs() if job.get("kind") != "live_cash"]

    def run_once(self, now=None):
        now = now or datetime.now(UTC)
        # OS lock is released on process death; avoids expiring-lease double sales.
        with self.ledger.exit_worker_lock() as acquired:
            if not acquired:
                return
            for job in self.jobs():
                if job.get('next_check_at') and now < datetime.fromisoformat(job['next_check_at']):
                    continue
                try:
                    self.reconcile(job, now)
                    job['last_error'] = None
                    job['missing_order_checks'] = 0
                    job['reconcile_failures'] = 0
                    job['next_check_at'] = None
                except OrderNotFound as exc:
                    if self._is_non_trading_hours_rejection(job, exc):
                        job['status'] = 'complete'
                        job['last_error'] = str(exc)
                        logger.warning('Scheduled stock exit %s discarded: broker rejected outside trading hours; no active exit tracking remains', job['id'])
                        self.ledger.save_exit_job(job)
                        continue
                    checks = min(int(job.get('missing_order_checks', 0)) + 1, 5)
                    delay = min(60 * 2 ** (checks - 1), 900)
                    job['missing_order_checks'] = checks
                    job['next_check_at'] = (now + timedelta(seconds=delay)).isoformat()
                    job['last_error'] = str(exc)
                    logger.warning(
                        'Scheduled stock exit %s waiting for broker reconciliation: %s; the entry order is not yet visible to Webull; checking again in %ss',
                        job['id'],
                        exc,
                        delay,
                    )
                except Exception as exc:
                    # Any other failure (broker schema drift, quote errors, ...)
                    # backs off exponentially instead of retrying every worker tick.
                    checks = min(int(job.get('reconcile_failures', 0)) + 1, 5)
                    delay = min(60 * 2 ** (checks - 1), 900)
                    job['reconcile_failures'] = checks
                    job['next_check_at'] = (now + timedelta(seconds=delay)).isoformat()
                    job['last_error'] = (str(exc) or repr(exc))[:500]
                    logger.warning(
                        'Scheduled stock exit %s deferred: %s; checking again in %ss',
                        job['id'], exc, delay,
                    )
                self.ledger.save_exit_job(job)

    def _order(self, job, order_id, side='SELL'):
        try:
            order = self.broker.order(job['account_id'], order_id, job['symbol'], side)
        except OrderNotFound:
            role = 'master BUY' if order_id == job['entry_id'] else 'exit'
            raise OrderNotFound(
                f'Webull cannot find the saved {role} order. Broker execution is unconfirmed; '
                'no new market sell will be submitted until this order is reconciled.'
            ) from None
        snapshots = job.setdefault('order_snapshots', {})
        previous = snapshots.get(order_id)
        if previous:
            if order.filled < Decimal(previous['filled_quantity']):
                raise ValueError('Broker fill count moved backwards; reconciliation deferred')
            if previous['terminal'] and not order.terminal:
                raise ValueError('Broker terminal status moved backwards; reconciliation deferred')
        snapshots[order_id] = {
            'status': order.status, 'terminal': order.terminal,
            'filled_quantity': str(order.filled),
        }
        return order

    def _bracket(self, job):
        return [self._order(job, job['entry_id'], 'BUY'),
                self._order(job, job['profit_id']), self._order(job, job['stop_id'])]

    def reconcile(self, job, now):
        entry = self._order(job, job['entry_id'], 'BUY')
        if entry.filled > Decimal(job['quantity']):
            raise ValueError('Entry fills exceed tracked quantity')
        if entry.filled and not job.get('due_at'):
            if entry.filled_at is None:
                raise ValueError('Entry fill timestamp missing')
            job['entry_filled_at'] = entry.filled_at.isoformat()
            job['due_at'] = self.calendar.next_exit(entry.filled_at).isoformat()
            job['status'] = 'scheduled'
            self.ledger.save_exit_job(job)
        job['entry_filled_quantity'] = str(entry.filled)
        if not entry.filled and not entry.terminal:
            return

        bracket = self._bracket(job)
        entry = bracket[0]
        closed_by_bracket = bracket[1].filled + bracket[2].filled
        due = job.get('due_at') and now >= datetime.fromisoformat(job['due_at'])
        already_closed = entry.terminal and closed_by_bracket == entry.filled
        if not already_closed and (not due or not self.calendar.is_open(now)):
            return

        job['status'] = 'cancelling'
        self.ledger.save_exit_job(job)
        # Cancel entry remainder first. Never sell against a still-growing entry.
        for order in bracket:
            if not order.terminal:
                self.broker.cancel(job['account_id'], order.client_order_id)
        bracket = self._bracket(job)
        if not all(order.terminal for order in bracket):
            return
        entry = bracket[0]
        remaining = entry.filled - bracket[1].filled - bracket[2].filled
        job['entry_filled_quantity'] = str(entry.filled)
        job['bracket_filled_quantity'] = str(bracket[1].filled + bracket[2].filled)

        # Persisted attempts include submissions whose HTTP response was lost.
        # Missing/unknown broker results raise: never blindly replay a market sell.
        active = False
        for attempt in job['market_orders']:
            order = self._order(job, attempt['id'])
            if order.total != Decimal(attempt['quantity']):
                raise ValueError('Market order quantity differs from persisted intent')
            attempt['status'] = order.status
            attempt['filled_quantity'] = str(order.filled)
            remaining -= order.filled
            active |= not order.terminal
        job['remaining_quantity'] = str(remaining)
        if remaining < 0:
            raise ValueError('Exit fills exceed entry; manual reconciliation required')
        if active:
            job['status'] = 'submitted'
            return
        if remaining == 0:
            job['status'] = 'complete'
            return
        if not self.calendar.is_open(now):
            return
        held = self.broker.position(job['account_id'], job['symbol'])
        if held < remaining:
            raise ValueError('Position is smaller than tracked shares; manual reconciliation required')
        # Only tracked fills are sold, even when the account has additional shares.
        order_id = uuid4().hex
        attempt = {'id': order_id, 'quantity': str(remaining), 'status': 'submitting'}
        job['market_orders'].append(attempt)
        job['status'] = 'submitted'
        self.ledger.save_exit_job(job)  # commit intent BEFORE the network call
        self.broker.market_sell(job['account_id'], job['symbol'], remaining, order_id)
        attempt['status'] = 'acknowledged'
