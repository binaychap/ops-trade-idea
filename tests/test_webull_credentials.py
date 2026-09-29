import pytest
from app.config.webull import WebullSettings, resolve_webull_credentials


@pytest.mark.parametrize('mode', ['paper', 'live'])
def test_selects_only_matching_pair(mode):
    settings = WebullSettings(_env_file=None, WEBULL_TRADING_MODE=mode,
        WEBULL_ENDPOINT='', WEBULL_PAPER_APP_KEY='paper-key',
        WEBULL_PAPER_APP_SECRET='paper-secret', WEBULL_LIVE_APP_KEY='live-key',
        WEBULL_LIVE_APP_SECRET='live-secret')
    assert resolve_webull_credentials(settings) == (f'{mode}-key', f'{mode}-secret')
    assert f'{mode}-secret' not in repr(settings)


def test_live_does_not_fall_back():
    settings = WebullSettings(_env_file=None, WEBULL_TRADING_MODE='live', WEBULL_ENDPOINT='',
        WEBULL_LIVE_APP_KEY='', WEBULL_LIVE_APP_SECRET='',
        WEBULL_APP_KEY='legacy', WEBULL_APP_SECRET='legacy')
    with pytest.raises(ValueError, match='WEBULL_LIVE_APP_KEY'):
        resolve_webull_credentials(settings)


def test_paper_legacy_pair_and_partial_pair():
    settings = WebullSettings(_env_file=None, WEBULL_TRADING_MODE='paper', WEBULL_ENDPOINT='',
        WEBULL_PAPER_APP_KEY='', WEBULL_PAPER_APP_SECRET='',
        WEBULL_APP_KEY='legacy-key', WEBULL_APP_SECRET='legacy-secret')
    assert resolve_webull_credentials(settings) == ('legacy-key', 'legacy-secret')
    settings.webull_paper_app_key = 'new-key'
    with pytest.raises(ValueError, match='WEBULL_PAPER_APP_KEY'):
        resolve_webull_credentials(settings)
