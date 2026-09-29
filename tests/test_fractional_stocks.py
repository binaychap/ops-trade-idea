from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.bullish import stock_bracket
from app.api import trading
from app.broker.stocks import StockExecution


def test_fixed_cash_payload_ignores_legacy_quantity_and_prices(monkeypatch):
    monkeypatch.setattr(stock_bracket, 'current_stock_quote', lambda s: {'price': 500})
    place = Mock(return_value=SimpleNamespace(status_code=200, json=lambda: {}))
    result = stock_bracket.buy_stock('account', 'AAPL', 99, 500, 450, 550,
                                    trade_client=SimpleNamespace(order_v3=SimpleNamespace(place_order=place)))
    order = place.call_args.args[1][0]
    assert order['total_cash_amount'] == '100.00'
    assert order['entrust_type'] == 'AMOUNT'
    assert order['order_type'] == 'MARKET'
    assert order['time_in_force'] == 'DAY'
    assert order['combo_type'] == 'NORMAL'
    assert 'quantity' not in order and 'limit_price' not in order
    assert result['entry_id'] == order['client_order_id']
    assert result['profit_id'] is None


@pytest.mark.parametrize('price', [100, 25, 0, float('nan'), float('inf')])
def test_unsupported_amount_blocks_before_intent_or_submission(monkeypatch, price):
    monkeypatch.setattr(stock_bracket, 'current_stock_quote', lambda s: {'price': price})
    before, client = Mock(), Mock()
    with pytest.raises(ValueError):
        stock_bracket.buy_stock('account', 'AAPL', trade_client=client, before_submit=before)
    before.assert_not_called()
    client.order_v3.place_order.assert_not_called()


def test_manual_preview_and_submission_agree_on_fixed_amount(monkeypatch):
    settings = SimpleNamespace(max_notional_usd=250, dry_run=False, bullish_stock_account_number='test')
    monkeypatch.setattr(trading, '_get_settings', lambda: settings)
    monkeypatch.setattr(trading, '_market_open', lambda: True)
    monkeypatch.setattr(trading, 'current_stock_quote', lambda *a, **k: {'price': 500})
    ticket = trading.OrderTicket(symbol='AAPL', side='buy', order_type='limit', quantity=99, limit_price=1)
    preview = trading._preview_ticket(ticket)
    assert not trading._blocking_failures(preview)
    assert preview['estimated_notional'] == 100
    assert preview['quantity'] is None
    assert preview['limit_price'] is None
    assert preview['order_type'] == 'market'
    buy = Mock(return_value={'entry_id': 'manual-id'})
    monkeypatch.setattr(trading, '_load_stock_module', lambda: SimpleNamespace(buy_stock=buy))
    monkeypatch.setattr(trading, 'bullish_stock_account_id', lambda *a: 'test')
    monkeypatch.setattr(trading, 'get_trade_client', Mock())
    result = trading._submit_stock_order(preview, 'manual-id', settings)
    assert result['order_id'] == 'manual-id'
    assert buy.call_args.kwargs['client_order_id'] == 'manual-id'
    assert 'quantity' not in buy.call_args.kwargs


def test_amount_order_detail_does_not_require_requested_share_quantity():
    raw = dict(client_order_id='entry', symbol='AAPL', side='BUY', instrument_type='EQUITY',
               status='FILLED', entrust_type='AMOUNT', filled_quantity='0.2',
               filled_time_at='2026-09-28T15:00:00Z')
    response = SimpleNamespace(status_code=200, json=lambda: {'orders': [raw]})
    client = SimpleNamespace(order_v3=SimpleNamespace(get_order_detail=lambda *a: response))
    order = StockExecution(client).order('test', 'entry', 'AAPL', 'BUY')
    assert order.filled == Decimal('0.2')
    assert order.total is None


def test_below_cap_stock_dry_run_is_blocked_without_broker(monkeypatch):
    from app.execution import submitter
    monkeypatch.setattr(submitter, '_load_webull_stock_module', lambda: pytest.fail('broker loaded'))
    settings = SimpleNamespace(dry_run=True, max_notional_usd=50)
    result = submitter.submit_paper_order({'action': 'buy', 'symbol': 'AAPL'}, settings, 'test')
    assert result['skipped']
    settings.max_notional_usd = 250
    result = submitter.submit_paper_order({'action': 'buy', 'symbol': 'AAPL', 'notional_usd': 250}, settings, 'test')
    assert result['notional_usd'] == 100
    assert result['time_in_force'] == 'DAY'


def test_fractional_limit_sell_is_blocked(monkeypatch):
    monkeypatch.setattr(trading, '_get_settings', lambda: SimpleNamespace(max_notional_usd=250, dry_run=True))
    monkeypatch.setattr(trading, '_market_open', lambda: True)
    monkeypatch.setattr(trading, 'current_stock_quote', lambda *a, **k: {'price': 500})
    monkeypatch.setattr(trading, '_resolve_account', lambda: ('test', None))
    monkeypatch.setattr(trading, '_fetch_position_rows', lambda a: [{'symbol': 'AAPL', 'quantity': 0.5}])
    preview = trading._preview_ticket(trading.OrderTicket(symbol='AAPL', side='sell', order_type='limit', quantity=0.2, limit_price=500))
    assert 'fractional sells require a market order' in trading._blocking_failures(preview)
