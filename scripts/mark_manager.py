"""Mark (or unmark) a person as a building manager by hand.

A manager gets the tenant-notice layout (core/notice.py): Telegram's "Kopiraj za stanare" and the
"Tekst za stanare" email. Since 05.11.2026 a manager is someone with targets.manager_list (set
automatically for /upravnici and scripts/bulk_register.py signups) or with 10+ active addresses
(core/notice.MANAGER_MIN_ADDRESSES). This script is for a manager with fewer who came in some
other way. Unmarking someone with 10+ addresses doesn't make them regular.

Prints no emails or Telegram chat ids: a person is shown, and picked, by one of their address ids.

Usage (on the VM, inside the app container):
    python -m scripts.mark_manager --list               # everyone with 2+ active addresses
    python -m scripts.mark_manager --target-id 123      # mark the owner of address 123
    python -m scripts.mark_manager --target-id 123 --unmark
"""
import argparse
import os
import sys

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from core.database import get_connection
from core.notice import MANAGER_MIN_ADDRESSES

# Same owner rule as core/notice.manager_sql: one account, or for rows without an account
# the same Telegram chat / email.
_SAME_OWNER = """
    t2.is_active = 1
    AND (t2.id = t.id
         OR t2.subscriber_id = t.subscriber_id
         OR t2.telegram_chat_id = t.telegram_chat_id
         OR LOWER(t2.email) = LOWER(t.email))
"""


def owner_target_ids(conn, target_id: int) -> list[int]:
    return [r[0] for r in conn.execute(
        f"SELECT t2.id FROM targets t JOIN targets t2 ON {_SAME_OWNER} WHERE t.id = ?",
        (target_id,))]


def list_people(conn) -> list[tuple]:
    """(first address id, active addresses, manager?, cities) per person with 2+ addresses."""
    seen, people = set(), []
    for (tid,) in conn.execute("SELECT id FROM targets WHERE is_active = 1 ORDER BY id"):
        if tid in seen:
            continue
        ids = owner_target_ids(conn, tid)
        seen.update(ids)
        if len(ids) < 2:
            continue
        q = ",".join("?" * len(ids))
        manager, cities = conn.execute(
            f"SELECT MAX(manager_list), GROUP_CONCAT(DISTINCT city) FROM targets WHERE id IN ({q})",
            ids).fetchone()
        people.append((min(ids), len(ids), bool(manager) or len(ids) >= MANAGER_MIN_ADDRESSES,
                       cities or ""))
    return sorted(people, key=lambda p: -p[1])


def set_manager(conn, target_id: int, on: bool) -> int:
    ids = owner_target_ids(conn, target_id)
    if ids:
        q = ",".join("?" * len(ids))
        conn.execute(f"UPDATE targets SET manager_list = ? WHERE id IN ({q})", [int(on), *ids])
        conn.commit()
    return len(ids)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--target-id", type=int)
    ap.add_argument("--unmark", action="store_true")
    args = ap.parse_args()
    conn = get_connection()
    try:
        if args.list:
            for first, n, manager, cities in list_people(conn):
                print(f"address #{first}: {n} addresses, {'MANAGER' if manager else 'regular'}, {cities}")
        elif args.target_id:
            n = set_manager(conn, args.target_id, not args.unmark)
            word = "regular" if args.unmark else "MANAGER"
            print(f"{n} active addresses of the owner of #{args.target_id} set to {word}." if n
                  else f"No active address #{args.target_id}.")
        else:
            ap.print_help()
    finally:
        conn.close()


if __name__ == "__main__":
    main()
