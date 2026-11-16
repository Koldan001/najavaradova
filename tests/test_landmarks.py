"""A power notice that names one building on a street cuts that building, not the street. EPS's
Novi Sad list for 30.09.2026 said only "ЛИДЛ на Футошком путу"; the parser kept Futoški put as the
street, the whole street was drawn, and everyone registered on it would have been alerted
(#3442). Now: the building's own address when OSM finds it and RGZ confirms the number, the house
numbers when the "landmark" is really the street and a number, and otherwise nothing.
OSM, RGZ, geometry and the operator's Telegram are stubbed, so this runs in CI."""
import os
import sys
from datetime import date, timedelta
from types import SimpleNamespace

import pytest

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
os.environ.setdefault("GEMINI_API_KEY", "test-placeholder")

import core.matchmaker as mm
from parser import OutageAlert, StreetSegment

DAY = (date.today() + timedelta(days=1)).strftime("%d.%m.%Y")
HEX = "89aaaaaaaaaaaaa"
LIDL = (45.2481, 19.8155)


def _lidl(road="Футошки пут", number="48", cls="shop"):
    return SimpleNamespace(latitude=LIDL[0], longitude=LIDL[1],
                           raw={"class": cls, "type": "supermarket",
                                "address": {"road": road, "house_number": number}})


@pytest.fixture()
def osm(monkeypatch):
    """Records lookups; answers with `osm.answer`. RGZ has 46 and 48 on the street, 48 at the Lidl."""
    calls = []
    state = SimpleNamespace(answer=_lidl(), calls=calls)

    def geocode(query):
        calls.append(query)
        if isinstance(state.answer, Exception):
            raise state.answer
        return state.answer
    monkeypatch.setattr(mm, "_geocode_landmark", geocode)
    monkeypatch.setattr(mm, "rgz_house_points", lambda *a, **k: [
        ("46", LIDL[0] + 0.0004, LIDL[1]), ("48", LIDL[0] + 0.0001, LIDL[1])])
    return state


def _alert(landmark="ЛИДЛ", street="ФУТОШКИ ПУТ", **kwargs):
    return OutageAlert(city="НОВИ САД", outage_date=DAY,
                       message_template=f"Planirano isključenje struje u ulici *** dana {DAY}.",
                       affected_locations=[StreetSegment(location_type="LANDMARK", main_street=street,
                                                         landmark=landmark, time_window="10:00 - 12:00",
                                                         **kwargs)])


def test_a_building_osm_finds_becomes_its_own_address(osm):
    alert = _alert()
    assert mm._place_landmarks(alert, "POWER") == []
    [loc] = alert.affected_locations
    assert (loc.location_type, loc.main_street, loc.explicit_numbers) == ("STREET", "ФУТОШКИ ПУТ", "48")
    assert osm.calls == ["Lidl, Futoški Put, Novi Sad"]


@pytest.mark.parametrize("answer", [
    None,                                              # OSM doesn't know it
    _lidl(cls="highway"),                              # only the street came back
    _lidl(road="Булевар Европе"),                      # a Lidl on another street
    _lidl(number="120"),                               # RGZ has no 120 there
    RuntimeError("Nominatim down"),
])
def test_a_building_not_found_is_not_the_street(osm, answer):
    osm.answer = answer
    alert = _alert()
    assert mm._place_landmarks(alert, "POWER") == ["ЛИДЛ, ФУТОШКИ ПУТ"]
    [loc] = alert.affected_locations
    assert loc.main_street is None and loc.landmark == "ЛИДЛ, ФУТОШКИ ПУТ"


def test_the_street_and_a_number_written_as_a_landmark_is_that_number(osm):
    # #3003, Smederevska Palanka: landmark "ШУЛЕЈИЋЕВА 2" on Šulejićeva.
    alert = _alert(landmark="ШУЛЕЈИЋЕВА 2", street="ШУЛЕЈИЋЕВА")
    mm._place_landmarks(alert, "POWER")
    [loc] = alert.affected_locations
    assert (loc.location_type, loc.explicit_numbers) == ("STREET", "2") and osm.calls == []


def test_water_and_heating_landmarks_and_numbered_ones_are_left_alone(osm):
    for category, alert in (("WATER", _alert(landmark="ОКРЕТНИЦА", street="ПУТ ЗА ТРЕШЊУ")),
                            ("HEATING", _alert(landmark="ДУШАНА ВУКАСОВИЋА", street="ЕВРОПСКА")),
                            ("POWER", _alert(landmark="ДЕЧЈИ ВРТИЋ МАЈА", explicit_numbers="14"))):
        before = alert.model_dump()
        assert mm._place_landmarks(alert, category) == []
        assert alert.model_dump() == before
    assert osm.calls == []


