"""Account identity + magic-link login. No passwords anywhere - see PLAN.md's subscriber design
and the "Multi-address, identity, and billing" section this implements.

A subscriber is identified only by an email address. There is nothing to hash or leak beyond
that email: every login is a fresh, single-use link. Both the login token (in the emailed link)
and the session token (in the cookie) are stored ONLY as a sha256 hash (hash_token) - a DB leak
then hands out unusable hashes, never a working login or session, unlike this codebase's existing
plaintext tokens (targets.unsub_token, pending_registrations.code) which stay plaintext since
they're single-purpose and already low-stakes.

Token lifecycle:
    start_login(email, payload=None)
        Find-or-creates the subscriber, mints a login token, emails the link. `payload` carries
        a pending {city, ulica, broj, channel} when this call is a signup, not a bare re-login.
    verify_and_consume_token(raw_token)
        Atomically burns the token (same "UPDATE ... WHERE not-already-consumed" shape as
        scripts/local_web_server.py's /unsubscribe) and returns (subscriber_id, payload) once -
        a replayed link (browser prefetch, forwarded email, a security scanner following the
        link) is refused, not silently re-accepted.

Session lifecycle:
    create_session(subscriber_id) -> raw token for the cookie
    get_subscriber_id_for_session(raw_token) -> subscriber_id or None; sliding-refreshes
        expires_at on every call so an active visitor is never logged out mid-use.
    delete_session(raw_token) -> logout (one browser)
    delete_subscriber_completely(subscriber_id) -> the real "Obriši sve moje podatke" button.
"""
import hashlib
import json
import re
import secrets
import smtplib
import time
from email.message import EmailMessage

import config
from core import partners
from core.database import get_connection

LOGIN_TOKEN_TTL_MINUTES = 15
# A signup's "Potvrdi prijavu" link. 15 minutes lost people who read their mail later (14.10.2026:
# a Kragujevac signup, address typed in, link never clicked in time). It can only save the
# address its owner typed, so a day is the usual confirmation-link lifetime and no real risk.
CONFIRM_TOKEN_TTL_MINUTES = 24 * 60
SESSION_TTL_DAYS = 30

# Per-address magic-link throttle, separate from the per-IP throttle in the web server. Without
# it, one caller (or a botnet spreading the per-IP hits) can point start_login at any third
# party's inbox and drip login/confirm mail at them, and every one of those sends also burns
# from the shared Brevo daily quota that real outage notifications need. Keyed on the normalised
# email, in memory, per process - a restart clearing it is fine (same as the web server's
# buckets). MIN_INTERVAL blocks the rapid-fire case; MAX_PER_DAY caps a slow drip.
LOGIN_EMAIL_MIN_INTERVAL_SECONDS = 90
LOGIN_EMAIL_MAX_PER_DAY = 6
_login_email_hits: dict[str, list[float]] = {}


def _login_email_allowed(email: str) -> bool:
    """True if a magic-link email to this address is within limits right now, recording the
    send. False means silently skip the send - start_login's response is generic either way, so
    a caller (or an abuse target) can't tell a throttled address from a delivered one."""
    key = (email or "").strip().lower()
    now = time.time()
    hits = [t for t in _login_email_hits.get(key, []) if now - t < 86400]
    allowed = not (
        (hits and now - hits[-1] < LOGIN_EMAIL_MIN_INTERVAL_SECONDS)
        or len(hits) >= LOGIN_EMAIL_MAX_PER_DAY
    )
    if allowed:
        hits.append(now)
    _login_email_hits[key] = hits
    return allowed

# Same convention as workers/email_worker.py's UNSUBSCRIBE_BASE_URL - app.najavaradova.rs is the
# Caddy-proxied API origin (see Caddyfile), not the static najavaradova.rs Cloudflare Pages site.
AUTH_BASE_URL = "https://app.najavaradova.rs"


def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# Referrer hosts that are one site under many names: "l.facebook.com", "lm.facebook.com",
# "com.google.android.googlequicksearchbox" (the Google app), "t.co" (links shared on X).
_SOURCE_NAMES = (("google", "google"), ("facebook", "facebook"), ("instagram", "instagram"),
                 ("reddit", "reddit"), ("bing", "bing"), ("duckduckgo", "duckduckgo"),
                 ("yahoo", "yahoo"), ("linkedin", "linkedin"), ("twitter", "x"),
                 ("viber", "viber"), ("telegram", "telegram"), ("chatgpt", "chatgpt"),
                 ("openai", "chatgpt"), ("perplexity", "perplexity"))
