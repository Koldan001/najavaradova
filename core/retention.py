"""Housekeeping: delete the short-lived auth artifacts that nothing reads once they are spent
or expired.

Deliberately narrow. It touches ONLY login_tokens, sessions and pending_registrations - never
outages, message_queue, target_hexes, targets, subscribers, alert_log or api_usage_daily.
Outage history and the per-address record of what was sent are kept indefinitely on purpose: a
future "how many outages has your address had" view is built from exactly that data.

Called once per pipeline cycle from main.py (every 15 min). The DELETEs are near-no-ops when
there is nothing old, so the cadence costs nothing.
"""
from core.database import get_connection

# Grace windows - well past the point any of these can still be used, but long enough that a
# clock skew or a slow client can't have a still-live token deleted from under it.
_LOGIN_TOKEN_GRACE_DAYS = 7   # tokens live 15 min; a week is pure paranoia margin
_SESSION_GRACE_DAYS = 2       # expires_at is a sliding 30-day window; past it the cookie is dead
_PENDING_GRACE_DAYS = 2       # Telegram deep-links are only honoured < 24h old (telegram_bot.py)


def prune_ephemeral() -> dict:
    """Delete spent/expired auth rows past their grace window. Returns {table: rows_deleted}."""
    conn = get_connection()
    try:
        cur = conn.cursor()
        deleted = {}

        cur.execute(
            "DELETE FROM login_tokens "
            "WHERE (used_at IS NOT NULL OR expires_at < datetime('now')) "
            "  AND created_at < datetime('now', ?)",
            (f"-{_LOGIN_TOKEN_GRACE_DAYS} days",),
        )
        deleted["login_tokens"] = cur.rowcount

        cur.execute(
            "DELETE FROM sessions WHERE expires_at < datetime('now', ?)",
            (f"-{_SESSION_GRACE_DAYS} days",),
        )
        deleted["sessions"] = cur.rowcount

        cur.execute(
            "DELETE FROM pending_registrations "
            "WHERE consumed_at IS NOT NULL OR created_at < datetime('now', ?)",
            (f"-{_PENDING_GRACE_DAYS} days",),
        )
        deleted["pending_registrations"] = cur.rowcount

        conn.commit()
        # Cheap, lock-free; lets SQLite refresh stats/indexes after the deletes. NOT VACUUM -
        # that rewrites the whole file under an exclusive lock and these tables are tiny.
        conn.execute("PRAGMA optimize")
        return deleted
    finally:
        conn.close()


if __name__ == "__main__":
    print(prune_ephemeral())
