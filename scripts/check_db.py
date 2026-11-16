import sqlite3

def check_latest_outages():
    conn = sqlite3.connect("data/outage_notifier.db")
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    outages = cursor.execute("""
        SELECT o.id, o.region, o.affected_streets, o.outage_date, o.time_window, 
               COUNT(oh.hex_id) as hex_count,
               (SELECT COUNT(*) FROM message_queue mq WHERE mq.outage_id = o.id) as matched_targets
        FROM outages o
        LEFT JOIN outage_hexes oh ON o.id = oh.outage_id
        GROUP BY o.id
        ORDER BY o.id DESC
        LIMIT 10
    """).fetchall()

    conn.close()

    print("\n=== LATEST PARSED OUTAGES IN DATABASE ===")
    for o in outages:
        print(f"ID #{o['id']} | Date: {o['outage_date']} | Time: {o['time_window']}")
        print(f"  Streets: {o['affected_streets']}")
        print(f"  H3 Hexes: {o['hex_count']} | Matched Notifications: {o['matched_targets']}")
        print("-" * 65)

if __name__ == "__main__":
    check_latest_outages()
