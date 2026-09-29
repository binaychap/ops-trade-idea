import pytest
from app.config.settings import Settings
from app.bullish.runner import BullishSettings
from app.config.strategy import bullish_stock_account_id, options_margin_account_id


@pytest.mark.parametrize('cls', [Settings, BullishSettings])
@pytest.mark.parametrize('mode', ['paper', 'live'])
def test_accounts_follow_mode(cls, mode, monkeypatch):
    monkeypatch.setenv('WEBULL_ENDPOINT', '')
    kwargs = {'WEBULL_TRADING_MODE': mode}
    for role in ('BULLISH_STOCK', 'TOP_BULLISH', 'OPTIONS_MARGIN'):
        kwargs[role + '_ACCOUNT_NUMBER'] = 'legacy'
        for environment in ('PAPER', 'LIVE'):
            kwargs[f'WEBULL_{environment}_{role}_ACCOUNT_NUMBER'] = f'{environment}-{role}'
    settings = cls(_env_file=None, **kwargs)
    for role in ('bullish_stock', 'top_bullish', 'options_margin'):
        assert getattr(settings, role + '_account_number') == f'{mode.upper()}-{role.upper()}'
    if cls is BullishSettings:
        assert settings.account_number == settings.top_bullish_account_number


def test_live_never_falls_back_to_legacy_or_paper(monkeypatch):
    monkeypatch.setenv('WEBULL_ENDPOINT', '')
    settings = Settings(_env_file=None, WEBULL_TRADING_MODE='live',
        BULLISH_STOCK_ACCOUNT_NUMBER='legacy', OPTIONS_MARGIN_ACCOUNT_NUMBER='legacy',
        WEBULL_PAPER_BULLISH_STOCK_ACCOUNT_NUMBER='paper',
        WEBULL_LIVE_BULLISH_STOCK_ACCOUNT_NUMBER='', WEBULL_LIVE_OPTIONS_MARGIN_ACCOUNT_NUMBER='')
    for resolver in (bullish_stock_account_id, options_margin_account_id):
        with pytest.raises(ValueError, match='WEBULL_LIVE_'):
            resolver(object(), settings)


def test_legacy_paper_configuration_remains_supported(monkeypatch):
    monkeypatch.setenv('WEBULL_ENDPOINT', '')
    settings = Settings(_env_file=None, WEBULL_TRADING_MODE='paper',
        BULLISH_STOCK_ACCOUNT_NUMBER='legacy', WEBULL_PAPER_BULLISH_STOCK_ACCOUNT_NUMBER='')
    assert settings.bullish_stock_account_number == 'legacy'
