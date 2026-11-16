import sqlite3
import sys
import os
import time
import requests

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import config
from core import broadcast, delivery, feedback
from core.database import get_connection
from core.notice import COPY_TEXT_MAX, distinct_bodies, manager_sql, one_per_person, share_notice

TELEGRAM_API = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage"

# A transient send error (timeout, 429, 5xx, network) doesn't fail the message - it's retried up
# to this many times with a quadratic backoff (send_attempts^2 * 60s: ~0s, 60s, 240s) before the
# row is finally marked FAILED and counted for the §1 watchdog alert. A permanent error (bot
# blocked, chat gone - Telegram 403/400) skips the retries and goes straight to FAILED.
MAX_ATTEMPTS = 3


def reclaim_stale_sending():
    """A worker that died between claiming a row and recording its outcome leaves that row stuck
    in 'SENDING'. Anything still 'SENDING' well past the longest a run can take was orphaned like
    that - put it back to 'QUEUED' so the next run retries it. The 5-minute floor is far longer
    than a real run (seconds) yet short enough that the watchdog's stuck-queue alert still trips
    on a genuinely wedged pipeline."""
    conn = get_connection()
    conn.execute(
        "UPDATE message_queue SET status = 'QUEUED' "
        "WHERE status = 'SENDING' AND channel = 'telegram' "
        "AND (last_attempt_at IS NULL OR last_attempt_at <= datetime('now', '-5 minutes'))"
    )
    conn.commit()
    conn.close()


def claim_queued_messages():
    """Atomically move this worker's eligible rows 'QUEUED' -> 'SENDING' and return them joined
    to their target + outage. The status flip and the row read are one statement, so a second
    worker running at the same time (a deploy recreating the `sender` container mid-run, an
    accidental double `docker compose up`) finds those rows already gone and CANNOT send the
    same notification twice - the bug this replaces. Same join/filter as email_worker.py's
    twin. Rows still inside their retry backoff window are left for a later run."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(f'''
        UPDATE message_queue SET status = 'SENDING', last_attempt_at = CURRENT_TIMESTAMP
        WHERE id IN (
            SELECT mq.id
            FROM message_queue mq
            JOIN targets t ON mq.target_id = t.id
            WHERE mq.status = 'QUEUED' AND mq.channel = 'telegram'
              AND t.telegram_chat_id IS NOT NULL AND t.is_active = 1 AND t.telegram_active = 1
              AND (
                mq.last_attempt_at IS NULL
                OR mq.last_attempt_at <= datetime('now', '-' || (mq.send_attempts * mq.send_attempts * 60) || ' seconds')
              )
              AND {delivery.ready_sql()}
        )
        RETURNING id
    ''')
    claimed = [r['id'] for r in cursor.fetchall()]
    conn.commit()
    if not claimed:
        conn.close()
        return []
    placeholders = ','.join('?' * len(claimed))
    cursor.execute(f'''
        SELECT
            mq.id as queue_id,
            mq.outage_id,
            mq.send_attempts,
            t.telegram_chat_id,
            mq.custom_message,
            o.raw_text,
            o.time_window,
            o.source_url,
            {manager_sql('telegram_chat_id')}
        FROM message_queue mq
        JOIN targets t ON mq.target_id = t.id
        JOIN outages o ON mq.outage_id = o.id
        WHERE mq.id IN ({placeholders})
    ''', claimed)
    jobs = cursor.fetchall()
    conn.close()
    return jobs


def mark_sent(queue_ids):
    """Record rows as SENT once Telegram accepted the message. Its own step, outside the send's
    error handling: a database error here used to escape the run with the rows still SENDING,
    and the stale-row reclaim sent the same message again 5 minutes later. Retried a few times
    (each try already waits out a 30 s busy lock). Mirror of email_worker.mark_sent."""
    for attempt in range(3):
        try:
            conn = get_connection()
            try:
                conn.executemany(
                    "UPDATE message_queue SET status = 'SENT', sent_at = CURRENT_TIMESTAMP WHERE id = ?",
                    [(i,) for i in queue_ids],
                )
                conn.commit()
            finally:
                conn.close()
            return
        except sqlite3.Error as e:
            print(f"⚠️ Could not mark queue IDs {list(queue_ids)} sent (try {attempt + 1}): {e}")
            time.sleep(2)
    print(f"❌ Queue IDs {list(queue_ids)} were delivered but are not marked SENT.")


def record_failure(queue_id, permanent):
    """Permanent -> FAILED now. Transient -> bump send_attempts + stamp last_attempt_at, and
    only flip to FAILED once this attempt was the MAX_ATTEMPTS-th."""
    conn = get_connection()
    if permanent:
        conn.execute("UPDATE message_queue SET status = 'FAILED' WHERE id = ?", (queue_id,))
    else:
        conn.execute(
            '''UPDATE message_queue
               SET send_attempts = send_attempts + 1,
                   last_attempt_at = CURRENT_TIMESTAMP,
                   status = CASE WHEN send_attempts + 1 >= ? THEN 'FAILED' ELSE 'QUEUED' END
               WHERE id = ?''',
            (MAX_ATTEMPTS, queue_id),
        )
    conn.commit()
    conn.close()


def run_worker():
    if not config.TELEGRAM_BOT_TOKEN:
        print("❌ TELEGRAM_BOT_TOKEN is not set - see .env.example. Aborting.")
        return

    reclaim_stale_sending()
    conn = get_connection()
    try:
        late = delivery.skip_ended(conn, "telegram")
        held = delivery.hold_for_quiet_hours(conn, "telegram")
    finally:
        conn.close()
    if late:
        print(f"⏭️ Skipped {late} queued Telegram message(s) about outages that are already over.")
    if held:
        print(f"🌙 Holding {held} Telegram message(s) until the morning (quiet hours).")
    try:
        city_wide = broadcast.send_telegram()
        if city_wide:
            print(f"📣 Sent {city_wide} approved city-wide notice(s) (core/broadcast.py).")
    except Exception as e:
        print(f"⚠️ City-wide notices failed this run: {e}")
    jobs = claim_queued_messages()

    if not jobs:
        print("📭 No pending Telegram messages in the queue. Sleeping...")
        return

    print(f"🚀 Found {len(jobs)} queued Telegram messages.")

    sent, failed = 0, 0
    # One message per chat for everything that arrived together (core/notice.one_per_person):
    # each address's text once, each outage followed by its official post. Split only when it
    # would pass Telegram's message length.
    for person in one_per_person(jobs, "telegram_chat_id"):
        for chunk in _chunks(person):
            ok = _send(chunk)
            sent += ok
            failed += not ok

    print(f"\n🎉 Done. Sent: {sent}, Failed/deferred: {failed}.")


# Telegram rejects a message over 4096 characters; the footer and emoji need some room.
TEXT_MAX = 3800


def _section(outage) -> tuple[str, str | None]:
    """(the outage's texts, each address's once, its official post) for one outage's rows."""
    body = "\n\n".join(distinct_bodies(
        j['custom_message'] or f"{j['raw_text']}\nTrajanje: {j['time_window']}" for j in outage))
    return body, outage[0]['source_url']


