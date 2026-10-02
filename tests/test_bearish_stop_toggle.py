from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.config.settings import Settings
from app.config.strategy import bearish_stop_loss_enabled
from app.bearish.executor import BearishPutOptionExecutor
from app.options import brackets


@pytest.mark.parametrize('mode,enabled,expected', [('paper', False, True), ('live', False, False), ('live', True, True)])
def test_toggle_is_live_only(mode, enabled, expected):
    settings = Settings(_env_file=None, WEBULL_TRADING_MODE=mode, WEBULL_ENDPOINT='',
                        WEBULL_LIVE_BEARISH_STOP_LOSS_ENABLED=enabled)
    assert bearish_stop_loss_enabled(settings) is expected


@pytest.mark.parametrize('enabled', [True, False])
def test_actual_put_orders_keep_profit_and_conditionally_include_stop(monkeypatch, enabled):
    monkeypatch.setattr(brackets, '_find_valid_contract', lambda *a, **kw: ('2026-12-18', 100, 'TEST-PUT'))
    monkeypatch.setattr('app.execution.daily_budget.reserve_live_option_budget', lambda *a: None)
    sdk = SimpleNamespace(order_v3=SimpleNamespace(place_order=Mock(return_value=SimpleNamespace(
        status_code=200, json=lambda: {}))))
    def builder(**kw):
        return brackets.buy_put_with_bracket(**kw, trade_client=sdk)
    executor = BearishPutOptionExecutor(SimpleNamespace(buy_put_with_bracket=builder))
    executor.submit(account_id='test', symbol='TEST', strike=100, expiration='2026-12-18',
                    quantity=1, entry_limit=2, stop_loss_enabled=enabled)
    orders = sdk.order_v3.place_order.call_args.args[1]
    assert [o['combo_type'] for o in orders] == ['MASTER', 'STOP_PROFIT'] + (['STOP_LOSS'] if enabled else [])
    assert orders[1]['limit_price'] == '2.40'
    assert orders[1]['side'] == 'SELL'
    assert orders[0]['quantity'] == '1'


def test_main_passes_live_stop_toggle_to_lifecycle(monkeypatch, tmp_path):
    from app.execution import submitter
    settings = SimpleNamespace(
        dry_run=False,
        webull_trading_mode='live',
        options_margin_account_number='test',
        live_options_daily_limit_usd=0,
        live_bearish_stop_loss_enabled=False,
        database_path=str(tmp_path / 'bearish.sqlite3'),
    )
    monkeypatch.setattr(submitter, '_load_webull_option_module', lambda: SimpleNamespace(get_account_id=lambda **kw: 'test'))
    submit = Mock(return_value={
        'status': 'entry_pending', 'entry_client_order_id': 'entry',
        'entry_order_id': None, 'contract_symbol': 'TEST-PUT',
        'expiration': '2026-12-18', 'strike': 100, 'quantity': 1,
    })
    lifecycle = Mock()
    lifecycle.submit_entry = submit
    monkeypatch.setattr('app.bearish.lifecycle.BearishPutLifecycle', lambda *a, **kw: lifecycle)
    monkeypatch.setattr('app.bearish.lifecycle.start_bearish_put_reconciler', lambda path: None)
    submitter.submit_paper_order({'action': 'sell_short', 'symbol': 'TEST'}, settings, 'test',
        SimpleNamespace(direction='bearish', entry_price=100))
    assert submit.call_args.kwargs['stop_loss_enabled'] is False
