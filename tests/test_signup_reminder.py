"""One reminder email for a Telegram signup that never pressed Start (core/signup_reminder.py).
Real case, 30.09.2026: a signup from Google via the Svilajnac city page confirmed their email in
23 seconds, never opened the bot, and was left with an account and no address."""
import os
import re
import sys
import threading
from datetime import datetime, timedelta, timezone
from http.server import HTTPServer

import pytest
import requests

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from core import auth
from core import signup_reminder as sr

NOW = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)          # 14:00 in Belgrade


@pytest.fixture()
def db(tmp_path, monkeypatch):
    import core.database as database
    monkeypatch.setattr(database, "DB_NAME", str(tmp_path / "remind.db"))
    database.init_db()
    monkeypatch.setattr(sr, "_bot_username", lambda: "NajavaTestBot")
    return database


@pytest.fixture()
def smtp(monkeypatch):
    """Swaps Brevo for a recorder; returns the list of sent EmailMessages."""
    sent = []

    class FakeSMTP:
        def __init__(self, *a, **k): pass
        def starttls(self): pass
        def login(self, *a): pass
        def send_message(self, msg): sent.append(msg)
        def quit(self): pass

    monkeypatch.setattr(auth.smtplib, "SMTP", FakeSMTP)
    for name, value in (("BREVO_SMTP_LOGIN", "x"), ("BREVO_SMTP_KEY", "x"),
                        ("LOGIN_EMAIL_FROM_ADDRESS", "prijava@najavaradova.rs")):
        monkeypatch.setattr(auth.config, name, value)
    return sent


def _signup(database, hours_ago, email="dule@example.com", code="kod123", consumed=False,
            attach=None, with_address=False):
    """A signup that chose Telegram and confirmed its email `hours_ago` before NOW."""
    made = (NOW - timedelta(hours=hours_ago)).strftime("%Y-%m-%d %H:%M:%S")
    conn = database.get_connection()
    sid = conn.execute("SELECT id FROM subscribers WHERE email = ?", (email,)).fetchone()
    sid = sid[0] if sid else conn.execute("INSERT INTO subscribers (email) VALUES (?)", (email,)).lastrowid
    conn.execute(
        "INSERT INTO pending_registrations (code, channel, city, ulica, broj, email, subscriber_id, "
        "attach_target_id, consumed_at, created_at) VALUES (?, 'telegram', 'СВИЛАЈНАЦ', "
        "'КАРАЂОРЂЕВА', '12', ?, ?, ?, ?, ?)",
        (code, email, sid, attach, made if consumed else None, made))
    if with_address:
        conn.execute("INSERT INTO targets (subscriber_id, city, ulica, broj, email, is_active) "
                     "VALUES (?, 'СВИЛАЈНАЦ', 'ДРУГА', '1', ?, 1)", (sid, email))
    conn.commit()
    conn.close()
    return sid


def _plain(msg):
    return msg.get_body(preferencelist=("plain",)).get_content()


def _reminded(database):
    conn = database.get_connection()
    rows = [r[0] for r in conn.execute("SELECT reminded_at FROM pending_registrations ORDER BY id")]
    conn.close()
    return rows


def test_an_unfinished_telegram_signup_gets_one_reminder(db, smtp):
    _signup(db, hours_ago=4)
    assert sr.send_signup_reminders(NOW) == 1
    [msg] = smtp
    assert msg["To"] == "dule@example.com"
    assert msg["Subject"] == "Tvoja adresa još nije sačuvana — NajavaRadova.rs"
    plain = _plain(msg)
    assert "Svilajnac, Karađorđeva 12" in plain
    assert "https://t.me/NajavaTestBot?start=kod123" in plain
    assert "https://app.najavaradova.rs/auth/verify?token=" in plain
    assert "važi do 01.10. u 10:00" in plain                        # made 08:00 UTC + 24h, Belgrade
    assert _reminded(db)[0] is not None
    assert sr.send_signup_reminders(NOW + timedelta(hours=1)) == 0  # only once
    assert len(smtp) == 1


@pytest.mark.parametrize("hours_ago", [1, 21])
def test_not_too_soon_and_not_once_the_telegram_link_is_nearly_gone(db, smtp, hours_ago):
    _signup(db, hours_ago=hours_ago)
    assert sr.send_signup_reminders(NOW) == 0 and not smtp


