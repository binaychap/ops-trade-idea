from dataclasses import replace
from datetime import datetime
from decimal import Decimal
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.exits.next_day import ExitCalendar, ExitScheduler
from app.persistence.ledger import Ledger
from app.broker.stocks import StockExecution, StockOrder


def dt(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00'))


FILL = dt('2026-09-04T15:00:00Z')
DUE = dt('2026-09-08T13:35:00Z')  # Labor Day weekend


@pytest.fixture(autouse=True)
def isolate_calendar_settings(monkeypatch):
    from app.main import get_settings

    # Calendar expectations must not depend on the operator's local .env.
    monkeypatch.setenv('NEXT_DAY_EXIT_TIME', '09:35')
    monkeypatch.setenv('NEXT_DAY_EXIT_TIMEZONE', 'America/New_York')
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


class FakeBroker:
    def __init__(self):
        self.orders = {
            'entry': StockOrder('entry', 'FILLED', Decimal(10), Decimal(10), FILL),
            'profit': StockOrder('profit', 'SUBMITTED', Decimal(0), Decimal(10), None),
            'stop': StockOrder('stop', 'SUBMITTED', Decimal(0), Decimal(10), None),
        }
        self.held = Decimal(10)
        self.cancels = []
        self.sells = []
        self.cancel_immediately = True
        self.on_cancel = None
        self.timeout = False

    def order(self, account, order_id, symbol, side):
        if order_id not in self.orders:
            raise RuntimeError('Order state unknown')
        return self.orders[order_id]

    def position(self, account, symbol):
        return self.held

    def cancel(self, account, order_id):
        self.cancels.append(order_id)
        if self.on_cancel:
            self.on_cancel(order_id)
        if self.cancel_immediately and not self.orders[order_id].terminal:
            self.orders[order_id] = replace(self.orders[order_id], status='CANCELLED')

    def market_sell(self, account, symbol, shares, order_id):
        self.sells.append((order_id, shares))
        self.orders[order_id] = StockOrder(order_id, 'SUBMITTED', Decimal(0), shares, None)
        if self.timeout:
            raise TimeoutError('Response lost after acceptance')


@pytest.fixture
def setup(tmp_path):
    ledger = Ledger(str(tmp_path / 'test.sqlite3'))
    ledger.register_exit_job({
        'id': 'trade-1', 'account_id': 'test', 'symbol': 'AAPL', 'quantity': '10',
        'entry_id': 'entry', 'profit_id': 'profit', 'stop_id': 'stop',
        'status': 'waiting_entry', 'market_orders': [], 'due_at': None,
    })
    broker = FakeBroker()
    return ledger, broker, ExitScheduler(ledger, broker)


@pytest.mark.parametrize(('fill', 'expected'), [
    ('2026-09-04T15:00:00Z', '2026-09-08T13:35:00Z'),
    ('2026-03-06T15:00:00Z', '2026-03-09T13:35:00Z'),
    ('2026-10-30T15:00:00Z', '2026-11-02T14:35:00Z'),
    ('2026-07-02T15:00:00Z', '2026-07-06T13:35:00Z'),
])
def test_calendar_holidays_and_dst(fill, expected):
    assert ExitCalendar().next_exit(dt(fill)) == dt(expected)


def test_early_close_and_custom_exit_time():
    calendar = ExitCalendar('15:30')
    assert calendar.next_exit(dt('2026-11-25T15:00:00Z')) == dt('2026-11-27T17:59:00Z')
    assert not calendar.is_open(dt('2026-11-27T18:00:00Z'))


def test_calendar_reads_configured_env_file(monkeypatch, tmp_path):
    from app import main

    env_file = tmp_path / '.env'
    env_file.write_text('NEXT_DAY_EXIT_TIME=10:15\nNEXT_DAY_EXIT_TIMEZONE=America/New_York\n')
    monkeypatch.delenv('NEXT_DAY_EXIT_TIME', raising=False)
    monkeypatch.delenv('NEXT_DAY_EXIT_TIMEZONE', raising=False)
    monkeypatch.setattr(main, 'get_settings', lambda: main.Settings(_env_file=env_file))
    calendar = ExitCalendar()
    assert calendar.exit_time.isoformat() == '10:15:00'
    assert calendar.zone.key == 'America/New_York'
    assert calendar.next_exit(FILL) == dt('2026-09-08T14:15:00Z')
    assert ExitCalendar('11:00').exit_time.isoformat() == '11:00:00'


def test_unfilled_entry_has_no_schedule(setup):
    ledger, broker, worker = setup
    broker.orders['entry'] = replace(broker.orders['entry'], status='SUBMITTED', filled=Decimal(0), filled_at=None)
    worker.run_once(DUE)
    assert ledger.exit_jobs()[0]['due_at'] is None
    assert broker.sells == broker.cancels == []


def test_schedule_before_due_does_not_cancel_protection(setup):
    ledger, broker, worker = setup
    worker.run_once(FILL)
    assert dt(ledger.exit_jobs()[0]['due_at']) == DUE
    assert broker.sells == broker.cancels == []


def test_cancel_confirm_sell_and_finish(setup):
    ledger, broker, worker = setup
    worker.run_once(DUE)
    assert broker.cancels == ['profit', 'stop']
    order_id, shares = broker.sells[0]
    assert shares == 10
    assert ledger.exit_jobs()[0]['market_orders'][0]['id'] == order_id
    broker.orders[order_id] = replace(broker.orders[order_id], status='FILLED', filled=shares)
    broker.held = Decimal(0)
    worker.run_once(DUE)
    assert ledger.exit_jobs() == []
    assert len(broker.sells) == 1


def test_waits_for_actual_cancellation(setup):
    ledger, broker, worker = setup
    broker.cancel_immediately = False
    worker.run_once(DUE)
    assert broker.sells == []
    assert ledger.exit_jobs()[0]['status'] == 'cancelling'


def test_fill_during_cancellation_reduces_sale(setup):
    ledger, broker, worker = setup
    def fill_profit(order_id):
        if order_id == 'profit':
            broker.orders['profit'] = replace(broker.orders['profit'], filled=Decimal(4))
            broker.held = Decimal(6)
    broker.on_cancel = fill_profit
    worker.run_once(DUE)
    assert broker.sells[0][1] == 6


def test_bracket_fills_entire_position_no_market_exit(setup):
    ledger, broker, worker = setup
    broker.orders['profit'] = replace(broker.orders['profit'], status='FILLED', filled=Decimal(10))
    broker.held = Decimal(0)
    worker.run_once(FILL)
    assert broker.cancels == ['stop']
    assert broker.sells == []
    assert ledger.exit_jobs() == []


def test_partial_entry_remainder_cancelled_first(setup):
    ledger, broker, worker = setup
    broker.orders['entry'] = replace(broker.orders['entry'], status='PARTIAL_FILLED', filled=Decimal(3))
    broker.held = Decimal(3)
    worker.run_once(DUE)
    assert broker.cancels[0] == 'entry'
    assert broker.sells[0][1] == 3


def test_restart_after_ambiguous_submission_does_not_resubmit(setup):
    ledger, broker, worker = setup
    broker.timeout = True
    worker.run_once(DUE)
    assert 'Response lost' in ledger.exit_jobs()[0]['last_error']
    restarted = ExitScheduler(Ledger(str(ledger.path)), broker)
    restarted.run_once(DUE)
    assert len(broker.sells) == 1


def test_unknown_submission_stays_pending(setup):
    ledger, broker, worker = setup
    worker.run_once(DUE)
    broker.orders.pop(broker.sells[0][0])
    worker.run_once(DUE)
    assert len(broker.sells) == 1
    assert 'unknown' in ledger.exit_jobs()[0]['last_error']


def test_partial_market_fill_waits_then_retries_terminal_remainder(setup):
    ledger, broker, worker = setup
    worker.run_once(DUE)
    order_id, _ = broker.sells[0]
    broker.orders[order_id] = replace(broker.orders[order_id], status='PARTIAL_FILLED', filled=Decimal(4))
    broker.held = Decimal(6)
    worker.run_once(DUE)
    assert len(broker.sells) == 1
    broker.orders[order_id] = replace(broker.orders[order_id], status='CANCELLED')
    worker.run_once(DUE)
    assert broker.sells[1][1] == 6
    assert broker.sells[1][0] != order_id


def test_duplicate_exit_job_is_idempotent_for_same_account_symbol(tmp_path):
    ledger = Ledger(str(tmp_path / 'duplicate.sqlite3'))
    job = {
        'id': 'trade-1', 'account_id': 'acct-1', 'symbol': 'AAPL', 'quantity': '10',
        'entry_id': 'entry', 'profit_id': 'profit', 'stop_id': 'stop',
        'status': 'waiting_entry', 'market_orders': [], 'due_at': None,
    }

    ledger.register_exit_job(job)
    ledger.register_exit_job({**job, 'id': 'trade-2', 'status': 'waiting_entry'})

    jobs = ledger.exit_jobs(include_complete=True)
    assert len(jobs) == 1
    assert jobs[0]['id'] == 'trade-1'
    assert jobs[0]['account_id'] == 'acct-1'
    assert jobs[0]['symbol'] == 'AAPL'


def test_other_worker_cannot_submit(setup):
    ledger, broker, worker = setup
    with ledger.exit_worker_lock() as acquired:
        assert acquired
        ExitScheduler(Ledger(str(ledger.path)), broker).run_once(DUE)
    assert broker.sells == broker.cancels == []
    worker.run_once(DUE)
    assert len(broker.sells) == 1


@pytest.mark.parametrize('now', ['2026-09-07T14:00:00Z', '2026-09-08T13:34:59Z', '2026-09-08T20:00:00Z'])
def test_no_exit_on_holiday_before_due_or_after_close(setup, now):
    ledger, broker, worker = setup
    worker.run_once(dt(now))
    assert broker.sells == broker.cancels == []


def test_overdue_job_recovers_next_open_session(setup):
    ledger, broker, worker = setup
    worker.run_once(dt('2026-09-09T13:31:00Z'))
    assert len(broker.sells) == 1


def test_position_mismatch_blocks_sale(setup):
    ledger, broker, worker = setup
    broker.held = Decimal(2)
    worker.run_once(DUE)
    assert broker.sells == []
    assert 'smaller' in ledger.exit_jobs()[0]['last_error']


def test_extra_shares_are_not_sold(setup):
    ledger, broker, worker = setup
    broker.held = Decimal(20)
    worker.run_once(DUE)
    assert broker.sells[0][1] == 10


def response(body, status=200):
    return SimpleNamespace(status_code=status, json=lambda: body)


def test_adapter_uses_documented_nested_order_response():
    raw = {'client_order_id': 'entry', 'symbol': 'AAPL', 'side': 'BUY',
           'instrument_type': 'EQUITY', 'status': 'FILLED', 'filled_quantity': '10',
           'total_quantity': '10', 'filled_time_at': FILL.isoformat()}
    client = SimpleNamespace(order_v3=SimpleNamespace(get_order_detail=lambda *args: response({'orders': [raw]})))
    broker = StockExecution(client)
    order = broker.order('test', 'entry', 'AAPL', 'BUY')
    assert order.filled_at == FILL
    assert order.filled == 10
    raw.pop('filled_quantity')
    with pytest.raises(KeyError):
        broker.order('test', 'entry', 'AAPL', 'BUY')


def test_adapter_rejects_unknown_status_and_http_errors():
    client = SimpleNamespace(order_v3=SimpleNamespace(get_order_detail=lambda *args: response({}, 429)))
    with pytest.raises(RuntimeError, match='429'):
        StockExecution(client).order('test', 'entry', 'AAPL', 'BUY')


def test_market_order_is_normal_sell_without_limit():
    calls = []
    def place(account, orders):
        calls.extend(orders)
        return response({'client_order_id': 'exit'})
    broker = StockExecution(SimpleNamespace(order_v3=SimpleNamespace(place_order=place)))
    broker.market_sell('test', 'AAPL', Decimal(3), 'exit')
    assert calls == [{'client_order_id': 'exit', 'combo_type': 'NORMAL', 'symbol': 'AAPL',
                      'instrument_type': 'EQUITY', 'market': 'US', 'side': 'SELL',
                      'order_type': 'MARKET', 'quantity': '3', 'time_in_force': 'DAY',
                      'support_trading_session': 'CORE', 'entrust_type': 'QTY'}]


def test_tracking_persisted_before_bracket_submission_with_gtc_exits():
    spec = importlib.util.spec_from_file_location('stock_test', Path(__file__).parents[1] / 'app/bullish/stock_bracket.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    tracked = {}
    def place(account, orders, **kwargs):
        assert tracked['entry_id'] == orders[0]['client_order_id']
        assert tracked['profit_id'] == orders[1]['client_order_id']
        assert tracked['stop_id'] == orders[2]['client_order_id']
        assert [o['time_in_force'] for o in orders] == ['DAY', 'GTC', 'GTC']
        return response({})
    result = module.buy_stock('test', 'AAPL', 1, 100, 95, 110,
                              trade_client=SimpleNamespace(order_v3=SimpleNamespace(place_order=place)),
                              exit_time_in_force='GTC', before_submit=tracked.update)
    assert result['entry_id'] == tracked['entry_id']


def test_dry_run_never_loads_broker_or_registers_job(monkeypatch, tmp_path):
    from app.main import Settings
    from app.execution.submitter import submit_paper_order
    monkeypatch.setattr('app.execution.submitter._load_webull_stock_module', lambda: pytest.fail('broker loaded'))
    settings = Settings(_env_file=None, DRY_RUN=True, NEXT_DAY_EXIT_ENABLED=True, DATABASE_PATH=str(tmp_path / 'dry.sqlite3'))
    assert submit_paper_order({'action': 'buy', 'symbol': 'AAPL'}, settings, 'test')['dry_run']
    assert not (tmp_path / 'dry.sqlite3').exists()


def test_stale_fill_count_cannot_increase_remaining_sale(setup):
    ledger, broker, worker = setup
    worker.run_once(DUE)
    order_id, _ = broker.sells[0]
    broker.orders[order_id] = replace(broker.orders[order_id], status='PARTIAL_FILLED', filled=Decimal(4))
    worker.run_once(DUE)
    broker.orders[order_id] = replace(broker.orders[order_id], status='CANCELLED', filled=Decimal(0))
    worker.run_once(DUE)
    assert len(broker.sells) == 1
    assert 'backwards' in ledger.exit_jobs()[0]['last_error']


def test_cancellation_error_does_not_sell(setup):
    ledger, broker, worker = setup
    def fail(*args):
        raise RuntimeError('Webull HTTP 429')
    broker.cancel = fail
    worker.run_once(DUE)
    assert broker.sells == []
    assert '429' in ledger.exit_jobs()[0]['last_error']


def test_only_one_active_job_per_account_symbol(setup):
    ledger, _, _ = setup
    job = ledger.exit_jobs()[0]
    ledger.register_exit_job({**job, 'id': 'another-trade'})
    jobs = ledger.exit_jobs(include_complete=True)
    assert len(jobs) == 1
    assert jobs[0]['id'] == 'trade-1'


def test_scheduled_submission_persists_intent_even_if_broker_times_out(monkeypatch, tmp_path):
    from app.main import Settings
    from app.execution.submitter import submit_paper_order
    spec = importlib.util.spec_from_file_location('scheduled_stock', Path(__file__).parents[1] / 'app/bullish/stock_bracket.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    database = str(tmp_path / 'scheduled.sqlite3')
    calls = []
    def place(account, orders, **kwargs):
        jobs = Ledger(database).exit_jobs()
        assert jobs[0]['entry_id'] == orders[0]['client_order_id']
        calls.append(orders)
        raise TimeoutError('ambiguous bracket response')
    client = SimpleNamespace(
        order_v3=SimpleNamespace(place_order=place),
        account_v2=SimpleNamespace(get_account_position=lambda *args: response([])),
    )
    monkeypatch.setattr(module, 'get_account_id', lambda **kw: 'test')
    monkeypatch.setattr(module, 'get_trade_client', lambda: client)
    monkeypatch.setattr('app.execution.submitter._load_webull_stock_module', lambda: module)
    settings = Settings(_env_file=None, DRY_RUN=False, BULLISH_STOCK_ACCOUNT_NUMBER="test-cash", NEXT_DAY_EXIT_ENABLED=True, DATABASE_PATH=database)
    decision = {'action': 'buy', 'symbol': 'AAPL', 'notional_usd': 250}
    with pytest.raises(TimeoutError):
        submit_paper_order(decision, settings, 'stable-trade-id')
    jobs = Ledger(database).exit_jobs()
    assert len(jobs) == 1
    assert jobs[0]['entry_submission_error'] == 'ambiguous bracket response'
    # A retry should be treated as duplicate intent, not a new bracket submission.
    with pytest.raises(RuntimeError, match='Duplicate active stock exit job already exists'):
        submit_paper_order(decision, settings, 'stable-trade-id')
    assert len(calls) == 1
    jobs = Ledger(database).exit_jobs()
    assert len(jobs) == 1


def test_worker_disabled_in_dry_run(monkeypatch):
    from app import main
    monkeypatch.setattr(main, 'get_settings', lambda: main.Settings(_env_file=None, DRY_RUN=True, NEXT_DAY_EXIT_ENABLED=True))
    monkeypatch.setattr(main.threading, 'Thread', lambda **kwargs: pytest.fail('worker started'))
    main.start_exit_scheduler()


def test_missing_entry_waits_and_backs_off_across_restart(setup):
    from datetime import timedelta
    from app.broker.stocks import OrderNotFound
    ledger, broker, worker = setup
    original_order = broker.order
    calls = []
    def missing(*args):
        calls.append(args)
        raise OrderNotFound('Order not present')
    broker.order = missing
    worker.run_once(DUE)
    job = ledger.exit_jobs()[0]
    assert job['status'] == 'waiting_entry'
    assert job['due_at'] is None
    assert 'master BUY' in job['last_error']
    assert dt(job['next_check_at']) == DUE + timedelta(seconds=60)
    ExitScheduler(Ledger(str(ledger.path)), broker).run_once(DUE + timedelta(seconds=30))
    assert len(calls) == 1
    assert broker.sells == broker.cancels == []
    broker.order = original_order
    worker.run_once(DUE + timedelta(seconds=60))
    assert len(broker.sells) == 1
    assert ledger.exit_jobs()[0]['next_check_at'] is None


def test_generic_reconcile_failure_backs_off_and_resets(setup):
    from datetime import timedelta
    ledger, broker, worker = setup
    original_order = broker.order
    calls = []
    def boom(*args, **kwargs):
        calls.append(args)
        raise RuntimeError('broker exploded')
    broker.order = boom
    worker.run_once(DUE)
    job = ledger.exit_jobs()[0]
    assert 'broker exploded' in job['last_error']
    assert dt(job['next_check_at']) == DUE + timedelta(seconds=60)
    worker.run_once(DUE + timedelta(seconds=30))
    assert len(calls) == 1
    worker.run_once(DUE + timedelta(seconds=61))
    assert len(calls) == 2
    assert dt(ledger.exit_jobs()[0]['next_check_at']) == DUE + timedelta(seconds=181)
    broker.order = original_order
    worker.run_once(DUE + timedelta(seconds=181))
    assert len(broker.sells) == 1
    job = ledger.exit_jobs()[0]
    assert job['next_check_at'] is None
    assert job['reconcile_failures'] == 0


@pytest.mark.parametrize('raised', [True, False])
def test_webull_missing_order_response_is_recognized(raised):
    from app.broker.stocks import OrderNotFound
    from webull.core.exception.exceptions import ServerException
    def query(*args):
        if raised:
            raise ServerException('OPENAPI_PARAM_ERR', 'Parameter error, Order not present.', http_status=417)
        return response({'error_code':'OPENAPI_PARAM_ERR', 'message':'Parameter error, Order not present.'},417)
    broker = StockExecution(SimpleNamespace(order_v3=SimpleNamespace(get_order_detail=query)))
    with pytest.raises(OrderNotFound):
        broker.order('test','entry','AAPL','BUY')


def test_other_parameter_errors_are_not_classified_as_missing_orders():
    from webull.core.exception.exceptions import ServerException
    def query(*args):
        raise ServerException('OPENAPI_PARAM_ERR', 'Invalid account', http_status=417)
    broker = StockExecution(SimpleNamespace(order_v3=SimpleNamespace(get_order_detail=query)))
    with pytest.raises(ServerException):
        broker.order('test','entry','AAPL','BUY')


def test_sdk_error_logs_do_not_emit_signed_request():
    import logging
    from app.broker.client import SafeSdkLogFilter
    record = logging.LogRecord('webull.core.client', logging.ERROR, '', 0,
                               'ServerException Request:%s', ('x-app-key=example x-signature=example',), None)
    assert SafeSdkLogFilter().filter(record)
    assert 'x-app-key' not in record.getMessage()
    assert 'x-signature' not in record.getMessage()
