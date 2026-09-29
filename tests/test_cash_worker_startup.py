from types import SimpleNamespace
from unittest.mock import Mock

from app.exits import live_cash


def setup_worker(monkeypatch, jobs, client):
    event = SimpleNamespace(is_set=Mock(side_effect=[False, True]), wait=Mock())
    monkeypatch.setattr(live_cash.threading, 'Event', lambda: event)
    monkeypatch.setattr(live_cash.threading, 'Thread', lambda **kw: SimpleNamespace(start=kw['target']))
    monkeypatch.setattr(live_cash, 'Ledger', lambda path: SimpleNamespace(exit_jobs=lambda: jobs))
    monkeypatch.setattr('app.broker.client.get_trade_client', client)
    return event


def test_empty_or_paper_ledger_never_initializes_sdk(monkeypatch):
    client = Mock(side_effect=AssertionError('unexpected authentication'))
    setup_worker(monkeypatch, [{'kind': 'bracket'}], client)
    live_cash.start_cash_exit_worker(SimpleNamespace(webull_trading_mode='live', dry_run=False, database_path='unused'))
    client.assert_not_called()


def test_auth_failure_uses_actionable_message_and_backoff(monkeypatch, caplog):
    class AuthenticationError(Exception):
        http_status = 401
    client = Mock(side_effect=AuthenticationError())
    event = setup_worker(monkeypatch, [{'kind': 'live_cash'}], client)
    live_cash.start_cash_exit_worker(SimpleNamespace(webull_trading_mode='live', dry_run=False, database_path='unused'))
    event.wait.assert_called_once_with(60)
    assert 'WEBULL_LIVE_APP_KEY' in caplog.text
    assert 'Exits are not being monitored' in caplog.text


def test_active_cash_jobs_still_start_reconciliation(monkeypatch):
    client = Mock(return_value=object())
    event = setup_worker(monkeypatch, [{'kind': 'live_cash'}], client)
    reconcile = Mock()
    monkeypatch.setattr(live_cash, 'CashExitScheduler', lambda *args: SimpleNamespace(run_once=reconcile))
    live_cash.start_cash_exit_worker(SimpleNamespace(webull_trading_mode='live', dry_run=False, database_path='unused'))
    client.assert_called_once()
    reconcile.assert_called_once()
    event.wait.assert_called_once_with(10)
