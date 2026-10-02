from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.broker.stocks import StockExecution
from app.execution.live_stock import submit_live_stock
from app.persistence.ledger import Ledger

NOW = datetime(2026, 9, 28, 15, tzinfo=UTC)


def settings(tmp_path, **kwargs):
    return SimpleNamespace(webull_trading_mode='live', dry_run=False,
                           database_path=str(tmp_path / 'test.db'),
                           max_notional_usd=250, live_bullish_amount_usd=100,
                           **kwargs)


def client():
    return SimpleNamespace(
        account_v2=SimpleNamespace(get_account_position=Mock(return_value=SimpleNamespace(
            status_code=200, json=lambda: []))),
        order_v3=SimpleNamespace(place_order=Mock(return_value=SimpleNamespace(
            status_code=200, json=lambda: {'client_order_id': 'receipt'}))))


def submit(tmp_path, **kwargs):
    return submit_live_stock('AAPL', settings(tmp_path), 'fingerprint',
        account_resolver=lambda: 'test-account', market_open=lambda: True,
        quote_provider=lambda symbol: {'price': 200}, **kwargs)


def test_live_cash_buy_is_market_order_and_manually_managed(tmp_path):
    broker = client()
    result = submit(tmp_path, trade_client=broker)
    account, orders = broker.order_v3.place_order.call_args.args
    assert account == 'test-account'
    assert orders == [result['order']]
    assert orders[0]['total_cash_amount'] == '100.00'
    assert orders[0]['entrust_type'] == 'AMOUNT'
    assert orders[0]['order_type'] == 'MARKET'
    assert orders[0]['combo_type'] == 'NORMAL'
    assert 'quantity' not in orders[0] and 'limit_price' not in orders[0]
    job = Ledger(settings(tmp_path).database_path).exit_jobs()[0]
    assert job['kind'] == 'live_cash'
    assert job['status'] == 'manual_management'
    assert job['manage_exits'] is False
    assert 'profit_percent' not in job and 'stop_loss_percent' not in job
    assert result['exit_management'] == 'manual'


def test_legacy_cash_job_does_not_block_a_new_buy(tmp_path):
    config = settings(tmp_path)
    ledger = Ledger(config.database_path)
    ledger.register_exit_job({
        'id': 'old-cash-job', 'kind': 'live_cash', 'account_id': 'test-account',
        'symbol': 'AAPL', 'status': 'waiting_entry',
    })
    broker = client()
    result = submit_live_stock('AAPL', config, 'new-fingerprint',
        account_resolver=lambda: 'test-account', market_open=lambda: True,
        quote_provider=lambda symbol: {'price': 200}, trade_client=broker)
    assert result['status'] == 'submitted'
    assert broker.order_v3.place_order.call_count == 1


def test_timeout_never_replays_cash_buy(tmp_path):
    broker = client()
    broker.order_v3.place_order.side_effect = TimeoutError()
    with pytest.raises(TimeoutError):
        submit(tmp_path, trade_client=broker)
    assert submit(tmp_path, trade_client=broker)['skipped']
    assert broker.order_v3.place_order.call_count == 1


def test_dry_run_is_broker_free_and_previews_cash(tmp_path):
    config = settings(tmp_path)
    config.dry_run = True
    result = submit_live_stock('AAPL', config, 'test', account_resolver=Mock(side_effect=AssertionError),
                               quote_provider=Mock(side_effect=AssertionError))
    assert result['dry_run'] and result['notional_usd'] == 100
    assert not (tmp_path / 'test.db').exists()


@pytest.mark.parametrize('price', [1, 5])
def test_unsupported_cash_amount_does_not_submit(tmp_path, price):
    resolver = Mock(side_effect=AssertionError)
    result = submit_live_stock('AAPL', settings(tmp_path), 'test', account_resolver=resolver,
                               market_open=lambda: True, quote_provider=lambda symbol: {'price': price})
    assert result['skipped']
    resolver.assert_not_called()


def test_lower_cap_blocks_instead_of_silently_spending_more(tmp_path):
    config = settings(tmp_path)
    config.max_notional_usd = 50
    with pytest.raises(ValueError, match='budget'):
        submit_live_stock('AAPL', config, 'test', account_resolver=Mock())


