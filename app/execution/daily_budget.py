"""Atomic daily entry-budget reservations; exits never consume budget."""
from contextlib import closing
from datetime import UTC, datetime
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
import os
import sqlite3
from zoneinfo import ZoneInfo

from app.persistence.ledger import Ledger


class DailyBudgetExceeded(ValueError):
    pass


def reserve_daily_budget(database_path, bucket, reservation_id, amount, limit, *, now=None):
    amount, limit = Decimal(str(amount)), Decimal(str(limit))
    if not amount.is_finite() or amount <= 0 or not limit.is_finite() or limit < 0:
        raise ValueError('Invalid daily budget amount or limit')
    cents = int((amount * 100).to_integral_value(rounding=ROUND_CEILING))
    cap = int((limit * 100).to_integral_value(rounding=ROUND_FLOOR))
    current = now or datetime.now(UTC)
    if current.tzinfo is None:
        raise ValueError('Budget clock must have a timezone')
    day = current.astimezone(ZoneInfo('America/New_York')).date().isoformat()
    ledger = Ledger(database_path)
    with closing(sqlite3.connect(ledger.path, timeout=30)) as conn:
        conn.execute('''CREATE TABLE IF NOT EXISTS daily_entry_budgets (
            bucket TEXT NOT NULL, reservation_id TEXT NOT NULL,
            trading_date TEXT NOT NULL, amount_cents INTEGER NOT NULL,
            PRIMARY KEY (bucket, reservation_id))''')
        conn.commit()
        conn.execute('BEGIN IMMEDIATE')
        if conn.execute('SELECT 1 FROM daily_entry_budgets WHERE bucket=? AND reservation_id=?',
                        (bucket, reservation_id)).fetchone():
            raise DailyBudgetExceeded('Entry budget already reserved; reconcile before retrying')
        spent = conn.execute('SELECT COALESCE(SUM(amount_cents), 0) FROM daily_entry_budgets WHERE bucket=? AND trading_date=?',
                             (bucket, day)).fetchone()[0]
        if spent + cents > cap:
            raise DailyBudgetExceeded(f'{bucket} daily budget exceeded: ${max(0, cap - spent) / 100:.2f} remaining')
        conn.execute('INSERT INTO daily_entry_budgets VALUES (?, ?, ?, ?)',
                     (bucket, reservation_id, day, cents))
        conn.commit()


def reserve_live_option_budget(amount, order_id):
    """Low-level option builders use the same process environment as SDK clients."""
    if os.getenv('WEBULL_TRADING_MODE', 'paper') != 'live':
        return
    limit = Decimal(os.getenv('WEBULL_LIVE_OPTIONS_DAILY_LIMIT_USD', '0'))
    if not limit.is_finite() or limit < 0:
        raise ValueError('Invalid live options daily limit')
    if limit == 0:  # Explicitly disabled until the operator chooses an option budget.
        return
    reserve_daily_budget(os.getenv('DATABASE_PATH', 'bot.sqlite3'), 'live_options',
                         order_id, amount, limit)