def test_not_at_night(db, smtp):
    _signup(db, hours_ago=4)
    night = datetime(2026, 9, 30, 20, 30, tzinfo=timezone.utc)      # 22:30 in Belgrade
    _signup(db, hours_ago=4 - 8.5, email="kasno@example.com", code="kod2")  # 4h before `night`
    assert sr.send_signup_reminders(night) == 0 and not smtp


@pytest.mark.parametrize("kwargs", [dict(consumed=True), dict(with_address=True), dict(attach=5)])
def test_not_for_someone_finished_or_already_getting_alerts(db, smtp, kwargs):
    _signup(db, hours_ago=4, **kwargs)
    assert sr.send_signup_reminders(NOW) == 0 and not smtp


def test_two_tries_at_one_address_are_one_reminder_with_the_newest_link(db, smtp):
    _signup(db, hours_ago=6, code="stari")
    _signup(db, hours_ago=4, code="novi")
    assert sr.send_signup_reminders(NOW) == 1
    plain = _plain(smtp[0])
    assert "start=novi" in plain and "start=stari" not in plain
    assert all(r is not None for r in _reminded(db))


def test_a_failed_send_is_tried_again(db, smtp, monkeypatch):
    _signup(db, hours_ago=4)

    def down(msg):
        raise OSError("Brevo down")
    real = auth.send_account_email
    monkeypatch.setattr(auth, "send_account_email", down)
    assert sr.send_signup_reminders(NOW) == 0
    assert _reminded(db) == [None]
    monkeypatch.setattr(auth, "send_account_email", real)
    assert sr.send_signup_reminders(NOW + timedelta(hours=1)) == 1


def test_the_email_button_saves_the_address_and_drops_the_telegram_link(db, smtp, monkeypatch):
    from scripts.local_web_server import Handler
    monkeypatch.setattr(Handler, "_backfill_target", lambda self, target_id: None)
    sid = _signup(db, hours_ago=4)
    sr.send_signup_reminders(NOW)
    link = re.search(r"https://app\.najavaradova\.rs(/auth/verify\?token=\S+)", _plain(smtp[0])).group(1)

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{server.server_port}"
        assert "Prijavljujemo te" in requests.get(base + link, timeout=5).text     # GET spends nothing
        done = requests.post(base + link, timeout=5, allow_redirects=False)
        assert done.status_code == 302 and done.headers["Location"] == "/dashboard"
        again = requests.post(base + link, timeout=5, allow_redirects=False)
        assert again.status_code == 400                                          # single use
    finally:
        server.shutdown()

    conn = db.get_connection()
    targets = [tuple(r) for r in conn.execute(
        "SELECT subscriber_id, city, ulica, broj, email, is_active FROM targets")]
    pending = conn.execute("SELECT COUNT(*) FROM pending_registrations").fetchone()[0]
    conn.close()
    assert targets == [(sid, "СВИЛАЈНАЦ", "КАРАЂОРЂЕВА", "12", "dule@example.com", 1)]
    assert pending == 0


# --- The confirmation link, never clicked (14.10.2026, Kragujevac) ------------------------------

def _form(database, smtp, hours_ago, email="marija@example.com", used=False, with_address=False):
    """Someone who sent the signup form `hours_ago` before NOW; returns their subscriber id."""
    auth._login_email_hits.clear()
    auth.start_login(email, payload={"city": "Kragujevac", "ulica": "Stojana Protića", "broj": "4",
                                     "channel": "email"})
    made = (NOW - timedelta(hours=hours_ago)).strftime("%Y-%m-%d %H:%M:%S")
    conn = database.get_connection()
    sid = conn.execute("SELECT id FROM subscribers WHERE email = ?", (email,)).fetchone()[0]
    conn.execute("UPDATE login_tokens SET created_at = ?, used_at = ? WHERE subscriber_id = ?",
                 (made, made if used else None, sid))
    if with_address:
        conn.execute("INSERT INTO targets (subscriber_id, city, ulica, broj, email, is_active) "
                     "VALUES (?, 'Kragujevac', 'Stojana Protića', '4', ?, 1)", (sid, email))
    conn.commit()
    conn.close()
    smtp.clear()
    return sid


