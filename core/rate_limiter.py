"""Proactive RPM pacer for Gemini calls - spaces calls to a model at a fixed interval
(60/rpm_limit seconds) so any rolling 60s window never exceeds rpm_limit requests, instead of
firing as fast as the network allows and reacting to 429s after they've already happened.

DB-persisted (not in-memory) so pacing holds across separate scraper processes run one after
another, not just within a single process - consistent with core/api_budget.py's approach.

Not built for concurrent callers (two scrapers actually running at the same instant) - this
project's scrapers run sequentially, one at a time, so a read-then-write race here is a
theoretical gap, not a real one given how this is actually used.
"""
import time
from core.database import get_connection


def _init_table(cursor):
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS api_rate_limiter (
            model TEXT PRIMARY KEY,
            next_allowed_at REAL NOT NULL
        )
    ''')


def wait_for_slot(model: str, rpm_limit: int):
    """Sleeps until it's safe to call `model` again without exceeding rpm_limit req/min, then
    reserves that slot before returning."""
    min_interval = 60.0 / rpm_limit
    conn = get_connection()
    try:
        cursor = conn.cursor()
        _init_table(cursor)
        cursor.execute("SELECT next_allowed_at FROM api_rate_limiter WHERE model = ?", (model,))
        row = cursor.fetchone()
        now = time.time()
        next_allowed = row[0] if row else now

        wait_time = next_allowed - now
        if wait_time > 0:
            time.sleep(wait_time)
            now = time.time()

        reserved = max(now, next_allowed) + min_interval
        cursor.execute('''
            INSERT INTO api_rate_limiter (model, next_allowed_at) VALUES (?, ?)
            ON CONFLICT(model) DO UPDATE SET next_allowed_at = excluded.next_allowed_at
        ''', (model, reserved))
        conn.commit()
    finally:
        conn.close()
