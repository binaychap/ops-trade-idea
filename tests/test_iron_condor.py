from datetime import UTC, datetime, timedelta
import json
import sqlite3
from types import SimpleNamespace

import pytest

from app.ironcondor.executor import CondorValidationError, IronCondorOptionExecutor
from app.broker.quotes import QuoteError

NOW = datetime(2026, 9, 16, 15, tzinfo=UTC)
EXPIRY = '2026-10-16'


def market(expiry=EXPIRY):
    rows, quotes = [], []
    # 5-wide wings, 3.00 credit, 200 dollars max spread loss.
    for kind, strike, bid, ask in [('PUT', 90, 0.9, 1), ('PUT', 95, 2.5, 2.6),
                                  ('CALL', 105, 2.5, 2.6), ('CALL', 110, 0.9, 1)]:
        symbol = f"AAPL{datetime.fromisoformat(expiry):%y%m%d}{kind[0]}{strike * 1000:08d}"
        rows.append({'option_type': kind, 'strike_price': str(strike), 'expiration_date': expiry,
                     'symbol': symbol, 'multiplier': '100'})
        quotes.append({'symbol': symbol, 'bid': str(bid), 'ask': str(ask),
                       'quote_time': NOW.timestamp() * 1000})
    return rows, quotes


def fake_data(rows, quotes):
    def snapshots(symbols, category):
        assert category == 'US_OPTION'
        assert set(symbols) == {r['symbol'] for r in quotes}
        return SimpleNamespace(status_code=200, json=lambda: quotes)
    return SimpleNamespace(
        instrument=SimpleNamespace(get_option_contracts=lambda **kw: SimpleNamespace(status_code=200, json=lambda: {'data': rows})),
        option_market_data=SimpleNamespace(get_option_snapshot=snapshots),
    )


def submit(rows=None, quotes=None, **kwargs):
    defaults = market()
    data = fake_data(rows if rows is not None else defaults[0], quotes if quotes is not None else defaults[1])
    calls = []

    def place_order(account, orders, **kw):
        calls.append((account, orders, kw))
        return SimpleNamespace(status_code=200, json=lambda: {'order_id': 'test'})

    executor = IronCondorOptionExecutor(data_client=data)
    params = dict(account_id='test', symbol='AAPL', expiration=EXPIRY, reference_price=100,
                  max_risk_usd=250, now=NOW, trade_client=SimpleNamespace(order_v3=SimpleNamespace(place_order=place_order)))
    params.update(kwargs)
    result = executor.submit(**params)
    return result, calls


def test_credit_bracket_reverses_all_four_legs_and_prices_from_premium():
    saved = []
    result, calls = submit(before_submit=lambda plan: saved.append(plan))
    assert len(calls) == len(saved) == 1
    entry, profit, stop = calls[0][1]
    assert entry['side'] == 'SELL'
    assert entry['order_type'] == 'LIMIT'
    assert entry['position_intent'] == 'SELL_TO_OPEN'
    assert entry['limit_price'] == '3.00'
    assert profit['limit_price'] == '2.70'
    assert stop['stop_price'] == '3.15'
    assert profit['side'] == stop['side'] == 'BUY'
    assert profit['time_in_force'] == stop['time_in_force'] == 'GTC'
    assert entry['time_in_force'] == 'GTC'
    for closing in (profit, stop):
        assert 'position_intent' not in closing
        assert len(closing['legs']) == 4
        for a, b in zip(entry['legs'], closing['legs']):
            assert a['side'] != b['side']
            assert {k: v for k, v in a.items() if k != 'side'} == {k: v for k, v in b.items() if k != 'side'}
    assert result['max_loss_usd'] == 200
    assert saved[0]['entry_id'] == entry['client_order_id']
    assert saved[0]['combo_id'] == calls[0][2]['client_combo_order_id']