@pytest.fixture()
def db(tmp_path, monkeypatch, osm):
    import core.database as database
    monkeypatch.setattr(database, "DB_NAME", str(tmp_path / "landmarks.db"))
    database.init_db()
    monkeypatch.setattr(mm, "calculate_outage_hexes", lambda *a, **k: ([HEX], "EXACT"))
    monkeypatch.setattr(mm, "_build_visual_features", lambda *a, **k: [])
    monkeypatch.setattr(mm, "query_rgz_streets", lambda *a, **k: [])
    notes = []
    monkeypatch.setattr(mm, "send_operator_note", lambda text, *a, **k: notes.append(text))
    database.notes = notes
    conn = database.get_connection()
    for number in ("12", "48"):
        tid = conn.execute(
            "INSERT INTO targets (city, ulica, broj, email, unsub_token, is_active, email_active) "
            "VALUES ('НОВИ САД', 'ФУТОШКИ ПУТ', ?, ?, ?, 1, 1)",
            (number, f"broj{number}@example.com", f"tok{number}")).lastrowid
        conn.execute("INSERT INTO target_hexes (target_id, hex_id) VALUES (?, ?)", (tid, HEX))
    conn.commit()
    conn.close()
    return database


def _told(database):
    conn = database.get_connection()
    rows = [(r[0], r[1]) for r in conn.execute(
        "SELECT t.broj, m.custom_message FROM message_queue m JOIN targets t ON t.id = m.target_id")]
    conn.close()
    return rows


def _publish(alert):
    raw = f'Datum: {DAY} | Opština: Нови Сад | Vreme: 10:00 - 12:00 | Ulice: " | ЛИДЛ на Футошком путу"'
    mm.process_and_queue_outage(alert, raw, text_hash=raw, category="POWER")


def test_only_the_lidls_own_address_is_alerted(db):
    _publish(_alert())
    [(number, message)] = _told(db)
    assert number == "48" and "Futoški Put 48" in message
    assert db.notes == []


def test_nobody_on_the_street_is_alerted_when_the_building_is_not_found(db, osm):
    osm.answer = None
    _publish(_alert())
    assert _told(db) == []
    assert len(db.notes) == 1 and "ЛИДЛ, ФУТОШКИ ПУТ" in db.notes[0]


def _with_street_line(street="ДР. МИЛАНА КОСТИЋА", settlement="СРЕМСКА МИТРОВИЦА", line_settlement=None):
    """#3813, Sremska Mitrovica 07.10.2026: "Др. Милана Костића 68-120, 9 и б.б., Атлетски стадион
    Др. Милана Костића б.б." - the stadium as a landmark, and its street on a line of its own."""
    return OutageAlert(city="СРЕМСКА МИТРОВИЦА", outage_date=DAY,
                       message_template=f"Planirano isključenje struje u ulici *** dana {DAY}.",
                       affected_locations=[
                           StreetSegment(location_type="STREET", main_street=street, number_range="68-120",
                                         settlement=line_settlement or settlement, time_window="09:00 - 11:00"),
                           StreetSegment(location_type="LANDMARK", main_street="ДР. МИЛАНА КОСТИЋА",
                                         landmark="АТЛЕТСКИ СТАДИОН", settlement=settlement,
                                         time_window="09:00 - 11:00")])


def test_no_note_when_the_buildings_street_is_listed_on_its_own(osm):
    osm.answer = None
    alert = _with_street_line()
    assert mm._place_landmarks(alert, "POWER") == []
    street, stadium = alert.affected_locations
    assert (street.main_street, street.number_range) == ("ДР. МИЛАНА КОСТИЋА", "68-120")   # untouched
    assert stadium.main_street is None and stadium.landmark == "АТЛЕТСКИ СТАДИОН, ДР. МИЛАНА КОСТИЋА"


@pytest.mark.parametrize("kwargs", [
    {"street": "ФРУШКОГОРСКА"},                       # another street is listed, not this one
    {"line_settlement": "ЛАЋАРАК"},                   # the same street name, but in another village
])
def test_the_note_stays_when_the_street_isnt_covered(osm, kwargs):
    osm.answer = None
    assert mm._place_landmarks(_with_street_line(**kwargs), "POWER") == [
        "АТЛЕТСКИ СТАДИОН, ДР. МИЛАНА КОСТИЋА"]
