"""Operator analytics: the numbers are right, and - far more important - the /stats gate only
ever opens for config.OPERATOR_CHAT_ID. This output carries every subscriber's email and home
address, so the auth test matters more than the arithmetic one.
"""
import os
import sys

import pytest

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))


@pytest.fixture()
def db(tmp_path, monkeypatch):
    import core.database as database
    monkeypatch.setattr(database, "DB_NAME", str(tmp_path / "a.db"))
    database.init_db()
    conn = database.get_connection()

    def sub(email):
        return conn.execute("INSERT INTO subscribers (email) VALUES (?)", (email,)).lastrowid

    def target(sid, city, ulica, broj, email=None, chat=None, active=1):
        tid = conn.execute(
            "INSERT INTO targets (subscriber_id, city, ulica, broj, email, telegram_chat_id, "
            "is_active, email_active, telegram_active) VALUES (?,?,?,?,?,?,?,1,1)",
            (sid, city, ulica, broj, email, chat, active),
        ).lastrowid
        conn.commit()
        return tid

    conn.commit()
    yield conn, sub, target
    conn.close()


def test_collect_counts_channels_and_subscribers(db):
    from core.admin_stats import collect
    conn, sub, target = db

    a = sub("a@example.com")
    b = sub("b@example.com")
    target(a, "Novi Sad", "Cankareva", "1", email="a@example.com", chat="111")   # both
    target(a, "Novi Sad", "Futoška", "2", email="a@example.com")                 # email only
    target(b, "Beograd", "Savska", "3", chat="222")                              # telegram only
    target(b, "Beograd", "Nemanjina", "4", email="b@example.com", active=0)      # paused

    s = collect(days=7)
    assert s["subscribers_total"] == 2
    assert s["targets_active"] == 3
    assert s["targets_paused"] == 1
    assert (s["ch_both"], s["ch_email_only"], s["ch_tg_only"]) == (1, 1, 1)
    assert s["ch_none"] == 0
    # a has 2 live addresses, b has 1 -> one subscriber at each count
    assert sorted(s["addr_per_sub"]) == [(1, 1), (2, 1)]
    assert s["recent"][0]["email"] == "b@example.com"


def test_format_summary_renders_without_crashing_on_empty_db(db):
    from core.admin_stats import collect, format_summary
    text = format_summary(collect(days=7))
    assert "još nema nijedne prijave" in text
    assert "ništa nije slato" in text


def test_format_summary_html_wraps_headings_and_escapes_data(db):
    from core.admin_stats import collect, format_summary
    conn, sub, target = db
    a = sub("a&b<c>@example.com")
    target(a, "Novi Sad", "Cankareva", "1", email="a&b<c>@example.com")

    html = format_summary(collect(days=7), html=True)
    assert "<b>👥 Ljudi</b>" in html
    # the only < > & in the output must be the tags we put there, never subscriber data
    assert "a&amp;b&lt;c&gt;@example.com" in html
    assert "a&b<c>@example.com" not in html

    plain = format_summary(collect(days=7), html=False)
    assert "<b>" not in plain
    assert "a&b<c>@example.com" in plain      # nothing to escape without a parse mode


@pytest.mark.parametrize("n, expected", [
    (1, "1 adresa"), (2, "2 adrese"), (4, "4 adrese"), (5, "5 adresa"),
    (11, "11 adresa"), (12, "12 adresa"), (14, "14 adresa"),
    (21, "21 adresa"), (22, "22 adrese"), (25, "25 adresa"), (0, "0 adresa"),
])
def test_serbian_plural_agreement(n, expected):
    from core.admin_stats import _adrese
    assert _adrese(n) == expected


def test_new_since_reports_only_rows_past_the_marks(db):
    from core.admin_stats import new_since, format_new
    conn, sub, target = db

    a = sub("first@example.com")
    t1 = target(a, "Niš", "Prva", "1", email="first@example.com")
    feed = new_since(0, 0)
    assert [x["email"] for x in feed["subscribers"]] == ["first@example.com"]
    assert len(feed["targets"]) == 1

    # nothing new since the high-water marks
    feed2 = new_since(feed["max_subscriber_id"], feed["max_target_id"])
    assert feed2["subscribers"] == [] and feed2["targets"] == []
    assert format_new(feed2) == ""

    b = sub("second@example.com")
    target(b, "Niš", "Druga", "2", chat="999")
    feed3 = new_since(feed["max_subscriber_id"], feed["max_target_id"])
    assert [x["email"] for x in feed3["subscribers"]] == ["second@example.com"]
    body = format_new(feed3)
    # grouped under the person, with the address and its channel underneath
    assert "👤 second@example.com (nov)" in body
    assert "📍 Niš, Druga 2 — 💬 telegram" in body


# --- the gate -------------------------------------------------------------------------------

@pytest.mark.parametrize("operator_id, chat_id, expected", [
    ("12345678", 12345678, True),      # the operator, as Telegram sends it (int)
    ("12345678", "12345678", True),    # ...and as a string
    ("12345678", 1234, False),         # prefix must NOT match
    ("12345678", 123456789, False),    # nor an extension of it
    ("1234", 12345678, False),         # nor the other way round
    ("12345678", -12345678, False),    # a group chat with a similar id
    ("", 12345678, False),             # unset OPERATOR_CHAT_ID -> nobody, ever
    (None, 12345678, False),
])
def test_is_operator_is_exact_and_fails_closed(monkeypatch, operator_id, chat_id, expected):
    import config
    import workers.telegram_bot as bot
    monkeypatch.setattr(config, "OPERATOR_CHAT_ID", operator_id, raising=False)
    assert bot.is_operator(chat_id) is expected


def test_stats_is_never_sent_to_a_non_operator(monkeypatch):
    """The dispatch path: a stranger's /stats must produce no Telegram call at all - not a
    refusal, not an 'unknown command'. Silence is the point."""
    import config
    import workers.telegram_bot as bot

    monkeypatch.setattr(config, "OPERATOR_CHAT_ID", "999", raising=False)
    sent = []
    monkeypatch.setattr(bot, "send_message", lambda cid, text: sent.append((cid, text)))

    assert bot.is_operator(1) is False
    assert sent == []
