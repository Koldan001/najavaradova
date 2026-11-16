"""A street whose announcement names house numbers draws only where those houses stand - the
stretch from the first to the last, or a pin for one house - placed with RGZ's address points.
The map only: matching stays on the whole street. RGZ is stubbed (CI has no rgz.db)."""
import json
import math
import os
import sys

import pytest

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
os.environ.setdefault("GEMINI_API_KEY", "test-placeholder")

import core.matchmaker as mm
from parser import OutageAlert, StreetSegment
from spatial.spatial_engine import house_number_stretch

LAT, LON = 45.25, 19.84
M_LON = 1 / (111320 * math.cos(math.radians(LAT)))   # degrees of longitude per metre
M_LAT = 1 / 111320


def _at(metres_east, metres_north=20):
    """(lat, lon) of a house `metres_east` along the test street, set back from it."""
    return LAT + metres_north * M_LAT, LON + metres_east * M_LON


def _street(*parts):
    """A straight west-east street; each part is (from, to) in metres east."""
    lines = [[[LON + a * M_LON, LAT], [LON + b * M_LON, LAT]] for a, b in parts]
    geom = ({"type": "LineString", "coordinates": lines[0]} if len(lines) == 1
            else {"type": "MultiLineString", "coordinates": lines})
    return {"type": "Feature", "geometry": geom, "properties": {"street": "SRBOBRANSKA"}}


def _span_m(geom):
    xs = [pt[0] for pt in geom["coordinates"]]
    return (min(xs) - LON) / M_LON, (max(xs) - LON) / M_LON


def test_a_range_draws_from_the_first_house_to_the_last():
    out = house_number_stretch(_street((0, 1000)), [_at(200), _at(300), _at(400)])
    assert out["geometry"]["type"] == "LineString"
    start, end = _span_m(out["geometry"])
    assert start == pytest.approx(185, abs=1) and end == pytest.approx(415, abs=1)
    assert out["properties"] == {"street": "SRBOBRANSKA"}


def test_one_house_or_houses_side_by_side_draw_as_a_pin_on_them():
    out = house_number_stretch(_street((0, 1000)), [_at(500)])
    assert out["geometry"] == {"type": "Point", "coordinates": [pytest.approx(_at(500)[1]),
                                                               pytest.approx(_at(500)[0])]}
    out = house_number_stretch(_street((0, 1000)), [_at(500), _at(510, -20)])
    assert out["geometry"]["type"] == "Point"


def test_a_stretch_never_runs_past_the_street_ends():
    start, end = _span_m(house_number_stretch(_street((0, 1000)), [_at(5), _at(995)])["geometry"])
    assert start == pytest.approx(0, abs=1) and end == pytest.approx(1000, abs=1)


def test_houses_on_two_pieces_of_a_street_draw_on_both():
    out = house_number_stretch(_street((0, 400), (600, 1000)), [_at(100), _at(200), _at(700), _at(800)])
    assert out["geometry"]["type"] == "MultiLineString"
    assert len(out["geometry"]["coordinates"]) == 2


def test_houses_far_from_the_line_are_not_placed():
    assert house_number_stretch(_street((0, 1000)), [_at(300, 400)]) is None
    out = house_number_stretch(_street((0, 1000)), [_at(200), _at(300), _at(900, 400)])
    assert _span_m(out["geometry"])[1] == pytest.approx(315, abs=1)


def test_a_house_set_back_from_a_boulevard_is_still_a_pin():
    # Bulevar Zorana Đinđića 54, Novi Beograd: its RGZ point sits 153 m off the drawn line.
    out = house_number_stretch(_street((0, 1000)), [_at(500, 153)])
    assert out["geometry"] == {"type": "Point", "coordinates": [pytest.approx(_at(500, 153)[1]),
                                                               pytest.approx(_at(500, 153)[0])]}


def test_nothing_to_place_on():
    assert house_number_stretch(_street((0, 1000)), []) is None
    assert house_number_stretch({"geometry": {"type": "Point", "coordinates": [LON, LAT]}}, [_at(1)]) is None
    assert house_number_stretch(None, [_at(1)]) is None


# --- which houses: core/matchmaker.py's _house_number_feature ---------------------------------

# Srbobranska, houses 1-40 every 20 m, odd numbers across the street; plus two without a number.
HOUSES = [(str(n), *_at(20 * n, 20 if n % 2 else -20)) for n in range(1, 41)] + [
    ("BB", *_at(900)), ("bb", *_at(10))]


