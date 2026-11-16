"""Sends queued outage notifications to targets who registered via email - the email twin of
workers/telegram_worker.py. Delivery goes through Brevo's free-tier SMTP relay (300/day,
permanent free tier, not a trial - see config.py's BREVO_* comments).

Every email carries a one-click unsubscribe link (targets.unsub_token, checked by the /unsubscribe
endpoint in scripts/local_web_server.py) - Telegram's equivalent is /stop, which needs no token
since the chat_id itself identifies the sender; email has no such built-in identity.

Usage: python -m workers.email_worker  (see docker-compose.yml's `email_sender` service for the
actual schedule - same sleep-loop pattern as `sender`)
"""
import smtplib
import sqlite3
import sys
import os
import time
from email.message import EmailMessage
from urllib.parse import urlencode

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import config
from core import broadcast, delivery
from core.database import get_connection
from core.notice import CHANGED_PREFIX, distinct_bodies, manager_sql, one_per_person, share_notice


UNSUBSCRIBE_BASE_URL = "https://app.najavaradova.rs/unsubscribe"
# 👍 / 👎 under each outage (core/feedback.py). The page only asks; the answer is written on its
# button's POST, so a mail scanner opening the link records nothing.
FEEDBACK_BASE_URL = "https://app.najavaradova.rs/tacno"
FEEDBACK_QUESTION = "Da li je obaveštenje tačno za ovu adresu?"


def feedback_links(outage_id, token) -> tuple[str, str]:
    """(yes link, no link) for one outage and one address's unsubscribe token."""
    def link(v):
        return f"{FEEDBACK_BASE_URL}?{urlencode([('o', outage_id), ('t', token), ('v', v)])}"
    return link("y"), link("n")

# Above a manager's email (see build_message): tells him the block below is ready to send as-is.
TENANT_HEADING = "Tekst za stanare - kopirajte i pošaljite u grupu zgrade:"

# Mirror workers/telegram_worker.py: a transient send error is retried up to this many times
# with a quadratic backoff (send_attempts^2 * 60s) before the row is marked FAILED and counted
# for the §1 watchdog alert. Brevo quota / rate-limit (4xx SMTP) and a dead SMTP connection are
# infrastructure-level "try again later" and are NOT counted against this cap - the row just
# stays QUEUED for the next run.
MAX_ATTEMPTS = 3

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOGO_PATH = os.path.join(REPO_ROOT, "web", "icon-512.png")
# Read once at import, not per-send - it's a fixed ~few KB asset, no reason to hit disk on every
# email. None if missing (e.g. a checkout that predates it) - the HTML template below degrades
# to no logo rather than crashing the whole worker over a missing image.
try:
    with open(LOGO_PATH, "rb") as f:
        LOGO_BYTES = f.read()
except FileNotFoundError:
    LOGO_BYTES = None


# The row-eligibility predicate, shared by has_pending_messages() (a cheap pre-check that
# avoids opening an SMTP connection for an empty queue) and claim_queued_messages() below. Built
# per call: core/delivery.ready_sql (the settle pause, a quiet-hours hold) reads its settings then.
def _eligible() -> str:
    return f'''
    mq.channel = 'email'
    AND t.email IS NOT NULL AND t.is_active = 1 AND t.email_active = 1
    AND (
        mq.last_attempt_at IS NULL
        OR mq.last_attempt_at <= datetime('now', '-' || (mq.send_attempts * mq.send_attempts * 60) || ' seconds')
    )
    AND {delivery.ready_sql()}
'''


def has_pending_messages():
    """Cheap 'is there anything to send' check - run before opening the Brevo SMTP connection so
    an idle queue doesn't reconnect every 60s. Does NOT claim; a row it sees may be claimed by a
    racing worker before this one gets to claim_queued_messages(), which is fine (that call just
    comes back empty)."""
    conn = get_connection()
    row = conn.execute(f'''
        SELECT 1 FROM message_queue mq JOIN targets t ON mq.target_id = t.id
        WHERE mq.status = 'QUEUED' AND {_eligible()} LIMIT 1
    ''').fetchone()
    conn.close()
    return row is not None


def reclaim_stale_sending():
    """A worker that died between claiming a row and recording its outcome leaves that row stuck
    in 'SENDING'. Anything still 'SENDING' well past the longest a run can take was orphaned like
    that - put it back to 'QUEUED'. Mirror of telegram_worker.reclaim_stale_sending()."""
    conn = get_connection()
    conn.execute(
        "UPDATE message_queue SET status = 'QUEUED' "
        "WHERE status = 'SENDING' AND channel = 'email' "
        "AND (last_attempt_at IS NULL OR last_attempt_at <= datetime('now', '-5 minutes'))"
    )
    conn.commit()
    conn.close()


