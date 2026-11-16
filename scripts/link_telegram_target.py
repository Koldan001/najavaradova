"""Manually attach a Telegram chat_id to an existing target row, for testing before the real
self-serve registration flow (address collection + activation code) exists.

Usage: python scripts/link_telegram_target.py <target_id> <chat_id>
"""
import sys
import os

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from core.database import get_connection


def main():
    if len(sys.argv) != 3:
        print("Usage: python scripts/link_telegram_target.py <target_id> <chat_id>")
        sys.exit(1)

    target_id, chat_id = sys.argv[1], sys.argv[2]

    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE targets SET telegram_chat_id = ? WHERE id = ?", (chat_id, target_id))
    if cursor.rowcount == 0:
        print(f"⚠️ No target with id {target_id}.")
    else:
        print(f"✅ Target #{target_id} linked to Telegram chat_id {chat_id}.")
    conn.commit()
    conn.close()


if __name__ == "__main__":
    main()
