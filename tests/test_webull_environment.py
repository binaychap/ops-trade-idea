"""Environment routing checks use fake SDK clients; no broker requests."""
from types import SimpleNamespace

import pytest

from app.config.webull import ENDPOINTS, resolve_webull_endpoint
from app.config.settings import Settings
from app.bullish.runner import BullishSettings
from app.broker import client


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    monkeypatch.delenv('WEBULL_TRADING_MODE', raising=False)
    monkeypatch.delenv('WEBULL_ENDPOINT', raising=False)


def test_paper_default():
    assert resolve_webull_endpoint() == ENDPOINTS['paper']


@pytest.mark.parametrize('mode', ['paper', 'live'])
@pytest.mark.parametrize('settings_class', [Settings, BullishSettings])
def test_settings_select_environment(mode, settings_class, monkeypatch):
    monkeypatch.setenv('WEBULL_TRADING_MODE', mode)
    settings = settings_class(_env_file=None)
    assert settings.webull_endpoint == ENDPOINTS[mode]
    assert settings.dry_run is True


@pytest.mark.parametrize('mode,endpoint', [
    ('paper', ENDPOINTS['live']), ('live', ENDPOINTS['paper']),
    ('live', 'untrusted.example'), ('invalid', ''),
])
def test_invalid_or_mismatched_environment_blocks_clients(mode, endpoint, monkeypatch):
    monkeypatch.setenv('WEBULL_TRADING_MODE', mode)
    monkeypatch.setenv('WEBULL_ENDPOINT', endpoint)
    with pytest.raises(ValueError):
        resolve_webull_endpoint()
    with pytest.raises(ValueError):
        Settings(_env_file=None)


@pytest.mark.parametrize('mode', ['paper', 'live'])
def test_trade_and_snapshot_clients_share_selected_host(mode, monkeypatch):
    import webull.data.data_client as data_module
    hosts = []
    api = SimpleNamespace(
        add_endpoint=lambda region, host: hosts.append(host),
        set_stream_logger=lambda **kwargs: None,
    )
    monkeypatch.setenv('WEBULL_TRADING_MODE', mode)
    monkeypatch.setenv('WEBULL_APP_KEY', 'test')
    monkeypatch.setenv('WEBULL_APP_SECRET', 'test')
    for name in ('_api_client', '_trade_client', '_data_client'):
        monkeypatch.setattr(client, name, None)
    monkeypatch.setattr(client, 'ApiClient', lambda *args: api)
    monkeypatch.setattr(client, 'TradeClient', lambda api: SimpleNamespace(api=api))
    monkeypatch.setattr(data_module, 'DataClient', lambda api: SimpleNamespace(api=api))
    assert client.get_trade_client().api is client.get_data_client().api
    assert hosts == [ENDPOINTS[mode]]


def test_live_dry_run_does_not_load_broker(monkeypatch):
    from app.execution import submitter
    monkeypatch.setenv('WEBULL_TRADING_MODE', 'live')
    settings = Settings(_env_file=None, DRY_RUN=True)
    monkeypatch.setattr(submitter, '_load_webull_stock_module', lambda: pytest.fail('broker loaded'))
    monkeypatch.setattr(submitter, '_load_webull_option_module', lambda: pytest.fail('broker loaded'))
    for action in ('buy', 'sell_short'):
        result = submitter.submit_paper_order({'action': action, 'symbol': 'TEST'}, settings, 'test')
        assert result['dry_run'] is True


@pytest.mark.parametrize('mode', ['paper', 'live'])
def test_option_contract_lookup_uses_selected_host(mode, monkeypatch):
    from app.options import brackets
    hosts = []
    api = SimpleNamespace(
        add_endpoint=lambda region, host: hosts.append(host),
        set_stream_logger=lambda **kwargs: None,
    )
    monkeypatch.setenv('WEBULL_TRADING_MODE', mode)
    monkeypatch.setenv('WEBULL_APP_KEY', 'test')
    monkeypatch.setenv('WEBULL_APP_SECRET', 'test')
    monkeypatch.setattr(brackets, 'ApiClient', lambda *args: api)
    monkeypatch.setattr(brackets, 'DataClient', lambda api: SimpleNamespace(
        instrument=SimpleNamespace(get_option_contracts=lambda **kwargs: [])))
    with pytest.raises(RuntimeError, match='No option contracts found'):
        brackets._find_valid_contract('TEST', None, 100, option_type='PUT')
    assert hosts == [ENDPOINTS[mode]]