def claim_queued_messages():
    """Atomically move eligible rows 'QUEUED' -> 'SENDING' and return them joined to target +
    outage, so two workers overlapping (a deploy recreating `email_sender` mid-run) can't each
    send the same email. Mirror of telegram_worker.claim_queued_messages(); called only after
    the SMTP connection is up, so a connect failure leaves everything 'QUEUED'."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(f'''
        UPDATE message_queue SET status = 'SENDING', last_attempt_at = CURRENT_TIMESTAMP
        WHERE id IN (
            SELECT mq.id FROM message_queue mq JOIN targets t ON mq.target_id = t.id
            WHERE mq.status = 'QUEUED' AND {_eligible()}
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
            t.email,
            t.unsub_token,
            mq.custom_message,
            o.raw_text,
            o.time_window,
            o.source_url,
            o.category,
            o.outage_date,
            o.status,
            {manager_sql('email')}
        FROM message_queue mq
        JOIN targets t ON mq.target_id = t.id
        JOIN outages o ON mq.outage_id = o.id
        WHERE mq.id IN ({placeholders})
    ''', claimed)
    jobs = cursor.fetchall()
    conn.close()
    return jobs


def mark_sent(queue_ids):
    """Record rows as SENT once their email is out. Its own step, never inside the send's error
    handling: a database error here used to count as a failed send, put the row back in the
    queue, and the same email went out again. Retried a few times (each try already waits out
    a 30 s busy lock); should all fail, the row stays SENDING and the stale-row reclaim would
    send it once more after 5 minutes - printed loudly so it can be seen."""
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
    """Permanent -> FAILED now. Transient -> bump send_attempts + stamp last_attempt_at, flipping
    to FAILED only once this attempt was the MAX_ATTEMPTS-th. Identical to telegram_worker's."""
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


def _html_escape(s: str) -> str:
    # Quotes included: source_url is scraped third-party text and goes into a double-quoted
    # href="" below, so " and ' both have to be encoded, not just the angle brackets.
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
             .replace('"', "&quot;").replace("'", "&#39;"))


# Every notification body is generated from the announcement, not quoted from it - so the
# official post is the authority, and the reader is told so and given the link. Telegram has
# carried this link since the start; email had neither the link nor the caveat.
DISCLAIMER = ("Obaveštenje je pripremljeno automatski na osnovu zvanične objave. "
              "U slučaju nejasnoća merodavna je zvanična objava komunalnog preduzeća.")


# A specific subject ("Najava isključenja struje — 09.09.2026") delivers better than a generic
# one ("Obaveštenje o prekidu", which reads as bulk) and is more useful in the inbox list.
_CATEGORY_SUBJECT = {
    "POWER": "Najava isključenja struje",
    "WATER": "Najava isključenja vode",
    "HEATING": "Najava prekida grejanja",
}


def _subject(category, outage_date):
    base = _CATEGORY_SUBJECT.get((category or "").strip().upper(), "Najava planiranih radova")
    d = (outage_date or "").strip()
    return f"{base} — {d}" if d and d.upper() != "N/A" else base


def _subject_for(outages) -> str:
    """One outage: its own subject - "Izmena: ..." for a change of hours, "Odloženo: ..." /
    "Otkazano: ..." for works called off (core/matchmaker). Several in one email: "Najava
    isključenja vode — 5 najava", or "Najave isključenja — 2 najave" across utilities."""
    if len(outages) == 1:
        o = outages[0]
        subject = _subject(o['category'], o['outage_date'])
        message = o['custom_message'] or ""
        if o['status'] == 'CANCELLATION':
            return ("Odloženo: " if "odložen" in message else "Otkazano: ") + subject
        if message.startswith(CHANGED_PREFIX):
            return "Izmena: " + subject
        return subject
    kinds = {(o['category'] or "").strip().upper() for o in outages}
    base = (_CATEGORY_SUBJECT.get(kinds.pop(), "Najave planiranih radova") if len(kinds) == 1
            else "Najave isključenja")
    n = len(outages)
    word = "najava" if n % 10 == 1 and n % 100 != 11 else (
        "najave" if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14 else "najava")
    return f"{base} — {n} {word}"


