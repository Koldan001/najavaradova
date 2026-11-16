"""One-off backfill: retag stored ns_toplana "почетак/наставак/завршетак радова" rows.

commit a740ae0 made the scraper store a works-lifecycle nstoplana post as category='WORKS'
(grey on the map, no notifications) unless its body announces an actual supply cut. That only
affects NEW scrapes - a post already in the DB keeps category='HEATING' forever, because the
raw_text_hash dedup means it is never re-scraped. Those rows stay orange and notifiable.

This applies the exact same decision (_WORKS_LIFECYCLE_SLUG + _is_supply_cut, imported from the
scraper) to existing rows and flips category to 'WORKS' where it matches. It never touches the
message_queue, so it cannot re-notify anyone. Idempotent - safe to run more than once.

Run:  python scripts/recategorise_works_lifecycle.py [--dry-run]
Then republish the web feed (scripts/publish_web_data.py, or the next scraper cycle).
"""
import argparse
import os
import sqlite3
import sys

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from core.database import get_connection
from scrapers.ns_toplana_scraper import _WORKS_LIFECYCLE_SLUG, _is_supply_cut


def run(dry_run: bool = False) -> None:
    conn = get_connection()
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("""
        SELECT id, category, source_url, raw_text
        FROM outages
        WHERE source_url LIKE '%nstoplana.rs%'
          AND (category IS NULL OR category != 'WORKS')
    """)
    rows = [dict(r) for r in cursor.fetchall()]

    to_flip = [
        r for r in rows
        if any(k in (r["source_url"] or "") for k in _WORKS_LIFECYCLE_SLUG)
        and not _is_supply_cut(r["raw_text"])
    ]

    print(f"nstoplana rows not already WORKS: {len(rows)}")
    print(f"  -> works-lifecycle slug with no supply-cut phrasing: {len(to_flip)}")
    for r in to_flip:
        print(f"     #{r['id']:5}  {r['category'] or 'NULL':8} -> WORKS   {r['source_url']}")

    if dry_run:
        print("\n--dry-run: no changes written.")
        conn.close()
        return

    if to_flip:
        cursor.executemany(
            "UPDATE outages SET category = 'WORKS' WHERE id = ?",
            [(r["id"],) for r in to_flip],
        )
        conn.commit()
        print(f"\n✅ Updated {len(to_flip)} row(s) to category='WORKS'.")
    else:
        print("\nNothing to update.")
    conn.close()
    print("Now republish: python scripts/publish_web_data.py  (or wait for the next scraper cycle)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="show what would change, write nothing")
    args = ap.parse_args()
    run(dry_run=args.dry_run)
