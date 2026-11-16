"""A toplana's works notice that says nothing is cut. Beogradske elektrane list "Planirani radovi
na toplovodnoj mreži" again every working day, with no hours and no end. One address on Braće
Jerković was mailed 14 times from 08.09. to 30.09.2026, each time in the model's words:
"Planirano isključenje toplovoda u ulici Braće Jerković dana 30.09.2026. u periodu 07:00 - 19:00."
(#3459). Now: what the notice says, no hours; the first two days of a run day by day, the third
"do daljnjeg", then quiet unless the notice says something new (an end, a different end).
Geometry, RGZ and the operator's Telegram are stubbed, so this runs in CI."""
import os
import sys
from datetime import date, timedelta

import pytest

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
os.environ.setdefault("GEMINI_API_KEY", "test-placeholder")

import core.matchmaker as mm
from parser import OutageAlert, StreetSegment

TOMORROW = date.today() + timedelta(days=1)
HEX = "89aaaaaaaaaaaaa"
STREET = "БРАЋЕ ЈЕРКОВИЋ"
EFFECT = "Moguć prekid grejanja ili tople vode."


@pytest.fixture()
def db(tmp_path, monkeypatch):
    import core.database as database
    monkeypatch.setattr(database, "DB_NAME", str(tmp_path / "works.db"))
    database.init_db()
    monkeypatch.setattr(mm, "calculate_outage_hexes", lambda *a, **k: ([HEX], "EXACT"))
    monkeypatch.setattr(mm, "_build_visual_features", lambda *a, **k: [])
    monkeypatch.setattr(mm, "query_rgz_streets", lambda *a, **k: [])
    monkeypatch.setattr(mm, "send_operator_note", lambda *a, **k: None)
    return database


def _target(database, street=STREET, email="stanar@example.com", city="БЕОГРАД"):
    conn = database.get_connection()
    tid = conn.execute(
        "INSERT INTO targets (city, ulica, broj, email, unsub_token, is_active, email_active, telegram_active) "
        "VALUES (?, ?, '1', ?, ?, 1, 1, 1)", (city, street, email, f"tok-{email}")).lastrowid
    conn.execute("INSERT INTO target_hexes (target_id, hex_id) VALUES (?, ?)", (tid, HEX))
    conn.commit()
    conn.close()
    return tid


def _day(offset):
    return TOMORROW + timedelta(days=offset)


def _d(offset):
    return _day(offset).strftime("%d.%m.%Y")


def _works(offset, street=STREET, city="БЕОГРАД", raw=None):
    """One day's copy of the list, as the model reads it: the rule-8 hours included."""
    d = _d(offset)
    alert = OutageAlert(city=city, outage_date=d,
                        message_template=f"Planirano isključenje toplovoda u ulici *** dana {d}. u periodu 07:00 - 19:00.",
                        affected_locations=[StreetSegment(location_type="STREET", main_street=street,
                                                          time_window="07:00 - 19:00")])
    raw = raw or (f"Beograd, opština Voždovac. Planirani radovi na toplovodnoj mreži ({d}).\n"
                  f"- Radovi na izgradnji toplovodne mreže u Mitrovdanskoj i Ulici braće Jerković.")
    mm.process_and_queue_outage(alert, raw, text_hash=raw + city + street + d, category="HEATING")


def _nis_works(offset, end_offset):
    start, end = _d(offset), _d(end_offset)
    alert = OutageAlert(city="НИШ", outage_date=start, end_date=end, message_template="",
                        affected_locations=[StreetSegment(location_type="STREET", main_street="ХИЛАНДАРСКА",
                                                          time_window="07:00 - 19:00")])
    raw = (f"Obaveštavamo vas da će dana {start} početi radovi na toplovodu u ulici Hilandarskoj. "
           f"Biće obustavljen saobraćaj motornih vozila. Predviđa se da će radovi trajati do {end}.")
    mm.process_and_queue_outage(alert, raw, text_hash=raw, category="HEATING")


def _messages(database):
    conn = database.get_connection()
    rows = [r[0] for r in conn.execute("SELECT custom_message FROM message_queue ORDER BY id")]
    conn.close()
    return rows


def _mark_all_sent(database):
    conn = database.get_connection()
    conn.execute("UPDATE message_queue SET status = 'SENT', sent_at = CURRENT_TIMESTAMP WHERE status = 'QUEUED'")
    conn.commit()
    conn.close()


def _run(database, *offsets, **kwargs):
    for offset in offsets:
        _works(offset, **kwargs)
        _mark_all_sent(database)


DALJE = "Za ove radove više ne javljamo, osim ako najave prekid ili kraj."


def test_the_first_post_says_what_the_notice_says(db):
    _target(db)
    _works(0)
    assert _messages(db) == [f"Radovi na toplovodnoj mreži u ulici Braće Jerković dana {_d(0)}. {EFFECT}"]
    conn = db.get_connection()
    [(window,)] = conn.execute("SELECT time_window FROM outages").fetchall()
    conn.close()
    assert "07:00" not in window                                     # no made-up hours on the map


