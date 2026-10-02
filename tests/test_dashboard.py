import hashlib
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import main
from app.ui.dashboard import read_trades
from app.persistence.ledger import Ledger


@pytest.fixture
def dashboard(tmp_path, monkeypatch):
    database = tmp_path / 'dashboard.sqlite3'
    settings = SimpleNamespace(database_path=str(database), dry_run=True,
                               dashboard_refresh_interval_seconds=3600,
                               next_day_exit_enabled=True, next_day_exit_time='09:35',
                               next_day_exit_timezone='America/New_York')
    monkeypatch.setattr(main, 'get_settings', lambda: settings)
    # No context-manager startup: UI tests must not start polling services.
    return TestClient(main.app), database


def add_job(ledger, trade_id='one', status='scheduled', **updates):
    job = {'id': 'om-' + hashlib.sha256(trade_id.encode()).hexdigest()[:24],
           'account_id': 'private-account', 'symbol': 'TSLA', 'status': status,
           'entry_id': 'entry-'+trade_id, 'profit_id': 'profit-'+trade_id,
           'stop_id': 'stop-'+trade_id, 'market_orders': [], 'quantity':'1',
           'due_at': '2026-09-08T13:35:00+00:00', **updates}
    ledger.register_exit_job(job)
    return job


def test_empty_dashboard_does_not_create_database(dashboard):
    client, database = dashboard
    response = client.get('/api/trades')
    assert response.status_code == 200
    assert response.json()['trades'] == []
    assert response.json()['settings']['scheduler_running'] is False
    assert not database.exists()
    assert response.headers['cache-control'] == 'no-store'


def test_ui_and_assets_are_served(dashboard):
    client, _ = dashboard
    page = client.get('/')
    assert page.status_code == 200
    assert 'Trade desk' in page.text
    assert 'Exit progress' in page.text
    assert client.get('/dashboard.css').status_code == 200
    assert client.get('/dashboard.js').status_code == 200
    assert client.post('/api/trades', json={}).status_code == 405


def test_distinct_statuses_are_joined_without_exposing_account_or_payload(dashboard):
    client, database = dashboard
    ledger = Ledger(str(database))
    ledger.save_trade_idea('one', {'symbol':'TSLA', 'private_field':'secret-payload'}, status='ordered')
    add_job(ledger)
    before = database.read_bytes()
    response = client.get('/api/trades')
    record = response.json()['trades'][0]
    assert record['status'] == 'ordered'
    assert record['exit']['status'] == 'scheduled'
    assert len(response.json()['trades']) == 1
    assert 'private-account' not in response.text
    assert 'secret-payload' not in response.text
    assert database.read_bytes() == before


def test_same_symbol_is_not_enough_to_link_trade(dashboard):
    client, database = dashboard
    ledger = Ledger(str(database))
    ledger.save_trade_idea('different', {'symbol':'TSLA'}, status='skipped')
    add_job(ledger)
    records = client.get('/api/trades').json()['trades']
    assert len(records) == 2
    assert next(t for t in records if t['trade_id']=='different')['exit'] is None
    assert next(t for t in records if t['status']=='unlinked')['exit']['status']=='scheduled'


def test_explicit_bracket_id_links_legacy_trade(dashboard):
    client, database = dashboard
    ledger = Ledger(str(database))
    ledger.save_trade_idea('legacy', {'symbol':'TSLA'})
    add_job(ledger)
    ledger.mark_trade_idea_status('legacy',status='ordered',order_payload={'bracket':{'entry_id':'entry-one'}})
    assert len(client.get('/api/trades').json()['trades']) == 1


def test_completed_and_error_jobs_count_separately(dashboard):
    client, database = dashboard
    ledger = Ledger(str(database))
    add_job(ledger, status='complete')
    add_job(ledger,'two',last_error='Cancellation not confirmed')
    result=client.get('/api/trades').json()
    assert result['summary']['active_exits']==1
    assert result['summary']['completed_exits']==1
    assert result['summary']['attention']==1


def test_old_ledger_without_scheduler_table(tmp_path):
    import sqlite3
    database = tmp_path / 'old.sqlite3'
    ledger = Ledger(str(database))
    ledger.save_trade_idea('old', {'symbol':'TSLA'},status='dry_run')
    with sqlite3.connect(database) as conn:
        conn.execute('DROP TABLE scheduled_stock_exits')
        conn.execute('UPDATE optionomics_trade_ideas SET decision_json=?',('invalid json',))
    records,_=read_trades(database)
    assert records[0]['status']=='dry_run'
    assert records[0]['exit'] is None


def test_corrupt_database_returns_useful_error(dashboard):
    client,database=dashboard
    database.write_text('not sqlite')
    response=client.get('/api/trades')
    assert response.status_code==503
    assert 'temporarily unavailable' in response.json()['detail']
    assert str(database) not in response.text