@pytest.fixture()
def rgz(monkeypatch):
    calls = []

    def fake(street, settlement=None, parent_region=None):
        calls.append((street, settlement, parent_region))
        return HOUSES
    monkeypatch.setattr(mm, "rgz_house_points", fake)
    return calls


@pytest.fixture()
def no_gate(monkeypatch):
    """The leak gate's area needs rgz.db (tests/test_leak_gate.py covers it); not under test here."""
    import scripts.redraw_house_numbers as rhn
    monkeypatch.setattr(mm, "_location_envelope", lambda *a, **k: None)
    monkeypatch.setattr(mm, "region_opstine", lambda *a, **k: None)
    monkeypatch.setattr(rhn, "_location_envelope", lambda *a, **k: None)
    monkeypatch.setattr(rhn, "region_opstine", lambda *a, **k: None)


def _loc(**kw):
    return StreetSegment(location_type="STREET", main_street="СРБОБРАНСКА", time_window="08:00 - 14:00", **kw)


def test_range_on_one_side_draws_only_those_houses(rgz):
    out = mm._house_number_feature(_street((0, 1000)), _loc(number_range="14-32", parity="PARNA"), "НОВИ САД")
    start, end = _span_m(out["geometry"])
    assert start == pytest.approx(280 - 15, abs=1) and end == pytest.approx(640 + 15, abs=1)
    assert rgz == [("СРБОБРАНСКА", None, "НОВИ САД")]


def test_one_named_house_is_a_pin(rgz):
    out = mm._house_number_feature(_street((0, 1000)), _loc(explicit_numbers="7"), "НОВИ САД")
    assert out["geometry"]["type"] == "Point"
    assert out["geometry"]["coordinates"][0] == pytest.approx(LON + 140 * M_LON)


def test_the_whole_street_stays_when_the_houses_cant_be_placed(rgz, monkeypatch):
    street = _street((0, 1000))
    assert mm._house_number_feature(street, _loc(), "НОВИ САД") is street                 # no numbers
    assert mm._house_number_feature(street, _loc(parity="PARNA"), "НОВИ САД") is street   # a side only
    assert mm._house_number_feature(street, _loc(number_range="200-300"), "НОВИ САД") is street  # not in RGZ
    assert mm._house_number_feature(None, _loc(number_range="1-5"), "НОВИ САД") is None
    monkeypatch.setattr(mm, "rgz_house_points", lambda *a, **k: [])                    # OSM-drawn street
    assert mm._house_number_feature(street, _loc(number_range="1-5"), "НОВИ САД") is street

    def broken(*a, **k):
        raise RuntimeError("rgz.db locked")
    monkeypatch.setattr(mm, "rgz_house_points", broken)
    assert mm._house_number_feature(street, _loc(number_range="1-5"), "НОВИ САД") is street


def test_the_map_draws_the_stretch_but_a_widened_location_keeps_its_street(rgz, no_gate, monkeypatch):
    monkeypatch.setattr(mm, "get_street_geojson_feature", lambda *a, **k: _street((0, 1000)))
    monkeypatch.setattr(mm, "_surrounding_streets", lambda *a, **k: (set(), []))
    alert = OutageAlert(city="НОВИ САД", outage_date="28.09.2026", message_template="x",
                        affected_locations=[_loc(number_range="14-32", parity="PARNA")])
    (feat,) = mm._build_visual_features(alert, "НОВИ САД")
    assert _span_m(feat["geometry"]) == (pytest.approx(265, abs=1), pytest.approx(655, abs=1))
    assert feat["properties"]["kind"] == "street"

    alert.affected_locations[0].includes_surrounding_streets = True
    (feat,) = mm._build_visual_features(alert, "НОВИ САД")
    assert _span_m(feat["geometry"]) == (pytest.approx(0, abs=1), pytest.approx(1000, abs=1))


# --- the feed and the redraw of what is already stored ----------------------------------------

def test_the_feed_carries_a_house_as_a_pin():
    from scripts.build_web_data import extract_shapes
    pin = {"type": "Feature", "properties": {"kind": "street"},
           "geometry": {"type": "Point", "coordinates": [19.84, 45.25]}}
    lines, areas = extract_shapes(json.dumps({"type": "FeatureCollection",
                                              "features": [pin, pin, _street((0, 100))]}))
    assert areas == [{"pin": [45.25, 19.84]}]
    assert len(lines) == 1


