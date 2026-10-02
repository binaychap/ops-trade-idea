from __future__ import annotations

import json
import sqlite3
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Any
from datetime import UTC, datetime


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


class Ledger:
    def __init__(self, database_path: str) -> None:
        self.path = Path(database_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    def initialize(self) -> None:
        with closing(sqlite3.connect(self.path, timeout=10)) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS events (
                    fingerprint TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    decision_json TEXT,
                    order_json TEXT,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS optionomics_trade_ideas (
                    trade_id TEXT PRIMARY KEY,
                    symbol TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    strategy TEXT,
                    pipeline_name TEXT,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    decision_json TEXT,
                    order_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS bearish_option_jobs (
                    job_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    state_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.commit()

        with closing(sqlite3.connect(self.path, timeout=10)) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS scheduled_stock_exits (
                    id TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    status TEXT NOT NULL,
                    state_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)
            conn.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS one_active_stock_exit
                ON scheduled_stock_exits(account_id, symbol)
                WHERE status != 'complete'
            """)
            conn.commit()

    @contextmanager
    def write_lock(self):
        """Serializes all SQLite writes across threads/processes using a single file lock."""
        import fcntl

        with self.path.with_suffix(self.path.suffix + '.write.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    @contextmanager
    def exit_worker_lock(self):
        """Single-host process/thread exclusion, automatically released on crash."""
        import fcntl

        with self.path.with_suffix(self.path.suffix + '.exits.lock').open('a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                yield False
                return
            try:
                yield True
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    @contextmanager
    def bearish_option_worker_lock(self):
        """Single-host exclusion for the fill-to-exit option reconciler."""
        import fcntl

        with self.path.with_suffix(self.path.suffix + '.bearish-options.lock').open('a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                yield False
                return
            try:
                yield True
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def register_exit_job(self, job):
        with self.write_lock():
            with closing(sqlite3.connect(self.path, timeout=30)) as conn:
                conn.execute(
                    "INSERT OR IGNORE INTO scheduled_stock_exits VALUES (?, ?, ?, ?, ?, ?)",
                    (job['id'], job['account_id'], job['symbol'], job['status'],
                     json.dumps(job), utc_now_iso()),
                )
                conn.commit()

    def has_active_exit_job(self, *, account_id: str, symbol: str) -> bool:
        with closing(sqlite3.connect(self.path, timeout=10)) as conn:
            row = conn.execute(
                "SELECT 1 FROM scheduled_stock_exits WHERE account_id = ? AND symbol = ? AND status != 'complete' LIMIT 1",
                (account_id, str(symbol).upper()),
            ).fetchone()
        return row is not None

    def exit_jobs(self, *, include_complete=False):
        with closing(sqlite3.connect(self.path, timeout=10)) as conn:
            rows = conn.execute(
                'SELECT state_json FROM scheduled_stock_exits '
                + ('' if include_complete else "WHERE status != 'complete' ")
                + 'ORDER BY updated_at, id'
            ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def save_exit_job(self, job):
        with self.write_lock():
            with closing(sqlite3.connect(self.path, timeout=30)) as conn:
                conn.execute(
                    'UPDATE scheduled_stock_exits SET status=?, state_json=?, updated_at=? WHERE id=?',
                    (job['status'], json.dumps(job), utc_now_iso(), job['id']),
                )
                conn.commit()

    def save_trade_idea(self, trade_id: str, payload: dict[str, Any], *, status: str = "queued") -> None:
        now = utc_now_iso()
        payload_json = json.dumps(payload, sort_keys=True)

        with self.write_lock():
            with closing(sqlite3.connect(self.path, timeout=30)) as conn:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO optionomics_trade_ideas
                        (trade_id, symbol, direction, strategy, pipeline_name, payload_json, status, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        trade_id,
                        str(payload.get("symbol") or ""),
                        str(payload.get("direction") or "neutral"),
                        str(payload.get("strategy") or ""),
                        str(payload.get("pipeline_name") or payload.get("pipeline_short_name") or ""),
                        payload_json,
                        status,
                        now,
                        now,
                    ),
                )
                conn.commit()

    def is_trade_idea_seen(self, trade_id: str, status: str | None = None) -> bool:
        """Return True if a trade idea with `trade_id` exists.

        If `status` is provided, restrict the check to rows with that status.
        """
        if status is None:
            with closing(sqlite3.connect(self.path, timeout=10)) as conn:
                row = conn.execute(
                    "SELECT 1 FROM optionomics_trade_ideas WHERE trade_id = ? LIMIT 1",
                    (trade_id,),
                ).fetchone()
        else:
            with closing(sqlite3.connect(self.path, timeout=10)) as conn:
                row = conn.execute(
                    "SELECT 1 FROM optionomics_trade_ideas WHERE status = ? AND trade_id = ? LIMIT 1",
                    (status, trade_id),
                ).fetchone()
        return row is not None

    def has_trade_or_symbol_seen(self, *, trade_id: str | None = None, symbol: str | None = None) -> bool:
        if trade_id is None and symbol is None:
            return False

        if trade_id is None:
            return False

        with closing(sqlite3.connect(self.path, timeout=10)) as conn:
            row = conn.execute(
                "SELECT 1 FROM optionomics_trade_ideas WHERE trade_id = ? LIMIT 1",
                (str(trade_id),),
            ).fetchone()
        return row is not None

    def has_ordered_trade(self, *, trade_id: str | None = None, symbol: str | None = None) -> bool:
        if trade_id is None and symbol is None:
            return False

        trade_id = str(trade_id).strip() if trade_id is not None else None
        symbol = str(symbol).strip().upper() if symbol is not None else None

        with closing(sqlite3.connect(self.path, timeout=10)) as conn:
            if trade_id is not None and symbol is not None:
                row = conn.execute(
                    "SELECT 1 FROM optionomics_trade_ideas WHERE status = ? AND trade_id = ? AND symbol = ? LIMIT 1",
                    ("ordered", trade_id, symbol),
                ).fetchone()
            elif trade_id is not None:
                row = conn.execute(
                    "SELECT 1 FROM optionomics_trade_ideas WHERE status = ? AND trade_id = ? LIMIT 1",
                    ("ordered", trade_id),
                ).fetchone()
            elif symbol is not None:
                row = conn.execute(
                    "SELECT 1 FROM optionomics_trade_ideas WHERE status = ? AND symbol = ? LIMIT 1",
                    ("ordered", symbol),
                ).fetchone()
            else:
                return False
        return row is not None

    def mark_trade_idea_status(self, trade_id: str, *, status: str, decision: Any | None = None, order_payload: dict[str, Any] | None = None) -> None:
        now = utc_now_iso()
        decision_json = json.dumps(decision.model_dump(mode="json") if hasattr(decision, "model_dump") else decision or {}, sort_keys=True) if decision else None
        order_json = json.dumps(order_payload or {}, sort_keys=True)

        with self.write_lock():
            with closing(sqlite3.connect(self.path, timeout=30)) as conn:
                conn.execute(
                    """
                    UPDATE optionomics_trade_ideas
                       SET status = ?, decision_json = ?, order_json = ?, updated_at = ?
                     WHERE trade_id = ?
                    """,
                    (status, decision_json, order_json, now, trade_id),
                )
                conn.commit()

    def register_bearish_option_job(self, job_id: str, job: dict[str, Any]) -> bool:
        now = utc_now_iso()
        state = dict(job)
        with self.write_lock():
            with closing(sqlite3.connect(self.path, timeout=30)) as conn:
                cursor = conn.execute(
                    """
                    INSERT OR IGNORE INTO bearish_option_jobs
                        (job_id, status, state_json, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (job_id, state["status"], json.dumps(state, sort_keys=True), now, now),
                )
                conn.commit()
        return cursor.rowcount == 1

    def bearish_option_job(self, job_id: str) -> dict[str, Any] | None:
        with closing(sqlite3.connect(self.path, timeout=10)) as conn:
            row = conn.execute(
                "SELECT state_json FROM bearish_option_jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
        return json.loads(row[0]) if row else None

    def bearish_option_jobs(self) -> list[dict[str, Any]]:
        with closing(sqlite3.connect(self.path, timeout=10)) as conn:
            rows = conn.execute(
                """
                SELECT state_json FROM bearish_option_jobs
                 WHERE status IN (
                    'entry_submitting', 'entry_pending', 'entry_cancel_pending',
                    'exits_submitting'
                 )
                 ORDER BY created_at, job_id
                """
            ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def save_bearish_option_job(self, job_id: str, job: dict[str, Any]) -> None:
        now = utc_now_iso()
        state = dict(job)
        with self.write_lock():
            with closing(sqlite3.connect(self.path, timeout=30)) as conn:
                cursor = conn.execute(
                    """
                    UPDATE bearish_option_jobs
                       SET status = ?, state_json = ?, updated_at = ?
                     WHERE job_id = ?
                    """,
                    (state["status"], json.dumps(state, sort_keys=True), now, job_id),
                )
                conn.commit()
        if cursor.rowcount != 1:
            raise KeyError(f"Bearish option job not found: {job_id}")

    def reserve(self, fingerprint: str, payload: Any) -> bool:
        now = utc_now_iso()
        payload_json = json.dumps(payload, sort_keys=True)

        with self.write_lock():
            with closing(sqlite3.connect(self.path, timeout=30)) as conn:
                cursor = conn.execute(
                    """
                    INSERT OR IGNORE INTO events
                        (fingerprint, status, payload_json, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (fingerprint, "queued", payload_json, now, now),
                )
                conn.commit()

        return cursor.rowcount == 1

    def finish(self, fingerprint: str, status: str, decision: Any, order_payload: dict[str, Any] | None = None) -> None:
        now = utc_now_iso()
        decision_json = json.dumps(decision.model_dump(mode="json") if hasattr(decision, "model_dump") else decision or {}, sort_keys=True)
        order_json = json.dumps(order_payload or {}, sort_keys=True)

        with self.write_lock():
            with closing(sqlite3.connect(self.path, timeout=30)) as conn:
                conn.execute(
                    """
                                    UPDATE events
                                         SET status = ?, decision_json = ?, order_json = ?, error = NULL, updated_at = ?
                                     WHERE fingerprint = ?
                    """,
                    (status, decision_json, order_json, now, fingerprint),
                )
                conn.commit()

    def fail(self, fingerprint: str, error: str) -> None:
        now = utc_now_iso()

        with self.write_lock():
            with closing(sqlite3.connect(self.path, timeout=30)) as conn:
                conn.execute(
                    """
                                    UPDATE events
                                         SET status = ?, error = ?, updated_at = ?
                                     WHERE fingerprint = ?
                    """,
                    ("failed", error[:1000], now, fingerprint),
                )
                conn.commit()

    def morning_sell_holdings(self) -> list[dict[str, Any]]:
        """Stock holdings the daily morning sell should liquidate.

        Union of actively tracked stock exits and submitted bullish-runner
        trades, deduplicated by (account_id, symbol). Scheduled exits carry
        their broker account_id; bullish-runner rows leave account_id None
        for the caller to resolve from settings.
        """
        holdings: list[dict[str, Any]] = []
        seen: set[tuple[str | None, str]] = set()
        with closing(sqlite3.connect(self.path, timeout=10)) as conn:
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "scheduled_stock_exits" in tables:
                rows = conn.execute(
                    "SELECT account_id, symbol, state_json FROM scheduled_stock_exits WHERE status != 'complete'"
                ).fetchall()
                for account_id, symbol, state_json in rows:
                    try:
                        job = json.loads(state_json)
                    except (ValueError, TypeError):
                        continue
                    tracked = job.get("entry_filled_quantity") or job.get("quantity") or "0"
                    key = (account_id, str(symbol).upper())
                    if key in seen:
                        continue
                    seen.add(key)
                    holdings.append({
                        "account_id": account_id,
                        "symbol": str(symbol).upper(),
                        "tracked_quantity": str(tracked),
                        "source": "scheduled_stock_exits",
                        "job": job,
                    })
            if "top_bullish_trades" in tables:
                rows = conn.execute(
                    "SELECT symbol, order_request_json FROM top_bullish_trades WHERE status = 'submitted'"
                ).fetchall()
                for symbol, order_request_json in rows:
                    try:
                        order_request = json.loads(order_request_json or "{}")
                    except (ValueError, TypeError):
                        order_request = {}
                    key = (None, str(symbol).upper())
                    if key in seen:
                        continue
                    seen.add(key)
                    holdings.append({
                        "account_id": None,
                        "symbol": str(symbol).upper(),
                        "tracked_quantity": str(order_request.get("quantity") or 0),
                        "source": "top_bullish_trades",
                        "job": None,
                    })
        return holdings
