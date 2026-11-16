"""Operational alerting - a Telegram DM to the operator when the pipeline breaks.

One channel, one function: send_operator_alert(key, text). Each `key` has a cooldown so a
persistent problem pings once an hour instead of every watchdog cycle; clear_alert(key) resets
it (and optionally announces recovery) so the next occurrence alerts immediately.

Deliberately best-effort: if OPERATOR_CHAT_ID or TELEGRAM_BOT_TOKEN is unset, or Telegram is
unreachable, the alert is logged and the caller carries on - alerting must never take down the
thing it's watching.
"""
import time

import requests

import config
from core.database import get_connection


def _ensure_table():
    conn = get_connection()
    conn.execute(
        "CREATE TABLE IF NOT EXISTS alert_log (alert_key TEXT PRIMARY KEY, last_sent REAL NOT NULL)"
    )
    conn.commit()
    conn.close()


def _post(text: str) -> bool:
    token = config.TELEGRAM_BOT_TOKEN
    chat_id = getattr(config, "OPERATOR_CHAT_ID", None)
    if not token or not chat_id:
        print(f"[alert] (not sent - no OPERATOR_CHAT_ID/TELEGRAM_BOT_TOKEN)\n{text}")
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "disable_web_page_preview": True},
            timeout=10,
        )
        return bool(r.ok and r.json().get("ok"))
    except Exception as e:
        print(f"[alert] send failed: {e}")
        return False


def send_operator_note(text: str) -> bool:
    """A plain operator DM with no cooldown and no alarm framing - for good news (a new signup)
    rather than a fault. Same best-effort contract as send_operator_alert: never raises."""
    return _post(text)


def send_operator_alert(alert_key: str, text: str, cooldown_s: int = 3600) -> bool:
    """Send `text` to the operator, at most once per `cooldown_s` for this `alert_key`.
    Returns True only if a message was actually delivered this call."""
    _ensure_table()
    conn = get_connection()
    row = conn.execute(
        "SELECT last_sent FROM alert_log WHERE alert_key = ?", (alert_key,)
    ).fetchone()
    now = time.time()
    if row and (now - row["last_sent"]) < cooldown_s:
        conn.close()
        return False

    sent = _post(f"\U0001f6a8 NajavaRadova\n{text}")
    if sent:
        conn.execute(
            "INSERT INTO alert_log (alert_key, last_sent) VALUES (?, ?) "
            "ON CONFLICT(alert_key) DO UPDATE SET last_sent = excluded.last_sent",
            (alert_key, now),
        )
        conn.commit()
    conn.close()
    return sent


def clear_alert(alert_key: str, recovery_text: str | None = None) -> None:
    """Condition recovered: drop the cooldown so the next occurrence alerts immediately. If a
    cooldown row existed (i.e. we had actually alerted) and recovery_text is given, announce it."""
    _ensure_table()
    conn = get_connection()
    existed = conn.execute(
        "SELECT 1 FROM alert_log WHERE alert_key = ?", (alert_key,)
    ).fetchone()
    conn.execute("DELETE FROM alert_log WHERE alert_key = ?", (alert_key,))
    conn.commit()
    conn.close()
    if existed and recovery_text:
        _post(f"✅ NajavaRadova\n{recovery_text}")
