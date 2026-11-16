"""Operator diagnostic: what the notification pipeline currently holds.

Answers the question `rematch_all` can't - "it printed nothing new, is that because everything
is already delivered, or because nothing ever matched?". Read-only, writes nothing.

    docker compose run --rm -T api python -m scripts.queue_status
    docker compose run --rm -T api python -m scripts.queue_status 20   # more queue rows
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.database import get_connection

# Same predicate backfill_target_against_active_outages uses to pick candidates, so the count
# below is literally "how many outages a re-match would even look at".
ACTIVE_OUTAGE_WHERE = """
    status = 'ACTIVE'
    AND (expires_at IS NULL OR expires_at > datetime('now'))
    AND (category IS NULL OR category != 'WORKS')
"""


def main(argv=None):
    limit = 10
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0].isdigit():
        limit = int(argv[0])

    conn = get_connection()

    print("== TARGETS ==")
    rows = conn.execute('''
        SELECT id, city, ulica, broj, email, telegram_chat_id,
               is_active, email_active, telegram_active
        FROM targets WHERE is_active = 1 ORDER BY id
    ''').fetchall()
    print(f"{len(rows)} active")
    for r in rows:
        chans = []
        if r['email'] and r['email_active']:
            chans.append("email")
        if r['telegram_chat_id'] and r['telegram_active']:
            chans.append("telegram")
        print(f"  #{r['id']:<4} {r['ulica']} {r['broj'] or ''}, {r['city']}"
              f"   [{', '.join(chans) or 'NO ACTIVE CHANNEL'}]")

    n_hex = conn.execute(
        "SELECT COUNT(*) c FROM target_hexes WHERE target_id IN "
        "(SELECT id FROM targets WHERE is_active = 1)").fetchone()['c']
    unmapped = [r['id'] for r in rows if not conn.execute(
        "SELECT 1 FROM target_hexes WHERE target_id = ? LIMIT 1", (r['id'],)).fetchone()]
    print(f"  hexes mapped: {n_hex}"
          + (f"   UNMAPPED TARGETS: {unmapped}" if unmapped else ""))

    print("\n== OUTAGES ==")
    total = conn.execute("SELECT COUNT(*) c FROM outages").fetchone()['c']
    active = conn.execute(
        f"SELECT COUNT(*) c FROM outages WHERE {ACTIVE_OUTAGE_WHERE}").fetchone()['c']
    print(f"{total} stored, {active} currently matchable (ACTIVE, unexpired, not WORKS)")
    for r in conn.execute(f'''
        SELECT category, COUNT(*) c FROM outages WHERE {ACTIVE_OUTAGE_WHERE}
        GROUP BY category ORDER BY c DESC'''):
        print(f"  {r['category'] or 'NULL':<10} {r['c']}")

    print("\n== MESSAGE QUEUE ==")
    breakdown = conn.execute('''
        SELECT status, channel, COUNT(*) c FROM message_queue
        GROUP BY status, channel ORDER BY status, channel''').fetchall()
    if not breakdown:
        print("EMPTY - nothing has ever been queued")
    for r in breakdown:
        print(f"  {r['status']:<8} {r['channel']:<9} {r['c']}")

    print(f"\n== LAST {limit} QUEUE ROWS ==")
    recent = conn.execute('''
        SELECT mq.id, mq.target_id, mq.channel, mq.status, mq.send_attempts,
               mq.created_at, mq.sent_at, o.outage_date, o.category, o.region
        FROM message_queue mq
        LEFT JOIN outages o ON o.id = mq.outage_id
        ORDER BY mq.id DESC LIMIT ?''', (limit,)).fetchall()
    if not recent:
        print("(none)")
    for r in recent:
        when = r['sent_at'] or r['created_at']
        print(f"  #{r['id']:<5} target #{r['target_id']:<4} {r['channel']:<9} "
              f"{r['status']:<8} att={r['send_attempts']}  "
              f"{r['category'] or '?'}/{r['region'] or '?'} {r['outage_date'] or '?'}  {when}")

    conn.close()


if __name__ == "__main__":
    main()
