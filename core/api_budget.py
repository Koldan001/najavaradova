"""Hard daily-call governor for Gemini models, keyed by a budget name and persisted in SQLite so it
survives process restarts. Wired into the two users of config.GEMINI_FALLBACK_MODEL_NAME (the
stronger tier with only 20 requests a day on the free plan): core/matchmaker.py's retry of a fully
unresolved outage, and core/alert_check.py's second reading. Each has its own share (budget names
"<model>:retry" and "<model>:check"), so a busy day of second readings can't eat the retries that
decide real alerts. The primary model (config.MODEL_NAME) isn't capped here.

What counts: every request sent to Google, not every job. parser._generate takes one unit before
each attempt, retries and timed-out requests included, because Google counts those too. On
10 Nov 2026 counting jobs let a cap of 15 reach 21/20 on Google's dashboard (a job can retry).

When the day turns: midnight Pacific time, when Google resets its daily quota, not midnight in
Serbia - otherwise one of Google's days can hold parts of two of ours.

Check the real quota in AI Studio's rate-limit view and set GEMINI_FALLBACK_DAILY_LIMIT to match.
"""
import datetime
from zoneinfo import ZoneInfo

from core.database import get_connection

# Google's free-tier daily quotas reset at midnight Pacific time.
_QUOTA_TZ = ZoneInfo("America/Los_Angeles")


def _today() -> str:
    return datetime.datetime.now(_QUOTA_TZ).date().isoformat()


def _init_table(cursor):
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS api_usage_daily (
            model TEXT NOT NULL,
            usage_date TEXT NOT NULL,
            call_count INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (model, usage_date)
        )
    ''')


def try_consume_budget(model: str, daily_limit: int) -> bool:
    """Atomically checks-and-increments today's call count for `model`. Returns True (and counts
    the call) if under budget, False (and does NOT count it) if today's limit is already spent.
    Never raises past the caller on a fresh/missing table - if this governor itself is broken,
    the safe failure mode is "refuse the extra call", not "silently allow unlimited calls"."""
    today = _today()
    conn = get_connection()
    try:
        cursor = conn.cursor()
        _init_table(cursor)
        cursor.execute(
            "INSERT OR IGNORE INTO api_usage_daily (model, usage_date, call_count) VALUES (?, ?, 0)",
            (model, today)
        )
        cursor.execute(
            "SELECT call_count FROM api_usage_daily WHERE model = ? AND usage_date = ?",
            (model, today)
        )
        count = cursor.fetchone()[0]
        if count >= daily_limit:
            return False

        cursor.execute(
            "UPDATE api_usage_daily SET call_count = call_count + 1 WHERE model = ? AND usage_date = ?",
            (model, today)
        )
        conn.commit()
        return True
    finally:
        conn.close()


def used_today(model: str) -> int:
    today = _today()
    conn = get_connection()
    try:
        cursor = conn.cursor()
        _init_table(cursor)
        cursor.execute(
            "SELECT call_count FROM api_usage_daily WHERE model = ? AND usage_date = ?",
            (model, today)
        )
        row = cursor.fetchone()
        return row[0] if row else 0
    finally:
        conn.close()


def remaining(model: str, daily_limit: int) -> int:
    """Units left today under `daily_limit` for this budget name, without taking any."""
    return max(0, daily_limit - used_today(model))
