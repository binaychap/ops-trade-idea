from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.execution.daily_budget import reserve_daily_budget, reserve_live_option_budget, DailyBudgetExceeded
from app.execution.live_stock import submit_live_stock


def test_daily_cap_survives_restart_and_does_not_mix_buckets(tmp_path):
    db = str(tmp_path / 'test.db')
    now = datetime.fromisoformat('2026-09-28T16:00:00+00:00')
    reserve_daily_budget(db, 'stocks', 'first', 60, 100, now=now)
    with pytest.raises(DailyBudgetExceeded):
        reserve_daily_budget(db, 'stocks', 'second', 50, 100, now=now)
    reserve_daily_budget(db, 'stocks', 'second', 40, 100, now=now)
    reserve_daily_budget(db, 'options', 'option', 100, 100, now=now)
    with pytest.raises(DailyBudgetExceeded):
        reserve_daily_budget(db, 'stocks', 'third', 1, 100, now=now)


def test_reset_uses_new_york_midnight_not_utc(tmp_path):
    db = str(tmp_path / 'test.db')
    reserve_daily_budget(db, 'stocks', 'first', 100, 100, now=datetime.fromisoformat('2026-09-28T23:59:00-04:00'))
    with pytest.raises(DailyBudgetExceeded):
        reserve_daily_budget(db, 'stocks', 'second', 100, 100, now=datetime.fromisoformat('2026-09-29T03:59:30+00:00'))
    reserve_daily_budget(db, 'stocks', 'second', 100, 100, now=datetime.fromisoformat('2026-09-29T04:00:00+00:00'))


def test_concurrent_workers_cannot_exceed_cap(tmp_path):
    db = str(tmp_path / 'test.db')
    def reserve(i):
        try:
            reserve_daily_budget(db, 'stocks', str(i), 60, 100)
            return True
        except DailyBudgetExceeded:
            return False
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sum(pool.map(reserve, range(2))) == 1


def test_reservation_cannot_be_reused_after_reset(tmp_path):
    db = str(tmp_path / 'test.db')
    reserve_daily_budget(db, 'stocks', 'same', 100, 100, now=datetime.fromisoformat('2026-09-28T12:00:00-04:00'))
    with pytest.raises(DailyBudgetExceeded):
        reserve_daily_budget(db, 'stocks', 'same', 100, 100, now=datetime.fromisoformat('2026-09-29T12:00:00-04:00'))


def test_live_stock_cap_blocks_second_symbol_without_broker_submission(tmp_path):
    settings = SimpleNamespace(webull_trading_mode='live', dry_run=False, database_path=str(tmp_path / 'test.db'),
                               max_notional_usd=250, live_bullish_amount_usd=100, live_bullish_daily_limit_usd=100)
    sdk = SimpleNamespace(account_v2=SimpleNamespace(get_account_position=Mock(return_value=SimpleNamespace(
        status_code=200, json=lambda: []))), order_v3=SimpleNamespace(place_order=Mock(return_value=SimpleNamespace(
        status_code=200, json=lambda: {}))))
    def buy(symbol):
        return submit_live_stock(symbol, settings, symbol, account_resolver=lambda: 'account',
             trade_client=sdk, market_open=lambda: True, quote_provider=lambda s: {'price': 200})
    assert buy('AAPL')['status'] == 'submitted'
    assert 'daily budget' in buy('MSFT')['reason']
    assert sdk.order_v3.place_order.call_count == 1


def test_options_cap_independent_and_paper_exempt(tmp_path, monkeypatch):
    monkeypatch.setenv('DATABASE_PATH', str(tmp_path / 'test.db'))
    monkeypatch.setenv('WEBULL_LIVE_OPTIONS_DAILY_LIMIT_USD', '200')
    monkeypatch.setenv('WEBULL_TRADING_MODE', 'paper')
    reserve_live_option_budget(300, 'paper')
    assert not (tmp_path / 'test.db').exists()
    monkeypatch.setenv('WEBULL_TRADING_MODE', 'live')
    reserve_live_option_budget(150, 'live')
    with pytest.raises(DailyBudgetExceeded):
        reserve_live_option_budget(100, 'second')


def test_options_zero_disables_cap(tmp_path, monkeypatch):
    monkeypatch.setenv('DATABASE_PATH', str(tmp_path / 'test.db'))
    monkeypatch.setenv('WEBULL_TRADING_MODE', 'live')
    monkeypatch.setenv('WEBULL_LIVE_OPTIONS_DAILY_LIMIT_USD', '0')
    reserve_live_option_budget(500, 'live')
    assert not (tmp_path / 'test.db').exists()


@pytest.mark.parametrize('builder_name', ['buy_call_with_bracket', 'buy_put_with_bracket'])
def test_option_builder_blocks_premium_above_daily_cap(tmp_path, monkeypatch, builder_name):
    from app.options import brackets
    monkeypatch.setenv('DATABASE_PATH', str(tmp_path / 'options.db'))
    monkeypatch.setenv('WEBULL_TRADING_MODE', 'live')
    monkeypatch.setenv('WEBULL_LIVE_OPTIONS_DAILY_LIMIT_USD', '100')
    monkeypatch.setattr(brackets, '_find_valid_contract', lambda *a, **kw: ('2026-12-18', 100, 'TEST-CONTRACT'))
    sdk = SimpleNamespace(order_v3=SimpleNamespace(place_order=Mock()))
    with pytest.raises(DailyBudgetExceeded):
        getattr(brackets, builder_name)('account', 'TEST', 100, '2026-12-18', 1,
                                       entry_limit=2, trade_client=sdk)
    sdk.order_v3.place_order.assert_not_called()