def test_redraw_changes_only_numbered_streets_and_only_once(rgz, no_gate):
    from scripts.redraw_house_numbers import redraw
    numbered, whole = _street((0, 1000)), _street((0, 1000))
    for f in (numbered, whole):
        f["properties"]["kind"] = "street"
    whole["properties"]["street"] = "FUTOŠKA"
    alert = OutageAlert(city="НОВИ САД", outage_date="28.09.2026", message_template="x",
                        affected_locations=[_loc(number_range="14-32", parity="PARNA"),
                                            StreetSegment(location_type="STREET", main_street="ФУТОШКА",
                                                          time_window="08:00 - 14:00")])
    fc = {"type": "FeatureCollection", "features": [numbered, whole]}
    new_fc, changes = redraw(fc, alert)
    assert [c[0] for c in changes] == ["SRBOBRANSKA"]
    assert changes[0][1] == pytest.approx(1.0, abs=.01) and changes[0][2] == pytest.approx(.39, abs=.01)
    assert new_fc["features"][1] is whole
    assert redraw(new_fc, alert)[1] == []


# --- the map popup names the piece that was clicked -------------------------------------------

def test_each_drawn_piece_carries_its_own_address():
    from scripts.build_web_data import extract_shapes_labelled

    def street(name, **props):
        f = _street(props.pop("at"))
        f["properties"] = {"street": name, "kind": "street", **props}
        return f
    rankeova = {"type": "Feature", "properties": {"street": "RANKEOVA", "kind": "street"},
                "geometry": {"type": "Point", "coordinates": [19.84, 45.25]}}
    side = _street((200, 300))
    side["properties"] = {"street": "SAZONOVA", "street_of": "Skerlićeva"}
    area = _street((500, 600))
    area["properties"] = {"street": "X", "area_type": "settlement", "settlement": "Vračar"}
    parsed = json.dumps({"affected_locations": [
        {"main_street": "НЕБОЈШИНА", "number_range": "27-31"},
        {"main_street": "РАНКЕОВА", "explicit_numbers": "2, 3"},
        {"main_street": "СКЕРЛИЋЕВА", "number_range": "2-12А", "parity": "PARNA"},
        {"main_street": "СКЕРЛИЋЕВА"},
    ]})
    features = [street("NEBOJŠINA", at=(0, 100)), rankeova, street("SKERLIĆEVA", at=(100, 200)), side, area]
    fc = json.dumps({"type": "FeatureCollection", "features": features})
    lines, areas, labels = extract_shapes_labelled(fc, parsed)
    assert labels == ["Nebojšina 27-31", "Skerlićeva 2-12A (parni)", "Sazonova", ""]
    assert len(lines) == len(labels)
    assert {"pin": [45.25, 19.84], "label": "Rankeova 2, 3"} in areas

    # Skerlićeva's second location drew the same piece again (its houses couldn't be placed).
    features.append(street("SKERLIĆEVA", at=(100, 200)))
    fc = json.dumps({"type": "FeatureCollection", "features": features})
    assert extract_shapes_labelled(fc, parsed)[2][1] == "Skerlićeva 2-12A (parni) · Skerlićeva"
    assert extract_shapes_labelled(fc)[2][:3] == ["Nebojšina", "Skerlićeva", "Sazonova"]   # no parse


def test_the_feed_ships_each_label_once():
    from scripts.build_web_data import _index_labels
    assert _index_labels(["Cankareva", "Cankareva", "", "Nebojšina 27-31", "Cankareva"]) == (
        ["Cankareva", "", "Nebojšina 27-31"], [0, 0, 1, 2, 0])
    assert _index_labels(["", ""]) == ([], [])
    assert _index_labels([]) == ([], [])


def test_a_kvart_label_rgz_doesnt_know_falls_back_to_the_opstina(monkeypatch):
    # "Јурија Гагарина 253 ... – Бежанијска коса" (BVK, 26.10.2026): RGZ files the house under
    # Beograd, not the kvart, and the map drew the whole street with every block driveway.
    calls = []

    def fake(street, settlement=None, parent_region=None):
        calls.append(settlement)
        return [] if settlement else HOUSES
    monkeypatch.setattr(mm, "rgz_house_points", fake)
    out = mm._house_number_feature(_street((0, 1000)), _loc(explicit_numbers="7", settlement="БЕЖАНИЈСКА КОСА"),
                                   "НОВИ БЕОГРАД")
    assert out["geometry"]["type"] == "Point"
    assert calls == ["БЕЖАНИЈСКА КОСА", None]
