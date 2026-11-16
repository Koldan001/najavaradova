"""A Važno obaveštenje by hand - for what no scraper reads (Novi Sad's heating season start is
often only in the news). It only makes the draft: the operator still approves it on Telegram
(✅ / ❌), exactly like one made from a scraped notice. See core/broadcast.py.

    python -m scripts.broadcast --city "Novi Sad" --text "🔥 Grejna sezona počinje 15.10. ..." [--url URL]
    python -m scripts.broadcast --list
"""
import argparse
import os
import sys

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from core import broadcast
from core.database import get_connection


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--city")
    ap.add_argument("--text")
    ap.add_argument("--url", help="the official post, shown under the text")
    ap.add_argument("--list", action="store_true", help="the last 20 notices and how many got each")
    args = ap.parse_args(argv)

    conn = get_connection()
    if args.list:
        for b in conn.execute(
                "SELECT b.id, b.city, b.status, b.created_at, "
                "(SELECT COUNT(*) FROM broadcast_sends s WHERE s.broadcast_id = b.id AND s.status = 'SENT') AS sent "
                "FROM broadcasts b ORDER BY b.id DESC LIMIT 20"):
            print(f"#{b['id']}  {b['created_at']}  {broadcast.city_label(b['city'])}  {b['status']}  sent {b['sent']}")
        conn.close()
        return 0
    if not args.city or not args.text:
        ap.error("--city and --text are needed (or --list)")
    found = broadcast.recipients(conn, args.city)
    conn.close()
    print(f"{broadcast.city_label(args.city)}: {len(found)} people would get it.")
    draft = broadcast.create_draft(args.city, args.text, source_url=args.url)
    print(f"Draft #{draft} is on Telegram - nothing is sent until you tap ✅." if draft
          else "No draft: nobody is signed up there.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