def test_cash_entry_parser_does_not_require_fixed_quantity():
    # Production order detail never echoes total_cash_amount (place-order
    # request field only); the parser must reconcile without it.
    raw = {'client_order_id': 'entry', 'symbol': 'AAPL', 'side': 'BUY',
           'instrument_type': 'EQUITY', 'status': 'FILLED', 'filled_quantity': '.5',
           'filled_price': '200', 'entrust_type': 'AMOUNT',
           'filled_time_at': NOW.isoformat()}
    sdk = SimpleNamespace(order_v3=SimpleNamespace(get_order_detail=lambda *args: SimpleNamespace(
        status_code=200, json=lambda: {'orders': [raw]})))
    order = StockExecution(sdk).order('account', 'entry', 'AAPL', 'BUY', cash_amount='100')
    assert order.filled == Decimal('.5') and order.filled_price == Decimal(200)


def test_cash_entry_parser_defers_without_crash_on_missing_fill_price():
    raw = {'client_order_id': 'entry', 'symbol': 'AAPL', 'side': 'BUY',
           'instrument_type': 'EQUITY', 'status': 'FILLED', 'filled_quantity': '.5',
           'entrust_type': 'AMOUNT', 'filled_time_at': NOW.isoformat()}
    sdk = SimpleNamespace(order_v3=SimpleNamespace(get_order_detail=lambda *args: SimpleNamespace(
        status_code=200, json=lambda: {'orders': [raw]})))
    with pytest.raises(ValueError, match='fill price missing'):
        StockExecution(sdk).order('account', 'entry', 'AAPL', 'BUY', cash_amount='100')


def test_cash_entry_parser_rejects_non_amount_entrust_type():
    raw = {'client_order_id': 'entry', 'symbol': 'AAPL', 'side': 'BUY',
           'instrument_type': 'EQUITY', 'status': 'FILLED', 'filled_quantity': '.5',
           'filled_price': '200', 'entrust_type': 'QTY', 'total_quantity': '1',
           'filled_time_at': NOW.isoformat()}
    sdk = SimpleNamespace(order_v3=SimpleNamespace(get_order_detail=lambda *args: SimpleNamespace(
        status_code=200, json=lambda: {'orders': [raw]})))
    with pytest.raises(ValueError, match='differs from persisted intent'):
        StockExecution(sdk).order('account', 'entry', 'AAPL', 'BUY', cash_amount='100')


def test_main_routes_only_live_bullish_stocks_to_cash(tmp_path, monkeypatch):
    from app.execution import submitter
    helper = Mock(return_value={'dry_run': True})
    monkeypatch.setattr('app.execution.live_stock.submit_live_stock', helper)
    config = settings(tmp_path)
    config.dry_run = True
    for direction, action in [('bullish', 'buy'), ('bearish', 'sell_short'), ('neutral', 'buy')]:
        submitter.submit_paper_order(dict(action=action, symbol='AAPL',
            strategy='iron_condor' if direction == 'neutral' else 'placeholder', notional_usd=250),
            config, 'test', SimpleNamespace(direction=direction))
    assert helper.call_count == 1
    config.webull_trading_mode = 'paper'
    submitter.submit_paper_order(dict(action='buy', symbol='AAPL'), config, 'test',
                                 SimpleNamespace(direction='bullish'))
    assert helper.call_count == 1


def test_bullish_runner_uses_cash_path_and_persists_entry(tmp_path, monkeypatch):
    from app.bullish.runner import MainTopBullish
    config = settings(tmp_path)
    config.account_number = 'test-number'
    sdk = client()
    monkeypatch.setattr('app.broker.client.get_trade_client', lambda: sdk)
    bracket = Mock(side_effect=AssertionError('bracket called'))
    bot = MainTopBullish(settings=config, market_open=lambda: True,
        quote_provider=lambda symbol: {'price': 200},
        stock_loader=lambda: SimpleNamespace(
            get_account_id=lambda **kwargs: 'test-account', buy_stock=bracket))
    item = {'symbol': 'AAPL', 'total_premium': 1000, 'trade_count': 1}
    assert bot.process(item)['status'] == 'submitted'
    assert bot.process(item)['status'] == 'skipped'
    assert sdk.order_v3.place_order.call_count == 1
    bracket.assert_not_called()


@pytest.mark.parametrize(('price', 'count'), [(200, 1), (100, 2), (30, 4), (10, 11), (5.01, 20)])
def test_split_cash_amounts_total_exact_budget(price, count):
    from app.execution.live_stock import split_cash_amount
    parts = split_cash_amount(100, price)
    assert len(parts) == count
    assert sum(parts) == Decimal(100)
    assert all(5 <= part < Decimal(str(price)) for part in parts)


