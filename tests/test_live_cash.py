from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.execution.live_stock import submit_live_stock
from app.exits.live_cash import CashExitScheduler
from app.broker.stocks import StockExecution, StockOrder, OrderNotFound
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
        quote_provider=lambda s: {'price': 200}, **kwargs)


def test_live_cash_request_has_amount_and_no_quantity_or_brackets(tmp_path):
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
    assert job['kind'] == 'live_cash' and job['entry_id'] == result['client_order_id']
    assert job['manage_exits'] is False
    assert 'profit_percent' not in job and 'stop_loss_percent' not in job
    assert result['exit_management'] == 'none'


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
                               market_open=lambda: True, quote_provider=lambda s: {'price': price})
    assert result['skipped']
    resolver.assert_not_called()


def test_lower_cap_blocks_instead_of_silently_spending_more(tmp_path):
    config = settings(tmp_path)
    config.max_notional_usd = 50
    with pytest.raises(ValueError, match='budget'):
        submit_live_stock('AAPL', config, 'test', account_resolver=Mock())


def setup_exit(tmp_path, price=220):
    submit(tmp_path, trade_client=client())
    ledger = Ledger(settings(tmp_path).database_path)
    job = ledger.exit_jobs()[0]
    job.update(manage_exits=True, profit_percent=10, stop_loss_percent=5,
               next_day_exit=False, exit_time='09:35')
    ledger.save_exit_job(job)
    entry = StockOrder(job['entry_id'], 'FILLED', Decimal('.5'), Decimal('.5'), NOW, Decimal(200))
    broker = SimpleNamespace(order=Mock(return_value=entry),
        position=Mock(return_value=Decimal('.5')), market_sell=Mock(), cancel=Mock())
    scheduler = CashExitScheduler(ledger, broker,
        calendar=SimpleNamespace(is_open=lambda now: True), quote_provider=lambda s: {'price': price})
    return ledger, job, broker, scheduler, entry


@pytest.mark.parametrize('price,expected', [(220, 'take_profit'), (190, 'stop_loss'), (200, None)])
def test_cash_exits_use_actual_fills_and_configured_thresholds(tmp_path, price, expected):
    ledger, job, broker, scheduler, entry = setup_exit(tmp_path, price)
    scheduler.run_once(NOW)
    saved = ledger.exit_jobs()[0]
    assert saved.get('exit_trigger') == expected
    if expected:
        assert broker.market_sell.call_args.args[2] == Decimal('.5')
        assert len(saved['market_orders']) == 1
    else:
        broker.market_sell.assert_not_called()


def test_live_cash_entry_reconciles_without_automatic_exit(tmp_path):
    ledger, job, broker, scheduler, entry = setup_exit(tmp_path, price=220)
    job.pop('manage_exits')
    ledger.save_exit_job(job)
    scheduler.quote_provider = Mock(side_effect=AssertionError('exit quote must not be read'))
    scheduler.run_once(NOW)
    broker.market_sell.assert_not_called()
    assert ledger.exit_jobs() == []


def test_disabled_exit_job_reconciles_existing_sell_without_replacing_it(tmp_path):
    ledger, job, broker, scheduler, entry = setup_exit(tmp_path)
    sell = StockOrder('existing-sell', 'CANCELLED', Decimal('.2'), Decimal('.5'), NOW)
    job.pop('manage_exits')
    job['market_orders'] = [{'id': 'existing-sell', 'quantity': '.5', 'status': 'submitting'}]
    ledger.save_exit_job(job)
    broker.order.side_effect = lambda a, oid, s, side, **kw: entry if side == 'BUY' else sell

    scheduler.run_once(NOW)

    broker.market_sell.assert_not_called()
    assert ledger.exit_jobs() == []


def test_lost_exit_response_is_not_replayed_after_restart(tmp_path):
    ledger, job, broker, scheduler, entry = setup_exit(tmp_path)
    broker.market_sell.side_effect = TimeoutError()
    scheduler.run_once(NOW)
    broker.order.side_effect = lambda a, oid, s, side, **kw: entry if side == 'BUY' else (_ for _ in ()).throw(OrderNotFound())
    restarted = CashExitScheduler(ledger, broker, scheduler.calendar, scheduler.quote_provider)
    restarted.run_once(NOW)
    assert broker.market_sell.call_count == 1
    assert ledger.exit_jobs()[0]['last_error']
    assert ledger.exit_jobs()[0]['next_check_at'] is not None


