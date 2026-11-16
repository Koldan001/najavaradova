"""One message per person per outage. A queue row is per address, so someone whose two addresses
were both in one outage got the same email twice (29.09.2026, outage #3394, two Novi Sad
addresses of one person). The workers now send each person one email / Telegram message per
outage naming every affected address once, mark every row SENT, and the email's unsubscribe
link turns off all the addresses it is about. A database error after delivery never re-sends."""
import os
import sqlite3
import sys
import threading
from urllib.parse import parse_qs, urlparse

import pytest

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))


@pytest.fixture()
def db(tmp_path, monkeypatch):
    import core.database as database
    monkeypatch.setattr(database, "DB_NAME", str(tmp_path / "one.db"))
    database.init_db()
    return database


def _person(database, addresses, email="osoba@example.com", chat=777, outage_id=None, manager=0):
    """One person (one account, one email, one Telegram chat) with the given addresses, each
    queued for the same outage on both channels, with the given message text. manager=1: the
    addresses came in as a building manager's list (targets.manager_list)."""
    conn = database.get_connection()
    sid = conn.execute("INSERT INTO subscribers (email) VALUES (?)", (email,)).lastrowid
    if outage_id is None:
        outage_id = conn.execute(
            "INSERT INTO outages (region, raw_text, status, category, outage_date) "
            "VALUES ('Novi Sad', 'raw', 'ACTIVE', 'HEATING', '29.09.2026')").lastrowid
    rows = []
    for i, (street, text) in enumerate(addresses):
        tid = conn.execute(
            "INSERT INTO targets (subscriber_id, city, ulica, broj, email, telegram_chat_id, "
            "unsub_token, is_active, email_active, telegram_active, manager_list) "
            "VALUES (?,?,?,?,?,?,?,1,1,1,?)",
            (sid, "НОВИ САД", street, str(i + 1), email, chat, f"tok-{email}-{i}", manager)).lastrowid
        for channel in ("email", "telegram"):
            rows.append(conn.execute(
                "INSERT INTO message_queue (outage_id, target_id, channel, custom_message, status, "
                "created_at) VALUES (?,?,?,?, 'QUEUED', datetime('now'))",
                (outage_id, tid, channel, text)).lastrowid)
    conn.commit()
    conn.close()
    return outage_id


def _statuses(database, channel):
    conn = database.get_connection()
    rows = conn.execute("SELECT status, send_attempts FROM message_queue WHERE channel = ? ORDER BY id",
                        (channel,)).fetchall()
    conn.close()
    return [tuple(r) for r in rows]


@pytest.fixture()
def smtp(monkeypatch):
    """Swaps Brevo for a recorder; returns the list of sent EmailMessages."""
    import workers.email_worker as ew
    sent = []

    class FakeSMTP:
        def __init__(self, *a, **k): pass
        def starttls(self): pass
        def login(self, *a): pass
        def send_message(self, msg): sent.append(msg)
        def quit(self): pass

    monkeypatch.setattr(ew.smtplib, "SMTP", FakeSMTP)
    for name, value in (("BREVO_SMTP_LOGIN", "x"), ("BREVO_SMTP_KEY", "x"), ("EMAIL_FROM_ADDRESS", "a@b.c")):
        monkeypatch.setattr(ew.config, name, value)
    return sent


def _plain(msg):
    return msg.get_body(preferencelist=("plain",)).get_content()


def test_two_addresses_in_one_outage_get_one_email(db, smtp):
    import workers.email_worker as ew
    _person(db, [("ДУШАНА ДАНИЛОВИЋА", "Bez grejanja u ulici Dušana Danilovića 29.09."),
                 ("БУЛЕВАР КНЕЗА МИЛОША", "Bez grejanja u ulici Bulevar kneza Miloša 29.09.")])
    ew.run_worker()

    assert len(smtp) == 1
    plain = _plain(smtp[0])
    assert "Dušana Danilovića" in plain and "Bulevar kneza Miloša" in plain
    assert _statuses(db, "email") == [("SENT", 0), ("SENT", 0)]
    # one click turns off both addresses the email is about
    link = smtp[0]["List-Unsubscribe"].strip("<>")
    assert parse_qs(urlparse(link).query)["token"] == ["tok-osoba@example.com-0", "tok-osoba@example.com-1"]


def test_the_same_text_for_two_addresses_is_shown_once(db, smtp):
    import workers.email_worker as ew
    same = "Bez grejanja u ulici Dušana Danilovića 29.09."
    _person(db, [("ДУШАНА ДАНИЛОВИЋА", same), ("ДУШАНА ДАНИЛОВИЋА", same)])
    ew.run_worker()
    assert len(smtp) == 1 and _plain(smtp[0]).count(same) == 1