@pytest.mark.parametrize('kwargs', [
    {'max_risk_usd': 199}, {'quantity': 2}, {'quantity': True}, {'reference_price': float('nan')},
    {'profit_percent': 100}, {'stop_loss_percent': 0}, {'wing_width': -1}, {'expiration': '2026-09-16'},
])
def test_invalid_setup_never_reaches_submission_callback(kwargs):
    def unexpected(plan):
        pytest.fail('invalid setup reached submission')
    with pytest.raises(CondorValidationError):
        submit(before_submit=unexpected, **kwargs)


@pytest.mark.parametrize('update', [
    {'bid': 'NaN'}, {'bid': 0}, {'bid': 50}, {'ask': True},
    {'quote_time': (NOW.timestamp() - 61) * 1000},
    {'quote_time': (NOW.timestamp() + 6) * 1000},
])
def test_bad_quotes_block_submission(update):
    rows, quotes = market()
    quotes[0].update(update)
    with pytest.raises((CondorValidationError, QuoteError)):
        submit(rows, quotes, before_submit=lambda p: pytest.fail('unexpected submission'))


def test_debit_setup_is_rejected():
    rows, quotes = market()
    quotes[0]['ask'] = '9'
    with pytest.raises(CondorValidationError, match='credit'):
        submit(rows, quotes)


@pytest.mark.parametrize('change', ['missing_leg', 'adjusted', 'wrong_type', 'mismatched_expiry', 'missing_multiplier'])
def test_unusable_contracts_block_submission(change):
    rows, quotes = market()
    if change == 'missing_leg':
        rows.pop()
    elif change == 'adjusted':
        rows[0]['symbol'] = rows[0]['symbol'].replace('AAPL', 'AAPL1')
    elif change == 'wrong_type':
        rows[0]['option_type'] = 'CALL'
    elif change == 'mismatched_expiry':
        rows[0]['expiration_date'] = '2026-10-17'
    else:
        del rows[0]['multiplier']
    with pytest.raises(CondorValidationError, match='four-leg'):
        submit(rows, quotes)


def test_listed_expiry_after_requested_date_is_used():
    rows, quotes = market('2026-10-19')
    result, _ = submit(rows, quotes)
    assert all(leg['option_expire_date'] == '2026-10-19' for leg in result['orders'][0]['legs'])


def setup_submission(monkeypatch, tmp_path, *, timeout=False):
    from app.execution import submitter as webull_submitter
    import app.ironcondor.executor as mod
    calls = []
    expiry = (datetime.now(UTC) + timedelta(days=30)).date().isoformat()
    rows, quotes = market(expiry)
    for q in quotes:
        q['quote_time'] = datetime.now(UTC).timestamp() * 1000
    data = fake_data(rows, quotes)
    database = tmp_path / 'ledger.sqlite3'

    def place(account, orders, **kwargs):
        # Reservation including actual request IDs must precede broker invocation.
        with sqlite3.connect(database) as conn:
            event = conn.execute('SELECT status, order_json FROM events').fetchone()
        assert event[0] == 'submitting'
        assert json.loads(event[1])['orders'] == orders
        calls.append(orders)
        if timeout:
            raise TimeoutError('mock lost response')
        return SimpleNamespace(status_code=200, json=lambda: {'order_id': 'test'})

    module = SimpleNamespace(get_account_id=lambda **kwargs: 'test', get_trade_client=lambda: SimpleNamespace(order_v3=SimpleNamespace(place_order=place)))
    monkeypatch.setattr(webull_submitter, '_load_webull_option_module', lambda: module)
    monkeypatch.setattr(mod, 'get_data_client', lambda: data)
    settings = SimpleNamespace(dry_run=False, max_notional_usd=250, database_path=str(database), force_reprocess=False, options_margin_account_number="test-margin")
    decision = dict(action='buy', symbol='AAPL', strategy='iron_condor', notional_usd=250)
    payload = SimpleNamespace(direction='neutral', entry_price=100, target_price=90, stop_price=110)
    return settings, decision, payload, calls