def _chunks(person) -> list[list]:
    """The person's outages packed into as few messages as fit TEXT_MAX."""
    chunks, size = [[]], 0
    for outage in person:
        body, url = _section(outage)
        length = len(body) + len(url or "") + 30
        if chunks[-1] and size + length > TEXT_MAX:
            chunks.append([])
            size = 0
        chunks[-1].append(outage)
        size += length
    return chunks


def _send(chunk) -> bool:
    """One Telegram message for a chunk of one person's outages; records every row's outcome."""
    group = [j for outage in chunk for j in outage]
    queue_ids = [j['queue_id'] for j in group]
    queue_id = ", ".join(map(str, queue_ids))
    chat_id = group[0]['telegram_chat_id']
    sections = [_section(outage) for outage in chunk]
    body = "\n\n".join(b for b, _ in sections)

    footer = []
    if len(sections) == 1:
        text_body = body
        if sections[0][1]:
            footer.append(f"🔗 Zvanična objava: {sections[0][1]}")
    else:
        # Several outages: each one's link right under its own text.
        text_body = "\n\n".join(b + (f"\n🔗 Zvanična objava: {u}" if u else "") for b, u in sections)
    # The body is generated from the announcement, not quoted from it - say so, and point at
    # the source as the authority. Same wording as the email footer and the site footer.
    footer.append("ℹ️ Obaveštenje je pripremljeno automatski; merodavna je zvanična objava.")
    # /odjava (alias /stop) is handled in workers/telegram_bot.py's handle_stop - kept working
    # as the fast in-chat path (already safely scoped to this chat_id, no reason to remove
    # it). The dashboard is the fuller option (see multiple addresses, toggle one at a time,
    # active-until once billing exists) - mentioned alongside it, not instead of it.
    footer.append("Za odjavu pošaljite /odjava, ili upravljajte svim adresama na "
                  "app.najavaradova.rs/dashboard")

    text_message = f"⚠️ OBAVEŠTENJE:\n{text_body}\n\n" + "\n".join(footer)
    payload = {"chat_id": chat_id, "text": text_message}

    # One-tap copy of a shareable notice - tenant version for managers, neighbour version for
    # everyone else (see core/notice.py). Over Telegram's 256-char copy_text limit the button
    # is left off rather than sending a cut-off notice.
    rows = []
    share = share_notice(body, any(j['is_manager'] for j in group))
    if len(share.text) <= COPY_TEXT_MAX:
        rows.append([{"text": share.button, "copy_text": {"text": share.text}}])
    else:
        print(f"ℹ️ Queue ID {queue_id}: share notice is {len(share.text)} chars, sent without copy button.")
    # 👍 / 👎 - was this right for your address? (core/feedback.py). Only on a message about one
    # outage: with several, one answer couldn't say which of them was wrong.
    if config.FEEDBACK_BUTTONS and len(chunk) == 1:
        rows.append(feedback.buttons(chunk[0][0]['outage_id']))
    if rows:
        payload["reply_markup"] = {"inline_keyboard": rows}

    try:
        res = requests.post(TELEGRAM_API, json=payload, timeout=10)
        delivered = res.ok and res.json().get("ok")
    except (requests.RequestException, ValueError) as e:
        # Network error / timeout / unparseable body - transient by nature, let it retry.
        for qid in queue_ids:
            record_failure(qid, permanent=False)
        print(f"⚠️ Queue IDs {queue_id} deferred for retry: {e}")
        return False
    if not delivered:
        # 403 (bot blocked by the user) and 400 (chat not found / deactivated / bad
        # payload) won't fix themselves - retrying the identical request just repeats the
        # rejection. Anything else (429 rate-limit, 5xx, gateway) is transient -> retry.
        code = res.status_code
        permanent = code in (400, 403)
        for qid in queue_ids:
            record_failure(qid, permanent)
        verdict = "failed permanently" if permanent else "deferred for retry"
        print(f"⚠️ Queue IDs {queue_id} {verdict}: {code} {res.text[:200]}")
        return False

    # Delivered: bookkeeping errors from here on must never count as a failed send.
    mark_sent(queue_ids)
    print(f"✅ Queue IDs {queue_id} sent to chat {chat_id} in one message.")
    return True


if __name__ == "__main__":
    run_worker()
