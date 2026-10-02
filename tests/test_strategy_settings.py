import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.main import Settings
from app.config.strategy import StrategyExitSettings, exit_percentages
from app.execution.submitter import submit_paper_order


def test_percentages_load_from_dotenv(tmp_path, monkeypatch):
    for field in StrategyExitSettings.model_fields.values():
        monkeypatch.delenv(field.alias, raising=False)
    env = tmp_path / '.env'
    env.write_text('BULLISH_PROFIT_PERCENT=12\nBULLISH_STOP_LOSS_PERCENT=6\n'
                   'BEARISH_PROFIT_PERCENT=25\nBEARISH_STOP_LOSS_PERCENT=12\n'
                   'IRON_CONDOR_PROFIT_PERCENT=30\nIRON_CONDOR_STOP_LOSS_PERCENT=15\n')
    settings = Settings(_env_file=env)
    assert exit_percentages(settings, 'bullish') == (12, 6)
    assert exit_percentages(settings, 'bearish') == (25, 12)
    assert exit_percentages(settings, 'iron_condor') == (30, 15)
    monkeypatch.setenv('BEARISH_PROFIT_PERCENT', '40')
    assert Settings(_env_file=env).bearish_profit_percent == 40


@pytest.mark.parametrize('name,value', [
    ('BULLISH_PROFIT_PERCENT', 0), ('BEARISH_PROFIT_PERCENT', 'NaN'),
    ('BULLISH_STOP_LOSS_PERCENT', 100), ('BEARISH_STOP_LOSS_PERCENT', -1),
    ('IRON_CONDOR_PROFIT_PERCENT', 100), ('IRON_CONDOR_STOP_LOSS_PERCENT', 'inf'),
])
def test_invalid_percentages_rejected(name, value):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{name: value})


def test_bullish_order_uses_configured_percentages(monkeypatch):
    captured = {}
    broker = SimpleNamespace(get_account_id=lambda **kw: 'test', buy_stock=lambda **kw: captured.update(kw) or {})
    monkeypatch.setattr('app.execution.submitter._load_webull_stock_module', lambda: broker)
    settings = Settings(_env_file=None, DRY_RUN=False, BULLISH_STOCK_ACCOUNT_NUMBER="test-cash", NEXT_DAY_EXIT_ENABLED=False,
                        BULLISH_PROFIT_PERCENT=12, BULLISH_STOP_LOSS_PERCENT=6)
    payload = SimpleNamespace(direction='bullish', entry_price=100, target_price=120, stop_price=90)
    submit_paper_order(dict(action='buy', symbol='AAPL', notional_usd=250), settings, 'test', payload)
    assert captured['target_price'] == 112
    assert captured['stop_price'] == 94


def test_bearish_lifecycle_receives_configured_percentages(monkeypatch, tmp_path):
    captured = {}
    broker = SimpleNamespace(get_account_id=lambda **kw: 'test')
    monkeypatch.setattr('app.execution.submitter._load_webull_option_module', lambda: broker)
    class FakeLifecycle:
        def __init__(self, ledger, *, option_module):
            pass

        def submit_entry(self, **kwargs):
            captured.update(kwargs)
            return {
                "status": "entry_pending", "entry_client_order_id": "entry",
                "entry_order_id": None, "contract_symbol": "AAPL-PUT",
                "expiration": "2026-10-16", "strike": 100.0, "quantity": 1,
            }

    monkeypatch.setattr('app.bearish.lifecycle.BearishPutLifecycle', FakeLifecycle)
    monkeypatch.setattr('app.bearish.lifecycle.start_bearish_put_reconciler', lambda path: None)
    settings = SimpleNamespace(
        dry_run=False, webull_trading_mode='paper',
        bearish_profit_percent=25, bearish_stop_loss_percent=12,
        options_margin_account_number='test-margin', live_options_daily_limit_usd=0,
        live_bearish_stop_loss_enabled=True, database_path=str(tmp_path / 'test.sqlite3'),
        max_notional_usd=250,
    )
    payload = SimpleNamespace(direction='bearish', entry_price=100, target_price=90, stop_price=110)
    submit_paper_order(dict(action='sell_short', symbol='AAPL', notional_usd=250), settings, 'test', payload)
    assert captured['desired_strike'] == 100
    assert captured['profit_percent'] == 25
    assert captured['stop_loss_percent'] == 12


def test_bullish_runner_uses_configured_percentages(tmp_path):
    path = Path(__file__).resolve().parents[1] / 'app/bullish/runner.py'
    spec = importlib.util.spec_from_file_location('bullish_percentage_test', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    settings = module.BullishSettings(_env_file=None, DRY_RUN=True, DATABASE_PATH=str(tmp_path / 'test.db'),
                                     BULLISH_PROFIT_PERCENT=12, BULLISH_STOP_LOSS_PERCENT=6)
    runner = module.MainTopBullish(settings=settings, quote_provider=lambda s: {'price': 100},
                                   market_open=lambda: True)
    result = runner.process(dict(symbol='AAPL', total_premium=1000, trade_count=3))
    assert result['order']['target_price'] == 112
    assert result['order']['stop_price'] == 94


def test_bearish_quote_age_from_environment_file(tmp_path, monkeypatch):
    monkeypatch.delenv('BEARISH_QUOTE_MAX_AGE_SECONDS', raising=False)
    env = tmp_path / '.env'
    env.write_text('BEARISH_QUOTE_MAX_AGE_SECONDS=1200\n')
    assert Settings(_env_file=env).bearish_quote_max_age_seconds == 1200
    assert Settings(_env_file=None).bearish_quote_max_age_seconds == 60
    with pytest.raises(ValidationError):
        Settings(_env_file=None, BEARISH_QUOTE_MAX_AGE_SECONDS=0)


def test_condor_quote_age_setting(tmp_path, monkeypatch):
    monkeypatch.delenv('IRON_CONDOR_QUOTE_MAX_AGE_SECONDS', raising=False)
    env = tmp_path / '.env'
    env.write_text('IRON_CONDOR_QUOTE_MAX_AGE_SECONDS=1200\n')
    assert Settings(_env_file=env).iron_condor_quote_max_age_seconds == 1200
    assert Settings(_env_file=None).iron_condor_quote_max_age_seconds == 60
    with pytest.raises(ValidationError):
        Settings(_env_file=None, IRON_CONDOR_QUOTE_MAX_AGE_SECONDS=0)