def build_message(to_email, unsub_token, body_text, source_url=None, subject=None,
                  manager=False, parts=None, opt_out_link=None, feedback=None):
    """One email for everything a person has queued in this run (see run_worker). `parts` is
    [(texts, source_url)], one per outage - each address's text once, then that outage's
    official post; without it the email is the single outage `body_text` / `source_url`.
    `body_text` may be a list of texts and `unsub_token` a list of tokens: the one unsubscribe
    link turns off every address the email is about. `opt_out_link` (a city-wide notice,
    core/broadcast.py) adds a second link that turns off only those notices. `feedback` is
    [(outage_id, unsub_token) or None], one per part: the 👍 / 👎 links under that outage."""
    if parts is None:
        parts = [([body_text] if isinstance(body_text, str) else body_text, source_url)]
    parts = [(distinct_bodies(texts), url) for texts, url in parts]
    tokens = [unsub_token] if isinstance(unsub_token, str) else list(dict.fromkeys(unsub_token))
    msg = EmailMessage()
    msg["Subject"] = subject or "Obaveštenje o prekidu"
    msg["From"] = f"{config.EMAIL_FROM_NAME} <{config.EMAIL_FROM_ADDRESS}>"
    msg["To"] = to_email
    unsub_link = f"{UNSUBSCRIBE_BASE_URL}?{urlencode([('token', t) for t in tokens])}"

    # RFC 8058 one-click unsubscribe. Gmail / Yahoo bulk-sender rules expect both headers: the
    # client shows a native "Unsubscribe" control and, when clicked, POSTs to the URL with the
    # body below - never a GET. scripts/local_web_server.py's /unsubscribe does the is_active=0
    # write only on that POST, so a mail scanner pre-fetching the link (a GET) can't unsubscribe
    # anyone. The visible "klikni ovde" link in the body is the same URL - a human clicking it
    # lands on a confirm page, not an instant unsubscribe.
    msg["List-Unsubscribe"] = f"<{unsub_link}>"
    msg["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"

    # Plain-text part first (set_content) - the fallback every client that can't/won't render
    # HTML falls back to, and part of what keeps spam filters happy (an HTML-only email is a
    # real deliverability red flag). add_alternative then adds the HTML version on top of it.
    # A manager's email IS the tenant notice - shown once, signed, in a box he copies into the
    # tenants' group (the email twin of Telegram's copy button, see core/notice.py). Shown once,
    # not alert-plus-copy, because these readers are mostly older people: one block, one job.
    # Everyone else gets the plain alert.
    # A manager gets one ready-to-paste notice per building; everyone else the texts one after
    # the other. Each outage is followed by its own official post.
    sections = [([share_notice(b, True).text for b in texts] if manager else texts, url)
                for texts, url in parts]
    asks = [feedback_links(*f) if f else None for f in (feedback or [])]
    asks += [None] * (len(sections) - len(asks))
    plain_sections = "".join(
        "\n\n".join(notices) + "\n\n" + (f"Zvanična objava: {url}\n\n" if url else "")
        + (f"{FEEDBACK_QUESTION}\nDa: {ask[0]}\nNe: {ask[1]}\n\n" if ask else "")
        for (notices, url), ask in zip(sections, asks))
    msg.set_content(
        (f"{TENANT_HEADING}\n\n" if manager else "")
        + plain_sections
        + f"---\n"
        f"{DISCLAIMER}\n\n"
        + (f"Ne želite obaveštenja za ceo grad: {opt_out_link}\n" if opt_out_link else "")
        + f"Za odjavu sa obaveštenja, klikni: {unsub_link}"
    )

    logo_html = (
        '<img src="cid:logo" width="40" height="40" alt="NajavaRadova.rs" '
        'style="display:block;border-radius:9px;">'
        if LOGO_BYTES else ""
    )
    # The announcement each part was built from, so the reader can check us. Escaped: the URL is
    # scraped from a third-party page and goes into an href.
    def source_html(url):
        return (f'<p style="margin:14px 0 0;font-size:14px;">'
                f'<a href="{_html_escape(url)}" style="color:#7FA9F5;text-decoration:none;">'
                f'Zvanična objava &rarr;</a></p>' if url else "")

    def ask_html(ask):
        if not ask:
            return ""
        a = 'style="color:#A5A1AB;text-decoration:none;"'
        return (f'<p style="margin:10px 0 0;font-size:13px;color:#8C8892;">{FEEDBACK_QUESTION} '
                f'<a href="{_html_escape(ask[0])}" {a}>👍 Da</a> &middot; '
                f'<a href="{_html_escape(ask[1])}" {a}>👎 Ne</a></p>')

    def notices_html(notices):
        if manager:
            return "".join(
                f'<p style="margin:0 0 10px;padding:14px 16px;border:1px dashed #4A4A55;border-radius:10px;'
                f'color:#F2F0EE;font-size:16px;line-height:1.55;white-space:pre-line;">'
                f'{_html_escape(n)}</p>' for n in notices)
        text = "\n\n".join(notices)
        return (f'<p style="color:#F2F0EE;font-size:15px;line-height:1.55;margin:0;'
                f'white-space:pre-line;">{_html_escape(text)}</p>')

    body_html = (
        (f'<p style="margin:22px 0 8px;font-size:14px;color:#A5A1AB;">{TENANT_HEADING}</p>' if manager else "")
        + "".join(f'<div style="margin:22px 0 0;">{notices_html(n)}{source_html(url)}{ask_html(ask)}</div>'
                  for (n, url), ask in zip(sections, asks))
    )
    opt_out_html = (
        f'<p style="margin:0 0 6px;font-size:12.5px;color:#8C8892;">Ne želite obaveštenja za ceo grad? '
        f'<a href="{_html_escape(opt_out_link)}" style="color:#A5A1AB;">Isključi ih</a>.</p>'
        if opt_out_link else "")
    html = f"""\
<!doctype html><html lang="sr"><body style="margin:0;padding:32px 16px;background:#101013;
font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;">
<table role="presentation" width="100%" style="max-width:480px;margin:0 auto;border-collapse:collapse;">
<tr><td style="background:#1B1B20;border:1px solid #2B2B31;border-radius:16px;padding:28px 26px;">
  <table role="presentation"><tr>
    <td style="padding-right:10px;">{logo_html}</td>
    <td style="color:#F2F0EE;font-size:16px;font-weight:700;letter-spacing:-.02em;">
      NajavaRadova<span style="color:#8C8892;font-weight:400;">.rs</span>
    </td>
  </tr></table>
  {body_html}
  <hr style="border:none;border-top:1px solid #2B2B31;margin:24px 0 16px;">
  <p style="margin:0 0 10px;font-size:12px;color:#77737C;line-height:1.5;">{DISCLAIMER}</p>
  {opt_out_html}<p style="margin:0;font-size:12.5px;color:#8C8892;">
    Za odjavu sa obaveštenja, <a href="{_html_escape(unsub_link)}" style="color:#A5A1AB;">klikni ovde</a>.
  </p>
</td></tr>
</table>
</body></html>"""
    msg.add_alternative(html, subtype="html")

    if LOGO_BYTES:
        # The HTML alternative just added is the last payload part - attach the logo TO it
        # (not to the message root) so it's related/embedded rather than a separate visible
        # attachment, and referenced via cid: in the <img> above instead of an external URL -
        # embedded means it renders even in clients that block loading remote images by default.
        msg.get_payload()[-1].add_related(LOGO_BYTES, maintype="image", subtype="png", cid="logo")

    return msg


def run_worker():
    if not config.BREVO_SMTP_LOGIN or not config.BREVO_SMTP_KEY or not config.EMAIL_FROM_ADDRESS:
        print("❌ BREVO_SMTP_LOGIN / BREVO_SMTP_KEY / EMAIL_FROM_ADDRESS not set - see .env.example. Aborting.")
        return

    reclaim_stale_sending()
    conn = get_connection()
    try:
        late = delivery.skip_ended(conn, "email")
    finally:
        conn.close()
    if late:
        print(f"⏭️ Skipped {late} queued email(s) about outages that are already over.")

    try:
        send_broadcast_emails()
    except Exception as e:
        print(f"⚠️ City-wide notices failed this run: {e}")

    if not has_pending_messages():
        print("📭 No pending email messages in the queue. Sleeping...")
        return

    sent, failed, deferred = 0, 0, 0
    try:
        smtp = smtplib.SMTP(config.BREVO_SMTP_HOST, config.BREVO_SMTP_PORT, timeout=15)
        smtp.starttls()
        smtp.login(config.BREVO_SMTP_LOGIN, config.BREVO_SMTP_KEY)
    except Exception as e:
        # Can't even connect/authenticate - nothing has been claimed yet, so every row stays
        # QUEUED (not FAILED) and the next run retries once whatever's wrong (network, bad creds,
        # Brevo down) clears up.
        print(f"❌ Could not connect to Brevo SMTP: {e} - leaving the queue untouched for retry.")
        return

    # Claim only now that SMTP is up: a connect failure above must not strand rows in 'SENDING'.
    jobs = claim_queued_messages()
    if not jobs:
        # A racing worker drained the queue between has_pending_messages() and here.
        try:
            smtp.quit()
        except Exception:
            pass
        print("📭 Queue already drained by another run. Sleeping...")
        return

    print(f"🚀 Found {len(jobs)} queued email messages.")

    # One email per person per outage: every row for the same address and outage goes out
    # together, each address's text once (core/notice.one_per_person).
    for person in one_per_person(jobs, "email"):
        group = [j for outage in person for j in outage]
        job = group[0]
        queue_ids = [j['queue_id'] for j in group]
        ids = ", ".join(map(str, queue_ids))
        to_email = job['email']

        parts = [([j['custom_message'] or f"{j['raw_text']}\nTrajanje: {j['time_window']}" for j in outage],
                  outage[0]['source_url']) for outage in person]
        subject = _subject_for([outage[0] for outage in person])
        msg = build_message(to_email, [j['unsub_token'] for j in group], None, subject=subject,
                            manager=any(j['is_manager'] for j in group), parts=parts,
                            feedback=[(outage[0]['outage_id'], outage[0]['unsub_token']) for outage in person]
                            if config.FEEDBACK_BUTTONS else None)

        try:
            smtp.send_message(msg)
        except smtplib.SMTPResponseException as e:
            code = e.smtp_code or 0
            if code < 500:
                # 4xx = Brevo daily-quota-exceeded / rate-limit: "try again later", not the
                # message's fault. Leave QUEUED untouched (no attempt counted) so it retries
                # freely once the quota window resets.
                deferred += 1
                print(f"⏸️ Queue IDs {ids} deferred (code {code}): {e.smtp_error} - left queued, not counted.")
            else:
                # 5xx = a hard SMTP rejection. Count it toward the retry cap.
                for queue_id in queue_ids:
                    record_failure(queue_id, permanent=False)
                failed += 1
                print(f"⚠️ Queue IDs {ids} deferred for retry (code {code}): {e.smtp_error}")
            continue
        except smtplib.SMTPRecipientsRefused:
            # The one case that IS the address's fault, not a quota/rate issue - Brevo rejected
            # the recipient outright (invalid/nonexistent address). Retrying won't help.
            for queue_id in queue_ids:
                record_failure(queue_id, permanent=True)
            failed += 1
            print(f"⚠️ Queue IDs {ids} failed permanently: recipient refused ({to_email}).")
            continue
        except Exception as e:
            # Unknown send-time error - transient by assumption, retried up to the cap.
            for queue_id in queue_ids:
                record_failure(queue_id, permanent=False)
            failed += 1
            print(f"⚠️ Queue IDs {ids} deferred for retry: {e}")
            continue

        # Delivered: bookkeeping errors from here on must never count as a failed send.
        mark_sent(queue_ids)
        sent += 1
        print(f"✅ Queue IDs {ids} sent to {to_email} in one email.")

    try:
        smtp.quit()
    except Exception:
        pass

    print(f"\n🎉 Done. Sent: {sent}, Failed: {failed}, Deferred (retry next run): {deferred}.")


def send_broadcast_emails() -> int:
    """Approved city-wide notices (core/broadcast.py), one email per person. Its own SMTP
    connection, opened only when one is due. Returns how many went out."""
    rows = broadcast.claim("email")
    if not rows:
        return 0
    try:
        smtp = smtplib.SMTP(config.BREVO_SMTP_HOST, config.BREVO_SMTP_PORT, timeout=15)
        smtp.starttls()
        smtp.login(config.BREVO_SMTP_LOGIN, config.BREVO_SMTP_KEY)
    except Exception as e:
        # Left in SENDING: broadcast.claim takes them again after 5 minutes.
        print(f"❌ Could not connect to Brevo SMTP for city-wide notices: {e}")
        return 0
    sent = 0
    for r in rows:
        opt_out = f"{broadcast.OPT_OUT_BASE_URL}?{urlencode([('token', r['unsub_token'])])}"
        msg = build_message(r["email"], r["unsub_token"], r["body"], source_url=r["source_url"],
                            subject=f"Važno obaveštenje — {broadcast.city_label(r['city'])}",
                            opt_out_link=opt_out)
        try:
            smtp.send_message(msg)
            ok = True
        except Exception as e:
            ok = False
            print(f"⚠️ City-wide notice #{r['broadcast_id']} to target {r['target_id']} failed: {e}")
        broadcast.finish(r["send_id"], ok)
        sent += ok
    try:
        smtp.quit()
    except Exception:
        pass
    print(f"📣 Sent {sent} city-wide notice email(s).")
    return sent


if __name__ == "__main__":
    run_worker()
