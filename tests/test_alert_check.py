"""The second check (core/alert_check.py): another model's reading of the notice, compared with
each person alerted. Shadow mode - it only records a verdict, never holds or sends anything."""
import json
import os
import sys

import pytest

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

STREET = "ПРЕШЕВСКА"


def _alert(street=STREET, date="17.09.2026", end=None, numbers=None):
    return {"event_type": "voda", "city": "ЗВЕЗДАРА", "outage_date": date, "end_date": end,
            "message_template": "Planirano isključenje vode u ulici *** dana 17.09.2026.",
            "affected_locations": [{"location_type": "STREET", "main_street": street, "parity": "ALL",
                                    "number_range": numbers, "time_window": "08:00 - 22:00"}],
            "excluded_locations": []}


@pytest.fixture()
def db(tmp_path, monkeypatch):
    import core.alert_check as alert_check
    import core.database as database
    import parser
    monkeypatch.setattr(database, "DB_NAME", str(tmp_path / "check.db"))
    database.init_db()
    monkeypatch.setattr(alert_check, "remaining", lambda *a: 99)
    calls = []

    def stub(second):
        def parse(raw_text, max_retries=4, model=None, image=None, budget=None):
            calls.append(model)
            return second
        monkeypatch.setattr(parser, "parse_announcement", parse)
    return database, stub, calls


def _setup(database, stored, people):
    """One outage stored with `stored`, and a SENT alert to each (street, number, message)."""
    conn = database.get_connection()
    oid = conn.execute("INSERT INTO outages (region, raw_text, parsed_json, outage_date) VALUES (?, ?, ?, ?)",
                       ("ЗВЕЗДАРА", "notice text", json.dumps(stored), stored["outage_date"])).lastrowid
    ids = []
    for street, number, message in people:
        tid = conn.execute("INSERT INTO targets (city, ulica, broj, email) VALUES ('Beograd (Zvezdara)', ?, ?, 'a@b.c')",
                           (street, number)).lastrowid
        conn.execute("INSERT INTO message_queue (outage_id, target_id, channel, custom_message, status, created_at) "
                     "VALUES (?, ?, 'email', ?, 'SENT', datetime('now'))", (oid, tid, message))
        ids.append(tid)
    conn.commit()
    conn.close()
    return oid, ids


def _verdicts(database):
    conn = database.get_connection()
    rows = conn.execute("SELECT target_id, verdict, reason FROM alert_checks ORDER BY target_id").fetchall()
    conn.close()
    return [(r["target_id"], r["verdict"], r["reason"]) for r in rows]


def test_same_street_and_day_agrees(db):
    database, stub, calls = db
    from core import alert_check
    stub(_alert())
    _, (tid,) = _setup(database, _alert(), [("Preševska", "71", "Planirano isključenje vode ...")])
    assert alert_check.run_pending() == 1
    assert _verdicts(database) == [(tid, "AGREE", None)]
    assert len(calls) == 1


def test_street_the_second_reading_does_not_name_disagrees(db):
    database, stub, _ = db
    from core import alert_check
    stub(_alert(street="ХЕКТОРОВИЋЕВА"))
    _setup(database, _alert(), [("Preševska", "71", "msg")])
    alert_check.run_pending()
    [(_, verdict, reason)] = _verdicts(database)
    assert verdict == "DISAGREE" and "street" in reason


def test_house_number_outside_the_second_readings_range_disagrees(db):
    database, stub, _ = db
    from core import alert_check
    stub(_alert(numbers="1-20"))
    _setup(database, _alert(), [("Preševska", "71", "msg")])
    alert_check.run_pending()
    assert _verdicts(database)[0][1] == "DISAGREE"


def test_another_day_disagrees(db):
    """The 30.10.2026 Voždovac case: the alert's day was not the notice's."""
    database, stub, _ = db
    from core import alert_check
    stub(_alert(date="04.11.2026"))
    _setup(database, _alert(date="02.11.2026"), [("Preševska", "71", "msg")])
    alert_check.run_pending()
    [(_, verdict, reason)] = _verdicts(database)
    assert verdict == "DISAGREE" and "02.11.2026" in reason


def test_a_night_window_may_end_the_next_morning(db):
    database, stub, _ = db
    from core import alert_check
    stub(_alert(date="04.11.2026"))
    _setup(database, _alert(date="05.11.2026"), [("Preševska", "71", "msg")])
    alert_check.run_pending()
    assert _verdicts(database)[0][1] == "AGREE"


def test_nearby_alert_is_not_judged_and_costs_no_call(db):
    database, stub, calls = db
    from core import alert_check
    stub(_alert())
    _setup(database, _alert(), [("Hadži-prodanova", "15", "💧 Moguć prekid vode u blizini. Kvar ...")])
    alert_check.run_pending()
    assert _verdicts(database)[0][1] == "NEARBY"
    assert calls == []


def test_one_call_per_outage_and_never_twice(db):
    database, stub, calls = db
    from core import alert_check
    stub(_alert())
    _setup(database, _alert(), [("Preševska", "71", "msg"), ("Preševska", "73", "msg")])
    alert_check.run_pending()
    alert_check.run_pending()
    assert len(calls) == 1
    assert [v for _, v, _ in _verdicts(database)] == ["AGREE", "AGREE"]


def test_no_answer_is_recorded_as_error(db):
    database, stub, _ = db
    from core import alert_check
    stub(None)
    _setup(database, _alert(), [("Preševska", "71", "msg")])
    alert_check.run_pending()
    assert _verdicts(database)[0][1] == "ERROR"


def test_stats_show_the_agreement_rate(db):
    database, stub, _ = db
    from core import admin_stats, alert_check
    stub(_alert())
    oid, _ = _setup(database, _alert(), [("Preševska", "71", "msg"), ("Hektorovićeva", "3", "msg")])
    alert_check.run_pending()
    text = admin_stats.format_summary(admin_stats.collect())
    assert f"druga provera: 1 od 2 se slaže (50%) · ne slaže se: #{oid}" in text


def test_trying_a_model_uses_it_and_writes_nothing(db):
    """scripts/try_model.py: a candidate model's verdicts, without touching alert_checks."""
    database, stub, calls = db
    from core import alert_check
    stub(_alert())
    oid, (tid,) = _setup(database, _alert(), [("Preševska", "71", "msg")])
    rows = alert_check.judge_outage(oid, [tid], model="candidate-model", tries=1)
    assert [(r[2], r[4]) for r in rows] == [("AGREE", "candidate-model")]
    assert calls == ["candidate-model"]
    assert _verdicts(database) == []