def test_a_signup_confirmation_link_lasts_a_day_a_login_link_15_minutes(db, smtp):
    _form(db, smtp, hours_ago=0)
    auth.start_login("stari@example.com")
    conn = db.get_connection()
    ttl = dict(conn.execute(
        "SELECT purpose, ROUND((julianday(expires_at) - julianday('now')) * 24 * 60) "
        "FROM login_tokens").fetchall())
    conn.close()
    assert ttl == {"confirm": 24 * 60, "login": 15}
    assert "Link važi 15 minuta" in _plain(smtp[-1])


def test_an_unclicked_confirmation_gets_one_reminder_with_a_fresh_link(db, smtp):
    sid = _form(db, smtp, hours_ago=2)
    assert sr.send_confirm_reminders(NOW) == 1
    [msg] = smtp
    assert msg["To"] == "marija@example.com"
    assert msg["Subject"] == "Potvrdi prijavu — adresa još nije sačuvana"
    plain = _plain(msg)
    assert "Kragujevac, Stojana Protića 4" in plain
    link = re.search(r"https://app\.najavaradova\.rs/auth/verify\?token=(\S+)", plain).group(1)
    assert auth.verify_and_consume_token(link)[0] == sid             # the fresh link works
    assert sr.send_confirm_reminders(NOW + timedelta(hours=2)) == 0  # only once
    assert len(smtp) == 1


@pytest.mark.parametrize("hours_ago", [0.5, 21])
def test_no_confirm_reminder_too_soon_or_too_late(db, smtp, hours_ago):
    _form(db, smtp, hours_ago=hours_ago)
    assert sr.send_confirm_reminders(NOW) == 0 and not smtp


@pytest.mark.parametrize("kwargs", [dict(used=True), dict(with_address=True)])
def test_no_confirm_reminder_for_someone_who_got_in(db, smtp, kwargs):
    _form(db, smtp, hours_ago=2, **kwargs)
    assert sr.send_confirm_reminders(NOW) == 0 and not smtp


def test_no_confirm_reminder_at_night(db, smtp):
    _form(db, smtp, hours_ago=2)
    night = datetime(2026, 9, 30, 20, 30, tzinfo=timezone.utc)      # 22:30 in Belgrade
    assert sr.send_confirm_reminders(night) == 0 and not smtp


# --- An expired link offers a new one instead of a dead end ------------------------------------

def test_an_expired_link_mails_a_fresh_one_to_the_same_address_once(db, smtp, monkeypatch):
    from scripts.local_web_server import Handler
    monkeypatch.setattr(Handler, "_backfill_target", lambda self, target_id: None)
    auth._login_email_hits.clear()
    auth.start_login("marija@example.com", payload={"city": "Kragujevac", "ulica": "Stojana Protića",
                                                    "broj": "4", "channel": "email"})
    old = re.search(r"https://app\.najavaradova\.rs/auth/verify\?token=(\S+)", _plain(smtp[0])).group(1)
    conn = db.get_connection()
    conn.execute("UPDATE login_tokens SET expires_at = datetime('now', '-1 minute')")
    conn.commit()
    conn.close()
    auth._login_email_hits.clear()

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{server.server_port}"
        dead = requests.post(f"{base}/auth/verify?token={old}", timeout=5, allow_redirects=False)
        assert dead.status_code == 400 and "Pošalji mi novi link" in dead.text
        sent = requests.post(f"{base}/auth/resend?token={old}", timeout=5)
        assert sent.status_code == 200 and "Poslali smo ti novi link" in sent.text
        assert len(smtp) == 2 and smtp[1]["To"] == "marija@example.com"
        auth._login_email_hits.clear()
        twice = requests.post(f"{base}/auth/resend?token={old}", timeout=5)
        assert twice.status_code == 400 and len(smtp) == 2                  # one new link per dead one

        new = re.search(r"/auth/verify\?token=(\S+)", _plain(smtp[1])).group(1)
        done = requests.post(f"{base}/auth/verify?token={new}", timeout=5, allow_redirects=False)
        assert done.status_code == 302
    finally:
        server.shutdown()
    conn = db.get_connection()
    targets = [tuple(r) for r in conn.execute("SELECT city, ulica, broj FROM targets")]
    conn.close()
    assert targets == [("Kragujevac", "Stojana Protića", "4")]


def test_a_used_or_made_up_link_gets_no_new_one(db, smtp):
    _form(db, smtp, hours_ago=0, used=True)
    assert not auth.is_expired_unused("made-up") and not auth.resend_expired_link("made-up")
    assert not smtp
