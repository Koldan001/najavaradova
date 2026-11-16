"""A second look before a risky alert goes out (core/send_check.py). An alert through a whole
settlement is held until our rule and a second model call agree; when they don't, the operator
gets the notice's link and decides - and without an answer nothing is sent. A date the notice
doesn't state holds every alert of the outage. Streets named in the notice are never held."""
import os
import sys
from datetime import date, timedelta

import pytest

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
os.environ.setdefault("GEMINI_API_KEY", "test-placeholder")

import core.matchmaker as mm
import parser
from core import send_check
from parser import OutageAlert, StreetSegment

DAY = (date.today() + timedelta(days=1)).strftime("%d.%m.%Y")
HEX = "891e2e6d82bffff"
URL = "https://www.vikns.rs/najava-radova-sr/servisne-informacije/"


@pytest.fixture()
def db(tmp_path, monkeypatch):
    import core.database as database
    monkeypatch.setattr(database, "DB_NAME", str(tmp_path / "check.db"))
    database.init_db()
    monkeypatch.setattr(mm, "calculate_outage_hexes", lambda *a, **k: ([HEX], "EXACT"))
    monkeypatch.setattr(mm, "_build_visual_features", lambda *a, **k: [])
    monkeypatch.setattr(mm, "query_rgz_streets", lambda *a, **k: [])
    monkeypatch.setattr(mm, "send_operator_note", lambda *a, **k: None)
    asked = []
    monkeypatch.setattr(send_check, "_operator_post", lambda method, payload: asked.append(payload))
    database.asked = asked
    return database


def _target(database, city, street="ЗАХУМСКА", email="a@example.com"):
    conn = database.get_connection()
    tid = conn.execute(
        "INSERT INTO targets (city, opstina, ulica, broj, email, unsub_token, is_active, email_active) "
        "VALUES (?, 'НОВИ САД', ?, '1', ?, ?, 1, 1)", (city, street, email, f"t-{email}")).lastrowid
    conn.execute("INSERT INTO target_hexes (target_id, hex_id) VALUES (?, ?)", (tid, HEX))
    conn.commit()
    conn.close()


def _publish(raw, locations):
    alert = OutageAlert(city="НОВИ САД", outage_date=DAY, message_template="Bez vode u ulici ***.",
                        affected_locations=locations)
    mm.process_and_queue_outage(alert, raw, text_hash=raw, category="WATER", source_url=URL)


def _village(name, **kw):
    return StreetSegment(location_type="ENTIRE_SETTLEMENT", settlement=name, time_window="08:00 - 14:00", **kw)


def _statuses(database):
    conn = database.get_connection()
    rows = [r[0] for r in conn.execute("SELECT status FROM message_queue ORDER BY id")]
    conn.close()
    return rows


def test_a_whole_village_goes_out_when_both_checks_agree(db, monkeypatch):
    monkeypatch.setattr(parser, "check_place_scope", lambda raw, places: {"БЕГЕЧ": "WHOLE"})
    _target(db, "БЕГЕЧ")
    _publish(f"Датум: {DAY}. Без воде ће бити насеље Бегеч.", [_village("БЕГЕЧ")])
    assert _statuses(db) == ["QUEUED"] and not db.asked


def test_a_disagreement_is_held_and_the_operator_gets_the_link(db, monkeypatch):
    monkeypatch.setattr(parser, "check_place_scope", lambda raw, places: {"БЕГЕЧ": "SPOT"})
    _target(db, "БЕГЕЧ")
    _publish(f"Датум: {DAY}. Без воде ће бити насеље Бегеч.", [_village("БЕГЕЧ")])
    assert _statuses(db) == ["HELD"]
    [ask] = db.asked
    assert URL in ask["text"] and "Бегеч" in ask["text"]
    assert [b["callback_data"] for b in ask["reply_markup"]["inline_keyboard"][0]] == ["sc:ok:1", "sc:no:1"]

    send_check.run_pending()                                     # no answer: still nothing sent,
    assert _statuses(db) == ["HELD"] and len(db.asked) == 1      # and not asked twice
    assert send_check.decide(1, approve=True) == 1
    assert _statuses(db) == ["QUEUED"]
    assert send_check.decide(1, approve=False) is None           # already decided


def test_no_second_opinion_means_held_not_sent(db):
    _target(db, "БЕГЕЧ")                                         # conftest: the model answers nothing
    _publish(f"Датум: {DAY}. Без воде ће бити насеље Бегеч.", [_village("БЕГЕЧ")])
    assert _statuses(db) == ["HELD"] and len(db.asked) == 1
    assert send_check.decide(1, approve=False) == 1
    assert _statuses(db) == ["SKIPPED"]


def test_a_named_street_is_never_held(db):
    _target(db, "НОВИ САД")
    _publish(f"Датум: {DAY}. Без воде Захумска.",
             [StreetSegment(location_type="STREET", main_street="ЗАХУМСКА", time_window="08:00 - 14:00")])
    assert _statuses(db) == ["QUEUED"] and not db.asked


def test_a_date_the_notice_doesnt_state_holds_everything(db):
    _target(db, "НОВИ САД")
    _publish("Датум: 01.01.2020. Без воде Захумска.",
             [StreetSegment(location_type="STREET", main_street="ЗАХУМСКА", time_window="08:00 - 14:00")])
    assert _statuses(db) == ["HELD"]
    assert "datum" in db.asked[0]["text"]


@pytest.mark.parametrize("text, held", [
    ("21.-26.09.2026. Редовно испирање", False),       # BVK's nightly flushing: a range
    ("Радови 22.09. и 23.09.", False),
    ("Радови 25.10.2026.", True),
    ("Радови у току.", False),                          # no date of its own: nothing to compare
    # the month spelled out (Niš, #1979, #6122)
    ("Обавештење за дан 22.09.2026. У среду 23. септембра планира се ископ", False),
    ("Обавештење за дан 22.09.2026. У среду 28- ог октобра радови", True),
    ("U sredu 23. septembra radovi", False),
    ("Обавештење за дан 22.09.2026. У среду 23 – ог септембра", False),       # #4077's dash
])
def test_dates_the_notice_states(text, held):
    assert mm._date_not_in_notice("23.09.2026", text) is held


def test_a_later_signup_gets_the_decision_already_made(db, monkeypatch):
    monkeypatch.setattr(parser, "check_place_scope", lambda raw, places: {"БЕГЕЧ": "WHOLE"})
    _target(db, "БЕГЕЧ")
    _publish(f"Датум: {DAY}. Без воде ће бити насеље Бегеч.", [_village("БЕГЕЧ")])
    conn = db.get_connection()
    status = mm._send_status(conn.cursor(), 1, area=True)
    conn.close()
    assert status == "QUEUED"
