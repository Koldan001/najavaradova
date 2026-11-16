"""When a queued notification may go out. Shared by workers/email_worker.py and
workers/telegram_worker.py, so both apply the same rules:

- A short settle pause before a fresh row is sent, so every row one notice produces (BVK's
  flushing notice splits into one outage per night) reaches the worker together and the person
  gets one message, not five.
- Telegram's quiet hours: a planned notice for later in the week that arrives at night waits
  until the morning; anything starting before then still goes out at once.
- No message about an outage that is already over - a row held back by the email provider's
  daily limit, or by a long retry, is dropped rather than sent after the fact.

Times here are Serbian local time, naive, the same way outages.expires_at is stored. The
containers run on UTC; SQLite's datetime('now') is UTC too, so anything written into
message_queue.scheduled_for is converted back to UTC first.
"""
import re
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

SERBIA = ZoneInfo("Europe/Belgrade")

# A fresh row waits this long before it can be claimed. One notice's rows are written within
# seconds of each other; 90 s covers a multi-night notice being parsed night by night.
SETTLE_SECONDS = 90

QUIET_START = time(23, 0)
QUIET_END = time(7, 0)
# A notice is held through the night only if what it announces starts at least this long after
# the quiet hours end - the person must still see it before it begins.
QUIET_MARGIN = timedelta(hours=1)

# core/matchmaker._EXPIRY_GRACE: outages.expires_at runs this long past the announced end, so
# the map keeps a row while crews finish. "Over" for sending purposes is without it.
EXPIRY_GRACE = timedelta(hours=3)

_DATE_FORMATS = ("%d.%m.%Y", "%Y-%m-%d", "%d.%m.%Y.", "%d/%m/%Y")
_TIME = re.compile(r"(\d{1,2})[:.](\d{2})")


def serbia_now() -> datetime:
    return datetime.now(SERBIA).replace(tzinfo=None)


def to_utc_sql(local: datetime) -> str:
    """A naive Serbian local time as the UTC 'YYYY-MM-DD HH:MM:SS' SQLite compares against."""
    aware = local.replace(tzinfo=SERBIA).astimezone(timezone.utc)
    return aware.strftime("%Y-%m-%d %H:%M:%S")


def parse_day(value) -> datetime | None:
    v = (value or "").strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(v, fmt)
        except ValueError:
            continue
    return None


def outage_start(outage_date, time_window) -> tuple[datetime | None, bool]:
    """(when the outage starts, whether the notice gave a time of day). The day alone when it
    gave none; (None, False) when the date doesn't parse."""
    day = parse_day(outage_date)
    if day is None:
        return None, False
    m = _TIME.search(time_window or "")
    if not m:
        return day, False
    return day.replace(hour=min(int(m[1]), 23), minute=min(int(m[2]), 59)), True


def quiet_until(now: datetime) -> datetime | None:
    """The end of the quiet hours now is inside of, or None outside them."""
    t = now.time()
    if t >= QUIET_START:
        return datetime.combine(now.date() + timedelta(days=1), QUIET_END)
    if t < QUIET_END:
        return datetime.combine(now.date(), QUIET_END)
    return None


def hold_until(outage_date, time_window, now: datetime) -> datetime | None:
    """When a Telegram notice arriving `now` should go out instead: the morning, if now is in the
    quiet hours and the outage starts well after them. None = send now - including whenever the
    start is unknown, or is today and so maybe already under way (a fault)."""
    until = quiet_until(now)
    if until is None:
        return None
    start, has_time = outage_start(outage_date, time_window)
    if start is None:
        return None
    if has_time:
        return until if start >= until + QUIET_MARGIN else None
    # A whole-day notice: holding is fine for a later day, never for today's.
    return until if start.date() >= until.date() and start.date() > now.date() else None


def is_over(expires_at, now: datetime) -> bool:
    """The outage's announced window is behind us (expires_at less the map's grace)."""
    if not expires_at:
        return False
    try:
        return datetime.fromisoformat(str(expires_at)) - EXPIRY_GRACE < now
    except ValueError:
        return False


# ---- queue side: both workers call these before claiming ------------------------------------

def ready_sql() -> str:
    """WHERE fragment (message_queue aliased `mq`): the row's quiet-hours hold, if any, has run
    out, and a fresh row has settled. A retry (send_attempts > 0) doesn't wait again. Built per
    call so SETTLE_SECONDS can be changed at runtime (tests set it to 0)."""
    return (f"(mq.scheduled_for IS NULL OR mq.scheduled_for <= datetime('now')) AND "
            f"(mq.send_attempts > 0 OR mq.created_at IS NULL "
            f"OR mq.created_at <= datetime('now', '-{int(SETTLE_SECONDS)} seconds'))")


def skip_ended(conn, channel: str, now: datetime | None = None) -> int:
    """Queued rows whose outage is already over become SKIPPED instead of going out late - a
    "no water today" email the day after helps nobody. Returns how many."""
    now = now or serbia_now()
    rows = conn.execute(
        "SELECT mq.id, o.expires_at FROM message_queue mq JOIN outages o ON o.id = mq.outage_id "
        "WHERE mq.status = 'QUEUED' AND mq.channel = ? AND o.expires_at IS NOT NULL", (channel,),
    ).fetchall()
    late = [r["id"] for r in rows if is_over(r["expires_at"], now)]
    if late:
        conn.executemany("UPDATE message_queue SET status = 'SKIPPED' WHERE id = ?", [(i,) for i in late])
        conn.commit()
    return len(late)


def hold_for_quiet_hours(conn, channel: str, now: datetime | None = None) -> int:
    """In the quiet hours, give each newly queued row whose outage starts well after them a
    scheduled_for of the morning (see hold_until). Returns how many were held."""
    now = now or serbia_now()
    if quiet_until(now) is None:
        return 0
    rows = conn.execute(
        "SELECT mq.id, o.outage_date, o.time_window FROM message_queue mq "
        "JOIN outages o ON o.id = mq.outage_id "
        "WHERE mq.status = 'QUEUED' AND mq.channel = ? AND mq.scheduled_for IS NULL", (channel,),
    ).fetchall()
    held = []
    for r in rows:
        until = hold_until(r["outage_date"], r["time_window"], now)
        if until:
            held.append((to_utc_sql(until), r["id"]))
    if held:
        conn.executemany("UPDATE message_queue SET scheduled_for = ? WHERE id = ?", held)
        conn.commit()
    return len(held)