def test_confirmed_exit_fill_completes_tracking(tmp_path):
    ledger, job, broker, scheduler, entry = setup_exit(tmp_path)
    scheduler.run_once(NOW)
    saved = ledger.exit_jobs()[0]
    sell = StockOrder(saved['market_orders'][0]['id'], 'FILLED', Decimal('.5'), Decimal('.5'), NOW)
    broker.order.side_effect = lambda a, oid, s, side, **kw: entry if side == 'BUY' else sell
    scheduler.run_once(NOW)
    assert ledger.exit_jobs() == []
    assert broker.market_sell.call_count == 1


def test_cash_entry_parser_does_not_require_fixed_quantity():
    # Production order detail never echoes total_cash_amount (place-order
    # request field only); the parser must reconcile without it.
    raw = {'client_order_id': 'entry', 'symbol': 'AAPL', 'side': 'BUY',
           'instrument_type': 'EQUITY', 'status': 'FILLED', 'filled_quantity': '.5',
           'filled_price': '200', 'entrust_type': 'AMOUNT',
           'filled_time_at': NOW.isoformat()}
    sdk = SimpleNamespace(order_v3=SimpleNamespace(get_order_detail=lambda *a: SimpleNamespace(
        status_code=200, json=lambda: {'orders': [raw]})))
    order = StockExecution(sdk).order('account', 'entry', 'AAPL', 'BUY', cash_amount='100')
    assert order.filled == Decimal('.5') and order.filled_price == Decimal(200)


def test_cash_entry_parser_defers_without_crash_on_missing_fill_price():
    raw = {'client_order_id': 'entry', 'symbol': 'AAPL', 'side': 'BUY',
           'instrument_type': 'EQUITY', 'status': 'FILLED', 'filled_quantity': '.5',
           'entrust_type': 'AMOUNT', 'filled_time_at': NOW.isoformat()}
    sdk = SimpleNamespace(order_v3=SimpleNamespace(get_order_detail=lambda *a: SimpleNamespace(
        status_code=200, json=lambda: {'orders': [raw]})))
    with pytest.raises(ValueError, match='fill price missing'):
        StockExecution(sdk).order('account', 'entry', 'AAPL', 'BUY', cash_amount='100')