_SOURCE_HOSTS = {"t.co": "x", "x.com": "x", "fb.com": "facebook", "fb.me": "facebook",
                 "mail.google.com": "gmail"}


def clean_source(raw) -> tuple[str | None, str | None]:
    """(signup_source, signup_page) from what the signup form sends: {"src": <the referring
    site's host, or a tag from our own link's ?src= / ?utm_source=>, "from": <the city page's
    slug>}. Only a site's name is kept, never a path or query - "google", "facebook",
    "pks-mejl" - and "direct" when the browser named no other site (a typed address, a
    bookmark, most chat apps). (None, None) when the form sent nothing at all: a page loaded
    before this was added."""
    if not isinstance(raw, dict):
        return None, None
    page = re.sub(r"[^a-z0-9-]", "", str(raw.get("from") or "").lower())[:60] or None
    src = str(raw.get("src") or "").strip().lower()[:200]
    host = re.sub(r"^[a-z][a-z0-9+.-]*://", "", src).split("/")[0].split("?")[0].split(":")[0]
    host = re.sub(r"^(www|m|l|lm|mobile)\.", "", host)
    if not host or host == "najavaradova.rs" or host.endswith(".najavaradova.rs"):
        return "direct", page
    if host in _SOURCE_HOSTS:
        return _SOURCE_HOSTS[host], page
    for needle, name in _SOURCE_NAMES:
        if needle in host:
            return name, page
    return re.sub(r"[^a-z0-9.-]", "", host)[:60] or "direct", page


def _find_or_create_subscriber(conn, email: str, ref: str | None = None,
                               source: tuple[str | None, str | None] = (None, None)) -> int:
    """`ref` (a partner slug from a referral link) and `source` (clean_source) are only applied
    to a NEW account - first touch wins, so a partner can never claim someone who already
    signed up directly or via another partner. An unknown ref is ignored (core/partners.py)."""
    email = email.strip().lower()
    row = conn.execute("SELECT id FROM subscribers WHERE email = ?", (email,)).fetchone()
    if row:
        return row["id"]
    return conn.execute(
        "INSERT INTO subscribers (email, referred_by, signup_source, signup_page) VALUES (?, ?, ?, ?)",
        (email, partners.known_partner(conn, ref), *source),
    ).lastrowid


def _insert_link(conn, subscriber_id: int, payload: dict | None, ttl_minutes: int) -> str:
    """Mints a single-use login token (stored hashed) and returns its /auth/verify link. With a
    `payload` it is a confirm: /auth/verify saves that address when the link is used."""
    raw_token = secrets.token_urlsafe(32)
    conn.execute(
        "INSERT INTO login_tokens (subscriber_id, token_hash, purpose, payload_json, expires_at) "
        "VALUES (?, ?, ?, ?, datetime('now', '+' || ? || ' minutes'))",
        (subscriber_id, hash_token(raw_token), "confirm" if payload else "login",
         json.dumps(payload) if payload else None, ttl_minutes),
    )
    return f"{AUTH_BASE_URL}/auth/verify?token={raw_token}"


def issue_link(subscriber_id: int, payload: dict | None, ttl_minutes: int) -> str:
    """The same kind of link for an account that already exists, sent by us rather than asked
    for: core/signup_reminder.py's "Šalji mi na email umesto toga"."""
    conn = get_connection()
    try:
        link = _insert_link(conn, subscriber_id, payload, ttl_minutes)
        conn.commit()
        return link
    finally:
        conn.close()


def start_login(email: str, payload: dict | None = None, ref: str | None = None,
                source: dict | None = None) -> None:
    """Always succeeds from the caller's point of view - no account-enumeration signal either
    way, matching /register's existing "never blocks the 200" pattern. An SMTP failure is logged
    here, not raised - the HTTP response stays the same generic "check your email" regardless."""
    if not _login_email_allowed(email):
        # Over the per-address limit. Skip the send AND the subscriber/token rows it would
        # create, so an abuse target's inbox isn't hit and the DB doesn't fill with junk. The
        # caller still returns the same generic 200 - no signal that this address was throttled.
        print(f"⚠️ magic-link email to {email} throttled (per-address limit)")
        return
    conn = get_connection()
    try:
        subscriber_id = _find_or_create_subscriber(conn, email, ref, clean_source(source))
        link = _insert_link(conn, subscriber_id, payload,
                            CONFIRM_TOKEN_TTL_MINUTES if payload else LOGIN_TOKEN_TTL_MINUTES)
        conn.commit()
    finally:
        conn.close()

    try:
        _send_login_email(email, link, is_signup=bool(payload), payload=payload)
    except Exception as e:
        # Don't raise - the token already exists and the generic "check your email" response is
        # about to go out either way. Loud print so this shows up in `docker compose logs api`;
        # worth wiring into core/alerts.py (the §1 watchdog) if it starts happening often.
        print(f"⚠️ magic-link email to {email} failed to send: {e}")