def test_different_people_get_their_own_and_one_persons_outages_go_together(db, smtp):
    import workers.email_worker as ew
    first = _person(db, [("ДУШАНА ДАНИЛОВИЋА", "Tekst A")], email="prva@example.com", chat=1)
    _person(db, [("ДУШАНА ДАНИЛОВИЋА", "Tekst A")], email="druga@example.com", chat=2, outage_id=first)
    conn = db.get_connection()                     # another outage for the first person's address
    other = conn.execute("INSERT INTO outages (region, raw_text, status) VALUES ('Novi Sad', 'r', 'ACTIVE')").lastrowid
    tid = conn.execute("SELECT id FROM targets WHERE email = 'prva@example.com'").fetchone()[0]
    conn.execute("INSERT INTO message_queue (outage_id, target_id, channel, custom_message, status, created_at) "
                 "VALUES (?, ?, 'email', 'Tekst B', 'QUEUED', datetime('now'))", (other, tid))
    conn.commit()
    conn.close()
    ew.run_worker()
    by_person = {m["To"]: _plain(m) for m in smtp}
    assert len(smtp) == 2 and set(by_person) == {"prva@example.com", "druga@example.com"}
    assert "Tekst A" in by_person["prva@example.com"] and "Tekst B" in by_person["prva@example.com"]
    assert "Tekst B" not in by_person["druga@example.com"]


def test_a_manager_gets_one_box_per_building(db, smtp):
    import workers.email_worker as ew
    _person(db, [("УЛИЦА А", "Bez vode u ulici A."), ("УЛИЦА Б", "Bez vode u ulici B."),
                 ("УЛИЦА В", "Bez vode u ulici V.")], manager=1)
    ew.run_worker()
    assert len(smtp) == 1
    plain = _plain(smtp[0])
    assert plain.startswith(ew.TENANT_HEADING)
    assert plain.count("— najavaradova.rs") == 3          # a signed notice per building
    html = smtp[0].get_body(preferencelist=("html",)).get_content()
    assert html.count("border:1px dashed") == 3


def test_telegram_sends_one_message_per_chat_per_outage(db, monkeypatch):
    import workers.telegram_worker as tw
    sent = []

    class Ok:
        ok, status_code = True, 200
        def json(self): return {"ok": True}

    monkeypatch.setattr(tw.config, "TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setattr(tw.requests, "post", lambda url, json=None, timeout=None: sent.append(json) or Ok())
    _person(db, [("ДУШАНА ДАНИЛОВИЋА", "Bez grejanja u ulici Dušana Danilovića."),
                 ("БУЛЕВАР КНЕЗА МИЛОША", "Bez grejanja u ulici Bulevar kneza Miloša.")])
    tw.run_worker()

    assert len(sent) == 1
    assert "Dušana Danilovića" in sent[0]["text"] and "Bulevar kneza Miloša" in sent[0]["text"]
    assert _statuses(db, "telegram") == [("SENT", 0), ("SENT", 0)]


def test_a_busy_database_after_delivery_never_resends(db, monkeypatch):
    """Marking a delivered email SENT used to sit inside the send's error handling: a database
    error there counted as a failed send and put the row back in the queue."""
    import workers.email_worker as ew
    real = ew.get_connection
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] <= 2:
            raise sqlite3.OperationalError("database is locked")
        return real()

    _person(db, [("УЛИЦА А", "Tekst")])
    conn = db.get_connection()
    ids = [r[0] for r in conn.execute("SELECT id FROM message_queue WHERE channel = 'email'")]
    conn.close()
    monkeypatch.setattr(ew, "get_connection", flaky)
    monkeypatch.setattr(ew.time, "sleep", lambda s: None)
    ew.mark_sent(ids)
    assert _statuses(db, "email") == [("SENT", 0)]


def test_unsubscribe_link_with_several_tokens_turns_them_all_off(db):
    import requests
    from http.server import HTTPServer
    from scripts.local_web_server import Handler
    _person(db, [("УЛИЦА А", "a"), ("УЛИЦА Б", "b")])
    _person(db, [("УЛИЦА В", "c")], email="neko@example.com", chat=9)

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{server.server_port}/unsubscribe"
        tokens = "token=tok-osoba@example.com-0&token=tok-osoba@example.com-1"
        page = requests.get(f"{base}?{tokens}", timeout=5).text
        assert "Odjaviti adrese" in page and "Ulica A" in page and "Ulica B" in page
        done = requests.post(f"{base}?{tokens}", data="List-Unsubscribe=One-Click", timeout=5).text
        assert "su odjavljene" in done
    finally:
        server.shutdown()

    conn = db.get_connection()
    active = dict(conn.execute("SELECT unsub_token, is_active FROM targets").fetchall())
    conn.close()
    assert active == {"tok-osoba@example.com-0": 0, "tok-osoba@example.com-1": 0,
                      "tok-neko@example.com-0": 1}          # someone else's address is untouched


def test_three_addresses_without_a_manager_list_get_the_plain_alert(db, smtp):
    """A family with a few flats, each typed in by hand, is not a building manager: no
    "Tekst za stanare" box, just the alert (05.11.2026 - the count of addresses no longer decides)."""
    import workers.email_worker as ew
    _person(db, [("УЛИЦА А", "Bez vode u ulici A."), ("УЛИЦА Б", "Bez vode u ulici B."),
                 ("УЛИЦА В", "Bez vode u ulici V.")])
    ew.run_worker()
    assert len(smtp) == 1
    plain = _plain(smtp[0])
    assert not plain.startswith(ew.TENANT_HEADING)
    assert "— najavaradova.rs" not in plain
    assert all(t in plain for t in ("Bez vode u ulici A.", "Bez vode u ulici B.", "Bez vode u ulici V."))
