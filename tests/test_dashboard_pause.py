"""The dashboard's master switch pauses and resumes one address. Each channel keeps its own on/off
through a pause (the page greys the channel switches out meanwhile), except that resuming an
address whose every connected channel is off turns them all on - otherwise "on" still sends
nothing. Pausing must stop every send, whatever the channel flags say."""
import os
import sys
import threading
from http.server import HTTPServer

import pytest
import requests

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
os.environ.setdefault("GEMINI_API_KEY", "test-placeholder")


@pytest.fixture()
def db(tmp_path, monkeypatch):
    import core.database as database
    monkeypatch.setattr(database, "DB_NAME", str(tmp_path / "pause.db"))
    database.init_db()
    return database


@pytest.fixture()
def toggle(db, monkeypatch):
    """POSTs /api/targets/toggle as subscriber 1; returns (status, json)."""
    from scripts.local_web_server import Handler
    monkeypatch.setattr(Handler, "_current_subscriber_id", lambda self: 1)
    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}"

    def call(target_id):
        r = requests.post(base + "/api/targets/toggle", json={"id": target_id}, timeout=5)
        return r.status_code, r.json()
    yield call
    server.shutdown()


def _target(db, email_active=1, telegram_active=1, email="a@example.com", chat="111", owner=1):
    conn = db.get_connection()
    cur = conn.execute(
        "INSERT INTO targets (city, ulica, broj, is_active, email, email_active, telegram_chat_id, "
        "telegram_active, subscriber_id) VALUES ('НОВИ САД', 'ПАРТИЗАНСКА', '36', 1, ?, ?, ?, ?, ?)",
        (email, email_active, chat, telegram_active, owner))
    conn.commit()
    tid = cur.lastrowid
    conn.close()
    return tid


def _flags(db, tid):
    conn = db.get_connection()
    row = tuple(conn.execute(
        "SELECT is_active, email_active, telegram_active FROM targets WHERE id = ?", (tid,)).fetchone())
    conn.close()
    return row


def test_pause_and_resume_keep_each_channel_as_it_was(db, toggle):
    tid = _target(db, email_active=0, telegram_active=1)       # email muted, Telegram on
    assert toggle(tid) == (200, {"ok": True, "is_active": 0, "email_active": 0, "telegram_active": 1})
    assert toggle(tid) == (200, {"ok": True, "is_active": 1, "email_active": 0, "telegram_active": 1})
    assert _flags(db, tid) == (1, 0, 1)


def test_resuming_with_every_channel_off_turns_them_on(db, toggle):
    tid = _target(db, email_active=0, telegram_active=0)
    toggle(tid)
    assert toggle(tid)[1] == {"ok": True, "is_active": 1, "email_active": 1, "telegram_active": 1}


def test_a_muted_channel_that_isnt_connected_doesnt_count_as_on(db, toggle):
    # Email-only address with email muted; telegram_active=1 is a default, no chat is connected.
    tid = _target(db, email_active=0, telegram_active=1, chat=None)
    toggle(tid)
    assert toggle(tid)[1]["email_active"] == 1


def test_someone_elses_address_is_not_touched(db, toggle):
    tid = _target(db, owner=2)
    assert toggle(tid)[0] == 404
    assert _flags(db, tid) == (1, 1, 1)


def test_a_paused_address_gets_nothing_even_with_its_channels_on(db, toggle):
    """The kept channel flags must never leak a send: the worker gates on is_active as well."""
    import workers.email_worker as ew
    tid = _target(db)
    conn = db.get_connection()
    conn.execute("INSERT INTO message_queue (outage_id, target_id, custom_message, status, channel, "
                 "created_at) VALUES (1, ?, 'x', 'QUEUED', 'email', datetime('now', '-1 day'))", (tid,))
    conn.commit()
    conn.close()
    assert ew.has_pending_messages()           # control: on, it would go out
    toggle(tid)
    assert _flags(db, tid) == (0, 1, 1)
    assert not ew.has_pending_messages()
