"""Ground truth for the accuracy figures: a reader's own 👍 / 👎 (core/feedback.py) and the
operator's weekly review (core/review.py). Nothing here calls Telegram or a model."""
import os
import sys
import threading
from datetime import datetime
from http.server import HTTPServer

import pytest
import requests

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
os.environ.setdefault("GEMINI_API_KEY", "test-placeholder")

TOKEN = "tok_abcdefghijklmnop"


@pytest.fixture()
def db(tmp_path, monkeypatch):
    import core.database as database
    import core.send_check as send_check
    monkeypatch.setattr(database, "DB_NAME", str(tmp_path / "fb.db"))
    database.init_db()
    told = []

    def post(method, payload):
        told.append(payload)
        return {"ok": True, "result": {"message_id": 1000 + len(told)}}
    monkeypatch.setattr(send_check, "operator_post", post)
    monkeypatch.setattr("core.review.time.sleep", lambda s: None)
    return database, told


def _alert(database, chat=999, token=TOKEN, status="SENT", street="ПРЕШЕВСКА", channel="telegram"):
    """One outage, one address, one alert row. Returns (outage_id, target_id)."""
    conn = database.get_connection()
    sid = conn.execute("INSERT INTO subscribers (email) VALUES ('a@example.com')").lastrowid
    tid = conn.execute(
        "INSERT INTO targets (subscriber_id, city, ulica, broj, email, telegram_chat_id, unsub_token, "
        "is_active, email_active, telegram_active) VALUES (?, 'Beograd (Zvezdara)', ?, '7', 'a@example.com', ?, ?, 1, 1, 1)",
        (sid, street, chat, token)).lastrowid
    oid = conn.execute(
        "INSERT INTO outages (region, raw_text, category, outage_date, parsed_json, source_url) "
        "VALUES ('ЗВЕЗДАРА', 'Без воде улица Прешевска', 'WATER', '12.11.2026', ?, 'https://example.com/n')",
        ('{"affected_locations": [{"location_type": "STREET", "main_street": "ПРЕШЕВСКА", '
         '"number_range": "1-20", "parity": "NEPARNA", "time_window": "08:00 - 14:00"}]}',)).lastrowid
    conn.execute(
        "INSERT INTO message_queue (outage_id, target_id, channel, custom_message, status, created_at, sent_at) "
        "VALUES (?, ?, ?, 'Bez vode u ulici Preševska', ?, datetime('now'), datetime('now'))",
        (oid, tid, channel, status))
    conn.commit()
    conn.close()
    return oid, tid


# ---------- a reader's 👍 / 👎 ----------

def test_only_someone_the_alert_was_sent_to_can_answer(db):
    database, told = db
    from core import feedback
    oid, _ = _alert(database)
    assert feedback.record(oid, "telegram", 12345, "WRONG") == 0          # another chat
    assert feedback.record(oid + 1, "telegram", 999, "WRONG") == 0        # another outage
    assert told == []
    assert feedback.record(oid, "telegram", 999, "RIGHT") == 1


def test_an_unsent_alert_cannot_be_answered(db):
    database, _ = db
    from core import feedback
    oid, _ = _alert(database, status="QUEUED")
    assert feedback.record(oid, "telegram", 999, "RIGHT") == 0


def test_second_tap_changes_the_answer_and_the_operator_hears_once(db):
    database, told = db
    from core import feedback
    oid, tid = _alert(database)
    feedback.record(oid, "telegram", 999, "WRONG")
    feedback.record(oid, "telegram", 999, "WRONG")
    assert len(told) == 1 and "nije tačno" in told[0]["text"] and f"#{oid}" in told[0]["text"]
    feedback.record(oid, "telegram", 999, "RIGHT")
    conn = database.get_connection()
    assert conn.execute("SELECT verdict FROM user_feedback").fetchall()[0][0] == "RIGHT"
    conn.close()


def test_email_answer_is_matched_by_the_unsubscribe_token(db):
    database, _ = db
    from core import feedback
    oid, _ = _alert(database, channel="email")
    assert feedback.record(oid, "email", "someone-elses-token", "RIGHT") == 0
    assert feedback.record(oid, "email", TOKEN, "RIGHT") == 1
    assert feedback.record(oid, "telegram", 999, "RIGHT") == 0            # sent by email only


