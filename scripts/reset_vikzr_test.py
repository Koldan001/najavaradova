"""Wipes outages inserted by scrapers/vikzr_scraper.py test runs so you can re-run the scraper
clean. Matches on source_url (every vikzr row carries a vikzr.rs link), so it won't touch the
legacy hand-entered Zrenjanin row that predates the scraper or any other city's rows.

Usage: python scripts/reset_vikzr_test.py
"""
import sqlite3

DB_PATH = "data/outage_notifier.db"
MATCH = "%vikzr.rs%"


def main():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    rows = cursor.execute("SELECT id FROM outages WHERE source_url LIKE ?", (MATCH,)).fetchall()
    ids = [r["id"] for r in rows]

    if not ids:
        print("Nothing to delete - no vikzr rows found.")
        conn.close()
        return

    placeholders = ",".join("?" * len(ids))
    cursor.execute(f"DELETE FROM message_queue WHERE outage_id IN ({placeholders})", ids)
    cursor.execute(f"DELETE FROM outage_hexes WHERE outage_id IN ({placeholders})", ids)
    cursor.execute(f"DELETE FROM outages WHERE id IN ({placeholders})", ids)
    conn.commit()
    conn.close()

    print(f"Deleted {len(ids)} vikzr outage(s): {ids}")


if __name__ == "__main__":
    main()