def verify_and_consume_token(raw_token: str):
    """Returns (subscriber_id, payload_dict_or_None) once, or None for anything invalid/expired/
    already-used - all three cases are deliberately indistinguishable to the caller."""
    if not raw_token:
        return None
    conn = get_connection()
    try:
        token_hash = hash_token(raw_token)
        cur = conn.execute(
            "UPDATE login_tokens SET used_at = CURRENT_TIMESTAMP "
            "WHERE token_hash = ? AND used_at IS NULL AND expires_at > CURRENT_TIMESTAMP",
            (token_hash,),
        )
        if cur.rowcount == 0:
            conn.commit()
            return None
        row = conn.execute(
            "SELECT subscriber_id, payload_json FROM login_tokens WHERE token_hash = ?",
            (token_hash,),
        ).fetchone()
        conn.commit()
        payload = json.loads(row["payload_json"]) if row["payload_json"] else None
        return row["subscriber_id"], payload
    finally:
        conn.close()


def _expired_unused(conn, raw_token: str):
    return conn.execute(
        "SELECT t.id, t.subscriber_id, t.payload_json, s.email FROM login_tokens t "
        "JOIN subscribers s ON s.id = t.subscriber_id "
        "WHERE t.token_hash = ? AND t.used_at IS NULL AND t.expires_at <= CURRENT_TIMESTAMP",
        (hash_token(raw_token),),
    ).fetchone() if raw_token else None


def is_expired_unused(raw_token: str) -> bool:
    """A real link that ran out before anyone clicked it - the one case /auth/verify answers with
    "send me a new one" instead of a dead end (14.10.2026: a signup opened the mail 3 minutes
    after its 15-minute link died and clicked it six times). Only the link's holder learns this."""
    conn = get_connection()
    try:
        return _expired_unused(conn, raw_token) is not None
    finally:
        conn.close()


def resend_expired_link(raw_token: str) -> bool:
    """Mails a fresh link of the same kind (same saved address, if any) to the address the
    expired one went to - never anywhere else - and spends the expired one, so each dead link
    gets one new one. Throttled like every login email. False if nothing was sent."""
    conn = get_connection()
    try:
        row = _expired_unused(conn, raw_token)
        if not row or not _login_email_allowed(row["email"]):
            return False
        payload = json.loads(row["payload_json"]) if row["payload_json"] else None
        conn.execute("UPDATE login_tokens SET used_at = CURRENT_TIMESTAMP WHERE id = ?", (row["id"],))
        link = _insert_link(conn, row["subscriber_id"], payload,
                            CONFIRM_TOKEN_TTL_MINUTES if payload else LOGIN_TOKEN_TTL_MINUTES)
        conn.commit()
    finally:
        conn.close()
    try:
        _send_login_email(row["email"], link, is_signup=bool(payload), payload=payload)
    except Exception as e:
        print(f"⚠️ renewed link for subscriber #{row['subscriber_id']} failed to send: {e}")
        return False
    return True


def create_session(subscriber_id: int) -> str:
    conn = get_connection()
    try:
        raw_token = secrets.token_urlsafe(32)
        conn.execute(
            "INSERT INTO sessions (subscriber_id, token_hash, expires_at) "
            "VALUES (?, ?, datetime('now', '+' || ? || ' days'))",
            (subscriber_id, hash_token(raw_token), SESSION_TTL_DAYS),
        )
        conn.commit()
        return raw_token
    finally:
        conn.close()


