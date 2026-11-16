"""Wipes outages inserted by ns_toplana_scraper.py test runs so you can re-run the scraper
clean. Matches on heating-specific vocabulary ('топлотне'/'топлан') that EPS (power) and the
NS water scraper don't use, so it won't touch their rows.

Usage: python scripts/reset_toplana_test.py
"""
import sqlite3

DB_PATH = "data/outage_notifier.db"
MATCH = "%топлотне%"  # "топлотне (енергије)" - appears in every nstoplana.rs announcement


def main():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    rows = cursor.execute("SELECT id FROM outages WHERE raw_text LIKE ?", (MATCH,)).fetchall()
    ids = [r["id"] for r in rows]

    if not ids:
        print("Nothing to delete - no toplana rows found.")
        conn.close()
        return

    placeholders = ",".join("?" * len(ids))
    cursor.execute(f"DELETE FROM message_queue WHERE outage_id IN ({placeholders})", ids)
    cursor.execute(f"DELETE FROM outage_hexes WHERE outage_id IN ({placeholders})", ids)
    cursor.execute(f"DELETE FROM outages WHERE id IN ({placeholders})", ids)
    conn.commit()
    conn.close()

    print(f"Deleted {len(ids)} toplana outage(s): {ids}")


if __name__ == "__main__":
    main()