def test_two_days_by_day_the_third_until_further_notice_then_quiet(db):
    _target(db)
    _run(db, 0, 1, 4, 5, 6)                                          # a weekend after the second
    assert _messages(db) == [
        f"Radovi na toplovodnoj mreži u ulici Braće Jerković dana {_d(0)}. {EFFECT}",
        f"Radovi na toplovodnoj mreži u ulici Braće Jerković dana {_d(1)}. {EFFECT}",
        f"Radovi na toplovodnoj mreži u ulici Braće Jerković traju od {_d(0)} do daljnjeg. {EFFECT} {DALJE}",
    ]


def test_the_street_spelt_another_way_is_the_same_run(db):
    _target(db, street="БРАЋЕ ЈЕРКОВИЋА")
    for offset, spelling in enumerate(["БРАЋЕ ЈЕРКОВИЋ", "БРАЋЕ ЈЕРКОВИЋА"] * 3):
        _run(db, offset, street=spelling)
    messages = _messages(db)
    assert len(messages) == 3 and "do daljnjeg" in messages[2]


def test_works_back_after_more_than_a_week_start_a_new_run(db):
    _target(db)
    _run(db, 0, 1, 2, 12)
    messages = _messages(db)
    assert len(messages) == 4 and messages[3].endswith(f"dana {_d(12)}. {EFFECT}")


def test_a_cut_on_the_same_street_is_always_sent(db):
    _target(db)
    _run(db, 0, 1, 2)
    d = _d(3)
    alert = OutageAlert(city="БЕОГРАД", outage_date=d, message_template="Bez tople vode u ulici ***.",
                        affected_locations=[StreetSegment(location_type="STREET", main_street=STREET,
                                                          time_window="08:00 - 14:00")])
    raw = f"Prekid u isporuci toplotne energije za pripremu potrošne tople vode, {d} od 08:00 do 14:00: Braće Jerković"
    mm.process_and_queue_outage(alert, raw, text_hash=raw, category="HEATING")
    cut = _messages(db)[-1]
    assert len(_messages(db)) == 4
    assert cut.startswith("Bez tople vode u ulici Braće Jerković") and "08:00 - 14:00" in cut


def test_someone_told_in_the_old_words_hears_until_further_notice(db):
    # Target 40: fourteen of the model's messages, then this goes live mid-run.
    _run(db, 0, 1, 2)
    tid = _target(db)
    conn = db.get_connection()
    [(last,)] = conn.execute("SELECT MAX(id) FROM outages").fetchall()
    conn.execute("INSERT INTO message_queue (outage_id, target_id, channel, custom_message, status) "
                 "VALUES (?, ?, 'email', 'Planirano isključenje toplovoda u ulici Braće Jerković "
                 "u periodu 07:00 - 19:00.', 'SENT')", (last, tid))
    conn.commit()
    conn.close()
    _run(db, 3, 4)
    old, fixed = _messages(db)
    assert fixed.startswith(f"Radovi na toplovodnoj mreži u ulici Braće Jerković traju od {_d(0)} do daljnjeg.")


def test_a_signup_during_a_long_run_hears_until_further_notice(db):
    _run(db, 0, 1, 2)
    conn = db.get_connection()                                       # earlier days are over
    conn.execute("UPDATE outages SET expires_at = datetime('now', '-1 hour') WHERE outage_date != ?", (_d(2),))
    conn.commit()
    conn.close()
    tid = _target(db)
    mm.backfill_target_against_active_outages(tid)
    [message] = _messages(db)
    assert message.startswith(f"Radovi na toplovodnoj mreži u ulici Braće Jerković traju od {_d(0)} do daljnjeg.")


def test_works_that_state_their_end_say_it_and_an_extension_is_told(db):
    # Niš toplana: "Predviđa se da će radovi trajati do ..." - the end is the notice's own.
    _target(db, street="ХИЛАНДАРСКА", city="НИШ")
    _nis_works(0, 20)
    _mark_all_sent(db)
    _nis_works(1, 20)                                                # the same notice again
    _nis_works(2, 30)                                                # extended
    first, extended = _messages(db)
    assert first == f"Radovi na toplovodnoj mreži u ulici Hilandarska od {_d(0)} do {_d(20)}. {EFFECT}"
    assert extended == f"Radovi na toplovodnoj mreži u ulici Hilandarska od {_d(0)} do {_d(30)}. {EFFECT}"


def test_an_end_announced_after_until_further_notice_is_told(db):
    _target(db)
    _run(db, 0, 1, 2, 3)
    d, end = _d(4), _d(10)
    alert = OutageAlert(city="БЕОГРАД", outage_date=d, end_date=end, message_template="",
                        affected_locations=[StreetSegment(location_type="STREET", main_street=STREET,
                                                          time_window="")])
    raw = f"Planirani radovi na toplovodnoj mreži ({d}). - Radovi u Ulici braće Jerković trajaće do {end}."
    mm.process_and_queue_outage(alert, raw, text_hash=raw, category="HEATING")
    messages = _messages(db)
    assert len(messages) == 4 and messages[3].endswith(f"od {_d(0)} do {end}. {EFFECT}")


def test_the_same_street_name_in_another_city_is_its_own_run(db):
    _target(db)
    _target(db, email="ns@example.com", city="НОВИ САД")
    _run(db, 0, 1, city="НОВИ САД", raw="Novi Sad. Radovi na izgradnji toplovodne mreže u Ulici braće Jerković.")
    _run(db, 2)
    assert _messages(db)[-1].endswith(f"dana {_d(2)}. {EFFECT}")     # Beograd's first post, not the third