def test_email_carries_the_links_under_each_outage():
    from workers.email_worker import FEEDBACK_QUESTION, build_message
    msg = build_message("a@example.com", TOKEN, None, parts=[(["Bez vode."], None), (["Bez struje."], None)],
                        feedback=[(11, TOKEN), (12, TOKEN)])
    plain = msg.get_body(preferencelist=("plain",)).get_content()
    html = msg.get_body(preferencelist=("html",)).get_content()
    assert plain.count(FEEDBACK_QUESTION) == 2
    assert f"tacno?o=11&t={TOKEN}&v=y" in plain and f"tacno?o=12&t={TOKEN}&v=n" in plain
    assert html.count("👍 Da") == 2
    plain_without = build_message("a@example.com", TOKEN, "Bez vode.").get_body(
        preferencelist=("plain",)).get_content()
    assert FEEDBACK_QUESTION not in plain_without


@pytest.fixture()
def server(db):
    from scripts.local_web_server import Handler
    srv = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield db[0], f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


def _answers(database):
    conn = database.get_connection()
    rows = conn.execute("SELECT verdict FROM user_feedback").fetchall()
    conn.close()
    return [r[0] for r in rows]


def test_opening_the_email_link_records_nothing_the_button_does(server):
    database, base = server
    oid, _ = _alert(database, channel="email")
    page = requests.get(f"{base}/tacno?o={oid}&t={TOKEN}&v=n", timeout=5)
    assert page.status_code == 200 and "Nije tačno" in page.text
    assert _answers(database) == []                       # a mail scanner opening it changes nothing
    done = requests.post(f"{base}/tacno?o={oid}&t={TOKEN}&v=n", timeout=5)
    assert "Zabeleženo" in done.text
    assert _answers(database) == ["WRONG"]


def test_a_made_up_link_stores_nothing(server):
    database, base = server
    oid, _ = _alert(database, channel="email")
    r = requests.post(f"{base}/tacno?o={oid}&t=not-the-right-token&v=y", timeout=5)
    assert "nije važeći" in r.text
    r = requests.get(f"{base}/tacno?o=x&t=<script>&v=y", timeout=5)
    assert "nije važeći" in r.text
    assert _answers(database) == []


# ---------- the weekly review ----------

def test_due_from_monday_nine_once_a_week(db):
    from core import review
    monday_early = datetime(2026, 11, 16, 8, 30, tzinfo=review.TZ)
    monday = datetime(2026, 11, 16, 9, 5, tzinfo=review.TZ)
    wednesday = datetime(2026, 11, 18, 14, 0, tzinfo=review.TZ)
    assert not review.due(monday_early)
    assert review.due(monday) and review.due(wednesday)   # a server down on Monday catches up
    conn = db[0].get_connection()
    conn.execute("INSERT INTO review_batches (batch) VALUES (?)", (review.week_id(monday),))
    conn.commit()
    conn.close()
    assert not review.due(wednesday)


def _batch(db):
    database, told = db
    from core import review
    oid, tid = _alert(database)
    conn = database.get_connection()
    conn.execute("INSERT INTO alert_checks (outage_id, target_id, verdict) VALUES (?, ?, 'DISAGREE')", (oid, tid))
    conn.commit()
    conn.close()
    assert review.send_batch("2026-W47") == 2              # the alert, and its notice
    assert review.send_batch("2026-W47") == 0              # never twice
    return database, told, oid, tid


def test_review_sends_alerts_and_notices_with_buttons(db):
    database, told, oid, tid = _batch(db)
    header, alert, notice = told
    assert "Nedeljna provera" in header["text"]
    assert "Adresa: ПРЕШЕВСКА 7" in alert["text"] and "nasumično" in alert["text"]
    assert [b["callback_data"][:6] for b in alert["reply_markup"]["inline_keyboard"][0]] == ["rv:ok:", "rv:no:"]
    assert "1. Preševska 1-20 (neparni)" in notice["text"]
    assert notice["reply_markup"]["inline_keyboard"][-1][0]["text"] == "Ništa ne fali"
    conn = database.get_connection()
    row = conn.execute("SELECT picked, shadow, message_id FROM review_items WHERE kind = 'alert'").fetchone()
    conn.close()
    # Picked at random first: a random pick stays random even when the second check disagreed,
    # so the random sample isn't skewed - and its DISAGREE is still kept for calibration.
    assert (row["picked"], row["shadow"], row["message_id"]) == ("random", "DISAGREE", 1002)