def test_submitter_persists_orders_and_suppresses_repeat(monkeypatch, tmp_path):
    from app.execution.submitter import submit_paper_order
    settings, decision, payload, calls = setup_submission(monkeypatch, tmp_path)
    first = submit_paper_order(decision, settings, 'fingerprint', payload)
    assert first['option']['type'] == 'IRON_CONDOR'
    assert first['side'] == 'SELL'
    assert first['entry_credit'] == 3
    second = submit_paper_order(decision, settings, 'fingerprint', payload)
    assert second['skipped']
    assert len(calls) == 1


def test_timeout_does_not_replay_order(monkeypatch, tmp_path):
    from app.execution.submitter import submit_paper_order
    settings, decision, payload, calls = setup_submission(monkeypatch, tmp_path, timeout=True)
    with pytest.raises(TimeoutError):
        submit_paper_order(decision, settings, 'fingerprint', payload)
    assert submit_paper_order(decision, settings, 'fingerprint', payload)['skipped']
    assert len(calls) == 1
    with sqlite3.connect(settings.database_path) as conn:
        status, payload_json = conn.execute('SELECT status, payload_json FROM events').fetchone()
    assert status == 'failed'
    assert json.loads(payload_json)['entry_id']


def test_dry_run_never_loads_broker_or_reserves(monkeypatch, tmp_path):
    from app.execution import submitter as webull_submitter
    monkeypatch.setattr(webull_submitter, '_load_webull_option_module', lambda: pytest.fail('broker loaded'))
    settings = SimpleNamespace(dry_run=True, database_path=str(tmp_path / 'unused.sqlite3'))
    result = webull_submitter.submit_paper_order(dict(action='buy', strategy='iron_condor'), settings, 'fp', SimpleNamespace(direction='neutral'))
    assert result['dry_run']
    assert not (tmp_path / 'unused.sqlite3').exists()


def test_polling_neutral_reaches_real_executor_and_records_order(monkeypatch, tmp_path):
    from app import main
    settings, _, _, calls = setup_submission(monkeypatch, tmp_path)
    settings.optionomics_api_url = 'unused'
    settings.allow_short_selling = False
    monkeypatch.setattr(main, 'get_settings', lambda: settings)
    monkeypatch.setattr(main, 'is_market_open_et', lambda: True)
    idea = dict(id='neutral-test', symbol='AAPL', direction='neutral', levels=dict(entry=100, target=90, stop=110))
    monkeypatch.setattr(main, 'fetch_trade_ideas', lambda *a, **kw: [idea])
    main.poll_optionomics_trade_ideas()
    main.poll_optionomics_trade_ideas()
    assert len(calls) == 1
    with sqlite3.connect(settings.database_path) as conn:
        status, order = conn.execute('SELECT status, order_json FROM optionomics_trade_ideas').fetchone()
    assert status == 'ordered'
    assert json.loads(order)['entry_credit'] == 3


def test_neutral_decision_and_risk_gates():
    from app.main import TradeIdea, build_trade_decision, apply_risk_gates
    payload = TradeIdea(alert_name='neutral', source='trade_idea', symbol='AAPL', direction='neutral',
                        strategy='iron_condor', entry_price=100, target_price=90, stop_price=110,
                        triggered_at=NOW)
    settings = SimpleNamespace(max_notional_usd=250, allow_short_selling=False)
    decision = build_trade_decision(payload, settings)
    assert decision.action == 'buy'
    assert decision.strategy == 'iron_condor'
    assert apply_risk_gates(payload, decision, settings).action == 'buy'


@pytest.mark.parametrize('levels', [{}, {'entry': 100, 'target': 110, 'stop': 90},
                                    {'entry': 100, 'target': 90, 'stop': float('inf')}])
def test_invalid_neutral_feed_levels_skip(levels):
    from app.feeds.decisions import build_trade_decision_from_optionomics_payload
    decision = build_trade_decision_from_optionomics_payload(
        dict(symbol='AAPL', direction='neutral', levels=levels),
        SimpleNamespace(max_notional_usd=250, allow_short_selling=False))
    assert decision['action'] == 'skip'


