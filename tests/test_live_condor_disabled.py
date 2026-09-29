from types import SimpleNamespace

import pytest

from app.execution import submitter
from app.ironcondor.executor import IronCondorOptionExecutor, CondorValidationError


@pytest.mark.parametrize('dry_run', [False, True])
@pytest.mark.parametrize('strategy,direction', [('iron_condor', None), ('iron_condor', 'bullish'), ('placeholder', 'neutral')])
def test_live_condor_skips_before_broker_or_ledger(monkeypatch, tmp_path, dry_run, strategy, direction):
    monkeypatch.setattr(submitter, '_load_webull_option_module', lambda: pytest.fail('broker loaded'))
    monkeypatch.setattr(submitter, '_load_webull_stock_module', lambda: pytest.fail('stock broker loaded'))
    config = SimpleNamespace(webull_trading_mode='live', dry_run=dry_run, database_path=str(tmp_path / 'test.db'))
    result = submitter.submit_paper_order({'action': 'buy', 'strategy': strategy}, config, 'test',
                                         SimpleNamespace(direction=direction))
    assert result['skipped'] and 'disabled in live' in result['reason']
    assert not (tmp_path / 'test.db').exists()


def test_paper_condor_remains_enabled():
    result = submitter.submit_paper_order({'action': 'buy', 'strategy': 'iron_condor'},
        SimpleNamespace(webull_trading_mode='paper', dry_run=True), 'test', SimpleNamespace(direction='neutral'))
    assert result['dry_run'] and not result.get('skipped')


def test_direct_executor_blocks_live_before_data_lookup(monkeypatch):
    monkeypatch.setenv('WEBULL_TRADING_MODE', 'live')
    executor = IronCondorOptionExecutor(data_client=object())
    with pytest.raises(CondorValidationError, match='disabled in live'):
        executor.submit(account_id='test', symbol='TEST', expiration='invalid', reference_price=100, max_risk_usd=250)


def test_alternate_runner_also_skips_live_condor():
    from app.options import runner
    result = runner.submit_paper_order(SimpleNamespace(strategy='iron_condor'),
        SimpleNamespace(webull_trading_mode='live'), 'test')
    assert result['skipped']


@pytest.mark.parametrize('value,expected', [('false', False), ('true', True)])
def test_flag_loads_and_controls_live_preview(monkeypatch, value, expected):
    from app.config.settings import Settings
    monkeypatch.setenv('WEBULL_TRADING_MODE', 'live')
    monkeypatch.setenv('WEBULL_ENDPOINT', '')
    monkeypatch.setenv('WEBULL_LIVE_IRON_CONDOR_ENABLED', value)
    settings = Settings(_env_file=None, DRY_RUN=True)
    result = submitter.submit_paper_order({'action': 'buy', 'strategy': 'iron_condor'},
        settings, 'test', SimpleNamespace(direction='neutral'))
    assert bool(result.get('dry_run')) is expected
    assert bool(result.get('skipped')) is not expected


def test_direct_executor_flag_enabled_allows_paper_equivalent_path(monkeypatch):
    monkeypatch.setenv('WEBULL_TRADING_MODE', 'live')
    monkeypatch.setenv('WEBULL_LIVE_IRON_CONDOR_ENABLED', 'true')
    executor = IronCondorOptionExecutor(data_client=object())
    # With mode gating lifted, normal validation runs before broker access.
    with pytest.raises(CondorValidationError, match='Invalid reference'):
        executor.submit(account_id='test', symbol='TEST', expiration='2026-12-18', reference_price=0, max_risk_usd=250)


def test_paper_ignores_live_flag(monkeypatch):
    from app.config.strategy import iron_condor_enabled
    monkeypatch.setenv('WEBULL_TRADING_MODE', 'paper')
    monkeypatch.setenv('WEBULL_LIVE_IRON_CONDOR_ENABLED', 'false')
    assert iron_condor_enabled()
