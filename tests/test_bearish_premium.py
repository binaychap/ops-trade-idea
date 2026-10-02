import importlib.util
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.broker.quotes import QuoteError, current_option_ask


NOW = datetime(2026, 9, 16, 15, tzinfo=UTC)
SYMBOL = 'AAPL260918P00100000'


def quote_client(rows, status=200):
    def snapshot(symbol, category):
        assert (symbol, category) == (SYMBOL, 'US_OPTION')
        return SimpleNamespace(status_code=status, json=lambda: rows)
    return SimpleNamespace(option_market_data=SimpleNamespace(get_option_snapshot=snapshot))


def test_current_option_ask_uses_ask_not_last_trade():
    row = {'symbol': SYMBOL, 'ask': '2.00', 'price': '1.75', 'quote_time': NOW.timestamp() * 1000}
    result = current_option_ask(SYMBOL, data_client=quote_client([row]), now=NOW)
    assert result['price'] == 2.0


@pytest.mark.parametrize('change', [
    {'ask': 0}, {'ask': -1}, {'ask': 'NaN'}, {'ask': 'inf'}, {'ask': True},
    {'quote_time': (NOW.timestamp() - 61) * 1000},
    {'quote_time': (NOW.timestamp() + 6) * 1000},
    {'quote_time': None}, {'symbol': 'WRONG'},
])
def test_invalid_option_quotes_are_rejected(change):
    row = {'symbol': SYMBOL, 'ask': '2.00', 'quote_time': NOW.timestamp() * 1000}
    row.update(change)
    with pytest.raises(QuoteError):
        current_option_ask(SYMBOL, data_client=quote_client([row]), now=NOW)


@pytest.mark.parametrize('rows,status', [([], 200), ({}, 200), ([], 403)])
def test_missing_or_unavailable_option_quote(rows, status):
    with pytest.raises(QuoteError):
        current_option_ask(SYMBOL, data_client=quote_client(rows, status), now=NOW)


