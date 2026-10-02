"""Read-only dashboard for local trade and scheduler records."""
from collections import Counter
from contextlib import closing
from datetime import UTC, datetime
import hashlib
import json
import logging
from pathlib import Path
import sqlite3

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, JSONResponse

router = APIRouter()
STATIC = Path(__file__).parent / 'static'
logger = logging.getLogger('optionomics_bot')


def object_json(value):
    try:
        parsed = json.loads(value or '{}')
        return parsed if isinstance(parsed, dict) else {}
    except (ValueError, TypeError):
        return {}


def exit_view(row):
    state = object_json(row['state_json'])
    keys = ('entry_id', 'profit_id', 'stop_id', 'combo_id', 'due_at',
            'entry_filled_at', 'entry_filled_quantity', 'bracket_filled_quantity',
            'remaining_quantity', 'quantity', 'last_error', 'next_check_at',
            'entry_submission_error')
    result = {key: state.get(key) for key in keys}
    snapshots = state.get('order_snapshots', {})
    result['order_snapshots'] = {
        key: {field: value.get(field) for field in ('status', 'filled_quantity')}
        for key, value in snapshots.items() if isinstance(value, dict)
    } if isinstance(snapshots, dict) else {}
    attempts = state.get('market_orders', [])
    result['market_orders'] = [
        {key: attempt.get(key) for key in ('id', 'quantity', 'status', 'filled_quantity')}
        for attempt in attempts if isinstance(attempt, dict)
    ] if isinstance(attempts, list) else []
    result.update(id=row['id'], symbol=row['symbol'], status=row['status'], updated_at=row['updated_at'])
    return result


def read_trades(database_path):
    """Open an existing ledger read-only; never initialize/migrate runtime state."""
    path = Path(database_path).resolve()
    if not path.exists():
        return [], 'No ledger yet. Trades will appear after the bot processes ideas.'
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=5)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute('BEGIN')  # both tables from the same snapshot
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        ideas = conn.execute('SELECT * FROM optionomics_trade_ideas ORDER BY created_at DESC').fetchall() if 'optionomics_trade_ideas' in tables else []
        jobs = conn.execute('SELECT * FROM scheduled_stock_exits').fetchall() if 'scheduled_stock_exits' in tables else []

    exits = {row['id']: exit_view(row) for row in jobs}
    by_entry = {job['entry_id']: job for job in exits.values() if job.get('entry_id')}
    used = set()
    records = []
    for idea in ideas:
        order = object_json(idea['order_json'])
        decision = object_json(idea['decision_json'])
        bracket = order.get('bracket') if isinstance(order.get('bracket'), dict) else {}
        stable_id = 'om-' + hashlib.sha256(idea['trade_id'].encode()).hexdigest()[:24]
        job = by_entry.get(bracket.get('entry_id')) or exits.get(stable_id)
        if job:
            used.add(job['id'])
        records.append({
            'key': 'idea:' + idea['trade_id'], 'trade_id': idea['trade_id'],
            'symbol': idea['symbol'], 'direction': idea['direction'],
            'strategy': decision.get('strategy') or idea['strategy'],
            'pipeline': idea['pipeline_name'], 'status': idea['status'],
            'created_at': idea['created_at'], 'updated_at': idea['updated_at'],
            'action': decision.get('action'), 'notional_usd': decision.get('notional_usd'),
            'rationale': decision.get('rationale'), 'exit': job,
            'bracket': {key: bracket.get(key) for key in ('entry_id', 'profit_id', 'stop_id', 'combo_id')},
        })
    # Submission can succeed while the trade-idea status write fails, or the
    # caller may submit without a feed idea. Keep these jobs visible too.
    for job_id, job in exits.items():
        if job_id not in used:
            records.append({
                'key': 'exit:' + job_id, 'trade_id': None, 'symbol': job['symbol'],
                'status': 'unlinked', 'direction': None, 'strategy': None,
                'pipeline': None, 'action': None, 'notional_usd': None,
                'rationale': 'Exit job has no matching trade idea record.',
                'created_at': None, 'updated_at': job['updated_at'], 'exit': job,
                'bracket': {},
            })
    records.sort(key=lambda r: max(r['updated_at'] or '', (r['exit'] or {}).get('updated_at') or ''), reverse=True)
    return records, None


@router.get('/', include_in_schema=False)
def dashboard():
    return FileResponse(STATIC / 'dashboard.html', headers={'Cache-Control': 'no-store'})


@router.get('/dashboard.css', include_in_schema=False)
def dashboard_css():
    return FileResponse(STATIC / 'dashboard.css', media_type='text/css')


@router.get('/dashboard.js', include_in_schema=False)
def dashboard_js():
    return FileResponse(STATIC / 'dashboard.js', media_type='application/javascript')


@router.get('/api/trades')
def trade_status():
    from app.main import get_settings

    settings = get_settings()
    try:
        records, notice = read_trades(settings.database_path)
    except (sqlite3.Error, OSError):
        logger.exception('Unable to read trade dashboard ledger')
        raise HTTPException(status_code=503, detail='Trade ledger is temporarily unavailable. Try refreshing.') from None
    counts = Counter(record['status'] for record in records)
    jobs = [record['exit'] for record in records if record['exit']]
    return JSONResponse({
        'trades': records, 'notice': notice,
        'as_of': datetime.now(UTC).isoformat(),
        'summary': {
            'total': len(records), 'ordered': counts['ordered'],
            'active_exits': sum(job['status'] != 'complete' for job in jobs),
            'completed_exits': sum(job['status'] == 'complete' for job in jobs),
            'attention': sum(record['status'] == 'failed' or bool((record['exit'] or {}).get('last_error')) for record in records),
        },
        'settings': {
            'dashboard_refresh_interval_seconds': settings.dashboard_refresh_interval_seconds,
            'dry_run': settings.dry_run,
            'scheduler_enabled': settings.next_day_exit_enabled,
            'scheduler_running': settings.next_day_exit_enabled and not settings.dry_run,
            'exit_time': settings.next_day_exit_time,
            'timezone': settings.next_day_exit_timezone,
        },
    }, headers={'Cache-Control': 'no-store'})
