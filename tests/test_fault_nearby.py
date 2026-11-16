"""A fault at one named house (BVK kvarovi, 26.10.2026): BVK says the listed AND SURROUNDING
streets are without water. Subscribers within the circle round the house - and not told about it
for their own address - hear it MAY reach them; the map rings the house. RGZ is stubbed."""
import json
import os
import sqlite3
import sys

import h3
import pytest

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
os.environ.setdefault("GEMINI_API_KEY", "test-placeholder")

import core.matchmaker as mm
from parser import OutageAlert, StreetSegment

FAULT = (44.80900, 20.39900)          # Omladinskih brigada 96, Novi Beograd
M_LAT = 1 / 111320                    # degrees of latitude per metre


def _north(metres):
    return FAULT[0] + metres * M_LAT, FAULT[1]


def _alert():
    return OutageAlert(city="НОВИ БЕОГРАД", outage_date="26.10.2026", message_template="x",
                       affected_locations=[StreetSegment(location_type="STREET", main_street="ОМЛАДИНСКИХ БРИГАДА",
                                                         explicit_numbers="96", time_window="07:00 - 15:00")])


@pytest.fixture()
def db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE targets (id INTEGER PRIMARY KEY, ulica TEXT, broj TEXT, telegram_chat_id TEXT, email TEXT,
                              email_active INTEGER DEFAULT 1, city TEXT, opstina TEXT, naselje TEXT,
                              lat REAL, lng REAL, is_active INTEGER DEFAULT 1);
        CREATE TABLE target_hexes (target_id INTEGER, hex_id TEXT);
        CREATE TABLE message_queue (id INTEGER PRIMARY KEY, outage_id INTEGER, target_id INTEGER, channel TEXT,
                                    custom_message TEXT, status TEXT, created_at TEXT);
        CREATE TABLE send_checks (outage_id INTEGER, reason TEXT, status TEXT);
    """)

    def add(tid, point, told=False):
        conn.execute("INSERT INTO targets (id, ulica, broj, email, city, lat, lng) VALUES (?, 'X', '1', 'a@b.c', 'Beograd', ?, ?)",
                     (tid, *point))
        conn.execute("INSERT INTO target_hexes VALUES (?, ?)", (tid, h3.latlng_to_cell(*point, 9)))
        if told:
            conn.execute("INSERT INTO message_queue (outage_id, target_id, channel, status) VALUES (1, ?, 'email', 'SENT')", (tid,))
    return conn, add


def test_only_untold_subscribers_inside_the_circle_hear_it(db):
    conn, add = db
    add(1, _north(150))               # neighbour, 150 m: told it may reach them
    add(2, _north(400))               # 400 m: outside the circle
    add(3, _north(20), told=True)     # the listed house itself, already told: no second message
    centres = [("Omladinskih Brigada 96", *FAULT)]
    assert mm._queue_nearby(conn, 1, _alert(), centres, 200, "BVK") == 1
    rows = conn.execute("SELECT target_id, custom_message, status FROM message_queue WHERE target_id != 3").fetchall()
    assert [r["target_id"] for r in rows] == [1]
    msg = rows[0]["custom_message"]
    assert msg.startswith(mm.NEARBY_PREFIX)
    assert "Omladinskih Brigada 96, oko 150 m od tvoje adrese" in msg
    assert "BVK navodi da su bez vode navedene i okolne ulice" in msg and msg.endswith("otklonjen do 15:00.")
    assert rows[0]["status"] == "QUEUED"
    # A second read of the same fault tells nobody twice.
    assert mm._queue_nearby(conn, 1, _alert(), centres, 200, "BVK") == 0


def test_an_address_that_cant_be_placed_is_left_out(db, monkeypatch):
    conn, add = db
    add(1, _north(100))
    conn.execute("UPDATE targets SET lat = NULL, lng = NULL")
    monkeypatch.setattr(mm, "rgz_house_points", lambda *a, **k: [])
    assert mm._queue_nearby(conn, 1, _alert(), [("Omladinskih Brigada 96", *FAULT)], 200, "BVK") == 0


def test_fault_centre_is_the_middle_of_the_houses_entrances(monkeypatch):
    # 96 is three entrances in RGZ (96М, 96Р, 96Ш); "– Бежанијска коса" is no RGZ naselje.
    houses = [("96М", 44.8090, 20.3990), ("96Ш", 44.8092, 20.3994), ("98", 44.9, 20.5)]
    monkeypatch.setattr(mm, "rgz_house_points", lambda street, settlement=None, parent_region=None:
                        [] if settlement else houses)
    alert = _alert()
    alert.affected_locations[0].settlement = "БЕЖАНИЈСКА КОСА"
    (label, lat, lon), = mm._fault_centres(alert, "НОВИ БЕОГРАД")
    assert label == "Omladinskih Brigada 96"
    assert lat == pytest.approx(44.8091) and lon == pytest.approx(20.3992)
    feat, = mm._nearby_features([(label, lat, lon)], 200)
    assert feat["properties"] == {"nearby_m": 200, "label": "Kvar: Omladinskih Brigada 96 i okolne ulice"}


def test_an_end_stated_as_do_hh_mm_is_not_repeated_as_a_window():
    msg = "Kvar na vodovodnoj mreži u ulici X dana 26.10.2026. BVK navodi da će kvar biti otklonjen do 15:00."
    assert mm._append_time_window(msg, "07:00 - 15:00") == msg
    assert mm._append_time_window("Kvar u ulici X.", "07:00 - 15:00") == "Kvar u ulici X, u periodu 07:00 - 15:00."


def test_feed_draws_the_ring_on_the_fault_pin():
    from scripts.build_web_data import extract_shapes_labelled
    fc = {"type": "FeatureCollection", "features": mm._nearby_features([("Omladinskih Brigada 96", *FAULT)], 200)}
    _, areas, _ = extract_shapes_labelled(json.dumps(fc))
    assert areas == [{"pin": [pytest.approx(FAULT[0]), pytest.approx(FAULT[1])],
                      "label": "Kvar: Omladinskih Brigada 96 i okolne ulice", "ring": 200}]