def load_builder():
    path = Path(__file__).resolve().parents[1] / 'app/options/brackets.py'
    spec = importlib.util.spec_from_file_location('bearish_premium_builder_test', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_put_bracket_quotes_selected_contract_and_submits_20_10_exits(monkeypatch):
    module = load_builder()
    calls = []

    def contract(symbol, expiration, strike, **kwargs):
        assert kwargs == {'option_type': 'PUT'}
        return '2026-09-18', 100.0, SYMBOL

    def ask(symbol, **kwargs):
        assert kwargs['max_age_seconds'] == 1200
        assert symbol == SYMBOL
        return {'price': 2.0}

    def place_order(account, orders, **kwargs):
        calls.extend(orders)
        return SimpleNamespace(status_code=200, json=lambda: {'order_id': 'test'})

    monkeypatch.setattr(module, '_find_valid_contract', contract)
    monkeypatch.setattr('app.broker.quotes.current_option_ask', ask)
    client = SimpleNamespace(order_v3=SimpleNamespace(place_order=place_order))
    module.buy_put_with_bracket('test', 'AAPL', 103, '2026-09-21', 1, trade_client=client, quote_max_age_seconds=1200)
    entry, profit, stop = calls
    assert (entry['side'], entry['position_intent'], entry['order_type']) == ('BUY', 'BUY_TO_OPEN', 'LIMIT')
    assert entry['limit_price'] == '2.00'
    assert profit['limit_price'] == '2.40'
    assert stop['stop_price'] == '1.80'
    assert profit['side'] == stop['side'] == 'SELL'
    assert all(order['legs'][0]['option_type'] == 'PUT' for order in calls)
    assert all(order['legs'][0]['strike_price'] == '100.00' for order in calls)


def test_quote_failure_prevents_order_submission(monkeypatch):
    module = load_builder()
    monkeypatch.setattr(module, '_find_valid_contract', lambda *a, **k: ('2026-09-18', 100, SYMBOL))

    def unavailable(symbol, **kwargs):
        raise QuoteError('stale')

    monkeypatch.setattr('app.broker.quotes.current_option_ask', unavailable)
    with pytest.raises(QuoteError, match='stale'):
        module.buy_put_with_bracket('test', 'AAPL', 100, '2026-09-18', 1, trade_client=object())


def test_contract_lookup_excludes_calls(monkeypatch):
    module = load_builder()
    class FixedClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW
    monkeypatch.setattr(module, 'datetime', FixedClock)
    rows = [
        {'option_type': 'PUT', 'expiration': '2026-09-18', 'strike': 100, 'symbol': SYMBOL},
        {'option_type': 'CALL', 'expiration': '2026-09-18', 'strike': 100, 'symbol': 'CALL'},
    ]
    response = SimpleNamespace(status_code=200, json=lambda: rows)
    api = SimpleNamespace(add_endpoint=lambda *a: None, set_stream_logger=lambda **k: None)
    data = SimpleNamespace(instrument=SimpleNamespace(get_option_contracts=lambda **k: response))
    monkeypatch.setenv('WEBULL_APP_KEY', 'test')
    monkeypatch.setenv('WEBULL_APP_SECRET', 'test')
    monkeypatch.setattr(module, 'ApiClient', lambda *a: api)
    monkeypatch.setattr(module, 'DataClient', lambda *a: data)
    assert module._find_valid_contract('AAPL', '2026-09-18', 100, option_type='PUT') == (
        '2026-09-18', 100.0, SYMBOL,
    )


@pytest.mark.parametrize('offset,fragment', [(-61, 'age 61.0 seconds'), (6, 'future-dated by 6.0 seconds'), (float('nan'), 'nonfinite quote_time')])
def test_option_quote_timestamp_diagnostics(offset, fragment):
    row = {'symbol': SYMBOL, 'ask': '2.00', 'quote_time': (NOW.timestamp() + offset) * 1000}
    with pytest.raises(QuoteError, match=fragment) as error:
        current_option_ask(SYMBOL, data_client=quote_client([row]), now=NOW)
    assert SYMBOL in str(error.value)


def test_bearish_market_entry_fails_closed_with_live_budget_cap(monkeypatch):
    from app.execution import submitter as webull_submitter
    called = []
    module = SimpleNamespace(get_account_id=lambda **kw: called.append(kw))
    monkeypatch.setattr(webull_submitter, '_load_webull_option_module', lambda: module)
    settings = SimpleNamespace(
        dry_run=False, webull_trading_mode='live', live_options_daily_limit_usd=100,
        options_margin_account_number='test-margin',
    )
    result = webull_submitter.submit_paper_order(
        {'action': 'sell_short', 'symbol': 'AAPL', 'notional_usd': 250}, settings, 'test',
        SimpleNamespace(entry_price=100, target_price=90, stop_price=110, direction='bearish'))
    assert result['skipped'] is True
    assert 'daily options budget' in result['reason']
    assert called == []


def test_chain_expiry_uses_listed_put_dates_without_five_day_filter(monkeypatch):
    from datetime import timedelta
    module = load_builder()
    tomorrow = (datetime.now(UTC).date() + timedelta(days=1)).isoformat()
    later = (datetime.now(UTC).date() + timedelta(days=8)).isoformat()
    yesterday = (datetime.now(UTC).date() - timedelta(days=1)).isoformat()
    rows = [
        {'option_type': 'PUT', 'expiration': later, 'strike': 100, 'symbol': 'later'},
        {'option_type': 'PUT', 'expiration': yesterday, 'strike': 100, 'symbol': 'expired'},
        {'option_type': 'PUT', 'expiration': tomorrow, 'strike': 100, 'symbol': 'nearest'},
    ]
    requests = []
    def contracts(**kwargs):
        requests.append(kwargs)
        return SimpleNamespace(status_code=200, json=lambda: rows)
    api = SimpleNamespace(add_endpoint=lambda *a: None, set_stream_logger=lambda **k: None)
    monkeypatch.setenv('WEBULL_APP_KEY', 'test')
    monkeypatch.setenv('WEBULL_APP_SECRET', 'test')
    monkeypatch.setattr(module, 'ApiClient', lambda *a: api)
    monkeypatch.setattr(module, 'DataClient', lambda *a: SimpleNamespace(instrument=SimpleNamespace(get_option_contracts=contracts)))
    assert module._find_valid_contract('AAPL', None, 100, option_type='PUT') == (tomorrow, 100, 'nearest')
    assert requests == [dict(category='US_OPTION', underlying_symbols='AAPL', page_size=500, option_type='PUT')]
    rows.clear()
    with pytest.raises(RuntimeError, match='No option contracts'):
        module._find_valid_contract('AAPL', None, 100, option_type='PUT')


def test_expiry_resolver_uses_next_listed_expiration_for_weekend():
    from app.options.expiration import resolve_option_expiry
    assert resolve_option_expiry('2026-09-19', ['2026-09-18', '2026-09-21', '2026-09-25']) == '2026-09-21'
    assert resolve_option_expiry('2026-09-21', ['2026-09-21']) == '2026-09-21'
    with pytest.raises(ValueError, match='No listed'):
        resolve_option_expiry('2026-09-19', ['2026-09-18'])


@pytest.mark.parametrize('age,limit,accepted', [(901.5, 60, False), (901.5, 1200, True), (1200, 1200, True), (1201, 1200, False), (-6, 1200, False)])
def test_configurable_delayed_quote_limit(age, limit, accepted):
    row = {'symbol': SYMBOL, 'ask': '1.13', 'quote_time': (NOW.timestamp() - age) * 1000}
    kwargs = dict(data_client=quote_client([row]), now=NOW, max_age_seconds=limit)
    if accepted:
        assert current_option_ask(SYMBOL, **kwargs)['price'] == 1.13
    else:
        with pytest.raises(QuoteError):
            current_option_ask(SYMBOL, **kwargs)


def test_executor_passes_configured_quote_limit():
    from app.bearish.executor import BearishPutOptionExecutor
    captured = {}
    executor = BearishPutOptionExecutor(module=SimpleNamespace(buy_put_with_bracket=lambda **kw: captured.update(kw) or {}))
    executor.submit(account_id='test', symbol='AAPL', strike=100, expiration=None,
                    quantity=1, quote_max_age_seconds=1200)
    assert captured['quote_max_age_seconds'] == 1200
