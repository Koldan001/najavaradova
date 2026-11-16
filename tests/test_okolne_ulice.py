""""i okolne ulice" from any source, and one notice naming places far apart (26.10.2026).

A named street: drawn solid, the side streets touching it dashed ("maybe"), and the people on
those side streets told it MAY reach them. A named house: a pin and a circle, like BVK's faults.
A notice whose places lie more than a kilometre apart: one card per place on the map's list.
RGZ is stubbed."""
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
from scripts import build_web_data as bwd

NAMED = (45.3300, 19.8600)            # a street in Stepanovićevo
SIDE = (45.3310, 19.8610)             # a side street touching it


def _alert(**loc):
    seg = dict(location_type="STREET", main_street="БИЋЕ КЕСИЋА", settlement="СТЕПАНОВИЋЕВО",
               includes_surrounding_streets=True, time_window="08:00 - 14:00")
    seg.update(loc)
    return OutageAlert(city="НОВИ САД", outage_date="10.09.2026", message_template="x",
                       affected_locations=[StreetSegment(**seg)])


@pytest.fixture()
def db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE targets (id INTEGER PRIMARY KEY, ulica TEXT, broj TEXT, telegram_chat_id TEXT, email TEXT,
                              email_active INTEGER DEFAULT 1, city TEXT, opstina TEXT, naselje TEXT,
                              lat REAL, lng REAL, is_active INTEGER DEFAULT 1);
        CREATE TABLE target_hexes (target_id INTEGER, hex_id TEXT);
        CREATE TABLE outage_hexes (outage_id INTEGER, hex_id TEXT);
        CREATE TABLE message_queue (id INTEGER PRIMARY KEY, outage_id INTEGER, target_id INTEGER, channel TEXT,
                                    custom_message TEXT, status TEXT, created_at TEXT);
        CREATE TABLE send_checks (outage_id INTEGER, reason TEXT, status TEXT);
    """)

    def add(tid, street, point):
        conn.execute("INSERT INTO targets (id, ulica, broj, email, city) VALUES (?, ?, '5', 'a@b.c', 'Stepanovićevo')",
                     (tid, street))
        conn.execute("INSERT INTO target_hexes VALUES (?, ?)", (tid, h3.latlng_to_cell(*point, 9)))
    return conn, add


def _stub_side_streets(monkeypatch, zone):
    side = {"type": "Feature", "geometry": {"type": "LineString", "coordinates": [[SIDE[1], SIDE[0]], [SIDE[1] + .001, SIDE[0]]]},
            "properties": {"kind": "street", "street": "Војводе Путника", "relation": "surrounding"}}
    monkeypatch.setattr(mm, "_surrounding_streets", lambda *a, **k: (set(zone), [side]))
    monkeypatch.setattr(mm, "rgz_house_points", lambda *a, **k: [])


def test_side_street_people_hear_it_may_reach_them(db, monkeypatch):
    conn, add = db
    side_hex, far_hex = h3.latlng_to_cell(*SIDE, 9), h3.latlng_to_cell(45.40, 19.90, 9)
    _stub_side_streets(monkeypatch, {side_hex})
    conn.execute("INSERT INTO outage_hexes VALUES (1, ?)", (side_hex,))
    add(1, "Vojvode Putnika", SIDE)       # on a side street: told "moguć"
    add(2, "Bića Kesića", SIDE)           # on the named street: the normal match decides, not this
    add(3, "Ive Lole Ribara", SIDE)       # in the zone, but not a street touching the named one
    add(4, "Vojvode Putnika", (45.40, 19.90))   # the same side street, far outside the stretch
    conn.execute("INSERT INTO target_hexes VALUES (4, ?)", (far_hex,))
    assert mm._queue_side_streets(conn, 1, _alert(), "НОВИ САД", "WATER") == 1
    row, = conn.execute("SELECT target_id, custom_message, status FROM message_queue").fetchall()
    assert row["target_id"] == 1 and row["status"] == "QUEUED"
    assert row["custom_message"] == (
        "💧 Moguć prekid vode u blizini. Najavljen je prekid vode u ulici Biće Kesića i okolnim ulicama, "
        "dana 10.09.2026, u periodu 08:00 - 14:00. Tvoja ulica Vojvode Putnika je jedna od okolnih, "
        "pa je moguće da i ti ostaneš bez vode.")
    # A second read of the same notice tells nobody twice.
    assert mm._queue_side_streets(conn, 1, _alert(), "НОВИ САД", "WATER") == 0


def test_side_streets_need_the_outages_own_hexes(db, monkeypatch):
    # The leak gate kept a hex out of outage_hexes: nobody there is told, whatever the zone says.
    conn, add = db
    _stub_side_streets(monkeypatch, {h3.latlng_to_cell(*SIDE, 9)})
    add(1, "Vojvode Putnika", SIDE)
    assert mm._queue_side_streets(conn, 1, _alert(), "НОВИ САД", "WATER") == 0


def test_power_wording():
    msg = mm._possible_message(_alert(), _alert().affected_locations[0], "POWER", "To je oko 150 m od tvoje adrese")
    assert msg.startswith("⚡ Moguć nestanak struje u blizini. Najavljeno je isključenje struje u ulici")
    assert msg.endswith("To je oko 150 m od tvoje adrese, pa je moguće da i ti ostaneš bez struje.")


def test_a_named_house_with_okolne_ulice_is_a_ring_not_its_whole_street(monkeypatch):
    # Јурија Гагарина 253 и околне улице drew the whole street and 20 streets touching it.
    monkeypatch.setattr(mm, "rgz_house_points", lambda *a, **k: [("253", *NAMED)])
    alert = _alert(main_street="ЈУРИЈА ГАГАРИНА", explicit_numbers="253", settlement=None)
    (label, lat, lon), = mm._fault_centres(alert, "НОВИ БЕОГРАД", widened_only=True)
    assert label == "Jurija Gagarina 253" and (lat, lon) == pytest.approx(NAMED)
    assert mm._side_street_locations(alert, "НОВИ БЕОГРАД") == []
    assert mm._ring_message(alert, "WATER")(label, 140) == (
        "💧 Moguć prekid vode u blizini. Najavljen je prekid vode u ulici Jurija Gagarina 253 i okolnim "
        "ulicama, dana 10.09.2026, u periodu 08:00 - 14:00. To je oko 150 m od tvoje adrese, "
        "pa je moguće da i ti ostaneš bez vode.")
    feat, = mm._nearby_features([(label, lat, lon)], mm.NEARBY_M, prefix="")
    assert feat["properties"]["label"] == "Jurija Gagarina 253 i okolne ulice"


def test_only_houses_named_with_okolne_ulice_get_a_ring_outside_bvk(monkeypatch):
    monkeypatch.setattr(mm, "rgz_house_points", lambda *a, **k: [("12", *NAMED)])
    alert = _alert(explicit_numbers="12", includes_surrounding_streets=False)
    assert mm._fault_centres(alert, "НОВИ САД", widened_only=True) == []
    assert len(mm._fault_centres(alert, "НОВИ САД")) == 1     # BVK: every named house


@pytest.mark.parametrize("text, kept", [
    ("Без воде ће бити Подгоричка и околне улице.", True),
    ("прекид у Тоше Јовановића и суседним улицама", True),
    ("Bez vode Balzakova i okolne ulice", True),
    ("СУБОТИЦА | 08:30-12:00 | Лује Амстронга, Ролана Ромена, Ускочка", False),
])
def test_okolne_ulice_only_when_the_notice_says_it(text, kept):
    alert = _alert()
    mm._drop_invented_surroundings(alert, text)
    assert alert.affected_locations[0].includes_surrounding_streets is kept


# --- the feed -----------------------------------------------------------------------------------

def _line(lat, lng, street, **props):
    return {"type": "Feature", "geometry": {"type": "LineString", "coordinates": [[lng, lat], [lng + .002, lat]]},
            "properties": {"kind": "street", "street": street, **props}}


def _parsed(*streets):
    return json.dumps({"affected_locations": [{"location_type": "STREET", "main_street": s} for s in streets]})


def test_side_streets_are_marked_maybe_and_belong_to_the_named_street():
    fc = {"type": "FeatureCollection", "features": [
        _line(*NAMED, "Biće Kesića"),
        _line(*SIDE, "Vojvode Putnika", street_of="Biće Kesića", settlement="Stepanovićevo")]}
    ex = bwd._extract(json.dumps(fc), _parsed("БИЋЕ КЕСИЋА"))
    assert ex["maybe"] == [False, True]
    assert ex["line_locs"] == [{0}, {0}]
    assert ex["labels"] == ["Biće Kesića", "Vojvode Putnika"]


def test_places_far_apart_are_separate_parts():
    # Two streets in town 300 m apart stay together; a village 8 km away is its own part.
    fc = {"type": "FeatureCollection", "features": [
        _line(44.8000, 20.4000, "Pašmanska"),
        _line(44.8027, 20.4000, "Đurđevdanska"),
        _line(44.8700, 20.4600, "Glavna")]}
    parsed = _parsed("ПАШМАНСКА", "ЂУРЂЕВДАНСКА", "ГЛАВНА")
    parts = bwd.split_parts(bwd._extract(json.dumps(fc), parsed))
    assert [p["locs"] for p in parts] == [{0, 1}, {2}]
    assert bwd.part_streets(parts[0]["locs"], parsed) == ("Pašmanska, Đurđevdanska", ["Pašmanska", "Đurđevdanska"])
    assert bwd.part_streets(parts[1]["locs"], parsed) == ("Glavna", ["Glavna"])


def test_a_piece_the_parse_cant_place_keeps_the_notice_whole():
    fc = {"type": "FeatureCollection", "features": [
        _line(44.8000, 20.4000, "Pašmanska"),
        _line(44.8700, 20.4600, "Nepoznata")]}
    parts = bwd.split_parts(bwd._extract(json.dumps(fc), _parsed("ПАШМАНСКА")))
    assert len(parts) == 1 and len(parts[0]["lines"]) == 2


def test_one_location_drawn_in_pieces_far_apart_stays_one_place():
    road = {"type": "Feature", "properties": {"kind": "street", "street": "Jasički put"},
            "geometry": {"type": "MultiLineString", "coordinates": [[[20.4000, 44.8000], [20.4020, 44.8000]],
                                                                       [[20.4000, 44.8500], [20.4020, 44.8500]]]}}
    fc = {"type": "FeatureCollection", "features": [road]}
    assert len(bwd.split_parts(bwd._extract(json.dumps(fc), _parsed("ЈАСИЧКИ ПУТ")))) == 1
