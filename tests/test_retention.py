"""core.retention.prune_ephemeral() - drops spent auth artifacts, leaves everything else
(outage history, message_queue, subscribers) strictly alone.
"""
import os
import sys

import pytest

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    import core.database as database
    monkeypatch.setattr(database, "DB_NAME", str(tmp_path / "r.db"))
    database.init_db()
    c = database.get_connection()
    yield c
    c.close()


def _count(c, table):
    return c.execute("SELECT COUNT(*) FROM %s" % table).fetchone()[0]


def test_prune_drops_spent_auth_rows_keeps_fresh_and_history(conn):
    from core.retention import prune_ephemeral

    sid = conn.execute("INSERT INTO subscribers (email) VALUES ('r@example.com')").lastrowid

    # login_tokens: one used & old, one fresh & unused
    conn.execute(
        "INSERT INTO login_tokens (subscriber_id, token_hash, purpose, expires_at, used_at, created_at) "
        "VALUES (?, 'h_old', 'login', datetime('now','-1 day'), datetime('now','-9 days'), datetime('now','-9 days'))",
        (sid,),
    )
    conn.execute(
        "INSERT INTO login_tokens (subscriber_id, token_hash, purpose, expires_at, used_at, created_at) "
        "VALUES (?, 'h_new', 'login', datetime('now','+15 minutes'), NULL, datetime('now'))",
        (sid,),
    )

    # sessions: one long-expired, one live
    conn.execute(
        "INSERT INTO sessions (subscriber_id, token_hash, expires_at, created_at) "
        "VALUES (?, 's_old', datetime('now','-5 days'), datetime('now','-40 days'))",
        (sid,),
    )
    conn.execute(
        "INSERT INTO sessions (subscriber_id, token_hash, expires_at, created_at) "
        "VALUES (?, 's_new', datetime('now','+30 days'), datetime('now'))",
        (sid,),
    )

    # pending_registrations: one consumed, one old-unconsumed, one fresh-unconsumed (a live banner)
    conn.execute(
        "INSERT INTO pending_registrations (code, channel, city, ulica, broj, consumed_at, created_at) "
        "VALUES ('p_consumed','telegram','X','Y','1', datetime('now','-1 hour'), datetime('now','-1 hour'))"
    )
    conn.execute(
        "INSERT INTO pending_registrations (code, channel, city, ulica, broj, consumed_at, created_at) "
        "VALUES ('p_stale','telegram','X','Y','1', NULL, datetime('now','-3 days'))"
    )
    conn.execute(
        "INSERT INTO pending_registrations (code, channel, city, ulica, broj, consumed_at, created_at) "
        "VALUES ('p_live','telegram','X','Y','1', NULL, datetime('now'))"
    )

    # history that must survive untouched
    oid = conn.execute(
        "INSERT INTO outages (region, raw_text, raw_text_hash, outage_date, status) "
        "VALUES ('НОВИ САД','stari kvar','oldhash','01.01.2020','ACTIVE')"
    ).lastrowid
    tid = conn.execute(
        "INSERT INTO targets (subscriber_id, city, ulica, broj, email, is_active) "
        "VALUES (?, 'X','Y','1','r@example.com',1)", (sid,)
    ).lastrowid
    conn.execute(
        "INSERT INTO message_queue (outage_id, target_id, channel, status, created_at) "
        "VALUES (?, ?, 'email', 'SENT', datetime('now','-200 days'))", (oid, tid)
    )
    conn.commit()

    out = prune_ephemeral()
    assert out == {"login_tokens": 1, "sessions": 1, "pending_registrations": 2}

    assert [r[0] for r in conn.execute("SELECT token_hash FROM login_tokens")] == ["h_new"]
    assert [r[0] for r in conn.execute("SELECT token_hash FROM sessions")] == ["s_new"]
    assert sorted(r[0] for r in conn.execute("SELECT code FROM pending_registrations")) == ["p_live"]

    # nothing analytical was touched
    assert _count(conn, "outages") == 1
    assert _count(conn, "message_queue") == 1
    assert _count(conn, "targets") == 1
    assert _count(conn, "subscribers") == 1


def test_prune_is_a_noop_on_a_clean_db(conn):
    from core.retention import prune_ephemeral
    assert prune_ephemeral() == {"login_tokens": 0, "sessions": 0, "pending_registrations": 0}
