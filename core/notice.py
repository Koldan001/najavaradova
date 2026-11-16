"""The shareable copy of a notification - a Telegram copy button for everyone, and for managers
the email itself is laid out as the paste-ready tenant notice.

Two audiences:
- A building manager - signed up through /upravnici or bulk_register (targets.manager_list), or
  has MANAGER_MIN_ADDRESSES or more active addresses - informs his tenants himself. He doesn't
  forward our alert: tenant groups live on Viber, the alert arrives on Telegram/email, and a
  forward would carry our /odjava + dashboard footer anyway - so he retyped it by hand. He gets
  the finished tenant notice, signed quietly.
- A regular user - a home, a parents' flat, a family's few flats - gets a Telegram copy to pass
  to neighbours or family, signed with where it came from - the cheapest way the service travels. His email
  stays a plain alert: a second copy of the same text there is just noise.
One builder so Telegram and email produce identical text.
"""
from dataclasses import dataclass

# Starts the follow-up an already-notified person gets when a notice's hours change
# (core/matchmaker._queue_time_change); the email subject reads "Izmena: ..." for it.
CHANGED_PREFIX = "🔁 Izmena najave, novo vreme: "

# Nobody looks after 10+ flats for family: that many is a manager even without the /upravnici
# signup (both early Novi Sad managers typed theirs in by hand). Fewer (was 3 until 05.11.2026)
# is a regular user unless they came through a manager's list.
MANAGER_MIN_ADDRESSES = 10

MANAGER_SIGNATURE = "— najavaradova.rs"
PERSONAL_SIGNATURE = "Obaveštenje preko najavaradova.rs"

# Telegram's CopyTextButton accepts 1-256 characters (Bot API 7.11). A longer notice is sent
# without the button - the message itself stays copyable by long-press.
COPY_TEXT_MAX = 256


@dataclass(frozen=True)
class ShareNotice:
    button: str    # Telegram button label
    text: str      # what gets copied / pasted


def share_notice(body: str, manager: bool) -> ShareNotice:
    body = body.strip()
    if manager:
        return ShareNotice("📋 Kopiraj za stanare", f"{body}\n\n{MANAGER_SIGNATURE}")
    return ShareNotice("📤 Pošalji komšijama", f"{body}\n\n{PERSONAL_SIGNATURE}")


def manager_sql(identity_column: str) -> str:
    """SELECT-list expression: 1 when the owner of target `t` is a building manager - any active
    address of theirs carries targets.manager_list, or they have MANAGER_MIN_ADDRESSES or more
    active addresses. (3+ used to be enough, and families with a few flats got the tenant layout.)
    Owner = same subscriber account, or - for rows that predate accounts (bulk_register.py's
    Telegram path leaves subscriber_id NULL) - the same Telegram chat / email. A NULL on either
    side never compares equal, so unowned rows only count themselves. `identity_column` is a
    fixed column name from the calling worker, never user input."""
    return f'''(
        SELECT MAX(t2.manager_list) = 1 OR COUNT(*) >= {MANAGER_MIN_ADDRESSES}
        FROM targets t2
        WHERE t2.is_active = 1
          AND (t2.id = t.id
               OR t2.subscriber_id = t.subscriber_id
               OR t2.{identity_column} = t.{identity_column})
    ) AS is_manager'''


def one_per_person(jobs, recipient_column: str) -> list[list[list]]:
    """Claimed queue rows grouped so each person gets ONE message per run: [person][outage][row].
    A queue row is per address and per outage, so someone whose two addresses were both in an
    outage got the same email twice (29.09.2026, outage #3394, two Novi Sad addresses), a manager
    with ten buildings in one outage would have got ten, and one notice split into several
    outages (BVK's night-by-night flushing) sent one email per night at once. Rows arriving
    together (core/delivery.SETTLE_SECONDS) now go out as one message, one section per outage.
    Order is kept: people, outages and rows in first-seen order. `recipient_column` is a fixed
    column name from the calling worker."""
    people: dict[str, dict] = {}
    for job in jobs:
        who = str(job[recipient_column] or "").strip().lower() or f"row-{job['queue_id']}"
        people.setdefault(who, {}).setdefault(job["outage_id"], []).append(job)
    return [list(outages.values()) for outages in people.values()]


def distinct_bodies(bodies) -> list[str]:
    """The message texts of one person's rows, each once: two addresses on the same street read
    the same, and the person needs to see it one time."""
    out = []
    for body in bodies:
        body = (body or "").strip()
        if body and body not in out:
            out.append(body)
    return out
