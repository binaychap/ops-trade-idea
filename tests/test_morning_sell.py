from datetime import datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from app.bullish.ledger import BullishLedger
from app.exits.morning_sell import MorningSellCalendar, MorningSellScheduler

ET = ZoneInfo('America/New_York')


def dt(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00'))


@pytest.fixture(autouse=True)
def isolate_calendar_settings(monkeypatch):
    from app.main import get_settings

    # Calendar expectations must not depend on the operator's local .env.
    monkeypatch.setenv('MORNING_SELL_TIME', '10:00')
    monkeypatch.setenv('MORNING_SELL_TIMEZONE', 'America/New_York')
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def calendar():
    return MorningSellCalendar('10:00', 'America/New_York')


class FakeBroker:
    def __init__(self, positions=None):
        self.positions = {**(positions or {})}
        self.sells = []

    def stock_positions(self, account_id):
        return {symbol: shares for (account, symbol), shares in self.positions.items()
                if account == account_id and shares > 0}

    def position(self, account_id, symbol):
        return self.positions.get((account_id, symbol), Decimal(0))

    def market_sell(self, account_id, symbol, shares, order_id):
        self.sells.append({'account_id': account_id, 'symbol': symbol,
                           'shares': shares, 'order_id': order_id})
        self.positions[(account_id, symbol)] = Decimal(0)
        return {'order_id': order_id, 'status': 'submitted'}


def make_ledger(tmp_path):
    ledger = BullishLedger(str(tmp_path / 'morning.sqlite3'))
    ledger.register_exit_job({
        'id': 'job-1', 'account_id': 'acct-bullish', 'symbol': 'AAPL',
        'quantity': '1', 'entry_filled_quantity': '1',
        'status': 'scheduled', 'market_orders': [], 'due_at': None,
    })
    ledger.claim('bullish-1',
                 {'symbol': 'MSFT', 'total_premium': 100.0, 'trade_count': 5},
                 {'symbol': 'MSFT', 'quantity': 1, 'entry_price': 400.0})
    ledger.update('bullish-1', 'submitted')
    return ledger


def test_morning_sell_does_not_start_for_live_accounts(monkeypatch, caplog):
    from app import main

    monkeypatch.setattr(main, 'get_settings', lambda: SimpleNamespace(
        morning_sell_enabled=True, dry_run=False, webull_trading_mode='live'))
    main.start_morning_sell()
    assert 'disabled in live mode' in caplog.text


def positions():
    return {('acct-bullish', 'AAPL'): Decimal(3), ('acct-bullish', 'MSFT'): Decimal(1)}


MONDAY_1005_ET = dt('2026-09-28T14:05:00Z')  # Monday 10:05 New York
MONDAY_0900_ET = dt('2026-09-28T13:00:00Z')  # Monday 09:00 New York


def test_calendar_next_run_skips_weekend():
    friday_close = dt('2026-09-25T20:00:00Z')  # Friday after close
    assert calendar().next_run(friday_close) == dt('2026-09-28T14:00:00Z')


def test_calendar_today_run_time():
    assert calendar().today_run_at_utc(MONDAY_0900_ET) == dt('2026-09-28T14:00:00Z')
    assert calendar().today_run_at_utc(dt('2026-09-27T14:00:00Z')) is None  # Sunday


def test_run_before_sell_time_waits(tmp_path):
    ledger = make_ledger(tmp_path)
    broker = FakeBroker(positions())
    summary = MorningSellScheduler(ledger, broker, calendar(),
                                   resolve_account=lambda: 'acct-bullish',
                                   dry_run=False, mark_bullish_rows=True).run_once(MONDAY_0900_ET)
    assert summary['status'] == 'waiting'
    assert broker.sells == []


def test_sell_pass_sells_tracked_holdings(tmp_path):
    ledger = make_ledger(tmp_path)
    broker = FakeBroker(positions())
    scheduler = MorningSellScheduler(ledger, broker, calendar(),
                                     resolve_account=lambda: 'acct-bullish',
                                     dry_run=False, mark_bullish_rows=True)
    summary = scheduler.run_once(MONDAY_1005_ET)
    assert summary['status'] == 'completed'
    assert {s['symbol'] for s in summary['sold']} == {'AAPL', 'MSFT'}
    by_symbol = {s['symbol']: s for s in broker.sells}
    assert by_symbol['AAPL']['account_id'] == 'acct-bullish'
    assert by_symbol['MSFT']['account_id'] == 'acct-bullish'
    assert by_symbol['AAPL']['shares'] == Decimal(3)
    # Sources are marked so other workers do not resell.
    assert ledger.exit_jobs() == []
    import sqlite3
    from contextlib import closing
    with closing(sqlite3.connect(ledger.path)) as conn:
        row = conn.execute(
            "SELECT status FROM top_bullish_trades WHERE symbol = 'MSFT'").fetchone()
    assert row[0] == 'sold'


def test_second_run_same_day_is_idempotent(tmp_path):
    ledger = make_ledger(tmp_path)
    broker = FakeBroker(positions())
    scheduler = MorningSellScheduler(ledger, broker, calendar(),
                                     resolve_account=lambda: 'acct-bullish',
                                     dry_run=False, mark_bullish_rows=True)
    scheduler.run_once(MONDAY_1005_ET)
    summary = scheduler.run_once(MONDAY_1005_ET + timedelta(minutes=5))
    assert summary['status'] == 'already_ran'
    assert len(broker.sells) == 2


def test_restart_same_day_does_not_resell(tmp_path):
    ledger = make_ledger(tmp_path)
    broker = FakeBroker(positions())
    kwargs = dict(calendar=calendar(), resolve_account=lambda: 'acct-bullish', dry_run=False, mark_bullish_rows=True)
    MorningSellScheduler(ledger, broker, **kwargs).run_once(MONDAY_1005_ET)
    # Simulate positions still visible after submission; restart must not replay.
    broker.positions = positions()
    # Fresh scheduler instance (process restart): reservations persist in the DB.
    summary = MorningSellScheduler(ledger, broker, **kwargs).run_once(MONDAY_1005_ET + timedelta(minutes=5))
    assert summary['status'] == 'completed'
    assert all(s['reason'] == 'already_attempted_today' for s in summary['skipped'])
    assert len(broker.sells) == 2


def test_zero_position_is_skipped(tmp_path):
    ledger = make_ledger(tmp_path)
    broker = FakeBroker({('acct-bullish', 'AAPL'): Decimal(0),
                         ('acct-bullish', 'MSFT'): Decimal(1)})
    summary = MorningSellScheduler(ledger, broker, calendar(),
                                   resolve_account=lambda: 'acct-bullish',
                                   dry_run=False, mark_bullish_rows=True).run_once(MONDAY_1005_ET)
    assert {s['symbol'] for s in summary['sold']} == {'MSFT'}
    assert all(s['symbol'] != 'AAPL' for s in broker.sells)


def test_dry_run_records_without_selling(tmp_path):
    ledger = make_ledger(tmp_path)
    broker = FakeBroker(positions())
    summary = MorningSellScheduler(ledger, broker, calendar(),
                                   resolve_account=lambda: 'acct-bullish',
                                   dry_run=True).run_once(MONDAY_1005_ET)
    assert broker.sells == []
    assert all(s['reason'] == 'dry_run' for s in summary['skipped'])


def test_untracked_stocks_and_account_isolation(tmp_path):
    ledger = make_ledger(tmp_path)
    broker = FakeBroker({('selected', 'UNTRACKED'): Decimal('7.5'),
                         ('other', 'AAPL'): Decimal(12)})
    summary = MorningSellScheduler(
        ledger, broker, calendar(), resolve_account=lambda: 'selected', dry_run=False,
    ).run_once(MONDAY_1005_ET)
    assert not summary['errors']
    assert [(s['account_id'], s['symbol'], s['shares']) for s in broker.sells] == [
        ('selected', 'UNTRACKED', Decimal('7.5'))]
    assert len(ledger.exit_jobs()) == 1


def test_position_parser_excludes_options_zero_and_shorts():
    from types import SimpleNamespace
    from app.broker.stocks import StockExecution
    rows = [dict(symbol=s, quantity=q, instrument_type=t) for s, q, t in [
        ('AAPL', '2', 'EQUITY'), ('AAPL', '3', 'EQUITY'),
        ('OPT', '1', 'OPTION'), ('ZERO', '0', 'EQUITY'), ('SHORT', '-2', 'EQUITY')]]
    response = SimpleNamespace(status_code=200, json=lambda: rows)
    client = SimpleNamespace(account_v2=SimpleNamespace(get_account_position=lambda account: response))
    assert StockExecution(client).stock_positions('selected') == {'AAPL': Decimal(5)}