def test_cash_entry_parser_rejects_non_amount_entrust_type():
    raw = {'client_order_id': 'entry', 'symbol': 'AAPL', 'side': 'BUY',
           'instrument_type': 'EQUITY', 'status': 'FILLED', 'filled_quantity': '.5',
           'filled_price': '200', 'entrust_type': 'QTY', 'total_quantity': '1',
           'filled_time_at': NOW.isoformat()}
    sdk = SimpleNamespace(order_v3=SimpleNamespace(get_order_detail=lambda *a: SimpleNamespace(
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
        submitter.submit_paper_order(dict(action=action, symbol='AAPL', strategy='iron_condor' if direction == 'neutral' else 'placeholder', notional_usd=250),
            config, 'test', SimpleNamespace(direction=direction))
    assert helper.call_count == 1
    config.webull_trading_mode = 'paper'
    submitter.submit_paper_order(dict(action='buy', symbol='AAPL'), config, 'test', SimpleNamespace(direction='bullish'))
    assert helper.call_count == 1


def test_bullish_runner_uses_cash_path_and_persists_entry(tmp_path, monkeypatch):
    from app.bullish.runner import MainTopBullish
    config = settings(tmp_path)
    config.account_number = 'test-number'
    sdk = client()
    monkeypatch.setattr('app.broker.client.get_trade_client', lambda: sdk)
    bracket = Mock(side_effect=AssertionError('bracket called'))
    bot = MainTopBullish(settings=config, market_open=lambda: True,
        quote_provider=lambda s: {'price': 200},
        stock_loader=lambda: SimpleNamespace(get_account_id=lambda **kw: 'test-account', buy_stock=bracket))
    item = {'symbol': 'AAPL', 'total_premium': 1000, 'trade_count': 1}
    assert bot.process(item)['status'] == 'submitted'
    assert bot.process(item)['status'] == 'skipped'
    assert sdk.order_v3.place_order.call_count == 1
    bracket.assert_not_called()


def test_partial_entry_cancels_before_exit(tmp_path):
    ledger, job, broker, scheduler, entry = setup_exit(tmp_path)
    broker.order.return_value = StockOrder(job['entry_id'], 'PARTIAL_FILLED', Decimal('.2'), Decimal('.2'), NOW, Decimal(200))
    scheduler.run_once(NOW)
    broker.cancel.assert_called_once_with('test-account', job['entry_id'])
    broker.market_sell.assert_not_called()


def test_morning_liquidation_uses_cash_worker_without_quote(tmp_path):
    ledger, job, broker, scheduler, entry = setup_exit(tmp_path, price=200)
    job['liquidate_requested'] = True
    ledger.save_exit_job(job)
    scheduler.quote_provider = Mock(side_effect=AssertionError('quote not needed'))
    scheduler.run_once(NOW)
    assert ledger.exit_jobs()[0]['exit_trigger'] == 'morning_sell'
    broker.market_sell.assert_called_once()


def test_next_day_liquidation_uses_persisted_deadline(tmp_path):
    ledger, job, broker, scheduler, entry = setup_exit(tmp_path, price=200)
    job.update(next_day_exit=True, due_at=NOW.isoformat())
    ledger.save_exit_job(job)
    scheduler.run_once(NOW)
    assert ledger.exit_jobs()[0]['exit_trigger'] == 'next_day'
    broker.market_sell.assert_called_once()


def test_exit_waits_for_active_partial_sell(tmp_path):
    ledger, job, broker, scheduler, entry = setup_exit(tmp_path)
    scheduler.run_once(NOW)
    saved = ledger.exit_jobs()[0]
    sell = StockOrder(saved['market_orders'][0]['id'], 'PARTIAL_FILLED', Decimal('.2'), Decimal('.5'), NOW)
    broker.order.side_effect = lambda a, oid, s, side, **kw: entry if side == 'BUY' else sell
    scheduler.run_once(NOW)
    assert broker.market_sell.call_count == 1
    assert ledger.exit_jobs()[0]['remaining_quantity'] == '0.3'


def test_terminal_partial_sell_retries_only_remaining_shares(tmp_path):
    ledger, job, broker, scheduler, entry = setup_exit(tmp_path)
    scheduler.run_once(NOW)
    saved = ledger.exit_jobs()[0]
    sell = StockOrder(saved['market_orders'][0]['id'], 'CANCELLED', Decimal('.2'), Decimal('.5'), NOW)
    broker.order.side_effect = lambda a, oid, s, side, **kw: entry if side == 'BUY' else sell
    scheduler.run_once(NOW)
    assert broker.market_sell.call_count == 2
    assert broker.market_sell.call_args.args[2] == Decimal('.3')


def test_missing_holding_blocks_managed_sale(tmp_path):
    ledger, job, broker, scheduler, entry = setup_exit(tmp_path)
    broker.position.return_value = Decimal(0)
    scheduler.run_once(NOW)
    broker.market_sell.assert_not_called()
    assert 'Holding below' in ledger.exit_jobs()[0]['last_error']


def test_legacy_exit_worker_leaves_cash_jobs_alone(tmp_path):
    from app.exits.next_day import ExitScheduler
    ledger, job, broker, scheduler, entry = setup_exit(tmp_path)
    ExitScheduler(ledger, broker, scheduler.calendar).run_once(NOW)
    broker.order.assert_not_called()


def test_morning_worker_delegates_and_does_not_complete_cash_job(tmp_path):
    from app.exits.morning_sell import MorningSellCalendar, MorningSellScheduler
    ledger, job, broker, scheduler, entry = setup_exit(tmp_path)
    broker.stock_positions = Mock(return_value={'AAPL': Decimal('.5')})
    morning = MorningSellScheduler(ledger, broker,
        MorningSellCalendar('10:00', 'America/New_York'),
        resolve_account=lambda: 'test-account', dry_run=False)
    morning.run_once(NOW)
    broker.market_sell.assert_not_called()
    saved = ledger.exit_jobs()[0]
    assert saved['liquidate_requested'] is True
    assert saved['status'] != 'complete'


def test_closed_market_and_stale_quotes_do_not_exit(tmp_path):
    ledger, job, broker, scheduler, entry = setup_exit(tmp_path)
    scheduler.calendar.is_open = lambda now: False
    scheduler.run_once(NOW)
    broker.market_sell.assert_not_called()
    scheduler.calendar.is_open = lambda now: True
    scheduler.quote_provider = Mock(side_effect=ValueError('stale quote'))
    scheduler.run_once(NOW)
    broker.market_sell.assert_not_called()
    assert ledger.exit_jobs()[0]['last_error'] == 'stale quote'


@pytest.mark.parametrize('price,count', [(200, 1), (100, 2), (30, 4), (10, 11), (5.01, 20)])
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
        quote_provider=lambda s: {'price': 30}, trade_client=sdk)
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
                  quote_provider=lambda s: {'price': 30}, trade_client=sdk)
    with pytest.raises(TimeoutError):
        submit_live_stock('AAPL', settings(tmp_path), 'test', **kwargs)
    job = Ledger(settings(tmp_path).database_path).exit_jobs()[0]
    assert [leg['submission_status'] for leg in job['entries']] == ['acknowledged', 'submitting', 'planned', 'planned']
    assert submit_live_stock('AAPL', settings(tmp_path), 'test', **kwargs)['skipped']
    assert sdk.order_v3.place_order.call_count == 2


def test_split_exit_aggregates_fills_and_sells_whole_then_fraction(tmp_path):
    ledger, job, broker, scheduler, entry = setup_exit(tmp_path, price=33)
    job['entries'] = [dict(id=f'leg-{i}', cash_amount='25', submission_status='acknowledged') for i in range(4)]
    ledger.save_exit_job(job)
    fills = {f'leg-{i}': StockOrder(f'leg-{i}', 'FILLED', Decimal('.833333'), Decimal('.833333'), NOW, Decimal(30)) for i in range(4)}
    broker.order.side_effect = lambda a, oid, s, side, **kw: fills[oid]
    broker.position.return_value = Decimal('3.333332')
    scheduler.run_once(NOW)
    assert broker.market_sell.call_args.args[2] == 3
    saved = ledger.exit_jobs()[0]
    assert Decimal(saved['entry_price']) == 30
    sell_id = saved['market_orders'][0]['id']
    fills[sell_id] = StockOrder(sell_id, 'FILLED', Decimal(3), Decimal(3), NOW)
    scheduler.run_once(NOW)
    assert broker.market_sell.call_args.args[2] == Decimal('.333332')


def test_crash_leaves_unsubmitted_cash_legs_unsubmitted(tmp_path):
    ledger, job, broker, scheduler, entry = setup_exit(tmp_path, price=200)
    job['entries'].append({'id': 'never-sent', 'cash_amount': '25', 'submission_status': 'planned'})
    ledger.save_exit_job(job)
    scheduler.run_once(NOW)
    broker.order.assert_called_once()
    assert ledger.exit_jobs()[0]['entries'][1]['submission_status'] == 'not_submitted'


@pytest.mark.parametrize('echo', [None, '100', '99'])
def test_cash_detail_optional_amount_echo(echo, monkeypatch):
    raw = dict(client_order_id='entry', symbol='AAPL', side='BUY',
               instrument_type='EQUITY', status='FILLED', filled_quantity='.5',
               filled_price='200', entrust_type='AMOUNT')
    if echo is not None:
        raw['total_cash_amount'] = echo
    broker = StockExecution(None)
    monkeypatch.setattr(broker, '_order_detail', lambda *a: SimpleNamespace(
        status_code=200, json=lambda: {'orders': [raw]}))
    if echo == '99':
        with pytest.raises(ValueError, match='persisted intent'):
            broker.order('account', 'entry', 'AAPL', 'BUY', cash_amount='100')
    else:
        assert broker.order('account', 'entry', 'AAPL', 'BUY', cash_amount='100').filled == Decimal('.5')
    del raw['filled_quantity']
    with pytest.raises(KeyError):
        broker.order('account', 'entry', 'AAPL', 'BUY', cash_amount='100')


def test_cash_rate_limit_persists_queue_cooldown(tmp_path):
    from datetime import timedelta
    ledger, job, broker, scheduler, entry = setup_exit(tmp_path)
    other = dict(job, id='another', symbol='MSFT')
    ledger.register_exit_job(other)
    broker.order.side_effect = RuntimeError(
        'HTTP Status: 429, Code: TOO_MANY_REQUESTS, Msg: Too many requests, RequestID: test'
    )
    scheduler.run_once(NOW)
    assert broker.order.call_count == 1
    assert all(j['next_check_at'] == (NOW + timedelta(seconds=60)).isoformat() for j in ledger.exit_jobs())
    scheduler.run_once(NOW + timedelta(seconds=30))
    assert broker.order.call_count == 1
    scheduler.run_once(NOW + timedelta(seconds=60))
    assert broker.order.call_count == 2
    assert all(j['next_check_at'] == (NOW + timedelta(seconds=180)).isoformat() for j in ledger.exit_jobs())
    broker.market_sell.assert_not_called()


def test_order_detail_pacing_shared_between_instances(monkeypatch):
    clock = [0.0]
    waits = []
    def sleep(seconds):
        waits.append(seconds)
        clock[0] += seconds
    monkeypatch.setattr('app.broker.stocks.time.monotonic', lambda: clock[0])
    monkeypatch.setattr('app.broker.stocks.time.sleep', sleep)
    monkeypatch.setattr(StockExecution, '_next_detail_at', 0)
    sdk = SimpleNamespace(order_v3=SimpleNamespace(get_order_detail=lambda *a: None))
    StockExecution(sdk)._order_detail('a', 'one')
    StockExecution(sdk)._order_detail('a', 'two')
    assert waits == [2.1]