def get_subscriber_id_for_session(raw_token: str):
    if not raw_token:
        return None
    conn = get_connection()
    try:
        token_hash = hash_token(raw_token)
        row = conn.execute(
            "SELECT subscriber_id FROM sessions WHERE token_hash = ? AND expires_at > CURRENT_TIMESTAMP",
            (token_hash,),
        ).fetchone()
        if not row:
            return None
        # Sliding expiry - refreshed on every authenticated request, so an active visitor is
        # never logged out mid-use; only real inactivity lets the session actually lapse.
        conn.execute(
            "UPDATE sessions SET expires_at = datetime('now', '+' || ? || ' days') WHERE token_hash = ?",
            (SESSION_TTL_DAYS, token_hash),
        )
        conn.commit()
        return row["subscriber_id"]
    finally:
        conn.close()


def delete_session(raw_token: str) -> None:
    if not raw_token:
        return
    conn = get_connection()
    conn.execute("DELETE FROM sessions WHERE token_hash = ?", (hash_token(raw_token),))
    conn.commit()
    conn.close()


def get_subscriber_email(subscriber_id: int) -> str | None:
    conn = get_connection()
    row = conn.execute("SELECT email FROM subscribers WHERE id = ?", (subscriber_id,)).fetchone()
    conn.close()
    return row["email"] if row else None


def delete_target(conn, subscriber_id: int, target_id: int) -> bool:
    """Hard-delete ONE address of this subscriber with its child rows - the dashboard's "Obriši"
    and the operator's ❌ on a manager's list (core/managers.py). Same explicit cleanup as below
    (no reliance on ON DELETE CASCADE), including an unconsumed Telegram link meant for this
    address. False when the address isn't this subscriber's; nothing is touched then. The caller
    commits."""
    if not conn.execute("SELECT 1 FROM targets WHERE id = ? AND subscriber_id = ?",
                        (target_id, subscriber_id)).fetchone():
        return False
    conn.execute("DELETE FROM message_queue WHERE target_id = ?", (target_id,))
    conn.execute("DELETE FROM target_hexes WHERE target_id = ?", (target_id,))
    conn.execute("DELETE FROM pending_registrations WHERE attach_target_id = ? AND subscriber_id = ?",
                 (target_id, subscriber_id))
    conn.execute("DELETE FROM targets WHERE id = ? AND subscriber_id = ?", (target_id, subscriber_id))
    return True


def delete_subscriber_completely(subscriber_id: int) -> None:
    """The real "Obriši sve moje podatke" button - a hard DELETE, not the existing is_active=0
    soft-deactivate everything else in this codebase uses. Every table is cleaned explicitly,
    with NO reliance on any table's declared `ON DELETE CASCADE` - confirmed by test that SQLite
    does not enforce those unless `PRAGMA foreign_keys = ON` is set per-connection, which
    core/database.py's get_connection() does not do (true for every table in this schema, not
    specific to this one - left alone here rather than flipping it globally, which would be a
    repo-wide behavior change well outside this feature's scope). target_hexes is cleaned via
    each target's own row for the same reason."""
    conn = get_connection()
    try:
        target_ids = [r["id"] for r in conn.execute(
            "SELECT id FROM targets WHERE subscriber_id = ?", (subscriber_id,)
        ).fetchall()]
        if target_ids:
            placeholders = ",".join("?" * len(target_ids))
            conn.execute(f"DELETE FROM message_queue WHERE target_id IN ({placeholders})", target_ids)
            conn.execute(f"DELETE FROM target_hexes WHERE target_id IN ({placeholders})", target_ids)
            conn.execute(f"DELETE FROM targets WHERE id IN ({placeholders})", target_ids)
        conn.execute("DELETE FROM pending_registrations WHERE subscriber_id = ?", (subscriber_id,))
        conn.execute("DELETE FROM login_tokens WHERE subscriber_id = ?", (subscriber_id,))
        conn.execute("DELETE FROM sessions WHERE subscriber_id = ?", (subscriber_id,))
        conn.execute("DELETE FROM subscribers WHERE id = ?", (subscriber_id,))
        conn.commit()
    finally:
        conn.close()


def _html_escape(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
             .replace('"', "&quot;").replace("'", "&#39;"))