def test_legacy_neutral_path_uses_shared_executor(monkeypatch, tmp_path):
    import importlib.util
    from pathlib import Path
    from app import main
    settings, decision, payload, calls = setup_submission(monkeypatch, tmp_path)
    path = Path(__file__).resolve().parents[1] / 'app/options/runner.py'
    spec = importlib.util.spec_from_file_location('legacy_condor_test', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(main, 'is_market_open_et', lambda: True)
    decision = main.TradingDecision(**decision, confidence=1, rationale='test')
    ledger = module.Ledger(settings.database_path)
    ledger.save_trade_idea('test', dict(symbol='AAPL', direction='neutral'))
    module.maybe_submit_order(ledger, decision, settings, payload, trade_id='test')
    assert len(calls) == 1
    assert calls[0][0]['option_strategy'] == 'IRON_CONDOR'


def test_contract_pagination(monkeypatch):
    from app.ironcondor.executor import select_contracts
    from decimal import Decimal
    rows, _ = market()
    pages = [([{'instrument_id': str(i)} for i in range(500)]), rows]
    requests = []

    def contracts(**kwargs):
        requests.append(kwargs)
        page = pages.pop(0)
        return SimpleNamespace(status_code=200, json=lambda: {'data': page})

    data = SimpleNamespace(instrument=SimpleNamespace(get_option_contracts=contracts))
    selected, wing = select_contracts(data, 'AAPL', EXPIRY, Decimal(100), Decimal(5))
    assert requests[1]['last_instrument_id'] == '499'
    assert len(selected) == 4
    assert wing == 5


def test_collapsed_exit_prices_block_submission():
    rows, quotes = market()
    quotes[1].update(bid='1.1', ask='1.2')
    quotes[2].update(bid='1.1', ask='1.2')
    with pytest.raises(CondorValidationError, match='collapse'):
        submit(rows, quotes, max_risk_usd=1000)


def test_configured_condor_exit_percentages(monkeypatch, tmp_path):
    from app.execution.submitter import submit_paper_order
    settings, decision, payload, calls = setup_submission(monkeypatch, tmp_path)
    settings.iron_condor_profit_percent = 20
    settings.iron_condor_stop_loss_percent = 10
    result = submit_paper_order(decision, settings, 'custom-percentages', payload)
    assert result['profit_debit'] == 2.4
    assert result['stop_debit'] == 3.3


@pytest.mark.parametrize('age,limit,accepted', [(901.5, 60, False), (901.5, 1200, True), (1200, 1200, True), (1201, 1200, False), (-6, 1200, False)])
def test_condor_configurable_quote_age(age, limit, accepted):
    rows, quotes = market()
    # One stale leg must block the entire combo.
    quotes[2]['quote_time'] = (NOW.timestamp() - age) * 1000
    if accepted:
        result, calls = submit(rows, quotes, quote_max_age_seconds=limit)
        assert len(calls) == 1
        assert result['entry_credit'] == 3
    else:
        with pytest.raises(QuoteError, match='maximum'):
            submit(rows, quotes, quote_max_age_seconds=limit,
                   before_submit=lambda p: pytest.fail('Rejected quote reached submission'))


def test_submitter_passes_condor_quote_limit(monkeypatch, tmp_path):
    from app.execution.submitter import submit_paper_order
    settings, decision, payload, calls = setup_submission(monkeypatch, tmp_path)
    settings.iron_condor_quote_max_age_seconds = 1200
    original = IronCondorOptionExecutor.submit
    def checked(self, **kwargs):
        assert kwargs['quote_max_age_seconds'] == 1200
        return original(self, **kwargs)
    monkeypatch.setattr(IronCondorOptionExecutor, 'submit', checked)
    submit_paper_order(decision, settings, 'quote-limit', payload)
    assert len(calls) == 1
