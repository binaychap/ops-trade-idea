from types import SimpleNamespace

import pytest

from app.broker import client as webull_broker


def test_bullish_passes_selected_account_to_order(tmp_path):
    import importlib.util
    from pathlib import Path
    from unittest.mock import Mock
    spec = importlib.util.spec_from_file_location(
        'bullish_account_test', Path(__file__).parents[1] / 'app/bullish/runner.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    stock = SimpleNamespace(get_account_id=Mock(return_value='selected'),
                            buy_stock=Mock(return_value={}))
    bot = module.MainTopBullish(
        settings=SimpleNamespace(dry_run=False, database_path=str(tmp_path / 'test.db'),
                                 max_notional_usd=250, account_number='wanted'),
        stock_loader=lambda: stock,
        quote_provider=lambda symbol: {'price': 200},
        market_open=lambda: True,
    )
    assert bot.process({'symbol': 'TEST', 'total_premium': 100, 'trade_count': 1})['status'] == 'submitted'
    stock.get_account_id.assert_called_once_with(account_number='wanted')
    assert stock.buy_stock.call_args.kwargs['account_id'] == 'selected'


@pytest.mark.parametrize('accounts,expected', [
    ([{'account_number': 'other', 'account_id': 'first'},
      {'account_number': 'wanted', 'account_id': 'selected'}], 'selected'),
    ([{'account_number': 'other', 'account_id': 'first'}], None),
    ([{'account_number': 'wanted', 'account_id': 'one'},
      {'account_number': 'wanted', 'account_id': 'two'}], None),
])
def test_exact_account_selection(monkeypatch, accounts, expected):
    response = SimpleNamespace(status_code=200, json=lambda: accounts)
    client = SimpleNamespace(account_v2=SimpleNamespace(get_account_list=lambda: response))
    monkeypatch.setattr(webull_broker, 'get_trade_client', lambda: client)
    if expected:
        assert webull_broker.get_account_id(account_number='wanted') == expected
    else:
        with pytest.raises(ValueError, match='exactly one'):
            webull_broker.get_account_id(account_number='wanted')
