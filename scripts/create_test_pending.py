"""Stand-in for the web form, which doesn't exist yet. Creates one pending_registrations row
with a fake test address and prints the exact Telegram deep-link a real form would redirect the
browser to - open it, tap Start, and workers/telegram_bot.py (must be running) completes the
registration for real.

Usage: python scripts/create_test_pending.py
"""
import secrets
import sys
import os

import requests

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from core.database import get_connection


def get_bot_username():
    res = requests.get(f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/getMe", timeout=10)
    data = res.json()
    if not data.get("ok"):
        raise RuntimeError(f"getMe failed: {data}")
    return data["result"]["username"]


def main():
    if not config.TELEGRAM_BOT_TOKEN:
        print("❌ TELEGRAM_BOT_TOKEN is not set - see .env.example. Aborting.")
        return

    code = secrets.token_urlsafe(8)

    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO pending_registrations (code, channel, city, ulica, broj)
        VALUES (?, 'telegram', 'НОВИ САД', 'Bulevar Oslobođenja', '10')
    ''', (code,))
    conn.commit()
    conn.close()

    username = get_bot_username()
    link = f"https://t.me/{username}?start={code}"

    print(f"✅ Pending registration created, code={code}")
    print(f"👉 Open this link, tap Start:\n\n{link}\n")


if __name__ == "__main__":
    main()
