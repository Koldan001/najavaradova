"""Wipes outages inserted by scrapers/vikkg_scraper.py test runs so you can re-run it clean.
Matches on source_url (every row carries a jkpvik-kg.com link).

Usage: python scripts/reset_vikkg_test.py
"""
import sqlite3

DB_PATH = "data/outage_notifier.db"
MATCH = "%jkpvik-kg.com%"


def main():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    rows = cursor.execute("SELECT id FROM outages WHERE source_url LIKE ?", (MATCH,)).fetchall()
    ids = [r["id"] for r in rows]
    if not ids:
        print("Nothing to delete - no vikkg rows found.")
        conn.close()
        return

    placeholders = ",".join("?" * len(ids))
    cursor.execute(f"DELETE FROM message_queue WHERE outage_id IN ({placeholders})", ids)
    cursor.execute(f"DELETE FROM outage_hexes WHERE outage_id IN ({placeholders})", ids)
    cursor.execute(f"DELETE FROM outages WHERE id IN ({placeholders})", ids)
    conn.commit()
    conn.close()
    print(f"Deleted {len(ids)} vikkg outage(s): {ids}")


if __name__ == "__main__":
    main()