def test_answers_and_figures(db):
    database, told, oid, tid = _batch(db)
    from core import review
    conn = database.get_connection()
    alert_id = conn.execute("SELECT id FROM review_items WHERE kind = 'alert'").fetchone()[0]
    street_id = conn.execute("SELECT id FROM review_items WHERE kind = 'street'").fetchone()[0]
    conn.close()

    answer, keyboard = review.decide(alert_id, "no")
    assert answer == "Zabeleženo: pogrešno."
    assert keyboard["inline_keyboard"][0][1]["text"] == "• ❌ Pogrešno •"
    review.decide(street_id, "ok")
    n, keyboard = review.missing_reply(1003, "Hektorovićeva\n- Ustanička 12\n")
    assert n == 2 and keyboard["inline_keyboard"][-1][0]["text"] == "Fali: 2 (iz odgovora)"
    assert review.missing_reply(999999, "x") is None       # a reply to something else

    q = review.quality()
    assert (q["random_right"], q["random_wrong"]) == (0, 1)
    assert q["DISAGREE_WRONG"] == 1 and q["street_right"] == 1 and q["missing"] == 2
    lines = "\n".join(review.format_quality(q))
    assert "obaveštenja tačna (nasumični uzorak): 0% (0/1" in lines
    assert "mesta iz objave pronađena: 33% (1/3" in lines        # 1 found, 2 missed
    assert "'ne slaže se' zaista greška: 100% (1/1" in lines


def test_a_small_sample_is_shown_with_its_range():
    from core.review import wilson
    lo, hi = wilson(9, 10)
    assert 0.55 < lo < 0.6 and 0.98 < hi <= 1.0
    lo, hi = wilson(90, 100)
    assert 0.82 < lo and hi < 0.95


def test_places_reads_like_the_notice():
    from core.review import places
    parsed = ('{"affected_locations": ['
              '{"location_type": "ENTIRE_SETTLEMENT", "settlement": "БЕГЕЧ", "scope": "PART"},'
              '{"location_type": "VECTOR", "main_street": "ФУТОШКА", "intersection_start": "ПАСТЕРОВА",'
              ' "intersection_end": "ХАЈДУК ВЕЉКОВА", "includes_surrounding_streets": true},'
              '{"location_type": "STREET", "main_street": "ФУТОШКА", "explicit_numbers": "41, 43"}]}')
    assert places(parsed) == ["Begeč (deo)", "Futoška od Pasterova do Hajduk Veljkova + okolne ulice",
                              "Futoška br. 41, 43"]


def test_tapping_thumbs_down_marks_it_and_keeps_the_copy_button(db, monkeypatch):
    database, _ = db
    import workers.telegram_bot as bot
    oid, _ = _alert(database)
    calls = []
    monkeypatch.setattr(bot.requests, "post", lambda url, json=None, timeout=None: calls.append((url, json)))
    copy_row = [{"text": "📤 Pošalji komšijama", "copy_text": {"text": "x"}}]
    cb = {"id": "1", "data": f"fb:n:{oid}", "from": {"id": 999},
          "message": {"message_id": 5, "chat": {"id": 999},
                      "reply_markup": {"inline_keyboard": [copy_row, [{"text": "👍 Tačno", "callback_data": f"fb:y:{oid}"},
                                                                       {"text": "👎 Nije tačno", "callback_data": f"fb:n:{oid}"}]]}}}
    bot.handle_callback(cb)
    edit = next(j for u, j in calls if u.endswith("editMessageReplyMarkup"))
    assert edit["reply_markup"]["inline_keyboard"][0] == copy_row
    assert edit["reply_markup"]["inline_keyboard"][1][1]["text"] == "• 👎 Nije tačno •"
    toast = next(j for u, j in calls if u.endswith("answerCallbackQuery"))
    assert "proverićemo" in toast["text"]
    calls.clear()
    bot.handle_callback({**cb, "from": {"id": 4242}})          # someone it wasn't sent to
    assert not any(u.endswith("editMessageReplyMarkup") for u, _ in calls)


def test_review_shows_the_reviewed_street_of_a_long_notice():
    """#6758: an EPS list of 40+ streets was cut at 1,500 characters, before "КРАЉА УРОША
    ПРВОГ", so a correct alert looked wrong in the review."""
    from core.review import TEXT_MAX, notice_excerpt
    filler = ", ".join(f"КРАЉА МИХАИЛА {i}: 2-{i}" for i in range(120))
    raw = f"Датум: 06.11.2026 | Општина: Земун | Улице: {filler}, КРАЉА УРОША ПРВОГ: 2-16Г, ЛАЗЕ КОСТИЋА: 1-9"
    assert len(raw) > TEXT_MAX
    out = notice_excerpt(raw, "Kralja Uroša Prvog")
    assert "»КРАЉА УРОША ПРВОГ«: 2-16Г" in out
    assert out.startswith("Датум: 06.11.2026")            # the opening still shows the date
    assert len(out) < TEXT_MAX + 1000
    # spelled differently in the notice: the rarest word still finds it
    assert "»УРОША«" in notice_excerpt(raw.replace("УРОША ПРВОГ", "УРОША I"), "Kralja Uroša Prvog")
    # a short notice is shown whole, unmarked
    assert notice_excerpt("Улице: ЛАЗЕ КОСТИЋА 1-9", "Laze Kostića") == "Улице: ЛАЗЕ КОСТИЋА 1-9"