def test_stock_under_budget_submits_multiple_cash_orders(tmp_path):
    sdk = client()
    result = submit_live_stock('AAPL', settings(tmp_path), 'test',
        account_resolver=lambda: 'test-account', market_open=lambda: True,
        quote_provider=lambda symbol: {'price': 30}, trade_client=sdk)
    orders = [call.args[1][0] for call in sdk.order_v3.place_order.call_args_list]
    assert len(orders) == 4
    assert {order['total_cash_amount'] for order in orders} == {'25.00'}
    assert len({order['client_order_id'] for order in orders}) == 4
    assert result['submitted_cash_amount'] == 100
    assert all('quantity' not in order for order in orders)


def test_split_timeout_preserves_attempted_legs_and_never_replays(tmp_path):
    sdk = client()
    sdk.order_v3.place_order.side_effect = [SimpleNamespace(status_code=200, json=lambda: {}), TimeoutError()]
    kwargs = dict(account_resolver=lambda: 'test-account', market_open=lambda: True,
                  quote_provider=lambda symbol: {'price': 30}, trade_client=sdk)
    with pytest.raises(TimeoutError):
        submit_live_stock('AAPL', settings(tmp_path), 'test', **kwargs)
    job = Ledger(settings(tmp_path).database_path).exit_jobs()[0]
    assert [leg['submission_status'] for leg in job['entries']] == [
        'acknowledged', 'submitting', 'planned', 'planned']
    assert submit_live_stock('AAPL', settings(tmp_path), 'test', **kwargs)['skipped']
    assert sdk.order_v3.place_order.call_count == 2


@pytest.mark.parametrize('echo', [None, '100', '99'])
def test_cash_detail_optional_amount_echo(echo, monkeypatch):
    raw = dict(client_order_id='entry', symbol='AAPL', side='BUY',
               instrument_type='EQUITY', status='FILLED', filled_quantity='.5',
               filled_price='200', entrust_type='AMOUNT')
    if echo is not None:
        raw['total_cash_amount'] = echo
    broker = StockExecution(None)
    monkeypatch.setattr(broker, '_order_detail', lambda *args: SimpleNamespace(
        status_code=200, json=lambda: {'orders': [raw]}))
    if echo == '99':
        with pytest.raises(ValueError, match='persisted intent'):
            broker.order('account', 'entry', 'AAPL', 'BUY', cash_amount='100')
    else:
        assert broker.order('account', 'entry', 'AAPL', 'BUY', cash_amount='100').filled == Decimal('.5')
    del raw['filled_quantity']
    with pytest.raises(KeyError):
        broker.order('account', 'entry', 'AAPL', 'BUY', cash_amount='100')


def test_morning_sell_skips_manually_managed_cash_position(tmp_path):
    from app.exits.morning_sell import MorningSellCalendar, MorningSellScheduler
    submit(tmp_path, trade_client=client())
    ledger = Ledger(settings(tmp_path).database_path)
    broker = SimpleNamespace(stock_positions=Mock(return_value={'AAPL': Decimal('.5')}),
        market_sell=Mock(), position=Mock(return_value=Decimal('.5')))
    scheduler = MorningSellScheduler(ledger, broker,
        MorningSellCalendar('10:00', 'America/New_York'),
        resolve_account=lambda: 'test-account', dry_run=False)
    result = scheduler.run_once(NOW)
    broker.market_sell.assert_not_called()
    assert result['skipped'] == [{'symbol': 'AAPL', 'reason': 'manual_cash_position'}]


def test_order_detail_pacing_shared_between_instances(monkeypatch):
    clock = [0.0]
    waits = []

    def sleep(seconds):
        waits.append(seconds)
        clock[0] += seconds

    monkeypatch.setattr('app.broker.stocks.time.monotonic', lambda: clock[0])
    monkeypatch.setattr('app.broker.stocks.time.sleep', sleep)
    monkeypatch.setattr(StockExecution, '_next_detail_at', 0)
    sdk = SimpleNamespace(order_v3=SimpleNamespace(get_order_detail=lambda *args: None))
    StockExecution(sdk)._order_detail('a', 'one')
    StockExecution(sdk)._order_detail('a', 'two')
    assert waits == [2.1]