def _send_login_email(to_email: str, link: str, is_signup: bool, payload: dict | None = None) -> None:
    """Sent synchronously, straight from the HTTP request - unlike workers/email_worker.py's
    outbound notifications (queued, drained every 60s by a separate worker process), a login
    link needs to feel instant. Own short-lived SMTP connection per call: login volume is tiny
    next to notification volume, so a fresh connect+send+quit each time is simpler than sharing
    a long-lived connection, and never blocks on someone else's notification batch."""
    msg = EmailMessage()
    msg["Subject"] = (
        "Potvrdi prijavu — NajavaRadova.rs" if is_signup
        else "Tvoj link za prijavu — NajavaRadova.rs"
    )
    msg["To"] = to_email
    # A building manager's list (/upravnici, core/managers.py): formal "Vi", and the button
    # loads the whole list.
    manager = (payload or {}).get("manager_request")
    if manager:
        del msg["Subject"]
        msg["Subject"] = f"Potvrdite unos zgrada ({payload.get('label', '')}) — NajavaRadova.rs"

    # The "nema lozinke" line is deliberate: a 30-50, low-tech user opens this looking for a
    # password to type and gets confused when there isn't one. Say it up front.
    intro = (
        "Klikni dugme ispod da potvrdiš svoju adresu i uđeš na svoju stranicu. "
        "Nema lozinke — ovaj link te prijavljuje."
        if is_signup else
        "Klikni dugme ispod da se prijaviš na svoju stranicu. "
        "Nema lozinke — ovaj link te prijavljuje."
    )
    lasts = "24 sata" if is_signup else f"{LOGIN_TOKEN_TTL_MINUTES} minuta"
    button = "Potvrdi adresu" if is_signup else "Prijavi se"
    ignore = "Ako ovo nisi ti tražio/la, slobodno ignoriši ovaj mejl."
    if manager:
        intro = (f"Poštovani, primili smo Vaš spisak ({payload.get('label', '')}). Kliknite dugme "
                 f"ispod da potvrdite mejl - adrese se odmah unose i obaveštenja kreću. Nema lozinke.")
        button = "Potvrdi i unesi zgrade"
        ignore = "Ako niste Vi poslali spisak, slobodno zanemarite ovaj mejl - ništa neće biti uneto."
    msg.set_content(
        f"{intro}\n\n{link}\n\n"
        f"Link važi {lasts} i radi samo jednom. {ignore}"
    )
    html = f"""\
<!doctype html><html lang="sr"><body style="margin:0;padding:32px 16px;background:#101013;
font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;">
<table role="presentation" width="100%" style="max-width:480px;margin:0 auto;border-collapse:collapse;">
<tr><td style="background:#1B1B20;border:1px solid #2B2B31;border-radius:16px;padding:28px 26px;">
  <div style="color:#F2F0EE;font-size:16px;font-weight:700;letter-spacing:-.02em;">
    NajavaRadova<span style="color:#7FA9F5;font-weight:400;">.rs</span>
  </div>
  <p style="color:#F2F0EE;font-size:15px;line-height:1.55;margin:22px 0 0;">{_html_escape(intro)}</p>
  <a href="{link}" style="display:inline-block;margin-top:20px;padding:13px 22px;border-radius:10px;
    background:#F2F0EE;color:#101013;font-size:15px;font-weight:600;text-decoration:none;">
    {button}
  </a>
  <hr style="border:none;border-top:1px solid #2B2B31;margin:24px 0 16px;">
  <p style="margin:0;font-size:12.5px;color:#8C8892;line-height:1.5;">
    Link važi {lasts} i radi samo jednom.
    {_html_escape(ignore)}
  </p>
</td></tr>
</table>
</body></html>"""
    msg.add_alternative(html, subtype="html")
    send_account_email(msg)


def send_account_email(msg: EmailMessage) -> None:
    """An account email (a login link, a signup reminder), over its own short-lived SMTP
    connection. From is set here: an own sender identity, kept apart from outage-notification
    mail so the two don't share a deliverability reputation (see config.LOGIN_EMAIL_FROM_ADDRESS -
    falls back to the notification sender when the second Brevo sender isn't set up yet)."""
    from_addr = config.LOGIN_EMAIL_FROM_ADDRESS
    if not config.BREVO_SMTP_LOGIN or not config.BREVO_SMTP_KEY or not from_addr:
        raise RuntimeError("BREVO_SMTP_LOGIN / BREVO_SMTP_KEY / LOGIN_EMAIL_FROM_ADDRESS not set")
    msg["From"] = f"{config.LOGIN_EMAIL_FROM_NAME} <{from_addr}>"

    smtp = smtplib.SMTP(config.BREVO_SMTP_HOST, config.BREVO_SMTP_PORT, timeout=15)
    try:
        smtp.starttls()
        smtp.login(config.BREVO_SMTP_LOGIN, config.BREVO_SMTP_KEY)
        smtp.send_message(msg)
    finally:
        try:
            smtp.quit()
        except Exception:
            pass
